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
)

logger = setup_logger(__name__)


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


def _get_location_name(part: str) -> str:
    """Преобразует ключ части здания в полное название."""
    mapping = {
        'Подземная': 'Подземная часть здания',
        'Цоколь': 'Цокольная часть здания',
        'Надземная': 'Надземная часть здания',
    }
    return mapping.get(part, 'Надземная часть здания')


def _normalize_material(material_str: str) -> str:
    """Нормализует название материала."""
    if not material_str or material_str == '-' or material_str == '':
        return 'Не указан'
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
    return material_str.capitalize() if material_str else 'Не указан'


def _get_original_geometry(element_data: dict, ifc_type: str) -> Optional[float]:
    """Возвращает оригинальное числовое значение геометрии элемента."""
    if ifc_type == 'IfcWall':
        # Пробуем ширину сечения, затем глубину выдавливания
        for key in ['Ширина_сечения_мм', 'Глубина_выдавливания_мм']:
            if key in element_data:
                val = safe_parse_float(element_data[key])
                if val > 0:
                    return val
        # Пробуем Длина_Width_мм
        val = safe_parse_float(element_data.get('Длина_Width_мм', 0))
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

    # Добавляем служебные колонки (как в zero_step)
    df_smetchik.insert(0, '№ п/п', range(1, len(df_smetchik) + 1))
    df_smetchik['Примечание_сметчика'] = ''
    df_smetchik['Стоимость_за_ед_руб'] = ''
    df_smetchik['Общая_стоимость_руб'] = ''

    logger.info(
        f"Колонок в smetchik-формате: {len(df_smetchik.columns)} "
        f"(из {len(df.columns)} исходных)"
    )

    # --- Сохраняем только XLSX с листом 'Данные' (smetchik-формат, как в zero_step) ---
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

        # Русское название элемента
        ru_name = first.get('Тип (RU)', 'Неизвестно')

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
        if geo_range:
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

        # Тип бетона (марка)
        concrete_grade = first.get(
            'ExpCheck_MaterialConcrete_MGE_ConcreteGrade', ''
        )
        if concrete_grade and concrete_grade != '-' and str(concrete_grade).strip():
            additional.append({
                'name': 'Тип бетона',
                'values': [{'strValue': str(concrete_grade)}],
            })

        # Водонепроницаемость
        water_resist = first.get(
            'ExpCheck_MaterialConcrete_MGE_WaterResist', ''
        )
        if water_resist and water_resist != '-' and str(water_resist).strip():
            wr = str(water_resist)
            if not wr.startswith('W'):
                wr = f'W{wr}'
            additional.append({
                'name': 'Водонепроницаемость',
                'values': [{'strValue': wr}],
            })

        # Морозостойкость
        freeze_durability = first.get(
            'ExpCheck_MaterialConcrete_MGE_FreezeDurability', ''
        )
        if freeze_durability and freeze_durability != '-' and str(freeze_durability).strip():
            fd = str(freeze_durability)
            if not fd.startswith('F'):
                fd = f'F{fd}'
            additional.append({
                'name': 'Морозостойкость',
                'values': [{'strValue': fd}],
            })

        # Оригинальное геометрическое значение
        orig_geo = _get_original_geometry(first, ifc_type)
        if orig_geo is not None:
            additional.append({
                'name': geo_name or geo_label,
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
            'buildingElementName': ru_name,
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
            df = pd.DataFrame(xlsx_rows).fillna('')
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