"""Compare undivided stair assembly concrete with source component totals."""
from collections import defaultdict


def add_totals(rows, generated, customer):
    from ..comparison_typical import norm
    from .mapping import classify_work, stair_component
    from .stair_grouping import source_band
    from .group_comparison import _aggregate, _family
    buckets = defaultdict(lambda: [[], []])
    for side, source in enumerate((generated, customer)):
        for row in source:
            if (row.get('kind') != 'work' or row.get('quantity') is None
                    or _family(row) != 'stair' or classify_work(row.get('work')) != 'concrete'):
                continue
            key = tuple(norm(row.get(k)) for k in ('part','section','subsection','unit'))
            buckets[key + (source_band(row.get('elementGroup')), )][side].append(row)
    for ours, theirs in buckets.values():
        if not ours or not theirs:
            continue
        g_components = {stair_component(r.get('elementGroup')) or stair_component(r.get('work')) for r in ours}
        c_components = {stair_component(r.get('elementGroup')) or stair_component(r.get('work')) for r in theirs}
        if 'combined' not in g_components or c_components not in ({'flight','landing'}, {'combined'}):
            continue
        if any(r.get('coveredByAggregate') and (r.get('generatedSourceRow') in {s['sourceRow'] for s in ours}
                                               or r.get('customerSourceRow') in {s['sourceRow'] for s in theirs}) for r in rows):
            continue
        name = 'Бетонирование монолитных железобетонных конструкций лестничных маршей и площадок'
        # A common operation represents the union; the source row IDs retain
        # the actual composition and provenance of each side.
        g = [dict(r,work=name) for r in ours]
        c = [dict(r,work=name) for r in theirs]
        total = _aggregate(rows,g,c,'stair')
        if total:
            total['comments'] = 'Общий объём маршей и площадок: в IFC нет разделения на компоненты. Исходные объёмы не распределяются; строки состава не суммируются повторно.'
            rows.append(total)
