"""TSN 2001.3-6 §2.8: one wall projection, without opening deductions."""
from .ifc_quantities import number


def wall_projection(row):
    """Return (m², source); net area and contact/skin area are ineligible."""
    def first(keys):
        for key in keys:
            value = number(row.get(key))
            if value is not None:
                return value, key
        return None, ''

    area, source = first((
        'QTO::Qto_WallBaseQuantities::GrossSideArea',
        'QTO_Qto_WallBaseQuantities_Площадь_GrossSideArea_м2',
        'Площадь_GrossSideArea_м2', 'RUS_GrossSideArea',
    ))
    if area is not None:
        return area, source
    length, ls = first(('QTO::Qto_WallBaseQuantities::Length', 'RUS_Length',
                        'QTO_Qto_WallBaseQuantities_Длина_Length_мм', 'Длина_Length_мм'))
    height, hs = first(('QTO::Qto_WallBaseQuantities::Height', 'RUS_Height',
                        'QTO_Qto_WallBaseQuantities_Длина_Height_мм', 'Длина_Height_мм',
                        'QTO_Qto_WallBaseQuantities_Высота_Height_мм', 'Высота_Height_мм'))
    if length is not None and height is not None and length > 0 and height > 0:
        return length * height / 1e6, f'Оценка проекции одной стороны: {ls} × {hs} / 1000000; без вычета проёмов'
    net, ns = first(('QTO::Qto_WallBaseQuantities::NetSideArea',
                     'QTO_Qto_WallBaseQuantities_Площадь_NetSideArea_м2',
                     'Площадь_NetSideArea_м2', 'RUS_NetSideArea', 'Площадь, м2'))
    if net is None:
        volume, vs = first(('Количество::Объём_м3', 'QTO::Qto_WallBaseQuantities::NetVolume',
                            'RUS_Volume', 'Объём, м3'))
        thickness, ts = first(('QTO::Qto_WallBaseQuantities::Width', 'RUS_Thickness',
                               'Длина_Width_мм', 'QTO_bbox::Ширина_мм'))
        if thickness is None:
            import re
            match = re.search(r'(\d+(?:[.,]\d+)?)\s*мм', str(row.get('Name') or row.get('Имя') or ''), re.I)
            if match:
                thickness, ts = float(match.group(1).replace(',', '.')), 'толщина из имени, мм'
        if volume is not None and thickness:
            net, ns = volume / (thickness / 1000), f'{vs} / ({ts} / 1000)'
    if net is not None:
        openings, source = first(('QTO_geom::Площадь_проёмов_стены_м2',
                                  'Геометрия_Площадь_проёмов_стены_м2'))
        if openings is not None:
            return net + openings, f'Оценка проекции: {ns} + {source}; проёмы восстановлены'
        return net, f'Оценка проекции по заменителю {ns}; площадь проёмов неизвестна, требуется проверка'
    return None, ''
