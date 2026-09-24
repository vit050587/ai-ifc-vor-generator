# works_table_selector.py
"""Подбор таблиц работ ГЭСН/ТСН-2001 к элементам IFC (режим АР).

Реализует алгоритм из data/algorithm.md:
  1. Фильтрация свойств элемента (только заполненные).
  2. Определение сборников-кандидатов по IfcClass + PredefinedType
     (data/ifc_to_collections.json).
  3. Уточнение по коду МССК (data/msck_elements_compact.json).
  4. Главный переключатель технологии: In-situ/монолит -> Сб. 6,
     Precast/сборный -> Сб. 7, кирпич/кладка -> Сб. 8 и т.д.
  5. Выбор таблиц по ключевым словам (data/tree_work_compact.json).
  6. Применение work_type (COMPLEX/SEPARATE) — правила из
     data/works_classification.json.
  7. Учёт глобальных констант проекта (global_constants).
  8. Объёмы работ из QTO элемента.

Выход: JSON «Подобранные_таблицы_работ.json» — для каждого элемента свой
список подобранных таблиц работ (сборник, таблицы, тип, объём, константы).
"""

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from src.core.logger import setup_logger

logger = setup_logger(__name__)

DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)

# Имя выходного JSON-файла с подбором таблиц работ
WORKS_TABLES_JSON_FILENAME = "Подобранные_таблицы_работ.json"

# --------------------------------------------------------------------------
#  Загрузка справочников (ленивая, с кешированием на уровне модуля)
# --------------------------------------------------------------------------

_CLASSIFICATION: Optional[dict] = None
_TREE: Optional[list] = None
_IFC_COLLECTIONS: Optional[dict] = None
_MSSK: Optional[dict] = None
_TABLE_INDEX: Optional[Dict[str, dict]] = None
_MSSK_INDEX: Optional[Dict[str, str]] = None


