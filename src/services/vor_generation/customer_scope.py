"""Select one template subsection per source scope, without distributing totals."""
import re
from dataclasses import replace


def scoped_rebar(rebar, rows):
    sources = {s for row in rows for s in getattr(row.volume, 'sources', ())}
    return replace(rebar, rows=[row for row in rebar.rows
                               if not row.normalized_scope or sources.intersection(getattr(row.volume, 'sources', ()))])


def assign_subsections(rows, template_rows):
    candidates = {}
    for item in template_rows:
        if item.parking or item.kind not in ('work', 'resource', 'variant'):
            continue
        candidates.setdefault((item.element_kind, item.part), set()).add(item.subsection)
    result = []
    for row in rows:
        if not row.normalized_scope:
            result.append(row)
            continue
        kind = 'foundation_slab' if row.kind == 'crane_foundation' else row.kind
        names = candidates.get((kind, row.part), set())
        text = row.source_context.lower()
        scored = []
        for name in names:
            name_text = (name or '').lower()
            score = 0
            for word in ('буронабив', 'забив', 'фундамент', 'горизонтальн', 'вертикальн'):
                if word in text and word in name_text:
                    score += 1
            if row.orientation and row.orientation in ('horizontal', 'vertical'):
                word = 'горизонтальн' if row.orientation == 'horizontal' else 'вертикальн'
                score += 2 if word in name_text else -2
            scored.append((score, name))
        best = max((s for s, _ in scored), default=None)
        winners = [name for score, name in scored if score == best]
        result.append(replace(row, target_subsection=winners[0] if len(winners) == 1 else ''))
    return result


def preserve_remaining(output, rows, template_rows, reference):
    """Keep quantities the template cannot express in their original source scope.

    Source references, not equal numerical values, determine coverage. Work and
    its material may legitimately refer to the same physical quantity.
    """
    from .template import OutputRow
    used = {source for item in output if item.volume is not None
            for source in getattr(item.volume, 'sources', ())}
    remaining = [row for row in rows if row.normalized_scope and row.volume != 0
                 and any(source not in used for source in getattr(row.volume, 'sources', ()))]
    samples = {item.index:item for item in template_rows}
    sample = next(item for item in template_rows if item.kind == 'work')
    buckets = {}
    for row in remaining:
        key = (row.part, row.external_scope, row.target_subsection or '', row.source_context)
        buckets.setdefault(key, []).append(row)
    for (part, external, target, context), members in buckets.items():
        # Insert original measures into their unique target subsection. Unscoped
        # and temporary/retaining works get their own explicit source section.
        additions = []
        index = None
        if target and not external:
            positions = [i for i, item in enumerate(output)
                         if samples[item.src_index].subsection == target
                         and samples[item.src_index].part == part
                         and not samples[item.src_index].parking]
            if positions:
                index = positions[-1]+1
        if index is None:
            heading = {'above':'Надземная часть здания (выше отм. 0,000)',
                       'underground':'Подземная часть здания (до отм. 0,000)'}.get(
                           part, 'Часть здания: без разделения в исходном ВОР')
            additions.extend([
                OutputRow(sample.index, label=heading, name_override='', extra={c:None for c in range(3,9)}),
                OutputRow(sample.index, label='Раздел. '+(external or 'Прочие работы исходного ВОР'),
                          name_override='', extra={c:None for c in range(3,9)})])
        additions.append(OutputRow(sample.index, label=context, name_override='', extra={c:None for c in range(3,9)}))
        for number, row in enumerate(members, 1):
            additions.append(OutputRow(sample.index, label=str(number), name_override=row.name,
                                        volume=row.volume, justification=reference, extra={3:row.unit,7:None,8:None}))
        # Retain unmatched source facts for audit, outside the visible template.
        for addition in additions:
            addition.hidden = True
        if index is None:
            output.extend(additions)
        else:
            output[index:index] = additions
    return len(rows)-len(remaining), len(remaining)


