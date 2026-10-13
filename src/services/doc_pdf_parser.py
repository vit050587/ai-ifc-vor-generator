"""Разбор PDF-документов режима ИИ: ВОР и ПД.

Модуль дополняет разбор ПОС/ПЗ (`pd_parser.py`) двумя источниками:

- ``parse_vor(...)`` — ведомость объёмов работ (ВОР): извлекаются группы
  элементов (наименования работ), их объёмы и единицы измерения;
- ``parse_pd(...)`` — проектная документация (ПД): разбирается только
  **текстовая** часть (графические/альбомные страницы без текста пропускаются),
  извлекаются характеристики элементов (класс бетона, прочность, пластичность,
  морозостойкость, водонепроницаемость).

Извлечение текста и таблиц — ``pdfplumber`` (тот же инструмент, что и в
``src/tools/tsn``); структурирование — локальная LLM (Ollama) через
``pd_parser.LLMClient``, то есть тем же пайплайном, что и разбор ПОС/ПЗ.

Результаты сохраняются в папку сессии отдельными JSON и XLSX:
``ВОР_ведомость_объемов_работ.json/.xlsx`` и
``ПД_характеристики_элементов.json/.xlsx``.
"""

import argparse
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Union

import pandas as pd
import pdfplumber

from src.services import pd_parser

try:
    from src.core.logger import setup_logger

    logger = setup_logger(__name__)
except Exception:  # pragma: no cover — на случай запуска модуля отдельно
    import logging

    logger = logging.getLogger(__name__)

Progress = Optional[Callable[[str], None]]

# Заголовок блока с таблицами страницы (аналог маркера в pd_parser.py)
TABLE_MARKER = "[Таблица страницы]"
# Ограничение объёма контекста одной LLM-задачи, знаков.
# Значение подобрано эмпирически: при увеличении порции модель (8B) начинает
# пропускать строки таблиц (проверено: ВОР 32 порции → 217 записей; 11 порций
# по ~30 000 знаков → 152 записи, теряется треть строк). Поэтому объём порции
# менять нельзя — число LLM-вызовов снижается только параллелизмом Ollama
# (``OLLAMA_NUM_PARALLEL``), см. ``_parse_batches``.
CONTEXT_CHAR_BUDGET = 14000


@dataclass
class Page:
    """Страница PDF: номер и извлечённый текст (вместе с таблицами)."""

    number: int
    text: str


# =====================================================================
#  Извлечение текста и таблиц (pdfplumber)
# =====================================================================

_LETTER_RE = re.compile(r"[А-Яа-яЁёA-Za-z]")


def _open_pdf(pdf_source: Union[str, Path, bytes, bytearray]):
    """Открывает PDF через pdfplumber (поддерживает путь и байты)."""
    if isinstance(pdf_source, (bytes, bytearray)):
        return pdfplumber.open(io.BytesIO(bytes(pdf_source)))
    return pdfplumber.open(str(pdf_source))


def _page_text(page) -> str:
    """Текст страницы + таблицы, развёрнутые построчно в «колонки через |»."""
    try:
        text = page.extract_text() or ""
    except Exception as exc:  # pragma: no cover — защита от битых страниц
        logger.warning(f"pdfplumber: не удалось извлечь текст страницы: {exc}")
        text = ""

    parts = [text.strip()]

    try:
        tables = page.extract_tables() or []
    except Exception as exc:  # pragma: no cover
        logger.warning(f"pdfplumber: не удалось извлечь таблицы страницы: {exc}")
        tables = []

    for table in tables:
        rows = []
        for row in table or []:
            cells = [" ".join(str(c).split()) if c else "" for c in (row or [])]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"{TABLE_MARKER}\n" + "\n".join(rows))

    return "\n\n".join(p for p in parts if p.strip())


def extract_pages(pdf_source: Union[str, Path, bytes, bytearray]) -> List[Page]:
    """Извлекает все страницы PDF (текст + таблицы) через pdfplumber."""
    pages: List[Page] = []
    with _open_pdf(pdf_source) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append(Page(number=i, text=_page_text(page)))
    return pages


