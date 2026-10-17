"""Приведение ВОР заказчика к структуре типового ВОР.

В ВОР заказчика (PDF) объёмы сгруппированы по деформационным блокам, секциям
и этажам. Типовой ВОР (``data/Типовой ВОР по КР.xlsx``) имеет иную структуру:
**часть здания → раздел → подраздел → (геометрический) блок → работы/ресурсы**.
Модуль переносит объёмы заказчика в структуру типового шаблона:

* строки ВОР заказчика относятся к подразделу типового ВОР по **типу
  конструкции** (``mapping.detect_kind``) и **части здания** (``part``);
* внутри подраздела объёмы **суммируются по всем блокам/секциям/этажам** —
  группировка заказчика «схлопывается» к подразделам и частям здания типового
  ВОР;
* работы сопоставляются по виду объёма (опалубка, бетонирование, армирование,
  изоляция, засыпка, прочие — по наименованию), ресурсы — по марке бетона,
  диаметру/классу арматуры или наименованию материала;
* блоки с геометрическим заголовком («Стены толщиной … мм», «Колонны сечением
  ахb мм») разбиваются по фактическим толщинам/сечениям заказчика; строки без
  геометрии (арматура, опалубка) распределяются пропорционально объёму бетона
  группы.

Результат — файл ``ВОР_заказчика.xlsx`` (структура типового шаблона, объёмы
ВОР заказчика) в каталоге задачи.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from src.core.logger import setup_logger
from . import documents, mapping, template
from .builder import _format_header

logger = setup_logger(__name__)

Progress = Optional[Callable[[str], None]]

_DASH = "—"

# Слова-признаки изоляционных/защитных материалов (уточняют kind строки ВОР
# заказчика, когда наименование раздела даёт неверный тип конструкции).
_INSULATION_WORDS = ("праймер", "мембран", "пенополистирол", "галтель", "шнур",
                     "герметик", "теплоизоляц", "гидроизоляц", "бентонит")
# Слова-признаки закладных деталей (металлопрокат: пластины, уголки, швеллеры и
# т. п.). Учитываются только для строк в тоннах — так они не путаются с
# изоляционными материалами и арматурой.
_EMBEDDED_WORDS = ("закладн", "пластин", "уголок", "швеллер", "двутавр",
                   "полос", "прокат", "листов")
# Слова-признаки материалов деформационного шва. В ВОР заказчика они идут под
# подзаголовками «Деформационные швы …» (в зоне бетонной подготовки / ФП / стен
# подвала), которые детерминированный разбор относит к предыдущему блоку и не
# сохраняет, поэтому тип конструкции определяется по наименованию материала.
_SEAM_WORDS = ("деф.шв", "деформационн", "мастика", "вилатерм", "герметик",
               "геотекстил", "пленк", "плёнк", "флекс", "гидрошпонк")
# Аббревиатуры-признаки — ищутся как отдельные токены (подстрокой нельзя).
_SEAM_TOKENS = ("ппс",)

# Явные соответствия «наименование строки типового ВОР → ключевые слова поиска в
# ВОР заказчика»: применяются, когда автоматическое сопоставление по словам не
# срабатывает из-за различий в наименовании (например, «Усиливающий слой
# гидроизоляции» у заказчика назван «Доп. г/и ТЕХНОЭЛАСТ ФЛЕКС 600мм») либо даёт
# неверный результат (например, «Полиуретановый герметик» и «Полиуретановая
# плёнка» различаются только материалом и при мягком сопоставлении путаются).
# Эти соответствия имеют приоритет над автоматическим подбором.
_NAME_ALIASES = {
    "усиливающий слой гидроизоляции": ("техноэласт флекс",),
    "полиуретановый герметик": ("полиуритановый герметик",),
    "полиуретановая плёнка": ("полиуритановая пленка",),
}
# Разделы ВОР заказчика, не относящиеся к типовому ВОР (пропускаются):
# металлоконструкции, земляные работы котлована, конструкции МАФ.
_SKIP_SECTION_WORDS = ("маф", "металлические конструкции", "котлован", "шпунт")

# Слова-заполнители, не являющиеся «ведущим» (главным) словом работы.
_LEAD_STOP = {"для", "или", "и", "в", "на", "с", "по", "из", "мм", "м2", "м3",
              "тип", "типа", "устройство", "устройства", "монтаж", "демонтаж",
              "установка", "работы", "выполнение", "укладка", "прочие"}

# Толщина в наименовании: «толщиной 200мм», «t=100 мм», «t-50мм».
_THICKNESS_RX = re.compile(r"толщин\w*[^\d]{0,12}(\d{2,4})", re.IGNORECASE)
_T_MM_RX = re.compile(r"\bt\s*[-=]?\s*(\d{2,4})\s*мм", re.IGNORECASE)
# Сечение в наименовании: «400х400».
_SECTION_RX = re.compile(r"(\d{2,4})\s*[xх×]\s*(\d{2,4})")
# Дочерняя строка арматуры по диаметру: «D25», «d 16».
_REBAR_CHILD_RX = re.compile(r"^[dDдД]\s*\d+\s*$")

# Виды заголовков, после которых начинается новый блок/раздел.
_HEADER_KINDS = ("part", "section", "subsection", "block")


def _say(progress: Progress, message: str) -> None:
    if progress is not None:
        try:
            progress(message)
        except Exception:  # pragma: no cover — прогресс не должен ломать разбор
            pass


# =====================================================================
#  Строки ВОР заказчика
# =====================================================================

@dataclass
class CustomerRow:
    """Обогащённая строка ВОР заказчика."""

    section: str = ""
    block: str = ""
    name: str = ""
    unit: str = ""
    volume: float = 0.0
    unit_family: str = ""            # m3|m2|t|m|pcs|other
    part: Optional[str] = None       # underground|above|parking
    kind: Optional[str] = None       # тип конструкции (mapping.detect_kind)
    category: str = "other"          # formwork|concrete|rebar|insulation|seam|backfill|count|other
    grade: Optional[str] = None      # марка бетона (B30)
    diameter: Optional[int] = None   # диаметр арматуры, мм
    rebar_class: Optional[str] = None
    thickness: Optional[int] = None  # толщина из наименования, мм
    section_label: Optional[str] = None  # сечение из наименования («400х400»)
    orientation: Optional[str] = None  # horizontal|vertical (для гидроизоляции)
    component: str = ""              # подтип лестницы: flight (марш)|landing (площадка)
    tokens: frozenset = frozenset()


def _pick(row: dict) -> Optional[float]:
    """Объём строки ВОР заказчика: приоритет колонки «стало» (в т. ч. нулей)."""
    now = row.get("volume_now")
    was = row.get("volume_was")
    if now is not None:
        return float(now)
    if was is not None:
        return float(was)
    return None


def _unit_family(unit: str) -> str:
    """Семейство единиц измерения (``m3``/``m2``/``t``/``m``/``pcs``)."""
    u = documents.norm_unit(unit).rstrip(".")
    if not u:
        return ""
    if u.startswith("м3") or u.startswith("м³") or u.startswith("куб"):
        return "m3"
    if u.startswith("м2") or u.startswith("м²") or u.startswith("кв"):
        return "m2"
    if u.startswith("т") or u.startswith("тонн"):
        return "t"
    if u.startswith("пог") or u.startswith("п.м") or u.startswith("м.п") or u == "м":
        return "m"
    if u.startswith("шт"):
        return "pcs"
    return "other"


def _detect_part(section: str, block: str) -> Optional[str]:
    """Определяет часть здания по тексту раздела/блока ВОР заказчика."""
    for text in (section, block):
        part = mapping.detect_part(text)
        if part:
            return part
    text = f"{section or ''} {block or ''}".lower()
    if any(w in text for w in ("этаж", "надземн", "типов", "техническ", "кровл")):
        return "above"
    if any(w in text for w in ("подзем", "подвал")):
        return "underground"
    return None


def _section_kind(section: str) -> Optional[str]:
    """Тип конструкции по наименованию раздела ВОР заказчика."""
    kind = mapping.detect_kind(section)
    if kind:
        return kind
    low = (section or "").lower()
    if "фундамент" in low and not any(w in low for w in ("сва", "маф")):
        # Включая фундаменты башенных кранов: объёмы плит 1400/1800 мм
        # приведены в этом разделе ВОР заказчика.
        return "foundation_slab"
    return None


def _detect_kind(name: str, section: str) -> Optional[str]:
    """Определяет тип конструкции строки ВОР заказчика.

    Приоритет — наименование строки, затем раздел; для сопутствующих
    изоляционных материалов (праймер, мембрана, галтель…) тип уточняется до
    ``insulation``, даже если раздел назван «по периметру стен». Материалы
    деформационного шва (мастика, герметик, шнур «Вилатерм», гидрошпонка,
    полиуретановая плёнка, геотекстиль, экструзионный ППС и т. п.) относятся к
    подразделу «Устройство деформационного шва» (``seam``).
    """
    low = (name or "").lower()
    if any(w in low for w in _SEAM_WORDS) or _has_token(low, _SEAM_TOKENS):
        return "seam"
    kind = mapping.detect_kind(name)
    if kind:
        return kind
    if any(w in low for w in _INSULATION_WORDS):
        return "insulation"
    return _section_kind(section)


def _has_token(text: str, tokens) -> bool:
    """Есть ли в тексте хотя бы один из токенов (как отдельное слово)."""
    words = set(re.findall(r"[a-zа-яё0-9]+", (text or "").lower()))
    return any(t in words for t in tokens)


def _detect_category(name: str, unit: str) -> str:
    """Вид объёма строки ВОР заказчика."""
    s = (name or "").lower()
    if "опалубк" in s:
        return "formwork"
    if mapping.is_rebar_text(name) or _REBAR_CHILD_RX.match(name or ""):
        return "rebar"
    if "бетон" in s or "бетонирован" in s:
        return "concrete"
    if "шов" in s or "аквастоп" in s or "гидрошпонк" in s:
        return "seam"
    if any(w in s for w in ("гидроизоляц", "праймер", "мембран", "теплоизоляц",
                            "пенополистирол", "утеплит")):
        return "insulation"
    if "засыпк" in s:
        return "backfill"
    if _unit_family(unit) == "t" and any(w in s for w in _EMBEDDED_WORDS):
        return "embedded"
    if _unit_family(unit) == "pcs":
        return "count"
    return "other"


def _detect_orientation(section: str, block: str, name: str) -> Optional[str]:
    """Ориентация гидроизоляции по тексту раздела/блока/наименования.

    У заказчика горизонтальная и вертикальная гидроизоляция идут в разных
    разделах (подготовка фундамента / по периметру стен подвала), а в типовом
    ВОР — в разных подразделах с одинаковым типом конструкции.
    """
    text = f"{section or ''} {block or ''} {name or ''}".lower()
    if any(w in text for w in ("вертикальн", "по периметру", "периметр",
                               "наружных стен", "стен подвала", "пазух")):
        return "vertical"
    if any(w in text for w in ("горизонтальн", "подготовк", "по подошве")):
        return "horizontal"
    return None


def _thickness(name: str) -> Optional[int]:
    """Толщина (мм) из наименования строки ВОР заказчика."""
    m = _THICKNESS_RX.search(name or "") or _T_MM_RX.search(name or "")
    if not m:
        return None
    value = int(m.group(1))
    return value if 30 <= value <= 3000 else None


def _section_label(name: str) -> Optional[str]:
    """Сечение (``400х400``) из наименования строки ВОР заказчика."""
    m = _SECTION_RX.search(name or "")
    if not m:
        return None
    return f"{int(m.group(1))}х{int(m.group(2))}"


def _block_thickness_map(rows: List[dict]) -> Dict[Tuple[str, str], int]:
    """Толщина конструкции по (разделу, блоку) ВОР заказчика.

    Строки одного блока/секции наследуют толщину конструкции из строки с
    наименованием «… толщиной N мм …» (тот же приём, что и в
    ``documents.extract_rebar``). Используется для строк без явной толщины в
    наименовании (закладные детали — пластины, уголки и т. п.), чтобы отнести
    их к нужному геометрическому блоку.
    """
    result: Dict[Tuple[str, str], int] = {}
    for row in rows:
        thickness = mapping.parse_thickness(str(row.get("name") or ""))
        if thickness:
            key = (str(row.get("section") or "").strip(),
                   str(row.get("block") or "").strip())
            result[key] = thickness
    return result


def collect_rows(rows: List[dict]) -> List[CustomerRow]:
    """Обогащает строки ВОР заказчика контекстом (тип, часть, категория, геометрия).

    Родительские строки, у которых есть дочерние (позиция ``N`` при наличии
    ``N.k``), пропускаются — их объём уже распределён по дочерним строкам
    (иначе объёмы считались бы дважды).
    """
    thickness_map = _block_thickness_map(rows)
    components = documents.row_components(rows)
    parents_with_children = set()
    for row in rows:
        pos = str(row.get("pos") or "").strip()
        if "." in pos:
            key = (str(row.get("section") or "").strip(),
                   str(row.get("block") or "").strip(),
                   pos.split(".", 1)[0])
            parents_with_children.add(key)

    result: List[CustomerRow] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        unit = str(row.get("unit") or "").strip()
        volume = _pick(row)
        if volume is None:
            continue
        pos = str(row.get("pos") or "").strip()
        section = str(row.get("section") or "").strip()
        block = str(row.get("block") or "").strip()
        if pos and "." not in pos and (section, block, pos) in parents_with_children:
            continue

        low_section = section.lower()
        if any(w in low_section for w in _SKIP_SECTION_WORDS):
            continue

        diameter, rebar_class = mapping.parse_rebar_spec(name)
        category = _detect_category(name, unit)
        thickness = _thickness(name)
        if thickness is None and category == "embedded":
            # Закладные детали обычно идут без толщины в наименовании — берём её
            # из строки блока/секции («… толщиной N мм …»), чтобы отнести позицию
            # к нужному геометрическому блоку (плита толщиной N мм).
            thickness = thickness_map.get((section, block))
        result.append(CustomerRow(
            section=section,
            block=block,
            name=name,
            unit=unit,
            volume=volume,
            unit_family=_unit_family(unit),
            part=_detect_part(section, block),
            kind=_detect_kind(name, section),
            category=category,
            grade=mapping.parse_concrete_grade(name),
            diameter=diameter,
            rebar_class=rebar_class,
            thickness=thickness,
            section_label=_section_label(name),
            orientation=_detect_orientation(section, block, name),
            component=components.get((section, block, pos), ""),
            tokens=frozenset(documents.tokens(name)),
        ))

    logger.info("ВОР заказчика: строк для приведения — %d", len(result))
    return result


# =====================================================================
#  Отбор и суммирование объёмов
# =====================================================================

def _in_scope(row: CustomerRow, kind: Optional[str], part: Optional[str]) -> bool:
    """Относится ли строка заказчика к подразделу типового ВОР (kind + part)."""
    if part and row.part and row.part != part:
        return False
    if kind and row.kind and row.kind != kind:
        return False
    return True


def _sum_rows(rows: List[CustomerRow], categories, unit_family: str = "") -> float:
    """Сумма объёмов строк указанных категорий (с фильтром по единице)."""
    total = 0.0
    for row in rows:
        if row.category not in categories:
            continue
        if unit_family and row.unit_family and row.unit_family != unit_family:
            continue
        total += row.volume
    return total


def _concrete_total(rows: List[CustomerRow], grade: Optional[str] = None) -> float:
    """Сумма объёма бетона строк заказчика (при указании — только марки)."""
    total = 0.0
    for row in rows:
        if row.category != "concrete" or row.unit_family not in ("m3", ""):
            continue
        if grade and (row.grade or "") != grade:
            continue
        total += row.volume
    return total


# Длина основы слова для сопоставления (учитывает падежи: «маршей»/«марш»).
_STEM_LEN = 4


def _stem(token: str) -> str:
    return token[:_STEM_LEN] if len(token) > _STEM_LEN else token


def _common_words(query_words: set, row_words: set) -> set:
    """Общие значимые слова запроса и строки (сравнение по основе)."""
    stems = {_stem(w) for w in row_words}
    return {w for w in query_words if _stem(w) in stems}


def _lead_word(name: str) -> Optional[str]:
    """«Ведущее» (главное) слово наименования работы/ресурса.

    Используется для мягкого сопоставления: «Гидроизоляция рулонная на основе
    битумного вяжущего» и «Гидроизоляция фундамента, горизонтальная…» связаны
    общим ведущим словом «гидроизоляция». Слова-заполнители («Устройство»,
    «Монтаж» и т. п.) пропускаются.
    """
    for token in re.findall(r"[a-zа-яё0-9]+", (name or "").lower()):
        if token in _LEAD_STOP or documents.is_num(token) or len(token) < 3:
            continue
        return token
    return None


def _context_orientation(subsection: str) -> Optional[str]:
    """Ориентация гидроизоляции по названию подраздела типового ВОР.

    Разделяет горизонтальную и вертикальную гидроизоляцию: у заказчика они
    могут быть в разных разделах, а в типовом ВОР — в разных подразделах с
    одинаковым типом конструкции.
    """
    low = (subsection or "").lower()
    if "горизонтальн" in low:
        return "horizontal"
    if "вертикальн" in low:
        return "vertical"
    return None


def _sum_by_name(rows: List[CustomerRow], name: str, unit_family: str = "",
                 lead: bool = False, lead_limit: int = 0,
                 orientation: Optional[str] = None,
                 cross_unit: bool = False,
                 min_score: float = 0.5) -> Optional[float]:
    """Сумма объёмов строк, близких по наименованию (устойчиво к падежам).

    Числовые токены запроса (диаметры, размеры) обязаны совпасть. Строка
    принимается, если совпало не менее двух значимых слов, или доля совпадений
    ≥ 0,6, или (при ``lead``) совпало «ведущее» слово наименования.
    ``lead_limit`` — ограничение числа значимых слов запроса, при котором
    правило «ведущего» слова применяется (для ресурсов — короткие названия).

    ``cross_unit`` — при отсутствии совпадений по своей единице измерения
    разрешить сопоставление при **другой** единице (например, стяжка у заказчика
    в м3, а в типовом ВОР — в м2). В этом случае берутся только строки с
    **наилучшим** совпадением наименований (не ниже ``min_score``) — это не
    порождает ложных совпадений («гидрошпонка» ~ «гидроизоляция») и двойного
    счёта. Используется только для работ: у материалов разные единицы обычно
    означают разные материалы.
    """
    query = documents.tokens(name)
    if not query:
        return None
    query_words = {t for t in query if not documents.is_num(t)}
    query_nums = {t for t in query if documents.is_num(t)}
    if lead_limit and len(query_words) > lead_limit:
        lead = False
    lead_token = _lead_word(name) if lead else None

    def _score(row: CustomerRow, strict_unit: bool) -> Optional[float]:
        """Оценка близости строки (``None`` — строка не подходит)."""
        if row.category == "rebar":
            return None
        if strict_unit and unit_family and row.unit_family and row.unit_family != unit_family:
            return None
        if orientation and row.orientation and row.orientation != orientation:
            return None
        if query_nums and not (query_nums & set(row.tokens)):
            return None
        row_words = {t for t in row.tokens if not documents.is_num(t)}
        common = _common_words(query_words, row_words)
        score = len(common) / len(query_words) if query_words else 0.0
        accepted = (len(common) >= 2 or score >= 0.6
                    or (lead_token is not None
                        and bool(_common_words({lead_token}, row_words))))
        return score if accepted else None

    # 1. Строгое сопоставление — в пределах той же единицы измерения.
    total = 0.0
    found = False
    for row in rows:
        if _score(row, True) is None:
            continue
        total += row.volume
        found = True
    if found:
        return total
    if not (cross_unit and unit_family):
        return None

    # 2. Другая единица измерения — только строки с наилучшим совпадением.
    scored = [(score, row) for row in rows
              for score in (_score(row, False),) if score is not None]
    if not scored:
        return None
    best = max(score for score, _ in scored)
    if best < min_score:
        return None
    total = sum(row.volume for score, row in scored if score >= best - 1e-9)
    return total if total else None


def _pick_total(primary: List[CustomerRow], categories, unit_family: str,
                fallback: List[CustomerRow], share: float) -> Optional[float]:
    """Объём категории: сначала в строках группы, затем в «негруппированных».

    Строки без геометрии (арматура, опалубка) распределяются между
    геометрическими группами пропорционально доле группы (``share``).
    """
    total = _sum_rows(primary, categories, unit_family)
    if total:
        return total
    if fallback:
        total = _sum_rows(fallback, categories, unit_family)
        if total:
            return total * share
    return None


# =====================================================================
#  Заполнение строк типового ВОР
# =====================================================================

@dataclass
class _Context:
    """Контекст заполнения (данные арматуры и ссылка на источник)."""

    rebar: documents.RebarData
    ref_vor: str


def _fill_work(row: template.TemplateRow, group_rows: List[CustomerRow],
               fallback_rows: List[CustomerRow], ctx: _Context,
               kind: Optional[str], part: Optional[str],
               share: float, thickness: Optional[int] = None,
               component: str = "") -> Tuple[Optional[float], Optional[str]]:
    """Объём и обоснование для строки-работы типового ВОР.

    ``component`` — подтип лестницы (``flight``/``landing``): для работ маршей
    и площадок объёмы берутся из соответствующих строк заказчика.
    """
    qk = mapping.classify_work(row.name)
    family = _unit_family(row.unit)
    orientation = _context_orientation(row.subsection)

    if qk == "rebar":
        t = documents.rebar_thickness(ctx.rebar, kind, part, thickness)
        total = ctx.rebar.section_total(kind, part, t, component) * share
        return (round(total, 2), ctx.ref_vor) if total > 0 else (None, None)

    # Явные соответствия наименований (`_NAME_ALIASES`) имеют приоритет: для этих
    # строк автоматическое сопоставление по словам даёт неверный результат.
    total: Optional[float] = _sum_by_aliases(row.name, group_rows, fallback_rows,
                                             share, orientation)
    if total is None:
        # Опалубка и бетонирование надёжно определяются видом работы; остальные
        # работы сопоставляются по наименованию с наименованиями заказчика.
        if qk == "formwork":
            total = _pick_total(group_rows, {"formwork"}, family, fallback_rows, share)
        elif qk == "concrete":
            total = _pick_total(group_rows, {"concrete"}, family or "m3", fallback_rows, share)

    if total is None:
        # Для штучных работ («Установка сборных … маршей») «ведущее» слово
        # слишком общее («сборных»), поэтому применяется строгое сопоставление.
        lead = qk != "count"
        total = _sum_by_name(group_rows, row.name, family, lead=lead,
                             orientation=orientation, cross_unit=True)
        if total is None and fallback_rows:
            total = _sum_by_name(fallback_rows, row.name, family, lead=lead,
                                 orientation=orientation, cross_unit=True)
            if total is not None:
                total *= share

    return (round(total, 2), ctx.ref_vor) if total else (None, None)


def _sum_by_aliases(name: str, group_rows: List[CustomerRow],
                    fallback_rows: List[CustomerRow], share: float,
                    orientation: Optional[str] = None) -> Optional[float]:
    """Сумма по явным соответствиям наименований (`_NAME_ALIASES`).

    Применяется приоритетно к автоматическому сопоставлению: если для строки
    типового ВОР задано явное соответствие, объём берётся по нему (в том числе
    когда автоматическое сопоставление дало бы неверный результат из-за
    похожих наименований материалов).
    """
    for alias in _NAME_ALIASES.get((name or "").strip().lower(), ()):
        total = _sum_by_name(group_rows, alias, orientation=orientation)
        if total is None and fallback_rows:
            total = _sum_by_name(fallback_rows, alias, orientation=orientation)
            if total is not None:
                total *= share
        if total:
            return total
    return None


def _fill_resource(row: template.TemplateRow, group_rows: List[CustomerRow],
                   fallback_rows: List[CustomerRow], ctx: _Context,
                   kind: Optional[str], part: Optional[str],
                   share: float, thickness: Optional[int] = None,
                   component: str = "") -> Tuple[Optional[float], Optional[str]]:
    """Объём и обоснование для строки-ресурса типового ВОР (кроме бетона)."""
    category = mapping.classify_resource(row.name)
    family = _unit_family(row.unit)

    if category == "rebar":
        diameter, rebar_class = mapping.parse_rebar_spec(row.name)
        t = documents.rebar_thickness(ctx.rebar, kind, part, thickness)
        total = ctx.rebar.diameter_class(diameter, rebar_class, kind=kind, part=part,
                                         thickness=t, component=component) * share
        if total <= 0 and diameter is None and rebar_class:
            total = ctx.rebar.diameter_class(None, rebar_class, kind=kind, part=part,
                                             thickness=t, component=component) * share
        return (round(total, 2), ctx.ref_vor) if total > 0 else (None, None)

    orientation = _context_orientation(row.subsection)
    total = _sum_by_name(group_rows, row.name, family, lead=True, lead_limit=2,
                         orientation=orientation)
    if total is None and fallback_rows:
        total = _sum_by_name(fallback_rows, row.name, family, lead=True, lead_limit=2,
                             orientation=orientation)
        if total is not None:
            total *= share
    return (round(total, 2), ctx.ref_vor) if total else (None, None)


# =====================================================================
#  Динамические строки-ресурсы (арматура и бетон по фактическим данным)
# =====================================================================

def _concrete_specs(rows: List[CustomerRow]) -> List[Tuple[str, float]]:
    """Марки бетона строк заказчика: ``[(«В30, W6, F200», объём), …]``.

    Порядок — по убыванию объёма (первой идёт преобладающая марка).
    """
    totals: Dict[str, float] = {}
    for row in rows:
        if row.category != "concrete" or row.unit_family not in ("m3", ""):
            continue
        spec = mapping.parse_concrete_spec(row.name)
        if not spec:
            continue
        totals[spec] = totals.get(spec, 0.0) + row.volume
    return sorted(((spec, volume) for spec, volume in totals.items() if volume > 0),
                  key=lambda item: -item[1])


def _embedded_specs(rows: List[CustomerRow]) -> List[Tuple[str, float]]:
    """Закладные детали строк заказчика: ``[(«Пластина t12», 2.38), …]``.

    Наименования берутся как в ВОР заказчика (пластины, уголки и т. п.),
    порядок — по убыванию объёма (первой идёт преобладающая позиция).
    """
    totals: Dict[str, float] = {}
    for row in rows:
        if row.category != "embedded":
            continue
        totals[row.name] = totals.get(row.name, 0.0) + row.volume
    return sorted(((name, volume) for name, volume in totals.items() if volume > 0),
                  key=lambda item: -item[1])


def _resource_sample(children: List[template.TemplateRow], category: str) -> Optional[int]:
    """Строка-образец ресурса (для стилей динамических строк)."""
    for row in children:
        if row.kind == "resource" and mapping.classify_resource(row.name) == category:
            return row.index
    for row in children:
        if row.kind == "resource":
            return row.index
    return None


def _fill_rows(children: List[template.TemplateRow], group_rows: List[CustomerRow],
               fallback_rows: List[CustomerRow], ctx: _Context,
               kind: Optional[str], part: Optional[str],
               share: float, thickness: Optional[int] = None) -> List[template.OutputRow]:
    """Заполняет строки одного блока/подраздела типового ВОР.

    Строки-работы арматуры («…стержней» / «…каркасов»), бетонирования и
    закладных деталей получают **динамические** строки-ресурсы по фактическим
    данным ВОР заказчика: арматура — диаметры и классы из разделов заказчика
    («Арматура класса …» → стержни, «Арматурные выпуски …» → каркасы), бетон —
    марки из наименований строк заказчика, закладные — фактические позиции
    (пластины, уголки и т. п.). Статические строки шаблона под этими работами
    заменяются.
    """
    buckets = documents.rebar_buckets(ctx.rebar, kind, part, thickness, share)
    embedded_specs = _embedded_specs(group_rows)
    if not embedded_specs and fallback_rows:
        # Закладные без привязки к геометрии распределяются по доле группы.
        embedded_specs = [(name, volume * share)
                          for name, volume in _embedded_specs(fallback_rows)]

    # Подтип лестницы для каждой строки блока (с наследованием в пределах блока):
    # объёмы маршей и площадок у заказчика идут разными строками.
    row_component: Dict[int, str] = {}
    if kind in ("stair", "precast"):
        current = ""
        for row in children:
            comp = mapping.stair_component(row.name)
            if comp:
                current = comp
            row_component[row.index] = current

    def component_of(row: template.TemplateRow) -> str:
        return row_component.get(row.index, "")

    subsets: Dict[str, Tuple[List[CustomerRow], List[CustomerRow]]] = {
        "": (group_rows, fallback_rows)}

    def subset(component: str) -> Tuple[List[CustomerRow], List[CustomerRow]]:
        """Строки заказчика группы по подтипу лестницы (марш/площадка)."""
        if component not in subsets:
            has_components = any(r.component for r in group_rows)
            sub = [r for r in group_rows if r.component == component] \
                if has_components else group_rows
            fb = [r for r in fallback_rows if r.component == component] \
                if component and any(r.component for r in fallback_rows) else fallback_rows
            subsets[component] = (sub, fb)
        return subsets[component]

    rebar_works = [r for r in children
                   if r.kind == "work" and mapping.classify_work(r.name) == "rebar"]
    has_bars_work = any(mapping.rebar_bucket(r.name) == "bars" for r in rebar_works)
    has_frames_work = any(mapping.rebar_bucket(r.name) == "frames" for r in rebar_works)

    fills: Dict[int, Tuple[Optional[float], Optional[str]]] = {}
    for row in children:
        component = component_of(row)
        sub_rows, sub_fallback = subset(component)
        if row.kind == "empty":
            fills[row.index] = (None, None)
        elif row.kind == "resource":
            fills[row.index] = (None, None) if mapping.classify_resource(row.name) == "concrete" \
                else _fill_resource(row, sub_rows, sub_fallback, ctx, kind, part,
                                    share, thickness, component)
        else:
            fills[row.index] = _fill_work(row, sub_rows, sub_fallback, ctx, kind, part,
                                          share, thickness, component)

    # Бетон — по маркам: объём марки заполняется в первую строку этой марки,
    # повторные строки той же марки остаются с прочерком. Если марки заказчика
    # не совпали ни с одной строкой — прочерк (объём виден в строке работы).
    concrete_rows = [r for r in children
                     if r.kind == "resource" and mapping.classify_resource(r.name) == "concrete"]
    used_grades = set()
    for row in concrete_rows:
        grade = mapping.parse_concrete_grade(row.name)
        key = (grade or "").upper().replace("В", "B")
        if key and key in used_grades:
            fills[row.index] = (None, None)
            continue
        sub_rows, sub_fallback = subset(component_of(row))
        volume = _concrete_total(sub_rows, key) if key else _concrete_total(sub_rows)
        if not volume and sub_fallback:
            volume = (_concrete_total(sub_fallback, key) if key
                      else _concrete_total(sub_fallback)) * share
        if volume:
            fills[row.index] = (round(volume, 2), ctx.ref_vor)
            if key:
                used_grades.add(key)
        else:
            fills[row.index] = (None, None)

    # Арматура: строка-работа = тоннаж раздела из ВОР заказчика (в доле группы);
    # если разрез пуст — сумма найденных ресурсов арматуры этой работы.
    rebar_resources = [r for r in children
                       if r.kind == "resource" and mapping.classify_resource(r.name) == "rebar"]
    for work in rebar_works:
        if fills[work.index][0]:
            continue
        prefix = str(work.number).strip() + "."
        related = [r for r in rebar_resources if str(r.number).strip().startswith(prefix)]
        resource_sum = sum(fills[r.index][0] or 0.0 for r in related)
        if resource_sum > 0:
            fills[work.index] = (round(resource_sum, 2), ctx.ref_vor)

    # Строки-ресурсы арматуры, не относящиеся к работам армирования («Устройство
    # закладных деталей» и т. п.), не заполняются из общего тоннажа раздела —
    # иначе объём арматуры удваивался бы с работами «…стержней»/«…каркасов».
    work_by_number = {str(r.number).strip(): r for r in children if r.kind == "work"}
    for r in rebar_resources:
        parent = work_by_number.get(str(r.number).strip().split(".", 1)[0])
        if parent is None or mapping.classify_work(parent.name) != "rebar":
            fills[r.index] = (None, None)

    # --- Эмиссия строк: подмена статических строк-ресурсов динамическими ---
    output: List[template.OutputRow] = []
    i = 0
    total_rows = len(children)
    while i < total_rows:
        row = children[i]
        kids: List[template.TemplateRow] = []
        j = i + 1
        while j < total_rows and children[j].kind in ("resource", "variant", "empty"):
            kids.append(children[j])
            j += 1

        items: Dict = {}
        category = ""
        dynamic = False
        if row.kind == "work":
            qk = mapping.classify_work(row.name)
            component = component_of(row)
            row_buckets = documents.rebar_buckets(ctx.rebar, kind, part, thickness,
                                                  share, component) if component else buckets
            if qk == "rebar" and (row_buckets.get("bars") or row_buckets.get("frames")):
                items = documents.rebar_work_items(row_buckets, row.name,
                                                   has_bars_work, has_frames_work)
                category = "rebar"
                dynamic = True
            elif qk == "concrete" and _unit_family(row.unit) in ("m3", ""):
                concrete_specs = _concrete_specs(subset(component)[0])
                if concrete_specs:
                    items = dict(concrete_specs)
                    category = "concrete"
                    dynamic = True
            elif qk == "embedded" and embedded_specs:
                items = dict(embedded_specs)
                category = "embedded"
                dynamic = True

        if not dynamic:
            output.append(_output_row(row, fills))
            i += 1
            continue

        output.append(template.OutputRow(src_index=row.index,
                                         volume=round(sum(items.values()), 2) if items else None,
                                         justification=ctx.ref_vor if items else None))
        sample = _resource_sample(kids, category) or row.index
        ordered = sorted(items.items(), key=documents.rebar_sort_key) if category == "rebar" \
            else list(items.items())
        number = 0
        for key, volume in ordered:
            number += 1
            if category == "rebar":
                name = documents.rebar_label(*key)
            elif category == "concrete":
                name = mapping.concrete_label(key)
            else:
                name = key
            output.append(template.OutputRow(
                src_index=sample, label=f"{row.number}.{number}", name_override=name,
                volume=round(volume, 2), justification=ctx.ref_vor))
        for child in kids:
            if child.kind == "resource":
                child_category = mapping.classify_resource(child.name)
                # Заменяем статические строки-ресурсы того же вида динамическими;
                # у закладных деталей подменяются и «прочие» металлоресурсы
                # шаблона (прокат, уголки) — их место занимают фактические позиции.
                if child_category == category or (category == "embedded"
                                                  and child_category == "other"):
                    continue
            label = None
            if child.kind == "resource":
                number += 1
                label = f"{row.number}.{number}"
            output.append(_output_row(child, fills, label))
        i = j

    return output


def _output_row(row: template.TemplateRow,
                fills: Dict[int, Tuple[Optional[float], Optional[str]]],
                label: Optional[str] = None) -> template.OutputRow:
    """Строка итогового ВОР по рассчитанным значениям."""
    volume, justification = fills.get(row.index, (None, None))
    return template.OutputRow(src_index=row.index, label=label,
                              volume=volume, justification=justification)


# =====================================================================
#  Группировка по геометрии
# =====================================================================

def _block_text(row: template.TemplateRow) -> str:
    """Текст геометрического/блочного заголовка шаблона.

    У строк-заголовков шаблона текст записан в колонке A (``number``), а не в
    колонке B (``name``), поэтому для заголовков блока используется ``number``.
    """
    return row.number or row.name


def _geometry_value(row: CustomerRow, param: str) -> Optional[str]:
    if param == "thickness":
        return str(row.thickness) if row.thickness else None
    if param == "section":
        return row.section_label
    return None


def _geometry_groups(rows: List[CustomerRow], param: str
                     ) -> Tuple[Dict[str, List[CustomerRow]], List[CustomerRow]]:
    """Делит строки на геометрические группы и «негруппированные» строки.

    Строки без объёма (например, позиции, у которых заполнена только колонка
    «было», а «стало» = 0) в геометрические группы не попадают — иначе в ВОР
    появлялся бы пустой блок.
    """
    groups: Dict[str, List[CustomerRow]] = {}
    ungrouped: List[CustomerRow] = []
    for row in rows:
        label = _geometry_value(row, param) if row.volume > 0 else None
        if label:
            groups.setdefault(label, []).append(row)
        else:
            ungrouped.append(row)
    return groups, ungrouped


def _geometry_sort(label: str):
    m = re.match(r"\d+(?:[.,]\d+)?", label or "")
    if m:
        return (0, float(m.group(0).replace(",", ".")), label)
    return (0, 0.0, label)


def _block_header(index, kind: Optional[str], part: Optional[str], label: Optional[str],
                  fallback_text: str, geometry: Optional[str]) -> str:
    """Заголовок геометрического блока ВОР заказчика.

    Вместо текста шаблона («Плита толщиной … мм») подставляется наименование
    группы элементов модели IFC (``Фундаментная плита:ADSK_Бетон В30_1000 мм``);
    если данных модели нет — текст шаблона с фактическим значением геометрии.
    """
    if index is not None and label:
        name = index.geometry_group_name(kind, part, label)
        if name:
            return name
    return _format_header(fallback_text, geometry, label)


def _geometry_int(label: Optional[str]) -> Optional[int]:
    """Числовое значение геометрической подписи («1400» → 1400)."""
    if not label:
        return None
    m = re.match(r"\d+", label)
    return int(m.group(0)) if m else None


# =====================================================================
#  Основная сборка
# =====================================================================

def build_customer_vor(vor_path: str, output_path: str, template_path: str = None,
                       sheet_name: str = template.DEFAULT_SHEET,
                       progress: Progress = None,
                       elements_json_path: str = None) -> Dict[str, object]:
    """Строит ВОР заказчика в структуре типового ВОР.

    ``elements_json_path`` — сырой дамп элементов модели IFC: используется для
    подстановки наименований групп элементов в заголовки геометрических блоков.
    Возвращает статистику сборки (число строк, заполненных объёмов, строк
    ВОР заказчика, тоннаж арматуры).
    """
    if os.path.splitext(vor_path)[1].lower() != ".pdf":
        raise ValueError("Приведение к типовому ВОР поддерживается только для PDF-файла ВОР")

    template_path = template_path or template.DEFAULT_TEMPLATE_PATH
    from src.services.doc_pdf_parser import extract_vor_rows

    _say(progress, "ВОР заказчика: разбор таблиц")
    raw = extract_vor_rows(vor_path)
    if not raw:
        raise ValueError("В ВОР заказчика не найдено строк таблиц")

    rows = collect_rows(raw)
    rebar = documents.extract_rebar(raw)
    _say(progress, f"ВОР заказчика: строк — {len(rows)}, арматура — {round(rebar.total, 2)} т")

    index = None
    if elements_json_path and os.path.isfile(elements_json_path):
        from .elements import ElementIndex
        try:
            index = ElementIndex.from_raw_dump(elements_json_path)
        except Exception as exc:  # pragma: no cover — заголовки блоков не критичны
            logger.warning("ВОР заказчика: не удалось построить индекс модели: %s", exc)

    trows = template.parse_template(template_path, sheet_name)
    by_index = {r.index: r for r in trows}
    pos_by_index = {r.index: k for k, r in enumerate(trows)}

    header_indices = [r.index for r in trows if r.kind in _HEADER_KINDS]
    block_headers = [r for r in trows if r.kind == "block"]
    block_rows: Dict[int, List[template.TemplateRow]] = {}
    block_end: Dict[int, int] = {}
    for header in block_headers:
        later = [i for i in header_indices if i > header.index]
        end = (later[0] - 1) if later else trows[-1].index
        block_end[header.index] = end
        block_rows[header.index] = [by_index[i] for i in range(header.index + 1, end + 1)
                                    if i in by_index and by_index[i].kind in
                                    ("work", "resource", "variant", "empty")]

    ctx = _Context(rebar=rebar, ref_vor=f"ВОР заказчика: {os.path.basename(vor_path)}")
    output: List[template.OutputRow] = []
    filled = 0
    i = 0
    total_rows = len(trows)

    while i < total_rows:
        row = trows[i]

        if row.parking:
            # Часть «Автостоянка»: строки раздела всегда переносятся в ВОР,
            # но не заполняются — если в проекте автостоянки нет, раздел
            # остаётся пустым (объёмы заказчика по нему не рассчитываются).
            output.append(template.OutputRow(src_index=row.index))
            i += 1
            continue

        if row.kind == "block":
            part = row.part if row.part in ("underground", "above") else None
            scope = [r for r in rows if _in_scope(r, row.element_kind, part)]
            children = block_rows.get(row.index, [])

            groups, ungrouped = _geometry_groups(scope, row.geometry) \
                if row.geometry in ("thickness", "section") and scope else ({}, scope)

            if groups:
                total_concrete = _concrete_total(scope)
                for label in sorted(groups, key=_geometry_sort):
                    group = groups[label]
                    group_concrete = _concrete_total(group)
                    share = (group_concrete / total_concrete) if total_concrete > 0 else 1.0
                    thickness = _geometry_int(label) if row.geometry == "thickness" else None
                    output.append(template.OutputRow(
                        src_index=row.index,
                        label=_block_header(index, row.element_kind, part, label,
                                            _block_text(row), row.geometry)))
                    for out_row in _fill_rows(children, group, ungrouped, ctx,
                                              row.element_kind, part, share, thickness):
                        if out_row.volume is not None:
                            filled += 1
                        output.append(out_row)
            else:
                header_label = _format_header(_block_text(row), row.geometry, None) \
                    if row.geometry else _block_text(row)
                output.append(template.OutputRow(src_index=row.index, label=header_label))
                for out_row in _fill_rows(children, scope, [], ctx, row.element_kind, part, 1.0):
                    if out_row.volume is not None:
                        filled += 1
                    output.append(out_row)
            i = pos_by_index[block_end[row.index]] + 1
            continue

        if row.kind in ("work", "resource", "variant"):
            group: List[template.TemplateRow] = []
            while i < total_rows and trows[i].kind in ("work", "resource", "variant"):
                group.append(trows[i])
                i += 1
            part = group[0].part if group[0].part in ("underground", "above") else None
            scope = [r for r in rows if _in_scope(r, group[0].element_kind, part)]
            for out_row in _fill_rows(group, scope, [], ctx, group[0].element_kind, part, 1.0):
                if out_row.volume is not None:
                    filled += 1
                output.append(out_row)
            continue

        output.append(template.OutputRow(src_index=row.index))
        i += 1

    _say(progress, "Запись ВОР заказчика по типовому шаблону")
    template.render_template(output, path=template_path, sheet_name=sheet_name,
                             output_path=output_path)

    stats = {
        "rows": len(output),
        "filled": filled,
        "customer_rows": len(rows),
        "rebar_total": round(rebar.total, 2),
        "source": os.path.basename(vor_path),
        "output": os.path.basename(output_path),
    }
    logger.info("ВОР заказчика приведён к типовому виду: строк %d, заполнено %d",
                len(output), filled)
    return stats
