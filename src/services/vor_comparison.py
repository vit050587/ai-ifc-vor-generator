"""Сравнение работ и объёмов ВОР заказчика и сформированного перечня работ (режим ИИ).

Модуль сопоставляет две таблицы:

* **ВОР** — ведомость объёмов работ заказчика. Источник — загруженный PDF ВОР
  (``vor_*.pdf`` в корне сессии), который разбирается **детерминированно**
  (``doc_pdf_parser.extract_vor_rows``): читается структура ведомости — разделы
  («6. Монолитные ж/б конструкции. Стены подземной части»), блоки/захватки
  («Деформационный блок №1. Секции 1 и 2», «Секция №N») и строки позиций с
  колонками «было / стало / разница» (для сравнения берётся **«стало»** — объём
  с учётом корректировки ПД). Если PDF нет — используется результат LLM-разбора
  ``ВОР_ведомость_объемов_работ.json`` (``doc_pdf_parser.parse_vor``);
* **перечень** — сформированный сервисом ``Финальный_перечень_работ.json``
  (``run_<NNN>/``): группы элементов, расценки (работы), единицы измерения и
  объёмы.

Задача — ответить на вопросы: какие работы и в каком объёме заявлены в ВОР и
какие попали в сформированный перечень, где объёмы расходятся и какие работы
присутствуют только в одном из документов. Сравнение ведётся **по каждой группе
элементов и по каждой работе**.

Группа элементов — канонический **вид конструкции** (``element_group``): Стены,
Плиты перекрытия, Лестничные марши, Лестничные площадки, Балки, Фундаментная
плита, Приямок, Парапеты, Термовкладыши и т. д. Для перечня вид берётся из
МССК-группы (``path[1]``) или из наименования элемента; для ВОР — из
наименования строки, а если вид в наименовании не указан (арматура, опалубка) —
**наследуется от ближайшей выше строки-конструкции** (в пределах раздела). Это
позволяет сопоставлять лестничные марши, площадки, балки и стены, которые в ВОР
и в перечне названы по-разному.

Разбивка ВОР по **секциям и этажам** (разделы/блоки ведомости) приводится к
уровню **части здания** (``_vor_part``: Подземная / Цоколь / Надземная) — так же,
как группируется сформированный перечень. Сравнение строится в двух разрезах:
по ключу «часть здания · вид конструкции → работа → единица» (лист «Сравнение по
группам») и по ключу «вид конструкции → работа → единица» без части здания (лист
«По видам конструкций»).

Проблемы сопоставления и их решение:

* **единицы измерения** — в ВОР объёмы в натуральных единицах (``м3``, ``м2``,
  ``т``, ``м``, ``шт``), а в перечне — в единицах расценки с множителем
  («100 м2», «100 м3», «1 Т», «10 фасонных частей»). Модуль приводит обе стороны
  к базовым единицам (``м3``/``м2``/``м``/``т``/``шт``), умножая объём перечня на
  множитель единицы расценки (напр. «100 м2» → объём × 100);
* **различия группировки и наименований** — работы классифицируются по категории
  (опалубка, армирование, бетонирование, уход за бетоном, гидроизоляция и т.д.)
  по ключевым словам, элементы — по каноническому виду конструкции
  (``element_group``);
* **арматура не моделируется в IFC** — расход арматуры в модели отсутствует,
  поэтому арматура **берётся из ВОР** и **не сравнивается**: строка получает
  статус «взято из ВОР», Δ не считается (объём перечня приводится справочно);
* **двойной учёт** — работы (расценки) и ресурсы/материалы (``is_resource``)
  сравниваются **раздельно**: работы — на основных листах, ресурсы — на отдельном
  листе «Ресурсы (материалы)», чтобы объём бетона/арматуры не учитывался дважды;
* **двойной учёт подпозиций ВОР** — для агрегации берутся только **листовые**
  строки (позиции без вложенных подпозиций) — иначе объём удваивался бы
  (родительская строка «2 Арматура класса А500С» + подстроки «2.1 D10…D28»);
* **БК и МАФ** — фундаменты башенного крана (``БК``) и конструкции МАФ
  (детские/спортивные площадки и т.п.) не относятся к конструкциям здания и
  выделяются в отдельные группы («Прочее (БК)», «Прочее (МАФ)»).

Результат — ``Сравнение_ВОР_и_перечня.xlsx`` (листы «Сравнение по группам»,
«По видам конструкций», «Ресурсы (материалы)», «Детализация ВОР»,
«Детализация перечня» с подсветкой расхождений) и ``Сравнение_ВОР_и_перечня.json``
в папке запуска ``run_<NNN>/``.

Подсветка строк:

* **красный** — работа присутствует только в одном из документов
  (в ВОР или в перечне);
* **жёлтый** — объёмы присутствуют в обоих, но расхождение аномально большое
  (``|Δ%| ≥ DIFF_PCT`` при ``|Δ| ≥ DIFF_ABS``);
* **голубой** — арматура: объём взят из ВОР, сравнение не выполняется;
* без заливки — объёмы сопоставлены и расхождение в пределах допуска.

CLI: ``python -m src.services.vor_comparison <каталог_сессии> [каталог_запуска]``.
"""

import argparse
import glob
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from src.core.logger import setup_logger

logger = setup_logger(__name__)

