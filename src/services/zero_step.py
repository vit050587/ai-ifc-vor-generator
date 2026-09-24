# ВЫТАСКИВАЕМ ИЗ ФАЙЛА .ifc ВСЮ ИНФОРМАЦИЮ (исправленная версия с литрами и QTO)

import ifcopenshell
import pandas as pd
import os
import re
import json
from datetime import datetime
from pathlib import Path

from src.core.logger import setup_logger
from src.services.ifc_raw_dump import (
    _compute_bbox_quantities,
    _compute_formwork_bbox_quantities,
)

logger = setup_logger("zero_step")

# Имя JSON-файла с глобальными константами, определёнными по модели IFC
# (высоты здания/этажа, отметки). Записывается в корень сессии рядом с
# height.txt; значения из него используются подбором работ (АР).
IFC_CONSTANTS_FILENAME = 'IFC_глобальные_константы.json'

# Заголовки констант, не входящих в схему works_classification.json
# (синхронизировано с EXTRA_POS_CONSTANTS в pd_parser.py).
# floor_height, crane_type, crane_capacity, bucket_capacity, equipment_power
# входят в схему global_constants — их заголовки берутся из схемы.
_EXTRA_CONSTANT_TITLES = {
    'soil_group': 'Группа грунтов',
    'movement_distance': 'Расстояние перемещения грунта',
}

# IFC-специфичные константы (в файле ПОС не определяются).
_IFC_ONLY_CONSTANT_TITLES = {
    'total_height_m': 'Общая высота здания, м',
    'min_ground_elevation_m': 'Минимальная отметка надземной части, м',
    'top_elevation_m': 'Максимальная отметка надземной части, м',
}

# Минимальный номер этажа, при котором здание считается многоэтажным:
# наибольший числовой индикатор в колонке «Этаж» >= MULTISTORY_THRESHOLD
# (наличие 2-го этажа делает здание многоэтажным).
MULTISTORY_THRESHOLD = 2


def _storey_indicator(storey_name):
    """Числовой индикатор этажа из значения колонки «Этаж».

    Правила (аналогичны _classify_storey_type_ar):
      'К01_1_этаж_основной'       → 1
      'К01_12_этаж_основной'      → 12
      'К01_-1/1_этаж_цокольный'   → 1 (цокольный этаж считается надземным)
      'К01_-1_подземный этаж'     → None (подземные этажи не учитываются)
      'К01_Крыша'                 → None (нет числового индикатора)

    Returns:
        int или None: номер надземного этажа.
    """
    if storey_name is None:
        return None
    for segment in re.split(r'[_\s]+', str(storey_name).strip()):
        seg = segment.strip()
        if re.match(r'^\d+$', seg):
            return int(seg)
        m = re.match(r'^-(\d+)\s*/\s*(\d+)$', seg)
        if m:
            return int(m.group(2))
    return None


def detect_storeys_type(storey_names):
    """Определяет этажность здания по значениям колонки «Этаж».

    Берётся наибольший числовой индикатор этажа по всем элементам:
    если он >= 2 (в здании есть 2-й этаж) — здание многоэтажное,
    иначе одноэтажное.

    Args:
        storey_names: iterable значений колонки «Этаж» (имена этажей IFC).

    Returns:
        str или None: 'одноэтажное' / 'многоэтажное'; None, если ни у одного
        этажа нет числового индикатора (этажность определить нельзя).
    """
    numbers = [n for n in (_storey_indicator(s) for s in storey_names) if n is not None]
    if not numbers:
        return None
    return 'многоэтажное' if max(numbers) >= MULTISTORY_THRESHOLD else 'одноэтажное'


