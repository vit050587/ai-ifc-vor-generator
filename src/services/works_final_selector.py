"""
Финальный подбор работ через LLM (режим АР). Версия 12.0.

Изменения от v11.9:
  * [FIX-43] Части здания определяются по path[0] листовой группы
    (Подземная → Цокольная → Надземная), а не по applied_constants.
    Раньше подземная и цокольная склеивались в одну «Цоколь».
  * [FIX-44] В entry прокидывается group_path, из него берётся часть.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.core.config import load_config
from src.core.logger import setup_logger
from src.services.works_cost import (
    _parse_money,
    format_money,
    safe_float,
)

logger = setup_logger(__name__)

try:
    import pymorphy3
    _MORPH = pymorphy3.MorphAnalyzer()
    _MORPH_AVAILABLE = True
except ImportError:
    _MORPH = None
    _MORPH_AVAILABLE = False

# ======================================================================
#  Файлы
# ======================================================================

FINAL_WORKS_JSON_FILENAME = "Финальный_перечень_работ.json"
FINAL_WORKS_XLSX_FILENAME = "Финальный_перечень_работ.xlsx"
GROUPED_JSON_FILENAME = "filtered_elements_grouped_AR.json"
FILTERED_XLSX_FILENAME = "filtered_elements.xlsx"
ELEMENT_PARAMS_FILENAME = "Параметры_подбора_элементов.json"
IFC_CONSTANTS_FILENAME = "IFC_глобальные_константы.json"

_TON_UNIT_RE = re.compile(r"(?:^|\s)(?:\d+(?:[.,]\d+)?\s*)?т(?:\s|$)")
_PHYSICAL_UNIT_RE = re.compile(r"\b(?:м|м2|м3|мм|мм2|мм3|т|кг|л|г)\b")
_UNIT_MULTIPLIER_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*)$")
_JOINT_UNIT_STEMS = ("шов", "шв", "гермет", "заделк")

_TABLE_CODE_RE = re.compile(r"(\d+\.\d+-\d+)")
_THICKNESS_WORK_RE = re.compile(
    r"(?:толщин(?:ой|ы)|толщ\.)\s+(?:до|свыше|более|от)?\s*(\d+)\s*мм",
    re.IGNORECASE,
)
_MM_IN_NAME_RE = re.compile(r"(\d+)\s*мм", re.IGNORECASE)
_SECTION_RE = re.compile(r"(\d+)\s*[хx]\s*(\d+)", re.IGNORECASE)

_NO_USTROYSTVO_FAMILIES = frozenset({"wall", "foundation_slab", "floor"})
_USTROYSTVO_PREFIX_RE = re.compile(r"^\s*устройств", re.IGNORECASE)

_REBAR_KEYWORDS = ("арматур", "каркас", "стержн", "сетк", "закладн")


def _extract_table_code(raw: Any) -> str:
    if raw is None:
        return ""
    m = _TABLE_CODE_RE.search(str(raw))
    return m.group(1) if m else ""


def _is_ustroystvo_work(work_title: Any) -> bool:
    return bool(_USTROYSTVO_PREFIX_RE.match(str(work_title or "")))


# ======================================================================
#  Доступ к element["row"]
# ======================================================================

def _lookup(el: Dict[str, Any], *keys: str) -> Optional[float]:
    if not isinstance(el, dict):
        return None
    row = el.get("row") if isinstance(el.get("row"), dict) else {}
    for key in keys:
        for source in (el, row):
            if not isinstance(source, dict):
                continue
            v = safe_float(source.get(key), default=None)
            if v is not None and v > 0:
                return float(v)
    return None


_LENGTH_KEYS = (
    "QTO_Qto_SlabBaseQuantities_Длина_Length_мм",
    "QTO_Qto_FootingBaseQuantities_Длина_Length_мм",
    "QTO_Qto_WallBaseQuantities_Длина_Length_мм",
    "QTO_Qto_BeamBaseQuantities_Длина_Length_мм",
    "QTO_Qto_ColumnBaseQuantities_Длина_Length_мм",
    "QTO_Qto_StairFlightBaseQuantities_Длина_Length_мм",
    "Длина, мм", "Длина_Length_мм",
)
_WIDTH_KEYS = (
    "QTO_Qto_SlabBaseQuantities_Длина_Width_мм",
    "QTO_Qto_FootingBaseQuantities_Длина_Width_мм",
    "QTO_Qto_WallBaseQuantities_Длина_Width_мм",
    "QTO_Qto_ColumnBaseQuantities_Длина_Width_мм",
    "QTO_Qto_BeamBaseQuantities_Длина_Width_мм",
    "QTO_Qto_StairFlightBaseQuantities_Длина_Width_мм",
    "Ширина, мм", "Длина_Width_мм",
)
_HEIGHT_KEYS = (
    "QTO_Qto_WallBaseQuantities_Длина_Height_мм",
    "QTO_Qto_FootingBaseQuantities_Длина_Height_мм",
    "QTO_Qto_ColumnBaseQuantities_Длина_Height_мм",
    "QTO_Qto_BeamBaseQuantities_Длина_Height_мм",
    "QTO_Qto_WindowBaseQuantities_Длина_Height_мм",
    "QTO_Qto_DoorBaseQuantities_Длина_Height_мм",
    "Высота, мм", "Длина_Height_мм",
)
_DEPTH_KEYS = (
    "QTO_Qto_SlabBaseQuantities_Длина_Depth_мм",
    "QTO_Qto_PlateBaseQuantities_Длина_Depth_мм",
    "QTO_Qto_CoveringBaseQuantities_Длина_Depth_мм",
    "Длина_Depth_мм",
    "Толщина, мм",
    "Свойство::IfcMaterialLayer::Thickness",
)
_VOLUME_KEYS = (
    "QTO_Qto_SlabBaseQuantities_Объём_NetVolume_м3",
    "QTO_Qto_WallBaseQuantities_Объём_NetVolume_м3",
    "QTO_Qto_BeamBaseQuantities_Объём_NetVolume_м3",
    "QTO_Qto_ColumnBaseQuantities_Объём_NetVolume_м3",
    "QTO_Qto_FootingBaseQuantities_Объём_NetVolume_м3",
    "QTO_Qto_CoveringBaseQuantities_Объём_NetVolume_м3",
    "QTO_Qto_PlateBaseQuantities_Объём_NetVolume_м3",
    "Объём, м3", "Объем, м3",
)
_RATIO_KEYS = (
    "Pset_ConcreteElementGeneral_ReinforcementVolumeRatio",
    "ReinforcementVolumeRatio",
)

_SLAB_PERIMETER_KEYS = (
    "QTO_Qto_SlabBaseQuantities_Длина_Perimeter_мм",
    "QTO_Qto_FootingBaseQuantities_Длина_Perimeter_мм",
    "Длина_Perimeter_мм",
    "Периметр, мм", "Периметр, м", "Периметр",
    "Perimeter", "perimeter",
)


# ======================================================================
#  Universal anti
# ======================================================================

_UNIVERSAL_HARD_ANTI = (
    "торкрет",
    "ветрозащитн",
    "бетонораспределительн",
    "бетоновод",
    "лотк", "непроходн канал", "неподвижн щит",
    "песколовк", "метантенк", "осветлител",
    "насосн станц",
    "алмазн резк", "канатн алмазн",
    "профилированн настил",
    "подземн част",
    "мост", "эстакад", "тоннел", "путепровод", "виадук",
    "дамб", "автодорог", "железнодорожн",
    "резервуар", "отстойник", "водозабор", "камер",
    "на каждые",
    "добавлять или исключать",
    "добавляется к",
    "исключается из",
    "изменения толщины",
    "изменения объёма",
    "изменения объема",
)

# ======================================================================
#  Семейные фильтры
# ======================================================================

FAMILY_FILTERS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "preparation": {
        "required":  ("подготовк", "подстилающ", "подушк", "подбетонк"),
        "forbidden": ("опалубк", "армирован", "бетонирован",
                      "стен", "перекрыт", "колонн", "лестничн"),
    },
    "foundation_slab": {
        "required":  ("фундамент", "плит"),
        "forbidden": ("стен", "перекрыт", "лестничн", "марш",
                      "колонн", "пилон", "балк", "подготовк", "торкрет"),
    },
    "wall": {
        "required":  ("стен",),
        "forbidden": ("перекрыт", "лестничн", "марш",
                      "колонн", "пилон", "балк", "фундамент", "подготовк"),
    },
    "masonry": {
        "required":  ("кладк", "кирпич", "блок"),
        "forbidden": ("опалубк", "бетонирован", "перекрыт",
                      "лестничн", "фундамент", "подготовк"),
    },
    "floor": {
        "required":  ("перекрыт",),
        "forbidden": ("стен", "лестничн", "марш",
                      "колонн", "пилон", "балк", "фундамент", "подготовк",
                      "канал", "лотк", "непроходн"),
    },
    "column": {
        "required":  ("колонн", "пилон"),
        "forbidden": ("перекрыт", "стен", "лестничн", "марш",
                      "балк", "фундамент", "подготовк"),
    },
    "beam": {
        "required":  ("балк", "ригел", "прогон"),
        "forbidden": ("перекрыт", "стен", "лестничн", "марш",
                      "колонн", "пилон", "фундамент", "подготовк"),
    },
    "stair_march": {
        "required":  ("марш",),
        "forbidden": ("площадк", "стен", "перекрыт",
                      "колонн", "балк", "фундамент", "подготовк"),
    },
    "stair_landing": {
        "required":  ("площадк", "перекрыт"),
        "forbidden": ("марш", "стен", "фундамент",
                      "колонн", "балк", "подготовк"),
    },
    "stair": {
        "required":  ("лестничн",),
        "forbidden": ("стен", "перекрыт", "фундамент",
                      "колонн", "балк", "подготовк"),
    },
    "pile": {
        "required":  ("свая", "сваи", "ростверк"),
        "forbidden": ("стен", "перекрыт", "лестничн",
                      "колонн", "балк", "подготовк"),
    },
    "insulation": {
        "required":  ("изоляц", "гидроизол", "теплоизол",
                      "пароизол", "утеплител", "мембран"),
        "forbidden": ("перекрыт", "стен", "фундамент", "лестничн",
                      "колонн", "балк", "торкрет"),
    },
}

_FAMILY_BY_IFC = {
    "ifcwall": "wall",
    "ifcslab": "floor",
    "ifccolumn": "column",
    "ifcbeam": "beam",
    "ifcfooting": "foundation_slab",
    "ifcpile": "pile",
    "ifcstair": "stair",
    "ifcstairflight": "stair_march",
    "ifccovering": "floor",
    "ifcroof": "floor",
}

# ======================================================================
#  Морфология
# ======================================================================

def _norm_text(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _text_tokens(value: Any) -> set:
    return set(_norm_text(value).split())


_RU_ENDINGS = (
    "иями", "ями", "ами", "ыми", "ими", "иям", "ием", "ией", "иях",
    "ому", "ему", "ого", "его", "ам", "ям", "ом", "ем", "ой", "ей",
    "ах", "ях", "ов", "ев", "ых", "их",
    "ый", "ий", "ая", "яя", "ое", "ее", "ые", "ие", "ия", "ии", "ию",
    "ую", "юю", "а", "я", "о", "е", "ы", "и", "у", "ю", "й", "ь",
)


def _stem(word: str) -> str:
    w = _norm_text(word)
    if not w:
        return ""
    for end in _RU_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[: -len(end)]
    return w


def _lemma(word: str) -> str:
    w = _norm_text(word)
    if not w:
        return ""
    if _MORPH_AVAILABLE:
        try:
            return _MORPH.parse(w)[0].normal_form
        except Exception:
            return _stem(w)
    return _stem(w)


_LEMMA_PREFIX_MIN = 6


def _prefix_match(a: str, b: str) -> bool:
    n = min(len(a), len(b))
    if n < _LEMMA_PREFIX_MIN:
        return a == b
    return a[:_LEMMA_PREFIX_MIN] == b[:_LEMMA_PREFIX_MIN]


def _stem_match(text: str, keyword: str) -> bool:
    t = _norm_text(text)
    kw = _norm_text(keyword)
    if not kw:
        return False
    if " " in kw:
        return all(_stem_match(t, part) for part in kw.split())
    kw_lemma = _lemma(kw)
    if not kw_lemma:
        return False
    for tok in _text_tokens(t):
        tok_lemma = _lemma(tok)
        if tok_lemma == kw_lemma or _prefix_match(tok_lemma, kw_lemma):
            return True
    return False


def _match_any(text: str, keywords: Tuple[str, ...]) -> bool:
    return any(_stem_match(text, k) for k in keywords)


def _work_full_text(work: Dict[str, Any]) -> str:
    return " ".join(str(work.get(k) or "") for k in
                    ("title", "name", "unitOfMeasure", "unit_of_measure"))


def _unit_text(work: Dict[str, Any]) -> str:
    return str(work.get("unitOfMeasure") or work.get("unit_of_measure") or "").strip().lower()


def _is_joint_unit(unit: str) -> bool:
    u = str(unit or "").lower().replace("²", "2").replace("³", "3")
    return any(stem in u for stem in _JOINT_UNIT_STEMS)


def _is_joint_work(work: Dict[str, Any]) -> bool:
    return _is_joint_unit(_unit_text(work))


def _is_formwork_work(work: Dict[str, Any]) -> bool:
    title = str(work.get("title") or work.get("name") or "").lower()
    unit = _unit_text(work)
    return "опалубк" in title and "м2" in unit


_REBAR_INSTALL_PREFIXES = (
    "установка арматурных",
    "монтаж арматурных",
    "установка арматуры",
    "монтаж арматуры",
)


def _is_rebar_install_work(work_title: str) -> bool:
    t = _norm_text(work_title)
    return any(t.startswith(prefix) for prefix in _REBAR_INSTALL_PREFIXES)


def _is_rebar_ton_work(work: Dict[str, Any]) -> bool:
    title = str(work.get("title") or work.get("name") or "").lower()
    unit = str(work.get("unit_of_measure") or work.get("unitOfMeasure") or "")
    has_kw = any(kw in title for kw in _REBAR_KEYWORDS)
    return has_kw and bool(_TON_UNIT_RE.search(unit))


# ======================================================================
#  Классификация
# ======================================================================

def _classify_family(element_payload: Dict[str, Any]) -> Optional[str]:
    element = element_payload.get("element", {}) or {}
    ifc = str(element.get("ifc_class") or "").lower()
    predefined = str(element.get("predefined_type") or "").upper()
    material = str(element.get("material") or "").lower()
    name = str(element.get("name") or "").lower()
    mssk = str((element_payload.get("mssk_context") or {}).get("name") or "").lower()

    if any(x in material for x in ("подготовк", "подстилающ")) \
            or "подготовк" in name or "подстилающ" in name:
        return "preparation"

    if ifc == "ifcfooting":
        return "foundation_slab"
    if ifc == "ifcslab" and predefined == "baseslab":
        return "foundation_slab"
    if "фундаментн" in name and "плит" in name:
        return "foundation_slab"
    if "фундамент" in mssk:
        return "foundation_slab"

    if ifc == "ifcwall":
        if "кирпич" in material or "кладк" in material or "блок" in material:
            return "masonry"
        return "wall"

    if ifc in ("ifcslab", "ifccovering", "ifcroof"):
        return "floor"

    if ifc == "ifcstairflight":
        if "площадк" in name or "площадк" in mssk:
            return "stair_landing"
        return "stair_march"
    if ifc == "ifcstair":
        if "площадк" in name or "площадк" in mssk:
            return "stair_landing"
        if "марш" in name or "марш" in mssk:
            return "stair_march"
        return "stair"

    if "стен" in mssk:
        return "wall"
    if "перекрыт" in mssk:
        return "floor"
    if "колонн" in mssk:
        return "column"
    if "балк" in mssk or "ригел" in mssk:
        return "beam"
    if "лестничн" in mssk:
        if "площадк" in mssk:
            return "stair_landing"
        if "марш" in mssk:
            return "stair_march"
        return "stair"

    return _FAMILY_BY_IFC.get(ifc)


# ======================================================================
#  Часть / высота / геометрия
# ======================================================================

def _work_part(title: str) -> str:
    t = _norm_text(title)
    if "подземн" in t or "цокольн" in t:
        return "underground"
    if "надземн" in t:
        return "above"
    return "universal"


def _part_compatible(work_title: str, element_part: str) -> bool:
    if not element_part:
        return True
    wp = _work_part(work_title)
    if wp == "universal":
        return True
    ep = element_part.lower()
    if "подземн" in ep or "цокольн" in ep:
        return wp == "underground"
    if "надземн" in ep:
        return wp == "above"
    return True


_HEIGHT_PATTERNS = [
    (re.compile(r"высот[а-яё]*\s+здан[а-яё]*\s+(?:более|свыше)\s+(\d+(?:[.,]\d+)?)\s*(?:до|и\s+до)\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE), "exclusive_low"),
    (re.compile(r"высот[а-яё]*\s+здан[а-яё]*\s+от\s+(\d+(?:[.,]\d+)?)\s*(?:до|и\s+до)\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE), "inclusive"),
    (re.compile(r"высот[а-яё]*\s+здан[а-яё]*\s+до\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE), "max"),
    (re.compile(r"высот[а-яё]*\s+здан[а-яё]*\s+(?:более|свыше)\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE), "min"),
    (re.compile(r"на\s+высот[а-яё]*\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE), "at_least"),
    (re.compile(r"высот[а-яё]*\s+до\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE), "max"),
    (re.compile(r"высот[а-яё]*\s+(?:более|свыше)\s+(\d+(?:[.,]\d+)?)", re.IGNORECASE), "min"),
]


def _height_range(title: str) -> Tuple[float, float]:
    t = str(title or "")
    for pattern, kind in _HEIGHT_PATTERNS:
        m = pattern.search(t)
        if not m:
            continue
        try:
            if kind == "exclusive_low":
                return (float(m.group(1).replace(",", ".")) + 0.001,
                        float(m.group(2).replace(",", ".")))
            if kind == "inclusive":
                return (float(m.group(1).replace(",", ".")),
                        float(m.group(2).replace(",", ".")))
            if kind == "max":
                return (0.0, float(m.group(1).replace(",", ".")))
            if kind == "min":
                return (float(m.group(1).replace(",", ".")) + 0.001, float("inf"))
            if kind == "at_least":
                return (float(m.group(1).replace(",", ".")), float("inf"))
        except (ValueError, TypeError):
            continue
    return (0.0, float("inf"))


def _is_height_compatible(title: str, building_height: Optional[float]) -> bool:
    if not building_height or building_height <= 0:
        return True
    lo, hi = _height_range(title)
    return lo <= building_height <= hi


def _get_building_height(run_dir: str) -> Optional[float]:
    path = os.path.join(os.path.dirname(os.path.abspath(run_dir)), IFC_CONSTANTS_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        spec = (payload.get("constants") or {}).get("building_height_m") or {}
        if not spec.get("found"):
            return None
        h = float(spec.get("value", 0))
        return h if h > 0 else None
    except Exception:
        return None


def _element_thickness_mm(element_payload: Dict[str, Any]) -> Optional[int]:
    el = element_payload.get("element", {}) or {}
    v = _lookup(el, *_DEPTH_KEYS)
    if v:
        return int(round(v))
    name = str(el.get("name") or "")
    if _SECTION_RE.search(name):
        return None
    m = _MM_IN_NAME_RE.search(name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _element_min_side_mm(element_payload: Dict[str, Any]) -> Optional[int]:
    name = str((element_payload.get("element") or {}).get("name") or "")
    m = _SECTION_RE.search(name)
    if m:
        try:
            return min(int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    el = element_payload.get("element", {}) or {}
    L = _lookup(el, *_LENGTH_KEYS)
    W = _lookup(el, *_WIDTH_KEYS)
    if L and W:
        return int(round(min(L, W)))
    return None


def _work_thickness_mm(work_title: str) -> Optional[int]:
    m = _THICKNESS_WORK_RE.search(str(work_title or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _geometry_compatible(work_title: str, family: Optional[str],
                          element_payload: Dict[str, Any]) -> bool:
    work_limit = _work_thickness_mm(work_title)
    if work_limit is None:
        return True
    if family in ("wall", "masonry", "floor", "foundation_slab", "stair_landing"):
        el = _element_thickness_mm(element_payload)
    elif family in ("column", "beam"):
        el = _element_min_side_mm(element_payload)
    else:
        return True
    if el is None:
        return True
    return work_limit >= el


# ======================================================================
#  Semantic score
# ======================================================================

_SEMANTIC_PENALTY = 5000


def _semantic_penalty(work_title: str, element_payload: Dict[str, Any]) -> int:
    title = _norm_text(work_title)
    element = element_payload.get("element", {}) or {}
    material = _norm_text(element.get("material") or "")
    name = _norm_text(element.get("name") or "")
    applied = element_payload.get("applied_constants", {}) or {}
    part = str(applied.get("building_part") or "").lower()

    penalty = 0

    if "железобетон" in material:
        if "бетонн" in title and "железобетонн" not in title:
            penalty += _SEMANTIC_PENALTY
    elif "бетон" in material and "железобетон" not in material:
        if "железобетонн" in title:
            penalty += _SEMANTIC_PENALTY

    if "фундаментн" in name and "плит" in name:
        if "ленточн" in title or "ростверк" in title:
            penalty += _SEMANTIC_PENALTY
        if "подколонник" in title:
            penalty += _SEMANTIC_PENALTY

    if part:
        wp = _work_part(title)
        if wp != "universal":
            if ("подземн" in part or "цокольн" in part) and wp == "above":
                penalty += _SEMANTIC_PENALTY
            elif "надземн" in part and wp == "underground":
                penalty += _SEMANTIC_PENALTY

    if "стеклокомпозитн" in title and "стеклокомпозитн" not in name:
        penalty += _SEMANTIC_PENALTY
    if "базальтопластик" in title and "базальтопластик" not in name:
        penalty += _SEMANTIC_PENALTY

    if "ребер" in title or "ребр" in title:
        if "ребер" not in name and "ребр" not in name:
            penalty += _SEMANTIC_PENALTY

    if "лестничн" in name or "лестница" in name:
        if "марш" not in name and "площадк" not in name:
            if "площадк" in title:
                penalty += 500
            if "марш" in title:
                penalty -= 100

    return penalty


def _semantic_bonus(work_title: str, element_payload: Dict[str, Any]) -> int:
    title = _norm_text(work_title)
    element = element_payload.get("element", {}) or {}
    name = _norm_text(element.get("name") or "")
    material = _norm_text(element.get("material") or "")
    bonus = 0
    if "железобетон" in material and "железобетонн" in title:
        bonus += 100
    if "фундаментн" in name and "плит" in name and "фундаментн" in title and "плит" in title:
        bonus += 100
    if "плоск" in title:
        bonus += 50
    return bonus


# ======================================================================
#  Дедупликация
# ======================================================================

_VARIANT_SPLIT_RE = re.compile(
    r"(?:,\s*|\s+с\s+(?=[а-я])|\s+без\s+|\s+при\s+"
    r"|\s+от\s+\d|\s+до\s+\d|\s+более\s+\d|\s+свыше\s+\d)",
    re.IGNORECASE,
)


def _variant_key(title: str) -> str:
    t = str(title or "").strip().lower().replace("ё", "е")
    m = _VARIANT_SPLIT_RE.search(t)
    if m:
        t = t[:m.start()]
    t = re.sub(r"\b(?:до|свыше|более|менее|от)\s+\d+.*$", "", t)
    return _norm_text(t)


def _dedupe_variants(works: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(works) <= 1:
        return works
    seen: set = set()
    result = []
    for w in works:
        key = _variant_key(str(w.get("title") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(w)
    return result


# ======================================================================
#  Фильтрация
# ======================================================================

def _filter_works_in_table(
    works: List[Dict[str, Any]],
    family: Optional[str],
    element_part: str,
    building_height: Optional[float],
    element_payload: Dict[str, Any],
    role: str = "additional",
) -> Tuple[List[Dict[str, Any]], bool]:
    stage1 = [w for w in works
              if not _match_any(_work_full_text(w), _UNIVERSAL_HARD_ANTI)]

    if role in ("main", "package") and family and family in FAMILY_FILTERS:
        rules = FAMILY_FILTERS[family]
        stage2 = [
            w for w in stage1
            if _match_any(_work_full_text(w), rules["required"])
            and not _match_any(_work_full_text(w), rules["forbidden"])
        ]
    else:
        stage2 = stage1

    stage3 = [w for w in stage2
              if _part_compatible(_work_full_text(w), element_part)]

    stage4 = [w for w in stage3
              if _is_height_compatible(_work_full_text(w), building_height)]

    stage5 = [w for w in stage4
              if _geometry_compatible(_work_full_text(w), family, element_payload)]

    if stage5:
        result, used_fallback = stage5, False
    else:
        result, used_fallback = works, True
        for prev, name in ((stage4, "geometry"), (stage3, "height"),
                           (stage2, "part"), (stage1, "family")):
            if prev:
                logger.info(f"  таблица: fallback на этап «{name}» ({len(prev)} работ)")
                result, used_fallback = prev, True
                break

    if family in _NO_USTROYSTVO_FAMILIES:
        before = len(result)
        result = [w for w in result
                  if not _is_ustroystvo_work(w.get("title") or w.get("name"))]
        if len(result) != before:
            logger.info(
                f"Убрано «Устройство …» ({family}): "
                f"{before} → {len(result)} работ"
            )

    return result, used_fallback


# ======================================================================
#  Ранжирование
# ======================================================================

_SCORE_FAMILY = 100
_SCORE_TECH = 50
_SCORE_PART = 30


def _score_work(work: Dict[str, Any], family: Optional[str],
                 element_part: str, element_payload: Dict[str, Any]) -> int:
    title = _work_full_text(work)
    score = 0
    if family and family in FAMILY_FILTERS:
        if _match_any(title, FAMILY_FILTERS[family]["required"]):
            score += _SCORE_FAMILY
    if _match_any(title, ("монтаж", "устройство", "установка",
                            "бетонирован", "армирован", "укладка",
                            "демонтаж", "уход", "кладк")):
        score += _SCORE_TECH
    if element_part:
        wp = _work_part(title)
        if wp == "universal" or _part_compatible(title, element_part):
            score += _SCORE_PART
    score += _semantic_bonus(title, element_payload)
    score -= _semantic_penalty(title, element_payload)
    return score


def _rank_works(works: List[Dict[str, Any]], family: Optional[str],
                 element_part: str, element_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    scored = [(w, _score_work(w, family, element_part, element_payload)) for w in works]
    scored.sort(key=lambda x: (-x[1], str(x[0].get("pressmark") or "")))
    return [w for w, _ in scored]


def _pick_closest(works: List[Dict[str, Any]], family: Optional[str],
                   element_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not works:
        return None
    clean = [w for w in works
             if _semantic_penalty(_work_full_text(w), element_payload) == 0]
    if clean:
        works = clean
    element_mm = _element_thickness_mm(element_payload) \
                 or _element_min_side_mm(element_payload)
    if element_mm is None:
        return works[0]
    parsed = []
    for w in works:
        t = _work_thickness_mm(_work_full_text(w))
        if t is not None:
            parsed.append((w, t))
    if not parsed:
        return works[0]
    above = [(w, t) for w, t in parsed if t >= element_mm]
    if above:
        return min(above, key=lambda x: x[1])[0]
    return max(parsed, key=lambda x: x[1])[0]


# ======================================================================
#  Промпт
# ======================================================================

SYSTEM_PROMPT = """Ты — инженер-сметчик. Для каждой таблицы работ-кандидатов выбери РОВНО ОДНУ строку — наиболее подходящую для указанного строительного элемента.

