"""
Построение карты соответствия МССК-кодов МАТЕРИАЛОВ и названий групп.

Источник: data/materials_mssk_nested.json — иерархический справочник:
  category (code_category)
    → purpose (code_purpose)
      → class (code_class, class_RU)
        → subclass (code, subclass_RU)
          → material (code, material)

Карта строится один раз (с кешированием) и используется при группировке
элементов в режиме АР: поле «Свойство::IfcMaterialLayer::Name» содержит
один или несколько материалов вида ``Название (СТ xx xx xx)``.
По коду в скобках определяется имя группы материалов.

Правила группировки:
  * 0 материалов / код не найден  → «Прочее»
  * 1 материал                     → группа с именем из справочника материалов
  * >1 материала                   → «Многослойные»
"""

import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Tuple

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# Путь к справочнику материалов относительно корня проекта
_DEFAULT_MATERIALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "materials_mssk_nested.json",
)

OTHER_LABEL = "Прочее"
MULTILAYER_LABEL = "Многослойные"

# Порядок сортировки «Многослойные»: после всех известных кодов, но до «Прочее».
_MULTILAYER_ORDER = 10 ** 9

# Шаблон поиска кода материала в скобках: (СТ 10 14 20 14), (СТ 10 01), ...
_CODE_RE = re.compile(r"\((СТ\s*[\d][\d\s]*)\)")


