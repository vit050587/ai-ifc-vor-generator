"""Explain the rectangular column/pylon checks quoted in the working-group file.

Geometry takes priority when all conditions are confirmed. Otherwise the
existing MSSK catalogue supplies the classification. Element height and
world-axis bounding boxes are not section dimensions.
"""
import math
import re

LABELS = {'wall': 'Стена', 'column': 'Колонна', 'pylon': 'Пилон'}


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(str(value).strip().replace(',', '.'))
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def _first(row, keys):
    for key in keys:
        value = _positive(row.get(key))
        if value is not None:
            return value, key
    return None, ''


def _assess_geometry(row, model_kind=None):
    """Return auditable data for walls/columns, or None for other elements.

    Raw dump QTO and RUS lengths are millimetres. Clear floor height is accepted
    only from explicitly named properties with an explicit unit. Round/irregular
    sections require a separate check and are never treated as rectangles.
    """
    cls = str(row.get('IfcClass') or row.get('Тип элемента') or '')
    text = ' '.join(str(row.get(k) or '') for k in
                    ('Name', 'Имя', 'ObjectType', 'Элемент_ЦИМ', 'RUS_MSSK_Element_Name'))
    named = 'pylon' if re.search(r'пилон', text, re.I) else 'column' if re.search(r'колонн', text, re.I) else 'wall'
    if cls not in ('IfcWall', 'IfcWallStandardCase', 'IfcWallElementedCase', 'IfcColumn', 'IfcColumnStandardCase'):
        return None
    if re.search(r'парапет|приям|фундамент', text, re.I):
        return None
    model_kind = model_kind or ('column' if 'Column' in cls or named != 'wall' else 'wall')
    result = dict(guid=str(row.get('GlobalId') or ''), name=str(row.get('Name') or row.get('Имя') or ''),
                  storey=str(row.get('Этаж') or row.get('RUS_Level') or ''), model_kind=model_kind,
                  status='unverified', candidates=[], sources=[], a_mm=None, b_mm=None,
                  clear_height_mm=None, section_ratio=None, height_ratio=None, load_bearing=None)
    notes = []
    bearing = set()
    for key, value in row.items():
        if re.search(r'(?:^|::)(?:LoadBearing|Несущая|Несущий|Несущая конструкция)$', str(key), re.I):
            value = str(value).strip().lower()
            if value in ('true', '1', 'да', 'yes'):
                bearing.add(True)
                result['sources'].append(str(key))
            elif value in ('false', '0', 'нет', 'no'):
                bearing.add(False)
                result['sources'].append(str(key))
    if len(bearing) == 1:
        result['load_bearing'] = bearing.pop()
    if result['load_bearing'] is False:
        result.update(status='not_applicable', message='Элемент обозначен как ненесущий; критерий несущих колонн и пилонов не применяется.')
        return result
    if result['load_bearing'] is None:
        notes.append('Несущая роль не подтверждена' + (': противоречащие признаки' if bearing else ''))

    if re.search(r'кругл|кругов|оваль|тавров|двутавр|сечени\w*\s*(?:Ø|⌀)|[Ø⌀]', text, re.I):
        result['message'] = 'Прямоугольное сечение не подтверждено; проверка по двум размерам неприменима.'
        return result
    if 'Wall' in cls:
        first, fs = _first(row, ('QTO::Qto_WallBaseQuantities::Width', 'RUS_Thickness', 'RUS_Width'))
        second, ss = _first(row, ('QTO::Qto_WallBaseQuantities::Length', 'RUS_Length'))
    else:
        first, fs = _first(row, ('QTO::Qto_ColumnBaseQuantities::Width', 'RUS_Width'))
        second, ss = _first(row, ('QTO::Qto_ColumnBaseQuantities::Depth', 'RUS_Height'))
    # Names supply a nominal section only if the numerical section is absent.
    if first is None or second is None:
        match = re.search(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)\s*мм\b', text, re.I)
        if match:
            first, second = (_positive(v) for v in match.groups())
            fs = ss = 'Номинальное сечение из наименования, мм'
            notes.append('Сечение взято из наименования; фактическая геометрия не подтверждена')
    if first is None or second is None:
        notes.append('Недостаточно размеров поперечного сечения; габариты и высота элемента не подставляются')
        result['message'] = '. '.join(notes)
        return result
    a, b = sorted((first, second))
    result.update(a_mm=a, b_mm=b, section_ratio=b/a)
    result['sources'].extend(dict.fromkeys((fs, ss)))
    height, hs = _first(row, ('Высота_этажа_в_свету_мм', 'RUS_ClearStoreyHeight',
                              'Свойство::Geometry::ClearStoreyHeight_mm'))
    if height is None:
        height_m, hs = _first(row, ('Высота_этажа_в_свету_м', 'Свойство::Geometry::ClearStoreyHeight_m'))
        height = height_m * 1000 if height_m is not None else None
    result['clear_height_mm'] = height
    result['height_ratio'] = height/b if height is not None else None
    if hs:
        result['sources'].append(hs)
    else:
        notes.append('Высота этажа в свету неизвестна; условие hэт/b > 3 не проверено')
    ratio = b/a
    candidates = []
    if ratio <= 2.5 or height is not None and height/b > 3:
        candidates.append('column')
    if 2.5 <= ratio <= 4:
        candidates.append('pylon')
    if ratio > 4:
        candidates.append('wall')
    result['candidates'] = candidates
    notes.insert(0, f'b/a = {ratio:g}' + (f'; hэт/b = {height/b:g}' if height is not None else ''))
    expected = {'column' if c == 'pylon' else c for c in candidates}
    if len(expected) > 1:
        result['status'] = 'ambiguous'
        notes.append('Критерий hэт/b и критерий вытянутости сечения дают разные подразделы; требуется проверка проектировщиком')
    elif result['load_bearing'] is True and height is not None and fs != 'Номинальное сечение из наименования, мм':
        if model_kind not in expected or named != 'wall' and named not in candidates:
            result['status'] = 'conflict'
            notes.append('Классификация модели расходится с геометрическими критериями')
        else:
            result['status'] = 'confirmed'
    result['message'] = '. '.join(notes)
    return result


