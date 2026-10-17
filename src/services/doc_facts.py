"""Факты из документов ВОР и ПД для подбора работ (режим ИИ).

Модуль читает результаты разбора дополнительных PDF-документов
(``doc_pdf_parser``, §4.1 ``readme_ar.md``) из корня сессии:

* ``ВОР_ведомость_объемов_работ.json`` — ведомость объёмов работ
  (наименование работы/конструкции, единица измерения, объём);
* ``ПД_характеристики_элементов.json`` — характеристики конструкций
  (класс бетона B, морозостойкость F, водонепроницаемость W, пластичность).

Сведения используются подбором работ по перечню (``perechen_pipeline``, ветка
``WORKS_SELECTOR=perechen``) там, где данных модели не хватает:

* **класс бетона B/W/F** — если в модели его нет (в проекте B/W/F указаны лишь
  у единичных элементов), берём по конструкции из ВОР (класс указан прямо в
  наименовании, напр. «Фундаментная плита, толщиной 1000мм, бетон В30 W8 F150»)
  или из ПД;
* **класс арматуры** — «Арматура класса А500С» (ВОР) / «Арматура A500c» (ПД);
* **объёмы работ, которых нет в модели** (опалубка торца плиты, гидроизоляция
  шва, засыпка и т.п.) — из ВОР для строк «объём по проекту»;
* **сверка объёмов модель ↔ ВОР** — для аналитической справки.

Сопоставление документа и элемента модели — семантическое: по виду конструкции
(``primary_kind``), части здания и пересечению слов наименования. Спорные случаи
решает локальная LLM (как в ``pd_full``/``tsn_coeffs``), решения кешируются в
``data/doc_facts_cache.json``.

Модуль аддитивен: если документы не загружены, ``load()`` возвращает ``None``
и подбор работ работает как раньше.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.logger import setup_logger

logger = setup_logger(__name__)

VOR_JSON = "ВОР_ведомость_объемов_работ.json"
PD_JSON = "ПД_характеристики_элементов.json"
CACHE_PATH = os.getenv("DOC_FACTS_CACHE_PATH", "data/doc_facts_cache.json")
CACHE_VERSION = 1


# =====================================================================
#  Единицы измерения
# =====================================================================

_UNIT_RX = (
    (re.compile(r"куб\.?\s*м|м3|м³", re.I), "м3"),
    (re.compile(r"кв\.?\s*м|м2|м²", re.I), "м2"),
    (re.compile(r"пог\.?\s*м|п\.\s*м|м\.\s*п", re.I), "м"),
    # «100 м», «1000 м», «1 м» — единица расценки с множителем
    (re.compile(r"^[\d\s.,]*м\.?$", re.I), "м"),
    (re.compile(r"^[\d\s.,]*т\.?$|тонн", re.I), "т"),
    (re.compile(r"^[\d\s.,]*шт", re.I), "шт"),
)


def norm_unit(unit: Any) -> str:
    """Приводит единицу измерения к канонической: м3 / м2 / м / т / шт (или '')."""
    s = str(unit or "").replace("\u00a0", " ").strip()
    for rx, code in _UNIT_RX:
        if rx.search(s):
            return code
    return ""


# =====================================================================
#  Классификация конструкций и частей здания
# =====================================================================

# Порядок важен: первый сработавший шаблон задаёт «главный» вид конструкции.
_PRIMARY = (
    ("foundation_slab", r"фундаментн\w*\s+плит|плит\w*\s+фундамент|базисн"),
    ("pile", r"сва[ийя]|буронабивн"),
    ("footing", r"фундамент|ростверк|подошв"),
    ("stair", r"лестничн|лестниц|марш|сход|косоур|площадк"),
    ("wall", r"стен|парапет|вертикальн"),
    ("column", r"колонн|пилон"),
    ("beam", r"балк|ригел|обвязочн"),
    ("slab", r"перекрыт|плит|покрыт|горизонтальн"),
    ("roof", r"кровл"),
    ("insulation", r"утепл|теплоизол|пенополист|минват|ппс|эппс|термовклад"),
    ("waterproofing", r"гидроизол|мембран|техноэласт|праймер|мастик|гидрошпонк"),
    ("masonry", r"кладк|кирпич|газобетон|блок"),
)

# Виды конструкций, которые считаются взаимозаменяемыми при сопоставлении
# (ПД пишет «вертикальные»/«горизонтальные» вместо конкретного вида).
_COMPAT = {
    "wall": {"wall", "column"},
    "column": {"wall", "column"},
    "slab": {"slab", "beam"},
    "beam": {"slab", "beam"},
    "footing": {"footing", "foundation_slab"},
    "foundation_slab": {"foundation_slab", "footing"},
}

# Слова, по которым в ВОР (наименование + цитата) определяется, к какой
# конструкции относится расход арматуры. Ключ — вид конструкции (primary_kind).
_REBAR_KIND_WORDS = {
    "foundation_slab": ("фп", "фундаментн"),
    "pile": ("сва",),
    "footing": ("фундамент", "ростверк"),
    "slab": ("плит", "перекрыт"),
    "wall": ("стен", "парапет"),
    "column": ("колонн", "пилон"),
    "beam": ("балк", "ригел"),
    "stair": ("лестничн", "марш", "площадк"),
}

# Записи о благоустройстве/МАФ/инженерных сетях — к конструкциям здания не относятся.
_NOISE_RX = re.compile(
    r"маф|лгик|игрового|качел|тренаж|велопарков|навес|детск|спортивн|благоустр|"
    r"шпунт|колодец|кабельн|план\s|схема\s|разрез|общий\s+вид|цим|паспорт|"
    r"труба\s|двутавр|швеллер|песок$|плёнк|пленк",
    re.I,
)
# Плиты/фундаменты башенного крана (БК) — отдельные конструкции.
_BK_RX = re.compile(r"\bбк\b|башенн", re.I)


def primary_kind(text: Any) -> Optional[str]:
    """Главный вид конструкции по тексту (наименование элемента/записи документа)."""
    t = str(text or "").lower()
    for kind, rx in _PRIMARY:
        if re.search(rx, t):
            return kind
    return None


def kinds(text: Any) -> Set[str]:
    """Все виды конструкций, упомянутые в тексте."""
    return {k for k, rx in _PRIMARY if re.search(rx, str(text or "").lower())}


def compatible(a: Optional[str], b: Optional[str]) -> bool:
    """Совместимы ли виды конструкций при сопоставлении (учёт групп ПД)."""
    if a is None or b is None:
        return False
    return b in _COMPAT.get(a, {a})


def part_of(text: Any) -> Optional[str]:
    """Часть здания по тексту: Подземная / Цоколь / Надземная (или None)."""
    t = str(text or "").lower()
    if "подзем" in t or "подвал" in t:
        return "Подземная"
    if "цокол" in t:
        return "Цоколь"
    if "надзем" in t:
        return "Надземная"
    return None


# =====================================================================
#  Классы бетона и арматуры
# =====================================================================

_B_CLASS_RX = re.compile(r"\bB\s?-?\s?(\d{1,2}(?:[.,]\d)?)")
_F_CLASS_RX = re.compile(r"\bF\s?-?\s?(\d{2,3})")
_W_CLASS_RX = re.compile(r"\bW\s?-?\s?(\d{1,2})")
_REBAR_RX = re.compile(r"[АA]\s?-?\s?\d{3}\s?[СC]?")
_JUNK = {"не указано", "не указан", "нет данных", "null", "none", "-", ""}


def class_b(text: Any) -> Optional[float]:
    """Класс бетона по прочности (число) из текста, напр. «бетон В30» → 30.0."""
    if str(text or "").strip().lower() in _JUNK:
        return None
    t = str(text or "").replace("В", "B").replace("в", "B")
    m = _B_CLASS_RX.search(t)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def class_f(text: Any) -> Optional[str]:
    """Морозостойкость F из текста («F150» → '150')."""
    if str(text or "").strip().lower() in _JUNK:
        return None
    m = _F_CLASS_RX.search(str(text or ""))
    return m.group(1) if m else None


def class_w(text: Any) -> Optional[str]:
    """Водонепроницаемость W из текста («W8» → '8')."""
    if str(text or "").strip().lower() in _JUNK:
        return None
    m = _W_CLASS_RX.search(str(text or ""))
    return m.group(1) if m else None


def _norm_rebar(value: str) -> str:
    s = re.sub(r"\s+", "", str(value or "")).upper()
    return s.replace("A", "А").replace("C", "С")


def rebar_classes(text: Any) -> List[str]:
    """Классы арматуры из текста («Арматура класса А500С» → ['А500С'])."""
    out: List[str] = []
    for m in _REBAR_RX.finditer(str(text or "")):
        v = _norm_rebar(m.group(0))
        if v and v not in out:
            out.append(v)
    return out


# =====================================================================
#  Стемминг для сопоставления наименований
# =====================================================================

_STOP = {"устрой", "устано", "монтаж", "работ", "поверх", "констр", "железо", "отдель"}


def stems(text: Any) -> Set[str]:
    """Набор «стеммов» (первые 6 букв значимых слов) для сравнения наименований."""
    words = re.findall(r"[а-яёa-z]{5,}", str(text or "").lower())
    return {w[:6] for w in words if w[:6] not in _STOP}


def el_text(el: Any) -> str:
    """Текст элемента модели для сопоставления с документами."""
    if not isinstance(el, dict):
        return str(el or "")
    return " ".join(str(el.get(k) or "") for k in
                    ("name", "mssk_name", "material", "predefined_type", "object_type", "ifc_class"))


# =====================================================================
#  Загрузка и кеш
# =====================================================================

_cache: Optional["Facts"] = None
_cache_dir: Optional[str] = None


def _find(session_dir: str, filename: str) -> Optional[str]:
    """Ищет файл в каталоге и до 3 уровней выше (run_<NNN>/ → корень сессии)."""
    p = os.path.abspath(session_dir)
    for _ in range(4):
        fp = os.path.join(p, filename)
        if os.path.isfile(fp):
            return fp
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"doc_facts: не прочитан {path}: {exc}")
        return {}


def _llm_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _llm_cache_save(cache: Dict[str, Any]) -> None:
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:
        logger.warning(f"doc_facts: кеш не записан: {exc}")


def _ask(prompt: str, key: str) -> Dict[str, Any]:
    """Запрос к локальной LLM с кешем по ключу (ошибка LLM не прерывает подбор)."""
    cache = _llm_cache()
    if key in cache:
        return cache[key]
    try:
        from src.services.pd_full import ask
        ans = ask(prompt)
    except Exception as exc:
        logger.warning(f"doc_facts: LLM недоступна: {exc}")
        return {}
    if isinstance(ans, dict) and ans:
        cache[key] = ans
        _llm_cache_save(cache)
    return ans if isinstance(ans, dict) else {}


def _fmt_num(value: float) -> str:
    """Число с пробелом-разделителем тысяч и запятой: 1244.38 → «1 244,38»."""
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def _num(value: Any) -> Optional[float]:
    """Число из значения (учёт «12,5», пробелов, «12.5»)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


