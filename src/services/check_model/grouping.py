"""Группировка проверенных элементов ЦИМ по структуре типового ВОР.

Функционал «Проверка ЦИМ модели» сохраняет проверенные элементы в
``Проверка_ЦИМ_модели.json``. Для веб-интерфейса нужен предпросмотр,
сгруппированный не по МССК (как в режимах ЦС/ИИ), а по иерархии типового ВОР
(``data/Типовой ВОР по КР.xlsx``):

    часть здания → раздел → подраздел → наименование элемента

Правила сопоставления элемента с подразделом:

* **часть здания** — по тексту этажа/наименования (``Автостоянка``/``паркинг``)
  либо по типу этажа и отметке (``zero_step.classify_storey_type``);
* **вид конструкции** — в первую очередь по коду МССК элемента
  (``RUS_MSSK_Element_Code``) через справочник
  ``data/elements_mssk_nested.json`` (например, ``ЭЛ 30 24 20`` — лестничная
  площадка, ``ЭЛ 30 10 80`` — плита перекрытия); если код не найден —
  по IFC-классу, ``Элемент_ЦИМ`` и тексту наименования (стена, плита, колонна,
  балка, лестница, свая, фундаментная плита, гидроизоляция, деформационный шов,
  засыпка и т. п.). Приямок (текст «приям» в наименовании/типе/материале)
  всегда относится к подразделу «Фундаментная плита» независимо от IFC-класса
  и кода МССК: в моделях он выгружается как ``IfcSlab``/«Перекрытие» с кодом
  МССК фундаментного блока;
* **технология** — по ``Pset_ConcreteElementGeneral.ConstructionMethod`` и
  тексту (``construction_method``): сборные стены/лестницы/парапеты попадают в
  раздел «Сборные ж/б конструкции», остальные — в монолитные разделы.

Пустые группы в результат не попадают. Элементы, для которых подраздел
типового ВОР не определён (например, проёмы ``IfcOpeningElement``), собираются
в отдельный блок «Прочие элементы (вне разделов типового ВОР)», чтобы превью
не теряло данные.

Площадь элемента в превью — это **площадь опалубки** (``formwork_area``,
см. ``check_model/formwork.py``): она рассчитывается только для монолитных
железобетонных конструкций по правилам вида конструкции (стены — четыре
боковые поверхности: две лицевые нетто и два торца по толщине,
колонны — четыре стороны, фундаменты/приямки — периметр × толщина, перекрытия
и площадки — горизонтальная площадь с одной стороны, марши — проекция на пол,
балки — низ и две длинные боковые грани). У сборного ж/б и неконструктивных
элементов площади опалубки нет.

Результат кешируется в каталоге сессии (``Проверка_ЦИМ_модели_группы.json``) и
пересчитывается, когда исходный ``Проверка_ЦИМ_модели.json`` новее кеша.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core.logger import setup_logger
from src.services.construction_method import construction_method
from src.services.zero_step import classify_storey_type

from .formwork import formwork_area

logger = setup_logger(__name__)

RESULT_JSON = "Проверка_ЦИМ_модели.json"
GROUPS_JSON = "Проверка_ЦИМ_модели_группы.json"

# Версия структуры кеша превью: при изменении правил группировки/расчёта
# площадей кеш пересчитывается, даже если исходный JSON не менялся.
GROUPS_VERSION = 4

# Справочник МССК-элементов: соответствие кода МССК виду конструкции.
MSSK_ELEMENTS_JSON = Path(__file__).resolve().parents[3] / "data" / "elements_mssk_nested.json"


@dataclass(frozen=True)
class VorNode:
    """Узел структуры типового ВОР."""

    level: str  # part | section | block | subsection
    key: str
    title: str


# Тексты заголовков — как в типовом шаблоне ``data/Типовой ВОР по КР.xlsx``
# (лист «ВОР КР_ЖС»). Уровни: part → section → (block) → subsection.
VOR_STRUCTURE: Tuple[VorNode, ...] = (
    VorNode("part", "underground", "Подземная часть здания (до отм. 0,000)"),
    VorNode("section", "ug_foundations", "Раздел 1. Монолитные ж/б конструкции. Фундаменты"),
    VorNode("subsection", "ug:pile_driven", "Подраздел 1.1. Сваи забивные железобетонные"),
    VorNode("subsection", "ug:pile_bored", "Подраздел 1.2. Сваи буронабивные"),
    VorNode("subsection", "ug:foundation_slab", "Подраздел 2. Фундаментная плита"),
    VorNode("subsection", "ug:equipment_foundation", "Подраздел 3. Фундамент под инженерное оборудование"),
    VorNode("subsection", "ug:hydro_horizontal", "Подраздел 4. Устройство горизонтальной гидроизоляции"),
    VorNode("subsection", "ug:hydro_vertical", "Подраздел 5. Устройство вертикальной гидроизоляции"),
    VorNode("subsection", "ug:seam_foundation", "Подраздел 6. Устройство деформационного шва"),
    VorNode("section", "ug_building", "Раздел 2. Монолитные ж/б конструкции. Подземная часть здания"),
    VorNode("subsection", "ug:wall", "Подраздел 1. Подземная часть здания. Стены"),
    VorNode("subsection", "ug:column", "Подраздел 2. Подземная часть здания. Колонны, пилоны"),
    VorNode("subsection", "ug:slab", "Подраздел 3. Подземная часть здания. Плиты перекрытия"),
    VorNode("subsection", "ug:beam", "Подраздел 4. Подземная часть здания. Балки"),
    VorNode("subsection", "ug:stair", "Подраздел 5. Подземная часть здания. Лестницы"),
    VorNode("subsection", "ug:insulation", "Подраздел 6. Подземная часть здания. Вертикальная гидроизоляция, теплоизоляция"),
    VorNode("subsection", "ug:seam_building", "Подраздел 7. Подземная часть здания. Устройство деформационного шва"),
    VorNode("subsection", "ug:backfill", "Подраздел 8. Подземная часть здания. Засыпка песком"),
    VorNode("part", "above", "Надземная часть здания (выше отм. 0,000)"),
    VorNode("section", "ag_monolithic", "Раздел 3. Монолитные ж/б конструкции. Надземная часть здания"),
    VorNode("subsection", "ag:wall", "Подраздел 1. Надземная часть здания. Стены"),
    VorNode("subsection", "ag:column", "Подраздел 2. Надземная часть здания. Колонны, пилоны"),
    VorNode("subsection", "ag:slab", "Подраздел 3. Надземная часть здания. Плиты перекрытия"),
    VorNode("subsection", "ag:beam", "Подраздел 4. Надземная часть здания. Балки"),
    VorNode("subsection", "ag:stair", "Подраздел 5. Надземная часть здания. Лестницы"),
    VorNode("subsection", "ag:parapet", "Подраздел 6. Надземная часть здания. Парапеты"),
    VorNode("subsection", "ag:equipment_foundation_roof", "Подраздел 7. Надземная часть здания. Фундаменты под оборудование на кровле"),
    VorNode("section", "ag_precast", "Раздел 4. Сборные ж/б конструкции. Надземная часть здания"),
    VorNode("subsection", "ag:precast_panel", "Подраздел 1. Надземная часть здания. Сборные стеновые панели"),
    VorNode("subsection", "ag:precast_stair", "Подраздел 2. Надземная часть здания. Лестницы сборные"),
    VorNode("subsection", "ag:precast_parapet", "Подраздел 3. Надземная часть здания. Сборные парапеты"),
    VorNode("subsection", "ag:precast_shaft", "Подраздел 4. Надземная часть здания. Сборные шахты лифтов"),
    VorNode("subsection", "ag:precast_bathroom", "Подраздел 5. Надземная часть здания. Устройство сантехкабин"),
    VorNode("subsection", "ag:precast_vent", "Подраздел 6. Надземная часть здания. Сборные вентблоки"),
    VorNode("subsection", "ag:precast_engineering", "Подраздел 7. Надземная часть здания. Инженерные блоки"),
    VorNode("subsection", "ag:precast_joints", "Подраздел 8. Надземная часть здания. Герметизация стыков сборных конструкций"),
    VorNode("part", "parking", "Автостоянка"),
    VorNode("section", "parking_section", "Раздел 5. Монолитные ж/б конструкции. Автостоянка"),
    VorNode("block", "parking_ug", "Подземная часть здания (до отм. 0,000)"),
    VorNode("subsection", "parking_ug:wall", "Подраздел 1. Автостоянка. Стены"),
    VorNode("subsection", "parking_ug:column", "Подраздел 2. Автостоянка. Колонны, пилоны"),
    VorNode("subsection", "parking_ug:slab", "Подраздел 3. Автостоянка. Плиты перекрытия"),
    VorNode("subsection", "parking_ug:beam", "Подраздел 4. Автостоянка. Балки"),
    VorNode("subsection", "parking_ug:stair", "Подраздел 5. Автостоянка. Лестницы"),
    VorNode("subsection", "parking_ug:backfill", "Подраздел 6. Автостоянка. Засыпка песком"),
    VorNode("subsection", "parking_ug:seam", "Подраздел 7. Автостоянка. Устройство деформационного шва"),
    VorNode("block", "parking_ag", "Надземная часть здания (выше отм. 0,000)"),
    VorNode("subsection", "parking_ag:slab", "Подраздел 1. Автостоянка. Плиты перекрытия"),
)

# Блок для элементов, которым не нашлось места в разделах типового ВОР
# (например, проёмы IfcOpeningElement).
UNASSIGNED_PART_KEY = "unassigned"
UNASSIGNED_PART_TITLE = "Прочие элементы (вне разделов типового ВОР)"
UNASSIGNED_SECTION_KEY = "unassigned_section"
UNASSIGNED_SECTION_TITLE = "Не распределено по разделам типового ВОР"

_PARKING_RE = re.compile(r"автостоянк|паркинг|\bстоянк", re.IGNORECASE)
_PILE_BORED_RE = re.compile(r"буронабив|буров|набивн", re.IGNORECASE)
_LANDING_RE = re.compile(r"\blanding\b", re.IGNORECASE)
_OPENING_CLASSES = {"IfcOpeningElement", "IfcOpeningStandardCase"}
_WALL_CLASSES = {"IfcWall", "IfcWallStandardCase", "IfcWallElementedCase"}
_SLAB_CLASSES = {"IfcSlab", "IfcSlabStandardCase", "IfcSlabElementedCase"}
_STAIR_CLASSES = {"IfcStair", "IfcStairFlight", "IfcStairStandardCase"}
_PROXY_CLASSES = {
    "IfcBuildingElementProxy",
    "IfcBuildingElementPart",
    "IfcMember",
    "IfcPlate",
}

_EMPTY = ("", "-", "nan", "none", "nat", "null", "не указано")

# Виды конструкций, которые всегда относятся к подземной части независимо от
# отметки этажа (фундаменты, сваи, изоляция фундаментов).
_ALWAYS_UNDERGROUND = {
    "pile_driven",
    "pile_bored",
    "foundation_slab",
    "equipment_foundation",
    "hydro_horizontal",
    "hydro_vertical",
    "seam_foundation",
}


def _text(value: Any) -> str:
    """Строковое значение ячейки без «пустых» заглушек."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _EMPTY else text


