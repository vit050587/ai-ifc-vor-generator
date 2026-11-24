"""Deterministic technology scopes. Alternatives belong to a scope, not a task each."""
import copy
import re
import json
from pathlib import Path
from functools import lru_cache
from .names import type_name


@lru_cache(maxsize=1)
def _configuration():
    path = Path(__file__).resolve().parents[3] / 'data' / 'architecture_table_mapping.json'
    return json.loads(path.read_text())


def _sets():
    return _configuration()['technology_sets']


def _mapped(identity):
    return list(_sets()[identity]['table_codes'])


def has(text, *stems):
    """Match the beginning of a word; 'пол' must not match 'полимер'."""
    return any(re.search(r'(?<![0-9A-Za-zА-Яа-яЁё])' + re.escape(stem)
                         + (r'(?![0-9A-Za-zА-Яа-яЁё])' if stem == 'пол' else ''), text, re.I)
               for stem in stems)


def location(element):
    cls = element.get('ifc_class') or ''
    pre = (element.get('predefined_type') or '').upper()
    name = str(element.get('name') or '').lower()
    if cls == 'IfcDoor': return 'door'
    if cls == 'IfcWindow': return 'window'
    if cls == 'IfcCurtainWall': return 'facade'
    if cls == 'IfcRailing': return 'railing'
    if cls == 'IfcRoof' or pre in ('ROOF', 'ROOFING') or has(name, 'кровл', 'крыш', 'козыр', 'навес', 'воронк'): return 'roof'
    if pre == 'CEILING' or has(name, 'потол'): return 'ceiling'
    if pre == 'FLOORING' or has(name, 'пол_', 'пол ', 'пол:', 'полы', 'наполь', 'floor'): return 'floor'
    if cls == 'IfcWall' or pre == 'CLADDING' or has(name, 'стен', 'перегород'): return 'wall'
    if cls == 'IfcSlab' and pre == 'FLOOR': return 'floor'
    if cls == 'IfcFurnishingElement': return 'furniture'
    return 'other'


def source_components(element):
    """Keep ordered, actual layers. Do not mine unrelated raw property keys."""
    row = element.get('row') or {}
    text = str(element.get('material') or '').strip()
    matches = list(re.finditer(r'(?<![\d.,])\d{1,2}\.\s+(?=[А-ЯЁA-Z]|\d+\s+сло)', text))
    parts = []
    # Project-specific numbered composition has precedence over a flattened
    # summary of IfcMaterialLayer names, which can describe a different assembly.
    if len(matches) > 1:
        for i, match in enumerate(matches):
            parts.append((text[match.end():matches[i+1].start() if i+1 < len(matches) else len(text)].strip(), ''))
    else:
        keys = sorted((key for key in row if re.fullmatch(r'Материальный_слой_\d+_Материал', key)),
                      key=lambda key: int(re.search(r'\d+', key)[0]))
        parts = [(str(row[key]), key.replace('_Материал', '_Толщина_мм')) for key in keys
                 if str(row[key]).strip()]
        if not parts:
            parts = [(text or type_name(element.get('name')), '')]
    result = []
    for index, (name, field) in enumerate(parts, 1):
        # Do not infer a layer thickness from a generic, flattened IFC attribute.
        # Only explicit layer fields or a dimension in that layer's own text.
        if not field:
            depths = list(re.finditer(r'(?<![\d.,])(\d+(?:[.,]\d+)?)(?:\s*[-–]\s*(\d+(?:[.,]\d+)?))?\s*мм\b', name, re.I))
            if depths and not depths[-1][2]:
                field = f'АР_слой_{index}_Толщина_мм'
                row[field] = depths[-1][1].replace(',', '.')
        result.append(dict(id=str(index), name=name, evidence=name, thickness_field=field))
    element['row'] = row
    return result


def substrate(element):
    name = type_name(element.get('name')).lower()
    material = str(element.get('material') or '').lower()
    # The assembly's substrate label precedes its numbered finish layers.
    prefix = re.split(r'(?<!\d)1\.\s*', material, maxsplit=1)[0]
    text = name + ' ' + prefix
    if has(text, 'гкл', 'гипсокартон'): return 'gypsum_board'
    if has(text, 'пгп', 'пазогреб'): return 'pazogreb'
    if re.search(r'(?<![а-я])ж\s*/?\s*б(?![а-я])', text) or has(text, 'бетон'): return 'concrete'
    if has(text, 'кладк', 'кирпич', 'блок'): return 'masonry'
    if has(text, 'дерев', 'древес'): return 'wood'
    if has(text, 'металл', 'сталь'): return 'metal'
    return 'unknown'


