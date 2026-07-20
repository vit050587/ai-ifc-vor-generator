# ЭТАП 4: ПОДБОР РАБОТ ПО МАТЕРИАЛУ + КЛЮЧЕВЫМ ФРАЗАМ ИЗ БАЗЫ ЗНАНИЙ

import pandas as pd
import json
import re
import os
import pymorphy3
import ollama
from fuzzywuzzy import fuzz

from src.core.config import load_config
from src.core.logger import setup_logger
from src.services.base_knowledge import KNOWLEDGE_BASE
from src.services.geometry_filter import geometry_filter

logger = setup_logger("fourth_step")
_cfg = load_config()

LLM_MODEL = _cfg.model_ollama
OLLAMA_URL = _cfg.ollama_url
KOEFS_FILE = _cfg.KOEFS_PATH
PRICE_COST_FILE = _cfg.PRICE_COST_PATH
SIMILARITY_THRESHOLD = 80

_price_cost_lookup_cache = None 

morph = pymorphy3.MorphAnalyzer()

STOP_WORDS = {'и', 'в', 'на', 'с', 'по', 'к', 'у', 'о', 'от', 'для',
              'из', 'за', 'под', 'над', 'без', 'до', 'при', 'через',
              'состав', 'слой', 'материал', 'конструкция', 'стена',
              'базовая', 'элемент', 'работа', 'не', 'указано', 'указана',
              'подземная', 'подземный', 'надземная', 'надземный',
              'цоколь', 'кровля', 'подвал', 'мансарда', 'техническая'}


def _get_corrected_volume(df):

    koefs = pd.read_excel(KOEFS_FILE)

    df_copy = df.copy()

    koefs_filtered = koefs[koefs['Шифр ТСН'].isin(df_copy['Шифр ТСН'])].copy()

    koefs_filtered_by_material = koefs_filtered[koefs_filtered['Наименование открытой группы ресурсов/\nресурса в составе открытой группы'].isin(df_copy['Наименование расценки/ресурса'])].copy()

    for idx, koef_row in koefs_filtered_by_material.iterrows():
        # Находим индекс строки в df
        df_idx = df_copy[df_copy['Наименование расценки/ресурса'] == koef_row['Наименование открытой группы ресурсов/\nресурса в составе открытой группы']].index[0]
        
        # Умножаем объём на норму расхода
        df_copy.loc[df_idx, 'Объём работ'] = df_copy.loc[df_idx, 'Объём работ'] * koef_row['Норма расхода'] / 100 if koef_row['Норма расхода'] > 100 else df_copy.loc[df_idx, 'Объём работ'] * koef_row['Норма расхода']

    print("Готово!")
    print(df_copy)     
    return df_copy  



def extract_words(text):
    if not text:
        return []
    words = []
    parts = re.split(r'[+;,/]| и | с ', str(text))
    for part in parts:
        part = part.strip()
        if part:
            found = re.findall(r'[А-Яа-яA-Za-z]{4,}', part)
            words.extend(found)
    return list(set([w.lower() for w in words]))

def normalize_quotes(text):
    """Заменяет кавычки-ёлочки на прямые кавычки"""
    if isinstance(text, str):
        text = text.replace('«', '"').replace('»', '"')
        text = text.replace('„', '"').replace('“', '"').replace('”', '"')
        text = text.replace('\n', ' ')
    return text


def _get_price_cost_lookup():
    """Загружает price_cost.xlsx и возвращает словарь {Шифр расценки: Текущие прямые затраты/Всего затр}"""
    global _price_cost_lookup_cache
    if _price_cost_lookup_cache is not None:
        return _price_cost_lookup_cache

    try:
        df = pd.read_excel(PRICE_COST_FILE, sheet_name='_Получние_параметры_позиции_sel')
        _price_cost_lookup_cache = dict(zip(df['Шифр расценки'], df['Текущие прямые затраты/Всего затр']))
        logger.info(f"Загружено {len(_price_cost_lookup_cache)} расценок из price_cost.xlsx")
    except Exception as e:
        logger.error(f"Ошибка загрузки price_cost.xlsx: {e}")
        _price_cost_lookup_cache = {}
    return _price_cost_lookup_cache