def _num(value: Any) -> Optional[float]:
    """Число из значения ячейки (пустое/нечисловое — ``None``)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text or text.lower() in _EMPTY:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _row_text(row: Dict[str, Any]) -> str:
    """Текст элемента для эвристик: наименования, тип, материал."""
    keys = (
        "Name",
        "ObjectType",
        "Description",
        "RUS_Name",
        "RUS_MSSK_Element_Name",
        "Элемент_ЦИМ",
        "Материал",
    )
    return " ".join(_text(row.get(key)) for key in keys).lower()


# Правила «наименование подкласса МССК → вид конструкции». Порядок важен:
# более специфичные правила идут раньше (площадка — до перекрытия, свая — до
# фундамента, парапет — до стены).
_MSSK_TITLE_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"свая[^.]*(бур|набивн)"), "pile_bored"),
    (re.compile(r"\bсвая|\bшпунт"), "pile_driven"),
    (
        re.compile(
            r"фундаментная плита|ростверк плитный|подливка фундамента|"
            r"бетонная подготовка|отмостка|фундаментный блок|кесонный фундамент|"
            r"фундамент (ленточный|столбчатый)|фундаментная балка|"
            r"стакан опорный|плита перекрытия фундамента|\bприямок"
        ),
        "foundation_slab",
    ),
    (re.compile(r"лестничн\w* площадк|горизонтальная площадка (пандуса|рампы)"), "stair"),
    (
        re.compile(
            r"лестничн\w* марш|\bлестница|лестница-трап|\bкосоур|"
            r"пандус|\bрампа|подъем (пандуса|рампы)"
        ),
        "stair",
    ),
    (
        re.compile(
            r"плита перекрытия|\bперекрытие|плита (балкона|лоджии|козырька)|"
            r"ортотропная плита"
        ),
        "slab",
    ),
    (re.compile(r"парапет"), "parapet"),
    (re.compile(r"термовкладыш"), "thermoliner"),
    (re.compile(r"деформационный шов|шов стыка"), "seam"),
    (re.compile(r"гидроизоляц|теплоизоляц|пароизоляц|\bизоляция"), "insulation"),
    (re.compile(r"засыпка|\bнасыпь|грунтовая подушка"), "backfill"),
    (re.compile(r"колонн|пилон|пилястр|капитель|база колонны|фахверк"), "column"),
    (
        re.compile(
            r"балка|ригель|перемычка|\bпояс\b|подкрановая|\bрельс|монорельс|"
            r"оголовок опоры|подферменник"
        ),
        "beam",
    ),
    (re.compile(r"стеновая панель|блок угловой стеновых панелей"), "precast_panel"),
    (re.compile(r"шахт\w* лифт"), "precast_shaft"),
    (re.compile(r"вентиляционный блок|вентблок"), "precast_vent"),
    (re.compile(r"инженерных коммуникаций"), "precast_engineering"),
    (re.compile(r"сантехкабин"), "precast_bathroom"),
    (re.compile(r"\bстена|\bперегородка|\bстенка|открылок"), "wall"),
)


def _kind_from_mssk_title(title: str) -> Optional[str]:
    """Вид конструкции по наименованию подкласса МССК."""
    text = title.strip().lower()
    if not text:
        return None
    for pattern, kind in _MSSK_TITLE_RULES:
        if pattern.search(text):
            return kind
    return None


@lru_cache(maxsize=1)
def _mssk_kinds() -> Dict[str, str]:
    """Карта «код МССК → вид конструкции» по ``data/elements_mssk_nested.json``.

    Справочник только читается. При недоступности файла возвращается пустая
    карта — классификация продолжится текстовыми эвристиками.
    """
    try:
        with open(MSSK_ELEMENTS_JSON, "r", encoding="utf-8") as stream:
            categories = json.load(stream)
    except (OSError, ValueError):
        logger.warning(
            "Не удалось загрузить справочник МССК-элементов %s",
            MSSK_ELEMENTS_JSON,
            exc_info=True,
        )
        return {}

    if not isinstance(categories, list):
        return {}

    kinds: Dict[str, str] = {}
    for category in categories:
        if not isinstance(category, dict):
            continue
        for purpose in category.get("purposes") or []:
            if not isinstance(purpose, dict):
                continue
            for cls in purpose.get("classes") or []:
                if not isinstance(cls, dict):
                    continue
                nodes = cls.get("subclasses") or [cls]
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    code = _text(node.get("code") or node.get("code_class"))
                    title = _text(node.get("subclass_RU") or node.get("class_RU"))
                    kind = _kind_from_mssk_title(title)
                    if code and kind:
                        kinds[code] = kind
    logger.info("Справочник МССК-элементов: видов конструкций %d", len(kinds))
    return kinds


def _elevation_m(row: Dict[str, Any]) -> Optional[float]:
    """Отметка этажа в метрах."""
    value = _num(row.get("Отметка_этажа_м"))
    if value is not None:
        return value
    value = _num(row.get("Отметка_этажа"))
    return value / 1000.0 if value is not None else None


def _storey_type(row: Dict[str, Any]) -> str:
    """Тип этажа по значению «Этаж» и отметке (как в пайплайне ЦС/ИИ)."""
    storey = _text(row.get("Этаж")) or _text(row.get("RUS_Level"))
    elevation = row.get("Отметка_этажа")
    try:
        elevation_value = float(str(elevation).strip().replace(",", "."))
    except (TypeError, ValueError):
        elevation_value = "-"
    return classify_storey_type(storey, elevation_value)


def _is_first_floor(row: Dict[str, Any]) -> bool:
    """Первый этаж (числовой индикатор значения «Этаж» = 1)."""
    storey = _text(row.get("Этаж")) or _text(row.get("RUS_Level"))
    for segment in re.split(r"[_\s]+", storey.lower()):
        segment = segment.strip()
        if re.fullmatch(r"\d+", segment):
            return int(segment) == 1
    return False


def _part_and_block(row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Часть здания и блок «подземная/надземная» (для автостоянки)."""
    if _PARKING_RE.search(_row_text(row)):
        block = "underground" if _storey_type(row) in ("Подземный", "Цокольный") else "above"
        return "parking", block

    storey_type = _storey_type(row)
    if storey_type in ("Подземный", "Цокольный"):
        return "underground", None
    if storey_type == "Надземный":
        return "above", None

    elevation = _elevation_m(row)
    if elevation is not None and elevation < 0:
        return "underground", None
    return "above", None


