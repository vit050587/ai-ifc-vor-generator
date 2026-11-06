"""IFC elevations and deterministic checks of height-dependent TSN clauses.

All elevations here are metres relative to the building reference zero.
Storey names are explanatory labels, never grouping boundaries or height evidence.
"""
import math
import re
from collections import deque

HEIGHT_FIELDS = ('Этаж_IFC_ID','Здание_IFC_ID','Отметка_этажа_м','Отметка_земли_м',
                 'Источник_отметки_земли','Отметка_низа_м','Отметка_верха_м','Источник_отметок_работ',
                 'Причина_отметки_земли','Версия_высот_IFC')
HEIGHT_BANDS = ((0,15),(16,35),(36,55),(56,75),(76,105))


def _terrain_from_properties(building, scale):
    """Exact MGE elevation pair; do not guess units of an untyped IfcReal.

    Identical elevations imply zero offset regardless of their length unit.
    Otherwise require length types or explicit units for both properties.
    """
    from ifcopenshell.util.unit import get_prefix_multiplier
    pairs = []
    for rel in getattr(building, 'IsDefinedBy', ()) or ():
        pset = getattr(rel, 'RelatingPropertyDefinition', None)
        if pset is None or not pset.is_a('IfcPropertySet'):
            continue
        values = {}
        for prop in pset.HasProperties:
            if not prop.is_a('IfcPropertySingleValue') or prop.Name not in ('MGE_ElevationOfTerrain','MGE_ElevationOfRefHeight'):
                continue
            nominal = prop.NominalValue
            value = numeric(getattr(nominal,'wrappedValue',None))
            unit = prop.Unit
            unit_scale = None
            if unit is not None and unit.is_a('IfcSIUnit') and unit.UnitType == 'LENGTHUNIT':
                unit_scale = get_prefix_multiplier(unit.Prefix)
            elif unit is None and nominal and nominal.is_a() in ('IfcLengthMeasure','IfcPositiveLengthMeasure'):
                unit_scale = scale
            values[prop.Name] = (value,unit_scale)
        terrain = values.get('MGE_ElevationOfTerrain')
        reference = values.get('MGE_ElevationOfRefHeight')
        if terrain and reference and terrain[0] is not None and reference[0] is not None:
            if terrain[0] == reference[0] and terrain[1] == reference[1]:
                delta = 0.0
            elif terrain[1] is not None and reference[1] is not None:
                delta = terrain[0]*terrain[1] - reference[0]*reference[1]
            else:
                continue
            pairs.append((delta,f'{pset.Name}.MGE_ElevationOfTerrain − MGE_ElevationOfRefHeight'))
    if pairs and len({round(value,9) for value,_ in pairs}) == 1:
        return pairs[0]
    return None


