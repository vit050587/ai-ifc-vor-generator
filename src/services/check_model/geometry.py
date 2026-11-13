"""Добор геометрических параметров ЦИМ по данным сырого дампа IFC.

Карта сопоставления (``mapping.py``) строится один раз на набор требований,
поэтому для всех элементов класса выбирается одна и та же колонка модели.
На практике QTO выгружены не у всех элементов (например, Revit не пишет
``Qto_WallBaseQuantities`` для части стен), и выбранная колонка у конкретного
элемента оказывается пустой — хотя габариты элемента есть в ``QTO_bbox::*``.

Модуль заполняет такие пропуски по цепочке:

1. альтернативные колонки дампа (QTO своего класса, ``QTO_bbox::*``,
   толщина слоя ``IfcMaterialLayer``);
2. расчёт по формулам, зависящим от класса IFC:

   * **стена** — толщина = ширина bbox, площадь в плане = длина × толщина,
     площадь боковой поверхности = объём / толщина (нетто; при отсутствии
     объёма — длина × высота), объём = длина × толщина × высота;
   * **перекрытие/плита** — толщина = минимальный габарит,
     площадь = длина × ширина (или QTO-площадь), объём = площадь × толщина,
     периметр = 2 × (длина + ширина);
   * **проём** — ширина/высота = два наибольших габарита (третий — глубина),
     площадь = ширина × высота;
   * **лестница** — объём = длина × ширина × высота;
   * **колонна/свая** — высота = длина QTO или вертикальный габарит bbox.

Единицы дампа: линейные значения ``QTO::*`` и ``QTO_bbox::*_мм`` — миллиметры,
площади QTO и ``QTO_bbox::Площадь_поверхности_м2`` — м²,
объёмы ``QTO_geom::Объём_м3`` / ``QTO_bbox::*_м3`` — м³.
Наружу значение возвращается в единице требования (``attribute.unit``).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, Optional, Sequence

from .requirements import RequiredAttribute

# Требуемые параметры, которые считаются геометрическими и могут быть
# восстановлены из габаритов/QTO элемента.
GEOMETRY_ATTRIBUTES = frozenset(
    {
        "RUS_Length",
        "RUS_Width",
        "RUS_Height",
        "RUS_Thickness",
        "RUS_Perimeter",
        "RUS_Diameter",
        "RUS_Area",
        "RUS_NetFootprintArea",
        "RUS_NetSideArea",
        "RUS_NetSurfaceArea",
        "RUS_Volume",
        "RUS_GrossVolume",
    }
)

_WALL_CLASSES = {
    "IfcWall",
    "IfcWallStandardCase",
    "IfcWallElementedCase",
    "IfcCurtainWall",
}
_SLAB_CLASSES = {
    "IfcSlab",
    "IfcSlabStandardCase",
    "IfcSlabElementedCase",
    "IfcPlate",
}
_STAIR_CLASSES = {"IfcStair", "IfcStairFlight"}
_OPENING_CLASSES = {"IfcOpeningElement", "IfcOpeningStandardCase"}
_COLUMN_CLASSES = {"IfcColumn", "IfcPile"}
_BEAM_CLASSES = {"IfcBeam", "IfcMember"}

# Колонки дампа.
_BBOX_LENGTH = "QTO_bbox::Длина_мм"
_BBOX_WIDTH = "QTO_bbox::Ширина_мм"
_BBOX_HEIGHT = "QTO_bbox::Высота_мм"
_BBOX_THICKNESS = "QTO_bbox::Толщина_мм"
_BBOX_PERIMETER = "QTO_bbox::Периметр_мм"
_BBOX_VOLUME = "QTO_bbox::Габаритный_объём_м3"
_BBOX_SURFACE = "QTO_bbox::Площадь_поверхности_м2"
_MESH_VOLUME = "QTO_geom::Объём_м3"
_MESH_VOLUME_ALIAS = "QTO_bbox::Объём_м3"
_QUANTITY_VOLUME = "Количество::Объём_м3"
_PROJECTION_AREA = "QTO_geom::Горизонтальная_проекция_м2"
_OUTER_PERIMETER = "QTO_geom::Внешний_периметр_мм"
_LAYER_THICKNESS = r"IfcMaterialLayer::LayerThickness"


def to_number(value: Any) -> Optional[float]:
    """Привести значение дампа к числу (``None``, если число не читается)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first(row: Dict[str, Any], patterns: Iterable[str]) -> Optional[float]:
    """Первое числовое значение по списку regex-шаблонов колонок.

    Шаблоны проверяются в порядке приоритета: значения первой подходящей
    непустой колонки возвращаются сразу.
    """
    for pattern in patterns:
        for key, value in row.items():
            if not re.search(pattern, str(key), re.IGNORECASE):
                continue
            number = to_number(value)
            if number is not None:
                return number
    return None


