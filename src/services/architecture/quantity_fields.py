"""ARCH-only unit and measured opening-area interpretation."""
import math
import re
from pathlib import Path


def positive(value):
    try:
        value = float(str(value).replace(',', '.'))
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def is_piece_unit(unit):
    return bool(re.match(r'^\s*(?:\d+(?:[.,]\d+)?\s*)?шт(?:\.|\s|$)', str(unit), re.I))


def opening_area(row, ifc_class, quantities):
    result = dict(quantities or {})
    if ifc_class not in ('IfcDoor', 'IfcWindow') or positive(result.get('area_m2')):
        return result
    width, height = positive(row.get('АР_Габаритная_ширина_мм')), positive(row.get('АР_Габаритная_высота_мм'))
    if width and height:
        area = width * height / 1_000_000
        source = 'АР_Габаритная_ширина_мм × АР_Габаритная_высота_мм / 1000000'
    else:
        scale = positive(row.get('АР_Масштаб_длины_IFC_в_метры'))
        width, height = positive(row.get('OverallWidth')), positive(row.get('OverallHeight'))
        if not (scale and width and height):
            result.pop('area_m2', None)
            return result
        area = width * height * scale**2
        source = 'OverallWidth × OverallHeight × АР_Масштаб_длины_IFC_в_метры²'
    result['area_m2'] = area
    result['_sources'] = dict(result.get('_sources') or {}, area_m2=source)
    return result


def source_length_scale(folder):
    """Read the actual IFC unit for old sessions whose raw dump has no unit stamp."""
    for directory in (Path(folder), Path(folder) / 'original', Path(folder).parent, Path(folder).parent / 'original'):
        paths = list(directory.glob('*.ifc'))
        if len(paths) == 1:
            import ifcopenshell
            from ifcopenshell.util.unit import calculate_unit_scale
            return float(calculate_unit_scale(ifcopenshell.open(str(paths[0]))))
        if len(paths) > 1:
            return None
    return None
