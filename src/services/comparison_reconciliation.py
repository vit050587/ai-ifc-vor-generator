"""Reconcile saved slab/stair links when construction recognition changes."""
import copy
import json
from .works_comparison.matching_rules import object_scope,operation,compatible,norm
from .works_comparison.units import normalize_positions
from .works_comparison.utils import sum_positions_quantity
from .works_comparison.construction_matching import main_section


def reconcile_saved_links(comparison, cs_mode=False):
    from .comparison_vor import position_identity
    comparison=copy.deepcopy(comparison)
    for entry in comparison:
        group=entry.get('ifcGroup')
        if group:
            positions=normalize_positions(group.get('positions',[]))
            group.update(positions=positions,**sum_positions_quantity(positions))
            if positions:
                group['normalized_unit']=positions[0]['unit']
                if not entry.get('projectGroup'):entry['normalized_unit']=positions[0]['unit']
    generated={}
    for entry in comparison:
        for p in normalize_positions((entry.get('ifcGroup') or {}).get('positions',[])):
            generated.setdefault(position_identity(p),p)
    if cs_mode:
        from .works_comparison.cs_operations import mark_operation_families
        mark_operation_families(list(generated.values()))
    result=[];customers={};old_generated=set()
    for original in comparison:
        gp=(original.get('ifcGroup') or {}).get('positions',[])
        cp=(original.get('projectGroup') or {}).get('positions',[])
        untouched=[]
        for source in cp:
            p=copy.deepcopy(source)
            anchor=p.get('_construction') or {}
            fresh=object_scope({'workAndCostName':anchor.get('name') or p.get('workAndCostName'),'sectionName':anchor.get('sectionName') or p.get('sectionName')})
            scope=dict(p.get('_matching_scope') or fresh)
            for key in ('kind','part','subtype','productionType','productMark'):
                if fresh.get(key):scope[key]=fresh[key]
            op=operation(p)
            impacted=scope.get('kind') in ('slab','stair') or (cs_mode and scope.get('kind') in ('wall','foundation_slab','beam','footing','column','parapet'))
            if not impacted:
                untouched.append(p);continue
            p['_matching_scope']=scope
            if p.get('_construction_group'):
                p['_construction_group']['scope']=dict(scope)
            # Preserve different products; merge identical products across sections.
            construction=p.get('_construction_group') or {}
            identity=construction.get('id') or (main_section(p.get('sectionName')),scope.get('productMark') or norm(p.get('workAndCostName')))
            key=json.dumps([p.get('sourceFiles'),identity,p.get('unit'),op],sort_keys=True,ensure_ascii=False)
            item=customers.setdefault(key,{'positions':{},'scope':scope,'op':op,'template':original})
            source_key=json.dumps({k:p.get(k) for k in ('sourceFiles','sourceLocation','sectionName','positionNumber','rowNumber','workAndCostName','quantity')},sort_keys=True,ensure_ascii=False)
            item['positions'].setdefault(source_key,p)
        if untouched or not cp:
            clone=copy.deepcopy(original)
            if cp:clone['projectGroup']={**clone['projectGroup'],'positions':untouched,**sum_positions_quantity(untouched)}
            result.append(clone)
        else:
            old_generated.update(position_identity(p) for p in gp)
    for item in customers.values():
        members=list(item['positions'].values());scope=item['scope'];op=item['op'];unit=members[0].get('unit')
        candidates=[p for p in generated.values() if p.get('positionType')!='material' and p.get('unit')==unit
                    and object_scope(p).get('kind')==scope.get('kind') and object_scope(p).get('subtype')!='assembly'
                    and p.get('_cs_operation_primary',True) and compatible(scope,object_scope(p)) and op
                    and operation(p) in ({'formwork_install','formwork_remove'} if op=='formwork' else {op})]
        if cs_mode:
            candidates=[p for p in candidates if all(not scope.get(key) or object_scope(p).get(key)==scope[key] for key in ('qualifier','building'))]
        if cs_mode and candidates:
            weights={'thickness':3,'concrete':3,'part':1,'subtype':3,'qualifier':5,'building':5,'productionType':2}
            def specificity(p):
                candidate=object_scope(p)
                return sum(weight for key,weight in weights.items() if scope.get(key) and candidate.get(key)==scope[key])
            best=max(map(specificity,candidates))
            candidates=[p for p in candidates if specificity(p)==best]
        binding_groups=list(dict.fromkeys((p.get('sourceFile'),p.get('groupId')) for p in candidates))
        # Multiple alternative rates in one model group cannot all consume one customer quantity.
        alternatives={}
        for p in candidates:alternatives.setdefault((p.get('sourceFile'),p.get('groupId'),operation(p)),set()).add(p.get('code'))
        ambiguous=any(len(codes)>1 for codes in alternatives.values())
        binding={'groups':[list(g) for g in binding_groups],'confirmed':bool(binding_groups),'method':'rules','generated':[],
                 'reason':'Конструкция сопоставлена по типу, расположению и способу изготовления.'}
        confirmed=bool(candidates) and not ambiguous
        reason=binding['reason'] if confirmed else 'Соответствующая работа не установлена.'
        if ambiguous:reason='В группе модели несколько альтернативных расценок на одну операцию; работа заказчика сохранена без распределения объёма между расценками.'
        if scope.get('productionType')=='precast':
            sections=list(dict.fromkeys(p.get('sectionName') for p in members))
            label=max((p.get('workAndCostName') or '' for p in members),key=len)
            group={'id':'precast:'+json.dumps([members[0].get('sourceFiles'),main_section(sections[0]),label],ensure_ascii=False),
                   'label':label,'scope':scope,'sectionName':main_section(sections[0]),'sections':sections,'objects':[]}
            for p in members:p['_construction_group']=copy.deepcopy(group)
        for p in members:p['_construction_match']=copy.deepcopy(binding)
        clone=copy.deepcopy(item['template']);clone['normalized_unit']=unit
        clone['projectGroup']={'normalized_code':clone.get('normalized_code'),'normalized_unit':unit,'positions':members,**sum_positions_quantity(members)}
        clone['ifcGroup']={'normalized_code':clone.get('normalized_code'),'normalized_unit':unit,'positions':candidates,**sum_positions_quantity(candidates)} if confirmed else None
        if cs_mode and len(binding_groups)>1:
            clone['_cs_aggregate_operation']=True
            clone['_shared_customer_set']=True
        clone['semanticMatch']={'confirmed':confirmed,'objectConfirmed':bool(binding_groups),'method':'rules','relation':'complex' if op=='formwork' else 'aggregate','constructionMatch':binding,'reason':reason}
        clone['status']='matched' if confirmed else 'matching_unresolved'
        clone.pop('difference',None);clone.pop('percentageDifference',None)
        result.append(clone)
    if cs_mode:
        for entry in result:
            group=entry.get('ifcGroup')
            if group:group['positions']=[copy.deepcopy(generated.get(position_identity(p),p)) for p in group.get('positions',[])]
    represented={position_identity(p) for e in result for p in (e.get('ifcGroup') or {}).get('positions',[])}
    for key,p in generated.items():
        if key not in represented:
            result.append({'normalized_code':p.get('code'),'normalized_unit':p.get('unit'),'status':'matching_unresolved','projectGroup':None,
                           'ifcGroup':{'normalized_code':p.get('code'),'normalized_unit':p.get('unit'),'positions':[p],**sum_positions_quantity([p])}})
    return result
