"""
Модуль построения справочной структуры из IFC-файла.

Работает параллельно с основным пайплайном (zero_step).
Формирует выходной JSON в формате, необходимом для поиска работ
по API-справочнику ТСН.

Этапы:
  1. extract_elements_from_ifc  — извлечение всех элементов из IFC
  2. group_elements_by_type     — группировка через process_ifc_excel (тот же путь, что в веб-интерфейсе)
  3. build_reference_output     — преобразование в целевой формат
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import ifcopenshell
import pandas as pd

from src.core.logger import setup_logger

# Переиспользуем функции из существующих модулей
from src.services.zero_step import (
    element_types,
    SPECIFIC_PROPERTIES,
    get_element_info,
)

from src.services.group_excel import (
    GEOMETRY_GROUP_RULES,
    get_ifc_type,
    safe_parse_float,
    process_ifc_excel,
    is_hydro_vertical,
    is_hydro_horizontal,
)

logger = setup_logger(__name__)

# Маппинг внутренних имён колонок XLSX на русские
_COLUMN_RU_NAMES = {
    'buildingElementName': 'Элемент',
    'isActive': 'Активен',
    'elementCount': 'Количество',
    'totalMeasure_type': 'Тип измерения',
    'totalMeasure_value': 'Значение',
    'totalMeasure_unit': 'Единица измерения',
    # characteristics (нормализованные)
    'char_Материал': 'Материал',
    'char_Расположение': 'Расположение',
    'char_Толщина': 'Толщина',
    'char_Площадь': 'Площадь',
    'char_Периметр': 'Периметр',
    'char_Длина': 'Длина',
    'char_Объём': 'Объём',
    # additional (оригинальные)
    'additional_Имя элемента': 'Имя элемента',
    'additional_Толщина': 'Толщина элемента',
    'additional_Площадь': 'Площадь элемента',
    'additional_Периметр': 'Периметр элемента',
    'additional_Длина': 'Длина элемента',
    'additional_Высота': 'Высота элемента',
    'additional_Этаж': 'Этаж',
    'additional_Тип этажа': 'Тип этажа',
}

# Порядок колонок в XLSX (внутренние имена, до переименования в русские).
_COLUMN_ORDER = [
    'buildingElementName',
    'isActive',
    'elementCount',
    'totalMeasure_type',
    'totalMeasure_value',
    'totalMeasure_unit',
    # characteristics (нормализованные)
    'char_Материал',
    'char_Расположение',
    'char_Толщина',
    'char_Площадь',
    'char_Периметр',
    # additionalCharacteristics (оригинальные)
    'additional_Имя элемента',
    'additional_Толщина',
    'additional_Площадь',
    'additional_Периметр',
]


def _prepare_xlsx_df(xlsx_rows: list) -> pd.DataFrame:
    """Формирует DataFrame из строк XLSX, упорядочивает колонки и переименовывает в русские."""
    if not xlsx_rows:
        return pd.DataFrame()
    df = pd.DataFrame(xlsx_rows).fillna('')
    # Упорядочиваем колонки по _COLUMN_ORDER (только те, что есть в данных)
    ordered_cols = [col for col in _COLUMN_ORDER if col in df.columns]
    # Добавляем колонки, которых нет в _COLUMN_ORDER, в конец
    remaining_cols = [col for col in df.columns if col not in _COLUMN_ORDER]
    df = df[ordered_cols + remaining_cols]
    # Переименовываем колонки в русские названия
    rename_map = {}
    for col in df.columns:
        if col in _COLUMN_RU_NAMES:
            rename_map[col] = _COLUMN_RU_NAMES[col]
        elif col.startswith('char_'):
            rename_map[col] = col.replace('char_', '', 1)
        elif col.startswith('additional_'):
            rename_map[col] = col.replace('additional_', '', 1)
    return df.rename(columns=rename_map)


# =====================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

def _get_geometry_label(ifc_type: str) -> str:
    """Возвращает название геометрической характеристики."""
    rule = GEOMETRY_GROUP_RULES.get(ifc_type, GEOMETRY_GROUP_RULES['default'])
    return rule.get('label', 'Объём')


def _extract_geometry_range(group_name: str) -> str:
    """
    Извлекает нормализованный диапазон из имени группы.
    Пример: 'Толщина: до 100 мм' → 'до 100'
            'Площадь: до 10 м²'  → 'до 10'
            'более 300 мм'       → 'более 300'
    """
    # Убираем префикс 'X: ' если есть
    name = group_name.split(': ', 1)[-1] if ': ' in group_name else group_name
    # Убираем единицы измерения
    name = re.sub(r'\s*(мм|м²|м3|м³|м)\s*$', '', name).strip()
    return name


def _extract_geometry_range_from_path(path: List[str], geo_label: str) -> Tuple[str, str]:
    """
    Ищет геометрический диапазон по всему пути группы.

    Листовая группа может называться 'Бетон: В35', а геометрия
    находится в родительской группе: 'Площадь: более 20 м²'.
    Проходим путь с конца к началу и ищем элемент, который
    начинается с geo_label (например 'Площадь:' или 'Толщина:').

    Если точное совпадение не найдено — ищем любой известный
    геометрический префикс (Площадь, Толщина, Длина).

    Возвращает (имя_характеристики, нормализованный_диапазон)
    или ('', '').
    """
    geometry_prefixes = {'Площадь', 'Толщина', 'Длина', 'Объём'}
    for part in reversed(path):
        if ': ' in part:
            prefix = part.split(': ', 1)[0].strip()
            if prefix == geo_label or prefix in geometry_prefixes:
                return prefix, _extract_geometry_range(part)
    return '', ''


def _determine_building_element_name(
    ru_type: str,
    name: str,
    ifc_type: str,
) -> str:
    """
    Определяет итоговое buildingElementName с учётом гидроизоляции.

    Если элемент относится к вертикальной или горизонтальной гидроизоляции
    (по тем же правилам, что в group_excel.py), возвращает соответствующее
    название. Иначе — возвращает ru_type (или name, если ru_type пуст).
    """
    if is_hydro_vertical(ru_type, name, ifc_type):
        return 'Вертикальная гидроизоляция'
    if is_hydro_horizontal(ru_type, name, ifc_type):
        return 'Горизонтальная гидроизоляция'
    # Значение по умолчанию: ru_type, иначе имя элемента
    if ru_type and ru_type != '-':
        return str(ru_type)
    if name and name != '-':
        return str(name)
    return ''


# Словарь для перевода названий элементов из множественного числа
# в единственное. Ключи — как они приходят из zero_step / group_excel,
# значения — единственное число для buildingElementName.
_SINGULAR_MAP = {
    'Стены': 'Стена',
    'Перекрытия': 'Перекрытие',
    'Колонны': 'Колонна',
    'Балки': 'Балка',
    'Лестницы': 'Лестница',
    'Пандусы': 'Пандус',
    'Плиты': 'Плита',
    'Плиты перекрытия': 'Плита перекрытия',
    'Прочие_элементы': 'Прочий_элемент',
    'Лестничные марши': 'Лестничный марш',
    'Сваи': 'Свая',
}


def _singularize_ru_name(name: str) -> str:
    """
    Приводит название конструктивного элемента к единственному числу.

    Если название найдено в словаре _SINGULAR_MAP — возвращает
    соответствующее значение. Иначе возвращает исходную строку без изменений.
    """
    if not name:
        return name
    return _SINGULAR_MAP.get(name, name)


def _get_location_name(part: str) -> str:
    """Преобразует ключ части здания в полное название."""
    mapping = {
        'Подземная': 'Подземная часть здания',
        'Цоколь': 'Цокольная часть здания',
        'Надземная': 'Надземная часть здания',
    }
    return mapping.get(part, 'Надземная часть здания')


def _normalize_material(material_str: str) -> str:
    """Нормализует название материала. Возвращает пустую строку, если материал не указан."""
    if not material_str or material_str in ('-', '', 'Не указан'):
        return ''
    mat = material_str.lower()
    if any(w in mat for w in ['железобетон', 'ж/б', 'жб', 'арматур']):
        return 'Железобетон'
    if any(w in mat for w in ['бетон', 'бетонн']):
        return 'Бетон'
    if any(w in mat for w in ['кирпич', 'кирпичн']):
        return 'Кирпичная кладка'
    if any(w in mat for w in ['металл', 'сталь', 'стально']):
        return 'Металл'
    if any(w in mat for w in ['дерев', 'древес']):
        return 'Дерево'
    if any(w in mat for w in ['камен', 'камень']):
        return 'Каменная кладка'
    return material_str.capitalize() if material_str else ''


def _get_original_geometry(element_data: dict, ifc_type: str) -> Optional[float]:
    """Возвращает оригинальное числовое значение геометрии элемента."""
    if ifc_type == 'IfcWall':
        # Приоритет: Длина_Width_мм (толщина стены, используется в GEOMETRY_GROUP_RULES)
        val = safe_parse_float(element_data.get('Длина_Width_мм', 0))
        if val > 0:
            return val
        # Запасной вариант: ширина сечения
        val = safe_parse_float(element_data.get('Ширина_сечения_мм', 0))
        if val > 0:
            return val
        # Глубина выдавливания — это высота/длина стены, НЕ толщина
        val = safe_parse_float(element_data.get('Глубина_выдавливания_мм', 0))
        if val > 0:
            return val
    elif ifc_type == 'IfcSlab':
        for key in ['Площадь_NetArea_м2', 'Площадь_GrossArea_м2']:
            val = safe_parse_float(element_data.get(key, 0))
            if val > 0:
                return round(val, 2)
    elif ifc_type in ('IfcColumn', 'IfcBeam', 'IfcStair', 'IfcStairFlight'):
        val = safe_parse_float(element_data.get('Длина_Length_мм', 0))
        if val > 0:
            return val
    return None


# Карта переименования геометрических параметров для additionalCharacteristics.
# Ключ — исходное имя колонки, значение — новое имя характеристики.
_GEOMETRY_RENAME_MAP = {
    'Длина_Width_мм': 'Толщина_мм',
    'Длина_Height_мм': 'Высота_мм',
    'Длина_Length_мм': 'Длина_мм',
    'Длина_Perimeter_мм': 'Периметр_мм',
    'Площадь_GrossSideArea_м2': 'Площадь_общая_м2',
    'Площадь_NetSideArea_м2': 'Площадь_чистая_м2',
    'Площадь_CrossSectionArea_м2': 'Площадь_поперечного_сечения_м2',
    'Площадь_GrossArea_м2': 'Площадь_общая_вся_м2',
    'Площадь_NetArea_м2': 'Площадь_чистая_вся_м2',
    'Площадь_OuterSurfaceArea_м2': 'Площадь_наружняя_м2',
    'Объём_NetVolume_м3': 'Объём_чистый_м3',
    'Объём_GrossVolume_литры': 'Объём_общий_литры',
}

# Параметры, которые не нужно добавлять в additionalCharacteristics.
_GEOMETRY_SKIP_KEYS = {
    'Глубина_выдавливания_мм',
    # Площадь_GROSS_м2 — дубликат, создаваемый в zero_step.py для приоритетного поиска
    'Площадь_GROSS_м2',
}


def _collect_all_geometry_params(element_data: dict) -> List[Dict[str, Any]]:
    """
    Собирает все нормализованные геометрические параметры элемента
    из его данных для добавления в additionalCharacteristics.

    Сканирует все ключи element_data и отбирает колонки с геометрическими
    параметрами (длины, ширины, высоты, глубины, толщины, периметры, площади,
    объёмы, веса). Значения уже нормализованы в zero_step.py / result_former.py
    (приведены к единым единицам измерения и округлены).

    Применяет карту переименований _GEOMETRY_RENAME_MAP для приведения
    названий к требуемому виду и пропусает параметры из _GEOMETRY_SKIP_KEYS.

    Возвращает список характеристик в формате {name, values}.
    """
    result = []

    # Паттерны геометрических параметров: (ключевое_слово, суффикс_единицы)
    geometry_patterns = [
        ('Длина', '_мм'),
        ('Ширина', '_мм'),
        ('Высота', '_мм'),
        ('Глубина', '_мм'),
        ('Толщина', '_мм'),
        ('Периметр', '_мм'),
        ('Площадь', '_м2'),
        ('Объём', '_м3'),
        ('Объём', '_литры'),
        ('Вес', '_кг'),
    ]

    seen_keys = set()

    for keyword, unit_suffix in geometry_patterns:
        for key, value in element_data.items():
            if keyword in key and key.endswith(unit_suffix) and key not in seen_keys:
                # Пропускаем дубликаты с префиксом QTO_ и Свойство_
                # (zero_step.py создаёт их одновременно со старым форматом без префикса)
                if key.startswith('QTO_') or key.startswith('Свойство_'):
                    continue
                # Пропускаем параметры из списка исключений
                if key in _GEOMETRY_SKIP_KEYS:
                    continue
                # Пропускаем пустые значения
                if value is None or value == '-' or value == '':
                    continue
                # Округляем числовые значения до 2 знаков
                val = value
                try:
                    num_val = float(val)
                    val = round(num_val, 2)
                except (ValueError, TypeError):
                    pass

                # Применяем переименование, если есть в карте
                display_name = _GEOMETRY_RENAME_MAP.get(key, key)

                result.append({
                    'name': display_name,
                    'values': [{'strValue': str(val)}],
                })
                seen_keys.add(key)

    return result


def _get_geometry_range_for_element(element_data: dict, ifc_type: str) -> Tuple[str, str]:
    """
    Определяет нормализованный геометрический диапазон для отдельного элемента.

    Использует GEOMETRY_GROUP_RULES для определения поля и диапазонов.
    Возвращает (имя_характеристики, нормализованный_диапазон) или ('', '').
    """
    rule = GEOMETRY_GROUP_RULES.get(ifc_type, GEOMETRY_GROUP_RULES['default'])
    geo_label = rule.get('label', 'Объём')
    field = rule.get('field', '')

    # Получаем значение геометрии
    value = 0.0
    if field and field in element_data:
        value = safe_parse_float(element_data[field])
    elif ifc_type == 'IfcWall':
        # Для стен — толщина из Длина_Width_мм, затем ширина сечения, глубина выдавливания — не толщина
        for key in ['Длина_Width_мм', 'Ширина_сечения_мм', 'Глубина_выдавливания_мм']:
            if key in element_data:
                val = safe_parse_float(element_data[key])
                if val > 0:
                    value = val
                    break
    elif ifc_type == 'IfcSlab':
        for key in ['Площадь_NetArea_м2', 'Площадь_GrossArea_м2']:
            if key in element_data:
                val = safe_parse_float(element_data[key])
                if val > 0:
                    value = val
                    break

    # Если значение не найдено — не добавляем геометрическую характеристику
    if value <= 0:
        return geo_label, ''

    # Ищем подходящий диапазон
    for rg in rule['ranges']:
        if value <= rg['max']:
            return geo_label, _extract_geometry_range(rg['label'])

    # Если не попали ни в один диапазон — берём последний
    return geo_label, _extract_geometry_range(rule['ranges'][-1]['label'])


def _get_location_from_storey_type(storey_type: str) -> str:
    """
    Определяет часть здания по типу этажа.

    'Подземный' → 'Подземная часть здания'
    'Цокольный' → 'Цокольная часть здания'
    остальное   → 'Надземная часть здания'
    """
    if not storey_type or storey_type == '-':
        return 'Надземная часть здания'
    st = str(storey_type).lower()
    if 'подзем' in st or 'подвал' in st:
        return 'Подземная часть здания'
    if 'цокол' in st:
        return 'Цокольная часть здания'
    return 'Надземная часть здания'


def build_elements_json_output(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Формирует массив объектов по каждому элементу IFC в целевом формате.

    Для каждого элемента создаётся объект с полями:
      - characteristics: нормализованные характеристики (Материал, Расположение, геометрия)
      - additionalCharacteristics: оригинальные значения (Имя, Тип бетона, геометрия, Этаж, Тип этажа)

    Вход:  DataFrame с данными элементов (полный набор колонок из get_element_info)
    Выход: список словарей в целевом формате
    """
    result = []

    for _, row in df.iterrows():
        element_data = row.to_dict()

        # Определяем IFC-тип
        ifc_type = element_data.get('Тип элемента', '')
        if not ifc_type or ifc_type == '-':
            ifc_type = get_ifc_type(
                element_data.get('Тип (RU)', ''),
                element_data.get('Имя', ''),
            )

        # ---- characteristics (нормализованные) ----
        characteristics = []

        # 1. Материал
        characteristics.append({
            'name': 'Материал',
            'values': [
                {'strValue': _normalize_material(str(element_data.get('Материал', '')))}
            ],
        })

        # 2. Расположение (по типу этажа)
        characteristics.append({
            'name': 'Расположение',
            'values': [
                {'strValue': _get_location_from_storey_type(
                    str(element_data.get('Тип_этажа', ''))
                )}
            ],
        })

        # 3. Геометрическая характеристика (нормализованный диапазон)
        geo_name, geo_range = _get_geometry_range_for_element(element_data, ifc_type)
        if geo_name and geo_range:
            characteristics.append({
                'name': geo_name,
                'values': [
                    {'strValue': geo_range}
                ],
            })

        # ---- additionalCharacteristics (оригинальные значения) ----
        additional = []

        # Имя элемента
        elem_name = element_data.get('Имя', '')
        if elem_name and elem_name != '-':
            additional.append({
                'name': 'Имя элемента',
                'values': [{'strValue': str(elem_name)}],
            })

        # Прочность бетона (марка)
        concrete_grade = element_data.get(
            'Свойство_ExpCheck_MaterialConcrete_MGE_ConcreteGrade', ''
        )
        if concrete_grade and concrete_grade != '-' and str(concrete_grade).strip():
            additional.append({
                'name': 'Прочность',
                'values': [{'strValue': str(concrete_grade)}],
            })

        # Морозостойкость
        freeze_durability = element_data.get(
            'Свойство_ExpCheck_MaterialConcrete_MGE_FreezeDurability', ''
        )
        if freeze_durability and freeze_durability != '-' and str(freeze_durability).strip():
            fd = str(freeze_durability)
            if not fd.startswith('F'):
                fd = f'F{fd}'
            additional.append({
                'name': 'Морозостойкость',
                'values': [{'strValue': fd}],
            })

        # Водонепроницаемость
        water_resist = element_data.get(
            'Свойство_ExpCheck_MaterialConcrete_MGE_WaterResist', ''
        )
        if water_resist and water_resist != '-' and str(water_resist).strip():
            wr = str(water_resist)
            if not wr.startswith('W'):
                wr = f'W{wr}'
            additional.append({
                'name': 'Водонепроницаемость',
                'values': [{'strValue': wr}],
            })

        # Все нормализованные геометрические параметры элемента
        # (длины, ширины, высоты, глубины, толщины, периметры, площади, объёмы, веса)
        geometry_params = _collect_all_geometry_params(element_data)
        additional.extend(geometry_params)

        # Этаж
        storey = element_data.get('Этаж', '')
        if storey and storey != '-':
            additional.append({
                'name': 'Этаж',
                'values': [{'strValue': str(storey)}],
            })

        # Тип этажа
        storey_type = element_data.get('Тип_этажа', '')
        if storey_type and storey_type != '-':
            additional.append({
                'name': 'Тип этажа',
                'values': [{'strValue': str(storey_type)}],
            })

        # ---- Собираем итоговый объект ----
        # buildingElementName — общее имя группы по IFC-типу с учётом гидроизоляции
        ru_type = element_data.get('Тип (RU)', '')
        group_name = _determine_building_element_name(ru_type, elem_name, ifc_type)

        obj = {
            'buildingElementName': _singularize_ru_name(group_name),
            'isActive': True,
            'characteristics': characteristics,
            'additionalCharacteristics': additional,
        }

        result.append(obj)

    return result