def _element_kind(row: Dict[str, Any]) -> Optional[str]:
    """Вид конструкции элемента в терминах подразделов типового ВОР."""
    ifc_class = _text(row.get("IfcClass"))
    element = _text(row.get("Элемент_ЦИМ")).lower()
    text = _row_text(row)
    predefined = _text(row.get("PredefinedType")).upper()

    if ifc_class in _OPENING_CLASSES or "проем" in element:
        return "opening"

    # Приямок — монолитная конструкция подземной части: в типовом ВОР его
    # объёмы и опалубка относятся к подразделу «Фундаментная плита» независимо
    # от IFC-класса и кода МССК (в моделях приямок выгружается как
    # IfcSlab/«Перекрытие» с кодом МССК фундаментного блока, поэтому без этого
    # правила он попадал в подраздел плит перекрытия).
    if "приям" in text:
        return "foundation_slab"

    # Приоритет — справочник МССК: код элемента точнее текстовых эвристик
    # (например, «Лестничная площадка» приходит как IfcSlab/«Перекрытие»).
    mssk_code = _text(row.get("RUS_MSSK_Element_Code"))
    mssk_kind = _mssk_kinds().get(mssk_code) if mssk_code else None
    if mssk_kind is None:
        mssk_kind = _kind_from_mssk_title(_text(row.get("RUS_MSSK_Element_Name")))
    if mssk_kind is not None:
        # Фундамент под инженерное оборудование — отдельный подраздел ВОР,
        # в справочнике МССК он не выделен.
        if ifc_class == "IfcFooting" and "оборудован" in text:
            return "equipment_foundation"
        return mssk_kind

    if ifc_class == "IfcPile" or "свая" in element:
        return "pile_bored" if _PILE_BORED_RE.search(text) else "pile_driven"

    if ifc_class == "IfcFooting":
        return "equipment_foundation" if "оборудован" in text else "foundation_slab"

    if ifc_class in _SLAB_CLASSES:
        if "фундаментная плита" in element or "фундамент" in text or "рамп" in text:
            return "foundation_slab"
        if predefined in ("BASESLAB", "FOUNDATION"):
            return "foundation_slab"
        # Лестничная площадка в модели — плита с признаком Landing в наименовании.
        if _LANDING_RE.search(_text(row.get("Name"))) or ("площадк" in text and "лестниц" in text):
            return "stair"
        return "slab"

    if ifc_class in _WALL_CLASSES:
        if re.search(r"\bпилон|\bколонн", text):
            return "column"
        if "парапет" in text:
            return "parapet"
        return "wall"

    if ifc_class in ("IfcColumn", "IfcColumnStandardCase"):
        return "column"

    if ifc_class == "IfcBeam":
        return "beam"

    if ifc_class in _STAIR_CLASSES:
        return "stair"

    if ifc_class == "IfcCovering":
        if "термовкладыш" in text:
            return "thermoliner"
        if re.search(r"гидроизоляц|теплоизоляц|\bизоляц", text):
            return "insulation"
        return None

    if ifc_class == "IfcRamp":
        return "foundation_slab"

    if ifc_class in _PROXY_CLASSES:
        # Сборные изделия: в типовом ВОР у каждого свой подраздел.
        if re.search(r"сборн\w*[^.;]*панел|панел\w*[^.;]*сборн", text):
            return "precast_panel"
        if re.search(r"сборн\w*[^.;]*шахт|шахт\w*[^.;]*сборн", text):
            return "precast_shaft"
        if "вентблок" in text:
            return "precast_vent"
        if "сантехкабин" in text:
            return "precast_bathroom"
        if re.search(r"инженерн\w*\s+блок", text):
            return "precast_engineering"
        if re.search(r"герметизац\w*[^.;]*стык", text):
            return "precast_joints"
        if re.search(r"лестниц|лестничн|\bмарш", text):
            return "stair"
        if "парапет" in text:
            return "parapet"
        if re.search(r"\bбалк", text):
            return "beam"
        if re.search(r"плит\w*\s+(перекрыти|покрыти)|\bперекрыти", text):
            return "slab"
        if re.search(r"\bстен", text):
            return "wall"
        if re.search(r"колонн|пилон", text):
            return "column"
        if re.search(r"\bсва[йияе]", text):
            return "pile_driven"
        if "фундамент" in text:
            return "foundation_slab"
        if re.search(r"гидроизоляц|теплоизоляц|\bизоляц", text):
            return "insulation"
        if "засыпк" in text:
            return "backfill"
        if re.search(r"деформационн\w*\s+шв", text):
            return "seam"
        return None

    return None


