"""Сборка разобранных блоков в дерево расценок, как в целевом JSON."""
import re
from src.tools.tsn.parse_norms import (SUMMARY, norm_ws, parse_value, column_header_path,
                         parse_document)

RESOURCE_SECTIONS = {
    "machines": "machines",
    "materials": "materials",
    "materials_not_included_in_rate": "materials_not_included_in_rate",
    "equipment": "equipment",
    "equipment_not_included_in_rate": "equipment_not_included_in_rate",
}

# Опечатки текстового слоя: римские цифры набраны кириллицей.
TYPO_FIXES = [
    (re.compile(r"\bсорт\s+П\b"),      "сорт II"),
    (re.compile(r"\bсорт\s+Ш\b"),      "сорт III"),
    (re.compile(r"\bсорт\s+1У\b"),     "сорт IV"),
    (re.compile(r"\bкласс\s+А-П\b"),   "класс А-II"),
    (re.compile(r"\bкласс\s+А-Ш\b"),   "класс А-III"),
    (re.compile(r"\bмарка\s+П\b"),     "марка II"),
]
RES_CODE_RE = re.compile(r"^\d+\.\d+(\.\d+)?-\d+-\d+$|^\d+-\d+-\d+$|^\d{6,}$")


def summary_key(name):
    """Итоговая строка по названию; допускает потерю первых букв в текстовом слое."""
    n = re.sub(r"[^а-яё: ]", "", name.lower()).strip()
    if n in SUMMARY:
        return SUMMARY[n]
    for full, key in SUMMARY.items():
        if len(n) >= 6 and (full.endswith(n) or n.endswith(full)):
            return key
    return None


def fix_typos(text, log, where):
    out = text
    for rx, rep in TYPO_FIXES:
        new = rx.sub(rep, out)
        if new != out:
            log.append({"type": "text_normalized", "where": where,
                        "from": out, "to": new})
            out = new
    return out


def fix_resource_code(code, log, where):
    """Шифры ресурсов состоят из цифр; буква в шифре — дефект текстового слоя."""
    if not code or RES_CODE_RE.fullmatch(code):
        return code, False
    fixed = (code.replace("Q", "0").replace("О", "0").replace("О", "0")
                 .replace("З", "3").replace("Э", "3").replace(" ", ""))
    if RES_CODE_RE.fullmatch(fixed):
        log.append({"type": "resource_code_normalized", "where": where,
                    "from": code, "to": fixed})
        return fixed, True
    log.append({"type": "resource_code_unparsed", "where": where, "code": code})
    return code, False


def split_grouping_dimension(rates):
    """Отделяет подпись измерения («число плит в 1 м2, до») от названия варианта.

    В книге обе строки стоят в одном пролёте шапки и склеиваются. Разрез
    делается только по границе исходных ячеек и только там, где хвост
    одинаков у всех вариантов таблицы, — иначе метка остаётся как есть."""
    tops = [r["_path"][0] for r in rates if r.get("_path")]
    if not tops:
        return
    distinct = {tuple(l["parts"]): l for l in tops}
    if len(distinct) < 2:
        return
    keys = list(distinct)
    k = 0
    while (k < min(len(x) for x in keys) - 1
           and len({x[-(k + 1)] for x in keys}) == 1):
        k += 1
    if k == 0:
        return
    for r in rates:
        p = r.get("_path")
        if not p:
            continue
        parts = p[0]["parts"]
        r["column_headers"] = ([" ".join(parts[:-k]), " ".join(parts[-k:])]
                               + [l["text"] for l in p[1:]])
        r["grouping_dimension"] = " ".join(parts[-k:])


def build_rates(table, issues):
    rates = []
    for blk in table["column_blocks"]:
        spans = blk["spans"]
        paths = [column_header_path(blk["headers"], s) for s in spans]
        cols = [{"code": s["code"], "column_headers": [l["text"] for l in p],
                 "_path": p,
                 "machines": [], "materials": [],
                 "materials_not_included_in_rate": [],
                 "equipment": [], "equipment_not_included_in_rate": []}
                for s, p in zip(spans, paths)]
        section = "summary"
        for row in blk["rows"]:
            if "section_header" in row:
                section = row["section_header"]
                continue
            name = norm_ws(row["name"])
            if not name:
                continue
            where = f"{table['code']} стр.{blk['page']}"
            key = summary_key(name)
            if section == "summary" and key:
                for ci, c in enumerate(cols):
                    c[key] = parse_value(row["values"][ci])
                continue
            if section == "summary":
                # строка до заголовка секции, не входящая в набор итоговых
                issues.append({"type": "unknown_summary_row", "where": where,
                               "name": name})
                continue
            code, _ = fix_resource_code(row["code"], issues, where)
            name = fix_typos(name, issues, where)
            for ci, c in enumerate(cols):
                v = parse_value(row["values"][ci])
                if v is None:
                    continue
                c[RESOURCE_SECTIONS[section]].append(
                    {"code": code or None, "name": name,
                     "unit": norm_ws(row["unit"]) or None, "value": v})
        base = len(rates)
        for ci, c in enumerate(cols):
            expected = f"{table['code']}-{base + ci + 1}"
            if c["code"] != expected:
                c["code_normalized"] = expected
                c["code_note"] = ("шифр в книге отличается от позиции расценки; "
                                  "связи строятся по code_normalized")
            for k in list(RESOURCE_SECTIONS.values()):
                if not c[k]:
                    c.pop(k)
            c["source_page"] = blk["page"]
            rates.append(c)
    split_grouping_dimension(rates)
    for c in rates:
        c.pop("_path", None)
    return rates


def group_variants(rates):
    """Расценки -> варианты, как в целевой структуре: вариант задаётся общей
    частью шапки, значение внутри варианта — её последним уровнем."""
    out = []
    for r in rates:
        h = r.get("column_headers") or []
        dim = r.get("grouping_dimension")
        prefix = [x for x in h[:-1] if x != dim]
        name = " / ".join(prefix) if prefix else None
        key = (name, dim)
        if not out or out[-1]["_key"] != key:
            out.append({"_key": key, "variant_name": name,
                        "grouping_dimension": dim, "rates": []})
        r["group_value"] = h[-1] if h else None
        r.pop("grouping_dimension", None)
        out[-1]["rates"].append(r)
    for v in out:
        v.pop("_key")
    return out


def iter_rates(table):
    for v in table["variants"]:
        for r in v["rates"]:
            yield r


def build(pdf_path):
    tables, issues = parse_document(pdf_path)
    strip_no = re.compile(r"^\d+\.\s*")
    out = []
    for t in tables:
        out.append({
            "table_code": t["code"],
            "title": t["title"],
            "location": {"otdel": t["otdel"], "razdel": t["razdel"]},
            "work_composition": [strip_no.sub("", w) for w in t["work_composition"]],
            "unit_of_measure": t["unit_of_measure"],
            "pages": t["pages"],
            "variants": group_variants(build_rates(t, issues)),
        })
    return out, issues