def _is_drywall(text):
    # Profiles 'for gypsum board' do not establish a gypsum-board lining.
    text = re.sub(r'для\s+гипсокартон\w*', '', text, flags=re.I)
    return has(text, 'гкл', 'гипсокартон')


def technology_tables(kind, text, name=''):
    """Small sets of technology alternatives, not catalogue-order truncation.

    Unknown material uses a documented generic alternative set. No unrelated
    finishing, substrate, insulation or fastening work is inferred automatically.
    """
    t = text.lower()
    n = name.lower()
    if operation_role(text) == 'adhesive':
        return []
    if has(t, 'покраск', 'окраск', 'краск'):
        routes = _configuration()['finishing_rules']['paint']
        if has(t+' '+n, 'металл', 'сталь') and has(t, 'акрил'):
            return list(routes['metal_acrylic'])
        if has(t, 'маслян'):
            return list(routes['metal_oil'] if has(t+' '+n, 'металл', 'сталь') else routes['oil'])
        if kind in ('wall', 'ceiling') and not has(t, 'эмаль'):
            return list(routes['wall_and_ceiling'])
    if has(t, 'грунтов') and has(t+' '+n, 'металл', 'сталь'):
        return _mapped('3.15-179')
    if kind in ('wall', 'ceiling') and has(t, 'шпатл', 'шпакл'):
        return list(_configuration()['finishing_rules']['putty']['other_substrates'])
    if kind == 'door':
        if has(t, 'алюмин'): return _mapped('3.9-68')
        if has(t, 'пвх', 'пластик'): return _mapped('3.10-83')
        if has(t, 'сталь', 'стальн', 'металл') or re.search(r'(?:^|[_\W])ст[-_]', n):
            return _mapped('3.9-74') if has(t+' '+n, 'противопож', 'ei', 'огнестой') else _mapped('3.9-36|3.9-74')
        if has(t, 'дерев', 'древес') or re.search(r'(?:^|[_\W])др[-_]', n): return _mapped('3.10-32|3.10-81')
        return _mapped('3.10-32|3.9-36|3.10-83')
    if kind == 'window':
        if has(t, 'алюмин'): return _mapped('3.9-83')
        if has(t, 'сталь', 'металл'): return _mapped('3.9-23')
        if has(t, 'пвх', 'пластик') or re.search(r'(?:^|[_\W])пл[_\W]', n): return _mapped('3.10-84')
        # Wooden window tables are resolved below by exact catalogue nouns.
        if has(t, 'дерев') or re.search(r'(?:^|[_\W])др[_\W]', n): return _mapped('3.10-14|3.10-75')
        return _mapped('3.10-84|3.9-83|3.9-23')
    if has(t, 'праймер', 'ультрапраймер'):
        return _mapped('3.12-20') if kind == 'roof' else _mapped('3.11-56')
    if has(t, 'грунтов', 'бетоноконтакт') and kind in ('wall', 'ceiling'): return _mapped('3.15-165')
    if has(t, 'стяжк'):
        if kind == 'roof': return _mapped('3.12-21')
        if has(t, 'сборн', 'сух', 'гипсоструж'): return _mapped('3.11-52')
        return _mapped('3.11-10|3.11-48')
    if has(t, 'пароизоляц'): return _mapped('3.12-19') if kind == 'roof' else _mapped('3.11-38')
    if kind == 'roof' and has(t, 'керамзит', 'засыпк'): return _mapped('3.12-18')
    if kind == 'floor' and has(t, 'керамзит', 'засыпк'): return _mapped('3.11-7')
    if kind == 'floor' and has(t, 'полиэтилен') and has(t, 'плен', 'плён'): return _mapped('3.11-38')
    if has(t, 'утепл', 'теплоизоляц', 'минераловат', 'минват', 'пенополистирол'):
        if kind == 'roof': return _mapped('3.12-17|3.12-18')
        if kind == 'floor': return _mapped('3.11-7') if has(t, 'засып', 'керамзит') else _mapped('3.11-8')
    if kind == 'roof':
        if has(t+' '+n, 'парапет') and has(t+' '+n, 'крышк', 'покрыти', 'колпак'):
            if has(t, 'гранит'): return _mapped('3.15-24')
            if has(t, 'сталь', 'металл', 'оцинк'): return _mapped('3.12-14')
        if has(t, 'примыкан'): return _mapped('3.12-36') if has(t, 'пвх', 'pvc') else _mapped('3.12-8')
        if has(t+' '+n, 'желоб', 'водосточ', 'воронк'): return _mapped('3.12-13')
        if has(t, 'поликарбонат'): return _mapped('3.15-169')
        if has(t, 'стекл', 'светопрозрач'): return _mapped('3.15-207|3.15-238')
        if has(t, 'пвх', 'pvc'): return _mapped('3.12-35')
        if has(t, 'эпдм', 'epdm'): return _mapped('3.12-45')
        if has(t, 'самокле'): return _mapped('3.12-39')
        if has(t, 'битум', 'икопал', 'техноэласт', 'наплавл', 'водоизоляц', 'рулон'):
            return _mapped('3.12-3|3.12-41')
        if has(t, 'мастик'): return _mapped('3.12-4|3.12-5')
        if has(t, 'металлочереп'): return _mapped('3.12-6|3.12-32')
        if has(t, 'профнаст', 'профилирован'): return _mapped('3.9-22')
        if has(t, 'медн'): return _mapped('3.12-7')
        if has(t, 'алюмин') and has(t+' '+n, 'фальц'): return _mapped('3.12-53|3.12-57')
        if has(t, 'бронирован'): return []
        if has(t, 'сталь', 'металл', 'оцинк'): return _mapped('3.12-31|3.12-57')
        return []
    if kind == 'floor':
        if has(t, 'бетон') and has(t, 'плит'): return []
        if has(t, 'гидроизоляц'): return _mapped('3.11-4|3.11-51')
        if has(t, 'керамогранит'): return _mapped('3.11-36')
        if has(t, 'керамич', 'плитк'): return _mapped('3.11-18')
        if has(t, 'линолеум', 'винил', 'гетероген'): return _mapped('3.11-26|3.11-42')
        if has(t, 'ламинат'): return _mapped('3.11-37')
        if has(t, 'паркет'): return _mapped('3.11-24|3.11-25')
        if has(t, 'бетон', 'цемент'): return _mapped('3.11-13|3.11-14')
        if has(t, 'полимер', 'полиуретан'): return _mapped('3.11-49|3.11-50')
        return []
    if kind == 'wall':
        if has(t, 'winal'): return _mapped('3.10-125|3.15-28')
        if has(t, 'керамогранит'): return _mapped('3.15-171|3.15-173')
        if has(t, 'керамич', 'плиточн', 'плитк'): return _mapped('3.15-144|3.15-171')
        if _is_drywall(t):
            if has(n, 'зашив', 'облицов', 'обшив') or has(t, 'облицов'): return _mapped('3.10-95|3.10-96|3.10-97')
            return _mapped('3.10-88|3.10-89|3.10-91')
        if has(t, 'пгп', 'пазогреб'): return _mapped('3.8-50')
        if has(t, 'газобетон', 'газосиликат', 'блок', 'ячеист'): return _mapped('3.8-11|3.8-18')
        if has(t, 'кирпич', 'кладк'): return _mapped('3.8-3|3.8-4')
    return []


