"""Сверка двух типовых ВОР: сформированного по модели и ВОР исполнителя.

Берёт попарное сопоставление строк коллег (``comparison_typical.compare_workbooks``:
часть здания → раздел → подраздел → группа → работа) и добавляет к каждой
строке понятный вывод — где правильнее и почему:

* ✅ совпадает            — объёмы равны (с допуском);
* 🟢 вероятно верен сформированный объём — объём измерен по модели, у исполнителя меньше/нет;
* 🟡 вероятно верно у исполнителя — работа не моделируется, проверить по РД;
* 🔵 разная разбивка      — по подразделу в сумме совпадает, иначе разнесено
                            по группам/толщинам;
* ❗ решение за сметчиком — расхождение, которое по данным не разрешить;
* ℹ️ не сравнивается      — арматура: в модели её нет, в оба ВОР она взята
                            из ВОР исполнителя.

Правила детерминированные (без LLM) и не привязаны к объекту: решение
принимается по виду работы (``mapping.classify_work``), источнику сформированного
объёма (столбец «Обоснование») и знаку/величине расхождения.

Результат — ``Сверка_ВОР.xlsx`` (листы «Сверка» и «Итог») и словарь сводки.
"""

from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import mapping

RECONCILE_FILENAME = "Сверка_ВОР.xlsx"

# Допуск: расхождение в пределах 3 % (или ничтожное по модулю) — совпадение.
REL_TOL = 0.03
ABS_TOL = 0.005

# Виды работ, объём которых измеряется по модели IFC.
MODELLED = {"formwork", "concrete", "thermoliner", "volume", "hydro_area", "count"}

VERDICTS = OrderedDict([
    ("parameters", ("Различаются сметные параметры", "FFF2CC")),
    ("parameters_missing", ("Нет значений сметных параметров", "EDEDED")),
    ("match", ("✅ Совпадает", "C6EFCE")),
    ("ours", ("🟢 Вероятно верен сформированный объём", "E2EFDA")),
    ("customer", ("🟡 Вероятно верно у исполнителя", "FFF2CC")),
    ("split", ("🔵 Разная разбивка", "DDEBF7")),
    ("check", ("❗ Решение за сметчиком", "F8CBAD")),
    ("info", ("ℹ️ Не сравнивается", "EDEDED")),
])


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def _category(row: dict) -> str:
    """Вид объёма строки: для ресурса — бетон/арматура или вид родительской работы."""
    name = row.get("generatedWork") or row.get("customerWork") or row.get("work") or ""
    if row.get("kind") == "resource":
        res = mapping.classify_resource(name)
        if res in ("concrete", "rebar"):
            return res
        return mapping.classify_work(row.get("parentWork")) or "other"
    return mapping.classify_work(name)


def _source(reason: str) -> str:
    """Откуда объём сформированной ВОР (по столбцу «Обоснование объема работ»)."""
    r = (reason or "").strip()
    for key, label in (("Модель IFC", "модель"), ("ПОС", "ПОС"), ("ПЗ", "ПЗ"), ("ПД", "ПД"),
                       ("ВОР", "ВОР исполнителя")):
        if r.startswith(key) or f"{key}:" in r:
            return label
    return "модель" if r else ""