def _value(row: Dict[str, Any], *keys: str) -> Optional[float]:
    """Числовое значение первой непустой колонки из перечисленных."""
    for key in keys:
        number = to_number(row.get(key))
        if number is not None:
            return number
    return None


def _class_group(ifc_class: Optional[str]) -> str:
    """Группа геометрических правил для IFC-класса элемента."""
    name = str(ifc_class or "").strip()
    if name in _WALL_CLASSES:
        return "wall"
    if name in _SLAB_CLASSES:
        return "slab"
    if name in _STAIR_CLASSES:
        return "stair"
    if name in _OPENING_CLASSES:
        return "opening"
    if name in _COLUMN_CLASSES:
        return "column"
    if name in _BEAM_CLASSES:
        return "beam"
    return "other"


def _round(value: float, digits: int) -> Any:
    """Округлить значение и убрать хвостовые нули у целых чисел."""
    rounded = round(float(value), digits)
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return rounded


def _is_metre(unit: Optional[str]) -> bool:
    text = str(unit or "").strip().lower().replace(" ", "")
    return text in ("м", "m")


def _is_square_millimetre(unit: Optional[str]) -> bool:
    text = str(unit or "").strip().lower().replace(" ", "")
    return "мм²" in text or "мм2" in text or "mm2" in text


def _is_cubic_millimetre(unit: Optional[str]) -> bool:
    text = str(unit or "").strip().lower().replace(" ", "")
    return "мм³" in text or "мм3" in text or "mm3" in text


def _length_value(value_mm: Optional[float], unit: Optional[str]) -> Any:
    """Миллиметры → единица требования."""
    if value_mm is None:
        return ""
    if _is_metre(unit):
        return _round(value_mm / 1000.0, 4)
    return _round(value_mm, 2)


def _area_value(value_m2: Optional[float], unit: Optional[str]) -> Any:
    """Квадратные метры → единица требования."""
    if value_m2 is None:
        return ""
    if _is_square_millimetre(unit):
        return _round(value_m2 * 1_000_000.0, 0)
    return _round(value_m2, 4)


def _volume_value(value_m3: Optional[float], unit: Optional[str]) -> Any:
    """Кубические метры → единица требования."""
    if value_m3 is None:
        return ""
    if _is_cubic_millimetre(unit):
        return _round(value_m3 * 1_000_000_000.0, 0)
    return _round(value_m3, 4)


# ---------------------------------------------------------------------
#  Исходные габариты (мм)
# ---------------------------------------------------------------------

def _raw_length_mm(row: Dict[str, Any], group: str) -> Optional[float]:
    if group == "wall":
        return _first(
            row,
            (
                r"^QTO::.*Wall.*::Length$",
                r"^QTO::BaseQuantities::Length$",
                _BBOX_LENGTH,
                r"^QTO::.*::Length$",
            ),
        )
    if group == "opening":
        box = _opening_box(row)
        return box[0] if box else None
    if group == "slab":
        value = _first(
            row,
            (_BBOX_LENGTH, r"^QTO::.*Slab.*::Length$", r"^QTO::.*::Length$"),
        )
        if value is None:
            plan = _slab_plan_dims_mm(row)
            value = plan[0] if plan else None
        return value
    if group == "column":
        # Для колонны длина — вертикальный габарит; QTO Length в разных
        # моделях может означать размер профиля.
        return _first(
            row,
            (_BBOX_HEIGHT, r"^QTO::.*Column.*::Length$"),
        )
    if group == "beam":
        # Длина элемента: QTO точнее габарита bbox (для наклонных элементов
        # bbox занижает длину).
        return _first(
            row,
            (
                r"^QTO::.*(?:Beam|Member).*::Length$",
                r"^QTO::.*::Length$",
                _BBOX_LENGTH,
            ),
        )
    return _first(row, (_BBOX_LENGTH, r"^QTO::.*::Length$"))


