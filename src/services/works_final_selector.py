"""
Финальный подбор подходящих работ через LLM (режим АР).

Выполняется после works_table_selector (Подобранные_таблицы_работ.json) и
works_fetcher (Подобранные_работы.json):

  Элементы объединяются в группы по дереву группировки АР
  (filtered_elements_grouped_AR.json: МССК → Материал → Наименование).
  Для каждой группы LLM (та же, что разбирает файл ПОС —
  pd_parser.LLMClient, Ollama) выбирает из кандидатов работы, действительно
  нужные для выполнения работ по группе элементов, и объясняет выбор.
  Логика подбора:

    * LLM-запрос строится только по ПЕРВОМУ элементу группы (представителю):
      его таблицы работ становятся источником работ-кандидатов, его описание
      (тип, материал, технология, этаж) и все найденные параметры подбора
      (Параметры_подбора_элементов.json: геометрия, константы проекта и т.д.)
      — контекстом запроса;
    * объём работ считается по ВСЕЙ группе: значения величин (volume_m3 /
      area_m2 / length_m / joint_length_m / count) каждого элемента группы
      суммируются и приводятся к нормализованному виду по единице измерения
      расценки («100 м2» → площадь / 100, «м2» → как есть, «100 м3» → объём /
      100, «100 м» → длина / 100, «100 м шва» → длина шва / 100,
      «100 сборных конструкций» → количество / 100 и т.д.);
    * если в quantity элемента нет объёма/площади/длины — значения
      подтягиваются из полей самого элемента («Объём, м3», «Площадь, м2»,
      «Длина, мм») через `_enrich_quantity_from_element`;
    * исключение 1 — работы по монтажу/демонтажу опалубки фундаментных плит
      (ед. изм. «м2»): объём = периметр × толщина (по параметрам подбора
      каждого элемента группы), а не QTO-площадь плиты;
    * исключение 2 — работы по швам/герметизации/заделке (единица измерения
      содержит «шов»/«шв»/«гермет»/«заделк», например «100 м шва»):
      объём = длина шва = периметр элемента (сумма по группе);
    * исключение 3 — арматурные работы (ед. изм. «1 т»: установка арматурных
      изделий/каркасов/сеток/отдельных стержней/закладных деталей): объём =
      суммарный расход арматуры группы (кг) ÷ 1000, где расход = Σ по
      элементам группы (ReinforcementVolumeRatio, кг/м³ × объём элемента,
      м³ — из filtered_elements.xlsx), как в режиме КР. Расход подставляется
      ровно в одну расценку группы (приоритет — «отдельные стержни»),
      остальные арматурные расценки идут без объёма (иначе задваивается).

  Периметр для швов и опалубки берётся так:
    1) явный параметр «perimeter», если он найден в параметрах подбора;
    2) fallback: 2 × (Длина + Высота) — периметр панели стены;
    3) fallback: 2 × (Длина + Ширина) — если высоты нет (плиты, балки).

  Результаты:
    - run_<NNN>/Финальный_перечень_работ.json — структурированный перечень
      выбранных работ по каждой группе элементов;
    - run_<NNN>/Финальный_перечень_работ.xlsx — итоговая таблица в структуре
      режима КР (ОБЩИЙ_Финальный_перечень_работ.xlsx): колонки «Шифр ТСН /
      Наименование расценки/ресурса / Ед. изм. / Объём работ / ЗП / ЭМ / МР /
      Стоимость», строки-заголовки групп элементов, итоговая строка «ИТОГО:».
      Компоненты стоимости считаются по показателям детальных параметров
      позиции цифрового сборника (larix, catalog/work-process/detail —
      запрашиваются по id каждой выбранной работы за период ТСН запуска),
      умноженным на объём работ:
         ЗП = curSalary × объём; ЭМ = curOperationOfMachines × объём;
         МР = curCostOfMaterialResources × объём (fallback — базовые
         salary / operationOfMachines / costOfMaterialResources);
      «Стоимость» = ЗП + ЭМ + МР.

При ошибке LLM для группы элементов в перечень попадают все работы-кандидаты
группы (с пометкой в note) — шаг не должен обнулять результат запуска.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from src.core.config import load_config
from src.core.logger import setup_logger
from src.services.works_cost import add_total_row, format_money, safe_float

logger = setup_logger(__name__)

# Имена выходных файлов (в папке запуска run_<NNN>/)
FINAL_WORKS_JSON_FILENAME = "Финальный_перечень_работ.json"
FINAL_WORKS_XLSX_FILENAME = "Финальный_перечень_работ.xlsx"

# Дерево группировки АР (group_excel.process_ifc_excel_ar) в папке запуска:
# листовые группы (МССК → Материал → Наименование) с индексами элементов
GROUPED_JSON_FILENAME = "filtered_elements_grouped_AR.json"

# Отфильтрованные элементы запуска (этап 1) — источник расхода арматуры:
# колонки ReinforcementVolumeRatio (кг/м³) и «Объём, м3» по каждому элементу
FILTERED_XLSX_FILENAME = "filtered_elements.xlsx"

# Параметры подбора элементов (selection_template_builder, этап 0) — лежат в
# корне сессии (родителе папки запуска run_<NNN>/); сопоставление с элементами
# Подобранные_таблицы_работ.json — по global_id.
ELEMENT_PARAMS_FILENAME = "Параметры_подбора_элементов.json"

# Ограничение числа работ-кандидатов в одном запросе LLM (защита контекста)
_MAX_CANDIDATE_WORKS = 200

# Стемы для определения «шовных» работ по ЕДИНИЦЕ ИЗМЕРЕНИЯ.
#   «шов»  — им./тв. падеж (шов, швом);
#   «шв»   — все прочие формы (шва, шву, шве, швы, швов, швам, швами…);
#            ВАЖНО: «100 м шва» содержит «шв», но НЕ содержит «шов» —
#            одного стема «шов» недостаточно.
#   «гермет» / «заделк» — синонимичные формулировки единиц.
_JOINT_UNIT_STEMS = ("шов", "шв", "гермет", "заделк")

# Алиасы ключей параметров подбора (Параметры_подбора_элементов.json).
# В разных шаблонах встречаются русские/английские имена — перебираем все
# варианты и берём первый найденный.
_PARAM_ALIASES: Dict[str, tuple] = {
    "perimeter": ("perimeter", "Периметр", "Периметр, мм", "Периметр, м"),
    "thickness": (
        "thickness", "Толщина", "Толщина, мм", "Толщина, м",
        "Ширина", "Ширина, мм", "Ширина, м",
    ),
    "length":    ("length", "Длина", "Длина, мм", "Длина, м"),
    "width":     ("width", "Ширина", "Ширина, мм", "Ширина, м"),
    "height":    ("height", "Высота", "Высота, мм", "Высота, м"),
}

# Алиасы ключей величин в quantity (Подобранные_таблицы_работ.json).
# Некоторые экспорты кладут объём/площадь/длину под русскими именами —
# перебираем их при выборе значения.
_QUANTITY_KEYS: Dict[str, tuple] = {
    "volume_m3":      ("volume_m3", "Объём, м3", "Объем, м3", "QTO_bbox::Объём_м3"),
    "area_m2":        ("area_m2", "Площадь, м2", "QTO_bbox::Площадь_м2"),
    "length_m":       ("length_m", "Длина, м"),
    "joint_length_m": ("joint_length_m",),
    "count":          ("count", "Количество", "шт"),
}

# Ключевые слова арматурных расценок в наименовании (как в КР):
# «Установка арматурных изделий, каркасов и сеток», «Установка отдельных
# стержней», «Установка закладных деталей» и т. п.
_REBAR_KEYWORDS = ("арматур", "каркас", "стержн", "сетк", "закладн")

# Единица измерения «тонны» («1 т», «т») — с границами слова, чтобы не
# ловить «т» внутри слов («100 м3 бетона» не матчится)
_TON_UNIT_RE = re.compile(r"(?:^|\s)(?:\d+(?:[.,]\d+)?\s*)?т(?:\s|$)")
_PHYSICAL_UNIT_RE = re.compile(r"\b(?:м|м2|м3|мм|мм2|мм3|т|кг|л|г)\b")

SYSTEM_PROMPT = """Ты - инженер-сметчик ПТО. Твоя задача - из списка работ-кандидатов (норм цифрового сборника) выбрать только те, которые действительно нужны для выполнения работ по заданной группе элементов здания.

