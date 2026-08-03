# Выделяем из строки файла ДЛЯ_СМЕТЧИКА_исправленный_КР.xlsx   все количественные и качественные характеристики
import ollama
import pandas as pd
import json
import numpy as np
import os
import re

from src.core.config import load_config
from src.core.prompt_manager import PromptManager
from src.core.logger import setup_logger

logger = setup_logger("first_step")

_cfg = load_config()

OLLAMA_URL = _cfg.ollama_url
OLLAMA_MODEL = _cfg.model_ollama


ELEMENTS_FILE = 'ДЛЯ_СМЕТЧИКА_исправленный_КР.xlsx'
ELEMENT_ROW_INDEX = 17


def convert_value(value):
    """Безопасное преобразование значений для JSON"""
    if pd.isna(value) or value == '' or value == '-':
        return None
    if isinstance(value, (np.integer, np.int64)):
        return int(value)
    if isinstance(value, (np.floating, np.float64)):
        if np.isnan(value) or np.isinf(value):
            return None
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    # Для всех остальных типов — преобразуем в строку
    try:
        return str(value)
    except:
        return None

def clean_json_response(answer):
   
    if answer.startswith('```json'):
        answer = answer[7:]
    if answer.startswith('```'):
        answer = answer[3:]
    if answer.endswith('```'):
        answer = answer[:-3]
    answer = answer.strip()
    
     
    lines = answer.split('\n')
    cleaned_lines = []
    for line in lines:
        
        if '//' in line:
            line = line[:line.index('//')]
        if line.strip():
            cleaned_lines.append(line)
    answer = '\n'.join(cleaned_lines)
    
     
    answer = answer.replace("'", '"')
    
     
    answer = re.sub(r',\s*}', '}', answer)
    answer = re.sub(r',\s*]', ']', answer)
    
    return answer

def validate_and_fix_json(data):
     
    fixed = {
        "размеры": {},
        "материал": {
            "название": "",
            "количественные_характеристики": {},
            "качественные_характеристики": {}
        },
        "описание_элемента": {}
    }
    
    
    if not isinstance(data, dict):
        return fixed
    
     
    if "размеры" in data and isinstance(data["размеры"], dict):
         
        for key, value in data["размеры"].items():
            if value and not isinstance(value, str):
                fixed["размеры"][key] = str(value)
            elif value and isinstance(value, str) and value not in ["не указаны конкретные размеры", "нет данных"]:
                fixed["размеры"][key] = value
    
    if "материал" in data and isinstance(data["материал"], dict):
        if "название" in data["материал"] and data["материал"]["название"]:
            fixed["материал"]["название"] = data["материал"]["название"]
        
        if "количественные_характеристики" in data["материал"] and isinstance(data["материал"]["количественные_характеристики"], dict):
            fixed["материал"]["количественные_характеристики"] = data["материал"]["количественные_характеристики"]
        
        if "качественные_характеристики" in data["материал"] and isinstance(data["материал"]["качественные_характеристики"], dict):
            fixed["материал"]["качественные_характеристики"] = data["материал"]["качественные_характеристики"]
    
    if "описание_элемента" in data and isinstance(data["описание_элемента"], dict):
        for key, value in data["описание_элемента"].items():
            if value and value not in ["", "нет данных", "не указаны"]:
                fixed["описание_элемента"][key] = value
    
    return fixed


def _upload_file(file):
    logger.info(f"\nЗагрузка файла: {file}")
    df_elements = pd.read_excel(file, sheet_name='Данные')
    logger.info(f"  - Всего элементов: {len(df_elements)}")
    return df_elements


