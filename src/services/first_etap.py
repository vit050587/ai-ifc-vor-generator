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
    
    if pd.isna(value) or value == '' or value == '-':
        return None
    if isinstance(value, (np.integer, np.int64)):
        return int(value)
    if isinstance(value, (np.floating, np.float64)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value

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
    element_row = df_elements.iloc[row_number]
    raw_data = {}
    for col in df_elements.columns:
        value = element_row.get(col)
        value = convert_value(value)
        if value is not None and value != '' and value != '-':
            raw_data[col] = value

    for key, value in raw_data.items():
        print(f"  • {key}: {value}")



    data_str = "\n".join([f"  • {k}: {v}" for k, v in raw_data.items()])

    return data_str, raw_data


def _create_first_step_prompt(prompt, data_str):
    data = {
        "data_str": data_str
    }
    prompt = prompt.format(**data)
    return prompt

def _analyze_row_wit_llm(raw_data, prompt, output_folder):

    logger.info(f"Обращение к модели {OLLAMA_MODEL}")
    
    try:
        client = ollama.Client(host=OLLAMA_URL, timeout=1200.0)
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={'temperature': 0.1}
        )
        
        answer = response['message']['content'].strip()
        #print(f"\nОтвет LLM (сырой):\n{answer[:500]}...")
        
        answer = clean_json_response(answer)
        #print(f"\nОтвет LLM (очищенный):\n{answer[:500]}...")
        
        
        result = json.loads(answer)
        
        
        result = validate_and_fix_json(result)
        
        if result.get('размеры') and len(result['размеры']) > 0:
            for key, value in result['размеры'].items():
                # Очищаем ключи от суффиксов для красивого вывода
                clean_key = key
                for suffix in ['_мм', '_м3', '_м2', '_литры', '_м']:
                    if key.endswith(suffix):
                        clean_key = key.replace(suffix, '')
                        break
                print(f"   • {clean_key}: {value}")
        else:
            print("   • Размеры не указаны")
        
        
        if result.get('материал'):
            material = result['материал']
            
            if material.get('название'):
                print(f"   • Название: {material['название']}")
            
            if material.get('количественные_характеристики') and len(material['количественные_характеристики']) > 0:
                print(f"   • Количественные характеристики:")
                for key, value in material['количественные_характеристики'].items():
                    print(f"      - {key}: {value}")
            
            if material.get('качественные_характеристики') and len(material['качественные_характеристики']) > 0:
                print(f"   • Качественные характеристики:")
                for key, value in material['качественные_характеристики'].items():
                    if key == 'слои' and isinstance(value, list):
                        print(f"      - {key}: {', '.join(value)}")
                    else:
                        print(f"      - {key}: {value}")
        else:
            print("   • Не определено")
        
        
        if result.get('описание_элемента') and len(result['описание_элемента']) > 0:
            for key, value in result['описание_элемента'].items():
                print(f"   • {key}: {value}")
        else:
            print("   • Нет данных")
        
    
        
        output = {
            "размеры": result.get('размеры', {}),
            "материал": result.get('материал', {}),
            "описание_элемента": result.get('описание_элемента', {}),
            "исходные_данные": raw_data
        }
        
        num = raw_data.get('№ п/п', '')

        output_filename = f'Нормализованные_данные_элемента_{num}.json' if not output_folder else os.path.join(output_folder, f'Нормализованные_данные_элемента_{num}.json')

        with open(output_filename, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=convert_value)
    
    except json.JSONDecodeError as e:
     
        if 'answer' in locals():
            print(f"Ответ LLM:\n{answer}")
    except Exception as e:
        print(e)
        if 'answer' in locals():
            print(f"Ответ LLM: {answer[:500]}")

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
    
 