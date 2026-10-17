"""Индексация элементов IFC из «сырого» дампа и агрегация объёмов.

Источник данных — ``IFC_исходные_параметры.json`` (сырой дамп всех элементов
IFC, формируется :mod:`src.services.ifc_raw_dump`). Каждый элемент содержит
прямые атрибуты, свойства (``Свойство::…``), количественные характеристики
(``QTO::…``), материал и этаж.

Модуль приводит элементы к нормализованному виду (:class:`Element`),
определяет тип конструкции (в терминах типового ВОР), часть здания
(подземная/надземная) и агрегирует количественные характеристики по группам
элементов — для заполнения столбца «Объем» типового ВОР.
"""

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# «Пустые» значения ячеек сырого дампа.
_EMPTY = ("", "-", "nan", "none", "nat", "null", "не указано")

# Ключи QTO по видам количественных характеристик (значения — в СИ:
# объёмы м3, площади м2, длины мм).
_QUANTITY_KEYS = {
    "volume": (
        "QTO::Qto_WallBaseQuantities::NetVolume",
        "QTO::Qto_SlabBaseQuantities::NetVolume",
        "QTO::Qto_BeamBaseQuantities::NetVolume",
        "QTO::Qto_StairFlightBaseQuantities::NetVolume",
    ),
    "gross_volume": (
        "QTO::Qto_WallBaseQuantities::GrossVolume",
        "QTO::Qto_SlabBaseQuantities::GrossVolume",
        "QTO::Qto_BeamBaseQuantities::GrossVolume",
        "QTO::Qto_StairFlightBaseQuantities::GrossVolume",
    ),
    "area": (
        "QTO::Qto_SlabBaseQuantities::NetArea",
        "QTO::Qto_SlabBaseQuantities::GrossArea",
        "QTO::Qto_CoveringBaseQuantities::NetArea",
        "QTO::Qto_CoveringBaseQuantities::GrossArea",
    ),
    "side_area": (
        "QTO::Qto_WallBaseQuantities::NetSideArea",
        "QTO::Qto_WallBaseQuantities::GrossSideArea",
    ),
    # Высота стены (мм) — используется для отделения парапетов (низкие стены
    # кровли) от стен кровельных технических помещений.
    "height": (
        "QTO::Qto_WallBaseQuantities::Height",
    ),
    "surface_area": (
        "QTO::Qto_BeamBaseQuantities::NetSurfaceArea",
        "QTO::Qto_BeamBaseQuantities::GrossSurfaceArea",
        "QTO::Qto_BeamBaseQuantities::OuterSurfaceArea",
    ),
    "length": (
        "QTO::Qto_BeamBaseQuantities::Length",
        "QTO::Qto_WallBaseQuantities::Length",
        "QTO::Qto_StairFlightBaseQuantities::Length",
    ),
    # Периметр контура плиты (мм) — для расчёта опалубки фундаментных плит
    # (площадь только с торцов: периметр × толщина). Если QTO-периметра нет,
    # используется bbox-оценка, добавляемая в сырой дамп менеджером задачи
    # формирования ВОР (колонка ``QTO_bbox::Периметр_мм``).
    "perimeter": (
        "QTO::Qto_SlabBaseQuantities::Perimeter",
        "QTO_bbox::Периметр_мм",
    ),
}

# Геометрические параметры по видам (для группировки по «…толщиной/сечением»).
_GEOMETRY_KEYS = {
    "thickness": (
        "QTO::Qto_WallBaseQuantities::Width",
        "QTO::Qto_SlabBaseQuantities::Depth",
        # bbox-оценка толщины (добавляется в сырой дамп для плит с неполным QTO).
        "QTO_bbox::Толщина_мм",
    ),
}

_SECTION_NAME_RE = re.compile(r"(\d+)\s*[xх×*]\s*(\d+)")
# Толщина в наименовании группы элементов («ADSK_Бетон В30_200 мм», «…1400мм»).
_GROUP_THICKNESS_RE = re.compile(r"(\d{2,4})\s*мм")