def _apply_technology(kind: str, part: str, row: Dict[str, Any]) -> str:
    """Учесть технологию изготовления (сборные/монолитные)."""
    method = construction_method(row)
    if method == "precast":
        if kind == "wall":
            return "precast_panel"
        if kind == "stair":
            return "precast_stair"
        if kind == "parapet":
            return "precast_parapet"
        return kind
    if method == "monolithic":
        # Явный монолитный признак снимает «сборную» технологию, полученную
        # только из текста наименования.
        return {"precast_stair": "stair", "precast_panel": "wall",
                "precast_parapet": "parapet"}.get(kind, kind)
    return kind


def _subsection_key(
    row: Dict[str, Any],
    *,
    has_underground: bool = False,
    kind: Optional[str] = None,
) -> Optional[str]:
    """Ключ подраздела типового ВОР для элемента (``None`` — не распределён)."""
    part, block = _part_and_block(row)
    if kind is None:
        kind = _element_kind(row)
    if kind is None:
        return None

    # Проёмы и прочие неконструктивные элементы в разделах ВОР не участвуют.
    if kind == "opening":
        return None

    kind = _apply_technology(kind, part, row)
    text = _row_text(row)

    if kind == "thermoliner":
        # Термовкладыши относятся к плитам перекрытий соответствующей части.
        kind = "slab"

    # Плита перекрытия первого этажа — перекрытие над подвалом (потолок
    # подземной части). Как в типовом ВОР, при наличии подземной части она
    # относится к подразделу плит подземной части.
    if (
        kind == "slab"
        and part == "above"
        and has_underground
        and _is_first_floor(row)
    ):
        part = "underground"

    if part == "parking":
        if kind == "seam_foundation":
            kind = "seam"
        if block == "underground":
            if kind in ("wall", "column", "slab", "beam", "stair", "backfill", "seam"):
                return f"parking_ug:{kind}"
            return None
        if kind == "slab":
            return "parking_ag:slab"
        return None

    # Фундаменты, сваи и изоляция фундаментов — всегда подземная часть.
    if kind in _ALWAYS_UNDERGROUND:
        part = "underground"

    if kind == "insulation":
        if part != "underground":
            return None
        if "фундамент" in text:
            return "ug:hydro_horizontal" if "горизонт" in text else "ug:hydro_vertical"
        return "ug:insulation"

    if kind == "seam":
        if part != "underground":
            return None
        return "ug:seam_foundation" if "фундамент" in text else "ug:seam_building"

    if part == "underground":
        if kind in (
            "pile_driven",
            "pile_bored",
            "foundation_slab",
            "equipment_foundation",
            "hydro_horizontal",
            "hydro_vertical",
            "seam_foundation",
            "wall",
            "column",
            "slab",
            "beam",
            "stair",
            "backfill",
        ):
            return f"ug:{kind}"
        if kind == "parapet":
            # Парапет — конструкция надземной части; в подземной части
            # подраздела нет.
            return None
        return None

    # Надземная часть.
    if kind == "equipment_foundation":
        return "ag:equipment_foundation_roof"
    if kind in ("wall", "column", "slab", "beam", "stair", "parapet"):
        return f"ag:{kind}"
    if kind in (
        "precast_panel",
        "precast_stair",
        "precast_parapet",
        "precast_shaft",
        "precast_bathroom",
        "precast_vent",
        "precast_engineering",
        "precast_joints",
    ):
        return f"ag:{kind}"
    return None


