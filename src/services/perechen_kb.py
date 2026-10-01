"""
Подбор работ по перечню сметчиков (perechen_kr.xlsx, лист «ВОР КР+расценки»).

Структура перечня:
  Часть здания → Раздел → Подраздел (конструкция) → Группа → Работа → расценки/ресурсы.

Подбор для элемента (pick_for_element):
  0) сборный ж/б пропускается (в перечне КР только монолит);
  1) LLM выбирает конструкцию (подраздел) в пределах части здания;
  2) фильтры кодом: высота здания (по названиям таблиц), толщина элемента;
  3) LLM выбирает работы; 4) LLM-контролёр проверяет выбор;
  5) правило: при установке арматуры — все диаметры из перечня;
  6) решение кэшируется по «подписи» элемента (data/kb_cache.json).

Команды:
    python -m src.services.perechen_kb list            # структура перечня
    python -m src.services.perechen_kb [N] [nocache]   # тест на последнем запуске
"""
import glob
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from src.core.logger import setup_logger

logger = setup_logger(__name__)

PERECHEN_XLSX = os.getenv("PERECHEN_KR_PATH", "/app/data/perechen_kr.xlsx")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
KB_MODEL = os.getenv("KB_LLM_MODEL", os.getenv("LLM_MODEL", "qwen3.8:27b"))
TREE_JSON = os.getenv("TREE_WORK_PATH", "/app/data/tree_work_compact.json")
CACHE_PATH = os.getenv("KB_CACHE_PATH", "/app/data/kb_cache.json")
CACHE_VERSION = 18

PARTS = ("Подземная", "Цоколь", "Надземная")


# =====================================================================
#  РАЗБОР ПЕРЕЧНЯ
# =====================================================================

def _clean(v: Any) -> str:
    return "" if pd.isna(v) else str(v).replace("\n", " / ").strip()


def _part_in(title: str) -> Optional[str]:
    t = title.lower()
    if "подзем" in t:
        return "Подземная"
    if "цокол" in t:
        return "Цоколь"
    if "надзем" in t:
        return "Надземная"
    return None


def _header_title(num: str, name: str) -> str:
    if num and name:
        if re.fullmatch(r"[\d.\s]+", num):
            return f"{num.strip().rstrip('.')}. {name}"
        return f"{num} {name}"
    return num or name


def _kind(title: str) -> str:
    t = title.lower().strip()
    if t.startswith("подраздел"):
        return "subsection"
    if t.startswith("раздел"):
        return "section"
    if re.match(r"^\d", t):
        return "group"
    if "част" in t and _part_in(t):
        return "part"
    return "group"


def load_perechen(path: str = PERECHEN_XLSX) -> List[Dict[str, Any]]:
    df = pd.read_excel(path)
    subs: List[Dict[str, Any]] = []
    part, section = "Надземная", ""
    current: Optional[Dict[str, Any]] = None
    group, work = "", None

    def new_sub(title: str) -> Dict[str, Any]:
        s = {"id": len(subs), "part": part, "section": section, "title": title, "works": []}
        subs.append(s)
        return s

    for _, r in df.iterrows():
        code = _clean(r.get("Шифр ТСН"))
        num, name = _clean(r.get("№ п/п")), _clean(r.get("Наименование работ"))
        if not code:
            title = _header_title(num, name)
            if not title:
                continue
            kind = _kind(title)
            if kind in ("part", "section", "subsection"):
                part = _part_in(title) or part
            if kind == "part":
                section, current = "", None
            elif kind == "section":
                section, current = title, None
            elif kind == "subsection":
                current = new_sub(title)
            else:
                group = title
            if kind != "group":
                group = ""
            work = None
            continue

        if current is None:
            current = new_sub(section or f"Работы ({part.lower()} часть)")
        rate = {
            "pressmark": code,
            "title": _clean(r.get("Наименование расценки/ресурса")),
            "unit": _clean(r.get("Ед. изм.")),
            "formula": _clean(r.get("Формула расчёта объёмов работ и расхода материалов")),
            "is_resource": not code.startswith("3."),
        }
        if name or work is None:
            work = {"wid": len(current["works"]), "group": group,
                    "name": name or rate["title"],
                    "ifc_class": _clean(r.get("IFC класс")),
                    "param": _clean(r.get("Параметризация")), "rates": []}
            current["works"].append(work)
        work["rates"].append(rate)
    subs = [s for s in subs if s["works"]]
    for i, s in enumerate(subs):
        s["id"] = i
    return subs


def _groups(sub: Dict[str, Any]) -> List[str]:
    seen: List[str] = []
    for w in sub["works"]:
        g = re.sub(r"^[\d.\s]+", "", w["group"]).strip()
        if g and g not in seen:
            seen.append(g)
    return seen


def _is_rebar_diameter(w: Dict[str, Any]) -> bool:
    return w["name"].lower().startswith("арматура ")


# =====================================================================
#  LLM
# =====================================================================

KB_THINK = os.getenv("KB_THINK", "1").strip() == "1"

# Режим «размышления» тратит почти весь лимит на reasoning: при num_predict=4096
# ответ обрывался (done_reason=length) и приходил пустым, поэтому лимиты выше.
KB_NUM_PREDICT = int(os.getenv("KB_NUM_PREDICT", "1024"))
KB_THINK_NUM_PREDICT = int(os.getenv("KB_THINK_NUM_PREDICT", "16000"))
KB_TIMEOUT = int(os.getenv("KB_TIMEOUT", "180"))
KB_THINK_TIMEOUT = int(os.getenv("KB_THINK_TIMEOUT", "900"))


