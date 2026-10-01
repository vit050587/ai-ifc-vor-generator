"""Техническая часть сборника ТСН: «1. Общие указания» и «2. Правила исчисления объемов работ».

Каждый пункт (1.2, 1.2.3, 2.8 …) — отдельная запись {num, text, page, refs}.
refs — ссылки на таблицы/расценки внутри текста («табл. 6-73», «6-1-2÷6-1-12»).

Запуск отдельно (дописать в готовый JSON, без повторного разбора норм):
    python tech_part.py <сборник.pdf> <tsn_3_N_full.json>
"""
import re, sys, json
import pdfplumber

HEAD_RE = re.compile(r"^\s*Техническая часть\s*$", re.M)
SEC1_RE = re.compile(r"^\s*1\.\s*Общие указания\s*$", re.M)
SEC2_RE = re.compile(r"^\s*2\.\s*Правила исчисления", re.M)
SEC3_RE = re.compile(r"^\s*3\.\s*Коэффициенты к нормам", re.M)
CLAUSE_RE = re.compile(r"^\s*([12]\.\d+(?:\.\d+)?)\.?\s+(?=\S)", re.M)
REF_RE = re.compile(r"(\d+)-(\d+)(?:-(\d+))?(?:\s*[÷–—]\s*(\d+)-(\d+)(?:-(\d+))?)?")


def _refs(text):
    out = []
    for m in REF_RE.finditer(text):
        a = [m.group(1), m.group(2), m.group(3)]
        b = [m.group(4), m.group(5), m.group(6)]
        out.append({"from": "-".join(x for x in a if x), "to": "-".join(x for x in b if x) or None})
    return out


def parse(pdf_path):
    pdf = pdfplumber.open(pdf_path)
    pages = []
    started = False
    for i, p in enumerate(pdf.pages):
        t = p.extract_text() or ""
        if not started:
            m = HEAD_RE.search(t)
            if not m:
                continue
            started = True
            t = t[m.end():]
        m3 = SEC3_RE.search(t)
        if m3:
            pages.append((i + 1, t[:m3.start()]))
            break
        pages.append((i + 1, t))
    general, rules = [], []
    cur, target = None, None
    for pno, t in pages:
        pos = 0
        # делим страницу на куски по заголовкам разделов и номерам пунктов
        marks = []
        for m in SEC1_RE.finditer(t):
            marks.append((m.start(), m.end(), "sec1", None))
        for m in SEC2_RE.finditer(t):
            marks.append((m.start(), t.find("\n", m.end()) if t.find("\n", m.end()) > 0 else m.end(), "sec2", None))
        for m in CLAUSE_RE.finditer(t):
            marks.append((m.start(), m.end(), "clause", m.group(1)))
        marks.sort()
        for s, e, kind, num in marks:
            if cur is not None and s > pos:
                cur["text"] += " " + t[pos:s]
            if kind == "sec1":
                target = general; cur = None
            elif kind == "sec2":
                target = rules; cur = None
            elif target is not None:
                cur = {"num": num, "text": "", "page": pno}
                target.append(cur)
            pos = e
        if cur is not None:
            cur["text"] += " " + t[pos:]
    for lst in (general, rules):
        for c in lst:
            c["text"] = re.sub(r"\s+", " ", c["text"]).strip()
            c["refs"] = _refs(c["text"])
    return {"general": general, "volume_rules": rules}


if __name__ == "__main__":
    tp = parse(sys.argv[1])
    print(f"общие указания: {len(tp['general'])} п., правила исчисления объёмов: {len(tp['volume_rules'])} п.")
    if len(sys.argv) > 2:
        d = json.load(open(sys.argv[2], encoding="utf-8"))
        d.setdefault("document", {})["technical_part"] = tp
        json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("дописано в", sys.argv[2])