def _element_name(row: Dict[str, Any]) -> str:
    """Наименование элемента для последнего уровня группировки.

    Как в превью ЦС/ИИ: используется ``Name``, у которого отрезаются
    завершающие числовые хвосты ``:<число>`` (технический идентификатор
    экземпляра Revit; у проёмов их может быть несколько — ``:1154616:1``).
    """
    value = ""
    for key in ("Name", "RUS_Name", "RUS_MSSK_Element_Name", "ObjectType"):
        value = _text(row.get(key))
        if value:
            break
    if not value:
        return "Без названия"

    # У маршей и лестничных площадок Name — техническое имя экземпляра
    # («Монолитная лестница:Лестница:1659390 Run 1»). Для группировки берём
    # тип конструкции из ObjectType, иначе каждая ступень стала бы отдельной
    # группой.
    if re.search(r"\b(Run|Landing)\b", value, re.IGNORECASE):
        object_type = _text(row.get("ObjectType"))
        if object_type:
            value = object_type

    match = re.match(r"^(.*?)(?::\s*\d+)+$", value)
    if match and match.group(1).strip():
        value = match.group(1).strip()
    return value


def _element_payload(row: Dict[str, Any], kind: Optional[str] = None) -> Dict[str, Any]:
    """Компактное описание элемента для листовой строки превью.

    ``area`` — площадь опалубки (м², только для монолитного ж/б; у сборных и
    неконструктивных элементов — ``None``), ``areaBasis`` — правило расчёта,
    ``elementArea`` — площадь самого элемента (``RUS_Area``) для справки.
    """
    volume = _num(row.get("RUS_Volume"))
    if volume is None:
        volume = _num(row.get("Количество::Объём_м3"))
    if volume is None:
        volume = _num(row.get("RUS_GrossVolume"))
    element_area = _num(row.get("RUS_Area"))
    formwork, basis = formwork_area(row, kind)

    return {
        "name": _text(row.get("Name")),
        "ifcClass": _text(row.get("IfcClass")),
        "storey": _text(row.get("RUS_Level")) or _text(row.get("Этаж")),
        "mssk": _text(row.get("RUS_MSSK_Element_Code")),
        "element": _text(row.get("Элемент_ЦИМ")),
        "requirement": _text(row.get("Код_требований_ЦИМ")),
        "volume": round(volume, 4) if volume is not None else None,
        "area": formwork,
        "areaBasis": basis,
        "elementArea": round(element_area, 4) if element_area is not None else None,
    }