def _raw_width_mm(row: Dict[str, Any], group: str) -> Optional[float]:
    if group == "wall":
        # У стены Qto_...WallBaseQuantities::Width — это толщина.
        return _first(
            row,
            (
                r"^QTO::.*Wall.*::Width$",
                _BBOX_WIDTH,
                _LAYER_THICKNESS,
                r"^QTO::BaseQuantities::Width$",
            ),
        )
    if group == "opening":
        return _first(
            row,
            (r"^QTO::.*Opening.*::Width$", r"^QTO::BaseQuantities::Width$"),
        )
    if group == "slab":
        # У плиты Qto_...SlabBaseQuantities::Width — толщина, шириной не является.
        value = _value(row, _BBOX_WIDTH)
        if value is None:
            plan = _slab_plan_dims_mm(row)
            value = plan[1] if plan else None
        return value
    if group == "column":
        return _first(row, (r"^QTO::.*Column.*::Width$", _BBOX_WIDTH))
    if group == "beam":
        # Габариты bbox отсортированы по убыванию: наименьший — ширина
        # сечения, средний — высота сечения.
        return _first(
            row,
            (
                r"^QTO::.*Beam.*::Width$",
                r"^QTO::.*Member.*::Width$",
                _BBOX_HEIGHT,
                _BBOX_WIDTH,
            ),
        )
    return _first(row, (_BBOX_WIDTH, r"^QTO::.*::Width$"))


def _raw_height_mm(row: Dict[str, Any], group: str) -> Optional[float]:
    if group == "wall":
        return _first(
            row,
            (
                r"^QTO::.*Wall.*::Height$",
                _BBOX_HEIGHT,
                r"^QTO::BaseQuantities::Height$",
            ),
        )
    if group == "opening":
        return _first(
            row,
            (r"^QTO::.*Opening.*::Height$", r"^QTO::BaseQuantities::Height$"),
        )
    if group == "column":
        # Высота сечения — больший горизонтальный габарит (для колонн
        # QTO_bbox::Длина_мм — это max(X, Y)).
        return _first(
            row,
            (
                _BBOX_LENGTH,
                r"^QTO::.*Column.*::Height$",
                r"^QTO::.*Column.*::Length$",
            ),
        )
    if group == "beam":
        # Высота сечения — средний габарит bbox.
        return _first(
            row,
            (
                r"^QTO::.*Beam.*::Height$",
                r"^QTO::.*Member.*::Height$",
                _BBOX_WIDTH,
            ),
        )
    if group == "slab":
        return None
    return _first(row, (_BBOX_HEIGHT, r"^QTO::.*::Height$"))


def _thickness_mm(row: Dict[str, Any], group: str) -> Optional[float]:
    if group == "wall":
        return _first(
            row,
            (
                r"^QTO::.*Wall.*::Width$",
                _BBOX_WIDTH,
                _LAYER_THICKNESS,
                r"^QTO::BaseQuantities::Width$",
                r"Толщина",
            ),
        )
    if group == "slab":
        # Минимальный габарит bbox у плиты — это толщина.
        return _first(
            row,
            (
                r"^QTO::.*Slab.*::Depth$",
                _BBOX_THICKNESS,
                r"^QTO::.*Slab.*::Width$",
                r"^QTO::BaseQuantities::Depth$",
                _LAYER_THICKNESS,
                _BBOX_HEIGHT,
            ),
        )
    if group == "column":
        return _first(
            row,
            (r"^QTO::.*Column.*::Width$", _BBOX_WIDTH, r"Толщина"),
        )
    return _first(
        row,
        (r"^QTO::.*::Depth$", r"Толщина", _BBOX_THICKNESS, r"^QTO::.*::Width$"),
    )


