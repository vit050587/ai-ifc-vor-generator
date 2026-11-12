"""Пайплайн проверки ЦИМ модели.

На вход подаётся каталог сессии, в котором уже сформирован сырой дамп IFC
(``IFC_исходные_параметры.json``). Пайплайн:

1. читает требования из ``data/parametrs_name.json``;
2. для каждого IFC-класса выбирает набор требований;
3. просит LLM сопоставить требуемые параметры с колонками модели;
4. дополняет каждый элемент требуемыми параметрами (``RUS_*``);
5. сохраняет результат и отчёт в каталог сессии.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from src.core.logger import setup_logger

from .llm_client import CheckModelLLM
from .mapping import (
    MappingEntry,
    MappingResult,
    apply_mapping,
    build_mapping,
    mapping_to_json,
)
from .requirements import (
    Requirement,
    element_mssk_code,
    group_by_ifc_class,
    iter_required_attributes,
    load_requirements,
    row_text,
    select_requirement,
)

logger = setup_logger(__name__)

RAW_DUMP_JSON = "IFC_исходные_параметры.json"
CHECK_MODEL_RESULT_JSON = "Проверка_ЦИМ_модели.json"
CHECK_MODEL_RESULT_XLSX = "Проверка_ЦИМ_модели.xlsx"
CHECK_MODEL_REPORT_JSON = "Проверка_ЦИМ_модели_отчет.json"
CHECK_MODEL_MAPPING_JSON = "Сопоставление_параметров.json"

# Фактические IFC-классы, которые наследуются от классов из справочника.
# Проектировщики выгружают как «обычные», так и *StandardCase/*ElementedCase
# варианты, поэтому все они приводятся к классу требований.
_CLASS_ALIASES = {
    "IfcWallStandardCase": "IfcWall",
    "IfcWallElementedCase": "IfcWall",
    "IfcSlabStandardCase": "IfcSlab",
    "IfcSlabElementedCase": "IfcSlab",
    "IfcOpeningStandardCase": "IfcOpeningElement",
    "IfcColumnStandardCase": "IfcColumn",
}

# Классы-«заглушки», которые проектировщики часто используют вместо
# типизированных элементов (например, лестничный марш как
# IfcBuildingElementProxy). Для них требования определяются по коду МССК или
# точному наименованию элемента из справочника.
_PROXY_CLASSES = {"IfcBuildingElementProxy"}

ProgressCallback = Callable[[int, str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_llm_unavailable(error: Optional[str]) -> bool:
    """True, если ошибка LLM означает недоступность сервиса (а не промпта)."""
    if not error:
        return False
    text = error.lower()
    return any(
        marker in text
        for marker in (
            "llm недоступна",
            "connection",
            "connect",
            "timed out",
            "timeout",
            "max retries",
        )
    )


def _requirement_class(ifc_class: str, grouped: Dict[str, List[Requirement]]) -> Optional[str]:
    """Определить класс требований с учётом наследников IFC."""
    if ifc_class in grouped:
        return ifc_class
    alias = _CLASS_ALIASES.get(ifc_class)
    if alias and alias in grouped:
        return alias
    return None


def _infer_proxy_requirement(
    row: Dict[str, Any],
    requirements: Dict[str, Requirement],
) -> Optional[Requirement]:
    """Определить требования для класса-заглушки по коду МССК или наименованию.

    Используется только для ``IfcBuildingElementProxy``: такие элементы
    не имеют собственного набора требований, но могут быть лестничным маршем,
    стеной и т. п. Сопоставление консервативное — точный код МССК или полное
    наименование элемента из справочника в тексте элемента. Догадки по
    отдельным словам не принимаются, чтобы не подменить один тип другим.
    """
    text = row_text(row)
    code = element_mssk_code(row).replace(" ", "").upper()
    best: Optional[Requirement] = None
    best_score = 0.0
    for requirement in requirements.values():
        score = 0.0
        if code:
            for mssk in requirement.mssk_codes:
                normalized = mssk.replace(" ", "").upper()
                if code == normalized:
                    score = max(score, 100.0)
                elif code.startswith(normalized):
                    score = max(score, 40.0)
        element = requirement.element.strip().lower()
        if element and element in text:
            score = max(score, 50.0)
        if score > best_score:
            best_score = score
            best = requirement
    return best if best_score >= 50.0 else None


def _read_raw_dump(session_dir: Path) -> List[Dict[str, Any]]:
    path = session_dir / RAW_DUMP_JSON
    if not path.is_file():
        raise RuntimeError(
            f"Не найден сырой дамп IFC: {RAW_DUMP_JSON}. "
            "Сначала выполните выгрузку параметров элементов."
        )
    with open(path, "r", encoding="utf-8") as stream:
        rows = json.load(stream)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"Файл {RAW_DUMP_JSON} должен содержать массив объектов")
    return rows


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
    os.replace(temporary, path)


def _write_xlsx(
    path: Path,
    rows: List[Dict[str, Any]],
    columns: List[str],
) -> None:
    full_rows = [{column: row.get(column, "") for column in columns} for row in rows]
    frame = pd.DataFrame(full_rows, columns=columns)
    if not frame.empty:
        frame = frame.fillna("")
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Проверка_ЦИМ", index=False)


def _count_filled(
    requirement: Requirement,
    values_by_row: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Посчитать заполненные значения по каждому требуемому параметру."""
    result: Dict[str, int] = {}
    for attribute in requirement.attributes:
        result[attribute.name] = sum(
            1
            for values in values_by_row
            if values.get(attribute.name) not in (None, "")
        )
    return result


