"""Разбор ссылок на нормы из графы 2 таблицы коэффициентов.

В книге встречаются четыре формы записи, вперемешку в одной ячейке:
    15-1              — вся таблица
    15-51÷15-73       — диапазон таблиц
    15-96-1           — одна расценка
    15-2-6÷15-2-10    — диапазон расценок
Плюс дефекты набора: «+» вместо «÷» и переносы строк внутри шифра.
"""
import re
from src.tools.tsn.tsn_cfg import N, NE

RANGE_SEPS = "÷–—+"
TOKEN_SPLIT = re.compile(r"[,;\s]+")
TABLE_RE = re.compile(rf"^{NE}-(\d+)$")
RATE_RE = re.compile(rf"^{NE}-(\d+)-(\d+)$")


def normalize(raw):
    """Склейка переносов внутри шифров и вокруг разделителей."""
    s = (raw or "").replace("\n", " ")
    s = re.sub(r"-\s+", "-", s)          # «15-77-\n12» -> «15-77-12»
    s = re.sub(r"\s*([" + RANGE_SEPS + r"])\s*", r"\1", s)
    s = re.sub(r",\s*", ", ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(raw):
    """Строка графы 2 -> (список ссылок, список замечаний)."""
    s = normalize(raw)
    refs, notes = [], []
    if not s or s in ("-", "—"):
        return refs, notes
    for tok in TOKEN_SPLIT.split(s):
        if not tok:
            continue
        sep = next((c for c in RANGE_SEPS if c in tok), None)
        if sep:
            left, _, right = tok.partition(sep)
            ref = _range(left, right, notes, tok)
            if sep == "+":
                notes.append({"type": "range_separator_typo", "token": tok,
                              "note": "в книге «+» вместо «÷»; прочитано как диапазон"})
        else:
            ref = _single(tok, notes)
        if ref:
            refs.append(ref)
    return refs, notes


def _single(tok, notes):
    m = TABLE_RE.match(tok)
    if m:
        return {"kind": "table", "table": tok}
    m = RATE_RE.match(tok)
    if m:
        return {"kind": "rate", "table": f"{N}-{m.group(1)}", "rate": tok}
    notes.append({"type": "ref_unparsed", "token": tok})
    return None


def _range(left, right, notes, tok):
    lt, rt = TABLE_RE.match(left), TABLE_RE.match(right)
    if lt and rt:
        return {"kind": "table_range", "from_table": left, "to_table": right}
    lr, rr = RATE_RE.match(left), RATE_RE.match(right)
    if lr and rr:
        if lr.group(1) != rr.group(1):
            notes.append({"type": "ref_range_cross_table", "token": tok,
                          "note": "диапазон расценок пересекает границу таблиц"})
        return {"kind": "rate_range", "table": f"{N}-{lr.group(1)}",
                "from_rate": left, "to_rate": right}
    notes.append({"type": "ref_unparsed", "token": tok})
    return None


def expand(refs, tables_index):
    """Ссылки -> конкретные шифры расценок. tables_index: {код таблицы: [шифры]}."""
    out, missing = [], []
    for r in refs:
        if r["kind"] == "table":
            codes = tables_index.get(r["table"])
            if codes is None:
                missing.append(r["table"])
            else:
                out += codes
        elif r["kind"] == "rate":
            if r["rate"] in tables_index.get(r["table"], []):
                out.append(r["rate"])
            else:
                missing.append(r["rate"])
        elif r["kind"] == "table_range":
            a = int(r["from_table"].split("-")[1]); b = int(r["to_table"].split("-")[1])
            for n in range(a, b + 1):
                codes = tables_index.get(f"{N}-{n}")
                if codes:
                    out += codes
        elif r["kind"] == "rate_range":
            codes = tables_index.get(r["table"], [])
            a = int(r["from_rate"].split("-")[2]); b = int(r["to_rate"].split("-")[2])
            for n in range(a, b + 1):
                c = f"{r['table']}-{n}"
                if c in codes:
                    out.append(c)
                else:
                    missing.append(c)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return uniq, sorted(set(missing))
