"""Правила сопоставления типового ВОР и элементов модели IFC.

Модуль не зависит от IFC напрямую: он содержит только словари и функции
сопоставления текстов типового ВОР (разделы, подразделы, геометрические
заголовки, наименования работ и ресурсов) с типами конструкций и видами
объёмов.

Основные понятия:

* **kind** — тип конструкции (``wall``, ``slab``, ``column``, ``beam``,
  ``foundation_slab``, ``stair``, ``precast`` — сборные лестницы, ``pile``,
  ``parapet``, ``insulation``, ``thermoliner``, ``backfill``, ``seam``,
  ``equipment_foundation``; отдельные типы сборных изделий —
  ``precast_panel``, ``precast_parapet``, ``precast_shaft``, ``precast_vent``);
* **part** — часть здания (``underground`` — подземная, ``above`` —
  надземная, ``parking`` — автостоянка);
* **quantity kind** работы — вид объёма, которым заполняется строка
  (опалубка → площадь, бетонирование → объём, армирование → тоннаж и т. д.);
* **resource spec** — характеристики ресурса (марка бетона, диаметр и класс
  арматуры).
"""

import re
from typing import Optional, Tuple

# =====================================================================
#  Определение типа конструкции (kind) по тексту
# =====================================================================

# Порядок ВАЖЕН: более специфичные правила проверяются раньше общих.
KIND_RULES = (
    # «Сборные» — это технология, а не отдельный тип конструкции: к типу
    # ``precast`` относятся только сборные **лестницы** (в модели — сборные
    # лестничные марши). Прочие сборные изделия (стеновые панели, парапеты,
    # шахты лифтов, вентблоки) выделяются в отдельные типы: у каждого подраздела
    # свой состав элементов, поэтому они не должны получать количество сборных
    # лестниц.
    ("precast", (r"лестниц\w*[^.;]*сборн", r"сборн\w*[^.;]*лестниц",
                 r"\bмарш\w*[^.;]*сборн", r"сборн\w*[^.;]*\bмарш")),
    ("precast_panel", (r"сборн\w*[^.;]*панел", r"панел\w*[^.;]*сборн")),
    ("precast_parapet", (r"сборн\w*[^.;]*парапет", r"парапет\w*[^.;]*сборн")),
    ("precast_shaft", (r"сборн\w*[^.;]*шахт", r"шахт\w*[^.;]*сборн")),
    ("precast_vent", (r"сборн\w*[^.;]*вентблок", r"вентблок\w*[^.;]*сборн")),
    ("equipment_foundation", (r"фундамент\w*\s+под\s+(инженерн|оборудован)",)),
    ("pile", (r"\bсва[йияе]",)),
    ("parapet", (r"парапет",)),
    ("foundation_slab", (r"фундаментн\w*\s+плит", r"плита\s+рамп", r"плит\w*\s+фундамент")),
    ("stair", (r"лестниц", r"лестничн")),
    ("column", (r"колонн", r"пилон")),
    ("beam", (r"\bбалк",)),
    ("slab", (r"плит\w*\s+(перекрыти|покрыти)", r"\bперекрыти", r"плита\s+(перекрытия|покрытия)")),
    ("wall", (r"\bстен", r"\bстена\b")),
    ("thermoliner", (r"термовкладыш",)),
    ("insulation", (r"гидроизоляц", r"теплоизоляц", r"\bизоляц")),
    ("seam", (r"деформационн\w*\s+шв",)),
    ("backfill", (r"засыпк",)),
)


def detect_kind(text: Optional[str]) -> Optional[str]:
    """Определяет тип конструкции (kind) по тексту раздела/подраздела.

    Возвращает ``None``, если тип не распознан.
    """
    s = (text or "").lower()
    if not s:
        return None
    for kind, patterns in KIND_RULES:
        for pattern in patterns:
            if re.search(pattern, s):
                return kind
    return None


# =====================================================================
#  Определение части здания (part)
# =====================================================================

PART_RULES = (
    ("underground", (r"подземн", r"подвал")),
    ("above", (r"надземн",)),
    ("parking", (r"автостоянк",)),
)


def detect_part(text: Optional[str]) -> Optional[str]:
    """Определяет часть здания (part) по тексту заголовка-части."""
    s = (text or "").lower()
    if not s:
        return None
    for part, patterns in PART_RULES:
        for pattern in patterns:
            if re.search(pattern, s):
                return part
    return None


# =====================================================================
#  Геометрические заголовки («Стены толщиной … мм» и т. п.)
# =====================================================================

GEOMETRY_RULES = (
    ("thickness", r"толщин"),
    ("section", r"сечени"),
    ("area", r"площад"),
)


# Толщина в наименовании: «толщиной 200мм», «t=100 мм», «t-50мм».
_THICKNESS_RX = re.compile(r"толщин\w*[^\d]{0,12}(\d{2,4})", re.IGNORECASE)
_T_MM_RX = re.compile(r"\bt\s*[-=]?\s*(\d{2,4})\s*мм", re.IGNORECASE)


