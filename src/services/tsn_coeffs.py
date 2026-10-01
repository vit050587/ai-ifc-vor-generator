"""Поправочные коэффициенты из сборников ТСН (раздел «3. Коэффициенты к нормам и расценкам»).

Сборники лежат в data/tsn/*.json — это результат build.py (tsn_3_<N>_full.json
или tsn_3_<N>_coefficients.json). Для каждой подобранной расценки находим пункты,
которые к ней относятся, и qwen решает по данным элемента: да / нет / неясно.
"""
import os, re, json, glob, hashlib, logging

logger = logging.getLogger(__name__)
# Каталог сборников ТСН: внутри docker — /app/data/tsn, на хосте — <корень проекта>/data/tsn
# (можно переопределить переменной окружения TSN_DIR)
_PROJECT_DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
TSN_DIR = os.getenv("TSN_DIR") or ("/app/data/tsn" if os.path.isdir("/app/data/tsn") else os.path.join(_PROJECT_DATA, "tsn"))
CACHE = os.path.join(os.path.dirname(TSN_DIR), "tsn_decisions_cache.json")
_IDX = None          # pressmark -> [(doc, item)]
_BOOK = {}           # шифр расценки ("6-97-3") -> данные таблицы сборника
_TECH = {}           # код сборника -> {n, general, volume_rules} (техническая часть)
_REFS = []           # (doc, item) — сверка по ссылкам на таблицы (надёжнее нумерации расценок)
_DOCS = []
_CACHE = None


