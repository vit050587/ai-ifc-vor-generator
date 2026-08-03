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
        'field': 'Ширина, мм',
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
        'field': 'Площадь, м2',
        'label': 'Площадь',
        'unit': 'м²',
        'ranges': [
            {'max': 10, 'label': 'до 10 м²'},
            {'max': 20, 'label': 'до 20 м²'},
            {'max': float('inf'), 'label': 'более 20 м²'}
        ]
    },
    'IfcColumn': {
        'field': 'Периметр, мм',
        'label': 'Периметр',
        'unit': 'мм',
        'ranges': [
            {'max': 1200, 'label': 'до 1200 мм'},
            {'max': float('inf'), 'label': 'более 1200 мм'}
        ],
        'sub_ranges': {
            'field': 'Ширина, мм',
            'label': 'Сторона',
            'unit': 'мм',
            'ranges': [
                {'max': 300, 'label': '≤ 300 мм'},
                {'max': 500, 'label': '≤ 500 мм'},
                {'max': float('inf'), 'label': '> 500 мм'}
            ]
        }
    },
    'IfcBeam': {
        'field': 'Площадь, м2',
        'label': 'Площадь',
        'unit': 'м²',
        'ranges': [
            {'max': 10, 'label': 'до 10 м²'},
            {'max': 20, 'label': 'до 20 м²'},
            {'max': float('inf'), 'label': 'более 20 м²'}
        ]
    },
    'IfcStair': {
        'field': 'Объём, м3',
        'label': '',
        'unit': 'м³',
        'ranges': [
            {'max': float('inf'), 'label': 'Все элементы'}
        ]
    },
    'IfcStairFlight': {
        'field': 'Объём, м3',
        'label': '',
        'unit': 'м³',
        'ranges': [
            {'max': float('inf'), 'label': 'Все элементы'}
        ]
    },
    'IfcPile': {
        'field': 'Длина, мм',
        'label': 'Длина',
        'unit': 'мм',
        'ranges': [
            {'max': 8000, 'label': '6-8 м'},
            {'max': 10000, 'label': '9-10 м'},
            {'max': 12000, 'label': '11-12 м'},
            {'max': float('inf'), 'label': 'более 12 м'}
        ]
    },
    'IfcProxyElement': {
        'field': 'Объём, м3',
        'label': '',
        'unit': 'м³',
        'ranges': [
            {'max': float('inf'), 'label': 'Все элементы'}
        ]
    },
    'default': {
        'field': 'Объём, м3',
        'label': 'Объём',
        'unit': 'м³',
        'ranges': [
            {'max': 1, 'label': 'до 1 м³'},
            {'max': 5, 'label': 'до 5 м³'},
            {'max': float('inf'), 'label': 'более 5 м³'}
        ]
    }
}

# Подразделы, для которых НЕ применяется геометрическая группировка
NO_GEOMETRY_GROUP_SUBSECTIONS = [
    'Фундаментная плита',
    'Фундамент под инженерное оборудование',
    'Фундамент под башенный кран',
]

