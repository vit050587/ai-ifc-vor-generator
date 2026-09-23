"""
Построение ссылок на позиции цифрового сборника для групп элементов.

Сразу после обработки IFC/PDF (до нажатия «Запустить обработку») читает
ifc_elements_output.json из корня сессии (поэлементный справочник в формате
API) и отправляет каждую группу элементов предпросмотра в API справочника
ТСН (тот же эндпоинт и тот же формат запроса, что используются при подборе
работ в режиме КР). Из ответов берутся id позиций, и формируется файл
position_links.json:

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

ВАЖНО: ключи строятся по каждой строке предпросмотра (по «Имени элемента»
из ifc_elements_output.json), а не по листовым группам группировки —
несколько nameKey могут попадать в одну листовую группу (например, стены
220 и 250 мм внутри геометрической группы «до 300»), и каждому нужен свой
запрос к API. Вариант (nameKey, part, geo), для которого API не вернул
позиций, сохраняется с пустым списком positions — фронтенд рисует для него
серую иконку «отсутствует в цифровом сборнике».

Позиции фильтруются по характеристикам группы:
  * точное совпадение («Расположение», «Материал»);
  * числовые диапазоны («Толщина: более 150 до 200» и т.п.) — по сырому
    значению геометрии из additionalCharacteristics группы.

Часть здания варианта определяется по «Расположению»/«Этажу» и уточняется
по «Типу этажа» (1-й этаж «Цокольный» → «Цоколь»; см. _resolve_part) —
синхронно с разделением смешанных групп при подборе работ
(ifc_reference_builder._element_part). Если уточнённая часть расходится
с «Расположением» элемента, в запросе «Расположение» подменяется на часть
варианта (см. _override_location) — иначе API вернёт позиции чужой части
здания, и ссылка укажет на карточку другой позиции.

Высота здания учитывается при отборе позиций: работы внутри позиций ЦС
сгруппированы по диапазонам высоты («до 57», «более 57 до 75 м»,
«более 75 до 105 м» …), и позиции, у которых для высоты здания нет ни
одной подходящей группы работ, в перечень не попадают — ссылка на них
не формируется (вариант сохраняется с пустым positions и пояснением в
reason). Высота берётся из файлов сессии (IFC_глобальные_константы.json /
height.txt) или передаётся явно; отбор позиций идёт тем же конвейером,
что и при подборе работ (api_works_lookup._filter_response_by_height →
_select_best_positions), поэтому иконка в предпросмотре ведёт на карточку
именно той позиции, работы которой попадают в финальный перечень.

Для каждого варианта группы сохраняется ровно одна (первая отфильтрованная)
позиция — у группы элементов одна ссылка на карточку цифрового сборника.
Фронтенд по выбранному варианту рисует кликабельную иконку 📎 со ссылкой
https://digital-collection.mgexp.org/building-elements/position/<id>.

Варианты, отсутствующие в цифровом сборнике (и варианты с ошибкой запроса),
дополнительно сохраняются отдельными файлами в корне сессии:
  * Отсутствующие_в_ЦС_группы.json — полный список с характеристиками;
  * Отсутствующие_в_ЦС_группы.xlsx — табличный вид для скачивания.
"""

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.core.logger import setup_logger
from src.services.api_works_lookup import (
    _fetch_one,
    _filter_response_by_height,
    _select_best_positions,
)

logger = setup_logger(__name__)

# Имя файла со ссылками в корне сессии
POSITION_LINKS_FILENAME = "position_links.json"

# Поэлементный справочник (источник групп предпросмотра)
ELEMENTS_JSON_FILENAME = "ifc_elements_output.json"

