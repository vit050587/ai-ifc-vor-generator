"""Compare quantities inside compatible thickness bands, without splitting volumes."""
from collections import defaultdict
import re

from .thickness import from_row, contains, label as thickness_label

_LABELS = {'wall':'стены','slab':'перекрытия','foundation_slab':'фундаментная плита',
           'crane_foundation':'фундамент под кран','column':'колонны','beam':'балки'}


def _family(row):
    from .mapping import detect_kind
    group = row.get('elementGroup', '')
    # The completed VOR subsection retains the builder's construction kind.
    # A wall type called "Пилон" must not move into columns during comparison.
    kind = detect_kind(row.get('subsection')) or detect_kind(group)
    if re.search(r'кран', group, re.I): kind = 'crane_foundation'
    return kind


def _unknown_breakdown(row):
    return not from_row(row) and bool(re.search(r'толщин.*—|площадью\s*—|без распределения по геометрии',row.get('elementGroup',''),re.I))


def _compatible_parameters(aggregate):
    """A contained thickness set is compatible with its declared source band."""
    parameters = aggregate['parameterComparison']
    parameters['differences'] = [d for d in parameters['differences'] if d['parameter']!='thickness']
    for field in ('missingGenerated','missingCustomer'):
        parameters[field] = [value for value in parameters[field] if value!='Толщина']
    parameters['status'] = ('mismatch' if parameters['differences'] else 'missing_data'
                            if parameters['missingGenerated'] or parameters['missingCustomer'] else 'matched')
    quantity_status = aggregate['quantityComparisonStatus']
    aggregate['status'] = ('parameters_mismatch' if parameters['status']=='mismatch' else 'parameters_missing'
                           if parameters['status']=='missing_data' else 'matched') if quantity_status=='matched' else quantity_status
    aggregate['isDifferent'] = aggregate['status']!='matched'


def _aggregate(rows, ours, theirs, kind, band=None):
    from ..comparison_typical import compare_rows
    g_ids, c_ids = {r['sourceRow'] for r in ours}, {r['sourceRow'] for r in theirs}
    members = [r for r in rows if r.get('generatedSourceRow') in g_ids or r.get('customerSourceRow') in c_ids]
    if len(members)==1 and members[0].get('generatedQuantity') is not None and members[0].get('customerQuantity') is not None:
        return None
    heading = theirs[0].get('elementGroup') or ours[0].get('elementGroup') or ''
    def total(source):
        value = dict(source[0],elementGroup=heading,quantity=sum(r['quantity'] for r in source))
        for field in ('reason','calculation','estimateParameterValues','estimateParameters'):
            value[field] = '; '.join(dict.fromkeys(r.get(field,'') for r in source if r.get(field)))
        value['calculation'] = ' + '.join(f"{r['quantity']:g} [строка {r['sourceRow']}]" for r in source)
        if any(r.get('quantityStatus')=='partial_data' for r in source): value['quantityStatus']='partial_data'
        return value
    result = compare_rows([total(ours)],[total(theirs)])[0]
    result.update(isAggregate=True,generatedSourceRows=sorted(g_ids),customerSourceRows=sorted(c_ids),
                  generatedSourceRow=None,customerSourceRow=None,
                  comparisonConfidence='Сумма совместимых групп; исходные объёмы не распределяются')
    if band:
        _compatible_parameters(result)
        result['thicknessBand'] = thickness_label(band)+' мм'
        result['thicknessRule'] = band
    result['comments'] = 'Сравнение исходных сумм '+('в группе толщин '+result['thicknessBand'] if band else 'при отсутствующей разбивке по геометрии')+'. Объёмы между группами не распределяются.'
    objects = {tuple(row['comparisonObjectKey']) for row in members if row.get('comparisonObjectKey')}
    if len(objects)==1 and all(row.get('comparisonObjectKey') for row in members):
        result.update(comparisonObjectKey=next(iter(objects)),comparisonObject=members[0]['comparisonObject'],
                      elementGroup=members[0]['comparisonObject'])
    for row in members:
        row.update(coveredByAggregate=True,isDifferent=False,status='group_component',
                   aggregateGroup=heading,aggregateGeneratedSourceRows=sorted(g_ids),
                   aggregateCustomerSourceRows=sorted(c_ids))
        row['comments'] = '. '.join(filter(None,[row.get('comments'),
            'Входит в итоговое сравнение: '+heading+'. Пустая вторая сторона этой строки не означает отсутствие пары; сравнивается сумма группы.']))
    return result


def add_totals(rows, generated, customer):
    from ..comparison_typical import norm, identity
    from .foundation_matching import operation_identity
    from .mapping import classify_resource
    buckets = defaultdict(lambda: [[],[]])
    upper_limits = defaultdict(set)
    for side, source in enumerate((generated,customer)):
        for row in source:
            if row.get('isSeparator') or row.get('quantity') is None: continue
            kind = _family(row)
            if kind not in _LABELS: continue
            context = tuple(norm(row.get(k)) for k in ('part','section','subsection'))+(kind,)
            operation = operation_identity(row) if row.get('kind')=='resource' and classify_resource(row.get('work'))=='concrete' else identity(row)
            buckets[context+(operation,)][side].append(row)
            spec = from_row(row)
            if side==1 and spec and spec['mode']=='upper': upper_limits[context].add(spec['high'])
    additions = []
    for key,(ours,theirs) in buckets.items():
        if not ours or not theirs: continue
        kind = key[-2]
        def with_lower_limit(spec):
            if spec and spec['mode']=='upper':
                previous = [v for v in upper_limits[key[:-1]] if v<spec['high']]
                if previous: return dict(spec,mode='band',low=max(previous))
            return spec
        target_groups = defaultdict(list)
        for row in theirs:
            spec = from_row(row)
            if spec: target_groups[row.get('elementGroup','')].append(row)
        constraints = {}
        for name,group in target_groups.items():
            constraints[name] = with_lower_limit(dict(from_row(group[0])))
        assigned = defaultdict(list)
        ambiguous = False
        for row in ours:
            spec = with_lower_limit(from_row(row))
            matches = [name for name,outer in constraints.items() if contains(outer,spec)]
            if len(matches)>1:
                ambiguous = True
                break
            if matches: assigned[matches[0]].append(row)
        used_g, used_c = set(),set()
        if not ambiguous:
            for name,source in assigned.items():
                target = target_groups[name]
                result = _aggregate(rows,source,target,kind,constraints[name])
                if result: additions.append(result)
                used_g.update(r['sourceRow'] for r in source)
                used_c.update(r['sourceRow'] for r in target)
        remaining_g = [r for r in ours if r['sourceRow'] not in used_g]
        remaining_c = [r for r in theirs if r['sourceRow'] not in used_c]
        # A declared, incompatible range must not fall back to the sum of all walls.
        if (remaining_g and remaining_c and any(_unknown_breakdown(r) for r in remaining_g+remaining_c)
                and not any(from_row(r) and from_row(r)['mode']!='exact' for r in remaining_c)):
            result = _aggregate(rows,remaining_g,remaining_c,kind)
            if result: additions.append(result)
    rows.extend(additions)
    from .stair_comparison import add_totals as stair_totals
    stair_totals(rows, generated, customer)
