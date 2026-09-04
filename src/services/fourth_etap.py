# ============================================================
# ЭТАП 4: ПОДБОР РАБОТ
# ============================================================
#
# Логика:
#
# JSON элемента
#       |
#       +--> IFC
#       +--> МССК -> find_name_by_code()
#       +--> материалы из нескольких источников
#       +--> имя элемента
#       +--> характеристики
#       |
#       v
# расширенные ключевые слова
#       |
#       v
# БАЗА ЗНАНИЙ AR / KR
#       |
#       v
# поиск кандидатов
#       |
#       +--> точный поиск
#       +--> морфологический поиск
#       +--> нечёткий поиск
#       +--> поиск по МССК
#       +--> поиск по материалу
#       +--> поиск по фразам базы знаний
#       |
#       v
# рейтинг кандидатов
#       |
#       v
# geometry_filter()
#       |
#       v
# LLM
#       |
#       v
# проверка required
#       |
#       v
# финальный Excel + JSON
#
# ============================================================

import pandas as pd
import json
import re
import os
import math
import pymorphy3
import ollama
from fuzzywuzzy import fuzz
from src.core.config import load_config
from src.core.logger import setup_logger
from src.services.base_knowledge import KNOWLEDGE_BASE
from src.services.geometry_filter import geometry_filter

# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

logger = setup_logger("fourth_step")
_cfg = load_config()
LLM_MODEL = _cfg.model_ollama
OLLAMA_URL = _cfg.ollama_url
KOEFS_FILE = _cfg.KOEFS_PATH
PRICE_COST_FILE = _cfg.PRICE_COST_PATH
MSSK_FILE = _cfg.MSSK_EXCEL_PATH

# ============================================================
# НАСТРОЙКИ ПОИСКА
# ============================================================

SIMILARITY_THRESHOLD = 80  # Порог для нечёткого поиска
MAX_CANDIDATES = 100       # Максимум кандидатов для geometry_filter
MAX_LLM_CANDIDATES = 100   # Максимум кандидатов для LLM
LLM_TEMPERATURE = 0.1      # Температура LLM
MAX_LLM_ATTEMPTS = 2       # Попытки LLM

_price_cost_lookup_cache = None
_price_material_lookup_cache = None

morph = pymorphy3.MorphAnalyzer()

STOP_WORDS = {
    "и", "в", "во", "на", "с", "со", "по", "к", "ко", "у", "о", "об", "от", "из",
    "за", "под", "над", "без", "до", "при", "через", "для", "как",
    "состав", "слой", "материал", "конструкция", "базовая", "элемент", "работа",
    "не", "указано", "указана", "указан",
    "подземная", "подземный", "подземное",
    "надземная", "надземный", "надземное",
    "цоколь", "кровля", "подвал", "мансарда", "техническая", "основной", "основная", "основное",
    "этаж", "этажа", "этажный",
    "актовый", "зал"
}

SYNONYMS = {
    # ОКНА
    "окно": ["окна", "оконный", "оконная", "оконные", "оконных", "оконным", "оконного", "оконную"],
    "окна": ["окно", "оконный", "оконная", "оконные", "оконных"],
    # ДВЕРИ
    "дверь": ["двери", "дверной", "дверная", "дверные", "дверных", "дверным", "дверного", "дверной блок", "дверные блоки"],
    "двери": ["дверь", "дверной", "дверная", "дверные", "дверных"],
    # АЛЮМИНИЙ
    "алюминий": ["алюминиевый", "алюминиевая", "алюминиевые", "алюминиевых", "алюминиевым", "алюминиевого"],
    "алюминиевый": ["алюминий", "алюминиевые", "алюминиевая", "алюминиевых"],
    # БЕТОН
    "бетон": ["бетонный", "бетонная", "бетонное", "бетонные", "бетонных", "железобетон", "железобетонный", "железобетонная", "железобетонные"],
    "железобетон": ["бетон", "бетонный", "железобетонный", "железобетонные"],
    # КИРПИЧ
    "кирпич": ["кирпичный", "кирпичная", "кирпичное", "кирпичные", "кирпичных"],
    "кирпичный": ["кирпич", "кирпичная", "кирпичные", "кирпичных"],
    # ГАЗОБЕТОН
    "газобетон": ["газобетонный", "газобетонная", "газобетонное", "газобетонные", "газобетонных"],
    # МЕТАЛЛ
    "металл": ["металлический", "металлическая", "металлическое", "металлические", "металлических", "сталь", "стальной", "стальная", "стальные"],
    "сталь": ["стальной", "стальная", "стальные", "металл", "металлический"],
    # СТЕКЛО
    "стекло": ["стеклянный", "стеклянная", "стеклянное", "стеклянные", "стеклопакет", "стеклопакеты"],
    # ПЛАСТИК
    "пластик": ["пластиковый", "пластиковая", "пластиковое", "пластиковые", "пвх", "pvc"],
    "пвх": ["пластик", "пластиковый", "пластиковая", "pvc"],
    # ДЕРЕВО
    "дерево": ["деревянный", "деревянная", "деревянное", "деревянные", "деревянных"],
    # УТЕПЛЕНИЕ
    "утепление": ["утеплитель", "теплоизоляция", "теплоизоляционный", "теплоизоляционная", "теплоизоляционные"],
    # ГИДРОИЗОЛЯЦИЯ
    "гидроизоляция": ["гидроизоляционный", "гидроизоляционная", "гидроизоляционные"],
    # ПАРОИЗОЛЯЦИЯ
    "пароизоляция": ["пароизоляционный", "пароизоляционная", "пароизоляционные"],
    # ШТУКАТУРКА
    "штукатурка": ["штукатурный", "штукатурная", "штукатурные", "оштукатуривание", "оштукатурить"],
    # ОКРАСКА
    "окраска": ["окрасочный", "окрасочная", "окрашивание", "окрасить", "покраска", "покрытие"],
    # АРМАТУРА
    "арматура": ["армирование", "армировать", "арматурный", "арматурная", "арматурные", "армированный"],
    "армирование": ["арматура", "армировать", "арматурный", "арматурная", "арматурные"],
    # ОПАЛУБКА
    "опалубка": ["опалубочный", "опалубочная", "распалубка", "распалубливание"],
    # ПОДОКОННИК
    "подоконник": ["подоконники", "подоконного", "подоконным"],
    # ОТЛИВ
    "отлив": ["отливы", "отливов", "отливом"],
    # ОТКОС
    "откос": ["откосы", "откосов", "откосом"],
    # ОГРАЖДЕНИЕ
    "ограждение": ["ограждения", "ограждений", "ограждающий", "ограждающие"]
}

# ============================================================
# ВЕСА ПОИСКА
# ============================================================

SCORE_EXACT = 20        # Точное совпадение
SCORE_MORPHOLOGY = 18   # Морфологическое совпадение
SCORE_KEYWORD = 20      # Ключевое слово
SCORE_MATERIAL = 35     # Материал
SCORE_MSSK = 35         # МССК
SCORE_IFC = 25          # IFC
SCORE_FUZZY = 5         # Нечёткий поиск
SCORE_KB = 70           # База знаний (фраза)
SCORE_RECOMMENDED = 80  # Рекомендуемые работы
SCORE_REQUIRED = 1000   # Обязательные работы

# ============================================================
# PROMPT AR
# ============================================================

