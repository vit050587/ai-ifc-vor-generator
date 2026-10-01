"""Разбор таблиц норм и расценок сборника ТСН в структуру.

Числа берутся из клеточной сетки PDF, без LLM. Всё, что не удалось
разобрать однозначно, попадает в `issues` и не подменяется догадкой.
"""
import re, sys, collections
import pdfplumber
from src.tools.tsn.tsn_cfg import N, NE
from src.tools.tsn.grid import group_lines, in_bbox, outside_bboxes, find_code_row, CODE_RE

TABLE_RE   = re.compile(rf"^Таблица\s+({NE}-\d+)\s*\.?\s*(.*)$")
OTDEL_RE   = re.compile(r"^Отдел\s+\d+\.")
RAZDEL_RE  = re.compile(r"^Раздел\s+\d+\.")
CONT_RE    = re.compile(r"\(продолжени[ея]\)", re.I)

# Заголовки секций внутри таблицы. Порядок важен: сначала самые длинные.
SECTIONS = [
    ("Материальные ресурсы, не учтенные в расценках", "materials_not_included_in_rate"),
    ("Оборудование, не учтенное в расценках",         "equipment_not_included_in_rate"),
    ("Машины и механизмы",                            "machines"),
    ("Материальные ресурсы",                          "materials"),
    ("Оборудование",                                  "equipment"),
]

# Итоговые ("жирные") строки расценки.
SUMMARY = {
    "прямые затраты:":                 "direct_costs_rub",
    "прямые затраты":                  "direct_costs_rub",
    "заработная плата рабочих":        "wages_rub",
    "эксплуатация машин":              "machine_operation_rub",
    "в том числе: заработная плата":   "machine_operation_wages_rub",
    "в том числе заработная плата":    "machine_operation_wages_rub",
    "материальные ресурсы":            "materials_rub",
    "затраты труда рабочих":           "labor_hours",
}

def _join(a, b):
    """Склейка перенесённого текста: «40-» + «60 мм» -> «40-60 мм»."""
    if not b:
        return a
    if not a:
        return b
    return (a + b) if a.endswith("-") else (a + " " + b)


def norm_ws(s):
    return re.sub(r"\s+", " ", (s or "").replace("\n", " ")).strip()

def parse_value(raw):
    """Ячейка -> число / None / признак «по проекту»."""
    s = norm_ws(raw)
    if s in ("", "-", "—", "–"):
        return None
    if s in ("П", "П.", "Π"):
        return {"by_project": True}
    t = s.replace(" ", "").replace(" ", "").replace(",", ".")
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        v = float(t)
        return int(v) if v.is_integer() and abs(v) < 1e15 and "." not in t else v
    return {"raw": s}


def header_labels(table, y_limit, data_x0):
    """Метки шапки над строкой шифров, сгруппированные по x-пролёту.

    Фрагменты одного пролёта склеиваются в одну метку; пустая ячейка того же
    пролёта разделяет уровни (так отличаются 'прямоугольных' и 'гладких'
    в таблицах с трёхуровневой шапкой)."""
    ex = table.extract()
    by_span = {}
    for ri, row in enumerate(table.rows):
        for ci, cell in enumerate(row.cells):
            if cell is None or cell[0] < data_x0 - 2 or cell[3] > y_limit + 1:
                continue
            by_span.setdefault((round(cell[0], 1), round(cell[2], 1)), []).append(
                (cell[1], norm_ws(ex[ri][ci]))
            )
    labels = []
    for (x0, x1), items in by_span.items():
        items.sort()
        buf = []
        for top, txt in items:
            if txt:
                buf.append((top, txt))
            elif buf:
                labels.append(_label(x0, x1, buf))
                buf = []
        if buf:
            labels.append(_label(x0, x1, buf))
    return labels


def _label(x0, x1, buf):
    """Метка шапки хранит и склеенный текст, и исходные ячейки: по их границам
    потом отделяется подпись измерения от названия варианта."""
    return {"x0": x0, "x1": x1, "top": buf[0][0],
            "text": " ".join(t for _, t in buf),
            "parts": [t for _, t in buf]}


def column_header_path(labels, span):
    path = [l for l in labels
            if l["x0"] <= span["x0"] + 2 and l["x1"] >= span["x1"] - 2]
    path.sort(key=lambda l: l["top"])
    return path