def _opening_box(row: Dict[str, Any]) -> Optional[Sequence[float]]:
    """Габариты проёма (мм) по убыванию: ширина, высота, глубина.

    В дампе габариты проёма отсортированы по убыванию, поэтому глубина
    (толщина стены/плиты) — наименьший габарит. Если выгружены не все три
    габарита, ориентация неоднозначна и размеры не восстанавливаются.
    """
    dims = [
        _value(row, _BBOX_LENGTH),
        _value(row, _BBOX_WIDTH),
        _value(row, _BBOX_HEIGHT),
    ]
    if any(dim is None or dim <= 0 for dim in dims):
        return None
    return sorted((float(dim) for dim in dims), reverse=True)


# ---------------------------------------------------------------------
#  Площади и объёмы (м² / м³)
# ---------------------------------------------------------------------

def _slab_plan_dims_mm(row: Dict[str, Any]) -> Optional[Sequence[float]]:
    """Длина и ширина плиты (мм) по площади и периметру QTO, без геометрии.

    След плиты приводится к прямоугольнику: ``a + b = P / 2``, ``a × b = A``.
    Так габариты в плане восстанавливаются, когда QTO содержит объём и
    площадь, но не содержит габаритов: для таких плит ``QTO_bbox::*`` не
    рассчитывается (см. ``ifc_geometry_quantities.fill_missing``) — расчёт
    геометрии плиты (create_shape с вычитанием проёмов) стоит до 19 с на
    элемент. Для непрямоугольного следа (отрицательный дискриминант) габариты
    не восстанавливаются.

    Returns:
        ``(длина, ширина)`` в миллиметрах, длина — большая сторона.
    """
    area = _raw_area_m2(row, "slab")
    perimeter = _first(
        row,
        (r"^QTO::.*::Perimeter$", _OUTER_PERIMETER, _BBOX_PERIMETER),
    )
    if not area or not perimeter:
        return None
    half = perimeter / 2.0
    area_mm2 = area * 1_000_000.0
    discriminant = half * half - 4.0 * area_mm2
    if discriminant < 0:
        return None
    root = math.sqrt(discriminant)
    length = (half + root) / 2.0
    width = (half - root) / 2.0
    if length <= 0 or width <= 0:
        return None
    return length, width


def _raw_area_m2(row: Dict[str, Any], group: str) -> Optional[float]:
    """Площадь из QTO/проекции (без расчёта по габаритам)."""
    if group == "slab":
        return _first(
            row,
            (
                r"^QTO::.*Slab.*::(Net|Gross)Area$",
                r"^QTO::.*::(Net|Gross)Area$",
                _PROJECTION_AREA,
            ),
        )
    if group == "opening":
        return _first(
            row,
            (r"^QTO::.*Opening.*::Area$", r"^QTO::BaseQuantities::Area$"),
        )
    return _first(
        row,
        (r"^QTO::.*::(Net|Gross)Area$", _PROJECTION_AREA),
    )


def _area_m2(row: Dict[str, Any], group: str) -> Optional[float]:
    value = _raw_area_m2(row, group)
    if value is not None:
        return value
    if group == "slab":
        length = _raw_length_mm(row, group)
        width = _raw_width_mm(row, group)
        if length and width:
            return length * width / 1_000_000.0
    if group == "opening":
        # Площадь проёма = ширина × высота. Ширина/высота берутся из QTO
        # (Qto_OpeningElementBaseQuantities / BaseQuantities), а при их
        # отсутствии — из габаритов bbox.
        width = _raw_width_mm(row, group)
        height = _raw_height_mm(row, group)
        if width and height:
            return width * height / 1_000_000.0
        box = _opening_box(row)
        if box:
            return box[0] * box[1] / 1_000_000.0
    if group == "wall":
        length = _raw_length_mm(row, group)
        height = _raw_height_mm(row, group)
        if length and height:
            return length * height / 1_000_000.0
    return None