PROMPT_AR = """
Ты — эксперт-сметчик по архитектурным решениям.

Твоя задача — выбрать наиболее подходящие работы для создания
или устройства указанного архитектурного элемента.

{element_info}

## ОСНОВНЫЕ ПРАВИЛА

1. Ты можешь выбирать работы ТОЛЬКО из списка доступных работ.
2. Нельзя придумывать новые названия работ.
3. Название выбранной работы должно полностью совпадать
   с названием в списке доступных работ.
4. Не добавляй работу, которой нет в списке.
5. Учитывай одновременно:
   - тип IFC;
   - тип элемента;
   - код МССК;
   - название элемента по МССК;
   - все найденные материалы;
   - характеристики материала;
   - имя элемента;
   - размеры;
   - правила базы знаний.

## ОСОБЕННОСТИ АР

- Для стен учитывай отделку: штукатурка, шпаклёвка, окраска,
  облицовка, обои.
- Для перекрытий учитывай устройство полов: стяжка, покрытие,
  гидроизоляция, подложка.
- Для дверей учитывай монтаж дверных блоков, коробок, полотен,
  фурнитуры, откосов, наличников и окраску.
- Для окон учитывай монтаж оконных блоков, откосы, подоконники,
  отливы, герметизацию и другие относящиеся к элементу работы.
- Для кровель учитывай кровельное покрытие, утепление,
  гидроизоляцию, пароизоляцию и водосточную систему.
- Учитывай фактический материал элемента.
- Не добавляй работы только потому, что они типичны для элемента,
  если они противоречат указанному материалу.

## БАЗА ЗНАНИЙ

Работы из списка "ОБЯЗАТЕЛЬНЫЕ" должны быть выбраны,
если они присутствуют среди доступных работ.

Работы из списка "РЕКОМЕНДУЕМЫЕ" следует выбирать,
если они действительно относятся к данному элементу.

Работы из списка "ЗАПРЕЩЁННЫЕ" выбирать нельзя.

## ДОСТУПНЫЕ РАБОТЫ

{works_text}

## ФОРМАТ ОТВЕТА

Верни ТОЛЬКО JSON:

{{
  "выбранные_работы": [
    {{
      "наименование": "ТОЧНОЕ НАЗВАНИЕ ИЗ СПИСКА",
      "обоснование": "почему работа подходит",
      "категория": "подготовительные/монтажные/отделочные/изоляционные/фасадные/заполнение проёмов/остекление/другие"
    }}
  ],
  "рекомендация": "краткий вывод"
}}
"""

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_text(value):
    """Приведение текста к удобному для поиска виду."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return " ".join(normalize_text(x) for x in value if x is not None)
    if isinstance(value, dict):
        return " ".join(f"{normalize_text(k)} {normalize_text(v)}" for k, v in value.items() if v is not None)
    text = str(value).strip()
    text = text.replace("ё", "е").replace("Ё", "Е")
    return text

def unique_keep_order(items):
    """Удаляет дубли, сохраняя порядок."""
    result = []
    seen = set()
    for item in items:
        item = normalize_text(item)
        if not item:
            continue
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

def normalize_quotes(text):
    """Заменяет типографские кавычки на обычные."""
    if not isinstance(text, str):
        return text
    text = (text
            .replace("«", '"')
            .replace("»", '"')
            .replace("„", '"')
            .replace("“", '"')
            .replace("”", '"')
            .replace("\n", " "))
    return text

# ============================================================
# МССК
# ============================================================

def find_name_by_code(path, target_code):
    """
    Поиск названия по коду МССК.
    Берём код из первой колонки Excel.
    Название ищем среди следующих непустых ячеек.
    Если значение начинается с Ifc — игнорируем его.
    """
    df = pd.read_excel(path, header=None)
    target_code = " ".join(str(target_code).upper().split())
    for row in df.values.tolist():
        if not row:
            continue
        cell_code = row[0]
        if cell_code is None or pd.isna(cell_code):
            continue
        clean_code = " ".join(str(cell_code).upper().split())
        if clean_code == target_code:
            for cell in row[1:]:
                if cell is None or pd.isna(cell):
                    continue
                value = str(cell).strip()
                if not value or value.lower() == "nan":
                    continue
                if not value.startswith("Ifc"):
                    return value
            return "Название не найдено (в строке пусто)"
    return "Код не найден в таблице"

def get_element_mssk_info(normalized_data):
    source = normalized_data.get("исходные_данные", {}) or {}
    mssk_code = (source.get("Код мсск") or source.get("Код МССК") or source.get("МССК") or "")
    mssk_code = normalize_text(mssk_code)
    if not mssk_code:
        return {"code": "", "name": ""}
    if not MSSK_FILE:
        logger.warning("Путь к файлу МССК не задан.")
        return {"code": mssk_code, "name": ""}
    if not os.path.exists(MSSK_FILE):
        logger.warning("Файл МССК не найден: %s", MSSK_FILE)
        return {"code": mssk_code, "name": ""}
    try:
        name = find_name_by_code(MSSK_FILE, mssk_code)
    except Exception as e:
        logger.error("Ошибка поиска названия МССК %s: %s", mssk_code, e)
        name = ""
    if name in {"Код не найден в таблице", "Название не найдено (в строке пусто)"}:
        name = ""
    return {"code": mssk_code, "name": normalize_text(name)}

# ============================================================
# ЧИСЛА
# ============================================================

def safe_float(value, default=0.0):
    """
    Безопасное преобразование значения во float.
    Поддерживает: 123.45, 123,45, 1 234,56 мм, 1.62 м³
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return default
        return float(value)
    if isinstance(value, (list, tuple, dict, set)):
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    if not text or text.lower() in {"nan", "inf", "-inf"}:
        return default
    text = text.replace("\xa0", " ").replace("\t", " ")
    if "," in text and "." in text:
        last_dot = text.rfind(".")
        last_comma = text.rfind(",")
        if last_dot > last_comma:
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    pattern = r"(-?[\d]+(?:\.[\d]+)?(?:[eE][+-]?[\d]+)?)"
    match = re.search(pattern, text)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            pass
    return default

# ============================================================
# МАТЕРИАЛЫ
# ============================================================

def collect_materials(normalized_data):
    """Собирает материал из всех возможных источников JSON."""
    materials = []
    material = normalized_data.get("материал", {}) or {}
    source = normalized_data.get("исходные_данные", {}) or {}

    # Основное название материала
    values = [
        material.get("название", ""),
        normalized_data.get("материал_определенный", ""),
        source.get("Материал", "")
    ]
    for value in values:
        value = normalize_text(value)
        if value and value.casefold() not in {"не указано", "не указан", "-"}:
            materials.append(value)

    # IfcMaterialLayer::Name
    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                yield str(key), value
                yield from walk(value)
        elif isinstance(obj, list):
            for value in obj:
                yield "", value

    for key, value in walk(normalized_data):
        if value and "ifcmateriallayer::name" in str(key).lower():
            value = normalize_text(value)
            if value:
                materials.append(value)

    # Состав из имени
    composition = normalize_text(normalized_data.get("состав_из_имени", ""))
    if composition:
        materials.append(composition)

    # Характеристики материала
    for container_name in ["качественные_характеристики", "количественные_характеристики"]:
        container = material.get(container_name, {}) or {}
        if isinstance(container, dict):
            for key, value in container.items():
                value = normalize_text(value)
                if value:
                    materials.append(f"{key}: {value}")

    return unique_keep_order(materials)

# ============================================================
# ИЗВЛЕЧЕНИЕ КЛЮЧЕВЫХ СЛОВ
# ============================================================

def extract_words(text):
    if not text:
        return []
    text = normalize_text(text)

    # Технические обозначения
    technical = re.findall(
        r"(?i)\b(?:B\d{1,3}|W\d{1,3}|F\d{1,4}|M\d{2,4}|RAL\s*\d{3,5}|"
        r"ПВХ|PVC|ГКЛ|ГВЛ|ОСБ|ЦСП|Ifc[A-Za-z]+)\b",
        text
    )

    # Обычные слова
    raw_words = re.findall(
        r"[А-Яа-яA-Za-z]{3,}(?:-[А-Яа-яA-Za-z]{2,})?|"
        r"[A-Za-zА-Яа-я]+\d+[A-Za-zА-Яа-я\d]*|\d+[A-Za-zА-Яа-я]+",
        text
    )

    result = []
    for word in raw_words + technical:
        word = word.strip(' .,:;()[]{}"\'').lower()
        if not word:
            continue
        # Чисто цифровые значения
        if word.isdigit():
            if len(word) >= 3:
                result.append(word)
            continue
        if re.fullmatch(r"[а-яa-z-]+", word) and word in STOP_WORDS:
            continue
        result.append(word)

        # Морфологическая нормальная форма
        if re.fullmatch(r"[а-яa-z-]+", word) and len(word) >= 4:
            try:
                parsed = morph.parse(word)[0]
                lemma = parsed.normal_form.lower()
                if lemma not in STOP_WORDS and len(lemma) >= 3:
                    result.append(lemma)
            except Exception:
                pass

        # Синонимы
        for synonym in SYNONYMS.get(word, []):
            synonym = synonym.lower()
            if synonym not in STOP_WORDS:
                result.append(synonym)

    return unique_keep_order(result)

