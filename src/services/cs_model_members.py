"""Recover exact CS group membership from the same saved selection used by CS."""
import copy
import json
import math
import re
from pathlib import Path


def exact_members(folder,selected):
    import pandas as pd
    from .ifc_reference_builder import split_leaf_groups_by_part,split_leaf_groups_by_name,build_reference_output
    folder=Path(folder);tree_path=folder/'filtered_elements_grouped_mssk.json';rows_path=folder/'filtered_elements.xlsx'
    if not tree_path.is_file() or not rows_path.is_file():return None
    tree=json.loads(tree_path.read_text(encoding='utf-8'))
    frame=pd.read_excel(rows_path,sheet_name='Данные').fillna('')
    rows=frame.to_dict('records');leaves=[]
    def visit(groups,path):
        for original in groups:
            group=copy.deepcopy(original);names=path+[group.get('name','')]
            if group.get('children'):visit(group['children'],names)
            else:group['path']=names;leaves.append(group)
    visit(tree,[])
    split=split_leaf_groups_by_name(split_leaf_groups_by_part(leaves,rows),rows)
    rebuilt=build_reference_output(split,tree)
    if len(rebuilt)!=len(selected):raise ValueError('Не удалось восстановить точный состав групп ЦС')
    fields=('buildingElementName','elementCount','totalMeasure','_typeKey','_buildingPart')
    for original,recovered in zip(selected,rebuilt):
        if any(original.get(k)!=recovered.get(k) for k in fields):raise ValueError('Восстановленная группа ЦС отличается от исходного запроса')
    # build_reference_output preserves order and all split groups have a first element.
    resolved = inherit_stair_storeys([rows])[0]
    return [[resolved[i] for i in group.get('indices',[]) if 0<=i<len(rows)] for group in split if group.get('first_element')]


def inherit_stair_storeys(groups):
    """Recover legacy child storeys only from an unambiguous exported parent ID."""
    groups = copy.deepcopy(groups)
    parents = {}
    for rows in groups:
        for row in rows:
            if row.get('Тип элемента') == 'IfcStair' and row.get('Этаж') not in (None, '', '-'):
                parents.setdefault(str(row.get('Имя')), []).append(row)
    for rows in groups:
        for row in rows:
            if row.get('Этаж') not in (None, '', '-'):
                continue
            name = re.fullmatch(r'(.+:\d+)\s+(?:Run|Landing)(?:\s+\d+)?', str(row.get('Имя') or ''), re.I)
            candidates = parents.get(name.group(1), []) if name else []
            if not candidates or len({p['Этаж'] for p in candidates}) != 1:
                continue
            parent = candidates[0]
            for field in ('Этаж', 'Тип_этажа', 'Уровень_этажа_мм'):
                if parent.get(field) not in (None, '', '-'):
                    row[field] = parent[field]
            row['_storeySource'] = {'source': 'exported_stair_parent', 'parentGuid': parent.get('GlobalId')}
    return groups


def apply_members(model,groups):
    from .group_excel import _element_formwork_area_m2, _find_formwork_columns
    from .works_comparison.units import unit_and_factor
    def number(value):
        try:
            value = float(str(value).replace(',','.'))
            return value if math.isfinite(value) and value >= 0 else 0.0
        except (ValueError,TypeError):return 0.0
    if groups is None:return model
    groups = inherit_stair_storeys(groups)
    if len(groups)!=len(model['elementGroups']):raise ValueError('Не совпал состав групп модели ЦС')
    for group,members in zip(model['elementGroups'],groups):
        el=group['element']
        guids=list(dict.fromkeys(str(row.get('GlobalId')) for row in members if row.get('GlobalId')))
        if len(guids)!=el.get('count'):raise ValueError('Количество GUID группы ЦС не совпало с исходным количеством элементов')
        el['guids']=guids
        levels=list(dict.fromkeys(str(row.get('Этаж')) for row in members if row.get('Этаж') and row.get('Этаж')!='-'))
        el['levels']=levels;el['level']='; '.join(levels) if levels else None
        if members:
            first=members[0]
            el['ifcClass']=first.get('Тип элемента') or el.get('ifcClass')
            el['typeName']=re.sub(r':\d+$','',str(first.get('Имя') or el.get('typeName') or ''))
        is_child = el.get('ifcClass') == 'IfcStairFlight' or bool(re.search(r'\b(?:Run|Landing)\b', el.get('typeName') or '', re.I))
        if is_child:
            parts = {'Подземный' if re.search(r'(?<!\d)-\d+\s*[_ -]*этаж|подвал|подзем', level, re.I) else 'Надземный' for level in levels}
            el['locationStatus'] = 'resolved' if len(parts) == 1 and len(levels) else 'unresolved'
            if len(parts) == 1:
                model_part = 'Подземная часть здания' if 'Подземный' in parts else 'Надземная часть здания'
                group.setdefault('properties', {})['location'] = model_part
            else:
                group.setdefault('properties', {}).pop('location', None)
        if el.get('ifcClass') in ('IfcSlab', 'IfcStairFlight', 'IfcBeam'):
            columns = list(dict.fromkeys(k for row in members for k in row))
            perim, depth = _find_formwork_columns(columns)
            el['comparisonMembers']=[{'guid':str(row.get('GlobalId')),'storey':str(row.get('Этаж') or ''),
                'storeySource': row.get('_storeySource'),
                'volume':number(row.get('Объём, м3')),'area':number(row.get('Площадь, м2')),
                'formwork': _element_formwork_area_m2(row, perim, depth),
                'reinforcement':number(row.get('ReinforcementVolumeRatio'))*number(row.get('Объём, м3'))/1000
                    if 0 < number(row.get('ReinforcementVolumeRatio')) < 500 else 0} for row in members]
        if el.get('ifcClass') == 'IfcBeam':
            areas = [member['formwork'] for member in el.get('comparisonMembers', [])]
            complete = bool(areas) and all(area > 0 for area in areas)
            for position in group.get('positions', []):
                unit, factor = unit_and_factor(position.get('unit'))
                if unit != 'м²' or 'опалуб' not in str(position.get('name') or '').lower():
                    continue
                position.setdefault('quantityBeforeGeometryCheck', position.get('quantity'))
                position['quantity'] = sum(areas) / float(factor) if complete else None
                position['quantityStatus'] = 'calculated' if complete else 'missing_data'
                position['quantityCalculation'] = {**(position.get('quantityCalculation') or {}),
                    'source': 'explicit_beam_formwork', 'reason': 'Площадь контакта с опалубкой из свойств элементов.' if complete else
                    'Площадь контакта балки с опалубкой не задана. Полная поверхность IFC не используется как площадь опалубки.',
                    'missingElements': sum(area <= 0 for area in areas), 'totalElements': len(areas)}
    return model