def detect_storeys_type_from_session(session_dir):
    """Этажность здания по файлам сессии (полная модель, не выбранные строки).

    Источники по приоритету:
      1. ``IFC_глобальные_константы.json`` — константа ``building_storeys_type``,
         определённая в zero_step по колонке «Этаж» всех элементов модели;
      2. ``ДЛЯ_СМЕТЧИКА_исправленный.xlsx`` (original/ или корень сессии) —
         fallback для сессий, обработанных до появления константы.

    Args:
        session_dir: путь к папке сессии (outputs/<session_id>).

    Returns:
        str или None: 'одноэтажное' / 'многоэтажное'.
    """
    # 1. IFC_глобальные_константы.json
    constants_path = os.path.join(session_dir, IFC_CONSTANTS_FILENAME)
    if os.path.isfile(constants_path):
        try:
            with open(constants_path, encoding='utf-8') as f:
                data = json.load(f)
            const = (data.get('constants') or {}).get('building_storeys_type')
            if isinstance(const, dict) and const.get('value'):
                return str(const['value'])
        except Exception as exc:
            logger.warning(f"Не удалось прочитать {constants_path}: {exc}")

    # 2. Excel сессии: колонка «Этаж» всех элементов модели
    excel_path = os.path.join(session_dir, 'original', 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx')
    if not os.path.isfile(excel_path):
        excel_path = os.path.join(session_dir, 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx')
    if os.path.isfile(excel_path):
        try:
            df = pd.read_excel(excel_path, sheet_name='Данные')
            if 'Этаж' in df.columns:
                return detect_storeys_type(df['Этаж'].tolist())
        except Exception as exc:
            logger.warning(f"Не удалось прочитать {excel_path}: {exc}")

    return None


def _schema_constant_titles():
    """Заголовки схемных констант из data/works_classification.json.

    Возвращает {имя константы: title} в порядке следования в схеме.
    При ошибке чтения возвращается пустой словарь.
    """
    schema_path = (Path(__file__).resolve().parents[2] / 'data'
                   / 'works_classification.json')
    try:
        with open(schema_path, encoding='utf-8') as f:
            data = json.load(f)
        return {c.get('name'): c.get('title', '')
                for c in data.get('global_constants', []) if c.get('name')}
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {schema_path}: {exc}")
        return {}


def _build_ifc_constants(building_height_info, ifc_file_name):
    """Глобальные константы IFC в структуре ПОС_глобальные_константы.json.

    Каждая константа имеет тот же набор полей, что и в pd_parser
    (extract_pos_constants): {name, title, value, found, raw_values,
    quote, page, source}. Файл содержит полный перечень констант (как в
    ПОС): константы, не определяемые по модели IFC, записываются со
    значением null и found=false — их значения берутся из ПОС или по
    умолчанию (приоритет источников: IFC → ПОС).

    Args:
        building_height_info: высотные характеристики модели (м).
        ifc_file_name: имя исходного IFC-файла.

    Returns:
        dict: {document, constants, warnings, file_name, generated_at}.
    """
    # Значения, вычисленные по модели IFC (определены всегда)
    ifc_values = {
        'building_height_m': building_height_info['Высота_надземной_части_м'],
        'floor_height': building_height_info.get('Высота_основного_этажа_м', 0.0),
        'total_height_m': building_height_info['Общая_высота_здания_м'],
        'min_ground_elevation_m': building_height_info['Минимальная_отметка_надземной_части_м'],
        'top_elevation_m': building_height_info['Максимальная_отметка_надземной_части_м'],
    }

    # Этажность здания ('одноэтажное'/'многоэтажное') — по наибольшему
    # числовому индикатору в колонке «Этаж». Записывается только если
    # определилась (иначе значение берётся из ПОС или по умолчанию).
    storeys_type = building_height_info.get('Этажность_здания')
    if storeys_type:
        ifc_values['building_storeys_type'] = storeys_type

    # Полный перечень констант в порядке ПОС: схема → доп. константы →
    # IFC-специфичные.
    ordered = []
    for name, title in _schema_constant_titles().items():
        ordered.append((name, title))
    for name, title in _EXTRA_CONSTANT_TITLES.items():
        if not any(n == name for n, _ in ordered):
            ordered.append((name, title))
    for name, title in _IFC_ONLY_CONSTANT_TITLES.items():
        if not any(n == name for n, _ in ordered):
            ordered.append((name, title))

    constants = {}
    for name, title in ordered:
        if name in ifc_values:
            value = ifc_values[name]
            constants[name] = {
                'name': name,
                'title': title,
                'value': value,
                'found': True,
                'raw_values': [value],
                'quote': '',
                'page': None,
                'source': 'IFC',
            }
        else:
            # Не определяется по IFC — заполняется из ПОС или по умолчанию
            constants[name] = {
                'name': name,
                'title': title,
                'value': None,
                'found': False,
                'raw_values': [],
                'quote': '',
                'page': None,
                'source': '',
            }

    return {
        'document': {
            'name': ifc_file_name,
            'pages': None,
            'object': '',
            'cipher': '',
        },
        'constants': constants,
        'warnings': [],
        'file_name': ifc_file_name,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }

 
# Список IFC-классов для режима КР (конструктивные решения).
# Конструктивные элементы из железобетона, а также изоляция (IfcCovering).
# Используется при processing_type="KR".
ELEMENT_TYPES_KR = [
    ('IfcWall', 'Стены'),
    ('IfcFooting', 'Фундамент'),
    ('IfcWallStandardCase', 'Стены'),
    ('IfcSlab', 'Перекрытия'),
    ('IfcColumn', 'Колонны'),
    ('IfcBeam', 'Балки'),
    ('IfcStair', 'Лестницы'),
    ('IfcStairFlight', 'Лестницы'),
    ('IfcRamp', 'Пандусы'),
    ('IfcPile', 'Сваи'),
    ('IfcCovering', 'Изоляция'),
]

# Дополнительные классы только для режима АР (архитектурные решения).
# Сюда перенесены архитектурно-отделочные элементы (ранее ошибочно
# присутствовавшие в списке КР): окна, двери, витражи, кровля,
# перила, мебель, прокси-элементы — у них есть код МССК в отдельных
# колонках Property Sets (ExpCheck_Covering::MGE_ElementCode,
# ExpCheck_Door::MGE_ElementCode, ...), но они НЕ являются ж/б конструкциями.
#
# IfcCovering здесь помечен как «Изоляция» — так же, как в списке КР:
# парсинг обоих режимов должен быть идентичен (одинаковое «Тип (RU)»
# влияет на заголовки групп финального перечня работ).
_ARCH_TYPES = [
    ('IfcBuildingElementProxy', 'Прочие_элементы'),
    ('IfcWindow', 'Окна'),
    ('IfcDoor', 'Двери'),
    ('IfcCurtainWall', 'Стены'),
    ('IfcRoof', 'Кровля'),
    ('IfcRailing', 'Перила'),
    ('IfcFurnishingElement', 'Мебель'),
    ('IfcPlate', 'Плиты'),
    ('IfcShadingDevice', 'Солнцезащитные устройства'),
    ('IfcWasteTerminal', 'Санитарно-технические приборы'),
    ('IfcAirTerminal', 'Воздухораспределители'),
    ('IfcMember', 'Стойка_ограждения'),
]

# Список IFC-классов для режима АР: конструктивные (КР) + архитектурные.
# Дедупликация по IFC-классу: IfcCovering присутствует и в КР, и в
# архитектурном списке с ОДИНАКОВОЙ меткой «Изоляция» — парсинг обоих
# режимов идентичен; элемент не попадает в таблицу АР дважды.
_ar_types_by_class = {}
for _ifc_type, _ru_name in ELEMENT_TYPES_KR:
    _ar_types_by_class.setdefault(_ifc_type, (_ifc_type, _ru_name))
for _ifc_type, _ru_name in _ARCH_TYPES:
    _ar_types_by_class[_ifc_type] = (_ifc_type, _ru_name)
ELEMENT_TYPES_AR = list(_ar_types_by_class.values())

# Обратная совместимость: внешний код может импортировать element_types.
# По умолчанию соответствует списку КР (как было до разделения).
element_types = ELEMENT_TYPES_KR

# Список специфических свойств для извлечения
SPECIFIC_PROPERTIES = [
    'Pset_ConcreteElementGeneral.ReinforcementVolumeRatio',
    'ExpCheck_MaterialConcrete.MGE_ConcreteGrade',
    'ExpCheck_MaterialConcrete.MGE_WaterResist',
    'ExpCheck_MaterialConcrete.MGE_FreezeDurability'
]


def safe_get_attr(obj, attr, default='-'):
    try:
        val = getattr(obj, attr, default)
        if val is None:
            return default
        if hasattr(val, 'wrappedValue'):
            return val.wrappedValue
        return val
    except Exception as e:
        logger.error(f"Ошибка при обработке: {e}")
        return default


def classify_storey_type(storey_name, elevation_mm):
    """Классифицирует тип этажа по имени и высоте

    Строгое правило — числовой индикатор в значении «Этаж»
    (разбивка по «_» и пробелам, текстовые префиксы К01_/С01_ игнорируются):
      '-1/1' / '-1/1_подземный этаж'  → Цокольный (индикатор вида «-N/M»)
      '-1_подземный этаж_основной'    → Подземный  (индикатор — «-N»)
      '1_этаж_основной'               → Надземный  (индикатор — «N»)
      '2_этаж_основной', '26_технический чердак', 'Крыша' → Надземный
    Индикатор проверяется ПЕРВЫМ: «-1/1_подземный этаж» — цоколь, несмотря
    на слово «подземный»; «1_этаж_основной» — надземный даже при отметке 0,000.
    """
    storey_name_lower = str(storey_name).lower()

    # 1. Числовой индикатор в значении «Этаж» — строгое правило
    for segment in re.split(r'[_\s]+', storey_name_lower):
        seg = segment.strip()
        if re.match(r'^-\d+\s*/\s*\d+$', seg):
            return 'Цокольный'
        if re.match(r'^-\d+$', seg):
            return 'Подземный'
        if re.match(r'^\d+$', seg):
            return 'Надземный'

    # 2. Текстовые признаки (только для имён без числового индикатора)
    if any(word in storey_name_lower for word in ['подвал', 'basement', 'подзем']):
        return 'Подземный'
    elif any(word in storey_name_lower for word in ['цоколь', 'ground', 'нулевой']):
        return 'Цокольный'
    elif any(word in storey_name_lower for word in ['техническ', 'technical']):
        return 'Технический'
    elif any(word in storey_name_lower for word in ['мансард', 'attic']):
        return 'Мансардный'
    elif any(word in storey_name_lower for word in ['крыш', 'roof', 'кровл']):
        return 'Кровля'

    # 3. Отметка этажа — только по знаку: отрицательная отметка — подземная,
    #    нулевая и положительная — надземная (1-й этаж на отм. 0,000 — надземный)
    if elevation_mm != '-':
        try:
            if float(elevation_mm) < 0:
                return 'Подземный'
            return 'Надземный'
        except Exception as e:
            print(f'Ошибка: {e}')

    return 'Не определен'


def _classify_storey_type_ar(storey_name):
    """Классифицирует тип этажа строго по числовому индикатору
    в значении «Этаж» (без анализа слов и отметок этажа).

    .. deprecated::
        Больше НЕ используется в пайплайне АР: get_element_storey
        классифицирует тип этажа одинаково для КР и АР через
        classify_storey_type (парсинг режимов идентичен). Функция
        сохранена для обратной совместимости внешнего кода.

    Правила (числовой индикатор ищется в сегментах значения, разделённых
    «_» и пробелами; текстовые префиксы К01_, С01_ и т.п. игнорируются):
      'К01_-1/1_этаж_цокольный'   → Цокольный (индикатор вида «-N/M»)
      'К01_-1_подземный этаж_основной' → Подземный (индикатор — отрицательное число)
      'К01_1_этаж_основной'       → Надземный (индикатор — положительное число)
      'К01_Крыша'                 → Надземный (нет числового индикатора)
    """
    if storey_name is None:
        return 'Надземный'
    s = str(storey_name).strip()
    if not s or s in ('-', 'nan'):
        return 'Надземный'
    for segment in re.split(r'[_\s]+', s):
        seg = segment.strip()
        if re.match(r'^-\d+\s*/\s*\d+$', seg):
            return 'Цокольный'
        if re.match(r'^-\d+$', seg):
            return 'Подземный'
        if re.match(r'^\d+$', seg):
            return 'Надземный'
    # Нет числового индикатора — Надземный (крыша, технический этаж и т.п.)
    return 'Надземный'


def find_main_floor_height(storeys):
    """Определяет высоту основного (типового) этажа по отметкам этажей.

    В режиме АР важна не общая высота здания, а высота типового этажа:
    расценки на архитектурно-отделочные работы привязаны к высоте этажа.
    Считаем разницу между последовательными надземными этажами
    (Цокольный/Надземный/Мансардный) и выбираем наиболее часто
    встречающийся шаг (моду), при неоднозначности — медиану.

    Args:
        storeys: dict {имя этажа: {'elevation': м, 'type': str}}

    Returns:
        float: высота основного этажа в метрах (0.0, если не удалось).
    """
    from collections import Counter

    elevs = sorted(
        info['elevation']
        for info in storeys.values()
        if info.get('type') in ('Цокольный', 'Надземный', 'Мансардный')
    )

    steps = []
    for lo, hi in zip(elevs, elevs[1:]):
        diff = hi - lo
        if 0.5 <= diff <= 20:  # типовой междуэтажный шаг
            steps.append(diff)

    if not steps:
        return 0.0

    # Мода по шагам с точностью до сантиметра
    steps_cm = [round(step * 100) for step in steps]
    counter = Counter(steps_cm)
    max_count = max(counter.values())
    modes = sorted(k for k, v in counter.items() if v == max_count)

    if len(modes) == 1:
        floor_cm = modes[0]
    else:
        # Несколько одинаково частых шагов — берём медиану всех шагов
        floor_cm = sorted(steps_cm)[len(steps_cm) // 2]

    return round(floor_cm / 100, 3)


def get_element_storey(element, processing_type: str = "KR"):
    """Извлекает информацию об этаже, на котором находится элемент

    processing_type: режим обработки — "KR" (по умолчанию) или "AR".
        Тип этажа классифицируется одинаково для обоих режимов — через
        classify_storey_type (слова + отметка этажа), чтобы парсинг
        IFC в режиме АР был идентичен режиму КР (одинаковые колонки
        «Тип_этажа», одинаковая разбивка по частям здания).
    """
    storey_info = {
        'Этаж': '-',
        'Уровень_этажа_мм': '-',
        'Тип_этажа': '-'
    }
    
    try:
        if hasattr(element, 'ContainedInStructure'):
            for rel in element.ContainedInStructure:
                if rel.is_a('IfcRelContainedInSpatialStructure'):
                    container = rel.RelatingStructure
                    if container and container.is_a('IfcBuildingStorey'):
                        storey_info['Этаж'] = safe_get_attr(container, 'Name')
                        
                        if hasattr(container, 'Elevation'):
                            elevation = safe_get_attr(container, 'Elevation')
                            if elevation != '-':
                                
                                elev_val = float(elevation)
                                if abs(elev_val) > 100:  
                                    elev_val = elev_val / 1000
                                storey_info['Уровень_этажа_мм'] = round(elev_val * 1000, 2)
                        
                        storey_info['Тип_этажа'] = classify_storey_type(
                            storey_info['Этаж'],
                            storey_info['Уровень_этажа_мм']
                        )
                        break
    except Exception as e:
        print(f'Error: {e}')
    
    return storey_info


def get_all_quantities(element):
    """Извлекает все количественные характеристики элемента (QTO) - ДУБЛИРУЕТ в старом и новом формате"""
    quantities = {}
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props and props.is_a('IfcElementQuantity'):
                        # Получаем название QTO набора
                        qto_set_name = safe_get_attr(props, 'Name')
                        
                        if hasattr(props, 'Quantities'):
                            for qty in props.Quantities:
                                qty_name = safe_get_attr(qty, 'Name')
                                qty_type = qty.is_a()
                                
                                if qty_type == 'IfcQuantityLength':
                                    value = safe_get_attr(qty, 'LengthValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат с QTO_
                                        quantities[f'QTO_{qto_set_name}_Длина_{qty_name}_мм'] = round(float(value), 2)
                                        # СТАРЫЙ формат без QTO_ (как было в работающей версии)
                                        quantities[f'Длина_{qty_name}_мм'] = round(float(value), 2)
                                
                                elif qty_type == 'IfcQuantityArea':
                                    value = safe_get_attr(qty, 'AreaValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат с QTO_
                                        quantities[f'QTO_{qto_set_name}_Площадь_{qty_name}_м2'] = round(float(value), 3)
                                        # СТАРЫЙ формат без QTO_ (как было в работающей версии)
                                        quantities[f'Площадь_{qty_name}_м2'] = round(float(value), 3)
                                        
                                        # ЕСЛИ ЭТО GROSS - создаем дополнительные ключи для приоритетного поиска
                                        if 'gross' in qty_name.lower():
                                            # Специальный ключ для поиска Gross в geometry_mapping
                                            quantities[f'QTO_{qto_set_name}_Площадь_GROSS_м2'] = round(float(value), 3)
                                            # И в старом формате тоже
                                            quantities[f'Площадь_GROSS_м2'] = round(float(value), 3)
                                
                                elif qty_type == 'IfcQuantityVolume':
                                    value = safe_get_attr(qty, 'VolumeValue')
                                    if value and value != '-':
                                        if qty_name.lower() == 'netvolume':
                                            # НОВЫЙ формат
                                            quantities[f'QTO_{qto_set_name}_Объём_{qty_name}_м3'] = round(float(value), 3)
                                            # СТАРЫЙ формат
                                            quantities[f'Объём_{qty_name}_м3'] = round(float(value), 3)
                                        else:
                                            # НОВЫЙ формат
                                            quantities[f'QTO_{qto_set_name}_Объём_{qty_name}_литры'] = round(float(value), 2)
                                            # СТАРЫЙ формат
                                            quantities[f'Объём_{qty_name}_литры'] = round(float(value), 2)
                                
                                elif qty_type == 'IfcQuantityCount':
                                    value = safe_get_attr(qty, 'CountValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат
                                        quantities[f'QTO_{qto_set_name}_Количество_{qty_name}'] = value
                                        # СТАРЫЙ формат
                                        quantities[f'Количество_{qty_name}'] = value
                                
                                elif qty_type == 'IfcQuantityWeight':
                                    value = safe_get_attr(qty, 'WeightValue')
                                    if value and value != '-':
                                        # НОВЫЙ формат
                                        quantities[f'QTO_{qto_set_name}_Вес_{qty_name}_кг'] = round(float(value), 2)
                                        # СТАРЫЙ формат
                                        quantities[f'Вес_{qty_name}_кг'] = round(float(value), 2)
    except Exception as e:
        logger.error(f"Ошибка при получении QTO параметров: {e}")
    return quantities


def get_geometry_from_representation(element):
    """Извлекает геометрические параметры из представления элемента"""
    geometry = {}
    try:
        if hasattr(element, 'Representation') and element.Representation:
            if hasattr(element.Representation, 'Representations'):
                for rep in element.Representation.Representations:
                    if hasattr(rep, 'Items'):
                        for item in rep.Items:
                            if item.is_a('IfcExtrudedAreaSolid'):
                                if hasattr(item, 'Depth'):
                                    geometry['Глубина_выдавливания_мм'] = round(float(item.Depth), 2)
                                
                                if hasattr(item, 'SweptArea') and item.SweptArea:
                                    swept = item.SweptArea
                                    if swept.is_a('IfcRectangleProfileDef'):
                                        if hasattr(swept, 'XDim'):
                                            geometry['Длина_мм'] = round(float(swept.XDim), 2)
                                        if hasattr(swept, 'YDim'):
                                            geometry['Толщина_мм'] = round(float(swept.YDim), 2)
    except Exception as e:
        logger.error(f"Ошибка при анализе геометрии: {e}")
    return geometry


def get_placement_info(element):
    """Извлекает информацию о размещении элемента"""
    placement = {}
    try:
        if hasattr(element, 'ObjectPlacement'):
            placement_obj = element.ObjectPlacement
            if placement_obj and placement_obj.is_a('IfcLocalPlacement'):
                if hasattr(placement_obj, 'RelativePlacement'):
                    rel_place = placement_obj.RelativePlacement
                    if rel_place and hasattr(rel_place, 'Location'):
                        loc = rel_place.Location
                        if hasattr(loc, 'Coordinates'):
                            coords = loc.Coordinates
                            if len(coords) >= 3:
                                placement['Координата_X_мм'] = round(float(coords[0]), 2)
                                placement['Координата_Y_мм'] = round(float(coords[1]), 2)
                                placement['Координата_Z_мм'] = round(float(coords[2]), 2)
    except:
        pass
    return placement


def get_all_properties(element):
    """Извлекает все свойства элемента из Property Sets"""
    properties = {}
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props:
                        pset_name = safe_get_attr(props, 'Name')
                        if props.is_a('IfcPropertySet'):
                            if hasattr(props, 'HasProperties'):
                                for prop in props.HasProperties:
                                    prop_name = safe_get_attr(prop, 'Name')
                                    val = prop.NominalValue
                                    if val:
                                        if hasattr(val, 'wrappedValue'):
                                            value = val.wrappedValue
                                        else:
                                            value = str(val)
                                        key = f"Свойство_{pset_name}_{prop_name}" if pset_name != '-' else f"Свойство_{prop_name}"
                                        properties[key] = value
    except:
        pass
    return properties


def get_specific_properties(element):
    """
    Извлекает конкретные свойства по заданному списку SPECIFIC_PROPERTIES
    Формат: 'PsetName.PropertyName'
    """
    properties = {}
    
    # Инициализируем все целевые свойства значением '-'
    for prop_path in SPECIFIC_PROPERTIES:
        col_name = prop_path.replace('.', '_')
        properties[col_name] = '-'
    
    try:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    if props:
                        pset_name = safe_get_attr(props, 'Name')
                        
                        # Проверяем Property Sets
                        if props.is_a('IfcPropertySet'):
                            if hasattr(props, 'HasProperties'):
                                for prop in props.HasProperties:
                                    prop_name = safe_get_attr(prop, 'Name')
                                    full_name = f"{pset_name}.{prop_name}"
                                    
                                    # Проверяем, нужно ли нам это свойство
                                    if full_name in SPECIFIC_PROPERTIES:
                                        col_name = full_name.replace('.', '_')
                                        if prop.is_a('IfcPropertySingleValue') and prop.NominalValue:
                                            if hasattr(prop.NominalValue, 'wrappedValue'):
                                                properties[col_name] = prop.NominalValue.wrappedValue
                                            else:
                                                properties[col_name] = str(prop.NominalValue)
                                        elif prop.is_a('IfcPropertyEnumeratedValue'):
                                            if prop.EnumerationValues:
                                                properties[col_name] = str(prop.EnumerationValues[0].wrappedValue)
                                        break
    except Exception as e:
        logger.error(f"Ошибка при извлечении специфических свойств: {e}")
    
    return properties


# Соответствие IFC-класса элемента и суффикса имени Pset «ExpCheck_<Suffix>»,
# из которого берётся материал (MGE_Material). Для большинства классов суффикс
# равен имени класса без префикса «Ifc» (IfcWall → Wall, IfcBeam → Beam,
# IfcStairFlight → StairFlight и т.д.). Исключения — алиасные классы
# (IfcWallStandardCase → Wall), у которых Pset называется «ExpCheck_Wall».
_EXP_CHECK_PSET_SUFFIX_ALIASES = {
    'IfcWallStandardCase': 'Wall',
}


def _resolve_expcheck_material(info: dict, ifc_class: str):
    """Возвращает материал элемента из Pset «ExpCheck_<Type>::MGE_Material».

    В режиме КР материал элемента должен браться не из ассоциаций материала
    IFC (IfcRelAssociatesMaterial — там часто «По умолчанию» или внутреннее
    имя Revit), а из свойства ``MGE_Material`` набора ``ExpCheck_<Класс>``
    (например ``ExpCheck_Beam::MGE_Material`` = «Железобетон сборный»).

    Свойства уже извлечены функцией get_all_properties в ключи вида
    ``Свойство_ExpCheck_<Pset>_MGE_Material``. Подбираем ключ, Pset которого
    соответствует классу элемента (с учётом алиасов). Если подходящего
    непустого значения нет — возвращаем None (материал остаётся прежним).
    """
    if not ifc_class or ifc_class == '-':
        return None

    suffix = _EXP_CHECK_PSET_SUFFIX_ALIASES.get(ifc_class)
    if suffix is None:
        suffix = ifc_class[3:] if ifc_class.startswith('Ifc') else ifc_class

    primary_key = f'Свойство_ExpCheck_{suffix}_MGE_Material'
    val = info.get(primary_key)
    if val is not None and str(val).strip() and str(val).strip() != '-':
        return str(val).strip()

    # Запасной вариант: среди всех ExpCheck_*::MGE_Material берём первый
    # непустой (на случай нестандартного имени Pset).
    for key, raw in info.items():
        if (isinstance(key, str)
                and key.startswith('Свойство_ExpCheck_')
                and key.endswith('_MGE_Material')
                and key != primary_key):
            if raw is not None and str(raw).strip() and str(raw).strip() != '-':
                return str(raw).strip()

    return None


def _has_positive_qto_value(info: dict, keys) -> bool:
    """Проверяет, есть ли в info положительное числовое значение среди ключей.

    Аргументы:
        info — словарь данных элемента (get_element_info);
        keys — кортеж имён колонок-источников (обычный/префиксованный QTO).

    Возвращает True, если хотя бы один ключ существует и содержит
    число больше нуля.
    """
    for key in keys:
        raw = info.get(key)
        if raw is None or raw == '-':
            continue
        try:
            if float(str(raw).replace(',', '.')) > 0:
                return True
        except (ValueError, TypeError):
            continue
    return False


def get_element_info(element, processing_type: str = "KR"):
    """Собирает всю информацию об элементе

    Args:
        element: IFC-элемент.
        processing_type: режим обработки — "KR" (по умолчанию) или "AR".
            В режиме КР материал элемента берётся из свойства
            ``ExpCheck_<Класс>::MGE_Material`` (а не из ассоциаций материала
            IFC), как требуется для корректного превью и подбора работ.
    """
    info = {
        'GlobalId': safe_get_attr(element, 'GlobalId'),
        'Имя': safe_get_attr(element, 'Name'),
        'Тип элемента': element.is_a(),
        'Тег': safe_get_attr(element, 'Tag'),
    }
    
    # Информация об этаже
    info.update(get_element_storey(element, processing_type=processing_type))
    
    # Материал
    material_found = False
    try:
        if hasattr(element, 'HasAssociations'):
            for rel in element.HasAssociations:
                if rel.is_a('IfcRelAssociatesMaterial'):
                    material = rel.RelatingMaterial
                    if material.is_a('IfcMaterial'):
                        info['Материал'] = safe_get_attr(material, 'Name')
                        material_found = True
                    elif material.is_a('IfcMaterialLayerSetUsage'):
                        if material.ForLayerSet and material.ForLayerSet.MaterialLayers:
                            layers = []
                            for layer in material.ForLayerSet.MaterialLayers:
                                if layer.Material:
                                    layers.append(safe_get_attr(layer.Material, 'Name'))
                            info['Материал'] = ', '.join(layers) if layers else '-'
                            material_found = True
    except:
        pass
    
    if not material_found:
        info['Материал'] = '-'
    
    # Геометрия
    info.update(get_geometry_from_representation(element))
    
    # Размещение
    info.update(get_placement_info(element))
    
    # QTO характеристики (количественные)
    quantities = get_all_quantities(element)
    info.update(quantities)

    # Если у элемента нет количественных характеристик QTO (IfcElementQuantity),
    # вычисляем их из геометрии (bbox) по координатам — как в ifc_raw_dump.
    # Колонки имеют префикс QTO_bbox:: и используются в geometry_mapping
    # как запасной источник для длины/ширины/высоты/площади/объёма
    # (важно для режима АР: окна, двери, покрытия и пр. часто без QTO).
    if not quantities:
        try:
            info.update(_compute_bbox_quantities(element))
        except Exception as exc:
            logger.debug(f"Не удалось вычислить bbox-количества для "
                         f"{safe_get_attr(element, 'GlobalId')}: {exc}")

    # По-элементный bbox-фолбэк периметра/толщины для расчёта площади
    # опалубки (оба режима — КР и АР; парсинг идентичен). QTO может быть
    # у файла в целом, но у отдельных элементов — неполным: IfcSlab без
    # Perimeter/Depth (приямки, часть фундаментных плит) и IfcStairFlight
    # (Qto_StairFlightBaseQuantities не содержит Perimeter/Depth вовсе).
    # Периметр и толщина вычисляются из геометрии (bbox) и попадают в
    # обычные нормализованные колонки 'Длина_Perimeter_мм'/'Длина_Depth_мм'
    # — по ним group_excel считает площадь опалубки плит (периметр ×
    # толщина; для маршей — площадь двух боковых граней).
    ifc_class = element.is_a()
    if ifc_class in ('IfcSlab', 'IfcStairFlight'):
        perim_present = _has_positive_qto_value(
            info, ('Длина_Perimeter_мм',
                   'QTO_Qto_SlabBaseQuantities_Длина_Perimeter_мм')
        )
        depth_present = _has_positive_qto_value(
            info, ('Длина_Depth_мм',
                   'QTO_Qto_SlabBaseQuantities_Длина_Depth_мм')
        )
        if not (perim_present and depth_present):
            try:
                bbox_formwork = _compute_formwork_bbox_quantities(element)
            except Exception as exc:
                bbox_formwork = {}
                logger.debug(
                    f"Не удалось вычислить bbox-периметр/толщину для "
                    f"{safe_get_attr(element, 'GlobalId')}: {exc}"
                )
            if bbox_formwork:
                if not perim_present:
                    info['Длина_Perimeter_мм'] = bbox_formwork['perimeter_mm']
                if not depth_present:
                    info['Длина_Depth_мм'] = bbox_formwork['depth_mm']
                logger.debug(
                    f"bbox-фолбэк опалубки ({ifc_class}) для "
                    f"'{safe_get_attr(element, 'Name')}': периметр="
                    f"{bbox_formwork['perimeter_mm']} мм, толщина="
                    f"{bbox_formwork['depth_mm']} мм"
                )

    # Все свойства
    info.update(get_all_properties(element))
    
    # СПЕЦИФИЧЕСКИЕ СВОЙСТВА ДЛЯ СМЕТЧИКА
    info.update(get_specific_properties(element))

    # Материал элемента берётся из Pset «ExpCheck_<Класс>::MGE_Material»
    # (колонки вида «Свойство::ExpCheck_Beam::MGE_Material», «Свойство::ExpCheck_Slab::MGE_Material»
    # и т.д.), а не из ассоциаций материала IFC, где часто стоит заглушка
    # «По умолчанию» или внутреннее имя Revit. Применяется в обоих режимах
    # (КР и АР) — парсинг идентичен.
    mge_material = _resolve_expcheck_material(info, info.get('Тип элемента', ''))
    if mge_material:
        info['Материал'] = mge_material

    return info


def analyze_qto_properties(ifc_file_path):
    """
    Анализирует все QTO свойства в IFC файле (для отладки)
    """
    model = ifcopenshell.open(ifc_file_path)
    
    print("=" * 80)
    print("АНАЛИЗ QTO (Quantity Take-Off) СВОЙСТВ В IFC ФАЙЛЕ")
    print("=" * 80)
    
    # Словарь для сбора всех уникальных QTO свойств
    all_qto_properties = {}
    
    # Проходим по всем элементам
    for element in model:
        if hasattr(element, 'IsDefinedBy'):
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByProperties'):
                    props = rel.RelatingPropertyDefinition
                    
                    # Проверяем, является ли это QTO
                    if props and props.is_a('IfcElementQuantity'):
                        qto_name = props.Name if hasattr(props, 'Name') else "Без имени"
                        element_type = element.is_a()
                        
                        if qto_name not in all_qto_properties:
                            all_qto_properties[qto_name] = {
                                'count': 0,
                                'element_types': set(),
                                'quantities': {}
                            }
                        
                        all_qto_properties[qto_name]['count'] += 1
                        all_qto_properties[qto_name]['element_types'].add(element_type)
                        
                        # Анализируем количества внутри QTO
                        if hasattr(props, 'Quantities'):
                            for qty in props.Quantities:
                                qty_name = qty.Name
                                qty_type = qty.is_a()
                                
                                if qty_name not in all_qto_properties[qto_name]['quantities']:
                                    all_qto_properties[qto_name]['quantities'][qty_name] = {
                                        'type': qty_type,
                                        'count': 0
                                    }
                                
                                all_qto_properties[qto_name]['quantities'][qty_name]['count'] += 1
    
    # Выводим результаты
    for qto_name, qto_data in all_qto_properties.items():
        print(f"\n📊 QTO Set: {qto_name}")
        print(f"   Количество использований: {qto_data['count']}")
        print(f"   Типы элементов: {', '.join(sorted(qto_data['element_types']))}")
        print(f"   Свойства:")
        
        for qty_name, qty_data in qto_data['quantities'].items():
            print(f"     • {qty_name} ({qty_data['type']}) - {qty_data['count']} использований")
    
    return all_qto_properties


def parse_name(name: str, ifc_class: str):
    """
    Извлекает геометрические параметры из имени элемента
    
    Для стен и перекрытий: ищет толщину в мм
    Для колонн: ищет размеры AxB, возвращает ширину (мин. размер) и периметр в мм
    Для балок: ищет размеры AxB, возвращает ширину (мин. размер)
    """
    if not name or name == '-':
        return None
    
    result = {}
    
    if ifc_class in ["IfcWall", "IfcWallStandardCase"]:
        # Ищем толщину стены: 100мм, 200 мм, t=100 и т.д.
        # (?<!\d) вместо \b: символ «_» считается word-символом, поэтому
        # \b не срабатывает перед числом в именах вида «ADSK_Бетон В25_200 мм»,
        # где толщина отделена подчёркиванием от марки бетона.
        # Аналогично (?!\d) вместо \b после «мм»: в именах вида
        # «Стена_180мм_ЖБ_B35_W4_F75» за «мм» идёт подчёркивание (word-символ)
        # и \b не срабатывает — толщина из таких имён не извлекалась.
        patterns = [
            r'(?:толщина|t)[\s=]*(\d{2,3})\s?мм(?!\d)',  # толщина 100мм или t=100
            r'(?<!\d)(\d{2,3})\s?мм(?!\d)',             # 100мм / В25_200 мм / Стена_180мм_ЖБ
            r'(?<!\d)(\d{2,3})mm(?!\d)',                # 100mm
        ]
        for pattern in patterns:
            match = re.search(pattern, name, re.IGNORECASE)
            if match:
                result['Ширина, мм'] = float(match.group(1))
                break

    elif ifc_class == "IfcSlab":
        # Ищем толщину перекрытия
        patterns = [
            r'(?:толщина|t|h)[\s=]*(\d{2,3})\s?мм(?!\d)',
            r'(?<!\d)(\d{2,3})\s?мм(?!\d)',
            r'(?<!\d)(\d{2,3})mm(?!\d)',
        ]
        for pattern in patterns:
            match = re.search(pattern, name, re.IGNORECASE)
            if match:
                result['Ширина, мм'] = float(match.group(1))
                break

    elif ifc_class == "IfcColumn":
        # Ищем размеры колонны: 400x400, 400х400, 400*400, 400/400
        patterns = [
            r'(\d{2,4})\s?[xх\*]\s?(\d{2,4})',
            r'(\d{2,4})/(\d{2,4})',
        ]
        for pattern in patterns:
            match = re.search(pattern, name, re.IGNORECASE)
            if match:
                a = float(match.group(1))
                b = float(match.group(2))
                result['Ширина, мм'] = min(a, b)
                result['Периметр, мм'] = 2 * (a + b)  # Периметр в миллиметрах
                break

    elif ifc_class == "IfcBeam":
        # Ищем размеры балки: 200x400, 200х400, bxh 200x400
        patterns = [
            r'(\d{2,4})\s?[xх\*]\s?(\d{2,4})',
            r'b\s?[xх\*]\s?h\s?(\d{2,4})\s?[xх\*]\s?(\d{2,4})',  # bxh 200x400
        ]
        for pattern in patterns:
            match = re.search(pattern, name, re.IGNORECASE)
            if match:
                a = float(match.group(1))
                b = float(match.group(2))
                # Для балки ширина - это обычно меньший размер
                result['Ширина, мм'] = min(a, b)
                break
    
    return result if result else None


def fill_missing_from_name(df):
    """
    Заполняет пропуски в геометрических параметрах, извлекая данные из имени элемента
    
    Для стен: заполняет 'Ширина, мм'
    Для перекрытий: заполняет 'Ширина, мм'
    Для колонн: заполняет 'Ширина, мм' и 'Периметр, мм'
    Для балок: заполняет 'Ширина, мм'
    """
    logger.info("Заполнение пропусков в геометрических параметрах из имени элемента...")
    
    filled_count = {
        'Стены': 0,
        'Перекрытия': 0,
        'Колонны': 0,
        'Балки': 0
    }
    
    for idx, row in df.iterrows():
        name = row.get('Имя', '-')
        ifc_class = row.get('Тип элемента', '')
        
        # Пропускаем, если нет имени или класс не поддерживается
        if name == '-' or ifc_class not in ['IfcWall', 'IfcWallStandardCase', 'IfcSlab', 'IfcColumn', 'IfcBeam']:
            continue
        
        # Извлекаем параметры из имени
        extracted = parse_name(name, ifc_class)
        
        if extracted is None:
            continue
        
        # Заполняем пропуски в зависимости от типа элемента
        if ifc_class in ['IfcWall', 'IfcWallStandardCase'] and 'Ширина, мм' in extracted:
            if row.get('Ширина, мм') == '-' or pd.isna(row.get('Ширина, мм')):
                df.at[idx, 'Ширина, мм'] = extracted['Ширина, мм']
                filled_count['Стены'] += 1
                logger.debug(f"Стена '{name}': заполнена Ширина = {extracted['Ширина, мм']} мм")
        
        elif ifc_class == 'IfcSlab' and 'Ширина, мм' in extracted:
            if row.get('Ширина, мм') == '-' or pd.isna(row.get('Ширина, мм')):
                df.at[idx, 'Ширина, мм'] = extracted['Ширина, мм']
                filled_count['Перекрытия'] += 1
                logger.debug(f"Перекрытие '{name}': заполнена Ширина = {extracted['Ширина, мм']} мм")
        
        elif ifc_class == 'IfcColumn':
            column_filled = False
            # Заполняем Ширину
            if 'Ширина, мм' in extracted:
                if row.get('Ширина, мм') == '-' or pd.isna(row.get('Ширина, мм')):
                    df.at[idx, 'Ширина, мм'] = extracted['Ширина, мм']
                    column_filled = True
                    logger.debug(f"Колонна '{name}': заполнена Ширина = {extracted['Ширина, мм']} мм")
            
            # Заполняем Периметр (в миллиметрах)
            if 'Периметр, мм' in extracted:
                if row.get('Периметр, мм') == '-' or pd.isna(row.get('Периметр, мм')):
                    df.at[idx, 'Периметр, мм'] = extracted['Периметр, мм']
                    column_filled = True
                    logger.debug(f"Колонна '{name}': заполнен Периметр = {extracted['Периметр, мм']} мм")
            
            if column_filled:
                filled_count['Колонны'] += 1
        
        elif ifc_class == 'IfcBeam' and 'Ширина, мм' in extracted:
            if row.get('Ширина, мм') == '-' or pd.isna(row.get('Ширина, мм')):
                df.at[idx, 'Ширина, мм'] = extracted['Ширина, мм']
                filled_count['Балки'] += 1
                logger.debug(f"Балка '{name}': заполнена Ширина = {extracted['Ширина, мм']} мм")
    
    # Выводим статистику
    logger.info("Статистика заполнения пропусков из имени элемента:")
    total_filled = 0
    for element_type, count in filled_count.items():
        if count > 0:
            logger.info(f"  • {element_type}: заполнено {count} элементов")
            total_filled += count
    
    if total_filled == 0:
        logger.info("  • Пропусков для заполнения из имени не найдено")
    else:
        logger.info(f"  • Всего заполнено: {total_filled} элементов")
    
    return df


def zero_step(ifc_file, output_folder=None, write_full_data=True, processing_type="KR"):
    """Основная функция обработки IFC файла

    Args:
        ifc_file: путь к IFC-файлу
        output_folder: папка для сохранения результатов
        write_full_data: если False — пропускает запись IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx
            (242 колонки), экономя ~90-100 с на больших файлах.
            ДЛЯ_СМЕТЧИКА_исправленный.xlsx и сокращённый создаются в любом случае.
        processing_type: тип обработки — "KR" (конструктив, по умолчанию)
            или "AR" (архитектура). Определяет набор извлекаемых IFC-классов:
            ELEMENT_TYPES_KR / ELEMENT_TYPES_AR.
    """
    logger.info(f"Начата обработка файла {ifc_file} (тип: {processing_type})")

    processing_type = str(processing_type).upper()
    selected_types = ELEMENT_TYPES_AR if processing_type == "AR" else ELEMENT_TYPES_KR
    logger.info(
        f"Извлекаемые IFC-классы (processing_type={processing_type}): "
        f"{len(selected_types)} шт"
    )

    model = ifcopenshell.open(ifc_file)

    logger.info("Обработка ifc с анализом этажей")
    
    # Анализ QTO свойств (для отладки)
    analyze_qto_properties(ifc_file)

    storeys = {}
    for storey in model.by_type('IfcBuildingStorey'):
        name = safe_get_attr(storey, 'Name')
        elevation = safe_get_attr(storey, 'Elevation')
        if elevation != '-':
            elev_val = float(elevation)
            
            if abs(elev_val) > 100:  
                elev_val = elev_val / 1000
                print(f"   ⚠️ Обнаружены миллиметры! {float(elevation)} мм → {elev_val} м")
            elevation_mm = round(elev_val * 1000, 2)
            storey_type = classify_storey_type(name, elevation_mm)
            print(f"   • {name}: {elev_val} м ({elevation_mm} мм) - {storey_type}")
            storeys[name] = {'elevation': elev_val, 'type': storey_type}

    all_elevations = []
    ground_elevations = []   

    for storey in model.by_type('IfcBuildingStorey'):
        if hasattr(storey, 'Elevation') and storey.Elevation is not None:
            elev = float(storey.Elevation)
            
            if abs(elev) > 100:   
                elev = elev / 1000
            
            all_elevations.append(elev)
            
            name = safe_get_attr(storey, 'Name')
            elev_mm = round(elev * 1000, 2)
            storey_type = classify_storey_type(name, elev_mm)
            
            # Цокольные этажи с отрицательной отметкой (например «-1/1») не
            # относятся к надземной части — надземная часть выше отм. 0,000
            if (storey_type in ['Цокольный', 'Надземный', 'Технический', 'Мансардный']
                    and elev >= 0):
                ground_elevations.append(elev)

    if ground_elevations:
        min_ground = min(ground_elevations)   
        max_ground = max(ground_elevations)   
        height_above_ground = max_ground - min_ground
        
        if all_elevations:
            total_height = max(all_elevations) - min(all_elevations)
            
            building_height_info = {
                'Высота_надземной_части_м': round(height_above_ground, 3),
                'Общая_высота_здания_м': round(total_height, 3),
                'Минимальная_отметка_надземной_части_м': round(min_ground, 3),
                'Максимальная_отметка_надземной_части_м': round(max_ground, 3)
            }
        else:
            height_above_ground = 0
            building_height_info = {
                'Высота_надземной_части_м': 0,
                'Общая_высота_здания_м': 0,
                'Минимальная_отметка_надземной_части_м': 0,
                'Максимальная_отметка_надземной_части_м': 0
            }
    else:
        height_above_ground = 0
        building_height_info = {
            'Высота_надземной_части_м': 0,
            'Общая_высота_здания_м': 0,
            'Минимальная_отметка_надземной_части_м': 0,
            'Максимальная_отметка_надземной_части_м': 0
        }

    # Высота основного (типового) этажа. В режиме АР расценки на
    # архитектурно-отделочные работы привязаны к высоте этажа, поэтому
    # в веб-интерфейсе в этом режиме должна показываться именно она,
    # а не общая высота здания (как в режиме КР).
    main_floor_height = find_main_floor_height(storeys)
    building_height_info['Высота_основного_этажа_м'] = round(main_floor_height, 3)
    logger.info(f"Высота основного этажа: {main_floor_height} м")

    elements = []

    for ifc_type, ru_name in selected_types:
        elems = model.by_type(ifc_type)
        if len(elems) > 0:
            print(f"   {ifc_type} ({ru_name}): {len(elems)} шт")
            for elem in elems:
                elem_info = get_element_info(elem, processing_type=processing_type)
                elem_info['Тип (RU)'] = ru_name
                elements.append(elem_info)

    # Высота здания = самый высокий уровень среди всех элементов.
    # Раньше высота считалась только по этажам типа Цокольный/Надземный/Технический/
    # Мансардный, из-за чего этаж "Крыша" (тип Кровля) исключался и высота
    # занижалась. Теперь берём максимум по уровням всех элементов.
    element_levels_mm = []
    for el in elements:
        lvl = el.get('Уровень_этажа_мм')
        if lvl is not None and lvl != '-':
            try:
                element_levels_mm.append(float(lvl))
            except (ValueError, TypeError):
                pass

    if element_levels_mm:
        max_element_level_m = max(element_levels_mm) / 1000.0
        building_height_info['Высота_надземной_части_м'] = round(max_element_level_m, 3)
        logger.info(
            f"Высота здания определена по максимальному уровню элементов: "
            f"{max_element_level_m} м"
        )
    else:
        logger.info(
            f"Уровни элементов не найдены, используется высота по этажам: "
            f"{building_height_info['Высота_надземной_части_м']} м"
        )

    # Этажность здания: по наибольшему числовому индикатору в колонке
    # «Этаж» параметров элементов (> 2 → многоэтажное).
    storeys_type = detect_storeys_type(el.get('Этаж') for el in elements)
    building_height_info['Этажность_здания'] = storeys_type
    logger.info(f"Этажность здания (по колонке «Этаж»): {storeys_type}")

    df = pd.DataFrame(elements)
    df = df.fillna('-')

    base_cols = ['Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал']
    storey_cols = ['Этаж', 'Тип_этажа', 'Уровень_этажа_мм']
    
    # Разделяем колонки на QTO и обычные
    qto_cols = [col for col in df.columns if col.startswith('QTO_')]
    regular_other_cols = [col for col in df.columns if col not in base_cols + storey_cols + qto_cols]
    
    # Сортируем: базовые, этажи, QTO, обычные свойства
    df = df[base_cols + storey_cols + qto_cols + regular_other_cols]

    # Проверка и переименование столбцов для совместимости
    if 'Длина_Width_мм' not in df.columns and 'Толщина_мм' in df.columns:
        df['Длина_Width_мм'] = df['Толщина_мм']
        logger.info("Столбец 'Толщина_мм' скопирован в 'Длина_Width_мм'")
    
    # ============================================================================
    # ОПРЕДЕЛЯЕМ СЛОВАРЬ СООТВЕТСТВИЯ ЭЛЕМЕНТОВ И ГЕОМЕТРИЧЕСКИХ ПАРАМЕТРОВ
    # ============================================================================
    
    geometry_mapping = {
        'Стены': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_WallBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_WallBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Thickness',
                'Длина_Width_мм',
                'QTO_Qto_WallBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_WallBaseQuantities_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Длина_Height_мм',
                'QTO_Qto_WallBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_WallBaseQuantities_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_WallBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_WallBaseQuantities_GROSS',
                'Площадь_GrossSideArea_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GrossSideArea_м2',
                'Свойство_Qto_WallBaseQuantities_GrossSideArea',
                'Площадь_GrossFootprintArea_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GrossFootprintArea_м2',
                'Свойство_Qto_WallBaseQuantities_GrossFootprintArea',
                'Площадь_GrossArea_м2',
                'QTO_Qto_WallBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_WallBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_WallBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_WallBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_WallBaseQuantities_NetVolume',
                'Свойство_Qto_WallBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_WallLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_WallReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Перекрытия': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_SlabBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Thickness',
                'Длина_Width_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_SlabBaseQuantities_Width',
                'Свойство_RusSet_SlabBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Thickness',
                'Длина_Height_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_SlabBaseQuantities_NominalThickness',
                'Свойство_Pset_PrecastSlab_NominalThickness',
                'Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'
            ],
            'ПЕРИМЕТР': [
                'Длина_Perimeter_мм',
                'QTO_Qto_SlabBaseQuantities_Длина_Perimeter_мм',
                'Свойство_Qto_SlabBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_SlabBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_SlabBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_SlabBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_SlabBaseQuantities_GrossArea',
                'Площадь_GrossSlabArea_м2',
                'QTO_Qto_SlabBaseQuantities_Площадь_GrossSlabArea_м2',
                'Свойство_Qto_SlabBaseQuantities_GrossSlabArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_SlabBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_SlabBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_SlabBaseQuantities_NetVolume',
                'Свойство_Qto_SlabBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_SlabLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_SlabReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Колонны': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_ColumnBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_ColumnBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Width',
                'Длина_Width_мм',
                'QTO_Qto_ColumnBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_ColumnBaseQuantities_Width',
                'Свойство_RusSet_ColumnBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Длина_Height_мм',
                'QTO_Qto_ColumnBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_ColumnBaseQuantities_Height',
                'Свойство_RusSet_ColumnBaseQuantities_RUS_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_ColumnBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_ColumnBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_ColumnBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_ColumnBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_ColumnBaseQuantities_GrossArea',
                'Площадь_GrossSurfaceArea_м2',
                'QTO_Qto_ColumnBaseQuantities_Площадь_GrossSurfaceArea_м2',
                'Свойство_Qto_ColumnBaseQuantities_GrossSurfaceArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_ColumnBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_ColumnBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_ColumnBaseQuantities_NetVolume',
                'Свойство_Qto_ColumnBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_ColumnLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_ColumnReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Балки': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_BeamBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_BeamBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Width',
                'Длина_Width_мм',
                'QTO_Qto_BeamBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_BeamBaseQuantities_Width',
                'Свойство_RusSet_BeamBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Длина_Height_мм',
                'QTO_Qto_BeamBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_BeamBaseQuantities_Height',
                'Свойство_RusSet_BeamBaseQuantities_RUS_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_BeamBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_BeamBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_BeamBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_BeamBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_BeamBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_BeamBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_BeamBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_BeamBaseQuantities_NetVolume',
                'Свойство_Qto_BeamBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_BeamLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_BeamReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Лестницы': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_StairBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_StairBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Width',
                'Длина_Width_мм',
                'QTO_Qto_StairBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_StairBaseQuantities_Width',
                'Свойство_RusSet_StairBaseQuantities_RUS_Width',
                'Свойство_RusSet_StairFlightBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Длина_Height_мм',
                'QTO_Qto_StairBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_StairBaseQuantities_Height',
                'Свойство_RusSet_StairBaseQuantities_RUS_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_StairBaseQuantities_Perimeter',
                # bbox-фолбэк периметра лестничных маршей (2 × длина по
                # склону) — заполняется в get_element_info для IfcStairFlight
                'Длина_Perimeter_мм',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_StairBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_StairBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_StairBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_StairBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_StairBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_StairBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_StairBaseQuantities_NetVolume',
                'Свойство_Qto_StairBaseQuantities_GrossVolume',
                'Свойство_Qto_StairFlightBaseQuantities_NetVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_StairLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Свойство_ExpCheck_StairReinforcement_MGE_ReinforceStrengthClass',
                'ReinforcementVolumeRatio'
            ],
        },
        'Пандусы': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_RampBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_RampBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Width',
                'Длина_Width_мм',
                'QTO_Qto_RampBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_RampBaseQuantities_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Длина_Height_мм',
                'QTO_Qto_RampBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_RampBaseQuantities_Height',
                'Высота_мм', 'Height_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_RampBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_RampBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_RampBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_RampBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_RampBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_RampBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_RampBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_RampBaseQuantities_NetVolume',
                'Свойство_Qto_RampBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_RusSet_RampLabel_RUS_ReinforcementVolumeRatio',
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Прочие_элементы': {
            'ДЛИНА': ['Свойство_RusSet_Quantities_RUS_Length', 'Длина_мм', 'Length_мм'],
            'ШИРИНА': ['Свойство_RusSet_Quantities_RUS_Width', 'Свойство_RusSet_Quantities_RUS_Thickness', 'Толщина_мм', 'Width_мм', 'Длина_Width_мм'],
            'ВЫСОТА': ['Свойство_RusSet_Quantities_RUS_Height', 'Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'],
            'ПЕРИМЕТР': ['Perimeter_мм', 'Периметр_мм'],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_BaseQuantities_Площадь_GROSS_м2',
                'Свойство_BaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_BaseQuantities_Площадь_GrossArea_м2',
                'Свойство_BaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_BaseQuantities_Объём_NetVolume_м3',
                'QTO_BaseQuantities_Объём_GrossVolume_литры',
                'Свойство_BaseQuantities_NetVolume',
                'Свойство_BaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Покрытие': {
            'ДЛИНА': ['Длина_мм', 'Length_мм'],
            'ШИРИНА': ['Толщина_мм', 'Width_мм', 'Длина_Width_мм'],
            'ВЫСОТА': ['Высота_мм', 'Height_мм'],
            'ПЕРИМЕТР': ['Perimeter_мм', 'Периметр_мм'],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_CoveringBaseQuantities_Площадь_GROSS_м2',
                'Свойство_CoveringBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_CoveringBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_CoveringBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_CoveringBaseQuantities_Объём_NetVolume_м3',
                'QTO_CoveringBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_CoveringBaseQuantities_NetVolume',
                'Свойство_CoveringBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Фундамент': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_FootingBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_FootingBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Width',
                'Длина_Width_мм',
                'QTO_Qto_FootingBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_FootingBaseQuantities_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Длина_Height_мм',
                'QTO_Qto_FootingBaseQuantities_Длина_Height_мм',
                'Свойство_Qto_FootingBaseQuantities_Height',
                'Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_FootingBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_FootingBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_FootingBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_FootingBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_FootingBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_FootingBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_FootingBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_FootingBaseQuantities_NetVolume',
                'Свойство_Qto_FootingBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
        'Свая': {
            'ДЛИНА': [
                'Свойство_RusSet_Quantities_RUS_Length',
                'Длина_Length_мм',
                'QTO_Qto_PileBaseQuantities_Длина_Length_мм',
                'Свойство_Qto_PileBaseQuantities_Length',
                'Длина_мм', 'Length_мм'
            ],
            'ШИРИНА': [
                'Свойство_RusSet_Quantities_RUS_Width',
                'Длина_Width_мм',
                'QTO_Qto_PileBaseQuantities_Длина_Width_мм',
                'Свойство_Qto_PileBaseQuantities_Width',
                'Свойство_RusSet_PileBaseQuantities_RUS_Width',
                'Толщина_мм', 'Width_мм'
            ],
            'ВЫСОТА': [
                'Свойство_RusSet_Quantities_RUS_Height',
                'Свойство_Qto_PileBaseQuantities_Height',
                'Высота_мм', 'Height_мм', 'Глубина_выдавливания_мм'
            ],
            'ПЕРИМЕТР': [
                'Свойство_Qto_PileBaseQuantities_Perimeter',
                'Perimeter_мм', 'Периметр_мм'
            ],
            'ПЛОЩАДЬ': [
                'Площадь_GROSS_м2',
                'QTO_Qto_PileBaseQuantities_Площадь_GROSS_м2',
                'Свойство_Qto_PileBaseQuantities_GROSS',
                'Площадь_GrossArea_м2',
                'QTO_Qto_PileBaseQuantities_Площадь_GrossArea_м2',
                'Свойство_Qto_PileBaseQuantities_GrossArea',
                'Площадь_м2', 'Area_м2'
            ],
            'ОБЪЕМ': [
                'Свойство_RusSet_Quantities_RUS_Volume',
                'Объём_NetVolume_м3',
                'Объём_GrossVolume_литры',
                'QTO_Qto_PileBaseQuantities_Объём_NetVolume_м3',
                'QTO_Qto_PileBaseQuantities_Объём_GrossVolume_литры',
                'Свойство_Qto_PileBaseQuantities_NetVolume',
                'Свойство_Qto_PileBaseQuantities_GrossVolume',
                'Объём_м3', 'Volume_м3'
            ],
            'ReinforcementVolumeRatio': [
                'Свойство_Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'Pset_ConcreteElementGeneral_ReinforcementVolumeRatio',
                'ReinforcementVolumeRatio'
            ],
        },
    }
    
    # ============================================================================
    # NET-ПЛОЩАДИ (NetArea/NetSideArea/...): альтернативные имена параметров
    # ============================================================================
    # У части элементов (гидроизоляции, утеплитель, приямки и т.п.) площадь
    # задана ТОЛЬКО в Net-количествах (NetArea), без Gross-аналогов —
    # такие элементы оставались без «Площадь, м2», и объёмы работ в м²
    # для них не считались. Добавляем Net-источники в списки ПЛОЩАДЬ
    # ПОСЛЕ Gross-источников (Gross сохраняет приоритет).
    _GROSS_TO_NET = [
        ('GrossFootprintArea', 'NetFootprintArea'),
        ('GrossSurfaceArea', 'NetSurfaceArea'),
        ('GrossSideArea', 'NetSideArea'),
        ('GrossSlabArea', 'NetSlabArea'),
        ('GrossArea', 'NetArea'),
        ('GROSS', 'NetArea'),
    ]

    def _net_area_source(gross_source):
        for gross_token, net_token in _GROSS_TO_NET:
            if gross_token in gross_source:
                return gross_source.replace(gross_token, net_token)
        return None

    for _type_mapping in geometry_mapping.values():
        _area_sources = _type_mapping.get('ПЛОЩАДЬ')
        if _area_sources is None:
            continue
        _net_sources = []
        for _src in _area_sources:
            _net_src = _net_area_source(_src)
            if _net_src and _net_src not in _area_sources and _net_src not in _net_sources:
                _net_sources.append(_net_src)
        if _net_sources:
            # Вставляем перед универсальными хвостами ('Площадь_м2', 'Area_м2')
            _insert_at = len(_area_sources)
            for _generic in ('Площадь_м2', 'Area_м2'):
                if _generic in _area_sources:
                    _insert_at = min(_insert_at, _area_sources.index(_generic))
            _area_sources[_insert_at:_insert_at] = _net_sources

    # Тип «Изоляция» (IfcCovering в режиме КР: гидроизоляции, утеплитель) —
    # те же источники, что и для «Покрытие». Ранее тип отсутствовал в
    # geometry_mapping, и агрегированные «Площадь, м2»/«Объём, м3» для
    # изоляции не заполнялись вовсе.
    if 'Изоляция' not in geometry_mapping:
        geometry_mapping['Изоляция'] = {
            k: list(v) for k, v in geometry_mapping['Покрытие'].items()
        }

    # ============================================================================
    # FALLBACK: КОЛИЧЕСТВА, ВЫЧИСЛЕННЫЕ ИЗ ГЕОМЕТРИИ (bbox)
    # ============================================================================
    # Если у элемента нет QTO (IfcElementQuantity), количества вычисляются
    # из геометрии по координатам и попадают в колонки с префиксом QTO_bbox::.
    # Добавляем эти колонки в конец списков возможных источников geometry_mapping
    # — они используются как запасной вариант (срабатывают только тогда,
    # когда в IFC-файле нет «настоящих» QTO). Важно для режима АР: окна,
    # двери, покрытия и пр. часто не содержат IfcElementQuantity.
    _BBOX_FALLBACK = {
        'ДЛИНА': 'QTO_bbox::Длина_мм',
        'ШИРИНА': 'QTO_bbox::Ширина_мм',
        'ВЫСОТА': 'QTO_bbox::Высота_мм',
        'ОБЪЕМ': 'QTO_bbox::Объём_м3',
        'ПЛОЩАДЬ': 'QTO_bbox::Площадь_поверхности_м2',
    }
    for _type_mapping in geometry_mapping.values():
        for _param, _bbox_col in _BBOX_FALLBACK.items():
            _sources = _type_mapping.get(_param)
            if _sources is not None and _bbox_col not in _sources:
                _sources.append(_bbox_col)

    # ============================================================================
    # ФУНКЦИЯ ДЛЯ ИЗВЛЕЧЕНИЯ ЗНАЧЕНИЯ ПО ТИПУ ЭЛЕМЕНТА
    # ============================================================================
    
    def get_geometry_value(row, param_name, convert_to_m=False, convert_to_m3=False):
        """
        Извлекает геометрический параметр для конкретного элемента
        Для ПЛОЩАДИ - ищет ТОЛЬКО Gross
        Для ReinforcementVolumeRatio - возвращает как строку
        """
        element_type = row['Тип (RU)']
        
        # Получаем список возможных столбцов для этого типа элемента и параметра
        if element_type not in geometry_mapping:
            return '-'
        
        possible_columns = geometry_mapping[element_type].get(param_name, [])
        
        # Ищем первый существующий столбец с непустым значением
        for col in possible_columns:
            if col in df.columns:
                value = row[col]
                if value != '-' and pd.notna(value):
                    try:
                        # Для ReinforcementVolumeRatio возвращаем как есть
                        if param_name == 'ReinforcementVolumeRatio':
                            return str(value)
                        
                        num_value = float(value)
                        
                        # Конвертация единиц измерения
                        if convert_to_m:
                            # Конвертируем мм в м
                            if 'мм' in col or 'mm' in col.lower():
                                return round(num_value / 1000, 3)
                        elif convert_to_m3:
                            # Конвертируем литры в м3
                            if 'литры' in col.lower() or 'liters' in col.lower():
                                return round(num_value / 1000, 3)
                        
                        return round(num_value, 3)
                    except:
                        # Если не удалось преобразовать в число, возвращаем как строку
                        if param_name == 'ReinforcementVolumeRatio':
                            return str(value)
                        continue
        
        return '-'
    
    # ============================================================================
    # СОЗДАЕМ АГРЕГИРОВАННЫЕ СТОЛБЦЫ ГЕОМЕТРИИ И СВОЙСТВ
    # ============================================================================
    
    logger.info("Создание агрегированных столбцов с приоритетом Gross...")
    
    df['Длина, мм'] = df.apply(lambda row: get_geometry_value(row, 'ДЛИНА'), axis=1)
    df['Ширина, мм'] = df.apply(lambda row: get_geometry_value(row, 'ШИРИНА'), axis=1)
    df['Высота, мм'] = df.apply(lambda row: get_geometry_value(row, 'ВЫСОТА'), axis=1)
    df['Периметр, мм'] = df.apply(lambda row: get_geometry_value(row, 'ПЕРИМЕТР', convert_to_m=False), axis=1)
    df['Площадь, м2'] = df.apply(lambda row: get_geometry_value(row, 'ПЛОЩАДЬ'), axis=1)
    df['Объём, м3'] = df.apply(lambda row: get_geometry_value(row, 'ОБЪЕМ', convert_to_m3=True), axis=1)
    df['ReinforcementVolumeRatio'] = df.apply(lambda row: get_geometry_value(row, 'ReinforcementVolumeRatio'), axis=1)
    
    # В pandas 3.0 столбцы из одних строк (например, '-' из get_geometry_value)
    # получают dtype 'str', и запись в них float (из fill_missing_from_name)
    # падает с TypeError. Приводим к object, чтобы хранить числа и '-' вместе.
    for geom_col in ['Длина, мм', 'Ширина, мм', 'Высота, мм', 'Периметр, мм',
                     'Площадь, м2', 'Объём, м3', 'ReinforcementVolumeRatio']:
        df[geom_col] = df[geom_col].astype(object)
    
    # ============================================================================
    # ЗАПОЛНЯЕМ ПРОПУСКИ В ГЕОМЕТРИЧЕСКИХ ПАРАМЕТРАХ ИЗ ИМЕНИ ЭЛЕМЕНТА
    # ============================================================================
    df = fill_missing_from_name(df)

    # ============================================================================
    # ВЫЧИСЛЯЕМ ПЛОЩАДЬ ДЛЯ СТЕН С ПРОПУЩЕННОЙ «Площадь, м2»
    # ============================================================================
    # У части стен в IFC отсутствуют площадные QTO (GrossSideArea, GROSS и т.д.),
    # но есть NetVolume и Ширина (толщина, заполненная из имени или из QTO).
    # Боковая площадь одной грани стены = Объём / Толщина (м),
    # что совпадает с GrossSideArea = Длина × Высота для монолитных стен.
    # Это значение нужно для расчёта объёмов работ по опалубке в финальном перечне.
    if 'Площадь, м2' in df.columns and 'Объём, м3' in df.columns and 'Ширина, мм' in df.columns:
        _area_filled = 0
        for idx, row in df.iterrows():
            cur_area = row.get('Площадь, м2')
            if cur_area is not None and not (isinstance(cur_area, str) and cur_area.strip() in ('-', '')):
                continue
            ifc_class = str(row.get('Тип элемента', ''))
            if ifc_class not in ('IfcWall', 'IfcWallStandardCase'):
                continue
            vol = row.get('Объём, м3')
            width = row.get('Ширина, мм')
            try:
                vol_f = float(vol)
                width_f = float(width)
            except (ValueError, TypeError):
                continue
            if vol_f <= 0 or width_f <= 0:
                continue
            area_m2 = round(vol_f / (width_f / 1000.0), 3)
            df.at[idx, 'Площадь, м2'] = area_m2
            _area_filled += 1
            logger.debug(
                f"Стена '{row.get('Имя', '?')}': вычислена Площадь = {area_m2} м² "
                f"(Объём {vol_f} м³ / Ширина {width_f} мм)"
            )
        if _area_filled:
            logger.info(f"Вычислена площадь для {_area_filled} стен с пропущенной «Площадь, м2»")
    
    # Удаляем технические столбцы
    cols_to_drop = ['Глубина_выдавливания_мм', 'Координата_X_мм', 'Координата_Y_мм', 'Координата_Z_мм']
    df = df.drop([col for col in cols_to_drop if col in df.columns], axis=1)

    # Полный файл всех данных (242 колонки) — опционально.
    # На больших файлах запись занимает ~90-100 с и не нужна для формирования
    # ДЛЯ_СМЕТЧИКА_*.xlsx и Финального перечня работ, поэтому по умолчанию
    # в пайплайне она отключена (write_full_data=False).
    if write_full_data:
        if output_folder:
            output_filename = os.path.join(output_folder, 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx')
        else:
            output_filename = 'IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx'

        df.to_excel(output_filename, index=False)
    else:
        logger.info("Пропущена запись IFC_ВСЕ_ДАННЫЕ_исправленный.xlsx (write_full_data=False)")
    
    # ============================================================================
    # ОПРЕДЕЛЯЕМ КОД ЭЛЕМЕНТА (Код мсск) И ФИЛЬТРУЕМ ЭЛЕМЕНТЫ БЕЗ КОДА
    # ============================================================================
    # Коды поиска: параметры, содержащие "ElementCode" (например
    # Свойство_ExpCheck_Wall_MGE_ElementCode) ИЛИ "Element_Code"
    # (например Свойство_RusSet_Common_RUS_MSSK_Element_Code).
    # Валидным считается код, начинающийся с "ЭЛ" (например "ЭЛ 30 10 30 15").
    # Значения-заглушки ("0", "-", "НЕТ ДАННЫХ" и пр.) не считаются кодом —
    # элементы с такими значениями исключаются из таблиц для сметчика.

    element_code_cols = [col for col in df.columns
                         if 'elementcode' in col.lower().replace('_', '')]

    def _get_element_code(row):
        for col in element_code_cols:
            val = row[col]
            if val is not None and str(val).strip() and str(val) != '-':
                return str(val)
        return '-'

    if element_code_cols:
        element_codes = df.apply(_get_element_code, axis=1)
    else:
        element_codes = pd.Series(['-'] * len(df), index=df.index)

    valid_code_mask = element_codes.astype(str).str.strip().str.startswith('ЭЛ') & \
                      (element_codes.astype(str).str.strip() != '-')

    n_valid = int(valid_code_mask.sum())
    n_total = len(df)
    logger.info(f"Код элемента: валидных {n_valid} из {n_total} "
                f"(исключено {n_total - n_valid} элементов без кода)")

    # ============================================================================
    # СОЗДАЕМ СОКРАЩЕННУЮ ТАБЛИЦУ ДЛЯ СМЕТЧИКА
    # ============================================================================
    
    # Создаем сокращенный DataFrame
    df_short = pd.DataFrame()
    df_short['№ п/п'] = range(1, len(df) + 1)
    
    # Базовые столбцы
    df_short['Тип (RU)'] = df['Тип (RU)']
    df_short['Тип элемента'] = df['Тип элемента']
    df_short['Имя'] = df['Имя']
    df_short['GlobalId'] = df['GlobalId']
    df_short['Материал'] = df['Материал']
    df_short['Этаж'] = df['Этаж']
    df_short['Тип_этажа'] = df['Тип_этажа']
    
    # Добавляем агрегированные геометрические столбцы
    df_short['Длина, мм'] = df['Длина, мм']
    df_short['Толщина, мм'] = df['Ширина, мм']  # Используем Ширину как Толщину
    df_short['Высота, мм'] = df['Высота, мм']
    df_short['Периметр, мм'] = df['Периметр, мм']
    df_short['Площадь (Gross), м2'] = df['Площадь, м2']  
    df_short['Объем (Net), м3'] = df['Объём, м3']
    df_short['ReinforcementVolumeRatio'] = df['ReinforcementVolumeRatio']
    
    # Заменяем NaN на '-'
    df_short = df_short.fillna('-')
    
    # Оставляем только элементы с валидным кодом, перенумеровываем № п/п
    df_short = df_short[valid_code_mask.values].reset_index(drop=True)
    df_short['№ п/п'] = range(1, len(df_short) + 1)
    
    # Сохраняем сокращенную таблицу
    if output_folder:
        short_output_file = os.path.join(output_folder, 'ДЛЯ_СМЕТЧИКА_сокращенный.xlsx')
    else:
        short_output_file = 'ДЛЯ_СМЕТЧИКА_сокращенный.xlsx'
    
    df_short.to_excel(short_output_file, index=False)
    
    # ============================================================================
    # СОЗДАЕМ ПОЛНУЮ ТАБЛИЦУ ДЛЯ СМЕТЧИКА
    # ============================================================================
    
    smetchik_cols = ['Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал', 'Этаж', 'Тип_этажа', 'Уровень_этажа_мм']

    # Приоритетно добавляем QTO колонки (Gross и Net для площадей)
    for col in df.columns:
        if col.startswith('QTO_'):
            # Для площадей - добавляем Gross и Net: у части элементов
            # (гидроизоляции, утеплитель и т.п.) площадь задана только
            # в NetArea, без Gross-аналогов
            if 'Площадь' in col:
                if 'Gross' in col or 'GROSS' in col or 'Net' in col:
                    smetchik_cols.append(col)
            else:
                smetchik_cols.append(col)
    
    # Добавляем обычные геометрические параметры
    for col in df.columns:
        if not col.startswith('QTO_') and not col.endswith('_агрег_мм') and not col.endswith('_агрег_м') and not col.endswith('_агрег_м2') and not col.endswith('_агрег_м3'):
            if any(term in col for term in ['Длина', 'Ширина', 'Высота', 'Глубина']) and '_мм' in col:
                smetchik_cols.append(col)
            elif 'Объём' in col and ('_м3' in col or '_литры' in col):
                smetchik_cols.append(col)
            elif 'Площадь' in col and ('Gross' in col or 'Net' in col) and '_м2' in col:
                smetchik_cols.append(col)

    # ДОБАВЛЯЕМ СПЕЦИФИЧЕСКИЕ СВОЙСТВА ИЗ СПИСКА
    specific_col_names = [prop.replace('.', '_') for prop in SPECIFIC_PROPERTIES]
    for col in specific_col_names:
        if col in df.columns:
            smetchik_cols.append(col)
    
    # Добавляем агрегированные столбцы
    aggregated_cols = ['Длина, мм', 'Ширина, мм', 'Высота, мм', 'Периметр, мм', 'Площадь, м2', 'Объём, м3', 'ReinforcementVolumeRatio']
    for col in aggregated_cols:
        if col in df.columns and col not in smetchik_cols:
            smetchik_cols.append(col)

    existing_cols = [col for col in smetchik_cols if col in df.columns]

    df_smetchik = df[existing_cols].copy()
    df_smetchik = df_smetchik.fillna('-')

    df_smetchik.insert(0, '№ п/п', range(1, len(df_smetchik) + 1))

    # Колонка "Код мсск" — извлекается из параметров, содержащих "ElementCode"
    # (например: Свойство_ExpCheck_Wall_MGE_ElementCode,
    #  Свойство_ExpCheck_Slab_MGE_ElementCode, Свойство_ExpCheck_Column_MGE_ElementCode,
    #  Свойство_RusSet_Common_RUS_MSSK_Element_Code)
    element_code_cols = [col for col in df.columns
                         if 'elementcode' in col.lower().replace('_', '')]
    df_smetchik.insert(1, 'Код мсск', element_codes.values)

    df_smetchik['Примечание_сметчика'] = ''
    df_smetchik['Стоимость_за_ед_руб'] = ''
    df_smetchik['Общая_стоимость_руб'] = ''

    # Оставляем только элементы с валидным кодом, перенумеровываем № п/п
    df_smetchik = df_smetchik[valid_code_mask.values].reset_index(drop=True)
    df_smetchik['№ п/п'] = range(1, len(df_smetchik) + 1)

    # Поиск колонки с объемом для сводки (приоритет агрегированный)
    volume_col = 'Объём, м3'
    if volume_col not in df.columns:
        volume_col = None
        for col in df.columns:
            if col.startswith('QTO_') and 'Объём_NetVolume_м3' in col:
                volume_col = col
                break
        if not volume_col:
            for col in df.columns:
                if 'Объём_NetVolume_м3' in col:
                    volume_col = col
                    break

    summary_data = []
    df_for_summary = df[valid_code_mask.values]
    grouped = df_for_summary.groupby(['Тип (RU)', 'Тип элемента', 'Материал'])

    for (type_ru, type_elem, material), group in grouped:
        count = len(group)
        
        total_volume = 0
        if volume_col and volume_col in df_for_summary.columns:
            vol_series = pd.to_numeric(group[volume_col], errors='coerce').fillna(0)
            total_volume = vol_series.sum()
        
        # Также считаем общую Gross площадь
        total_gross_area = 0
        if 'Площадь, м2' in df_for_summary.columns:
            area_series = pd.to_numeric(group['Площадь, м2'], errors='coerce').fillna(0)
            total_gross_area = area_series.sum()
        
        summary_data.append({
            'Тип (RU)': type_ru,
            'Тип элемента': type_elem,
            'Материал': material if material != '-' else 'Не указан',
            'Количество, шт': count,
            'Объем, м³': round(total_volume, 3) if total_volume > 0 else '-',
            'Площадь Gross, м²': round(total_gross_area, 3) if total_gross_area > 0 else '-',
        })

    # Если данные отсутствуют (например, все элементы исключены фильтром кода),
    # заранее создаём каркас сводки с нужными колонками, чтобы итоговая таблица
    # существовала даже при нуле валидных элементов (pandas из пустого списка
    # создаёт DataFrame вообще без колонок, и обращение к 'Количество, шт' упадёт с KeyError).
    if summary_data:
        df_summary = pd.DataFrame(summary_data)
    else:
        df_summary = pd.DataFrame(columns=[
            'Тип (RU)', 'Тип элемента', 'Материал', 'Количество, шт',
            'Объем, м³', 'Площадь Gross, м²'
        ])

    total_count = df_summary['Количество, шт'].sum()
    total_volume = 0
    total_gross_area = 0
    for _, row in df_summary.iterrows():
        if row['Объем, м³'] != '-':
            total_volume += row['Объем, м³']
        if row['Площадь Gross, м²'] != '-':
            total_gross_area += row['Площадь Gross, м²']

    total_row = pd.DataFrame([{
        'Тип (RU)': 'ВСЕГО',
        'Тип элемента': '',
        'Материал': '',
        'Количество, шт': total_count,
        'Объем, м³': round(total_volume, 3),
        'Площадь Gross, м²': round(total_gross_area, 3),
    }])
    df_summary = pd.concat([df_summary, total_row], ignore_index=True)

    if output_folder:
        output_file = os.path.join(output_folder, 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx')
        height_file = os.path.join(output_folder, 'height.txt')
    else:
        output_file = 'ДЛЯ_СМЕТЧИКА_исправленный.xlsx'
        height_file = 'height.txt'

    logger.info("Обработка файла завершена")

    # В режиме АР показываем высоту основного этажа, в КР — высоту здания.
    if processing_type == "AR":
        primary_height = building_height_info['Высота_основного_этажа_м']
        primary_label = 'Высота основного этажа'
    else:
        primary_height = building_height_info['Высота_надземной_части_м']
        primary_label = 'Высота надземной части'

    with open(height_file, 'w', encoding='utf-8') as file:
        file.write(str(primary_height))

    # Глобальные константы, определённые по модели IFC: сохраняются отдельным
    # JSON-файлом (структура идентична ПОС_глобальные_константы.json — полный
    # перечень констант, приоритет источников: IFC → ПОС).
    ifc_constants_doc = _build_ifc_constants(
        building_height_info, Path(ifc_file).name)
    ifc_constants_file = os.path.join(
        os.path.dirname(height_file) or '.', IFC_CONSTANTS_FILENAME)
    try:
        with open(ifc_constants_file, 'w', encoding='utf-8') as f:
            json.dump(ifc_constants_doc, f, ensure_ascii=False, indent=2)
        logger.info(f"Глобальные константы IFC сохранены: {ifc_constants_file}")
    except Exception as exc:
        logger.warning(f"Не удалось записать {ifc_constants_file}: {exc}")

    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        df_smetchik.to_excel(writer, sheet_name='Данные', index=False)
        df_summary.to_excel(writer, sheet_name='Сводка_по_типам', index=False)

        # Основная высота (для веб-интерфейса) всегда первой строкой листа.
        height_rows = [{
            'Параметр': primary_label,
            'Значение_м': primary_height,
            'Значение_мм': primary_height * 1000
        }]
        # Остальные параметры высоты — информационно, после основной строки.
        for label, value in [
            ('Высота надземной части', building_height_info['Высота_надземной_части_м']),
            ('Высота основного этажа', building_height_info.get('Высота_основного_этажа_м', 0.0)),
            ('Общая высота здания', building_height_info['Общая_высота_здания_м']),
            ('Минимальная отметка надземной части', building_height_info['Минимальная_отметка_надземной_части_м']),
            ('Максимальная отметка надземной части', building_height_info['Максимальная_отметка_надземной_части_м']),
        ]:
            if label == primary_label:
                continue
            height_rows.append({
                'Параметр': label,
                'Значение_м': value,
                'Значение_мм': value * 1000
            })

        df_height = pd.DataFrame(height_rows)
        df_height.to_excel(writer, sheet_name='Высота_здания', index=False)

    logger.info(f"Файл сохранен в {output_file}")
    logger.info(f"Сокращенный файл сохранен в {short_output_file}")
    logger.info("===ПРЕДВАРИТЕЛЬНЫЙ ЭТАП ЗАВЕРШЕН===")