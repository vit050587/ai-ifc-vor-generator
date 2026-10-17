"""Compare CS operation quantities once without selecting or summing alternative rates."""
import json
from .matching_rules import operation


def mark_operation_families(positions):
    buckets={}
    for p in positions:
        if not isinstance(p.get('quantityCalculation'),dict) or p['quantityCalculation'].get('source')!='final_cs_workbook':continue
        op=operation(p)
        if not op:continue
        key=(p.get('sourceFile'),p.get('groupId'),p.get('unit'),op)
        buckets.setdefault(key,[]).append(p)
    for key,members in buckets.items():
        if len(members)<2:continue
        quantities={round(p['quantity'],6) for p in members if p.get('quantity') is not None}
        if len(quantities)>1:continue
        # A known quantity is authoritative; missing resource quantities do not add volume.
        ordered=sorted(members,key=lambda p:(str(p.get('code')),str(p.get('name'))))
        primary=next((p for p in ordered if p.get('quantity') is not None),ordered[0])
        family=json.dumps(key,ensure_ascii=False)
        for p in members:
            p['_cs_operation_family']=family
            p['_cs_operation_primary']=(p is primary)
            p['_cs_operation_codes']=[m.get('code') for m in ordered]
    return positions


def operation_candidates(offered):
    return [(index,p) for index,p in offered if p.get('_cs_operation_primary',True)]


def link_operation_rows(rows):
    """Show alternatives as linked to an existing comparison without repeating volumes."""
    links={}
    for row in rows:
        if not row.get('generatedWork') or not row.get('customerWork'):continue
        for p in (row.get('detail',{}).get('ifcGroup') or {}).get('positions',[]):
            if p.get('_cs_operation_family'):links.setdefault(p['_cs_operation_family'],row)
    for row in rows:
        if not row.get('generatedWork') or row.get('customerWork'):continue
        positions=(row.get('detail',{}).get('ifcGroup') or {}).get('positions',[])
        primary=[links.get(p.get('_cs_operation_family')) for p in positions]
        if not positions or not all(primary) or len({id(item) for item in primary})!=1:continue
        match=primary[0]
        row.update(operationLink=True,customerWork=match['customerWork'],customerQuantity=None,difference=None,differencePercent=None,
                   quantityAccountingKey=match['quantityAccountingKey'],customerQuantityForTotals=0,generatedQuantityForTotals=0,
                   status='operation_linked',presence='В обоих ВОР',result='Связь установлена через общую операцию',
                   comparisonConfidence='Сопоставлено в составе операции',
                   modelReason='Работа заказчика сопоставлена с общей операцией группы. Эта строка содержит другую расценку той же операции; её отдельный объём у заказчика не выделен.')
        row['comments']=('Связь установлена через общую операцию. Объём заказчика и расхождение приведены в основной строке; повторно не суммировать. '+row.get('comments','')).replace('Требуется сопоставление по наименованию; ','')
    return rows