# =====================================================================
#  Арматура из ВОР: масса по классу и диаметру (разделы/блоки/секции)
# =====================================================================
# Детерминированный разбор PDF ВОР (`doc_pdf_parser.extract_vor_rows`) даёт
# подпозиции-диаметры арматуры («D12», «D20», …) с массой (т) и родительскую
# позицию с классом («Арматура класса А500С» / «… А240»). Ведомость разбита на
# разделы (вид конструкции), блоки/захватки и секции/этажи — масса подпозиций
# суммируется по всем блокам раздела (см. `Facts.rebar_by_diameter`).
VOR_PDF_PATTERNS = ("vor_*.pdf", "*ВОР*.pdf", "*вор*.pdf")
_DET_D_RX = re.compile(r"^[Dd]\s?-?\s?(\d{1,3})$")
_vor_det_cache: Dict[str, List[Dict[str, Any]]] = {}


def _find_vor_pdf(session_dir: str) -> Optional[str]:
    """Ищет загруженный PDF ВОР в каталоге сессии/запуска (и в ``original/``)."""
    import glob
    p = os.path.abspath(session_dir)
    for _ in range(4):
        for directory in (p, os.path.join(p, "original")):
            for pattern in VOR_PDF_PATTERNS:
                hits = sorted(glob.glob(os.path.join(directory, pattern)))
                if hits:
                    return hits[0]
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def _section_part(text: Any) -> Optional[str]:
    """Часть здания по названию раздела ВОР (Подземная / Цоколь / Надземная)."""
    t = str(text or "").lower()
    if "подзем" in t or "подвал" in t:
        return "Подземная"
    if "цокол" in t:
        return "Цоколь"
    if any(k in t for k in ("надзем", "этаж", "парапет", "кровл", "покрыт")):
        return "Надземная"
    if "фундамент" in t:
        return "Подземная"
    return None