def fill_product_counts(output, rows, template_rows, reference):
    """A source total without a panel variant stays one total, with its marks."""
    from .template import OutputRow
    used = {s for item in output if item.volume is not None for s in getattr(item.volume, 'sources', ())}
    groups = {}
    for row in rows:
        if (row.normalized_scope and row.kind and row.kind.startswith('precast')
                and row.category == 'count' and row.target_subsection
                and row.volume != 0 and not used.intersection(getattr(row.volume, 'sources', ()))):
            groups.setdefault((row.kind, row.part, row.target_subsection), []).append(row)
    by_index = {r.index:r for r in template_rows}
    for (kind, part, subsection), members in groups.items():
        samples = [r for r in template_rows if r.subsection == subsection and r.kind == 'work' and not r.parking]
        if not samples:
            continue
        positions = [i for i, item in enumerate(output) if by_index[item.src_index].subsection == subsection
                     and by_index[item.src_index].part == part and not by_index[item.src_index].parking]
        if not positions:
            continue
        sample = samples[0]
        number = str(max((int(r.number) for r in samples if r.number.isdigit()), default=0)+1)
        name = {'precast_panel':'Монтаж сборных железобетонных панелей',
                'precast_vent':'Монтаж сборных вентиляционных блоков',
                'precast':'Монтаж сборных лестничных маршей'}.get(kind, 'Монтаж сборных железобетонных изделий')
        additions = [OutputRow(sample.index, label='Сборные изделия без разделения на варианты', name_override='',
                               extra={c:None for c in range(3,9)}),
                     OutputRow(sample.index, label=number, name_override=name, volume=sum(r.volume for r in members),
                               justification=reference, extra={3:'шт.',7:None,8:None})]
        additions.extend(OutputRow(sample.index, label=f'{number}.{i}', name_override=r.name,
                                    volume=r.volume, justification=reference, extra={3:'шт.',7:None,8:None})
                         for i,r in enumerate(members,1))
        output[positions[-1]+1:positions[-1]+1] = additions


def insert_source_rebar(output, source_rows, template_rows, rebar, reference):
    """Copy source reinforcement once; IFC geometry cannot reduce its total."""
    from . import mapping, customer_vor
    from .template import OutputRow
    from .thickness import label as thickness_label
    rows = assign_subsections(source_rows, template_rows)
    groups = {}
    for row in rows:
        if row.normalized_scope and row.category == 'rebar' and not row.external_scope and row.target_subsection:
            groups.setdefault((row.kind, row.part, row.target_subsection, thickness_label(row.thickness_spec)), []).append(row)
    by_index = {r.index:r for r in template_rows}
    pending = []
    for (kind, part, subsection, geometry), members in groups.items():
        components = {row.component for row in members} if kind in ('stair', 'precast') else {''}
        for component in components:
            pending.append((kind, part, subsection, component, geometry,
                            [row for row in members if row.component == component] if kind in ('stair', 'precast') else members))
    for kind, part, subsection, component, geometry, members in pending:
        children = []
        parent_rebar = False
        template_component = ''
        selected_buckets = set()
        for item in template_rows:
            if item.subsection != subsection or item.part != part or item.parking:
                continue
            if item.kind == 'work':
                template_component = mapping.stair_component(item.name) or template_component
                parent_rebar = mapping.classify_work(item.name) == 'rebar'
                if kind in ('stair', 'precast'):
                    if component in ('flight', 'landing'):
                        parent_rebar = parent_rebar and template_component == component
                    elif parent_rebar:
                        bucket = mapping.rebar_bucket(item.name)
                        parent_rebar = bucket not in selected_buckets
                        selected_buckets.add(bucket)
            elif item.kind not in ('resource', 'variant', 'empty'):
                parent_rebar = False
            if parent_rebar:
                children.append(item)
        if not children:
            continue
        positions = [i for i, item in enumerate(output) if by_index[item.src_index].subsection == subsection
                     and by_index[item.src_index].part == part and not by_index[item.src_index].parking]
        if not positions:
            continue
        title = {'wall':'Стены', 'slab':'Плиты перекрытия', 'foundation_slab':'Фундаментная плита',
                 'crane_foundation':'Фундамент под башенный кран', 'stair':'Лестницы'}.get(kind, subsection)
        if component:
            title += ' ' + {'flight':'лестничные марши', 'landing':'лестничные площадки', 'combined':'марши и площадки'}.get(component, '')
        heading = title+' толщиной '+geometry+' мм — армирование по ВОР заказчика' if geometry else title+' — армирование без распределения по геометрии'
        additions = [OutputRow(children[0].index, label=heading,
                               name_override='', extra={c:None for c in range(3,9)})]
        additions.extend(customer_vor._fill_rows(children, members, [],
                         customer_vor._Context(scoped_rebar(rebar, members), reference), kind, part, 1.0))
        output[positions[-1]+1:positions[-1]+1] = additions
