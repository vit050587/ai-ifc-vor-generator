# ВЫбираем высоту зданий и фильтруем по ней
import os
import re
import ollama
import pandas as pd

from src.core.config import load_config
from src.core.logger import setup_logger

BUILDING_HEIGHT = 56      # Высота здания в метрах
logger = setup_logger("third_step")

_cfg = load_config()
 
LLM_MODEL = _cfg.model_ollama
OLLAMA_URL = _cfg.ollama_url

COLUMN_NAME = "Наименование расценки/ресурса"

def has_height_word(text):
 
    if pd.isna(text) or not isinstance(text, str):
        return False
    
    text_lower = text.lower()
    
    height_words = ['высот']
    
    for word in height_words:
        if word in text_lower:
            return True
    
    return False


def parse_height_condition(text, building_height):
 
    if pd.isna(text) or not isinstance(text, str):
        return None, "нет текста"
    
    text_lower = text.lower()
  
    height_pos = text_lower.find('высот')
    if height_pos == -1:
        return None, "нет слова высота"
    
     
    after_height = text_lower[height_pos + 6: height_pos + 150]
    
 
    match = re.search(r'более\s*(\d+(?:\.\d+)?)\s+до\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        min_h = float(match.group(1))
        max_h = float(match.group(2))
        result = min_h < building_height <= max_h
        return result, f"более {min_h} до {max_h} → {building_height}м {'входит' if result else 'НЕ входит'}"
    
 
    match = re.search(r'от\s*(\d+(?:\.\d+)?)\s+до\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        min_h = float(match.group(1))
        max_h = float(match.group(2))
        result = min_h <= building_height <= max_h
        return result, f"от {min_h} до {max_h} → {building_height}м {'входит' if result else 'НЕ входит'}"
    
 
    match = re.search(r'до\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        max_h = float(match.group(1))
        result = building_height <= max_h
        return result, f"до {max_h} → {building_height}м {'≤' if result else '>'} {max_h}"
 
    match = re.search(r'более\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        min_h = float(match.group(1))
        result = building_height > min_h
        return result, f"более {min_h} → {building_height}м {'>' if result else '≤'} {min_h}"
    
 
    match = re.search(r'с\s*(\d+(?:\.\d+)?)\s+до\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        min_h = float(match.group(1))
        max_h = float(match.group(2))
        result = min_h <= building_height <= max_h
        return result, f"с {min_h} до {max_h} → {building_height}м {'входит' if result else 'НЕ входит'}"
    
 
    match = re.search(r'от\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        min_h = float(match.group(1))
        result = building_height >= min_h
        return result, f"от {min_h} → {building_height}м {'≥' if result else '<'} {min_h}"
    
 
    match = re.search(r'свыше\s*(\d+(?:\.\d+)?)', after_height)
    if match:
        min_h = float(match.group(1))
        result = building_height > min_h
        return result, f"свыше {min_h} → {building_height}м {'>' if result else '≤'} {min_h}"
    
 
    return None, "нет чисел после слова высота"


def check_height_llm(text, building_height):
    
    prompt = f"""Ты - эксперт-сметчик. Определи, подходит ли эта работа для здания высотой {building_height} метров.

Текст работы: "{text}"

Правила:
1. Если в тексте НЕТ упоминаний о высоте здания - ответь ДА
2. Если есть "до X" - подходит если {building_height} ≤ X
3. Если есть "от X до Y" - подходит если X ≤ {building_height} ≤ Y
4. Если есть "более X" - подходит если {building_height} > X
5. Если есть "X<Y" - подходит если X < {building_height} < Y

Ответь ТОЛЬКО одним словом: ДА или НЕТ

Ответ:"""
    
    try:
        client = ollama.Client(host=OLLAMA_URL, timeout=1200.0)
        response = client.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={'temperature': 0.0}
        )
        answer = response['message']['content'].strip().upper()
        return 'ДА' in answer and 'НЕТ' not in answer
    except Exception as e:
        logger.info(f"Ошибка LLM: {e}")
        return True   

def has_height_condition(text):
    """Проверяет, есть ли в тексте упоминание о высоте здания"""
     
    height_keywords = [
        r'высот[аеы]?\s+здан',
        r'высот[аеы]?\s+сооруж',
        r'высот[аеы]?\s+строит',
        r'надземн[а-я]+\s+часть',
        r'подземн[а-я]+\s+часть',
        r'этажн[а-я]+',
        r'H\s*[<>=]',
        r'высот[аеы]?\s+до\s+\d+',
        r'высот[аеы]?\s+от\s+\d+',
        r'высот[аеы]?\s+более',
    ]
    
    text_lower = text.lower()
    for pattern in height_keywords:
        if re.search(pattern, text_lower):
            return True
    return False

 

def _filter_one_row_by_height(building_heqight, file_path, row_number, output_folder):
    
    logger.info(f"Высота здания: {building_heqight} м")
    
    logger.info(f"Загрузка файла: {file_path}")
    df_works = pd.read_excel(file_path)
    logger.info(f"  - Загружено строк: {len(df_works)}")
    
    if len(df_works) == 0:
        logger.info("  Нет данных для фильтрации")
        return

    rows_to_keep = []
    stats = {
        'no_height_word': 0,   
        'pattern_match': 0,    
        'pattern_removed': 0,  
        'llm_kept': 0,         
        'llm_removed': 0,      
    }

    
 
    
    for idx, row in df_works.iterrows():
        text = row[COLUMN_NAME]
        if pd.isna(text) or not isinstance(text, str):
            rows_to_keep.append(idx)
            stats['no_height_word'] += 1
            continue
        
    
        if not has_height_word(text):
            rows_to_keep.append(idx)
            stats['no_height_word'] += 1
            continue
        
       
        result, reason = parse_height_condition(text, building_heqight)
        
        if result is not None:
            # Паттерн сработал
            if result:
                rows_to_keep.append(idx)
                stats['pattern_match'] += 1
                #print(f"  [{idx}] ОСТАВЛЕНО (паттерн): {reason}")
            else:
                stats['pattern_removed'] += 1
                #print(f"  [{idx}]  УДАЛЕНО (паттерн): {reason}")
            continue
        
        
        #print(f"  [{idx}]  Паттерны не сработали, используем LLM...")
        llm_result = check_height_llm(text, building_heqight)
        
        if llm_result:
            rows_to_keep.append(idx)
            stats['llm_kept'] += 1
            #print(f"  [{idx}]  ОСТАВЛЕНО (LLM)")
        else:
            stats['llm_removed'] += 1
            #print(f"  [{idx}]  УДАЛЕНО (LLM)")
        
         
        
    total = len(df_works)
    kept = len(rows_to_keep)
    

    print(f"  - Всего строк: {total}")
    print(f"  - ОСТАВЛЕНО: {kept}")
    print(f"  - УДАЛЕНО: {total - kept}")
    print(f"\n  Детали:")
    print(f"    • Без слова 'высота' (оставлены): {stats['no_height_word']}")
    print(f"    • Паттерны (оставлены): {stats['pattern_match']}")
    print(f"    • Паттерны (удалены): {stats['pattern_removed']}")
    print(f"    • LLM (оставлены): {stats['llm_kept']}")
    print(f"    • LLM (удалены): {stats['llm_removed']}")
    
    result_df = df_works.iloc[rows_to_keep] if rows_to_keep else pd.DataFrame()
    
 
    if output_folder:
        output_file = os.path.join(output_folder, f'Промежуточные_работы_{row_number}_после_фильтров.xlsx')
    else:
        output_file = f'Промежуточные_работы_по_классу_{row_number}_после_фильтров.xlsx'

    result_df.to_excel(output_file, index=False)
    
    logger.info(f"\n  Сохранено в: {output_file}")
    

def third_step(input_folder, building_height):

    logger.info("НАЧАТ ТРЕТИЙ ЭТАП")

    count = sum(1 for f in os.listdir(input_folder) 
            if f.endswith('.xlsx') and "после_фильтра_части" in f)

    logger.info(f"Найдено файлов: {count}")
    for filename in os.listdir(input_folder):
        if filename.endswith('.xlsx') and "после_фильтра_части" in filename:
            file_path = os.path.join(input_folder, filename)
            match = re.search(r'(\d+)(?=_после_фильтра_части\.xlsx$)', filename)
            if match:
                row_number = match.group(1)
            else:
                row_number = 76790
            logger.info(f"Загрузка отфильтрованных мероприятий из файла {filename}")
            try:
                _filter_one_row_by_height(building_height, file_path, row_number, input_folder)
                logger.info(f"Обработан файл: {filename}")
            except Exception as e:
                logger.warning(f"Ошибка чтения файлa {filename}: {e}")

    logger.info("  ЭТАП 3 ЗАВЕРШЕН")