def _rebar_kind_match(a: Optional[str], b: Optional[str]) -> bool:
    """Совпадают ли виды конструкций для раскладки арматуры (жёстко, без «плита↔балка»)."""
    if a is None or b is None:
        return False
    if a == b:
        return True
    return {a, b} == {"footing", "foundation_slab"}


def vor_rebar_rows(session_dir: str) -> List[Dict[str, Any]]:
    """Строки арматуры ВОР: класс, диаметр (мм), масса (т), вид и часть конструкции.

    Класс берётся из родительской позиции («Арматура класса А500С»), вид
    конструкции — из раздела ведомости (раздел назван по конструкции: «Стены
    1го этажа», «Плиты перекрытия этажей», …), часть здания — из раздела.
    Записи БК/МАФ/благоустройства исключаются. Кешируется по каталогу.
    """
    key = os.path.abspath(session_dir)
    if key in _vor_det_cache:
        return _vor_det_cache[key]
    out: List[Dict[str, Any]] = []
    rows: List[dict] = []
    try:
        from src.services import doc_pdf_parser
        pdf = _find_vor_pdf(key)
        if pdf:
            rows = doc_pdf_parser.extract_vor_rows(pdf)
    except Exception as exc:
        logger.warning(f"doc_facts: арматура ВОР не разобрана ({exc})")
    idx = {(r.get("section"), r.get("block"), str(r.get("pos") or "")): str(r.get("name") or "")
           for r in rows}
    for r in rows:
        m = _DET_D_RX.match(str(r.get("name") or "").strip())
        if not m or norm_unit(r.get("unit")) != "т":
            continue
        section = str(r.get("section") or "")
        if _NOISE_RX.search(section) or _BK_RX.search(section):
            continue
        vol = _num(r.get("volume_now"))
        if vol is None:
            vol = _num(r.get("volume_was"))
        if not vol:
            continue
        pos = str(r.get("pos") or "")
        parent = idx.get((r.get("section"), r.get("block"), pos.rsplit(".", 1)[0])) if "." in pos else ""
        classes = rebar_classes(f"{parent} {section}")
        cls = "A240" if any("240" in c for c in classes) else "A500"
        out.append({"cls": cls, "diameter": int(m.group(1)), "volume": float(vol),
                    "kind": primary_kind(section), "part": _section_part(section),
                    "section": section})
    _vor_det_cache[key] = out
    logger.info(f"doc_facts: арматура ВОР — {len(out)} подпозиций по диаметрам")
    return out