Правила:
1. Отвечай строго в формате JSON, без пояснений вне JSON.
2. Выбирай работы только из приведённого списка работ-кандидатов (поле "pressmark"). Ничего не выдумывай и не добавляй работы с другими шифрами.
3. Ориентируйся на тип элемента (стена, плита, окно, пол и т.д.), материал, технологию возведения (монолит/сборные), расположение в здании и объёмы работ. Ненужные для этой группы элементы работы отбрасывай.
4. Если приведён раздел «Параметры элемента» — используй его для выбора работ и оценки объёмов: геометрические параметры (толщина, периметр, площадь, объём, глубина) позволяют оценивать объёмы работ (например, площадь опалубки вертикальных граней плиты ≈ периметр × толщина, длина шва ≈ периметр), а технологические параметры и константы проекта (схема бетонирования, класс бетона, класс арматуры, тип крана, период ухода за бетоном и т.п.) — выбирать конкретные расценки. Линейные размеры в параметрах приведены в метрах; при расчётах следи за единицами измерения (м, м2, м3).
5. В поле "reason" кратко (одним предложением) объясни, почему работа нужна для этой группы элементов.
6. Если подходят все работы-кандидаты - верни их все.
7. Если ни одна работа не подходит - верни пустой список "selected".

Формат ответа:
{
  "selected": [
    {"pressmark": "шифр работы из списка", "reason": "краткое пояснение"}
  ]
}
"""


# ======================================================================
#  Утилиты определения «шовных» единиц измерения
# ======================================================================

def _unit_text(work: Dict[str, Any]) -> str:
    """Единица измерения работы в нижнем регистре.

    Принимает оба варианта ключа (unitOfMeasure из JSON сборника и
    unit_of_measure из наших внутренних записей).
    """
    return str(
        work.get("unitOfMeasure") or work.get("unit_of_measure") or ""
    ).strip().lower()


def _is_joint_unit(unit: str) -> bool:
    """True, если единица измерения относится к работам по швам.

    Проверяем ОБА стема — «шов» и «шв»: одного «шов» недостаточно, он не
    ловит форму «шва» («100 м шва»). «шв» безопасен, т.к. проверка идёт
    внутри ветки длины (единица уже содержит «м») и служит для форм
    родительного/множественного падежей.
    """
    u = str(unit or "").lower().replace("²", "2").replace("³", "3")
    return any(stem in u for stem in _JOINT_UNIT_STEMS)


def _is_joint_work(work: Dict[str, Any]) -> bool:
    """Работа по швам — определяется ТОЛЬКО по единице измерения.

    Название работы не анализируется. Единицы вида «100 м шва»,
    «м швов», «100 м герметизации», «100 м заделки» → True.
    """
    return _is_joint_unit(_unit_text(work))


def _load_element_params(run_dir: str) -> Dict[str, Dict[str, Any]]:
    """Карта «global_id → parameters» из Параметры_подбора_элементов.json.

    Файл строится на этапе 0 (selection_template_builder) и лежит в корне
    сессии (родителе папки запуска run_<NNN>/). Если файла нет или он
    повреждён — возвращается пустая карта (LLM-запрос строится без
    параметров, как раньше).
    """
    session_dir = os.path.dirname(os.path.abspath(run_dir))
    path = os.path.join(session_dir, ELEMENT_PARAMS_FILENAME)
    if not os.path.isfile(path):
        logger.warning(
            f"Файл параметров подбора не найден ({path}) — "
            "LLM-запрос строится без параметров элементов"
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}

    params_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in payload.get("elements", []) or []:
        if not isinstance(entry, dict):
            continue
        global_id = str(entry.get("global_id") or "").strip()
        if global_id and isinstance(entry.get("parameters"), dict):
            params_by_id[global_id] = entry["parameters"]
    logger.info(
        f"Параметры подбора элементов загружены: {len(params_by_id)} записей "
        f"({os.path.basename(path)})"
    )
    return params_by_id


def _format_element_params(params: Optional[Dict[str, Any]]) -> List[str]:
    """Строки раздела «Параметры элемента» для LLM-промпта.

    Включаются параметры с найденным значением (origin != not_found) в
    исходном порядке шаблона — с единицами измерения. Неопределённые
    параметры перечисляются одной строкой в конце (модель видит полный
    набор доступных параметров и понимает, чего в элементе нет).
    Линейные размеры в мм нормализуются к м (`_normalize_param_value`).
    """
    if not isinstance(params, dict) or not params:
        return []
    found_lines: List[str] = []
    missing: List[str] = []
    for name, spec in params.items():
        if not isinstance(spec, dict):
            continue
        value = spec.get("value")
        origin = str(spec.get("origin") or "")
        if value is None or value == "" or origin == "not_found":
            missing.append(str(name))
            continue
        unit = str(spec.get("unit") or "").strip()
        value, unit = _normalize_param_value(value, unit)
        if isinstance(value, float):
            # Компактный вид без «хвостов» вычислений (145600.0000000028 → 145600)
            value = f"{value:.6f}".rstrip("0").rstrip(".")
        unit_text = f" {unit}" if unit else ""
        found_lines.append(f"- {name}: {value}{unit_text}")

    lines = ["## Параметры элемента (для подбора работ)"]
    if found_lines:
        lines.extend(found_lines)
    else:
        lines.append("(все параметры не определены)")
    if missing:
        lines.append(f"Не определены: {', '.join(missing)}")
    return lines


# Нормализация единиц: мм → м (и производные площади/объёма). Сырой дамп IFC
# хранит размеры преимущественно в мм, расценки оперируют метрами.
_UNIT_NORMALIZE = {
    "мм": ("м", 1e-3),
    "мм2": ("м2", 1e-6),
    "мм²": ("м2", 1e-6),
    "мм3": ("м3", 1e-9),
    "мм³": ("м3", 1e-9),
}


def _normalize_param_value(value: Any, unit: str):
    """Переводит числовое значение из мм (мм2/мм3) в м (м2/м3).

    Нечисловые значения и прочие единицы возвращаются без изменений.
    Возвращает пару (значение, единица).
    """
    norm = _UNIT_NORMALIZE.get(str(unit or "").strip())
    if norm is None:
        return value, unit
    target_unit, factor = norm
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value, unit
    return value * factor, target_unit


# ======================================================================
#  Обогащение quantity недостающими величинами
# ======================================================================

def _pick_quantity(q: Dict[str, Any], canonical: str) -> Optional[float]:
    """Берёт величину из quantity по каноническому имени или его алиасам.

    Для `joint_length_m` (длина швов) значение ВСЕГДА делится на 1000 —
    периметр приходит из параметров подбора в миллиметрах, а в объём
    работ должен идти в метрах.
    """
    if not q:
        return None
    for key in _QUANTITY_KEYS.get(canonical, (canonical,)):
        v = safe_float(q.get(key), default=None)
        if v is None or v <= 0:
            continue
        # Длину швов всегда переводим из мм в м
        if canonical == "joint_length_m":
            v = v / 1000.0
        return float(v)
    return None


def _enrich_quantity_from_element(
    q: Optional[Dict[str, Any]],
    element: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Дополняет quantity недостающими величинами из полей элемента.

    В разных экспортах объёмы/площади/длины могут лежать не в quantity, а в
    полях самого элемента (first_element дерева группировки):
        «Объём, м3», «Площадь, м2», «Длина, мм», «Ширина, мм», «Высота, мм».
    Если в quantity этих величин нет — берём их из element.

    Длина «Длина, мм» нормализуется в метры (÷1000).
    """
    result: Dict[str, Any] = dict(q or {})
    element = element or {}

    if _pick_quantity(result, "volume_m3") is None:
        v = safe_float(element.get("Объём, м3"), default=None)
        if v is None:
            v = safe_float(element.get("Объем, м3"), default=None)
        if v is None:
            v = safe_float(element.get("QTO_bbox::Объём_м3"), default=None)
        if v is not None and v > 0:
            result["volume_m3"] = float(v)

    if _pick_quantity(result, "area_m2") is None:
        a = safe_float(element.get("Площадь, м2"), default=None)
        if a is None:
            a = safe_float(element.get("QTO_bbox::Площадь_м2"), default=None)
        if a is not None and a > 0:
            result["area_m2"] = float(a)

    if _pick_quantity(result, "length_m") is None:
        length_mm = safe_float(element.get("Длина, мм"), default=None)
        if length_mm is not None and length_mm > 0:
            result["length_m"] = length_mm / 1000.0

    if not result.get("count"):
        result["count"] = 1

    return result


