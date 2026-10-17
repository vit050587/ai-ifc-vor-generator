"""Shared document constants for AI comparison sessions."""
import json
from pathlib import Path

DOC_FILENAMES = {'pos': 'ПОС_глобальные_константы.json', 'pz': 'ПЗ_глобальные_константы.json', 'vor':'ВОР_ведомость_объемов_работ.json', 'pd':'ПД_характеристики_элементов.json'}


def present(value):
    return value is not None and value != ''


def normalize_constant(name, value):
    ranges = {
        'floor_height': [(3.6, 'до 3,6 м'), (4, 'до 4 м'), (6, 'до 6 м')],
        'crane_capacity': [(5, 'до 5 т'), (10, 'до 10 т'), (16, 'до 16 т'), (20, 'до 20 т')],
        'bucket_capacity': [(.25, 'до 0,25 м3'), (.5, 'до 0,5 м3'), (1, 'до 1 м3'), (1.8, 'до 1,8 м3')],
        'equipment_power': [(30, 'до 30 кВт'), (59, '59 кВт'), (79, '79 кВт'), (96, '96 кВт'), (132, '132 кВт')],
    }
    tails = {'floor_height':'свыше 6 м','crane_capacity':'свыше 20 т','bucket_capacity':'3-15 м3 (скрепер)','equipment_power':'свыше 132 кВт'}
    if name == 'building_height_m':
        from .works_table_selector import _height_to_range
        try:
            return _height_to_range(float(value)) or value
        except (ValueError, TypeError):
            return value
    if name in ranges:
        try:
            number = float(str(value).replace(',', '.'))
        except (ValueError, TypeError):
            return value
        return next((label for maximum, label in ranges[name] if number <= maximum), tails[name])
    return value


def resolve_constants(documents, session_dir, schema, overrides=None, use_documents=True):
    """Match the main UI precedence: IFC → POS → PZ → schema default."""
    path = Path(session_dir) / 'IFC_глобальные_константы.json'
    ifc = json.loads(path.read_text(encoding='utf-8')).get('constants', {}) if path.is_file() else {}
    values, rows = {}, []
    for definition in schema:
        name = definition['name']
        value, source, evidence = definition.get('default', ''), 'По умолчанию', {}
        for kind in (('pz', 'pos') if use_documents else ()):
            item = documents.get(kind, {}).get('result', {}).get('constants', {}).get(name, {})
            if present(item.get('value')):
                value, source, evidence = item['value'], 'ПОС' if kind == 'pos' else 'ПЗ', item
        item = ifc.get(name, {})
        if name in ('building_height_m', 'floor_height', 'building_storeys_type') and present(item.get('value')):
            value, source, evidence = item['value'], 'IFC', item
        if name in (overrides or {}):
            value, source, evidence = overrides[name], 'Вручную', {}
        normalized = normalize_constant(name, value)
        if present(normalized):
            values[name] = normalized
        rows.append({'name':name,'title':definition.get('title',name),'value':normalized,'raw_value':value,'source':source,'page':evidence.get('page'),'quote':evidence.get('quote','')})
    return values, rows


def material_choices(sessions, session_id):
    """Use the same material lookup and original row indices as the main preview."""
    import pandas as pd
    import hashlib
    from .ifc_json_builder import clean_element_name
    from .materials_lookup import build_materials_lookup, resolve_material_group_with_code, material_code_options, extract_material_value
    if not hasattr(sessions, '_get_original_excel_path'):
        return []
    path = sessions._get_original_excel_path(session_id)
    if not path:
        return []
    with pd.ExcelFile(path) as book:
        frame = pd.read_excel(book, sheet_name='Данные' if 'Данные' in book.sheet_names else book.sheet_names[0])
    lookup, _ = build_materials_lookup()
    raw = sessions._load_material_layer_map(session_id) or {}
    groups = {}
    for index, row in frame.iterrows():
        value = raw.get(str(row.get('GlobalId')), '') or extract_material_value(row.to_dict())
        name, _, code = resolve_material_group_with_code(value, lookup)
        options = material_code_options(code) if code else []
        if len(options) >= 2:
            object_type = clean_element_name(str(row.get('Имя') or ''))
            part = str(row.get('Тип_этажа') or '')
            identity = (code, object_type, part)
            choice_id = 'material-' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:16]
            entry = groups.setdefault(identity, {'id':choice_id,'code':code, 'name':name, 'object_type':object_type, 'part':part, 'options':options, 'rows':[]})
            entry['rows'].append(int(index))
    return list(groups.values())