# ---------------------------------------------------------------------
#  Имена файлов (артефакты режима ИИ)
# ---------------------------------------------------------------------
VOR_JSON = "ВОР_ведомость_объемов_работ.json"
FINAL_WORKS_JSON = "Финальный_перечень_работ.json"
COMPARE_XLSX = "Сравнение_ВОР_и_перечня.xlsx"
COMPARE_JSON = "Сравнение_ВОР_и_перечня.json"
# Маска загруженного PDF ВОР (session_manager сохраняет как vor_<имя>.pdf).
VOR_PDF_PATTERNS = ("vor_*.pdf", "*ВОР*.pdf", "*вор*.pdf")

# Пороги «аномального» расхождения объёмов.
DIFF_PCT = 20.0      # относительное расхождение, %
DIFF_ABS = 1.0       # минимальное абсолютное расхождение (в базовой единице)

# ---------------------------------------------------------------------
#  Единицы измерения: базовая единица + множитель единицы расценки
# ---------------------------------------------------------------------
# Порядок важен: м3/м2 проверяются раньше «м», «т» — раньше общего «м».
_UNIT_BASE = (
    (re.compile(r"м3|м³|куб\.?\s*м|кубическ"), "м3"),
    (re.compile(r"м2|м²|кв\.?\s*м|квадратн"), "м2"),
    (re.compile(r"пог\.?\s*м|п\.?\s*м|м\.?\s*п\b"), "м"),
    (re.compile(r"^[\d\s.,]*м\.?$"), "м"),
    (re.compile(r"тонн|^[\d\s.,]*т\.?$|^т$"), "т"),
    (re.compile(r"шт|фасонн|частей|компл"), "шт"),
)
_UNIT_MULT_RX = re.compile(r"^\s*(\d+(?:[.,]\d+)?)")


def base_unit(unit: Any) -> Tuple[Optional[str], float]:
    """Разбирает единицу измерения: ``(базовая_единица, множитель)``.

    «100 м2» → ``("м2", 100.0)``; «1 Т» → ``("т", 1.0)``;
    «10 фасонных частей» → ``("шт", 10.0)``; «куб.м» → ``("м3", 1.0)``;
    неизвестная единица → ``(None, 1.0)``.
    """
    s = str(unit or "").replace("\u00a0", " ").strip().lower()
    if not s:
        return None, 1.0
    code: Optional[str] = None
    for rx, c in _UNIT_BASE:
        if rx.search(s):
            code = c
            break
    if code is None:
        return None, 1.0
    mult = 1.0
    m = _UNIT_MULT_RX.match(s)
    if m:
        try:
            mult = float(m.group(1).replace(",", "."))
        except ValueError:
            mult = 1.0
    if mult <= 0:
        mult = 1.0
    return code, mult


# ---------------------------------------------------------------------
#  Классификация работ по категориям (по ключевым словам)
# ---------------------------------------------------------------------
# (код, отображаемое имя, regex). Первое совпадение задаёт категорию.
WORK_CATEGORIES: Tuple[Tuple[str, str, str], ...] = (
    ("curing", "Уход за бетоном",
     r"уход за бетоном|тепловлагозащит|тепловлажн|выдерживан"),
    ("rebar", "Армирование",
     r"арматур|армир|стержн|сетк|каркас|^d\s?\d+$"),
    ("opening", "Опалубка", r"опалуб"),
    ("hydrobar", "Гидрошпонка / герметизация швов",
     r"гидрошпонк|аквастоп|набухающ|заполнение.{0,30}швов|герметик|вилатерм"),
    ("concrete", "Бетонирование (бетон)",
     r"бетонирован|бетонн\w*\s*смес|смесь бетонн|бетон"),
    ("waterproofing", "Гидроизоляция / праймер / мембрана",
     r"гидроизол|праймер|мастик|мембран|геотекстил|техноэласт|плёнк|пленк"),
    ("embeds", "Закладные детали", r"закладн"),
    ("injector", "Штуцер инъекционный", r"штуцер|инъекц|фасонн"),
    ("backfill", "Обратная засыпка / песок", r"засыпк|песок|подушк"),
    ("soil", "Разработка грунта",
     r"разработк\w*\s*грунт|выемка грунт|котлован|грунт"),
    ("steel", "Металлоконструкции / шпунт",
     r"шпунт|труба\s*\d|двутавр|швеллер|металл"),
    ("piling", "Сваи / бурение", r"сва[ийя]|бурени"),
    ("prep", "Бетонная подготовка / основание", r"подготовк|щебеночн"),
    ("screed", "Стяжка", r"стяжк"),
    ("fillet", "Галтель", r"галтел"),
    ("insulation", "Утепление",
     r"утепл|пенополистирол|минват|термовклад|ппс\b|xpc|термовкладыш"),
    ("masonry", "Кладка", r"кладк|кирпич|газобетон"),
    ("stairs", "Лестницы", r"лестничн|марш|косоур|стремянк|ступен"),
    ("railing", "Ограждения / перила", r"огражд|перил"),
    ("paint", "Окраска", r"покраск|окраск|грунтовк"),
)