@dataclass
class _GroupNode:
    """Узел дерева группировки."""

    level: str
    key: str
    title: str
    children: List["_GroupNode"] = field(default_factory=list)
    elements: List[Dict[str, Any]] = field(default_factory=list)
    count: int = 0
    volume: float = 0.0
    area: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "level": self.level,
            "key": self.key,
            "title": self.title,
            "count": self.count,
            "volume": round(self.volume, 4),
            "area": round(self.area, 4),
        }
        if self.children:
            payload["children"] = [child.to_dict() for child in self.children]
        if self.elements:
            payload["elements"] = self.elements
        return payload


def _build_structure() -> Tuple[List[_GroupNode], Dict[str, _GroupNode]]:
    """Построить пустое дерево по структуре типового ВОР."""
    roots: List[_GroupNode] = []
    by_key: Dict[str, _GroupNode] = {}
    stack: List[_GroupNode] = []
    depth_by_level = {"part": 0, "section": 1, "block": 2}

    for node in VOR_STRUCTURE:
        group = _GroupNode(level=node.level, key=node.key, title=node.title)
        by_key[node.key] = group

        if node.level == "subsection":
            # В разделе автостоянки между разделом и подразделом стоит блок
            # «Подземная/Надземная часть» — тогда подраздел на уровень глубже.
            has_block = any(parent.level == "block" for parent in stack[1:])
            depth = 3 if has_block else 2
        else:
            depth = depth_by_level[node.level]

        stack = stack[:depth]
        if stack:
            stack[-1].children.append(group)
        else:
            roots.append(group)
        stack.append(group)

    return roots, by_key