def _ask_llm(prompt: str, think: bool = False) -> Dict[str, Any]:
    """Запрос к qwen: лимит длины ответа, таймаут, повтор без format=json."""
    import ollama
    num_predict = KB_THINK_NUM_PREDICT if think else KB_NUM_PREDICT
    client = ollama.Client(host=OLLAMA_URL, timeout=KB_THINK_TIMEOUT if think else KB_TIMEOUT)
    base = dict(model=KB_MODEL, messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0, "num_predict": num_predict})

    def _parse(text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        try:
            return json.loads(text)
        except ValueError:
            m = re.search(r"\{.*\}", text, re.S)
            try:
                return json.loads(m.group(0)) if m else {}
            except ValueError:
                return {}

    for use_json in (True, False):
        kwargs = dict(base, format="json") if use_json else dict(base)
        try:
            try:
                resp = client.chat(think=think, **kwargs)
            except TypeError:
                resp = client.chat(**kwargs)
            content = resp["message"]["content"]
            if resp.get("done_reason") == "length" or not (content or "").strip():
                logger.warning(
                    f"LLM-ответ обрезан или пуст (think={think}, num_predict={num_predict}, "
                    f"done_reason={resp.get('done_reason')}, eval_count={resp.get('eval_count')})")
                if resp.get("done_reason") == "length":
                    break   # тот же промпт при том же лимите снова не поместится
            result = _parse(content)
            if result:
                return result
        except Exception as exc:
            logger.warning(f"LLM-запрос не удался (json={use_json}): {exc}")
    return {}


def _ids(values: Any) -> set:
    return {int(i) for i in (values or []) if str(i).strip().isdigit()}


# =====================================================================
#  ФИЛЬТРЫ КОДОМ: высота здания, толщина, сборный ж/б
# =====================================================================

def _parse_range(title: str, keyword: str, span: int = 45):
    """«…<keyword> от 30 до 40 м» -> (30.0, 40.0); «до 30» -> (None, 30.0)."""
    t = title.lower().replace(",", ".")
    i = t.find(keyword)
    if i < 0:
        return None
    seg = t[i + len(keyword): i + len(keyword) + span]
    lo = re.search(r"(?:от|свыше|более|больше)\s+(\d+(?:\.\d+)?)", seg)
    hi = re.search(r"до\s+(\d+(?:\.\d+)?)", seg)
    if not lo and not hi:
        return None
    k = 10.0 if re.search(r"\d\s*см", seg) and "мм" not in seg else 1.0
    return (float(lo.group(1)) * k if lo else None, float(hi.group(1)) * k if hi else None)


def _in_range(rng, v: float) -> bool:
    lo, hi = rng
    return (lo is None or v > lo) and (hi is None or v <= hi)


def _table_of(pressmark: str) -> str:
    return pressmark.rsplit("-", 1)[0] if pressmark.count("-") >= 2 else pressmark


_table_heights: Optional[Dict[str, Any]] = None
_full_titles: Optional[Dict[str, str]] = None


def table_heights() -> Dict[str, Any]:
    """{шифр таблицы: (от, до)} — диапазон высоты здания из названия таблицы."""
    global _table_heights
    if _table_heights is None:
        _table_heights = {}
        rx = re.compile(r"Таблица\s+(\d+\.\d+-\d+)\.")
        for c in json.load(open(TREE_JSON, encoding="utf-8")):
            for d in c.get("departments", []):
                for p in d.get("parts", []):
                    for t in p.get("tables", []):
                        m = rx.match(t.get("table", ""))
                        rng = _parse_range(t["table"], "высоте здания") if m else None
                        if rng:
                            _table_heights[m.group(1)] = rng
    return _table_heights


def _full_title(rate: Dict[str, Any]) -> str:
    """Полное наименование расценки из локального справочника (в перечне бывает сокращено)."""
    global _full_titles
    if _full_titles is None:
        _full_titles = {}
        try:
            from src.services.local_works import load_local_works
            for works in load_local_works()["tables"].values():
                for w in works:
                    _full_titles[w["pressmark"]] = w.get("title") or ""
        except Exception as exc:
            logger.warning(f"Локальный справочник недоступен: {exc}")
    return _full_titles.get(rate["pressmark"]) or rate["title"]


def filter_by_height(sub: Dict[str, Any], height: Optional[float]) -> Dict[str, Any]:
    if not height:
        return sub
    heights = table_heights()
    works = []
    for w in sub["works"]:
        kept = [r for r in w["rates"]
                if heights.get(_table_of(r["pressmark"])) is None
                or _in_range(heights[_table_of(r["pressmark"])], height)]
        works.append(dict(w, rates=kept or w["rates"]))
    return dict(sub, works=works)


def element_thickness(el: Dict[str, Any]) -> Optional[float]:
    for src in (el.get("name"), el.get("material")):
        m = re.search(r"(\d{2,4})\s*мм", str(src or ""))
        if m:
            return float(m.group(1))
    return None


def filter_by_thickness(sub: Dict[str, Any], thickness: Optional[float]) -> Dict[str, Any]:
    """Варианты расценок по толщине конструкции: остаётся подходящий диапазон."""
    if not thickness:
        return sub
    works = []
    for w in sub["works"]:
        ranged = [(r, _parse_range(_full_title(r), "толщин")) for r in w["rates"]]
        if sum(1 for r, g in ranged if g and not r["is_resource"]) >= 2:
            kept = [r for r, g in ranged if g is None or _in_range(g, thickness)]
            if any(not r["is_resource"] for r in kept):
                w = dict(w, rates=kept)
        works.append(w)
    return dict(sub, works=works)


def is_precast(el: Dict[str, Any]) -> bool:
    text = " ".join(str(el.get(k) or "") for k in ("material", "construction_method", "name")).lower()
    return "сборн" in text or "precast" in text


def building_height_for_run(run_json_path: str, data: Dict[str, Any]) -> Optional[float]:
    session_dir = os.path.dirname(os.path.dirname(run_json_path))
    try:
        c = json.load(open(os.path.join(session_dir, "IFC_глобальные_константы.json"),
                           encoding="utf-8")).get("constants")
        item = c.get("building_height_m") if isinstance(c, dict) else \
            next((x for x in c if x.get("name") == "building_height_m"), None)
        if item and item.get("value") is not None:
            return float(item["value"])
    except Exception:
        pass
    try:
        return float(str((data.get("global_constants") or {}).get("building_height_m")).replace(",", "."))
    except (TypeError, ValueError):
        return None


# =====================================================================
#  ШАГИ LLM
# =====================================================================

_mssk_map: Optional[Dict[str, str]] = None
_MSSK_CODE = re.compile(r"ЭЛ[\s\d]+")


def mssk_name(code: Any) -> str:
    """Название элемента по коду МССК (data/elements_mssk_nested.json), с укорачиванием кода."""
    global _mssk_map
    if _mssk_map is None:
        _mssk_map = {}
        path = os.getenv("MSSK_NESTED_PATH", "/app/data/elements_mssk_nested.json")

        def norm(c: str) -> str:
            return re.sub(r"\s+", " ", c).strip()

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                code_v = next((v for v in o.values() if isinstance(v, str) and _MSSK_CODE.fullmatch(v.strip())), None)
                name_v = next((v for k, v in o.items() if isinstance(v, str)
                               and str(k).lower() in ("name", "title", "наименование", "название")), None)
                if code_v and name_v:
                    _mssk_map.setdefault(norm(code_v), name_v.strip())
                for k, v in o.items():
                    if _MSSK_CODE.fullmatch(str(k).strip()):
                        if isinstance(v, str):
                            _mssk_map.setdefault(norm(k), v.strip())
                        elif isinstance(v, dict):
                            nm = v.get("name") or v.get("title") or v.get("наименование")
                            if isinstance(nm, str):
                                _mssk_map.setdefault(norm(k), nm.strip())
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        try:
            from src.services.mssk_lookup import build_mssk_lookup
            lookup = build_mssk_lookup()
            if isinstance(lookup, tuple):
                lookup = lookup[0]
            for k, v in (lookup or {}).items():
                nm = v.get("name") if isinstance(v, dict) else v
                if isinstance(nm, str) and nm.strip():
                    _mssk_map[norm(str(k))] = nm.strip()
        except Exception as exc:
            logger.warning(f"mssk_lookup недоступен: {exc}")
        if not _mssk_map:
            try:
                walk(json.load(open(path, encoding="utf-8")))
            except Exception as exc:
                logger.warning(f"Справочник МССК недоступен: {exc}")
    c = re.sub(r"\s+", " ", str(code or "")).strip()
    while c:
        if c in _mssk_map:
            return _mssk_map[c]
        if " " not in c:
            break
        c = c.rsplit(" ", 1)[0]
    return ""


def enrich(el: Dict[str, Any]) -> Dict[str, Any]:
    """Дополняет элемент названием по МССК."""
    if el.get("mssk_code") and not el.get("mssk_name"):
        return dict(el, mssk_name=mssk_name(el["mssk_code"]))
    return el


def _element_block(el: Dict[str, Any], part: str) -> str:
    th = element_thickness(el)
    return (
        f"- наименование: {el.get('name')}\n"
        f"- IFC-класс: {el.get('ifc_class')} ({el.get('predefined_type') or '-'})\n"
        f"- код МССК: {el.get('mssk_code') or '-'}\n"
        f"- элемент по МССК: {el.get('mssk_name') or '-'}\n"
        f"- материал: {el.get('material') or '-'}\n"
        f"- способ возведения: {el.get('construction_method') or '-'}\n"
        f"- часть здания: {part}\n"
        + (f"- толщина: {th:.0f} мм\n" if th else "")
    )


def _work_line(w: Dict[str, Any]) -> str:
    rates = "; ".join(r["title"][:60] for r in w["rates"] if not r["is_resource"])
    grp = re.sub(r"^[\d.\s]+", "", w["group"]).strip()
    extra = [x for x in (w["ifc_class"] and f"IFC: {w['ifc_class'][:50]}",
                         w["param"] and f"параметр: {w['param'][:50]}") if x]
    mats = "; ".join(r["title"][:60] for r in w["rates"] if r["is_resource"])
    return (f'{w["wid"]}: [{grp or "-"}] {w["name"][:80]} — {rates[:160]}'
            + (f' | материалы: {mats[:140]}' if mats else "")
            + (f' ({", ".join(extra)})' if extra else ""))


def select_subsection(el: Dict[str, Any], part: str,
                      subs: List[Dict[str, Any]]) -> Dict[str, Any]:
    allowed = {part} | ({"Подземная"} if part == "Цоколь" else set())
    if "фундамент" in f"{el.get('name') or ''} {el.get('mssk_name') or ''}".lower():
        allowed.add("Подземная")   # фундаменты описаны в перечне в подземной части
    options = [s for s in subs if s["part"] in allowed] or subs
    if not _is_concrete(el):
        lk = _layer_kind(el)
        options = [s for s in options
                   if any(not _is_concrete_work(w) and not _is_rebar_diameter(w)
                          and (lk is None or _work_layer(w) == lk) for w in s["works"])]
        if not options:
            return {"subsection": None,
                    "reason": f"в перечне для этой части здания нет работ слоя «{lk or 'не бетон'}»"}
    lines = []
    for s in options:
        names: List[str] = []
        for w in s["works"]:
            if _is_rebar_diameter(w):
                continue
            n = (re.sub(r"^[\d.\s]+", "", w["group"]).strip() or w["name"])[:45]
            if n not in names:
                names.append(n)
        lines.append(f'{s["id"]}: {s["section"]} → {s["title"]}  [состав: {"; ".join(names[:14])}]')
    prompt = (
        "Ты опытный сметчик. Определи, к какой конструкции (подразделу перечня работ) "
        "относится элемент BIM-модели. Смотри прежде всего на наименование и материал: "
        "IFC-класс в модели часто неточен (мембраны и утеплители бывают смоделированы "
        "как стены, перекрытия — как балки). Слои (гидроизоляция, утеплитель) относятся "
        "к той конструкции, в составе которой они перечислены.\n\n"
        "Элемент:\n" + _element_block(el, part) + "\n"
        "Подразделы (номер: раздел → подраздел [состав]):\n" + "\n".join(lines) + "\n\n"
        'Ответь строго JSON: {"id": <номер или null>, "reason": "<кратко почему>"}'
    )
    ans: Dict[str, Any] = {}
    for attempt in range(2):
        ans = _ask_llm(prompt)
        chosen = next((s for s in options if s["id"] == ans.get("id")), None)
        if chosen or ans.get("reason"):
            return {"subsection": chosen, "reason": ans.get("reason", "")}
    return {"subsection": None, "reason": "модель не ответила"}


def select_works(el: Dict[str, Any], part: str, sub: Dict[str, Any]) -> Dict[str, Any]:
    visible = [w for w in sub["works"] if not _is_rebar_diameter(w)]
    if not _is_concrete(el):
        visible = [w for w in visible if not _is_concrete_work(w)] or visible
        lk = _layer_kind(el)
        if lk:
            visible = [w for w in visible if _work_layer(w) == lk] or visible
    prompt = (
        f"Ты опытный сметчик. Конструкция: «{sub['title']}». Элемент BIM-модели — это "
        "ОДИН слой или одна часть этой конструкции. Выбери работы, которые относятся "
        "именно к этому элементу.\n\n"
        "Правила:\n"
        "- в квадратных скобках — группа работ; бери работы только из групп, "
        "относящихся к элементу;\n"
        "- взаимоисключающие варианты (разные материалы, разные типы опалубки или "
        "гидроизоляции, разные классы бетона): выбери ОДИН, подходящий по материалу "
        "и наименованию элемента;\n"
        "- для бетонного элемента включай опалубку, установку арматуры, бетонирование, "
        "бетон и уход за бетоном, даже если они «не моделируются»;\n"
        "- название работы из перечня важнее названия расценки: сметчики сопоставили их "
        "сознательно, даже если расценка названа шире или иначе (например, термовкладыши "
        "из ЭППС по расценке для волокнистых материалов);\n"
        "- работы по другим слоям, которые моделируются отдельными элементами, не включай.\n"
        "Справка по материалам: Техноэласт, Бикрост, Унифлекс, Линокром, Изопласт, ЭПП/ЭКП — "
        "рулонные наплавляемые битумно-полимерные; ВИЛЛАДРЕЙН, Planter, Тефонд — профилированные "
        "(дренажные) мембраны; ПВХ-мембраны (Logicroof, Sikaplan) — полимерные мембраны; "
        "ППС, ЭППС, XPS, Пеноплэкс, Техноплекс — плиты пенополистирола; Rockwool, Техноблок, "
        "минвата — минераловатные плиты.\n\n"
        "Элемент:\n" + _element_block(el, part) + "\n"
        "Работы (номер: [группа] работа — расценки):\n"
        + "\n".join(_work_line(w) for w in visible) + "\n\n"
        'Ответь строго JSON: {"works": [<номера работ>], "reason": "<кратко почему>"}'
    )
    ans: Dict[str, Any] = {}
    for attempt in range(3):
        ans = _ask_llm(prompt if attempt == 0 else prompt +
                       "\n\nВАЖНО: в списке works должна быть хотя бы одна работа.",
                       think=(attempt == 2))
        ids = _ids(ans.get("works"))
        chosen = [w for w in visible if w["wid"] in ids]
        if chosen:
            return {"works": chosen, "reason": ans.get("reason", "")}
    return {"works": [], "reason": ans.get("reason", "") or "модель не вернула работ"}


def review_selection(el: Dict[str, Any], part: str, sub: Dict[str, Any],
                     chosen: List[Dict[str, Any]]):
    """Контролёр: убирает взаимоисключающие/чужие работы, добавляет пропущенные."""
    chosen_ids = {w["wid"] for w in chosen}
    visible = [w for w in sub["works"] if not _is_rebar_diameter(w)]
    selected = "\n".join(_work_line(w) for w in visible if w["wid"] in chosen_ids)
    rest = "\n".join(_work_line(w) for w in visible if w["wid"] not in chosen_ids) or "(нет)"
    prompt = (
        "Ты — главный сметчик и проверяешь работу коллеги. Для элемента BIM-модели "
        "выбраны работы. Проверь:\n"
        "1) нет ли среди выбранных взаимоисключающих вариантов (например, обычная и "
        "несъёмная опалубка, два типа гидроизоляции, два класса бетона) — оставь один, "
        "подходящий элементу;\n"
        "2) не выбраны ли работы для другой конструкции или другого слоя;\n"
        "3) не пропущены ли обязательные для этого элемента работы. Работы с пометкой "
        "IFC «не моделируется» (опалубка, арматура, закладные, гидрошпонки, уплотнители швов "
        "и т.п.) — ОБЯЗАТЕЛЬНАЯ часть бетонной конструкции: их нет в модели отдельными "
        "элементами, поэтому для бетонного элемента их нужно добавить;\n"
        "4) соответствует ли выбор материалу и классу элемента.\n"
        "Подумай как сметчик, который составляет полную смету на этот элемент: чего "
        "не хватает и что лишнее.\n"
        "ВАЖНО: несколько расценок внутри одной позиции (например, съёмная и несъёмная "
        "опалубка) — это НЕ ошибка, конкретная расценка выбирается позже; не удаляй "
        "позицию из-за этого. Не добавляй работы по слоям, которые моделируются "
        "отдельными элементами (бетонная подготовка, гидроизоляция, утеплитель, стяжка).\n"
        "Если всё верно — верни пустые списки.\n\n"
        "Элемент:\n" + _element_block(el, part) +
        f"\nКонструкция: «{sub['title']}»\n\nВыбрано:\n{selected}\n\nНе выбрано:\n{rest}\n\n"
        'Ответь строго JSON: {"remove": [<номера>], "add": [<номера>], "reason": "<кратко>"}'
    )
    ans = _ask_llm(prompt, think=KB_THINK)
    valid = {w["wid"] for w in visible}
    names = {w["wid"]: w["name"].lower() for w in visible}
    blocked = ("подготов", "гидроизол", "утепл", "теплоизол", "стяжк", "пароизол") if _is_concrete(el) else ()
    added = {i for i in _ids(ans.get("add")) & valid
             if not any(b in names.get(i, "") for b in blocked)}
    new_ids = (chosen_ids - _ids(ans.get("remove"))) | added
    if not _is_concrete(el):
        lk = _layer_kind(el)
        by = {w["wid"]: w for w in visible}
        new_ids = {i for i in new_ids if i in chosen_ids or lk is None or _work_layer(by[i]) == lk}
        main = [w for w in visible if w["wid"] in new_ids and (lk is None or _work_layer(w) == lk)
                and not re.search(r"праймер|огрунт", w["name"].lower())]
        if not main:
            return chosen, "контролёр оставил бы только вспомогательные работы — выбор сохранён"
    if _is_concrete(el):  # обязательные работы бетонного элемента контролёр не удаляет
        new_ids |= {w["wid"] for w in chosen
                    if any(k in w["name"].lower() for k in ("опалуб", "бетонир", "арматур"))}
    if not new_ids:
        return chosen, "контролёр предложил убрать всё — оставлен исходный выбор"
    changed = new_ids != chosen_ids
    reason = ans.get("reason", "") if changed else ""
    return [w for w in sub["works"] if w["wid"] in new_ids], reason


_NON_CONCRETE_RX = re.compile(
    r"утепл|изол|мембран|эппс|ппс|пенопол|пеноплэкс|минват|минерал|геотекст|вилладрейн|техноэласт|дренаж")
_STRUCTURAL = {"IfcSlab", "IfcWall", "IfcWallStandardCase", "IfcBeam", "IfcColumn", "IfcStair",
               "IfcStairFlight", "IfcFooting", "IfcPile", "IfcRamp", "IfcMember"}


_INSUL_RX = re.compile(r"утепл|теплоизол|пенопол|полистир|эппс|ппс|пеноплэкс|техноплекс|xps|"
                       r"минват|минерал|термовклад|rockwool")
_WATER_RX = re.compile(r"гидроизол|техноэласт|бикрост|унифлекс|мембран|вилладрейн|planter|тефонд|"
                       r"мастик|огрунт|праймер|рулон")


def _layer_kind(el: Dict[str, Any]) -> Optional[str]:
    """Вид слоя небетонного элемента: insulation / waterproofing / None."""
    text = f"{el.get('material') or ''} {el.get('name') or ''}".lower()
    ins, wat = bool(_INSUL_RX.search(text)), bool(_WATER_RX.search(text))
    if ins != wat:
        return "insulation" if ins else "waterproofing"
    mssk = str(el.get("mssk_name") or "").lower()
    if _INSUL_RX.search(mssk):
        return "insulation"
    if _WATER_RX.search(mssk):
        return "waterproofing"
    return None


def _work_layer(w: Dict[str, Any]) -> Optional[str]:
    text = (w["name"] + " " + " ".join(r["title"] for r in w["rates"] if not r["is_resource"])).lower()
    if _INSUL_RX.search(text):
        return "insulation"
    if _WATER_RX.search(text):
        return "waterproofing"
    return None


def _is_concrete(el: Dict[str, Any]) -> bool:
    text = f"{el.get('material') or ''} {el.get('name') or ''} {el.get('mssk_name') or ''}".lower()
    if _NON_CONCRETE_RX.search(text):
        return False
    if any(k in text for k in ("бетон", "железобет", "_жб", " жб", "ж/б")):
        return True
    # материал не задан, но элемент несущий — в модели КР это железобетон
    return not str(el.get("material") or "").strip() and el.get("ifc_class") in _STRUCTURAL


_CONCRETE_WORK_WORDS = ("бетон", "опалуб", "арматур", "закладн")


_CONCRETE_WORK_RX = re.compile(
    r"бетонир|опалуб|арматур|закладн|смес\w* бетон|бетонной подготовк|уход за бетон")


def _is_concrete_work(w: Dict[str, Any]) -> bool:
    """Работа по бетону: по названию работы и расценок (без учёта материалов-ресурсов)."""
    text = (w["name"] + " " + " ".join(r["title"] for r in w["rates"] if not r["is_resource"])).lower()
    return bool(_CONCRETE_WORK_RX.search(text)) or w["name"].lower().startswith("бетон ")


def _attach_parent_works(sub: Dict[str, Any], chosen: List[Dict[str, Any]]):
    """Материал без своей работы: добавить работу, за которой он идёт в перечне."""
    ids = {w["wid"] for w in chosen}
    works = sub["works"]
    for w in chosen:
        if w["rates"] and all(r["is_resource"] for r in w["rates"]) and not _is_rebar_diameter(w):
            for prev in reversed(works[:w["wid"]]):
                if any(not r["is_resource"] for r in prev["rates"]):
                    ids.add(prev["wid"])
                    break
    return [w for w in works if w["wid"] in ids]


def _apply_rebar_rule(sub: Dict[str, Any], chosen: List[Dict[str, Any]]):
    """При выбранной установке арматуры — все диаметры арматуры из перечня конструкции."""
    has_rebar = any("арматур" in w["name"].lower() and not _is_rebar_diameter(w) for w in chosen)
    if not has_rebar:
        return chosen
    ids = {w["wid"] for w in chosen} | {w["wid"] for w in sub["works"] if _is_rebar_diameter(w)}
    return [w for w in sub["works"] if w["wid"] in ids]


# =====================================================================
#  КЭШ И ОСНОВНОЙ ВХОД
# =====================================================================

def type_key(name: Any) -> str:
    """«Лестницы 230:Лестницы 1:3325181» -> «Лестницы:Лестницы» (без номеров экземпляров)."""
    parts = [p.strip() for p in str(name or "").split(":")]
    parts = [re.sub(r"\s+\d+$", "", p) for p in parts if p and not p.isdigit()]
    return ":".join(parts)


def _signature(el: Dict[str, Any], part: str, height: Optional[float]) -> str:
    stamp = int(os.path.getmtime(PERECHEN_XLSX)) if os.path.exists(PERECHEN_XLSX) else 0
    key = json.dumps([CACHE_VERSION, stamp, KB_MODEL, part, type_key(el.get("name")),
                      el.get("ifc_class"), el.get("material"), el.get("mssk_code"), height],
                     ensure_ascii=False)
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _load_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)