def _has_text(text: str, min_chars: int = 60) -> bool:
    """Есть ли на странице осмысленный текстовый слой (для ПД)."""
    stripped = (text or "").strip()
    if len(stripped) < min_chars:
        return False
    letters = len(_LETTER_RE.findall(stripped))
    return letters >= max(20, min_chars // 2)


def _batch_pages(pages: List[Page], budget: int = CONTEXT_CHAR_BUDGET) -> List[str]:
    """Группирует страницы в порции, укладывающиеся в бюджет LLM."""
    batches: List[List[str]] = []
    current: List[str] = []
    size = 0
    for p in pages:
        block = f"=== Страница {p.number} ===\n{p.text.strip()}"
        if current and size + len(block) > budget:
            batches.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        batches.append(current)
    return ["\n\n".join(b) for b in batches]


def _document_info(name: str, pages: List[Page]) -> dict:
    return {"file_name": name, "pages": len(pages)}


def _say(progress: Progress, message: str) -> None:
    if progress is not None:
        try:
            progress(message)
        except Exception:  # pragma: no cover — прогресс не должен ломать разбор
            pass


def _parse_batches(batches: List[str], extract_fn, label: str,
                   progress: Progress) -> List[dict]:
    """Разбирает порции документа LLM-функцией ``extract_fn``.

    Порции обрабатываются параллельно (до ``llm_concurrency()`` одновременно) —
    это ускоряет разбор при поднятом параллелизме Ollama
    (``OLLAMA_NUM_PARALLEL``); при последовательной Ollama результат тот же, но
    запросы просто ждут очереди. Порядок порций сохраняется, чтобы результат
    разбора не зависел от порядка ответов.
    """
    if not batches:
        return []
    workers = max(1, min(pd_parser.llm_concurrency(), len(batches)))
    if workers == 1:
        out: List[dict] = []
        for i, context in enumerate(batches, start=1):
            _say(progress, f"{label}: разбор порции {i} из {len(batches)}...")
            out += extract_fn(context)
        return out

    results: List[Optional[List[dict]]] = [None] * len(batches)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_fn, context): i
                   for i, context in enumerate(batches)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:  # pragma: no cover — не роняем разбор
                logger.warning(f"{label}: порция {i + 1} не разобрана ({exc})")
                results[i] = []
            done += 1
            _say(progress, f"{label}: разбор порции {done} из {len(batches)}...")
    out = []
    for part in results:
        out += part or []
    return out


def _to_float(value) -> Optional[float]:
    """Приводит значение объёма из LLM к float (учёт «12,5» и пробелов)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _dedupe(items: List[dict], keys: tuple) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        key = tuple(_norm(it.get(k)).lower() for k in keys)
        if not any(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# =====================================================================
#  ВОР — ведомость объёмов работ
# =====================================================================

VOR_SYSTEM_PROMPT = """Ты — инженер ПТО. Тебе переданы страницы ведомости объёмов работ (ВОР) из проектной документации. Нужно извлечь перечень работ (групп элементов) с их объёмами и единицами измерения.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Используй только сведения из приведённых страниц, ничего не выдумывай.
3. Для каждой строки ведомости верни:
   - name — наименование работы или группы элементов (как в документе);
   - unit — единица измерения (м3, м2, м, т, шт и т.п.);
   - volume — числовое значение объёма (без единицы измерения; если объём не указан — null);
   - page — номер страницы, указанный в заголовке «=== Страница N ===»;
   - quote — дословный фрагмент строки, из которой взяты данные.
4. Не включай заголовки таблиц, номера разделов и итоговые строки («Итого», «Всего»).
5. Если строка содержит несколько работ — верни каждую отдельной записью.
"""

VOR_USER_TEMPLATE = """Страницы ВОР:

{context}

