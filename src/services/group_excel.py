"""
IFC Elements Grouping Pipeline
Input: Excel file with IFC elements
Output: Excel file with hierarchical grouping + JSON file
"""

import pandas as pd
import json
import re
from typing import List, Dict, Any
from collections import defaultdict
from pathlib import Path
import openpyxl
from openpyxl.utils import get_column_letter


# ========== Configuration ==========
GEOMETRY_GROUP_RULES = {
    'IfcWall': {
        'field': 'Длина_Width_мм',
        'label': 'Толщина',
        'unit': 'мм',
        'ranges': [
            {'max': 100, 'label': 'до 100 мм'},
            {'max': 150, 'label': 'до 150 мм'},
            {'max': 200, 'label': 'до 200 мм'},
            {'max': 300, 'label': 'до 300 мм'},
            {'max': float('inf'), 'label': 'более 300 мм'}
        ]
    },
    'IfcSlab': {
        'field': 'Площадь_NetArea_м2',
        'label': 'Площадь',
        'unit': 'м²',
        'ranges': [
            {'max': 10, 'label': 'до 10 м²'},
            {'max': 20, 'label': 'до 20 м²'},
            {'max': float('inf'), 'label': 'более 20 м²'}
        ]
    },
    'IfcColumn': {
        'field': 'Длина_Length_мм',
        'label': 'Длина',
        'unit': 'мм',
        'ranges': [
            {'max': 1200, 'label': 'до 1200 мм'},
            {'max': float('inf'), 'label': 'более 1200 мм'}
        ]
    },
    'IfcBeam': {
        'field': 'Длина_Length_мм',
        'label': 'Длина',
        'unit': 'мм',
        'ranges': [
            {'max': 3000, 'label': 'до 3000 мм'},
            {'max': 6000, 'label': 'до 6000 мм'},
            {'max': float('inf'), 'label': 'более 6000 мм'}
        ]
    },
    'IfcStair': {
        'field': 'Длина_Length_мм',
        'label': 'Длина',
        'unit': 'мм',
        'ranges': [
            {'max': 2000, 'label': 'до 2000 мм'},
            {'max': 4000, 'label': 'до 4000 мм'},
            {'max': float('inf'), 'label': 'более 4000 мм'}
        ]
    },
    'IfcProxyElement': {
        'field': 'Объём_NetVolume_м3',
        'label': 'Объём',
        'unit': 'м³',
        'ranges': [
            {'max': float('inf'), 'label': 'все размеры'}
        ]
    },
    'default': {
        'field': 'Объём_NetVolume_м3',
        'label': 'Объём',
        'unit': 'м³',
        'ranges': [
            {'max': 1, 'label': 'до 1 м³'},
            {'max': 5, 'label': 'до 5 м³'},
            {'max': float('inf'), 'label': 'более 5 м³'}
        ]
    }
}