def _filtered(sub: Dict[str, Any], el: Dict[str, Any], height: Optional[float]) -> Dict[str, Any]:
    return filter_by_thickness(filter_by_height(sub, height), element_thickness(el))


def select_rates(el: Dict[str, Any], part: str, chosen: List[Dict[str, Any]]):
    """Выбор конкретных расценок внутри работ, где их несколько (варианты одной операции)."""
    multi_ids = {w["wid"] for w in chosen
                 if sum(1 for r in w["rates"] if not r["is_resource"]) >= 2}
    if not multi_ids:
        return chosen, ""
    blocks = []
    for w in chosen:
        if w["wid"] not in multi_ids:
            continue
        lines = [f"  {w['wid']}.{i}: {r['pressmark']} — {_full_title(r)[:170]}"
                 for i, r in enumerate(w["rates"]) if not r["is_resource"]]
        blocks.append(f"Работа «{w['name'][:80]}»:\n" + "\n".join(lines))
    prompt = (
        "Ты опытный сметчик. Для элемента BIM-модели выбраны работы, но у некоторых "
        "из них несколько расценок. Оставь нужные.\n\n"
        "Правила:\n"
        "- разные операции одной работы (монтаж и демонтаж опалубки, установка и "
        "снятие) — нужны ОБЕ;\n"
        "- варианты одной операции (разные толщины, размеры, условия, типы опалубки — "
        "обычная или несъёмная, разные способы) — оставь ОДИН, подходящий элементу;\n"
        "- если по данным элемента вариант выбрать нельзя, оставь самый типичный "
        "для такой конструкции.\n\n"
        "Элемент:\n" + _element_block(el, part) + "\n"
        + "\n\n".join(blocks) + "\n\n"
        'Ответь строго JSON: {"keep": ["<номер вида 0.1>", ...], "reason": "<кратко>"}'
    )
    ans = _ask_llm(prompt)
    keep = {str(k).strip() for k in (ans.get("keep") or [])}
    result = []
    for w in chosen:
        if w["wid"] not in multi_ids:
            result.append(w)
            continue
        sel = [r for i, r in enumerate(w["rates"])
               if r["is_resource"] or f"{w['wid']}.{i}" in keep]
        if not any(not r["is_resource"] for r in sel):
            sel = w["rates"]
        # страховка: из одной таблицы внутри работы — не больше одной расценки
        seen_tables, deduped = set(), []
        for r in sel:
            t = _table_of(r["pressmark"])
            if not r["is_resource"]:
                if t in seen_tables:
                    continue
                seen_tables.add(t)
            deduped.append(r)
        result.append(dict(w, rates=deduped))
    return result, ans.get("reason", "")


