"""Preserve reinforcement categories and source scopes without arbitrary allocation."""
from collections import defaultdict
from dataclasses import replace
import re

from . import documents, mapping
from .template import OutputRow


def _is_rebar(item, source):
    if item.name_override == '' and item.label:
        return False
    name = item.name_override or source.name
    return (mapping.classify_work(name) == 'rebar'
            or mapping.classify_work(source.name) == 'rebar'
            or source.kind == 'resource' and mapping.classify_resource(source.name) == 'rebar'
            or item.name_override and mapping.classify_resource(name) == 'rebar'
               and mapping.classify_work(name) not in ('concrete', 'formwork'))


def source_scope(row):
    from .customer_vor import _section_kind, _detect_part
    from .thickness import exact
    kind = row.source_kind or _section_kind(row.section)
    if row.external_scope:
        kind = 'external:'+row.external_scope
    part = row.source_part or _detect_part(row.section, '')
    if not part and kind in ('foundation_slab', 'crane_foundation', 'equipment_foundation'):
        part = 'underground'
    if not part and kind == 'parapet':
        part = 'above'
    spec = row.thickness_spec or (exact(row.thickness) if row.thickness is not None else None)
    return kind or 'unscoped', part, spec


def shared_heading(row):
    from .thickness import label
    kind, _, spec = source_scope(row)
    title = {'wall':'Стены', 'slab':'Плиты перекрытия', 'column':'Колонны',
             'foundation_slab':'Фундаментная плита', 'crane_foundation':'Фундамент под башенный кран',
             'stair':'Лестницы', 'parapet':'Парапеты', 'beam':'Балки'}.get(kind, 'Конструкции')
    if kind == 'stair' and row.component:
        title = {'flight':'Лестничные марши', 'landing':'Лестничные площадки',
                 'combined':'Лестничные марши и площадки'}.get(row.component,title)
    geometry = ' толщиной '+label(spec)+' мм' if spec else ''
    if row.cross_section:
        geometry = ' сечением '+row.cross_section+' мм'
    context = row.source_context or row.section
    if row.normalized_scope:
        # Some source readers repeat the construction and append the individual
        # steel resource after a colon. A diameter is a child, not a new object.
        components = [re.sub(r':\s*арматур[аы]\b.*$', '', p, flags=re.I).strip()
                      for p in context.split(' / ')]
        context = ' / '.join(dict.fromkeys(filter(None, components)))
    return title+geometry+' — общий объём армирования по ВОР заказчика: '+context


def is_shared_group(value):
    return 'общий объем армирования по вор заказчика:' in str(value or '').lower().replace('ё','е')