def assess(row, model_kind=None):
    result = _assess_geometry(row, model_kind)
    if result is None:
        return None
    from .check_model.requirements import element_mssk_code
    from .check_model.grouping import _mssk_kinds
    code = element_mssk_code(row)
    mssk_kind = _mssk_kinds().get(code) if code else None
    result.update(mssk_code=code, mssk_kind=mssk_kind)
    if result['status'] in ('confirmed', 'conflict'):
        result['effective_kind'] = 'column' if result['candidates'][0] == 'pylon' else result['candidates'][0]
        result['basis'] = 'geometry'
        result['basis_label'] = 'По геометрии'
        if mssk_kind and mssk_kind != result['effective_kind']:
            result['status'] = 'conflict'
            result['message'] += '. Геометрическая классификация отличается от кода МССК'
    elif mssk_kind:
        result['effective_kind'] = mssk_kind
        result['basis'] = 'mssk'
        result['basis_label'] = 'По коду МССК'
    else:
        result['effective_kind'] = result['model_kind']
        result['basis'] = 'model'
        result['basis_label'] = 'По IFC и наименованию: код МССК не определён'
    result['message'] += f'. Итог: {LABELS.get(result["effective_kind"], result["effective_kind"])}. {result["basis_label"]}'
    return result


def quantity_row(row, check):
    """Translate section/vertical dimensions when IFC class and final kind differ.

    Original QTO and GUID remain intact. A clear storey height is never used as
    the height of an element for quantity calculations.
    """
    if not check or not check.get('a_mm') or not check.get('b_mm'):
        return row
    cls = str(row.get('IfcClass') or row.get('Тип элемента') or '')
    kind = check['effective_kind']
    prepared = dict(row)
    if kind == 'column':
        height_keys = ('QTO::Qto_WallBaseQuantities::Height', 'RUS_Height') if 'Wall' in cls else ('QTO::Qto_ColumnBaseQuantities::Height', 'RUS_Length')
        height, _ = _first(row, height_keys)
        if height is not None:
            prepared.update(RUS_Width=check['a_mm'], RUS_Height=check['b_mm'], RUS_Length=height)
            prepared['_classification_section_mm'] = (check['a_mm'], check['b_mm'])
    elif 'Column' in cls and kind == 'wall':
        height, _ = _first(row, ('QTO::Qto_ColumnBaseQuantities::Height', 'RUS_Length'))
        if height is not None:
            prepared.update(RUS_Thickness=check['a_mm'], RUS_Width=check['a_mm'],
                            RUS_Height=height, RUS_Length=check['b_mm'])
    return prepared


