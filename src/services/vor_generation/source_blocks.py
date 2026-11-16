"""Place source-only groups inside their declared template hierarchy."""
import re


def heading_level(item, originals):
    original = originals[item.src_index]
    if item.hidden:
        return None
    if original.kind not in ('part', 'section', 'subsection', 'block') and not (item.name_override == '' and item.label):
        return None
    text = item.label or original.number or original.name
    if re.match(r'(?:подземная часть|надземная часть|автостоянка|часть здания:)', text, re.I):
        return 0
    if re.match(r'раздел(?:\s|\.)', text, re.I):
        return 1
    if re.match(r'подраздел(?:\s|\.)', text, re.I):
        return 2
    return 3


def place_block(output, parents, block, template_rows):
    """Reuse existing ancestors and insert before the next peer section.

    Only row order and repeated ancestor headings change. Construction groups,
    resource specifications, quantities and provenance remain untouched.
    """
    if not block:
        return
    originals = {r.index: r for r in template_rows}
    if not parents:
        output.extend(block)
        return
    parent_levels = [(heading_level(p, originals), p) for p in parents]
    wanted = {level:(p.label or originals[p.src_index].number or originals[p.src_index].name).strip()
              for level, p in parent_levels if level is not None}
    headings = []
    active = {}
    best = None
    for at, item in enumerate(output):
        level = heading_level(item, originals)
        if level is None:
            continue
        headings.append((at, level))
        text = (item.label or originals[item.src_index].number or originals[item.src_index].name).strip()
        active = {k:v for k,v in active.items() if k < level}
        active[level] = text
        if level in wanted and all(active.get(i) == text for i, text in wanted.items() if i <= level):
            if best is None or level > best[1]:
                best = (at, level)
    if best is None:
        output.extend([*parents, *block])
        return
    at, level = best
    end = next((i for i, depth in headings if i > at and depth <= level), len(output))
    output[end:end] = [*(p for depth, p in parent_levels if depth > level), *block]