def split_section_suffix(name):
    """Отделяет прилипший к имени заголовок секции: имя -> (имя, секция|None)."""
    n = norm_ws(name)
    for phrase, key in SECTIONS:
        if n == phrase:
            return "", key
        if n.endswith(" " + phrase):
            return n[: -len(phrase) - 1].strip(), key
    return n, None


def parse_body(table, y_start, spans, label_x):
    """Строки таблицы ниже шифров -> список логических строк."""
    ex = table.extract()
    ncols = len(spans)
    rows_out, cur = [], None

    def flush():
        nonlocal cur
        if cur is None:
            return
        name, sec = split_section_suffix(cur["name"])
        cur["name"] = name
        rows_out.append(cur)
        if sec:
            rows_out.append({"section_header": sec})
        cur = None

    for ri, row in enumerate(table.rows):
        if row.bbox[1] < y_start - 1:
            continue
        code = name = unit = ""
        vals = [None] * ncols
        has_val = False
        for ci, cell in enumerate(row.cells):
            if cell is None:
                continue
            txt = norm_ws(ex[ri][ci])
            cx = (cell[0] + cell[2]) / 2
            if cx < label_x["name_x0"]:
                code = code or txt
            elif cx < label_x["unit_x0"]:
                name = (name + " " + txt).strip()
            elif cx < spans[0]["x0"] - 2:
                unit = unit or txt
            else:
                for si, s in enumerate(spans):
                    if s["x0"] - 2 <= cx <= s["x1"] + 2:
                        raw = (ex[ri][ci] or "").strip()
                        if "\n" in raw and re.fullmatch(r"[\d\s,.\-]+", raw):
                            txt = raw.split("\n")[0].strip()   # ячейка задвоенной рамки на 2 строки
                        if txt and (vals[si] is None or " " in vals[si]):
                            vals[si] = txt
                            has_val = True
                        break
        if code or has_val:
            flush()
            cur = {"code": code, "name": name, "unit": unit, "values": vals}
        elif name or unit:
            # перенос строки: единица измерения тоже рвётся («маш.-» + «ч»)
            if cur is None:
                cur = {"code": "", "name": name, "unit": unit,
                       "values": [None] * ncols}
            else:
                cur["name"] = _join(cur["name"], name)
                if unit and unit != cur["unit"]:
                    cur["unit"] = _join(cur["unit"], unit)
    flush()
    return rows_out


def parse_words(lines, spans, label_x):
    """То же, что parse_body, но по строкам слов (для хвостов из обрывков рамок)."""
    ncols = len(spans)
    rows_out, cur = [], None

    def flush():
        nonlocal cur
        if cur is None:
            return
        name, sec = split_section_suffix(cur["name"])
        cur["name"] = name
        rows_out.append(cur)
        if sec:
            rows_out.append({"section_header": sec})
        cur = None

    for ln in lines:
        code = name = unit = ""
        vals = [None] * ncols
        has_val = False
        for w in ln["words"]:
            cx = (w["x0"] + w["x1"]) / 2
            txt = w["text"]
            if cx < label_x["name_x0"]:
                code = (code + txt) if code else txt
            elif cx < label_x["unit_x0"]:
                name = (name + " " + txt).strip()
            elif cx < spans[0]["x0"] - 2:
                unit = (unit + txt) if unit else txt
            else:
                for si, sp in enumerate(spans):
                    if sp["x0"] - 2 <= cx <= sp["x1"] + 2:
                        vals[si] = (vals[si] + " " + txt) if vals[si] else txt
                        has_val = True
                        break
        if code or has_val:
            flush()
            cur = {"code": code, "name": name, "unit": unit, "values": vals}
        elif name or unit:
            if cur is None:
                cur = {"code": "", "name": name, "unit": unit, "values": [None] * ncols}
            else:
                cur["name"] = _join(cur["name"], name)
                if unit and unit != cur["unit"]:
                    cur["unit"] = _join(cur["unit"], unit)
    flush()
    return rows_out


# ---------------------------------------------------------------- обход книги

def _is_wide(t):
    return (t.bbox[2] - t.bbox[0]) > 0.6 * (t.page.bbox[2] - t.page.bbox[0])