def _footprint_area_m2(row: Dict[str, Any], group: str) -> Optional[float]:
    """Площадь в плане (для стены — длина × толщина)."""
    if group == "wall":
        value = _first(
            row,
            (
                r"^QTO::.*Wall.*::(Net|Gross)FootprintArea$",
                r"^QTO::.*::(Net|Gross)FootprintArea$",
            ),
        )
        if value is not None:
            return value
        length = _raw_length_mm(row, group)
        thickness = _thickness_mm(row, group)
        if length and thickness:
            return length * thickness / 1_000_000.0
        return None
    if group == "slab":
        return _area_m2(row, group)
    value = _first(row, (r"::(Net|Gross)FootprintArea$",))
    if value is not None:
        return value
    length = _raw_length_mm(row, group)
    width = _raw_width_mm(row, group)
    if length and width:
        return length * width / 1_000_000.0
    return None


def _net_volume_m3(row: Dict[str, Any]) -> Optional[float]:
    """Нетто-объём из QTO, без расчёта по габаритам.

    Габаритный объём bbox (``QTO_bbox::Габаритный_объём_м3``) сюда не входит:
    для стены он равен ``длина × толщина × высота``, то есть брутто, и
    восстановленная по нему площадь боковой поверхности тоже была бы брутто.
    """
    value = _first(row, (r"^QTO::.*::NetVolume$",))
    if value is not None:
        return value
    return _value(row, _MESH_VOLUME, _MESH_VOLUME_ALIAS, _QUANTITY_VOLUME)


def _side_area_m2(row: Dict[str, Any], group: str) -> Optional[float]:
    """Площадь боковой поверхности (нетто).

    Для стены при отсутствии ``NetSideArea``/``GrossSideArea`` площадь
    восстанавливается как ``NetVolume / толщина`` — это точное нетто-значение:
    ``QTO Length`` у Revit включает запас на примыкание, а ``Height`` не
    учитывает вырезы, поэтому произведение ``длина × высота`` завышено
    (по обследованной модели — на 4–26 % у стен без QTO).
    """
    if group == "wall":
        value = _first(row, (r"^QTO::.*Wall.*::(Net|Gross)SideArea$",))
        if value is not None:
            return value
        volume = _net_volume_m3(row)
        thickness = _thickness_mm(row, group)
        if volume and thickness:
            return volume / (thickness / 1000.0)
        length = _raw_length_mm(row, group)
        height = _raw_height_mm(row, group)
        if length and height:
            return length * height / 1_000_000.0
        return None
    value = _first(row, (r"::(Net|Gross)SideArea$",))
    if value is not None:
        return value
    if group == "slab":
        perimeter = _perimeter_mm(row, group)
        thickness = _thickness_mm(row, group)
        if perimeter and thickness:
            return perimeter * thickness / 1_000_000.0
    return None


def _perimeter_mm(row: Dict[str, Any], group: str) -> Optional[float]:
    value = _first(
        row,
        (
            r"^QTO::.*::Perimeter$",
            _OUTER_PERIMETER,
            _BBOX_PERIMETER,
        ),
    )
    if value is not None:
        return value
    if group == "slab":
        length = _raw_length_mm(row, group)
        width = _raw_width_mm(row, group)
        if length and width:
            return 2.0 * (length + width)
    if group == "wall":
        length = _raw_length_mm(row, group)
        height = _raw_height_mm(row, group)
        if length and height:
            return 2.0 * (length + height)
    return None


