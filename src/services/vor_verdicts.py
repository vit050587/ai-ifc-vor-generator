"""Выводы «где правильнее и почему» для сравнения ВОР исполнителя с сформированным перечнем.

Дополняет строки ``vor_comparison`` (сформированный перечень работ ↔ ВОР исполнителя)
полями ``verdict`` и ``why``. Правила детерминированные (без LLM), зависят
только от вида работы, наличия объёмов с обеих сторон и величины расхождения —
к конкретному объекту не привязаны.
"""

from collections import OrderedDict
from typing import Any, Dict, List, Optional

REL_TOL = 0.03      # расхождение до 3 % — совпадает
ABS_TOL = 0.01

VERDICTS = OrderedDict([
    ("match", ("✅ Совпадает", "C6EFCE")),
    ("ours", ("🟢 Вероятно верен сформированный объём", "E2EFDA")),
    ("customer", ("🟡 Вероятно верно у исполнителя", "FFF2CC")),
    ("check", ("❗ Решение за сметчиком", "F8CBAD")),
    ("info", ("ℹ️ Не сравнивается", "EDEDED")),
])

# Объём измеряется по модели (тело конструкций и их поверхности).
MEASURED = {"concrete", "opening", "prep", "stairs", "insulation"}
# Работы вне модели КР: перечень по модели их не содержит.
OUTSIDE = {"soil", "backfill", "steel", "piling", "railing"}


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def _one(r: Dict[str, Any]) -> None:
    ours, theirs = r.get("model_volume"), r.get("vor_volume")
    cat, unit, status = r.get("category") or "other", r.get("unit") or "", r.get("status") or ""

    def put(v: str, why: str) -> None:
        r["verdict"], r["why"] = v, why

    if r.get("status") in ("ifc_quantity_incomplete", "project_quantity_incomplete"):
        put("check", "Неполные данные: известна только часть объёма, требуется проверка.")
        return

    if cat == "rebar":
        if status == "нет в ВОР (оценка модели)":
            put("check", "У исполнителя арматуры нет, а в сформированном перечне объём — оценка по норме армирования "
                         "(в модели арматура не моделируется). Проверьте по спецификации КЖ.")
        else:
            put("info", "Арматура в модели не моделируется — её объём берётся из ВОР исполнителя, "
                        "поэтому по модели не проверяется.")
        return

    if ours is None and theirs is None:
        r["verdict"], r["why"] = None, ""
        return

    if ours is not None and theirs is not None:
        diff = theirs - ours
        p = f"{abs(diff) / abs(ours) * 100:.0f}%" if ours else ""
        if _close(ours, theirs):
            put("match", "Объёмы совпадают (в пределах 3%).")
            return
        d = f"{'больше' if diff > 0 else 'меньше'} на {_fmt(abs(diff))} {unit} ({p})"
        if cat in ("concrete", "prep", "stairs"):
            if diff > 0:
                put("check", f"У исполнителя {d}. Объём сформированного перечня — измерение по модели; если в модели есть не все "
                             f"конструкции, прав исполнитель. Проверьте по чертежам КЖ.")
            else:
                put("ours", f"У исполнителя {d}. По модели объём больше — вероятно, исполнитель учёл не все "
                            f"конструкции этой группы.")
        elif cat == "opening":
            put("check", f"У исполнителя {d}. Площадь опалубки зависит от методики подсчёта (торцы, проёмы, "
                         f"нижняя поверхность плит) — сверьте методику.")
        elif cat == "curing":
            put("check", f"У исполнителя {d}. Уход за бетоном считается по объёму бетона — сверьте с "
                         f"объёмом бетонирования.")
        elif cat == "waterproofing":
            put("check", f"У исполнителя {d}. В гидроизоляции часто учитывают нахлёсты, заходы на стены и "
                         f"несколько слоёв — проверьте по узлам.")
        else:
            put("check", f"У исполнителя {d}. Проверьте по рабочей документации.")
        return

    if status == "только в перечне" or (ours is not None and theirs is None):
        if cat == "curing":
            put("ours", "Уход за бетоном обязателен при бетонировании (по сборнику ТСН / ПОС), а у исполнителя "
                        "этой работы нет — вероятно, пропущена.")
        elif cat in MEASURED:
            put("ours", f"По модели {_fmt(ours)} {unit}, у исполнителя этой работы нет — возможен пропуск "
                        f"у исполнителя или учтено в другой строке. Проверьте.")
        else:
            put("check", "В сформированном перечне работа есть, у исполнителя нет — проверьте, не входит ли она у исполнителя "
                         "в состав другой работы.")
        return

    if status == "нет объёма в перечне" or (theirs and ours is None and status != "только в ВОР"):
        put("customer", f"Работа в сформированном перечне есть, но в модели её объём не посчитать (по проекту) — "
                        f"объём исполнителя {_fmt(theirs)} {unit} принять, сверив с рабочей документацией.")
        return

    if status == "только в ВОР":
        if cat in OUTSIDE:
            put("customer", f"Этих работ нет в модели КР (земляные работы, шпунт, сваи, металл) — сформированный перечень "
                            f"их не содержит; объём исполнителя {_fmt(theirs)} {unit} проверить по проекту.")
        elif cat in MEASURED:
            put("check", f"У исполнителя {_fmt(theirs)} {unit}, а в модели таких элементов нет: либо модель "
                         f"неполная, либо у исполнителя лишнее. Проверьте по чертежам.")
        else:
            put("customer", f"Работа в модели не моделируется (швы, гидрошпонка, закладные, материалы) — "
                            f"объём исполнителя {_fmt(theirs)} {unit} по модели не проверить, сверьте с РД.")
        return

    r["verdict"], r["why"] = None, ""


