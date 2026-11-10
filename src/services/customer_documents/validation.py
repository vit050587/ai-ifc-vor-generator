"""Source-based validation; inconsistencies never authorize a model to edit numbers."""
from collections import defaultdict
import math


def uncertainty(source):
    text=str((source or {}).get('text') or '').replace(' ','').replace(',','.')
    if '/' in text: return 0
    digits=len(text.rsplit('.',1)[-1]) if '.' in text else 0
    return .5*10**(-digits)


def validate(facts):
    seen=set(); groups=defaultdict(list);checks=[]
    for fact in facts:
        source=fact['source']
        key=(source.get('page'),source.get('sheet'),source['table'],source['row'],source['column'],source.get('component'))
        if key in seen: raise ValueError('Одна исходная величина повторно включена в документ')
        seen.add(key)
        for name in ('volume_now','volume_was','delta'):
            value=fact.get(name)
            if value is not None and (isinstance(value,bool) or not math.isfinite(value)):
                raise ValueError('В документе обнаружена некорректная числовая величина')
        groups[key[:4]].append(fact)
    for group in groups.values():
        totals=[f for f in group if f['role']=='reinforcement_total' and f['volume_now'] is not None]
        details=[f for f in group if f['role']=='quantity' and f['name'].startswith('Арматура ') and f['unit']=='кг' and f['volume_now'] is not None]
        if totals and details:
            expected=totals[0]['volume_now']; actual=sum(f['volume_now'] for f in details)
            tolerance=sum(uncertainty(f['source']) for f in [totals[0],*details])
            checks.append(dict(kind='reinforcement_sum',source=totals[0]['source'],printed=expected,calculated=actual,
                               status='matched' if abs(actual-expected)<1e-7 else 'source_rounding' if abs(actual-expected)<=tolerance+1e-8 else 'mismatch'))
        counts=[f for f in group if f['role']=='quantity' and f['unit']=='шт' and f['volume_now'] is not None]
        volumes=[f for f in group if f['role']=='quantity' and f['unit']=='м3' and f['volume_now'] is not None]
        units=[f for f in group if f['role']=='unit_measure' and f['unit']=='м3' and f['volume_now'] is not None]
        if len(counts)==len(volumes)==len(units)==1:
            count,unit,total=counts[0],units[0],volumes[0]
            actual=count['volume_now']*unit['volume_now'];expected=total['volume_now']
            tolerance=count['volume_now']*uncertainty(unit['source'])+uncertainty(total['source'])
            checks.append(dict(kind='product_volume',source=total['source'],printed=expected,calculated=actual,
                               status='matched' if abs(actual-expected)<1e-7 else 'source_rounding' if abs(actual-expected)<=tolerance+1e-8 else 'mismatch'))
    return checks