SECTION_STRUCTURE = {
    'Подземная': {
        'label': 'Подземная часть здания (до отм. 0,000)',
        'other_label': 'Прочие элементы подземной части',
        'sections': [
            {
                'name': 'Раздел 1. Монолитные ж/б конструкции. Фундаменты',
                'subsections': [
                    {'key': 'Фундаментная плита', 'patterns': ['фундаментная плита', 'фунд. плита', 'фундамент плита'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Свайно-ростверковый фундамент', 'patterns': ['свай', 'ростверк'], 'ifcTypes': ['IfcSlab', 'IfcBeam', 'IfcPile']},
                    {'key': 'Фундамент под инженерное оборудование', 'patterns': ['инженер', 'оборудование'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Фундамент под башенный кран', 'patterns': ['башен', 'кран'], 'ifcTypes': ['IfcSlab', 'IfcBeam']},
                    {'key': 'Устройство горизонтальной гидроизоляции', 'patterns': ['горизонт', 'гидроизол'], 'ifcTypes': ['IfcSlab']},
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
                    {'key': 'Подземная часть здания. Лестницы', 'patterns': ['лестн', 'марш', 'площадк', 'плм', 'лмн'], 'ifcTypes': ['IfcStair', 'IfcSlab', 'IfcStairFlight']},
                    {'key': 'Подземная часть здания. Приямки', 'patterns': ['приям'], 'ifcTypes': ['IfcSlab', 'IfcWall']},
                    {'key': 'Подземная часть здания. Вертикальная гидроизоляция', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']}
                ]
            }
        ]
    },
    'Цоколь': {
        'label': 'Цокольная часть здания (отм. 0,000)',
        'other_label': 'Прочие элементы цокольной части',
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
                    {'key': 'Цокольная часть здания. Лестницы', 'patterns': ['лестн', 'марш', 'площадк', 'плм', 'лмн'], 'ifcTypes': ['IfcStair', 'IfcSlab', 'IfcStairFlight']},
                    {'key': 'Цокольная часть здания. Приямки', 'patterns': ['приям'], 'ifcTypes': ['IfcSlab', 'IfcWall']},
                    {'key': 'Цокольная часть здания. Вертикальная гидроизоляция', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']}
                ]
            }
        ]
    },
    'Надземная': {
        'label': 'Надземная часть здания (выше отм. 0,000)',
        'other_label': 'Прочие элементы надземной части',
        'sections': [
            {
                'name': 'Раздел 3. Монолитные ж/б конструкции. Надземная часть здания',
                'subsections': [
                    {'key': 'Надземная часть здания. Стены', 'patterns': ['стен'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Надземная часть здания. Перекрытия', 'patterns': ['перекрыт', 'плит'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Надземная часть здания. Колонны', 'patterns': ['колонн'], 'ifcTypes': ['IfcColumn']},
                    {'key': 'Надземная часть здания. Балки', 'patterns': ['балк', 'ригел'], 'ifcTypes': ['IfcBeam']},
                    {'key': 'Надземная часть здания. Парапеты', 'patterns': ['парапет'], 'ifcTypes': ['IfcWall']},
                    {'key': 'Надземная часть здания. Лестничные площадки', 'patterns': ['лестничн', 'площадк', 'плм', 'лмн'], 'ifcTypes': ['IfcSlab']},
                    {'key': 'Надземная часть здания. Лестничные марши', 'patterns': ['лестничн', 'марш'], 'ifcTypes': ['IfcStair', 'IfcStairFlight']},
                    {'key': 'Надземная часть здания. Вертикальная гидроизоляция', 'patterns': ['вертикал', 'гидроизол'], 'ifcTypes': ['IfcWall']}
                ]
            }
        ]
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
    
    if 'ifcproxy' in type_lower or 'proxy' in type_lower:
        return 'IfcProxyElement'
    
    if any(w in name_lower for w in ['отверсти', 'проём', 'проем', 'окно', 'двер']):
        return 'IfcOpening'
    
    if 'труб' in name_lower:
        return 'IfcProxyElement'
    
    # Проверка по ТИПУ (до проверки по имени)
    if 'ifcpile' in type_lower:
        return 'IfcPile'
    if 'ifcstairflight' in type_lower:
        return 'IfcStairFlight'
    
    # Проверка по ИМЕНИ
    if 'сва' in name_lower:
        return 'IfcPile'
    
    if any(w in name_lower for w in ['плм', 'лмн', 'площадк']):
        return 'IfcSlab'
    
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
    if 'ifcstair' in type_lower:
        return 'IfcStair'
    
    return 'default'


def is_hydro_vertical(type_ru: str, name: str, ifc_type: str) -> bool:
    """Определяет, относится ли элемент к ВЕРТИКАЛЬНОЙ гидроизоляции (стены)"""
    name_lower = name.lower() if name else ''
    # Вертикальная = гидроизоляция + стена (или вертикальная в названии)
    has_hydro = 'гидроизол' in name_lower
    has_vertical = 'вертикал' in name_lower
    has_wall = 'стен' in name_lower or ifc_type == 'IfcWall'
    has_fundament = any(w in name_lower for w in ['фундамент', 'фунд. плита', 'фундаментная'])
    return has_hydro and has_wall and not has_fundament


def is_hydro_horizontal(type_ru: str, name: str, ifc_type: str) -> bool:
    """Определяет, относится ли элемент к ГОРИЗОНТАЛЬНОЙ гидроизоляции (плиты/перекрытия)"""
    name_lower = name.lower() if name else ''
    has_hydro = 'гидроизол' in name_lower
    has_fundament_or_slab = any(w in name_lower for w in ['фундамент', 'фунд. плита', 'фундаментная', 'плит', 'перекрыт']) or ifc_type == 'IfcSlab'
    if is_hydro_vertical(type_ru, name, ifc_type):
        return False
    return has_hydro and has_fundament_or_slab


def determine_subsection(type_ru: str, name: str, part: str) -> str:
    name_lower = name.lower() if name else ''
    type_lower = type_ru.lower() if type_ru else ''
    
    # Сначала определяем ifc_type для гидроизоляции
    temp_ifc_type = get_ifc_type(type_ru, name)
    
    # Проверка на IfcProxyElement — всегда в «Прочие элементы»
    if 'ifcproxy' in type_lower or 'proxy' in type_lower:
        return '__OTHER__'
    
    temp_ifc_type = get_ifc_type(type_ru, name)
    if temp_ifc_type == 'IfcOpening':
        return '__OTHER__'
    
    if 'труб' in name_lower:
        return '__OTHER__'
    
    if is_hydro_vertical(type_ru, name, temp_ifc_type):
        # Гидроизоляция стен → в Раздел 2
        if part == 'Подземная':
            return 'Подземная часть здания. Вертикальная гидроизоляция'
        elif part == 'Цоколь':
            return 'Цокольная часть здания. Вертикальная гидроизоляция'
        else:
            return 'Надземная часть здания. Вертикальная гидроизоляция'
    if is_hydro_horizontal(type_ru, name, temp_ifc_type):
        return 'Устройство горизонтальной гидроизоляции'
    
    if 'сва' in name_lower or 'ifcpile' in type_lower:
        return 'Свайно-ростверковый фундамент'
    
    if 'приям' in name_lower:
        mapping = {'Подземная': 'Подземная часть здания. Приямки',
                   'Цоколь': 'Цокольная часть здания. Приямки'}
        return mapping.get(part, 'Надземная часть здания. Приямки')
    
    if 'парапет' in name_lower:
        return 'Надземная часть здания. Парапеты'
    
    # Лестничные марши по ifcType (IfcStairFlight)
    if temp_ifc_type == 'IfcStairFlight':
        if part in ('Подземная', 'Цоколь'):
            return f'{part} часть здания. Лестницы'
        return 'Надземная часть здания. Лестничные марши'
    
    # Лестничные площадки (Плм)
    if 'плм' in name_lower:
        if part in ('Подземная', 'Цоколь'):
            return f'{part} часть здания. Лестницы'
        return 'Надземная часть здания. Лестничные площадки'
    
    # ЛМн без признаков марша — площадка
    if 'лмн' in name_lower:
        if part in ('Подземная', 'Цоколь'):
            return f'{part} часть здания. Лестницы'
        return 'Надземная часть здания. Лестничные площадки'
    
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
    
    def get_material_group(elem: ElementData) -> str:
        material = str(elem.row.get('Материал', ''))
        if not material or material == '-' or material == '':
            return 'Материал: не указан'
        return f'Материал: {material}'
    
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
    
    def apply_material_concrete_grouping(parent_group: dict, elems: List[ElementData], 
                                         mat_level: int, concrete_level: int) -> None:
        """Группировка по материалу, затем по бетону внутри каждого материала"""
        material_groups = defaultdict(list)
        for e in elems:
            mat_key = get_material_group(e)
            material_groups[mat_key].append(e)
        
        if len(material_groups) == 1:
            # Один материал — группируем сразу по бетону
            concrete_groups = defaultdict(list)
            for e in elems:
                concrete_key = get_concrete_group(e)
                concrete_groups[concrete_key].append(e)
            
            if len(concrete_groups) == 1:
                # Один бетон — не создаём лишних уровней
                return
            else:
                for concrete_key, concrete_elements in concrete_groups.items():
                    concrete_group = create_group(concrete_key, concrete_level, concrete_elements)
                    if concrete_group:
                        parent_group['children'].append(concrete_group)
        else:
            # Несколько материалов
            for mat_key, mat_elements in material_groups.items():
                mat_group = create_group(mat_key, mat_level, mat_elements)
                if not mat_group:
                    continue
                
                concrete_groups = defaultdict(list)
                for e in mat_elements:
                    concrete_key = get_concrete_group(e)
                    concrete_groups[concrete_key].append(e)
                
                if len(concrete_groups) > 1:
                    for concrete_key, concrete_elements in concrete_groups.items():
                        concrete_group = create_group(concrete_key, concrete_level, concrete_elements)
                        if concrete_group:
                            mat_group['children'].append(concrete_group)
                
                parent_group['children'].append(mat_group)
    
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
                
                skip_geometry = subsection_key in NO_GEOMETRY_GROUP_SUBSECTIONS
                
                if skip_geometry:
                    # Без геометрической группировки — сразу материал/бетон
                    apply_material_concrete_grouping(subsection_group, subsection_elements, 4, 5)
                else:
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
                                'IfcStairFlight': 'Лестничные марши',
                                'IfcPile': 'Сваи',
                                'default': 'Прочее'
                            }
                            ifc_group = create_group(ifc_labels.get(ifc_type, ifc_type), 4, ifc_elements)
                            if ifc_group:
                                parent['children'].append(ifc_group)
                                parent = ifc_group
                        
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
                            
                            if rule.get('sub_ranges'):
                                sub_rule = rule['sub_ranges']
                                
                                sub_geo_groups = defaultdict(list)
                                for e in geo_elements:
                                    val = safe_parse_float(e.row.get(sub_rule['field'], 0))
                                    assigned = False
                                    for rg in sub_rule['ranges']:
                                        if val <= rg['max']:
                                            sub_geo_groups[rg['label']].append(e)
                                            assigned = True
                                            break
                                    if not assigned:
                                        sub_geo_groups[sub_rule['ranges'][-1]['label']].append(e)
                                
                                for sub_label, sub_elems in sub_geo_groups.items():
                                    if not sub_elems:
                                        continue
                                    
                                    sub_geo_group = create_group(
                                        f'{sub_rule["label"]}: {sub_label}', 6, sub_elems
                                    )
                                    if not sub_geo_group:
                                        continue
                                    
                                    # Группировка по материалу/бетону внутри sub_geo_group
                                    apply_material_concrete_grouping(sub_geo_group, sub_elems, 7, 8)
                                    
                                    geo_group['children'].append(sub_geo_group)
                                
                                if geo_group['children']:
                                    parent['children'].append(geo_group)
                            else:
                                # Группировка по материалу/бетону внутри geo_group
                                apply_material_concrete_grouping(geo_group, geo_elements, 6, 7)
                                parent['children'].append(geo_group)
                
                if subsection_group['children']:
                    section_group['children'].append(subsection_group)
                elif subsection_elements:
                    apply_material_concrete_grouping(subsection_group, subsection_elements, 4, 5)
                    if subsection_group['children']:
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
                    apply_material_concrete_grouping(sub_group, sub_elems, 3, 4)
                    if sub_group['children']:
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
                        apply_material_concrete_grouping(name_group, name_elems, 4, 5)
                        if name_group['children']:
                            other_group['children'].append(name_group)
                
                if other_group['children']:
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