# Файлы отсутствующих в цифровом сборнике групп (корень сессии)
MISSING_GROUPS_JSON_FILENAME = "Отсутствующие_в_ЦС_группы.json"
MISSING_GROUPS_XLSX_FILENAME = "Отсутствующие_в_ЦС_группы.xlsx"

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
    по которому фронтенд группирует строки предпросмотра), иначе —
    'Без названия' (как getNameKey() во фронтенде для пустых имён).
    """
    for char in group.get("additionalCharacteristics", []) or []:
        if isinstance(char, dict) and char.get("name") == "Имя элемента":
            values = char.get("values") or []
            if values and isinstance(values[0], dict):
                raw = values[0].get("strValue", "")
                if raw and raw != "-":
                    return normalize_element_name(raw)
    return "Без названия"


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


def _extract_part(
    characteristics: Dict[str, str],
    additional: Optional[Dict[str, str]] = None,
) -> str:
    """Часть здания группы из характеристики «Расположение».

    'Надземная часть здания' → 'Надземная' и т.п.

    Если «Расположение» не задано (например, у фундаментных плит эта
    характеристика не отправляется в API — у позиции ЦС её нет), часть
    здания определяется по «Этажу» из additionalCharacteristics
    ('-1_подземный этаж_основной' → 'Подземная'), а при его отсутствии —
    по «Типу этажа» ('Подземный'/'Цокольный'/...). Пусто, если не найдено.
    """
    location = characteristics.get("Расположение", "")
    for part in ("Подземная", "Цоколь", "Надземная"):
        if part.lower() in location.lower():
            return part

    # Fallback 1: «Этаж» из additionalCharacteristics
    # ('-1_подземный этаж_основной' → 'Подземная', '-1/1_...' → 'Цоколь')
    storey = (additional or {}).get("Этаж", "")
    if storey:
        from src.services.group_excel import _get_part_from_storey_name

        part = _get_part_from_storey_name(storey)
        if part:
            return part

    # Fallback 2: «Тип этажа» ('Подземный'/'Цокольный'/...)
    storey_type = (additional or {}).get("Тип этажа", "")
    if storey_type:
        st = storey_type.lower()
        if "подзем" in st:
            return "Подземная"
        if "цоколь" in st:
            return "Цоколь"

    return ""


def _resolve_part(
    characteristics: Dict[str, str],
    additional: Optional[Dict[str, str]] = None,
) -> str:
    """Часть здания элемента с уточнением по «Типу этажа».

    База — результат _extract_part («Расположение» → «Этаж» → «Тип этажа»).
    Для элементов, отнесённых к надземной части по числовому индикатору
    «Этажа» («1_этаж_основной»), часть уточняется по «Типу этажа»:
    модели размечают 1-й этаж как «Цокольный», хотя индикатор относит
    его к надземной части. Такие элементы относятся к цокольной части
    здания — та же логика, что в ifc_reference_builder._element_part
    (разделение смешанных групп по частям здания при подборе работ).
    """
    part = _extract_part(characteristics, additional) or "Надземная"
    if part == "Надземная":
        storey_type = (additional or {}).get("Тип этажа", "")
        if storey_type:
            st = storey_type.lower()
            if st.startswith("подземн"):
                return "Подземная"
            if st.startswith("цоколь"):
                return "Цоколь"
    return part


# Названия частей здания для характеристики «Расположение» в запросе к API
# (значения — как в ifc_reference_builder._get_location_name)
_LOCATION_NAMES = {
    "Подземная": "Подземная часть здания",
    "Цоколь": "Цокольная часть здания",
    "Надземная": "Надземная часть здания",
}


def _override_location(payload: Dict[str, Any], part: str) -> bool:
    """Подменяет «Расположение» в характеристиках запроса на часть варианта.

    Часть здания варианта может быть уточнена по «Типу этажа» (1-й этаж →
    «Цоколь»), а характеристика «Расположение» элемента при этом осталась
    по числовому индикатору «Этажа» («Надземная часть здания»). Без
    подмены API вернёт позиции надземной части — ссылка укажет на карточку
    другой части здания (например, «Стена надземной части…» вместо
    «Стена цокольной части…»).

    Возвращает True, если характеристика найдена и подменена.
    """
    for char in payload.get("characteristics") or []:
        if isinstance(char, dict) and char.get("name") == "Расположение":
            values = char.get("values") or []
            if values and isinstance(values[0], dict):
                values[0]["strValue"] = _LOCATION_NAMES[part]
                return True
    return False


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
    element: Dict[str, Any],
    characteristics: Dict[str, str],
    additional: Dict[str, str],
    building_height: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Извлекает одну наиболее подходящую позицию (id + название) из ответа API.

    Порядок отбора — тот же, что при подборе работ в запуске
    (api_works_lookup.fetch_works_from_api + build_final_works_from_api),
    чтобы иконка в предпросмотре вела на карточку именно той позиции,
    работы которой попадают в финальный перечень:

      1. фильтрация по высоте здания (`_filter_response_by_height`):
         у позиций ЦС работы сгруппированы по диапазонам высоты
         («до 57», «более 57 до 75 м», «более 75 до 105 м» …) — позиции,
         у которых для высоты здания нет ни одной подходящей группы
         работ, в перечень не попадают и не должны получать ссылку;
      2. отбор позиций без лишних характеристик (`_select_best_positions`):
         позиции с характеристиками, отсутствующими у группы
         (например, «Фундаментная плита под оборудование …»), не берутся;
      3. фильтрация по характеристикам группы (расположение/материал/
         геометрические диапазоны).

    Затем берётся первая из отобранных — у группы элементов должна быть
    ровно одна ссылка на карточку цифрового сборника (как в режиме КР).

    Возвращает кортеж (позиции, reason): reason — пояснение, почему
    позиций нет (работы не подходят высоте здания), пустое при успехе.
    """
    data: List[Dict[str, Any]] = [
        item for item in (response.get("data") or [])
        if isinstance(item, dict) and item.get("id") is not None
    ]

    # 1. Фильтрация по высоте здания (как в fetch_works_from_api)
    if building_height is not None and data:
        height_filtered = _filter_response_by_height(
            {"data": data}, building_height
        ).get("data") or []
        if not height_filtered:
            # Позиции есть, но ни у одной нет работ для высоты здания —
            # в финальный перечень работы этой группы не попадут
            return [], (
                f"Работы позиций не подходят высоте здания "
                f"({building_height} м) — в перечень работ не попадают"
            )
        data = height_filtered

    # 2. Позиции без лишних характеристик (как в build_final_works_from_api)
    data = _select_best_positions(element, data)

    # 3. Фильтрация по характеристикам группы
    raw = [
        {
            "id": int(item["id"]),
            "name": item.get("fullName") or item.get("name") or "",
            "characteristics": item.get("characteristics") or [],
        }
        for item in data
    ]
    filtered = _filter_positions(raw, characteristics, additional)
    return [{"id": p["id"], "name": p["name"]} for p in filtered[:1]], ""