def reset_vor_rebar() -> None:
    """Сбрасывает кеш детерминированного разбора арматуры ВОР."""
    _vor_det_cache.clear()


# =====================================================================
#  Факты по документам
# =====================================================================

class Facts:
    """Факты ВОР/ПД, сопоставляемые с элементами модели."""

    def __init__(self, vor_items: List[dict], pd_items: List[dict], session_dir: str = ""):
        self.session_dir = session_dir
        self.vor: List[Dict[str, Any]] = []
        self.pd: List[Dict[str, Any]] = []
        self._records: List[Dict[str, Any]] = []

        for it in vor_items or []:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            rec = {
                "src": "ВОР",
                "text": name,
                "name": name,
                "unit": norm_unit(it.get("unit")),
                "raw_unit": str(it.get("unit") or ""),
                "volume": _num(it.get("volume")),
                "page": it.get("page"),
                "quote": str(it.get("quote") or ""),
                "kind": primary_kind(name),
                "part": part_of(name) or part_of(str(it.get("quote") or "")),
                "b": class_b(name),
                "f": class_f(name),
                "w": class_w(name),
                "stems": stems(name),
                # стеммы наименования и цитаты: в ВОР конструкция часто указана
                # только в цитате («Уточнение объемов арматуры ФП и …»)
                "all_stems": stems(name + " " + str(it.get("quote") or "")),
                "rebar": rebar_classes(name),
            }
            self.vor.append(rec)
            self._records.append(rec)

        for it in pd_items or []:
            if not isinstance(it, dict):
                continue
            element = str(it.get("element") or "").strip()
            if not element:
                continue
            text = " ".join(x for x in (element, str(it.get("quote") or "")) if x)
            rec = {
                "src": "ПД",
                "text": text,
                "element": element,
                "page": it.get("page"),
                "quote": str(it.get("quote") or ""),
                "kind": primary_kind(element),
                "part": part_of(element) or part_of(str(it.get("quote") or "")),
                "b": class_b(it.get("concrete_class")) or class_b(element),
                "f": class_f(it.get("frost_resistance")),
                "w": class_w(it.get("water_resistance")),
                "stems": stems(text),
                "all_stems": stems(text),
                "rebar": rebar_classes(element) or rebar_classes(it.get("quote")),
            }
            self.pd.append(rec)
            self._records.append(rec)

        self._records = [r for r in self._records if not _NOISE_RX.search(r["text"])]
        # Частота «стеммов» по ВОР: редкие слова («гидрош», «акваст») различают
        # работы надёжнее, чем частые («бетон», «работ»). Редкие считаются
        # специфичными при мягком сопоставлении объёмов (см. volume_for).
        freq: Dict[str, int] = {}
        for rec in self.vor:
            for s in rec.get("all_stems") or rec["stems"]:
                freq[s] = freq.get(s, 0) + 1
        self._stem_freq = freq
        self._stem_rare = max(2, int(0.15 * max(1, len(self.vor))))
        logger.info(
            f"doc_facts: записей ВОР {len(self.vor)}, ПД {len(self.pd)} "
            f"(после фильтра — {len(self._records)})"
        )

    # ------------------------------------------------------------------
    #  Класс бетона B/W/F
    # ------------------------------------------------------------------

    def _scored(self, el: Any, part: Optional[str]) -> List[Tuple[int, dict]]:
        """Кандидаты-записи с классом бетона, отсортированные по убыванию оценки."""
        text = el_text(el)
        kind = primary_kind(text)
        if kind is None:
            return []
        storey = str((el or {}).get("storey") or "") if isinstance(el, dict) else ""
        p = part or part_of(text) or part_of(storey)
        bk_el = bool(_BK_RX.search(text))
        el_stems = stems(text)
        out: List[Tuple[int, dict]] = []
        for rec in self._records:
            if rec.get("b") is None:
                continue
            if not compatible(kind, rec.get("kind")):
                continue
            score = 0
            if rec.get("kind") == kind:
                score += 3
            if rec.get("part") and p:
                score += 3 if rec["part"] == p else -2
            if bool(_BK_RX.search(rec["text"])) != bk_el:
                score -= 5
            if rec["src"] == "ВОР":
                score += 1                      # в ВОР класс указан по конкретной конструкции
            score += len(el_stems & rec.get("stems", set()))
            if score > 0:
                out.append((score, rec))
        out.sort(key=lambda x: -x[0])
        return out

    def concrete_bwf(self, el: Any, part: Optional[str] = None
                     ) -> Optional[Tuple[float, Optional[str], Optional[str]]]:
        """Класс бетона ``(B, W, F)`` для элемента по ВОР/ПД (или None, если не найден)."""
        scored = self._scored(el, part)
        if not scored:
            return None
        top = scored[0][0]
        best = [rec for s, rec in scored if s == top]
        rec = best[0]
        if len(best) > 1:
            # неоднозначность: у лучших кандидатов разные классы — решает LLM
            marks = {(r["b"], r.get("f"), r.get("w")) for r in best}
            if len(marks) > 1:
                rec = self._ask_pick(el, best) or rec
        return self._with_fw(rec, scored)

    @staticmethod
    def _with_fw(rec: dict, scored: List[Tuple[int, dict]]
                 ) -> Tuple[float, Optional[str], Optional[str]]:
        """Добирает W/F из других записей того же класса бетона, если у выбранной их нет.

        Возвращает кортеж ``(B, W, F)`` — тот же порядок, что у ``_bwf`` в
        ``perechen_pipeline``.
        """
        b, f, w = rec["b"], rec.get("f"), rec.get("w")
        if f is None or w is None:
            for _, cand in scored:
                if cand.get("b") != b:
                    continue
                if f is None and cand.get("f"):
                    f = cand["f"]
                if w is None and cand.get("w"):
                    w = cand["w"]
                if f is not None and w is not None:
                    break
        return b, w, f

    def _ask_pick(self, el: Any, candidates: List[dict]) -> Optional[dict]:
        lines = "\n".join(
            f'{i}: {r["text"][:120]} — бетон B{r["b"]}'
            + (f' F{r["f"]}' if r.get("f") else "") + (f' W{r["w"]}' if r.get("w") else "")
            + f' (источник: {r["src"]}, стр. {r.get("page")})'
            for i, r in enumerate(candidates))
        key = f"bwf|{CACHE_VERSION}|{el_text(el)[:200]}|" + "|".join(r["text"][:80] for r in candidates)
        prompt = (
            "Ты инженер-сметчик. Для конструкции здания нужно выбрать класс бетона из проектной "
            "документации. Ниже конструкция BIM-модели и варианты из ВОР (ведомость объёмов работ) "
            "и ПД (проектная документация).\n\n"
            f"Конструкция: {el_text(el)[:300]}\n\n"
            "Варианты (номер: запись — класс, источник):\n" + lines + "\n\n"
            "Выбери ОДИН вариант, наиболее подходящий этой конструкции (учти часть здания и вид "
            "конструкции; записи про башенные краны (БК), МАФ и благоустройство не подходят).\n"
            'Ответ строго JSON: {"id": <номер>}'
        )
        ans = _ask(prompt, key)
        i = ans.get("id")
        if isinstance(i, bool):
            return None
        if isinstance(i, int) and 0 <= i < len(candidates):
            return candidates[i]
        if isinstance(i, str) and i.strip().isdigit():
            n = int(i.strip())
            if 0 <= n < len(candidates):
                return candidates[n]
        return None

    # ------------------------------------------------------------------
    #  Класс арматуры
    # ------------------------------------------------------------------

    def rebar_class(self, el: Any = None) -> str:
        """Класс арматуры по ВОР/ПД (предпочтение — А500 и выше)."""
        found: List[str] = []
        for rec in self._records:
            if "арматур" not in rec["text"].lower():
                continue
            for cls in rec.get("rebar") or []:
                if cls not in found:
                    found.append(cls)
        for cls in found:
            if "500" in cls or "III" in cls:
                return cls if "С" in cls else cls + "С"
        return found[0] if found else ""

    # ------------------------------------------------------------------
    #  Объёмы
    # ------------------------------------------------------------------

    def volume_for(self, work_title: str, unit: Any, el: Any = None,
                   part: Optional[str] = None) -> Optional[float]:
        """Объём работы из ВОР (в единице расценки) — для работ, которых нет в модели.

        Сопоставление намеренно «мягкое»: строка перечня сметчиков и запись ВОР
        почти никогда не совпадают дословно (расценка «Заполнение вертикальных
        швов стеновых панелей упругими прокладками» ↔ ВОР «Гидрошпонка
        АКВАСТОП…»). Учитываются:

        * единица измерения (обязательное совпадение);
        * совпадение вида конструкции (``kind``) и части здания;
        * общие слова наименования — достаточно **одного специфичного**
          (редкого в ВОР) слова, напр. «гидрош» для гидрошпонки;
        * цитата записи ВОР (конструкция часто указана только в ней).

        Несовпадение вида конструкции не отбрасывает запись, если нашлось
        специфичное общее слово, — иначе расценка и работа расходятся по названию
        конструкции и объём не подтянулся бы вовсе.
        """
        u = norm_unit(unit)
        if not u or not self.vor:
            return None
        title = str(work_title or "")
        text = f"{title} {el_text(el)}"
        kind = primary_kind(text)
        p = part or part_of(text)
        title_stems = stems(title)
        if not title_stems:
            return None
        cands: List[Tuple[float, dict]] = []
        for v in self.vor:
            if v["volume"] is None or v["unit"] != u:
                continue
            v_kind = v.get("kind")
            common = title_stems & (v.get("all_stems") or v["stems"])
            specific = {s for s in common if self._stem_freq.get(s, 0) <= self._stem_rare}
            if len(common) < 2 and not specific:
                continue
            score = 2.0 * len(common) + (3.0 if specific else 0.0)
            if kind and v_kind:
                if v_kind == kind:
                    score += 3.0
                elif compatible(kind, v_kind):
                    score += 1.0
                elif not specific:
                    continue        # чужой вид конструкции без общего слова — не берём
                else:
                    score -= 2.0
            if p and v.get("part"):
                score += 2.0 if v["part"] == p else -2.0
            if score <= 0:
                continue
            cands.append((score, v))
        if not cands:
            return None
        top = max(c[0] for c in cands)
        sel = [v for s, v in cands if s == top]
        names = {re.sub(r"\W+", " ", v["text"].lower()).strip() for v in sel}
        if len(names) == 1:
            return round(sum(v["volume"] for v in sel), 4)
        return round(sel[0]["volume"], 4)

    def rebar_mass(self, kind: Optional[str], part: Optional[str] = None) -> Optional[float]:
        """Масса арматуры (т) по ВОР для конструкций указанного вида.

        В ВОР расход арматуры указан по конструкциям: наименование — просто
        «Арматура класса А500С», а конструкция — в цитате («Уточнение объемов
        арматуры ФП и …», «… ж/б плит перекрытия»). Поэтому вид конструкции
        ищется и в наименовании, и в цитате.

        Возвращает ``None``, если для этого вида арматуры в ВОР нет.
        """
        words = _REBAR_KIND_WORDS.get(kind or "")
        if not words:
            return None
        total, hits, pages = 0.0, 0, []
        for v in self.vor:
            if v["unit"] != "т" or not v["volume"]:
                continue
            if "арматур" not in v["text"].lower():
                continue
            text = f"{v['text']} {v.get('quote') or ''}".lower()
            if not any(re.search(r"\b" + w, text) for w in words):
                continue
            if part and v.get("part") and v["part"] != part:
                continue
            total += float(v["volume"])
            hits += 1
            pages.append(v.get("page"))
        if not hits:
            return None
        logger.info(f"doc_facts: арматура по ВОР для «{kind}» — {round(total, 2)} т "
                    f"({hits} поз., стр. {', '.join(str(x) for x in pages[:5])})")
        return round(total, 4)

    def rebar_by_diameter(self, kind: Optional[str], part: Optional[str] = None
                          ) -> Dict[Tuple[str, int], float]:
        """Масса арматуры (т) из ВОР по классу и диаметру для вида конструкции.

        ``{(класс, диаметр_мм): масса_т}``. ВОР разбита по разделам (вид
        конструкции), блокам/захваткам и секциям/этажам — массы подпозиций
        суммируются по всем блокам раздела. ``part`` (Подземная / Цоколь /
        Надземная) ограничивает выборку частью здания, если она определена и в
        разделе, и в запросе; при неопределённой части фильтр не применяется.
        """
        out: Dict[Tuple[str, int], float] = {}
        for r in vor_rebar_rows(self.session_dir):
            if not _rebar_kind_match(kind, r.get("kind")):
                continue
            if part and r.get("part") and r["part"] != part:
                continue
            key = (r["cls"], int(r["diameter"]))
            out[key] = round(out.get(key, 0.0) + float(r["volume"]), 4)
        return out

    def summary_items(self) -> List[Dict[str, Any]]:
        """Тезисная сводка фактов ВОР/ПД для веб-интерфейса.

        Записи ВОР с объёмом агрегируются по наименованию (одна строка на
        материал/работу с суммарным объёмом), записи ПД — по классам бетона.
        """
        items: List[Dict[str, Any]] = []
        agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for v in self.vor:
            if v["volume"] is None:
                continue
            key = (re.sub(r"\s+", " ", v["text"].strip().lower()), v["unit"])
            a = agg.setdefault(key, {"title": v["text"], "unit": v["raw_unit"] or v["unit"],
                                     "volume": 0.0, "page": v.get("page"),
                                     "quote": v.get("quote") or "", "n": 0})
            a["volume"] += float(v["volume"])
            a["n"] += 1
        for a in agg.values():
            unit = re.sub(r"\s*т\.?\s*$", "т", a["unit"]).strip()
            items.append({
                "group": "ВОР — ведомость объёмов работ",
                "title": a["title"][:140],
                "value": f"{_fmt_num(a['volume'])} {unit}".strip(),
                "src": "ВОР", "page": a["page"], "quote": a["quote"],
                "use": "calc" if re.search(r"арматур|бетон|гидрошпонк", a["title"], re.I) else "info",
                "count": a["n"],
            })
        for r in self.pd:
            b, w, f = r.get("b"), r.get("w"), r.get("f")
            if not any((b, w, f)):
                continue
            value = " ".join(x for x in (f"B{b:g}" if b else "", f"W{w}" if w else "",
                                         f"F{f}" if f else "") if x)
            items.append({
                "group": "ПД — характеристики конструкций",
                "title": r["element"][:140], "value": value,
                "src": "ПД", "page": r.get("page"), "quote": r.get("quote") or "",
                "use": "calc",
            })
        return items

    def beton_volume(self, el: Any, part: Optional[str] = None) -> Optional[float]:
        """Суммарный объём бетона (м³) по ВОР для конструкций, совпадающих с элементом."""
        text = el_text(el)
        kind = primary_kind(text)
        if kind is None:
            return None
        bk_el = bool(_BK_RX.search(text))
        total = 0.0
        for v in self.vor:
            if v["unit"] != "м3" or v["volume"] is None:
                continue
            if not compatible(kind, v["kind"]):
                continue
            if bool(_BK_RX.search(v["text"])) != bk_el:
                continue
            total += v["volume"]
        return round(total, 4) if total > 0 else None


