"""Проверка ЦИМ модели.

Функционал выгружает параметры элементов IFC тем же шагом, что и режим ЦС
(``IFC_исходные_параметры.json/.xlsx``), затем по справочнику
``data/parametrs_name.json`` определяет требования к каждому IFC-классу и
дополняет элементы требуемыми параметрами (``RUS_*``). Соответствие между
параметрами модели и требуемыми параметрами определяет LLM.

Пакет не относится к режимам ЦС/ИИ/Архитектура: он использует только сырой
дамп IFC и не выполняет подбор работ.
"""

from .pipeline import (
    CHECK_MODEL_MAPPING_JSON,
    CHECK_MODEL_REPORT_JSON,
    CHECK_MODEL_RESULT_JSON,
    CHECK_MODEL_RESULT_XLSX,
    run_check_model,
)
from .requirements import load_requirements

__all__ = [
    "CHECK_MODEL_MAPPING_JSON",
    "CHECK_MODEL_REPORT_JSON",
    "CHECK_MODEL_RESULT_JSON",
    "CHECK_MODEL_RESULT_XLSX",
    "load_requirements",
    "run_check_model",
]
