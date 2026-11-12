"""Сопоставление параметров модели с требуемыми параметрами ЦИМ.

Для каждого набора требований (IFC-класс + код требований) строится карта
«требуемый параметр → колонка сырого дампа IFC + преобразование». Карту
предлагает LLM, а детерминированные правила используются как fallback и для
проверки ответа модели.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core.config import load_config
from src.core.logger import setup_logger

from .geometry import GEOMETRY_ATTRIBUTES, resolve_geometry
from .llm_client import CheckModelLLM, CheckModelLLMError
from .requirements import (
    RequiredAttribute,
    Requirement,
    SERVICE_COLUMNS,
    extract_concrete_grade,
    extract_freeze_durability,
    extract_steel_grade,
    extract_water_resist,
    first_number,
    storey_parts,
)

logger = setup_logger(__name__)

# Допустимые преобразования значения.
TRANSFORM_NONE = "none"
TRANSFORM_MM_TO_M = "mm_to_m"
TRANSFORM_MM2_TO_M2 = "mm2_to_m2"
TRANSFORM_MM3_TO_M3 = "mm3_to_m3"
TRANSFORM_STOREY_SUB = "storey_sub"
TRANSFORM_STOREY_LEVEL = "storey_level"
TRANSFORM_CONCRETE_GRADE = "extract_concrete_grade"
TRANSFORM_STEEL_GRADE = "extract_steel_grade"
TRANSFORM_WATER_RESIST = "extract_water_resist"
TRANSFORM_FREEZE_DURABILITY = "extract_freeze_durability"
TRANSFORM_FIRST_NUMBER = "first_number"

ALLOWED_TRANSFORMS = {
    TRANSFORM_NONE,
    TRANSFORM_MM_TO_M,
    TRANSFORM_MM2_TO_M2,
    TRANSFORM_MM3_TO_M3,
    TRANSFORM_STOREY_SUB,
    TRANSFORM_STOREY_LEVEL,
    TRANSFORM_CONCRETE_GRADE,
    TRANSFORM_STEEL_GRADE,
    TRANSFORM_WATER_RESIST,
    TRANSFORM_FREEZE_DURABILITY,
    TRANSFORM_FIRST_NUMBER,
}

# Марки бетона по водонепроницаемости/морозостойкости всегда извлекаются из
# значения: в моделях они часто записаны вместе с другими данными
# («B30 W6 F150»), а в требовании ожидается «W6» / «F150».
_GRADE_TRANSFORMS = {
    "RUS_WaterResist": TRANSFORM_WATER_RESIST,
    "RUS_FreezeDurability": TRANSFORM_FREEZE_DURABILITY,
}

# Объёмные параметры: значение из QTO проверяется на согласованность с
# геометрической оценкой (в выгрузках Revit объём плит иногда записан в литрах).
_VOLUME_ATTRIBUTES = {"RUS_Volume", "RUS_GrossVolume"}
# Допустимое расхождение QTO-объёма и геометрической оценки (разы).
_VOLUME_TOLERANCE = 100.0

# Колонки, которые не несёт смысла показывать LLM (слишком длинные/служебные).
_NOISY_COLUMNS = {
    "OwnerHistory",
    "ObjectPlacement",
    "Representation",
    "GlobalId",
    "Этаж_IFC_ID",
    "Здание_IFC_ID",
    "Версия_высот_IFC",
    "Источник_отметки_земли",
    "Источник_отметок_работ",
    "QTO_geom::Метод",
    "Количество::Источник_объёма",
    "Количество::Статус_объёма",
}

# Версия промпта и правил разбора ответа: входит в ключ кэша, чтобы правки
# инструкции не давали результаты, построенные по старым правилам.
PROMPT_VERSION = 4

# Поля элемента, которые показываются LLM как контекст (не как источники).
_CONTEXT_COLUMNS = (
    "IfcClass",
    "Name",
    "ObjectType",
    "Description",
    "PredefinedType",
    "Tag",
    "Материал",
    "Этаж",
)

_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class MappingEntry:
    """Правило получения значения требуемого параметра."""

    source: Optional[str] = None
    transform: str = TRANSFORM_NONE

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "transform": self.transform}


@dataclass
class MappingResult:
    """Результат построения карты параметров."""

    mapping: Dict[str, MappingEntry]
    candidates: Dict[str, List[str]]
    llm_used: bool = False
    llm_error: Optional[str] = None
    from_cache: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mapping": {
                name: entry.as_dict() for name, entry in self.mapping.items()
            },
            "llm_used": self.llm_used,
            "llm_error": self.llm_error,
            "from_cache": self.from_cache,
        }


# ---------------------------------------------------------------------
#  Кандидаты: колонки модели и примеры значений
# ---------------------------------------------------------------------

def candidate_columns(
    rows: Iterable[Dict[str, Any]],
    *,
    exclude: Iterable[str] = (),
    sample_limit: int = 3,
    max_value_length: int = 160,
    scan_limit: Optional[int] = None,
) -> Dict[str, List[str]]:
    """Собрать колонки модели и примеры значений для промпта LLM.

    Args:
        rows: строки сырого дампа IFC одного IFC-класса.
        exclude: дополнительные имена колонок, которые нужно исключить.
        sample_limit: сколько примеров значения показывать на колонку.
        max_value_length: максимальная длина одного примера.
        scan_limit: сколько первых строк просматривать для примеров
            (``None`` — все строки класса).

    Returns:
        Словарь ``{колонка: [примеры значений]}``.
    """
    excluded = set(SERVICE_COLUMNS) | _NOISY_COLUMNS | set(exclude)
    candidates: Dict[str, List[str]] = {}

    rows_list = list(rows)
    if scan_limit is not None:
        rows_list = rows_list[:scan_limit]

    for row in rows_list:
        for key, value in row.items():
            if key in excluded or str(key).startswith("RUS_"):
                continue
            samples = candidates.setdefault(str(key), [])
            if value in (None, ""):
                continue
            text = str(value).strip()
            if not text or len(samples) >= sample_limit:
                continue
            text = text[:max_value_length]
            if text not in samples:
                samples.append(text)

    # Оставляем только колонки, у которых есть хотя бы один пример значения.
    return {key: samples for key, samples in candidates.items() if samples}


def _column_priority(column: str) -> int:
    """Приоритет колонки при равном совпадении с шаблоном (меньше — лучше)."""
    if column.startswith("QTO::"):
        return 0
    if column.startswith("Свойство::"):
        return 1
    if column.startswith("Количество::"):
        return 2
    if column.startswith("QTO_geom::"):
        return 3
    if column.startswith("QTO_bbox::"):
        return 4
    return 5


def _pick_column(
    candidates: Iterable[str],
    patterns: Iterable[str],
    *,
    preferred: Iterable[str] = (),
) -> Optional[str]:
    """Выбрать первую подходящую колонку по списку regex-шаблонов."""
    columns = list(candidates)
    preferred_list = [item.lower() for item in preferred]
    for pattern in patterns:
        matches = [column for column in columns if re.search(pattern, column, re.I)]
        if not matches:
            continue
        matches.sort(
            key=lambda column: (
                0
                if any(item in column.lower() for item in preferred_list)
                else 1,
                _column_priority(column),
                len(column),
            )
        )
        return matches[0]
    return None


def _entry(source: Optional[str], transform: str = TRANSFORM_NONE) -> MappingEntry:
    return MappingEntry(source=source, transform=transform)


# ---------------------------------------------------------------------
#  Детерминированное сопоставление (fallback)
# ---------------------------------------------------------------------

def deterministic_mapping(
    requirement: Requirement,
    candidates: Iterable[str],
) -> Dict[str, MappingEntry]:
    """Построить карту параметров без LLM по именам колонок.

    Используется как fallback, если LLM недоступна, а также как база,
    поверх которой применяются валидные ответы LLM.
    """
    columns = list(candidates)
    ifc_class = requirement.ifc_class
    mapping: Dict[str, MappingEntry] = {}

    def put(name: str, patterns: Iterable[str], transform: str = TRANSFORM_NONE,
            preferred: Iterable[str] = ()) -> None:
        source = _pick_column(columns, patterns, preferred=preferred)
        mapping[name] = _entry(source, transform)

    # Общие данные / маркировка.
    put("RUS_MSSK_Element_Code", [r"::MGE_ElementCode$"])
    put("RUS_MSSK_Material_Code", [r"::MGE_MaterialCode$"])
    put("RUS_Name", [r"::MGE_Name$", r"^Name$"])
    put("RUS_MSSK_Element_Name", [r"::MGE_Name$", r"^Name$"])
    put("RUS_MSSK_Material_Name", [r"::MGE_Material$", r"^Материал$"])
    put("RUS_Standard", [r"::MGE_Gost$", r"::Gost$", r"ГОСТ"])
    put("RUS_Position", [r"::MGE_Position$", r"Марка"])
    put("RUS_StrengthClass", [r"::MGE_ConcreteGrade$"])
    put("RUS_SteelGrade", [r"::MGE_ReinforceStrengthClass$"])
    put("RUS_ReinforcementVolumeRatio", [r"::ReinforcementVolumeRatio$"])
    put(
        "RUS_WaterResist",
        [r"::MGE_WaterResist$", r"WaterResist", r"Водонепрониц"],
        TRANSFORM_WATER_RESIST,
    )
    put(
        "RUS_FreezeDurability",
        [r"::MGE_FreezeDurability$", r"FreezeDurability", r"Морозостой"],
        TRANSFORM_FREEZE_DURABILITY,
    )

    # Местоположение.
    put("RUS_Level", [r"^Этаж$"], TRANSFORM_STOREY_LEVEL)
    put("RUS_Object_Sub", [r"^Этаж$"], TRANSFORM_STOREY_SUB)
    mapping.setdefault("RUS_Object", _entry(None))

    # Количественные параметры.
    put("RUS_Length", [r"Qto_.*::Length$", r"::Length$", r"Длина"])
    put("RUS_Width", [r"Qto_.*::Width$", r"::Width$", r"Ширина"])
    put("RUS_Height", [r"Qto_.*::Height$", r"::Height$", r"Высота"])
    put("RUS_Diameter", [r"::Diameter$", r"Диаметр"])
    put("RUS_Perimeter", [r"Qto_.*::Perimeter$", r"::Perimeter$", r"Периметр"])
    put("RUS_PitchAngle", [r"::PitchAngle$", r"::Slope$", r"Уклон", r"Угол"])
    put("RUS_Slope", [r"::Slope$", r"::PitchAngle$", r"Уклон", r"Угол"])
    put("RUS_RequiredSlope", [r"::Slope$", r"::PitchAngle$", r"Уклон", r"Угол"])
    put("RUS_Area", [r"::NetArea$", r"::GrossArea$", r"::NetSideArea$",
                     r"::NetFootprintArea$", r"Площадь"])
    put("RUS_NetFootprintArea", [r"::NetFootprintArea$", r"::GrossFootprintArea$"])
    put("RUS_NetSideArea", [r"::NetSideArea$", r"::GrossSideArea$"])
    put("RUS_NetSurfaceArea", [r"::NetSurfaceArea$", r"::GrossSurfaceArea$",
                               r"Площадь_поверхности"])
    put("RUS_Volume", [r"::NetVolume$", r"Количество::Объём_м3", r"::GrossVolume$",
                       r"QTO_geom::Объём_м3", r"QTO_bbox::Объём_м3"])
    put("RUS_GrossVolume", [r"::GrossVolume$", r"QTO_bbox::Габаритный_объём_м3"])
    put("RUS_Weight", [r"::Weight$", r"Weight$", r"Масса"])

    # Толщина зависит от класса элемента.
    if ifc_class in ("IfcWall", "IfcWallStandardCase"):
        put("RUS_Thickness", [r"Qto_WallBaseQuantities::Width$", r"Толщина",
                              r"::Width$", r"::Depth$"])
    elif ifc_class in ("IfcSlab", "IfcPlate"):
        put("RUS_Thickness", [r"Qto_(?:Slab|Plate)BaseQuantities::Depth$",
                              r"Толщина", r"::Depth$"])
    else:
        put("RUS_Thickness", [r"::Depth$", r"Толщина", r"::Width$"])

    # Для колонн высота часто хранится в Length (длина колонны).
    if ifc_class == "IfcColumn":
        if mapping.get("RUS_Height", _entry(None)).source is None:
            source = _pick_column(columns, [r"Qto_.*::Length$", r"Длина"])
            if source:
                mapping["RUS_Height"] = _entry(source)
    if ifc_class in ("IfcBeam", "IfcMember"):
        # Ширина/высота сечения — габариты bbox (Length — это длина элемента,
        # её нельзя подставлять в высоту сечения).
        if mapping.get("RUS_Width", _entry(None)).source is None:
            source = _pick_column(columns, [r"QTO_bbox::Ширина_мм$", r"Ширина"])
            if source:
                mapping["RUS_Width"] = _entry(source)
        if mapping.get("RUS_Height", _entry(None)).source is None:
            source = _pick_column(columns, [r"QTO_bbox::Высота_мм$", r"Высота"])
            if source:
                mapping["RUS_Height"] = _entry(source)

    # В карте оставляем только параметры, требуемые этим набором требований.
    allowed = set(requirement.attribute_names)
    return {name: entry for name, entry in mapping.items() if name in allowed}


# ---------------------------------------------------------------------
#  LLM-сопоставление
# ---------------------------------------------------------------------

def _mapping_cache_path() -> Path:
    configured = os.getenv("CHECK_MODEL_CACHE_DIR")
    if configured:
        base = Path(configured)
    else:
        base = Path(load_config().CACHE_PATH) / "check_model"
    return base / "mapping_cache.json"


def _cache_key(
    requirement: Requirement,
    candidates: Dict[str, List[str]],
    model: str,
) -> str:
    payload = {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "code": requirement.code,
        "ifc_class": requirement.ifc_class,
        "columns": sorted(candidates),
        "attributes": requirement.attribute_names,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_cache() -> Dict[str, Any]:
    path = _mapping_cache_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    path = _mapping_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(cache, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except OSError as exc:
        logger.warning("Не удалось сохранить кэш сопоставления ЦИМ: %s", exc)


def _describe_sample_row(row: Optional[Dict[str, Any]]) -> str:
    """Краткое описание элемента для контекста промпта."""
    if not row:
        return ""
    lines = []
    for column in _CONTEXT_COLUMNS:
        value = row.get(column)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if not text:
            continue
        lines.append(f"- {column}: {text[:200]}")
    return "\n".join(lines)


def _build_prompt(
    requirement: Requirement,
    candidates: Dict[str, List[str]],
    sample_row: Optional[Dict[str, Any]] = None,
) -> str:
    attributes = []
    for attr in requirement.attributes:
        unit = attr.unit or "без единицы"
        attributes.append(
            f"- {attr.name} — {attr.characteristic}; тип: {attr.data_type}; "
            f"ед.: {unit}; пример: {attr.example or '—'}"
        )

    columns = []
    for column, samples in candidates.items():
        example = "; ".join(samples) if samples else "—"
        columns.append(f"- {column} — {example}")

    context = _describe_sample_row(sample_row)
    context_block = (
        "\n\nПРИМЕР ЭЛЕМЕНТА ЭТОГО КЛАССА ИЗ МОДЕЛИ:\n" + context
        if context
        else ""
    )

    return (
        "Ты инженер по цифровым информационным моделям (ЦИМ) и формату IFC.\n"
        f"Требования к параметрам элемента IFC класса {requirement.ifc_class} "
        f"({requirement.element}), набор {requirement.code}.\n\n"
        "ТРЕБУЕМЫЕ ПАРАМЕТРЫ (имя — характеристика; тип; единица; пример):\n"
        + "\n".join(attributes)
        + context_block
        + "\n\nДОСТУПНЫЕ ПАРАМЕТРЫ ЭЛЕМЕНТА В МОДЕЛИ (колонка — примеры значений):\n"
        + "\n".join(columns)
        + "\n\nВАЖНО: модели приходят от разных проектировщиков, поэтому имена "
        "колонок (свойств, наборов, QTO) произвольны и в каждом файле свои. "
        "Сопоставляй по СМЫСЛУ характеристики, типу значения и единицам, а не "
        "по совпадению названия. Наименование элемента тоже может подсказывать "
        "смысл (например, «Плита перекрытия», «Фундаментная плита»).\n\n"
        "Сопоставь КАЖДЫЙ требуемый параметр с одной колонкой модели. "
        "Если значение требует преобразования, укажи transform одним из значений: "
        "none, mm_to_m, mm2_to_m2, mm3_to_m3, storey_sub, storey_level, "
        "extract_concrete_grade, extract_steel_grade, extract_water_resist, "
        "extract_freeze_durability, first_number.\n"
        "Правила:\n"
        "- source — ТОЧНОЕ имя колонки из списка выше, ничего не придумывай;\n"
        "- если соответствия нет, верни source: null (не подставляй похожее по "
        "имени, но другое по смыслу значение);\n"
        "- для RUS_Level и RUS_Object_Sub используй колонку «Этаж» с transform "
        "storey_level / storey_sub;\n"
        "- для класса бетона используй колонку с классом бетона (например, "
        "MGE_ConcreteGrade), для стали — с классом арматуры (например, "
        "MGE_ReinforceStrengthClass); если таких колонок нет — извлекай из "
        "наименования/материала через extract_concrete_grade / extract_steel_grade;\n"
        "- для RUS_WaterResist (водонепроницаемость, например W6) и "
        "RUS_FreezeDurability (морозостойкость, например F150) используй колонки "
        "с маркой по водонепроницаемости/морозостойкости (например, "
        "MGE_WaterResist, MGE_FreezeDurability); если марка записана вместе с "
        "другими данными (класс бетона, материал), укажи transform "
        "extract_water_resist / extract_freeze_durability;\n"
        "- не подставляй площади вместо объёмов и наоборот, не подставляй длину "
        "элемента вместо размера сечения;\n"
        "- не применяй mm_to_m / mm2_to_m2 / mm3_to_m3, если колонка уже в "
        "метрах, квадратных или кубических метрах.\n\n"
        "Ответ строго JSON без пояснений:\n"
        '{"RUS_Length": {"source": "QTO::...", "transform": "none"}, ...}'
    )


def _parse_llm_mapping(
    answer: Dict[str, Any],
    requirement: Requirement,
    candidates: Dict[str, List[str]],
    fallback: Dict[str, MappingEntry],
) -> Dict[str, MappingEntry]:
    """Проверить ответ LLM и наложить его на fallback-карту."""
    by_lower = {column.lower(): column for column in candidates}
    required_names = set(requirement.attribute_names)
    mapping = dict(fallback)

    for name, raw_entry in answer.items():
        if name not in required_names:
            continue
        if isinstance(raw_entry, str):
            source, transform = raw_entry, TRANSFORM_NONE
        elif isinstance(raw_entry, dict):
            source = raw_entry.get("source")
            transform = str(raw_entry.get("transform") or TRANSFORM_NONE).strip()
        else:
            continue

        if transform not in ALLOWED_TRANSFORMS:
            transform = TRANSFORM_NONE

        # Марки W/F всегда извлекаются из значения: даже если LLM указала
        # колонку с составной строкой, в результате нужна именно марка.
        if name in _GRADE_TRANSFORMS and transform == TRANSFORM_NONE:
            transform = _GRADE_TRANSFORMS[name]

        if source in (None, "", "null", "None"):
            # LLM явно не нашла соответствие — оставляем fallback, если он есть.
            continue
        source = str(source).strip()
        if source not in candidates:
            source = by_lower.get(source.lower(), "")
        if not source:
            continue

        # Разбор этажа имеет смысл только для колонки «Этаж».
        if transform in (TRANSFORM_STOREY_SUB, TRANSFORM_STOREY_LEVEL):
            if "Этаж" in candidates:
                source = "Этаж"
            else:
                transform = TRANSFORM_NONE

        # Наименование элемента — колонка Name, а не ObjectType.
        if name == "RUS_Name" and source == "ObjectType" and "Name" in candidates:
            source = "Name"

        mapping[name] = _entry(source, transform)

    return mapping


def build_mapping(
    requirement: Requirement,
    rows: List[Dict[str, Any]],
    llm: Optional[CheckModelLLM] = None,
) -> MappingResult:
    """Построить карту требуемых параметров (LLM + детерминированный fallback)."""
    candidates = candidate_columns(rows, exclude=requirement.attribute_names)
    fallback = deterministic_mapping(requirement, candidates.keys())

    if llm is None:
        return MappingResult(mapping=fallback, candidates=candidates)

    model_name = getattr(llm, "model", "unknown")
    key = _cache_key(requirement, candidates, model_name)

    with _CACHE_LOCK:
        cache = _load_cache()
        cached = cache.get(key)
    if isinstance(cached, dict):
        mapping = dict(fallback)
        for name, entry in cached.items():
            if name not in mapping or not isinstance(entry, dict):
                continue
            source = entry.get("source")
            transform = str(entry.get("transform") or TRANSFORM_NONE)
            if transform not in ALLOWED_TRANSFORMS:
                transform = TRANSFORM_NONE
            if source in candidates:
                mapping[name] = _entry(source, transform)
            elif source is None:
                mapping[name] = _entry(None, transform)
        return MappingResult(
            mapping=mapping,
            candidates=candidates,
            llm_used=True,
            from_cache=True,
        )

    prompt = _build_prompt(requirement, candidates, sample_row=rows[0] if rows else None)
    try:
        answer = llm.chat_json(
            prompt,
            system="Ты аккуратный инженер-эксперт по ЦИМ. Отвечай только JSON.",
        )
    except CheckModelLLMError as exc:
        logger.warning(
            "LLM-сопоставление %s (%s) не выполнено: %s",
            requirement.code,
            requirement.ifc_class,
            exc,
        )
        return MappingResult(
            mapping=fallback,
            candidates=candidates,
            llm_error=str(exc),
        )

    mapping = _parse_llm_mapping(answer, requirement, candidates, fallback)
    serialized = {
        name: entry.as_dict()
        for name, entry in mapping.items()
        if name in requirement.attribute_names
    }
    with _CACHE_LOCK:
        cache = _load_cache()
        cache[key] = serialized
        _save_cache(cache)

    return MappingResult(
        mapping=mapping,
        candidates=candidates,
        llm_used=True,
    )


# ---------------------------------------------------------------------
#  Применение карты к элементу
# ---------------------------------------------------------------------

def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_length_column(source: str) -> bool:
    source_lower = source.lower()
    markers = (
        "perimeter",
        "периметр",
        "length",
        "длина",
        "width",
        "ширина",
        "height",
        "высота",
        "depth",
        "толщина",
        "diameter",
        "диаметр",
    )
    return any(marker in source_lower for marker in markers)


def _convert_units(value: Any, source: Optional[str], unit: Optional[str]) -> Any:
    """Привести числовое значение к единице требуемого параметра."""
    if not source or not unit:
        return value
    number = _to_number(value)
    if number is None:
        return value

    target = str(unit).strip().lower().replace(" ", "")
    source_lower = source.lower()

    if target in ("м", "m") and _is_length_column(source):
        number = number / 1000.0
    elif target in ("м²", "м2", "m2") and ("мм²" in source_lower or "mm2" in source_lower):
        number = number / 1_000_000.0
    elif target in ("м³", "м3", "m3") and ("мм³" in source_lower or "mm3" in source_lower):
        number = number / 1_000_000_000.0
    else:
        return value

    # Убираем хвостовые нули у целых значений.
    rounded = round(number, 6)
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return rounded


def _apply_transform(
    value: Any,
    transform: str,
    row: Dict[str, Any],
) -> Any:
    if transform == TRANSFORM_MM_TO_M:
        number = _to_number(value)
        return "" if number is None else round(number / 1000.0, 6)
    if transform == TRANSFORM_MM2_TO_M2:
        number = _to_number(value)
        return "" if number is None else round(number / 1_000_000.0, 9)
    if transform == TRANSFORM_MM3_TO_M3:
        number = _to_number(value)
        return "" if number is None else round(number / 1_000_000_000.0, 12)
    if transform == TRANSFORM_STOREY_SUB:
        return storey_parts(row)["object_sub"]
    if transform == TRANSFORM_STOREY_LEVEL:
        return storey_parts(row)["level"]
    if transform == TRANSFORM_CONCRETE_GRADE:
        return extract_concrete_grade(value)
    if transform == TRANSFORM_STEEL_GRADE:
        return extract_steel_grade(value)
    if transform == TRANSFORM_WATER_RESIST:
        return extract_water_resist(value)
    if transform == TRANSFORM_FREEZE_DURABILITY:
        return extract_freeze_durability(value)
    if transform == TRANSFORM_FIRST_NUMBER:
        return first_number(value)
    return value


def _first_present(row: Dict[str, Any], suffixes: Iterable[str]) -> Any:
    for suffix in suffixes:
        for key, value in row.items():
            if str(key).endswith(suffix) and value not in (None, ""):
                return value
    return ""


def _fallback_value(attribute: RequiredAttribute, row: Dict[str, Any]) -> Any:
    """Дополнительный fallback для отдельных требуемых параметров."""
    if attribute.name == "RUS_Object_Sub":
        return storey_parts(row)["object_sub"]
    if attribute.name == "RUS_Level":
        return storey_parts(row)["level"]
    if attribute.name == "RUS_StrengthClass":
        return extract_concrete_grade(
            _first_present(row, ("::MGE_ConcreteGrade",))
            or row.get("Материал")
            or row.get("Name")
        )
    if attribute.name == "RUS_SteelGrade":
        return extract_steel_grade(
            _first_present(row, ("::MGE_ReinforceStrengthClass",))
            or row.get("Name")
        )
    if attribute.name == "RUS_WaterResist":
        return extract_water_resist(
            _first_present(row, ("::MGE_WaterResist",))
            or row.get("Материал")
            or row.get("Name")
        )
    if attribute.name == "RUS_FreezeDurability":
        return extract_freeze_durability(
            _first_present(row, ("::MGE_FreezeDurability",))
            or row.get("Материал")
            or row.get("Name")
        )
    if attribute.name == "RUS_Object":
        return _first_present(row, ("::MGE_Object", "::Object", "::Объект"))
    return ""


def _consistent_volume(value: Any, estimate: Any) -> Any:
    """Заменить объём, расходящийся с геометрической оценкой на порядки.

    В выгрузках Revit объём плит иногда записан в литрах (площадь м² ×
    толщина мм), из-за чего QTO-значение отличается от геометрии примерно
    в 1000 раз. Такое значение заменяется геометрической оценкой; корректные
    значения (расхождение в пределах допуска) не меняются.
    """
    mapped = _to_number(value)
    expected = _to_number(estimate)
    if mapped is None or not expected:
        return value
    if mapped > expected * _VOLUME_TOLERANCE or mapped * _VOLUME_TOLERANCE < expected:
        return estimate
    return value


def apply_mapping(
    requirement: Requirement,
    mapping: Dict[str, MappingEntry],
    row: Dict[str, Any],
) -> Dict[str, Any]:
    """Получить значения требуемых параметров для одного элемента."""
    result: Dict[str, Any] = {}
    for attribute in requirement.attributes:
        entry = mapping.get(attribute.name) or MappingEntry()
        value: Any = ""
        if entry.source:
            value = row.get(entry.source, "")
        if value in (None, ""):
            fallback = _fallback_value(attribute, row)
            if fallback not in (None, ""):
                value = fallback
        value = _apply_transform(value, entry.transform, row)
        # Марка не извлеклась из выбранной колонки (например, LLM указала
        # колонку класса бетона) — ищем её в известных свойствах элемента.
        if value in (None, "") and attribute.name in _GRADE_TRANSFORMS:
            value = _fallback_value(attribute, row)
        # Если преобразование единиц уже выполнено transform-ом, повторно
        # пересчитывать нельзя (иначе 27600 мм → 27.6 → 0.0276).
        if entry.transform == TRANSFORM_NONE:
            value = _convert_units(value, entry.source, attribute.unit)
        # Выбранная картой колонка у этого элемента пуста (QTO выгружены не
        # у всех элементов) — добираем геометрию из QTO_bbox и расчётных
        # формул класса IFC. Значение уже приведено к единице требования.
        if value in (None, ""):
            if attribute.name in GEOMETRY_ATTRIBUTES:
                value = resolve_geometry(attribute, row, requirement.ifc_class)
        elif attribute.name in _VOLUME_ATTRIBUTES:
            value = _consistent_volume(
                value,
                resolve_geometry(attribute, row, requirement.ifc_class),
            )
        if isinstance(value, str):
            value = value.strip()
        result[attribute.name] = value
    return result


def mapping_to_json(mapping: Dict[str, MappingEntry]) -> Dict[str, Any]:
    """Сериализовать карту для файла результата."""
    return {name: entry.as_dict() for name, entry in mapping.items()}