def load(session_dir: str) -> Optional["Facts"]:
    """Читает ВОР/ПД из каталога сессии (кеш по каталогу).

    Возвращает ``None``, если ни одного документа в сессии нет.
    """
    global _cache, _cache_dir
    key_dir = os.path.abspath(session_dir)
    if _cache is not None and _cache_dir == key_dir:
        return _cache
    vor_path = _find(key_dir, VOR_JSON)
    pd_path = _find(key_dir, PD_JSON)
    if not vor_path and not pd_path:
        _cache, _cache_dir = None, key_dir
        return None
    vor_items = (_load_json(vor_path).get("items") or []) if vor_path else []
    pd_items = (_load_json(pd_path).get("items") or []) if pd_path else []
    _cache = Facts(vor_items, pd_items, session_dir=key_dir)
    _cache_dir = key_dir
    return _cache


def reset() -> None:
    """Сбрасывает кеш (для тестов и повторных запусков)."""
    global _cache, _cache_dir
    _cache, _cache_dir = None, None


def facts_items(session_dir: str) -> Dict[str, Any]:
    """Тезисная сводка фактов ВОР/ПД для веб-интерфейса (блок «Всё найденное»).

    Возвращает ``{"status": "ready"|"none", "items": [...]}`` в том же формате,
    что и полный разбор ПОС/ПЗ (``pd_full``): группа, наименование, значение,
    источник, страница и цитата.
    """
    facts = load(session_dir)
    if facts is None:
        return {"status": "none", "items": []}
    try:
        items = facts.summary_items()
    except Exception as exc:
        logger.warning(f"doc_facts: сводка не построена: {exc}")
        return {"status": "ready", "items": []}
    return {"status": "ready", "items": items,
            "vor": len(facts.vor), "pd": len(facts.pd)}


