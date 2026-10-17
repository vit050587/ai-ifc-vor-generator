"""Conservative construction vocabulary and object scopes, independent of volumes."""
import re


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '').lower().replace('ё', 'е')).strip()


def work_name(p):
    return str(p.get('name') or p.get('workAndCostName') or '')


def operation(p):
    name = norm(work_name(p))
    if re.search(r'(?:марш|площадк).*сборн|сборн.*(?:марш|площадк)|установк.*лестничн.*(?:марш|площадк)',name):
        return 'precast_install'
    if re.search(r'арм[- ]?ра|арматур|армирован|установк.*(?:каркас|стержн|сетк|закладн)|^[dдøф⌀]\s*\d+(?:\s*мм)?(?:\s+[аa][1-6]\d{2}[сc]?|$)', name):
        return 'reinforcement'
    if 'опалуб' in name:
        return 'formwork_remove' if re.search(r'демонтаж|разбор|сняти', name) else 'formwork_install' if re.search(r'монтаж|установ|устройств', name) else 'formwork'
    if re.search(r'уход.*бетон|выдерживан.*бетон', name):
        return 'curing'
    if re.search(r'подготовк.*бетон|бетонн.*подготовк',name):
        return 'concrete_prepare'
    if re.search(r'бетонирован|укладк.*бетон', name) or (p.get('unit') in ('м³','м3') and re.search(r'бетон|ж/б|железобетон',name)):
        return 'concrete'
    if re.search(r'засып',name):
        return 'backfill'
    if re.search(r'грунт|котлован', name):
        return 'excavation' if re.search(r'разработ|выемк|рыть', name) else 'backfill' if re.search(r'засып', name) else None
    for key, pattern in [('priming',r'огрунтов|праймер|грунтовк'),('waterstop',r'гидрошпон|набухающ.*профил|аквастоп|холодн.*шов'),('waterproofing',r'гидроизол|мембран|техноэласт'),('insulation',r'утеплен|теплоизол|пенополистир|минераловат')]:
        if re.search(pattern,name):
            return key
    return None


def object_scope(p):
    if p.get('_matching_scope') is not None:
        return dict(p['_matching_scope'])
    el, props = p.get('element') or {}, p.get('properties') or {}
    context = ' '.join(str(v or '') for v in [p.get('sectionName'), el.get('category'), el.get('typeName'), el.get('level'), props.get('location'), props.get('material'), props.get('concreteClass')])
    text = norm(context) if el else norm(work_name(p) + ' ' + context)
    kinds = [('foundation_slab', r'фундаментн\w* плит|плит\w* фундамент'),
             ('footing_specific', r'ростверк|ленточн\w* фундамент'), ('beam', r'балк|ригел'), ('parapet', r'парапет'), ('wall', r'стен'),
             ('column', r'колонн|пилон'),
             ('stair', r'лестниц|лестнич|марш'), ('slab', r'перекрыт|плит'), ('footing', r'фундамент')]
    primary = norm(context) if el else norm(work_name(p))
    kind = next((key for key, pattern in kinds if re.search(pattern, primary)), None)
    kind = kind or next((key for key, pattern in kinds if re.search(pattern, text)), None)
    if kind == 'footing_specific':
        kind = 'footing'
    object_text = norm(context) if el else text
    part = 'under' if re.search(r'подзем|подвал|фундамент|ростверк', object_text) else 'above' if re.search(r'надзем', object_text) else 'plinth' if 'цокол' in object_text else None
    level = re.search(r'(?<!\d)(-?\d+)\s*[-_ ]*(?:го|й|ый)?\s*[-_ ]*этаж', object_text)
    concrete = re.search(r'(?<!\w)[bв]\s*(\d+(?:[.,]\d+)?)', text)
    water = re.search(r'(?<![a-zа-я])[wш]\s*(\d+)',text)
    frost = re.search(r'(?<![a-zа-я])[fф]\s*(\d+)',text)
    thickness = re.search(r'(?:толщин\w*\s*|[tт]\s*=\s*)(\d+(?:[.,]\d+)?)\s*мм', text)
    if not thickness and el:
        thickness = re.search(r'[_ ](\d+(?:[.,]\d+)?)\s*мм', norm(el.get('typeName')))
    level_range = re.search(r'(выше|ниже)\s+(-?\d+)\s*[-_ ]*(?:го|й|ый)?\s*[-_ ]*этаж',object_text)
    if level_range or (el and ';' in str(el.get('level') or '')):
        level = None
    if not part and (level or level_range):
        floor = int((level or level_range).group(1 if level else 2))
        part = 'under' if floor<0 else 'above' if floor>0 else None
    if not part and re.search(r'кров[её]?л|крыш|типов\w*\s+этаж|техническ\w*\s+этаж|перекрыти\w*\s+этаж',object_text):
        part = 'above'
    qualifier = next((word for word in ('благоустрой','маф','паркинг','подпорн','наружн','внутренн') if word in text),None)
    if re.search(r'(?<!\w)бк\d*(?!\w)',text):
        qualifier = 'бк'
    building = re.search(r'корпус\s*([а-яa-z0-9]+)',text)
    # Preserve sections/axes instead of broadening a scoped customer row silently.
    section = re.search(r'секци\w*\s*№?\s*([\d, и-]+)', text)
    axes = re.search(r'ос[яьи]\w*\s+([\dа-яa-z]+\s*[-–/]\s*[\dа-яa-z]+)', text)
    grade = re.search(r'(?<!\w)[аa]\s*([1-6]\d{2}[сc]?)', text)
    subtype_text = norm(context) if el else norm(work_name(p))
    subtype = 'landing' if re.search(r'лестничн\w* площад|площадк.*лестниц|\blanding\b',subtype_text) else 'flight' if re.search(r'марш|\brun\b',subtype_text) else None
    if subtype in ('landing','flight'):
        kind = 'stair'
    production_text=norm(' '.join(str(v or '') for v in [el.get('typeName'),props.get('material'),props.get('materialLayer')])) if el else norm(work_name(p)+' '+context)
    production='precast' if re.search(r'сборн',production_text) else 'monolithic' if re.search(r'монолит',production_text) else None
    if el.get('ifcClass')=='IfcStair':subtype='assembly'
    if el and kind=='slab' and re.search(r'площадк',norm(el.get('typeName'))):kind,subtype='stair','landing'
    product=re.search(r'(?<!\w)(лм[-– ]?\d+(?:[-–]\d+)+)',norm(work_name(p))) if not el else None
    return {'productionType':production,'productMark':product.group(1) if product else None,'waterResistance':water.group(1) if water else None,'frostResistance':frost.group(1) if frost else None,'subtype':subtype,'qualifier':qualifier,'building':building.group(1) if building else None,'levelRange':(':'.join(level_range.groups()) if level_range else None),'rebarClass':norm(p.get('_rebar_class')) or (grade.group(1).replace('с','c') if grade else None),'kind':kind,'part':part,'level':level.group(1) if level else None,
            'concrete':concrete.group(1) if concrete else None,
            'thickness':thickness.group(1) if thickness else None,
            'section':section.group(1).strip() if section else None,
            'axes':axes.group(1) if axes else None}