def _volume_m3(row: Dict[str, Any], group: str) -> Optional[float]:
    """Объём нетто: объём меша, затем расчёт по габаритам, затем bbox-объём.

    Габаритный объём bbox (``QTO_bbox::Габаритный_объём_м3``) — произведение
    габаритов, поэтому для некруглых элементов он завышен и используется
    только как последний вариант.
    """
    value = _value(row, _MESH_VOLUME, _MESH_VOLUME_ALIAS)
    if value is not None:
        return value
    if group == "wall":
        length = _raw_length_mm(row, group)
        thickness = _thickness_mm(row, group)
        height = _raw_height_mm(row, group)
        if length and thickness and height:
            return length * thickness * height / 1_000_000_000.0
    if group == "slab":
        area = _raw_area_m2(row, group)
        thickness = _thickness_mm(row, group)
        if area and thickness:
            return area * thickness / 1000.0
        length = _raw_length_mm(row, group)
        width = _raw_width_mm(row, group)
        if length and width and thickness:
            return length * width * thickness / 1_000_000_000.0
    if group == "opening":
        box = _opening_box(row)
        if box:
            return box[0] * box[1] * box[2] / 1_000_000_000.0
    if group == "stair":
        length = _raw_length_mm(row, group)
        width = _raw_width_mm(row, group)
        height = _raw_height_mm(row, group)
        if length and width and height:
            return length * width * height / 1_000_000_000.0
    return _value(row, _BBOX_VOLUME)


def _gross_volume_m3(row: Dict[str, Any], group: str) -> Optional[float]:
    """Объём брутто: габаритный объём bbox, иначе обычный объём."""
    value = _value(row, _BBOX_VOLUME)
    if value is not None:
        return value
    return _volume_m3(row, group)


# ---------------------------------------------------------------------
#  Публичная точка входа
# ---------------------------------------------------------------------

def resolve_geometry(
    attribute: RequiredAttribute,
    row: Dict[str, Any],
    ifc_class: Optional[str] = None,
) -> Any:
    """Восстановить геометрический параметр элемента по данным дампа.

    Args:
        attribute: требуемый параметр (имя ``RUS_*`` и единица измерения).
        row: строка сырого дампа IFC.
        ifc_class: IFC-класс элемента (по умолчанию — класс требований).

    Returns:
        Значение в единице требования или пустая строка, если восстановить
        параметр не удалось.
    """
    name = attribute.name
    if name not in GEOMETRY_ATTRIBUTES:
        return ""

    group = _class_group(ifc_class)
    unit = attribute.unit

    if name == "RUS_Length":
        return _length_value(_raw_length_mm(row, group), unit)
    if name == "RUS_Width":
        value = _raw_width_mm(row, group)
        if value is None and group == "slab":
            # Ширина плиты = площадь / длина.
            area = _raw_area_m2(row, group)
            length = _raw_length_mm(row, group)
            if area and length:
                value = area * 1_000_000.0 / length
        elif value is None and group == "opening":
            box = _opening_box(row)
            value = box[0] if box else None
        return _length_value(value, unit)
    if name == "RUS_Height":
        value = _raw_height_mm(row, group)
        if value is None and group == "opening":
            box = _opening_box(row)
            value = box[1] if box else None
        return _length_value(value, unit)
    if name == "RUS_Thickness":
        return _length_value(_thickness_mm(row, group), unit)
    if name == "RUS_Perimeter":
        return _length_value(_perimeter_mm(row, group), unit)
    if name == "RUS_Diameter":
        return _length_value(
            _first(row, (r"::Diameter$", r"Диаметр")),
            unit,
        )
    if name == "RUS_Area":
        return _area_value(_area_m2(row, group), unit)
    if name == "RUS_NetFootprintArea":
        return _area_value(_footprint_area_m2(row, group), unit)
    if name == "RUS_NetSideArea":
        return _area_value(_side_area_m2(row, group), unit)
    if name == "RUS_NetSurfaceArea":
        return _area_value(
            _first(row, (r"::(Net|Gross)SurfaceArea$", _BBOX_SURFACE)),
            unit,
        )
    if name == "RUS_Volume":
        return _volume_value(_volume_m3(row, group), unit)
    if name == "RUS_GrossVolume":
        return _volume_value(_gross_volume_m3(row, group), unit)
    return ""