def split_slab_roles(models):
    """Allocate mixed source groups by element measures, conserving every source quantity."""
    import re
    from .works_comparison.units import unit_and_factor
    output=copy.deepcopy(models)
    for model in output:
        groups=[]
        for source in model.get('elementGroups',[]):
            from .works_comparison.matching_rules import object_scope
            scope=object_scope({'element':source.get('element') or {},'properties':source.get('properties') or {}})
            element = source.get('element') or {}
            is_slab = scope.get('kind') == 'slab' and not scope.get('subtype')
            is_stair = scope.get('kind') == 'stair' and scope.get('subtype') in ('landing', 'flight')
            members=(element.get('comparisonMembers') or []) if is_slab or is_stair else []
            buckets={}
            for member in members:
                match=re.search(r'(?<!\d)(-?\d+)\s*[-_ ]*(?:го|й|ый)?\s*[-_ ]*этаж',member['storey'].lower())
                if is_slab:
                    role='basement_cover' if match and int(match.group(1))<=1 else 'upper_slabs' if match or re.search(r'крыш|кров', member['storey'], re.I) else 'unresolved'
                else:
                    role='under' if (match and int(match.group(1))<0) or re.search(r'подвал|подзем', member['storey'], re.I) else 'above' if match or re.search(r'надзем', member['storey'], re.I) else 'unresolved'
                buckets.setdefault(role,[]).append(member)
            if not buckets:groups.append(source);continue
            if is_stair and len(buckets) == 1:
                groups.append(source);continue
            for role,part in buckets.items():
                group=copy.deepcopy(source);el=group['element'];original_id=source.get('sourceGroupId') or source['groupId']
                group['sourceGroupId']=original_id;el['sourceGroupId']=original_id
                if len(buckets)>1:group['groupId']=original_id+'::'+role
                el['structuralRole']=role;el['guids']=[p['guid'] for p in part];el['count']=len(part)
                el['levels']=list(dict.fromkeys(p['storey'] for p in part));el['level']='; '.join(el['levels'])
                el['comparisonMembers']=part
                el['locationStatus'] = 'unresolved' if role == 'unresolved' else 'resolved'
                if role == 'unresolved':
                    group.setdefault('properties',{}).pop('location', None)
                else:
                    group.setdefault('properties',{})['location']='Подземная часть здания' if role in ('basement_cover', 'under') else 'Надземная часть здания'
                for position in group['positions']:
                    position['_sourceGroupId']=original_id
                    if len(buckets)<2:continue
                    unit,factor=unit_and_factor(position.get('unit'))
                    measure={'м³':'volume','м²':'formwork' if 'опалуб' in str(position.get('name') or '').lower() else 'area','т':'reinforcement','шт.':'count'}.get(unit)
                    if measure == 'formwork' and not any('formwork' in member for member in members):
                        measure = 'area'  # Older saved membership without formwork measures.
                    weights=[1 if measure=='count' else member.get(measure,0) for member in members] if measure else []
                    total=sum(weights);subtotal=sum(1 if measure=='count' else member.get(measure,0) for member in part) if measure else 0
                    old=position.get('quantity')
                    position['_sourceQuantity']=old
                    if old is not None:
                        if not total:position['quantity']=None;position['quantityStatus']='missing_allocation_measure'
                        else:position['quantity']=float(old)*subtotal/total
                    calculation = position.get('quantityCalculation') or {}
                    position['quantityCalculation'] = {**calculation, 'allocation': {'sourceGroupId':original_id,'role':role,'basis':'storey_name','measure':measure,'fraction':subtotal/total if total else None,'sourceQuantity':old,'elementCount':len(part)}}
                groups.append(group)
        model['elementGroups']=groups
    return output