def copy_rebar(output, template_rows, rebar, reference, *, preserve_unmatched=True, shared_scopes=()):
    """Assign each source row once, only to an unambiguous construction group.

    A source without a matching template scope cannot be moved into an unrelated
    construction group. Missing geometry never causes proportional allocation.
    """
    from .thickness import parse, contains
    from .stair_grouping import source_band

    by_index = {row.index: row for row in template_rows}
    slots = []
    heading = ''
    component = ''
    band = 'general'
    for position, item in enumerate(output):
        source = by_index[item.src_index]
        if source.kind in ('part', 'section', 'subsection', 'block') or item.name_override == '' and item.label:
            heading = item.label or source.number or source.name
            component = mapping.stair_component(heading.split(' — общий объём армирования',1)[0]) or ''
            band = source_band(heading)
        component = mapping.stair_component(item.name_override or source.name) or component
        if source.kind == 'work' and item.name_override != '' and mapping.classify_work(source.name) == 'rebar':
            if item.name_override and mapping.classify_work(item.name_override) == 'rebar':
                source = replace(source, name=item.name_override, number=item.label or source.number)
            kind = source.element_kind
            if kind == 'foundation_slab' and mapping.detect_kind(heading) == 'crane_foundation':
                kind = 'crane_foundation'
            spec = parse(heading.rsplit('толщина по IFC:', 1)[-1], bare=True)
            slots.append((position, source, kind, spec, component, band, heading))
    assigned = defaultdict(list)
    pending = []
    limits = defaultdict(set)
    for row in rebar.rows:
        kind, part, spec = source_scope(row)
        if spec and spec['mode'] == 'upper':
            limits[(kind, part)].add(spec['high'])
    for row in rebar.rows:
        kind, part, spec = source_scope(row)
        if shared_heading(row) in shared_scopes:
            pending.append(row)
            continue
        bucket = mapping.rebar_bucket(row.group or row.name)
        candidates = [slot for slot in slots if not slot[1].parking
                      and slot[2] == kind
                      and (not part or slot[1].part == part)
                      and (not is_shared_group(slot[6]) or slot[6] == shared_heading(row))]
        explicit = [slot for slot in candidates if slot[6] == shared_heading(row)]
        if explicit:
            # This heading identifies the complete source scope. Its section
            # may be known even when no scalar thickness can be parsed from it.
            target = next((slot for slot in explicit if mapping.rebar_bucket(slot[1].name) == bucket), explicit[0])
            assigned[(target[0], bucket)].append(row)
            continue
        if row.cross_section:
            from .customer_vor import _section_label
            candidates = [slot for slot in candidates if _section_label(slot[6]) == row.cross_section]
        if not candidates:
            pending.append(row)
            continue
        pit = bool(re.search('приям', row.source_context or row.section, re.I))
        candidates = [slot for slot in candidates if bool(re.search('приям', slot[6], re.I)) == pit]
        if row.component:
            candidates = [slot for slot in candidates if slot[4] == row.component]
        row_band = source_band(row.section)
        if row_band != 'general':
            candidates = [slot for slot in candidates if slot[5] == row_band]
        if spec:
            if spec['mode'] == 'upper':
                lower = [v for v in limits[(kind, part)] if v < spec['high']]
                if lower:
                    spec = dict(spec, mode='band', low=max(lower))
            def slot_spec(slot):
                value = slot[3]
                if value and value['mode'] == 'upper':
                    previous = [v for v in limits[(kind, part)] if v < value['high']]
                    if previous:
                        value = dict(value, mode='band', low=max(previous))
                return value
            candidates = [slot for slot in candidates if contains(spec, slot_spec(slot))
                          and (spec['mode'] not in ('set','range') or contains(slot_spec(slot),spec))]
        groups = {(slot[6], slot[1].subsection, slot[4], slot[5]) for slot in candidates}
        if len(groups) != 1:
            pending.append(row)
            continue
        target = next((slot for slot in candidates if mapping.rebar_bucket(slot[1].name) == bucket), candidates[0])
        assigned[(target[0], bucket)].append(row)
    # Shared source quantities are explicit groups, identical on both sides.
    # They never fill an arbitrary model group or disappear when geometry differs.
    appended_at = len(output)
    relocations = []
    if preserve_unmatched:
        shared = defaultdict(list)
        for row in pending:
            kind, part, _ = source_scope(row)
            shared[(kind, part, shared_heading(row))].append(row)
        for (kind, part, heading), members in shared.items():
            start = len(output)
            candidates = [s for s in slots if not s[1].parking and s[2] == kind
                          and (not part or s[1].part == part)]
            source = candidates[-1][1] if candidates else next((r for r in template_rows
                if r.kind == 'work' and mapping.classify_work(r.name) == 'rebar' and not r.parking), None)
            if source is None:
                continue
            for level in ('part', 'section', 'subsection') if candidates else ():
                header = next((r for r in template_rows if r.kind == level and r.part == source.part
                               and (level == 'part' or r.section == source.section)
                               and (level != 'subsection' or r.subsection == source.subsection)), None)
                if header:
                    output.append(OutputRow(header.index))
            if not candidates:
                part_header = {'above':'Надземная часть здания (выше отм. 0,000)',
                               'underground':'Подземная часть здания (до отм. 0,000)'}.get(part,'Часть здания: не указана в исходном ВОР')
                output.append(OutputRow(source.index, label=part_header, name_override='', extra={c:None for c in range(3,9)}))
                output.append(OutputRow(source.index, label='Раздел. '+(members[0].external_scope or 'Армирование без установленной группы конструкций'),
                                        name_override='', extra={c:None for c in range(3,9)}))
                output.append(OutputRow(source.index, label='Подраздел. Исходные объёмы арматуры',
                                        name_override='', extra={c:None for c in range(3,9)}))
            body = len(output)
            output.append(OutputRow(source.index, label=heading, name_override='', extra={c:None for c in range(3,9)}))
            for number, bucket in enumerate(('bars', 'frames', 'releases'), 1):
                rows = [r for r in members if mapping.rebar_bucket(r.group or r.name) == bucket]
                if not rows:
                    continue
                at = len(output)
                name = {'bars':'Устройство арматурных стержней', 'frames':'Устройство арматурных каркасов',
                        'releases':'Устройство арматурных выпусков'}[bucket]
                output.append(OutputRow(source.index, label=str(number), name_override=name))
                assigned[(at, bucket)] = rows
            relocations.append((start, body, len(output)))
    resources = [row for row in template_rows if row.kind == 'resource'
                 and mapping.classify_resource(row.name) == 'rebar']
    result = []
    offsets = {}
    for position, item in enumerate(output):
        offsets[position] = len(result)
        source = by_index[item.src_index]
        if not _is_rebar(item, source):
            result.append(item)
            continue
        if source.kind != 'work' or mapping.classify_work(source.name) != 'rebar':
            continue
        if item.name_override and mapping.classify_work(item.name_override) == 'rebar':
            source = replace(source, name=item.name_override, number=item.label or source.number)
        buckets = [(bucket, members) for (at, bucket), members in assigned.items() if at == position]
        if not buckets:
            item.volume = None
            item.justification = ''
            result.append(item)
            continue
        for offset, (bucket, members) in enumerate(buckets):
            work_name = source.name if mapping.rebar_bucket(source.name) == bucket else {
                'bars': 'Устройство арматурных стержней',
                'frames': 'Устройство арматурных каркасов',
                'releases': 'Устройство арматурных выпусков'}[bucket]
            number = item.label or source.number
            if offset:
                # Inserted work categories need their own parent number.
                number = str(max((int(r.number) for r in template_rows if r.subsection == source.subsection
                                  and r.kind == 'work' and r.number.isdigit()), default=0)+offset)
            result.append(OutputRow(source.index, label=number, name_override=work_name,
                                    volume=sum(row.volume for row in members),
                                    justification=reference, extra={**item.extra, 3: 'т'}))
            sample = next((row for row in resources if row.subsection == source.subsection), source)
            specs = defaultdict(list)
            for row in members:
                # Sum original tonnes only inside this existing group and work
                # category. Keep unknown specifications distinct by source name.
                key = (row.diameter, row.rebar_class or '',
                       row.name if not row.diameter and not row.rebar_class else '')
                specs[key].append(row)
            for child, ((diameter, rebar_class, _), rows) in enumerate(specs.items(), 1):
                name = documents.rebar_label(diameter, rebar_class) if diameter or rebar_class else rows[0].name
                result.append(OutputRow(sample.index, label=f'{number}.{child}',
                                        name_override=name, volume=sum(row.volume for row in rows),
                                        justification=reference, extra={3: 'т'}))
    offsets[len(output)] = len(result)
    if relocations:
        from .source_blocks import place_block
        placed = result[:offsets[appended_at]]
        for start, body, end in relocations:
            place_block(placed, result[offsets[start]:offsets[body]],
                        result[offsets[body]:offsets[end]], template_rows)
        result = placed
    output[:] = result
    return pending
