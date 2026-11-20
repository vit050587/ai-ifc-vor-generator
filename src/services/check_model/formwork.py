"""Площадь опалубки монолитных железобетонных элементов ЦИМ.

Превью по типовому ВОР показывает по каждой группе суммарную **площадь
опалубки** — величину, по которой в ВОР считаются работы монтажа и демонтажа
опалубки. Площадь опалубки нужна только элементам из **монолитного**
железобетона: у сборного ж/б опалубливание выполняется на заводе и в ВОР не
попадает, у неконструктивных элементов (проёмы, изоляция, швы, засыпка, сваи)
опалубки нет.

Правила расчёта по видам конструкций типового ВОР:

* **стена, парапет** — вертикальная проекция одной стороны без вычета
  проёмов (ТСН-2001.3-6, п. 2.8), без добавления торцов и откосов;
* **колонна, пилон** — площадь с четырёх сторон: периметр сечения × высота
  (``2 × (RUS_Width + RUS_Height) × RUS_Length``; fallback —
  ``RUS_NetSideArea``);
* **фундамент, приямок** — площадь контакта опалубки с бетоном; при
  отсутствии явного значения — оценка по граням или периметру × толщине;
* **перекрытие** — горизонтальная проекция без вычета отверстий;
* **лестничная площадка, пандус** — проекция без вычета отверстий.
  При нехватке данных сохраняются заменители по площади и размерам,
  с пояснением об оценке;
* **лестничный марш** — площадь проекции на пол (shapely-проекция меша
  ``QTO_geom::Горизонтальная_проекция_м2``; fallback — ``RUS_Length ×
  RUS_Width``);
* **балка** — низ и две длинные боковые грани, верхняя не опалубливается:
  ``RUS_Length × b + 2 × RUS_Length × h``, где ``b`` — ширина сечения,
  ``h`` — высота сечения. Ориентация сечения уточняется по низу меша
  (``QTO_geom::Площадь_низа_м2``) и объёму; без геометрии берутся меньший и
  больший из ``RUS_Width``/``RUS_Height``.

Источник величин — **требуемые параметры** ``RUS_*``: на шаге проверки ЦИМ они
заполняются из исходных параметров модели, которые в разных проектах
называются по-разному, поэтому расчёт по ``RUS_*`` универсален. Геометрические
колонки сырого дампа (``QTO_geom::*``) используются только как уточнение там,
где ``RUS_*`` принципиально недостаточно: ориентация сечения балки и проекция
лестничного марша на пол (shapely).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from src.services.construction_method import construction_method

from .geometry import to_number

# Колонки сырого дампа, уточняющие расчёт (не являются требуемыми параметрами).
_PROJECTION = "QTO_geom::Горизонтальная_проекция_м2"
_BEAM_UNDERSIDE = "QTO_geom::Площадь_низа_м2"

# Виды конструкций, которым площадь опалубки не требуется.
_NO_FORMWORK_KINDS = frozenset(
    {
        "opening",
        "pile_driven",
        "pile_bored",
        "insulation",
        "seam",
        "seam_foundation",
        "backfill",
        "thermoliner",
        "hydro_horizontal",
        "hydro_vertical",
        "precast_panel",
        "precast_stair",
        "precast_parapet",
        "precast_shaft",
        "precast_bathroom",
        "precast_vent",
        "precast_engineering",
        "precast_joints",
    }
)

_PRECAST_RE = re.compile(r"сборн|\bprecast\b|\bprefabricated\b", re.IGNORECASE)
_CONCRETE_RE = re.compile(r"железобетон|бетон|\bconcrete\b", re.IGNORECASE)
_MONOLITHIC_RE = re.compile(
    r"монолит|\bcast[ _-]?in[ _-]?(?:situ|place)\b|\bmonolithic\b", re.IGNORECASE
)

# Подписи правил — показываются в превью, чтобы было видно, как посчитана
# площадь опалубки.
BASIS = {
    "wall": "вертикальная проекция одной стороны без вычета проёмов",
    "column": "4 боковые поверхности (периметр сечения × высота)",
    "foundation": "внешний периметр × толщина плиты",
    "slab": "горизонтальная проекция без вычета отверстий",
    "landing": "горизонтальная площадь площадки с одной стороны",
    "flight": "проекция марша на пол",
    "beam": "низ + две длинные боковые грани",
}


def element_technology(row: Dict[str, Any]) -> str:
    """Технология изготовления элемента: ``precast`` | ``monolithic`` | ``unknown``.

    Признаки проверяются по требуемым параметрам (``RUS_MSSK_Material_Name``,
    ``RUS_MSSK_Material_Code``) и по общему определению технологии
    (``construction_method``: ``ConstructionMethod``/``Материал``/наименование).
    """
    material = " ".join(
        str(row.get(key) or "")
        for key in ("RUS_MSSK_Material_Name", "RUS_MSSK_Material_Code", "Материал")
    )
    if _PRECAST_RE.search(material):
        return "precast"
    method = construction_method(row)
    if method == "precast":
        return "precast"
    if method == "monolithic":
        return "monolithic"
    if _MONOLITHIC_RE.search(material):
        return "monolithic"
    if _CONCRETE_RE.search(material):
        return "monolithic"
    return "unknown"


def _millimetres_to_metres(value: Any) -> Optional[float]:
    """Длина из ``RUS_*`` (мм) в метры."""
    number = to_number(value)
    if number is None or number <= 0:
        return None
    return number / 1000.0


def _positive(value: Any) -> Optional[float]:
    """Положительное число (площадь/объём) или ``None``."""
    number = to_number(value)
    if number is None or number <= 0:
        return None
    return number


def _wall_area(row: Dict[str, Any]) -> Optional[float]:
    """Одна вертикальная проекция без вычета проёмов, ТСН-2001.3-6 п. 2.8."""
    from ..wall_formwork import wall_projection
    return wall_projection(row)[0]


def _column_area(row: Dict[str, Any]) -> Optional[float]:
    """Колонна/пилон: площадь контакта с бетоном, п. 2.8."""
    from ..contact_formwork import contact_area
    contact, _ = contact_area(row)
    if contact is not None:
        return contact
    width = _millimetres_to_metres(row.get("RUS_Width"))
    height = _millimetres_to_metres(row.get("RUS_Height"))
    length = _millimetres_to_metres(row.get("RUS_Length"))
    if width and height and length:
        return 2.0 * (width + height) * length
    # Боковая площадь QTO уже учитывает все боковые грани колонны.
    return _positive(row.get("RUS_NetSideArea"))


def _foundation_area(row: Dict[str, Any]) -> Optional[float]:
    """Фундамент/приямок: площадь контакта, п. 2.8; торцы как оценка."""
    from ..contact_formwork import contact_area
    contact, _ = contact_area(row)
    if contact is not None:
        return contact
    perimeter = _positive(row.get("RUS_Perimeter"))  # требуемый параметр — в метрах
    thickness = _millimetres_to_metres(row.get("RUS_Thickness"))
    if perimeter and thickness:
        return perimeter * thickness
    length = _millimetres_to_metres(row.get("RUS_Length"))
    width = _millimetres_to_metres(row.get("RUS_Width"))
    if length and width and thickness:
        return 2.0 * (length + width) * thickness
    return None


def _horizontal_area(row: Dict[str, Any]) -> Optional[float]:
    """Площадка: проекция без отверстий, как перекрытие (пп. 1.2.5, 2.8)."""
    from ..stair_formwork import floor_projection
    return floor_projection(row)[0]


def _slab_area(row: Dict[str, Any]) -> Optional[float]:
    """Перекрытие: горизонтальная проекция без отверстий, п. 2.8."""
    return _horizontal_area(row)


def _flight_area(row: Dict[str, Any]) -> Optional[float]:
    """Марш: только измеренная горизонтальная проекция, п. 2.8."""
    from ..stair_formwork import flight_projection
    return flight_projection(row)[0]


def _beam_area(row: Dict[str, Any]) -> Optional[float]:
    """Балка: низ и две длинные боковые грани, без верхней грани."""
    from ..contact_formwork import contact_area
    contact, _ = contact_area(row, kind='beam')
    if contact is not None:
        return contact
    length = _millimetres_to_metres(row.get("RUS_Length"))
    if not length:
        return None
    volume = _positive(row.get("RUS_Volume"))
    underside = _positive(row.get(_BEAM_UNDERSIDE))
    if underside and volume:
        # Ориентация сечения по фактическому низу меша: ширина = низ / длина,
        # высота = объём / (длина × ширина). Не зависит от поворота элемента.
        width = underside / length
        if width > 0:
            height = volume / (length * width)
            if height > 0:
                return underside + 2.0 * length * height
    width = _millimetres_to_metres(row.get("RUS_Width"))
    height = _millimetres_to_metres(row.get("RUS_Height"))
    if width and height:
        narrow, tall = sorted((width, height))
        return length * narrow + 2.0 * length * tall
    return None


_RULES = {
    "wall": _wall_area,
    "column": _column_area,
    "foundation": _foundation_area,
    "slab": _slab_area,
    "landing": _horizontal_area,
    "flight": _flight_area,
    "beam": _beam_area,
}


def _rule(row: Dict[str, Any], kind: Optional[str]) -> Optional[str]:
    """Правило расчёта опалубки для вида конструкции (``None`` — не требуется)."""
    if not kind or kind in _NO_FORMWORK_KINDS:
        return None
    ifc_class = str(row.get("IfcClass") or "").strip()
    if kind in ("wall", "parapet"):
        return "wall"
    if kind == "column":
        return "column"
    if kind in ("foundation_slab", "equipment_foundation"):
        return "foundation"
    if kind == "beam":
        return "beam"
    if kind == "slab":
        return "slab"
    if kind == "stair":
        if ifc_class in ("IfcStairFlight", "IfcRampFlight"):
            return "flight"
        if ifc_class == "IfcRamp":
            return "landing"
        if ifc_class in ("IfcStair", "IfcStairStandardCase"):
            # Агрегат лестницы: марши и площадки учитываются собственными
            # элементами, иначе площадь опалубки удвоится.
            return None
        return "landing"
    return None


def formwork_area(
    row: Dict[str, Any],
    kind: Optional[str],
) -> Tuple[Optional[float], str]:
    """Площадь опалубки элемента (м²) и подпись правила расчёта.

    Args:
        row: строка ``Проверка_ЦИМ_модели.json`` (содержит требуемые ``RUS_*``).
        kind: вид конструкции в терминах подразделов типового ВОР
            (``grouping._element_kind``).

    Returns:
        ``(площадь_м², правило)``. Для сборного ж/б и элементов, которым
        опалубка не требуется, — ``(None, "")``.
    """
    rule = _rule(row, kind)
    if rule is None:
        return None, ""
    if element_technology(row) == "precast":
        return None, ""
    value = _RULES[rule](row)
    if value is None or value <= 0:
        return None, ""
    basis = BASIS[rule]
    if rule in ('slab', 'landing', 'flight'):
        from ..stair_formwork import floor_projection, flight_projection
        _, source = flight_projection(row) if rule == 'flight' else floor_projection(row)
        if source and 'Оценка' in source:
            basis += '; ' + source
    elif rule == 'wall':
        from ..wall_formwork import wall_projection
        _, source = wall_projection(row)
        if source and 'Оценка' in source:
            basis += '; ' + source
    if rule in ('foundation', 'column', 'beam'):
        from ..contact_formwork import contact_area
        _, source = contact_area(row)
        basis = source or 'Оценка площади контакта: ' + basis + '; примыкания требуют проекта'
    return round(float(value), 4), basis
