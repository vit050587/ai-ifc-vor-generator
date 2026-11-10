"""Bind whole construction groups before matching their works and resources."""
from collections import defaultdict
import json

from .thickness import from_row, contains
from .group_comparison import _family


def bindings(generated, customer, aliases, row_links):
    from ..comparison_typical import scope
    from .mapping import stair_component
    from .stair_grouping import source_band
    groups = [defaultdict(list), defaultdict(list)]
    for side, rows in enumerate((generated, customer)):
        for row in rows:
            if not row.get('isSeparator'):
                groups[side][scope(row)].append(row)
    active = [{key for key, members in side.items() if any(r.get('quantity') is not None for r in members)}
              for side in groups]
    limits = defaultdict(set)
    def context(key, rows):
        return key[:3]+(_family(rows[0]),)
    def measure(rows):
        specs = {json.dumps(spec,sort_keys=True):spec for r in rows
                 if (spec := from_row(r)) is not None}
        return next(iter(specs.values())) if len(specs)==1 else None
    for key in active[1]:
        spec = measure(groups[1][key])
        if spec and spec['mode']=='upper':
            limits[context(key,groups[1][key])].add(spec['high'])
    def band(spec, ctx):
        if spec and spec['mode']=='upper':
            lower = [v for v in limits[ctx] if v<spec['high']]
            if lower:
                return dict(spec,mode='band',low=max(lower))
        return spec
    evidence = defaultdict(set)
    for ckey, members in groups[1].items():
        if ckey not in active[1]:
            continue
        if ckey in aliases:
            evidence[aliases[ckey]].add(ckey)
        if ckey in active[0]:
            evidence[ckey].add(ckey)
        for row in members:
            if id(row) in row_links:
                evidence[row_links[id(row)]].add(ckey)
    result, bands = {}, {}
    for gkey in active[0]:
        members = groups[0][gkey]
        ctx = context(gkey,members)
        spec = band(measure(members),ctx)
        candidates = set()
        for ckey in active[1]:
            target = groups[1][ckey]
            if context(ckey,target)!=ctx:
                continue
            family = ctx[-1]
            outer = band(measure(target),ctx)
            if family in ('wall','slab','foundation_slab','crane_foundation'):
                compatible = contains(outer,spec)
            elif family in ('stair','precast'):
                gc,cc = stair_component(gkey[-1]),stair_component(ckey[-1])
                compatible = bool(gc and gc==cc and source_band(gkey[-1])==source_band(ckey[-1]))
            else:
                compatible = False
            if compatible:
                candidates.add(ckey)
        # Existing unique foundation/area evidence can establish the object
        # when geometry is missing or is itself a compared parameter.
        if not candidates and len(evidence[gkey])==1:
            candidates = evidence[gkey]
        if len(candidates)==1:
            target = next(iter(candidates))
            result[gkey] = target
            if spec and (outer := band(measure(groups[1][target]),ctx)) and contains(outer,spec):
                bands[target] = outer
    return result,bands