# Типы конструкций, для которых толщина берётся из наименования группы
# элементов, если количественные характеристики её не содержат.
_THICKNESS_KINDS = ("wall", "slab", "foundation_slab", "parapet")

# Максимальная высота стены (мм), считающейся парапетом: стены на кровле ниже
# этого значения относятся к парапетам, более высокие — к стенам кровельных
# технических помещений.
_PARAPET_MAX_HEIGHT = 2000.0

# Номер родительской лестницы в наименовании марша
# («Монолитная лестница:Лестница:1659390 Run 1»).
_STAIR_ID_RE = re.compile(r":(\d+)(?:\s|$)")


def _num(value) -> Optional[float]:
    """Приводит значение QTO к float (или None для пустых)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s or s.lower() in _EMPTY:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _text(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in _EMPTY else s


def _first_key(row: dict, suffix: str) -> str:
    """Возвращает первое непустое значение свойства с заданным суффиксом."""
    for key in row:
        if str(key).endswith(suffix):
            value = _text(row.get(key))
            if value:
                return value
    return ""


@dataclass
class Element:
    """Нормализованный элемент IFC."""

    ifc_class: str
    kind: Optional[str]
    part: str                      # underground|above|unknown
    storey_type: str
    storey: str
    mssk_code: str
    mge_name: str
    material: str
    material_code: str
    concrete_grade: str
    quantities: Dict[str, float] = field(default_factory=dict)
    geometry: Dict[str, float] = field(default_factory=dict)
    name: str = ""
    group_name: str = ""           # наименование группы элементов (ObjectType)
    concrete_spec: str = ""        # марка бетона с показателями («В30, W8, F150»)
    component: str = ""            # подтип лестницы: flight (марш)|landing (площадка)

    def quantity(self, name: str) -> float:
        return float(self.quantities.get(name) or 0.0)


def _concrete_spec(grade: str, water: str, freeze: str) -> str:
    """Собирает подпись марки бетона: «В30, W8, F150»."""
    parts = []
    if grade:
        parts.append("В" + grade.lstrip("ВBвb"))
    if water:
        parts.append("W" + water.lstrip("Ww"))
    if freeze:
        parts.append("F" + freeze.lstrip("Ff"))
    return ", ".join(parts)


def _group_thickness(group_name: str) -> Optional[int]:
    """Толщина конструкции (мм) из наименования группы элементов."""
    m = _GROUP_THICKNESS_RE.search(group_name or "")
    if not m:
        return None
    value = int(m.group(1))
    return value if 30 <= value <= 3000 else None


def _element_kind(ifc_class: str, mge_name: str, name: str = "",
                  group_name: str = "", storey_type: str = "",
                  height: Optional[float] = None) -> Optional[str]:
    """Определяет тип конструкции элемента (в терминах типового ВОР).

    Дополнительно к IFC-классу учитываются наименование элемента, тип этажа и
    высота:

    * стены с признаком «парапет» в наименовании, а также **низкие** стены
      кровли (``storey_type == 'Кровля'`` и высота ≤ 2 м) относятся к парапетам
      (``parapet``) — так объёмы парапетов попадают в свой подраздел, а стены
      кровельных технических помещений остаются в подразделе стен;
    * **сборные** лестничные марши (признак «сборн» в наименовании) относятся
      к сборным конструкциям (``precast``), а не к монолитным лестницам.
    """
    n = (mge_name or "").lower()
    name_low = f"{(name or '').lower()} {(group_name or '').lower()}"
    text = f"{n} {name_low}"
    if ifc_class in ("IfcWall", "IfcWallStandardCase"):
        if "парапет" in text:
            return "parapet"
        if storey_type == "Кровля" and height and height <= _PARAPET_MAX_HEIGHT:
            return "parapet"
        return "wall"
    if ifc_class in ("IfcStair", "IfcStairFlight"):
        return "precast" if "сборн" in text else "stair"
    if ifc_class == "IfcBeam":
        return "beam"
    if ifc_class == "IfcColumn":
        return "column"
    if ifc_class == "IfcPile":
        return "pile"
    if ifc_class == "IfcRamp":
        return "foundation_slab"
    if ifc_class == "IfcFooting":
        return "equipment_foundation" if "оборудован" in name_low else "foundation_slab"
    if ifc_class == "IfcSlab":
        if "фундамент" in name_low or "рамп" in name_low:
            return "foundation_slab"
        # Лестничная площадка в модели — это плита (``IfcSlab``) с типом
        # «Монолитная площадка» и наименованием родительской лестницы
        # («Монолитная лестница:Лестница:<номер> Landing N»); относится к
        # подразделу лестниц, а не к плитам перекрытий.
        if "площадк" in name_low and ("лестниц" in name_low
                                      or re.search(r"\blanding\b", name_low)):
            return "stair"
        return "slab"
    if ifc_class == "IfcCovering":
        if "термовкладыш" in name_low:
            return "thermoliner"
        return "insulation"
    return None


def _element_component(ifc_class: str, name: str, group_name: str) -> str:
    """Подтип лестничного элемента: ``flight`` (марш) или ``landing`` (площадка).

    Определяется по наименованию элемента/группы («Марш сборный …»,
    «Лестничная площадка …»); для полётов лестницы (``IfcStairFlight``) без
    явного указания подтипа по умолчанию принимается марш. Для прочих
    элементов возвращается пустая строка.
    """
    text = f"{(name or '').lower()} {(group_name or '').lower()}"
    if re.search(r"площад[ко]", text):
        return "landing"
    if "марш" in text or re.search(r"\brun\b", text):
        return "flight"
    if ifc_class == "IfcStairFlight":
        return "flight"
    return ""


def _storey_part(storey: str, elevation) -> tuple:
    """Определяет тип этажа и часть здания (подземная/надземная)."""
    from src.services.zero_step import classify_storey_type

    elevation = elevation if str(elevation).strip() not in ("", "nan", "None") else "-"
    storey_type = classify_storey_type(storey, elevation)
    part = "underground" if storey_type in ("Подземный", "Цокольный") else "above"
    return storey_type, part


def _stair_id(name: str) -> str:
    """Номер родительской лестницы из наименования марша (``…:1659390 Run 1``)."""
    m = _STAIR_ID_RE.search(name or "")
    return m.group(1) if m else ""


def _is_first_floor(storey: str) -> bool:
    """Является ли этаж «первым» (числовой индикатор значения «Этаж» = 1).

    Плита перекрытия первого этажа — это перекрытие над подвалом (потолок
    подземной части), поэтому её объём относится к подземной части здания
    (в типовом ВОР — подраздел «Плиты перекрытия» подземной части).
    """
    for segment in re.split(r"[_\s]+", (storey or "").lower()):
        if re.fullmatch(r"\d+", segment.strip()):
            return int(segment) == 1
    return False


def _stair_parent_storeys(raw: List[dict]) -> Dict[str, Tuple[str, str]]:
    """Этажи родительских лестниц: ``{номер лестницы: (этаж, отметка)}``.

    Нужен для маршей (``IfcStairFlight``), у которых в сыром дампе не заполнен
    этаж: номер родительской лестницы указан в наименовании марша
    (``Монолитная лестница:Лестница:1659390 Run 1``).
    """
    result: Dict[str, Tuple[str, str]] = {}
    for row in raw:
        if not isinstance(row, dict) or _text(row.get("IfcClass")) != "IfcStair":
            continue
        storey = _text(row.get("Этаж"))
        elevation = _text(row.get("Отметка_этажа"))
        keys = [_stair_id(_text(row.get("Name"))), _text(row.get("Tag"))]
        for key in keys:
            if key:
                result[key] = (storey, elevation)
    return result


class ElementIndex:
    """Индекс элементов IFC: выборка и агрегация по группам."""

    def __init__(self, elements: List[Element]):
        self.elements = elements
        self._names_cache: Dict[bool, Dict[Tuple[str, str, str], str]] = {}

    # -----------------------------------------------------------------
    @classmethod
    def from_raw_dump(cls, path: str) -> "ElementIndex":
        """Строит индекс из файла сырого дампа IFC."""
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Не найден сырой дамп IFC: {path}")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, list):
            raise ValueError("Некорректный формат сырого дампа IFC")

        elements: List[Element] = []
        stair_parents = _stair_parent_storeys(raw)
        for row in raw:
            if not isinstance(row, dict):
                continue
            ifc_class = _text(row.get("IfcClass"))
            mge_name = _first_key(row, "MGE_Name")
            name = _text(row.get("Name"))
            group_name = _text(row.get("ObjectType")) or re.sub(r":\d+$", "", name)
            storey = _text(row.get("Этаж"))
            elevation = _text(row.get("Отметка_этажа"))
            # У маршей-«полётов» (IfcStairFlight) и лестничных площадок
            # (IfcSlab «…Landing N») этаж в дампе может быть не заполнен —
            # берём его у родительской лестницы (IfcStair), на которую
            # ссылается номер в наименовании элемента.
            if not storey and (ifc_class == "IfcStairFlight"
                               or (ifc_class == "IfcSlab"
                                   and "landing" in name.lower())):
                parent = stair_parents.get(_stair_id(name))
                if parent:
                    storey, elevation = parent
            storey_type, part = _storey_part(storey, elevation)
            kind = _element_kind(
                ifc_class, mge_name, name=name, group_name=group_name,
                storey_type=storey_type,
                height=_num(row.get("QTO::Qto_WallBaseQuantities::Height")),
            )
            if kind is None:
                continue

            quantities: Dict[str, float] = {}
            for qname, keys in _QUANTITY_KEYS.items():
                for key in keys:
                    value = _num(row.get(key))
                    if value is not None:
                        quantities[qname] = value
                        break

            geometry: Dict[str, float] = {}
            for gname, keys in _GEOMETRY_KEYS.items():
                for key in keys:
                    value = _num(row.get(key))
                    if value is not None:
                        geometry[gname] = value
                        break
            if kind == "column" or kind == "beam":
                section = _first_key(row, "MGE_Section")
                area = _num(row.get("QTO::Qto_BeamBaseQuantities::CrossSectionArea"))
                if section:
                    geometry["section_label"] = section
                elif area:
                    geometry["section_area"] = area
                m = _SECTION_NAME_RE.search(_text(row.get("Name")))
                if m:
                    geometry["section_w"] = float(m.group(1))
                    geometry["section_h"] = float(m.group(2))
            if kind == "slab" or kind == "foundation_slab":
                area = _num(row.get("QTO::Qto_SlabBaseQuantities::NetArea"))
                if area:
                    geometry["area"] = area

            group_name = _text(row.get("ObjectType")) or re.sub(r":\d+$", "", _text(row.get("Name")))
            if "thickness" not in geometry and kind in _THICKNESS_KINDS:
                thickness = _group_thickness(group_name)
                if thickness:
                    geometry["thickness"] = float(thickness)

            grade = _text(row.get("Свойство::ExpCheck_MaterialConcrete::MGE_ConcreteGrade"))
            elements.append(Element(
                ifc_class=ifc_class,
                kind=kind,
                part=part,
                storey_type=storey_type,
                storey=storey,
                mssk_code=_first_key(row, "MGE_ElementCode"),
                mge_name=mge_name,
                material=_text(row.get("Материал")),
                material_code=_first_key(row, "MGE_MaterialCode"),
                concrete_grade=grade,
                quantities=quantities,
                geometry=geometry,
                name=name,
                group_name=group_name,
                concrete_spec=_concrete_spec(
                    grade,
                    _text(row.get("Свойство::ExpCheck_MaterialConcrete::MGE_WaterResist")),
                    _text(row.get("Свойство::ExpCheck_MaterialConcrete::MGE_FreezeDurability")),
                ),
                component=_element_component(ifc_class, name, group_name)
                if kind in ("stair", "precast") else "",
            ))

        # Плита перекрытия первого этажа — перекрытие над подвалом (потолок
        # подземной части): при наличии подземной части её объём относится к
        # подразделу «Плиты перекрытия» подземной части (как в ВОР заказчика).
        if any(el.part == "underground" for el in elements):
            for el in elements:
                if el.kind == "slab" and el.part == "above" and _is_first_floor(el.storey):
                    el.part = "underground"

        logger.info("Индекс элементов IFC построен: элементов %d", len(elements))
        return cls(elements)

    # -----------------------------------------------------------------
    @staticmethod
    def group_label(element: Element) -> str:
        """Наименование группы элементов модели (``ObjectType``)."""
        return element.group_name or element.mge_name or ""

    def group_names_by_geometry(self, ignore_part: bool = False) -> Dict[Tuple[str, str, str], str]:
        """Карта «(тип конструкции, часть здания, геометрия) → группа элементов».

        Используется для подстановки наименований групп элементов модели в
        заголовки геометрических блоков ВОР (``builder``, ``customer_vor``).
        При ``ignore_part=True`` часть здания в ключе пустая (``""``) — для
        случаев, когда часть здания блока ВОР не определена.
        """
        if ignore_part in self._names_cache:
            return self._names_cache[ignore_part]
        result: Dict[Tuple[str, str, str], str] = {}
        for element in self.elements:
            name = self.group_label(element)
            if not name:
                continue
            for param in ("thickness", "section"):
                label = self.geometry_label(element, param)
                if label:
                    key = (element.kind or "", "" if ignore_part else element.part, label)
                    result.setdefault(key, name)
        self._names_cache[ignore_part] = result
        return result

    def geometry_group_name(self, kind: Optional[str], part: Optional[str],
                            label: Optional[str]) -> Optional[str]:
        """Наименование группы элементов модели по типу, части здания и геометрии."""
        if not label:
            return None
        name = self.group_names_by_geometry().get((kind or "", part, label))
        if name:
            return name
        return self.group_names_by_geometry(ignore_part=True).get((kind or "", "", label))

    # -----------------------------------------------------------------
    def select(self, kind: Optional[str], part: Optional[str] = None) -> List[Element]:
        """Выбирает элементы по типу конструкции и части здания."""
        result = []
        for el in self.elements:
            if kind and el.kind != kind:
                continue
            if part in ("underground", "above") and el.part != part:
                continue
            result.append(el)
        return result

    # -----------------------------------------------------------------
    def geometry_value(self, element: Element, param: Optional[str]):
        """Возвращает числовое значение геометрического параметра элемента."""
        if not param:
            return None
        if param == "thickness":
            return element.geometry.get("thickness")
        if param == "section":
            return element.geometry.get("section_w") or element.geometry.get("section_area")
        if param == "area":
            return element.geometry.get("area")
        return None

    def geometry_label(self, element: Element, param: Optional[str]) -> Optional[str]:
        """Возвращает подпись геометрической группы элемента (для заголовка)."""
        if not param:
            return None
        if param == "thickness":
            value = element.geometry.get("thickness")
            if value:
                return f"{int(round(value))}"
            return None
        if param == "section":
            label = element.geometry.get("section_label")
            if label:
                return label
            w = element.geometry.get("section_w")
            h = element.geometry.get("section_h")
            if w and h:
                return f"{int(w)}х{int(h)}"
            return None
        if param == "area":
            value = element.geometry.get("area")
            if value:
                return f"{round(value, 2)}"
            return None
        return None


def aggregate(elements: List[Element]) -> Dict[str, object]:
    """Агрегирует количественные характеристики группы элементов."""
    total = {
        "count": len(elements),
        "volume": 0.0,
        "gross_volume": 0.0,
        "area": 0.0,
        "side_area": 0.0,
        "surface_area": 0.0,
        "length": 0.0,
    }
    grades: Counter = Counter()
    for el in elements:
        total["volume"] += el.quantity("volume")
        total["gross_volume"] += el.quantity("gross_volume")
        total["area"] += el.quantity("area")
        total["side_area"] += el.quantity("side_area")
        total["surface_area"] += el.quantity("surface_area")
        # Длины в сыром дампе — в мм, переводим в метры.
        total["length"] += el.quantity("length") / 1000.0
        if el.concrete_grade:
            grades[el.concrete_grade] += el.quantity("volume")
    total["grades"] = grades
    return total
