"""Physical thicknesses and declared grouping limits, always in millimetres."""
import re

_NUMBER = r'\d+(?:[.,]\d+)?'
_UNIT = r'(?:мм|см|м)(?![а-яa-z0-9²³])'


def millimetres(value, unit='мм'):
    number = float(str(value).replace(',', '.').replace(' ', ''))
    result = number * {'мм':1, 'см':10, 'м':1000}[unit.lower()]
    return result if 1 <= result <= 10000 else None


def parse(text, *, bare=False):
    """Keep exact, enumeration, interval and upper limit as different measures."""
    text = str(text or '').lower().replace('ё','е').replace('\u00a0',' ')
    # Explicit limits take precedence over a list of example thicknesses.
    match = re.search(r'(?:св(?:ыше)?\.?\s*('+_NUMBER+r')\s*(мм|см|м)?\s*)?до\s*('+_NUMBER+r')\s*('+_UNIT+r')', text)
    if match:
        low, low_unit, high, unit = match.groups()
        upper = millimetres(high,unit)
        lower = millimetres(low,low_unit or unit) if low else None
        if upper is not None and (lower is None or lower < upper):
            return dict(mode='upper' if low is None else 'band',low=lower,high=upper,values=[])
    match = re.search(r'(?:толщин\w*\s*[:=]?\s*|\bt\s*[:=]\s*|\bh\s*=\s*)('+_NUMBER+r')\s*(мм|см|м)?\s*[-–—…]\s*('+_NUMBER+r')\s*('+_UNIT+r')',text)
    if match is None and bare and re.search(r'стен|плит|перекрыти',text):
        match = re.search(r'(?<![а-яa-z0-9.,])('+_NUMBER+r')\s*(мм|см|м)?\s*[-–—…]\s*('+_NUMBER+r')\s*('+_UNIT+r')',text)
    if match:
        a, a_unit, b, unit = match.groups()
        lower, upper = millimetres(a,a_unit or unit), millimetres(b,unit)
        if lower is not None and upper is not None and lower <= upper:
            return dict(mode='range',low=lower,high=upper,values=[])
    # Explicit lists retain their individual values; 180+200 does not include 190.
    list_match = re.search(r'(?<![а-яa-z0-9.,])('+_NUMBER+r'\s*(?:мм\s*)?(?:[+/()]\s*'+_NUMBER+r'\s*(?:мм\s*)?)+)\)?\s*мм', text)
    if list_match and (bare or 'толщин' in text):
        values = sorted({float(v.replace(',','.')) for v in re.findall(_NUMBER,list_match[1])})
        if values and all(1 <= v <= 10000 for v in values):
            return dict(mode='set',low=min(values),high=max(values),values=values)
    explicit = re.search(r'(?:толщин\w*(?:\s+по\s+ifc)?\s*[:=]?\s*|\bt\s*[-=:]?\s*|\bh\s*=\s*)('+_NUMBER+r')\s*('+_UNIT+r')',text)
    if explicit is None and bare:
        candidates = re.findall(r'(?<![\d.,])('+_NUMBER+r')\s*('+_UNIT+r')',text)
        # Never choose one dimension from an axb section or several dimensions.
        if len(candidates)==1 and not re.search(r'\d\s*[xх×]\s*\d',text):
            value = millimetres(*candidates[0])
            if value is not None:
                return exact(value)
    if explicit:
        value = millimetres(*explicit.groups())
        if value is not None:
            return exact(value)
    return None


def exact(value):
    return dict(mode='exact',low=value,high=value,values=[value])


def label(spec):
    if not spec:
        return ''
    mode = spec['mode']
    if mode=='upper': return f"до {spec['high']:g}"
    if mode=='band': return f"свыше {spec['low']:g} до {spec['high']:g}"
    if mode=='range': return f"{spec['low']:g}–{spec['high']:g}"
    return '/'.join(f'{v:g}' for v in spec['values'])


def contains(outer, inner):
    if not outer or not inner: return False
    if outer['mode'] in ('exact','set'):
        return inner['mode'] in ('exact','set') and all(v in outer['values'] for v in inner['values'])
    lower = outer.get('low')
    if lower is not None:
        if inner.get('low') is None: return False
        if outer['mode']=='band' and (inner['low'] < lower or inner['low']==lower and inner['mode']!='band'): return False
        if outer['mode']!='band' and inner['low'] < lower: return False
    return inner['high'] <= outer['high']


def from_row(row):
    # Actual numerical IFC geometry takes precedence over nominal type names.
    values = row.get('estimateParameterValues') or ''
    multiple = [float(v.replace(',','.')) for v in re.findall(r'Толщина:\s*('+_NUMBER+r')\s*мм',values,re.I)]
    if len(set(multiple))>1:
        return dict(mode='set',low=min(multiple),high=max(multiple),values=sorted(set(multiple)))
    spec = parse(values)
    if spec: return spec
    return parse(row.get('elementGroup'),bare=True)


def source_column(table, schema, ri):
    """Retain an explicit thickness field, including its original source cell."""
    from ..customer_documents.schema import revision
    candidates = []
    for ci,path in enumerate(schema.get('paths', [])):
        if ci in (schema.get('name'),schema.get('pos'),schema.get('unit')): continue
        headings = [value for value in path if re.match(r'^(?:(?:общая|проектная|фактическая)\s+)?толщи\s*н',value,re.I)]
        if not headings: continue
        text = ' '.join(headings)
        revisions = [revision(v) for v in path if revision(v)]
        if revisions and 'now' not in revisions: continue
        unit = re.search(r'(?<![а-я])(мм|см|м)(?![а-я0-9²³])',text,re.I)
        if not unit: continue
        value = table.rows[ri][ci]
        spec = parse(str(value)+' '+unit[1],bare=True)
        if spec:
            candidates.append(dict(spec=spec,source=table.source(ri,ci)))
    distinct = {str(c['spec']) for c in candidates}
    return candidates[0] if len(distinct)==1 else None


def fill_work_names(output, source_rows, value):
    """Render a known thickness in t=… placeholders, leaving unknowns explicit."""
    by_index = {row.index:row for row in source_rows}
    for row in output:
        original = by_index.get(row.src_index)
        if not original or original.kind not in ('work','variant'):
            continue
        name = row.name_override if row.name_override is not None else original.name
        if re.search(r'\bt\s*=\s*(?:…|\.{3})?\s*мм',name,re.I):
            row.name_override = re.sub(r'\bt\s*=\s*(?:…|\.{3})?\s*мм',
                                      't='+ (f'{value:g}' if value is not None else 'не указана')+' мм',name,flags=re.I)
