"""Раздел «3. Коэффициенты к нормам и расценкам» технической части."""
import re
import pdfplumber
import src.tools.tsn.refs as refs_mod
from src.tools.tsn.parse_norms import norm_ws, parse_value

CLAUSE_RE = re.compile(r"^(3\.\d+)\.?\s*(.*)$", re.S)
HEADING_RE = re.compile(r"3\.\s*Коэффициенты к нормам и расценкам")
COEF_KEYS = ["labor_and_wages", "machine_operation", "material_consumption"]


def find_section_pages(pdf):
    """Страницы, на которых живёт §3, — от заголовка до первого «Отдел»."""
    start = end = None
    for i, p in enumerate(pdf.pages):
        txt = p.extract_text() or ""
        if start is None and HEADING_RE.search(norm_ws(txt)) and "....." not in txt:
            start = i
        elif start is not None and re.search(r"^\s*Отдел\s+\d+\.\s*\S", txt, re.M):
            end = i
            break
    return start, (end if end is not None else start)


def grid_rows(pdf, start, end):
    """Строки всех 5-колоночных сеток §3 по порядку."""
    out = []
    for i in range(start, end + 1):
        page = pdf.pages[i]
        for t in sorted(page.find_tables(), key=lambda t: t.bbox[1]):
            ex = t.extract()
            if not ex or len(ex[0]) not in (4, 5):
                continue            # 5 граф (труд, машины, материалы) или 4 (без материалов)
            flat = " ".join(norm_ws(c) for r in ex for c in r if c)
            if not re.search(r"\b3\.\d+\.", flat) and "Условия применения" not in flat:
                continue
            for r in ex:
                out.append({"page": i + 1,
                            "text": norm_ws(r[0]),
                            "norms": (r[1] or ""),
                            "coefs": (list(r[2:5]) + [None, None, None])[:3]})
    return out


def parse(pdf_path):
    pdf = pdfplumber.open(pdf_path)
    start, end = find_section_pages(pdf)
    if start is None:
        return {"items": [], "issues": [{"type": "coeff_section_not_found"}]}

    items, issues = [], []
    cur = None            # запись, уже получившая коэффициенты
    pending = None        # пункт, у которого коэффициенты идут строкой ниже

    def add_norms(target, frag):
        target["applies_to"]["raw"] = refs_mod.normalize(
            target["applies_to"]["raw"] + " " + frag)

    last_norms, last_cond = "", ""
    for row in grid_rows(pdf, start, end):
        text, raw_norms = row["text"], norm_ws(row["norms"])
        # «то же» и кавычки-повторы « » " в графе 2 — те же таблицы, что в пункте выше
        if raw_norms in ("«", "»", '"', "“", "”", "то же", "-//-"):
            raw_norms = last_norms
        if raw_norms:
            last_norms = raw_norms
        vals = [parse_value(c) for c in row["coefs"]]
        vals = [v if not isinstance(v, dict) else None for v in vals]
        has_coef = any(v is not None for v in vals)
        if not text and not raw_norms and not has_coef:
            continue
        if text.startswith("Условия применения") or re.fullmatch(r"[1-5\s]+", text):
            continue
        m = CLAUSE_RE.match(text) if text else None

        if m and has_coef:                        # пункт целиком в одной строке
            cur = _new(m.group(1), m.group(2), raw_norms, vals, row["page"])
            items.append(cur); pending = None
        elif m:                                   # заголовок пункта, числа ниже
            pending = {"clause": m.group(1), "text": m.group(2),
                       "norms": raw_norms, "page": row["page"]}
            cur = None
        elif has_coef and pending:                # строка-подпункт («к расходу: утеплителя»)
            cur = _new(pending["clause"], pending["text"],
                       norm_ws(pending["norms"] + " " + raw_norms), vals, pending["page"])
            cur["applies_to_resource"] = text or None
            items.append(cur); pending = None
        elif has_coef and cur:                    # второй набор норм у того же пункта
            var = _new(cur["clause"], cur["condition"], raw_norms, vals, row["page"])
            var["variant_of"] = cur["clause"]
            items.append(var); cur = var
        elif has_coef:
            issues.append({"type": "coeff_row_without_clause",
                           "page": row["page"], "text": text})
        else:                                     # перенос строки: норм и/или текста
            tgt = cur or pending
            if tgt is None:
                continue
            if raw_norms:
                if tgt is cur:
                    add_norms(cur, raw_norms)
                else:
                    tgt["norms"] = norm_ws(tgt["norms"] + " " + raw_norms)
            if text:
                if tgt is pending:
                    tgt["text"] = norm_ws(tgt["text"] + " " + text)
                elif cur["applies_to_resource"]:
                    cur["applies_to_resource"] = norm_ws(
                        cur["applies_to_resource"] + " " + text)
                else:
                    cur["condition"] = norm_ws(cur["condition"] + " " + text)

    # «То же, от 36 до 55 м» -> полное условие по пункту выше
    prev = None
    for it in items:
        m = re.match(r"(?i)^то же[,\s]*(.*)$", it["condition"])
        if m and prev:
            base = re.split(r"\s+от\s+\d", prev)[0].rstrip(" ,")
            it["condition"] = f"{base} {m.group(1)}".strip()
        else:
            prev = it["condition"]
    # переносы графы 2 приходят отдельными строками — доклеиваем
    for it in items:
        parsed, notes = refs_mod.parse(it["applies_to"]["raw"])
        it["applies_to"]["refs"] = parsed
        for n in notes:
            issues.append(dict(n, clause=it["clause"]))
    return {"title": "3. Коэффициенты к нормам и расценкам",
            "columns": ["Условия применения",
                        "Номера нормативных таблиц, норм и расценок",
                        "Коэффициент к затратам труда и заработной плате",
                        "Коэффициент к затратам по эксплуатации машин",
                        "Коэффициент к расходу материалов"],
            "items": items, "issues": issues}


def _new(clause, condition, raw_norms, vals, page):
    return {
        "clause": clause,
        "condition": norm_ws(condition),
        "applies_to_resource": None,
        "applies_to_norms_raw": refs_mod.normalize(raw_norms),   # как в примере
        "applies_to": {"raw": refs_mod.normalize(raw_norms), "refs": []},
        "coefficients": {k: (v if not isinstance(v, dict) else None)
                         for k, v in zip(COEF_KEYS, vals)},
        "source_page": page,
    }