Верни JSON вида:
{{"items": [{{"name": "наименование работы", "unit": "м3", "volume": "12,5", "page": 3, "quote": "дословная строка"}}]}}
"""

VOR_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "unit": {"type": "string"},
                    "volume": {"type": "string"},
                    "page": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["name", "unit", "page"],
            },
        }
    },
    "required": ["items"],
}


def _normalize_vor_item(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    name = _norm(raw.get("name"))
    if not name:
        return None
    page = raw.get("page")
    if not isinstance(page, int):
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = None
    return {
        "name": name,
        "unit": _norm(raw.get("unit")),
        "volume": _to_float(raw.get("volume")),
        "page": page,
        "quote": _norm(raw.get("quote"))[:300],
    }


def _extract_vor_batch(llm, context: str, label: str = "ВОР") -> List[dict]:
    try:
        result = llm.complete_json(VOR_SYSTEM_PROMPT, VOR_USER_TEMPLATE.format(context=context),
                                   schema=VOR_SCHEMA)
    except Exception as exc:
        logger.warning(f"{label}: LLM не разобрала порцию ({exc})")
        return []
    items = result.get("items")
    if not isinstance(items, list):
        return []
    return [it for it in (_normalize_vor_item(r) for r in items) if it]


# Единицы измерения для fallback-разбора без LLM
_VOR_UNIT_RE = re.compile(
    r"(м3|м³|м2|м²|пог\.?\s*м|п\.?\s*м|шт|т|кг|м)\b", re.IGNORECASE)
_VOR_NUM_RE = re.compile(r"(\d[\d\s]*[.,]?\d*)")


def _regex_vor_items(pages: List[Page]) -> List[dict]:
    """Простейший fallback-разбор таблиц ВОР без LLM (число + единица)."""
    items: List[dict] = []
    for p in pages:
        for line in p.text.splitlines():
            line = line.strip()
            if not line or " | " not in line:
                continue
            unit_m = _VOR_UNIT_RE.search(line)
            if not unit_m:
                continue
            tail = line[unit_m.end():]
            num_m = _VOR_NUM_RE.search(tail) or _VOR_NUM_RE.search(line[:unit_m.start()])
            if not num_m:
                continue
            volume = _to_float(num_m.group(0))
            if volume is None:
                continue
            name = line.replace(unit_m.group(0), " ").replace(num_m.group(0), " ")
            name = _norm(name.strip(" |;"))
            if not name:
                continue
            items.append({
                "name": name,
                "unit": _norm(unit_m.group(0)),
                "volume": volume,
                "page": p.number,
                "quote": line[:300],
            })
    return items


# =====================================================================
#  Детерминированный разбор структуры таблиц ВОР (разделы, блоки, объёмы)
# =====================================================================
# В отличие от LLM-разбора (`parse_vor`), эта функция читает **структуру**
# ведомости: разделы («N. Название»), блоки/захватки («Деформационный блок №N.
# Секции X и Y», «Секция №N») и строки позиций с колонками «было / стало /
# разница». Нужна для сравнения ВОР с перечнем в разрезе групп элементов
# (`vor_comparison.py`): в ВОР одна конструкция (напр. фундаментная плита
# толщиной 1000 мм) разбита по блокам и секциям, а в перечне — одна группа.
_VOR_POS_RX = re.compile(r"^\d+(?:\.\d+)*$")
_VOR_SECTION_RX = re.compile(r"^\d+\.\s+\S")
_VOR_BLOCK_RX = re.compile(r"деформационн\w*\s+блок|^секц", re.I)
_VOR_UNIT_RX = re.compile(
    r"м3|м³|куб|м2|м²|кв|пог|п\.?\s*м|шт|^т\.?$|тонн|кг|фасонн|компл", re.I)
# Служебные строки колонтитула/шапки таблицы — пропускаются.
_VOR_FURNITURE_RX = re.compile(
    r"№\.вни|немазВ|атад|ьсипдоП|лдоп|\.внИ|онавосалгоС|^Изм\.|Кол\.уч|"
    r"^Подп\.|№док|^Лист$|^Дата$|^было$|^стало$|^разница$", re.I)


def _vor_cell(value) -> str:
    """Значение ячейки таблицы ВОР: одна строка без лишних пробелов."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def _vor_num(value) -> Optional[float]:
    """Число из ячейки ВОР (учёт переносов: «1059,0 0» → 1059.00, «12,5»)."""
    text = re.sub(r"\s+", "", _vor_cell(value).replace("\u00a0", " ")).replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _is_vor_unit(cell: str) -> bool:
    return bool(cell) and len(cell) < 20 and bool(_VOR_UNIT_RX.search(cell))


