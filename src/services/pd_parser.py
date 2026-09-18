import argparse
import json
import math
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import fitz
import httpx

REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "params_registry.json"
)
WORKS_CLASSIFICATION_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "works_classification.json"
)
TABLE_MARKER = "[Структура таблиц страницы]"
CONTEXT_CHAR_BUDGET = 36000


@dataclass(frozen=True)
class Config:
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "http://localhost:11434"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gemma3:27b"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "ollama"))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "300")))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0")))
    llm_num_ctx: int = field(default_factory=lambda: int(os.getenv("LLM_NUM_CTX", "16384")))
    llm_parallel: int = field(default_factory=lambda: max(1, int(os.getenv("LLM_PARALLEL", "1"))))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("RETRIEVAL_TOP_K", "6")))
    registry_path: str = field(default_factory=lambda: os.getenv("PARAMS_REGISTRY_PATH", str(REGISTRY_PATH)))


PROMPTS = {
    "material_system": """Ты - инженер ПТО. Твоя задача - найти в выдержках из пояснительной записки (раздел «Конструктивные решения») материал заданной группы конструктивных элементов здания.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Используй только сведения из приведённых выдержек. Ничего не выдумывай.
3. Для железобетонных конструкций материал - это класс бетона (B25, B30, B35...), марка по водонепроницаемости (W4, W6...), марка по морозостойкости (F100, F150...) и ГОСТ, если указан.
4. В поле "quote" приведи дословную фразу из текста, из которой взят материал, а в "page" - номер страницы этой фразы (номера указаны в заголовках выдержек).
5. Внимательно проверь, не отличается ли материал для разных этажей, секций здания или толщин (например: «1-го этажа из бетона B35», «2-го - 19-го этажей из бетона B30»). Если отличается - обязательно перечисли каждый случай отдельной записью в "variants", и в "scope" каждой записи укажи, к каким этажам/секциям она относится.
5а. В блоках «[Структура таблиц страницы]» строки уже развёрнуты в пары «заголовок колонки: значение», например: «Бетон всех вертикальных конструкций - 1-го этажа: Бетон B35, W6, F150; Со 2-го по 19-й этаж: Бетон B30 W6 F150». Если такая строка относится к искомым элементам, перенеси каждую пару в "variants" ДОСЛОВНО: scope = заголовок колонки (например «Со 2-го по 19-й этаж»), material = значение именно этой пары (в примере для «Со 2-го по 19-й этаж» это B30, а не B35). Не смешивай значения соседних пар.
6. Бери сведения только из описаний той части здания, которая указана в задаче (подземная/цокольная/надземная). В заголовке каждой выдержки указан раздел ПЗ - не переноси материал из раздела про другую часть здания (например, из раздела про подземную часть - на надземные конструкции).
7. Материал в ответе должен относиться именно к типу конструкций из задачи. Не подставляй материал конструкций другого типа из тех же выдержек (например, материал стен для перекрытий): если про нужный тип в выдержках не сказано - верни "found": false.
8. Если материал в выдержках не найден - верни "found": false и пустые остальные поля.
""",
    "material_user": """## Группа элементов
Тип элементов: {element_type}
Расположение: {location}
Варианты в группе: {qualifiers}
Количество элементов: {element_count}
{measure_line}

## Выдержки из пояснительной записки
{context}

## Задача
Определи материал (для ж/б конструкций - класс бетона, W, F, ГОСТ) для этой группы элементов.
{type_hint}
Учти: группа может объединять разные конструкции одного типа (например, «Перекрытия» подземной части включают и фундаментную плиту, и плиты перекрытия подземного этажа). Если материалы этих конструкций отличаются - перечисли каждую в "variants".

Формат ответа:
{{
  "found": true,
  "material": "полное наименование материала, например: Бетон B30 W6 F150 (ГОСТ 26633-2015)",
  "concrete_class": "B30",
  "waterproofing": "W6",
  "frost_resistance": "F150",
  "gost": "ГОСТ 26633-2015",
  "quote": "дословная цитата из текста",
  "page": 20,
  "confidence": "high",
  "variants": [
    {{"scope": "к чему относится (этаж, толщина и т.п.)", "material": "материал"}}
  ],
  "note": "краткое примечание при необходимости"
}}

Поле "confidence": "high" - материал указан явно для этих элементов; "medium" - материал выведен из близкого по смыслу описания; "low" - уверенности нет.
""",
    "building_system": """Ты - инженер ПТО. По выдержкам из документа проектной документации определи высотные характеристики здания.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Используй только сведения из приведённых выдержек. Ничего не выдумывай.
3. Для каждого параметра указывай значение как в тексте (с единицами измерения) и номер страницы, откуда оно взято (номера указаны в заголовках выдержек).
4. Включай только параметры, которые названы в тексте дословно. Не выдумывай и не дублируй: если в тексте нет «высоты типового этажа» - не включай её.
5. Если один параметр приведён в нескольких вариантах (например, высота подземного этажа под жилым домом и под двором) - включи каждый вариант отдельной строкой.
6. Высота здания может быть указана как верхняя отметка (например, «+64,800») или предельная высота по проекту - включи её, если есть.
""",
    "building_user": """## Выдержки из документа
{context}

## Задача
Найди высотные характеристики здания: этажность (количество этажей, в т.ч. подземных), высоты этажей (подземного, первого, типового, технического, чердака), а также высоту здания целиком - она может называться «верхняя отметка здания» (например, «+64,800 по парапету кровли») или «предельная высота зданий, строений» (значение «согласно проекта»). Если нашёл верхнюю отметку или предельную высоту - обязательно включи их в parameters.

Формат ответа:
{{
  "storeys": "этажность здания как в тексте, например: 19 этажей + подземный этаж",
  "parameters": [
    {{"name": "Высота 1 этажа", "value": "3,3 м", "page": 35}},
    {{"name": "Высота типового этажа", "value": "3,0 м", "page": 35}}
  ]
}}
""",
    "pos_works_system": """Ты - инженер ПТО. По выдержкам из проекта организации строительства (ПОС) найди сведения о заданной категории строительных работ.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Используй только сведения из приведённых выдержек. Ничего не выдумывай.
3. В список включай конкретные работы, технологии, требования и объёмы, относящиеся к заданной категории (например, для бетонных работ: укладка бетонной смеси, уход за бетоном, зимнее бетонирование, требования к смеси).
3а. ОБЯЗАТЕЛЬНО включай отдельной записью способ производства работ и применяемые машины и механизмы, если они указаны в тексте: например «Способ бетонирования - автобетононасосами типа Schwing S34X, бетононасосами Schwing SP750», «Подача - башенным краном Potain MDT 178». Марки механизмов переноси дословно, отдельно для подземной и надземной части, если они различаются.
4. Для каждой работы укажи краткое название, суть как в тексте и номер страницы (номера указаны в заголовках выдержек).
5. Если сведений по категории в выдержках нет - верни "found": false и пустой список.
""",
    "pos_works_user": """## Категория работ
{category}

## Выдержки из проекта организации строительства (ПОС)
{context}

## Задача
Найди в выдержках работы категории «{category}»: перечисли, что предусмотрено проектом, с сутью и страницей.

Формат ответа:
{{
  "found": true,
  "works": [
    {{"name": "краткое название работы", "details": "суть как в тексте", "page": 12}}
  ],
  "note": "краткое примечание при необходимости"
}}
""",
    "params_system": """Ты извлекаешь характеристики конструкций и условий производства работ
из проектной документации (пояснительная записка, проект организации
строительства). Отвечай строго JSON, без пояснений.

Правила:
- бери значения только из переданных страниц, ничего не додумывай;
- если параметра на страницах нет, просто не включай его в ответ,
  пустые и предположительные значения недопустимы;
- в поле quote давай дословный фрагмент страницы (10-200 знаков),
  из которого взято значение, без изменений и без сокращений;
- в поле page ставь номер страницы, указанный в заголовке
  «=== Страница N ===», откуда взята цитата;
- если один параметр имеет разные значения для разных частей здания,
  этажей или конструкций, верни несколько записей и укажи в value,
  к чему относится значение.
""",
    "params_user": """Тема: {topic}

Нужно найти значения следующих параметров:
{fields}

Страницы документа:

{context}

Верни JSON вида:
{{"parameters": [{{"name": "<точное название параметра из списка выше>",
"value": "<значение с единицей измерения>", "quote": "<дословная цитата>",
"page": <номер страницы>}}]}}
""",
}


@dataclass
class Page:
    number: int
    text: str
    section: str = ""


_HEADING_RE = re.compile(
    r"^\s*(\d{1,2}(?:\.\d{1,2})?)\.?\s+((?:Описание|Общие|Сведения|Обоснование|"
    r"Характеристика|Перечень|Мероприятия|Конструктивные|Технические)"
    r"[^\n]*(?:\n[а-яё][^\n]*){0,2})",
    re.MULTILINE,
)


def _page_headings(text: str) -> List[str]:
    headings = []
    for m in _HEADING_RE.finditer(text):
        title = " ".join(m.group(2).split())
        headings.append(f"{m.group(1)} {title}"[:130])
    return headings


def _clean_rows(t) -> List[List[str]]:
    try:
        rows = t.extract()
    except Exception:
        return []
    return [[" ".join(str(c).split()) if c else "" for c in row] for row in rows]


def _find_header_idx(rows: List[List[str]]) -> int:
    for i, vals in enumerate(rows[:3]):
        nonempty = [v for v in vals if v]
        if len(nonempty) >= 2 and all(len(v) <= 40 for v in nonempty):
            return i
    return -1