def explain(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Добавляет verdict/why к строкам сравнения и возвращает счётчики выводов."""
    counts = OrderedDict((k, 0) for k in VERDICTS)
    for r in rows:
        _one(r)
        if r.get("verdict"):
            counts[r["verdict"]] += 1
    return counts


def summary_lines(counts: Dict[str, int], rows: List[Dict[str, Any]]) -> List[str]:
    lines = [
        "Сверка сформированного перечня работ (по модели, ПОС, ПЗ и сборникам ТСН) с ВОР исполнителя",
        "",
        f"✅ Совпадает: {counts.get('match', 0)}",
        f"🟢 Вероятно верен сформированный объём: {counts.get('ours', 0)}",
        f"🟡 Вероятно верно у исполнителя: {counts.get('customer', 0)}",
        f"❗ Решение за сметчиком: {counts.get('check', 0)}",
        f"ℹ️ Не сравнивается (арматура из ВОР исполнителя): {counts.get('info', 0)}",
        "",
        "Как читать: объём сформированного перечня — измерение по цифровой модели; работы, которых в модели нет",
        "(швы, гидрошпонка, земляные работы и т.п.), проверяются по рабочей документации.",
        "Допуск совпадения — 3%. «Вероятно верно» — подсказка, где искать ошибку; решение за сметчиком.",
    ] + GUIDE
    top = sorted((r for r in rows if r.get("verdict") in ("check", "ours")
                  and r.get("model_volume") is not None and r.get("vor_volume") is not None),
                 key=lambda r: -abs(r["vor_volume"] - r["model_volume"]))[:10]
    if top:
        lines += ["", "Самые большие расхождения:"]
        for r in top:
            lines.append(f"• {r.get('label')} — {r.get('category_name')}: в сформированном перечне {_fmt(r['model_volume'])}, "
                         f"у исполнителя {_fmt(r['vor_volume'])} {r.get('unit') or ''}")
    return lines


ORDER = {"check": 0, "ours": 1, "customer": 2, "match": 3, "info": 4}
GUIDE = [
    "",
    "Какие листы смотреть:",
    "• «Сверка» — главное: каждая работа, объём сформированного перечня, объём исполнителя, вывод и почему. Сначала — что требует решения.",
    "• «Подробно — …» — то же самое в разрезах для сметчика (по группам, по видам конструкций, по материалам).",
    "• «Исходные строки ВОР» и «Исходные строки перечня» — откуда взяты числа (страница ВОР, расценка перечня).",
]


def write_simple_sheet(wb, rows: List[Dict[str, Any]], index: int = 1):
    """Лист «Сверка» для обычного пользователя: одна таблица, самое важное сверху."""
    from openpyxl.styles import Alignment, Font, PatternFill
    ws = wb.create_sheet("Сверка", index)
    head = ["Конструкция", "Работа", "Ед. изм.", "Объём сформированного перечня", "Объём исполнителя", "Расхождение", "Разница, %", "Вывод", "Почему"]
    ws.append(head)
    ws.row_dimensions[1].height = 45
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in sorted((r for r in rows if r.get("verdict")),
                    key=lambda r: (ORDER.get(r["verdict"], 9), str(r.get("label")), str(r.get("category_name")))):
        ours, theirs = r.get("model_volume"), r.get("vor_volume")
        partial = r.get("status") in ("ifc_quantity_incomplete", "project_quantity_incomplete")
        difference = None if ours is None or theirs is None or partial else round(theirs - ours, 6)
        pct = round(difference / abs(ours) * 100, 1) if difference is not None and ours else None
        label, color = VERDICTS[r["verdict"]]
        ws.append([r.get("label"), r.get("category_name"), r.get("unit"), ours, theirs, difference, pct, label, r.get("why")])
        for c in ws[ws.max_row]:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for c in ws[ws.max_row][7:9]:
            c.fill = PatternFill("solid", fgColor=color)
    for col, w in zip("ABCDEFGHI", (34, 30, 9, 26, 18, 16, 12, 34, 80)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=6):
        for c in row:
            c.number_format = "# ##0.00"
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:I{ws.max_row}"
    return ws