def _add_cost_column(df):
    """Добавляет колонку 'Стоимость' = 'Текущие прямые затраты/Всего затр' × 'Объём работ'"""
    lookup = _get_price_cost_lookup()
    if 'Шифр ТСН' not in df.columns or 'Объём работ' not in df.columns:
        logger.warning("Не найдены колонки 'Шифр ТСН' или 'Объём работ' для расчёта стоимости")
        df['Стоимость'] = ''
        return df

    def calculate_cost(row):
        shifr = row.get('Шифр ТСН')
        volume = row.get('Объём работ')
        if pd.isna(shifr) or pd.isna(volume):
            return ''
        try:
            shifr_clean = str(shifr).strip()
            if shifr_clean not in lookup:
                return ''
            cost = float(lookup[shifr_clean]) * float(volume)
            return round(cost, 2)
        except (ValueError, TypeError):
            return ''

    df['Стоимость'] = df.apply(calculate_cost, axis=1)
    df['Стоимость'] = df['Стоимость'].fillna('')
    return df

def _find_column_with_volume(data, marker, extra_marker):
    if 'Количество_в_группе' in data.keys():
        logger.info('обнаружены данные после группирвоки')
        for param, value in data.items():
            if marker in param and extra_marker in param and value and 'grouped' in param:
                return value
        for param, value in data.items():
            if marker in param and value and 'grouped' in param:
                return value
    else:
        for param, value in data.items():
            if marker in param and extra_marker in param and value:
                return value
        for param, value in data.items():
            if marker in param and value:
                return value
    
    return ''


