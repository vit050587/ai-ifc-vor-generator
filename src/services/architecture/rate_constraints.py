"""Hard compatibility and explicit, catalogue-linked quantity adjustments."""
import re
from .table_matching import has


def layer_count(text):
    match = re.search(r'(?<!\d)(\d+)\s*(?:[-–]\s*х?\s*)?(?:сло[йя]\b|сло[её]в\b)', text, re.I)
    if match:
        return int(match[1])
    for value, words in ((1, ('один слой', 'однослойн', 'первого слоя', 'первый слой')), (2, ('два слоя', 'двухслойн')),
                         (3, ('три слоя', 'трехслойн', 'трёхслойн')), (4, ('четыре слоя', 'четырехслойн'))):
        if any(word in text.lower() for word in words):
            return value
    return None


def thickness(text):
    match = re.search(r'толщин\w*\s*(?:(?:слоя|подготовки|стяжки)\s+)?(\d+(?:[.,]\d+)?)\s*мм', text, re.I)
    return float(match[1].replace(',', '.')) if match else None


def is_adjustment(work):
    return bool(re.search(r'^\s*(?:добавлять|добавляется|добавляются|при изменении|исключать|на каждый дополнительн)',
                          str(work.get('title') or ''), re.I))


def linked_adjustments(base, pool):
    code = re.escape(str(base.get('pressmark') or ''))
    result = []
    for work in pool:
        if not is_adjustment(work):
            continue
        title = str(work.get('title') or '')
        references = re.search(r'(?:к норм(?:е|ам)|к позици(?:и|ям))\s+(.+)', title, re.I)
        if references and re.search(r'(?<![\d.-])'+code+r'(?![-\d])', references[1]):
            result.append(work)
    return result



def adjustments(scope, base, pool, element):
    """Return (rate, repetitions, calculation) using explicit source dimensions."""
    source = scope['evidence']
    title = str(base.get('title') or '')
    requested, priced = layer_count(source), layer_count(title)
    linked = linked_adjustments(base, pool)
    if requested and priced and requested > priced:
        candidates = [w for w in linked if re.search(r'(?:дополнительный|последующий) слой', str(w.get('title') or ''), re.I)]
        if has(source, 'битум'):
            candidates = [w for w in candidates if has(str(w.get('title') or ''), 'битум')]
        if len(candidates) == 1:
            return [(candidates[0], requested-priced, f'Слоёв по описанию {requested} − учтено нормой {priced}')]
    field = scope.get('thickness_field') or ''
    try:
        depth = float(str((element.get('row') or {}).get(field)).replace(',', '.'))
    except (TypeError, ValueError):
        depth = None
    included = thickness(title)
    if depth and included and depth > included and (has(source, 'стяжк') or (scope.get('role') == 'adhesive' and has(title, 'кле'))):
        candidates = [(w, re.search(r'на кажд(?:ый|ые)\s+(\d+(?:[.,]\d+)?)\s*мм', str(w.get('title') or ''), re.I)) for w in linked]
        candidates = [(w, float(m[1].replace(',', '.'))) for w, m in candidates if m and float(m[1].replace(',', '.')) > 0]
        if len(candidates) == 1:
            addon, step = candidates[0]
            return [(addon, (depth-included)/step, f'Толщина из {field}: ({depth:g} − {included:g}) / {step:g} мм')]
    return []


def compatible(scope, work, pool, element):
    source, title = scope['evidence'], str(work.get('title') or '')
    if is_adjustment(work):
        return False
    kind = scope.get('kind')
    wall_rate, ceiling_rate = has(title, 'стен'), has(title, 'потолк', 'потолоч')
    if kind == 'wall' and ceiling_rate and not wall_rate:
        return False
    if kind == 'ceiling' and wall_rate and not ceiling_rate:
        return False
    from .table_matching import substrate
    base = scope.get('substrate') or substrate(element)
    if has(title, 'пазогреб') and base != 'pazogreb':
        return False
    if has(source, 'бетоноконтакт') and has(title, 'глубок'):
        return False
    if scope.get('role') == 'paint':
        if has(source, 'маслян', 'эмаль') and has(title, 'водно-дисперс'):
            return False
        if base == 'wood' and has(title, 'штукатурк', 'сборн') and not has(title, 'дерев'):
            return False
        plastered = has(str(element.get('material') or ''), 'штукатур')
        if plastered and has(title, 'сборн'):
            return False
        if base == 'gypsum_board' and not plastered and has(title, 'штукатурк'):
            return False
    code = str(work.get('pressmark') or '')
    if scope.get('role') == 'adhesive':
        return False  # Glue is included only with an explicit parent rate.
    if code.startswith(('3.12-31-', '3.12-57-')) and (has(source, 'бронирован') or
            not has(source, 'сталь', 'металл', 'оцинк', 'фальц')):
        return False
    if code.startswith('3.11-26-') and has(source, 'керамзит', 'бетон', 'плитк', 'керамогранит', 'плен', 'плён', 'клей'):
        return False
    if code.startswith(('3.11-18-', '3.11-36-', '3.15-171-')) and has(source, 'керамзит', 'бетон', 'плен', 'плён', 'клей', 'винил', 'линолеум'):
        return False
    if has(source, 'керамзит') and not has(title, 'керамзит', 'засыпн'):
        return False
    if has(source, 'плит', 'матов', 'минераловат', 'базальт') and has(title, 'керамзит', 'засыпн', 'щебн'):
        return False
    if has(source, 'пеноплэкс', 'пенополистирол') and has(title, 'минераловат', 'стекловолок', 'древесноволок'):
        return False
    if (has(source, 'минераловат', 'минеральн') or 'базальт' in source.lower()) and has(title, 'пенопласт', 'полистирол', 'пеностекл', 'древесноволок'):
        return False
    # Select the actual tile size band, rather than a random row of the table.
    tile = re.search(r'(\d+)\s*[xх×]\s*(\d+)(?:\s*[xх×]\s*\d+)?\s*мм', source, re.I)
    if tile and has(source, 'плитк', 'керамогранит'):
        area = int(tile[1]) * int(tile[2]) / 1_000_000
        band = re.search(r'площадь\w*\s+(?:одной\s+)?плитки\s+(?:свыше\s+([\d.,]+)\s*(?:м2)?\s*)?до\s+([\d.,]+)', title, re.I)
        if band and (area > float(band[2].replace(',', '.')) or
                     (band[1] and area <= float(band[1].replace(',', '.')))):
            return False

    if has(source, 'стяжк') and has(source, 'сборн', 'сух', 'лист') and has(title, 'цементно-песчан', 'асфальтобетон', 'раствор'):
        return False
    if has(source, 'стяжк'):
        def grade(text):
            match = re.search(r'(?<![а-яa-z])[мm]\s*(\d{2,3})\b|марки\s+(\d{2,3})\b', text, re.I)
            return int(match[1] or match[2]) if match else None
        if grade(source) and grade(title) and grade(source) != grade(title):
            return False
    requested, priced = layer_count(source), layer_count(title)
    if requested and priced and requested != priced:
        return bool(requested > priced and adjustments(scope, work, pool, element))
    try:
        depth = float(str((element.get('row') or {}).get(scope.get('thickness_field'))).replace(',', '.'))
    except (TypeError, ValueError):
        depth = None
    included = thickness(title)
    if depth and included and has(source, 'стяжк') and depth != included:
        return bool(depth > included and adjustments(scope, work, pool, element))
    return True