# =====================================================================
#  Подтверждение наличия работы в документах (ВОР/ПД/ПОС/ПЗ/IFC)
# =====================================================================
# Работы, которых нет в модели (гидрошпонка, штуцер инъекционный, набухающий
# профиль, закладные детали и т.п.), добавляются к бетонной конструкции
# «по перечню сметчиков». Однако их наличие должно быть подтверждено проектом:
# если ни в одном документе (IFC-модель, ПОС, ВОР, ПД) нет упоминания работы —
# строка-заглушка «объём по проекту» не нужна. Подтверждение ищется по
# характерным словам работы (напр. «шпонк»/«аквастоп» для гидрошпонки,
# «штуцер»/«инъекц» для инъекционного штуцера).

_CORPUS_CACHE: Dict[str, List[Tuple[str, Any, str]]] = {}


def _corpus(session_dir: str) -> List[Tuple[str, Any, str]]:
    """Корпус текстов документов сессии: (источник, страница, текст).

    Читаются ВОР, ПД, ПОС/ПЗ (`*_глобальные_константы.json`), полный разбор
    ПОС/ПЗ (`pd_full.json`) и наименования элементов модели
    (`filtered_elements.xlsx`, `Подобранные_таблицы_работ.json`).
    Кешируется по каталогу (файлы сессии не меняются в ходе расчёта).
    """
    key_dir = os.path.abspath(session_dir)
    if key_dir in _CORPUS_CACHE:
        return _CORPUS_CACHE[key_dir]
    corpus: List[Tuple[str, Any, str]] = []
    vor = _find(key_dir, VOR_JSON)
    if vor:
        for it in _load_json(vor).get("items") or []:
            corpus.append(("ВОР", it.get("page"),
                           f"{it.get('name') or ''} {it.get('quote') or ''}"))
    pd = _find(key_dir, PD_JSON)
    if pd:
        for it in _load_json(pd).get("items") or []:
            corpus.append(("ПД", it.get("page"),
                           f"{it.get('element') or ''} {it.get('quote') or ''}"))
    mode_file = _find(key_dir, "pd_mode.json")
    include_pos_pz = not mode_file or _load_json(mode_file).get("mode", "on") != "off"
    for fname, src in ((("ПОС_глобальные_константы.json", "ПОС"),
                       ("ПЗ_глобальные_константы.json", "ПЗ")) if include_pos_pz else ()):
        fp = _find(key_dir, fname)
        if fp:
            for k, v in _load_json(fp).items():
                corpus.append((src, None, f"{k} {json.dumps(v, ensure_ascii=False)}"))
    fp = _find(key_dir, "pd_full.json") if include_pos_pz else None
    if fp:
        data = _load_json(fp)
        rows = data.values() if isinstance(data, dict) else data
        if isinstance(rows, (list, tuple)) or hasattr(rows, "__iter__"):
            for r in rows:
                if isinstance(r, dict):
                    corpus.append(("ПОС/ПЗ", r.get("page"),
                                   f"{r.get('title') or ''} {r.get('value') or ''} "
                                   f"{r.get('quote') or ''}"))
    fp = _find(key_dir, "filtered_elements.xlsx")
    if fp:
        try:
            import pandas as pd
            df = pd.read_excel(fp, sheet_name="Данные")
            name_col = next((c for c in df.columns if str(c).strip().lower() == "имя"), None)
            if name_col is not None:
                for v in df[name_col].dropna().unique():
                    corpus.append(("IFC (модель)", None, str(v)))
        except Exception as exc:
            logger.warning(f"doc_facts: элементы модели не прочитаны: {exc}")
    fp = _find(key_dir, "Подобранные_таблицы_работ.json")
    if fp:
        for entry in _load_json(fp).get("elements") or []:
            el = entry.get("element") or {}
            if isinstance(el, dict):
                corpus.append(("IFC (модель)", None, str(el.get("name") or "")))
    _CORPUS_CACHE[key_dir] = corpus
    logger.info(f"doc_facts: корпус документов — {len(corpus)} записей")
    return corpus