def extract_element_search_words(normalized_data, ifc_type, element_type, element_name, mssk_name, materials, composition):
    """Формирует расширенный набор ключевых слов из всей информации об элементе."""
    fields = []
    fields.append(ifc_type)
    fields.append(element_type)
    fields.append(mssk_name)
    fields.append(element_name)
    fields.append(composition)
    fields.extend(materials)

    source = normalized_data.get("исходные_данные", {}) or {}
    material = normalized_data.get("материал", {}) or {}

    # Свойства исходных данных, связанные с материалом
    for key, value in source.items():
        if not value:
            continue
        key_lower = str(key).lower()
        if any(term in key_lower for term in ["material", "материал", "ral", "concrete", "армат", "steel", "бетон"]):
            fields.append(key)
            fields.append(value)

    # Характеристики материала
    for container_name in ["качественные_характеристики", "количественные_характеристики"]:
        container = material.get(container_name, {}) or {}
        if isinstance(container, dict):
            for key, value in container.items():
                fields.append(key)
                fields.append(value)

    result = []
    for field in fields:
        result.extend(extract_words(field))
    return unique_keep_order(result)

# ============================================================
# БАЗА ЗНАНИЙ
# ============================================================

def get_kb_rules(processing_type, ifc_type, mssk_name, materials):
    """
    Получает правила из новой структуры:
    KNOWLEDGE_BASE
        AR/KR
            IFC
                works_rules
                mssk
                material
    """
    processing_type = str(processing_type).upper()
    section = KNOWLEDGE_BASE.get(processing_type, {}) or {}
    ifc_rules = section.get(ifc_type, {}) or {}

    search = []
    required = []
    recommended = []
    forbidden = []
    sources = []

    def add_rules(block, source_name):
        if not isinstance(block, dict):
            return
        search.extend(block.get("search", []) or [])
        required.extend(block.get("required", []) or [])
        recommended.extend(block.get("recommended", []) or [])
        forbidden.extend(block.get("forbidden", []) or [])
        sources.append(source_name)

    # Общие правила IFC
    add_rules(ifc_rules.get("works_rules", {}), f"IFC:{ifc_type}")

    # МССК
    mssk_rules = ifc_rules.get("mssk", {}) or {}
    mssk_lower = normalize_text(mssk_name).casefold()
    if mssk_lower:
        for mssk_key, rules in mssk_rules.items():
            key_lower = normalize_text(mssk_key).casefold()
            if key_lower == mssk_lower or key_lower in mssk_lower or mssk_lower in key_lower:
                add_rules(rules, f"MSSK:{mssk_key}")

    # Материал
    material_rules = ifc_rules.get("material", {}) or {}
    material_text = " ".join(normalize_text(x) for x in materials).casefold()
    for material_key, rules in material_rules.items():
        if not isinstance(rules, dict):
            continue
        aliases = rules.get("aliases", [material_key]) or []
        aliases = [normalize_text(x).casefold() for x in aliases]
        material_key_lower = normalize_text(material_key).casefold()
        matched = False
        for alias in aliases:
            if alias and alias in material_text:
                matched = True
                break
        if not matched and material_key_lower and material_key_lower in material_text:
            matched = True
        if matched:
            add_rules(rules, f"MATERIAL:{material_key}")

    return {
        "search": unique_keep_order(search),
        "required": unique_keep_order(required),
        "recommended": unique_keep_order(recommended),
        "forbidden": unique_keep_order(forbidden),
        "sources": unique_keep_order(sources)
    }

# ============================================================
# ПОИСК КАНДИДАТА
# ============================================================

def _ensure_candidate(storage, work_name):
    if work_name not in storage:
        storage[work_name] = {
            "наименование": work_name,
            "score": 0,
            "совпадения": [],
            "тип_поиска": [],
            "matches": {
                "ifc": [],
                "mssk": [],
                "material": [],
                "keywords": [],
                "knowledge": [],
                "recommended": [],
                "required": []
            }
        }
    return storage[work_name]

def add_candidate(storage, work_name, score, search_type, source, match=None):
    item = _ensure_candidate(storage, work_name)
    item["score"] += score
    if search_type not in item["тип_поиска"]:
        item["тип_поиска"].append(search_type)
    if match:
        match = normalize_text(match)
        if match:
            if match not in item["совпадения"]:
                item["совпадения"].append(match)
            if source in item["matches"]:
                if match not in item["matches"][source]:
                    item["matches"][source].append(match)

# ============================================================
# ФОРМЫ СЛОВ
# ============================================================

def build_word_forms(words):
    all_forms = {}
    for word in words:
        word = normalize_text(word).lower()
        if not word:
            continue
        forms = {word}
        try:
            parsed = morph.parse(word)[0]
            for form in parsed.lexeme:
                form_word = form.word.lower()
                if form_word:
                    forms.add(form_word)
        except Exception as e:
            logger.warning('Не удалось получить формы слова "%s": %s', word, e)
        # Синонимы
        for synonym in SYNONYMS.get(word, []):
            forms.add(synonym.lower())
        all_forms[word] = list(forms)
    return all_forms

# ============================================================
# ПОИСК РАБОТ
# ============================================================