def _works_from_rates(rates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"wid": i, "group": "", "name": r["title"][:120], "ifc_class": "", "param": "",
             "rates": [{"pressmark": r["pressmark"], "title": r["title"], "unit": r.get("unit", ""),
                        "formula": "", "is_resource": not r["pressmark"].startswith("3.")}]}
            for i, r in enumerate(rates)]


def _search_sub(part: str, works: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": -1, "part": part, "section": "Полный справочник ТСН",
            "title": "подбор по справочнику", "works": works}


def _search_candidates(keywords: List[str], height: Optional[float], limit: int = 40):
    """Расценки локального справочника, в названиях которых больше всего ключевых слов."""
    from src.services.local_works import load_local_works
    stems = [k.lower().strip()[:7] for k in keywords if len(k.strip()) >= 3]
    heights = table_heights()
    scored = []
    for tcode, works in load_local_works()["tables"].items():
        rng = heights.get(tcode)
        if height and rng and not _in_range(rng, height):
            continue
        for w in works:
            t = (w.get("title") or "").lower()
            score = sum(1 for st in set(stems) if st in t)
            if score:
                scored.append((score, w))
    scored.sort(key=lambda x: (-x[0], len(x[1].get("title") or "")))
    return [w for _, w in scored[:limit]]


def search_full(el: Dict[str, Any], part: str, height: Optional[float]) -> Dict[str, Any]:
    """Запасной путь: поиск по полному справочнику ТСН (47 тыс. расценок)."""
    kw = _ask_llm(
        "Ты опытный сметчик. Нужно найти в справочнике ТСН-2001 расценки на устройство "
        "элемента BIM-модели.\n\nЭлемент:\n" + _element_block(el, part) + "\n"
        "Дай 5-8 ключевых слов для поиска по наименованиям расценок: основы слов без "
        "окончаний (4-7 букв), описывающие работу, материал и конструкцию "
        "(например: «теплоизол», «пенополист», «плит», «стен»).\n"
        'Ответь строго JSON: {"keywords": ["...", "..."]}')
    keywords = [str(k) for k in (kw.get("keywords") or []) if str(k).strip()]
    cands = _search_candidates(keywords, height)
    if not cands:
        return {"works": [], "reason": f"по словам {keywords} в справочнике ничего не найдено"}
    lines = "\n".join(f"{i}: {w['pressmark']} | {w.get('unitOfMeasure', '')} | {(w.get('title') or '')[:170]}"
                      for i, w in enumerate(cands))
    ans = _ask_llm(
        "Ты опытный сметчик. Выбери из списка расценки ТСН для устройства элемента BIM-модели: "
        "основную работу и обязательные сопутствующие (не более 4). Учитывай материал, вид "
        "конструкции и часть здания. Если ничего не подходит — верни пустой список.\n\n"
        "Элемент:\n" + _element_block(el, part) +
        "\nРасценки (номер: шифр | ед. | наименование):\n" + lines + "\n\n"
        'Ответь строго JSON: {"selected": [<номера>], "reason": "<кратко почему>"}')
    ids = sorted(i for i in _ids(ans.get("selected")) if i < len(cands))[:4]
    rates, titles = [], set()
    for i in ids:
        t = (cands[i].get("title") or "").strip().lower()
        if t in titles:
            continue
        titles.add(t)
        rates.append({"pressmark": cands[i]["pressmark"], "title": cands[i].get("title") or "",
                      "unit": cands[i].get("unitOfMeasure", "")})
    return {"works": _works_from_rates(rates),
            "reason": f"ключевые слова {keywords}; {ans.get('reason', '')}"}