def _aggregate(node: _GroupNode) -> None:
    """Пересчитать количество и суммы объёмов/площадей по поддереву."""
    count = len(node.elements)
    volume = sum(float(element.get("volume") or 0.0) for element in node.elements)
    area = sum(float(element.get("area") or 0.0) for element in node.elements)
    for child in node.children:
        _aggregate(child)
        count += child.count
        volume += child.volume
        area += child.area
    node.count = count
    node.volume = volume
    node.area = area


def _prune_empty(node: _GroupNode) -> bool:
    """Удалить пустые поддеревья; вернуть ``True``, если узел остался."""
    node.children = [child for child in node.children if _prune_empty(child)]
    return bool(node.children or node.elements)


def _add_element(
    node: _GroupNode,
    row: Dict[str, Any],
    kind: Optional[str] = None,
) -> None:
    """Добавить элемент в подраздел с группировкой по наименованию."""
    name = _element_name(row)
    name_key = f"name:{node.key}:{name}"
    for child in node.children:
        if child.level == "name" and child.key == name_key:
            child.elements.append(_element_payload(row, kind))
            return
    group = _GroupNode(level="name", key=name_key, title=name)
    group.elements.append(_element_payload(row, kind))
    node.children.append(group)


def _unassigned_tree(rows: Iterable[Dict[str, Any]]) -> Optional[_GroupNode]:
    """Дерево для элементов, не попавших в разделы типового ВОР."""
    part = _GroupNode(level="part", key=UNASSIGNED_PART_KEY, title=UNASSIGNED_PART_TITLE)
    section = _GroupNode(
        level="section", key=UNASSIGNED_SECTION_KEY, title=UNASSIGNED_SECTION_TITLE
    )
    part.children.append(section)

    by_type: Dict[str, _GroupNode] = {}
    for row in rows:
        title = (
            _text(row.get("Элемент_ЦИМ"))
            or _text(row.get("IfcClass"))
            or "Без типа"
        )
        subsection = by_type.get(title)
        if subsection is None:
            subsection = _GroupNode(
                level="subsection", key=f"{UNASSIGNED_SECTION_KEY}:{title}", title=title
            )
            by_type[title] = subsection
            section.children.append(subsection)
        _add_element(subsection, row, _element_kind(row))

    return part if _prune_empty(part) else None