def extract_vor_rows(pdf_source: Union[str, Path, bytes, bytearray]) -> List[dict]:
    """Детерминированно разбирает таблицы ВОР: строки с разделами и блоками.

    Возвращает список записей
    ``{page, section, block, pos, name, unit, volume_was, volume_now}``:
    ``section`` — раздел ведомости («5. Монолитные ж/б конструкции.
    Фундаменты»), ``block`` — блок/захватка («Деформационный блок №1. Секции 1
    и 2», «Секция №1»), ``volume_was``/``volume_now`` — объёмы колонок
    «было»/«стало» (для сравнения берётся **«стало»** — объём с учётом
    корректировки ПД).
    """
    rows: List[dict] = []
    section: Optional[str] = None
    block: Optional[str] = None
    with _open_pdf(pdf_source) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables() or []
            except Exception as exc:  # pragma: no cover — защита от битых страниц
                logger.warning(f"ВОР: не удалось извлечь таблицы стр. {page_no}: {exc}")
                continue
            for table in tables:
                for raw_row in table or []:
                    cells = [_vor_cell(c) for c in (raw_row or [])]
                    if not any(cells):
                        continue
                    if "наименование работ" in " ".join(cells).lower():
                        continue
                    filled = [c for c in cells if c]
                    # строка-шапка «1 2 3 4 5 6 7» — пропускаем
                    if len(filled) >= 5 and all(_VOR_POS_RX.match(c) for c in filled):
                        continue
                    # строка позиции: наименование + единица измерения
                    if (len(cells) >= 3 and cells[1]
                            and not _VOR_FURNITURE_RX.search(cells[1])
                            and _is_vor_unit(cells[2])):
                        rows.append({
                            "page": page_no,
                            "section": section,
                            "block": block,
                            "pos": cells[0] if _VOR_POS_RX.match(cells[0]) else "",
                            "name": cells[1],
                            "unit": cells[2],
                            "volume_was": _vor_num(cells[3]) if len(cells) > 3 else None,
                            "volume_now": _vor_num(cells[4]) if len(cells) > 4 else None,
                        })
                        continue
                    # заголовок раздела / блока — единственная текстовая ячейка
                    if len(filled) == 1:
                        head = filled[0]
                        if (_VOR_FURNITURE_RX.search(head) or len(head) < 4
                                or not _LETTER_RE.search(head)):
                            continue
                        if _VOR_SECTION_RX.match(head):
                            section, block = head, None
                        elif _VOR_BLOCK_RX.search(head):
                            block = head
    logger.info(f"ВОР: детерминированный разбор таблиц — строк {len(rows)}")
    return rows


def parse_vor(pdf_source: Union[str, Path, bytes, bytearray],
              use_llm: bool = True,
              config: Optional["pd_parser.Config"] = None,
              progress: Progress = None) -> dict:
    """Разбор PDF ведомости объёмов работ (ВОР).

    Возвращает ``{"document": ..., "items": [...], "warnings": [...]}``;
    каждая запись — ``{name, unit, volume, page, quote}``.
    """
    config = config or pd_parser.Config()
    cached = pd_parser.doc_cache_load("vor", pdf_source, config, use_llm)
    if cached is not None:
        _say(progress, "ВОР: результат разбора взят из кэша")
        return cached
    pages = extract_pages(pdf_source)
    name = Path(pdf_source).name if not isinstance(pdf_source, (bytes, bytearray)) else ""
    result: dict = {
        "document": _document_info(name, pages),
        "items": [],
        "warnings": [],
    }

    text_pages = [p for p in pages if p.text.strip()]
    if not text_pages:
        result["warnings"].append("В файле ВОР нет текстового слоя")
        return result

    raw_items: List[dict] = []
    if use_llm:
        llm = pd_parser.LLMClient(config)
        batches = _batch_pages(text_pages)
        raw_items = _parse_batches(
            batches, lambda ctx: _extract_vor_batch(llm, ctx), "ВОР", progress)
    else:
        _say(progress, "ВОР: разбор таблиц без LLM (fallback)")
        raw_items = _regex_vor_items(text_pages)

    result["items"] = _dedupe(raw_items, ("name", "unit", "volume"))
    _say(progress, f"ВОР: извлечено записей - {len(result['items'])}")
    pd_parser.doc_cache_save("vor", pdf_source, config, result, use_llm)
    return result


VOR_XLSX_COLUMNS = [
    ("name", "Наименование работы / группы элементов"),
    ("unit", "Ед. изм."),
    ("volume", "Объём"),
    ("page", "Стр."),
    ("quote", "Цитата"),
]


# =====================================================================
#  ПД — проектная документация (только текстовая часть)
# =====================================================================

