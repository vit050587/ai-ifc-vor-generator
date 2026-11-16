"""Keep document-only insulation and joints once, in their declared scope."""
from collections import defaultdict
import re

from .template import OutputRow


def copy_auxiliary(output, rows, template_rows, reference):
    by_index = {r.index:r for r in template_rows}
    source = [r for r in rows if r.kind in ('insulation','seam')
              and r.category not in ('concrete','preparation','rebar','formwork')]
    sources = {s for r in source for s in getattr(r.volume,'sources',())}
    contexts = defaultdict(set)
    for row in source:
        contexts[(row.name,row.unit,row.part)].add(row.source_context or row.section)
    for item in output:
        original = by_index[item.src_index]
        if original.element_kind in ('insulation','seam') or sources.intersection(getattr(item.volume,'sources',())):
            item.volume = None
            item.justification = ''
    groups = defaultdict(list)
    for row in source:
        part = row.part or 'underground'
        candidates = [r for r in template_rows if r.kind == 'work' and not r.parking
                      and r.element_kind == row.kind and r.part == part]
        if not candidates:
            continue
        def priority(item):
            name = (item.subsection or '').lower()
            orientation = 'горизонтальн' if row.orientation == 'horizontal' else 'вертикальн'
            thermal = bool(re.search('тепло|утеплен|пенополист', row.name, re.I))
            return (bool(row.target_subsection and row.target_subsection == item.subsection),
                    bool(row.orientation and orientation in name),
                    bool(thermal and 'теплоизоляц' in name),
                    bool('стен' in row.source_context.lower() and 'подземная часть' in name))
        # Ties choose a canonical template subsection for the *source group*,
        # never a model construction. Each fact is emitted only here.
        sample = max(candidates, key=priority)
        groups[(sample.index, row.source_context or row.section, part)].append(row)
    for (index, context, part), members in groups.items():
        sample = by_index[index]
        additions = []
        for level in ('part','section','subsection'):
            header = next((r for r in template_rows if r.kind == level and r.part == part
                           and (level == 'part' or r.section == sample.section)
                           and (level != 'subsection' or r.subsection == sample.subsection)), None)
            if header:
                additions.append(OutputRow(header.index))
        parents = additions
        additions = []
        additions.append(OutputRow(index, label='По ВОР заказчика: '+context, name_override='',
                                   extra={c:None for c in range(3,9)}))
        quantities = defaultdict(list)
        for row in members:
            quantities[(row.name,row.unit)].append(row)
        for number, ((name,unit), items) in enumerate(quantities.items(),1):
            existing = [item for item in output if by_index[item.src_index].subsection == sample.subsection
                        and by_index[item.src_index].part == part
                        and (item.name_override or by_index[item.src_index].name) == name
                        and (item.extra.get(3) or by_index[item.src_index].unit) == unit]
            if len(existing) == 1 and existing[0].volume is None and len(contexts[(name,unit,items[0].part)]) == 1:
                existing[0].volume = sum(r.volume for r in items)
                existing[0].justification = reference
                continue
            additions.append(OutputRow(index, label=str(number), name_override=name,
                                       volume=sum(r.volume for r in items), justification=reference,
                                       extra={3:unit,7:None,8:None}))
        if any(item.volume is not None for item in additions):
            from .source_blocks import place_block
            place_block(output, parents, additions, template_rows)
    return len(source)
