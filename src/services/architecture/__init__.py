"""Пайплайн режима «Архитектура» (ARCH).

Пакет содержит собственные копии модулей, необходимых для обработки
**только архитектурных решений** (окна, двери, витражи, кровля, отделка,
перила, мебель и т.п.) без конструктивных элементов (стены, колонны,
перекрытия, балки, фундаменты, сваи, лестницы, пандусы).

Режим «Архитектура» повторяет пайплайн ИИ:

1. ``zero_step`` — извлечение элементов IFC по архитектурному набору
   IFC-классов (``ELEMENT_TYPES_ARCH``) и формирование Excel-таблиц;
2. ``group_excel`` — группировка «Часть здания → МССК → Материал →
   Наименование» (правила АР);
3. ``works_table_selector`` — детерминированный подбор таблиц работ
   (8 шагов ``data/algorithm.md``);
4. ``works_fetcher`` — работы таблиц из цифрового сборника (larix);
5. ``works_final_selector`` — финальный подбор работ через LLM;
6. ``ifc_json_builder`` — сборка ``final_result_ARCH.json``.

Документы ПОС/ПЗ/ВОР/ПД разбираются общими модулями (``pd_parser``,
``doc_pdf_parser``, ``pd_full``, ``doc_facts``), поскольку их формат не
зависит от режима.

Модули пакета — копии соответствующих модулей ``src/services/`` с
изменёнными относительными импортами и архитектурным набором IFC-классов.
Они не должны влиять на режимы ЦС (``CS``) и ИИ (``AI``).
"""

from .zero_step import (  # noqa: F401
    ELEMENT_TYPES_ARCH,
    IFC_CONSTANTS_FILENAME,
    classify_storey_type,
    detect_storeys_type_from_session,
    zero_step,
)
from .group_excel import (  # noqa: F401
    group_elements_ar as group_elements_arch,
    process_ifc_excel_ar as process_ifc_excel_arch,
)
from .works_table_selector import (  # noqa: F401
    WORKS_TABLES_JSON_FILENAME,
    build_works_tables_json,
)
from .works_fetcher import (  # noqa: F401
    PERIOD_JSON_FILENAME,
    WORKS_JSON_FILENAME,
    fetch_and_save_works,
)
from .works_final_selector import (  # noqa: F401
    FINAL_WORKS_JSON_FILENAME,
    FINAL_WORKS_XLSX_FILENAME,
    select_final_works,
)
from .ifc_json_builder import build_final_json  # noqa: F401

__all__ = [
    "ELEMENT_TYPES_ARCH",
    "IFC_CONSTANTS_FILENAME",
    "classify_storey_type",
    "detect_storeys_type_from_session",
    "zero_step",
    "group_elements_arch",
    "process_ifc_excel_arch",
    "WORKS_TABLES_JSON_FILENAME",
    "build_works_tables_json",
    "PERIOD_JSON_FILENAME",
    "WORKS_JSON_FILENAME",
    "fetch_and_save_works",
    "FINAL_WORKS_JSON_FILENAME",
    "FINAL_WORKS_XLSX_FILENAME",
    "select_final_works",
    "build_final_json",
]
