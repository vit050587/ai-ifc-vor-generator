"""Recover construction blocks from customer rows and link model groups first."""
import copy
import json
import hashlib
import logging
import re
from pathlib import Path
from .matching_rules import object_scope, operation, work_name, object_identity, compatible


def attach_constructions(positions):
    rows = []
    active, section = None, None
    for index, original in enumerate(positions):
        row = copy.deepcopy(original)
        boundary = (json.dumps(row.get('sourceFiles') or row.get('sourceFile'),ensure_ascii=False),row.get('estimateNumber'),row.get('sectionName'))
        if boundary != section:
            active = None
            section = boundary
        scope = object_scope(row)
        # Concrete/descriptive volume rows introduce the following work block.
        anchor = scope.get('kind') and ((row.get('unit') == 'м³' and operation(row) in ('concrete','concrete_prepare')) or operation(row)=='precast_install')
        if anchor:
            active = {'id':index,'name':work_name(row),'scope':scope,'sourceLocation':row.get('sourceLocation'),
                      'sectionName':row.get('sectionName')}
        if operation(row) in ('excavation','backfill'):
            active = None
        if active:
            inherited = dict(active['scope'])
            # A child supplies its own steel grade; it must not change the concrete object.
            inherited['rebarClass'] = scope.get('rebarClass')
            if anchor:
                inherited = scope
            row['_construction'] = copy.deepcopy(active)
            row['_matching_scope'] = inherited
        rows.append(row)
    return rows


def main_section(name):
    return re.split(r'\s*>\s*секци', str(name or ''), maxsplit=1, flags=re.I)[0].strip()


def group_constructions(rows):
    """Pool customer objects within a main section before looking at model works."""
    buckets = {}
    for row in rows:
        obj = row.get('_construction')
        if not obj:
            continue
        scope = obj['scope']
        key = (json.dumps(row.get('sourceFiles') or row.get('sourceFile'),ensure_ascii=False),row.get('estimateNumber'),
               main_section(obj['sectionName']), *(scope.get(k) for k in ('kind','subtype','part','concrete','waterResistance','frostResistance','qualifier','building','productionType','productMark')))
        bucket = buckets.setdefault(key,{'objects':{},'rows':[]})
        bucket['objects'][obj['id']] = obj
        bucket['rows'].append(row)
    for key,bucket in buckets.items():
        objects = list(bucket['objects'].values())
        scope = dict(objects[0]['scope'])
        for dimension in scope:
            values = {obj['scope'].get(dimension) for obj in objects}
            if len(values)!=1 or dimension in ('section','level','levelRange','axes','rebarClass'):
                scope[dimension] = None
        type_labels = {'foundation_slab':'Фундаментные плиты','footing':'Фундаменты','wall':'Стены','beam':'Балки','parapet':'Парапеты','column':'Колонны','slab':'Плиты','stair':'Лестницы'}
        label = {'landing':'Лестничные площадки','flight':'Лестничные марши'}.get(scope.get('subtype'),type_labels.get(scope.get('kind'),objects[0]['name']))
        parts = {'under':'подземная часть','above':'надземная часть','plinth':'цокольная часть'}
        if scope.get('productionType')=='precast':label+=' сборные'
        if scope.get('productMark'):label+=' '+scope['productMark'].upper()
        label += ', '+parts[scope['part']] if scope.get('part') else ''
        concrete = ' '.join(prefix+str(scope[key]) for key,prefix in [('concrete','B'),('waterResistance','W'),('frostResistance','F')] if scope.get(key))
        label += ', бетон '+concrete if concrete else ''
        group = {'label':label,'id':hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()[:20],
                 'name':objects[0]['name'],'sectionName':key[2],'scope':scope,
                 'sections':list(dict.fromkeys(obj['sectionName'] for obj in objects)),
                 'objects':objects}
        for row in bucket['rows']:
            row['_construction_group'] = group
            row['_matching_scope'] = dict(scope)
    return rows


