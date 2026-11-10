"""Normalize model response syntax; physical column choices still need validation."""
from copy import deepcopy
import re


def _pick(mapping, key, aliases=(), default=None, required=False):
    found = [mapping[k] for k in (key,*aliases) if k in mapping]
    if found and any(value != found[0] for value in found[1:]):
        raise ValueError(f'Противоречивые значения поля {key}')
    if found:
        return found[0]
    if required:
        raise ValueError(f'Не указано обязательное поле {key}')
    return default


def _index(value):
    # Convert JSON string indices, but never guess a one-based coordinate.
    return int(value) if isinstance(value,str) and re.fullmatch(r'\d+',value.strip()) else value


def normalize(result, kind, table):
    from .schema import header, infer_vor, norm, ESTIMATE_FIELDS
    if not isinstance(result,dict):
        raise ValueError('Модель должна вернуть объект схемы')
    result = deepcopy(result)
    roots = [result[key] for key in ('schema','result','mapping') if isinstance(result.get(key),dict)]
    essential = ('columns','quantity_columns','name','name_col') if kind=='vor' else ('fields',)
    if not any(key in result for key in essential) and roots:
        if len(roots)!=1:
            raise ValueError('В ответе несколько схем, нельзя выбрать одну')
        result = roots[0]
    known = header(table)
    start = _pick(result,'start',('data_start_row','start_row'),default=known['start'] if known else None)
    if kind!='vor':
        fields = _pick(result,'fields',required=True)
        if not isinstance(fields,dict):
            raise ValueError('Поле fields должно содержать столбцы сметы')
        required = {'code','name','unit','quantity','total'}
        fields = {key:_index(_pick(fields,key,required=key in required)) for key in ESTIMATE_FIELDS}
        return dict(start=_index(start),fields=fields)
    name = _pick(result,'name',('name_col',),default=known['name'] if known else None)
    unit = _pick(result,'unit',('unit_col',))
    pos = _pick(result,'pos',('pos_col','position_col'))
    start = _index(start)
    if (unit is None or pos is None) and isinstance(start,int) and not isinstance(start,bool) and 0<=start<len(table.rows):
        # Recover a missing unit/position field only from an explicit header.
        candidates = []
        positions = []
        for ci in range(len(table.rows[0])):
            labels = [norm(row[ci]) for row in table.expanded[:start]]
            if any(value in ('едизм','единицаизмерения','мера') for value in labels):
                candidates.append(ci)
            if any(value in ('пп','поз','позиция','номер') for value in labels) or any(str(row[ci] or '').strip().startswith('№') for row in table.expanded[:start]):
                positions.append(ci)
        if unit is None and len(candidates)==1:
            unit = candidates[0]
        if pos is None and len(positions)==1:
            pos = positions[0]
    columns = _pick(result,'columns',('quantity_columns',),required=True)
    if not isinstance(columns,list) or not columns:
        raise ValueError('Не указаны столбцы величин columns')
    inferred = infer_vor(table)
    known_columns = {c['now']:c for c in inferred['columns']} if inferred else {}
    output = []
    for column in columns:
        if not isinstance(column,dict):
            raise ValueError('Описание величины columns должно быть объектом')
        now = _index(_pick(column,'now',('current_col','quantity_col'),required=True))
        labels = _pick(column,'label_cells',('header_cells',),default=[])
        if isinstance(labels,list):
            labels = [[_index(v) for v in cell] if isinstance(cell,list) else cell for cell in labels]
        role = _pick(column,'role')
        if role is None:
            if isinstance(now,int) and not isinstance(now,bool) and now in known_columns:
                role = known_columns[now]['role']
            elif len(columns)==1 and unit is not None:
                role = 'quantity'
            else:
                raise ValueError('Не указана роль величины role: итог, составляющая или величина единицы')
        measure_unit = _pick(column,'unit',default='')
        if isinstance(measure_unit,str):
            measure_unit = {'м³':'м3','м²':'м2','шт.':'шт'}.get(measure_unit.strip(),measure_unit.strip())
        output.append(dict(now=now,was=_index(_pick(column,'was',('previous_col',))),
                           delta=_index(_pick(column,'delta',('delta_col',))),unit=measure_unit,
                           label_cells=labels,role=role))
    return dict(name=_index(name),unit=_index(unit),pos=_index(pos),start=start,columns=output)