def classify_work(text: Any) -> str:
    """Код категории работы по тексту (наименование работы/расценки).

    Неопознанные работы относятся к категории ``other`` («Прочие работы»).
    """
    t = str(text or "").lower()
    for code, _name, rx in WORK_CATEGORIES:
        if re.search(rx, t):
            return code
    return "other"


CATEGORY_NAMES: Dict[str, str] = {code: name for code, name, _ in WORK_CATEGORIES}
CATEGORY_NAMES["other"] = "Прочие работы"

# Категории работ, которые в ВОР применяются к вышестоящей конструкции и в
# наименовании её не называют (арматура, опалубка, холодный шов и т.п.). Только
# для них группа элементов наследуется от ближайшей строки-конструкции; у
# самостоятельных работ (гидроизоляция, засыпка, грунт, утепление) вид берётся
# из раздела ведомости, чтобы не приписать их случайной конструкции.
APPLIED_CATEGORIES = frozenset({
    "concrete", "opening", "rebar", "hydrobar", "curing", "fillet",
    "embeds", "injector",
})


# ---------------------------------------------------------------------
#  Канонические группы элементов (виды конструкций)
# ---------------------------------------------------------------------
# Порядок важен: первое совпадение задаёт группу. Правила согласованы с
# МССК-группами перечня (``path[1]``: «Стена», «Плита перекрытия»,
# «Лестничный марш», «Лестничная площадка», «Балка», «Фундаментная плита»,
# «Термовкладыш», «Лестница», «Приямок») и разделами ВОР.
GROUP_OTHER = "Прочее"
ELEMENT_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("Прочее (МАФ)", r"маф|игров|качел|тренаж|благоустр|велопарков|детск|спортивн"),
    ("Прочее (БК)", r"\bбк\b|башенн"),
    ("Металлоконструкции", r"труба|двутавр|швеллер|шпунт|металл|стремянк"),
    ("Приямок", r"приямок"),
    ("Фундаментная плита", r"фундаментн\w*\s+плит|плит\w*\s+фундамент|базисн"),
    ("Лестничные площадки", r"лестничн\w*\s*площад|промежуточн\w*\s*площад"),
    ("Лестничные марши", r"лестничн\w*\s*марш|\bмарш"),
    ("Лестницы", r"лестниц|косоур|ступен"),
    ("Балки", r"\bбалк|\bригел|обвязочн"),
    ("Колонны", r"колонн|пилон"),
    ("Термовкладыши / утепление",
     r"термовклад|утепл|теплоизол|пенополист|минват|эппс|\bппс\b|утеплит"),
    ("Парапеты", r"парапет"),
    ("Стены", r"\bстен|\bстена"),
    ("Плиты перекрытия / покрытия", r"перекрыт|покрыт|\bплит"),
    ("Сваи", r"сва[ийя]|бурени|буронабивн"),
    ("Фундаменты", r"фундамент|ростверк|подошв"),
)


def element_group(text: Any) -> str:
    """Каноническая группа элементов (вид конструкции) по тексту.

    Перед определением вида гасится ложное срабатывание «балкон» → «балка»
    (подстрока «балк»). Неопознанные тексты → ``GROUP_OTHER`` («Прочее»).
    """
    t = str(text or "").lower().replace("балкон", " ")
    for label, rx in ELEMENT_GROUPS:
        if re.search(rx, t):
            return label
    return GROUP_OTHER


# ---------------------------------------------------------------------
#  Толщина и класс бетона (справочно — для детализации)
# ---------------------------------------------------------------------
_THICKNESS_RX = re.compile(r"толщин\w*\s*(\d{2,4})")
_THICKNESS_MM_RX = re.compile(r"(\d{2,4})\s*мм")
_CONCRETE_CLASS_RX = re.compile(r"[вb]\s?-?\s?(\d{1,2}(?:[.,]\d)?)")
# Слова конструкций: «N мм» без слова «толщиной» принимается за толщину только
# для них (в наименованиях материалов «пленка 400мм», «пенополистирол 100мм»
# число — это размер материала, а не толщина конструкции).
_STRUCTURAL_RX = re.compile(
    r"плит|стен|колонн|пилон|балк|ригел|лестнич|марш|фундамент|парапет|"
    r"покрыт|перекрыт|обвязочн|ростверк|сва")


def _thickness(text: Any) -> str:
    """Толщина конструкции (мм) из текста («толщиной 1000мм» → «1000»).

    Явное «толщиной N мм» распознаётся всегда; «N мм» без слова «толщиной» —
    только для наименований конструкций (см. ``_STRUCTURAL_RX``), чтобы размеры
    материалов (мембран, утеплителя, плёнок) не создавали ложных групп.
    """
    t = str(text or "").lower()
    m = _THICKNESS_RX.search(t)
    if m:
        return m.group(1)
    if _STRUCTURAL_RX.search(t):
        m = _THICKNESS_MM_RX.search(t)
        if m:
            return m.group(1)
    return ""


def _concrete_class(text: Any) -> str:
    """Класс бетона по прочности (число) из текста («бетон В30» → «30»)."""
    m = _CONCRETE_CLASS_RX.search(str(text or "").lower())
    return m.group(1).replace(",", ".") if m else ""