def _linearize_table(rows: List[List[str]], headers: List[str]) -> List[str]:
    lines = []
    for vals in rows:
        if not any(vals):
            continue
        label = vals[0]
        pairs = []
        for j, v in enumerate(vals[1:], 1):
            if not v:
                continue
            h = headers[j] if j < len(headers) and headers[j] not in ("", "-") else ""
            pairs.append(f"{h}: {v}" if h else v)
        if not pairs:
            continue
        lines.append((f"{label} - " if label else "") + "; ".join(pairs))
    return lines


def _tables_linearized(page, prev_tail: Optional[dict]) -> Tuple[str, Optional[dict]]:
    try:
        tabs = page.find_tables()
    except Exception:
        return "", None
    page_h = float(page.rect.height) or 1.0
    blocks = []
    new_tail = None
    first_good = True
    for t in tabs.tables:
        rows = _clean_rows(t)
        cells = [c for row in rows for c in row if c]
        if not cells or max(len(c) for c in cells) > 400:
            continue
        bbox = t.bbox
        header_idx = _find_header_idx(rows)
        headers: List[str] = []
        data_rows = rows
        continued = (first_good and prev_tail and prev_tail["cols"] == t.col_count
                     and bbox[1] < page_h * 0.4)
        if continued:
            headers = prev_tail["headers"]
        elif header_idx >= 0:
            headers = [v or "-" for v in rows[header_idx]]
            data_rows = rows[header_idx + 1:]
        lines = _linearize_table(data_rows, headers)
        first_good = False
        if lines:
            prefix = "(продолжение таблицы с предыдущей страницы) " if continued else ""
            blocks.append(prefix + "\n".join(lines))
        if bbox[3] > page_h * 0.6:
            if headers and any(h not in ("", "-") for h in headers):
                new_tail = {"cols": t.col_count, "headers": headers}
            else:
                new_tail = None
    return "\n\n".join(blocks), new_tail


def extract_pages(source: Union[str, Path, bytes]) -> List[Page]:
    if isinstance(source, (bytes, bytearray)):
        doc = fitz.open(stream=bytes(source), filetype="pdf")
    else:
        doc = fitz.open(str(source))
    try:
        pages = []
        current_section = ""
        tail = None
        for i, page in enumerate(doc):
            text = page.get_text("text")
            headings = _page_headings(text)
            if headings:
                current_section = headings[-1]
            tables_md, tail = _tables_linearized(page, tail)
            if tables_md:
                text = f"{text}\n\n{TABLE_MARKER}\n{tables_md}"
            pages.append(Page(number=i + 1, text=text, section=current_section))
        return pages
    finally:
        doc.close()


_ADDR_STOPWORDS = {
    "жилой", "дом", "дома", "здание", "инженерными", "сетями",
    "благоустройством", "территории", "адресу", "город", "москва", "район",
    "улица", "ул", "земельный", "участок", "вл", "влд", "владение", "корпус",
    "корп", "строение", "административный", "округ", "этап", "строительства",
    "строительство", "многоквартирный", "проектируемый", "мкр", "квартал",
    "кварталы", "северный", "северное", "южный", "южное", "восточный",
    "восточное", "западный", "западное", "центральный", "зеленоградский",
    "новомосковский", "троицкий", "вао", "сао", "зао", "юао", "цао", "свао",
    "сзао", "ювао", "юзао", "тинао", "зелао",
    "проектная", "рабочая", "документация", "раздел", "подраздел", "том",
    "часть", "книга", "шифр",
    "проспект", "переулок", "шоссе", "набережная", "проезд", "бульвар",
    "площадь", "линия", "аллея", "тупик", "магистраль",
}

_TITLE_MARKERS = (
    "ПРОЕКТНАЯ ДОКУМЕНТАЦИЯ", "РАБОЧАЯ ДОКУМЕНТАЦИЯ", "Том ", "Раздел ",
    "Подраздел", "Заказчик", "Застройщик", "Генеральн", "Технический",
    "выполнен", "разработан", "составлен", "Проект организации", "Шифр",
)

_ADDR_ABBR = {
    "г", "ул", "вл", "д", "корп", "стр", "мкр", "обл", "пер", "наб",
    "пр", "ш", "т", "эт", "отм", "ж", "б", "кв", "р", "п", "с", "им",
}


def _trim_address(address: str) -> str:
    cut = len(address)
    for marker in _TITLE_MARKERS:
        i = address.find(marker)
        if 15 <= i < cut:
            cut = i
    address = address[:cut].rstrip(" ,;-")
    for m in re.finditer(r"\.\s", address):
        i = m.start()
        if i < 15:
            continue
        word = re.search(r"([а-яёa-z0-9]+)$", address[:i].lower())
        if word and word.group(1) in _ADDR_ABBR:
            continue
        return address[:i].strip()
    return address


def extract_object_info(pages: List[Page], max_pages: int = 6) -> dict:
    text = "\n".join(p.text for p in pages[:max_pages])
    address = ""
    m = re.search(r"по адресу\s*:?\s*(.{10,250})", text, re.IGNORECASE | re.DOTALL)
    if m:
        raw = m.group(1).split("»")[0]
        address = _trim_address(" ".join(raw.split()))[:200]
    if not address:
        for qm in re.finditer(r"«([^»]{20,250})»", text):
            q = qm.group(1)
            if re.search(r"жило[йг]|корпус|корп\.|ул\.|улица|проспект|переул|"
                         r"шоссе|набережн|проезд|бульвар|квартал", q, re.IGNORECASE):
                address = _trim_address(" ".join(q.split()))[:200]
                break
    cipher = ""
    fallback = ""
    for cm in re.finditer(r"\b\d{1,4}(?:-[А-ЯЁA-Z0-9]{1,6}){2,6}\b", text):
        c = cm.group(0)
        if not re.search(r"[А-ЯЁ]", c):
            continue
        if re.search(r"-(КР|ПОС|ПЗ|АР|ИОС|ТКР|ПИР)\d*\b", c):
            cipher = c
            break
        if not fallback:
            fallback = c
    cipher = cipher or fallback
    return {"address": address, "cipher": cipher}


def _addr_tokens(address: str) -> set:
    return {
        t for t in re.findall(r"[а-яё0-9]+", address.lower())
        if len(t) >= 3 and t not in _ADDR_STOPWORDS
    }


def _cipher_root(cipher: str) -> str:
    return "-".join(cipher.split("-")[:3])


def objects_mismatch(info_a: dict, info_b: dict) -> bool:
    ca, cb = info_a.get("cipher", ""), info_b.get("cipher", "")
    if ca and cb and _cipher_root(ca) == _cipher_root(cb):
        return False
    ta, tb = _addr_tokens(info_a.get("address", "")), _addr_tokens(info_b.get("address", ""))
    if ta and tb:
        return len(ta & tb) == 0
    if ca and cb:
        return _cipher_root(ca) != _cipher_root(cb)
    return False


_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

_SUFFIXES = (
    "иями", "ями", "ами", "иях", "иям", "ыми", "ими", "ого", "его",
    "ому", "ему", "ая", "яя", "ое", "ее", "ый", "ий", "ой", "ей",
    "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев", "ую", "юю",
    "ье", "ья", "ия", "ие", "ы", "и", "а", "я", "о", "е", "у", "ю", "ь",
)
_MIN_STEM = 4
_MAX_STEM = 10


def _stem(token: str) -> str:
    if len(token) > _MIN_STEM:
        for suf in _SUFFIXES:
            if len(token) - len(suf) >= _MIN_STEM and token.endswith(suf):
                token = token[: -len(suf)]
                break
    return token[:_MAX_STEM]


def tokenize(text: str) -> List[str]:
    return [_stem(t.lower().replace("ё", "е")) for t in _TOKEN_RE.findall(text)]


class PageIndex:
    def __init__(self, pages: Sequence[Page], window: int = 2,
                 k1: float = 1.5, b: float = 0.75):
        self.pages = list(pages)
        self.k1 = k1
        self.b = b
        self._windows: List[Tuple[int, ...]] = []
        if self.pages:
            n = len(self.pages)
            w = max(1, min(window, n))
            self._windows = [tuple(range(i, i + w)) for i in range(n - w + 1)]
        self._tf: List[Counter] = []
        self._df: Counter = Counter()
        self._len: List[int] = []
        for win in self._windows:
            tokens = []
            for i in win:
                tokens.extend(tokenize(self.pages[i].text))
            tf = Counter(tokens)
            self._tf.append(tf)
            self._len.append(len(tokens))
            for term in tf:
                self._df[term] += 1
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 0.0
        self._n = len(self._windows)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query: str) -> List[Tuple[Tuple[int, ...], float]]:
        q_terms = tokenize(query)
        scores: Dict[int, float] = {}
        for i, tf in enumerate(self._tf):
            s = 0.0
            dl = self._len[i] or 1
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = self._idf(term)
                s += idf * f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self._avg_len)
                )
            if s > 0:
                scores[i] = s
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [(self._windows[i], s) for i, s in ranked]

    def top_pages(self, query: str, k: int) -> List[Page]:
        selected: List[int] = []
        for win, _ in self.score(query):
            for i in win:
                if i not in selected:
                    selected.append(i)
            if len(selected) >= k:
                break
        return [self.pages[i] for i in sorted(selected[:k])]