def search_work_candidates(df_works, search_col, all_words, ifc_type, mssk_name, materials, kb_rules):
    all_found_works = {}

    # Предварительно собираем названия
    work_rows = []
    for idx, row in df_works.iterrows():
        value = row.get(search_col)
        if pd.isna(value):
            continue
        work_name = normalize_text(value)
        if len(work_name) < 3:
            continue
        work_rows.append((idx, work_name, work_name.lower()))

    # Формы слов
    all_forms = build_word_forms(all_words)

    # Точный + морфологический поиск
    for word, forms in all_forms.items():
        for _, work_name, work_lower in work_rows:
            matched = []
            for form in forms:
                if not form:
                    continue
                # Для коротких технических обозначений ищем как отдельный фрагмент
                if len(form) <= 3:
                    pattern = r"(?<![A-Za-zА-Яа-я0-9])" + re.escape(form) + r"(?![A-Za-zА-Яа-я0-9])"
                    if re.search(pattern, work_lower):
                        matched.append(form)
                else:
                    if form in work_lower:
                        matched.append(form)
            if not matched:
                continue
            # Основное слово
            if word in matched:
                add_candidate(all_found_works, work_name, SCORE_EXACT, "точный", "keywords", word)
            # Другие морфологические формы
            else:
                for form in matched:
                    add_candidate(all_found_works, work_name, SCORE_MORPHOLOGY, "морфологический", "keywords", form)

    # Нечёткий поиск
    for word in all_words:
        if len(word) < 4:  # Слишком короткие слова дают много мусора
            continue
        for _, work_name, work_lower in work_rows:
            similarity = fuzz.partial_ratio(word, work_lower)
            if similarity >= SIMILARITY_THRESHOLD:
                fuzzy_score = SCORE_FUZZY
                if similarity >= 90:
                    fuzzy_score += 5
                if similarity >= 95:
                    fuzzy_score += 5
                add_candidate(all_found_works, work_name, fuzzy_score, "нечеткий", "keywords", f"{word} ({similarity}%)")

    # Поиск по IFC
    ifc_search_phrases = (KNOWLEDGE_BASE
                          .get(str(processing_type_global).upper(), {})
                          .get(ifc_type, {})
                          .get("works_rules", {})
                          .get("search", []))
    for phrase in ifc_search_phrases:
        phrase_lower = normalize_text(phrase).lower()
        if not phrase_lower:
            continue
        for _, work_name, work_lower in work_rows:
            if phrase_lower in work_lower:
                add_candidate(all_found_works, work_name, SCORE_IFC, "база_IFC", "ifc", phrase)

    # Поиск по МССК
    if mssk_name:
        mssk_words = extract_words(mssk_name)
        mssk_forms = build_word_forms(mssk_words)
        for word, forms in mssk_forms.items():
            for _, work_name, work_lower in work_rows:
                matched = [form for form in forms if len(form) >= 3 and form in work_lower]
                for form in matched:
                    add_candidate(all_found_works, work_name, SCORE_MSSK, "МССК", "mssk", form)

    # Поиск по материалу
    material_words = []
    for material in materials:
        material_words.extend(extract_words(material))
    material_words = unique_keep_order(material_words)
    material_forms = build_word_forms(material_words)
    for word, forms in material_forms.items():
        for _, work_name, work_lower in work_rows:
            matched = [form for form in forms if len(form) >= 3 and form in work_lower]
            for form in matched:
                add_candidate(all_found_works, work_name, SCORE_MATERIAL, "материал", "material", form)

    # Фразы базы знаний
    knowledge_phrases = kb_rules["search"]
    for phrase in knowledge_phrases:
        phrase_lower = normalize_text(phrase).lower()
        if not phrase_lower:
            continue
        for _, work_name, work_lower in work_rows:
            if phrase_lower in work_lower:
                add_candidate(all_found_works, work_name, SCORE_KB, "база_знаний_фраза", "knowledge", phrase)

    # Рекомендуемые
    for phrase in kb_rules["recommended"]:
        phrase_lower = normalize_text(phrase).lower()
        if not phrase_lower:
            continue
        for _, work_name, work_lower in work_rows:
            if phrase_lower in work_lower:
                add_candidate(all_found_works, work_name, SCORE_RECOMMENDED, "рекомендуемая", "recommended", phrase)

    # Обязательные
    for phrase in kb_rules["required"]:
        phrase_lower = normalize_text(phrase).lower()
        if not phrase_lower:
            continue
        for _, work_name, work_lower in work_rows:
            if phrase_lower in work_lower:
                add_candidate(all_found_works, work_name, SCORE_REQUIRED, "обязательная", "required", phrase)

    # Удаляем запрещённые
    forbidden = [normalize_text(x).lower() for x in kb_rules["forbidden"]]
    if forbidden:
        filtered = {}
        for work_name, data in all_found_works.items():
            work_lower = work_name.lower()
            is_forbidden = any(phrase in work_lower for phrase in forbidden)
            if not is_forbidden:
                filtered[work_name] = data
        all_found_works = filtered

    return all_found_works

# ============================================================
# СОРТИРОВКА КАНДИДАТОВ
# ============================================================

def sort_candidates(candidates):
    result = list(candidates.items())
    result.sort(key=lambda item: (item[1].get("score", 0), len(item[1].get("совпадения", []))), reverse=True)
    return [item[1] for item in result]

# ============================================================
# РАЗМЕРЫ / ГЕОМЕТРИЯ
# ============================================================

def prepare_works_for_geometry(works):
    result = []
    for work in works:
        name = work["наименование"]
        search_types = ", ".join(work.get("тип_поиска", []))
        result.append(f"{name} [источник: {search_types}]")
    return result

def restore_geometry_results(geometry_result, candidates):
    if not geometry_result:
        return []
    candidate_by_name = {item["наименование"]: item for item in candidates}
    result = []
    for item in geometry_result:
        text = normalize_text(item)
        # Убираем номер перед названием
        text_without_number = re.sub(r"^\s*\d+\.\s*", "", text)
        # Убираем [источник: ...]
        clean_name = re.sub(r"\s*\[источник:.*?\]\s*$", "", text_without_number, flags=re.IGNORECASE).strip()
        # Сначала точное совпадение
        if clean_name in candidate_by_name:
            result.append(candidate_by_name[clean_name])
            continue
        # Если geometry_filter изменил строку, пробуем найти исходное название внутри
        for name, candidate in candidate_by_name.items():
            if (name.lower() in clean_name.lower() or clean_name.lower() in name.lower()):
                result.append(candidate)
                break
    return result

# ============================================================
# ОБЯЗАТЕЛЬНЫЕ РАБОТЫ
# ============================================================

def find_required_candidates(candidates, required_phrases):
    result = []
    for required in required_phrases:
        required_lower = normalize_text(required).lower()
        if not required_lower:
            continue
        for candidate in candidates:
            name = candidate["наименование"].lower()
            if required_lower in name:
                result.append(candidate)
                break
    # Удаляем дубли
    unique = {candidate["наименование"]: candidate for candidate in result}
    return list(unique.values())

def validate_required_works(selected_works, required_phrases):
    """
    Проверяет ответ LLM.
    Возвращает список обязательных работ, которые LLM пропустила.
    """
    if not required_phrases:
        return []
    selected_names = []
    for work in selected_works:
        if not isinstance(work, dict):
            continue
        name = normalize_text(work.get("наименование", "")).lower()
        if name:
            selected_names.append(name)
    missing = []
    for required in required_phrases:
        required_lower = normalize_text(required).lower()
        found = False
        for selected in selected_names:
            if required_lower in selected or selected in required_lower:
                found = True
                break
        if not found:
            missing.append(required)
    return missing

# ============================================================
# JSON ОТВЕТ LLM
# ============================================================

def clean_llm_answer(answer):
    if not answer:
        return ""
    answer = answer.strip()
    if answer.startswith("```json"):
        answer = answer[len("```json"):]
    elif answer.startswith("```"):
        answer = answer[len("```"):]
    if answer.endswith("```"):
        answer = answer[:-3]
    return answer.strip()

def parse_llm_json(answer):
    answer = clean_llm_answer(answer)
    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        # Иногда модель пишет что-то до или после JSON
        start = answer.find("{")
        end = answer.rfind("}")
        if start >= 0 and end > start:
            json_text = answer[start:end + 1]
            return json.loads(json_text)
        raise

# ============================================================
# PROMPT ДЛЯ ПОВТОРНОЙ ПРОВЕРКИ
# ============================================================

def build_retry_prompt(original_prompt, missing_required):
    missing_text = "\n".join(f"- {x}" for x in missing_required)
    return original_prompt + f"""

## КРИТИЧЕСКАЯ ПРОВЕРКА

Предыдущий ответ был отклонён.

Ты пропустил следующие обязательные работы:

{missing_text}

Теперь повтори выбор.

Каждая из указанных обязательных работ должна присутствовать
в "выбранные_работы", ЕСЛИ она присутствует в списке
доступных работ.

Напоминаю:
- нельзя придумывать работы;
- название должно полностью совпадать со списком;
- выбирать можно только из списка доступных работ.

Верни ТОЛЬКО JSON.
"""

# ============================================================
# ВЫЗОВ LLM
# ============================================================

def select_works_by_llm(prompt, required_phrases):
    client = ollama.Client(host=OLLAMA_URL, timeout=120.0)
    last_answer = ""
    last_result = None

    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        current_prompt = prompt

        if attempt > 1 and last_result is not None:
            selected_previous = last_result.get("выбранные_работы", [])
            missing = validate_required_works(selected_previous, required_phrases)
            if missing:
                current_prompt = build_retry_prompt(prompt, missing)

        try:
            logger.info("Запрос LLM, попытка %s/%s", attempt, MAX_LLM_ATTEMPTS)
            response = client.chat(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": current_prompt}],
                options={"temperature": LLM_TEMPERATURE}
            )
            last_answer = response["message"]["content"].strip()
            result = parse_llm_json(last_answer)

            if not isinstance(result, dict):
                raise ValueError("Ответ LLM не является JSON-объектом")

            selected_works = result.get("выбранные_работы", [])
            if not isinstance(selected_works, list):
                raise ValueError("Поле выбранные_работы не является списком")

            last_result = result
            missing = validate_required_works(selected_works, required_phrases)

            if not missing:
                logger.info("Ответ LLM прошёл проверку обязательных работ.")
                return result

            logger.warning("LLM пропустил обязательные работы: %s", missing)

        except Exception as e:
            logger.error("Ошибка LLM, попытка %s: %s", attempt, e)
            if last_answer:
                logger.error("Ответ LLM: %s", last_answer[:1000])

    # Если обязательные работы так и не прошли проверку, возвращаем последний результат
    if last_result is not None:
        return last_result

    return {
        "выбранные_работы": [],
        "рекомендация": "Не удалось получить корректный ответ LLM."
    }