def _load():
    global _IDX, _DOCS
    if _IDX is not None:
        return _IDX
    _IDX, _DOCS = {}, []
    _REFS.clear()
    _TECH.clear()
    _BOOK.clear()
    for f in sorted(glob.glob(os.path.join(TSN_DIR, "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            logger.warning(f"ТСН: не прочитан {f}: {e}")
            continue
        items = ((d.get("coefficients_table") or {}).get("items")) or []
        doc = d.get("document") or {}
        code = doc.get("code") or os.path.basename(f)
        ttl = doc.get("title", "") or ""
        if ttl.isupper():
            ttl = ttl[:1] + ttl[1:].lower()
        _DOCS.append({"code": code, "title": ttl, "items": len(items)})
        tp = doc.get("technical_part") or {}
        n = str(doc.get("collection_number") or "")
        if not n:
            m = re.search(r"-(\d+)$", code)
            n = m.group(1) if m else ""
        for tb in d.get("tables") or []:
            for v in tb.get("variants") or []:
                for r in v.get("rates") or []:
                    info = {"doc": code, "table": tb.get("table_code"), "table_title": tb.get("title", ""),
                            "unit": tb.get("unit_of_measure") or "", "composition": tb.get("work_composition") or [],
                            "not_included": r.get("materials_not_included_in_rate") or []}
                    for k in {r.get("code"), r.get("code_normalized")}:
                        if k:
                            _BOOK.setdefault(k, info)
        # поправки, записанные текстом в общих указаниях («…применять коэффициент 1,15 к затратам труда…»)
        n_txt = 0
        ncoll = str(doc.get("collection_number") or "")
        for c in ((doc.get("technical_part") or {}).get("general") or []):
            for it in _text_coef_items(c, ncoll):
                _REFS.append((code, it))
                n_txt += 1
        if n_txt:
            _DOCS[-1]["items"] += n_txt
            _DOCS[-1]["text_items"] = n_txt
        _TECH[code] = {"n": n, "general": tp.get("general") or [], "volume_rules": tp.get("volume_rules") or []}
        for it in items:
            for c in (it.get("applies_to") or {}).get("expanded_rate_codes") or []:
                for key in {c, "3." + c if not c.startswith("3.") else c}:
                    _IDX.setdefault(key, []).append((code, it))
            _REFS.append((code, it))
    logger.info(f"ТСН-поправки: сборников {len(_DOCS)}, расценок с поправками {len(_IDX)}")
    return _IDX


_COEF_RE = re.compile(r"(?:коэффициент\w*|К\s*=)\s*(?:равн\w+\s*)?(\d+[,.]\d+)", re.I)


def _text_coef_items(clause, ncoll):
    """Пункт общих указаний с числом-коэффициентом -> «поправка» в том же виде, что из таблицы §3."""
    t = clause.get("text") or ""
    vals = [float(v.replace(",", ".")) for v in _COEF_RE.findall(t)]
    vals = [v for v in vals if 0.3 <= v <= 5 and v != 1.0]
    if not vals:
        return []
    low = t.lower()
    k = vals[0]
    coefs = {"labor_and_wages": None, "machine_operation": None, "material_consumption": None}
    if "труд" in low or "заработн" in low:
        coefs["labor_and_wages"] = k
    if "машин" in low or "механизм" in low:
        coefs["machine_operation"] = k
    if "материал" in low or "расход" in low:
        coefs["material_consumption"] = k
    if not any(v is not None for v in coefs.values()):
        coefs["labor_and_wages"] = k          # к чему — не сказано явно; qwen увидит полный текст условия
    refs = []
    for r in clause.get("refs") or []:
        a, b = r.get("from") or "", r.get("to")
        pa = a.split("-")
        if not ncoll or pa[0] != ncoll:
            continue                          # ссылки на другие сборники / графы пропускаем
        if b:
            pb = b.split("-")
            if len(pa) == 2 and len(pb) == 2:
                refs.append({"kind": "table_range", "from_table": a, "to_table": b})
            elif len(pa) == 3 and len(pb) == 3:
                refs.append({"kind": "rate_range", "table": "-".join(pa[:2]), "from_rate": a, "to_rate": b})
        elif len(pa) == 2:
            refs.append({"kind": "table", "table": a})
        elif len(pa) == 3:
            refs.append({"kind": "rate", "table": "-".join(pa[:2]), "rate": a})
    if not refs and ncoll:
        refs = [{"kind": "table_range", "from_table": f"{ncoll}-1", "to_table": f"{ncoll}-999"}]   # весь сборник
    return [{"clause": clause.get("num"), "condition": t[:400], "applies_to_resource": None,
             "applies_to": {"raw": "текст п." + str(clause.get("num")), "refs": refs, "expanded_rate_codes": []},
             "coefficients": coefs, "source_page": clause.get("page"), "from_text": True}]


def _nums(code):
    """'3.6-97-3' / '6-97-3' -> ('6', 97, 3); '6-97' -> ('6', 97, None)"""
    c = code[2:] if code.startswith("3.") else code
    p = c.split("-")
    try:
        return p[0], int(p[1]), (int(p[2]) if len(p) > 2 else None)
    except Exception:
        return None


def _ref_hits(pressmark, ref):
    pm = _nums(pressmark)
    if not pm or pm[2] is None:
        return False
    k = ref.get("kind")
    if k == "table":
        t = _nums(ref["table"]); return bool(t) and t[:2] == pm[:2]
    if k == "rate":
        r = _nums(ref["rate"]); return r == pm
    if k == "table_range":
        a, b = _nums(ref["from_table"]), _nums(ref["to_table"])
        return bool(a and b) and a[0] == pm[0] and a[1] <= pm[1] <= b[1]
    if k == "rate_range":
        a, b = _nums(ref["from_rate"]), _nums(ref["to_rate"])
        return bool(a and b) and a[:2] == pm[:2] and a[2] <= pm[2] <= b[2]
    return False


def candidates(pressmark):
    idx = _load()
    out, seen = [], set()
    for d, it in idx.get(pressmark) or []:
        if id(it) not in seen:
            seen.add(id(it)); out.append((d, it))
    for d, it in _REFS:
        if id(it) in seen:
            continue
        if any(_ref_hits(pressmark, r) for r in (it.get("applies_to") or {}).get("refs") or []):
            seen.add(id(it)); out.append((d, it))
    return out


def docs():
    _load()
    return list(_DOCS)


def coef_text(it):
    c = it.get("coefficients") or {}
    parts = []
    fmt = lambda v: str(v).replace(".", ",")
    if c.get("labor_and_wages") is not None:
        parts.append(f"труд ×{fmt(c['labor_and_wages'])}")
    if c.get("machine_operation") is not None:
        parts.append(f"машины ×{fmt(c['machine_operation'])}")
    if c.get("material_consumption") is not None:
        res = it.get("applies_to_resource")
        parts.append(f"{('расход «' + res + '»') if res else 'материалы'} ×{fmt(c['material_consumption'])}")
    return ", ".join(parts)


def _el_facts(el, q):
    out = {}
    for k, v in (el or {}).items():
        if isinstance(v, (str, int, float)) and v not in ("", None) and len(str(v)) < 150:
            out[k] = v
    for k, v in (q or {}).items():
        if isinstance(v, (int, float)) and v:
            out["q_" + k] = round(v, 3)
    return out


def _cache():
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            _CACHE = {}
    return _CACHE


def decide(pressmark, rate_title, el, q, extra=None):
    """-> [{doc, clause, condition, coef, decision: да|нет|неясно, why}]"""
    cands = candidates(pressmark)
    if not cands:
        return []
    facts = _el_facts(el, q)
    key = hashlib.md5(json.dumps([pressmark, facts, [(d, i["clause"], i["condition"]) for d, i in cands], extra or {}],
                                 ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    cache = _cache()
    if key in cache:
        return cache[key]
    lines = []
    for n, (d, it) in enumerate(cands, 1):
        res = f" (относится к ресурсу: {it['applies_to_resource']})" if it.get("applies_to_resource") else ""
        lines.append(f'{n}. {d} п.{it["clause"]}: «{it["condition"]}»{res} → {coef_text(it)}')
    prompt = (
        "Ты опытный инженер-сметчик. К расценке могут относиться поправочные коэффициенты из сборника ТСН. "
        "Реши для КАЖДОГО пункта, выполняется ли его условие для данного элемента.\n"
        "«да» — только если по данным элемента это ТОЧНО видно (например, толщина, высота помещения, материал совпадают).\n"
        "«нет» — если условие точно не выполняется или относится к другому виду работ/материалу.\n"
        "«неясно» — если по данным элемента нельзя понять (например, криволинейность, цветность, условия производства).\n"
        "why — ОЧЕНЬ простыми словами до 110 символов, что у элемента (например: «толщина утеплителя в модели 150 мм»).\n"
        f"\nРасценка {pressmark}: {rate_title}\n"
        f"Элемент из модели: {json.dumps(facts, ensure_ascii=False)}\n"
        + (f"Данные ПОС/ПЗ и параметры расчёта: {json.dumps(extra, ensure_ascii=False)}\n" if extra else "")
        + "\nПункты поправок:\n" + "\n".join(lines)
        + '\n\nОтвет строго JSON: {"1": {"decision": "да|нет|неясно", "why": "..."}, "2": {...}}'
    )
    try:
        from src.services.pd_full import ask
        ans = ask(prompt)
    except Exception as e:
        logger.warning(f"ТСН-поправки: LLM недоступна: {e}")
        ans = {}
    out = []
    for n, (d, it) in enumerate(cands, 1):
        a = ans.get(str(n)) or {}
        dec = str(a.get("decision") or "неясно").strip().lower()
        if dec not in ("да", "нет", "неясно"):
            dec = "неясно"
        out.append({"doc": d, "clause": it["clause"], "condition": it["condition"],
                    "coef": coef_text(it), "decision": dec, "why": str(a.get("why") or "")[:140]})
    cache[key] = out
    try:
        json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return out


def tech_for(pressmarks):
    """Техническая часть сборников, к которым относятся расценки перечня (3.6-… -> сборник 6)."""
    _load()
    nums = set()
    for pm in pressmarks:
        m = re.match(r"^3\.(\d+)-", str(pm))
        if m:
            nums.add(m.group(1))
    return {code: t for code, t in _TECH.items() if t["n"] in nums and (t["general"] or t["volume_rules"])}


def book_rate(pressmark):
    """Данные сборника по расценке перечня: '3.6-97-3' -> таблица 6-97, состав работ, не учтённые материалы."""
    _load()
    pm = str(pressmark)
    return _BOOK.get(pm[2:] if pm.startswith("3.") else pm)


def tables_of(n):
    """[(шифр таблицы в справочнике '3.6-100', название)] для сборника n (по загруженным JSON)."""
    _load()
    out, seen = [], set()
    for code, info in _BOOK.items():
        t = info.get("table") or ""
        if t.split("-")[0] == str(n) and t not in seen:
            seen.add(t)
            out.append(("3." + t, info.get("table_title") or ""))
    out.sort(key=lambda x: [int(y) for y in re.findall(r"\d+", x[0])])
    return out