def _process_one_row(df_elements, row_number):
    try:
        element_row = df_elements.iloc[row_number]
    except IndexError:
        logger.error(f"Строка {row_number} не найдена в DataFrame (всего строк: {len(df_elements)})")
        raise
    
    raw_data = {}
    
    # Колонки, которые пропускаем
    SKIP_COLUMNS = [
        'Объём_NetVolume_м3_grouped',
        'Количество_в_группе_grouped',
        'Название_группы',
        'Уровень_группы',
        'Индексы_элементов',
        'Объём работ',  # может добавляться на предыдущих этапах
    ]
    
    # Максимальная длина значения
    MAX_VALUE_LENGTH = 500
    
    for col in df_elements.columns:
        # Пропускаем группировочные колонки
        if col in SKIP_COLUMNS or col.endswith('_grouped'):
            continue
        
        value = element_row.get(col)
        value = convert_value(value)
        
        if value is not None and value != '' and value != '-':
            # Обрезаем слишком длинные значения
            str_value = str(value)
            if len(str_value) > MAX_VALUE_LENGTH:
                str_value = str_value[:MAX_VALUE_LENGTH] + '...'
            
            raw_data[col] = str_value
    
    logger.info(f"Обработка строки {row_number}: {raw_data.get('Имя', 'неизвестно')[:100]}")
    logger.info(f"  Полей для отправки в LLM: {len(raw_data)}")
    
    data_str = "\n".join([f"  • {k}: {v}" for k, v in raw_data.items()])
    
    return data_str, raw_data


def _create_first_step_prompt(prompt, data_str):
    data = {
        "data_str": data_str
    }
    prompt = prompt.format(**data)
    return prompt