# ============================================================
# ИСПРАВЛЕНИЕ ОБЯЗАТЕЛЬНЫХ РАБОТ
# ============================================================

def force_add_missing_required(selected_works, candidates, required_phrases):
    """
    Если LLM всё-таки пропустила обязательную работу,
    добавляем её программно, но только если такая работа
    реально есть среди кандидатов.
    """
    if not required_phrases:
        return selected_works

    selected_names = set()
    for work in selected_works:
        if not isinstance(work, dict):
            continue
        selected_names.add(normalize_text(work.get("наименование", "")).casefold())

    candidate_by_name = {
        normalize_text(x["наименование"]).casefold(): x
        for x in candidates
    }

    for required in required_phrases:
        required_lower = normalize_text(required).casefold()
        found = False

        for selected_name in selected_names:
            if required_lower in selected_name or selected_name in required_lower:
                found = True
                break

        if found:
            continue

        # Ищем среди кандидатов
        for candidate_name, candidate in candidate_by_name.items():
            if required_lower in candidate_name:
                selected_works.append({
                    "наименование": candidate["наименование"],
                    "обоснование": "Добавлено программно как обязательная работа базы знаний.",
                    "категория": "другие"
                })
                selected_names.add(candidate_name)
                logger.warning("Обязательная работа добавлена программно: %s", candidate["наименование"])
                break

    return selected_works

# ============================================================
# КОРРЕКТИРОВКА ОБЪЁМА
# ============================================================

def _get_corrected_volume(df):
    koefs = pd.read_excel(KOEFS_FILE)
    df_copy = df.copy()

    if "Шифр ТСН" not in df_copy.columns:
        return df_copy
    if "Наименование расценки/ресурса" not in df_copy.columns:
        return df_copy

    koefs_filtered = koefs[koefs["Шифр ТСН"].isin(df_copy["Шифр ТСН"])].copy()
    resource_col = "Наименование открытой группы ресурсов/\nресурса в составе открытой группы"

    if resource_col not in koefs_filtered.columns:
        return df_copy

    koefs_filtered_by_material = koefs_filtered[
        koefs_filtered[resource_col].isin(df_copy["Наименование расценки/ресурса"])
    ].copy()

    for _, koef_row in koefs_filtered_by_material.iterrows():
        resource_name = koef_row[resource_col]
        matching_indices = df_copy[
            df_copy["Наименование расценки/ресурса"] == resource_name
        ].index

        if len(matching_indices) == 0:
            continue

        df_idx = matching_indices[0]

        if "Объём работ" not in df_copy.columns:
            continue

        norm = safe_float(koef_row.get("Норма расхода", 0))
        volume = safe_float(df_copy.loc[df_idx, "Объём работ"])

        if norm > 100:
            df_copy.loc[df_idx, "Объём работ"] = volume * norm / 100
        elif norm:
            df_copy.loc[df_idx, "Объём работ"] = volume * norm

    return df_copy

# ============================================================
# СТОИМОСТЬ
# ============================================================

def _get_price_material_cost():
    global _price_material_lookup_cache

    if _price_material_lookup_cache is not None:
        return _price_material_lookup_cache

    try:
        df = pd.read_excel(PRICE_COST_FILE, sheet_name="_Связаанные_ресурсы_select_wp_p")
        _price_material_lookup_cache = dict(
            zip(df["Шифр ресурса"], df["Сметная цена текущая"])
        )
        logger.info("Загружено %s расценок из price_cost.xlsx (ресурсы)", len(_price_material_lookup_cache))
    except Exception as e:
        logger.error("Ошибка загрузки price_cost.xlsx (ресурсы): %s", e)
        _price_material_lookup_cache = {}

    return _price_material_lookup_cache

def _get_price_cost_lookup():
    global _price_cost_lookup_cache

    if _price_cost_lookup_cache is not None:
        return _price_cost_lookup_cache

    try:
        df = pd.read_excel(PRICE_COST_FILE, sheet_name="_Получние_параметры_позиции_sel")
        _price_cost_lookup_cache = dict(
            zip(df["Шифр расценки"], df["Текущие прямые затраты/Всего затр"])
        )
        logger.info("Загружено %s расценок из price_cost.xlsx (расценки)", len(_price_cost_lookup_cache))
    except Exception as e:
        logger.error("Ошибка загрузки price_cost.xlsx (расценки): %s", e)
        _price_cost_lookup_cache = {}

    return _price_cost_lookup_cache

def _add_cost_column(df):
    lookup = _get_price_cost_lookup()
    lookup_material = _get_price_material_cost()

    if "Шифр ТСН" not in df.columns or "Объём работ" not in df.columns:
        logger.warning("Не найдены колонки 'Шифр ТСН' или 'Объём работ'")
        df["Стоимость за Ед. Изм."] = ""
        df["Стоимость"] = ""
        return df

    def lookup_price(shifr_clean):
        # Проверяем основную таблицу расценок
        if shifr_clean in lookup:
            return lookup[shifr_clean]
        
        # Проверяем таблицу ресурсов
        if shifr_clean in lookup_material:
            return lookup_material[shifr_clean]
        
        # Пробуем с префиксом "3."
        if not shifr_clean.startswith("3."):
            alt = "3." + shifr_clean
            if alt in lookup:
                return lookup[alt]
            if alt in lookup_material:
                return lookup_material[alt]
        
        # Пробуем без префикса "3."
        if shifr_clean.startswith("3."):
            alt = shifr_clean[2:]
            if alt in lookup:
                return lookup[alt]
            if alt in lookup_material:
                return lookup_material[alt]
        
        # Специальные случаи (если нужно)
        if "1.7-4-2" in shifr_clean:
            return 342.51
        if "1.7-4-3" in shifr_clean:
            return 290.82
        if "1.1-1-3" in shifr_clean:
            return 104.98
        if "1.1-1-37" in shifr_clean:
            return 104.98
        
        return None

    def calculate_cost(row):
        shifr = row.get("Шифр ТСН")
        volume = row.get("Объём работ")

        if pd.isna(shifr) or pd.isna(volume):
            return "", ""

        try:
            shifr_clean = str(shifr).strip()
            price = lookup_price(shifr_clean)
            
            if price is None:
                return "", ""
            
            # Цена за единицу измерения
            unit_price = float(price)
            
            # Общая стоимость
            cost = unit_price * float(volume)
            
            return round(unit_price, 2), round(cost, 2)
        except (ValueError, TypeError):
            return "", ""

    # Создаем две колонки
    df[["Стоимость за Ед. Изм.", "Стоимость"]] = df.apply(
        lambda row: pd.Series(calculate_cost(row)), axis=1
    )
    
    # Заполняем пустые значения
    df["Стоимость за Ед. Изм."] = df["Стоимость за Ед. Изм."].fillna("")
    df["Стоимость"] = df["Стоимость"].fillna("")
    
    return df

# ============================================================
# ПОИСК ОБЪЁМА
# ============================================================

