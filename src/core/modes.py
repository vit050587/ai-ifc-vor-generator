# modes.py
"""Режимы обработки сервиса и конвертация их кодов.

Режимы переименованы (обрабатывают одни и те же файлы — IFC/PDF,
различается только пайплайн подбора работ):

- **ЦС** («Цифровой сборник», публичный код ``CS``) — бывший **КР**
  («Конструктивные решения», код ``KR``). Подбор работ выполняется
  через внешний API цифрового справочника ТСН.
- **ИИ** («Искусственный интеллект», публичный код ``AI``) — бывший **АР**
  («Архитектурные решения», код ``AR``). Подбор таблиц работ —
  детерминированный алгоритм, финальный выбор работ и разбор ПОС/ПЗ —
  локальная LLM.

Публичные коды ``CS``/``AI`` используются в API (поле ``processingType``),
веб-интерфейсе и ответах сервера. Легаси-коды ``KR``/``AR`` продолжают
приниматься на входе (обратная совместимость со старыми клиентами и
сессиями) и остаются **внутренними кодами пайплайн-модулей**
(``zero_step``, ``group_excel``, ``works_*``, ``ifc_json_builder`` и др.),
поэтому модули пайплайна работают с ними без изменений.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Публичные коды режимов (API / UI / ответы сервера)
MODE_CS = "CS"  # ЦС — цифровой сборник (бывший КР)
MODE_AI = "AI"  # ИИ — искусственный интеллект (бывший АР)

# Легаси-коды: внутренние коды пайплайн-модулей и старых сессий
LEGACY_KR = "KR"  # бывший код режима ЦС
LEGACY_AR = "AR"  # бывший код режима ИИ

# Человекочитаемые названия режимов (для UI/логов/документации)
MODE_NAMES: Dict[str, str] = {
    MODE_CS: "Цифровой сборник (ЦС)",
    MODE_AI: "Искусственный интеллект (ИИ)",
}

# Короткие подписи режимов (для бейджей интерфейса)
MODE_SHORT_NAMES: Dict[str, str] = {
    MODE_CS: "ЦС",
    MODE_AI: "ИИ",
}

# Допустимые входные значения → публичный код режима.
# Включает легаси-коды (KR/AR, в т.ч. русские «КР»/«АР») для обратной
# совместимости со старыми клиентами и сохранёнными сессиями.
_INPUT_TO_MODE: Dict[str, str] = {
    MODE_CS: MODE_CS,
    LEGACY_KR: MODE_CS,
    "КР": MODE_CS,
    MODE_AI: MODE_AI,
    LEGACY_AR: MODE_AI,
    "АР": MODE_AI,
}

# Публичный код → внутренний (легаси) код пайплайна
_MODE_TO_PIPELINE: Dict[str, str] = {
    MODE_CS: LEGACY_KR,
    MODE_AI: LEGACY_AR,
}

# Внутренний (легаси) код пайплайна → публичный код.
# Русские «КР»/«АР» — значения поля discipline финального JSON.
_PIPELINE_TO_MODE: Dict[str, str] = {
    LEGACY_KR: MODE_CS,
    "КР": MODE_CS,
    LEGACY_AR: MODE_AI,
    "АР": MODE_AI,
}


def normalize_processing_type(value: Optional[str]) -> str:
    """Нормализовать входное значение режима в публичный код ``CS``/``AI``.

    Принимает новые коды (``CS``/``AI``), легаси-коды (``KR``/``AR``,
    русские «КР»/«АР») в любом регистре. При неизвестном значении —
    тихий fallback на ``CS`` (как раньше — на ``KR``).

    Args:
        value: входное значение (поле ``processingType`` и т.п.).

    Returns:
        Публичный код режима: ``"CS"`` или ``"AI"``.
    """
    if not value:
        return MODE_CS
    return _INPUT_TO_MODE.get(str(value).strip().upper(), MODE_CS)


def to_pipeline_type(value: Optional[str]) -> str:
    """Преобразовать режим во внутренний (легаси) код пайплайна.

    Пайплайн-модули (``zero_step``, ``group_excel``, ``works_*``,
    ``ifc_json_builder`` и др.) продолжают работать с кодами ``KR``/``AR``.

    Args:
        value: публичный код режима (``CS``/``AI``) либо легаси-код.

    Returns:
        Внутренний код пайплайна: ``"KR"`` или ``"AR"``.
    """
    mode = normalize_processing_type(value)
    return _MODE_TO_PIPELINE[mode]


def to_public_type(value: Optional[str]) -> str:
    """Преобразовать внутренний (легаси) код режима в публичный ``CS``/``AI``.

    Используется при построении ответов API, чтобы клиент всегда получал
    актуальные коды режимов (в т.ч. для старых сессий, сохранённых
    с кодами ``KR``/``AR``).

    Args:
        value: внутренний код (``KR``/``AR``, русские «КР»/«АР»),
            публичный код либо ``None``.

    Returns:
        Публичный код режима: ``"CS"`` или ``"AI"``.
    """
    if not value:
        return MODE_CS
    return _PIPELINE_TO_MODE.get(str(value).strip().upper(), MODE_CS)


def mode_name(value: Optional[str]) -> str:
    """Человекочитаемое название режима по любому его коду."""
    return MODE_NAMES[normalize_processing_type(value)]


def public_run(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Копия словаря запуска (run) с публичным кодом режима.

    Не изменяет исходный словарь; ``processing_type`` (если есть)
    приводится к ``CS``/``AI``.
    """
    if run is None:
        return None
    result = dict(run)
    if "processing_type" in result:
        result["processing_type"] = to_public_type(result.get("processing_type"))
    return result


def public_runs(runs: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Список запусков с публичными кодами режимов (см. public_run)."""
    if not runs:
        return []
    return [public_run(r) for r in runs]


def public_session(session: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Копия словаря сессии с публичными кодами режимов.

    Приводит ``processing_type`` самой сессии и каждого запуска из ``runs``
    к публичным кодам ``CS``/``AI`` (легаси-значения старых сессий — тоже).
    """
    if session is None:
        return None
    result = dict(session)
    if "processing_type" in result:
        result["processing_type"] = to_public_type(result.get("processing_type"))
    if result.get("runs"):
        result["runs"] = public_runs(result["runs"])
    return result