TYPE_QUERY = {
    "стены": "стена стены монолитные железобетонные наружные внутренние бетон класс толщиной этажа этажей",
    "перекрытия": "перекрытие плита перекрытия покрытия фундаментная плита монолитная железобетонная бетон класс этажа этажей",
    "колонны": "колонна колонны пилон пилоны монолитные железобетонные бетон класс сечением этажа",
    "лестницы": "лестница лестницы лестничный марш марши площадка площадки монолитные железобетонные бетон",
    "пандусы": "пандус пандусы рампа монолитные железобетонные бетон",
    "прочие элементы": "конструкции монолитные железобетонные бетон класс",
}

LOCATION_QUERY = {
    "подземная часть здания": "подземная часть фундамент фундаментная подвал стены подземной",
    "цокольная часть здания": "цокольная цоколь первый этаж стены цокольного",
    "надземная часть здания": "надземная часть типовой этаж стены надземной",
    "автостоянка": "автостоянка паркинг подземная стоянка рампа въезд",
}

TYPE_QUERY_BY_LOCATION = {
    ("подземная часть здания", "перекрытия"):
        "фундаментная плита фундаментной плиты отметка низа верха ростверк "
        "плита перекрытия подземного этажа монолитная железобетонная бетон класс",
    ("надземная часть здания", "перекрытия"):
        "перекрытие перекрытия плита плиты покрытия монолитная железобетонная "
        "безбалочные пролетом толщиной бетон класс этажа этажей",
    ("цокольная часть здания", "перекрытия"):
        "перекрытие перекрытия плита плиты монолитная железобетонная "
        "безбалочные пролетом толщиной бетон класс этажа цокольного",
}

BUILDING_INFO_QUERY = (
    "высота здания этажность этажей подземный этаж технический чердак "
    "отметка верхняя высота этажа жилой дом количество этажей"
)


def unit_query(element_type: str, location: str) -> str:
    loc_key = location.strip().lower()
    type_key = element_type.strip().lower()
    t = TYPE_QUERY_BY_LOCATION.get((loc_key, type_key)) or TYPE_QUERY.get(type_key)
    if t is None:
        if "гидроизоляц" in type_key:
            tail = "гидроизоляция мембрана рулонная обмазочная праймер материал слой"
        else:
            tail = "монолитная железобетонная бетон класс"
        t = f"{element_type} {tail}"
    l = LOCATION_QUERY.get(loc_key, location)
    return f"{t} {l}"


def format_context(pages: List[Page], budget: int = CONTEXT_CHAR_BUDGET) -> str:
    if not pages:
        return ""
    per_page = max(1500, budget // len(pages))
    blocks = []
    for p in pages:
        text = p.text.strip()
        if len(text) > per_page:
            text = text[:per_page] + "\n[...текст страницы обрезан...]"
        header = f"=== Страница {p.number}"
        if p.section:
            header += f" · Раздел: {p.section}"
        blocks.append(f"{header} ===\n{text}")
    return "\n\n".join(blocks)


def normalize(text: str) -> str:
    text = text.replace("-\n", "").replace("­", "")
    text = text.replace(" ", " ")
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text)
    for ch in "«»\"„“”-–—":
        text = text.replace(ch, " ")
    return " ".join(text.split()).lower()


def page_contains_quote(page: Page, quote_norm: str) -> bool:
    page_norm = normalize(page.text)
    if quote_norm in page_norm:
        return True
    fragments = [
        normalize(f)
        for f in re.split(r"[;:,.()\n]", quote_norm)
        if len(f.strip()) >= 12
    ]
    if not fragments:
        return False
    hits = sum(1 for f in fragments if f in page_norm)
    return hits / len(fragments) >= 0.6


def find_quote_page(quote: str, pages: List[Page],
                    claimed_page: Optional[int] = None) -> Optional[int]:
    if not quote:
        return None
    quote_norm = normalize(quote)
    if len(quote_norm) < 10:
        return None
    ordered = sorted(pages, key=lambda p: p.number != claimed_page)
    for p in ordered:
        if page_contains_quote(p, quote_norm):
            return p.number
    return None


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        result = json.loads(text[start: end + 1])
        if isinstance(result, dict):
            return result
    raise LLMError(f"Модель вернула не JSON-объект: {text[:200]!r}")


class LLMClient:
    def __init__(self, config: Config):
        self.config = config
        base = config.llm_base_url.rstrip("/")
        self.openai_mode = base.endswith("/v1")
        self.base = base
        self._schema_supported = True

    def complete_json(self, system: str, user: str, schema: Optional[dict] = None,
                      retries: int = 2) -> dict:
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 8))
            try:
                raw = self._chat(system, user, schema)
                return _extract_json(raw)
            except (httpx.HTTPError, LLMError, json.JSONDecodeError) as e:
                last_err = e
        raise LLMError(f"LLM не ответил корректно после {retries + 1} попыток: {last_err}")

    @staticmethod
    def _content_or_raise(data: dict, *path) -> str:
        node = data
        for key in path:
            try:
                node = node[key]
            except (KeyError, IndexError, TypeError):
                raise LLMError(f"Неожиданный формат ответа LLM: {str(data)[:200]!r}")
        if not isinstance(node, str) or not node.strip():
            raise LLMError(f"Пустой content в ответе LLM: {str(data)[:200]!r}")
        return node

    def _chat(self, system: str, user: str, schema: Optional[dict]) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        with httpx.Client(timeout=self.config.llm_timeout) as client:
            if self.openai_mode:
                return self._chat_openai(client, messages, schema)
            payload = {
                "model": self.config.llm_model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": self.config.llm_temperature,
                    "num_ctx": self.config.llm_num_ctx,
                },
                "format": schema if schema else "json",
            }
            r = client.post(f"{self.base}/api/chat", json=payload)
            r.raise_for_status()
            return self._content_or_raise(r.json(), "message", "content")

    def _chat_openai(self, client: httpx.Client, messages: list,
                     schema: Optional[dict]) -> str:
        payload = {
            "model": self.config.llm_model,
            "messages": messages,
            "temperature": self.config.llm_temperature,
        }
        if schema and self._schema_supported:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.config.llm_api_key}"}
        r = client.post(f"{self.base}/chat/completions", json=payload, headers=headers)
        if r.status_code == 400 and schema and self._schema_supported:
            self._schema_supported = False
            payload["response_format"] = {"type": "json_object"}
            r = client.post(f"{self.base}/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        return self._content_or_raise(r.json(), "choices", 0, "message", "content")


GENERIC_MATERIALS = {
    "", "бетон", "железобетон", "ж/б", "жб", "ж б", "0", "-", "нет",
    "по умолчанию", "default", "<unnamed>", "unnamed", "не задано",
    "металл", "сталь", "металлопрокат",
}

MEASURE_UNITS = {"volume": "м³", "area": "м²", "count": "шт"}


def _is_generic_material(raw: str) -> bool:
    norm = " ".join(raw.replace("/", " ").replace("-", " ").lower().split())
    if norm in GENERIC_MATERIALS:
        return True
    has_digits = any(c.isdigit() for c in norm)
    return norm.startswith(("бетон", "железобетон")) and not has_digits


@dataclass
class LeafGroup:
    index: int
    element_type: str
    location: str
    qualifier_name: str
    qualifier_value: str
    element_count: int
    measure_value: Optional[float]
    measure_type: str
    measure_unit: str
    ifc_material: str

    @property
    def measure_text(self) -> str:
        if self.measure_value is None:
            return ""
        unit = self.measure_unit or MEASURE_UNITS.get(self.measure_type, "")
        if self.measure_type == "count":
            return f"{int(self.measure_value)} {unit}".strip()
        return f"{self.measure_value:.2f} {unit}".strip()

    @property
    def title(self) -> str:
        parts = [self.location, self.element_type]
        if self.qualifier_name and self.qualifier_value:
            parts.append(f"{self.qualifier_name.lower()}: {self.qualifier_value}")
        return ". ".join(p for p in parts if p)

    @property
    def needs_lookup(self) -> bool:
        if not _is_generic_material(self.ifc_material):
            return False
        mat = self.ifc_material.strip().lower()
        if self.element_type.replace("_", " ").strip().lower() == "прочие элементы" and mat in ("", "0"):
            return False
        return True


@dataclass
class ExtractionUnit:
    key: str
    element_type: str
    location: str
    qualifiers: List[str] = field(default_factory=list)
    group_indexes: List[int] = field(default_factory=list)
    total_volume: float = 0.0
    total_area: float = 0.0
    count_elements: int = 0
    element_count: int = 0

    @property
    def measure_text(self) -> str:
        parts = []
        if self.total_volume:
            parts.append(f"{self.total_volume:.2f} {MEASURE_UNITS['volume']}")
        if self.total_area:
            parts.append(f"{self.total_area:.2f} {MEASURE_UNITS['area']}")
        if self.count_elements:
            parts.append(f"{self.count_elements} {MEASURE_UNITS['count']}")
        return "; ".join(parts)

    @property
    def measure_line(self) -> str:
        if self.total_volume or self.total_area:
            return f"Мера по модели: {self.measure_text}"
        if self.count_elements:
            return ("Объём и площадь в модели не заданы, известно только "
                    f"количество элементов: {self.count_elements} шт")
        return "Объём и площадь неизвестны"


def _char_value(chars: List[dict], name: str) -> str:
    for ch in chars or []:
        if isinstance(ch, dict) and ch.get("name") == name:
            values = ch.get("values")
            if isinstance(values, list) and values and isinstance(values[0], dict):
                return str(values[0].get("strValue", "") or "")
    return ""


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_groups(raw: List[dict]) -> List[LeafGroup]:
    groups = []
    for i, g in enumerate(raw):
        if not isinstance(g, dict):
            continue
        chars = g.get("characteristics")
        chars = chars if isinstance(chars, list) else []
        material = _char_value(chars, "Материал")
        location = _char_value(chars, "Расположение")
        qualifier_name, qualifier_value = "", ""
        for name in ("Толщина", "Площадь", "Длина"):
            val = _char_value(chars, name)
            if val:
                qualifier_name, qualifier_value = name, val
                break
        measure = g.get("totalMeasure")
        measure = measure if isinstance(measure, dict) else {}
        measure_value = measure.get("value")
        if not isinstance(measure_value, (int, float)):
            measure_value = None
        measure_type = str(measure.get("type", "") or "").strip().lower()
        groups.append(LeafGroup(
            index=i,
            element_type=str(g.get("buildingElementName", "") or "").replace("_", " ").strip(),
            location=location,
            qualifier_name=qualifier_name,
            qualifier_value=qualifier_value,
            element_count=_to_int(g.get("elementCount")),
            measure_value=measure_value,
            measure_type=measure_type,
            measure_unit=str(measure.get("unit", "") or "") or MEASURE_UNITS.get(measure_type, ""),
            ifc_material=material.strip(),
        ))
    return groups


def build_units(groups: List[LeafGroup]) -> List[ExtractionUnit]:
    units: Dict[str, ExtractionUnit] = {}
    for g in groups:
        if not g.needs_lookup:
            continue
        key = f"{g.location}|{g.element_type}"
        unit = units.get(key)
        if unit is None:
            unit = ExtractionUnit(key=key, element_type=g.element_type, location=g.location)
            units[key] = unit
        if g.qualifier_name and g.qualifier_value:
            q = f"{g.qualifier_name.lower()} {g.qualifier_value}"
            if q not in unit.qualifiers:
                unit.qualifiers.append(q)
        unit.group_indexes.append(g.index)
        unit.element_count += g.element_count
        if g.measure_type == "volume" and isinstance(g.measure_value, (int, float)):
            unit.total_volume += float(g.measure_value)
        elif g.measure_type == "area" and isinstance(g.measure_value, (int, float)):
            unit.total_area += float(g.measure_value)
        else:
            unit.count_elements += g.element_count
    for unit in units.values():
        unit.total_volume = round(unit.total_volume, 2)
        unit.total_area = round(unit.total_area, 2)
    return list(units.values())


def group_dict(g: LeafGroup) -> dict:
    d = asdict(g)
    d["title"] = g.title
    d["needs_lookup"] = g.needs_lookup
    d["measure_text"] = g.measure_text
    return d


MATERIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "material": {"type": "string"},
        "concrete_class": {"type": "string"},
        "waterproofing": {"type": "string"},
        "frost_resistance": {"type": "string"},
        "gost": {"type": "string"},
        "quote": {"type": "string"},
        "page": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "material": {"type": "string"},
                },
                "required": ["scope", "material"],
            },
        },
        "note": {"type": "string"},
    },
    "required": ["found", "material", "quote", "page", "confidence"],
}