# ---------------------------------------------------------------------
#  Часть здания
# ---------------------------------------------------------------------
def _vor_part(section: Any, name: Any = "") -> str:
    """Часть здания для строки ВОР по разделу/наименованию.

    В ВОР объёмы разбиты по секциям и этажам, а сформированный перечень
    сгруппирован по части здания; приведение к части здания (Подземная /
    Цоколь / Надземная) позволяет сопоставить обе таблицы.
    """
    t = f"{section or ''} {name or ''}".lower()
    if "подзем" in t or "подвал" in t:
        return "Подземная"
    if "цокол" in t:
        return "Цоколь"
    if any(k in t for k in ("надзем", "этаж", "парапет", "кровл", "покрыт")):
        return "Надземная"
    if "фундамент" in t:
        return "Подземная"
    return ""


# ---------------------------------------------------------------------
#  Загрузка исходных данных
# ---------------------------------------------------------------------
def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _find_session_dir(run_dir: str, filename: str) -> Optional[str]:
    """Ищет файл в каталоге запуска и до 3 уровней выше (run_<NNN>/ → корень сессии)."""
    p = os.path.abspath(run_dir)
    for _ in range(4):
        fp = os.path.join(p, filename)
        if os.path.isfile(fp):
            return fp
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def _find_vor_pdf(run_dir: str) -> Optional[str]:
    """Ищет загруженный PDF ВОР в каталоге запуска/сессии (и в original/)."""
    p = os.path.abspath(run_dir)
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


