"""Извлечение данных из документов (ВОР застройщика, ПОС, ПЗ, ПД).

Главный источник для нового функционала — **ВОР застройщика**: из него
берётся объём арматуры («Устройство арматурных каркасов», «Устройство
арматурных стержней») и объёмы прочих ресурсов/материалов, отсутствующих
в модели IFC. Остальные документы (ПОС, ПЗ, ПД) используются как
дополнительный источник объёмов, когда данных модели недостаточно.

Разбор ВОР выполняется детерминированно (:func:`doc_pdf_parser.extract_vor_rows`)
— быстро и без обращения к LLM. Разбор ПОС/ПЗ/ПД — существующими модулями
сервиса (``pd_parser``, ``doc_pdf_parser``) с использованием локальной LLM.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from src.core.logger import setup_logger
from . import mapping

logger = setup_logger(__name__)

Progress = Optional[Callable[[str], None]]

# Единицы измерения арматуры (тонны).
_TON_UNITS = ("т", "т.", "тонн", "тонна", "тн")
# Единицы объёма (м3).
_VOLUME_UNITS = ("м3", "м³", "куб.м", "куб.м.", "куб м")
# Единицы площади (м2).
_AREA_UNITS = ("м2", "м²", "кв.м", "кв.м.")
# Единицы длины (м).
_LENGTH_UNITS = ("м", "м.п.", "м.п", "пог.м", "пог.м.", "п.м.", "п.м")


def _say(progress: Progress, message: str) -> None:
    if progress is not None:
        try:
            progress(message)
        except Exception:  # pragma: no cover — прогресс не должен ломать разбор
            pass


def _pick_volume(now, was) -> Optional[float]:
    """Выбирает объём строки ВОР: приоритет «стало», затем «было»."""
    if now is not None and now > 0:
        return now
    if was is not None and was > 0:
        return was
    if now is not None:
        return now
    return was


def _norm_unit(unit: str) -> str:
    return (unit or "").strip().lower().replace(" ", "")


def _is_unit(unit: str, variants) -> bool:
    u = _norm_unit(unit).rstrip(".")
    return any(u == v.rstrip(".") for v in variants)


# =====================================================================
#  Арматура из ВОР застройщика
# =====================================================================

@dataclass
class RebarRow:
    section: str
    name: str
    diameter: Optional[int]
    rebar_class: Optional[str]
    volume: float
    group: str = ""                  # наименование родительской строки арматуры
    thickness: Optional[int] = None  # толщина конструкции блока, мм
    component: str = ""              # подтип лестницы: flight (марш)|landing (площадка)


@dataclass
class RebarData:
    """Данные об арматуре, извлечённые из ВОР застройщика."""

    rows: List[RebarRow] = field(default_factory=list)
    by_section: Dict[str, float] = field(default_factory=dict)
    by_diameter_class: Dict[Tuple[Optional[int], str], float] = field(default_factory=dict)
    by_class: Dict[str, float] = field(default_factory=dict)
    total: float = 0.0

    def scope_rows(self, kind: Optional[str], part: Optional[str],
                   thickness: Optional[int] = None, component: str = ""):
        """Строки арматуры, относящиеся к подразделу типового ВОР.

        ``kind``/``part`` — тип конструкции и часть здания (``mapping.vor_section_matches``);
        ``thickness`` — толщина конструкции (мм), если подраздел разбит на
        геометрические блоки; ``component`` — подтип лестницы (``flight``/
        ``landing``) для разделения арматуры маршей и площадок.
        """
        for row in self.rows:
            if kind is not None and not mapping.vor_section_matches(row.section, kind, part):
                continue
            if thickness is not None and row.thickness != thickness:
                continue
            if component and row.component != component:
                continue
            yield row

    def has_thickness(self, kind: Optional[str], part: Optional[str]) -> bool:
        """Есть ли у строк арматуры подраздела привязка к толщине конструкции."""
        return any(row.thickness for row in self.scope_rows(kind, part))

    def section_total(self, kind: Optional[str], part: Optional[str],
                      thickness: Optional[int] = None,
                      component: str = "") -> float:
        """Суммарный тоннаж арматуры по разделам ВОР, подходящим подразделу."""
        return sum(row.volume
                   for row in self.scope_rows(kind, part, thickness, component))

    def diameter_class(self, diameter: Optional[int], rebar_class: Optional[str],
                       kind: Optional[str] = None, part: Optional[str] = None,
                       thickness: Optional[int] = None,
                       component: str = "") -> float:
        """Тоннаж арматуры по диаметру и классу.

        При указании ``kind``/``part`` учитываются только разделы ВОР
        застройщика, относящиеся к соответствующему подразделу типового ВОР
        (:func:`mapping.vor_section_matches`) — иначе тоннаж «размазывается»
        по всем подразделам; ``thickness`` дополнительно ограничивает выборку
        геометрическим блоком (толщиной конструкции).
        """
        total = 0.0
        for row in self.scope_rows(kind, part, thickness, component):
            if diameter is not None and row.diameter != diameter:
                continue
            if rebar_class and row.rebar_class and row.rebar_class != rebar_class:
                continue
            total += row.volume
        return total

    def class_total(self, rebar_class: Optional[str]) -> float:
        """Тоннаж арматуры по классу."""
        if not rebar_class:
            return self.total
        return self.by_class.get(rebar_class, 0.0)

    def buckets(self, kind: Optional[str], part: Optional[str],
                thickness: Optional[int] = None,
                component: str = "") -> Dict[str, Dict[Tuple, float]]:
        """Раскладка арматуры по категориям работ.

        Возвращает словарь ``{'bars': {(diameter, class): тоннаж}, 'frames': {...}}``:
        ``bars`` — продольная/поперечная арматура («Арматура класса …»),
        ``frames`` — арматурные выпуски и каркасы («Арматурные выпуски …»).
        """
        result: Dict[str, Dict[Tuple, float]] = {"bars": {}, "frames": {}}
        for row in self.scope_rows(kind, part, thickness, component):
            bucket = mapping.rebar_bucket(row.group or row.name)
            key = (row.diameter, row.rebar_class or "")
            result[bucket][key] = result[bucket].get(key, 0.0) + row.volume
        return result


def rebar_buckets(rebar: Optional[RebarData], kind: Optional[str], part: Optional[str],
                  thickness: Optional[int] = None,
                  share: float = 1.0,
                  component: str = "") -> Dict[str, Dict[Tuple, float]]:
    """Раскладка арматуры по категориям с учётом толщины и доли группы.

    Если строки арматуры подраздела привязаны к толщине конструкции и известна
    толщина блока, тоннаж берётся точно по этому блоку (``share`` не применяется).
    Иначе тоннаж распределяется пропорционально доле группы (``share``).
    ``component`` — подтип лестницы (``flight``/``landing``).
    """
    if rebar is None or not rebar.rows:
        return {"bars": {}, "frames": {}}
    if thickness is not None and rebar.has_thickness(kind, part):
        buckets = rebar.buckets(kind, part, thickness, component)
        if buckets["bars"] or buckets["frames"]:
            return buckets
    result = rebar.buckets(kind, part, None, component)
    if share != 1.0:
        result = {bucket: {key: value * share for key, value in items.items()}
                  for bucket, items in result.items()}
    return result


def rebar_thickness(rebar: Optional[RebarData], kind: Optional[str], part: Optional[str],
                    thickness: Optional[int]) -> Optional[int]:
    """Толщина для точного отбора арматуры (``None`` — если данных по толщине нет)."""
    if thickness is None or rebar is None:
        return None
    return thickness if rebar.has_thickness(kind, part) else None


def rebar_sort_key(item: Tuple[Tuple[Optional[int], str], float]):
    """Порядок строк арматуры: по диаметру, затем по классу."""
    (diameter, rebar_class), _volume = item
    return (diameter if diameter is not None else 0, rebar_class or "")


def rebar_work_items(buckets: Dict[str, Dict[Tuple, float]], work_name: str,
                     has_bars_work: bool, has_frames_work: bool) -> Dict[Tuple, float]:
    """Строки арматуры для работы типового ВОР.

    Работа «…стержней» получает продольную/поперечную арматуру («Арматура
    класса …»), работа «…каркасов» — арматурные выпуски и каркасы
    («Арматурные выпуски …»). Если для категории работы данных нет, а работы
    другой категории в подразделе нет (двойного счёта не будет) — берётся
    другая категория.
    """
    bucket = mapping.rebar_bucket(work_name)
    items = {key: volume for key, volume in buckets.get(bucket, {}).items() if volume > 0}
    if not items:
        other = "frames" if bucket == "bars" else "bars"
        other_work = has_frames_work if bucket == "bars" else has_bars_work
        if not other_work:
            items = {key: volume for key, volume in buckets.get(other, {}).items() if volume > 0}
    return items


def rebar_label(diameter: Optional[int], rebar_class: Optional[str]) -> str:
    """Наименование строки-ресурса арматуры («Арматура d=10 мм А500С»)."""
    if diameter and rebar_class:
        return f"Арматура d={diameter} мм {rebar_class}"
    if diameter:
        return f"Арматура d={diameter} мм"
    if rebar_class:
        return f"Арматура класса {rebar_class}"
    return "Арматура"


def row_components(rows: List[dict]) -> Dict[Tuple[str, str, str], str]:
    """Подтип лестничной строки ВОР заказчика: ``(раздел, блок, позиция) → flight|landing``.

    У заказчика объёмы маршей и площадок идут отдельными строками
    («Лестничные марши подвала …», «Лестничные площадки подвала …»), а
    сопутствующие строки («Опалубка», «Арматура класса …») подтипа не содержат.
    Такие строки наследуют подтип от ближайшей предыдущей строки с явным
    подтипом в пределах раздела и блока.
    """
    result: Dict[Tuple[str, str, str], str] = {}
    current: Dict[Tuple[str, str], str] = {}
    for row in rows:
        section = str(row.get("section") or "").strip()
        block = str(row.get("block") or "").strip()
        key = (section, block)
        component = mapping.stair_component(str(row.get("name") or ""))
        if component:
            current[key] = component
        else:
            component = current.get(key, "")
        result[(section, block, str(row.get("pos") or "").strip())] = component
    return result


def extract_rebar(rows: List[dict]) -> RebarData:
    """Извлекает данные об арматуре из детерминированно разобранных строк ВОР.

    Учитываются как строки-родители («Арматура класса А500С»), так и их
    дочерние строки по диаметрам («D25», «D20», …), у которых в наименовании
    нет слова «арматура» — они наследуют класс и наименование группы от
    родителя. Родительские строки пропускаются, если у них есть дочерние
    (во избежание двойного счёта); при отсутствии дочерних строка учитывается
    сама.

    Каждой строке дополнительно присваивается **толщина конструкции** блока
    (из наименований строк блока — «… толщиной 1000мм …») — она используется
    для точного отнесения арматуры к геометрическим блокам типового ВОР.
    """
    data = RebarData()
    components = row_components(rows)

    # Толщина конструкции по (разделу, блоку) — последнее значение в блоке.
    thickness_map: Dict[Tuple[str, str], int] = {}
    for row in rows:
        thickness = mapping.parse_thickness(str(row.get("name") or ""))
        if thickness:
            key = (str(row.get("section") or "").strip(),
                   str(row.get("block") or "").strip())
            thickness_map[key] = thickness

    # Первый проход: позиции родительских строк-арматуры, их классы и наименования.
    # Ключ — (раздел, блок, позиция): нумерация позиций повторяется в каждом
    # разделе/блоке ВОР, поэтому глобальный ключ по позиции давал бы чужие классы
    # и наименования групп.
    rebar_parents: Dict[Tuple[str, str, str], Tuple[Optional[str], str]] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not mapping.is_rebar_text(name) or not _is_unit(row.get("unit") or "", _TON_UNITS):
            continue
        pos = str(row.get("pos") or "").strip()
        if pos:
            key = (str(row.get("section") or "").strip(),
                   str(row.get("block") or "").strip(), pos)
            rebar_parents[key] = (mapping.parse_rebar_spec(name)[1], name)

    # Позиции, у которых есть дочерние строки (позиция вида «N.k»).
    parents_with_children = set()
    for row in rows:
        pos = str(row.get("pos") or "").strip()
        if "." in pos:
            parents_with_children.add((str(row.get("section") or "").strip(),
                                       str(row.get("block") or "").strip(),
                                       pos.split(".", 1)[0]))

    last_class_by_section: Dict[str, str] = {}

    for row in rows:
        name = str(row.get("name") or "").strip()
        unit = row.get("unit") or ""
        if not _is_unit(unit, _TON_UNITS):
            continue
        pos = str(row.get("pos") or "").strip()
        section = str(row.get("section") or "").strip()
        block = str(row.get("block") or "").strip()
        parent_pos = pos.split(".", 1)[0] if "." in pos else None
        parent_key = (section, block, parent_pos) if parent_pos is not None else None
        is_rebar = mapping.is_rebar_text(name) or (parent_key is not None
                                                   and parent_key in rebar_parents)
        if not is_rebar:
            continue
        volume = _pick_volume(row.get("volume_now"), row.get("volume_was"))
        if volume is None:
            continue

        diameter, rebar_class = mapping.parse_rebar_spec(name)
        group = name
        if parent_key is not None and parent_key in rebar_parents:
            parent_class, parent_name = rebar_parents[parent_key]
            group = parent_name
            if rebar_class is None:
                rebar_class = parent_class
        if rebar_class is None:
            rebar_class = last_class_by_section.get(section)
        if mapping.is_rebar_text(name) and rebar_class and diameter is None:
            last_class_by_section[section] = rebar_class

        # Пропускаем родительскую строку, если у неё есть дочерние.
        if diameter is None and pos and (section, block, pos) in parents_with_children:
            continue

        thickness = thickness_map.get((section, block))
        data.rows.append(RebarRow(section=section, name=name, diameter=diameter,
                                  rebar_class=rebar_class, volume=volume,
                                  group=group, thickness=thickness,
                                  component=components.get((section, block, pos), "")))
        data.by_section[section] = data.by_section.get(section, 0.0) + volume
        key = (diameter, rebar_class or "")
        data.by_diameter_class[key] = data.by_diameter_class.get(key, 0.0) + volume
        if rebar_class:
            data.by_class[rebar_class] = data.by_class.get(rebar_class, 0.0) + volume
        data.total += volume

    logger.info("ВОР: арматура — строк %d, всего %.2f т", len(data.rows), data.total)
    return data


# =====================================================================
#  Документы (ПОС/ПЗ/ПД) и общий поиск ресурсов
# =====================================================================

@dataclass
class DocumentRow:
    name: str
    unit: str
    volume: Optional[float]
    section: str = ""
    source: str = ""      # 'ВОР'|'ПОС'|'ПЗ'|'ПД'
    file_name: str = ""


@dataclass
class Documents:
    """Разобранные документы задачи формирования ВОР."""

    rebar: RebarData = field(default_factory=RebarData)
    rows: List[DocumentRow] = field(default_factory=list)
    constants: Dict[str, object] = field(default_factory=dict)
    file_names: Dict[str, str] = field(default_factory=dict)

    def add_rows(self, rows: List[DocumentRow]) -> None:
        self.rows.extend(rows)

    def lookup(self, name: str, unit: str = "",
               exclude_sources=()) -> Optional[DocumentRow]:
        """Находит в документах строку ресурса, наиболее похожую на ``name``.

        Сопоставление — по доле общих значимых слов (токенов) наименования;
        токены сравниваются по основе (префиксу), чтобы учитывать падежи.
        Числовые токены (диаметры, размеры) должны совпадать, иначе строка
        не считается найденной. При равенстве приоритет у совпадающей единицы
        измерения.

        ``exclude_sources`` — источники, строки которых не учитываются (для
        типового ВОР ВОР застройщика исключается: из него берётся только
        арматура, см. §4.3).
        """
        query = _tokens(name)
        if not query:
            return None
        query_words = {t for t in query if not _is_num(t)}
        query_nums = {t for t in query if _is_num(t)}
        if not query_words and not query_nums:
            return None

        best: Optional[DocumentRow] = None
        best_score = 0.0
        for row in self.rows:
            if row.volume is None:
                continue
            if row.source in exclude_sources:
                continue
            tokens = _tokens(row.name)
            if not tokens:
                continue
            row_words = {t for t in tokens if not _is_num(t)}
            row_nums = {t for t in tokens if _is_num(t)}

            # Числа в запросе (диаметры/размеры) обязаны совпасть.
            if query_nums and not (query_nums & row_nums):
                continue

            common = _common_stems(query_words, row_words)
            if not common:
                continue
            score = len(common) / len(query_words) if query_words else 0.0
            if unit and row.unit and _norm_unit(unit)[:2] == _norm_unit(row.unit)[:2]:
                score += 0.25
            # Требуем либо совпадения всех значимых слов, либо не менее двух.
            if score < 0.6 or (len(common) < 2 and len(query_words) > 1):
                continue
            if score > best_score:
                best_score = score
                best = row
        return best


_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+")
_STOP = {"для", "или", "и", "в", "на", "с", "по", "из", "мм", "м2", "м3", "тип", "типа"}
# Длина основы токена для сравнения слов с учётом падежей/окончаний.
_STEM_LEN = 7


def _is_num(token: str) -> bool:
    """Токен, начинающийся с цифры (диаметр, размер, марка)."""
    return bool(token) and token[0].isdigit()


def _stem(token: str) -> str:
    return token[:_STEM_LEN] if len(token) > _STEM_LEN else token


def _tokens(text: str) -> set:
    tokens = set()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in _STOP:
            continue
        if not _is_num(token) and len(token) < 3:
            continue
        tokens.add(token)
    return tokens


def _common_stems(query_words: set, row_words: set) -> set:
    """Общие слова запроса и строки документа (сравнение по основе)."""
    stems = {_stem(w) for w in row_words}
    return {w for w in query_words if _stem(w) in stems}


# Публичные алиасы для переиспользования другими модулями пакета
# (приведение ВОР заказчика к типовому виду — `customer_vor.py`).
pick_volume = _pick_volume
norm_unit = _norm_unit
is_unit = _is_unit
tokens = _tokens
is_num = _is_num
common_stems = _common_stems


def load_documents(paths: Dict[str, str], use_llm: bool = True,
                   progress: Progress = None) -> Documents:
    """Разбирает загруженные документы и собирает данные для заполнения ВОР.

    ``paths`` — словарь ``{kind: путь}``, где ``kind`` ∈
    ``{'vor', 'pos', 'pz', 'pd'}``.
    """
    docs = Documents()
    labels = {"vor": "ВОР", "pos": "ПОС", "pz": "ПЗ", "pd": "ПД"}

    # --- ВОР застройщика: детерминированный разбор (без LLM) ----------
    vor_path = paths.get("vor")
    if vor_path and os.path.isfile(vor_path):
        docs.file_names["vor"] = os.path.basename(vor_path)
        _say(progress, "ВОР: разбор ведомости объёмов работ")
        try:
            from src.services.doc_pdf_parser import extract_vor_rows
            rows = extract_vor_rows(vor_path)
            docs.rebar = extract_rebar(rows)
            for row in rows:
                docs.add_rows([DocumentRow(
                    name=str(row.get("name") or ""),
                    unit=str(row.get("unit") or ""),
                    volume=_pick_volume(row.get("volume_now"), row.get("volume_was")),
                    section=str(row.get("section") or ""),
                    source="ВОР", file_name=docs.file_names["vor"],
                )])
            _say(progress, f"ВОР: строк — {len(rows)}")
        except Exception as exc:  # pragma: no cover — не роняем задачу
            logger.exception("ВОР: не удалось разобрать ведомость: %s", exc)

    # --- ПОС / ПЗ / ПД: существующие парсеры (LLM, при доступности) ----
    from src.core.config import load_config
    from src.services import pd_parser
    from src.services import doc_pdf_parser

    cfg = load_config()
    llm_config = pd_parser.Config(llm_base_url=cfg.ollama_url, llm_model=cfg.model_ollama)

    for kind in ("pos", "pz", "pd"):
        path = paths.get(kind)
        if not path or not os.path.isfile(path):
            continue
        docs.file_names[kind] = os.path.basename(path)
        label = labels[kind]
        _say(progress, f"{label}: разбор документа")
        try:
            if kind in ("pos", "pz"):
                result = pd_parser.parse_pos_constants(
                    path, use_llm=use_llm, config=llm_config,
                    progress=lambda m, l=label: _say(progress, f"{l}: {m}"),
                    document_label=label,
                )
                docs.constants[kind] = result
            else:
                result = doc_pdf_parser.parse_pd(
                    path, use_llm=use_llm, config=llm_config,
                    progress=lambda m, l=label: _say(progress, f"{l}: {m}"),
                )
                for item in result.get("items", []) if isinstance(result, dict) else []:
                    docs.add_rows([DocumentRow(
                        name=str(item.get("element") or ""),
                        unit="",
                        volume=None,
                        source=label, file_name=docs.file_names[kind],
                    )])
        except Exception as exc:  # pragma: no cover — не роняем задачу
            logger.warning("%s: разбор не выполнен: %s", label, exc)

    return docs
