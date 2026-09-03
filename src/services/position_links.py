"""
Построение ссылок на позиции цифрового сборника для групп элементов.

Сразу после обработки IFC/PDF (до нажатия «Запустить обработку») читает
ifc_raw_elements_grouped.json из корня сессии и отправляет каждую группу
элементов в API справочника ТСН (тот же эндпоинт и тот же формат запроса,
что используются при подборе работ в режиме КР). Из ответов берутся id
позиций, и формируется файл position_links.json:

    {
        "<Имя элемента без цифрового ID>": [
            {
                "part": "Надземная",          // часть здания группы
                "geo": "до 200",              // геометрический диапазон группы
                "positions": [
                    {"id": 1391, "name": "Стена надземной части здания толщиной до 200 мм"}
                ]
            },
            ...
        ],
        ...
    }

Ключ — имя элемента (колонка «Имя» таблицы предпросмотра) без хвостового
«:1234567» — совпадает с nameKey групп в веб-интерфейсе. Один nameKey может
иметь несколько вариантов (разные части здания / геометрия) — фронтенд
выбирает вариант по контексту своей группы.

Позиции фильтруются по характеристикам группы:
  * точное совпадение («Расположение», «Материал»);
  * числовые диапазоны («Толщина: более 150 до 200» и т.п.) — по сырому
    значению геометрии из additionalCharacteristics группы.

Фронтенд по выбранному варианту рисует кликабельную иконку 📎 со ссылкой
https://digital-collection.mgexp.org/building-elements/position/<id>.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from src.core.logger import setup_logger
from src.services.api_works_lookup import _fetch_one

logger = setup_logger(__name__)

# Имя файла со ссылками в корне сессии
POSITION_LINKS_FILENAME = "position_links.json"

# Базовый URL страницы позиции в цифровом сборнике
POSITION_URL_TEMPLATE = (
    "https://digital-collection.mgexp.org/building-elements/position/{id}"
)

# Хвост «:1234567» в имени элемента из Revit
_TRAILING_ID_RE = re.compile(r":\d+\s*$")

# Числа внутри диапазонных значений («более 150 до 200», «до 10», «≤ 300»)
_RANGE_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")

# Ключи геометрии в характеристиках позиций API и соответствующие
# ключи сырых значений в additionalCharacteristics группы (по приоритету).
_GEOMETRY_RAW_KEYS = {
    "толщина": ["Толщина_мм", "Толщина"],
    "сторона": ["Ширина_сечения_мм", "Толщина_мм"],
    "ширина": ["Ширина_сечения_мм", "Толщина_мм"],
    "площадь": [
        "Площадь_чистая_вся_м2", "Площадь_общая_вся_м2",
        "Площадь_чистая_м2", "Площадь_общая_м2", "Площадь",
    ],
    "длина": ["Длина_мм", "Длина"],
    "высота": ["Высота_мм", "Высота"],
    "периметр": ["Периметр_мм", "Периметр"],
    "объём": ["Объём_чистый_м3", "Объём"],
    "объем": ["Объём_чистый_м3", "Объём"],
}

# Характеристики, сверяемые с группой точным совпадением строк
_EXACT_MATCH_KEYS = ("расположение", "материал")


def normalize_element_name(full_name: str) -> str:
    """Убирает цифровой ID в конце имени элемента.

    'Базовая стена:ADSK_Бетон В25_200 мм:3200941' →
    'Базовая стена:ADSK_Бетон В25_200 мм'
    """
    name = str(full_name or "").strip()
    return _TRAILING_ID_RE.sub("", name).strip()


def _get_name_key(group: Dict[str, Any]) -> str:
    """Определяет ключ группы (имя элемента без цифрового ID).

    Приоритет: additionalCharacteristics['Имя элемента'] (имя из Revit,
    по которому фронтенд группирует строки), иначе buildingElementName.
    """
    for char in group.get("additionalCharacteristics", []) or []:
        if isinstance(char, dict) and char.get("name") == "Имя элемента":
            values = char.get("values") or []
            if values and isinstance(values[0], dict):
                raw = values[0].get("strValue", "")
                if raw and raw != "-":
                    return normalize_element_name(raw)
    return normalize_element_name(group.get("buildingElementName", ""))


def _chars_to_dict(chars: List[Dict[str, Any]]) -> Dict[str, str]:
    """Приводит характеристики группы к виду {name: strValue}."""
    result: Dict[str, str] = {}
    for char in chars or []:
        if not isinstance(char, dict):
            continue
        values = char.get("values") or []
        if values and isinstance(values[0], dict):
            value = values[0].get("strValue", "")
            if value and value != "-":
                result[str(char.get("name", ""))] = str(value)
    return result


def _extract_part(characteristics: Dict[str, str]) -> str:
    """Часть здания группы из характеристики «Расположение».

    'Надземная часть здания' → 'Надземная' и т.п. Пусто, если не найдено.
    """
    location = characteristics.get("Расположение", "")
    for part in ("Подземная", "Цоколь", "Надземная"):
        if part.lower() in location.lower():
            return part
    return ""


def _extract_geo(characteristics: Dict[str, str]) -> str:
    """Геометрический диапазон группы ('до 200') из её характеристик."""
    for name, value in characteristics.items():
        if name.lower() in _GEOMETRY_RAW_KEYS:
            return str(value)
    return ""


def _parse_range(value: str) -> Optional[Tuple[float, float]]:
    """Разбирает диапазонное значение характеристики позиции.

    'до 200' → (0, 200]; 'более 150 до 200' → (150, 200];
    'более 20' → (20, +inf); '≤ 300' → (0, 300]; '6-8' → (6, 8].
    Возвращает None, если диапазон разобрать не удалось.
    """
    s = str(value or "").lower().replace(",", ".").replace(" ", "")
    nums = [float(x) for x in _RANGE_NUM_RE.findall(s)]
    if not nums:
        return None

    low, high = 0.0, float("inf")
    if "более" in s or ">" in s:
        low = nums[0]
        if len(nums) >= 2:
            high = nums[1]
    elif "до" in s or "≤" in s or "<" in s:
        high = nums[-1]
    elif "-" in s and len(nums) >= 2:
        low, high = nums[0], nums[1]
    else:
        # Одиночное число — точное значение
        low = high = nums[0]
    return low, high


def _raw_geometry_value(
    pos_char_name: str,
    additional: Dict[str, str],
) -> Optional[float]:
    """Сырое числовое значение геометрии группы для характеристики позиции.

    Ищет в additionalCharacteristics группы ключ по карте
    _GEOMETRY_RAW_KEYS (сначала детальный 'Толщина_мм', затем общий).
    """
    name_lower = str(pos_char_name or "").lower()
    for geo_key, candidates in _GEOMETRY_RAW_KEYS.items():
        if geo_key in name_lower:
            for candidate in candidates:
                raw = additional.get(candidate)
                if raw is None:
                    continue
                try:
                    return float(str(raw).replace(",", "."))
                except (ValueError, TypeError):
                    continue
            return None
    return None


def _position_matches(
    position: Dict[str, Any],
    characteristics: Dict[str, str],
    additional: Dict[str, str],
) -> bool:
    """Проверяет, соответствует ли позиция характеристикам группы.

    Правила:
      * «Расположение»/«Материал» позиции должны совпадать со значениями
        группы (если у группы они заданы);
      * диапазонные геометрические характеристики позиции («Толщина:
        более 150 до 200» и т.п.) должны содержать сырое значение
        геометрии группы из additionalCharacteristics.
    """
    pos_chars = position.get("characteristics") or []

    for pos_char in pos_chars:
        if not isinstance(pos_char, dict):
            continue
        name = str(pos_char.get("name", ""))
        value = str(pos_char.get("value", ""))
        name_lower = name.lower()

        # Точное совпадение (Расположение) и вхождение подстроки (Материал:
        # у группы нормализованное 'Бетон', у позиции 'Железобетон' и т.п.)
        if name_lower in _EXACT_MATCH_KEYS:
            group_value = characteristics.get(name)
            if group_value:
                gv, pv = group_value.lower(), value.lower()
                if gv != pv and gv not in pv and pv not in gv:
                    return False
            continue

        # Числовой диапазон по сырому значению группы
        raw = _raw_geometry_value(name, additional)
        if raw is None:
            continue
        parsed = _parse_range(value)
        if parsed is None:
            continue
        low, high = parsed
        if not (low < raw <= high):
            return False

    return True


def _filter_positions(
    positions: List[Dict[str, Any]],
    characteristics: Dict[str, str],
    additional: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Фильтрует позиции по характеристикам группы.

    Если фильтр отсеял все позиции (например, единицы измерения группы
    и позиции не совпадают) — возвращает неотфильтрованный список,
    чтобы не остаться совсем без ссылок.
    """
    filtered = [
        pos for pos in positions
        if _position_matches(pos, characteristics, additional)
    ]
    return filtered if filtered else positions