def _semantic_fallback(kind, text, catalogue):
    """Technology-specific nouns only; never whole-family 300-table fallbacks."""
    rules = [
        (('штукатур',), ('оштукатуривание поверхностей по камню и бетону внутри', 'штукатурка поверхностей стен')),
        (('шпатл', 'шпакл'), ('подготовка поверхностей',)),
        (('покраск', 'окраск', 'краск', 'эмаль'), ('окраска водно-дисперсионными', 'окраска масляными')),
        (('обои',), ('оклейка обоями',)),
        (('натяжн',), ('устройство натяжных потолков',)),
        (('кассет', 'армстронг', 'armstrong', 'акустич'), ('устройство подвесных потолков из декоративно-акустических',)),
        (('реечн', 'рейк'), ('устройство потолков реечных',)),
        (('гипс', 'гкл'), ('устройство подвесных потолков из гипсоволокнистых',)),
        (('гидроизоляц',), ('устройство гидроизоляции',)),
    ]
    if has(text, 'эмаль'):
        return []
    needles = next((names for signals, names in rules if has(text, *signals)), ())
    if kind == 'window' and has(text, 'дерев'):
        needles = ('установка деревянных блоков в оконных', 'заполнение оконных проемов')
    if kind == 'railing': needles = ('установка металлических ограждений', 'установка ограждений лестниц', 'установка поручней')
    if kind == 'facade': needles = ('устройство вентилируемого фасада',) if has(text, 'вентил') else ('монтаж навесных фасадных модулей со светопрозрачным',)
    matches = [code for code, table in catalogue.items() if code.startswith(('3.8-', '3.9-', '3.10-', '3.11-', '3.12-', '3.15-'))
               and any(table['name'].lower().startswith(needle) for needle in needles)]
    # If a noun still denotes many incompatible technologies, leave it explicit
    # as unresolved instead of fetching every variation or taking the first N.
    return matches if len(matches) <= 6 else []