BUILDING_SCHEMA = {
    "type": "object",
    "properties": {
        "storeys": {"type": "string"},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "page": {"type": "integer"},
                },
                "required": ["name", "value", "page"],
            },
        },
    },
    "required": ["storeys", "parameters"],
}

POS_WORKS_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "works": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "details": {"type": "string"},
                    "page": {"type": "integer"},
                },
                "required": ["name", "details", "page"],
            },
        },
        "note": {"type": "string"},
    },
    "required": ["found", "works"],
}

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                    "page": {"type": "integer"},
                },
                "required": ["name", "value", "quote", "page"],
            },
        }
    },
    "required": ["parameters"],
}

TYPE_HINTS = {
    "перекрытия": "Ищи фразы про плиты: «фундаментная плита», «плита перекрытия», "
                  "«плита покрытия». НЕ используй материал вертикальных конструкций "
                  "(стен, пилонов, колонн, простенков).",
    "стены": "Ищи фразы про стены, пилоны, простенки, вертикальные конструкции. "
             "НЕ используй материал плит перекрытия и фундаментной плиты.",
    "колонны": "Ищи фразы про колонны и пилоны. НЕ используй материал стен и плит, "
               "если про колонны или пилоны сказано отдельно.",
    "лестницы": "Ищи фразы про лестничные марши и лестничные площадки. "
                "НЕ используй материал стен и перекрытий.",
    "пандусы": "Ищи фразы про пандусы, рампы и их плиты. "
               "НЕ используй материал стен и перекрытий.",
    "фундаментная плита": "Ищи фразы про фундаментную плиту. НЕ используй "
                          "материал стен, перекрытий и бетонной подготовки "
                          "(подготовка - отдельный слой из тощего бетона).",
    "гидроизоляция": "Ищи фразы про гидроизоляцию: мембраны, рулонные и "
                     "обмазочные материалы, праймеры. Класс бетона тут "
                     "не нужен, если он не относится к самой гидроизоляции.",
}

TYPE_ROOTS = (
    ("балк", ("балк", "балок", "ригел")),
    ("ригел", ("ригел", "балк", "балок")),
    ("колонн", ("колонн", "пилон")),
    ("пилон", ("пилон", "колонн")),
    ("лестни", ("лестни", "марш", "площадк")),
    ("перекрыт", ("перекрыт", "плит")),
    ("покрыт", ("покрыт", "кровл", "плит")),
    ("простен", ("простен", "стен")),
    ("стен", ("стен",)),
    ("фундамент", ("фундамент", "плит", "ростверк", "сва")),
    ("ростверк", ("ростверк", "сва", "фундамент")),
    ("сва", ("сва", "ростверк", "фундамент")),
    ("плит", ("плит",)),
    ("приям", ("приям",)),
    ("парапет", ("парапет",)),
    ("рамп", ("рамп", "пандус")),
    ("пандус", ("пандус", "рамп")),
    ("шв", ("шов", "шва", "швы", "деформацион")),
    ("гидроизоляц", ("гидроизоляц", "изоляц", "мембран")),
)

UNDERGROUND_MARKERS = ("подземн", "подвал")
ABOVEGROUND_KEYS = ("этаж", "надземн")
CONTEXT_WINDOW = 400

_CLASS_RE = re.compile(r"[BВ]\s?\d+(?:[.,]\d+)?")
_W_RE = re.compile(r"\bW\s?(\d{1,2})\b")
_F_RE = re.compile(r"\bF\s?(\d{2,3})\b")
_GOST_RE = re.compile(r"ГОСТ\s?\d{4,5}(?:\.\d{1,3})?(?:[-–—]\d{2,4})?")
_EMPTY_RE = re.compile(r"не указан|не найден|отсутств|нет данных|^[-—]+$", re.IGNORECASE)


@dataclass
class UnitResult:
    unit_key: str
    element_type: str
    location: str
    qualifiers: List[str]
    group_indexes: List[int]
    element_count: int
    total_volume: float
    total_area: float = 0.0
    count_elements: int = 0
    measure_text: str = ""
    pages_used: List[int] = field(default_factory=list)
    found: bool = False
    material: str = ""
    concrete_class: str = ""
    waterproofing: str = ""
    frost_resistance: str = ""
    gost: str = ""
    quote: str = ""
    page: Optional[int] = None
    confidence: str = ""
    variants: List[dict] = field(default_factory=list)
    note: str = ""
    error: str = ""


def _norm_ru(text: str) -> str:
    return text.lower().replace("ё", "е")


def _norm_class(c: str) -> str:
    return c.replace(" ", "").replace("В", "B").replace(",", ".")


def _page_num(value) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and value else None


def _add_note(result: UnitResult, text: str):
    result.note = (result.note + " " if result.note else "") + text


def type_roots(element_type: str) -> Optional[tuple]:
    et = _norm_ru(element_type)
    return next((r for key, r in TYPE_ROOTS if key in et), None)


def quote_matches_type(element_type: str, quote: str) -> Optional[bool]:
    roots = type_roots(element_type)
    if not roots:
        return None
    quote_norm = _norm_ru(quote)
    return any(root in quote_norm for root in roots)


def _type_hint(element_type: str) -> str:
    et = element_type.strip().lower()
    if et in TYPE_HINTS:
        return TYPE_HINTS[et]
    for key, hint in TYPE_HINTS.items():
        if key[:5] in et:
            return hint
    return ""


def split_material(material: str) -> dict:
    text = material or ""
    cls = _CLASS_RE.search(text)
    w = _W_RE.search(text)
    f = _F_RE.search(text)
    gost = _GOST_RE.search(text)
    return {
        "concrete_class": _norm_class(cls.group()) if cls else "",
        "waterproofing": f"W{w.group(1)}" if w else "",
        "frost_resistance": f"F{f.group(1)}" if f else "",
        "gost": gost.group() if gost else "",
    }


def _table_pairs(pages: List[Page]) -> List[tuple]:
    pairs = []
    for p in pages:
        idx = p.text.find(TABLE_MARKER)
        if idx == -1:
            continue
        for line in p.text[idx:].splitlines():
            if " - " not in line or ":" not in line:
                continue
            _, _, tail = line.partition(" - ")
            for seg in tail.split(";"):
                scope, sep, val = seg.partition(":")
                scope = " ".join(scope.split()).lower()
                val = val.strip()
                if sep and len(scope) >= 5 and _CLASS_RE.search(val):
                    pairs.append((scope, val))
    return pairs