def parse_thickness(text: Optional[str]) -> Optional[int]:
    """Толщина конструкции (мм) из наименования строки документа.

    Понимает «толщиной 200мм», «t=100 мм», «t-50мм». Значения вне диапазона
    30…3000 мм игнорируются (защита от случайных чисел в наименовании).
    """
    m = _THICKNESS_RX.search(text or "") or _T_MM_RX.search(text or "")
    if not m:
        return None
    value = int(m.group(1))
    return value if 30 <= value <= 3000 else None


def detect_geometry(text: Optional[str]) -> Optional[str]:
    """Определяет вид геометрического параметра заголовка группы элементов.

    ``thickness`` (толщина), ``section`` (сечение а×b), ``area`` (площадь).
    """
    s = (text or "").lower()
    if not s:
        return None
    for param, pattern in GEOMETRY_RULES:
        if re.search(pattern, s):
            return param
    return None


# =====================================================================
#  Подтип лестничной конструкции (марш / площадка)
# =====================================================================

def stair_component(text: Optional[str]) -> Optional[str]:
    """Определяет подтип лестничной конструкции по тексту.

    Возвращает ``'flight'`` (лестничный марш), ``'landing'`` (лестничная
    площадка) или ``None``, если подтип не указан. Используется для разделения
    работ/ресурсов лестниц типового ВОР и строк ВОР заказчика: у заказчика
    объёмы опалубки и бетона маршей и площадок идут отдельными строками, а в
    типовом ВОР — в разных работах одного подраздела.
    """
    s = (text or "").lower()
    if not s:
        return None
    if re.search(r"площад[ко]", s):
        return "landing"
    if "марш" in s or re.search(r"\brun\b", s):
        return "flight"
    return None


# =====================================================================
#  Классификация строк-работ (целочисленные номера)
# =====================================================================

# Вид объёма работы: определяет, какая количественная характеристика группы
# элементов подставляется в столбец «Объем».
def classify_work(name: Optional[str]) -> str:
    """Определяет вид объёма для строки-работы типового ВОР.

    Возвращаемые значения:

    * ``formwork``   — площадь опалубки (м2);
    * ``rebar``      — тоннаж арматуры (берётся из ВОР застройщика);
    * ``embedded``   — закладные детали (т);
    * ``concrete``   — объём бетона (м3);
    * ``thermoliner``— объём термовкладышей (м3);
    * ``volume``     — прочий объём конструкций (м3);
    * ``hydro_area`` — площадь гидроизоляции (м2);
    * ``backfill``   — объём засыпки (м3);
    * ``count``      — штучный объём (шт.);
    * ``other``      — прочее (нет данных из модели).
    """
    s = (name or "").lower()
    if not s:
        return "other"
    if "опалубк" in s:
        return "formwork"
    if "арматурн" in s and ("стержн" in s or "каркас" in s):
        return "rebar"
    if "закладн" in s:
        return "embedded"
    # «Устройство холодного шва бетонирования» — не бетонирование: объём работы
    # это длина шва (п.м.), а не объём бетона (иначе строка получала бы объём
    # бетонной подготовки, см. customer_vor). Учитываются все словоформы
    # («шов», «шва», «швы», «швов»), кроме «швеллера».
    if "швеллер" not in s and re.search(r"\bш(?:ов|в[аеыуои])", s):
        return "other"
    if "бетонирован" in s:
        return "concrete"
    if "термовкладыш" in s:
        return "thermoliner"
    if "ж/б балок" in s or "ж/б конструкц" in s:
        return "volume"
    if "гидроизоляц" in s or "теплоизоляц" in s:
        return "hydro_area"
    if "засыпк" in s:
        return "backfill"
    if any(w in s for w in ("испытани", "вырубк", "погружени", "бурени",
                            "контроль", "устройство буронабивных",
                            "устройство сборных", "монтаж блоков",
                            "установка сборных", "устройство монолитных")):
        return "count"
    return "other"


# =====================================================================
#  Классификация строк-ресурсов (дробные номера)
# =====================================================================

_GRADE_RE = re.compile(r"[ВB]\s*(\d+(?:[.,]\d+)?)")
_W_RE = re.compile(r"\bW\s*(\d+)", re.IGNORECASE)
_F_RE = re.compile(r"\bF\s*(\d+)", re.IGNORECASE)
_REBAR_DIAMETER_RE = re.compile(r"(?:d|D|Ø|Д)\s*=?\s*(\d+(?:[.,]\d+)?)")
_REBAR_CLASS_RE = re.compile(r"(А\d{3}[СсC]?|A\d{3}[СсC]?|Вр-?1|Bp-?1|В500[СсC]?|B500[СсC]?)")
_REBAR_WORD_RE = re.compile(r"арматур|арм-?ра|стержн|каркас", re.IGNORECASE)