# =====================================================================
#  ЭТАП A: ИЗВЛЕЧЕНИЕ ВСЕХ ЭЛЕМЕНТОВ ИЗ IFC
# =====================================================================

def extract_elements_from_ifc(ifc_path: str, output_folder: str) -> str:
    """
    Извлекает все элементы из IFC-файла.
    Переиспользует функции из zero_step.py.

    Сохраняет Excel с листом 'Данные' — точно в том же формате,
    что и zero_step (ДЛЯ_СМЕТЧИКА_исправленный.xlsx):
    тот же набор колонок (smetchik_cols), те же служебные поля.
    Это гарантирует идентичную группировку с веб-интерфейсом.

    Вход:  путь к IFC-файлу
    Выход: путь к созданному Excel-файлу (ifc_raw_elements.xlsx с листом 'Данные')
           + файл ifc_raw_elements.json для отладки
    """
    logger.info(f"Извлечение элементов из IFC: {ifc_path}")

    if not os.path.exists(ifc_path):
        raise FileNotFoundError(f"IFC файл не найден: {ifc_path}")

    model = ifcopenshell.open(ifc_path)
    elements = []

    for ifc_type, ru_name in element_types:
        elems = model.by_type(ifc_type)
        logger.info(f"  {ifc_type} ({ru_name}): {len(elems)} шт")
        for elem in elems:
            elem_info = get_element_info(elem)
            elem_info['Тип (RU)'] = ru_name
            elements.append(elem_info)

    if not elements:
        logger.warning("Не найдено ни одного элемента в IFC-файле")
        return ''

    df = pd.DataFrame(elements)
    df = df.fillna('-')

    # --- Формируем набор колонок точно как в zero_step (smetchik_cols) ---
    smetchik_cols = [
        'Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал',
        'Этаж', 'Тип_этажа', 'Уровень_этажа_мм',
    ]

    # Геометрические параметры
    for col in df.columns:
        if 'Длина' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Ширина' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Высота' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Глубина' in col and '_мм' in col:
            smetchik_cols.append(col)

    # Объемы
    for col in df.columns:
        if 'Объём' in col and ('_м3' in col or '_литры' in col):
            smetchik_cols.append(col)

    # Площади
    for col in df.columns:
        if 'Площадь' in col and '_м2' in col:
            smetchik_cols.append(col)

    # Специфические свойства
    specific_col_names = [prop.replace('.', '_') for prop in SPECIFIC_PROPERTIES]
    for col in specific_col_names:
        if col in df.columns:
            smetchik_cols.append(col)

    # Оставляем только существующие колонки, убираем дубликаты
    existing_cols = []
    seen = set()
    for col in smetchik_cols:
        if col in df.columns and col not in seen:
            existing_cols.append(col)
            seen.add(col)

    df_smetchik = df[existing_cols].copy()
    df_smetchik = df_smetchik.fillna('-')

    # --- Добавляем агрегированные колонки геометрии, необходимые для GEOMETRY_GROUP_RULES ---
    # group_excel.py использует 'Ширина, мм', 'Площадь, м2', 'Объём, м3', 'Длина, мм', 'Периметр, мм'
    # Эти колонки ожидаются в GEOMETRY_GROUP_RULES для правильной группировки по толщине/площади и т.д.
    # Без них get_geometry_value() падает на get_volume() (объём в м³), что даёт неверные диапазоны.

    # Ширина, мм — для стен это толщина (Длина_Width_мм), для колонн — ширина сечения
    if 'Ширина, мм' not in df_smetchik.columns:
        width_col = None
        for candidate in ['Длина_Width_мм', 'Ширина_сечения_мм', 'Глубина_выдавливания_мм']:
            if candidate in df_smetchik.columns:
                width_col = candidate
                break
        if width_col:
            df_smetchik['Ширина, мм'] = df_smetchik[width_col].apply(
                lambda v: safe_parse_float(v) if v != '-' else 0
            )
        else:
            df_smetchik['Ширина, мм'] = 0

    # Площадь, м2 — для плит, балок и т.д.
    if 'Площадь, м2' not in df_smetchik.columns:
        area_col = None
        for candidate in ['Площадь_GrossArea_м2', 'Площадь_NetArea_м2', 'Площадь_GROSS_м2']:
            if candidate in df_smetchik.columns:
                area_col = candidate
                break
        if area_col:
            df_smetchik['Площадь, м2'] = df_smetchik[area_col].apply(
                lambda v: safe_parse_float(v) if v != '-' else 0
            )
        else:
            df_smetchik['Площадь, м2'] = 0

    # Объём, м3
    if 'Объём, м3' not in df_smetchik.columns:
        vol_col = None
        for candidate in ['Объём_NetVolume_м3', 'Объём_GrossVolume_м3']:
            if candidate in df_smetchik.columns:
                vol_col = candidate
                break
        if vol_col:
            df_smetchik['Объём, м3'] = df_smetchik[vol_col].apply(
                lambda v: safe_parse_float(v) if v != '-' else 0
            )
        else:
            # Пробуем из литров
            for candidate in ['Объём_GrossVolume_литры', 'Объём_NetVolume_литры']:
                if candidate in df_smetchik.columns:
                    df_smetchik['Объём, м3'] = df_smetchik[candidate].apply(
                        lambda v: safe_parse_float(v) / 1000 if v != '-' else 0
                    )
                    break
            else:
                df_smetchik['Объём, м3'] = 0

    # Длина, мм — для свай, балок
    if 'Длина, мм' not in df_smetchik.columns:
        length_col = None
        for candidate in ['Длина_Length_мм', 'Длина_мм', 'Длина_Height_мм']:
            if candidate in df_smetchik.columns:
                length_col = candidate
                break
        if length_col:
            df_smetchik['Длина, мм'] = df_smetchik[length_col].apply(
                lambda v: safe_parse_float(v) if v != '-' else 0
            )
        else:
            df_smetchik['Длина, мм'] = 0

    # Периметр, мм — для колонн
    if 'Периметр, мм' not in df_smetchik.columns:
        perim_col = None
        for candidate in ['Длина_Perimeter_мм', 'Периметр_мм']:
            if candidate in df_smetchik.columns:
                perim_col = candidate
                break
        if perim_col:
            df_smetchik['Периметр, мм'] = df_smetchik[perim_col].apply(
                lambda v: safe_parse_float(v) if v != '-' else 0
            )
        else:
            df_smetchik['Периметр, мм'] = 0

    # Высота, мм
    if 'Высота, мм' not in df_smetchik.columns:
        height_col = None
        for candidate in ['Длина_Height_мм', 'Высота_мм', 'Глубина_выдавливания_мм']:
            if candidate in df_smetchik.columns:
                height_col = candidate
                break
        if height_col:
            df_smetchik['Высота, мм'] = df_smetchik[height_col].apply(
                lambda v: safe_parse_float(v) if v != '-' else 0
            )
        else:
            df_smetchik['Высота, мм'] = 0

    # Добавляем служебные колонки (как в zero_step)
    df_smetchik.insert(0, '№ п/п', range(1, len(df_smetchik) + 1))
    df_smetchik['Примечание_сметчика'] = ''
    df_smetchik['Стоимость_за_ед_руб'] = ''
    df_smetchik['Общая_стоимость_руб'] = ''

    logger.info(
        f"Колонок в smetchik-формате: {len(df_smetchik.columns)} "
        f"(из {len(df.columns)} исходных)"
    )

    # --- Сохраняем JSON с массивом объектов по каждому элементу IFC ---
    # Формат: characteristics + additionalCharacteristics для каждого элемента.
    elements_json_path = os.path.join(output_folder, 'ifc_elements_output.json')
    elements_output = build_elements_json_output(df)
    with open(elements_json_path, 'w', encoding='utf-8') as f:
        json.dump(elements_output, f, ensure_ascii=False, indent=2, default=str)
    logger.info(
        f"Сохранён {elements_json_path} "
        f"({len(elements_output)} элементов в формате characteristics/additionalCharacteristics)"
    )

    # --- Сохраняем XLSX с листом 'Данные' (smetchik-формат, как в zero_step) ---
    # Этот файл нужен как промежуточный для process_ifc_excel на этапе B.
    xlsx_path = os.path.join(output_folder, 'ifc_raw_elements.xlsx')
    with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
        df_smetchik.to_excel(writer, sheet_name='Данные', index=False)
    logger.info(f"Сохранён {xlsx_path} (лист 'Данные', {len(df_smetchik.columns)} колонок)")

    return xlsx_path