def match_constructions(rows, generated, llm, cache_path=None, progress=None):
    from .semantic_matching import propose_all, describe
    model_groups = {}
    for position in generated:
        model_groups.setdefault(object_identity(position), position)
    constructions = {(row.get('_construction_group') or row['_construction'])['id']:(row.get('_construction_group') or row['_construction']) for row in rows if row.get('_construction')}
    bindings = {}
    cache_file = Path(cache_path).with_name('construction_match_cache.json') if cache_path else None
    try:
        cache = json.loads(cache_file.read_text()) if cache_file and cache_file.is_file() else {}
        if not isinstance(cache,dict):cache = {}
    except (OSError,ValueError):
        cache = {}
    for identifier, construction in constructions.items():
        scope = construction['scope']
        eligible = [(identity,p) for identity,p in model_groups.items() if object_scope(p).get('subtype')!='assembly' and object_scope(p).get('kind') == scope.get('kind') and compatible(scope,object_scope(p))]
        # Known finer construction types must stay separate (landing versus flight).
        exact = [(identity,p) for identity,p in eligible if all(not scope.get(key) or object_scope(p).get(key)==scope[key] for key in ('subtype','qualifier','building'))]
        if exact:
            weights = {'thickness':3,'concrete':3,'subtype':3,'part':1,'level':1,'section':2,'axes':2,'qualifier':2,'waterResistance':1,'frostResistance':1}
            def specificity(pair):
                candidate_scope = object_scope(pair[1])
                return sum(weight for key,weight in weights.items() if scope.get(key) and scope[key]==candidate_scope.get(key))
            best = max(map(specificity,exact))
            exact = [pair for pair in exact if specificity(pair)==best]
            selected, confirmed, method = exact, True, 'rules'
            reason = 'Конструкция заказчика «'+construction['name']+'» сопоставлена с группами модели по типу и совместимым характеристикам.'
        elif eligible:
            customer = {'name':construction['name'],'sectionName':construction['sectionName'],'unit':'объект','_matching_scope':scope}
            candidates = [(i,dict(p,name=(p.get('element') or {}).get('typeName') or (p.get('element') or {}).get('category') or work_name(p),unit='объект')) for i,(_,p) in enumerate(eligible)]
            try:
                key = hashlib.sha256(json.dumps(['construction-v2',str(getattr(llm,'model','')),describe(customer),[(identity,describe(p)) for identity,p in eligible]],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                from .semantic_matching import validate_answer
                answer = validate_answer(cache[key],candidates) if key in cache else propose_all(llm,customer,candidates)
                cache[key] = answer
                selected = [eligible[i] for i in answer['ids']]
                confirmed, method, reason = answer['confirmed'], 'llm', answer['reason']
            except Exception as exc:
                logging.getLogger(__name__).warning('Сопоставление конструкции %s: %s',construction['name'],exc)
                if cache_file:
                    try:
                        diagnostic = cache_file.with_name('semantic_match_diagnostics.jsonl')
                        diagnostic.parent.mkdir(parents=True,exist_ok=True)
                        with diagnostic.open('a',encoding='utf-8') as stream:
                            stream.write(json.dumps({'stage':'construction','construction':construction,'error':str(exc),'attempts':getattr(exc,'diagnostics',[])},ensure_ascii=False)+'\n')
                    except OSError:
                        pass
                selected, confirmed, method, reason = [], False, 'unresolved', 'Не удалось сопоставить конструкцию: '+str(exc)
        else:
            selected, confirmed, method, reason = [], False, 'unresolved', 'В модели не найдены совместимые группы этой конструкции.'
        if progress:
            progress(len(bindings)+1,len(constructions))
        bindings[identifier] = {'groups':[list(identity) for identity,_ in selected],'confirmed':confirmed,'method':method,'reason':reason,
                                'customer':construction,'generated':[describe(p) for _,p in selected]}
    if cache_file and cache:
        try:
            cache_file.parent.mkdir(parents=True,exist_ok=True)
            temporary = cache_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8')
            temporary.replace(cache_file)
        except OSError as exc:
            logging.getLogger(__name__).warning('Не удалось сохранить связи конструкций: %s',exc)
    for row in rows:
        if row.get('_construction'):
            row['_construction_match'] = bindings[(row.get('_construction_group') or row['_construction'])['id']]
    return rows