def _explain(row: dict) -> None:
    """Добавляет к строке поля verdict / why."""
    g, c = row.get("generatedQuantity"), row.get("customerQuantity")
    unit = row.get("unit") or ""
    cat = _category(row)
    src = _source(row.get("modelReason"))
    status = row.get("status")
    row["category"] = cat
    row["ourSource"] = src

    def put(verdict: str, why: str) -> None:
        row["verdict"], row["why"] = verdict, why

    if row.get('coveredByAggregate'):
        put('info', 'Строка учтена в суммарном сравнении по подразделу; объём между группами не распределяется.')
        return
    if g is not None and c is None:
        put('check', f'В сформированной ВОР {_fmt(g)} {unit}; сопоставимый объём заказчика отсутствует. По отсутствию пары нельзя определить правильность объёма.')
        return
    parameters = row.get('parameterComparison') or {}
    if status == 'quantity_estimate':
        put('info', 'Показана геометрическая оценка площади, а не подтверждённая площадь контакта с опалубкой. Для балок учитываются низ и вертикальные грани; примыкания к соседним конструкциям не исключены. Численная разница приведена справочно.')
        return
    if parameters.get('status') in ('mismatch', 'missing_data'):
        from ..estimate_parameter_comparison import explanation
        put('parameters' if parameters['status'] == 'mismatch' else 'parameters_missing', explanation(parameters))
        return

    if g is None and c is None:
        row["verdict"] = None
        return

    if cat == "rebar":
        if g is not None and c is not None and not _close(g, c):
            put("check", f"Арматура в оба ВОР взята из ВОР исполнителя и должна совпадать, а здесь {_fmt(g)} и "
                         f"{_fmt(c)} {unit}: проверьте разнесение арматуры по разделам.")
        else:
            put("info", "Арматура в модели не моделируется — в оба ВОР она взята из ВОР исполнителя, "
                        "поэтому по модели не проверяется.")
        return

    if status in ("ifc_quantity_incomplete", "project_quantity_incomplete"):
        side = "в модели" if status == "ifc_quantity_incomplete" else "у исполнителя"
        put("check", f"Неполные данные {side} — объём посчитан частично, сравнение неточное.")
        return

    if status == 'group_composition_difference':
        put('split', 'Итоговые количества равны, но у заказчика объединены разные толщины. Распределение по толщинам не сравнивается.')
        return

    if g is not None and c is not None:
        diff = c - g
        pct = diff / abs(g) * 100 if g else None
        p = f"{abs(pct):.0f}%" if pct is not None else ""
        if _close(g, c):
            put("match", "Объёмы совпадают" + (f" (разница {abs(pct):.1f}%)." if pct else "."))
            return
        more = diff > 0
        d = f"{'больше' if more else 'меньше'} на {_fmt(abs(diff))} {unit} ({p})"
        if src not in ("", "модель"):
            put("check", f"Объём сформированной ВОР взят из документа ({src}), у исполнителя {d}. Сверьте с рабочей документацией.")
        elif cat in ("concrete", "volume", "thermoliner"):
            if more:
                put("check", f"У исполнителя {d}. Объём сформированной ВОР — измерение по модели; если в модели есть не все "
                             f"конструкции, прав исполнитель. Проверьте по чертежам КЖ.")
            else:
                put("ours", f"У исполнителя {d}. По модели объём больше — вероятно, исполнитель учёл не все "
                            f"конструкции этой группы.")
        elif cat == "formwork":
            if 'ТСН-2001.3-6, п. 2.8' in (row.get('modelReason') or ''):
                put("check", f"У исполнителя {d}. В модели принята горизонтальная проекция лестничных конструкций "
                             "по ТСН-2001.3-6, п. 2.8; вертикальные грани и наклонный низ не прибавляются. "
                             "Состав выборки и источники указаны в обосновании объёма.")
            else:
                put("check", f"У исполнителя {d}. Площадь опалубки зависит от методики подсчёта (торцы, проёмы, "
                             f"нижняя поверхность плит) — сверьте методику.")
        elif cat == "hydro_area":
            put("check", f"У исполнителя {d}. В гидроизоляции часто учитывают нахлёсты, заходы на стены и "
                         f"несколько слоёв — проверьте по узлам.")
        elif cat == "count":
            put("check", f"Количество различается: в сформированной ВОР {_fmt(g)}, у исполнителя {_fmt(c)} {unit}. "
                         f"Проверьте спецификацию.")
        else:
            put("check", f"У исполнителя {d}. Проверьте по рабочей документации.")
        return

    if g is not None:   # только в сформированной ВОР
        if cat in MODELLED or src == "модель":
            put("ours", f"По модели {_fmt(g)} {unit}, а у исполнителя этой строки нет — возможен пропуск "
                        f"у исполнителя или учтено в другой строке. Проверьте.")
        else:
            put("ours", f"Объём сформированной ВОР {_fmt(g)} {unit} взят из документа ({src or 'документы'}), "
                        f"у исполнителя этой строки нет.")
        return

    # только у исполнителя
    if cat in MODELLED:
        put("check", f"У исполнителя {_fmt(c)} {unit}, а в модели данных нет: либо модель неполная, "
                     f"либо у исполнителя лишнее. Проверьте по чертежам.")
    else:
        put("customer", f"Эта работа в модели не моделируется (швы, гидрошпонка, закладные, материалы и т.п.) — "
                        f"объём исполнителя {_fmt(c)} {unit} по модели не проверить, сверьте с рабочей документацией.")


def _detect_splits(rows: List[dict]) -> None:
    """Если по подразделу в сумме совпадает, а построчно нет — это разная разбивка по группам."""
    tot = defaultdict(lambda: [0.0, 0.0, False, False, []])
    for r in rows:
        if r.get("status") == "separator" or not r.get("verdict") or r["verdict"] == "info":
            continue
        name = (r.get("generatedWork") or r.get("customerWork") or "").lower().strip()
        key = (r.get("part"), r.get("section"), r.get("subsection"), r.get("kind"),
               (r.get("parentWork") or "").lower().strip(), name, r.get("unit"))
        t = tot[key]
        if r.get("generatedQuantity") is not None:
            t[0] += r["generatedQuantity"]; t[2] = True
        if r.get("customerQuantity") is not None:
            t[1] += r["customerQuantity"]; t[3] = True
        t[4].append(r)
    for (_, _, _, _, _, _, unit), (g, c, hg, hc, members) in tot.items():
        if len(members) < 2 or not (hg and hc) or not _close(g, c):
            continue
        if any(m.get("status") in ("ifc_quantity_incomplete", "project_quantity_incomplete") for m in members):
            continue
        if all(m["verdict"] == "match" for m in members):
            continue
        for m in members:
            if m["verdict"] != "match":
                m["verdict"] = "split"
                m["why"] = (f"По подразделу в сумме совпадает (в сформированной ВОР {_fmt(g)}, у исполнителя {_fmt(c)} {unit}) — "
                            f"различается только разбивка по группам/толщинам.")


