"""Fill reinforcement inside existing template groups without scaling tonnes."""
from collections import defaultdict
from dataclasses import replace

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


def copy_rebar(output, template_rows, rebar, reference):
    """Assign each source row once to an existing work slot; add no headings.

    A source without a matching template scope cannot be moved into an unrelated
    construction group. Missing geometry never causes proportional allocation.
    """
    from .customer_vor import _section_kind, _detect_part
    from .thickness import parse
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
            component = mapping.stair_component(heading) or ''
            band = source_band(heading)
        component = mapping.stair_component(item.name_override or source.name) or component
        if source.kind == 'work' and item.name_override != '' and mapping.classify_work(source.name) == 'rebar':
            if item.name_override and mapping.classify_work(item.name_override) == 'rebar':
                source = replace(source, name=item.name_override, number=item.label or source.number)
            kind = source.element_kind
            if kind == 'foundation_slab' and mapping.detect_kind(heading) == 'crane_foundation':
                kind = 'crane_foundation'
            spec = parse(heading.rsplit('толщина по IFC:', 1)[-1], bare=True)
            thickness = tuple(spec['values']) if spec else ()
            slots.append((position, source, kind, thickness, component, band))
    assigned = defaultdict(list)
    for row in rebar.rows:
        kind = row.source_kind or _section_kind(row.section)
        part = row.source_part or _detect_part(row.section, "")
        if not kind:
            continue
        bucket = mapping.rebar_bucket(row.group or row.name)
        candidates = [slot for slot in slots if not slot[1].parking
                      and (not kind or slot[2] == kind)
                      and (not part or slot[1].part == part)]
        if not candidates:
            continue
        if row.component and any(slot[4] == row.component for slot in candidates):
            candidates = [slot for slot in candidates if slot[4] == row.component]
        row_band = source_band(row.section)
        if row_band != 'general' and any(slot[5] == row_band for slot in candidates):
            candidates = [slot for slot in candidates if slot[5] == row_band]
        def score(slot):
            _, source, _, thickness, component, band = slot
            return (not row.thickness or row.thickness in thickness,
                    mapping.rebar_bucket(source.name) == bucket,
                    not row.component or component == row.component)
        target = max(candidates, key=score)
        assigned[(target[0], bucket)].append(row)
    resources = [row for row in template_rows if row.kind == 'resource'
                 and mapping.classify_resource(row.name) == 'rebar']
    result = []
    for position, item in enumerate(output):
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
        for bucket, members in buckets:
            work_name = source.name if mapping.rebar_bucket(source.name) == bucket else {
                'bars': 'Устройство арматурных стержней',
                'frames': 'Устройство арматурных каркасов',
                'releases': 'Устройство арматурных выпусков'}[bucket]
            result.append(OutputRow(source.index, label=item.label, name_override=work_name,
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
            for number, ((diameter, rebar_class, _), rows) in enumerate(specs.items(), 1):
                name = documents.rebar_label(diameter, rebar_class) if diameter or rebar_class else rows[0].name
                result.append(OutputRow(sample.index, label=f'{source.number}.{number}',
                                        name_override=name, volume=sum(row.volume for row in rows),
                                        justification=reference, extra={3: 'т'}))
    output[:] = result