# Слова-признаки «каркасов»/выпусков арматуры (в отличие от продольной арматуры).
_REBAR_FRAME_WORDS = ("выпуск", "каркас")


def parse_concrete_grade(text: Optional[str]) -> Optional[str]:
    """Извлекает марку бетона (``B30``) из наименования ресурса."""
    m = _GRADE_RE.search(text or "")
    if not m:
        return None
    return "B" + m.group(1).replace(",", ".")


def parse_concrete_spec(text: Optional[str]) -> Optional[str]:
    """Извлекает марку бетона с показателями: «В30, W8, F150».

    Используется для подписи строк-ресурсов бетона по фактическим данным
    (ВОР заказчика, модель IFC). Возвращает ``None``, если марка не найдена.
    """
    s = text or ""
    m = _GRADE_RE.search(s)
    if not m:
        return None
    parts = ["В" + m.group(1)]
    w = _W_RE.search(s)
    if w:
        parts.append("W" + w.group(1))
    f = _F_RE.search(s)
    if f:
        parts.append("F" + f.group(1))
    return ", ".join(parts)


def concrete_label(spec: Optional[str]) -> str:
    """Наименование строки-ресурса бетона («Бетон В30, W8, F150»)."""
    return f"Бетон {spec}" if spec else "Бетон"


def parse_rebar_spec(text: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """Извлекает диаметр (мм) и класс арматуры из наименования ресурса."""
    s = text or ""
    dm = _REBAR_DIAMETER_RE.search(s)
    diameter = int(float(dm.group(1).replace(",", "."))) if dm else None
    cm = _REBAR_CLASS_RE.search(s)
    cls = None
    if cm:
        cls = (cm.group(1).upper()
               .replace("A", "А").replace("B", "В").replace("C", "С").replace("P", "Р"))
    return diameter, cls


def is_rebar_text(text: Optional[str]) -> bool:
    """Является ли наименование арматурой/стержнями/каркасом."""
    return bool(_REBAR_WORD_RE.search(text or ""))


def rebar_bucket(name: Optional[str]) -> str:
    """Категория арматуры по наименованию строки.

    ``frames`` — арматурные выпуски/каркасы («Арматурные выпуски класса А500С»),
    ``bars`` — продольная и поперечная арматура («Арматура класса А500С»).
    """
    low = (name or "").lower()
    return "frames" if any(w in low for w in _REBAR_FRAME_WORDS) else "bars"


# Соответствие ресурса источнику: 'concrete' (марка бетона),
# 'rebar' (арматура), 'other' (прочие материалы).
def classify_resource(name: Optional[str]) -> str:
    """Определяет категорию ресурса (дробной строки) типового ВОР."""
    s = (name or "").strip()
    low = s.lower()
    if not low:
        return "other"
    if low.startswith("бетон") or "бетон " in low or "бетон," in low:
        return "concrete"
    if is_rebar_text(s):
        return "rebar"
    return "other"


# =====================================================================
#  Сопоставление разделов ВОР застройщика с подразделами типового ВОР
# =====================================================================

def vor_section_matches(section: Optional[str], kind: Optional[str],
                        part: Optional[str]) -> bool:
    """Проверяет, относится ли раздел ВОР застройщика к подразделу типового ВОР.

    Используется для переноса объёмов арматуры из ВОР застройщика в
    соответствующий подраздел типового ВОР (арматура берётся исключительно
    из ВОР застройщика).
    """
    s = (section or "").lower()
    if not s:
        return False

    def has(*words: str) -> bool:
        return any(w in s for w in words)

    underground = has("подзем", "подвал")
    above = has("этаж", "типов", "техническ", "кровел", "надзем")

    if kind == "pile":
        return has("сва")
    if kind == "equipment_foundation":
        return has("оборудован") or has("маф")
    if kind == "foundation_slab":
        # Фундаментные плиты: включая плиты фундаментов башенных кранов
        # (у заказчика объёмы плит толщиной 1400 мм приведены в этом разделе).
        return has("фундамент") and not has("сва", "маф")
    if kind == "wall":
        if part == "underground":
            return has("стен") and underground
        if part == "above":
            return has("стен") and above and not underground
        return has("стен")
    if kind == "slab":
        if part == "underground":
            return has("плит") and has("перекрыти", "покрыти") and underground
        if part == "above":
            return has("плит") and has("перекрыти", "покрыти") and not underground
        return has("плит") and has("перекрыти", "покрыти")
    if kind == "beam":
        if part == "underground":
            return has("балк") and underground
        if part == "above":
            return has("балк") and above and not underground
        return has("балк")
    if kind == "stair":
        if part == "underground":
            return has("лестниц") and underground
        if part == "above":
            return has("лестниц") and not underground
        return has("лестниц")
    if kind == "parapet":
        return has("парапет")
    if kind == "insulation":
        return has("изоляц")
    if kind == "backfill":
        return has("засып")
    if kind == "seam":
        return has("деформац")
    return False