def _load_json(name: str):
    path = os.path.join(DATA_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _classification() -> dict:
    global _CLASSIFICATION
    if _CLASSIFICATION is None:
        _CLASSIFICATION = _load_json("works_classification.json")
    return _CLASSIFICATION


def _tree() -> list:
    global _TREE
    if _TREE is None:
        _TREE = _load_json("tree_work_compact.json")
    return _TREE


def _ifc_collections() -> dict:
    global _IFC_COLLECTIONS
    if _IFC_COLLECTIONS is None:
        _IFC_COLLECTIONS = _load_json("ifc_to_collections.json")
    return _IFC_COLLECTIONS


def _mssk_tree() -> dict:
    global _MSSK
    if _MSSK is None:
        _MSSK = _load_json("msck_elements_compact.json")
    return _MSSK


def _norm_key(name: str) -> str:
    """Ключ сравнения названий сборников (схлопывание пробелов, lower)."""
    return re.sub(r"\s+", " ", str(name)).strip().lower()


def _collection_number(collection_name: str) -> Optional[int]:
    """Номер сборника из названия: 'Сборник  6. ...' -> 6."""
    m = re.search(r"сборник\s*(\d+)", str(collection_name).lower())
    return int(m.group(1)) if m else None


def _table_code(table_name: str) -> Optional[str]:
    """Код таблицы: 'Таблица 3.6-8. ...' -> '3.6-8'."""
    m = re.search(r"(\d+\.\d+-\d+)", str(table_name))
    return m.group(1) if m else None


def _table_index() -> Dict[str, dict]:
    """Индекс всех таблиц дерева работ: код -> {name, collection, dept, section}."""
    global _TABLE_INDEX
    if _TABLE_INDEX is not None:
        return _TABLE_INDEX

    index: Dict[str, dict] = {}
    for coll in _tree():
        coll_name = coll.get("collection", "")
        for dept in coll.get("departments", []):
            dept_name = dept.get("dept", "")
            for part in dept.get("parts", []):
                part_name = part.get("part", "")
                for tbl in part.get("tables", []):
                    tbl_name = tbl.get("table", "")
                    code = _table_code(tbl_name)
                    if not code:
                        continue
                    # Название без префикса 'Таблица X.Y-Z. '
                    clean_name = re.sub(r"^Таблица\s+[\d.]+\-\d+\.\s*", "", tbl_name).strip()
                    index[code] = {
                        "code": code,
                        "name": clean_name or tbl_name,
                        "collection": coll_name,
                        "department": dept_name,
                        "section": part_name,
                    }
    _TABLE_INDEX = index
    return index


def _mssk_index() -> Dict[str, str]:
    """Индекс МССК: код -> название узла."""
    global _MSSK_INDEX
    if _MSSK_INDEX is not None:
        return _MSSK_INDEX

    index: Dict[str, str] = {}

    def walk(node: dict) -> None:
        code = str(node.get("code", "")).strip()
        if code:
            index[code] = str(node.get("name", "")).strip()
        for child in node.get("children", []) or []:
            walk(child)

    walk(_mssk_tree())
    _MSSK_INDEX = index
    return index


def get_constants_schema() -> List[dict]:
    """Схема глобальных констант из data/works_classification.json.

    Используется веб-интерфейсом для отрисовки полей выбора констант.
    """
    return _classification().get("global_constants", [])


# --------------------------------------------------------------------------
#  Шаг 1. Нормализация элемента (фильтрация пустых свойств)
# --------------------------------------------------------------------------

# Приведение конкретных IFC-подтипов к базовым классам алгоритма
_CLASS_FALLBACK: Dict[str, str] = {
    "IfcWallStandardCase": "IfcWall",
    "IfcWallElementedCase": "IfcWall",
    "IfcSlabStandardCase": "IfcSlab",
    "IfcSlabElementedCase": "IfcSlab",
    "IfcDoorStandardCase": "IfcDoor",
    "IfcWindowStandardCase": "IfcWindow",
    "IfcStairFlight": "IfcStair",
    "IfcRamp": "IfcSlab",
    "IfcRampFlight": "IfcSlab",
    "IfcPlateStandardCase": "IfcPlate",
    "IfcMemberStandardCase": "IfcMember",
    "IfcCoveringStandardCase": "IfcCovering",
}


def _normalize_ifc_class(ifc_class: str) -> str:
    """Приводит подтип IFC к базовому классу (IfcStairFlight -> IfcStair)."""
    return _CLASS_FALLBACK.get(ifc_class, ifc_class)


def _clean(value: Any) -> str:
    """Строковое значение без пустых заглушек ('-', '', 'nan', 'None')."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("", "-", "nan", "none", "null"):
        return ""
    return s


def _find_key(row: dict, *substrings: str) -> Optional[str]:
    """Ищет ключ словаря, содержащий все подстроки (без учёта регистра)."""
    for key in row.keys():
        kl = str(key).lower().replace("_", "").replace(" ", "")
        if all(s.replace("_", "").replace(" ", "").lower() in kl for s in substrings):
            return key
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        s = str(value).replace(",", ".").replace(" ", "")
        if s in ("", "-"):
            return None
        val = float(s)
        return val
    except (ValueError, TypeError):
        return None


def _extract_quantities(row: dict) -> Dict[str, Optional[float]]:
    """Шаг 8. Объёмы работ из QTO: NetVolume — бетон, площадь — полы/покрытия."""
    volume = None
    area = None

    for key, value in row.items():
        kl = str(key).lower()
        cleaned = _clean(value)
        if not cleaned:
            continue
        if volume is None and ("объ" in kl) and ("net" in kl.replace("_", "")) and "м3" in kl:
            volume = _to_float(cleaned)
        elif volume is None and kl in ("объем (net), м3", "объём (net), м3"):
            volume = _to_float(cleaned)
        elif area is None and ("площадь" in kl) and ("gross" in kl) and "м2" in kl:
            area = _to_float(cleaned)
        elif area is None and kl == "площадь (gross), м2":
            area = _to_float(cleaned)

    return {"volume_m3": volume, "area_m2": area}


def _element_from_row(row: dict, raw_by_gid: Dict[str, dict]) -> dict:
    """Шаг 1. Собирает нормализованный элемент из строки Excel + сырого дампа IFC."""
    gid = _clean(row.get("GlobalId"))
    raw = raw_by_gid.get(gid, {}) if gid else {}

    # Склеиваем строку Excel и сырую запись дампа (дамп приоритетнее для
    # отсутствующих в сокращённой таблице свойств: PredefinedType и т.п.)
    merged = dict(row)
    for k, v in raw.items():
        merged.setdefault(k, v)

    ifc_class = _clean(merged.get("Тип элемента")) or _clean(raw.get("IfcClass"))
    ifc_class = _normalize_ifc_class(ifc_class)
    predefined_type = _clean(raw.get("PredefinedType"))
    if not predefined_type:
        pk = _find_key(merged, "predefinedtype")
        if pk:
            predefined_type = _clean(merged.get(pk))

    mssk_code = _clean(merged.get("Код мсск"))
    if not mssk_code:
        ck = _find_key(merged, "elementcode")
        if ck:
            candidate = _clean(merged.get(ck))
            if candidate.startswith("ЭЛ"):
                mssk_code = candidate

    material = _clean(merged.get("Материал"))
    if not material:
        mk = _find_key(merged, "ifcmateriallayer", "name")
        if mk:
            material = _clean(merged.get(mk))

    # ConstructionMethod (In-situ / Precast) — прямой параметр или Pset
    construction_method = _clean(raw.get("ConstructionMethod"))
    if not construction_method:
        ck = _find_key(merged, "constructionmethod")
        if ck:
            construction_method = _clean(merged.get(ck))

    # Этаж / часть здания
    storey = _clean(merged.get("Этаж"))
    storey_type = _clean(merged.get("Тип_этажа"))
    storey_all = f"{storey} {storey_type}".lower()

    # wall_location: Pset_WallCommon::IsExternal (параметр элемента IFC-модели)
    is_external_raw = _clean(raw.get("Pset_WallCommon::IsExternal"))
    if not is_external_raw:
        ek = _find_key(merged, "isexternal")
        if ek:
            is_external_raw = _clean(merged.get(ek))

    quantities = _extract_quantities(merged)

    return {
        "global_id": gid,
        "name": _clean(merged.get("Имя")),
        "ifc_class": ifc_class,
        "predefined_type": predefined_type,
        "mssk_code": mssk_code,
        "material": material,
        "construction_method": construction_method,
        "storey": storey,
        "storey_type": storey_type,
        "is_external_raw": is_external_raw,
        "quantities": quantities,
        "row": {k: _clean(v) for k, v in merged.items() if _clean(v)},
    }


# --------------------------------------------------------------------------
#  Шаг 2. Сборники-кандидаты по IfcClass + PredefinedType
# --------------------------------------------------------------------------

def _candidate_collections(el: dict) -> List[str]:
    ifc_class = (el.get("ifc_class") or "").strip().lower()
    predefined = (el.get("predefined_type") or "").strip().upper()

    exact: List[str] = []
    by_class: List[str] = []

    for mapping in _ifc_collections().get("mappings", []):
        map_class = str(mapping.get("ifc_class", "")).strip().lower()
        map_predef = str(mapping.get("predefined_type") or "").strip().upper()
        if ifc_class != map_class:
            continue
        if map_predef and predefined == map_predef:
            exact.extend(mapping.get("collections", []))
        elif not map_predef:
            by_class.extend(mapping.get("collections", []))

    # Сначала совпадения по PredefinedType, затем по классу (без дублей)
    result: List[str] = []
    for name in exact + by_class:
        if name not in result:
            result.append(name)
    return result


# --------------------------------------------------------------------------
#  Шаг 3. Уточнение по коду МССК
# --------------------------------------------------------------------------

def _mssk_context(el: dict) -> dict:
    code = el.get("mssk_code") or ""
    index = _mssk_index()
    context = {}
    # Точное совпадение, затем по префиксу (ЭЛ 30 10 15 01 -> ЭЛ 30 10 15)
    parts = code.split()
    for cut in range(len(parts), 0, -1):
        prefix = " ".join(parts[:cut])
        if prefix in index:
            context = {"code": prefix, "name": index[prefix]}
            break
    return context


# --------------------------------------------------------------------------
#  Шаг 4. Главный переключатель технологии
# --------------------------------------------------------------------------

def _detect_technology_collection(el: dict, candidates: List[str]) -> Optional[str]:
    """Возвращает название сборника по ConstructionMethod + материалу.

    Приоритет у явного материала элемента (сталь, дерево, кирпич/блоки):
    технология Precast/In-situ применима только к бетонным и ж/б
    конструкциям, но может ошибочно стоять в IFC у элементов из других
    материалов (например, стальная труба-колонна ГОСТ 30245-2003 с
    ConstructionMethod=Precast должна попадать в Сборник 9, а не 7).
    """
    material = (el.get("material") or "").lower()
    mat = f"{el.get('material', '')} {el.get('name', '')}".lower()
    cm = (el.get("construction_method") or "").lower().replace(" ", "").replace("-", "")

    def pick(number: int) -> Optional[str]:
        for name in candidates:
            if _collection_number(name) == number:
                return name
        # Сборник может не быть в кандидатах, но технология однозначна —
        # ищем его в дереве работ по номеру.
        for coll in _tree():
            if _collection_number(coll.get("collection", "")) == number:
                return coll.get("collection")
        return None

    # Сталь/металл — только по полю «Материал» (не по имени: в имени ж/б
    # элемента может встречаться, например, «стальная закладная деталь»)
    if "металл" in material or "стал" in material:
        return pick(9)
    if "дерев" in mat or "пластмасс" in mat or "пвх" in mat or "гкл" in mat:
        return pick(10)
    if "кирпич" in mat or "кладк" in mat or "блок" in mat:
        return pick(8)
    if "precast" in cm:
        return pick(7)
    if "insitu" in cm:
        return pick(6)
    if "сборн" in mat:
        return pick(7)
    if "монолит" in mat:
        return pick(6)
    return None


# --------------------------------------------------------------------------
#  Шаг 5. Выбор таблиц по ключевым словам
# --------------------------------------------------------------------------

def _search_tables(collection_name: str, keywords: List[str]) -> List[dict]:
    """Ищет таблицы сборника по ключевым словам в названиях.

    Возвращает список записей индекса таблиц (code/name/...), отсортированный
    по порядку в дереве работ.
    """
    index = _table_index()
    target_key = _norm_key(collection_name)
    matched: List[dict] = []

    for code, info in index.items():
        if _norm_key(info["collection"]) != target_key:
            continue
        name_lower = info["name"].lower()
        for kw in keywords:
            kw = str(kw).lower().strip()
            if kw and kw in name_lower:
                matched.append(info)
                break

    return matched


_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "IfcWall": ["стен", "перегородок"],
    "IfcSlab": ["перекрыт", "плит"],
    "IfcColumn": ["колонн"],
    "IfcBeam": ["балок", "ригел", "перемычек"],
    "IfcStair": ["лестниц"],
    "IfcDoor": ["двер"],
    "IfcWindow": ["окон", "переплет"],
    "IfcRoof": ["кровл"],
    "IfcFooting": ["фундамент"],
    "IfcPile": ["сваи", "свай", "погружен"],
    "IfcRailing": ["ограждени", "перил"],
    "IfcCurtainWall": ["фасад", "навесн"],
}


def _material_keywords(el: dict) -> List[str]:
    """Ключевые слова из материала/имени элемента (слова длиной 4+)."""
    text = f"{el.get('material', '')} {el.get('name', '')}".lower()
    text = re.sub(r"[^\wа-яё\s-]", " ", text)
    words = [w for w in text.split() if len(w) >= 4]
    # Убираем служебные слова
    stop = {"этаж", "этаж", "типовой", "подземный", "надземный", "цокольный", "существующий"}
    return [w for w in words if w not in stop][:8]


def _keyword_tables(el: dict, collection_name: str) -> List[dict]:
    """Шаг 5 для сборников без явных правил: по ключевым словам.

    Сначала ищем по типу элемента (колонны, балки, стены, ...). Если по
    типу ничего не найдено — фолбэк по ключевым словам материала/имени.
    Смешивание наборов не используется: слово материала (например,
    «сталь») матчит нерелевантные таблицы («защита листовой сталью
    бункеров», «резервуары стальные») рядом с точным совпадением по типу.
    Таблицы «усиления» существующих конструкций не подбираются: элементы
    из IFC-модели — новое строительство, а не усиление.
    """
    type_kws = _TYPE_KEYWORDS.get(el.get("ifc_class", ""), [])
    matched = _search_tables(collection_name, type_kws) if type_kws else []
    matched = [t for t in matched if "усилен" not in t["name"].lower()]
    if not matched:
        matched = _search_tables(collection_name, _material_keywords(el))
    return matched


# --------------------------------------------------------------------------
#  Шаги 6-7. Правила для Сборников 6/7/8/11 + константы
# --------------------------------------------------------------------------

def _height_to_range(height_m: Optional[float]) -> Optional[str]:
    """Числовая высота -> диапазон из works_classification."""
    if height_m is None or height_m <= 0:
        return None
    if height_m <= 30:
        return "до 30"
    if height_m <= 40:
        return "30-40"
    if height_m <= 57:
        return "40-57"
    if height_m <= 75:
        return "57-75"
    if height_m <= 105:
        return "75-105"
    if height_m <= 150:
        return "105-150"
    if height_m <= 250:
        return "150-250"
    return "свыше 200 (шпиль)"


_RANGE_TO_HEIGHT_M: Dict[str, float] = {
    "до 30": 30.0,
    "30-40": 40.0,
    "40-57": 57.0,
    "57-75": 75.0,
    "75-105": 105.0,
    "105-150": 150.0,
    "150-250": 250.0,
    "свыше 200 (шпиль)": 250.0,
}


def _resolve_constants(el: dict, constants: Dict[str, Any]) -> Dict[str, Any]:
    """Шаг 7. Применение констант: элемент -> константа -> значение по умолчанию."""
    resolved: Dict[str, Any] = {}

    # building_part: только из элемента (этаж/тип этажа). Константой проекта
    # этот параметр больше не задаётся — в перспективе берётся из параметров
    # элемента IFC-модели. Fallback — «надземная».
    storey = str(el.get("storey", "") or "")
    storey_type = str(el.get("storey_type", "") or "")
    # Строгое правило — числовой индикатор значения «Этаж» (как в группировке
    # элементов): '-N/M' и '-N' → подземная/цокольная, 'N' → надземная.
    # Проверяется первым: «-1/1_подземный этаж» — цоколь, «1_этаж_основной» —
    # надземная (даже если «Тип_этажа» ошибочно размечен как цокольный).
    part = None
    for segment in re.split(r"[_\s]+", storey.strip().lower()):
        seg = segment.strip()
        if re.match(r"^-\d+\s*/\s*\d+$", seg) or re.match(r"^-\d+$", seg):
            part = "подземная/цокольная"
            break
        if re.match(r"^\d+$", seg):
            part = "надземная"
            break
    if part is None:
        storey_all = f"{storey} {storey_type}".lower()
        if any(w in storey_all for w in ("подзем", "подвал", "цоколь")):
            part = "подземная/цокольная"
        else:
            part = "надземная"
    resolved["building_part"] = part

    # wall_location: только из элемента (Pset_WallCommon::IsExternal). Константой
    # проекта этот параметр больше не задаётся — в перспективе берётся из
    # параметров элемента IFC-модели. Fallback — «наружная».
    is_ext = (el.get("is_external_raw") or "").strip().upper()
    if is_ext in ("TRUE", "1", "T", "ДА"):
        wall_loc = "наружная"
    elif is_ext in ("FALSE", "0", "F", "НЕТ"):
        wall_loc = "внутренняя"
    else:
        wall_loc = "наружная"
    resolved["wall_location"] = wall_loc

    # building_height_m: константа-диапазон или число -> диапазон
    height_const = constants.get("building_height_m")
    height_num = _to_float(height_const)
    if height_num is not None:
        resolved["building_height_m"] = _height_to_range(height_num)
        resolved["building_height_value_m"] = height_num
    elif _clean(height_const):
        range_str = str(height_const).strip()
        resolved["building_height_m"] = range_str
        # Диапазон -> верхняя граница (для выбора раздела отдела 1.2)
        resolved["building_height_value_m"] = _RANGE_TO_HEIGHT_M.get(range_str)
    else:
        resolved["building_height_m"] = None

    resolved["formwork_type"] = (
        _clean(constants.get("formwork_type"))
        or "деревянная щитовая"
    )
    resolved["concrete_curing_season"] = _clean(constants.get("concrete_curing_season"))
    resolved["concrete_placement_scheme"] = _clean(constants.get("concrete_placement_scheme"))
    resolved["crane_type"] = _clean(constants.get("crane_type"))
    # Этажность здания ('одноэтажное'/'многоэтажное') — константа схемы
    # works_classification.json; определяется по модели IFC (zero_step,
    # колонка «Этаж») и подставляется в веб-интерфейсе.
    resolved["building_storeys_type"] = _clean(
        constants.get("building_storeys_type")
    )
    return resolved


def _is_reinforced(el: dict) -> bool:
    """Признак железобетона (для выбора 3.6-10 vs 3.6-11)."""
    mat = f"{el.get('material', '')}".lower()
    if "железобетон" in mat or "ж/б" in mat:
        return True
    row = el.get("row", {})
    for key, value in row.items():
        if "reinforcementvolumeratio" in str(key).lower():
            val = _to_float(value)
            if val is not None and val > 0:
                return True
    return False


def _is_base_slab(el: dict) -> bool:
    """Признак фундаментной плиты.

    IfcSlab с PredefinedType=BASESLAB либо IfcSlab с «фундаментн…» и
    «плит…» в имени (например, «Фундаментная плита_800мм_ ЖБ_B30_W6_F150»).
    Такие плиты — фундаменты, а не перекрытия: им подбираются таблицы
    разделов 1.1.1/1.1.2 (Сб. 6), а не 1.1.7 «Перекрытия».
    """
    if (el.get("ifc_class") or "") != "IfcSlab":
        return False
    if (el.get("predefined_type") or "").strip().upper() == "BASESLAB":
        return True
    name = (el.get("name") or "").lower()
    return "фундаментн" in name and "плит" in name


def _curing_table(constants: Dict[str, Any]) -> str:
    """Таблица ухода за бетоном по константе сезона."""
    season = (constants.get("concrete_curing_season") or "").lower()
    if "холодн" in season or "тепловлаг" in season:
        return "3.6-61"
    return "3.6-98"


def _monolith_package_codes(el: dict, constants: Dict[str, Any]) -> List[str]:
    """Пакет таблиц отдела 1.2 Сб. 6 (SEPARATE): опалубка+демонтаж+арматура+бетон."""
    part = constants["building_part"]
    height = constants.get("building_height_value_m")
    height_range = constants.get("building_height_m") or ""

    # Фундаменты (IfcFooting) и фундаментные плиты (IfcSlab BASESLAB /
    # «фундаментная плита» в имени) — всегда раздел 1.2.2 «Фундаменты под
    # здания и сооружения» (3.6-70…3.6-73), а не разделы стен/перекрытий
    # надземной части. Крупнощитовая опалубка отдела 1.3 (3.6-107/108)
    # применяется только к стенам, колоннам и днищам — к фундаментам она
    # не подбирается независимо от константы formwork_type: на проекте
    # может использоваться несколько типов опалубки одновременно
    # (крупнощитовая — для стен/колонн, мелкощитовая — для фундаментов).
    if (el.get("ifc_class") or "") == "IfcFooting" or _is_base_slab(el):
        return ["3.6-70", "3.6-71", "3.6-72", "3.6-73"]

    def in_range(range_str: str) -> bool:
        return range_str and range_str in height_range

    # Отдел 1.4 (переставная опалубка, высотные здания 105-250 м)
    if (constants.get("formwork_type") or "").lower().startswith("переставная") or in_range("105-150") or in_range("150-250"):
        if in_range("150-250") or (height is not None and 150 < height <= 250):
            return ["3.6-134", "3.6-135", "3.6-127", "3.6-128"]
        return ["3.6-116", "3.6-117", "3.6-111", "3.6-110"]

    # Отдел 1.3 (крупнощитовая): опалубка 3.6-107/3.6-108 + бетонирование из 1.2
    if (constants.get("formwork_type") or "").lower().startswith("крупнощитовая"):
        base = _monolith_base_section_codes(part, height)
        return ["3.6-107", "3.6-108"] + base[2:]

    # Отдел 1.2 (индустриальная мелкощитовая) — по умолчанию для SEPARATE
    return _monolith_base_section_codes(part, height)


def _monolith_base_section_codes(part: str, height: Optional[float]) -> List[str]:
    """Разделы 1.2.2-1.2.8: фундаменты / подземная / надземная по высоте."""
    if part == "подземная/цокольная":
        return ["3.6-74", "3.6-75", "3.6-76", "3.6-77"]
    if height is None or height <= 30:
        return ["3.6-78", "3.6-79", "3.6-80", "3.6-81"]
    if height <= 40:
        return ["3.6-82", "3.6-83", "3.6-84", "3.6-85"]
    if height <= 57:
        return ["3.6-86", "3.6-87", "3.6-88", "3.6-89"]
    if height <= 75:
        return ["3.6-90", "3.6-91", "3.6-92", "3.6-93"]
    if height <= 105:
        return ["3.6-94", "3.6-95", "3.6-96", "3.6-97"]
    # Свыше 105 м без явного отдела 1.4 — используем верхний раздел 1.2.8
    return ["3.6-94", "3.6-95", "3.6-96", "3.6-97"]


def _select_collection_6(el: dict, constants: Dict[str, Any]) -> List[dict]:
    """Сб. 6 (монолит): COMPLEX или SEPARATE. Для монолитных элементов
    в COMPLEX-режиме ДОПОЛНИТЕЛЬНО добавляем SEPARATE-пакет таблиц —
    это нужно для развёрнутого расчёта (опалубка + армирование +
    бетонирование + уход отдельными работами)."""
    formwork = (constants.get("formwork_type") or "").lower()
    complex_mode = formwork.startswith("деревянная") or not formwork

    index = _table_index()

    if complex_mode:
        # ... существующая логика выбора комплексной таблицы (без изменений) ...
        ifc_class = el.get("ifc_class", "")
        part = constants["building_part"]
        main, additional = None, []
        if ifc_class in ("IfcFooting", "IfcPile"):
            main, additional = "3.6-1", ["3.6-4", "3.6-6"]
        elif _is_base_slab(el):
            # Фундаментная плита — таблицы фундаментов (разделы 1.1.1/1.1.2),
            # а не перекрытий (1.1.7): 3.6-1 (бетонная подготовка и
            # фундаменты общего назначения) + 3.6-6 (закладные детали,
            # армирование подстилающих слоёв)
            main, additional = "3.6-1", ["3.6-6"]
        elif ifc_class == "IfcWall":
            if part == "подземная/цокольная":
                main = "3.6-8"
            elif _is_reinforced(el):
                main = "3.6-11"
            else:
                main = "3.6-10"
        elif ifc_class == "IfcColumn":
            main = "3.6-9"
        elif ifc_class == "IfcBeam":
            name = (el.get("name") or "").lower()
            main = "3.6-13" if "пояс" in name else "3.6-12"
        elif ifc_class in ("IfcSlab", "IfcRoof", "IfcStair"):
            main, additional = "3.6-15", ["3.6-5"]
        else:
            # Неизвестный класс — поиск по ключевым словам в отделе 1.1
            matched = _keyword_tables(el, "Сборник  6. Бетонные, железобетонные конструкции монолитные")
            works = [dict(t, role="main", work_type="COMPLEX") for t in matched[:1]]
            return works

        works = []
        if main and main in index:
            works.append(dict(index[main], role="main", work_type="COMPLEX"))
        for code in additional:
            if code in index:
                works.append(dict(index[code], role="additional", work_type="COMPLEX"))

        # [NEW] Для ж/б монолитных — добавляем SEPARATE-пакет для развёрнутого расчёта
        if _is_reinforced(el):
            package_codes = _monolith_package_codes(el, constants)
            for code in package_codes:
                if code in index:
                    works.append(dict(index[code], role="package", work_type="SEPARATE"))
            curing = _curing_table(constants)
            if curing in index:
                works.append(dict(index[curing], role="additional", work_type="SEPARATE"))

        return works

    # Отделы 1.2-1.4 — SEPARATE (без изменений)
    codes = _monolith_package_codes(el, constants)
    works = []
    for code in codes:
        if code in index:
            works.append(dict(index[code], role="package", work_type="SEPARATE"))
    curing = _curing_table(constants)
    if curing in index:
        works.append(dict(index[curing], role="additional", work_type="SEPARATE"))
    return works


def _select_collection_7(el: dict, constants: Dict[str, Any]) -> List[dict]:
    """Сб. 7 (сборные конструкции) — всегда SEPARATE (монтаж + стыки)."""
    ifc_class = el.get("ifc_class", "")
    part = constants["building_part"]
    wall_loc = constants["wall_location"]
    height = constants.get("building_height_value_m")

    # Этажность здания: константа building_storeys_type ('одноэтажное'/
    # 'многоэтажное'), определённая по колонке «Этаж» IFC (наибольший
    # числовой индикатор > 2 → многоэтажное). Если константа не задана —
    # прежняя эвристика по высоте здания.
    storeys_type = (constants.get("building_storeys_type") or "").strip().lower()
    if storeys_type in ("одноэтажное", "многоэтажное"):
        multistory = storeys_type == "многоэтажное"
    else:
        multistory = height is None or height > 30

    index = _table_index()
    main, additional = None, []

    if ifc_class in ("IfcFooting", "IfcPile") or _is_base_slab(el):
        main, additional = "3.7-1", ["3.7-2"]
    elif ifc_class == "IfcWall":
        if part == "подземная/цокольная":
            main = "3.7-21"
        elif wall_loc == "наружная":
            if multistory:
                main, additional = "3.7-17", ["3.7-19", "3.7-58", "3.7-88"]
            else:
                main, additional = "3.7-16", ["3.7-19"]
        else:
            main = "3.7-28"
    elif ifc_class == "IfcColumn":
        main = "3.7-22" if multistory else "3.7-5"
    elif ifc_class == "IfcBeam":
        main = "3.7-10" if multistory else "3.7-9"
    elif ifc_class == "IfcSlab":
        if multistory:
            main, additional = "3.7-15", ["3.7-4"]
        else:
            main = "3.7-13"
    elif ifc_class == "IfcStair":
        main, additional = "3.7-20", ["3.7-25"]
    else:
        matched = _keyword_tables(el, "Сборник  7. Бетонные и железобетонные конструкции сборные")
        return [dict(t, role="main", work_type="SEPARATE") for t in matched[:1]]

    works = []
    if main and main in index:
        works.append(dict(index[main], role="main", work_type="SEPARATE"))
    for code in additional:
        if code in index:
            works.append(dict(index[code], role="additional", work_type="SEPARATE"))
    return works


def _select_collection_8(el: dict, constants: Dict[str, Any]) -> List[dict]:
    """Сб. 8 (кирпич/блоки) — COMPLEX, таблицы кладки по ключевым словам."""
    keywords = ["кирпич", "кладк", "блок"]
    keywords.extend(_TYPE_KEYWORDS.get(el.get("ifc_class", ""), []))
    matched = _search_tables("Сборник  8. Конструкции из кирпича и блоков", keywords)
    if not matched:
        matched = _keyword_tables(el, "Сборник  8. Конструкции из кирпича и блоков")
    return [dict(t, role="main", work_type="COMPLEX") for t in matched[:1]]


def _select_collection_11(el: dict, constants: Dict[str, Any]) -> List[dict]:
    """Сб. 11 (полы) — SEPARATE, пирог пола = несколько таблиц.

    Слой пола определяется по коду МССК (30 32 30 стяжка, 30 32 10 покрытие,
    30 32 15 гидроизоляция, 30 32 20 теплоизоляция), покрытие — по материалу.
    """
    code = el.get("mssk_code") or ""
    mat = f"{el.get('material', '')} {el.get('name', '')}".lower()

    if code.startswith("ЭЛ 30 32 30") or "стяжк" in mat:
        keywords = ["стяжк"]
    elif code.startswith("ЭЛ 30 32 15") or "гидроизоляц" in mat:
        keywords = ["гидроизоляц", "оклеечн", "обмазочн"]
    elif code.startswith("ЭЛ 30 32 20") or "теплоизоляц" in mat or "утеплит" in mat:
        keywords = ["теплоизоляц", "утеплит"]
    else:
        # Покрытие: по материалу
        keywords = []
        for token in ("керамогранит", "керамическ", "линолеум", "паркет", "ламинат",
                      "плитк", "мозаик", "бетон", "покрыти"):
            if token in mat:
                keywords.append(token)
        if not keywords:
            keywords = ["покрыти"]

    matched = _search_tables("Сборник 11. Полы", keywords)
    return [dict(t, role="main", work_type="SEPARATE") for t in matched[:2]]


def _work_type_for_collection(collection_name: str, works: List[dict]) -> str:
    """Шаг 6. work_type из works_classification (по умолчанию SEPARATE)."""
    target = _norm_key(collection_name)
    for coll in _classification().get("collections", []):
        if _norm_key(coll.get("collection", "")) == target:
            wt = str(coll.get("work_type", "")).strip().lower()
            if wt.startswith("complex"):
                return "COMPLEX"
            if wt.startswith("зависит"):
                # Сб. 6: COMPLEX только для отдела 1.1
                for w in works:
                    if "1.1." in str(w.get("department", "")):
                        return "COMPLEX"
                return "SEPARATE"
            return "SEPARATE"
    return "SEPARATE"


# --------------------------------------------------------------------------
#  Главная функция подбора для одного элемента
# --------------------------------------------------------------------------

def select_works_for_element(el: dict, constants: Dict[str, Any]) -> dict:
    """Подбор таблиц работ для одного элемента (шаги 1-8 алгоритма)."""
    resolved = _resolve_constants(el, constants)

    # Шаг 2: кандидаты
    candidates = _candidate_collections(el)

    # Шаг 4: переключатель технологии
    primary = _detect_technology_collection(el, candidates)
    if not primary and candidates:
        primary = candidates[0]

    works: List[dict] = []
    note = ""

    if primary:
        num = _collection_number(primary)
        if num == 6:
            works = _select_collection_6(el, resolved)
        elif num == 7:
            works = _select_collection_7(el, resolved)
        elif num == 8:
            works = _select_collection_8(el, resolved)
        elif num == 11:
            works = _select_collection_11(el, resolved)
        else:
            works = _keyword_tables(el, primary)
            works = [dict(t, role="main", work_type="SEPARATE") for t in works[:3]]

        if not works:
            note = "Не найдено подходящих таблиц в сборнике по ключевым словам"
    else:
        # Фолбэк: глобальный поиск по ключевым словам по всем сборникам
        keywords = list(_TYPE_KEYWORDS.get(el.get("ifc_class", ""), []))
        keywords.extend(_material_keywords(el))
        for coll in _tree():
            coll_name = coll.get("collection", "")
            matched = _search_tables(coll_name, keywords)
            if matched:
                primary = coll_name
                works = [
                    dict(t, role="main", work_type="SEPARATE")
                    for t in matched[:2]
                ]
                note = "Сборник определён поиском по ключевым словам"
                break
        if not works:
            note = "Не определены сборники-кандидаты для типа элемента"

    work_type = _work_type_for_collection(primary, works) if primary else "SEPARATE"

    # Шаг 3: контекст МССК
    mssk_context = _mssk_context(el)

    return {
        "element": {
            "global_id": el.get("global_id", ""),
            "name": el.get("name", ""),
            "ifc_class": el.get("ifc_class", ""),
            "predefined_type": el.get("predefined_type", ""),
            "mssk_code": el.get("mssk_code", ""),
            "material": el.get("material", ""),
            "construction_method": el.get("construction_method", ""),
            "storey": el.get("storey", ""),
            "storey_type": el.get("storey_type", ""),
        },
        "mssk_context": mssk_context,
        "candidate_collections": candidates,
        "selected_collection": primary or "",
        "work_type": work_type,
        "works": [
            {
                "code": w["code"],
                "name": w["name"],
                "collection": w["collection"],
                "department": w["department"],
                "section": w["section"],
                "role": w.get("role", "main"),
                "work_type": w.get("work_type", work_type),
                "quantity": el.get("quantities", {}),
            }
            for w in works
        ],
        "applied_constants": {
            k: v for k, v in resolved.items() if v not in (None, "")
        },
        "note": note,
    }


def select_works_for_elements(
    elements: List[dict], global_constants: Dict[str, Any]
) -> List[dict]:
    """Подбор таблиц работ для списка элементов."""
    # Этажность здания (building_storeys_type) — константа проекта: приходит
    # из UI или подставляется вызывающим кодом из файлов сессии (см.
    # zero_step.detect_storeys_type_from_session). ВАЖНО: не определяем её по
    # элементам этого списка — в него попадает только подмножество выбранных
    # строк, этажность же свойство всего здания.
    constants = dict(global_constants or {})
    results = []
    for el in elements:
        try:
            results.append(select_works_for_element(el, constants))
        except Exception as exc:
            logger.warning(
                f"Ошибка подбора работ для элемента {el.get('global_id')}: {exc}",
                exc_info=True,
            )
            results.append({
                "element": {"global_id": el.get("global_id", ""), "name": el.get("name", "")},
                "works": [],
                "note": f"Ошибка подбора: {exc}",
            })
    return results


# --------------------------------------------------------------------------
#  Сборка итогового JSON по Excel с элементами
# --------------------------------------------------------------------------

def build_works_tables_json(
    excel_path: str,
    output_path: str,
    global_constants: Optional[Dict[str, Any]] = None,
    raw_dump_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Формирует JSON с подобранными таблицами работ для элементов из Excel.

    Args:
        excel_path: XLSX с элементами (лист 'Данные'), например
            filtered_elements.xlsx из папки запуска.
        output_path: путь сохранения итогового JSON.
        global_constants: константы проекта (global_constants из
            data/works_classification.json, задаются в веб-интерфейсе).
        raw_dump_json_path: путь к IFC_исходные_параметры.json (сырой дамп
            свойств IFC) — используется для обогащения элементов свойствами
            (PredefinedType, ConstructionMethod, IsExternal и др.).

    Returns:
        Словарь с итоговой структурой (он же сохраняется в output_path).
    """
    constants = dict(global_constants or {})

    df = pd.read_excel(excel_path, sheet_name="Данные")
    rows = df.where(pd.notna(df), "").to_dict("records")

    # Сырой дамп IFC: GlobalId -> запись свойств
    raw_by_gid: Dict[str, dict] = {}
    if raw_dump_json_path and os.path.isfile(raw_dump_json_path):
        try:
            with open(raw_dump_json_path, "r", encoding="utf-8") as fh:
                raw_rows = json.load(fh)
            if isinstance(raw_rows, list):
                for rec in raw_rows:
                    gid = _clean(rec.get("GlobalId"))
                    if gid:
                        raw_by_gid[gid] = rec
            logger.info(f"Сырой дамп IFC загружен: {len(raw_by_gid)} элементов")
        except Exception as exc:
            logger.warning(f"Не удалось прочитать сырой дамп IFC: {exc}")

    elements = [_element_from_row(row, raw_by_gid) for row in rows]
    results = select_works_for_elements(elements, constants)

    total_works = sum(len(r.get("works", [])) for r in results)
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "algorithm": "data/algorithm.md",
        "processing_type": "AR",
        "global_constants": constants,
        "total_elements": len(results),
        "total_works": total_works,
        "elements": results,
    }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    logger.info(
        f"Подбор таблиц работ: {len(results)} элементов, "
        f"{total_works} работ -> {output_path}"
    )
    return payload