Правила:
1. Выбирай ТОЛЬКО из строк приведённой секции таблицы.
2. В поле "pressmark" верни ПОЛНЫЙ шифр (например, "3.6-70-3").
3. В поле "table" верни ТОЛЬКО код таблицы без слова «Таблица» и без названия (например, "3.6-70").
4. По каждой таблице — ровно одна выбранная строка.
5. Для ЖЕЛЕЗОБЕТОННОГО элемента бери «железобетонных», НЕ «бетонных».
6. Для ФУНДАМЕНТНОЙ ПЛИТЫ бери «фундаментных плит» / «плоских», НЕ «ленточных», «ростверков», «с подколонниками».
7. По толщине — «до X мм» с X ≥ толщины элемента.
8. Не бери «стеклокомпозитную» / «базальтопластиковую» арматуру без неё в элементе.
9. Не бери «с рёбрами вверх», если у элемента нет рёбер.

Отвечай ТОЛЬКО JSON:
{"selected": [{"table": "3.6-70", "pressmark": "3.6-70-3", "reason": "..."}]}
"""


_TABLE_ROLE_LABELS = {
    "main": "ОСНОВНАЯ",
    "package": "ЭТАП ТЕХНОЛОГИЧЕСКОГО КОМПЛЕКСА",
    "additional": "ДОПОЛНИТЕЛЬНАЯ",
}


def _build_user_prompt(element_payload, family, tables_with_works,
                        building_height, group_count):
    element = element_payload.get("element", {}) or {}
    applied = element_payload.get("applied_constants", {}) or {}

    lines = ["## Элемент"]
    if group_count:
        lines.append(f"Количество в группе: {group_count}")
    if element.get("name"):
        lines.append(f"Наименование: {element['name']}")
    if element.get("ifc_class"):
        lines.append(f"IFC-класс: {element['ifc_class']}")
    mssk = element_payload.get("mssk_context") or {}
    if mssk.get("name"):
        lines.append(f"МССК: {mssk.get('name')}")
    if element.get("material"):
        lines.append(f"Материал: {element['material']}")
    if applied.get("building_part"):
        lines.append(f"Часть здания: {applied['building_part']}")
    if building_height:
        lines.append(f"Высота здания: {building_height:.1f} м")

    thickness = _element_thickness_mm(element_payload)
    if thickness:
        lines.append(f"Толщина элемента: {thickness} мм")
    min_side = _element_min_side_mm(element_payload)
    if min_side:
        lines.append(f"Наименьшая сторона сечения: {min_side} мм")

    lines.append("")
    lines.append("## Задача")
    lines.append("По КАЖДОЙ таблице ниже выбери РОВНО ОДНУ строку. Верни JSON: "
                 '{"selected": [{"table": "3.6-70", "pressmark": "3.6-70-3", "reason": "..."}]}.')

    for entry in tables_with_works:
        table = entry["table"]
        role = entry.get("role", "additional")
        is_fallback = entry.get("fallback", False)
        role_label = _TABLE_ROLE_LABELS.get(role, role)
        fallback_note = "  ⚠️ нет точного соответствия — выбери ближайший" if is_fallback else ""
        lines.append("")
        lines.append(f"## {table['code']} | {table['name']} [{role_label}]{fallback_note}")
        for w in entry["works"]:
            unit = w.get("unitOfMeasure") or w.get("unit_of_measure") or ""
            lines.append(f"- {w.get('pressmark')} | {w.get('title')} | {unit}")

    return "\n".join(lines)


# ======================================================================
#  LLM-сопоставление
# ======================================================================

def _match_selection(selected, table_to_works):
    result = {}
    for item in selected or []:
        if not isinstance(item, dict):
            continue
        table_code = _extract_table_code(item.get("table"))
        pm = str(item.get("pressmark") or "").strip()
        reason = str(item.get("reason") or "")
        if not table_code or not pm:
            continue
        works = table_to_works.get(table_code)
        if not works:
            logger.warning(f"LLM: неизвестная таблица {table_code!r}")
            continue
        by_pm = {str(w.get("pressmark")): w for w in works}
        work = by_pm.get(pm)
        if work is None:
            logger.warning(f"LLM: шифр {pm} не из таблицы {table_code}")
            continue
        if table_code in result:
            continue
        result[table_code] = {"work": work, "reason": reason, "llm": True}
    return result


# ======================================================================
#  Параметры подбора
# ======================================================================

_PARAM_ALIASES: Dict[str, tuple] = {
    "perimeter": ("perimeter", "Периметр", "Периметр, мм", "Периметр, м"),
    "thickness": (
        "thickness", "Толщина", "Толщина, мм", "Толщина, м",
        "Ширина", "Ширина, мм", "Ширина, м",
    ),
    "length":    ("length", "Длина", "Длина, мм", "Длина, м"),
    "width":     ("width", "Ширина", "Ширина, мм", "Ширина, м"),
    "height":    ("height", "Высота", "Высота, мм", "Высота, м"),
}

_UNIT_NORMALIZE = {
    "мм": ("м", 1e-3), "mm": ("м", 1e-3),
    "мм2": ("м2", 1e-6), "mm2": ("м2", 1e-6), "мм²": ("м2", 1e-6),
    "мм3": ("м3", 1e-9), "mm3": ("м3", 1e-9), "мм³": ("м3", 1e-9),
}


def _load_element_params(run_dir: str) -> Dict[str, Dict[str, Any]]:
    session_dir = os.path.dirname(os.path.abspath(run_dir))
    path = os.path.join(session_dir, ELEMENT_PARAMS_FILENAME)
    if not os.path.isfile(path):
        logger.warning(f"Файл параметров подбора не найден ({path})")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}
    params_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in payload.get("elements", []) or []:
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("global_id") or "").strip()
        if gid and isinstance(entry.get("parameters"), dict):
            params_by_id[gid] = entry["parameters"]
    logger.info(f"Параметры подбора загружены: {len(params_by_id)} записей")
    return params_by_id


def _normalize_param_value(value: Any, unit: str):
    norm = _UNIT_NORMALIZE.get(str(unit or "").strip())
    if norm is None:
        return value, unit
    target_unit, factor = norm
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value, unit
    return value * factor, target_unit


def _param_meters(params: Optional[Dict[str, Any]], name: str) -> Optional[float]:
    if not isinstance(params, dict):
        return None
    aliases = _PARAM_ALIASES.get(name, (name,))
    for alias in aliases:
        spec = params.get(alias)
        if not isinstance(spec, dict):
            continue
        if spec.get("origin") == "not_found":
            continue
        num = safe_float(spec.get("value"), default=None)
        if num is None or num <= 0:
            continue
        value, unit = _normalize_param_value(num, spec.get("unit"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if str(unit or "").strip().lower() in ("м", "m") and value > 100:
            value = value / 1000.0
        return float(value)
    return None


def _joint_length_m(params: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(params, dict):
        return None
    explicit = _param_meters(params, "perimeter")
    if explicit is not None and 0 < explicit < 1000:
        return explicit / 2.0
    length = _param_meters(params, "length")
    height = _param_meters(params, "height")
    width = _param_meters(params, "width")
    if length is not None and height is not None:
        return length + height
    if length is not None and width is not None:
        return length + width
    return None


def _element_perimeter_m(element: Dict[str, Any],
                          params: Optional[Dict[str, Any]] = None) -> Optional[float]:
    if params:
        p = _joint_length_m(params)
        if p and p > 0:
            return 2.0 * p
    L = _lookup(element, *_LENGTH_KEYS)
    H = _lookup(element, *_HEIGHT_KEYS)
    W = _lookup(element, *_WIDTH_KEYS)
    if L and H:
        return 2.0 * (L + H) / 1000.0
    if L and W:
        return 2.0 * (L + W) / 1000.0
    if L:
        return L / 1000.0
    return None


# ======================================================================
#  Полные данные элементов из filtered_elements_grouped_AR.json
# ======================================================================

def _load_full_element_data(run_dir: str) -> Dict[str, Dict[str, Any]]:
    path = os.path.join(run_dir, GROUPED_JSON_FILENAME)
    if not os.path.isfile(path):
        logger.warning(f"Нет {GROUPED_JSON_FILENAME} — геометрия элементов недоступна")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = json.load(fh)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}

    result: Dict[str, Dict[str, Any]] = {}

    def walk(node):
        if not isinstance(node, dict):
            return
        fe = node.get("first_element")
        if isinstance(fe, dict):
            gid = str(fe.get("GlobalId") or fe.get("global_id") or "").strip()
            if gid and gid not in result:
                result[gid] = fe
        for ch in node.get("children") or []:
            walk(ch)

    if isinstance(tree, list):
        for n in tree:
            walk(n)
    logger.info(f"Полные данные элементов загружены: {len(result)} записей")
    return result


def _full_value(full: Optional[Dict[str, Any]], *keys: str) -> Optional[float]:
    if not isinstance(full, dict):
        return None
    for k in keys:
        v = safe_float(full.get(k), default=None)
        if v is not None and v > 0:
            return float(v)
    return None


def _foundation_slab_perimeter_m(
    element: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    full: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    if full:
        p = _full_value(full, *_SLAB_PERIMETER_KEYS)
        if p and p > 0:
            return p / 1000.0 if p > 500 else p

    p = _lookup(element, *_SLAB_PERIMETER_KEYS)
    if p and p > 0:
        return p / 1000.0 if p > 500 else p

    if params:
        explicit = _param_meters(params, "perimeter")
        if explicit and 0 < explicit < 1000:
            return explicit

    if full:
        L_mm = _full_value(full, *_LENGTH_KEYS)
        W_mm = _full_value(full, *_WIDTH_KEYS)
        if L_mm and W_mm:
            return 2.0 * (L_mm + W_mm) / 1000.0

    if params:
        L = _param_meters(params, "length")
        W = _param_meters(params, "width")
        if L and W:
            return 2.0 * (L + W)

    L_mm = _lookup(element, *_LENGTH_KEYS)
    W_mm = _lookup(element, *_WIDTH_KEYS)
    if L_mm and W_mm:
        return 2.0 * (L_mm + W_mm) / 1000.0

    if L_mm:
        return L_mm / 1000.0
    return None


def _sum_joint_length(payloads: List[Dict[str, Any]],
                       params_by_id: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[float]:
    total = 0.0
    found = False
    for payload in payloads:
        el = payload.get("element") or {}
        gid = str(el.get("global_id") or "").strip()
        p = _element_perimeter_m(el, (params_by_id or {}).get(gid))
        if p:
            total += p
            found = True
    return round(total, 4) if found else None


# ======================================================================
#  Площадь опалубки
# ======================================================================

def _formwork_area_m2(
    element_payload: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    full: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    el = element_payload.get("element", {}) or {}
    family = _classify_family(element_payload)

    if family == "foundation_slab":
        perimeter = _foundation_slab_perimeter_m(el, params, full)

        t: Optional[float] = None
        if full:
            depth_mm = _full_value(full, *_DEPTH_KEYS)
            if depth_mm:
                t = depth_mm / 1000.0
        if not t:
            depth_mm = _lookup(el, *_DEPTH_KEYS)
            if depth_mm:
                t = depth_mm / 1000.0
        if not t and params:
            t = _param_meters(params, "thickness") or _param_meters(params, "width")
        if not t:
            name_s = str(el.get("name") or "")
            m = _MM_IN_NAME_RE.search(name_s)
            if m:
                try:
                    t = int(m.group(1)) / 1000.0
                except ValueError:
                    pass

        logger.info(
            f"[formwork slab] gid={str(el.get('global_id') or '')[:8]} "
            f"name={str(el.get('name') or '')[:50]!r} "
            f"perimeter_m={perimeter} thickness_m={t} "
            f"area={perimeter * t if perimeter and t else None}"
        )

        if perimeter and t:
            return perimeter * t
        return None

    if params:
        thickness = _param_meters(params, "thickness") \
                    or _param_meters(params, "width")
        if family in ("wall", "masonry"):
            L = _param_meters(params, "length")
            H = _param_meters(params, "height")
            if L and H:
                return 2.0 * L * H
        if family in ("floor", "plate"):
            L = _param_meters(params, "length")
            W = _param_meters(params, "width")
            if L and W and thickness:
                return 2.0 * (L + W) * thickness
        if family == "column":
            L = _param_meters(params, "length")
            W = _param_meters(params, "width")
            H = _param_meters(params, "height")
            if L and W and H:
                return 2.0 * (L + W) * H
        perimeter = _joint_length_m(params)
        if perimeter and thickness:
            return 2.0 * perimeter * thickness

    if full:
        L_mm = _full_value(full, *_LENGTH_KEYS)
        W_mm = _full_value(full, *_WIDTH_KEYS)
        H_mm = _full_value(full, *_HEIGHT_KEYS)
        t_mm = _full_value(full, *_DEPTH_KEYS)
        if family in ("floor", "plate") and L_mm and W_mm and t_mm:
            return 2.0 * ((L_mm + W_mm) / 1000.0) * (t_mm / 1000.0)
        if family in ("wall", "masonry") and L_mm and H_mm:
            return 2.0 * (L_mm / 1000.0) * (H_mm / 1000.0)
        if family == "column" and L_mm and W_mm and H_mm:
            return 2.0 * ((L_mm + W_mm) / 1000.0) * (H_mm / 1000.0)

    L_mm = _lookup(el, *_LENGTH_KEYS)
    W_mm = _lookup(el, *_WIDTH_KEYS)
    H_mm = _lookup(el, *_HEIGHT_KEYS)
    t_mm = _lookup(el, *_DEPTH_KEYS) or _element_thickness_mm(element_payload)

    if not L_mm:
        return None
    L = L_mm / 1000.0

    if family in ("wall", "masonry"):
        if H_mm:
            return 2.0 * L * (H_mm / 1000.0)
        if W_mm:
            return 2.0 * L * (W_mm / 1000.0)

    if family in ("floor", "plate"):
        if W_mm and t_mm:
            W = W_mm / 1000.0
            t = t_mm / 1000.0
            return 2.0 * (L + W) * t
        return None

    if family == "column":
        if W_mm and H_mm:
            return 2.0 * (L + W_mm / 1000.0) * (H_mm / 1000.0)
        return None

    if H_mm:
        return 2.0 * L * (H_mm / 1000.0)
    if W_mm and t_mm:
        return 2.0 * (L + W_mm / 1000.0) * (t_mm / 1000.0)
    return None


def _sum_formwork_area(
    payloads: List[Dict[str, Any]],
    params_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    full_data_by_gid: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[float]:
    total = 0.0
    found = False
    for payload in payloads:
        gid = str((payload.get("element") or {}).get("global_id") or "").strip()
        a = _formwork_area_m2(
            payload,
            (params_by_id or {}).get(gid),
            (full_data_by_gid or {}).get(gid),
        )
        if a:
            total += a
            found = True
    return round(total, 4) if found else None


# ======================================================================
#  Масса арматуры
# ======================================================================

def _load_rebar_mass_by_gid(run_dir: str) -> Dict[str, float]:
    path = os.path.join(run_dir, FILTERED_XLSX_FILENAME)
    if not os.path.isfile(path):
        logger.warning(f"Нет {FILTERED_XLSX_FILENAME} — расход арматуры не учтён")
        return {}
    try:
        df = pd.read_excel(path)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}
    if "GlobalId" not in df.columns or "ReinforcementVolumeRatio" not in df.columns:
        logger.warning(
            "В filtered_elements.xlsx нет колонок GlobalId / "
            "ReinforcementVolumeRatio — расход арматуры не будет учтён"
        )
        return {}
    volume_col = "Объём, м3" if "Объём, м3" in df.columns else None
    if volume_col is None:
        for col in df.columns:
            s = str(col)
            if s.startswith("Объём") and "м3" in s and not s.startswith("QTO"):
                volume_col = col
                break
    if volume_col is None:
        logger.warning("В filtered_elements.xlsx нет колонки объёма")
        return {}
    masses: Dict[str, float] = {}
    for _, row in df.iterrows():
        gid = str(row.get("GlobalId") or "").strip()
        if not gid:
            continue
        ratio = safe_float(row.get("ReinforcementVolumeRatio"), default=0.0)
        volume = safe_float(row.get(volume_col), default=0.0)
        if ratio > 0 and volume > 0:
            masses[gid] = masses.get(gid, 0.0) + ratio * volume
    logger.info(f"Расход арматуры определён для {len(masses)} элементов")
    return masses


def _assign_rebar_volume(selected_works: List[Dict[str, Any]],
                          rebar_mass_kg: float,
                          family: Optional[str]) -> None:
    if rebar_mass_kg <= 0 or not selected_works:
        return
    if family == "wall":
        candidates = [
            w for w in selected_works
            if _is_rebar_ton_work(w)
            and _is_rebar_install_work(str(w.get("title") or ""))
        ]
    else:
        candidates = [w for w in selected_works if _is_rebar_ton_work(w)]
    if not candidates:
        return
    target = next(
        (w for w in candidates
         if "отдельн" in str(w.get("title") or "").lower()
         and "стержн" in str(w.get("title") or "").lower()),
        candidates[0],
    )
    tons = round(rebar_mass_kg / 1000.0, 4)
    quantity = dict(target.get("quantity") or {})
    quantity["mass_t"] = tons
    target["quantity"] = quantity
    logger.info(f"Арматура → «{target.get('title')}»: {tons:.4f} т")


def _calc_rebar_mass_t(element_payload: Dict[str, Any]) -> Optional[float]:
    el = element_payload.get("element", {}) or {}
    ratio = _lookup(el, *_RATIO_KEYS)
    volume = _lookup(el, *_VOLUME_KEYS)
    if not ratio or not volume:
        return None
    return round(ratio * volume / 1000.0, 4)


def _sum_group_rebar_mass_t(payloads: List[Dict[str, Any]]) -> Optional[float]:
    total = 0.0
    found = False
    for payload in payloads:
        m = _calc_rebar_mass_t(payload)
        if m:
            total += m
            found = True
    return round(total, 4) if found else None


def _sum_group_rebar_kg(payloads: List[Dict[str, Any]],
                          rebar_mass_by_gid: Dict[str, float]) -> float:
    total = 0.0
    for payload in payloads:
        gid = str((payload.get("element") or {}).get("global_id") or "").strip()
        total += rebar_mass_by_gid.get(gid, 0.0)
    return round(total, 2)


# ======================================================================
#  Quantity
# ======================================================================

_QUANTITY_KEYS = {
    "volume_m3":      ("volume_m3", "Объём, м3", "Объем, м3"),
    "area_m2":        ("area_m2", "Площадь, м2"),
    "length_m":       ("length_m", "Длина, м"),
    "joint_length_m": ("joint_length_m",),
    "count":          ("count", "Количество", "шт"),
}


def _pick_quantity(q, canonical):
    if not q:
        return None
    for key in _QUANTITY_KEYS.get(canonical, (canonical,)):
        v = safe_float(q.get(key), default=None)
        if v is not None and v > 0:
            return float(v)
    return None


def _enrich_quantity(q, element):
    result = dict(q or {})
    element = element or {}

    if _pick_quantity(result, "volume_m3") is None:
        v = _lookup(element, *_VOLUME_KEYS)
        if v and v > 0:
            result["volume_m3"] = float(v)

    if _pick_quantity(result, "area_m2") is None:
        a = safe_float(element.get("Площадь, м2"), default=None)
        if a and a > 0:
            result["area_m2"] = float(a)

    if _pick_quantity(result, "length_m") is None:
        L = _lookup(element, *_LENGTH_KEYS)
        if L and L > 0:
            result["length_m"] = float(L) / 1000.0

    if _pick_quantity(result, "joint_length_m") is None:
        p = _element_perimeter_m(element)
        if p and p > 0:
            result["joint_length_m"] = float(p)

    if not result.get("count"):
        result["count"] = 1
    return result


def _element_quantity_for_table(element_payload, table_code):
    for work in element_payload.get("works", []) or []:
        if str(work.get("code")) == str(table_code):
            return _enrich_quantity(dict(work.get("quantity") or {}),
                                     element_payload.get("element") or {})
    for work in element_payload.get("works", []) or []:
        return _enrich_quantity(dict(work.get("quantity") or {}),
                                 element_payload.get("element") or {})
    return _enrich_quantity({}, element_payload.get("element") or {})


def _sum_group_quantities(payloads):
    total = {"volume_m3": 0.0, "area_m2": 0.0, "length_m": 0.0,
             "joint_length_m": 0.0, "count": 0}
    for payload in payloads:
        q = {}
        for work in payload.get("works", []) or []:
            wq = work.get("quantity") or {}
            if any(wq.get(k) for k in ("volume_m3", "area_m2", "length_m")):
                q = dict(wq)
                break
        q = _enrich_quantity(q, payload.get("element") or {})
        for key in ("volume_m3", "area_m2", "length_m", "joint_length_m"):
            v = _pick_quantity(q, key)
            if v:
                total[key] += v
        cnt = _pick_quantity(q, "count")
        total["count"] += int(cnt) if cnt else 1
    return total


# ======================================================================
#  Строка работы
# ======================================================================

def _build_work_row(
    element_payload, table, work, reason, llm_selected,
    quantity=None,
    group_formwork_area: Optional[float] = None,
    group_joint_length_m: Optional[float] = None,
):
    if quantity is None:
        quantity = _element_quantity_for_table(element_payload, table["code"])
    quantity = _enrich_quantity(dict(quantity or {}),
                                 element_payload.get("element") or {})

    if _is_formwork_work(work):
        if group_formwork_area is not None:
            quantity["area_m2"] = group_formwork_area
        else:
            quantity.pop("area_m2", None)

    if group_joint_length_m is not None and _is_joint_work(work):
        quantity["joint_length_m"] = group_joint_length_m

    return {
        "pressmark": work.get("pressmark"),
        "title": work.get("title"),
        "unit_of_measure": (work.get("unitOfMeasure") or work.get("unit_of_measure")
                            or work.get("unit") or work.get("measure")),
        "work_id": work.get("id"),
        "table_code": table["code"],
        "table_name": table["name"],
        "salary": work.get("salary"),
        "cur_salary": work.get("curSalary"),
        "operation_of_machines": work.get("operationOfMachines"),
        "cur_operation_of_machines": work.get("curOperationOfMachines"),
        "cost_of_material_resources": work.get("costOfMaterialResources"),
        "cur_cost_of_material_resources": work.get("curCostOfMaterialResources"),
        "direct_costs": work.get("directCosts"),
        "cur_direct_costs": work.get("curDirectCosts"),
        "quantity": quantity,
        "reason": reason,
        "llm_selected": llm_selected,
    }


# ======================================================================
#  Стоимость
# ======================================================================

def _enrich_selected_works_with_costs(result_elements, period_id):
    try:
        period_num = int(period_id)
    except (TypeError, ValueError):
        return
    work_ids, seen = [], set()
    for entry in result_elements:
        for w in entry.get("selected_works") or []:
            wid = w.get("work_id")
            if wid is not None and wid not in seen:
                seen.add(wid)
                work_ids.append(int(wid))
    if not work_ids:
        return
    try:
        from src.services.works_fetcher import fetch_work_details
        details = fetch_work_details(work_ids, period_num)
    except Exception as exc:
        logger.warning(f"fetch_work_details failed: {exc}")
        return
    field_map = {
        "salary": "salary", "curSalary": "cur_salary",
        "operationOfMachines": "operation_of_machines",
        "curOperationOfMachines": "cur_operation_of_machines",
        "costOfMaterialResources": "cost_of_material_resources",
        "curCostOfMaterialResources": "cur_cost_of_material_resources",
        "directCosts": "direct_costs", "curDirectCosts": "cur_direct_costs",
    }
    for entry in result_elements:
        for w in entry.get("selected_works") or []:
            detail = details.get(w.get("work_id"))
            if not detail:
                continue
            for src, dst in field_map.items():
                v = detail.get(src)
                if v is not None:
                    w[dst] = v


# ======================================================================
#  Группы
# ======================================================================

def _load_leaf_groups(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = json.load(fh)
    except Exception:
        return []
    leaves = []

    def walk(nodes, path_):
        for node in nodes or []:
            cur = path_ + [str(node.get("name", ""))]
            children = node.get("children") or []
            if children:
                walk(children, cur)
            else:
                indices = [int(i) for i in node.get("indices", []) or []
                           if isinstance(i, (int, str))
                           and str(i).strip().lstrip("-").isdigit()]
                if indices:
                    leaves.append({"name": str(node.get("name", "")),
                                   "path": cur, "indices": sorted(indices)})

    if isinstance(tree, list):
        walk(tree, [])
    return leaves


# ======================================================================
#  Основной алгоритм
# ======================================================================

def _select_works_for_element(
    element_payload, works_by_table, family, element_part,
    building_height, llm_client, group_count,
    group_quantity, group_formwork_area, group_joint_length_m,
):
    tables_with_works = []
    for tbl_meta in element_payload.get("works", []) or []:
        code = str(tbl_meta.get("code") or "").strip()
        if not code:
            continue
        table = {
            "code": code,
            "name": tbl_meta.get("name") or "",
            "role": tbl_meta.get("role") or "additional",
        }
        all_works = list((works_by_table.get(code, {}) or {}).get("works", []) or [])
        if not all_works:
            continue

        expected_prefix = code + "-"
        wrong = [w for w in all_works
                 if not str(w.get("pressmark") or "").startswith(expected_prefix)]
        if wrong:
            logger.error(
                f"Таблица {code}: {len(wrong)} работ с чужими шифрами. Отфильтровано."
            )
            all_works = [w for w in all_works
                         if str(w.get("pressmark") or "").startswith(expected_prefix)]
            if not all_works:
                continue

        filtered, fallback = _filter_works_in_table(
            all_works, family, element_part, building_height, element_payload,
            role=table["role"],
        )
        if not filtered:
            continue
        filtered = _dedupe_variants(filtered)
        filtered = _rank_works(filtered, family, element_part, element_payload)
        tables_with_works.append({
            "table": table,
            "works": filtered,
            "fallback": fallback,
        })

    if not tables_with_works:
        return [], "Нет работ в таблицах после фильтрации"

    user_prompt = _build_user_prompt(element_payload, family,
                                       tables_with_works, building_height,
                                       group_count)
    table_to_works = {e["table"]["code"]: e["works"] for e in tables_with_works}

    selected_by_table = {}
    note = ""
    try:
        answer = llm_client.complete_json(system=SYSTEM_PROMPT, user=user_prompt)
        selected = (answer or {}).get("selected")
        if not isinstance(selected, list):
            selected = []
        selected_by_table = _match_selection(selected, table_to_works)
    except Exception as exc:
        note = f"Ошибка LLM ({exc})"
        logger.error(f"LLM failed: {exc}", exc_info=True)

    selected_works = []
    for entry in tables_with_works:
        table = entry["table"]
        code = table["code"]
        works = entry["works"]

        if code in selected_by_table:
            item = selected_by_table[code]
            reason = item.get("reason") or "выбор LLM"
            llm_sel = True
            work = item["work"]
        else:
            work = _pick_closest(works, family, element_payload)
            if work is None:
                continue
            reason = "LLM не выбрала работу по таблице — взята ближайшая"
            llm_sel = False
            logger.warning(f"Таблица {code}: LLM пропустила — "
                            f"взята ближайшая {work.get('pressmark')}")

        if entry.get("fallback") and llm_sel:
            reason += " (в таблице не было точного соответствия — выбран ближайший)"

        selected_works.append(_build_work_row(
            element_payload, table, work, reason, llm_sel,
            quantity=group_quantity,
            group_formwork_area=group_formwork_area,
            group_joint_length_m=group_joint_length_m,
        ))

    return selected_works, note


# ======================================================================
#  Главная функция
# ======================================================================

def select_final_works(tables_json_path, works_json_path, run_dir,
                        llm_config=None):
    if not os.path.isfile(tables_json_path) or not os.path.isfile(works_json_path):
        logger.warning("Нет входных файлов")
        return None

    with open(tables_json_path, "r", encoding="utf-8") as fh:
        tables_payload = json.load(fh)
    with open(works_json_path, "r", encoding="utf-8") as fh:
        works_payload = json.load(fh)

    works_by_table = {str(t.get("code")): t
                       for t in works_payload.get("tables", []) or []}

    from src.services.pd_parser import LLMClient
    if llm_config is None:
        cfg = load_config()
        from src.services.pd_parser import Config as LLMConfig
        llm_config = LLMConfig(llm_base_url=cfg.ollama_url, llm_model=cfg.model_ollama)
    llm = LLMClient(llm_config)

    result_elements = []
    total_selected = 0
    elements_payload = tables_payload.get("elements", []) or []

    leaf_groups = _load_leaf_groups(os.path.join(run_dir, GROUPED_JSON_FILENAME))
    building_height = _get_building_height(run_dir)
    if building_height:
        logger.info(f"Высота здания: {building_height:.2f} м")

    params_by_id = _load_element_params(run_dir)
    rebar_mass_by_gid = _load_rebar_mass_by_gid(run_dir)
    full_data_by_gid = _load_full_element_data(run_dir)

    processing_units = []
    if leaf_groups:
        covered = set()
        for group in leaf_groups:
            indices = [i for i in group["indices"] if 0 <= i < len(elements_payload)]
            if not indices:
                continue
            covered.update(indices)
            processing_units.append({
                "first": elements_payload[indices[0]],
                "payloads": [elements_payload[i] for i in indices],
                "group": group,
            })
        for idx, payload in enumerate(elements_payload):
            if idx not in covered:
                processing_units.append({"first": payload,
                                          "payloads": [payload],
                                          "group": None})
    else:
        processing_units = [{"first": p, "payloads": [p], "group": None}
                            for p in elements_payload]

    for unit in processing_units:
        element_payload = unit["first"]
        group = unit["group"]
        element = element_payload.get("element", {}) or {}
        group_quantity = _sum_group_quantities(unit["payloads"]) if group else None

        group_formwork_area = _sum_formwork_area(
            unit["payloads"], params_by_id, full_data_by_gid,
        )
        group_joint_m = _sum_joint_length(unit["payloads"], params_by_id)
        group_rebar_t_fallback = _sum_group_rebar_mass_t(unit["payloads"])
        group_rebar_kg = _sum_group_rebar_kg(unit["payloads"], rebar_mass_by_gid)

        family = _classify_family(element_payload)
        applied = element_payload.get("applied_constants", {}) or {}
        element_part = str(applied.get("building_part") or "").lower()

        logger.info(f"«{element.get('name')}»: family={family}, "
                    f"part={element_part or 'unknown'}, "
                    f"tables={len(element_payload.get('works', []) or [])}, "
                    f"formwork_m2={group_formwork_area}, "
                    f"rebar_kg={group_rebar_kg}, "
                    f"joint_m={group_joint_m}")

        selected_works, note = _select_works_for_element(
            element_payload, works_by_table, family, element_part,
            building_height, llm,
            group_count=len(unit["payloads"]) if group else None,
            group_quantity=group_quantity,
            group_formwork_area=group_formwork_area,
            group_joint_length_m=group_joint_m,
        )

        rebar_kg_for_assign = group_rebar_kg
        if rebar_kg_for_assign <= 0 and group_rebar_t_fallback:
            rebar_kg_for_assign = group_rebar_t_fallback * 1000.0
        if rebar_kg_for_assign > 0 and selected_works:
            _assign_rebar_volume(selected_works, rebar_kg_for_assign, family)

        total_selected += len(selected_works)
        entry = {
            "element": element_payload.get("element", {}),
            "mssk_context": element_payload.get("mssk_context"),
            "tables": element_payload.get("works", []),
            "selected_works": selected_works,
            "note": note,
            "family": family,
            "part": element_part,
            "work_type": element_payload.get("work_type", ""),
        }
        if group:
            entry["group"] = {"name": group.get("name", ""),
                              "element_count": len(unit["payloads"])}
            entry["group_path"] = list(group.get("path") or [])
            entry["group_quantity"] = group_quantity
            entry["group_formwork_area_m2"] = group_formwork_area
            entry["group_joint_length_m"] = group_joint_m
            if rebar_kg_for_assign > 0:
                entry["group_rebar_mass_kg"] = rebar_kg_for_assign
        result_elements.append(entry)

    _enrich_selected_works_with_costs(result_elements, works_payload.get("period_id"))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processing_type": "AR",
        "period": works_payload.get("period"),
        "period_id": works_payload.get("period_id"),
        "source": os.path.basename(works_json_path),
        "total_elements": len(result_elements),
        "total_works": total_selected,
        "elements": result_elements,
    }

    json_path = os.path.join(run_dir, FINAL_WORKS_JSON_FILENAME)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    xlsx_path = os.path.join(run_dir, FINAL_WORKS_XLSX_FILENAME)
    build_final_works_xlsx(result_elements, xlsx_path)
    return json_path


# ======================================================================
#  Excel
# ======================================================================

def _clean_header(name):
    text = str(name or "").strip()
    if ":" in text:
        parts = text.split(":")
        if parts[-1].strip().isdigit():
            text = ":".join(parts[:-1])
    return text


_PART_LABELS = {
    "подземная/цокольная": "подземная/цокольная",
    "надземная": "надземная",
}


def _element_header(entry):
    element = entry.get("element", {}) or {}
    base = _clean_header(element.get("name")) or str(element.get("ifc_class") or "Элемент")

    parts = []
    material = str(element.get("material") or "").strip()
    if material:
        parts.append(material)
    mssk = str((entry.get("mssk_context") or {}).get("name") or "").strip()
    if mssk and mssk.lower() != base.lower():
        parts.append(mssk)

    head = f"{base} ({', '.join(parts)})" if parts else base

    grp = entry.get("group") or {}
    cnt = grp.get("element_count")
    if cnt and cnt > 1:
        head += f" Кол-во: {cnt}"

    return head


# ======================================================================
#  Объём работ
# ======================================================================

def _work_divisor(work: Dict[str, Any]) -> float:
    unit = str(work.get("unit_of_measure") or work.get("unitOfMeasure") or "")
    unit = unit.strip().lower().replace(" ", "")
    unit = unit.replace("²", "2").replace("³", "3")
    m = _UNIT_MULTIPLIER_RE.match(unit)
    if not m:
        return 1.0
    try:
        v = float(m.group(1).replace(",", "."))
        return v if v > 0 else 1.0
    except ValueError:
        return 1.0


def _resolve_unit_label(work: Dict[str, Any]) -> str:
    unit = str(work.get("unit_of_measure", "")
               or work.get("unitOfMeasure", "") or "")
    unit_low = unit.lower().replace(" ", "")
    divisor = _work_divisor(work)

    if ("м3" in unit_low or "m3" in unit_low
            or "м[3" in unit_low or "m[3" in unit_low):
        base = "м³"
    elif ("м2" in unit_low or "m2" in unit_low
            or "м[2" in unit_low or "m[2" in unit_low):
        base = "м²"
    elif "шт" in unit_low:
        base = "шт"
    else:
        return unit or ""

    return f"{divisor} {base}" if divisor > 1 else base


def _build_total_measure(quantity: Dict[str, Any]) -> Dict[str, Any]:
    q = quantity or {}
    vol = _pick_quantity(q, "volume_m3")
    if vol and vol > 0:
        return {"type": "volume", "value": float(vol), "unit": "м3"}
    area = _pick_quantity(q, "area_m2")
    if area and area > 0:
        return {"type": "area", "value": float(area), "unit": "м2"}
    length = _pick_quantity(q, "length_m")
    if length and length > 0:
        return {"type": "length", "value": float(length), "unit": "м"}
    joint = _pick_quantity(q, "joint_length_m")
    if joint and joint > 0:
        return {"type": "joint", "value": float(joint), "unit": "м"}
    cnt = _pick_quantity(q, "count")
    if cnt and cnt > 0:
        return {"type": "count", "value": float(cnt), "unit": "шт"}
    return {"type": "", "value": 0.0, "unit": ""}


def _build_total_areas(quantity: Dict[str, Any]) -> Dict[str, Any]:
    q = quantity or {}
    return {"area_m2": _pick_quantity(q, "area_m2") or 0.0}


def _pick_area_value(total_areas: Optional[Dict[str, Any]]) -> float:
    if not total_areas:
        return 0.0
    if isinstance(total_areas, (int, float)):
        return float(total_areas)
    if not isinstance(total_areas, dict):
        return 0.0
    for key in ("area_m2", "side_area", "net_side_area",
                "area", "value", "total", "Площадь, м2"):
        v = safe_float(total_areas.get(key), default=None)
        if v is not None and v > 0:
            return float(v)
    return 0.0


def _calculate_work_volume(
    work: Dict[str, Any],
    total_measure: Dict[str, Any],
    total_areas: Optional[Dict[str, Any]] = None,
    formwork_area: float = 0.0,
) -> str:
    measure_type = (total_measure or {}).get("type", "")
    measure_value = safe_float((total_measure or {}).get("value", 0))

    if measure_value <= 0:
        return ""

    unit = str(work.get("unitOfMeasure", "")
               or work.get("unit_of_measure", "") or "")
    unit = unit.lower().replace(" ", "").replace("²", "2").replace("³", "3")

    is_volume = ("м3" in unit or "m3" in unit
                 or "м[3" in unit or "m[3" in unit)
    is_area = ("м2" in unit or "m2" in unit
               or "м[2" in unit or "m[2" in unit)
    is_count = ("шт" in unit or any(
        k in unit for k in
        ("штук", "конструкц", "элемент", "сборн", "компл", "узл")
    ))
    is_length = "м" in unit and not is_area and not is_volume
    is_ton = bool(_TON_UNIT_RE.search(unit))

    divisor = _work_divisor(work)

    if is_volume and measure_type == "volume":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 3
        return f"{vol:.{decimals}f}"

    if is_area and measure_type == "area":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 2
        return f"{vol:.{decimals}f}"

    if is_count and measure_type == "count":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 0
        return f"{vol:.{decimals}f}"

    if is_area and _is_formwork_work(work) and formwork_area > 0:
        vol = formwork_area / divisor
        decimals = 4 if divisor > 1 else 2
        return f"{vol:.{decimals}f}"

    if is_area and total_areas:
        area_value = _pick_area_value(total_areas)
        if area_value > 0:
            vol = area_value / divisor
            decimals = 4 if divisor > 1 else 2
            return f"{vol:.{decimals}f}"

    if is_length and measure_type == "joint":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 2
        return f"{vol:.{decimals}f}"

    if is_length and measure_type == "length":
        vol = measure_value / divisor
        decimals = 4 if divisor > 1 else 2
        return f"{vol:.{decimals}f}"

    if is_ton:
        mass = safe_float((work.get("quantity") or {}).get("mass_t"),
                           default=None)
        if mass and mass > 0:
            return f"{mass / divisor:.4f}"

    return ""


# ======================================================================
#  Части здания — ТРИ разных по path[0]
# ======================================================================

BUILDING_PART_ORDER = ("Подземная", "Цокольная", "Надземная")

_BUILDING_PART_TITLES = {
    "Подземная": "ПОДЗЕМНАЯ ЧАСТЬ",
    "Цокольная": "ЦОКОЛЬНАЯ ЧАСТЬ",
    "Надземная": "НАДЗЕМНАЯ ЧАСТЬ",
}

_BUILDING_PART_TOTAL_LABELS = {
    "Подземная": "ИТОГО по подземной части:",
    "Цокольная": "ИТОГО по цокольной части:",
    "Надземная": "ИТОГО по надземной части:",
}


def _resolve_building_part(entry):
    """Часть здания: Подземная / Цокольная / Надземная.

    Приоритет:
      1) group_path[0] — первый уровень группировки
         («Подземная часть здания (до отм. 0,000)» / «Цокольная часть
         здания (отм. 0,000)» / «Надземная часть здания (выше отм. 0,000)»);
      2) applied_constants.building_part — если надземная → «Надземная»,
         иначе «Подземная»;
      3) имя элемента / МССК.
    """
    # 1) path[0]
    path = entry.get("group_path") or []
    if path:
        top = str(path[0]).lower()
        if "подземн" in top:
            return "Подземная"
        if "цокольн" in top:
            return "Цокольная"
        if "надземн" in top:
            return "Надземная"

    # 2) applied_constants.building_part
    part = str(entry.get("part") or "").lower()
    if "надземн" in part:
        return "Надземная"
    if "подземн" in part or "цокольн" in part:
        return "Подземная"

    # 3) фолбэк по имени/МССК
    element = entry.get("element", {}) or {}
    haystack = (
        str(element.get("name") or "").lower()
        + " "
        + str((entry.get("mssk_context") or {}).get("name") or "").lower()
    )
    if "подземн" in haystack:
        return "Подземная"
    if "цокольн" in haystack:
        return "Цокольная"
    if "надземн" in haystack:
        return "Надземная"

    return "Надземная"


# ======================================================================
#  Excel builder
# ======================================================================

def build_final_works_xlsx(result_elements, xlsx_path):
    columns = [
        "Шифр ТСН", "Наименование расценки/ресурса", "Ед. изм.",
        "Объём работ", "ЗП", "ЭМ", "МР", "Стоимость",
        "_is_part_header", "_is_element_header", "_is_part_total",
        "_is_grand_total",
    ]

    def _cost(work, cur_key, base_key, vol):
        unit = safe_float(work.get(cur_key), default=0.0)
        if unit <= 0:
            unit = safe_float(work.get(base_key), default=0.0)
        if unit > 0 and vol > 0:
            return round(unit * vol, 2)
        return None

    def _blank():
        return {col: "" for col in columns[:-4]} | {
            "_is_part_header": False,
            "_is_element_header": False,
            "_is_part_total": False,
            "_is_grand_total": False,
        }

    def _sum_money(rows):
        return {
            col: format_money(sum(_parse_money(r.get(col)) for r in rows))
            for col in ("ЗП", "ЭМ", "МР", "Стоимость")
        }

    element_blocks = []
    for entry in result_elements:
        part = _resolve_building_part(entry)
        header = _element_header(entry)

        work_rows: List[Dict[str, Any]] = []
        for work in entry.get("selected_works") or []:
            q = work.get("quantity") or {}
            total_measure = _build_total_measure(q)
            total_areas = _build_total_areas(q)
            formwork_area = (
                safe_float(q.get("area_m2"), default=0.0)
                if _is_formwork_work(work) else 0.0
            )
            vol_text = _calculate_work_volume(
                work, total_measure,
                total_areas=total_areas,
                formwork_area=formwork_area,
            )
            vol_num = safe_float(vol_text, default=0.0)
            zp = _cost(work, "cur_salary", "salary", vol_num)
            em = _cost(work, "cur_operation_of_machines",
                       "operation_of_machines", vol_num)
            mr = _cost(work, "cur_cost_of_material_resources",
                       "cost_of_material_resources", vol_num)
            parts = [v for v in (zp, em, mr) if v is not None]
            cost = round(sum(parts), 2) if parts else ""

            row = _blank()
            row["Шифр ТСН"] = work.get("pressmark") or ""
            row["Наименование расценки/ресурса"] = work.get("title") or ""
            row["Ед. изм."] = _resolve_unit_label(work)
            row["Объём работ"] = vol_text
            row["ЗП"] = zp if zp is not None else ""
            row["ЭМ"] = em if em is not None else ""
            row["МР"] = mr if mr is not None else ""
            row["Стоимость"] = cost
            work_rows.append(row)

        element_blocks.append({
            "part": part,
            "header": header,
            "rows": work_rows,
        })

    from collections import Counter
    _part_counts = Counter(b["part"] for b in element_blocks)
    logger.info(
        "Разбивка по частям здания: "
        + ", ".join(f"{k}={v}" for k, v in _part_counts.items())
    )

    for block in element_blocks:
        for r in block["rows"]:
            for col in ("ЗП", "ЭМ", "МР", "Стоимость"):
                r[col] = format_money(r[col])

    final_rows: List[Dict[str, Any]] = []
    all_work_rows: List[Dict[str, Any]] = []

    parts_in_order = [
        p for p in BUILDING_PART_ORDER
        if any(b["part"] == p for b in element_blocks)
    ]

    if not parts_in_order:
        for block in element_blocks:
            hdr = _blank()
            hdr["_is_element_header"] = True
            hdr["Наименование расценки/ресурса"] = block["header"]
            final_rows.append(hdr)
            final_rows.extend(block["rows"])
            all_work_rows.extend(block["rows"])
    else:
        for part_pos, part in enumerate(parts_in_order):
            part_hdr = _blank()
            part_hdr["_is_part_header"] = True
            part_hdr["Наименование расценки/ресурса"] = (
                _BUILDING_PART_TITLES.get(part, part)
            )
            final_rows.append(part_hdr)

            part_blocks = [b for b in element_blocks if b["part"] == part]
            part_work_rows: List[Dict[str, Any]] = []

            for block_pos, block in enumerate(part_blocks):
                hdr = _blank()
                hdr["_is_element_header"] = True
                hdr["Наименование расценки/ресурса"] = block["header"]
                final_rows.append(hdr)

                if block["rows"]:
                    final_rows.extend(block["rows"])
                    part_work_rows.extend(block["rows"])
                    all_work_rows.extend(block["rows"])
                else:
                    empty = _blank()
                    empty["Наименование расценки/ресурса"] = "Работы не подобраны"
                    final_rows.append(empty)

                if block_pos < len(part_blocks) - 1:
                    final_rows.append(_blank())

            part_total = _blank()
            part_total["_is_part_total"] = True
            part_total["Наименование расценки/ресурса"] = (
                _BUILDING_PART_TOTAL_LABELS.get(part, f"ИТОГО по части ({part}):")
            )
            for col, value in _sum_money(part_work_rows).items():
                part_total[col] = value
            final_rows.append(part_total)

            if part_pos < len(parts_in_order) - 1:
                final_rows.append(_blank())

        if all_work_rows:
            grand = _blank()
            grand["_is_grand_total"] = True
            grand["Наименование расценки/ресурса"] = "ИТОГО:"
            for col, value in _sum_money(all_work_rows).items():
                grand[col] = value
            final_rows.append(grand)

    if not final_rows:
        empty = _blank()
        empty["Наименование расценки/ресурса"] = "Работы не подобраны"
        final_rows.append(empty)

    df = pd.DataFrame(final_rows)
    for col in columns:
        if col not in df.columns:
            df[col] = ""

    df = df[columns]
    df_for_excel = df.drop(columns=[
        "_is_part_header", "_is_element_header",
        "_is_part_total", "_is_grand_total",
    ])

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df_for_excel.to_excel(writer, sheet_name="Данные", index=False)
        worksheet = writer.sheets["Данные"]

        from openpyxl.styles import Font, PatternFill, Alignment

        part_header_font = Font(bold=True, size=12)
        part_header_fill = PatternFill(
            start_color="A9A9A9", end_color="A9A9A9", fill_type="solid"
        )
        header_font = Font(bold=True, size=11)
        header_fill = PatternFill(
            start_color="D3D3D3", end_color="D3D3D3", fill_type="solid"
        )
        center = Alignment(horizontal="center", vertical="center")
        bold_font = Font(bold=True, size=11)

        for row_idx in range(2, len(df) + 2):
            row_data = df.iloc[row_idx - 2]
            if row_data["_is_part_header"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = part_header_font
                    cell.fill = part_header_fill
                    cell.alignment = center
            elif row_data["_is_element_header"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = center
            elif row_data["_is_part_total"] or row_data["_is_grand_total"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = bold_font
                    cell.alignment = center

        worksheet.column_dimensions["A"].width = 15
        worksheet.column_dimensions["B"].width = 60
        worksheet.column_dimensions["C"].width = 10
        worksheet.column_dimensions["D"].width = 15
        for col in "EFGH":
            worksheet.column_dimensions[col].width = 15
        worksheet.auto_filter.ref = (
            f"A1:{chr(64 + len(df_for_excel.columns))}{len(df_for_excel) + 1}"
        )

    logger.info(f"Excel сохранён: {xlsx_path}")
    return xlsx_path


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Использование: python -m src.services.works_final_selector "
              "<tables.json> <works.json> <run_dir>")
        sys.exit(1)
    _t, _w, _d = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(_d, exist_ok=True)
    _r = select_final_works(_t, _w, _d)
    print(f"OK: {_r}" if _r else "Не выполнено")