def _is_fragment(t):
    """Узкий одностолбцовый кусок: хвост таблицы, у которого рамки рисованы
    отдельными прямоугольниками на каждую графу."""
    try:
        ex = t.extract()
    except Exception:
        return False
    return bool(ex) and max(len(r) for r in ex) == 1 and not _is_wide(t)


def _is_text_frame(t):
    try:
        ex = t.extract()
    except Exception:
        return False
    if not ex:
        return False
    cells = [norm_ws(c) for r in ex for c in r if c]
    code_any = re.compile(rf"(?<![\d.-]){NE}-\d+-\s*\d+")
    if any(code_any.search(c) for c in cells):
        return False                                   # есть шифры расценок — это таблица норм
    if max(len(r) for r in ex) <= 2:
        return _is_wide(t)                             # узкий столбик — обрывок таблицы, не рамка
    if len(ex) > 8:
        return False                                   # большая сетка — данные, не заголовок
    return any(re.match(r"^(Таблица\s+\S+-\d+|Отдел\s+\d+\.|Раздел\s+\d+\.|Измеритель|Состав работ)", c)
               for c in cells)


def page_elements(page):
    """Элементы страницы в порядке сверху вниз: prose-строки и сетки таблиц."""
    tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
    # Однocтолбцовая «сетка» — это рамка вокруг обычного текста (заголовки
    # «Отдел / Раздел / Таблица / Измеритель» в части сборников). Читаем её как текст.
    tables = [t for t in tables if not _is_text_frame(t)]
    frags = [t for t in tables if _is_fragment(t)]
    tables = [t for t in tables if not _is_fragment(t)]
    clusters = []                                  # объединяем обрывки, лежащие в одной полосе
    for f in sorted(frags, key=lambda t: t.bbox[1]):
        b = list(f.bbox)
        for c in clusters:
            if b[1] < c[3] - 1 and b[3] > c[1] + 1:
                c[0], c[1], c[2], c[3] = min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3])
                break
        else:
            clusters.append(b)
    words = page.extract_words()
    boxes = [t.bbox for t in tables] + [tuple(c) for c in clusters]
    prose = outside_bboxes(words, boxes)
    els = [("table", t.bbox[1], t) for t in tables]
    for c in clusters:
        els.append(("wtail", c[1], {"bbox": tuple(c), "lines": group_lines(in_bbox(words, tuple(c), pad=2))}))
    for ln in group_lines(prose):
        if ln["bottom"] > page.height - 45:      # колонтитул с номером страницы
            continue
        els.append(("prose", ln["top"], ln))
    els.sort(key=lambda e: e[1])
    return els


HEADER_WORDS = ("Наименование", "статей затрат", "измер.")


def is_header_fragment(table):
    """Сетка без шифров, но с подписями служебных колонок — это начало шапки
    следующего блока, перенесённое на предыдущую страницу, а не хвост данных."""
    flat = " ".join(norm_ws(c) for row in table.extract() for c in row if c)
    return any(w in flat for w in HEADER_WORDS)


def label_bounds(table, spans):
    """x-границы служебных колонок Код / Наименование / Ед.измер.

    Берём все ячейки левее расценок, выкидываем артефакты рамок (узкие и
    задвоенные ячейки) и строим цепочку непересекающихся колонок слева направо."""
    lim = spans[0]["x0"] + 2
    cells = {(round(c[0], 1), round(c[2], 1)) for row in table.rows for c in row.cells
             if c is not None and c[2] <= lim and c[2] - c[0] >= 12}
    cells = sorted(cells)

    def ov(a, b):
        return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    inner = [c for c in cells if not any(o != c and (o[1] - o[0]) < (c[1] - c[0])
                                         and ov(c, o) > 0.7 * (o[1] - o[0]) for o in cells)]
    chain = []
    for c in sorted(inner):
        if not chain or c[0] >= chain[-1][1] - 3:
            chain.append(c)
    if len(chain) >= 3:
        return {"name_x0": chain[1][0], "unit_x0": chain[2][0]}
    if len(chain) == 2:
        return {"name_x0": chain[1][0], "unit_x0": chain[1][1]}
    return {"name_x0": table.bbox[0] + 55, "unit_x0": spans[0]["x0"] - 35}