def _extract_positions(
    response: Dict[str, Any],
    characteristics: Dict[str, str],
    additional: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Извлекает отфильтрованный список позиций (id + название) из ответа API."""
    raw: List[Dict[str, Any]] = []
    for item in (response.get("data") or []):
        if not isinstance(item, dict):
            continue
        pos_id = item.get("id")
        if pos_id is None:
            continue
        raw.append({
            "id": int(pos_id),
            "name": item.get("fullName") or item.get("name") or "",
            "characteristics": item.get("characteristics") or [],
        })

    filtered = _filter_positions(raw, characteristics, additional)
    return [{"id": p["id"], "name": p["name"]} for p in filtered]


def build_position_links(
    session_dir: str,
    grouped_filename: str = "ifc_raw_elements_grouped.json",
) -> Dict[str, List[Dict[str, Any]]]:
    """Запрашивает id позиций для всех групп и сохраняет position_links.json.

    Аргументы:
        session_dir — корневая директория сессии.
        grouped_filename — имя JSON с группами элементов в формате API
            (ifc_raw_elements_grouped.json).

    Возвращает:
        Словарь {имя_элемента: [{"part": ..., "geo": ..., "positions": [...]}, ...]}.
        При отсутствии групп/ошибках запросов возвращается то, что удалось
        собрать (возможно, пустой словарь).
    """
    grouped_path = os.path.join(session_dir, grouped_filename)
    if not os.path.isfile(grouped_path):
        logger.warning(
            f"position_links: не найден {grouped_path} — ссылки не построены"
        )
        return {}

    try:
        with open(grouped_path, "r", encoding="utf-8") as fh:
            groups = json.load(fh)
    except Exception as exc:
        logger.error(f"position_links: ошибка чтения {grouped_path}: {exc}")
        return {}

    if not isinstance(groups, list) or not groups:
        logger.warning("position_links: пустой список групп — ссылки не построены")
        return {}

    # {nameKey: {(part, geo): {"id": position}}}
    variants: Dict[str, Dict[Tuple[str, str], Dict[int, Dict[str, Any]]]] = {}
    errors = 0

    for index, group in enumerate(groups, 1):
        if not isinstance(group, dict):
            continue
        name_key = _get_name_key(group)
        if not name_key:
            continue

        characteristics = _chars_to_dict(group.get("characteristics"))
        additional = _chars_to_dict(group.get("additionalCharacteristics"))
        part = _extract_part(characteristics) or "Надземная"
        geo = _extract_geo(characteristics)

        try:
            response = _fetch_one(group)
        except Exception as exc:
            errors += 1
            logger.warning(
                f"position_links: запрос {index}/{len(groups)} ({name_key}) "
                f"не удался: {exc}"
            )
            continue

        positions = _extract_positions(response, characteristics, additional)
        if not positions:
            continue

        group_variants = variants.setdefault(name_key, {})
        bucket = group_variants.setdefault((part, geo), {})
        for pos in positions:
            bucket.setdefault(pos["id"], pos)

    # Собираем итоговый формат
    links: Dict[str, List[Dict[str, Any]]] = {}
    for name_key, group_variants in variants.items():
        links[name_key] = [
            {
                "part": part,
                "geo": geo,
                "positions": sorted(bucket.values(), key=lambda p: p["id"]),
            }
            for (part, geo), bucket in sorted(group_variants.items())
        ]

    output_path = os.path.join(session_dir, POSITION_LINKS_FILENAME)
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(links, fh, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.error(f"position_links: не удалось сохранить {output_path}: {exc}")
        return links

    total = sum(len(v["positions"]) for vs in links.values() for v in vs)
    logger.info(
        f"position_links: сохранён {output_path} "
        f"(групп={len(links)}, позиций={total}, ошибок={errors})"
    )
    return links


def read_position_links(session_dir: str) -> Dict[str, List[Dict[str, Any]]]:
    """Читает position_links.json сессии (пустой словарь, если файла нет)."""
    path = os.path.join(session_dir, POSITION_LINKS_FILENAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"position_links: ошибка чтения {path}: {exc}")
        return {}