def _clean_variants(variants: List[dict], main_material: str) -> List[dict]:
    main_norm = " ".join(str(main_material).split()).lower()
    main_has_class = bool(_CLASS_RE.search(main_material or ""))
    cleaned = []
    seen = set()
    for v in variants:
        material = " ".join(str(v.get("material", "") or "").split())
        scope = " ".join(str(v.get("scope", "") or "").split())
        if not material or not material.strip("-—–.,:; "):
            continue
        if main_has_class and not _CLASS_RE.search(material):
            continue
        key = material.lower()
        if key == main_norm or key in seen:
            continue
        seen.add(key)
        cleaned.append({"scope": scope, "material": material})
    return cleaned


def _fix_variants_by_tables(variants: List[dict], pages: List[Page]) -> bool:
    pairs = _table_pairs(pages)
    if not pairs:
        return False
    fixed = False
    for v in variants:
        scope = " ".join(str(v.get("scope", "")).split()).lower()
        mat_class = _CLASS_RE.search(str(v.get("material", "")))
        if len(scope) < 5 or not mat_class:
            continue
        for s, val in pairs:
            if s in scope or scope in s:
                val_class = _CLASS_RE.search(val)
                if val_class and _norm_class(val_class.group()) != _norm_class(mat_class.group()):
                    v["material"] = val
                    fixed = True
                break
    return fixed


def _norm_material_text(text: str) -> str:
    return re.sub(r"\bв(?=\s?\d)", "b", _norm_ru(text))


def _underground_context(quote: str, pages: List[Page]) -> bool:
    quote_norm = normalize(quote)
    if not quote_norm:
        return False
    if any(m in quote_norm for m in UNDERGROUND_MARKERS):
        return True
    for p in pages:
        text = normalize(p.text)
        i = text.find(quote_norm)
        if i == -1:
            continue
        window = text[max(0, i - CONTEXT_WINDOW): i + len(quote_norm) + CONTEXT_WINDOW]
        return any(m in window for m in UNDERGROUND_MARKERS)
    return False


def _aboveground_variant(variants: List[dict]) -> Optional[dict]:
    for key in ABOVEGROUND_KEYS:
        for v in variants:
            scope = _norm_ru(str(v.get("scope", "") or ""))
            if key not in scope:
                continue
            if any(m in scope for m in UNDERGROUND_MARKERS):
                continue
            if _CLASS_RE.search(str(v.get("material", "") or "")):
                return v
    return None


def _sentences_with(pages: List[Page], groups: List[tuple]) -> List[tuple]:
    groups = [g for g in groups if g]
    if not groups:
        return []
    found = []
    for p in pages:
        text = " ".join(p.text.split())
        for sentence in re.split(r"(?<=[.;])\s+(?=[А-ЯЁA-Z])", text):
            low = _norm_material_text(sentence)
            if all(any(v in low for v in g) for g in groups):
                found.append((sentence.strip(), p.number))
    return found


def _supporting_sentences(pages: List[Page], material: str, element_type: str,
                          extra: tuple = ()) -> List[tuple]:
    parts = split_material(material)
    marks = [(_norm_material_text(x),) for x in
             (parts["concrete_class"], parts["waterproofing"], parts["frost_resistance"]) if x]
    if not marks:
        return []
    roots = type_roots(element_type) or ()
    return (_sentences_with(pages, marks + [roots, extra])
            or _sentences_with(pages, marks + [roots]))


def _fix_underground_mixup(result: UnitResult, pages: List[Page]) -> str:
    if not result.found or "надземн" not in _norm_ru(result.location):
        return ""
    if not _underground_context(result.quote, pages):
        return ""

    variant = _aboveground_variant(result.variants)
    if variant is not None:
        scope = _norm_ru(str(variant.get("scope", "") or ""))
        key = next((k for k in ABOVEGROUND_KEYS if k in scope), "")
        candidates = _supporting_sentences(
            pages, str(variant.get("material", "")), result.element_type,
            (key,) if key else ())
        if candidates:
            wrong_material = result.material
            result.material = str(variant.get("material", ""))
            for name, value in split_material(result.material).items():
                setattr(result, name, value)
            result.variants = [v for v in result.variants if v is not variant]
            result.variants.insert(0, {"scope": "подземная часть (по цитате модели)",
                                       "material": wrong_material})
            result.quote, result.page = candidates[0]
            if result.confidence == "high":
                result.confidence = "medium"
            return "swapped"

    quote_norm = normalize(result.quote)
    for sentence, page in _supporting_sentences(pages, result.material, result.element_type):
        if normalize(sentence) == quote_norm:
            continue
        if _underground_context(sentence, pages):
            continue
        result.quote, result.page = sentence, page
        return "requoted"

    if result.confidence == "high":
        result.confidence = "medium"
    return "flagged"


def _filter_by_section(pages: List[Page], location: str) -> List[Page]:
    loc = location.lower()
    if "надземн" in loc:
        wrong = "подземной части"
    elif "подземн" in loc:
        wrong = "надземной части"
    else:
        return pages
    kept = [p for p in pages if wrong not in p.section.lower()]
    return kept or pages


def _unit_result(unit: ExtractionUnit, **kwargs) -> UnitResult:
    return UnitResult(
        unit_key=unit.key,
        element_type=unit.element_type,
        location=unit.location,
        qualifiers=unit.qualifiers,
        group_indexes=unit.group_indexes,
        element_count=unit.element_count,
        total_volume=unit.total_volume,
        total_area=unit.total_area,
        count_elements=unit.count_elements,
        measure_text=unit.measure_text,
        **kwargs,
    )


def extract_unit(llm: Optional[LLMClient], index: PageIndex, unit: ExtractionUnit,
                 top_k: int) -> UnitResult:
    pages = index.top_pages(unit_query(unit.element_type, unit.location), top_k)
    pages = _filter_by_section(pages, unit.location)
    result = _unit_result(unit, pages_used=[p.number for p in pages])
    if not pages:
        result.error = "Релевантные страницы в документе не найдены"
        return result
    if llm is None:
        result.error = "LLM не задана"
        return result

    user = PROMPTS["material_user"].format(
        element_type=unit.element_type,
        location=unit.location,
        qualifiers=", ".join(unit.qualifiers) or "-",
        element_count=unit.element_count,
        measure_line=unit.measure_line,
        type_hint=_type_hint(unit.element_type),
        context=format_context(pages),
    )
    try:
        data = llm.complete_json(PROMPTS["material_system"], user, schema=MATERIAL_SCHEMA)
    except LLMError as e:
        result.error = str(e)
        return result

    result.found = bool(data.get("found"))
    result.material = str(data.get("material", "") or "")
    result.concrete_class = str(data.get("concrete_class", "") or "")
    result.waterproofing = str(data.get("waterproofing", "") or "")
    result.frost_resistance = str(data.get("frost_resistance", "") or "")
    result.gost = str(data.get("gost", "") or "")
    for key, value in split_material(result.material).items():
        if not getattr(result, key):
            setattr(result, key, value)
    result.quote = str(data.get("quote", "") or "")
    result.page = _page_num(data.get("page"))
    result.confidence = str(data.get("confidence", "") or "")
    variants = data.get("variants")
    if isinstance(variants, list):
        result.variants = _clean_variants([v for v in variants if isinstance(v, dict)],
                                          result.material)
    result.note = str(data.get("note", "") or "")

    if _fix_variants_by_tables(result.variants, pages):
        _add_note(result, "Классы бетона в вариантах сверены со структурой таблиц документа.")

    mixup = _fix_underground_mixup(result, pages)
    if mixup == "swapped":
        _add_note(result, "Основной материал заменён вариантом для надземной части: "
                          "модель процитировала описание подземной части.")
    elif mixup == "requoted":
        _add_note(result, "Цитата заменена: прежняя относилась к подземной части.")
    elif mixup == "flagged":
        _add_note(result, "Цитата относится к описанию подземной части здания - "
                          "материал надземной части нужно проверить.")

    if result.found:
        real_page = find_quote_page(result.quote, pages, result.page)
        if real_page is None:
            if result.confidence == "high":
                result.confidence = "medium"
            _add_note(result, "Цитата не сверилась с текстом документа автоматически.")
        elif real_page != result.page:
            result.page = real_page
            _add_note(result, "Номер страницы уточнён по тексту цитаты.")

    if result.found and result.quote and \
            quote_matches_type(result.element_type, result.quote) is False:
        if result.confidence == "high":
            result.confidence = "medium"
        _add_note(result, "Материал выведен по аналогии: в цитате не упоминаются эти конструкции.")
    return result


def extract_building(llm: Optional[LLMClient], index: PageIndex, top_k: int) -> dict:
    pages = index.top_pages(BUILDING_INFO_QUERY, top_k + 2)
    if not pages:
        return {"error": "Релевантные страницы не найдены"}
    if llm is None:
        return {"error": "LLM не задана", "pages_used": [p.number for p in pages]}
    user = PROMPTS["building_user"].format(context=format_context(pages))
    try:
        data = llm.complete_json(PROMPTS["building_system"], user, schema=BUILDING_SCHEMA)
    except LLMError as e:
        return {"error": str(e)}
    parameters = []
    for p in data.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        value = str(p.get("value", "") or "").strip()
        if not value or _EMPTY_RE.search(value):
            continue
        parameters.append({
            "name": str(p.get("name", "") or ""),
            "value": value,
            "page": _page_num(p.get("page")),
        })
    storeys = str(data.get("storeys", "") or "").strip()
    if _EMPTY_RE.search(storeys):
        storeys = ""
    return {
        "storeys": storeys,
        "parameters": parameters,
        "pages_used": [p.number for p in pages],
    }


