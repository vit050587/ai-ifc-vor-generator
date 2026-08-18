"""
Построение карты соответствия MSSK-кодов и названий групп.

Источник: data/elements_mssk_nested.json — иерархический справочник:
  category (code_category) → purpose (code_purpose) → class (code_class) → subclass (code)

Карта строится один раз (с кешированием) и отдаётся на фронтенд,
чтобы превью таблицы группировалось по «Код мсск».
Если код отсутствует в справочнике — группа называется «Прочее».
"""

import json
import os
from functools import lru_cache
from typing import Dict, Tuple

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# Путь к справочнику относительно корня проекта
_DEFAULT_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "elements_mssk_nested.json",
)

OTHER_LABEL = "Прочее"


@lru_cache(maxsize=4)
def build_mssk_lookup(data_path: str = _DEFAULT_DATA_PATH) -> Tuple[Dict[str, Dict[str, object]], Tuple[str, ...]]:
    """Строит плоскую карту code → {name, order}.

    При совпадении на нескольких уровнях выигрывает самый глубокий
    (subclass → class → purpose → category). Порядок order назначается по DFS
    обходу справочника, чтобы группы в превью шли в логичной последовательности.
    Неизвестные коды получают order = infinity и имя «Прочее».
    """
    lookup: Dict[str, Dict[str, object]] = {}
    order_counter = 0

    def register(code: str, name: str) -> None:
        nonlocal order_counter
        if not code or not name:
            return
        code = code.strip()
        # Глубокий уровень перезаписывает мелкий только если ещё не задан
        # (мы обходим в порядке category→purpose→class→subclass, поэтому
        # более глубокие регистрируются позже и корректно перекрывают).
        lookup[code] = {"name": name.strip(), "order": order_counter}
        order_counter += 1

    try:
        with open(data_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.error(f"MSSK-справочник не найден: {data_path}")
        return {}, ()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(f"Ошибка чтения MSSK-справочника: {exc}")
        return {}, ()

    if not isinstance(data, list):
        logger.error("MSSK-справочник должен содержать массив категорий")
        return {}, ()

    for category in data:
        if not isinstance(category, dict):
            continue
        register(category.get("code_category"), category.get("category"))
        for purpose in category.get("purposes", []) or []:
            if not isinstance(purpose, dict):
                continue
            register(purpose.get("code_purpose"), purpose.get("purpose"))
            for cls in purpose.get("classes", []) or []:
                if not isinstance(cls, dict):
                    continue
                register(cls.get("code_class"), cls.get("class_RU"))
                for sub in cls.get("subclasses", []) or []:
                    if not isinstance(sub, dict):
                        continue
                    register(sub.get("code"), sub.get("subclass_RU"))

    # Упорядоченный список кодов (для детерминированной сортировки групп)
    ordered_codes = tuple(
        code for code, _ in sorted(lookup.items(), key=lambda kv: kv[1]["order"])
    )
    logger.info(f"MSSK-карта построена: {len(lookup)} кодов")
    return lookup, ordered_codes


def get_mssk_code_map(data_path: str = _DEFAULT_DATA_PATH) -> Dict[str, Dict[str, object]]:
    """Возвращает карту code → {name, order} для отдачи на фронтенд."""
    lookup, _ = build_mssk_lookup(data_path)
    return {code: dict(info) for code, info in lookup.items()}


def resolve_mssk_group(code, lookup: Dict[str, Dict[str, object]] = None) -> Tuple[str, int]:
    """Разрешает код в (имя_группы, порядок_сортировки).

    Возвращает («Прочее», бесконечность) для неизвестных/пустых кодов.
    """
    if lookup is None:
        lookup, _ = build_mssk_lookup()
    if code is None:
        return OTHER_LABEL, float("inf")
    code_str = str(code).strip()
    if not code_str or code_str == "-":
        return OTHER_LABEL, float("inf")
    info = lookup.get(code_str)
    if not info:
        return OTHER_LABEL, float("inf")
    return info["name"], info["order"]