@lru_cache(maxsize=4)
def build_materials_lookup(
    data_path: str = _DEFAULT_MATERIALS_PATH,
) -> Tuple[Dict[str, Dict[str, object]], Tuple[str, ...]]:
    """Строит плоскую карту code → {name, order} для справочника материалов.

    При совпадении на нескольких уровнях выигрывает самый глубокий
    (material → subclass → class → purpose → category). Порядок order
    назначается по DFS-обходу справочника.
    """
    lookup: Dict[str, Dict[str, object]] = {}
    order_counter = 0

    def register(code, name) -> None:
        nonlocal order_counter
        if not code or not name:
            return
        code = str(code).strip()
        name = str(name).strip()
        if not code or not name:
            return
        # Более глубокий уровень регистрируется позже и перекрывает мелкий.
        lookup[code] = {"name": name, "order": order_counter}
        order_counter += 1

    try:
        with open(data_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.error(f"Справочник материалов не найден: {data_path}")
        return {}, ()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(f"Ошибка чтения справочника материалов: {exc}")
        return {}, ()

    if not isinstance(data, list):
        logger.error("Справочник материалов должен содержать массив категорий")
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
                    for mat in sub.get("materials", []) or []:
                        if not isinstance(mat, dict):
                            continue
                        register(mat.get("code"), mat.get("material"))

    ordered_codes = tuple(
        code for code, _ in sorted(lookup.items(), key=lambda kv: kv[1]["order"])
    )
    logger.info(f"Карта материалов построена: {len(lookup)} кодов")
    return lookup, ordered_codes


def parse_material_segments(value) -> List[Tuple[str, str]]:
    """Разбирает строку материалов вида ``Имя (СТ ...)Имя2 (СТ ...)``.

    Возвращает список пар (имя_материала, код). Имя — текст между концом
    предыдущего кода и началом текущего. Если значение пустое — пустой список.
    """
    if value is None:
        return []
    s = str(value).strip()
    if not s or s == "-" or s.lower() == "nan":
        return []

    segments: List[Tuple[str, str]] = []
    prev_end = 0
    for m in _CODE_RE.finditer(s):
        code = m.group(1).strip()
        # Нормализуем пробелы внутри кода: "СТ  10 14" -> "СТ 10 14"
        code = re.sub(r"\s+", " ", code)
        name = s[prev_end:m.start()].strip()
        segments.append((name, code))
        prev_end = m.end()
    return segments


def resolve_material_group(
    value, lookup: Dict[str, Dict[str, object]] = None
) -> Tuple[str, int]:
    """Разрешает значение поля материалов в (имя_группы, порядок_сортировки).

    * 0 материалов / кода нет в справочнике → («Прочее», inf)
    * 1 материал                          → (имя из справочника, order)
    * >1 материала                        → («Многослойные», _MULTILAYER_ORDER)
    """
    if lookup is None:
        lookup, _ = build_materials_lookup()

    segments = parse_material_segments(value)
    if not segments:
        return OTHER_LABEL, float("inf")
    if len(segments) > 1:
        return MULTILAYER_LABEL, _MULTILAYER_ORDER

    code = segments[0][1]
    info = lookup.get(code)
    if info:
        return info["name"], info["order"]
    return OTHER_LABEL, float("inf")


# Колонки-источники материалов из Pset «ExpCheck_*» (MGE_MaterialCode/MGE_Material).
# Используются как fallback, когда у элемента нет IfcMaterialLayer::Name.
_MGE_MATERIAL_COLS = (
    "MGE_MaterialCode",
    "MGE_Material",
    "MGE_MaterialCode1",
    "MGE_Material1",
    "MGE_MaterialCode2",
    "MGE_Material2",
)

# Значения, считающиеся «пустыми» (в т.ч. NaN из Excel/pandas).
_EMPTY_VALUES = ("", "-", "nan", "none", "nat", "null")


def _is_empty_value(val) -> bool:
    """Проверяет, что значение материала пустое (None, NaN, '-', '')."""
    if val is None:
        return True
    return str(val).strip().lower() in _EMPTY_VALUES


def extract_material_value(row: Dict[str, Any]) -> str:
    """Собирает строку материалов элемента с учётом fallback-источников.

    Приоритет:
      1. «Свойство::IfcMaterialLayer::Name» — уже содержит «Имя (СТ ...)»;
      2. пары MGE_MaterialCode/MGE_Material из Pset «ExpCheck_*» (для дверей,
         окон, витражей, облицовок и т.п., где нет слоёв материала).

    Из пар MGE строится строка вида «Имя (СТ ...)Имя2 (СТ ...)», которую
    умеет разбирать parse_material_segments(). Повторяющиеся коды
    (например MGE_MaterialCode1 == MGE_MaterialCode2 у двери) отбрасываются,
    чтобы элемент не попал в «Многослойные» из-за дубликата.

    Если ни одного источника нет — возвращается пустая строка (→ «Прочее»).
    """
    if row is None:
        return ""

    # 1) Основной источник — слои материала.
    for key in row:
        if "IfcMaterialLayer::Name" in str(key):
            val = row.get(key)
            if not _is_empty_value(val):
                return str(val).strip()
            break

    # 2) Fallback — пары MGE_MaterialCode/MGE_Material из Pset ExpCheck_*.
    #    Группируем колонки по Pset-префиксу и суффиксу номера (1, 2, ...),
    #    чтобы корректно сопоставить Code1↔Material1, Code2↔Material2 и т.д.
    from collections import OrderedDict

    # pset_prefix → {suffix → {"code": ..., "name": ...}}
    pset_pairs: "OrderedDict[str, OrderedDict[str, Dict[str, str]]]" = OrderedDict()

    for key in row:
        skey = str(key)
        if "MGE_Material" not in skey:
            continue
        idx = skey.rfind("::")
        if idx == -1:
            continue
        pset = skey[:idx]          # «Свойство::ExpCheck_Door»
        short = skey[idx + 2:]     # «MGE_MaterialCode1»
        if short not in _MGE_MATERIAL_COLS:
            continue
        val = row.get(key)
        if _is_empty_value(val):
            continue
        val = str(val).strip()

        # Суффикс: '' для MGE_MaterialCode/MGE_Material, '1', '2' и т.д.
        if short.startswith("MGE_MaterialCode"):
            suffix = short[len("MGE_MaterialCode"):]
        else:
            suffix = short[len("MGE_Material"):]

        pairs = pset_pairs.setdefault(pset, OrderedDict())
        pair = pairs.setdefault(suffix, {"code": None, "name": None})
        if short.startswith("MGE_MaterialCode"):
            pair["code"] = val
        else:
            pair["name"] = val

    if not pset_pairs:
        return ""

    parts = []
    seen_codes = set()
    for pairs in pset_pairs.values():
        for pair in pairs.values():
            code = pair["code"]
            name = pair["name"]
            if code:
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                parts.append(f"{name or ''} ({code})" if name else f"({code})")
            elif name:
                parts.append(name)
    return "".join(parts)
