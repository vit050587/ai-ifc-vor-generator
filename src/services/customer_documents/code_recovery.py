"""Second deterministic VOR pass after an unavailable or invalid LLM answer."""
from dataclasses import replace
from .readers import clean
from .schema import infer_vor, norm, units, is_number


_ALIASES = {
    'описание':'Наименование', 'описаниеработ':'Наименование', 'видработ':'Наименование',
    'видыработ':'Наименование', 'наименованиеконструкций':'Наименование конструкции',
    'мера':'Ед. изм.', 'единицыизмерения':'Ед. изм.',
    'факт':'Объем', 'фактическийобъем':'Объем', 'выполнено':'Объем',
    'итоговыйобъем':'Объем', 'актуальныйобъем':'Стало',
}


def _relaxed_schema(table):
    def normalized(rows):
        result=[]
        data=False
        for row in rows:
            if any(units(cell) and len(clean(cell))<15 for cell in row) and any(is_number(cell) for cell in row):
                data=True
            result.append([_ALIASES.get(norm(cell),cell) if not data else cell for cell in row])
        return result
    schema=infer_vor(replace(table,rows=normalized(table.rows),expanded=normalized(table.expanded)))
    if schema:
        # Keep the actual source header in quantity provenance.
        schema['paths']=[list(dict.fromkeys(clean(row[ci]) for row in table.expanded[:schema['start']] if clean(row[ci])))
                         for ci in range(len(table.rows[0]))]
    return schema


def _legacy_schema(table, records):
    """Use old parser choices only when the original header confirms the column."""
    from ..vor_parser import choose_quantity, quantity_kind
    from ..vor_parser import QuantityColumnRequired
    matches=[]
    for ri,row in enumerate(table.rows):
        if len(row)<4 or not clean(row[1]) or not units(row[2]):
            continue
        eligible=[r for r in records if r.get('page')==table.page and clean(r.get('name'))==clean(row[1])
                  and clean(r.get('unit'))==clean(row[2]) and clean(r.get('pos'))==clean(row[0])]
        if len(eligible)==1:
            matches.append((ri,eligible[0]))
    if not matches:
        return None
    start=min(ri for ri,_ in matches)
    paths=[list(dict.fromkeys(clean(row[ci]) for row in table.expanded[:start] if clean(row[ci])))
           for ci in range(len(table.rows[0]))]
    candidates=[]
    for ci,path in enumerate(paths):
        if ci<3:continue
        current=next((label for label in reversed(path) if quantity_kind(label)),None)
        if current:candidates.append((ci,current,quantity_kind(current)))
    try:
        chosen=choose_quantity(candidates)[0]
    except QuantityColumnRequired:
        return None
    if any(record.get('quantity_column')!=chosen for _,record in matches):
        return None
    old=next((ci for ci,path in enumerate(paths) if any(norm(label)=='было' for label in path)),None)
    return dict(name=1,unit=2,pos=0,start=start,paths=paths,
                columns=[dict(now=chosen,was=old,delta=None,unit='',label='',role='quantity')])


def recover(table, initial_schema=None, legacy_loader=None):
    from .service import _parse_vor_table
    best=([],[],list(range(len(table.rows))))
    for schema in (initial_schema,_relaxed_schema(table)):
        if not schema:continue
        parsed,issues,missing=_parse_vor_table(table,schema)
        if parsed and (not best[0] or len(missing)<len(best[2])):
            best=(parsed,issues,missing)
        if parsed and not missing:
            return parsed,issues,missing
    if legacy_loader and table.page is not None:
        try:
            schema=_legacy_schema(table,legacy_loader())
            if schema:
                parsed,issues,missing=_parse_vor_table(table,schema)
                if parsed and (not best[0] or len(missing)<len(best[2])):
                    best=(parsed,issues,missing)
        except (ValueError,OSError,RuntimeError):
            pass
    return best
