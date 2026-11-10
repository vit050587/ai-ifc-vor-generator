"""Link unique foundation operations independently of differing estimate parameters."""
from collections import defaultdict

from .group_comparison import _family
from .thickness import from_row


def operation_identity(row):
    from ..comparison_typical import identity
    key = list(identity(row))
    if row.get('kind')=='resource':
        from .mapping import classify_resource, parse_concrete_grade
        grade = parse_concrete_grade(row.get('work'))
        if classify_resource(row.get('work'))=='concrete' and grade:
            key[2] = 'бетон '+grade.upper().replace('В','B')
    return tuple(key)


def operation_links(generated, customer):
    from ..comparison_typical import identity, scope
    candidates = [defaultdict(list),defaultdict(list)]
    for side,rows in enumerate((generated,customer)):
        for row in rows:
            if row.get('isSeparator') or row.get('quantity') is None: continue
            family = _family(row)
            if family not in ('foundation_slab','crane_foundation'): continue
            key = scope(row)[:3]+(family,operation_identity(row))
            candidates[side][key].append(row)
    links = {}
    for key,ours in candidates[0].items():
        theirs = candidates[1].get(key,[])
        if len(ours)!=1 or len(theirs)!=1: continue
        g,c = ours[0],theirs[0]
        # A combined source group is governed by the interval/set rules. Never
        # infer which part of that total belongs to a single physical slab.
        if any(spec and spec['mode']!='exact' for spec in (from_row(g),from_row(c))): continue
        if scope(g)!=scope(c) or identity(g)!=identity(c): links[id(c)] = scope(g)
    return links