def pick_for_element(el: Dict[str, Any], part: str, subs: List[Dict[str, Any]],
                     height: Optional[float], use_cache: bool = True,
                     review: bool = True) -> Dict[str, Any]:
    """Подбор работ: перечень сметчиков, при неудаче — полный справочник ТСН.
    Возвращает {status: ok|search|cached|none, subsection, works, reason}."""
    el = enrich(el)
    if "подготов" in f"{el.get('name') or ''} {el.get('mssk_name') or ''}".lower():
        # бетонная подготовка: только «Устройство бетонной подготовки» (3.6-1-1 уже
        # включает укладку бетона) — без опалубки, арматуры и пакета плиты
        for s in subs:
            for w in s["works"]:
                if any(r["pressmark"].startswith("3.6-1-") for r in w["rates"]):
                    return {"status": "ok", "subsection": s, "works": [w],
                            "reason": "бетонная подготовка — по правилу: только 3.6-1-1 и бетон В7,5"}
    sig = _signature(el, part, height)
    cache = _load_cache()
    if use_cache and sig in cache:
        c = cache[sig]
        if c.get("sub_id") == "search":
            works = _works_from_rates(c.get("rates") or [])
            return {"status": "cached", "subsection": _search_sub(part, works),
                    "works": works, "reason": c.get("reason", "")}
        sub = next((s for s in subs if s["id"] == c.get("sub_id")), None)
        if sub is None:
            return {"status": "cached", "subsection": None, "works": [], "reason": c.get("reason", "")}
        subf = _filtered(sub, el, height)
        by_wid = {x["wid"]: set(x["rates"]) for x in (c.get("works") or []) if isinstance(x, dict)}
        works = [dict(w, rates=[r for r in w["rates"] if r["pressmark"] in by_wid[w["wid"]]] or w["rates"])
                 for w in subf["works"] if w["wid"] in by_wid]
        return {"status": "cached", "subsection": subf, "works": works, "reason": c.get("reason", "")}

    def fallback(why: str) -> Dict[str, Any]:
        res = search_full(el, part, height)
        reason = f"{why} → ПОИСК ПО СПРАВОЧНИКУ: {res['reason']}"
        if res["works"]:
            cache[sig] = {"sub_id": "search", "reason": reason, "element": type_key(el.get("name")),
                          "rates": [{"pressmark": r["pressmark"], "title": r["title"], "unit": r["unit"]}
                                    for w in res["works"] for r in w["rates"]]}
            _save_cache(cache)
            return {"status": "search", "subsection": _search_sub(part, res["works"]),
                    "works": res["works"], "reason": reason}
        cache[sig] = {"sub_id": None, "works": [], "reason": reason}
        _save_cache(cache)
        return {"status": "none", "subsection": None, "works": [], "reason": reason}

    if is_precast(el):
        return fallback("сборный железобетон — в перечне КР только монолит")

    s1 = select_subsection(el, part, subs)
    sub = s1["subsection"]
    if not sub:
        return fallback(f"в перечне КР нет подходящей конструкции ({s1['reason'][:150]})")

    subf = _filtered(sub, el, height)
    s2 = select_works(el, part, subf)
    chosen, reason = s2["works"], s2["reason"]
    if review and chosen:
        chosen, r_reason = review_selection(el, part, subf, chosen)
        if r_reason:
            reason += f" | КОНТРОЛЁР: {r_reason}"
    is_body = any(re.search(r"бетонир", (w["name"] + " " + " ".join(r["title"] for r in w["rates"])).lower())
                  for w in chosen)
    if _is_concrete(el) and is_body:
        # только для ТЕЛА конструкции (есть бетонирование), не для подготовки и т.п.
        # по перечню сметчиков: работы «не моделируется», арматура и закладные —
        # обязательная часть бетонной конструкции
        have = {w["wid"] for w in chosen}
        implied = [w for w in subf["works"] if w["wid"] not in have and not _is_rebar_diameter(w)
                   and re.search(r"не модел|reinforc|elementassembly|discreteacc", w["ifc_class"].lower())
                   # только сопутствующие работы самой бетонной конструкции
                   and re.search(r"опалуб|арматур|закладн|шпонк|шов|швов|профил|штуцер|инъекц|уплотн|гермет",
                                 w["name"].lower())]
        if implied:
            chosen = sorted(chosen + implied, key=lambda w: w["wid"])
            reason += " | ПО ПЕРЕЧНЮ добавлено: " + "; ".join(w["name"][:40] for w in implied)
    chosen = _apply_rebar_rule(subf, chosen)
    chosen = _attach_parent_works(subf, chosen)
    if not _is_concrete(el):
        chosen = [w for w in chosen if not _is_concrete_work(w)]
        if not chosen:
            return fallback("материал элемента — не бетон, в перечне КР подходящих работ нет")
    if not chosen:
        return fallback("модель не выбрала работ из перечня")
    chosen, rates_reason = select_rates(el, part, chosen)
    if rates_reason:
        reason += f" | РАСЦЕНКИ: {rates_reason}"

    cache[sig] = {"sub_id": sub["id"],
                  "works": [{"wid": w["wid"], "rates": [r["pressmark"] for r in w["rates"]]}
                            for w in chosen],
                  "reason": reason, "element": type_key(el.get("name")),
                  "subsection": sub["title"]}
    _save_cache(cache)
    return {"status": "ok", "subsection": subf, "works": chosen, "reason": reason}