def run_check_model(
    session_dir: str,
    *,
    progress: Optional[ProgressCallback] = None,
    llm: Any = None,
    use_llm: Optional[bool] = None,
    class_totals: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Выполнить проверку ЦИМ модели в каталоге сессии.

    Args:
        session_dir: каталог сессии с ``IFC_исходные_параметры.json``.
        progress: необязательный callback ``(percent, message)``.
        llm: необязательный клиент LLM (для тестов). ``None`` — создать
            :class:`CheckModelLLM`; ``False`` — не использовать LLM.
        use_llm: принудительно включить/выключить LLM. По умолчанию
            управляется переменной окружения ``CHECK_MODEL_USE_LLM``.
        class_totals: необязательная карта «IFC-класс → количество элементов
            этого класса в файле IFC» (считается по открытой модели до
            выгрузки дампа). Если не передана, количество берётся из сырого
            дампа. Нужна, чтобы в отчёте показать «проверено X из Y».

    Returns:
        Отчёт о проверке (он же сохраняется в
        ``Проверка_ЦИМ_модели_отчет.json``).
    """
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)

    def report_progress(percent: int, message: str) -> None:
        if progress:
            try:
                progress(max(0, min(100, percent)), message)
            except Exception:
                logger.debug("Ошибка callback прогресса", exc_info=True)

    report_progress(45, "Чтение параметров элементов IFC...")
    raw_rows = _read_raw_dump(session_path)

    requirements = load_requirements()
    grouped = group_by_ifc_class(requirements)
    required_attributes = iter_required_attributes(requirements.values())

    if use_llm is None:
        use_llm = os.getenv("CHECK_MODEL_USE_LLM", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

    llm_client = None
    if use_llm:
        llm_client = llm if llm is not None else CheckModelLLM()
        if llm_client is False:
            llm_client = None

    # 1. Распределяем элементы по наборам требований.
    report_progress(50, "Определение требований по IFC-классам...")
    groups: Dict[str, Dict[str, Any]] = {}
    skipped_classes: Dict[str, int] = {}
    dump_class_counts: Dict[str, int] = {}
    for row in raw_rows:
        ifc_class = str(row.get("IfcClass") or "")
        requirement_class = _requirement_class(ifc_class, grouped)
        if requirement_class:
            requirement = select_requirement(row, grouped[requirement_class])
        elif ifc_class in _PROXY_CLASSES:
            # Класс-заглушка: требования определяются по коду МССК/названию.
            requirement = _infer_proxy_requirement(row, requirements)
            if requirement is None:
                skipped_classes[ifc_class] = skipped_classes.get(ifc_class, 0) + 1
                continue
        else:
            skipped_classes[ifc_class or "—"] = (
                skipped_classes.get(ifc_class or "—", 0) + 1
            )
            continue
        dump_class_counts[requirement.ifc_class] = (
            dump_class_counts.get(requirement.ifc_class, 0) + 1
        )
        group = groups.setdefault(
            requirement.code,
            {
                "requirement": requirement,
                "rows": [],
            },
        )
        group["rows"].append(row)

    total_groups = max(1, len(groups))
    mappings: Dict[str, MappingResult] = {}
    values_by_code: Dict[str, List[Dict[str, Any]]] = {}
    llm_disabled = llm_client is None
    llm_errors: Dict[str, str] = {}

    # 2. Строим карту параметров и заполняем значения.
    for index, (code, group) in enumerate(sorted(groups.items()), start=1):
        requirement: Requirement = group["requirement"]
        rows: List[Dict[str, Any]] = group["rows"]
        report_progress(
            50 + int(35 * (index - 1) / total_groups),
            f"Сопоставление параметров: {requirement.ifc_class} / {code} "
            f"({requirement.element})...",
        )

        if llm_disabled:
            mapping_result = build_mapping(requirement, rows, llm=None)
        else:
            mapping_result = build_mapping(requirement, rows, llm=llm_client)
            if mapping_result.llm_error:
                llm_errors[code] = mapping_result.llm_error
                if _is_llm_unavailable(mapping_result.llm_error):
                    llm_disabled = True
                    logger.warning(
                        "LLM недоступна — дальнейшие классы обрабатываются "
                        "детерминированными правилами"
                    )

        mappings[code] = mapping_result
        values_by_code[code] = [
            apply_mapping(requirement, mapping_result.mapping, row)
            for row in rows
        ]

    # 3. Формируем итоговые строки.
    report_progress(86, "Формирование результата проверки...")
    required_names = [attribute.name for attribute in required_attributes]
    result_rows: List[Dict[str, Any]] = []
    class_stats: Dict[str, Dict[str, Any]] = {}

    for code, group in sorted(groups.items()):
        requirement: Requirement = group["requirement"]
        rows: List[Dict[str, Any]] = group["rows"]
        values_by_row = values_by_code.get(code, [])
        filled = _count_filled(requirement, values_by_row)

        class_key = requirement.ifc_class
        stats = class_stats.setdefault(
            class_key,
            {
                "ifc_class": requirement.ifc_class,
                "elements": 0,
                "requirements": {},
            },
        )
        stats["elements"] += len(rows)
        stats["requirements"][code] = {
            "code": code,
            "element": requirement.element,
            "section": requirement.section,
            "elements": len(rows),
            "attributes": requirement.attribute_names,
            "filled": filled,
            "missing": {
                name: len(rows) - count for name, count in filled.items()
            },
            "llm_used": mappings[code].llm_used,
            "from_cache": mappings[code].from_cache,
            "llm_error": mappings[code].llm_error,
        }

        for row, values in zip(rows, values_by_row):
            result_row = dict(row)
            result_row["Код_требований_ЦИМ"] = code
            result_row["Элемент_ЦИМ"] = requirement.element
            result_row["Статус_проверки"] = "проверено"
            for name in required_names:
                result_row.setdefault(name, "")
            result_row.update(values)
            result_rows.append(result_row)

    # Всего элементов каждого IFC-класса в файле: точное значение из открытой
    # модели (``class_totals``), иначе — количество строк класса в сыром дампе.
    # Позволяет показать в интерфейсе «проверено X из Y».
    for class_key, stats in class_stats.items():
        total = None
        if class_totals:
            total = class_totals.get(class_key)
        if total is None:
            total = dump_class_counts.get(class_key, stats["elements"])
        # Проверенных не может быть больше, чем есть в файле (страховка от
        # расхождений между моделью и дампом).
        stats["elements_total"] = max(int(total), int(stats["elements"]))
        for requirement_stats in stats["requirements"].values():
            requirement_stats["elements_total"] = stats["elements_total"]

    # 4. Колонки результата: исходные (по проверяемым элементам) + служебные
    #    + все требуемые параметры справочника.
    base_columns: List[str] = []
    seen_columns = set()
    for row in result_rows:
        for key in row.keys():
            if key in seen_columns:
                continue
            seen_columns.add(key)
            base_columns.append(key)
    service_columns = ["Код_требований_ЦИМ", "Элемент_ЦИМ", "Статус_проверки"]
    ordered_columns = [
        column
        for column in base_columns
        if column not in service_columns and column not in required_names
    ] + service_columns + required_names

    # 5. Сохраняем файлы.
    report_progress(92, "Сохранение результата проверки...")
    result_json_path = session_path / CHECK_MODEL_RESULT_JSON
    result_xlsx_path = session_path / CHECK_MODEL_RESULT_XLSX
    mapping_path = session_path / CHECK_MODEL_MAPPING_JSON
    report_path = session_path / CHECK_MODEL_REPORT_JSON

    _write_json(result_json_path, result_rows)
    _write_xlsx(result_xlsx_path, result_rows, ordered_columns)

    mapping_payload: Dict[str, Any] = {}
    for code, mapping_result in sorted(mappings.items()):
        requirement = groups[code]["requirement"]
        mapping_payload[code] = {
            "ifc_class": requirement.ifc_class,
            "element": requirement.element,
            "section": requirement.section,
            "llm_used": mapping_result.llm_used,
            "from_cache": mapping_result.from_cache,
            "llm_error": mapping_result.llm_error,
            "mapping": mapping_to_json(mapping_result.mapping),
        }
    _write_json(mapping_path, mapping_payload)

    # 6. Отчёт.
    required_values = 0
    filled_values = 0
    for code, group in groups.items():
        requirement = group["requirement"]
        values_by_row = values_by_code.get(code, [])
        required_values += len(requirement.attributes) * len(values_by_row)
        filled_values += sum(
            1
            for values in values_by_row
            for attribute in requirement.attributes
            if values.get(attribute.name) not in (None, "")
        )

    report: Dict[str, Any] = {
        "generated_at": _now_iso(),
        "llm_model": getattr(llm_client, "model", None) if llm_client else None,
        "llm_enabled": bool(use_llm and llm_client is not None),
        "llm_errors": llm_errors,
        "raw_dump": RAW_DUMP_JSON,
        "outputs": {
            "result_json": CHECK_MODEL_RESULT_JSON,
            "result_xlsx": CHECK_MODEL_RESULT_XLSX,
            "report_json": CHECK_MODEL_REPORT_JSON,
            "mapping_json": CHECK_MODEL_MAPPING_JSON,
        },
        "elements_total": len(raw_rows),
        "elements_checked": len(result_rows),
        "elements_skipped": len(raw_rows) - len(result_rows),
        "skipped_classes": dict(sorted(skipped_classes.items())),
        "required_attributes": len(required_names),
        "required_values": required_values,
        "filled_values": filled_values,
        "empty_values": required_values - filled_values,
        "classes": dict(sorted(class_stats.items())),
    }
    _write_json(report_path, report)
    report_progress(100, "Проверка ЦИМ модели завершена")
    return report