SECTION_STRUCTURE = {
    'Подземная': {
        'label': 'Подземная часть здания (до отм. 0,000)',
        'sections': [
            {
                'name': 'Раздел 1. Монолитные ж/б конструкции. Фундаменты',
                'subsections': [
                    {'key': 'Фундаментная плита', 'patterns': ['фундаментная плита', 'фунд. плита', 'фундамент плита'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Свайно-ростверковый фундамент', 'patterns': ['свай', 'ростверк'], 'ifcTypes': ['IfcSlab', 'IfcBeam', 'IfcPile']},
                    {'key': 'Фундамент под инженерное оборудование', 'patterns': ['инженер', 'оборудование'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Фундамент под башенный кран', 'patterns': ['башен', 'кран'], 'ifcTypes': ['IfcSlab', 'IfcBeam']},
                    {'key': 'Устройство горизонтальной гидроизоляции', 'patterns': ['горизонт', 'гидроизол'], 'ifcTypes': ['IfcSlab', 'IfcWall']},
                    {'key': 'Устройство вертикальной гидроизоляции', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Устройство деформационного шва', 'patterns': ['деформац', 'шов'], 'ifcTypes': ['IfcSlab', 'IfcWall']}
                ]
            },
            {
                'name': 'Раздел 2. Монолитные ж/б конструкции. Подземная часть здания',
                'subsections': [
                    {'key': 'Подземная часть здания. Стены', 'patterns': ['стен'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Подземная часть здания. Колонны', 'patterns': ['колонн'], 'ifcTypes': ['IfcColumn']},
                    {'key': 'Подземная часть здания. Плиты перекрытия', 'patterns': ['перекрыт', 'плит'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Подземная часть здания. Балки', 'patterns': ['балк', 'ригел'], 'ifcTypes': ['IfcBeam']},
                    {'key': 'Подземная часть здания. Лестницы', 'patterns': ['лестн', 'марш', 'площадк'], 'ifcTypes': ['IfcStair', 'IfcSlab', 'IfcStairFlight']},
                    {'key': 'Подземная часть здания. Приямки', 'patterns': ['приям'], 'ifcTypes': ['IfcSlab', 'IfcWall']},
                    {'key': 'Подземная часть здания. Вертикальная гидроизоляция', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']}
                ]
            }
        ],
        'other_label': 'Прочие элементы подземной части'
    },
    'Цоколь': {
        'label': 'Цокольная часть здания (отм. 0,000)',
        'sections': [
            {
                'name': 'Раздел 1. Монолитные ж/б конструкции. Фундаменты',
                'subsections': [
                    {'key': 'Фундаментная плита', 'patterns': ['фундаментная плита', 'фунд. плита', 'фундамент плита'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Свайно-ростверковый фундамент', 'patterns': ['свай', 'ростверк'], 'ifcTypes': ['IfcSlab', 'IfcBeam', 'IfcPile']},
                    {'key': 'Фундамент под инженерное оборудование', 'patterns': ['инженер', 'оборудование'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Фундамент под башенный кран', 'patterns': ['башен', 'кран'], 'ifcTypes': ['IfcSlab', 'IfcBeam']},
                    {'key': 'Устройство горизонтальной гидроизоляции', 'patterns': ['горизонт', 'гидроизол'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Устройство вертикальной гидроизоляции', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Устройство деформационного шва', 'patterns': ['деформац', 'шов'], 'ifcTypes': ['IfcSlab', 'IfcWall']}
                ]
            },
            {
                'name': 'Раздел 2. Монолитные ж/б конструкции. Цокольная часть здания',
                'subsections': [
                    {'key': 'Цокольная часть здания. Стены', 'patterns': ['стен'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Цокольная часть здания. Колонны', 'patterns': ['колонн'], 'ifcTypes': ['IfcColumn']},
                    {'key': 'Цокольная часть здания. Плиты перекрытия', 'patterns': ['перекрыт', 'плит'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Цокольная часть здания. Балки', 'patterns': ['балк', 'ригел'], 'ifcTypes': ['IfcBeam']},
                    {'key': 'Цокольная часть здания. Лестницы', 'patterns': ['лестн', 'марш', 'площадк'], 'ifcTypes': ['IfcStair', 'IfcSlab', 'IfcStairFlight']},
                    {'key': 'Цокольная часть здания. Приямки', 'patterns': ['приям'], 'ifcTypes': ['IfcSlab', 'IfcWall']},
                    {'key': 'Цокольная часть здания. Вертикальная гидроизоляция', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']}
                ]
            }
        ],
        'other_label': 'Прочие элементы цокольной части'
    },
    'Надземная': {
        'label': 'Надземная часть здания (выше отм. 0,000)',
        'sections': [
            {
                'name': 'Раздел 3. Монолитные ж/б конструкции. Надземная часть здания',
                'subsections': [
                    {'key': 'Надземная часть здания. Стены', 'patterns': ['стен'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Надземная часть здания. Перекрытия', 'patterns': ['перекрыт', 'плит'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Надземная часть здания. Колонны', 'patterns': ['колонн'], 'ifcTypes': ['IfcColumn']},
                    {'key': 'Надземная часть здания. Балки', 'patterns': ['балк', 'ригел'], 'ifcTypes': ['IfcBeam']},
                    {'key': 'Надземная часть здания. Парапеты', 'patterns': ['парапет'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Надземная часть здания. Лестничные площадки', 'patterns': ['лестничн', 'площадк'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Надземная часть здания. Лестничные марши', 'patterns': ['лестничн', 'марш'], 'ifcTypes': ['IfcStair', 'IfcStairFlight']},
                    {'key': 'Надземная часть здания. Вертикальная гидроизоляция', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']}
                ]
            }
        ],
        'other_label': 'Прочие элементы надземной части'
    }
}


# ========== Utility Functions ==========
def safe_parse_float(value: Any) -> float:
    if value is None or value == '' or value == '-':
        return 0.0
    try:
        cleaned = str(value).replace(' ', '').replace(',', '.')
        cleaned = re.sub(r'[^\d.\-]', '', cleaned)
        return float(cleaned) if cleaned else 0.0
    except (ValueError, TypeError):
        return 0.0


def round_value(value: float, column_name: str) -> float:
    col_lower = column_name.lower()
    if 'м3' in col_lower or 'литр' in col_lower:
        return round(value, 3)
    elif 'м2' in col_lower:
        return round(value, 2)
    elif 'мм' in col_lower:
        return round(value)
    else:
        return round(value, 2)


# ========== Classification Functions ==========
def get_ifc_type(type_ru: str, name: str) -> str:
    name_lower = name.lower() if name else ''
    type_lower = type_ru.lower() if type_ru else ''
    
    # Проверка на отверстия/проёмы/IfcProxyElement — ДОЛЖНА БЫТЬ ПЕРВОЙ
    if any(w in name_lower for w in ['отверсти', 'проём', 'проем', 'окно', 'двер']):
        return 'IfcOpening'
    if 'ifcproxy' in type_lower or 'proxy' in type_lower:
        return 'IfcProxyElement'
    
    if any(w in name_lower for w in ['стен', 'парапет']):
        return 'IfcWall'
    if any(w in name_lower for w in ['колонн', 'пилон']):
        return 'IfcColumn'
    if any(w in name_lower for w in ['балк', 'ригел', 'перемычк']):
        return 'IfcBeam'
    if any(w in name_lower for w in ['лестн', 'марш']):
        return 'IfcStair'
    if 'площадк' in name_lower:
        return 'IfcSlab'
    if any(w in name_lower for w in ['перекрыт', 'плит', 'приям', 'фундамент']):
        return 'IfcSlab'
    
    if 'ifcwall' in type_lower:
        return 'IfcWall'
    if 'ifcslab' in type_lower:
        return 'IfcSlab'
    if 'ifccolumn' in type_lower:
        return 'IfcColumn'
    if 'ifcbeam' in type_lower:
        return 'IfcBeam'
    if any(t in type_lower for t in ['ifcstair', 'ifcstairflight']):
        return 'IfcStair'
    
    return 'default'


def is_hydro_vertical(type_ru: str, name: str, ifc_type: str) -> bool:
    """Определяет, относится ли элемент к ВЕРТИКАЛЬНОЙ гидроизоляции (стены)"""
    name_lower = name.lower() if name else ''
    # Вертикальная = гидроизоляция + стена (или вертикальная в названии)
    has_hydro = 'гидроизол' in name_lower
    has_vertical = 'вертикал' in name_lower
    has_wall = 'стен' in name_lower or ifc_type == 'IfcWall'
    return has_hydro and (has_vertical or has_wall)


def is_hydro_horizontal(type_ru: str, name: str, ifc_type: str) -> bool:
    """Определяет, относится ли элемент к ГОРИЗОНТАЛЬНОЙ гидроизоляции (плиты/перекрытия)"""
    name_lower = name.lower() if name else ''
    has_hydro = 'гидроизол' in name_lower
    has_horizontal = 'горизонт' in name_lower
    has_slab = ifc_type == 'IfcSlab'
    # Горизонтальная = гидроизоляция + плита (или горизонтальная в названии)
    # Если уже определена как вертикальная — не горизонтальная
    if is_hydro_vertical(type_ru, name, ifc_type):
        return False
    return has_hydro and (has_horizontal or has_slab)


def determine_subsection(type_ru: str, name: str, part: str) -> str:
    name_lower = name.lower() if name else ''
    type_lower = type_ru.lower() if type_ru else ''
    
    # Сначала определяем ifc_type для гидроизоляции
    temp_ifc_type = get_ifc_type(type_ru, name)
    
    # Проверка на IfcProxyElement — всегда в «Прочие элементы»
    if 'ifcproxy' in type_lower or 'proxy' in type_lower:
        return '__OTHER__'
    
    # Отверстия — тоже в прочие
    if temp_ifc_type == 'IfcOpening':
        return '__OTHER__'
    
    # Гидроизоляция: чёткое разделение
    if is_hydro_vertical(type_ru, name, temp_ifc_type):
        return 'Устройство вертикальной гидроизоляции'
    if is_hydro_horizontal(type_ru, name, temp_ifc_type):
        return 'Устройство горизонтальной гидроизоляции'
    
    if 'приям' in name_lower:
        mapping = {'Подземная': 'Подземная часть здания. Приямки',
                   'Цоколь': 'Цокольная часть здания. Приямки'}
        return mapping.get(part, 'Надземная часть здания. Приямки')
    
    if 'парапет' in name_lower:
        return 'Надземная часть здания. Парапеты'
    
    if 'лестничн' in name_lower or 'лестниц' in name_lower:
        if 'площадк' in name_lower:
            mapping = {'Подземная': 'Подземная часть здания. Лестницы',
                       'Цоколь': 'Цокольная часть здания. Лестницы'}
            return mapping.get(part, 'Надземная часть здания. Лестничные площадки')
        if 'марш' in name_lower:
            mapping = {'Подземная': 'Подземная часть здания. Лестницы',
                       'Цоколь': 'Цокольная часть здания. Лестницы'}
            return mapping.get(part, 'Надземная часть здания. Лестничные марши')
        mapping = {'Подземная': 'Подземная часть здания. Лестницы',
                   'Цоколь': 'Цокольная часть здания. Лестницы'}
        return mapping.get(part, 'Надземная часть здания. Лестничные марши')
    
    part_structure = SECTION_STRUCTURE.get(part)
    if part_structure:
        for section in part_structure['sections']:
            for subsection in section['subsections']:
                if any(pattern in name_lower for pattern in subsection['patterns']):
                    return subsection['key']
    
    type_mapping = {
        'wall': 'Стены',
        'slab': 'Плиты перекрытия',
        'column': 'Колонны',
        'beam': 'Балки',
        'stair': 'Лестницы'
    }
    for key, value in type_mapping.items():
        if key in type_lower:
            return f'{part} часть здания. {value}'
    
    return '__OTHER__'


# ========== Core Grouping Logic ==========
class ElementData:
    def __init__(self, index: int, row: Dict[str, Any], headers: List[str]):
        self.index = index
        self.row = row
        type_ru_idx = headers.index('Тип элемента') if 'Тип элемента' in headers else -1
        name_idx = headers.index('Имя') if 'Имя' in headers else -1
        self.type_ru = row.get(headers[type_ru_idx], 'Неизвестно') if type_ru_idx >= 0 else 'Неизвестно'
        self.name = row.get(headers[name_idx], '') if name_idx >= 0 else ''
        self.part = row.get('Часть здания', 'Надземная')
        self.subsection = determine_subsection(self.type_ru, self.name, self.part)
        self.ifc_type = get_ifc_type(self.type_ru, self.name)


def group_elements(rows: List[Dict[str, Any]], headers: List[str]) -> List[Dict[str, Any]]:
    """
    Группировка элементов по иерархии.
    """
    sum_columns = [h for h in headers if any(keyword in h.lower() for keyword in ['объём', 'объем', 'площадь', 'стоимость'])]
    
    def get_part_from_floor_type(floor_type: str) -> str:
        if not floor_type:
            return 'Надземная'
        
        floor_lower = floor_type.lower().strip()
        
        if any(w in floor_lower for w in ['подзем', 'подвал', 'basement', '-1']):
            return 'Подземная'
        if any(w in floor_lower for w in ['цокол', 'ground', 'нулев']):
            return 'Цоколь'
        if any(w in floor_lower for w in ['надзем', 'этаж', 'кровл', 'техническ', 'мансард']):
            return 'Надземная'
        
        return 'Надземная'
    
    def get_concrete_group(elem: ElementData) -> str:
        concrete_grade = str(elem.row.get('ExpCheck_MaterialConcrete_MGE_ConcreteGrade', ''))
        water_resist = str(elem.row.get('ExpCheck_MaterialConcrete_MGE_WaterResist', ''))
        freeze_durability = str(elem.row.get('ExpCheck_MaterialConcrete_MGE_FreezeDurability', ''))
        
        parts = []
        if concrete_grade and concrete_grade != '-' and concrete_grade != '':
            parts.append(concrete_grade)
        if water_resist and water_resist != '-' and water_resist != '':
            parts.append(f"W{water_resist}" if not water_resist.startswith('W') else water_resist)
        if freeze_durability and freeze_durability != '-' and freeze_durability != '':
            parts.append(f"F{freeze_durability}" if not freeze_durability.startswith('F') else freeze_durability)
        
        if parts:
            return f"Бетон: {', '.join(parts)}"
        else:
            return "Бетон: без характеристик"
    
    def get_name_group(elem: ElementData) -> str:
        """Группировка по имени (для прочих элементов)"""
        name = elem.name.strip() if elem.name else 'Без названия'
        # Очищаем имя от ID в конце (после последнего двоеточия)
        last_colon = name.rfind(':')
        if last_colon != -1:
            after = name[last_colon+1:].strip()
            if after.isdigit():
                name = name[:last_colon].strip()
        return name if name else 'Без названия'
    
    elements = []
    
    for i, row in enumerate(rows):
        floor_type = str(row.get('Тип_этажа', ''))
        part = get_part_from_floor_type(floor_type)
        
        type_ru = str(row.get('Тип элемента', 'Неизвестно'))
        name = str(row.get('Имя', ''))
        
        elem = ElementData(i, row, headers)
        elem.part = part
        elem.subsection = determine_subsection(type_ru, name, part)
        
        elements.append(elem)
    
    def get_volume(elem: ElementData) -> float:
        for h in headers:
            h_lower = h.lower()
            if 'netvolume' in h_lower or ('объём' in h_lower and 'м3' in h_lower):
                return safe_parse_float(elem.row.get(h, 0))
        return 0.0
    
    def get_geometry_value(rule: dict, elem: ElementData) -> float:
        if not rule or not rule.get('field'):
            return get_volume(elem)
        field = rule['field']
        if field not in headers:
            return get_volume(elem)
        return safe_parse_float(elem.row.get(field, 0))
    
    def calculate_areas(elems: List[ElementData]) -> dict:
        areas = {}
        for col in sum_columns:
            if 'площадь' in col.lower():
                total = sum(safe_parse_float(e.row.get(col, 0)) for e in elems)
                if total > 0:
                    areas[col] = round_value(total, col)
        return areas
    
    def create_group(name: str, level: int, elems: List[ElementData], children: List[dict] = None) -> dict:
        if not elems:
            return None
        volume = sum(get_volume(e) for e in elems)
        return {
            'name': name,
            'level': level,
            'indices': sorted([e.index for e in elems]),
            'total_volume': round(volume, 2),
            'total_areas': calculate_areas(elems),
            'first_element': dict(elems[0].row) if elems else {},
            'count': len(elems),
            'children': children or []
        }
    
    result = []
    part_order = ['Подземная', 'Цоколь', 'Надземная']
    
    for part in part_order:
        part_elements = [e for e in elements if e.part == part]
        
        if not part_elements:
            continue
        
        part_structure = SECTION_STRUCTURE.get(part)
        if not part_structure:
            continue
        
        part_group = create_group(part_structure['label'], 1, part_elements)
        if not part_group:
            continue
        
        # Разделяем на «обычные» и «прочие» (IfcProxyElement, IfcOpening, __OTHER__)
        regular_elements = []
        other_elements = []
        
        for e in part_elements:
            if e.subsection == '__OTHER__' or e.ifc_type in ('IfcProxyElement', 'IfcOpening'):
                other_elements.append(e)
            else:
                regular_elements.append(e)
        
        # Обрабатываем обычные элементы по разделам
        for section in part_structure['sections']:
            section_elements = []
            section_subsections = defaultdict(list)
            
            for e in regular_elements:
                for sub in section['subsections']:
                    if e.subsection == sub['key']:
                        section_elements.append(e)
                        section_subsections[sub['key']].append(e)
                        break
            
            if not section_elements:
                continue
            
            section_group = create_group(section['name'], 2, section_elements)
            if not section_group:
                continue
            
            for subsection_key, subsection_elements in section_subsections.items():
                subsection_group = create_group(subsection_key, 3, subsection_elements)
                if not subsection_group:
                    continue
                
                # Группировка по IFC типу
                ifc_groups = defaultdict(list)
                for e in subsection_elements:
                    ifc_groups[e.ifc_type].append(e)
                
                need_ifc_group = len(ifc_groups) > 1
                
                for ifc_type, ifc_elements in ifc_groups.items():
                    rule = GEOMETRY_GROUP_RULES.get(ifc_type, GEOMETRY_GROUP_RULES['default'])
                    
                    parent = subsection_group
                    
                    if need_ifc_group:
                        ifc_labels = {
                            'IfcWall': 'Стены',
                            'IfcSlab': 'Плиты',
                            'IfcColumn': 'Колонны',
                            'IfcBeam': 'Балки',
                            'IfcStair': 'Лестницы',
                            'default': 'Прочее'
                        }
                        ifc_group = create_group(ifc_labels.get(ifc_type, ifc_type), 4, ifc_elements)
                        if ifc_group:
                            parent['children'].append(ifc_group)
                            parent = ifc_group
                    
                    # Геометрическая группировка
                    geo_groups = defaultdict(list)
                    for e in ifc_elements:
                        value = get_geometry_value(rule, e)
                        assigned = False
                        for rg in rule['ranges']:
                            if value <= rg['max']:
                                geo_groups[rg['label']].append(e)
                                assigned = True
                                break
                        if not assigned:
                            geo_groups[rule['ranges'][-1]['label']].append(e)
                    
                    for geo_label, geo_elements in geo_groups.items():
                        if not geo_elements:
                            continue
                        
                        geo_group = create_group(f'{rule["label"]}: {geo_label}', 5, geo_elements)
                        if not geo_group:
                            continue
                        
                        concrete_groups = defaultdict(list)
                        for e in geo_elements:
                            concrete_key = get_concrete_group(e)
                            concrete_groups[concrete_key].append(e)
                        
                        if len(concrete_groups) == 1:
                            parent['children'].append(geo_group)
                        else:
                            for concrete_key, concrete_elements in concrete_groups.items():
                                concrete_group = create_group(concrete_key, 6, concrete_elements)
                                if concrete_group:
                                    geo_group['children'].append(concrete_group)
                            parent['children'].append(geo_group)
                
                if subsection_group['children']:
                    section_group['children'].append(subsection_group)
                elif subsection_elements:
                    # Группируем по бетону
                    concrete_groups = defaultdict(list)
                    for e in subsection_elements:
                        concrete_key = get_concrete_group(e)
                        concrete_groups[concrete_key].append(e)
                    
                    if len(concrete_groups) > 1:
                        for concrete_key, concrete_elements in concrete_groups.items():
                            concrete_group = create_group(concrete_key, 4, concrete_elements)
                            if concrete_group:
                                subsection_group['children'].append(concrete_group)
                    else:
                        for e in subsection_elements:
                            elem_group = create_group(f"Элемент: {e.name}", 5, [e])
                            if elem_group:
                                subsection_group['children'].append(elem_group)
                    
                    section_group['children'].append(subsection_group)
            
            if section_group['children']:
                part_group['children'].append(section_group)
        
        # Если нет разделов с детьми, но есть обычные элементы
        if not part_group['children'] and regular_elements:
            by_subsection = defaultdict(list)
            for e in regular_elements:
                by_subsection[e.subsection].append(e)
            
            for sub_key, sub_elems in by_subsection.items():
                sub_group = create_group(sub_key, 2, sub_elems)
                if sub_group:
                    concrete_groups = defaultdict(list)
                    for e in sub_elems:
                        concrete_key = get_concrete_group(e)
                        concrete_groups[concrete_key].append(e)
                    
                    if len(concrete_groups) > 1:
                        for concrete_key, concrete_elements in concrete_groups.items():
                            concrete_group = create_group(concrete_key, 3, concrete_elements)
                            if concrete_group:
                                sub_group['children'].append(concrete_group)
                    else:
                        for e in sub_elems:
                            elem_group = create_group(f"Элемент: {e.name}", 3, [e])
                            if elem_group:
                                sub_group['children'].append(elem_group)
                    
                    part_group['children'].append(sub_group)
        
        # Обрабатываем ПРОЧИЕ элементы
        if other_elements:
            other_group = create_group(part_structure.get('other_label', 'Прочие элементы'), 2, other_elements)
            if other_group:
                # Группируем прочие элементы по имени
                by_name = defaultdict(list)
                for e in other_elements:
                    name_key = get_name_group(e)
                    by_name[name_key].append(e)
                
                for name_key, name_elems in by_name.items():
                    name_group = create_group(name_key, 3, name_elems)
                    if name_group:
                        # Внутри группы по имени — группировка по бетону (если есть)
                        concrete_groups = defaultdict(list)
                        for e in name_elems:
                            concrete_key = get_concrete_group(e)
                            concrete_groups[concrete_key].append(e)
                        
                        if len(concrete_groups) > 1:
                            for concrete_key, concrete_elements in concrete_groups.items():
                                concrete_group = create_group(concrete_key, 4, concrete_elements)
                                if concrete_group:
                                    name_group['children'].append(concrete_group)
                        else:
                            # Один тип бетона — просто элементы
                            pass  # элементы уже учтены в name_group
                        
                        other_group['children'].append(name_group)
                
                part_group['children'].append(other_group)
        
        result.append(part_group)
    
    return result


# ========== Excel Export ==========
def create_excel_report(groups: List[Dict], headers: List[str], rows: List[Dict], output_path: str) -> str:
    wb = openpyxl.Workbook()
    
    ws = wb.active
    ws.title = "Группировка"
    
    columns = [
        ('Уровень', 8),
        ('Название группы', 60),
        ('Кол-во элементов', 15),
        ('Общий объём, м³', 18),
        ('Индексы элементов (№ п/п)', 40),
        ('Первый элемент', 12),
    ]
    
    for header in headers:
        columns.append((header, 20))
    
    area_columns = set()
    def collect_areas(groups_list):
        for g in groups_list:
            if g.get('total_areas'):
                area_columns.update(g['total_areas'].keys())
            if g.get('children'):
                collect_areas(g['children'])
    collect_areas(groups)
    
    for area_name in sorted(area_columns):
        columns.append((f'Суммарно: {area_name}', 20))
    
    for col_idx, (title, width) in enumerate(columns, 1):
        ws.cell(row=1, column=col_idx, value=title)
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    
    row_num = 2
    
    def format_indices(indices):
        if not indices:
            return ''
        return ', '.join(str(i + 1) for i in sorted(indices))
    
    def write_group(group, level, indent=''):
        nonlocal row_num
        
        ws.cell(row=row_num, column=1, value=level)
        ws.cell(row=row_num, column=2, value=f"{indent}{group['name']}")
        ws.cell(row=row_num, column=3, value=group.get('count', 0))
        cell = ws.cell(row=row_num, column=4, value=group.get('total_volume', 0))
        cell.number_format = '#,##0.000'
        ws.cell(row=row_num, column=5, value=format_indices(group.get('indices', [])))
        
        indices = group.get('indices', [])
        if indices:
            first_index = sorted(indices)[0] + 1
            ws.cell(row=row_num, column=6, value=first_index)
        else:
            ws.cell(row=row_num, column=6, value='')
        
        first = group.get('first_element', {})
        for col_idx, header in enumerate(headers, 7):
            value = first.get(header, '')
            ws.cell(row=row_num, column=col_idx, value=value)
        
        area_start_col = 7 + len(headers)
        for i, area_name in enumerate(sorted(area_columns)):
            cell = ws.cell(row=row_num, column=area_start_col + i, 
                          value=group.get('total_areas', {}).get(area_name, 0))
            cell.number_format = '#,##0.00'
        
        row_num += 1
        
        for child in group.get('children', []):
            write_group(child, level + 1, indent + '  ')
    
    for group in groups:
        write_group(group, 0)
    
    ws2 = wb.create_sheet("Детали")
    
    detail_headers = ['№ группы', 'Название группы', 'Уровень'] + headers
    for col_idx, title in enumerate(detail_headers, 1):
        ws2.cell(row=1, column=col_idx, value=title)
        ws2.column_dimensions[get_column_letter(col_idx)].width = 20
    
    detail_row = 2
    group_counter = [0]
    
    def write_detail_groups(groups_list, parent_name=''):
        nonlocal detail_row
        
        for group in groups_list:
            group_counter[0] += 1
            group_name = f"{parent_name} > {group['name']}" if parent_name else group['name']
            
            for idx in group.get('indices', []):
                if idx < len(rows):
                    row_data = rows[idx]
                    ws2.cell(row=detail_row, column=1, value=group_counter[0])
                    ws2.cell(row=detail_row, column=2, value=group_name)
                    ws2.cell(row=detail_row, column=3, value=group.get('level', 0))
                    
                    for col_idx, header in enumerate(headers, 4):
                        ws2.cell(row=detail_row, column=col_idx, value=row_data.get(header, ''))
                    
                    detail_row += 1
            
            if group.get('children'):
                write_detail_groups(group['children'], group_name)
    
    write_detail_groups(groups)
    
    wb.save(output_path)
    return output_path


# ========== Main Function ==========
def process_ifc_excel(input_excel_path: str, output_dir: str = None) -> Dict[str, str]:
    """
    Process IFC Excel file and create grouped output.
    
    Args:
        input_excel_path: Path to input Excel file
        output_dir: Output directory (default: same as input file)
    
    Returns:
        Dict with paths to output files: {'excel': '...', 'json': '...'}
    """
    input_path = Path(input_excel_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_excel_path}")
    
    output_dir = Path(output_dir) if output_dir else input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    
    base_name = input_path.stem
    excel_output = output_dir / f"{base_name}_grouped.xlsx"
    json_output = output_dir / f"{base_name}_grouped.json"
    
    df = pd.read_excel(input_path)
    headers = df.columns.tolist()
    rows = df.to_dict('records')
    
    groups = group_elements(rows, headers)
    
    create_excel_report(groups, headers, rows, str(excel_output))
    
    with open(json_output, 'w', encoding='utf-8') as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    
    return {
        'excel': str(excel_output),
        'json': str(json_output)
    }