def build_grouped_preview(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Построить сгруппированное превью проверенных элементов.

    Args:
        rows: строки ``Проверка_ЦИМ_модели.json``.

    Returns:
        Словарь с деревом групп, счётчиками и перечнем нераспределённых
        элементов. Пустые группы в ``groups`` не включаются.
    """
    roots, by_key = _build_structure()
    has_underground = any(
        _part_and_block(row)[0] == "underground" for row in rows if isinstance(row, dict)
    )

    unassigned: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = _element_kind(row)
        key = _subsection_key(row, has_underground=has_underground, kind=kind)
        node = by_key.get(key) if key else None
        if node is None:
            unassigned.append(row)
            continue
        _add_element(node, row, kind)

    for root in roots:
        _aggregate(root)
        _prune_empty(root)
    roots = [root for root in roots if root.count > 0]
    assigned = sum(root.count for root in roots)

    extra = _unassigned_tree(unassigned)
    if extra is not None:
        _aggregate(extra)
        roots.append(extra)

    payload = {
        "version": GROUPS_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalElements": len(rows),
        "groupedElements": assigned,
        "ungroupedElements": len(unassigned),
        "totalFormworkArea": round(sum(root.area for root in roots), 4),
        "groups": [root.to_dict() for root in roots],
    }
    logger.info(
        "Превью по типовому ВОР: элементов %d, распределено %d, вне разделов %d",
        len(rows),
        assigned,
        len(unassigned),
    )
    return payload


def _write_json(path: Path, payload: Any) -> None:
    """Атомарная запись JSON."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.replace(temporary, path)


def load_grouped_preview(session_dir: str, *, refresh: bool = False) -> Dict[str, Any]:
    """Загрузить (при необходимости — построить) сгруппированное превью.

    Кеш ``Проверка_ЦИМ_модели_группы.json`` пересчитывается, если исходный
    ``Проверка_ЦИМ_модели.json`` новее кеша или запрошено ``refresh=True``.
    """
    session_path = Path(session_dir)
    result_path = session_path / RESULT_JSON
    cache_path = session_path / GROUPS_JSON

    if not result_path.is_file():
        raise FileNotFoundError(
            f"Не найден результат проверки ЦИМ модели: {RESULT_JSON}"
        )

    if not refresh and cache_path.is_file():
        try:
            if cache_path.stat().st_mtime >= result_path.stat().st_mtime:
                with open(cache_path, "r", encoding="utf-8") as stream:
                    cached = json.load(stream)
                # Кеш прежней версии структуры/правил расчёта пересчитывается.
                if isinstance(cached, dict) and cached.get("version") == GROUPS_VERSION:
                    return cached
        except (OSError, ValueError):
            logger.warning("Не удалось прочитать кеш групп, пересчитываю", exc_info=True)

    with open(result_path, "r", encoding="utf-8") as stream:
        rows = json.load(stream)
    if not isinstance(rows, list):
        raise ValueError(f"Файл {RESULT_JSON} должен содержать массив объектов")

    payload = build_grouped_preview(rows)
    _write_json(cache_path, payload)
    return payload
