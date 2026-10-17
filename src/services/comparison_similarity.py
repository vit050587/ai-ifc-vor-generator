"""Balanced coverage of matched logical works, independent of volume differences."""
import json
from .works_comparison.matching_rules import operation


def composition_similarity(rows):
    generated=set();customer=set();matched_generated=set();matched_customer=set()

    def work(p):
        op=operation(p)
        return 'formwork' if op in ('formwork','formwork_install','formwork_remove') else op or p.get('name') or p.get('workAndCostName')

    def generated_key(p):
        # Mounting/removing the same formwork describes one area and one work family.
        if p.get('_cs_operation_family'):
            family=json.loads(p['_cs_operation_family'])
            if work(p)=='formwork':family[-1]='formwork'
            return json.dumps(family,ensure_ascii=False)
        identity=work(p)
        if identity!='formwork':identity=(p.get('code'),p.get('name') or p.get('workAndCostName'))
        return json.dumps([p.get('sourceFile'),p.get('groupId'),p.get('unit'),identity],ensure_ascii=False,sort_keys=True)

    def customer_key(p):
        construction=p.get('_construction_group') or p.get('_construction') or {}
        return json.dumps([p.get('sourceFiles') or p.get('sourceFile'),p.get('estimateNumber'),
                           construction.get('id',p.get('sectionName')),p.get('unit'),work(p)],ensure_ascii=False,sort_keys=True)

    for row in rows:
        if row.get('isEmptyGroup'):continue
        detail=row.get('detail') or {}
        gp=[p for p in (detail.get('ifcGroup') or {}).get('positions',[]) if p.get('positionType')!='material'] if row.get('generatedWork') else []
        cp=(detail.get('projectGroup') or {}).get('positions',[]) if row.get('customerWork') else []
        gkeys={generated_key(p) for p in gp};ckeys={customer_key(p) for p in cp}
        generated.update(gkeys);customer.update(ckeys)
        if gkeys and ckeys:
            matched_generated.update(gkeys);matched_customer.update(ckeys)
    gcoverage=len(matched_generated)/len(generated) if generated else None
    ccoverage=len(matched_customer)/len(customer) if customer else None
    if not generated and not customer:score=None
    elif not gcoverage or not ccoverage:score=0.0
    else:score=2*gcoverage*ccoverage/(gcoverage+ccoverage)
    return {'score':None if score is None else round(score*100,1),
            'generatedCoverage':None if gcoverage is None else round(gcoverage*100,1),
            'customerCoverage':None if ccoverage is None else round(ccoverage*100,1),
            'generatedMatched':len(matched_generated),'generatedTotal':len(generated),
            'customerMatched':len(matched_customer),'customerTotal':len(customer),
            'method':'harmonic_work_coverage',
            'explanation':'Оценка состава работ по установленным связям. Итог — гармоническое среднее долей сопоставленных групп работ двух ВОР. Монтаж и демонтаж одной опалубки считаются одной группой работ. Пустые группы, объёмы и уверенность модели в оценку не входят.'}