def _num(value: Any) -> Optional[float]:
    """Число из значения (учёт «12,5», пробелов)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _load_vor_items(run_dir: str) -> List[dict]:
    """Записи ВОР из ``ВОР_ведомость_объемов_работ.json`` (корень сессии)."""
    path = _find_session_dir(run_dir, VOR_JSON)
    if not path:
        return []
    return [it for it in (_load_json(path).get("items") or []) if isinstance(it, dict)]


def _rows_from_items(items: List[dict]) -> List[dict]:
    """Приводит LLM-записи ВОР к форме строк ``extract_vor_rows`` (без разделов)."""
    rows: List[dict] = []
    for it in items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        rows.append({
            "page": it.get("page"), "section": None, "block": None, "pos": "",
            "name": name, "unit": it.get("unit"),
            "volume_was": None, "volume_now": _num(it.get("volume")),
        })
    return rows


def _load_vor_rows(run_dir: str) -> Tuple[List[dict], str]:
    """Строки ВОР: детерминированный разбор PDF ВОР, иначе LLM-JSON (fallback).

    Возвращает ``(строки, источник)``, где источник — ``"pdf"`` или ``"json"``.
    """
    pdf_path = _find_vor_pdf(run_dir)
    if pdf_path:
        try:
            from src.services import doc_pdf_parser
            rows = doc_pdf_parser.extract_vor_rows(pdf_path)
            if rows:
                return rows, "pdf"
        except Exception as exc:
            logger.warning(f"vor_comparison: не удалось разобрать PDF ВОР ({exc})")
    return _rows_from_items(_load_vor_items(run_dir)), "json"


def _load_model_groups(run_dir: str) -> List[dict]:
    """Группы элементов из ``Финальный_перечень_работ.json`` (папка запуска)."""
    path = os.path.join(os.path.abspath(run_dir), FINAL_WORKS_JSON)
    if not os.path.isfile(path):
        path = _find_session_dir(run_dir, FINAL_WORKS_JSON) or ""
    if not path or not os.path.isfile(path):
        return []
    return [g for g in (_load_json(path).get("groups") or []) if isinstance(g, dict)]


# ---------------------------------------------------------------------
#  Сбор нормализованных записей ВОР
# ---------------------------------------------------------------------
def _leaf_rows(rows: List[dict]) -> List[dict]:
    """Оставляет только листовые позиции (без вложенных подпозиций).

    В ВОР родительская позиция («2 Арматура класса А500С») часто равна сумме
    подпозиций («2.1 D10» … «2.4 D28»); суммирование всех строк удвоило бы
    объём. Поэтому из каждой группы (раздел + блок) берутся строки, для позиции
    которых нет дочерних позиций (``pos + "."``).
    """
    by_block: Dict[Tuple[Optional[str], Optional[str]], List[dict]] = defaultdict(list)
    for r in rows:
        by_block[(r.get("section"), r.get("block"))].append(r)
    out: List[dict] = []
    for rs in by_block.values():
        positions = {r["pos"] for r in rs if r.get("pos")}
        for r in rs:
            pos = r.get("pos")
            if pos and any(o != pos and o.startswith(pos + ".") for o in positions):
                continue
            out.append(r)
    return out


def _row_category(name: Any, unit: Optional[str]) -> str:
    """Категория работы по наименованию строки ВОР (с отнесением «м3 без слов» к бетону)."""
    category = classify_work(name)
    if category == "other" and unit == "м3":
        category = "concrete"
    return category


_TOP_POS_RX = re.compile(r"^\d+$")


def _assign_groups(rows: List[dict]) -> Dict[int, str]:
    """Группа элементов для каждой строки ВОР (по ``id`` записи).

    Строка-конструкция — верхнеуровневая позиция (``pos`` вида «1», «5»), в
    наименовании которой распознан вид конструкции. Работы, применяемые к
    конструкции (``APPLIED_CATEGORIES`` — арматура, опалубка, холодный шов и
    т.п.), наследуют группу **ближайшей выше** строки-конструкции в пределах
    блока/захватки; остальные работы и строки без конструкции берут вид из
    заголовка раздела. Это позволяет разложить ВОР, разбитый по секциям и
    этажам, по тем же группам элементов, что и сформированный перечень.
    """
    out: Dict[int, str] = {}
    current: Optional[str] = None
    last_key: Any = object()
    for r in rows:
        key = (r.get("section"), r.get("block"))
        if key != last_key:
            current = None
            last_key = key
        name = r.get("name")
        unit, _ = base_unit(r.get("unit"))
        g = element_group(name)
        if g != GROUP_OTHER:
            out[id(r)] = g
            if _TOP_POS_RX.match(str(r.get("pos") or "")):
                current = g
            continue
        category = _row_category(name, unit)
        if category in APPLIED_CATEGORIES and current:
            out[id(r)] = current
        else:
            out[id(r)] = element_group(r.get("section"))
            if category not in APPLIED_CATEGORIES:
                # цепочка «конструкция → работы» прервана самостоятельной работой
                current = None
    return out


def _vor_records(rows: List[dict]) -> List[Dict[str, Any]]:
    """Нормализует строки ВОР: группа, часть здания, категория, единица, объём.

    Объём берётся из колонки «стало» (корректировка ПД), при её отсутствии — из
    «было». Единицы приводятся к базовым (``м3``/``м2``/``м``/``т``/``шт``).
    """
    ordered = [r for r in rows if str(r.get("name") or "").strip()]
    groups = _assign_groups(ordered)
    leaf_ids = {id(r) for r in _leaf_rows(ordered)}
    out: List[Dict[str, Any]] = []
    for r in ordered:
        if id(r) not in leaf_ids:
            continue
        name = str(r.get("name") or "").strip()
        unit, mult = base_unit(r.get("unit"))
        volume = r.get("volume_now")
        if volume is None:
            volume = r.get("volume_was")
        category = _row_category(name, unit)
        group = groups.get(id(r)) or GROUP_OTHER
        part = _vor_part(r.get("section"), name)
        label = f"{part} · {group}" if part else group
        out.append({
            "source": "ВОР",
            "name": name,
            "section": r.get("section") or "",
            "block": r.get("block") or "",
            "pos": r.get("pos") or "",
            "page": r.get("page"),
            "part": part,
            "group": group,
            "label": label,
            "thickness": _thickness(name),
            "bclass": _concrete_class(name),
            "category": category,
            "unit": unit,
            "raw_unit": str(r.get("unit") or "").strip(),
            "volume": (volume * mult) if volume is not None else None,
            "is_resource": False,
        })
    return out


def _model_records(groups: List[dict]) -> List[Dict[str, Any]]:
    """Нормализует работы и ресурсы перечня: группа, часть здания, категория, объём.

    Группа элементов определяется по МССК-группе (``path[1]``), а при её
    отсутствии — по наименованию элемента. Ресурсы/материалы (``is_resource``)
    помечаются флагом и сравниваются на отдельном листе.
    """
    out: List[Dict[str, Any]] = []
    for g in groups:
        element = str(g.get("element") or "").strip()
        part = str(g.get("part") or "").strip()
        path = g.get("path") or []
        path_name = str(path[1]) if len(path) > 1 else ""
        group = element_group(path_name)
        if group == GROUP_OTHER:
            group = element_group(element)
        label = f"{part} · {group}" if part else group
        for r in g.get("rows") or []:
            if not isinstance(r, dict):
                continue
            unit, mult = base_unit(r.get("unit"))
            vol = _num(r.get("volume"))
            work = str(r.get("work") or "")
            title = str(r.get("title") or "")
            # Категория — по полю «work» (категория работы из подбора); наименование
            # расценки — только fallback (в нём бывает «в опалубку» и т.п.).
            category = classify_work(work)
            if category == "other":
                category = classify_work(title)
            out.append({
                "source": "Перечень",
                "group": group,
                "label": label,
                "part": part,
                "element": element,
                "thickness": _thickness(element),
                "bclass": _concrete_class(element),
                "pressmark": str(r.get("pressmark") or ""),
                "work": work,
                "title": title,
                "category": category,
                "unit": unit,
                "raw_unit": str(r.get("unit") or "").strip(),
                "volume": vol * mult if vol is not None else None,
                "is_resource": bool(r.get("is_resource")),
            })
    return out


# ---------------------------------------------------------------------
#  Сопоставление и формирование строк сравнения
# ---------------------------------------------------------------------
def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _status(vor_vol: Optional[float], model_vol: Optional[float],
            has_vor: bool, has_model: bool, category: str = ""
            ) -> Tuple[str, Optional[float], Optional[float]]:
    """Возвращает ``(статус, дельта, дельта_%)`` для ключа сравнения.

    Арматура не моделируется в IFC и берётся из ВОР — для категории ``rebar``
    объёмы не сравниваются, статус «взято из ВОР» (Δ не считается).
    """
    if category == "rebar":
        if has_vor:
            return "взято из ВОР", None, None
        if has_model:
            return "нет в ВОР (оценка модели)", None, None
        return "нет данных", None, None
    if has_vor and not has_model:
        return "только в ВОР", None, None
    if has_model and not has_vor:
        return "только в перечне", None, None
    if vor_vol is None and model_vol is None:
        return "нет объёма (по проекту)", None, None
    if vor_vol is None or vor_vol == 0:
        return "нет объёма в ВОР", None, None
    if model_vol is None:
        return "нет объёма в перечне", None, None
    diff = model_vol - vor_vol
    pct = diff / vor_vol * 100.0
    if abs(pct) >= DIFF_PCT and abs(diff) >= DIFF_ABS:
        return "расхождение", diff, pct
    return "совпадает", diff, pct


def _sum_vol(items: List[Dict[str, Any]]) -> Optional[float]:
    """Сумма объёмов записей (``None``, если ни у одной нет объёма)."""
    values = [x["volume"] for x in items if x.get("volume") is not None]
    return round(sum(values), 4) if values else None


def _join_names(items: List[Dict[str, Any]], key: str, limit: int = 8) -> str:
    """Список уникальных наименований (с номерами страниц для ВОР)."""
    seen: List[str] = []
    for it in items:
        label = str(it.get(key) or "").strip()
        if not label:
            continue
        if key == "name" and it.get("page"):
            label = f"{label} (с.{it['page']})"
        if label not in seen:
            seen.append(label)
    if len(seen) > limit:
        seen = seen[:limit] + [f"… ещё {len(seen) - limit}"]
    return "; ".join(seen)


_ORDER = {"расхождение": 0, "только в ВОР": 1, "только в перечне": 2,
          "нет в ВОР (оценка модели)": 3, "взято из ВОР": 4,
          "нет объёма в ВОР": 5, "нет объёма в перечне": 6,
          "нет объёма (по проекту)": 7, "нет данных": 8, "совпадает": 9}


def _compare_level(records: List[Dict[str, Any]], key_fields: Tuple[str, ...]
                   ) -> List[Dict[str, Any]]:
    """Сравнивает объёмы ВОР и перечня по ключу из ``key_fields``.

    ``records`` — общий список нормализованных записей (ВОР + перечень). Ключ
    собирается из полей ``key_fields`` (напр. ``("label", "category", "unit")``).
    """
    vor_agg: Dict[Tuple[str, ...], List[Dict[str, Any]]] = defaultdict(list)
    model_agg: Dict[Tuple[str, ...], List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        key = tuple(str(r.get(f) or "") for f in key_fields)
        (vor_agg if r["source"] == "ВОР" else model_agg)[key].append(r)

    rows: List[Dict[str, Any]] = []
    for key in set(vor_agg) | set(model_agg):
        v_items = vor_agg.get(key, [])
        m_items = model_agg.get(key, [])
        first = (v_items or m_items)[0]
        vor_vol = _sum_vol(v_items)
        model_vol = _sum_vol(m_items)
        status, diff, pct = _status(vor_vol, model_vol, bool(v_items), bool(m_items),
                                    first.get("category", ""))
        label = first.get("label") if "label" in key_fields else first.get("group")
        rows.append({
            "label": label,
            "group": first.get("group"),
            "part": first.get("part", ""),
            "category": first.get("category", ""),
            "category_name": CATEGORY_NAMES.get(first.get("category", ""),
                                                first.get("category", "")),
            "unit": first.get("unit") or "",
            "vor_volume": vor_vol,
            "model_volume": model_vol,
            "diff": round(diff, 4) if diff is not None else None,
            "diff_pct": round(pct, 2) if pct is not None else None,
            "status": status,
            "source": "ВОР" if status == "взято из ВОР" else "",
            "model_groups": _join_names(m_items, "group"),
            "vor_names": _join_names(v_items, "name"),
            "vor_pages": sorted({x["page"] for x in v_items if x.get("page")}),
        })
    rows.sort(key=lambda r: (_ORDER.get(r["status"], 99), str(r["label"]),
                             r["category_name"], r["unit"]))
    return rows


def compare(run_dir: str, vor_rows: Optional[List[dict]] = None,
            vor_source: Optional[str] = None) -> Dict[str, Any]:
    """Строит сравнение ВОР и перечня работ для каталога запуска ``run_<NNN>``.

    ``vor_rows``/``vor_source`` можно передать заранее (чтобы не разбирать PDF
    ВОР повторно). Возвращает структуру ``{meta, rows, kind_rows, resource_rows,
    details_vor, details_model, summary}``.
    """
    if vor_rows is None:
        vor_rows, vor_source = _load_vor_rows(run_dir)
    vor_source = vor_source or "json"
    groups = _load_model_groups(run_dir)

    vor = _vor_records(vor_rows)
    model = _model_records(groups)
    records = vor + model

    # Основное сравнение — работы (расценки); ресурсы/материалы перечня
    # сравниваются отдельно, чтобы объём бетона/арматуры не учитывался дважды.
    work_records = [r for r in records
                    if r["source"] == "ВОР" or not r.get("is_resource")]
    rows = _compare_level(work_records, ("label", "category", "unit"))
    kind_rows = _compare_level(work_records, ("group", "category", "unit"))
    resource_records = vor + [r for r in model if r.get("is_resource")]
    resource_rows = [r for r in _compare_level(resource_records, ("label", "category", "unit"))
                     if r["status"] != "только в ВОР"]

    details_vor = [{
        "page": r["page"], "section": r["section"], "block": r["block"],
        "pos": r["pos"], "name": r["name"], "part": r["part"], "group": r["group"],
        "category_name": CATEGORY_NAMES.get(r["category"], r["category"]),
        "unit": r["unit"] or r["raw_unit"], "volume": r["volume"],
    } for r in sorted(vor, key=lambda x: (str(x["section"]), str(x["block"]),
                                          str(x["pos"]), str(x["name"])))]
    details_model = [{
        "group": r["group"], "part": r["part"],
        "kind": "ресурс" if r["is_resource"] else "работа",
        "category_name": CATEGORY_NAMES.get(r["category"], r["category"]),
        "pressmark": r["pressmark"], "title": r["title"],
        "unit": r["unit"] or r["raw_unit"], "volume": r["volume"],
    } for r in sorted(model, key=lambda x: (str(x["label"]), str(x["pressmark"])))]

    def _count(rws: List[Dict[str, Any]], status: str) -> int:
        return sum(1 for r in rws if r["status"] == status)

    summary = {
        "rows": len(rows),
        "kind_rows": len(kind_rows),
        "resource_rows": len(resource_rows),
        "расхождение": _count(rows, "расхождение"),
        "только в ВОР": _count(rows, "только в ВОР"),
        "только в перечне": _count(rows, "только в перечне"),
        "совпадает": _count(rows, "совпадает"),
        "взято из ВОР": _count(rows, "взято из ВОР"),
        "vor_items": len(vor),
        "model_works": sum(1 for r in model if not r["is_resource"]),
        "model_resources": sum(1 for r in model if r["is_resource"]),
        "vor_items_with_volume": sum(1 for r in vor if r["volume"]),
        "model_works_with_volume": sum(1 for r in model if r["volume"]),
    }
    return {
        "meta": {
            "diff_pct": DIFF_PCT,
            "diff_abs": DIFF_ABS,
            "vor_source": vor_source,
            "vor_file": os.path.basename(_find_vor_pdf(run_dir) or "")
            or os.path.basename(_find_session_dir(run_dir, VOR_JSON) or ""),
            "run_dir": os.path.abspath(run_dir),
        },
        "rows": rows,
        "kind_rows": kind_rows,
        "resource_rows": resource_rows,
        "details_vor": details_vor,
        "details_model": details_model,
        "summary": summary,
    }


# ---------------------------------------------------------------------
#  Запись результата
# ---------------------------------------------------------------------
_FILLS: Dict[str, Any] = {}


def _fills() -> Dict[str, Any]:
    """Ленивая инициализация заливок/шрифтов openpyxl."""
    if not _FILLS:
        from openpyxl.styles import Font, PatternFill
        _FILLS["header"] = PatternFill("solid", fgColor="D9E1F2")
        _FILLS["red"] = PatternFill("solid", fgColor="F8CBAD")
        _FILLS["yellow"] = PatternFill("solid", fgColor="FFE699")
        _FILLS["info"] = PatternFill("solid", fgColor="DDEBF7")
        _FILLS["bold"] = Font(bold=True)
    return _FILLS


def _status_fill(status: str):
    """Заливка строки по статусу сравнения (красный/жёлтый/голубой/без заливки)."""
    fills = _fills()
    if status in ("только в ВОР", "только в перечне"):
        return fills["red"]
    if status == "расхождение":
        return fills["yellow"]
    if status == "взято из ВОР":
        return fills["info"]
    return None


def _write_compare_sheet(ws, rows: List[Dict[str, Any]], first_header: str) -> None:
    """Пишет лист сравнения (по группам / по видам / по ресурсам) с подсветкой."""
    fills = _fills()
    headers = [first_header, "Работа (категория)", "Ед. изм.", "Объём ВОР",
               "Объём перечня", "Δ (перечень − ВОР)", "Δ, %", "Статус",
               "Источник объёма", "Позиции ВОР (стр.)", "Группы элементов (перечень)"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = fills["bold"]
        cell.fill = fills["header"]
    for r in rows:
        ws.append([
            r["label"], r["category_name"], r["unit"] or "",
            _fmt(r["vor_volume"]), _fmt(r["model_volume"]), _fmt(r["diff"]),
            "" if r["diff_pct"] is None else f"{r['diff_pct']:.1f}",
            r["status"], r.get("source", ""), r["vor_names"], r["model_groups"],
        ])
        fill = _status_fill(r["status"])
        if fill is not None:
            for cell in ws[ws.max_row]:
                cell.fill = fill
    widths = {"A": 40, "B": 30, "C": 10, "D": 14, "E": 14, "F": 16, "G": 8,
              "H": 24, "I": 14, "J": 50, "K": 40}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"


def write_xlsx(result: Dict[str, Any], path: str) -> str:
    """Пишет сравнение в XLSX с подсветкой расхождений."""
    from openpyxl import Workbook

    fills = _fills()
    wb = Workbook()

    # ---- Лист «Сравнение по группам» -------------------------------
    ws = wb.active
    ws.title = "Сравнение по группам"
    _write_compare_sheet(ws, result["rows"], "Группа элементов (часть здания · вид)")

    # ---- Лист «По видам конструкций» -------------------------------
    ws2 = wb.create_sheet("По видам конструкций")
    _write_compare_sheet(ws2, result["kind_rows"], "Вид конструкции")

    # ---- Лист «Ресурсы (материалы)» --------------------------------
    ws5 = wb.create_sheet("Ресурсы (материалы)")
    _write_compare_sheet(ws5, result.get("resource_rows", []),
                         "Группа элементов (часть здания · вид)")

    # ---- Лист «Детализация ВОР» ------------------------------------
    ws3 = wb.create_sheet("Детализация ВОР")
    headers3 = ["Стр.", "Раздел", "Блок / захватка", "Поз.", "Наименование (ВОР)",
                "Часть здания", "Группа элементов", "Работа (категория)",
                "Ед. изм.", "Объём"]
    ws3.append(headers3)
    for cell in ws3[1]:
        cell.font = fills["bold"]
        cell.fill = fills["header"]
    for d in result["details_vor"]:
        ws3.append([d["page"], d["section"], d["block"], d["pos"], d["name"],
                    d["part"], d["group"], d["category_name"], d["unit"] or "",
                    _fmt(d["volume"])])
    for col, width in {"A": 6, "B": 40, "C": 26, "D": 8, "E": 50, "F": 14,
                       "G": 26, "H": 30, "I": 10, "J": 14}.items():
        ws3.column_dimensions[col].width = width
    ws3.freeze_panes = "A2"

    # ---- Лист «Детализация перечня» --------------------------------
    ws4 = wb.create_sheet("Детализация перечня")
    headers4 = ["Группа элементов", "Часть здания", "Тип", "Работа (категория)",
                "Расценка", "Наименование расценки", "Ед. изм.", "Объём"]
    ws4.append(headers4)
    for cell in ws4[1]:
        cell.font = fills["bold"]
        cell.fill = fills["header"]
    for d in result["details_model"]:
        ws4.append([d["group"], d["part"], d["kind"], d["category_name"],
                    d["pressmark"], d["title"], d["unit"] or "", _fmt(d["volume"])])
    for col, width in {"A": 30, "B": 14, "C": 10, "D": 30, "E": 12, "F": 60,
                       "G": 10, "H": 14}.items():
        ws4.column_dimensions[col].width = width
    ws4.freeze_panes = "A2"

    wb.save(path)
    return path


def run(session_or_run_dir: str, run_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Строит и сохраняет сравнение ВОР ↔ перечень работ.

    ``session_or_run_dir`` — каталог сессии или запуска; ``run_dir`` — каталог
    запуска (если первый аргумент — сессия). Если ВОР или перечень не найдены,
    возвращает ``None`` (сравнение не создаётся).
    """
    target = os.path.abspath(run_dir or session_or_run_dir)
    vor_rows, source = _load_vor_rows(target)
    groups = _load_model_groups(target)
    if not vor_rows:
        logger.info("vor_comparison: ВОР не найден — сравнение не строится")
        return None
    if not groups:
        logger.info("vor_comparison: перечень работ не найден — сравнение не строится")
        return None

    result = compare(target, vor_rows, source)
    xlsx_path = os.path.join(target, COMPARE_XLSX)
    json_path = os.path.join(target, COMPARE_JSON)
    write_xlsx(result, xlsx_path)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    s = result["summary"]
    logger.info(
        f"vor_comparison: сравнение построено ({xlsx_path}, источник ВОР: "
        f"{result['meta']['vor_source']}): расхождений {s['расхождение']}, "
        f"только в ВОР {s['только в ВОР']}, только в перечне {s['только в перечне']}, "
        f"взято из ВОР {s['взято из ВОР']}"
    )
    return result