def _variant_material(characteristics: List[Dict[str, Any]]) -> str:
    """Материал варианта группы (из characteristics в формате API)."""
    for char in characteristics or []:
        if isinstance(char, dict) and char.get("name") == "Материал":
            values = char.get("values") or []
            if values and isinstance(values[0], dict):
                return str(values[0].get("strValue", ""))
    return ""


def _save_missing_groups(
    missing: List[Dict[str, Any]],
    session_dir: str,
) -> None:
    """Сохраняет отсутствующие в цифровом сборнике группы в JSON и XLSX.

    Аргументы:
        missing — список записей {nameKey, part, geo, buildingElementName,
            characteristics, elementCount, reason}.
        session_dir — корневая директория сессии.
    """
    json_path = os.path.join(session_dir, MISSING_GROUPS_JSON_FILENAME)
    try:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(missing, fh, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.error(f"position_links: не удалось сохранить {json_path}: {exc}")

    # Табличный вид (№ / часть здания / группа / тип позиции ЦС /
    # материал / геометрия / количество элементов / причина)
    columns = [
        "№ п/п", "Часть здания", "Группа элементов", "Тип позиции ЦС",
        "Материал", "Геометрия", "Кол-во элементов", "Причина",
    ]
    rows = []
    for index, item in enumerate(missing, 1):
        rows.append({
            "№ п/п": index,
            "Часть здания": item.get("part", ""),
            "Группа элементов": item.get("nameKey", ""),
            "Тип позиции ЦС": item.get("buildingElementName", ""),
            "Материал": _variant_material(item.get("characteristics")),
            "Геометрия": item.get("geo", ""),
            "Кол-во элементов": item.get("elementCount", 0),
            "Причина": item.get("reason", ""),
        })

    xlsx_path = os.path.join(session_dir, MISSING_GROUPS_XLSX_FILENAME)
    try:
        pd.DataFrame(rows, columns=columns).to_excel(xlsx_path, index=False)
    except Exception as exc:
        logger.error(f"position_links: не удалось сохранить {xlsx_path}: {exc}")
        return

    logger.info(
        f"position_links: сохранены отсутствующие в ЦС группы "
        f"({len(missing)} шт.): {json_path}, {xlsx_path}"
    )


def detect_building_height(session_dir: str) -> Optional[float]:
    """Высота здания сессии (м) из файлов сессии.

    Приоритет источников:
      1. IFC_глобальные_константы.json → constants.building_height_m.value
         (высота надземной части, определённая по IFC);
      2. height.txt — legacy-файл с числом.

    Возвращает None, если высоту определить не удалось.
    """
    constants_path = os.path.join(session_dir, "IFC_глобальные_константы.json")
    if os.path.isfile(constants_path):
        try:
            with open(constants_path, "r", encoding="utf-8") as fh:
                constants = json.load(fh)
            entry = (constants.get("constants") or {}).get("building_height_m") or {}
            value = entry.get("value")
            if value is not None:
                return float(value)
        except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
            logger.warning(
                f"position_links: не удалось прочитать высоту из "
                f"{constants_path}: {exc}"
            )

    height_path = os.path.join(session_dir, "height.txt")
    if os.path.isfile(height_path):
        try:
            with open(height_path, "r", encoding="utf-8") as fh:
                return float(fh.read().strip())
        except (ValueError, OSError) as exc:
            logger.warning(
                f"position_links: не удалось прочитать высоту из "
                f"{height_path}: {exc}"
            )
    return None


def build_position_links(
    session_dir: str,
    grouped_filename: str = "ifc_raw_elements_grouped.json",
    building_height: Optional[float] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Запрашивает id позиций для всех групп предпросмотра и сохраняет
    position_links.json (+ файлы отсутствующих групп).

    Группы берутся из ifc_elements_output.json — по каждой строке
    предпросмотра (nameKey = «Имя элемента» без цифрового ID), а не по
    листовым группам группировки: несколько nameKey могут попадать в одну
    листовую группу (например, стены 220 и 250 мм внутри геометрической
    группы «до 300»), и каждой нужен свой запрос к API.

    Аргументы:
        session_dir — корневая директория сессии.
        grouped_filename — устаревший параметр (совместимость сигнатуры),
            не используется: источник групп — ifc_elements_output.json.
        building_height — высота здания, м; отбрасывает позиции, у
            которых нет работ для этой высоты (тот же фильтр, что при
            подборе работ). None — высота определяется автоматически
            из файлов сессии (IFC_глобальные_константы.json /
            height.txt); если и там нет — позиции выбираются без
            фильтрации по высоте.

    Возвращает:
        Словарь {имя_элемента: [{"part": ..., "geo": ..., "positions": [...],
        "reason": ...}, ...]}.
        Варианты, отсутствующие в ЦС или не подходящие высоте здания,
        сохраняются с пустым positions и пояснением в reason.
        При отсутствии элементов/ошибках запросов возвращается то, что
        удалось собрать (возможно, пустой словарь).
    """
    if building_height is None:
        building_height = detect_building_height(session_dir)
    if building_height is not None:
        logger.info(
            f"position_links: высота здания {building_height} м — "
            f"позиции отбираются с учётом высоты"
        )
    else:
        logger.warning(
            "position_links: высота здания не определена — "
            "позиции отбираются без учёта высоты"
        )

    elements_path = os.path.join(session_dir, ELEMENTS_JSON_FILENAME)
    if not os.path.isfile(elements_path):
        logger.warning(
            f"position_links: не найден {elements_path} — ссылки не построены"
        )
        return {}

    try:
        with open(elements_path, "r", encoding="utf-8") as fh:
            elements = json.load(fh)
    except Exception as exc:
        logger.error(f"position_links: ошибка чтения {elements_path}: {exc}")
        return {}

    if not isinstance(elements, list) or not elements:
        logger.warning("position_links: пустой список элементов — ссылки не построены")
        return {}

    # Варианты групп предпросмотра: {nameKey: {(part, geo): [элементы]}}
    variants: Dict[str, Dict[Tuple[str, str], List[Dict[str, Any]]]] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        name_key = _get_name_key(element)
        if not name_key:
            continue

        characteristics = _chars_to_dict(element.get("characteristics"))
        additional = _chars_to_dict(element.get("additionalCharacteristics"))
        part = _resolve_part(characteristics, additional)
        geo = _extract_geo(characteristics)

        variants.setdefault(name_key, {}).setdefault((part, geo), []).append(element)

    total_variants = sum(len(v) for v in variants.values())
    logger.info(
        f"position_links: групп предпросмотра={len(variants)}, "
        f"вариантов (часть/геометрия)={total_variants}"
    )

    # {nameKey: [(part, geo, positions)]}, отсутствующие в ЦС варианты
    links: Dict[str, List[Dict[str, Any]]] = {}
    missing: List[Dict[str, Any]] = []
    errors = 0
    index = 0

    for name_key in variants:
        links[name_key] = []
        for (part, geo), elems in sorted(variants[name_key].items()):
            index += 1
            first = elems[0]

            # Запрос к API — первый элемент варианта в формате API
            # (служебные поля с префиксом '_' в запрос не входят).
            # Копия в глубину: «Расположение» может быть подменено на часть
            # варианта (см. _override_location) — исходный элемент менять нельзя.
            payload = {
                key: copy.deepcopy(value) for key, value in first.items()
                if not key.startswith('_')
            }
            characteristics = _chars_to_dict(first.get("characteristics"))
            additional = _chars_to_dict(first.get("additionalCharacteristics"))

            # Уточнённая по «Типу этажа» часть варианта (например, «Цоколь»
            # для 1-го этажа) может расходиться с «Расположением» элемента
            # («Надземная часть здания» по индикатору «Этажа») — подменяем,
            # иначе API вернёт позиции чужой части и ссылка укажет на
            # карточку другого элемента цифрового сборника.
            if part != "Надземная":
                _override_location(payload, part)

            positions: List[Dict[str, Any]] = []
            reason = ""
            try:
                response = _fetch_one(payload)
                positions, reason = _extract_positions(
                    response, first, characteristics, additional,
                    building_height=building_height,
                )
            except Exception as exc:
                errors += 1
                reason = f"Ошибка запроса к API ТСН: {exc}"
                logger.warning(
                    f"position_links: запрос {index}/{total_variants} "
                    f"({name_key}, {part}, {geo}) не удался: {exc}"
                )

            links[name_key].append({
                "part": part,
                "geo": geo,
                "positions": positions,
                "reason": reason,
            })

            # Вариант отсутствует в цифровом сборнике (или ошибка запроса,
            # или работы позиции не подходят высоте здания) — фиксируем
            # для отдельного файла отсутствующих групп
            if not positions:
                missing.append({
                    "nameKey": name_key,
                    "part": part,
                    "geo": geo,
                    "buildingElementName": first.get("buildingElementName", ""),
                    "characteristics": first.get("characteristics") or [],
                    "elementCount": len(elems),
                    "reason": reason or "Позиции в цифровом сборнике не найдены",
                })

    output_path = os.path.join(session_dir, POSITION_LINKS_FILENAME)
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(links, fh, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.error(f"position_links: не удалось сохранить {output_path}: {exc}")
        return links

    # Отдельные файлы отсутствующих в ЦС групп (JSON + XLSX)
    _save_missing_groups(missing, session_dir)

    total = sum(len(v["positions"]) for vs in links.values() for v in vs)
    logger.info(
        f"position_links: сохранён {output_path} "
        f"(групп={len(links)}, позиций={total}, "
        f"отсутствующих в ЦС={len(missing)}, ошибок={errors})"
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