PD_SYSTEM_PROMPT = """Ты — инженер ПТО. Тебе переданы текстовые страницы проектной документации (ПД). Нужно найти характеристики материалов и бетона по элементам (конструкциям) здания.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Используй только сведения из приведённых страниц, ничего не выдумывай.
3. Для каждого элемента/конструкции верни:
   - element — наименование конструкции, элемента или марки (например: «Фундаментная плита», «Колонны подземной части», «Стены 1-го этажа»);
   - concrete_class — класс бетона по прочности (B25, B30, B35...);
   - strength — марка/класс по прочности, если указаны отдельно от класса бетона;
   - plasticity — пластичность/подвижность бетонной смеси (П1..П5, ОК, осадка конуса);
   - frost_resistance — морозостойкость (F...);
   - water_resistance — водонепроницаемость (W...);
   - page — номер страницы из заголовка «=== Страница N ===»;
   - quote — дословный фрагмент, из которого взяты характеристики.
4. Если характеристика для элемента не указана — оставь соответствующее поле пустой строкой.
5. Если один элемент приведён в нескольких вариантах (по этажам, секциям) — верни каждую запись отдельно.
"""

PD_USER_TEMPLATE = """Страницы проектной документации (текстовая часть):

{context}

Верни JSON вида:
{{"items": [{{"element": "наименование конструкции", "concrete_class": "B30", "strength": "", "plasticity": "П3", "frost_resistance": "F150", "water_resistance": "W6", "page": 20, "quote": "дословный фрагмент"}}]}}
"""

PD_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "element": {"type": "string"},
                    "concrete_class": {"type": "string"},
                    "strength": {"type": "string"},
                    "plasticity": {"type": "string"},
                    "frost_resistance": {"type": "string"},
                    "water_resistance": {"type": "string"},
                    "page": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["element", "page"],
            },
        }
    },
    "required": ["items"],
}