POS_WORK_CATEGORIES = [
    ("Бетонные работы",
     "бетонные работы бетонирование укладка подача бетонной смеси уход за "
     "бетоном зимнее бетонирование прогрев бетона монолитные конструкции "
     "автобетононасос бетононасос автобетоносмеситель механизмы"),
    ("Опалубочные работы",
     "опалубка опалубочные работы щиты монтаж демонтаж опалубки распалубка "
     "оборачиваемость опалубки башенный кран механизмы"),
    ("Арматурные работы",
     "арматура арматурные работы каркасы сетки вязка сварка стыковка "
     "арматурных стержней муфтовые соединения кран механизмы"),
]

_MECHANISM_STEMS = {
    "Бетонные работы": r"бетонные\s+работы",
    "Опалубочные работы": r"опалубочные(?:\s+и\s+арматурные)?\s+работы",
    "Арматурные работы": r"(?:опалубочные\s+и\s+)?арматурные\s+работы",
}


def extract_mechanisms(pages: List[Page], category: str) -> List[dict]:
    stem = _MECHANISM_STEMS.get(category)
    if not stem:
        return []
    pattern = re.compile(stem + r"\s*[-–—]\s*([^;]{5,200}?)(?=;|\.\s+[А-ЯЁ]|$)", re.IGNORECASE)
    results, seen = [], set()
    for p in pages:
        text = " ".join(p.text.split())
        for m in pattern.finditer(text):
            detail = m.group(1).strip().rstrip(".,")
            key = detail.lower()
            if key in seen:
                continue
            seen.add(key)
            before = text[max(0, m.start() - 400):m.start()].lower()
            i_nad = before.rfind("надземн")
            i_pod = before.rfind("подземн")
            if i_nad > i_pod:
                scope = " (надземная часть)"
            elif i_pod >= 0:
                scope = " (подземная часть)"
            else:
                scope = ""
            results.append({
                "name": f"Способ производства работ{scope}",
                "details": detail,
                "page": p.number,
            })
    if category == "Бетонные работы":
        for p in pages:
            text = " ".join(p.text.split())
            for sent in re.split(r"[;•·]|\.\s+(?=[А-ЯЁ])", text):
                sent = sent.strip()
                if not (30 <= len(sent) <= 250):
                    continue
                if not re.search(r"(авто)?бетононасос", sent, re.IGNORECASE):
                    continue
                if not re.search(r"бетониров|подач|помощ|производ|предусмотр", sent, re.IGNORECASE):
                    continue
                if re.search(r"\bштук\b|\bшт\.|кол-во", sent, re.IGNORECASE):
                    continue
                key = sent.lower()[:60]
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "name": "Способ бетонирования",
                    "details": sent.rstrip(".,"),
                    "page": p.number,
                })
                if len(results) >= 8:
                    return results
    return results


def extract_pos_works(llm: Optional[LLMClient], index: PageIndex, category: str,
                      query: str, top_k: int) -> dict:
    result = {"category": category, "found": False, "works": [],
              "note": "", "pages_used": [], "error": ""}
    pages = index.top_pages(query, top_k)
    result["pages_used"] = [p.number for p in pages]
    if not pages:
        result["error"] = "Релевантные страницы в документе не найдены"
        return result
    works = []
    if llm is not None:
        user = PROMPTS["pos_works_user"].format(category=category, context=format_context(pages))
        try:
            data = llm.complete_json(PROMPTS["pos_works_system"], user, schema=POS_WORKS_SCHEMA)
        except LLMError as e:
            result["error"] = str(e)
            return result
        result["found"] = bool(data.get("found"))
        result["note"] = str(data.get("note", "") or "")
        for w in data.get("works") or []:
            if not isinstance(w, dict):
                continue
            works.append({
                "name": str(w.get("name", "") or ""),
                "details": str(w.get("details", "") or ""),
                "page": _page_num(w.get("page")),
            })
    mechanisms = extract_mechanisms(pages, category)
    existing = {w["details"][:60].lower() for w in works}
    for mech in mechanisms:
        if mech["details"][:60].lower() not in existing:
            works.append(mech)
    if mechanisms:
        result["found"] = True
    result["works"] = works
    return result


EMPTY_ANSWERS = {
    "не указано", "не указан", "не указана", "не указаны", "нет", "нет данных",
    "не найдено", "не найден", "отсутствует", "отсутствуют", "неизвестно",
    "не определено", "не определен", "не применимо", "н/д", "—", "-",
}


def load_registry(path: Union[str, Path, None] = None) -> dict:
    return json.loads(Path(path or REGISTRY_PATH).read_text(encoding="utf-8"))


def norm_name(name: str) -> str:
    name = name.lower().replace("ё", "е").split(",")[0]
    return "".join(ch for ch in name if ch.isalnum())


def _norm_value(value: str) -> str:
    return value.lower().replace(" ", "")


def _first_group(m) -> str:
    if m.lastindex:
        for g in m.groups():
            if g:
                return g
    return m.group(0)


def _window(text: str, start: int, end: int, pad: int = 110) -> str:
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    return " ".join(text[left:right].split())[:260]


def harvest_field(pages: List[Page], field_def: dict, max_matches: int = 400) -> List[dict]:
    flags = 0 if field_def.get("case") else re.IGNORECASE
    try:
        pattern = re.compile(field_def["re"], flags)
    except re.error:
        return []
    context = [c.lower() for c in field_def.get("context", [])]
    prefix = field_def.get("prefix", "")
    found: Dict[str, dict] = {}
    total = 0
    for page in pages:
        text = page.text
        for m in pattern.finditer(text):
            if total >= max_matches:
                break
            if context:
                around = text[max(0, m.start() - 150): m.end() + 150].lower()
                if not any(c in around for c in context):
                    continue
            raw = " ".join(_first_group(m).split())[:120]
            if not raw:
                continue
            value = f"{prefix}{raw}" if prefix else raw
            total += 1
            item = found.get(_norm_value(value))
            if item is None:
                found[_norm_value(value)] = {
                    "value": value,
                    "source": "шаблон",
                    "pages": [page.number],
                    "page": page.number,
                    "section": page.section,
                    "quote": _window(text, m.start(), m.end()),
                    "verified": True,
                    "count": 1,
                }
            else:
                item["count"] += 1
                if page.number not in item["pages"]:
                    item["pages"].append(page.number)
    return sorted(found.values(), key=lambda v: (-v["count"], v["value"]))


def ask_llm_params(llm: LLMClient, pages: List[Page], ptype: dict) -> dict:
    fields = "\n".join(f"- {f['name']}" for f in ptype["fields"])
    user = PROMPTS["params_user"].format(topic=ptype["name"], fields=fields,
                                         context=format_context(pages))
    t0 = time.time()
    try:
        data = llm.complete_json(PROMPTS["params_system"], user, PARAMS_SCHEMA)
    except LLMError as e:
        return {"error": str(e)[:300], "items": [], "sec": round(time.time() - t0, 1)}
    items = []
    for raw in (data.get("parameters") or [])[:60]:
        if not isinstance(raw, dict):
            continue
        value = " ".join(str(raw.get("value", "")).split())[:200]
        if not value or value.lower().strip(" .-") in EMPTY_ANSWERS:
            continue
        quote = " ".join(str(raw.get("quote", "")).split())[:400]
        claimed = raw.get("page") if isinstance(raw.get("page"), int) else None
        real_page = find_quote_page(quote, pages, claimed)
        items.append({
            "name": " ".join(str(raw.get("name", "")).split())[:120],
            "value": value,
            "source": "модель",
            "pages": [real_page] if real_page else [],
            "page": real_page or claimed,
            "quote": quote,
            "verified": real_page is not None,
            "count": 1,
        })
    return {"items": items, "sec": round(time.time() - t0, 1)}


def _merge(template_values: List[dict], model_values: List[dict]) -> List[dict]:
    merged: Dict[str, dict] = {}
    for v in template_values:
        merged[_norm_value(v["value"])] = dict(v)
    for v in model_values:
        key = _norm_value(v["value"])
        old = merged.get(key)
        if old is None:
            merged[key] = dict(v)
            continue
        old["source"] = "шаблон + модель"
        if v["verified"] and v.get("quote"):
            old["quote"] = v["quote"]
            if v.get("page"):
                old["page"] = v["page"]
    order = {"шаблон + модель": 0, "шаблон": 1, "модель": 2}
    return sorted(merged.values(),
                  key=lambda v: (order.get(v["source"], 3), -v["count"], v["value"]))