def parse_document(pdf_path):
    pdf = pdfplumber.open(pdf_path)
    tables, issues = [], []
    ctx = {"otdel": None, "razdel": None}
    cur_tbl = None
    cur_blk = None
    pending_header = None      # шапка блока, начавшаяся на предыдущей странице
    first_body_page = None
    mode = None            # 'composition' | 'unit' | None
    razdel_open = False

    def close_table():
        nonlocal cur_tbl, cur_blk
        if cur_tbl:
            tables.append(cur_tbl)
        cur_tbl, cur_blk = None, None

    for pno, page in enumerate(pdf.pages, start=1):
        els = page_elements(page)
        n_tables = sum(1 for k, _, _ in els if k == "table")
        seen_tables = 0
        for kind, _, el in els:
            if kind == "table":
                seen_tables += 1
            if kind == "wtail":
                if cur_blk is not None:
                    tail = parse_words(el["lines"], cur_blk["spans"], cur_blk["label_x"])
                    if (tail and "section_header" not in tail[0] and not tail[0]["code"]
                            and not tail[0]["unit"] and not any(v for v in tail[0]["values"])):
                        frag = tail.pop(0)
                        prev = next((r for r in reversed(cur_blk["rows"]) if "section_header" not in r), None)
                        if prev is not None and frag["name"]:
                            prev["name"] = norm_ws(prev["name"] + " " + frag["name"])
                            prev["name"], sec = split_section_suffix(prev["name"])
                            if sec:
                                cur_blk["rows"].append({"section_header": sec})
                    cur_blk["rows"] += tail
                    if cur_tbl and pno not in cur_tbl["pages"]:
                        cur_tbl["pages"].append(pno)
                continue
            if kind == "prose":
                txt = norm_ws(el["text"])
                if not txt or "....." in txt:
                    continue
                if OTDEL_RE.match(txt):
                    ctx["otdel"], ctx["razdel"] = txt, None
                    mode, razdel_open = None, False
                    continue
                if RAZDEL_RE.match(txt):
                    ctx["razdel"] = txt
                    mode, razdel_open = None, True
                    continue
                m = TABLE_RE.match(txt)
                if m:
                    razdel_open = False
                    if CONT_RE.search(txt) or (not m.group(2) and cur_tbl
                                               and cur_tbl["code"] == m.group(1)):
                        mode = None
                        continue
                    close_table()
                    pending_header = None
                    cur_tbl = {
                        "code": m.group(1),
                        "title": m.group(2).strip(),
                        "otdel": ctx["otdel"],
                        "razdel": ctx["razdel"],
                        "work_composition": [],
                        "unit_of_measure": None,
                        "pages": [pno],
                        "column_blocks": [],
                    }
                    mode = "title"
                    continue
                if txt.startswith("Состав работ"):
                    mode = "composition"
                    rest = txt.split(":", 1)[1].strip() if ":" in txt else ""
                    if cur_tbl and rest:
                        cur_tbl["work_composition"].append(rest)
                    continue
                if txt.startswith("Измеритель"):
                    mode = "unit"
                    rest = txt.split(":", 1)[1].strip() if ":" in txt else ""
                    if cur_tbl and rest:
                        cur_tbl["unit_of_measure"] = rest
                    continue
                # продолжение предыдущего prose-блока
                if razdel_open and ctx["razdel"]:
                    ctx["razdel"] += " " + txt
                elif mode == "title" and cur_tbl:
                    cur_tbl["title"] = (cur_tbl["title"] + " " + txt).strip()
                elif mode == "composition" and cur_tbl:
                    if re.match(r"^\d+\.", txt) or not cur_tbl["work_composition"]:
                        cur_tbl["work_composition"].append(txt)
                    else:
                        cur_tbl["work_composition"][-1] += " " + txt
                elif mode == "unit" and cur_tbl:
                    if len(txt) < 70 and not re.search(r"\d+\.\d+-\d+-\d+|\d{7,}", txt) \
                            and len(re.findall(r"\d+[,.]?\d*", txt)) < 3 and not txt.startswith(("-", "руб")) \
                            and len(cur_tbl["unit_of_measure"] or "") < 120:
                        cur_tbl["unit_of_measure"] = ((cur_tbl["unit_of_measure"] or "")
                                                      + " " + txt).strip()
                    else:
                        mode = None                        # дальше не измеритель, а обрывок таблицы
                continue

            # --- сетка
            t = el
            band_top, y_data, spans = find_code_row(page, t)
            if spans is None and is_header_fragment(t):
                # обрывок шапки переносится на следующую страницу только
                # если он в самом низу текущей
                pending_header = ({"page": pno, "table": t}
                                  if seen_tables == n_tables else None)
                continue
            if spans:
                if first_body_page is None:
                    first_body_page = pno
                prefixes = ["-".join(s["code"].split("-")[:2])
                            for s in spans if s["code"] and CODE_RE.fullmatch(s["code"])]
                tcode = collections.Counter(prefixes).most_common(1)[0][0]
                for s_ in spans:
                    if s_["code"] and "-".join(s_["code"].split("-")[:2]) != tcode:
                        issues.append({"type": "rate_code_typo", "page": pno,
                                       "printed": s_["code"], "table": tcode,
                                       "note": "шифр расценки не совпадает с номером таблицы"})
                if cur_tbl is None or cur_tbl["code"] != tcode:
                    issues.append({"type": "orphan_grid", "page": pno, "code": tcode,
                                   "note": "сетка с шифрами без заголовка «Таблица»"})
                    close_table()
                    cur_tbl = {"code": tcode, "title": "", "otdel": ctx["otdel"],
                               "razdel": ctx["razdel"], "work_composition": [],
                               "unit_of_measure": None, "pages": [pno],
                               "column_blocks": []}
                lb = label_bounds(t, spans)
                hdrs = header_labels(t, band_top, spans[0]["x0"])
                if (pending_header is not None
                        and pending_header["page"] == pno - 1
                        and seen_tables == 1):
                    ph = pending_header["table"]
                    prev = header_labels(ph, ph.bbox[3], spans[0]["x0"])
                    # шапка с прошлой страницы всегда выше по смыслу
                    hdrs = [dict(l, top=l["top"] - 10000) for l in prev] + hdrs
                pending_header = None
                body = parse_body(t, y_data, spans, lb)
                # строки сетки бывают склеены (задвоенные рамки) — пробуем прочитать тело по словам
                # и берём вариант, где больше заполненных чисел
                def _score(rows):
                    return sum(1 for r in rows if "values" in r for v in r["values"] if v)
                wb = in_bbox(page.extract_words(), (t.bbox[0], y_data - 1, t.bbox[2], t.bbox[3]), pad=2)
                alt = parse_words(group_lines(wb), spans, lb)
                if _score(alt) > _score(body) * 1.2:
                    body = alt
                    issues.append({"type": "body_parsed_by_words", "page": pno, "table": tcode})
                cur_blk = {
                    "spans": spans, "label_x": lb, "page": pno,
                    "headers": hdrs,
                    "rows": body,
                }
                cur_tbl["column_blocks"].append(cur_blk)
                if pno not in cur_tbl["pages"]:
                    cur_tbl["pages"].append(pno)
            elif cur_blk is not None:
                tail = parse_body(t, t.bbox[1] - 1, cur_blk["spans"],
                                  cur_blk["label_x"])
                if (tail and "section_header" not in tail[0]
                        and not tail[0]["code"] and not tail[0]["unit"]
                        and not any(v for v in tail[0]["values"])):
                    frag = tail.pop(0)
                    prev = next((r for r in reversed(cur_blk["rows"])
                                 if "section_header" not in r), None)
                    if prev is not None and frag["name"]:
                        prev["name"] = norm_ws(prev["name"] + " " + frag["name"])
                        prev["name"], sec = split_section_suffix(prev["name"])
                        if sec:
                            cur_blk["rows"].append({"section_header": sec})
                    elif frag["name"]:
                        tail.insert(0, frag)
                cur_blk["rows"] += tail
                if cur_tbl and pno not in cur_tbl["pages"]:
                    cur_tbl["pages"].append(pno)
    close_table()
    # Заголовки из оглавления не имеют сеток — это не таблицы.
    real = [t for t in tables if t["column_blocks"]]
    for t in tables:
        if not t["column_blocks"] and first_body_page and t["pages"][0] >= first_body_page:
            issues.append({"type": "table_without_grid", "page": t["pages"][0],
                           "table": t["code"], "note": "заголовок таблицы без сетки расценок"})
    return real, issues


if __name__ == "__main__":
    tbs, iss = parse_document(sys.argv[1])
    print(f"таблиц: {len(tbs)}, проблем: {len(iss)}")
