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
    ELEMENT_TYPES_KR,
    ELEMENT_TYPES_AR,
    SPECIFIC_PROPERTIES,
    get_element_info,
)

from src.services.group_excel import (
    GEOMETRY_GROUP_RULES,
    get_ifc_type,
    safe_parse_float,
    process_ifc_excel,
    process_ifc_excel_ar,
    is_hydro_vertical,
    is_hydro_horizontal,
    _find_volume_columns,
    _get_part_from_storey_name,
    _find_formwork_columns,
    _element_formwork_area_m2,
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


# Специальные типы элементов цифрового сборника (ЦС): позиции ЦС, которым
# соответствуют свои названия (buildingElementName) и наборы характеристик,
# отличные от стандартных («Перекрытие», «Стена», «Колонна» и т.д.).
#
# Ключ — итоговое buildingElementName (совпадает с name позиции ЦС),
# значения:
#   * send_location — отправлять ли характеристику «Расположение»
#     (у позиции ЦС «Фундаментная плита» её нет — при отправке API
#     вернёт 0 позиций; у лестничных маршей/площадок — есть);
#   * send_geometry — отправлять ли геометрическую характеристику
#     (диапазон «Площадь: более 20» и т.п.; у всех специальных позиций
#     геометрических характеристик нет).
_CS_SPECIAL_TYPES = {
    # ЭЛ 10 10 30 04 — только «Материал»
    'Фундаментная плита': {'send_location': False, 'send_geometry': False},
    # Лестничные марши/площадки — «Материал» + «Расположение»
    'Лестничный марш': {'send_location': True, 'send_geometry': False},
    'Лестничная площадка': {'send_location': True, 'send_geometry': False},
}


def _detect_cs_special_type(
    name: str,
    ifc_type: str,
    element_data: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Определяет специальный тип позиции ЦС по IFC-классу и имени элемента.

    Возвращает итоговое buildingElementName (ключ _CS_SPECIAL_TYPES)
    или None, если элемент не относится к специальным типам.

    Правила определения:
      * «Фундаментная плита» — IfcSlab с PredefinedType=BASESLAB либо
        «фундамент…» в имени Revit («Фундаментная плита…»,
        «Фундамент несущей конструкции…»);
      * «Лестничная площадка» — IfcSlab с PredefinedType=LANDING либо
        «лестниц…» в имени (в Revit площадки лестниц — IfcSlab);
      * «Лестничный марш» — IfcStairFlight (в Revit марши лестниц).

    Гидроизоляция («Гидроизоляция_фундамент…» и т.п.) отсекается — она
    определяется правилами is_hydro_* и проверяется раньше.
    """
    name_lower = str(name or '').lower()
    if 'гидроизол' in name_lower:
        return None

    ifc = str(ifc_type or '')
    predefined = str(
        (element_data or {}).get('PredefinedType', '') or ''
    ).strip().upper()

    if ifc == 'IfcSlab':
        if predefined == 'BASESLAB' or 'фундамент' in name_lower:
            return 'Фундаментная плита'
        if predefined == 'LANDING' or 'лестниц' in name_lower:
            return 'Лестничная площадка'
    elif ifc in ('IfcStairFlight', 'IfcStair'):
        # Марши лестниц в Revit — IfcStairFlight; IfcStair (сборная
        # лестница) тоже относится к маршам
        return 'Лестничный марш'
    return None


def _determine_building_element_name(
    ru_type: str,
    name: str,
    ifc_type: str,
    element_data: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Определяет итоговое buildingElementName с учётом гидроизоляции
    и специальных типов позиций ЦС.

    Правила (по приоритету):
      * вертикальная/горизонтальная гидроизоляция — соответствующее название;
      * специальный тип ЦС (фундаментная плита, лестничный марш,
        лестничная площадка) — название позиции ЦС (см. _CS_SPECIAL_TYPES);
      * иначе — ru_type (или name, если ru_type пуст).
    """
    if is_hydro_vertical(ru_type, name, ifc_type):
        return 'Вертикальная гидроизоляция'
    if is_hydro_horizontal(ru_type, name, ifc_type):
        return 'Горизонтальная гидроизоляция'
    # Специальные типы позиций ЦС (фундаментные плиты, лестницы)
    cs_special = _detect_cs_special_type(name, ifc_type, element_data)
    if cs_special:
        return cs_special
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


def _is_precast_element(element_data: dict, path=None) -> bool:
    """Определяет, является ли элемент СБОРНЫМ железобетоном.

    В цифровом сборнике (ЦС) содержатся только монолитные конструкции,
    поэтому для сборных элементов работы из ЦС не подбираются. Признак
    определяется по «сырым» данным элемента/группы:

      1. Материал элемента (колонка «Материал», в режиме КР заполняется
         из ExpCheck_*::MGE_Material): «Железобетон сборный» и т.п.;
      2. Свойство ConstructionMethod (Pset_ConcreteElementGeneral):
         значение Precast;
      3. Путь группировки: уровень материала из справочника
         materials_mssk_nested.json («Железобетон сборный (СТ 00 15 01)»).

    Нормализованное имя материала групп НЕ меняется (в характеристике
    «Материал» остаётся «Железобетон» — существующие группы не ломаются),
    признак сборности передаётся отдельным служебным полем ``_isPrecast``.

    Аргументы:
        element_data — данные первого элемента группы (сырой ряд таблицы);
        path         — путь группировки листовой группы (или None).

    Возвращает:
        True, если элемент сборный.
    """
    # 1. Сырой материал элемента
    material = str(element_data.get('Материал', '') or '').lower()
    if 'сборн' in material:
        return True

    # 2. ConstructionMethod = Precast (Pset_ConcreteElementGeneral)
    for key, value in element_data.items():
        if 'constructionmethod' in str(key).lower():
            if 'precast' in str(value or '').lower():
                return True

    # 3. Уровень материала в пути группировки
    for seg in path or []:
        if 'сборн' in str(seg or '').lower():
            return True

    return False


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


def _geometry_from_name(name: Any, ifc_type: str) -> float:
    """Геометрический параметр, извлечённый из имени элемента.

    Используется как последний fallback для `_get_geometry_range_for_element`:
    у части элементов модели нет QTO-количеств (например, у стены
    «Стена_180мм_ЖБ_B35_W4_F75:1433653» пусты QTO Width и все площади) —
    толщина в них известна только из имени. Применяется та же логика
    извлечения, что и в zero_step.parse_name (используется в
    fill_missing_from_name при заполнении таблиц для сметчика), поэтому
    характеристики в ifc_elements_output.json совпадают с геометрической
    группировкой превью.

    Fallback только для стен: у перекрытий/колонн/балок имя может
    содержать посторонние числа, а правило группировки использует другой
    параметр (площадь/периметр).

    Аргументы:
        name     — имя элемента («Базовая стена:Стена_180мм_ЖБ_B35_W4_F75:123»).
        ifc_type — тип элемента (IfcWall/IfcWallStandardCase).

    Возвращает:
        Числовое значение толщины (мм) или 0.0, если извлечь не удалось.
    """
    name_str = str(name or '').strip()
    if not name_str or name_str == '-':
        return 0.0
    if ifc_type not in ('IfcWall', 'IfcWallStandardCase'):
        return 0.0
    try:
        from src.services.zero_step import parse_name
        extracted = parse_name(name_str, ifc_type)
    except Exception:
        return 0.0
    if not extracted:
        return 0.0
    for key in ('Ширина, мм', 'Периметр, мм'):
        val = safe_parse_float(extracted.get(key, 0))
        if val > 0:
            return val
    return 0.0


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

    # Fallback по «сырым» колонкам, если поле правила отсутствует или пустое
    if value <= 0:
        if ifc_type == 'IfcWall':
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

    # Геометрии нет и в QTO-колонках — пробуем извлечь толщину из имени
    # элемента (у части стен модели нет QTO Width, но толщина есть в имени:
    # «Стена_180мм_ЖБ_B35_W4_F75» → 180 мм)
    if value <= 0:
        value = _geometry_from_name(element_data.get('Имя', ''), ifc_type)

    # Если значение не найдено — не добавляем геометрическую характеристику
    if value <= 0:
        return geo_label, ''

    # Ищем подходящий диапазон
    for rg in rule['ranges']:
        if value <= rg['max']:
            return geo_label, _extract_geometry_range(rg['label'])

    # Если не попали ни в один диапазон — берём последний
    return geo_label, _extract_geometry_range(rule['ranges'][-1]['label'])


def _get_location_from_storey_type(storey_name: str) -> str:
    """
    Определяет часть здания по значению параметра «Этаж».

    Правила (по числовому индикатору):
      '-1/1_Подземный этаж'            → 'Цокольная часть здания'
      '-1_Подземный этаж_основной'     → 'Подземная часть здания'
      '1_Этаж_основной'                → 'Надземная часть здания'
      'К01_1_этаж_основной'            → 'Надземная часть здания'
      'К01_-1_подземный этаж_основной' → 'Подземная часть здания'
      'К01_Крыша'                      → 'Надземная часть здания'
      'С01_1_этаж_основной'            → 'Надземная часть здания'
      'С01_-1_подвал_основной'         → 'Подземная часть здания'

    Если «Этаж» пуст или не распознан — 'Надземная часть здания'.
    """
    from src.services.group_excel import _get_part_from_storey_name
    part = _get_part_from_storey_name(storey_name)
    return _get_location_name(part)


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

        # Специальный тип позиции ЦС (фундаментная плита, лестничный марш,
        # лестничная площадка) — у таких позиций свой набор характеристик
        # (см. _CS_SPECIAL_TYPES): «Расположение» и геометрию отправляем
        # только если они есть у позиции ЦС, иначе подбор вернёт 0 позиций.
        cs_special = _detect_cs_special_type(
            element_data.get('Имя', ''), ifc_type, element_data
        )
        cs_flags = _CS_SPECIAL_TYPES.get(cs_special, {}) if cs_special else {}
        send_location = cs_flags.get('send_location', True)
        send_geometry = cs_flags.get('send_geometry', True)

        # 1. Материал
        characteristics.append({
            'name': 'Материал',
            'values': [
                {'strValue': _normalize_material(str(element_data.get('Материал', '')))}
            ],
        })

        # 2. Расположение (по параметру «Этаж»)
        if send_location:
            characteristics.append({
                'name': 'Расположение',
                'values': [
                    {'strValue': _get_location_from_storey_type(
                        str(element_data.get('Этаж', ''))
                    )}
                ],
            })

        # 3. Геометрическая характеристика (нормализованный диапазон)
        geo_name, geo_range = _get_geometry_range_for_element(element_data, ifc_type)
        if geo_name and geo_range and send_geometry:
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
        # buildingElementName — общее имя группы по IFC-типу с учётом
        # гидроизоляции и фундаментных плит
        ru_type = element_data.get('Тип (RU)', '')
        group_name = _determine_building_element_name(
            ru_type, elem_name, ifc_type, element_data
        )

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

def extract_elements_from_ifc(ifc_path: str, output_folder: str, processing_type: str = "KR") -> str:
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

    processing_type: тип обработки — "KR" (конструктив, по умолчанию)
        или "AR" (архитектура). Определяет набор извлекаемых IFC-классов:
        ELEMENT_TYPES_KR / ELEMENT_TYPES_AR.
    """
    logger.info(f"Извлечение элементов из IFC: {ifc_path} (тип: {processing_type})")

    if not os.path.exists(ifc_path):
        raise FileNotFoundError(f"IFC файл не найден: {ifc_path}")

    processing_type = str(processing_type).upper()
    selected_types = ELEMENT_TYPES_AR if processing_type == "AR" else ELEMENT_TYPES_KR

    model = ifcopenshell.open(ifc_path)
    elements = []

    for ifc_type, ru_name in selected_types:
        elems = model.by_type(ifc_type)
        logger.info(f"  {ifc_type} ({ru_name}): {len(elems)} шт")
        for elem in elems:
            elem_info = get_element_info(elem, processing_type=processing_type)
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

    # Колонка "Код мсск" — извлекается из параметров, содержащих "ElementCode"
    # (например: Свойство_ExpCheck_Wall_MGE_ElementCode,
    #  Свойство_RusSet_Common_RUS_MSSK_Element_Code).
    # Нужна для группировки элементов в режиме АР.
    element_code_cols = [col for col in df.columns
                         if 'elementcode' in col.lower().replace('_', '')]

    def _get_element_code(row):
        for col in element_code_cols:
            val = row[col]
            if val is not None and str(val).strip() and str(val) != '-':
                return str(val)
        return '-'

    if element_code_cols:
        df_smetchik.insert(1, 'Код мсск', df.apply(_get_element_code, axis=1).values)

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
    processing_type: str = "KR",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, str]:
    """
    Группирует элементы через process_ifc_excel()/process_ifc_excel_ar() —
    точно тот же путь, что использует веб-интерфейс при нажатии «Авто-группировка».

    Для АР используется process_ifc_excel_ar (другой набор правил группировки),
    для КР — process_ifc_excel.

    Вход:  путь к Excel-файлу с листом 'Данные'
    Выход: (листовые_группы, полное_дерево_групп, путь_к_grouped_json, путь_к_grouped_xlsx)
    """
    if not input_excel_path or not os.path.exists(input_excel_path):
        logger.warning(f"Excel файл не найден: {input_excel_path}")
        return [], [], '', ''

    processing_type = processing_type.upper()
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"

    # Вызываем ту же функцию, что и веб-интерфейс (разная для КР и АР)
    if processing_type == "AR":
        logger.info(f"Группировка (АР) через process_ifc_excel_ar: {input_excel_path}")
        group_result = process_ifc_excel_ar(input_excel_path, output_folder)
    else:
        logger.info(f"Группировка (КР) через process_ifc_excel: {input_excel_path}")
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
#  РАЗДЕЛЕНИЕ ГРУПП ПО ЧАСТЯМ ЗДАНИЯ (ПЕРЕД ЗАПРОСОМ К API)
# =====================================================================

# Метки частей здания для path[0] подгрупп, полученных разделением
# смешанной группы по частям здания (см. split_leaf_groups_by_part).
# В путь подставляется метка нужной части здания — по ней
# build_reference_output определяет характеристику «Расположение».
_PART_PATH_LABELS = {
    'Подземная': 'Подземная часть здания (до отм. 0,000)',
    'Цоколь': 'Цокольная часть здания (отм. 0,000)',
    'Надземная': 'Надземная часть здания (выше отм. 0,000)',
}


def _group_base_part(group: Dict[str, Any]) -> str:
    """Часть здания листовой группы (для split_leaf_groups_by_part).

    Приоритет — первый элемент пути группировки (Часть здания). Если части
    здания в пути нет (АР-группировка по кодам МССК) — определяем по
    «Этажу» первого элемента группы.
    """
    path = group.get('path') or []
    part = str(path[0]) if path else ''
    for known_part in ('Подземная', 'Цоколь', 'Надземная'):
        if known_part in part:
            return known_part
    # АР-режим: части здания нет в пути — по «Этажу» первого элемента
    first = group.get('first_element') or {}
    location = _get_location_from_storey_type(str(first.get('Этаж', '')))
    for known_part in ('Подземная', 'Цоколь', 'Надземная'):
        if location.startswith(known_part):
            return known_part
    return 'Надземная'


def _element_part(row: Dict[str, Any], base_part: str) -> str:
    """Часть здания отдельного элемента группы — строго по «Этажу».

    Числовой индикатор значения «Этаж» (разбивка по «_» и пробелам,
    текстовые префиксы К01_/С01_ игнорируются):
      '-1/1' / '-1/1_подземный этаж' → Цоколь   (индикатор вида «-N/M»)
      '-1_подземный этаж_основной'   → Подземная (индикатор — «-N»)
      '1_этаж_основной' и другие положительные → Надземная
      'Крыша' / '26_технический чердак'        → Надземная

    Переквалификация по «Типу этажа» не выполняется: отметка 1-го этажа
    (0,000) ошибочно размечала его как цокольный, и элементы 1-го этажа
    попадали в цокольную часть здания.

    Если числовой индикатор не распознан (Этаж пуст, «-», «Крыша» и т.п.),
    элемент остаётся в базовой части группы (path[0] дерева группировки).
    """
    from src.services.group_excel import _get_part_from_storey_name
    storey = str(row.get('Этаж') or '')
    part = _get_part_from_storey_name(storey)
    # Распознан отрицательный индикатор («-N/M» / «-N») — строго по нему
    if part in ('Подземная', 'Цоколь'):
        return part
    # Положительный индикатор («N») — надземная часть
    for segment in re.split(r'[_\s]+', storey.strip()):
        if re.match(r'^\d+$', segment.strip()):
            return 'Надземная'
    # Индикатор не распознан (Этаж пуст, «-», «Крыша» и т.п.) —
    # базовая часть группы (path[0] дерева группировки)
    return base_part


def split_leaf_groups_by_part(
    leaf_groups: List[Dict[str, Any]],
    elements_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Разделяет листовые группы по частям здания перед запросом к API ТСН.

    В группе могут быть элементы разных частей здания — например, стены
    одного типоразмера на 1-м цокольном этаже и на надземных этажах.
    Параметры в запросе к API берутся по первому элементу группы, поэтому
    вся группа запрашивалась как одна часть здания: расценки остальных
    частей не попадали в финальный перечень, а объём группы целиком
    относился к части первого элемента.

    Смешанная группа делится на подгруппы по частям здания
    (надземная / подземная / цокольная). Каждая подгруппа отправляется
    отдельным запросом со СВОИМИ объёмом, площадями и расходом арматуры;
    сумма показателей подгрупп равна показателям исходной группы.

    Аргументы:
        leaf_groups   — листовые группы дерева группировки (ключ indices
            ссылается на позиции строк elements_rows).
        elements_rows — строки элементов (записи DataFrame отфильтрованных
            элементов, по которым выполнялась группировка).

    Возвращает:
        Новый список листовых групп: несмешанные группы — без изменений,
        смешанные — разбитые на подгруппы по частям здания.
    """
    if not leaf_groups or not elements_rows:
        return list(leaf_groups)

    headers = list(elements_rows[0].keys())
    volume_cols = _find_volume_columns(headers)

    def _row_volume(row: Dict[str, Any]) -> float:
        # Первое ненулевое значение среди колонок-источников объёма
        # (coalesce — та же логика, что в _create_group/group_elements)
        for col in volume_cols:
            val = safe_parse_float(row.get(col, 0))
            if val > 0:
                return val
        return 0.0

    def _recompute_aggregates(sub_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Агрегаты подгруппы: объём, площади, расход арматуры (суммы)."""
        volume = float(round(sum(_row_volume(r) for r in sub_rows), 2))
        areas: Dict[str, float] = {}
        for col in headers:
            if 'площадь' not in str(col).lower():
                continue
            total = sum(safe_parse_float(r.get(col, 0)) for r in sub_rows)
            if total > 0:
                areas[col] = float(round(total, 2))
        reinforcement = float(round(sum(
            safe_parse_float(r.get('ReinforcementVolumeRatio', 0)) * _row_volume(r)
            for r in sub_rows
        ), 2))
        # Площадь опалубки плит подгруппы (фундаментные плиты — периметр ×
        # толщина; перекрытия — периметр × толщина + площадь плиты) —
        # пересчитывается по элементам своей части здания, чтобы сумма
        # площадей опалубки подгрупп равнялась площади исходной группы.
        perim_cols, depth_cols = _find_formwork_columns(headers)
        formwork_area = 0.0
        if perim_cols and depth_cols:
            formwork_area = float(round(sum(
                _element_formwork_area_m2(r, perim_cols, depth_cols)
                for r in sub_rows
            ), 2))
        return {
            'total_volume': volume,
            'total_areas': areas,
            'total_reinforcement': reinforcement,
            'formwork_area': formwork_area,
        }

    result: List[Dict[str, Any]] = []

    for group in leaf_groups:
        # Раскладываем элементы группы по частям здания
        rows_by_part: Dict[str, List[int]] = {}
        parts_order: List[str] = []
        base_part = _group_base_part(group)
        for idx in group.get('indices') or []:
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(elements_rows):
                continue
            part = _element_part(elements_rows[idx], base_part)
            if part not in rows_by_part:
                rows_by_part[part] = []
                parts_order.append(part)
            rows_by_part[part].append(idx)

        # Несмешанная группа (или элементы не найдены) — без изменений
        if len(rows_by_part) <= 1:
            result.append(group)
            continue

        path = list(group.get('path') or [])
        logger.info(
            f"Разделение группы по частям здания: "
            f"{path[-1] if path else group.get('name', '?')} "
            f"({group.get('count', len(group.get('indices') or []))} эл.) → "
            + ", ".join(f"{p}: {len(rows_by_part[p])} эл." for p in parts_order)
        )

        for part in parts_order:
            sub_indices = sorted(rows_by_part[part])
            sub_rows = [elements_rows[i] for i in sub_indices]
            aggregates = _recompute_aggregates(sub_rows)

            sub_group = dict(group)
            sub_group['indices'] = sub_indices
            sub_group['count'] = len(sub_indices)
            # Первый элемент подгруппы — элемент СВОЕЙ части здания
            sub_group['first_element'] = dict(elements_rows[sub_indices[0]])
            sub_group.update(aggregates)

            # В path[0] подставляем метку части подгруппы — по ней
            # build_reference_output определит «Расположение»
            sub_path = list(path)
            if sub_path:
                sub_path[0] = _PART_PATH_LABELS[part]
            else:
                sub_path = [_PART_PATH_LABELS[part]]
            sub_group['path'] = sub_path

            result.append(sub_group)

    return result


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

        # Суммарный расход арматуры (ReinforcementVolumeRatio, кг) по всем
        # элементам группы. Берётся из агрегированного поля total_reinforcement
        # (сумма по группе), а не из first_element. Используется в финальном
        # перечне работ для расценок по установке арматуры (перевод кг → т).
        reinforcement_volume_ratio = safe_parse_float(
            group.get('total_reinforcement', 0)
        )

        # ---- Определяем основные параметры группы ----

        # Часть здания (первый элемент пути — Подземная/Цоколь/Надземная)
        part = path[0] if path else 'Надземная'
        # Ищем ключ части здания среди известных
        part_key = None
        for known_part in ['Подземная', 'Цоколь', 'Надземная']:
            if known_part in part:
                part_key = known_part
                break
        # В АР-режиме часть здания не является уровнем группировки
        # (группировка сразу по коду МССК), поэтому при отсутствии части
        # в пути определяем «Расположение» по типу этажа первого элемента.
        if part_key is None:
            location = _get_location_from_storey_type(str(first.get('Этаж', '')))
            for known_part in ['Подземная', 'Цоколь', 'Надземная']:
                if location.startswith(known_part):
                    part_key = known_part
                    break
            part_key = part_key or 'Надземная'

        # IFC-тип для определения геометрических характеристик.
        # Приоритет: поле 'Тип элемента' (IfcSlab, IfcWall, ...),
        # затем get_ifc_type() по имени.
        ifc_type = first.get('Тип элемента', '')
        if not ifc_type or ifc_type == '-':
            ifc_type = get_ifc_type(
                first.get('Тип (RU)', ''),
                first.get('Имя', ''),
            )

        # Русское название элемента (с учётом гидроизоляции и специальных
        # типов ЦС: фундаментная плита, лестничный марш/площадка)
        elem_name = first.get('Имя', '')
        ru_name = _determine_building_element_name(
            first.get('Тип (RU)', 'Неизвестно'),
            elem_name,
            ifc_type,
            first,
        )

        # Специальный тип позиции ЦС — у таких позиций свой набор
        # характеристик (см. _CS_SPECIAL_TYPES): «Расположение» и геометрию
        # отправляем только если они есть у позиции ЦС. API ТСН требует
        # полного совпадения характеристик — при отправке лишней подбор
        # вернёт 0 позиций.
        cs_special = _detect_cs_special_type(elem_name, ifc_type, first)
        cs_flags = _CS_SPECIAL_TYPES.get(cs_special, {}) if cs_special else {}
        send_location = cs_flags.get('send_location', True)
        send_geometry = cs_flags.get('send_geometry', True)

        # ---- Формируем totalMeasure ----
        # Значения total_volume/total_areas приходят из JSON-дерева групп
        # (json.dump(default=str)) и могут быть строками вместо чисел —
        # numpy-скаляры np.int64 сериализуются в строки. Парсим их как числа.
        total_volume = safe_parse_float(group.get('total_volume', 0))
        total_areas = group.get('total_areas', {})

        if total_volume and total_volume > 0:
            measure_type = 'volume'
            measure_value = round(total_volume, 2)
            measure_unit = 'м³'
        elif total_areas:
            measure_type = 'area'
            measure_value = round(
                safe_parse_float(next(iter(total_areas.values()))), 2
            )
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

        # 2. Расположение — отправляем, если оно есть у позиции ЦС
        if send_location:
            characteristics.append({
                'name': 'Расположение',
                'values': [
                    {'strValue': _get_location_name(part_key)}
                ],
            })

        # 3. Геометрическая характеристика (нормализованный диапазон) —
        #    отправляем, если она есть у позиции ЦС
        geo_label = _get_geometry_label(ifc_type)
        # Ищем геометрический диапазон по всему пути, а не только в имени листовой группы.
        # Листовая группа может называться 'Бетон: В35', а геометрия — в родителе 'Площадь: более 20 м²'.
        geo_name, geo_range = _extract_geometry_range_from_path(path, geo_label)
        if geo_name and geo_range and send_geometry:
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
        # totalMeasure содержит только ОДИН измеритель (объём ИЛИ площадь),
        # поэтому суммарные площади групп сохраняются дополнительно в totalAreas.
        # Они нужны для расчёта объёмов работ в единицах площади
        # (монтаж/демонтаж опалубки и т.п.) в финальном перечне работ.
        total_areas_clean = {}
        if isinstance(total_areas, dict):
            for area_key, area_val in total_areas.items():
                area_num = safe_parse_float(area_val)
                if area_num and area_num > 0:
                    total_areas_clean[str(area_key)] = round(area_num, 2)

        obj = {
            'buildingElementName': _singularize_ru_name(ru_name),
            'isActive': True,
            'elementCount': group.get('count', 0),
            'totalMeasure': {
                'type': measure_type,
                'value': measure_value,
                'unit': measure_unit,
            },
            'totalAreas': total_areas_clean,
            'characteristics': characteristics,
            'additionalCharacteristics': additional,
        # Внутреннее служебное поле: расход арматуры на куб бетона
        # (ReinforcementVolumeRatio из IFC). Не отправляется в API,
        # используется только при формировании финального перечня работ.
        '_reinforcementVolumeRatio': reinforcement_volume_ratio,
        # Внутреннее служебное поле: площадь опалубки плит группы (м²,
        # сумма по элементам). Не отправляется в API — используется при
        # формировании финального перечня работ для расценок монтажа/
        # демонтажа опалубки: фундаментные плиты опалубливаются по боковым
        # граням (периметр × толщина), перекрытия — по боковым граням
        # и нижней поверхности (периметр × толщина + площадь плиты).
        '_formworkArea': safe_parse_float(group.get('formwork_area', 0)),
        # Внутреннее служебное поле: признак сборного железобетона (см.
        # _is_precast_element). В ЦС только монолитные конструкции, поэтому
        # для сборных групп работы из ЦС не подбираются (api_works_lookup).
        # Не отправляется в API (префикс '_').
        '_isPrecast': _is_precast_element(first, path),
        # Внутреннее служебное поле: часть здания группы (Подземная /
        # Цоколь / Надземная). Не отправляется в API (префикс '_'),
        # используется при формировании финального перечня работ —
        # таблица разбивается на части здания с итогами по каждой части.
        '_buildingPart': part_key,
    }

        result.append(obj)

    return result


# =====================================================================
#  ГЛАВНАЯ ТОЧКА ВХОДА (ОРКЕСТРАТОР)
# =====================================================================

def build_reference_from_ifc(ifc_path: str, output_folder: str, processing_type: str = "KR") -> List[Dict[str, Any]]:
    """
    Главная функция: запускает полный пайплайн построения справочной структуры.

    Аргументы:
        ifc_path — путь к IFC-файлу
        output_folder — папка для сохранения результатов
        processing_type — тип обработки: "KR" (конструктив) или "AR" (архитектура).
            Влияет на выбор функции группировки (process_ifc_excel / process_ifc_excel_ar).

    Возвращает:
        Массив объектов в формате ifc_reference_output.json
    """
    logger.info("=" * 60)
    logger.info("НАЧАТО ПОСТРОЕНИЕ СПРАВОЧНОЙ СТРУКТУРЫ ИЗ IFC")
    logger.info("=" * 60)

    processing_type = processing_type.upper()
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"
    logger.info(f"Тип обработки: {processing_type}")

    # Создаём папку, если её нет
    os.makedirs(output_folder, exist_ok=True)

    # Этап A: Извлечение элементов
    logger.info("\n--- ЭТАП A: Извлечение элементов из IFC ---")
    excel_path = extract_elements_from_ifc(ifc_path, output_folder, processing_type)
    if not excel_path:
        logger.error("Не удалось извлечь элементы из IFC")
        return []

    # Этап B: Группировка (через process_ifc_excel — тот же путь, что в веб-интерфейсе)
    logger.info("\n--- ЭТАП B: Группировка элементов ---")
    leaf_groups, full_groups, grouped_json_path, grouped_excel_path = group_elements_by_type(
        excel_path, output_folder, processing_type
    )
    if not leaf_groups:
        logger.error("Не удалось сгруппировать элементы")
        return []

    # Этап C: Формирование выходного формата
    logger.info("\n--- ЭТАП C: Формирование выходного формата ---")
    result = build_reference_output(leaf_groups, full_groups)

    # --- Перезаписываем ifc_raw_elements_grouped.json новым форматом ---
    # process_ifc_excel()/process_ifc_excel_ar() создали его в формате дерева групп
    # (для АР — с суффиксом _AR). Перезаписываем в формате справочника (как требует ТЗ)
    # и всегда под стандартным именем (без суффикса), чтобы веб-интерфейс и
    # session_manager находили файл по одному пути.
    # Это безопасно: веб-интерфейс использует filtered_elements_grouped.json,
    # а не ifc_raw_elements_grouped.json.
    final_json_path = os.path.join(output_folder, 'ifc_raw_elements_grouped.json')

    if grouped_json_path and result:
        with open(final_json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(
            f"Файл {os.path.basename(final_json_path)} перезаписан "
            f"в формате справочника ({len(result)} групп)"
        )

    # Удаляем промежуточные файлы — они больше не нужны
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
        logger.info(f"Удалён промежуточный файл {os.path.basename(excel_path)}")
    if grouped_excel_path and os.path.exists(grouped_excel_path):
        os.remove(grouped_excel_path)
        logger.info(f"Удалён промежуточный файл {os.path.basename(grouped_excel_path)}")

    logger.info("=" * 60)
    logger.info(f"ПОСТРОЕНИЕ СПРАВОЧНОЙ СТРУКТУРЫ ЗАВЕРШЕНО. "
                f"Сформировано {len(result)} групп.")
    logger.info("=" * 60)

    return result


# =====================================================================
#  ФУНКЦИЯ ДЛЯ PDF: ФОРМИРОВАНИЕ ifc_elements_output.json И ifc_raw_elements_grouped.json
# =====================================================================

def build_reference_from_pdf(df: pd.DataFrame, output_folder: str, processing_type: str = "KR") -> List[Dict[str, Any]]:
    """
    Формирует ifc_elements_output.json и ifc_raw_elements_grouped.json
    из DataFrame, полученного при обработке PDF-чертежа.

    Аналог build_reference_from_ifc, но работает с готовым DataFrame
    вместо IFC-файла.

    Аргументы:
        df — DataFrame с данными элементов из PDF (формат form_result_df)
        output_folder — папка для сохранения результатов
        processing_type — тип обработки: "KR" (конструктив) или "AR" (архитектура)

    Возвращает:
        Массив объектов в формате ifc_reference_output.json (через build_reference_output)
    """
    logger.info("=" * 60)
    logger.info("ФОРМИРОВАНИЕ JSON-ФАЙЛОВ ИЗ PDF-ЧЕРТЕЖА")
    logger.info("=" * 60)

    processing_type = processing_type.upper()
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"
    logger.info(f"Тип обработки: {processing_type}")

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
        xlsx_path, output_folder, processing_type
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
    # и всегда под стандартным именем (без суффикса _AR), чтобы веб-интерфейс
    # и session_manager находили файл по одному пути.
    final_json_path = os.path.join(output_folder, 'ifc_raw_elements_grouped.json')

    if grouped_json_path and result:
        with open(final_json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(
            f"Файл {os.path.basename(final_json_path)} перезаписан "
            f"в формате справочника ({len(result)} групп)"
        )

    # Удаляем временные файлы
    if os.path.exists(xlsx_path):
        os.remove(xlsx_path)
        logger.info(f"Удалён временный файл {os.path.basename(xlsx_path)}")
    if grouped_excel_path and os.path.exists(grouped_excel_path):
        os.remove(grouped_excel_path)
        logger.info(f"Удалён промежуточный файл {os.path.basename(grouped_excel_path)}")

    logger.info("=" * 60)
    logger.info(f"ФОРМИРОВАНИЕ JSON-ФАЙЛОВ ИЗ PDF ЗАВЕРШЕНО. "
                f"Сформировано {len(result)} групп.")
    logger.info("=" * 60)

    return result