def extract_params_type(llm: Optional[LLMClient], index: PageIndex, pages: List[Page],
                        ptype: dict, top_k: int) -> dict:
    top_pages = index.top_pages(ptype["query"], top_k) if pages else []
    llm_result = ask_llm_params(llm, top_pages, ptype) if (llm and top_pages) else {}
    by_field: Dict[str, List[dict]] = {}
    for it in llm_result.get("items", []):
        by_field.setdefault(norm_name(it["name"]), []).append(it)

    known = {norm_name(f["name"]) for f in ptype["fields"]}
    type_need = ptype.get("smeta_need", "")
    fields = []
    for field_def in ptype["fields"]:
        fields.append({
            "name": field_def["name"],
            "smeta_need": field_def.get("smeta_need", type_need),
            "values": _merge(harvest_field(pages, field_def),
                             by_field.get(norm_name(field_def["name"]), [])),
        })
    extra = [it for key, items in by_field.items() if key not in known for it in items]
    return {
        "id": ptype["id"],
        "name": ptype["name"],
        "doc": ptype["doc"],
        "smeta": ptype["smeta"],
        "smeta_need": type_need,
        "pages_used": [p.number for p in top_pages],
        "fields": fields,
        "extra": extra,
        "error": llm_result.get("error", ""),
        "sec": llm_result.get("sec", 0),
    }


Progress = Optional[Callable[[str], None]]


def _say(progress: Progress, stage: str):
    if progress:
        progress(stage)


def _document_info(name: str, pages: List[Page]) -> dict:
    info = extract_object_info(pages)
    return {"name": name, "pages": len(pages), "object": info["address"], "cipher": info["cipher"]}


def _params(llm: Optional[LLMClient], index: PageIndex, pages: List[Page], doc: str,
            config: Config, registry: Optional[dict], progress: Progress) -> List[dict]:
    registry = registry or load_registry(config.registry_path)
    results = []
    for ptype in registry["types"]:
        if ptype["doc"] != doc:
            continue
        _say(progress, f"{doc}: параметры - {ptype['name']}")
        results.append(extract_params_type(llm, index, pages, ptype, config.retrieval_top_k))
    return results


def parse_pz(pages: List[Page], llm: Optional[LLMClient], config: Config,
             raw_groups: Optional[List[dict]] = None, name: str = "",
             registry: Optional[dict] = None, progress: Progress = None) -> dict:
    index = PageIndex(pages)
    groups = parse_groups(raw_groups) if raw_groups else []
    units = build_units(groups)
    unit_results: List[Optional[dict]] = [None] * len(units)
    workers = max(1, min(config.llm_parallel, len(units) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_unit, llm, index, unit, config.retrieval_top_k): i
                   for i, unit in enumerate(units)}
        done = 0
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                unit_results[i] = asdict(fut.result())
            except Exception as e:
                unit_results[i] = asdict(_unit_result(units[i], error=f"{type(e).__name__}: {e}"))
            done += 1
            _say(progress, f"ПЗ: материалы {done} из {len(units)}")
    _say(progress, "ПЗ: высотные характеристики")
    building = extract_building(llm, index, config.retrieval_top_k)
    return {
        "document": _document_info(name, pages),
        "groups": [group_dict(g) for g in groups],
        "units": unit_results,
        "building": building,
        "parameters": _params(llm, index, pages, "КР", config, registry, progress),
    }


def parse_pos(pages: List[Page], llm: Optional[LLMClient], config: Config, name: str = "",
              registry: Optional[dict] = None, progress: Progress = None) -> dict:
    index = PageIndex(pages)
    _say(progress, "ПОС: высотность")
    building = extract_building(llm, index, config.retrieval_top_k)
    works = []
    for category, query in POS_WORK_CATEGORIES:
        _say(progress, f"ПОС: {category.lower()}")
        works.append(extract_pos_works(llm, index, category, query, config.retrieval_top_k))
    return {
        "document": _document_info(name, pages),
        "building": building,
        "works": works,
        "parameters": _params(llm, index, pages, "ПОС", config, registry, progress),
    }


# =========================================================================
#  Глобальные константы подбора работ из ПОС (global_constants из
#  data/works_classification.json: тип опалубки, высота здания, схема
#  бетонирования, тип крана, уход за бетоном и т.д.)
# =========================================================================

def load_global_constants_schema() -> List[dict]:
    """Схема глобальных констант из data/works_classification.json."""
    return json.loads(
        WORKS_CLASSIFICATION_PATH.read_text(encoding="utf-8")
    ).get("global_constants", [])


# Запросы для отбора релевантных страниц по каждой константе
POS_CONSTANT_QUERIES = {
    "formwork_type": (
        "опалубка опалубочные работы щитовая мелкощитовая крупнощитовая "
        "скользящая самоподъемная переставная балочно-ригельная монолитные "
        "конструкции монтаж демонтаж опалубки"
    ),
    "building_height_m": (
        "высота здания этажность этажей подземный этаж отметка верхняя "
        "предельная высота количество этажей"
    ),
    "concrete_curing_season": (
        "уход за бетоном зимнее бетонирование прогрев бетона тепловлагозащита "
        "противоморозные добавки электропрогрев"
    ),
    "concrete_placement_scheme": (
        "подача бетонной смеси автобетононасос бетононасос бадья кран "
        "бетонирование укладка бетонной смеси"
    ),
    "crane_type": (
        "башенный кран автомобильный кран гусеничный кран монтажные работы "
        "грузоподъемные механизмы краны"
    ),
    # floor_height, crane_capacity, bucket_capacity, equipment_power —
    # константы схемы works_classification.json (параметры следующего этапа);
    # soil_group, movement_distance — вне схемы.
    "floor_height": (
        "высота этажа высота типового этажа отметка чистого пола "
        "высота помещений этажей"
    ),
    "soil_group": (
        "группа грунтов грунты основания разработка грунта группа по трудности "
        "разработки грунты встречающиеся при производстве работ"
    ),
    "movement_distance": (
        "перемещение грунта перевозка расстояние транспортировка отвозка "
        "каьер резерв грунта"
    ),
    "crane_capacity": (
        "грузоподъемность крана тонн краны монтажные работы подбор крана"
    ),
    "bucket_capacity": (
        "вместимость ковша экскаватор скрепер землеройные машины"
    ),
    "equipment_power": (
        "мощность оборудования кВт машины механизмы бульдозер компрессор"
    ),
}

# Поле реестра (params_registry.json, тип pos_global_constants) для каждой константы
POS_CONSTANT_FIELDS = {
    "formwork_type": "Тип опалубки",
    "building_height_m": "Высота здания, м",
    "concrete_curing_season": "Сезон ухода за бетоном",
    "concrete_placement_scheme": "Схема бетонирования",
    "crane_type": "Тип монтажного крана",
    # floor_height, crane_capacity, bucket_capacity, equipment_power входят
    # в схему global_constants works_classification.json (параметры следующего
    # этапа подбора — выбор строк/норм внутри таблиц ГЭСН).
    "floor_height": "Высота этажа, м",
    "soil_group": "Группа грунтов",
    "movement_distance": "Расстояние перемещения грунта",
    "crane_capacity": "Грузоподъемность крана",
    "bucket_capacity": "Вместимость ковша",
    "equipment_power": "Мощность оборудования",
}

# Заголовки дополнительных констант ПОС (для констант, отсутствующих в схеме
# works_classification.json — заголовок берётся отсюда; floor_height, crane_type,
# crane_capacity, bucket_capacity, equipment_power теперь входят в схему).
EXTRA_POS_CONSTANTS = {
    "soil_group": "Группа грунтов",
    "movement_distance": "Расстояние перемещения грунта",
}

_HEIGHT_RANGES = (
    (30, "до 30"),
    (40, "30-40"),
    (57, "40-57"),
    (75, "57-75"),
    (105, "75-105"),
    (150, "105-150"),
    (250, "150-250"),
)


def _map_formwork_type(values: List[str]) -> Optional[str]:
    text = normalize(" ".join(values))
    if re.search(r"самоподъем|скользящ|переставн", text):
        return "переставная (скользящая/самоподъемная)"
    if re.search(r"крупнощит|балочно.?ригельн", text):
        return "крупнощитовая"
    if re.search(r"мелкощит|деревометаллич|industrial|peri|doka", text):
        return "индустриальная деревометаллическая мелкощитовая"
    if re.search(r"щитов|щитовая", text):
        return "деревянная щитовая"
    return None


def _map_building_height(values: List[str]) -> Optional[str]:
    for v in values:
        m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)", str(v))
        if not m:
            continue
        try:
            h = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if h < 5:
            # Слишком мало для высоты здания - скорее этажность или высота этажа
            continue
        if h > 250:
            return "свыше 200 (шпиль)"
        for limit, label in _HEIGHT_RANGES:
            if h <= limit:
                return label
    return None


def _map_curing_season(values: List[str]) -> Optional[str]:
    text = normalize(" ".join(values))
    if re.search(r"зимн|холодн|прогрев|тепловлагозащит|противоморозн|"
                 r"электропрогрев|термомат|утепл", text):
        return "холодный период (тепловлагозащита)"
    if "уход" in text and "бетон" in text:
        return "среднесуточная температура +5 и выше"
    return None


def _map_placement_scheme(values: List[str]) -> Optional[str]:
    text = normalize(" ".join(values))
    if re.search(r"бетононасос", text):
        return "бетононасос"
    if re.search(r"бадь", text):
        return "кран-бадья"
    return None


def _map_crane_type(values: List[str]) -> Optional[str]:
    """Тип крана — нормализация к значениям схемы works_classification.json."""
    text = normalize(" ".join(values))
    if re.search(r"башенн", text):
        return "башенный"
    if re.search(r"козлов", text):
        return "козловой"
    if re.search(r"мостов", text):
        return "мостовой"
    if re.search(r"подвес", text):
        return "подвесной"
    if re.search(r"гусеничн|пневмоколесн", text):
        return "на гусеничном/пневмоколесном ходу"
    if re.search(r"автомобильн|автокран|стрелов", text):
        return "самоходный стреловой"
    return None