def confirm_keys(keys, session_dir: str) -> Optional[Dict[str, Any]]:
    """Ищет упоминание работы по характерным словам в документах сессии.
    Возвращает ``{"src", "page", "text"}`` первого совпадения или ``None``.
    """
    if not keys:
        return None
    for src, page, text in _corpus(session_dir):
        low = str(text).lower()
        if any(k in low for k in keys):
            return {"src": src, "page": page, "text": str(text)[:160]}
    return None


def reset_corpus() -> None:
    """Сбрасывает кеш корпуса документов (для тестов и повторных запусков)."""
    _CORPUS_CACHE.clear()



if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "outputs"
    facts = load(d)
    if not facts:
        print("Документы ВОР/ПД не найдены")
        sys.exit(0)
    print(f"ВОР записей: {len(facts.vor)}, ПД записей: {len(facts.pd)}")
    print("классы бетона (ВОР):", sorted({r['b'] for r in facts.vor if r.get('b')}))
    print("классы бетона (ПД):", sorted({r['b'] for r in facts.pd if r.get('b')}))
    print("класс арматуры:", facts.rebar_class() or "—")
    for name in ("Фундаментная плита", "Стены подвала", "Плита перекрытия подвала",
                 "Лестничные марши", "Колонна", "Парапет кровли", "Свая буронабивная"):
        got = facts.concrete_bwf({"name": name})
        mark = "—" if not got else f"B{got[0]:g}" + (f" W{got[1]}" if got[1] else "") \
            + (f" F{got[2]}" if got[2] else "")
        print(f"  {name:<28} -> {mark}")