def _normalize_pd_item(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    element = _norm(raw.get("element"))
    if not element:
        return None
    page = raw.get("page")
    if not isinstance(page, int):
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = None
    return {
        "element": element,
        "concrete_class": _norm(raw.get("concrete_class")),
        "strength": _norm(raw.get("strength")),
        "plasticity": _norm(raw.get("plasticity")),
        "frost_resistance": _norm(raw.get("frost_resistance")),
        "water_resistance": _norm(raw.get("water_resistance")),
        "page": page,
        "quote": _norm(raw.get("quote"))[:300],
    }


def _extract_pd_batch(llm, context: str, label: str = "ПД") -> List[dict]:
    try:
        result = llm.complete_json(PD_SYSTEM_PROMPT, PD_USER_TEMPLATE.format(context=context),
                                   schema=PD_SCHEMA)
    except Exception as exc:
        logger.warning(f"{label}: LLM не разобрала порцию ({exc})")
        return []
    items = result.get("items")
    if not isinstance(items, list):
        return []
    return [it for it in (_normalize_pd_item(r) for r in items) if it]


_PD_CLASS_RE = re.compile(r"[BВ]\s?\d{1,3}")
_PD_W_RE = re.compile(r"\bW\s?\d{1,2}\b")
_PD_F_RE = re.compile(r"\bF\s?\d{2,3}\b")
_PD_PLAST_RE = re.compile(r"\b(П\s?[1-5]|ОК\s?\d+|осадк\w*\s*конуса[^.;\n]{0,20})", re.IGNORECASE)


def _regex_pd_items(pages: List[Page]) -> List[dict]:
    """Простейший fallback-разбор ПД без LLM (по абзацам с марками бетона)."""
    items: List[dict] = []
    for p in pages:
        for block in re.split(r"\n\s*\n", p.text):
            block = _norm(block)
            if len(block) < 15:
                continue
            cls = _PD_CLASS_RE.search(block)
            w = _PD_W_RE.search(block)
            f = _PD_F_RE.search(block)
            plast = _PD_PLAST_RE.search(block)
            if not any((cls, w, f, plast)):
                continue
            items.append({
                "element": block[:120],
                "concrete_class": cls.group(0) if cls else "",
                "strength": "",
                "plasticity": plast.group(0) if plast else "",
                "frost_resistance": f.group(0) if f else "",
                "water_resistance": w.group(0) if w else "",
                "page": p.number,
                "quote": block[:300],
            })
    return items


def parse_pd(pdf_source: Union[str, Path, bytes, bytearray],
             use_llm: bool = True,
             config: Optional["pd_parser.Config"] = None,
             progress: Progress = None) -> dict:
    """Разбор PDF проектной документации (ПД): только текстовая часть.

    Графические/альбомные страницы без текстового слоя пропускаются.
    Возвращает ``{"document": ..., "items": [...], "warnings": [...]}``;
    каждая запись — характеристики элемента (класс бетона, прочность,
    пластичность, морозостойкость, водонепроницаемость).
    """
    config = config or pd_parser.Config()
    cached = pd_parser.doc_cache_load("pd", pdf_source, config, use_llm)
    if cached is not None:
        _say(progress, "ПД: результат разбора взят из кэша")
        return cached
    pages = extract_pages(pdf_source)
    name = Path(pdf_source).name if not isinstance(pdf_source, (bytes, bytearray)) else ""
    result: dict = {
        "document": _document_info(name, pages),
        "items": [],
        "warnings": [],
    }

    text_pages = [p for p in pages if _has_text(p.text)]
    skipped = len(pages) - len(text_pages)
    if skipped:
        result["warnings"].append(
            f"Пропущено графических страниц без текстового слоя: {skipped}"
        )
    if not text_pages:
        result["warnings"].append("В файле ПД нет текстового слоя")
        return result

    raw_items: List[dict] = []
    if use_llm:
        llm = pd_parser.LLMClient(config)
        batches = _batch_pages(text_pages)
        raw_items = _parse_batches(
            batches, lambda ctx: _extract_pd_batch(llm, ctx), "ПД", progress)
    else:
        _say(progress, "ПД: разбор текста без LLM (fallback)")
        raw_items = _regex_pd_items(text_pages)

    result["items"] = _dedupe(
        raw_items,
        ("element", "concrete_class", "strength", "plasticity",
         "frost_resistance", "water_resistance"),
    )
    _say(progress, f"ПД: извлечено записей - {len(result['items'])}")
    pd_parser.doc_cache_save("pd", pdf_source, config, result, use_llm)
    return result


PD_XLSX_COLUMNS = [
    ("element", "Элемент / конструкция"),
    ("concrete_class", "Класс бетона (B)"),
    ("strength", "Прочность"),
    ("plasticity", "Пластичность / подвижность"),
    ("frost_resistance", "Морозостойкость (F)"),
    ("water_resistance", "Водонепроницаемость (W)"),
    ("page", "Стр."),
    ("quote", "Цитата"),
]


# =====================================================================
#  Экспорт в XLSX
# =====================================================================

def items_to_xlsx(items: List[dict], columns: List[tuple], path: Union[str, Path],
                  sheet_name: str = "Данные") -> str:
    """Сохраняет перечень записей в XLSX по заданной схеме колонок."""
    rows = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rows.append({title: it.get(key, "") for key, title in columns})
    df = pd.DataFrame(rows, columns=[title for _, title in columns])
    df.to_excel(str(path), index=False, sheet_name=sheet_name)
    return str(path)


def vor_to_xlsx(items: List[dict], path: Union[str, Path]) -> str:
    return items_to_xlsx(items, VOR_XLSX_COLUMNS, path)


def pd_to_xlsx(items: List[dict], path: Union[str, Path]) -> str:
    return items_to_xlsx(items, PD_XLSX_COLUMNS, path)


# =====================================================================
#  CLI
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="Разбор PDF ВОР/ПД (pdfplumber + LLM)")
    parser.add_argument("pdf", help="PDF-файл ВОР или ПД")
    parser.add_argument("--kind", choices=["vor", "pd"], default="vor")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--out", default=None, help="Базовый путь результата (без расширения)")
    args = parser.parse_args()

    start = time.time()
    parse_fn = parse_vor if args.kind == "vor" else parse_pd
    xlsx_fn = vor_to_xlsx if args.kind == "vor" else pd_to_xlsx
    result = parse_fn(
        args.pdf, use_llm=not args.no_llm,
        progress=lambda s: print(f"{time.time() - start:7.1f}s {s}", flush=True),
    )

    out = args.out or os.path.splitext(args.pdf)[0] + f"_{args.kind}"
    Path(out + ".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    xlsx_fn(result["items"], out + ".xlsx")
    print(f"Записей: {len(result['items'])} -> {out}.json / {out}.xlsx")


if __name__ == "__main__":
    main()