def _map_floor_height(values: List[str]) -> Optional[float]:
    """Высота этажа, м — первое число в правдоподобном диапазоне 1.5–12 м."""
    for v in values:
        m = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)", str(v))
        if not m:
            continue
        try:
            h = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 1.5 <= h <= 12:
            return h
    return None


def _map_soil_group(values: List[str]) -> Optional[int]:
    """Группа грунтов — целое 1–11 (группы по трудности разработки)."""
    for v in values:
        m = re.search(r"(\d{1,2})", str(v))
        if not m:
            continue
        group = int(m.group(1))
        if 1 <= group <= 11:
            return group
    return None


def _map_movement_distance(values: List[str]) -> Optional[str]:
    """Расстояние перемещения/перевозки — число с единицей (м или км)."""
    for v in values:
        m = re.search(r"(\d{1,4}(?:[.,]\d{1,2})?)\s?(м|км)\b", str(v),
                      re.IGNORECASE)
        if not m:
            continue
        num = m.group(1).replace(",", ".")
        return f"{num} {m.group(2).lower()}"
    return None


def _map_crane_capacity(values: List[str]) -> Optional[float]:
    """Грузоподъемность крана, т — первое число в диапазоне 1–1000 т."""
    for v in values:
        m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)", str(v))
        if not m:
            continue
        try:
            t = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 1 <= t <= 1000:
            return t
    return None


def _map_bucket_capacity(values: List[str]) -> Optional[float]:
    """Вместимость ковша, м3 — первое число в диапазоне 0.1–50 м3."""
    for v in values:
        m = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)", str(v))
        if not m:
            continue
        try:
            v3 = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 0.1 <= v3 <= 50:
            return v3
    return None


def _map_equipment_power(values: List[str]) -> Optional[float]:
    """Мощность оборудования, кВт — первое число в диапазоне 1–1000 кВт."""
    for v in values:
        m = re.search(r"(\d{1,4}(?:[.,]\d{1,2})?)", str(v))
        if not m:
            continue
        try:
            kw = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 1 <= kw <= 1000:
            return kw
    return None


# Нормализация значения константы из «сырых» находок в значение
# из списка values схемы works_classification.json
CONSTANT_MAPPERS = {
    "formwork_type": _map_formwork_type,
    "building_height_m": _map_building_height,
    "concrete_curing_season": _map_curing_season,
    "concrete_placement_scheme": _map_placement_scheme,
    "crane_type": _map_crane_type,
    # Дополнительные константы ПОС (числовые/проектные значения)
    "floor_height": _map_floor_height,
    "soil_group": _map_soil_group,
    "movement_distance": _map_movement_distance,
    "crane_capacity": _map_crane_capacity,
    "bucket_capacity": _map_bucket_capacity,
    "equipment_power": _map_equipment_power,
}


def extract_pos_constants(llm: Optional[LLMClient], index: PageIndex,
                          pages: List[Page], config: Config,
                          progress: Progress = None,
                          schema: Optional[List[dict]] = None) -> dict:
    """Извлекает глобальные константы подбора работ из страниц ПОС.

    Для каждой константы из схемы works_classification.json:
    1) отбираются релевантные страницы (BM25-индекс);
    2) значения собираются regex-шаблонами из реестра (тип pos_global_constants);
    3) при наличии LLM значения дополнительно ищутся моделью;
    4) «сырые» значения нормализуются к допустимым значениям константы.

    Возвращает: {имя константы: {value, raw_values, quote, page, source, found}}.
    """
    schema = schema if schema is not None else load_global_constants_schema()
    registry = load_registry(config.registry_path)
    ptype = next((t for t in registry["types"]
                  if t.get("id") == "pos_global_constants"), None)
    fields_by_name = {f["name"]: f for f in (ptype or {}).get("fields", [])}

    # Сначала константы из схемы works_classification.json, затем
    # дополнительные константы ПОС (EXTRA_POS_CONSTANTS — вне схемы:
    # soil_group, movement_distance).
    schema_by_name = {const.get("name"): const for const in schema}
    ordered_names = [n for n in schema_by_name if n in POS_CONSTANT_FIELDS]
    ordered_names += [n for n in POS_CONSTANT_FIELDS if n not in schema_by_name]

    result: Dict[str, dict] = {}
    for name in ordered_names:
        const = schema_by_name.get(name) or {
            "name": name,
            "title": EXTRA_POS_CONSTANTS.get(name, name),
        }
        field_name = POS_CONSTANT_FIELDS[name]
        _say(progress, f"ПОС: константа - {const.get('title', name)}")
        query = POS_CONSTANT_QUERIES.get(name, name)
        top_pages = index.top_pages(query, config.retrieval_top_k + 2)

        template_values: List[dict] = []
        field_def = fields_by_name.get(field_name)
        if field_def and top_pages:
            template_values = harvest_field(top_pages, field_def)

        model_items: List[dict] = []
        if llm is not None and top_pages:
            pseudo_type = {
                "id": name,
                "name": const.get("title", field_name),
                "fields": [field_def] if field_def else [],
            }
            llm_result = ask_llm_params(llm, top_pages, pseudo_type)
            model_items = llm_result.get("items", [])

        # Сначала шаблон (проверяемый), затем модель
        ordered = ([{"value": v["value"], "quote": v.get("quote", ""),
                     "page": v.get("page"), "source": "шаблон"}
                    for v in template_values]
                   + [{"value": it["value"], "quote": it.get("quote", ""),
                       "page": it.get("page"), "source": "модель"}
                      for it in model_items])
        raw_values = [o["value"] for o in ordered]
        mapper = CONSTANT_MAPPERS.get(name)
        mapped = mapper(raw_values) if mapper and raw_values else None

        # Цитата/страница - от первой находки, давшей итоговое значение
        quote, page, source = "", None, ""
        if mapped is not None:
            winner = next((o for o in ordered
                           if mapper([o["value"]]) == mapped), None)
            if winner:
                quote, page, source = (winner["quote"], winner["page"],
                                       winner["source"])
        result[name] = {
            "name": name,
            "title": const.get("title", ""),
            "value": mapped,
            "found": mapped is not None,
            "raw_values": raw_values[:10],
            "quote": quote,
            "page": page,
            "source": source,
        }
    return result


def parse_pos_constants(pdf_source: Union[str, Path, bytes],
                        use_llm: bool = True,
                        config: Optional[Config] = None,
                        progress: Progress = None) -> dict:
    """Разбор файла ПОС: извлечение глобальных констант подбора работ.

    Возвращает {"document": ..., "constants": {...}, "warnings": [...]}.
    """
    config = config or Config()
    llm = LLMClient(config) if use_llm else None
    pages = extract_pages(pdf_source)
    name = Path(pdf_source).name if not isinstance(pdf_source, (bytes, bytearray)) else ""
    result: dict = {
        "document": _document_info(name, pages),
        "constants": {},
        "warnings": [],
    }
    if not any(p.text.strip() for p in pages):
        result["warnings"].append("В файле ПОС нет текстового слоя")
        return result
    index = PageIndex(pages)
    result["constants"] = extract_pos_constants(llm, index, pages, config, progress)
    return result


def parse_documents(pz: Union[str, Path, bytes, None] = None,
                    pos: Union[str, Path, bytes, None] = None,
                    raw_groups: Optional[List[dict]] = None,
                    config: Optional[Config] = None,
                    use_llm: bool = True,
                    progress: Progress = None) -> dict:
    config = config or Config()
    llm = LLMClient(config) if use_llm else None
    registry = load_registry(config.registry_path)
    result: dict = {"pz": None, "pos": None, "warnings": []}
    pz_pages = extract_pages(pz) if pz is not None else []
    pos_pages = extract_pages(pos) if pos is not None else []
    if pz is not None and not any(p.text.strip() for p in pz_pages):
        result["warnings"].append("В файле ПЗ нет текстового слоя")
    if pos is not None and not any(p.text.strip() for p in pos_pages):
        result["warnings"].append("В файле ПОС нет текстового слоя")
    if pz_pages and pos_pages and objects_mismatch(extract_object_info(pz_pages),
                                                   extract_object_info(pos_pages)):
        result["warnings"].append("ПЗ и ПОС относятся к разным объектам")
    if pz_pages:
        name = Path(pz).name if not isinstance(pz, (bytes, bytearray)) else ""
        result["pz"] = parse_pz(pz_pages, llm, config, raw_groups, name, registry, progress)
    if pos_pages:
        name = Path(pos).name if not isinstance(pos, (bytes, bytearray)) else ""
        result["pos"] = parse_pos(pos_pages, llm, config, name, registry, progress)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pz")
    parser.add_argument("--pos")
    parser.add_argument("--groups")
    parser.add_argument("--out", default="pd_result.json")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--pos-constants", action="store_true",
                        help="Разобрать из ПОС только глобальные константы подбора работ")
    args = parser.parse_args()
    start = time.time()
    if args.pos_constants and args.pos:
        result = parse_pos_constants(
            args.pos, use_llm=not args.no_llm,
            progress=lambda s: print(f"{time.time() - start:7.1f}s {s}", flush=True),
        )
    else:
        raw_groups = None
        if args.groups:
            raw_groups = json.loads(Path(args.groups).read_text(encoding="utf-8-sig"))
        result = parse_documents(
            pz=args.pz, pos=args.pos, raw_groups=raw_groups, use_llm=not args.no_llm,
            progress=lambda s: print(f"{time.time() - start:7.1f}s {s}", flush=True),
        )
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