def latest_run_with_final_works(session_dir: str) -> Optional[str]:
    """Каталог последнего запуска ``run_<NNN>`` с ``Финальный_перечень_работ.json``."""
    base = os.path.abspath(session_dir)
    try:
        names = os.listdir(base)
    except OSError:
        return None
    best: Optional[Tuple[float, str]] = None
    for name in names:
        run_dir = os.path.join(base, name)
        if not os.path.isdir(run_dir):
            continue
        fp = os.path.join(run_dir, FINAL_WORKS_JSON)
        if os.path.isfile(fp):
            mtime = os.path.getmtime(fp)
            if best is None or mtime > best[0]:
                best = (mtime, run_dir)
    return best[1] if best else None


# ---------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Сравнение ВОР заказчика и сформированного перечня работ (режим ИИ)")
    parser.add_argument("session_dir", help="Каталог сессии (outputs/<session_id>)")
    parser.add_argument("run_dir", nargs="?", default=None,
                        help="Каталог запуска run_<NNN> (по умолчанию — последний)")
    args = parser.parse_args()

    target = args.run_dir or latest_run_with_final_works(args.session_dir)
    if not target:
        print("Не найден запуск с Финальный_перечень_работ.json")
        return
    result = run(target)
    if result is None:
        print("Сравнение не построено (нет ВОР или перечня работ)")
        return
    s = result["summary"]
    print(f"Сравнение: {os.path.join(target, COMPARE_XLSX)} "
          f"(источник ВОР: {result['meta']['vor_source']})")
    print(f"Строк сравнения: {s['rows']}; расхождений: {s['расхождение']}; "
          f"только в ВОР: {s['только в ВОР']}; только в перечне: {s['только в перечне']}; "
          f"взято из ВОР: {s['взято из ВОР']}")


if __name__ == "__main__":
    main()