# ======================================================================
#  Промпт и работа с LLM
# ======================================================================

def _build_user_prompt(
    element_payload: Dict[str, Any],
    tables: List[Dict[str, str]],
    candidate_works: List[Dict[str, Any]],
    quantity: Optional[Dict[str, Any]] = None,
    group_count: Optional[int] = None,
    element_params: Optional[Dict[str, Any]] = None,
) -> str:
    """Формирует пользовательский запрос LLM по группе элементов и кандидатам.

    element_payload — запись ПЕРВОГО элемента группы (представителя): по нему
    LLM понимает тип/материал/технологию работ. quantity — суммарные объёмы
    по всей группе (если None — объёмы представителя), group_count — число
    элементов в группе, element_params — параметры подбора представителя
    (Параметры_подбора_элементов.json: геометрия, константы проекта и т.д.).
    """
    element = element_payload.get("element", {}) or {}
    if quantity is None:
        quantity = {}
        for work in element_payload.get("works", []) or []:
            q = work.get("quantity") or {}
            if q:
                for key, value in q.items():
                    if value:
                        quantity[key] = value
                break

    lines = ["## Группа элементов"]
    if group_count:
        lines.append(f"Количество элементов в группе: {group_count}")
    if element.get("name"):
        lines.append(f"Наименование: {element['name']}")
    if element.get("ifc_class"):
        lines.append(f"IFC-класс: {element['ifc_class']}")
    if element.get("predefined_type"):
        lines.append(f"Тип (PredefinedType): {element['predefined_type']}")
    mssk = element_payload.get("mssk_context") or {}
    if mssk.get("code") or mssk.get("name"):
        lines.append(f"Код МССК: {mssk.get('code', '')} ({mssk.get('name', '')})")
    if element.get("material"):
        lines.append(f"Материал: {element['material']}")
    if element.get("construction_method"):
        lines.append(f"Технология: {element['construction_method']}")
    if element.get("storey"):
        storey = element["storey"]
        if element.get("storey_type"):
            storey += f" ({element['storey_type']})"
        lines.append(f"Этаж: {storey}")
    if quantity:
        qty_text = ", ".join(f"{k}={v}" for k, v in quantity.items())
        lines.append(f"Объёмы: {qty_text}")
    # Параметры подбора представителя группы (геометрия + константы проекта)
    param_lines = _format_element_params(element_params)
    if param_lines:
        lines.append("")
        lines.extend(param_lines)
    if tables:
        lines.append("")
        lines.append("## Подобранные таблицы работ")
        for table in tables:
            lines.append(f"- {table['code']} — {table['name']}")

    lines.append("")
    lines.append("## Работы-кандидаты (нормы цифрового сборника)")
    for work in candidate_works:
        cost = work.get("directCosts")
        cost_text = f", прямые затраты: {cost}" if cost is not None else ""
        lines.append(
            f"- {work['pressmark']} | {work.get('title', '')} "
            f"| ед. изм.: {work.get('unitOfMeasure', '')}{cost_text}"
        )

    lines.append("")
    lines.append(
        "## Задача\nВыбери из работ-кандидатов только те, которые нужны для "
        "выполнения работ по этой группе элементов. Верни JSON "
        '{"selected": [{"pressmark": "...", "reason": "..."}]}.'
    )
    return "\n".join(lines)


