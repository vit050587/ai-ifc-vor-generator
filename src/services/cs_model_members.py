"""Recover exact CS group membership from the same saved selection used by CS."""
import copy
import json
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
    return [[rows[i] for i in group.get('indices',[]) if 0<=i<len(rows)] for group in split if group.get('first_element')]


def apply_members(model,groups):
    import re
    def number(value):
        try:return float(str(value).replace(',','.'))
        except (ValueError,TypeError):return 0.0
    if groups is None:return model
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
        if el.get('ifcClass')=='IfcSlab' and 'площадк' not in (el.get('typeName') or '').lower():
            el['comparisonMembers']=[{'guid':str(row.get('GlobalId')),'storey':str(row.get('Этаж') or ''),
                'volume':number(row.get('Объём, м3')),'area':number(row.get('Площадь, м2')),
                'reinforcement':number(row.get('ReinforcementVolumeRatio'))*number(row.get('Объём, м3'))/1000} for row in members]
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
            members=((source.get('element') or {}).get('comparisonMembers') or []) if scope.get('kind')=='slab' and not scope.get('subtype') else []
            buckets={}
            for member in members:
                match=re.search(r'(?<!\d)(-?\d+)\s*[-_ ]*(?:го|й|ый)?\s*[-_ ]*этаж',member['storey'].lower())
                role='basement_cover' if match and int(match.group(1))<=1 else 'upper_slabs'
                buckets.setdefault(role,[]).append(member)
            if not buckets:groups.append(source);continue
            for role,part in buckets.items():
                group=copy.deepcopy(source);el=group['element'];original_id=source['groupId']
                group['sourceGroupId']=original_id;el['sourceGroupId']=original_id
                if len(buckets)>1:group['groupId']=original_id+'::'+role
                el['structuralRole']=role;el['guids']=[p['guid'] for p in part];el['count']=len(part)
                el['levels']=list(dict.fromkeys(p['storey'] for p in part));el['level']='; '.join(el['levels'])
                el['comparisonMembers']=part
                group.setdefault('properties',{})['location']='Подземная часть здания' if role=='basement_cover' else 'Надземная часть здания'
                for position in group['positions']:
                    position['_sourceGroupId']=original_id
                    if len(buckets)<2:continue
                    unit,factor=unit_and_factor(position.get('unit'))
                    measure={'м³':'volume','м²':'area','т':'reinforcement','шт.':'count'}.get(unit)
                    weights=[1 if measure=='count' else member.get(measure,0) for member in members] if measure else []
                    total=sum(weights);subtotal=sum(1 if measure=='count' else member.get(measure,0) for member in part) if measure else 0
                    old=position.get('quantity')
                    position['_sourceQuantity']=old
                    if old is not None:
                        if not total:position['quantity']=None;position['quantityStatus']='missing_allocation_measure'
                        else:position['quantity']=float(old)*subtotal/total
                    position.setdefault('quantityCalculation',{})['allocation']={'sourceGroupId':original_id,'role':role,'measure':measure,'fraction':subtotal/total if total else None,'sourceQuantity':old,'elementCount':len(part)}
                groups.append(group)
        model['elementGroups']=groups
    return output
