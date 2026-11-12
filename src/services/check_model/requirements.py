"""Требования к параметрам ЦИМ-элементов из ``data/parametrs_name.json``.

Справочник описывает наборы требуемых параметров (``наборы_атрибутов``) для
каждого вида элемента ЦИМ. Один IFC-класс может встречаться в нескольких
записях (например, ``IfcSlab`` — перекрытие и фундаментная плита), поэтому
дополнительно реализован выбор подходящей записи по коду МССК, типу
``PredefinedType`` и наименованию элемента.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Корень проекта: src/services/check_model/requirements.py -> parents[3]
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REQUIREMENTS_PATH = _PROJECT_ROOT / "data" / "parametrs_name.json"

# Служебные колонки результата (не являются параметрами модели).
SERVICE_COLUMNS = ("Код_требований_ЦИМ", "Элемент_ЦИМ", "Статус_проверки")

_ATTR_SUFFIX_RE = re.compile(r"\s*\*+\s*$")
_STOREY_PREFIX_RE = re.compile(r"^([A-Za-zА-Яа-я]{1,6}\d{1,6})[_\-\s]+(.+)$")
_CONCRETE_GRADE_RE = re.compile(r"[BВ]\s?(\d{1,3}(?:[.,]\d+)?)", re.IGNORECASE)
_STEEL_GRADE_RE = re.compile(
    r"\b([AА]\s?\d{3}(?:[.,]\d+)?(?:\s?[CС])?)\b", re.IGNORECASE
)
# Марка по водонепроницаемости: латинская W (W6, W 8). Кириллическая «В»
# совпадает по начертанию с классом бетона (В30), поэтому она учитывается
# только при явном упоминании водонепроницаемости в тексте.
_WATER_RESIST_LATIN_RE = re.compile(
    r"(?<![A-Za-zА-Яа-я])W\s?(\d{1,3}(?:[.,]\d+)?)", re.IGNORECASE
)
_WATER_RESIST_CYRILLIC_RE = re.compile(
    r"(?<![A-Za-zА-Яа-я])В\s?(\d{1,3}(?:[.,]\d+)?)", re.IGNORECASE
)
# Марка по морозостойкости: F150 (встречается и кириллическая «Ф»).
_FREEZE_DURABILITY_RE = re.compile(
    r"(?<![A-Za-zА-Яа-я])[FФ]\s?(\d{1,3}(?:[.,]\d+)?)", re.IGNORECASE
)


@dataclass(frozen=True)
class RequiredAttribute:
    """Один требуемый параметр элемента ЦИМ."""

    name: str                 # нормализованное имя колонки (RUS_*)
    raw_name: str             # имя как в справочнике (может содержать «*»)
    characteristic: str       # характеристика элемента
    data_type: str            # тип данных
    unit: Optional[str]       # единица измерения
    example: str              # пример значения
    set_name: str             # набор атрибутов (RusSet_*)
    section: str              # раздел набора
    number: Optional[int]     # № в справочнике


@dataclass
class Requirement:
    """Требования к одному виду элемента ЦИМ."""

    code: str
    ifc_class: str
    element: str
    section: str
    object_kind: str
    mssk_codes: List[str] = field(default_factory=list)
    attributes: List[RequiredAttribute] = field(default_factory=list)
    sets: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def attribute_names(self) -> List[str]:
        """Уникальные нормализованные имена требуемых параметров."""
        result: List[str] = []
        for attr in self.attributes:
            if attr.name not in result:
                result.append(attr.name)
        return result

    def attribute(self, name: str) -> Optional[RequiredAttribute]:
        for attr in self.attributes:
            if attr.name == name:
                return attr
        return None


def normalize_attribute_name(name: Optional[str]) -> str:
    """Нормализовать имя атрибута: убрать пробелы и звёздочки-маркеры."""
    if not name:
        return ""
    return _ATTR_SUFFIX_RE.sub("", str(name)).strip()


def _parse_requirement(code: str, payload: Dict[str, Any]) -> Requirement:
    meta = payload.get("метаданные") or {}
    attributes: List[RequiredAttribute] = []
    sets: List[Dict[str, Any]] = []
    for attribute_set in payload.get("наборы_атрибутов") or []:
        set_name = str(attribute_set.get("набор") or "")
        section = str(attribute_set.get("раздел") or "")
        sets.append({"name": set_name, "section": section})
        for attr in attribute_set.get("атрибуты") or []:
            raw_name = str(attr.get("наименование_атрибута") or "")
            name = normalize_attribute_name(raw_name)
            if not name:
                continue
            attributes.append(
                RequiredAttribute(
                    name=name,
                    raw_name=raw_name,
                    characteristic=str(attr.get("характеристика_элемента") or ""),
                    data_type=str(attr.get("тип_данных") or ""),
                    unit=(str(attr["ед_изм"]) if attr.get("ед_изм") else None),
                    example=str(attr.get("пример") or ""),
                    set_name=set_name,
                    section=section,
                    number=attr.get("№"),
                )
            )
    return Requirement(
        code=str(meta.get("код") or code),
        ifc_class=str(meta.get("класс_IFC") or ""),
        element=str(meta.get("элемент") or ""),
        section=str(meta.get("раздел_ЦИМ") or ""),
        object_kind=str(meta.get("вид_объекта") or ""),
        mssk_codes=[str(item) for item in (meta.get("код_МССК") or [])],
        attributes=attributes,
        sets=sets,
    )


def load_requirements(path: Optional[str] = None) -> Dict[str, Requirement]:
    """Загрузить справочник требований.

    Args:
        path: необязательный путь к JSON. По умолчанию
            ``data/parametrs_name.json``.

    Returns:
        Словарь ``{код_требований: Requirement}``.
    """
    requirements_path = Path(path) if path else DEFAULT_REQUIREMENTS_PATH
    with open(requirements_path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("parametrs_name.json должен содержать объект")

    result: Dict[str, Requirement] = {}
    for code, item in payload.items():
        if not isinstance(item, dict):
            continue
        requirement = _parse_requirement(code, item)
        if requirement.ifc_class:
            result[requirement.code] = requirement
    return result


def group_by_ifc_class(
    requirements: Dict[str, Requirement]
) -> Dict[str, List[Requirement]]:
    """Сгруппировать требования по IFC-классу."""
    grouped: Dict[str, List[Requirement]] = {}
    for requirement in requirements.values():
        grouped.setdefault(requirement.ifc_class, []).append(requirement)
    for items in grouped.values():
        items.sort(key=lambda req: req.code)
    return grouped


def row_text(row: Dict[str, Any]) -> str:
    """Склеить текстовые поля элемента для эвристик выбора требований."""
    keys = (
        "Name",
        "ObjectType",
        "Description",
        "Материал",
        "Этаж",
        "PredefinedType",
        "Tag",
    )
    parts = []
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    # Коды МССК из любых свойств MGE_ElementCode.
    for key, value in row.items():
        if str(key).endswith("::MGE_ElementCode") and value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts).lower()


def element_mssk_code(row: Dict[str, Any]) -> str:
    """Найти код элемента МССК в строке сырого дампа."""
    for key, value in row.items():
        if str(key).endswith("::MGE_ElementCode") and value not in (None, ""):
            return str(value).strip()
    return ""


def _tokenize(text: str) -> List[str]:
    return [token for token in re.split(r"[^0-9a-zа-яё]+", text.lower()) if token]


def _score_requirement(row: Dict[str, Any], requirement: Requirement) -> float:
    """Эвристическая оценка соответствия элемента требованиям."""
    score = 0.0

    # Код МССК — самый надёжный признак.
    code = element_mssk_code(row)
    if code:
        normalized_code = code.replace(" ", "").upper()
        if any(
            normalized_code == mssk.replace(" ", "").upper()
            for mssk in requirement.mssk_codes
        ):
            score += 100.0
        elif any(
            normalized_code.startswith(mssk.replace(" ", "").upper())
            for mssk in requirement.mssk_codes
        ):
            score += 40.0

    text = row_text(row)
    element_tokens = _tokenize(requirement.element)
    if element_tokens:
        hits = sum(1 for token in element_tokens if token in text)
        score += 10.0 * hits / len(element_tokens)

    # Уточнение по PredefinedType для плит.
    predefined = str(row.get("PredefinedType") or "").upper()
    element_lower = requirement.element.lower()
    if "фундамент" in element_lower and predefined in ("BASESLAB", "FOUNDATION"):
        score += 25.0
    if "перекрыт" in element_lower and predefined in ("FLOOR", "ROOF"):
        score += 25.0
    if "фундамент" in element_lower and any(
        word in text for word in ("фундамент", "ростверк", "подготовк")
    ):
        score += 20.0
    if "перекрыт" in element_lower and "перекрыт" in text:
        score += 20.0

    return score


def select_requirement(
    row: Dict[str, Any],
    candidates: List[Requirement],
) -> Requirement:
    """Выбрать требования для элемента из нескольких вариантов одного класса.

    Если для IFC-класса есть только одни требования — возвращаются они.
    Иначе используется эвристика по коду МССК, ``PredefinedType`` и
    наименованию. При полном отсутствии признаков — первые требования.
    """
    if not candidates:
        raise ValueError("Не переданы требования для выбора")
    if len(candidates) == 1:
        return candidates[0]

    scored = sorted(
        ((_score_requirement(row, requirement), requirement) for requirement in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score > 0:
        return best
    return candidates[0]


def storey_parts(row: Dict[str, Any]) -> Dict[str, str]:
    """Разобрать этаж на номер подобъекта и уровень.

    Пример: ``С02_1_этаж_основной`` → ``{"object_sub": "С02",
    "level": "1_этаж_основной"}``.
    """
    storey = str(row.get("Этаж") or "").strip()
    if not storey:
        return {"object_sub": "", "level": ""}
    match = _STOREY_PREFIX_RE.match(storey)
    if match:
        return {"object_sub": match.group(1), "level": match.group(2).strip()}
    return {"object_sub": "", "level": storey}


def extract_concrete_grade(value: Any) -> str:
    """Извлечь класс бетона (B30, В25) из строки."""
    if value in (None, ""):
        return ""
    match = _CONCRETE_GRADE_RE.search(str(value))
    return match.group(1).replace(",", ".") if match else ""


def extract_steel_grade(value: Any) -> str:
    """Извлечь класс арматурной стали (А500С, A400) из строки."""
    if value in (None, ""):
        return ""
    match = _STEEL_GRADE_RE.search(str(value))
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def _canonical_grade(prefix: str, number: str) -> str:
    """Привести марку к каноническому виду: ``W6``, ``F150``."""
    try:
        numeric = float(str(number).replace(",", "."))
    except (TypeError, ValueError):
        return ""
    if numeric == int(numeric):
        return f"{prefix}{int(numeric)}"
    return f"{prefix}{numeric}"


def extract_water_resist(value: Any) -> str:
    """Извлечь марку по водонепроницаемости (W4, W6) из строки."""
    if value in (None, ""):
        return ""
    text = str(value)
    match = _WATER_RESIST_LATIN_RE.search(text)
    if not match and "водонепрониц" in text.lower():
        match = _WATER_RESIST_CYRILLIC_RE.search(text)
    return _canonical_grade("W", match.group(1)) if match else ""


def extract_freeze_durability(value: Any) -> str:
    """Извлечь марку по морозостойкости (F75, F150) из строки."""
    if value in (None, ""):
        return ""
    match = _FREEZE_DURABILITY_RE.search(str(value))
    return _canonical_grade("F", match.group(1)) if match else ""


def first_number(value: Any) -> Any:
    """Вернуть первое число из строки (или исходное значение)."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return value
    number = match.group(0).replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return value


def iter_required_attributes(
    requirements: Iterable[Requirement],
) -> List[RequiredAttribute]:
    """Уникальные требуемые атрибуты по списку требований (в порядке появления)."""
    result: List[RequiredAttribute] = []
    seen = set()
    for requirement in requirements:
        for attr in requirement.attributes:
            if attr.name in seen:
                continue
            seen.add(attr.name)
            result.append(attr)
    return result