# =====================================================================
#  ЭТАП B: ГРУППИРОВКА ЭЛЕМЕНТОВ
# =====================================================================

def group_elements_by_type(
    input_excel_path: str,
    output_folder: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, str]:
    """
    Группирует элементы через process_ifc_excel() — точно тот же путь,
    что использует веб-интерфейс при нажатии «Авто-группировка».

    Вход:  путь к Excel-файлу с листом 'Данные'
    Выход: (листовые_группы, полное_дерево_групп, путь_к_grouped_json, путь_к_grouped_xlsx)
    """
    if not input_excel_path or not os.path.exists(input_excel_path):
        logger.warning(f"Excel файл не найден: {input_excel_path}")
        return [], [], '', ''

    logger.info(f"Группировка через process_ifc_excel: {input_excel_path}")

    # Вызываем ту же функцию, что и веб-интерфейс
    group_result = process_ifc_excel(input_excel_path, output_folder)

    grouped_json_path = group_result['json']
    grouped_excel_path = group_result['excel']

    if not os.path.exists(grouped_json_path):
        logger.error(f"JSON группировки не создан: {grouped_json_path}")
        return [], [], '', ''

    # Читаем полное дерево групп из JSON
    with open(grouped_json_path, 'r', encoding='utf-8') as f:
        full_groups = json.load(f)

    if not full_groups:
        logger.warning("Группировка не дала результатов")
        return [], [], grouped_json_path, grouped_excel_path

    # Собираем только листовые группы (без детей) — как в _run_processing_pipeline
    leaf_groups = []

    def collect_leaves(group_list, path=None):
        if path is None:
            path = []
        for group in group_list:
            current_path = path + [group.get('name', '')]
            children = group.get('children', [])
            if children:
                collect_leaves(children, current_path)
            else:
                leaf_groups.append({
                    **group,
                    'path': current_path,
                })

    collect_leaves(full_groups)

    logger.info(f"Собрано {len(leaf_groups)} листовых групп")

    # ВАЖНО: process_ifc_excel уже создал ifc_raw_elements_grouped.json/.xlsx
    # в исходном формате дерева групп. На этапе C мы перезапишем JSON и XLSX
    # в формате справочника.

    return leaf_groups, full_groups, grouped_json_path, grouped_excel_path