def _analyze_row_wit_llm(raw_data, prompt, output_folder):
    """Анализ строки через LLM с надёжным сохранением результата"""
    
    # Получаем номер элемента для имени файла
    num = raw_data.get('№ п/п', 'unknown')
    output_filename = os.path.join(output_folder, f'Нормализованные_данные_элемента_{num}.json')
    
    logger.info(f"=== Анализ элемента {num} ===")
    logger.info(f"Имя элемента: {raw_data.get('Имя', 'неизвестно')[:150]}")
    logger.info(f"Тип элемента: {raw_data.get('Тип элемента', 'неизвестно')}")
    
    answer = None  # Для логирования в случае ошибки
    
    try:
        # Обращение к LLM
        logger.info(f"Обращение к модели {OLLAMA_MODEL}...")
        
        client = ollama.Client(host=OLLAMA_URL, timeout=1200.0)
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={'temperature': 0.1}
        )
        
        answer = response['message']['content'].strip()
        logger.info(f"Получен ответ от LLM длиной {len(answer)} символов")
        logger.info(f"Первые 300 символов ответа:\n{answer[:300]}")
        
        # Очищаем ответ от лишнего форматирования
        answer_cleaned = clean_json_response(answer)
        logger.info(f"Ответ после очистки (первые 300 символов):\n{answer_cleaned[:300]}")
        
        # Парсим JSON
        result = None
        parse_error = None
        
        try:
            result = json.loads(answer_cleaned)
            logger.info(f"JSON успешно распарсен")
        except json.JSONDecodeError as e:
            parse_error = e
            logger.error(f"Ошибка парсинга JSON: {e}")
            logger.error(f"Позиция ошибки: строка {e.lineno}, столбец {e.colno}")
            
            # Показываем проблемный фрагмент
            lines = answer_cleaned.split('\n')
            if e.lineno and e.lineno <= len(lines):
                error_line = lines[e.lineno - 1]
                logger.error(f"Проблемная строка: {error_line[:200]}")
            
            # Пробуем исправить частые проблемы
            # 1. Убираем запятые перед закрывающими скобками
            fixed_json = re.sub(r',\s*}', '}', answer_cleaned)
            fixed_json = re.sub(r',\s*]', ']', fixed_json)
            
            # 2. Убираем незакрытые строки в конце
            # Если JSON обрывается на "ключ": без значения
            fixed_json = re.sub(r':\s*$', ': ""', fixed_json, flags=re.MULTILINE)
            # Если JSON обрывается на "ключ":
            fixed_json = re.sub(r'"\s*$', '": ""', fixed_json, flags=re.MULTILINE)
            
            # 3. Пробуем добавить недостающие закрывающие скобки
            open_braces = fixed_json.count('{')
            close_braces = fixed_json.count('}')
            if open_braces > close_braces:
                fixed_json += '\n' + '}' * (open_braces - close_braces)
            
            try:
                result = json.loads(fixed_json)
                logger.info(f"JSON исправлен после автоматических правок")
            except json.JSONDecodeError as e2:
                logger.error(f"Повторная ошибка парсинга JSON: {e2}")
                
                # Сохраняем отладочную информацию
                debug_file = os.path.join(output_folder, f'DEBUG_ответ_LLM_{num}.txt')
                try:
                    with open(debug_file, 'w', encoding='utf-8') as f:
                        f.write("="*60 + "\n")
                        f.write(f"Элемент: {num}\n")
                        f.write(f"Имя: {raw_data.get('Имя', 'неизвестно')}\n")
                        f.write("="*60 + "\n\n")
                        f.write("=== СЫРОЙ ОТВЕТ LLM ===\n")
                        f.write(answer + "\n\n")
                        f.write("=== ОЧИЩЕННЫЙ ОТВЕТ ===\n")
                        f.write(answer_cleaned + "\n\n")
                        f.write("=== ИСПРАВЛЕННЫЙ JSON ===\n")
                        f.write(fixed_json + "\n\n")
                        f.write("=== ОШИБКИ ===\n")
                        f.write(f"Первая ошибка: {e}\n")
                        f.write(f"Вторая ошибка: {e2}\n")
                    logger.info(f"Отладочная информация сохранена в {debug_file}")
                except Exception as write_err:
                    logger.error(f"Не удалось сохранить отладку: {write_err}")
                
                # Создаём минимальную рабочую структуру
                result = {
                    "размеры": {},
                    "материал": {
                        "название": "",
                        "количественные_характеристики": {},
                        "качественные_характеристики": {}
                    },
                    "описание_элемента": {},
                    "ошибка_парсинга_json": str(e2)[:300]
                }
        
        # Если result всё ещё None (не должно быть, но на всякий случай)
        if result is None:
            result = {
                "размеры": {},
                "материал": {
                    "название": "",
                    "количественные_характеристики": {},
                    "качественные_характеристики": {}
                },
                "описание_элемента": {}
            }
        
        # Валидируем и исправляем структуру
        result = validate_and_fix_json(result)
        
        # Добавляем исходные данные
        result["исходные_данные"] = raw_data
        
        # Логируем результат
        logger.info(f"Результат анализа элемента {num}:")
        logger.info(f"  Размеры: {len(result.get('размеры', {}))} полей")
        if result.get('размеры'):
            for k, v in result['размеры'].items():
                logger.info(f"    - {k}: {v}")
        
        logger.info(f"  Материал: {result.get('материал', {}).get('название', 'не указан')}")
        material_quant = result.get('материал', {}).get('количественные_характеристики', {})
        material_qual = result.get('материал', {}).get('качественные_характеристики', {})
        logger.info(f"    Количественные: {len(material_quant)} полей")
        logger.info(f"    Качественные: {len(material_qual)} полей")
        
        logger.info(f"  Описание: {len(result.get('описание_элемента', {}))} полей")
        
        # Сохраняем результат АТОМАРНО
        try:
            # Сначала сериализуем в строку
            json_str = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            
            # Проверяем, что JSON валидный (парсим обратно)
            try:
                json.loads(json_str)
            except json.JSONDecodeError as json_err:
                logger.error(f"Сгенерированный JSON невалиден: {json_err}")
                # Пробуем упростить данные
                result_simple = {
                    "размеры": {str(k): str(v)[:200] for k, v in result.get('размеры', {}).items()},
                    "материал": {
                        "название": str(result.get('материал', {}).get('название', ''))[:300],
                        "количественные_характеристики": {
                            str(k): str(v)[:200] 
                            for k, v in result.get('материал', {}).get('количественные_характеристики', {}).items()
                        },
                        "качественные_характеристики": {
                            str(k): str(v)[:200] if not isinstance(v, list) else [str(x)[:200] for x in v[:10]]
                            for k, v in result.get('материал', {}).get('качественные_характеристики', {}).items()
                        }
                    },
                    "описание_элемента": {str(k): str(v)[:200] for k, v in result.get('описание_элемента', {}).items()},
                    "исходные_данные": {
                        "№ п/п": raw_data.get('№ п/п', num),
                        "Имя": str(raw_data.get('Имя', ''))[:300],
                        "Тип элемента": str(raw_data.get('Тип элемента', ''))[:200],
                    }
                }
                json_str = json.dumps(result_simple, ensure_ascii=False, indent=2, default=str)
                logger.info(f"Использована упрощённая версия JSON")
            
            # Записываем во временный файл
            temp_file = output_filename + '.tmp'
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(json_str)
            
            # Атомарно заменяем основной файл
            if os.path.exists(output_filename):
                os.remove(output_filename)
            os.rename(temp_file, output_filename)
            
            file_size = os.path.getsize(output_filename)
            logger.info(f"✅ Сохранён файл: {os.path.basename(output_filename)} ({file_size} bytes)")
            
        except Exception as write_error:
            logger.error(f"❌ Ошибка при сохранении JSON для элемента {num}: {write_error}", exc_info=True)
            
            # Последняя попытка — сохраняем минимальную версию
            try:
                minimal_result = {
                    "размеры": {},
                    "материал": {
                        "название": str(result.get('материал', {}).get('название', ''))[:200],
                        "количественные_характеристики": {},
                        "качественные_характеристики": {}
                    },
                    "описание_элемента": {},
                    "исходные_данные": {
                        "№ п/п": num,
                        "Имя": str(raw_data.get('Имя', ''))[:200],
                        "Тип элемента": str(raw_data.get('Тип элемента', ''))[:100],
                    },
                    "ошибка_сохранения": str(write_error)[:200]
                }
                
                with open(output_filename, 'w', encoding='utf-8') as f:
                    json.dump(minimal_result, f, ensure_ascii=False, indent=2)
                logger.info(f"⚠️ Сохранена минимальная версия JSON для элемента {num}")
            except:
                logger.error(f"💥 Полный провал сохранения JSON для элемента {num}")
        
        return result
        
    except Exception as e:
        logger.error(f"💥 Критическая ошибка при анализе элемента {num}: {e}", exc_info=True)
        
        # Сохраняем ответ LLM если он был получен
        if answer:
            try:
                debug_file = os.path.join(output_folder, f'CRASH_элемент_{num}_ответ_LLM.txt')
                with open(debug_file, 'w', encoding='utf-8') as f:
                    f.write(f"Элемент: {num}\n")
                    f.write(f"Ошибка: {e}\n\n")
                    f.write(f"Ответ LLM:\n{answer[:5000]}")
            except:
                pass
        
        # Создаём аварийный JSON
        emergency_result = {
            "размеры": {},
            "материал": {
                "название": "",
                "количественные_характеристики": {},
                "качественные_характеристики": {}
            },
            "описание_элемента": {},
            "исходные_данные": {
                "№ п/п": num,
                "Имя": str(raw_data.get('Имя', ''))[:200],
                "Тип элемента": str(raw_data.get('Тип элемента', ''))[:100],
            },
            "критическая_ошибка": str(e)[:300]
        }
        
        try:
            with open(output_filename, 'w', encoding='utf-8') as f:
                json.dump(emergency_result, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"Создан аварийный JSON для элемента {num}")
        except Exception as write_error:
            logger.error(f"Не удалось сохранить даже аварийный JSON: {write_error}")
        
        return emergency_result

def _process_row_with_llm(df_elements, row_number, prompt, output_folder):
    data_str, raw_data = _process_one_row(df_elements, row_number)
    prompt_excluded = _create_first_step_prompt(prompt, data_str)
    _analyze_row_wit_llm(raw_data, prompt_excluded, output_folder)
    


def first_step(prompt_manager: PromptManager, file, rows=None, output_folder=None):

    print(rows)

    df_elements =_upload_file(file)

    first_step_prompt = prompt_manager.get_prompt('element_analyze')

    if not rows:
        for idx, row in df_elements.iterrows():
            _process_row_with_llm(df_elements, idx, first_step_prompt, output_folder)
    else:
        for row in rows:
            _process_row_with_llm(df_elements, row-1, first_step_prompt, output_folder)


    

    logger.info('=====ПЕРВЫЙ ЭТАП ЗАВЕРШЕН=====')
 