# =====================================================================
#  CLI
# =====================================================================

def _element_part(entry: Dict[str, Any]) -> str:
    p = str((entry.get("applied_constants") or {}).get("building_part") or "").lower()
    return "Подземная" if "подзем" in p else "Цоколь" if "цокол" in p else "Надземная"


def _print_structure(subs: List[Dict[str, Any]]) -> None:
    for s in subs:
        print(f"{s['id']:>3} | {s['part']:<9} | {s['section'][:45]:<45} | "
              f"{s['title'][:50]:<50} | работ: {len(s['works']):>2} | "
              f"группы: {'; '.join(g[:25] for g in _groups(s)[:6])}")


def _print_result(res: Dict[str, Any]) -> None:
    sub = res["subsection"]
    if res["status"] == "skipped":
        print(f"  ⏭  {res['reason']}")
        return
    if not sub:
        print(f"  ❌ конструкция не выбрана: {res['reason'][:200]}")
        return
    print(f"  📂 {sub['section']} → {sub['title']}"
          + ("   (из кэша)" if res["status"] == "cached" else ""))
    print(f"  🔧 работ: {len(res['works'])}. {res['reason'][:300]}")
    diameters = [w["name"].replace("Арматура ", "") for w in res["works"] if _is_rebar_diameter(w)]
    for w in res["works"]:
        if _is_rebar_diameter(w):
            continue
        print(f"     • {w['name'][:75]}")
        for r in w["rates"]:
            mark = "   ресурс" if r["is_resource"] else "  "
            print(f"       {mark} {r['pressmark']:<12} {r['unit']:<14} {_full_title(r)[:70]}")
    if diameters:
        print(f"     • Арматура (ресурсы): {', '.join(diameters)}")