# =====================================================================
#  ЭТАП C: ФОРМИРОВАНИЕ ВЫХОДНОГО ФОРМАТА
# =====================================================================

def build_reference_output(
    leaf_groups: List[Dict[str, Any]],
    full_groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Трансформирует листовые группы в целевой формат для API-справочника.

    Вход:  листовые группы
    Выход: массив объектов в формате ТЗ
    """
    if not leaf_groups:
        logger.warning("Нет групп для формирования выходного формата")
        return []

    result = []

    for group in leaf_groups:
        first = group.get('first_element', {})
        path = group.get('path', [])
        if not first:
            continue

        # ---- Определяем основные параметры группы ----

        # Часть здания (первый элемент пути — Подземная/Цоколь/Надземная)
        part = path[0] if path else 'Надземная'
        # Ищем ключ части здания среди известных
        part_key = 'Надземная'
        for known_part in ['Подземная', 'Цоколь', 'Надземная']:
            if known_part in part:
                part_key = known_part
                break

        # IFC-тип для определения геометрических характеристик.
        # Приоритет: поле 'Тип элемента' (IfcSlab, IfcWall, ...),
        # затем get_ifc_type() по имени.
        ifc_type = first.get('Тип элемента', '')
        if not ifc_type or ifc_type == '-':
            ifc_type = get_ifc_type(
                first.get('Тип (RU)', ''),
                first.get('Имя', ''),
            )

        # Русское название элемента (с учётом гидроизоляции)
        ru_name = _determine_building_element_name(
            first.get('Тип (RU)', 'Неизвестно'),
            first.get('Имя', ''),
            ifc_type,
        )

        # ---- Формируем totalMeasure ----
        total_volume = group.get('total_volume', 0)
        total_areas = group.get('total_areas', {})

        if total_volume and total_volume > 0:
            measure_type = 'volume'
            measure_value = round(total_volume, 2)
            measure_unit = 'м³'
        elif total_areas:
            measure_type = 'area'
            measure_value = round(list(total_areas.values())[0], 2)
            measure_unit = 'м²'
        else:
            measure_type = 'count'
            measure_value = group.get('count', 0)
            measure_unit = 'шт'

        # ---- Формируем characteristics (нормализованные) ----
        characteristics = []

        # 1. Материал
        characteristics.append({
            'name': 'Материал',
            'values': [
                {'strValue': _normalize_material(first.get('Материал', ''))}
            ],
        })

        # 2. Расположение
        characteristics.append({
            'name': 'Расположение',
            'values': [
                {'strValue': _get_location_name(part_key)}
            ],
        })

        # 3. Геометрическая характеристика (нормализованный диапазон)
        geo_label = _get_geometry_label(ifc_type)
        # Ищем геометрический диапазон по всему пути, а не только в имени листовой группы.
        # Листовая группа может называться 'Бетон: В35', а геометрия — в родителе 'Площадь: более 20 м²'.
        geo_name, geo_range = _extract_geometry_range_from_path(path, geo_label)
        if geo_name and geo_range:
            characteristics.append({
                'name': geo_name,
                'values': [
                    {'strValue': geo_range}
                ],
            })

        # ---- Формируем additionalCharacteristics (оригинальные значения) ----
        additional = []

        # Имя первого элемента в группе
        elem_name = first.get('Имя', '')
        if elem_name and elem_name != '-':
            additional.append({
                'name': 'Имя элемента',
                'values': [{'strValue': str(elem_name)}],
            })

        # Прочность бетона (марка)
        concrete_grade = first.get(
            'Свойство_ExpCheck_MaterialConcrete_MGE_ConcreteGrade', ''
        )
        if concrete_grade and concrete_grade != '-' and str(concrete_grade).strip():
            additional.append({
                'name': 'Прочность',
                'values': [{'strValue': str(concrete_grade)}],
            })

        # Морозостойкость
        freeze_durability = first.get(
            'Свойство_ExpCheck_MaterialConcrete_MGE_FreezeDurability', ''
        )
        if freeze_durability and freeze_durability != '-' and str(freeze_durability).strip():
            fd = str(freeze_durability)
            if not fd.startswith('F'):
                fd = f'F{fd}'
            additional.append({
                'name': 'Морозостойкость',
                'values': [{'strValue': fd}],
            })

        # Водонепроницаемость
        water_resist = first.get(
            'Свойство_ExpCheck_MaterialConcrete_MGE_WaterResist', ''
        )
        if water_resist and water_resist != '-' and str(water_resist).strip():
            wr = str(water_resist)
            if not wr.startswith('W'):
                wr = f'W{wr}'
            additional.append({
                'name': 'Водонепроницаемость',
                'values': [{'strValue': wr}],
            })

        # Оригинальное геометрическое значение
        orig_geo = _get_original_geometry(first, ifc_type)
        original_geo_name = geo_name or geo_label
        if orig_geo is not None and original_geo_name:
            additional.append({
                'name': original_geo_name,
                'values': [{'strValue': str(orig_geo)}],
            })

        # Этаж
        storey = first.get('Этаж', '')
        if storey and storey != '-':
            additional.append({
                'name': 'Этаж',
                'values': [{'strValue': str(storey)}],
            })

        # Тип этажа
        storey_type = first.get('Тип_этажа', '')
        if storey_type and storey_type != '-':
            additional.append({
                'name': 'Тип этажа',
                'values': [{'strValue': str(storey_type)}],
            })

        # ---- Собираем итоговый объект ----
        obj = {
            'buildingElementName': _singularize_ru_name(ru_name),
            'isActive': True,
            'elementCount': group.get('count', 0),
            'totalMeasure': {
                'type': measure_type,
                'value': measure_value,
                'unit': measure_unit,
            },
            'characteristics': characteristics,
            'additionalCharacteristics': additional,
        }

        result.append(obj)

    return result


# =====================================================================
#  ГЛАВНАЯ ТОЧКА ВХОДА (ОРКЕСТРАТОР)
# =====================================================================

def build_reference_from_ifc(ifc_path: str, output_folder: str) -> List[Dict[str, Any]]:
    """
    Главная функция: запускает полный пайплайн построения справочной структуры.

    Аргументы:
        ifc_path — путь к IFC-файлу
        output_folder — папка для сохранения результатов

    Возвращает:
        Массив объектов в формате ifc_reference_output.json
    """
    logger.info("=" * 60)
    logger.info("НАЧАТО ПОСТРОЕНИЕ СПРАВОЧНОЙ СТРУКТУРЫ ИЗ IFC")
    logger.info("=" * 60)

    # Создаём папку, если её нет
    os.makedirs(output_folder, exist_ok=True)

    # Этап A: Извлечение элементов
    logger.info("\n--- ЭТАП A: Извлечение элементов из IFC ---")
    excel_path = extract_elements_from_ifc(ifc_path, output_folder)
    if not excel_path:
        logger.error("Не удалось извлечь элементы из IFC")
        return []

    # Этап B: Группировка (через process_ifc_excel — тот же путь, что в веб-интерфейсе)
    logger.info("\n--- ЭТАП B: Группировка элементов ---")
    leaf_groups, full_groups, grouped_json_path, grouped_excel_path = group_elements_by_type(excel_path, output_folder)
    if not leaf_groups:
        logger.error("Не удалось сгруппировать элементы")
        return []

    # Этап C: Формирование выходного формата
    logger.info("\n--- ЭТАП C: Формирование выходного формата ---")
    result = build_reference_output(leaf_groups, full_groups)

    # --- Перезаписываем ifc_raw_elements_grouped.json и .xlsx новым форматом ---
    # process_ifc_excel() создал их в формате дерева групп.
    # Перезаписываем в формате справочника (как требует ТЗ).
    # Это безопасно: веб-интерфейс использует filtered_elements_grouped.json,
    # а не ifc_raw_elements_grouped.json.
    if grouped_json_path and result:
        # JSON
        with open(grouped_json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(
            f"Файл {os.path.basename(grouped_json_path)} перезаписан "
            f"в формате справочника ({len(result)} групп)"
        )

        # XLSX — плоская таблица в формате справочника
        xlsx_rows = []
        for item in result:
            row = {
                'buildingElementName': item['buildingElementName'],
                'isActive': item['isActive'],
                'elementCount': item['elementCount'],
                'totalMeasure_type': item['totalMeasure']['type'],
                'totalMeasure_value': item['totalMeasure']['value'],
                'totalMeasure_unit': item['totalMeasure']['unit'],
            }
            for ch in item['characteristics']:
                vals = ', '.join(v['strValue'] for v in ch['values'])
                row[f'char_{ch["name"]}'] = vals
            for ch in item['additionalCharacteristics']:
                vals = ', '.join(v['strValue'] for v in ch['values'])
                row[f'additional_{ch["name"]}'] = vals
            xlsx_rows.append(row)

        if xlsx_rows:
            df = _prepare_xlsx_df(xlsx_rows)
            df.to_excel(grouped_excel_path, index=False)
            logger.info(
                f"Файл {os.path.basename(grouped_excel_path)} перезаписан "
                f"в формате справочника ({len(result)} групп)"
            )

    # Удаляем промежуточный файл ifc_raw_elements.xlsx — он больше не нужен
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
        logger.info(f"Удалён промежуточный файл {os.path.basename(excel_path)}")

    logger.info("=" * 60)
    logger.info(f"ПОСТРОЕНИЕ СПРАВОЧНОЙ СТРУКТУРЫ ЗАВЕРШЕНО. "
                f"Сформировано {len(result)} групп.")
    logger.info("=" * 60)

    return result


# =====================================================================
#  ФУНКЦИЯ ДЛЯ PDF: ФОРМИРОВАНИЕ ifc_elements_output.json И ifc_raw_elements_grouped.json
# =====================================================================

def build_reference_from_pdf(df: pd.DataFrame, output_folder: str) -> List[Dict[str, Any]]:
    """
    Формирует ifc_elements_output.json и ifc_raw_elements_grouped.json
    из DataFrame, полученного при обработке PDF-чертежа.

    Аналог build_reference_from_ifc, но работает с готовым DataFrame
    вместо IFC-файла.

    Аргументы:
        df — DataFrame с данными элементов из PDF (формат form_result_df)
        output_folder — папка для сохранения результатов

    Возвращает:
        Массив объектов в формате ifc_reference_output.json (через build_reference_output)
    """
    logger.info("=" * 60)
    logger.info("ФОРМИРОВАНИЕ JSON-ФАЙЛОВ ИЗ PDF-ЧЕРТЕЖА")
    logger.info("=" * 60)

    os.makedirs(output_folder, exist_ok=True)

    # Заполняем пропуски
    df = df.fillna('-')

    # ---- Этап 1: Создаём ifc_elements_output.json ----
    logger.info("--- Этап 1: Формирование ifc_elements_output.json ---")
    elements_json_path = os.path.join(output_folder, 'ifc_elements_output.json')
    elements_output = build_elements_json_output(df)
    with open(elements_json_path, 'w', encoding='utf-8') as f:
        json.dump(elements_output, f, ensure_ascii=False, indent=2, default=str)
    logger.info(
        f"Сохранён {elements_json_path} "
        f"({len(elements_output)} элементов в формате characteristics/additionalCharacteristics)"
    )

    # ---- Этап 2: Формируем Excel с листом 'Данные' для группировки ----
    logger.info("--- Этап 2: Подготовка Excel для группировки ---")

    # Формируем набор колонок как в smetchik-формате (аналог extract_elements_from_ifc)
    smetchik_cols = [
        'Тип (RU)', 'Тип элемента', 'Имя', 'GlobalId', 'Материал',
    ]

    # Геометрические параметры
    for col in df.columns:
        if 'Длина' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Ширина' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Высота' in col and '_мм' in col:
            smetchik_cols.append(col)
        elif 'Глубина' in col and '_мм' in col:
            smetchik_cols.append(col)

    # Объёмы
    for col in df.columns:
        if 'Объём' in col and ('_м3' in col or '_литры' in col):
            smetchik_cols.append(col)

    # Площади
    for col in df.columns:
        if 'Площадь' in col and '_м2' in col:
            smetchik_cols.append(col)

    # Агрегированные колонки (единый формат с zero_step, через запятую)
    for col in df.columns:
        if col in ('Ширина, мм', 'Длина, мм', 'Высота, мм', 'Периметр, м', 'Площадь, м2', 'Объём, м3'):
            smetchik_cols.append(col)

    # Оставляем только существующие колонки, убираем дубликаты
    existing_cols = []
    seen = set()
    for col in smetchik_cols:
        if col in df.columns and col not in seen:
            existing_cols.append(col)
            seen.add(col)

    df_smetchik = df[existing_cols].copy()
    df_smetchik = df_smetchik.fillna('-')

    # Добавляем служебные колонки
    df_smetchik.insert(0, '№ п/п', range(1, len(df_smetchik) + 1))
    df_smetchik['Примечание_сметчика'] = ''
    df_smetchik['Стоимость_за_ед_руб'] = ''
    df_smetchik['Общая_стоимость_руб'] = ''

    # Сохраняем временный Excel для группировки
    xlsx_path = os.path.join(output_folder, 'ifc_raw_elements.xlsx')
    with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
        df_smetchik.to_excel(writer, sheet_name='Данные', index=False)
    logger.info(f"Сохранён {xlsx_path} (лист 'Данные', {len(df_smetchik.columns)} колонок)")

    # ---- Этап 3: Группировка через process_ifc_excel ----
    logger.info("--- Этап 3: Группировка элементов ---")
    leaf_groups, full_groups, grouped_json_path, grouped_excel_path = group_elements_by_type(
        xlsx_path, output_folder
    )
    if not leaf_groups:
        logger.warning("Группировка не дала результатов, удаляем временный файл")
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
        return []

    # ---- Этап 4: Трансформация в формат справочника ----
    logger.info("--- Этап 4: Формирование выходного формата ---")
    result = build_reference_output(leaf_groups, full_groups)

    # Перезаписываем ifc_raw_elements_grouped.json в формате справочника
    if grouped_json_path and result:
        with open(grouped_json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(
            f"Файл {os.path.basename(grouped_json_path)} перезаписан "
            f"в формате справочника ({len(result)} групп)"
        )

        # XLSX — плоская таблица в формате справочника
        xlsx_rows = []
        for item in result:
            row = {
                'buildingElementName': item['buildingElementName'],
                'isActive': item['isActive'],
                'elementCount': item['elementCount'],
                'totalMeasure_type': item['totalMeasure']['type'],
                'totalMeasure_value': item['totalMeasure']['value'],
                'totalMeasure_unit': item['totalMeasure']['unit'],
            }
            for ch in item['characteristics']:
                vals = ', '.join(v['strValue'] for v in ch['values'])
                row[f'char_{ch["name"]}'] = vals
            for ch in item['additionalCharacteristics']:
                vals = ', '.join(v['strValue'] for v in ch['values'])
                row[f'additional_{ch["name"]}'] = vals
            xlsx_rows.append(row)

        if xlsx_rows:
            df_out = _prepare_xlsx_df(xlsx_rows)
            df_out.to_excel(grouped_excel_path, index=False)
            logger.info(
                f"Файл {os.path.basename(grouped_excel_path)} перезаписан "
                f"в формате справочника ({len(result)} групп)"
            )

    # Удаляем временный файл
    if os.path.exists(xlsx_path):
        os.remove(xlsx_path)
        logger.info(f"Удалён временный файл {os.path.basename(xlsx_path)}")

    logger.info("=" * 60)
    logger.info(f"ФОРМИРОВАНИЕ JSON-ФАЙЛОВ ИЗ PDF ЗАВЕРШЕНО. "
                f"Сформировано {len(result)} групп.")
    logger.info("=" * 60)

    return result
