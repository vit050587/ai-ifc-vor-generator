"""Customer-only grouping and coverage; independent of generated model candidates."""
import hashlib
import json


def source_key(p):
    return json.dumps([p.get(k) for k in ('sourceFiles','estimateNumber','sourceLocation','sectionName','positionNumber','rowNumber','workAndCostName')],sort_keys=True,ensure_ascii=False)


def trace(p):
    yield p
    for field in ('sourceDetailRows','sourceContextRows'):
        for child in p.get(field,[]):yield from trace(child)


def customer_group_manifest(projects):
    from .positions_extractor import PositionsExtractor
    from .units import normalize_positions
    from .construction_matching import attach_constructions,group_constructions
    from .semantic_matching import _customer_groups
    rows=group_constructions(attach_constructions(normalize_positions(PositionsExtractor(projects).run())))
    groups=[]
    for representative,members in _customer_groups(rows):
        keys=sorted({source_key(p) for member in members for p in trace(member)})
        identity=hashlib.sha256(json.dumps(keys,ensure_ascii=False).encode()).hexdigest()[:20]
        groups.append({'id':identity,'sourceRows':keys})
    return {'version':'customer-only-v1','totalGroups':len(groups),'groups':groups}


def customer_group_coverage(manifest,rows):
    source_to_groups={}
    for group in manifest['groups']:
        for source in group['sourceRows']:source_to_groups.setdefault(source,set()).add(group['id'])
    matched=set()
    for row in rows:
        if not row.get('generatedWork') or not row.get('customerWork'):continue
        for member in (row.get('detail',{}).get('projectGroup') or {}).get('positions',[]):
            for p in trace(member):matched.update(source_to_groups.get(source_key(p),set()))
    return {'customerWorkGroups':manifest['totalGroups'],'matchedWorkGroups':len(matched),
            'customerUnpairedGroups':manifest['totalGroups']-len(matched)}