if __name__ == "__main__":
    subs = load_perechen()
    args = sys.argv[1:]
    if args and args[0] == "list":
        _print_structure(subs)
        sys.exit(0)
    use_cache = "nocache" not in args
    nums = [a for a in args if a.isdigit()]
    limit = int(nums[0]) if nums else 10

    path = sorted(glob.glob("/app/outputs/*/run_*/Подобранные_таблицы_работ.json"),
                  key=os.path.getmtime)[-1]
    data = json.load(open(path, encoding="utf-8"))
    height = building_height_for_run(path, data)
    print(f"Элементы из: {path}\nВысота здания: {height} м | кэш: {'да' if use_cache else 'нет'}\n",
          flush=True)

    seen = set()
    for entry in data.get("elements", []):
        if len(seen) >= limit:
            break
        el = entry.get("element", {})
        part = _element_part(entry)
        key = (type_key(el.get("name")), part)
        if key in seen:
            continue
        seen.add(key)
        print("=" * 100)
        th = element_thickness(el)
        print(f"ЭЛЕМЕНТ: {key[0]} | {el.get('ifc_class')} | {part} | {el.get('material')}"
              + (f" | толщина {th:.0f} мм" if th else ""))
        t0 = time.time()
        res = pick_for_element(el, part, subs, height, use_cache=use_cache)
        _print_result(res)
        print(f"  ⏱  {time.time() - t0:.1f} с\n", flush=True)
