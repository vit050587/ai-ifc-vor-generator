"""Contact-based formwork measures; mesh skin is only an estimate of contact."""
import re
from .ifc_quantities import number


def contact_area(row, without_openings=False, kind=None):
    key = 'ТСН::Площадь_контакта_без_проёмов_м2' if without_openings else 'ТСН::Площадь_контакта_опалубки_м2'
    for candidate in (key, 'Свойство::' + key):
        value = number(row.get(candidate))
        if value is not None:
            return value, candidate
    if without_openings:
        area, source = contact_area(row, kind=kind)
        if area is not None:
            return area, 'Оценка площади контакта без проёмов: ' + source + '; вычет проёмов требует проверки'
        from .wall_formwork import wall_projection
        area, source = wall_projection(row)
        if area is not None:
            return area, 'Оценка площади контакта по заменителю проекции: ' + source
        return None, ''
    for key, raw in row.items():
        label = str(key)
        if (re.search(r'formwork|опалуб', label, re.I) and re.search(r'area|площад', label, re.I)
                and not re.search(r'геометр|qto_geom|мм2|mm2', label, re.I)):
            value = number(raw)
            if value is not None:
                return value, label
    for key in ('QTO_geom::Площадь_опалубки_м2', 'Геометрия_Площадь_опалубки_м2'):
        value = number(row.get(key))
        if value is not None:
            return value, 'Оценка площади контакта: ' + key + '; примыкания и условия опирания требуют проекта'
    return _dimension_contact(row, kind)


def _dimension_contact(row, kind=None):
    """One fallback for normalized, raw, preview and VOR element records."""
    from .check_model.geometry import (_raw_length_mm, _raw_width_mm, _raw_height_mm,
                                       _perimeter_mm, _thickness_mm)
    prepared = dict(row)
    cls = str(row.get('IfcClass') or row.get('Тип элемента') or '')
    name = str(row.get('Name') or row.get('Имя') or '')
    if kind is None:
        kind = ('column' if cls == 'IfcColumn' or re.search(r'пилон|колонн', name, re.I) else
                'beam' if cls == 'IfcBeam' or re.search(r'балк|ригел', name, re.I) else
                'foundation' if cls == 'IfcFooting' or re.search(r'фундамент|приям', name, re.I) else None)
    if kind in ('foundation_slab', 'crane_foundation'):
        kind = 'foundation'
    if kind not in ('column', 'beam', 'foundation'):
        return None, ''
    family = {'column': 'Column', 'beam': 'Beam', 'foundation': 'Slab'}[kind]
    for key, raw in row.items():
        match = re.search(r'(Length|Width|Height|Depth|Perimeter)_мм$', str(key))
        if match:
            prepared.setdefault(f'QTO::Qto_{family}BaseQuantities::{match.group(1)}', raw)
    def metric(label, function, group):
        value = number(row.get(label))
        return value if value is not None else function(prepared, group)
    group = 'slab' if kind == 'foundation' else kind
    length = metric('RUS_Length', _raw_length_mm, group)
    width = metric('RUS_Width', _raw_width_mm, group)
    height = metric('RUS_Height', _raw_height_mm, group)
    section = re.search(r'(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)', name)
    if section:
        width, height = (float(v.replace(',', '.')) for v in section.groups())
    if kind == 'column':
        if length and width and height:
            return 2 * (width + height) * length / 1e6, 'Оценка площади контакта: периметр сечения × высота; примыкания требуют проекта'
        for key, raw in row.items():
            if re.search(r'OuterSurfaceArea|NetSurfaceArea|GrossSurfaceArea|RUS_NetSideArea', str(key)):
                value = number(raw)
                if value is not None:
                    return value, f'Оценка площади контакта: {key}; нерабочие грани требуют проверки'
    elif kind == 'beam':
        from .ifc_quantities import resolve_volume
        underside = number(row.get('QTO_geom::Площадь_низа_м2', row.get('Геометрия_Площадь_низа_м2')))
        volume = resolve_volume(row).value
        if length and underside and volume:
            # The same orientation fallback used by the model preview.
            return underside + 2 * volume * (length / 1000) / underside, 'Оценка площади контакта балки: низ + две стороны по объёму; примыкания требуют проекта'
        if length and width and height:
            b, h = sorted((width, height))
            return length * (b + 2 * h) / 1e6, 'Оценка площади контакта балки: низ + две стороны; примыкания требуют проекта'
    else:
        perimeter = number(row.get('RUS_Perimeter'))
        perimeter = perimeter * 1000 if perimeter is not None else _perimeter_mm(prepared, 'slab')
        thickness = metric('RUS_Thickness', _thickness_mm, 'slab')
        if not thickness:
            match = re.search(r'(\d+(?:[.,]\d+)?)\s*мм', name)
            thickness = float(match.group(1).replace(',', '.')) if match else None
        if perimeter and thickness:
            return perimeter * thickness / 1e6, 'Оценка площади контакта: периметр × толщина; внутренние контуры и примыкания требуют проверки'
    return None, ''