def _match_works_by_pressmark(
    selected: List[Dict[str, Any]],
    candidate_works: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Сопоставляет ответ LLM (шифры) с работами-кандидатами."""
    by_pressmark = {str(w.get("pressmark")): w for w in candidate_works}
    reasons = {
        str(item.get("pressmark")): str(item.get("reason") or "")
        for item in selected
        if isinstance(item, dict) and item.get("pressmark")
    }
    matched: List[Dict[str, Any]] = []
    for pressmark, reason in reasons.items():
        work = by_pressmark.get(pressmark)
        if work is None:
            logger.warning(f"LLM выбрал неизвестный шифр работы: {pressmark}")
            continue
        matched.append({"work": work, "reason": reason, "llm_selected": True})
    return matched


def _load_leaf_groups(grouped_json_path: str) -> List[Dict[str, Any]]:
    """Листовые группы элементов из filtered_elements_grouped_AR.json.

    Обход дерева группировки АР (МССК → Материал → Наименование) в глубину:
    листовые узлы (без children) содержат indices — 0-based индексы строк
    filtered_elements.xlsx, они же — позиции записей elements в
    Подобранные_таблицы_работ.json (файлы строятся по одним и тем же строкам
    в одном порядке). Возвращает список листовых групп в порядке дерева.
    """
    if not os.path.isfile(grouped_json_path):
        logger.warning(
            f"Нет файла группировки {os.path.basename(grouped_json_path)} — "
            "LLM-подбор выполняется по каждому элементу отдельно"
        )
        return []
    try:
        with open(grouped_json_path, "r", encoding="utf-8") as fh:
            tree = json.load(fh)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать файл группировки: {exc}")
        return []

    leaves: List[Dict[str, Any]] = []

    def walk(nodes: List[Dict[str, Any]], path: List[str]) -> None:
        for node in nodes or []:
            current_path = path + [str(node.get("name", ""))]
            children = node.get("children") or []
            if children:
                walk(children, current_path)
            else:
                indices = [
                    int(i) for i in node.get("indices", []) or []
                    if isinstance(i, (int, str)) and str(i).strip().lstrip("-").isdigit()
                ]
                if indices:
                    leaves.append({
                        "name": str(node.get("name", "")),
                        "path": current_path,
                        "indices": sorted(indices),
                    })

    if isinstance(tree, list):
        walk(tree, [])
    return leaves


def _extract_quantity_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Берёт quantity первого непустого work из payload.

    Дополнительно обогащает её полями самого элемента («Объём, м3»,
    «Площадь, м2», «Длина, мм») — на случай, если в quantity этих данных
    нет (такое встречается в разных экспортах).
    """
    q: Dict[str, Any] = {}
    for work in payload.get("works", []) or []:
        wq = work.get("quantity") or {}
        if any(wq.get(k) for k in (
            "volume_m3", "area_m2", "length_m", "joint_length_m", "count",
        )):
            q = dict(wq)
            break
    return _enrich_quantity_from_element(q, payload.get("element") or {})


def _sum_group_quantities(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Суммарные величины по всем элементам группы.

    Поддерживаются:
      * volume_m3       — объём, м³;
      * area_m2         — площадь, м²;
      * length_m        — длина (обычная), м;
      * joint_length_m  — длина швов, м;
      * count           — количество элементов, шт.

    У каждого элемента quantity может быть неполной — недостающие величины
    подтягиваются из полей самого элемента (_extract_quantity_from_payload).
    """
    total: Dict[str, Any] = {
        "volume_m3": 0.0,
        "area_m2": 0.0,
        "length_m": 0.0,
        "joint_length_m": 0.0,
        "count": 0,
    }
    for payload in payloads:
        q = _extract_quantity_from_payload(payload)
        for key in ("volume_m3", "area_m2", "length_m", "joint_length_m"):
            v = _pick_quantity(q, key)
            if v is not None and v > 0:
                total[key] += v
        cnt = _pick_quantity(q, "count")
        total["count"] += int(cnt) if cnt else 1
    return total


def _element_quantity_for_table(
    element_payload: Dict[str, Any], table_code: str,
) -> Dict[str, Any]:
    """Объёмы (quantity) группы элементов для конкретной таблицы работ."""
    for work in element_payload.get("works", []) or []:
        if str(work.get("code")) == str(table_code):
            q = dict(work.get("quantity") or {})
            return _enrich_quantity_from_element(
                q, element_payload.get("element") or {},
            )
    # Если таблицы с таким кодом нет — вернём обогащённую quantity из первой
    # попавшейся работы (лучше, чем совсем пусто).
    return _extract_quantity_from_payload(element_payload)


# ======================================================================
#  Площадь опалубки вертикальных граней фундаментной плиты
#  (периметр × толщина — по параметрам подбора элементов)
# ======================================================================

def _is_foundation_slab(element_payload: Dict[str, Any]) -> bool:
    """Фундаментная плита: IfcSlab/BASESLAB или «фундаментная плита» в имени/МССК.

    Только для фундаментных плит площадь опалубки вертикальных граней
    считается как периметр × толщина (у перекрытий опалубка — нижняя
    площадь, у стен — боковая, поэтому на них правило не распространяется).
    """
    element = element_payload.get("element", {}) or {}
    text = " ".join([
        str(element.get("name") or ""),
        str((element_payload.get("mssk_context") or {}).get("name") or ""),
    ]).lower()
    if "фундаментн" in text and "плит" in text:
        return True
    return (
        str(element.get("ifc_class") or "") == "IfcSlab"
        and str(element.get("predefined_type") or "").upper() == "BASESLAB"
    )


def _is_formwork_work(work: Dict[str, Any]) -> bool:
    """Работа по опалубке с единицей измерения «м2» (монтаж/демонтаж опалубки).

    Прочие работы с опалубкой в наименовании (например, установка арматуры
    «в опалубку», ед. изм. «1 т») не подпадают — несовпадение единицы.
    """
    title = str(work.get("title") or "").lower()
    unit = str(
        work.get("unitOfMeasure") or work.get("unit_of_measure") or ""
    ).lower().replace("²", "2")
    return "опалубк" in title and "м2" in unit


def _param_meters(params: Optional[Dict[str, Any]], name: str) -> Optional[float]:
    """Числовое значение параметра подбора, приведённое к метрам.

    Ищет параметр по каноническому имени и его алиасам (русские/английские
    варианты в Параметры_подбора_элементов.json). Значения с единицами
    мм/мм2/мм3 нормализуются `_normalize_param_value`; нечисловые и
    ненайденные параметры (not_found) возвращают None.
    """
    if not isinstance(params, dict):
        return None
    aliases = _PARAM_ALIASES.get(name, (name,))
    for alias in aliases:
        spec = params.get(alias)
        if not isinstance(spec, dict):
            continue
        if spec.get("origin") == "not_found":
            continue
        num = safe_float(spec.get("value"), default=None)
        if num is None or num <= 0:
            continue
        value, _unit = _normalize_param_value(num, spec.get("unit"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def _joint_length_m(params: Optional[Dict[str, Any]]) -> Optional[float]:
    """Длина шва = периметр контура элемента (м).

    Приоритет:
      1. Явный параметр «perimeter» — если найден, берём его.
      2. Fallback: 2 × Длина + Высота — периметр панели стены
         в вертикальной плоскости (по габаритам элемента).
      3. Fallback: 2 × Длина + Ширина — если высоты нет (плиты, балки).

    Все габариты нормализуются в метры через `_param_meters`.
    """
    explicit = _param_meters(params, "perimeter")
    if explicit is not None:
        return explicit

    length = _param_meters(params, "length")
    height = _param_meters(params, "height")
    width = _param_meters(params, "width")

    if length is not None and height is not None:
        return 2.0 * (length + height)
    if length is not None and width is not None:
        return 2.0 * (length + width)
    return None


def _sum_joint_length(
    payloads: List[Dict[str, Any]],
    params_by_id: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    """Суммарная длина швов по всем элементам группы (м).

    Считается по параметрам подбора: явный perimeter либо периметр из
    габаритов (2 × (length + height) / 2 × (length + width)).
    Возвращает None, если ни у одного элемента группы периметр не удалось
    определить.
    """
    total = 0.0
    found = False
    for payload in payloads:
        global_id = str((payload.get("element") or {}).get("global_id") or "").strip()
        length = _joint_length_m(params_by_id.get(global_id))
        if length is None:
            continue
        total += length
        found = True
    return round(total, 3) if found else None


def _formwork_area_m2(params: Optional[Dict[str, Any]]) -> Optional[float]:
    """Площадь опалубки вертикальных граней плиты: периметр × толщина (м²).

    Толщина = «thickness» или fallback «width» (для стен толщина = ширина).
    Периметр = `_joint_length_m` (явный или из габаритов).
    """
    thickness = _param_meters(params, "thickness")
    if thickness is None:
        thickness = _param_meters(params, "width")
    if thickness is None:
        return None
    perimeter = _joint_length_m(params)
    if perimeter is None:
        return None
    return perimeter * thickness


def _sum_formwork_area(
    payloads: List[Dict[str, Any]],
    params_by_id: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    """Суммарная площадь опалубки по всем элементам группы (периметр × толщина).

    Возвращает None, если ни у одного элемента группы не удалось определить
    периметр и толщину (объёмы работ остаются по QTO, как раньше).
    """
    total = 0.0
    found = False
    for payload in payloads:
        global_id = str((payload.get("element") or {}).get("global_id") or "").strip()
        area = _formwork_area_m2(params_by_id.get(global_id))
        if area is None:
            continue
        total += area
        found = True
    return round(total, 3) if found else None


# ======================================================================
#  Расход арматуры группы (ReinforcementVolumeRatio × объём, кг → т)
#  — объём арматурных расценок (ед. изм. «1 т»), как в режиме КР
# ======================================================================

def _is_rebar_ton_work(work: Dict[str, Any]) -> bool:
    """Арматурная расценка с единицей измерения «т» («1 т»).

    Наименование содержит одно из ключевых слов арматурных работ
    (арматура/каркасы/стержни/сетки/закладные детали), единица измерения —
    тонны. Прочие работы с «арматурой» в названии (например, «в опалубку»,
    ед. изм. «100 м3») не подпадают — несовпадение единицы.
    """
    title = str(work.get("title") or work.get("name") or "").lower()
    unit = str(work.get("unit_of_measure") or work.get("unitOfMeasure") or "")
    has_keyword = any(kw in title for kw in _REBAR_KEYWORDS)
    return has_keyword and bool(_TON_UNIT_RE.search(unit))


def _load_rebar_mass_by_gid(run_dir: str) -> Dict[str, float]:
    """Расход арматуры (кг) по каждому элементу из filtered_elements.xlsx.

    ReinforcementVolumeRatio — плотность армирования (кг/м³), расход
    элемента = кг/м³ × объём элемента (м³) — как суммарный расход группы
    в режиме КР (group_excel). Возвращает карту {global_id: кг}; при
    отсутствии файла/колонок — пустая карта (объём арматурных работ не
    заполняется, прежнее поведение).
    """
    path = os.path.join(run_dir, FILTERED_XLSX_FILENAME)
    if not os.path.isfile(path):
        logger.warning(
            f"Нет файла {FILTERED_XLSX_FILENAME} — расход арматуры не "
            "определён, объём арматурных работ не заполняется"
        )
        return {}
    try:
        df = pd.read_excel(path)
    except Exception as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}

    if "GlobalId" not in df.columns or "ReinforcementVolumeRatio" not in df.columns:
        logger.warning(
            f"В {FILTERED_XLSX_FILENAME} нет колонок GlobalId/"
            "ReinforcementVolumeRatio — расход арматуры не определён"
        )
        return {}

    # Колонка объёма: агрегированная «Объём, м3» (как в group_excel, КР);
    # fallback — первая колонка «Объём … м3» без QTO-префикса
    volume_col = "Объём, м3" if "Объём, м3" in df.columns else None
    if volume_col is None:
        for col in df.columns:
            if (
                str(col).startswith("Объём")
                and "м3" in str(col)
                and not str(col).startswith("QTO")
            ):
                volume_col = col
                break
    if volume_col is None:
        logger.warning(
            f"В {FILTERED_XLSX_FILENAME} нет колонки объёма (м3) — "
            "расход арматуры не определён"
        )
        return {}

    masses: Dict[str, float] = {}
    for _, row in df.iterrows():
        gid = str(row.get("GlobalId") or "").strip()
        if not gid:
            continue
        ratio = safe_float(row.get("ReinforcementVolumeRatio"), default=0.0)
        volume = safe_float(row.get(volume_col), default=0.0)
        if ratio > 0 and volume > 0:
            masses[gid] = masses.get(gid, 0.0) + ratio * volume

    logger.info(
        f"Расход арматуры определён для {len(masses)} элементов "
        f"({FILTERED_XLSX_FILENAME}: ReinforcementVolumeRatio × объём)"
    )
    return masses


def _assign_rebar_volume(
    selected_works: List[Dict[str, Any]], rebar_mass_kg: float,
) -> None:
    """Назначает объём (т) арматурной расценке группы — расход, кг ÷ 1000.

    Суммарный расход арматуры группы (ReinforcementVolumeRatio × объёмы
    элементов, кг) уже включает все арматурные работы группы, поэтому
    подставляется ровно в одну расценку (как в КР — иначе объём задваивается):
      * приоритет — расценка «Установка … отдельных стержней …»;
      * если такой нет — первая арматурная расценка с единицей «т»
        (в АР LLM обычно выбирает одну арматурную работу на группу).
    Остальные арматурные расценки (каркасы/сетки/закладные детали) остаются
    без объёма. Объём записывается в quantity["mass_t"] (копия quantity —
    словарь объёмов группы общий для всех работ группы).
    """
    if rebar_mass_kg <= 0 or not selected_works:
        return
    rebar_rows = [w for w in selected_works if _is_rebar_ton_work(w)]
    if not rebar_rows:
        return
    target = next(
        (
            w for w in rebar_rows
            if "отдельн" in str(w.get("title") or "").lower()
            and "стержн" in str(w.get("title") or "").lower()
        ),
        rebar_rows[0],
    )
    tons = rebar_mass_kg / 1000.0
    quantity = dict(target.get("quantity") or {})
    quantity["mass_t"] = round(tons, 4)
    target["quantity"] = quantity
    target["rebar_mass_kg"] = round(rebar_mass_kg, 2)
    logger.info(
        f"Объём арматурной работы «{target.get('title')}»: "
        f"{tons:.4f} т (расход арматуры группы {rebar_mass_kg:.2f} кг)"
    )


def _build_work_row(
    element_payload: Dict[str, Any],
    table: Dict[str, str],
    work: Dict[str, Any],
    reason: str,
    llm_selected: bool,
    quantity: Optional[Dict[str, Any]] = None,
    formwork_area: Optional[float] = None,
    joint_length: Optional[float] = None,
) -> Dict[str, Any]:
    """Строка выбранной работы в итоговом перечне.

    quantity — объёмы для расчёта (сумма по группе либо объёмы отдельного
    элемента); если None — берётся quantity таблицы из записи элемента.
    formwork_area — площадь опалубки фундаментной плиты (периметр × толщина,
    сумма по группе); для работ по монтажу/демонтажу опалубки (ед. изм. «м2»)
    подставляется вместо QTO-площади элемента.
    joint_length — длина швов группы (= сумма периметров); для работ,
    у которых ЕДИНИЦА ИЗМЕРЕНИЯ относится к швам (содержит «шов»/«шв»/
    «гермет»/«заделк»), подставляется вместо QTO-длины элемента.
    """
    element = element_payload.get("element", {}) or {}

    # 1. Базовая quantity: из аргумента или из works записи элемента
    if quantity is None:
        quantity = _element_quantity_for_table(element_payload, table["code"])
    quantity = dict(quantity or {})

    # 2. Обогащение из полей элемента — гарантированно добавляет
    #    volume_m3 / area_m2 / length_m, если их нет в quantity.
    #    (функция уже есть в файле — если нет, см. блок «если её нет» ниже)
    quantity = _enrich_quantity_from_element(quantity, element)

    # 3. Гарантия наличия length_m: если после enrichment его нет —
    #    ещё раз ищем длину в полях элемента вручную (на случай, если
    #    _enrich_quantity_from_element не отработал или вернул 0).
    if not quantity.get("length_m"):
        length_mm = safe_float(element.get("Длина, мм"), default=None)
        if length_mm is None:
            length_mm = safe_float(element.get("Длина_Length_мм"), default=None)
        if length_mm is None:
            length_mm = safe_float(
                element.get("QTO_Qto_WallBaseQuantities_Длина_Length_мм"),
                default=None,
            )
        if length_mm is None:
            length_mm = safe_float(
                element.get("QTO_Qto_SlabBaseQuantities_Длина_Length_мм"),
                default=None,
            )
        if length_mm is None:
            length_mm = safe_float(element.get("QTO_bbox::Длина_мм"), default=None)
        if length_mm is not None and length_mm > 0:
            quantity["length_m"] = length_mm / 1000.0
        else:
            # Возможно, длина уже в метрах
            length_m = safe_float(element.get("Длина, м"), default=None)
            if length_m is not None and length_m > 0:
                quantity["length_m"] = float(length_m)

    # 4. Опалубка (м²)
    formwork_area_m2: Optional[float] = None
    if formwork_area and _is_formwork_work(work):
        quantity["area_m2"] = formwork_area
        formwork_area_m2 = formwork_area

    # 5. Швы (м) — если единица измерения шовная
    joint_length_m: Optional[float] = None
    if joint_length and _is_joint_work(work):
        quantity["joint_length_m"] = joint_length
        joint_length_m = joint_length

    return {
        "pressmark": work.get("pressmark"),
        "title": work.get("title"),
        "unit_of_measure": (
            work.get("unitOfMeasure")
            or work.get("unit_of_measure")
            or work.get("unit")
            or work.get("measure")
        ),
        "work_id": work.get("id"),
        "table_code": table["code"],
        "table_name": table["name"],
        "direct_costs": work.get("directCosts"),
        "cur_direct_costs": work.get("curDirectCosts"),
        "salary": work.get("salary"),
        "cur_salary": work.get("curSalary"),
        "operation_of_machines": work.get("operationOfMachines"),
        "cur_operation_of_machines": work.get("curOperationOfMachines"),
        "cost_of_material_resources": work.get("costOfMaterialResources"),
        "cur_cost_of_material_resources": work.get("curCostOfMaterialResources"),
        "quantity": quantity,
        "formwork_area_m2": formwork_area_m2,
        "joint_length_m": joint_length_m,
        "reason": reason,
        "llm_selected": llm_selected,
    }


# Соответствие полей разбивки стоимости: детальные параметры позиции
# (catalog/work-process/detail) → поля строки работы в итоговом перечне.
# Список работ (catalog/work-process/list) этих полей не содержит (только МР
# и итоги), поэтому ЗП/ЭМ берутся из детальных параметров позиции.
_DETAIL_COST_FIELD_MAP = {
    "salary": "salary",
    "curSalary": "cur_salary",
    "operationOfMachines": "operation_of_machines",
    "curOperationOfMachines": "cur_operation_of_machines",
    "costOfMaterialResources": "cost_of_material_resources",
    "curCostOfMaterialResources": "cur_cost_of_material_resources",
    "directCosts": "direct_costs",
    "curDirectCosts": "cur_direct_costs",
    "totalCost": "total_cost",
    "curTotalCost": "cur_total_cost",
}


def _enrich_selected_works_with_costs(
    result_elements: List[Dict[str, Any]],
    period_id: Any,
) -> None:
    """Дополняет выбранные работы показателями стоимости (ЗП/ЭМ/МР).

    Список работ цифрового сборника (catalog/work-process/list) содержит
    только МР и итоговые затраты — разбивка стоимости (curSalary — ЗП,
    curOperationOfMachines — ЭМ) отсутствует. Полная разбивка берётся из
    детальных параметров позиции (catalog/work-process/detail) по id каждой
    ВЫБРАННОЙ работы (LLM отбирает единицы из десятков кандидатов таблицы —
    запрашивать детали всех кандидатов не нужно) за период ТСН запуска.

    Обновляет строки selected_works на месте (поля _DETAIL_COST_FIELD_MAP);
    при недоступности эндпоинта/периода стоимость остаётся по данным списка
    (как раньше) — шаг не должен обнулять результат запуска.
    """
    try:
        period_num = int(period_id)
    except (TypeError, ValueError):
        logger.warning(
            "Неизвестен период ТСН (period_id) — разбивка стоимости работ "
            "(ЗП/ЭМ) не заполнена"
        )
        return

    work_ids: List[int] = []
    seen: set = set()
    for entry in result_elements:
        for work in entry.get("selected_works") or []:
            wid = work.get("work_id")
            if wid is None or wid in seen:
                continue
            seen.add(wid)
            work_ids.append(int(wid))
    if not work_ids:
        return

    from src.services.works_fetcher import fetch_work_details

    logger.info(
        f"Запрос разбивки стоимости (ЗП/ЭМ/МР) для {len(work_ids)} "
        "выбранных работ (catalog/work-process/detail)"
    )
    details = fetch_work_details(work_ids, period_num)

    enriched = 0
    for entry in result_elements:
        for work in entry.get("selected_works") or []:
            detail = details.get(work.get("work_id"))
            if not detail:
                continue
            for src_field, dst_field in _DETAIL_COST_FIELD_MAP.items():
                value = detail.get(src_field)
                if value is not None:
                    work[dst_field] = value
            enriched += 1
    logger.info(
        f"Разбивка стоимости получена для {enriched} из {len(work_ids)} "
        "выбранных работ"
    )


def select_final_works(
    tables_json_path: str,
    works_json_path: str,
    run_dir: str,
    llm_config: Optional[Any] = None,
) -> Optional[str]:
    """Финальный подбор работ через LLM для каждой группы элементов.

    Args:
        tables_json_path: путь к Подобранные_таблицы_работ.json запуска.
        works_json_path: путь к Подобранные_работы.json запуска (кандидаты).
        run_dir: папка запуска run_<NNN>/ (сюда пишутся итоговые файлы).
        llm_config: конфигурация LLM (pd_parser.Config); по умолчанию —
            из src.core.config.load_config (OLLAMA_BASE_URL / NORMS_LLM_MODEL).

    Returns:
        Путь к Финальный_перечень_работ.json либо None, если данных нет.
    """
    if not os.path.isfile(tables_json_path):
        logger.warning(f"Нет файла подобранных таблиц: {tables_json_path}")
        return None
    if not os.path.isfile(works_json_path):
        logger.warning(
            f"Нет файла работ цифрового сборника: {works_json_path} — "
            "финальный подбор работ через LLM не выполняется"
        )
        return None

    with open(tables_json_path, "r", encoding="utf-8") as fh:
        tables_payload = json.load(fh)
    with open(works_json_path, "r", encoding="utf-8") as fh:
        works_payload = json.load(fh)

    # Карта «шифр таблицы → работы» из Подобранные_работы.json
    works_by_table: Dict[str, Dict[str, Any]] = {}
    for table in works_payload.get("tables", []) or []:
        works_by_table[str(table.get("code"))] = table

    # LLM-клиент (тот же, что для разбора ПОС)
    from src.services.pd_parser import LLMClient

    if llm_config is None:
        cfg = load_config()
        from src.services.pd_parser import Config as LLMConfig

        llm_config = LLMConfig(
            llm_base_url=cfg.ollama_url,
            llm_model=cfg.model_ollama,
        )
    llm = LLMClient(llm_config)

    result_elements: List[Dict[str, Any]] = []
    total_selected_works = 0

    elements_payload = tables_payload.get("elements", []) or []

    # Единицы обработки: листовые группы элементов (МССК → Материал →
    # Наименование) из filtered_elements_grouped_AR.json.
    leaf_groups = _load_leaf_groups(os.path.join(run_dir, GROUPED_JSON_FILENAME))

    # Параметры подбора элементов (global_id → parameters) из корня сессии
    params_by_id = _load_element_params(run_dir)

    # Расход арматуры (кг) по каждому элементу (ReinforcementVolumeRatio ×
    # объём, filtered_elements.xlsx) — объём арматурных расценок (ед. «т»)
    rebar_mass_by_gid = _load_rebar_mass_by_gid(run_dir)

    processing_units: List[Dict[str, Any]] = []
    if leaf_groups:
        covered: set = set()
        for group in leaf_groups:
            indices = sorted(
                i for i in group["indices"] if 0 <= i < len(elements_payload)
            )
            if not indices:
                continue
            covered.update(indices)
            processing_units.append({
                "first": elements_payload[indices[0]],
                "payloads": [elements_payload[i] for i in indices],
                "group": group,
            })
        for idx, payload in enumerate(elements_payload):
            if idx not in covered:
                processing_units.append({
                    "first": payload, "payloads": [payload], "group": None,
                })
        logger.info(
            f"LLM-подбор работ: {len(leaf_groups)} групп элементов, "
            f"{len(elements_payload)} элементов (подбор — по первому элементу "
            "группы, объёмы — по всей группе)"
        )
    else:
        processing_units = [
            {"first": payload, "payloads": [payload], "group": None}
            for payload in elements_payload
        ]

    for unit in processing_units:
        element_payload = unit["first"]
        group = unit["group"]
        element = element_payload.get("element", {}) or {}
        # Параметры подбора представителя группы (геометрия, константы и т.д.)
        element_params = params_by_id.get(
            str(element.get("global_id") or "").strip()
        )
        # Суммарные объёмы по всем элементам группы (для отдельного элемента —
        # объёмы берутся из его записи, как раньше)
        group_quantity = (
            _sum_group_quantities(unit["payloads"]) if group is not None else None
        )

        # Площадь опалубки фундаментной плиты (периметр × толщина, сумма по
        # группе): подставляется в работы монтажа/демонтажа опалубки вместо
        # QTO-площади элемента.
        formwork_area = (
            _sum_formwork_area(unit["payloads"], params_by_id)
            if _is_foundation_slab(element_payload)
            else None
        )

        # Длина швов группы (= сумма периметров элементов). Подставляется в
        # работы, единица измерения которых содержит «шов»/«шв»/«гермет»/
        # «заделк» (например «100 м шва»).
        joint_length = _sum_joint_length(unit["payloads"], params_by_id)

        # Суммарный расход арматуры группы (кг) — Σ по элементам группы
        # (ReinforcementVolumeRatio × объём каждого элемента)
        group_rebar_kg = 0.0
        if rebar_mass_by_gid:
            for payload in unit["payloads"]:
                gid = str((payload.get("element") or {}).get("global_id") or "").strip()
                group_rebar_kg += rebar_mass_by_gid.get(gid, 0.0)
            group_rebar_kg = round(group_rebar_kg, 2)

        # Таблицы группы (уникальные шифры, порядок как в файле) — только по
        # первому элементу группы (представителю)
        seen: set = set()
        tables: List[Dict[str, str]] = []
        candidate_works: List[Dict[str, Any]] = []
        for work in element_payload.get("works", []) or []:
            code = str(work.get("code") or "").strip()
            if not code:
                continue
            if code not in seen:
                seen.add(code)
                tables.append({"code": code, "name": work.get("name") or ""})
                for w in (works_by_table.get(code, {}) or {}).get("works", []) or []:
                    candidate_works.append(w)

        if not candidate_works:
            entry = {
                "element": element_payload.get("element", {}),
                "mssk_context": element_payload.get("mssk_context"),
                "selected_collection": element_payload.get("selected_collection"),
                "tables": tables,
                "selected_works": [],
                "note": "Нет работ-кандидатов в цифровом сборнике",
            }
            if element_params:
                entry["element_parameters"] = element_params
            if group is not None:
                entry["group"] = {
                    "name": group.get("name", ""),
                    "path": group.get("path", []),
                    "element_count": len(unit["payloads"]),
                    "element_indices": group["indices"],
                }
                entry["group_quantity"] = group_quantity
            if joint_length is not None:
                entry["joint_length_m"] = joint_length
            result_elements.append(entry)
            continue

        # Ограничение кандидатов (защита контекста LLM)
        candidate_works = candidate_works[:_MAX_CANDIDATE_WORKS]

        user_prompt = _build_user_prompt(
            element_payload, tables, candidate_works,
            quantity=group_quantity,
            group_count=len(unit["payloads"]) if group is not None else None,
            element_params=element_params,
        )
        selected_works: List[Dict[str, Any]] = []
        note = ""
        try:
            answer = llm.complete_json(
                system=SYSTEM_PROMPT, user=user_prompt,
            )
            selected = answer.get("selected")
            if not isinstance(selected, list):
                selected = []
            matched = _match_works_by_pressmark(selected, candidate_works)
            if matched:
                # Таблица работы определяется по её шифру (префикс шифра таблицы)
                def _table_for(work: Dict[str, Any]) -> Dict[str, str]:
                    pressmark = str(work.get("pressmark") or "")
                    for table in tables:
                        if pressmark.startswith(table["code"]):
                            return table
                    return tables[0]

                for item in matched:
                    table = _table_for(item["work"])
                    selected_works.append(
                        _build_work_row(
                            element_payload, table, item["work"],
                            item["reason"], item["llm_selected"],
                            quantity=group_quantity,
                            formwork_area=formwork_area,
                            joint_length=joint_length,
                        )
                    )
            else:
                note = "LLM не выбрал работы — включены все работы-кандидаты"
                logger.warning(
                    f"LLM не выбрал работы для группы "
                    f"«{(element_payload.get('element') or {}).get('name')}» — "
                    "fallback на полный список кандидатов"
                )
        except Exception as exc:
            note = f"Ошибка LLM ({exc}) — включены все работы-кандидаты"
            logger.error(
                f"Ошибка LLM при подборе работ для группы "
                f"«{(element_payload.get('element') or {}).get('name')}»: {exc}",
                exc_info=True,
            )

        if not selected_works:
            # Fallback: все кандидаты, сгруппированные по таблицам
            for table in tables:
                for work in (works_by_table.get(table["code"], {}) or {}).get("works", []) or []:
                    selected_works.append(
                        _build_work_row(
                            element_payload, table, work, "", False,
                            quantity=group_quantity,
                            formwork_area=formwork_area,
                            joint_length=joint_length,
                        )
                    )

        # Объём арматурных работ (ед. изм. «т») — суммарный расход арматуры
        # группы (кг ÷ 1000), назначается ровно одной расценке (как в КР)
        if group_rebar_kg > 0:
            _assign_rebar_volume(selected_works, group_rebar_kg)

        total_selected_works += len(selected_works)
        entry = {
            "element": element_payload.get("element", {}),
            "mssk_context": element_payload.get("mssk_context"),
            "selected_collection": element_payload.get("selected_collection"),
            "tables": tables,
            "selected_works": selected_works,
            "note": note,
        }
        if element_params:
            # Параметры представителя группы, переданные в LLM-запрос
            entry["element_parameters"] = element_params
        if formwork_area is not None:
            # Площадь опалубки группы фундаментных плит (периметр × толщина),
            # подставленная в работы монтажа/демонтажа опалубки
            entry["formwork_area_m2"] = formwork_area
        if joint_length is not None:
            # Длина швов группы (= сумма периметров), подставленная в работы
            # по швам / герметизации / заделке
            entry["joint_length_m"] = joint_length
        if group_rebar_kg > 0:
            # Суммарный расход арматуры группы (кг) — основа объёма
            # арматурных расценок (ед. изм. «т») группы
            entry["rebar_mass_kg"] = group_rebar_kg
        if group is not None:
            # Метаданные группы: подбор выполнен по первому элементу
            # (представителю), объёмы — сумма по всем элементам группы
            entry["group"] = {
                "name": group.get("name", ""),
                "path": group.get("path", []),
                "element_count": len(unit["payloads"]),
                "element_indices": group["indices"],
            }
            entry["group_quantity"] = group_quantity
        result_elements.append(entry)

    # Разбивка стоимости выбранных работ (ЗП/ЭМ/МР) из детальных параметров
    # позиций цифрового сборника (catalog/work-process/detail, по period_id)
    _enrich_selected_works_with_costs(result_elements, works_payload.get("period_id"))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processing_type": "AR",
        "period": works_payload.get("period"),
        "period_id": works_payload.get("period_id"),
        "source": os.path.basename(works_json_path),
        "total_elements": len(result_elements),
        "total_works": total_selected_works,
        "elements": result_elements,
    }

    # JSON
    json_path = os.path.join(run_dir, FINAL_WORKS_JSON_FILENAME)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info(
        f"Финальный перечень работ сохранён: {json_path} "
        f"({len(result_elements)} групп, {total_selected_works} работ)"
    )

    # XLSX — итоговая таблица в структуре режима КР
    xlsx_path = os.path.join(run_dir, FINAL_WORKS_XLSX_FILENAME)
    build_final_works_xlsx(result_elements, xlsx_path)

    return json_path


# ======================================================================
#  XLSX в структуре режима КР (ОБЩИЙ_Финальный_перечень_работ.xlsx)
# ======================================================================

def _clean_element_header_name(name: Any) -> str:
    """Убирает цифровой ID в конце имени элемента (после последнего двоеточия).

    Аналогично обработке «Имя элемента» из Revit в api_works_lookup (КР):
    «Фундаментная плита3:Фундаментная плита:2176357» → «Фундаментная плита3:Фундаментная плита».
    """
    text = str(name or "").strip()
    if ":" in text:
        parts = text.split(":")
        if parts[-1].strip().isdigit():
            text = ":".join(parts[:-1])
    return text


def _element_header(element_entry: Dict[str, Any]) -> str:
    """Заголовок группы элементов для строки-заголовка в таблице.

    Формат как в КР: «Имя (материал, МССК)» — например
    «Фундаментная плита3:Фундаментная плита (Железобетон монолитный, Фундаментная плита)».
    """
    element = element_entry.get("element", {}) or {}
    header = _clean_element_header_name(element.get("name")) or str(
        element.get("ifc_class") or "Элемент"
    )
    info_parts: List[str] = []
    material = str(element.get("material") or "").strip()
    if material:
        info_parts.append(material)
    mssk_name = str((element_entry.get("mssk_context") or {}).get("name") or "").strip()
    if mssk_name and mssk_name.lower() != header.lower():
        info_parts.append(mssk_name)
    if info_parts:
        header = f"{header} ({', '.join(info_parts)})"
    return header


# Множитель нормы в начале единицы измерения: «100 м2» → 100, «1000 шт.» → 1000
_UNIT_MULTIPLIER_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*)$")


def _volume_for_unit(quantity: Dict[str, Any], unit_of_measure: Any) -> str:
    """Объём работ по единице измерения расценки, нормализованный по норме.

    Выбор величины — ТОЛЬКО по единице измерения (название работы не
    анализируется):

      * «м2», «м²»                                        → area_m2;
      * «м3», «м³»                                        → volume_m3;
      * «шт», «штук», «конструкц», «элемент», «сборн»,
        «компл», «узл» (напр. «100 сборных конструкций»)  → count;
      * «т», «1 т» (арматурные расценки)                  → mass_t
        (расход арматуры группы, кг ÷ 1000);
      * единица, содержащая «шов»/«шв»/«гермет»/«заделк»
        (напр. «100 м шва», «м швов», «100 м герметизации»),
        И содержащая «м»                                  → joint_length_m
        (с fallback на length_m);
      * «м», «пог.м», «пм» (без слов про швы)             → length_m;
      * единица без физической размерности («1 стеклянная
        стойка», «1 изделие», «1 секция», «1 деталь»)     → count
        (catch-all: считаем штучной).

    ВАЖНО: проверяем оба стема — «шов» и «шв»: одного «шов» недостаточно,
    он не ловит форму «шва» в единице «100 м шва».

    Множитель нормы из начала единицы («100 м2» → 100, «1000 шт.» → 1000,
    «100 сборных конструкций» → 100, «100 м шва» → 100) применяется как
    делитель. Единицы с физической размерностью, не покрытые основными
    ветками (кг, л, мм как самостоятельные), остаются без объёма.
    """
    unit = str(unit_of_measure or "").strip().lower()
    unit = unit.replace("²", "2").replace("³", "3")

    # Снимаем множитель нормы: «100 м2» → («100», «м2»)
    divisor = 1.0
    unit_body = unit
    m = _UNIT_MULTIPLIER_RE.match(unit)
    if m:
        try:
            mult = float(m.group(1).replace(",", "."))
            if mult > 0:
                divisor = mult
        except ValueError:
            pass
        unit_body = m.group(2).strip()

    q = quantity or {}
    value = None

    # 1. Площадь
    if "м2" in unit_body:
        value = _pick_quantity(q, "area_m2")

    # 2. Объём
    elif "м3" in unit_body:
        value = _pick_quantity(q, "volume_m3")

    # 3. Количество: шт / штук / сборных конструкций / элементов / комплектов
    elif any(k in unit_body for k in (
        "шт", "штук", "конструкц", "элемент", "сборн", "компл", "узл",
    )):
        value = _pick_quantity(q, "count")

    # 4. Арматура (ед. изм. «1 т», «т») — расход арматуры группы, т
    elif _TON_UNIT_RE.search(unit):
        value = safe_float((q or {}).get("mass_t"), default=None)
        if value is not None and value <= 0:
            value = None

    # 5. Длина. Приоритет — швы (по единице измерения), иначе обычная длина.
    elif "м" in unit_body or "пог" in unit_body or "пм" in unit_body:
        if _is_joint_unit(unit_body):
            value = (_pick_quantity(q, "joint_length_m")
                     or _pick_quantity(q, "length_m"))
        else:
            value = _pick_quantity(q, "length_m")

    # 6. Нестандартная единица без физической размерности → количество.
    #    Примеры: «1 стеклянная стойка», «1 изделие», «1 секция», «1 деталь»,
    #    «1 панель», «1 блок». Единицы с явной физической размерностью
    #    (кг, л, мм как самостоятельные), не покрытые выше, остаются без
    #    объёма — не знаем, как их пересчитать.
    elif unit_body and not _PHYSICAL_UNIT_RE.search(unit_body):
        value = _pick_quantity(q, "count")

    if value is None:
        return ""

    num = float(value) / divisor
    if num > 0:
        return f"{num:.4f}"
    return ""


def build_final_works_xlsx(
    result_elements: List[Dict[str, Any]],
    xlsx_path: str,
) -> str:
    """Строит Финальный_перечень_работ.xlsx в структуре режима КР.

    Колонки и оформление — как в ОБЩИЙ_Финальный_перечень_работ.xlsx (КР):
        Шифр ТСН              = pressmark работы;
        Наименование расценки/ресурса = title работы;
        Ед. изм.              = unitOfMeasure;
        Объём работ           = объём группы по ед. изм. (м² → площадь, м³ → объём,
                                м → длина / длина шва, шт → количество,
                                т → расход арматуры группы);
        ЗП                    = curSalary × Объём работ (fallback — salary);
        ЭМ                    = curOperationOfMachines × Объём работ
                                (fallback — operationOfMachines);
        МР                    = curCostOfMaterialResources × Объём работ
                                (fallback — costOfMaterialResources);
        Стоимость             = ЗП + ЭМ + МР.

    Перед работами каждой группы — строка-заголовок группы (жирный шрифт,
    серая заливка), между группами — пустая строка, последняя строка — «ИТОГО:»
    (суммы по колонкам ЗП, ЭМ, МР, Стоимость).

    Аргументы:
        result_elements — список групп (элементы «elements» Финальный_перечень_работ.json).
        xlsx_path       — путь к создаваемому файлу.

    Возвращает путь к созданному файлу.
    """
    columns = [
        "Шифр ТСН", "Наименование расценки/ресурса", "Ед. изм.",
        "Объём работ", "ЗП", "ЭМ", "МР", "Стоимость",
        "_is_header",
    ]

    def _component_cost(
        work: Dict[str, Any], cur_key: str, base_key: str, volume_num: float,
    ) -> Optional[float]:
        """Компонент стоимости (ЗП/ЭМ/МР): значение из ответа API × объём.

        Текущее значение (curSalary / curOperationOfMachines /
        curCostOfMaterialResources); fallback — базовое значение
        (salary / operationOfMachines / costOfMaterialResources).
        None — если объём или стоимость за единицу не определены.
        """
        unit_value = safe_float(work.get(cur_key), default=0.0)
        if unit_value <= 0:
            unit_value = safe_float(work.get(base_key), default=0.0)
        if unit_value > 0 and volume_num > 0:
            return round(unit_value * volume_num, 2)
        return None

    def _empty_row(is_header: bool = False) -> Dict[str, Any]:
        return {col: "" for col in columns[:-1]} | {"_is_header": is_header}

    final_rows: List[Dict[str, Any]] = []
    for idx, element_entry in enumerate(result_elements):
        # Строка-заголовок группы элементов
        header_row = _empty_row(is_header=True)
        header_row["Наименование расценки/ресурса"] = _element_header(element_entry)
        final_rows.append(header_row)

        works = element_entry.get("selected_works") or []
        if not works:
            row = _empty_row()
            row["Наименование расценки/ресурса"] = "Работы не подобраны"
            final_rows.append(row)
        for work in works:
            volume_text = _volume_for_unit(
                work.get("quantity"), work.get("unit_of_measure")
            )
            volume_num = safe_float(volume_text, default=0.0)
            # Компоненты стоимости: значение из ответа API × объём работ
            zp = _component_cost(work, "cur_salary", "salary", volume_num)
            em = _component_cost(
                work, "cur_operation_of_machines", "operation_of_machines",
                volume_num,
            )
            mr = _component_cost(
                work, "cur_cost_of_material_resources",
                "cost_of_material_resources", volume_num,
            )
            # «Стоимость» = ЗП + ЭМ + МР (сумма вычисленных компонентов)
            parts = [v for v in (zp, em, mr) if v is not None]
            cost = round(sum(parts), 2) if parts else ""
            final_rows.append({
                "Шифр ТСН": work.get("pressmark") or "",
                "Наименование расценки/ресурса": work.get("title") or "",
                "Ед. изм.": work.get("unit_of_measure") or "",
                "Объём работ": volume_text,
                "ЗП": zp if zp is not None else "",
                "ЭМ": em if em is not None else "",
                "МР": mr if mr is not None else "",
                "Стоимость": cost,
                "_is_header": False,
            })

        # Пустая строка после каждой группы (кроме последней)
        if idx < len(result_elements) - 1:
            final_rows.append(_empty_row())

    if not final_rows:
        final_rows.append(_empty_row())
        final_rows[0]["Наименование расценки/ресурса"] = "Работы не подобраны"

    df = pd.DataFrame(final_rows, columns=columns)

    # Форматирование денежных колонок: разряды через пробел, 2 знака
    # после точки (например, '392 458.21') — как в режиме КР
    for col in ("ЗП", "ЭМ", "МР", "Стоимость"):
        df[col] = df[col].apply(format_money)

    # Итоговая строка «ИТОГО:» — суммы колонок ЗП, ЭМ, МР, Стоимость
    # (works_cost.add_total_row)
    df_for_excel = add_total_row(df.drop(columns=["_is_header"]))

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df_for_excel.to_excel(writer, sheet_name="Данные", index=False)
        worksheet = writer.sheets["Данные"]

        # Форматируем строки-заголовки групп (жирный шрифт, заливка) — как в КР
        from openpyxl.styles import Alignment, Font, PatternFill

        header_font = Font(bold=True, size=11)
        header_fill = PatternFill(
            start_color="D3D3D3", end_color="D3D3D3", fill_type="solid"
        )
        center_alignment = Alignment(horizontal="center", vertical="center")

        for row_idx in range(2, len(df) + 2):  # +2 из-за заголовка таблицы
            if df.iloc[row_idx - 2]["_is_header"]:
                for col_idx in range(1, len(df_for_excel.columns) + 1):
                    cell = worksheet.cell(row=row_idx, column=col_idx)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = center_alignment

        # Ширина колонок — как в КР
        worksheet.column_dimensions["A"].width = 15
        worksheet.column_dimensions["B"].width = 60
        worksheet.column_dimensions["C"].width = 10
        worksheet.column_dimensions["D"].width = 15
        worksheet.column_dimensions["E"].width = 15  # ЗП
        worksheet.column_dimensions["F"].width = 15  # ЭМ
        worksheet.column_dimensions["G"].width = 15  # МР
        worksheet.column_dimensions["H"].width = 15  # Стоимость

        # Автофильтр
        worksheet.auto_filter.ref = (
            f"A1:{chr(64 + len(df_for_excel.columns))}{len(df_for_excel) + 1}"
        )

    logger.info(f"Финальный перечень работ (Excel) сохранён: {xlsx_path}")
    return xlsx_path


# ======================================================================
#  CLI для отладки:
#      python -m src.services.works_final_selector <tables.json>
#      <works.json> <run_dir>
# ======================================================================

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(
            "Использование: python -m src.services.works_final_selector "
            "<путь_к_Подобранные_таблицы_работ.json> "
            "<путь_к_Подобранные_работы.json> <папка_запуска>"
        )
        sys.exit(1)

    _tables_path = sys.argv[1]
    _works_path = sys.argv[2]
    _run_dir = sys.argv[3]
    if not os.path.isfile(_tables_path):
        print(f"Файл не найден: {_tables_path}")
        sys.exit(1)
    if not os.path.isfile(_works_path):
        print(f"Файл не найден: {_works_path}")
        sys.exit(1)
    os.makedirs(_run_dir, exist_ok=True)

    _result = select_final_works(_tables_path, _works_path, _run_dir)
    if _result:
        print(f"OK: {_result}")
    else:
        print("Данные для подбора не найдены — шаг не выполнен")