# Здесь мы фильтруем по месту где располагается элемент надземная, подземная, цоколь .....
import pandas as pd
import json
import os
import re
from fuzzysearch import find_near_matches

from src.core.config import load_config
from src.core.logger import setup_logger

logger = setup_logger("second step")
_cfg = load_config()

LLM_MODEL = _cfg.model_ollama
OLLAMA_URL = _cfg.ollama_url
WORKS_FILE = _cfg.DOCUMENTS_PATH

IFC_CLLASS_FILTERING_FLAG = False

COLUMN_NAME = "Наименование расценки/ресурса"
MAX_DISTANCE = 2  # Максимальное расстояние Левенштейна

BUILDING_PART_LIST = ["Надземная", "Подземная", "Цоколь"]
ALL_PARTS = ["подземн", "надземн", "цоколь"]

def _load_building_parts(input_folder):
    """Загружает словарь {row_number: building_part} из JSON-файла"""
    parts_file = os.path.join(input_folder, 'building_parts.json')
    if os.path.exists(parts_file):
        try:
            with open(parts_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка чтения building_parts.json: {e}")
    return {}


def has_any_part(text):
     
    text_lower = text.lower()
    for part in ALL_PARTS:
        if part in text_lower:
            return True
    return False

def check_building_part(text, target_part, max_distance=2):
     
    if pd.isna(text) or not isinstance(text, str):
        return False
    
    text_lower = text.lower()
    target_lower = target_part.lower()
    
    
    if target_lower in text_lower:
        return True
    
     
    matches = find_near_matches(target_lower, text_lower, max_l_dist=max_distance)
    return len(matches) > 0

 

def _filter_one_element_by_part(output_folder, file_works_path, row_number, building_part, normalized_data):

    logger.info(f"Часть здания для фильтрации (строка {row_number}): {building_part}")
 
    logger.info(f"Загрузка файла: {file_works_path}")
    df_works = pd.read_excel(file_works_path)
    logger.info(f"  - Загружено строк: {len(df_works)}")

    if IFC_CLLASS_FILTERING_FLAG:
        logger.info(f"Фильтрация по IFC классу")
        ifc_class = normalized_data.get('основные_характеристики', {}).get('ifc_class', '')
        if not ifc_class:
            ifc_class = normalized_data.get('качественные', {}).get('Тип элемента', '')
    
        logger.info(f"IFC класс: '{ifc_class}'")
        df_works = df_works[df_works['IFC класс'] == ifc_class]
    
    if len(df_works) == 0:
        logger.info("   Нет данных для фильтрации")
        return
    
     
    df_reset = df_works.reset_index(drop=True)
    
     
    rows_to_keep = []
    found_examples = []
    skipped_examples = []
    
    for idx, row in df_reset.iterrows():
        text = row[COLUMN_NAME]
        if pd.isna(text) or not isinstance(text, str):
            rows_to_keep.append(idx)
            continue
        
         
        if not has_any_part(text):
             
            rows_to_keep.append(idx)
            if len(skipped_examples) < 5:
                preview = text[:60] + '...' if len(text) > 60 else text
                skipped_examples.append(f"[{idx}] {preview}")
            continue
        
         
        if check_building_part(text, building_part, MAX_DISTANCE):
            rows_to_keep.append(idx)
            if len(found_examples) < 10:
                preview = text[:80] + '...' if len(text) > 80 else text
                found_examples.append(f"[{idx}] {preview}")
    
    
    
    if found_examples:
         
        for ex in found_examples:
            print(f"  {ex}")
    else:
        print(f"\n  НЕ НАЙДЕНО СОВПАДЕНИЙ С '{building_part}'")
    
    if skipped_examples:
        print(f"\n  ОСТАВЛЕНО (без упоминаний частей здания):")
        for ex in skipped_examples:
            print(f"  {ex}")
        if len(skipped_examples) == 5:
            print(f"  ... и ещё несколько")
    
    
    total = len(df_reset)
    kept = len(rows_to_keep)

    logger.info(f'Всего работ {total}, оставлено {kept}')

    result_df = df_reset.iloc[rows_to_keep] if rows_to_keep else pd.DataFrame()
    
    
    if output_folder:
        output_file = os.path.join(output_folder, f'Промежуточные_работы_по_классу_{row_number}_после_фильтра_части.xlsx')
    else:
        output_file = f'Промежуточные_работы_по_классу_{row_number}_после_фильтра_части.xlsx'
    
    
    result_df.to_excel(output_file, index=False)
    
    logger.info(f"\n  Сохранено в: {output_file}")




def second_step(input_folder):
    logger.info("НАЧАТ ВТОРОЙ ЭТАП")
    
    # Загружаем словарь частей здания
    building_parts = _load_building_parts(input_folder)

    logger.info(building_parts)

    count = sum(1 for f in os.listdir(input_folder) 
            if f.endswith('.json') and f.startswith('Нормализованные_данные'))

    logger.info(f"Найдено файлов: {count}")
    for filename in os.listdir(input_folder):
        if filename.endswith('.json') and filename.startswith('Нормализованные_данные'):
            file_path = os.path.join(input_folder, filename)
            match = re.search(r'(\d+)(?=\.json$)', filename)
            if match:
                row_number = match.group(1)
            else:
                row_number = 1
            
            building_part = building_parts.get(str(int(row_number)-1), "Надземная")

            with open(file_path, 'r', encoding='utf-8') as file:
                logger.info(f"Загрузка нормализованных данных из файла {filename}")
                try:
                    data = json.load(file)
                    _filter_one_element_by_part(input_folder, WORKS_FILE, row_number, building_part, data)  
                    logger.info(f"Обработан файл: {filename}")
                except json.JSONDecodeError:
                    logger.warning(f"Ошибка чтения JSON в файле: {filename}")
    logger.info("  ===ЭТАП 2 ЗАВЕРШЕН===")