def _find_column_with_volume(data, marker, extra_marker):
    if not isinstance(data, dict):
        return ""

    if "Количество_в_группе" in data.keys():
        logger.info("Обнаружены данные после группировки")
        for param, value in data.items():
            if (marker in str(param) and extra_marker in str(param) and value and "grouped" in str(param)):
                return value
        for param, value in data.items():
            if (marker in str(param) and value and "grouped" in str(param)):
                return value
    else:
        for param, value in data.items():
            if (marker in str(param) and extra_marker in str(param) and value):
                return value
        for param, value in data.items():
            if (marker in str(param) and value):
                return value

    return ""

# ============================================================
# ГЛОБАЛЬНЫЙ ТИП ОБРАБОТКИ
# ============================================================

processing_type_global = "KR"

# ============================================================
# ОСНОВНАЯ ОБРАБОТКА ОДНОГО ЭЛЕМЕНТА
# ============================================================

def _process_one_element(normalized_data, row_number, output_folder, processing_type="KR"):
    global processing_type_global
    processing_type_global = str(processing_type).upper()

    logger.info("=" * 80)
    logger.info("Обработка элемента %s", row_number)

    # ========================================================
    # 1. ИСХОДНЫЕ ДАННЫЕ
    # ========================================================

    material = normalized_data.get("материал", {}) or {}
    source = normalized_data.get("исходные_данные", {}) or {}

    material_name = normalize_text(material.get("название", ""))
    material_quant = (material.get("количественные_характеристики", {}) or {})
    material_qual = (material.get("качественные_характеристики", {}) or {})

    element_type = normalize_text(source.get("Тип (RU)", ""))
    ifc_type = normalize_text(source.get("Тип элемента", ""))
    element_name = normalize_text(source.get("Имя", ""))
    storey_type = normalize_text(source.get("Тип_этажа", ""))
    element_part = normalize_text(source.get("Этаж", ""))
    composition = normalize_text(normalized_data.get("состав_из_имени", ""))
    material_detected = normalize_text(normalized_data.get("материал_определенный", ""))

    sizes = (normalized_data.get("размеры", {}) or {})
    quantitative = (normalized_data.get("количественные", {}) or {})
    previous_data = source

    # ========================================================
    # 2. АРМАТУРА
    # ========================================================

    reinforcement_raw = source.get("ReinforcementVolumeRatio", 0)
    reinforcement_value = safe_float(reinforcement_raw)
    armature_ratio = reinforcement_value / 1000.0 if reinforcement_value else 0.0

    # ========================================================
    # 3. МАТЕРИАЛЫ ИЗ ВСЕХ ИСТОЧНИКОВ
    # ========================================================

    materials = collect_materials(normalized_data)

    # Если основного материала нет, но есть определённый материал — используем его
    if not material_name and material_detected:
        material_name = material_detected

    # ========================================================
    # 4. МССК
    # ========================================================

    mssk_info = get_element_mssk_info(normalized_data)
    mssk_code = mssk_info["code"]
    mssk_name = mssk_info["name"]

    # ========================================================
    # 5. КЛЮЧЕВЫЕ СЛОВА
    # ========================================================

    all_words = extract_element_search_words(
        normalized_data=normalized_data,
        ifc_type=ifc_type,
        element_type=element_type,
        element_name=element_name,
        mssk_name=mssk_name,
        materials=materials,
        composition=composition
    )

    # ========================================================
    # 6. БАЗА ЗНАНИЙ
    # ========================================================

    kb_rules = get_kb_rules(
        processing_type=processing_type,
        ifc_type=ifc_type,
        mssk_name=mssk_name,
        materials=materials
    )

    knowledge_phrases = kb_rules["search"]
    required_phrases = kb_rules["required"]
    recommended_phrases = kb_rules["recommended"]
    forbidden_phrases = kb_rules["forbidden"]

    logger.info("IFC: %s", ifc_type)
    logger.info("Тип RU: %s", element_type)
    logger.info("МССК: %s", mssk_code)
    logger.info("Название МССК: %s", mssk_name)
    logger.info("Материалы: %s", materials)
    logger.info("Ключевые слова: %s", all_words)
    logger.info("Источники KB: %s", kb_rules["sources"])
    logger.info("Required: %s", required_phrases)
    logger.info("Recommended: %s", recommended_phrases)
    logger.info("Forbidden: %s", forbidden_phrases)

    # ========================================================
    # 7. ФАЙЛ ПРОМЕЖУТОЧНЫХ РАБОТ
    # ========================================================

    works_file = os.path.join(output_folder, f"Промежуточные_работы_{row_number}_после_фильтров.xlsx")
    if not os.path.exists(works_file):
        logger.warning("Файл работ не найден: %s", works_file)
        return

    logger.info("Загрузка работ: %s", works_file)
    try:
        df_works = pd.read_excel(works_file)
    except Exception as e:
        logger.error("Ошибка чтения файла работ: %s", e)
        return

    # ========================================================
    # 8. КОЛОНКА С НАЗВАНИЕМ
    # ========================================================

    search_col = None
    if "Наименование" in df_works.columns:
        search_col = "Наименование"
    else:
        for col in df_works.columns:
            col_lower = str(col).lower()
            if "наименование" in col_lower and "расценк" in col_lower:
                search_col = col
                break

    if not search_col:
        logger.warning("Колонка с наименованием работы не найдена.")
        return

    # ========================================================
    # 9. ПОИСК КАНДИДАТОВ
    # ========================================================

    candidates = search_work_candidates(
        df_works=df_works,
        search_col=search_col,
        all_words=all_words,
        ifc_type=ifc_type,
        mssk_name=mssk_name,
        materials=materials,
        kb_rules=kb_rules
    )

    sorted_works = sort_candidates(candidates)
    logger.info("Найдено кандидатов: %s", len(sorted_works))

    if not sorted_works:
        logger.warning("Работы не найдены для элемента %s", row_number)
        return

    # ========================================================
    # 10. TOP КАНДИДАТОВ
    # ========================================================

    top_works = sorted_works[:MAX_CANDIDATES]

    # ========================================================
    # 11. ГЕОМЕТРИЧЕСКИЙ ФИЛЬТР
    # ========================================================

    geometry_input = prepare_works_for_geometry(top_works)
    try:
        geometry_result = geometry_filter(geometry_input, sizes, ifc_type)
        filtered_works = restore_geometry_results(geometry_result, top_works)
    except Exception as e:
        logger.error("Ошибка geometry_filter: %s", e)
        filtered_works = top_works

    # Если геометрический фильтр ничего не вернул — не теряем кандидатов
    if not filtered_works:
        logger.warning("После geometry_filter кандидатов нет. Используем исходный список.")
        filtered_works = top_works

    filtered_works = filtered_works[:MAX_LLM_CANDIDATES]

    # ========================================================
    # 12. ОБЯЗАТЕЛЬНЫЕ КАНДИДАТЫ
    # ========================================================

    required_candidates = find_required_candidates(sorted_works, required_phrases)

    # Добавляем обязательные работы, если geometry_filter их удалил
    existing_names = {x["наименование"] for x in filtered_works}
    for required_candidate in required_candidates:
        name = required_candidate["наименование"]
        if name not in existing_names:
            filtered_works.append(required_candidate)
            existing_names.add(name)

    # ========================================================
    # 13. ФИНАЛЬНЫЙ СПИСОК ДЛЯ LLM
    # ========================================================

    works_list = []
    for index, work in enumerate(filtered_works, 1):
        search_type = ", ".join(work.get("тип_поиска", []))
        works_list.append(f"{index}. {work['наименование']} [источник: {search_type}]")
    works_text = "\n".join(works_list)

    # ========================================================
    # 14. ИНФОРМАЦИЯ ОБ ЭЛЕМЕНТЕ
    # ========================================================

    element_info = f"""
## ИНФОРМАЦИЯ ОБ ЭЛЕМЕНТЕ

### Идентификация

- Тип IFC: {ifc_type or "не указан"}
- Тип элемента: {element_type or "не указан"}
- Код МССК: {mssk_code or "не указан"}
- Название по МССК: {mssk_name or "не найдено"}
- Имя элемента: {element_name or "не указано"}

### Материалы

Все найденные источники материала:

{json.dumps(materials, ensure_ascii=False, indent=2)}

Основной материал:
{material_name or "не указан"}

Определённый материал:
{material_detected or "не указан"}

Состав из имени:
{composition or "не указан"}

### Характеристики материала

Количественные:
{json.dumps(material_quant, ensure_ascii=False, indent=2)}

Качественные:
{json.dumps(material_qual, ensure_ascii=False, indent=2)}

### Размеры

{json.dumps(sizes, ensure_ascii=False, indent=2)}

### Дополнительные количественные данные

{json.dumps(quantitative, ensure_ascii=False, indent=2)}

### Расположение

Тип этажа:
{storey_type or "не указан"}

Часть здания / этаж:
{element_part or "не указан"}

### Ключевые слова для поиска

{", ".join(all_words)}

### Источники правил базы знаний

{", ".join(kb_rules["sources"]) or "нет"}

### ОБЯЗАТЕЛЬНЫЕ РАБОТЫ

{json.dumps(required_phrases, ensure_ascii=False, indent=2)}

### РЕКОМЕНДУЕМЫЕ РАБОТЫ

{json.dumps(recommended_phrases, ensure_ascii=False, indent=2)}

### ЗАПРЕЩЁННЫЕ РАБОТЫ

{json.dumps(forbidden_phrases, ensure_ascii=False, indent=2)}
"""

    # ========================================================
    # 15. PROMPT
    # ========================================================

    if processing_type.upper() == "AR":
        prompt = PROMPT_AR.format(element_info=element_info, works_text=works_text)
    else:
        prompt = f"""
Ты — эксперт-сметчик по строительным конструкциям.

Твоя задача — выбрать наиболее подходящие работы
для создания или устройства указанного строительного элемента.

{element_info}

## ОСНОВНЫЕ ПРАВИЛА

1. Выбирать можно ТОЛЬКО работы из списка ниже.
2. Нельзя придумывать собственные названия работ.
3. Название должно полностью совпадать с названием в списке доступных работ.
4. Нельзя выбирать работу, которой нет в списке.
5. Учитывай IFC, МССК, материал, имя элемента, характеристики и размеры одновременно.

## ПРАВИЛА КР

- Если элемент железобетонный, учитывай бетонные, арматурные и опалубочные работы.
- Установка арматурных изделий не заменяет саму арматуру
  или арматурные заготовки, если соответствующая работа присутствует в списке.
- Если точного совпадения марки бетона нет, выбирай наиболее подходящую бетонную смесь.
- Если элемент относится к подземной или цокольной части,
  это само по себе не означает автостоянку.
- Не добавляй работы, которые явно относятся к другому типу конструкции или материалу.

## БАЗА ЗНАНИЙ

Работы из списка ОБЯЗАТЕЛЬНЫЕ должны быть выбраны,
если они присутствуют среди доступных работ.

Рекомендуемые работы выбирай, если они действительно соответствуют элементу.

Запрещённые работы выбирать нельзя.

## ДОСТУПНЫЕ РАБОТЫ

{works_text}

## ФОРМАТ ОТВЕТА

Верни ТОЛЬКО JSON:

{{
  "выбранные_работы": [
    {{
      "наименование": "ТОЧНОЕ НАЗВАНИЕ ИЗ СПИСКА",
      "обоснование": "почему работа подходит",
      "категория": "подготовительные/опалубочные/арматурные/бетонные/уход за бетоном/гидроизоляционные/пароизоляционные/теплоизоляционные/отделочные/монтажные/другие"
    }}
  ],
  "рекомендация": "краткий вывод"
}}
"""

    # ========================================================
    # 16. LLM
    # ========================================================

    result = select_works_by_llm(prompt, required_phrases)
    selected_works = result.get("выбранные_работы", [])

    # ========================================================
    # 17. ФИНАЛЬНАЯ ПРОГРАММНАЯ ПРОВЕРКА
    # ========================================================

    selected_works = force_add_missing_required(selected_works, filtered_works, required_phrases)
    result["выбранные_работы"] = selected_works

    if not selected_works:
        logger.warning("LLM не выбрала работы для элемента %s", row_number)
        return

    # ========================================================
    # 18. ФОРМИРОВАНИЕ EXCEL
    # ========================================================

    rows = []
    df_works["normalized_col"] = df_works[search_col].apply(normalize_quotes)

    for work in selected_works:
        if not isinstance(work, dict):
            continue

        work_name = normalize_quotes(
            normalize_text(work.get("наименование", "")).replace("'", '"')
        )

        if not work_name:
            continue

        logger.info("Выбрана работа: %s", work_name)

        # Точное совпадение
        matching = df_works[df_works["normalized_col"] == work_name]
        if len(matching) > 0:
            row_data = matching.iloc[0].to_dict()
        else:
            # Частичное совпадение
            matching = df_works[
                df_works["normalized_col"].str.contains(re.escape(work_name), case=False, na=False)
            ]
            if len(matching) > 0:
                row_data = matching.iloc[0].to_dict()
            else:
                # Если название длинное, пробуем первые 65 символов
                short_name = work_name[:65]
                matching = df_works[
                    df_works["normalized_col"].str.contains(re.escape(short_name), case=False, na=False)
                ]
                if len(matching) > 0:
                    row_data = matching.iloc[0].to_dict()
                else:
                    logger.warning("Не удалось найти работу в исходном Excel: %s", work_name)
                    row_data = {search_col: work_name}

        row_data["Категория"] = work.get("категория", "")
        row_data["Обоснование"] = work.get("обоснование", "")
        rows.append(row_data)

    if not rows:
        logger.warning("После обработки ответа LLM не осталось строк для Excel.")
        return

    df_result = pd.DataFrame(rows)

    # ========================================================
    # 19. УДАЛЕНИЕ ЛИШНИХ КОЛОНОК
    # ========================================================

    cols_to_drop = [
        "Ед. изм", "Наименование работ", "IFC класс",
        "Формула расчёта объёмов работ и расхода материалов",
        "Обозначения", "Обоснование", "Категория", "V по смете",
        "normalized_col", "Параметризация", "№ п/п"
    ]
    df_result = df_result.drop([c for c in cols_to_drop if c in df_result.columns], axis=1)

    # ========================================================
    # 20. ОБЪЁМ РАБОТ
    # ========================================================

    net_volume = _find_column_with_volume(previous_data, "м3", "Объём")
    gross_square = _find_column_with_volume(previous_data, "м2", "Площадь")
    length_str = _find_column_with_volume(previous_data, "мм", "Длина")

    if "Ед. изм." in df_result.columns:
        def get_volume_of_work(row):
            unit = str(row.get("Ед. изм.", "")).lower().replace(" ", "")
            volume_net = safe_float(net_volume)
            square = safe_float(gross_square)
            length = safe_float(length_str)
            armature_volume = volume_net * armature_ratio if volume_net and armature_ratio else 0

            conversions = {
                "м2": (square, 1, "м2"),
                "100м2": (square, 100, "(100 м2)"),
                "м3": (volume_net, 1, "м3"),
                "100м3": (volume_net, 100, "(100 м3)"),
                "т": (armature_volume, 1, "т"),
                "1т": (armature_volume, 1, "т"),
                "м": (length, 1000, "м"), 
                "1м": (length, 1000, "м")
            }

            for unit_key, (value, divisor, label) in conversions.items():
                if unit_key == unit and value:
                    converted = safe_float(value) / divisor
                    if divisor > 1:
                        decimals = 4
                    elif "м2" in unit_key:
                        decimals = 2
                    else:
                        decimals = 3
                    return f"{converted:.{decimals}f}"

            return ""

        df_result["Объём работ"] = df_result.apply(get_volume_of_work, axis=1)
    else:
        logger.warning("Колонка 'Ед. изм.' не найдена.")
        df_result["Объём работ"] = ""

    # ========================================================
    # 21. КОРРЕКТИРОВКА ОБЪЁМА
    # ========================================================

    try:
        df_result = _get_corrected_volume(df_result)
    except Exception as e:
        logger.error("Ошибка корректировки объёма: %s", e)

    # ========================================================
    # 22. СТОИМОСТЬ
    # ========================================================

    try:
        df_result = _add_cost_column(df_result)
    except Exception as e:
        logger.error("Ошибка расчёта стоимости: %s", e)

    # ========================================================
    # 23. СОХРАНЕНИЕ ФИНАЛЬНОГО EXCEL
    # ========================================================

    ifc_class = normalize_text(
        normalized_data.get("основные_характеристики", {}).get("ifc_class", "")
    )
    if not ifc_class:
        ifc_class = normalize_text(
            normalized_data.get("качественные", {}).get("Тип элемента", "")
        )
    if not ifc_class:
        ifc_class = ifc_type

    output_filename = os.path.join(
        output_folder,
        f"Финальный_перечень_работ_{ifc_class}_{row_number}.xlsx"
    )
    try:
        df_result.to_excel(output_filename, index=False)
    except Exception as e:
        logger.error("Ошибка сохранения Excel: %s", e)
        return

    # ========================================================
    # 24. Сохраняем JSON выбранных работ
    # ========================================================

    json_filename = os.path.join(output_folder, f"Подобранные_работы_{row_number}.json")
    try:
        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ошибка сохранения JSON: %s", e)

    # ========================================================
    # 25. СОХРАНЯЕМ ВСЕ КАНДИДАТЫ
    # ========================================================

    all_works_output = {
        "элемент": {
            "тип_ifc": ifc_type,
            "тип_элемента": element_type,
            "мсск": {"код": mssk_code, "название": mssk_name},
            "имя": element_name,
            "материалы": materials,
            "материал": {
                "название": material_name,
                "определенный": material_detected,
                "состав": composition,
                "количественные": material_quant,
                "качественные": material_qual
            },
            "размеры": sizes,
            "тип_этажа": storey_type,
            "часть_здания": element_part
        },
        "ключевые_слова": all_words,
        "база_знаний": {
            "источники": kb_rules["sources"],
            "search": knowledge_phrases,
            "required": required_phrases,
            "recommended": recommended_phrases,
            "forbidden": forbidden_phrases
        },
        "найденные_работы": sorted_works,
        "кандидаты_после_геометрии": filtered_works,
        "выбранные_работы": selected_works,
        "тип_обработки": processing_type
    }

    all_json_filename = os.path.join(output_folder, f"Все_найденные_работы_{row_number}.json")
    try:
        with open(all_json_filename, "w", encoding="utf-8") as f:
            json.dump(all_works_output, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.error("Ошибка сохранения полного JSON: %s", e)

    logger.info("Сохранено: %s", output_filename)
    logger.info("Элемент %s обработан.", row_number)

# ============================================================
# ОБЪЕДИНЕНИЕ ФИНАЛЬНЫХ ПЕРЕЧНЕЙ
# ============================================================

def merge_final_worklists(input_folder):
    output_file = os.path.join(input_folder, "ОБЩИЙ_Финальный_перечень_работ.xlsx")

    # Ищем Excel
    excel_files = []
    for filename in os.listdir(input_folder):
        if (filename.startswith("Финальный_перечень_работ_")
                and filename.endswith(".xlsx")
                and not filename.startswith("ОБЩИЙ_")):
            excel_files.append(filename)

    if not excel_files:
        logger.error("Файлы финальных перечней не найдены.")
        return None

    # Сортировка по номеру
    def sort_filename(filename):
        match = re.search(r"_(\d+)\.xlsx$", filename)
        if match:
            return int(match.group(1))
        return 0

    excel_files.sort(key=sort_filename)
    logger.info("Найдено файлов для объединения: %s", len(excel_files))

    all_parts = []

    # Первый файл
    first_file_path = os.path.join(input_folder, excel_files[0])
    first_df = pd.read_excel(first_file_path)
    all_columns = first_df.columns.tolist()
    num_columns = len(all_columns)

    # Все файлы
    for filename in excel_files:
        file_path = os.path.join(input_folder, filename)
        match = re.search(r"_(\d+)\.xlsx$", filename)
        row_number = match.group(1) if match else "1"

        try:
            df = pd.read_excel(file_path)

            # JSON нормализованных данных
            json_filename = f"Нормализованные_данные_элемента_{row_number}.json"
            json_path = os.path.join(input_folder, json_filename)

            ifc_class = ""
            name_elem = ""
            global_id = ""
            floor = ""

            if os.path.exists(json_path):
                with open(json_path, "r", encoding="utf-8") as file:
                    try:
                        data = json.load(file)
                        prev_data = data.get("исходные_данные", {}) or {}
                        ifc_class = normalize_text(prev_data.get("Тип элемента", ""))
                        global_id = normalize_text(prev_data.get("GlobalId", ""))
                        name_elem = normalize_text(prev_data.get("Имя", ""))
                        floor = normalize_text(prev_data.get("Тип_этажа", ""))
                    except json.JSONDecodeError:
                        logger.warning("Ошибка чтения JSON: %s", json_filename)

            # Разделитель
            separator_row = {}
            separator_row[all_columns[0]] = f"{ifc_class} {name_elem} {global_id} {floor}"
            for col in all_columns[1:]:
                separator_row[col] = ""

            separator = pd.DataFrame([separator_row])
            empty_row = pd.DataFrame([[" "] * num_columns], columns=all_columns)

            all_parts.append(separator)
            all_parts.append(df)
            all_parts.append(empty_row)

            logger.info("Объединён %s: %s строк", filename, len(df))

        except Exception as e:
            logger.error("Ошибка обработки %s: %s", filename, e)

    # Объединение
    if not all_parts:
        logger.warning("Нет данных для объединения.")
        return None

    result = pd.concat(all_parts, ignore_index=True)
    result.to_excel(output_file, index=False)

    logger.info("Объединение завершено.")
    logger.info("Файл: %s", output_file)
    return result

# ============================================================
# ЧЕТВЁРТЫЙ ЭТАП
# ============================================================

def fourth_step(input_folder, processing_type="KR"):
    """
    Четвёртый этап:
    подбор работ по данным JSON,
    базе знаний, материалу, МССК и IFC.
    """
    global processing_type_global
    processing_type_global = str(processing_type).upper()

    logger.info("=" * 80)
    logger.info("НАЧАТ ЧЕТВЁРТЫЙ ЭТАП")
    logger.info("Тип обработки: %s", processing_type_global)

    # Проверка МССК
    if MSSK_FILE:
        if os.path.exists(MSSK_FILE):
            logger.info("Файл МССК: %s", MSSK_FILE)
        else:
            logger.warning("Файл МССК не найден: %s", MSSK_FILE)

    # Нормализованные JSON
    json_files = []
    for filename in os.listdir(input_folder):
        if (filename.endswith(".json")
                and filename.startswith("Нормализованные_данные")):
            json_files.append(filename)

    json_files.sort()
    logger.info("Найдено нормализованных JSON: %s", len(json_files))

    # Обработка
    for filename in json_files:
        file_path = os.path.join(input_folder, filename)
        match = re.search(r"(\d+)(?=\.json$)", filename)
        row_number = match.group(1) if match else "1"

        logger.info("-" * 80)
        logger.info("Загрузка: %s", filename)

        try:
            with open(file_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            _process_one_element(
                data,
                row_number,
                input_folder,
                processing_type_global
            )
            logger.info("Обработан файл: %s", filename)

        except json.JSONDecodeError as e:
            logger.error("Ошибка JSON %s: %s", filename, e)

        except Exception as e:
            logger.error("Ошибка обработки %s: %s", filename, e)

    # Объединение
    try:
        merge_final_worklists(input_folder)
    except Exception as e:
        logger.error("Ошибка объединения финальных перечней: %s", e)

    logger.info("=" * 80)
    logger.info("ЧЕТВЁРТЫЙ ЭТАП ЗАВЕРШЕН")
    logger.info("=" * 80)