def operation_role(text):
    # Subsidiary words (glue, welded joints) in a finished covering do not
    # turn the whole floor or tiling layer into a joint/adhesive operation.
    if has(text, 'керамогранит', 'линолеум', 'винил', 'гетероген') or has(text, 'плитк'):
        return 'main'
    if has(text, 'клей') or re.match(r'\s*плиточн\w*\s+клей', text, re.I):
        return 'adhesive'
    for role, signals in (
        ('primer', ('праймер', 'ультрапраймер', 'грунтов', 'бетоноконтакт')),
        ('screed', ('стяжк',)), ('vapour', ('пароизоляц',)),
        ('insulation', ('утепл', 'теплоизоляц', 'минераловат', 'минват', 'пенополистирол')),
        ('junction', ('примыкан', 'шов', 'швов')), ('drainage', ('желоб', 'водосточ')),
        ('adhesive', ('клей',)), ('putty', ('шпатл', 'шпакл')), ('plaster', ('штукатур',)), ('paint', ('покраск', 'окраск', 'краск', 'эмаль'))):
        if has(text, *signals):
            return role
    return 'main'


def resolve(element, catalogue):
    element = copy.deepcopy(element)
    kind = location(element)
    components = source_components(element)
    assumptions, excluded, scopes = [], [], []
    if kind == 'roof':
        from .rate_constraints import layer_count
        # Consecutive bituminous sheets form one multi-layer waterproofing
        # system; price its base and explicit extra-layer norms once.
        merged = []
        for component in components:
            codes = technology_tables(kind, component['name'])
            is_roll = codes == _mapped('3.12-3|3.12-41')
            if is_roll and merged and merged[-1].get('_roll_layers'):
                group = merged[-1]
                group['_source_layers'].append(component)
                group['_roll_layers'] += layer_count(component['name']) or 1
                group['id'] += '+' + component['id']
            else:
                group = dict(component)
                if is_roll:
                    group.update(_roll_layers=layer_count(component['name']) or 1,
                                 _source_layers=[component])
                merged.append(group)
        components = []
        for component in merged:
            layers = component.pop('_source_layers', [])
            count = component.pop('_roll_layers', None)
            if len(layers) > 1:
                text = f'Битумно-полимерный водоизоляционный ковёр, {count} слоя: ' + '; '.join(c['name'] for c in layers)
                component.update(name=text, evidence=text, thickness_field='', source_layers=layers)
                assumptions.append('Смежные слои битумного ковра объединены в одну систему; дополнительные слои учитываются добавкой справочника')
            components.append(component)
    assembly_text = str(element.get('material') or '') + ' ' + type_name(element.get('name'))
    if kind == 'wall' and any(_is_drywall(c['name']) for c in components):
        core, finish = [], []
        for c in components:
            (finish if has(c['name'], 'покраск', 'окраск', 'краск', 'штукатур', 'обои', 'шпатл', 'шпакл', 'керамогранит', 'плитк', 'клей', 'грунтов') else core).append(c)
        if core:
            components = [dict(id='assembly', name=('Каркасная облицовка: ' if has(type_name(element.get('name')), 'зашив', 'облицов', 'обшив') else 'Каркасная перегородка: ') + assembly_text,
                               evidence='; '.join(c['evidence'] for c in core), thickness_field='')] + finish
    if kind == 'wall' and any(has(c['name'], 'winal') for c in components):
        panel, rest = [], []
        for c in components:
            (panel if has(c['name'], 'winal', 'подсистем', 'профил') else rest).append(c)
        components = [dict(id='panels', name='Облицовка стен панелями: ' + '; '.join(c['name'] for c in panel),
                           evidence='; '.join(c['evidence'] for c in panel), thickness_field='',
                           source_layers=panel)] + rest
    for component in components:
        text = component['name']
        if re.search(r'\b(?:см\.?|смотри)\s*(?:.*[-_]|.*раздел\w*\s+)?(?:КР|КЖ)\b', text, re.I):
            excluded.append(dict(component, reason='Конструкция отнесена исходным описанием к КР/КЖ'))
            continue
        codes = technology_tables(kind, text, str(element.get('name') or ''))
        if not codes and operation_role(text) != 'adhesive':
            codes = _semantic_fallback(kind, text.lower(), catalogue)
        absent = set(codes) - set(catalogue)
        if absent:
            raise RuntimeError('Неизвестные таблицы в правилах АР: ' + ', '.join(sorted(absent)))
        if has(text, 'winal'):
            component['mapping_note'] = 'Подобрать облицовку панелями; материал основы панелей и состав подсистемы проверить по источникам, неподтверждённый аналог явно отметить'
        if not codes:
            component['mapping_note'] = 'Нет узкого соответствия технологии в справочнике; работа сохранена без расценки'
        if has(text, 'сборн', 'сух') and has(text, 'стяжк') and kind == 'roof':
            component['mapping_note'] = 'Таблица стяжек проверяется как аналог: сухая сборная стяжка не считается растворной автоматически'
        if operation_role(text) == 'paint' and '3.15-176' in codes and not has(text, 'водно', 'дисперс', 'латекс', 'акрил'):
            component['mapping_note'] = 'Тип краски в составе не указан; водно-дисперсионная окраска рассматривается как вероятная технология, допущение требует проверки по проекту'
        if operation_role(text) == 'putty':
            if substrate(element) == 'pazogreb':
                codes = list(_configuration()['finishing_rules']['putty']['pazogreb'])
            else:
                component['mapping_note'] = 'Дополнительная шпатлёвка: проверить, не учтена ли она выбранной комплексной окраской'
        scopes.append(dict(component, table_codes=codes, quantity_basis='area',
                           name=text, kind=kind, role=operation_role(text), substrate=substrate(element)))
    # Identical primer applications at different interfaces are repeated work,
    # not separate priced rows with the same code. Preserve their multiplicity.
    merged_scopes = []
    primers = {}
    for scope in scopes:
        key = (re.sub(r'\s+', ' ', scope['evidence']).strip().lower(), tuple(scope['table_codes']))
        if scope['role'] == 'primer' and key in primers:
            parent = primers[key]
            parent.setdefault('source_layers', [dict(parent)]).append(scope)
            parent['component_repetitions'] = parent.get('component_repetitions', 1) + 1
        else:
            merged_scopes.append(scope)
            if scope['role'] == 'primer':
                primers[key] = scope
    scopes = merged_scopes
    # Exact construction marks can establish document-only operations. Weak
    # textual matches remain context and never create additional work scopes.
    for record in element.get('document_evidence') or []:
        if record.get('match_strength') != 'exact_type' or record.get('source') != 'ВОР':
            continue
        text = record['name'] + ': ' + record['quote']
        codes = technology_tables(kind, text, str(element.get('name') or ''))
        if not codes or any(set(codes) & set(c['table_codes']) for c in scopes):
            continue
        if set(codes) - set(catalogue):
            raise RuntimeError('Неизвестные таблицы документированной работы АР')
        role = 'main' if kind in ('door', 'window') else operation_role(text)
        same_scope = next((c for c in scopes if c['role'] == role), None)
        if same_scope is not None:
            same_scope['table_codes'] = list(dict.fromkeys(same_scope['table_codes'] + codes))
            same_scope.setdefault('document_alternatives', []).append(record)
            assumptions.append('IFC и документ по одной марке дают разные технологии; ИИ сравнивает их как альтернативы одной работы')
            continue
        scopes.append(dict(id=f'doc-{len(scopes)+1}', name=record['name'], evidence=text,
                           thickness_field='', table_codes=codes, kind=kind,
                           quantity_basis='unknown', document_source=record, role=role,
                           mapping_note='Работа найдена в ВОР по марке конструкции; общий объём ВОР не распределяется на элементы IFC'))
    if any(not scope['table_codes'] for scope in scopes):
        assumptions.append('Часть работ не имеет точного соответствия; они выводятся отдельно без выдуманного шифра')
    return element, kind, scopes, excluded, assumptions