def explain(rows: List[dict]) -> Dict[str, object]:
    """Добавляет выводы к строкам сравнения и возвращает сводку."""
    for r in rows:
        if r.get("status") != "separator":
            _explain(r)
    _detect_splits(rows)
    counts = OrderedDict((k, 0) for k in VERDICTS)
    for r in rows:
        if r.get("verdict"):
            counts[r["verdict"]] += 1
    return {"counts": counts, "total": sum(counts.values())}


def _summary_lines(summary: dict, rows: List[dict]) -> List[str]:
    c = summary["counts"]
    lines = [
        f"Сравнено строк: {summary['total']}.",
        f"✅ Совпадает: {c['match']}.",
        f"🟢 Вероятно верен сформированный объём (по модели): {c['ours']}.",
        f"🟡 Вероятно верно у исполнителя (работа не моделируется): {c['customer']}.",
        f"🔵 Разная разбивка (в сумме совпадает): {c['split']}.",
        f"❗ Решение за сметчиком: {c['check']}.",
        f"ℹ️ Не сравнивается (арматура из ВОР исполнителя): {c['info']}.",
        "",
        "Как читать: объём сформированной ВОР берётся из модели или указанного документа; ВОР исполнителя приведён к той же",
        "типовой форме. Допуск совпадения — 3%. Выводы «вероятно верно» — подсказка, где искать",
        "ошибку, окончательное решение принимает сметчик.",
    ]
    top = sorted((r for r in rows if r.get("verdict") in ("check", "ours")
                  and r.get("generatedQuantity") is not None and r.get("customerQuantity") is not None),
                 key=lambda r: -abs((r["customerQuantity"] or 0) - (r["generatedQuantity"] or 0)))[:10]
    if top:
        lines += ["", "Самые большие расхождения:"]
        for r in top:
            lines.append(f"• {r.get('subsection') or r.get('section') or ''} — "
                         f"{r.get('generatedWork') or r.get('customerWork')}: в сформированной ВОР {_fmt(r['generatedQuantity'])}, "
                         f"у исполнителя {_fmt(r['customerQuantity'])} {r.get('unit') or ''}")
    return lines


def write_excel(rows: List[dict], summary: dict, path: str, *, workbook=None) -> None:
    wb = workbook if workbook is not None else Workbook()
    ws = wb.create_sheet("Сверка") if workbook is not None else wb.active
    ws.title = "Сверка"
    head = ["Часть здания", "Раздел", "Подраздел", "Группа", "Наименование работ", "Ед. изм.",
            "Объём сформированной ВОР (модель)", "Объём исполнителя", "Расхождение", "Расхождение, %", "Вывод", "Почему",
            "Откуда объём сформированной ВОР", "Строки: сформированная ВОР / ВОР исполнителя"]
    ws.append(head)
    ws.row_dimensions[1].height = 60
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rows:
        if r.get("coveredByAggregate") or r.get("status") == "separator" or not r.get("verdict"):
            continue
        g, c = r.get("generatedQuantity"), r.get("customerQuantity")
        diff = None if g is None or c is None or r.get("status") in ("ifc_quantity_incomplete", "project_quantity_incomplete") else round(c - g, 3)
        pct = None if diff is None or not g else round(diff / abs(g) * 100, 1)
        label, color = VERDICTS[r["verdict"]]
        ws.append([r.get("part"), r.get("section"), r.get("subsection"), r.get("elementGroup"),
                   r.get("generatedWork") or r.get("customerWork"), r.get("unit"), g, c, diff, pct,
                   label, r.get("why"), r.get("ourSource"),
                   f"{r.get('generatedSourceRow') or '—'} / {r.get('customerSourceRow') or '—'}"])
        fill = PatternFill("solid", fgColor=color)
        for cell in ws[ws.max_row][10:12]:
            cell.fill = fill
        for cell in ws[ws.max_row]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate([16, 22, 26, 22, 40, 8, 24, 18, 16, 16, 32, 60, 28, 28], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=9):
        for cell in row:
            cell.number_format = "0.00000" if str(ws.cell(cell.row,6).value or "").rstrip(".") == "т" else "0.00"
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    ws2 = wb.create_sheet("Итог")
    for line in _summary_lines(summary, rows):
        ws2.append([line])
    ws2.column_dimensions["A"].width = 120
    ws2["A1"].font = Font(bold=True)
    wb.save(path)


def build(generated_path: str, customer_path: str, output_path: str) -> Dict[str, object]:
    """Сверяет два типовых ВОР и пишет ``Сверка_ВОР.xlsx``. Возвращает сводку."""
    from src.services.comparison_typical import compare_workbooks
    rows = compare_workbooks(generated_path, customer_path)["rows"]
    summary = explain(rows)
    write_excel(rows, summary, output_path)
    summary["file"] = output_path
    return summary