def _process_one_element(normalized_data, row_number, output_folder):
    """Обработка одного элемента: поиск работ по материалу + базе знаний"""

    # Данные элемента
    material = normalized_data.get('материал', {})
    material_name = material.get('название', '')
    material_quant = material.get('количественные_характеристики', {})
    material_qual = material.get('качественные_характеристики', {})

    element_description = normalized_data.get('исходные_данные', {})
    element_type = element_description.get('Тип (RU)', '')

    sizes = normalized_data.get('размеры', {})

    # Дополнительные поля из нового формата
    ifc_type = element_description.get('Тип элемента', '')
    storey_type = element_description.get('Тип_этажа', '')
    element_part = element_description.get('Этаж', '')
    composition = normalized_data.get('состав_из_имени', '')
    material_detected = normalized_data.get('материал_определенный', '')
    element_name = element_description.get('Имя', '')
    armature_ratio = element_description.get('Pset_ConcreteElementGeneral_ReinforcementVolumeRatio', 0)
    probably_beton = 'Смесь бетонная' + element_description.get('ExpCheck_MaterialConcrete_MGE_ConcreteGrade', '') + element_description.get('Property_ExpCheck_MaterialConcrete.MGE_WaterResist', '') + element_description.get('Property_ExpCheck_MaterialConcrete.MGE_FreezeDurability', '')

    if not material_name:
        material_detected = element_description.get('Имя', '')


    previous_data = normalized_data.get('исходные_данные', {})
    quantitative = normalized_data.get('количественные', {})

    # Путь к файлу с промежуточными работами
    works_file = os.path.join(output_folder, f'Промежуточные_работы_{row_number}_после_фильтров.xlsx')
    if not os.path.exists(works_file):
        logger.warning(f"Файл работ не найден для строки {row_number}")
        return

    logger.info(f"Обработка элемента {row_number}: материал={material_name}, IFC={ifc_type}")

    df_works = pd.read_excel(works_file)

    # Ищем колонку с наименованием
    search_col = None
    for col in df_works.columns:
        if col == 'Наименование':
            search_col = col
            break
    if not search_col:
        for col in df_works.columns:
            if 'наименование' in col.lower() and 'расценк' in col.lower():
                search_col = col
                break
    if not search_col:
        logger.warning("Колонка с наименованием не найдена")
        return

    # === Шаг 1: Извлечение ключевых слов ===
    all_words = []

    if material_name and material_name not in ['не указано', '-']:
        all_words.extend(extract_words(material_name))
    if material_detected and material_detected not in ['не указано', '-']:
        all_words.extend(extract_words(material_detected))
    if composition:
        all_words.extend(extract_words(composition))

    for key, value in material_qual.items():
        if value:
            all_words.extend(extract_words(value))
    for key, value in material_quant.items():
        if value and isinstance(value, str):
            all_words.extend(extract_words(value))

    all_words = [w for w in all_words if w not in STOP_WORDS and len(w) > 3]
    all_words = list(set(all_words))
    logger.info(f"Ключевые слова: {all_words}")

    # === Шаг 1.5: Ключевые фразы из базы знаний ===
    knowledge_phrases = []
    knowledge_phrases_material = []
    if ifc_type in KNOWLEDGE_BASE:
        beton_pattern = r'B\d{2}(?!\d)|W\d(?!\d)|F\d{3}(?!\d)'
        if re.search(beton_pattern, element_name) or 'бетон' in element_name.lower() or 'бетон' in material_name.lower():
            base_material = 'бетон'
        else:
            base_material = ''
        knowledge_phrases = list(KNOWLEDGE_BASE[ifc_type].get('keywords_for_search', []))
        knowledge_phrases_material = list(KNOWLEDGE_BASE[ifc_type].get('material_key_words', {}).get(base_material, []))
        knowledge_phrases.extend(knowledge_phrases_material)
        logger.info(f"Ключевые фразы из базы знаний: {knowledge_phrases}")

    # === Шаг 2: Поиск форм слов ===
    all_forms = {}
    for word in all_words:
        try:
            parsed = morph.parse(word)[0]
            forms = list(set([form.word for form in parsed.lexeme]))
            all_forms[word] = forms
        except Exception as e:
            logger.warning(f'Не удалось проанализировать слово "{word}": {e}')
            all_forms[word] = [word]

    # === Шаг 3: Поиск работ ===
    all_found_works = {}

    # Точный поиск по формам
    for word, forms in all_forms.items():
        for idx, row in df_works.iterrows():
            work_name = str(row[search_col]) if pd.notna(row[search_col]) else ''
            if not work_name or len(work_name) < 3:
                continue

            work_lower = work_name.lower()
            matched_forms = [form for form in forms if form.lower() in work_lower]

            if matched_forms:
                if work_name not in all_found_works:
                    all_found_works[work_name] = {
                        'наименование': work_name,
                        'совпадения': [],
                        'тип_поиска': []
                    }
                all_found_works[work_name]['совпадения'].extend(matched_forms)
                if 'точный' not in all_found_works[work_name]['тип_поиска']:
                    all_found_works[work_name]['тип_поиска'].append('точный')

    # Нечёткий поиск
    all_work_names = [str(row[search_col]) for _, row in df_works.iterrows()
                      if pd.notna(row[search_col]) and len(str(row[search_col])) > 3]

    for word in all_words:
        for work_name in all_work_names:
            similarity = fuzz.partial_ratio(word, work_name.lower())
            if similarity >= SIMILARITY_THRESHOLD:
                if work_name not in all_found_works:
                    all_found_works[work_name] = {
                        'наименование': work_name,
                        'совпадения': [],
                        'тип_поиска': []
                    }
                if 'нечеткий' not in all_found_works[work_name]['тип_поиска']:
                    all_found_works[work_name]['тип_поиска'].append('нечеткий')

    # Поиск по ключевым фразам из базы знаний
    if ifc_type in KNOWLEDGE_BASE and knowledge_phrases:
        for keyword_phrase in knowledge_phrases:
            keyword_phrase_lower = keyword_phrase.lower()
            for idx, row in df_works.iterrows():
                work_name = str(row[search_col]) if pd.notna(row[search_col]) else ''
                if not work_name or len(work_name) < 3:
                    continue
                if keyword_phrase_lower in work_name.lower():
                    if work_name not in all_found_works:
                        all_found_works[work_name] = {
                            'наименование': work_name,
                            'совпадения': [keyword_phrase],
                            'тип_поиска': []
                        }
                    if 'база_знаний_фраза' not in all_found_works[work_name]['тип_поиска']:
                        all_found_works[work_name]['тип_поиска'].append('база_знаний_фраза')

    
    base_works = KNOWLEDGE_BASE.get(ifc_type, '')
    
    base_works_words = []

    base_works_words_str = ''

    if base_works:
        base_works_words = base_works.get('keywords_for_search', '')
        if base_works_words:
            base_works_words_str = ', '. join(base_works_words)

    # === Шаг 4: Сортировка ===
    def sort_key(x):
        score = 0
        if 'точный' in x[1]['тип_поиска']:
            score += 1000
        if 'нечеткий' in x[1]['тип_поиска']:
            score += 100
        if 'база_знаний_фраза' in x[1]['тип_поиска']:
            score += 10
        return score

    unique_works = {}
    for work_name, work_data in all_found_works.items():
        key = work_name.strip()
        if key not in unique_works:
            unique_works[key] = work_data

    sorted_works = list(unique_works.items())
    sorted_works.sort(key=sort_key, reverse=True)
    sorted_works = [work_data for work_name, work_data in sorted_works]

    logger.info(f"Найдено работ: {len(sorted_works)}")

    if not sorted_works:
        logger.warning(f"Работы не найдены для элемента {row_number}")
        return

    # === Шаг 5: LLM-отбор ===
    top_works = sorted_works[:100]
    works_list = []
    for i, work in enumerate(top_works, 1):
        search_type = ', '.join(work['тип_поиска'])
        works_list.append(f"{i}. {work['наименование']} [источник: {search_type}]")


    #Фильтрация по геометрическим параметрам объекта (толщина для стен, площадь для плит и т.д.)
    works_list = geometry_filter(works_list, sizes, ifc_type)
        
    works_text = "\n".join(works_list)

    element_info = f"""
## ОПИСАНИЕ ЭЛЕМЕНТА:
- Тип IFC: {ifc_type}
- Тип элемента: {element_type}
- Описание элемента: {element_name}
- Материал: {material_name}
- Материал (определенный): {material_detected}
- Состав: {composition if composition else 'не указан'}
- Тип этажа: {storey_type if storey_type else 'не указан'}
- Часть здания: {element_part if element_part else 'не указана'}
- Количественные характеристики: {json.dumps(material_quant, ensure_ascii=False, indent=2)}
- Качественные характеристики: {json.dumps(material_qual, ensure_ascii=False, indent=2)}
- Размеры: {json.dumps(sizes, ensure_ascii=False, indent=2)}
"""
    
    material_base_works_str = ''

    if base_works:
        base_materials_works = base_works.get('material_key_words', '')
        
        beton_pattern = r'B\d{2}(?!\d)|W\d(?!\d)|F\d{3}(?!\d)'

        for word in all_words:
            if 'бетон' in word or 'железобетон' in word:
                base_material = 'бетон'
                element_info += f"\n -Бетонная смесь по описанию ifc: {probably_beton}"
                break
        else: 
            base_material = ''
        if not base_material:
            if re.search(beton_pattern, element_name) or 'бетон' in element_name.lower():
                base_material = 'бетон'
            else:
                base_material = ''

        if base_material and base_materials_works:
            material_base_works_list = base_materials_works.get(base_material, '')
            material_base_works_str = ', '. join(material_base_works_list) + '\n ВАЖНО Если в названии объекта нет указания на материал железобетонной смеси, то выбирай Смесь бетонная тяжелого бетона БСТ на гранитном щебне, крупность заполнителя от 5 до 20 мм, класс прочности В7,5 (М100), П3'

    

    try:
        if base_works_words_str:
            element_info += f"\n -Работы для класса {ifc_type} обязательно должны содержать: {base_works_words_str}"
            print(f'Класс: {base_works_words_str}') 
    except Exception as e:
        print('Информации по работам по классам нет')
    
    try:
        print(f'Материал: {material_base_works_str}')
        element_info += f"""\n -Работы для этого элемента обязательно должны содержать: {material_base_works_str}. \n Если не будет хотя бы одной работы из этого списка в ответе, то я тебя уволю
        НИ В КОЕМ СЛУЧАЕ НЕЛЬЗЯ ЗАБЫВАТЬ ПРО РАБОТЫ: {material_base_works_str}. Без них ответ будет считаться неверным."""
    except Exception as e:
        print('Информации по работам по материалу нет')


    prompt = f"""Ты — эксперт-сметчик. Выбери наиболее подходящие работы для создания строительного элемента в здании.

{element_info}  

## ВАЖНОЕ ПРАВИЛО:
Ты МОЖЕШЬ выбирать работы ТОЛЬКО из списка ниже.
НЕЛЬЗЯ придумывать свои названия работ.
НУЖНО брать названия ТОЧНО ТАК, КАК ОНИ НАПИСАНЫ в списке.

Если нет точного совпадения по бетонной смеси, цементу, кирпичу и прочему, выбери и включи в список самый подходящий (если нет похожих, то ничего не добавляй)
Если элемент относится к подземной или цокольной части здания, то это не является автостоянкой, если это не указано явно

ВАЖНО: в установку армутурных изделей не входит сама арматура, поэтому их нужно обязательно включить в список отдельно. Причем бери любую арматуру или арматурные заготовки (например, первую попавшуюся в списке), если нет точного совпадения по марке арматуры. Если есть точное совпадение по марке арматуры, то бери её.

## ОСОБОЕ ВНИМАНИЕ К РАБОТАМ ИЗ БАЗЫ ЗНАНИЙ:
Работы с пометкой [источник: база_знаний_фраза] ОБЯЗАТЕЛЬНО должны быть включены.


## СПИСОК ДОСТУПНЫХ РАБОТ:
{works_text}



## ФОРМАТ ОТВЕТА (ТОЛЬКО JSON):
{{
  "выбранные_работы": [
    {{
      "наименование": "ТОЧНОЕ НАЗВАНИЕ ИЗ СПИСКА",
      "обоснование": "почему подходит",
      "категория": "подготовительные/опалубочные/арматурные/бетонные/уход за бетоном/гидроизоляционные/пароизоляционные/теплоизоляционные/отделочные/монтажные/другие"
    }}
  ],
  "рекомендация": "краткий вывод"
}}"""
    try:
        print(f'Материал: {material_base_works_str}')
        prompt += f"""\n Перед тем как дать ответ, проверь, что в выбранных работах есть все работы из списка: {material_base_works_str}."""
    except Exception as e:
        print('Информации по работам по материалу нет')
    try:
        client = ollama.Client(host=OLLAMA_URL, timeout=120.0)
        response = client.chat(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={'temperature': 0.1}
        )
        answer = response['message']['content'].strip()

        if answer.startswith('```json'):
            answer = answer[7:]
        if answer.startswith('```'):
            answer = answer[3:]
        if answer.endswith('```'):
            answer = answer[:-3]
        answer = answer.strip()

        result = json.loads(answer)
        selected_works = result.get('выбранные_работы', [])

        print(f"Выбранные работы для элемента {row_number}: {selected_works}")

        if not selected_works:
            logger.warning(f"LLM не выбрал работы для элемента {row_number}")
            return
        # === Шаг 6: Формируем Excel ===
        rows = []
        for work in selected_works:
            work_name = normalize_quotes(work['наименование'].replace("'", '"').replace("ё", "е"))
            print(work_name)
            df_works['normalized_col'] = df_works[search_col].apply(normalize_quotes)
            matching = df_works[df_works['normalized_col'] == work_name]
            if len(matching) > 0:
                row_data = matching.iloc[0].to_dict()
            else:
                matching = df_works[df_works['normalized_col'].str.contains(re.escape(element_type[:-2].lower()), case=False, na=False) &
                    df_works['normalized_col'].str.contains(re.escape(work_name), case=False, na=False)
                ]
                if len(matching) > 0:
                    row_data = matching.iloc[0].to_dict()
                else:
                    matching = df_works[df_works['normalized_col'].str.contains(re.escape(work_name[:65]), case=False, na=False)
                ]
                    if len(matching) > 0:
                        row_data = matching.iloc[0].to_dict()
                    else:
                        row_data = {search_col: work_name}

            row_data['Категория'] = work.get('категория', '')
            row_data['Обоснование'] = work.get('обоснование', '')
            rows.append(row_data)

        df_result = pd.DataFrame(rows)

        cols_to_drop = ["Ед. изм", "Наименование работ", "IFC класс",
                        "Формула расчёта объёмов работ и расхода материалов",
                        "Обозначения", "Обоснование", "Категория", "V по смете", "normalized_col",
                        "Параметризация", "№ п/п"]
        df_result = df_result.drop([c for c in cols_to_drop if c in df_result.columns], axis=1)

        # Объём работ
        net_square = _find_column_with_volume(previous_data, "м2", "Net")
        net_volume = _find_column_with_volume(previous_data, "м3", "Net")
        gross_square = _find_column_with_volume(previous_data, "м2", "_GrossArea")

        if 'Ед. изм.' in df_result.columns:
            def get_volume_of_work(row):
                unit = str(row.get('Ед. изм.', '')).lower().replace(' ', '')
                
                conversions = {
                    'м2': (gross_square, 1, 'м2'),
                    '100м2': (gross_square, 100, '(100 м2)'),
                    'м3': (net_volume, 1, 'м3'),
                    '100м3': (net_volume, 100, '(100 м3)'),
                    'т': (net_volume * armature_ratio, 1, 'т'),
                    '1т': (net_volume * armature_ratio, 1, 'т')
                }
                
                for unit_key, (value, divisor, label) in conversions.items():
                    if unit_key == unit and value:
                        converted = value / divisor
                        decimals = 4 if divisor > 1 else (2 if 'м2' in unit_key else 3)
                        return f"{converted:.{decimals}f}"
                
                return ''
            df_result['Объём работ'] = df_result.apply(get_volume_of_work, axis=1)
        else:
            logger.warning("Колонка 'Ед. изм.' не найдена в df_result. Объём работ не будет рассчитан.")
            df_result['Объём работ'] = ''
        
        try:
            df_result = _get_corrected_volume(df_result)
        except Exception as e: 
            logger.error(f"Ошибка при корректировке объёма работ: {e}")

        # Добавляем колонку "Стоимость"
        try:
            df_result = _add_cost_column(df_result)
        except Exception as e:
            logger.error(f"Ошибка при расчёте стоимости: {e}")

        # Сохраняем
        ifc_class = normalized_data.get('основные_характеристики', {}).get('ifc_class', '')
        if not ifc_class:
            ifc_class = normalized_data.get('качественные', {}).get('Тип элемента', '')

        output_filename = os.path.join(output_folder, f'Финальный_перечень_работ_{ifc_class}_{row_number}.xlsx')
        df_result.to_excel(output_filename, index=False)

        json_filename = os.path.join(output_folder, f'Подобранные_работы_{row_number}.json')
        with open(json_filename, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # Сохраняем все найденные работы
        all_works_output = {
            'элемент': {
                'тип_ifc': ifc_type,
                'тип_элемента': element_type,
                'материал': {
                    'название': material_name,
                    'определенный': material_detected,
                    'состав': composition,
                    'количественные': material_quant,
                    'качественные': material_qual
                },
                'размеры': sizes,
                'тип_этажа': storey_type,
                'часть_здания': element_part
            },
            'ключевые_слова_из_материала': all_words,
            'ключевые_фразы_из_базы': knowledge_phrases if ifc_type in KNOWLEDGE_BASE else [],
            'найденные_работы': sorted_works
        }
        all_json_filename = os.path.join(output_folder, f'Все_найденные_работы_{row_number}.json')
        with open(all_json_filename, 'w', encoding='utf-8') as f:
            json.dump(all_works_output, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"Сохранено: {output_filename}")

    except Exception as e:
        logger.error(f"Ошибка LLM для элемента {row_number}: {e}")
        if 'answer' in locals():
            logger.error(f"Ответ LLM: {answer[:500]}")


def merge_final_worklists(input_folder):
    """
    Объединяет все файлы 'Финальный_перечень_работ_*.xlsx' в один файл.
    Перед данными каждого файла вставляет строку со значениями переменных
    ifc_class, name_element, global_id из соответствующего JSON файла
    """
    
    output_file = os.path.join(input_folder, 'ОБЩИЙ_Финальный_перечень_работ.xlsx')
    
    # Ищем все финальные перечни
    excel_files = []
    for filename in os.listdir(input_folder):
        if filename.startswith('Финальный_перечень_работ_') and filename.endswith('.xlsx'):
            if not filename.startswith('ОБЩИЙ_'):
                excel_files.append(filename)
    
    if not excel_files:
        logger.error("❌ Файлы 'Финальный_перечень_работ_*.xlsx' не найдены!")
        return None
    
    # Сортируем по номеру
    excel_files.sort(key=lambda x: int(re.search(r'_(\d+)\.xlsx$', x).group(1)) 
                     if re.search(r'_(\d+)\.xlsx$', x) else 0)
    
    logger.info(f"📁 Найдено файлов для объединения: {len(excel_files)}")
    print(f"Найдено файлов: {len(excel_files)}")
    print("-" * 50)
    
    all_parts = []
    
    # Читаем первый файл чтобы узнать все колонки
    first_file_path = os.path.join(input_folder, excel_files[0])
    first_df = pd.read_excel(first_file_path)
    all_columns = first_df.columns.tolist()
    num_columns = len(all_columns)
    
    for filename in excel_files:
        file_path = os.path.join(input_folder, filename)
        match = re.search(r'_(\d+)\.xlsx$', filename)
        row_number = match.group(1) if match else '1'
        
        try:
            # Читаем Excel файл
            df = pd.read_excel(file_path)
            
            # Ищем соответствующий JSON файл
            json_filename = f"Нормализованные_данные_элемента_{row_number}.json"
            json_path = os.path.join(input_folder, json_filename)
            
            # Значения по умолчанию
            ifc_class = ''
            name_elem = ''
            global_id = ''
            
            # Если JSON существует, читаем из него значения
            if os.path.exists(json_path):
                with open(json_path, 'r', encoding='utf-8') as file:
                    try:
                        data = json.load(file)
                        prev_data = data.get('исходные_данные', '')
                        ifc_class = prev_data.get('Тип элемента', '')
                        global_id = prev_data.get('GlobalId', '')
                        name_elem = prev_data.get('Имя', '')
                        
                        logger.info(f"Данные из JSON для {filename}:")
                        logger.info(f"  ifc_class: {ifc_class}")
                        logger.info(f"  name_element: {name_elem}")
                        logger.info(f"  global_id: {global_id}")
                        
                    except json.JSONDecodeError:
                        logger.warning(f"Ошибка чтения JSON: {json_filename}")
            else:
                logger.warning(f"JSON файл не найден: {json_filename}")
            
            # Создаем строку со значениями переменных
            separator_row = {}
            separator_row[all_columns[0]] = ifc_class + " " + name_elem + " " + global_id
            
            # Остальные колонки оставляем пустыми
            for col in all_columns[1:]:
                separator_row[col] = ''
            
            empty_row = pd.DataFrame([[' '] * num_columns], columns=all_columns)
            
            separator = pd.DataFrame([separator_row])
            
            # Добавляем разделитель и данные
            all_parts.append(separator)
            all_parts.append(df)
            all_parts.append(empty_row)
            
            print(f"✓ {filename} - {len(df)} строк(и)")
            print(f"  ifc_class: {ifc_class}, name: {name_elem}, id: {global_id}")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке {filename}: {e}")
            print(f"✗ Ошибка: {filename} - {e}")
    
    if all_parts:
        # Объединяем все части
        result = pd.concat(all_parts, ignore_index=True)
        
        # Сохраняем результат
        result.to_excel(output_file, index=False)
        
        print("-" * 50)
        print(f"✅ ОБЪЕДИНЕНИЕ ЗАВЕРШЕНО!")
        print(f"📁 Объединено файлов: {len(excel_files)}")
        print(f"📊 Всего строк: {len(result)}")
        print(f"💾 Сохранено: {output_file}")
        
        return result
    else:
        logger.warning("Нет данных для объединения!")
        return None


def fourth_step(input_folder):
    """Четвёртый этап: подбор работ по материалу + базе знаний"""
    logger.info("НАЧАТ ЧЕТВЁРТЫЙ ЭТАП")

    count = sum(1 for f in os.listdir(input_folder)
                if f.endswith('.json') and f.startswith('Нормализованные_данные'))

    logger.info(f"Найдено файлов: {count}")

    for filename in os.listdir(input_folder):
        if filename.endswith('.json') and filename.startswith('Нормализованные_данные'):
            file_path = os.path.join(input_folder, filename)
            match = re.search(r'(\d+)(?=\.json$)', filename)
            row_number = match.group(1) if match else '1'

            with open(file_path, 'r', encoding='utf-8') as file:
                logger.info(f"Загрузка нормализованных данных из {filename}")
                try:
                    data = json.load(file)
                    _process_one_element(data, row_number, input_folder)
                    logger.info(f"Обработан файл: {filename}")
                except json.JSONDecodeError:
                    logger.warning(f"Ошибка чтения JSON в файле: {filename}")
    
    merge_final_worklists(input_folder)


    logger.info("ЧЕТВЁРТЫЙ ЭТАП ЗАВЕРШЕН")