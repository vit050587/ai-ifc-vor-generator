"""Conservative exact-name matching for uncoded VOR rows; no fuzzy code guesses."""
import copy
import re
from .normalization_functions import code_normalize
from .units import unit_and_factor


def name_key(value):
    return re.sub(r'[^\w]+',' ',str(value or '').lower().replace('ё','е')).strip()


def match_customer_names(customer, generated):
    from src.services.comparison_vor import template_index
    result=copy.deepcopy(customer)
    candidates={}
    generated_keys=set()
    for p in generated:
        code=code_normalize(p.get('code'))
        unit=unit_and_factor(p.get('unit'))[0]
        if code:
            generated_keys.add((code,unit))
            candidates.setdefault((name_key(p.get('name')),unit),set()).add((code,'exact_name'))
    for code, entries in template_index().items():
        for entry in entries:
            for generated_code,unit in generated_keys:
                if generated_code==code:
                    candidates.setdefault((name_key(entry[3]),unit),set()).add((code,'exact_template_name'))
    for p in result:
        if str(p.get('code') or '').strip():
            p.setdefault('matchBasis','code')
            continue
        matches=candidates.get((name_key(p.get('workAndCostName')),unit_and_factor(p.get('unit'))[0]),set())
        codes={code for code,basis in matches}
        p['originalCode']=None
        if len(codes)==1:
            p['code']=next(iter(codes))
            p['matchBasis']='exact_name' if any(basis=='exact_name' for code,basis in matches) else 'exact_template_name'
        else:
            p['matchBasis']='unresolved'
    return result
