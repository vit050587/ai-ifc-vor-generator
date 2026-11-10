"""Pair unique operations by compatible thickness or explicit stair component."""
from collections import defaultdict

from .thickness import from_row, contains
from .group_comparison import _family


def operation_links(generated, customer):
    from ..comparison_typical import scope, identity
    from .mapping import stair_component
    from .stair_grouping import source_band
    buckets = defaultdict(lambda: [[], []])
    limits = defaultdict(set)
    for side, rows in enumerate((generated, customer)):
        for row in rows:
            if row.get('isSeparator') or row.get('quantity') is None:
                continue
            family = _family(row)
            if family not in ('wall', 'slab', 'stair', 'precast'):
                continue
            context = scope(row)[:3] + (family,)
            buckets[context + (identity(row),)][side].append(row)
            spec = from_row(row)
            if side == 1 and spec and spec['mode'] == 'upper':
                limits[context].add(spec['high'])
    links, bands = {}, {}
    for key, (ours, theirs) in buckets.items():
        def spec(row):
            value = from_row(row)
            if value and value['mode'] == 'upper':
                previous = [v for v in limits[key[:-1]] if v < value['high']]
                if previous:
                    return dict(value, mode='band', low=max(previous))
            return value
        def compatible(g, c):
            if key[-2] in ('stair', 'precast'):
                gc, cc = (stair_component(r.get('elementGroup')) for r in (g, c))
                return bool(gc and gc == cc and source_band(g.get('elementGroup')) == source_band(c.get('elementGroup')))
            return contains(spec(c), spec(g))
        candidates = {id(c): [g for g in ours if compatible(g, c)] for c in theirs}
        for c in theirs:
            matches = candidates[id(c)]
            if len(matches) != 1:
                continue
            g = matches[0]
            if sum(g in matches for matches in candidates.values()) != 1:
                continue
            links[id(c)] = scope(g)
            if key[-2] not in ('stair', 'precast'):
                bands[id(c)] = spec(c)
    return links, bands