def compatible(a, b):
    return all(not a.get(key) or not b.get(key) or a[key] == b[key] for key in a)


def object_identity(p):
    return (p.get('sourceFile'), p.get('groupId'))


def rule_match(customer, candidates):
    scope, op = object_scope(customer), operation(customer)
    if not scope['kind'] or not candidates:
        return None
    if not op:
        candidates = [(i,p) for i,p in candidates if norm(work_name(p))==norm(work_name(customer))]
        if not candidates:
            return None
    # Every explicitly stated customer qualifier must be known on the generated side.
    construction_confirmed = (customer.get('_construction_match') or {}).get('confirmed') is True
    def agrees(p,key,value):
        actual=object_scope(p).get(key)
        if not value or actual==value:return True
        if actual is None and key=='rebarClass':return True
        if actual is None and key=='level':
            levels=re.findall(r'(?<!\d)(-?\d+)\s*[-_ ]*(?:го|й|ый)?\s*[-_ ]*этаж',norm((p.get('element') or {}).get('level')))
            return str(value) in levels
        return False
    if not construction_confirmed and any(any(not agrees(p,key,value) for key,value in scope.items()) for _,p in candidates):
        return None
    if not scope['part'] and len({object_scope(p)['part'] for _, p in candidates}) > 1:
        return None
    expected = {'formwork_install','formwork_remove'} if op == 'formwork' else {op}
    if any(operation(p) not in expected for _, p in candidates):
        return None
    # Two alternative rates for the same operation/object must not be summed.
    seen = set()
    for _, p in candidates:
        key = (object_identity(p), operation(p))
        if key in seen:
            return None
        seen.add(key)
    complex_work = op == 'formwork'
    return {'ids':[i for i, _ in candidates], 'relation':'complex' if complex_work else 'aggregate' if len(candidates)>1 else 'equivalent',
            'objectConfirmed':True, 'confidence':None, 'confirmed':not complex_work, 'method':'rules',
            'reason':'Совпали объект, вид работы и совместимые единицы.' +
                     (' Монтаж и демонтаж связаны с общим комплексом опалубки; состав комплекса нужно подтвердить, площади операций не складываются.' if complex_work else '')}
