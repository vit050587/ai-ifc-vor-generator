import re

from src.core.logger import setup_logger

logger = setup_logger("geometry_filter")

def _extract_mm_value(text):
    """Извлекает числовое значение в миллиметрах из строки"""
    # Ищем число (с запятой или точкой) перед 'мм'
    match = re.search(r'(\d+(?:[.,]\d+)?)\s*мм', text, re.IGNORECASE)
    if match:
        # Заменяем запятую на точку и преобразуем в float
        return float(match.group(1).replace(',', '.'))
    return None

def _extract_m2_value(text):
    """Извлекает числовое значение в квадратных метрах из строки"""
    if not text or not isinstance(text, str):
        return None
    
    # Очищаем текст от лишних пробелов
    text = text.strip()
    
    # Ищем число (с запятой или точкой) перед 'м²', 'м2', 'кв.м', 'кв. м', 'm²', 'm2'
    match = re.search(
        r'(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м|m²|m2|sq\.?\s*m)',
        text, 
        re.IGNORECASE
    )
    
    if match:
        try:
            # Заменяем запятую на точку и преобразуем в float
            value = float(match.group(1).replace(',', '.'))
            # Проверяем на отрицательные значения
            if value < 0:
                logger.warning(f'Отрицательное значение площади: {value} м² в строке "{text}"')
                return abs(value)  # или return None, зависит от требований
            return value
        except (ValueError, TypeError) as e:
            logger.warning(f'Не удалось преобразовать значение площади: {e} в строке "{text}"')
            return None
    
    # Дополнительно: ищем просто число, если единицы измерения указаны отдельно
    match = re.search(r'(\d+(?:[.,]\d+)?)\s*$', text)
    if match:
        try:
            value = float(match.group(1).replace(',', '.'))
            logger.debug(f'Извлечено числовое значение без явного указания м²: {value} из "{text}"')
            return value if value >= 0 else None
        except (ValueError, TypeError):
            pass
    
    logger.debug(f'Не удалось извлечь значение площади из строки: "{text}"')
    return None

def _check_condition(required_value, actual_value, condition):
    """Проверяет выполнение условия"""
    if condition == 'до':
        return actual_value <= required_value
    elif condition == 'не более':
        return actual_value <= required_value
    elif condition == 'более':
        return actual_value > required_value
    elif condition == 'не менее':
        return actual_value >= required_value
    elif condition == 'менее':
        return actual_value < required_value
    elif condition == 'равно':
        return abs(actual_value - required_value) < 0.001  # учет погрешности
    elif condition == 'не до':
        return actual_value > required_value
    else:
        return False

def _determine_condition(matched_text, full_text):
    """Определяет тип условия сравнения"""
    matched_lower = matched_text.lower()
    
    if 'не более' in matched_lower:
        return 'не более'
    elif 'не менее' in matched_lower:
        return 'не менее'
    elif 'более' in matched_lower or 'больше' in matched_lower or 'свыше' in matched_lower:
        return 'более'
    elif 'менее' in matched_lower or 'меньше' in matched_lower:
        return 'менее'
    elif 'равно' in matched_lower:
        return 'равно'
    elif 'до' in matched_lower and 'толщин' in matched_lower:
        # Проверяем контекст вокруг слова "до"
        if 'не до' in matched_lower:
            return 'не до'
        else:
            return 'до'
    else:
        return 'равно'

def _filter_by_width(works_list, width):
    """"Фильтрация работ для стен по толщине"""

    patterns = [
        r'толщин(?:ой|а|ы)\s*(?:не\s+)?(?:до|более|менее|больше|меньше|свыше|равно|не\s+менее|не\s+более)\s*(\d+(?:[.,]\d+)?)',
        r'толщин(?:ой|а|ы)\s*(\d+(?:[.,]\d+)?)\s*(?:мм|см|м)?\s*(?:не\s+)?(?:до|более|менее|больше|меньше|свыше|равно|не\s+менее|не\s+более)',
        r'(?:до|не\s+более|более|менее|не\s+менее|больше|меньше|свыше|равно)\s*(\d+(?:[.,]\d+)?)\s*(?:мм|см|м)?\s*толщин',
    ]
    works_list_filtered = []

    for work in works_list:
        found_match = False
        for pattern in patterns:
            match = re.search(pattern, work, re.IGNORECASE)
            if match:
                found_match = True
                # Извлекаем число
                number_str = match.group(1).replace(',', '.')
                extracted_number = float(number_str)
                
                # Определяем условие
                condition = _determine_condition(match.group(0), work)
                
                # Сравниваем с фактической толщиной
                satisfied = _check_condition(extracted_number, width, condition)
                
                if satisfied:
                    works_list_filtered.append(work)
            # Если не нашли ни одного совпадения по паттернам
        if not found_match:
            works_list_filtered.append(work)

                
    return works_list_filtered


def _filter_by_square(works_list, square):
    """"Фильтрация работ для плит по площади"""
    
    patterns = [
        r'площади перекрытия между осями колонн или стен\s*(?:не\s+)?(?:до|более|менее|больше|меньше|свыше|равно|не\s+менее|не\s+более)\s*(\d+(?:[.,]\d+)?)',
        r'площадь перекрытия между осями колонн или стен\s*(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м|m²|m2|sq\.?\s*m)?\s*(?:не\s+)?(?:до|более|менее|больше|меньше|свыше|равно|не\s+менее|не\s+более)',
        r'(?:до|не\s+более|более|менее|не\s+менее|больше|меньше|свыше|равно)\s*(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м|m²|m2|sq\.?\s*m)?\s*площадь',
    ]
    works_list_filtered = []

    for work in works_list:
        found_match = False
        for pattern in patterns:
            match = re.search(pattern, work, re.IGNORECASE)
            if match:
                found_match = True
                # Извлекаем число
                number_str = match.group(1).replace(',', '.')
                extracted_number = float(number_str)
                
                # Определяем условие
                condition = _determine_condition(match.group(0), work)
                
                # Сравниваем с фактической площадью
                satisfied = _check_condition(extracted_number, square, condition)
                
                if satisfied:
                    works_list_filtered.append(work)
            # Если не нашли ни одного совпадения по паттернам
        if not found_match:
            works_list_filtered.append(work)

                
    return works_list_filtered



def geometry_filter(works_list, sizes, ifc_type):
    """Фильтрация работ по геометрии"""

    works_list_filtered = []

    if not sizes:
        logger.warning('Размеры не предоставлены, возвращаем исходный список работ без фильтрации.')
        return works_list

    if ifc_type == 'IfcWall':
        width = sizes.get('ширина', '')
        if not width:
            width = sizes.get('ширина_сечения', '')
        
        try:
            width = _extract_mm_value(width)
            width = float(width)
        except Exception as e:
            width = ''
            logger.warning(f'Не удалось привести толщину к численному значению {e}')
        if width:
            works_list_filtered = _filter_by_width(works_list, width)
    
    elif ifc_type == 'IfcSlab':
        square = sizes.get('площадь_NetArea', '')
        if not square:
            square = sizes.get('площадь_GrossArea', '')
            if not square:
                square = sizes.get('площадь', '')
    

        try:
            square = _extract_m2_value(square)
            square = float(square)
        except Exception as e:
            square = ''
            logger.warning(f'Не удалось привести площадь к численному значению {e}')
        if square:
            works_list_filtered = _filter_by_square(works_list, square)

    else: 
        works_list_filtered = works_list.copy()

    return works_list_filtered