def numeric(value):
    try:
        result = float(str(value).replace(',', '.'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _ancestor(entity, kind):
    pending, seen = deque([entity]), set()
    while pending:
        current = pending.popleft()
        if current.id() in seen:
            continue
        seen.add(current.id())
        if current.is_a(kind):
            return current
        for attr in ('ContainedInStructure', 'Decomposes', 'Nests'):
            for rel in getattr(current, attr, ()) or ():
                parent = getattr(rel, 'RelatingStructure', None) or getattr(rel, 'RelatingObject', None)
                if parent is not None:
                    pending.append(parent)
    return None


def ifc_fields(element, vertices=None):
    """Use IFC units, placements and explicit terrain/reference attributes.

    ElevationOfTerrain and ElevationOfRefHeight are both sea-level elevations;
    their difference is the terrain relative to the building zero. RefElevation
    of the site alone does not establish that difference.
    """
    import numpy as np
    from ifcopenshell.util.placement import get_local_placement
    from ifcopenshell.util.unit import calculate_unit_scale
    result = {'Версия_высот_IFC':1}
    try:
        scale = calculate_unit_scale(element.file)
        building = _ancestor(element, 'IfcBuilding')
        storey = _ancestor(element, 'IfcBuildingStorey')
        if storey is not None:
            result['Этаж_IFC_ID'] = storey.GlobalId
            if numeric(storey.Elevation) is not None:
                result['Отметка_этажа_м'] = float(storey.Elevation) * scale
        if building is None:
            return result
        result['Здание_IFC_ID'] = building.GlobalId
        terrain = numeric(getattr(building, 'ElevationOfTerrain', None))
        reference = numeric(getattr(building, 'ElevationOfRefHeight', None))
        pair = _terrain_from_properties(building,scale)
        if terrain is not None and reference is not None:
            delta = (terrain - reference) * scale
            if pair and abs(delta-pair[0]) > 1e-6:
                result['Причина_отметки_земли'] = 'Стандартные поля IFC и свойства MGE дают разные отметки земли'
            else:
                result['Отметка_земли_м'] = delta
                result['Источник_отметки_земли'] = 'IfcBuilding.ElevationOfTerrain − ElevationOfRefHeight'
        else:
            if pair:
                result['Отметка_земли_м'],result['Источник_отметки_земли'] = pair
            else:
                result['Причина_отметки_земли'] = 'Нет пары отметок земли и нуля здания с подтверждёнными единицами IFC'
        if vertices is None:
            return result
        if getattr(element, 'ObjectPlacement', None) is None or getattr(building, 'ObjectPlacement', None) is None:
            return result
        transform = np.linalg.inv(get_local_placement(building.ObjectPlacement)) @ get_local_placement(element.ObjectPlacement)
        points = np.asarray(vertices)
        z = (points @ transform[2, :3] + transform[2, 3]) * scale
        if len(z) and np.isfinite(z).all():
            result['Отметка_низа_м'] = float(z.min())
            result['Отметка_верха_м'] = float(z.max())
            result['Источник_отметок_работ'] = 'Геометрия IFC и ObjectPlacement относительно нуля здания'
    except Exception:
        # Missing evidence is kept missing, never substituted with zero.
        pass
    return result


def facts(row):
    nested = row.get('row') or {}
    return dict(nested, **{k:v for k,v in row.items() if k != 'row'})


def height_key(row):
    """Combine storeys with the same height-rule outcomes, within one building.

    Keep depths separate from heights so the combined extent does not cross
    terrain and falsely invalidate an otherwise confirmed coefficient.
    """
    row = facts(row)
    low, high, ground = (numeric(row.get(k)) for k in ('Отметка_низа_м','Отметка_верха_м','Отметка_земли_м'))
    base = (str(row.get('Здание_IFC_ID') or ''),ground)
    if None in (low,high,ground) or low > high:
        return base + ('unknown',)
    lo,hi = round(low-ground,6),round(high-ground,6)
    side = 'above' if lo >= 0 else 'below' if hi <= 0 else 'crossing_terrain'
    a = 0 if lo <= 0 <= hi else min(abs(lo),abs(hi))
    b = max(abs(lo),abs(hi))
    # The signature also separates different boundary-crossing cases without
    # creating a new group for every floor or exact elevation.
    signature = tuple('inside' if start <= a <= b <= end else
                      'before' if b < start else 'after' if a > end else 'overlap'
                      for start,end in HEIGHT_BANDS)
    return base + (side,signature)


def band(row):
    row = facts(row)
    low, high, ground = (numeric(row.get(k)) for k in ('Отметка_низа_м','Отметка_верха_м','Отметка_земли_м'))
    if None in (low, high, ground) or low > high:
        return 'unknown'
    lo, hi = round(low-ground,6), round(high-ground,6)
    # A structure crossing terrain includes both zero and its furthest end.
    a = 0 if lo <= 0 <= hi else min(abs(lo), abs(hi))
    b = max(abs(lo), abs(hi))
    for start, end in HEIGHT_BANDS:
        if start <= a <= b <= end:
            return f'{start}–{end} м'
    return 'crossing_or_outside'


def partitions(indices, rows):
    buckets = {}
    for index in indices:
        # Keep unmatched rows separate rather than assigning another element's height.
        key = height_key(rows[index]) if 0 <= index < len(rows) else ('missing', index)
        buckets.setdefault(key, []).append(index)
    return list(buckets.values())


def context(rows):
    values = [facts(row) for row in rows]
    result = {'storeys': list(dict.fromkeys(str(r.get('Этаж') or r.get('storey') or 'Не указан') for r in values))}
    bands = {band(r) for r in values}
    if len(bands) == 1 and not bands.intersection({'unknown','crossing_or_outside'}):
        result['height_band'] = next(iter(bands))
    buildings = {str(r.get('Здание_IFC_ID') or '') for r in values}
    grounds = {numeric(r.get('Отметка_земли_м')) for r in values}
    lows = [numeric(r.get('Отметка_низа_м')) for r in values]
    highs = [numeric(r.get('Отметка_верха_м')) for r in values]
    if len(buildings) == 1:
        result['building_id'] = next(iter(buildings))
    if len(grounds) == 1 and None not in grounds and len(buildings) == 1:
        result['ground_elevation_m'] = next(iter(grounds))
        result['ground_source'] = '; '.join(dict.fromkeys(str(r.get('Источник_отметки_земли') or 'IFC') for r in values))
    if lows and None not in lows and None not in highs and all(a <= b for a,b in zip(lows,highs)):
        result['min_elevation_m'], result['max_elevation_m'] = min(lows), max(highs)
        result['elevation_source'] = 'Геометрия IFC; весь объём элементов группы'
    return result


def label(ctx):
    storeys = ctx.get('storeys') or ['Не указан']
    text = 'Этажи: ' + ' / '.join(storeys[:4])
    if len(storeys) > 4:
        text += f' / … (всего {len(storeys)})'
    if ctx.get('height_band'):
        text = 'Высота от земли: ' + ctx['height_band'] + '; ' + text
    if ctx.get('building_id'):
        text += '; здание ' + ctx['building_id']
    if ctx.get('min_elevation_m') is not None:
        text += f"; отм. {ctx['min_elevation_m']:.3f}…{ctx['max_elevation_m']:.3f} м"
    else:
        text += '; отметки работ не определены'
    return text


def height_decisions(code, title, element_facts, candidates, decisions):
    """Replace model decisions only for clauses about height from terrain.

    Candidates and numerical factors remain those of the loaded collection.
    A band-crossing element is not prorated by its bounding-box height: that
    would invent a distribution of concrete, rebar and surface quantities.
    """
    from .tsn_coeffs import coef_text
    ctx = element_facts.get('height_context') or context([element_facts])
    low, high, ground = (numeric(ctx.get(k)) for k in ('min_elevation_m','max_elevation_m','ground_elevation_m'))
    replacements = {}
    for doc,item in candidates:
        condition = item.get('condition') or ''
        if 'от поверхности земли' not in condition.lower() or 'высот' not in condition.lower():
            continue
        match = re.search(r'от\s+(\d+(?:[.,]\d+)?)\s+до\s+(\d+(?:[.,]\d+)?)\s*м', condition)
        if not match:
            continue
        start, end = map(lambda s: float(s.replace(',','.')), match.groups())
        decision, why = 'неясно', 'В IFC отсутствуют подтверждённые отметки работ или земли относительно нуля здания'
        if None not in (low, high, ground) and low <= high:
            lo, hi = round(low-ground,6), round(high-ground,6)
            a = 0 if lo <= 0 <= hi else min(abs(lo),abs(hi))
            b = max(abs(lo),abs(hi))
            why = f'Работы на {lo:.3f}…{hi:.3f} м от земли (земля {ground:.3f} м); {ctx.get("ground_source", "IFC")}'
            if start <= a <= b <= end:
                decision = 'да'
            elif b < start or a > end:
                decision = 'нет'
            else:
                why += '; элемент пересекает границу диапазона, требуется разделение объёмов'
        if re.search(r'при высоте здания|при высоте зданий', title, re.I):
            decision = 'неясно'
            why = 'Высота указана в самой расценке; нужно проверить допустимость дополнительной поправки'
        replacements[(doc,str(item.get('clause')))] = {
            'doc':doc,'clause':item.get('clause'),'condition':condition,'decision':decision,'why':why,
            'coef':coef_text(item),
            'coefficients':item.get('coefficients') or {},'source_page':item.get('source_page'),
            'resource':item.get('applies_to_resource'), 'height_check':True}
    if replacements and None not in (low,high,ground) and low <= high:
        distance = max(abs(low-ground),abs(high-ground))
        if distance > 15 and all(d['decision'] == 'нет' for d in replacements.values()):
            for d in replacements.values():
                d['decision'] = 'неясно'
                d['why'] += '; отметки не попали в указанные интервалы: требуется проверить границы и расценку'
    return [d for d in decisions if (d.get('doc'),str(d.get('clause'))) not in replacements] + list(replacements.values())


def enrich_frame(frame, ifc_path):
    """Refresh old session rows by GlobalId, preserving order and quantities."""
    if frame.empty or 'GlobalId' not in frame or not ifc_path:
        return frame
    if 'Версия_высот_IFC' in frame and all(numeric(v) == 1 for v in frame['Версия_высот_IFC']):
        return frame
    import ifcopenshell
    from .ifc_raw_dump import enrich_work_height
    model = ifcopenshell.open(str(ifc_path))
    enriched = []
    for gid in frame['GlobalId']:
        fields = {}
        try:
            element = model.by_guid(str(gid))
            if element is not None:
                fields = enrich_work_height({},element)
        except (RuntimeError, ValueError):
            pass
        enriched.append(fields)
    result = frame.copy()
    for name in HEIGHT_FIELDS:
        # Drop stale values for unmatched GlobalIds instead of retaining another source's heights.
        result[name] = [fields.get(name) for fields in enriched]
    return result
