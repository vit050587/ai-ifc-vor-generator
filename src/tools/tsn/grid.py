"""Низкоуровневая работа со страницей PDF: слова, строки, сетки таблиц.

Модуль намеренно не знает ничего про ТСН — только про геометрию.
Числа берутся из клеточной сетки детерминированно, без эвристик и без LLM.
"""
import re
from src.tools.tsn.tsn_cfg import N, NE

CODE_RE = re.compile(rf"^{NE}-\d+-\d+$")


def squash(text):
    """Шифр расценки может быть разорван переносом внутри ячейки: «15-115-\n1»."""
    return re.sub(r"\s+", "", text or "")


def group_lines(words, ytol=3.0):
    """Слова -> визуальные строки. Группировка по нижней границе:
    надстрочные индексы (м2, м3) сидят выше по `top`, но на той же базовой линии."""
    lines = []
    for w in sorted(words, key=lambda w: (w["bottom"], w["x0"])):
        if lines and abs(w["bottom"] - lines[-1]["bottom"]) <= ytol:
            lines[-1]["words"].append(w)
            lines[-1]["bottom"] = (lines[-1]["bottom"] + w["bottom"]) / 2
        else:
            lines.append({"bottom": w["bottom"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["text"] = " ".join(w["text"] for w in ln["words"])
        ln["top"] = min(w["top"] for w in ln["words"])
        ln["x0"] = min(w["x0"] for w in ln["words"])
        ln["x1"] = max(w["x1"] for w in ln["words"])
    return lines


def in_bbox(words, bbox, pad=1.0):
    x0, top, x1, bottom = bbox
    return [w for w in words
            if w["x0"] >= x0 - pad and w["x1"] <= x1 + pad
            and w["top"] >= top - pad and w["bottom"] <= bottom + pad]


def outside_bboxes(words, bboxes, pad=1.0):
    def hit(w):
        cy = (w["top"] + w["bottom"]) / 2
        cx = (w["x0"] + w["x1"]) / 2
        return any(b[0] - pad <= cx <= b[2] + pad and b[1] - pad <= cy <= b[3] + pad
                   for b in bboxes)
    return [w for w in words if not hit(w)]


ANCHOR_RE = re.compile(r"^\s*прямые\s+затраты", re.I)
CODEISH_RE = re.compile(rf"^{NE}[-\d\wЭОQ]*$|^-?\d+$")


def _collect_codes(page, table, spans, y_bottom):
    """Шифры колонок — из слов над строкой «Прямые затраты».

    По ячейкам это делать нельзя: при вертикальном объединении часть ячеек
    теряет текст. Слова дают полосу шифров целиком, включая случаи, когда
    шифр разорван переносом или колонки напечатаны на разной высоте."""
    words = in_bbox(page.extract_words(),
                    (table.bbox[0], table.bbox[1], table.bbox[2], y_bottom))
    frags = [[] for _ in spans]
    band_top = y_bottom
    for ln in reversed(group_lines(words)):
        row = [None] * len(spans)
        ok = True
        for w in ln["words"]:
            cx = (w["x0"] + w["x1"]) / 2
            for si, sp in enumerate(spans):
                if sp["x0"] - 2 <= cx <= sp["x1"] + 2:
                    txt = squash(w["text"])
                    if not CODEISH_RE.match(txt):
                        ok = False
                    row[si] = txt if row[si] is None else row[si] + txt
                    break
        if not ok:
            break
        if any(row):
            for si, t in enumerate(row):
                if t:
                    frags[si].insert(0, t)
            band_top = ln["top"]
            if all(CODE_RE.fullmatch("".join(f)) for f in frags):
                break
    for si, f in enumerate(frags):
        code = "".join(f)
        spans[si]["code"] = code or None
    return band_top


def find_code_row(page, table):
    """Колонки расценок таблицы. Возвращает (верх полосы шифров, верх строки
    «Прямые затраты», колонки) либо (None, None, None)."""
    data = table.extract()
    rows = table.rows

    anchor = next((ri for ri, cells in enumerate(data)
                   if any(c and ANCHOR_RE.match(c) for c in cells)), None)
    spans = None
    if anchor is not None:
        cells = sorted([c for c in rows[anchor].cells if c is not None],
                       key=lambda c: c[0])
        texts = []
        for c in cells:
            ci = next(i for i, cc in enumerate(rows[anchor].cells)
                      if cc is not None and cc[0] == c[0])
            texts.append(re.sub(r"\s+", " ", data[anchor][ci] or "").strip())
        name_i = next((i for i, t in enumerate(texts) if ANCHOR_RE.match(t)), None)
        if name_i is not None and name_i + 2 < len(cells):
            spans = [{"x0": c[0], "x1": c[2], "code": None}
                     for c in cells[name_i + 2:]]
            y_bottom = rows[anchor].bbox[1]
    if spans is None:
        # блок оборван границей страницы: строки «Прямые затраты» здесь нет,
        # колонки берём из самой строки шифров
        best_ri, best_n = None, 0
        for ri, cells in enumerate(data):
            n = sum(1 for c in cells if c and CODE_RE.fullmatch(squash(c)))
            if n > best_n:
                best_n, best_ri = n, ri
        if best_ri is None:
            return None, None, None
        cells = sorted([c for c in rows[best_ri].cells if c is not None],
                       key=lambda c: c[0])
        idx = {round(c[0], 1): i for i, c in enumerate(rows[best_ri].cells)
               if c is not None}
        first = next((i for i, c in enumerate(cells)
                      if CODE_RE.fullmatch(squash(data[best_ri][idx[round(c[0], 1)]]))), 0)
        spans = [{"x0": c[0], "x1": c[2], "code": None} for c in cells[first:]]
        y_bottom = rows[best_ri].bbox[3]
        anchor = best_ri

    # узкие «колонки» в 2–10 pt — артефакт рамок PDF, а не расценки
    spans = [s for s in spans if s["x1"] - s["x0"] >= 12] or spans
    # задвоенные рамки: две колонки почти одна в другой — оставляем внутреннюю (узкую)
    def _ov(a, b):
        return max(0.0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]))
    keep = []
    for s_ in spans:
        w = s_["x1"] - s_["x0"]
        if any(o is not s_ and (o["x1"] - o["x0"]) < w and _ov(s_, o) > 0.7 * (o["x1"] - o["x0"])
               for o in spans):
            continue
        keep.append(s_)
    spans = keep or spans
    band_top = _collect_codes(page, table, spans, y_bottom)
    if not any(s["code"] and CODE_RE.fullmatch(s["code"]) for s in spans):
        return None, None, None
    # колонка левее первого шифра без шифра — это «Ед. измер.», не расценка
    first_x = min(s["x0"] for s in spans if s["code"] and CODE_RE.fullmatch(s["code"]))
    spans[:] = [s for s in spans if not (not s["code"] and s["x1"] <= first_x + 2)]
    return band_top, y_bottom, spans


def label_columns(table):
    """x-границы служебных колонок (Код / Наименование / Ед.измер.) по самой полной строке."""
    rows, data = table.rows, table.extract()
    best, best_n = None, 0
    for ri, r in enumerate(rows):
        cells = [c for c in r.cells if c is not None]
        if len(cells) > best_n:
            best_n, best = len(cells), sorted(cells, key=lambda c: c[0])
    return best or []
