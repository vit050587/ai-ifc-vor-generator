from src.tools.tsn.assemble import iter_rates
"""Автопроверки извлечённых данных.

Расхождения не исправляются: значение остаётся как в книге, а факт
несоответствия попадает в отчёт. Так ошибки первоисточника видно отдельно
от ошибок разбора.
"""
EPS = 0.011


def check_direct_costs(tables):
    out = []
    for t in tables:
        for r in iter_rates(t):
            parts = [r.get("wages_rub"), r.get("machine_operation_rub"),
                     r.get("materials_rub")]
            total = r.get("direct_costs_rub")
            if not isinstance(total, (int, float)) or \
                    any(not isinstance(p, (int, float)) for p in parts):
                continue
            s = round(sum(parts), 2)
            if abs(s - total) > EPS:
                out.append({
                    "type": "direct_costs_mismatch", "rate": r["code"],
                    "page": r.get("source_page"),
                    "printed": total, "computed": s, "delta": round(s - total, 2),
                    "note": "прямые затраты в книге не равны сумме ЗП + эксплуатация "
                            "машин + материалы",
                })
    return out


def check_rate_sequences(tables):
    """Расценки в таблице нумеруются подряд с единицы. Отклонение от этого —
    почти всегда опечатка в шифре: сравниваем позицию с напечатанным шифром."""
    out = []
    for t in tables:
        for i, r in enumerate(iter_rates(t), start=1):
            expected = f"{t['table_code']}-{i}"
            if (r["code"] or "") != expected:
                out.append({"type": "rate_code_mismatch", "table": t["table_code"],
                            "position": i, "printed": r["code"],
                            "expected": expected, "page": r.get("source_page"),
                            "note": "шифр в книге не соответствует позиции расценки "
                                    "в таблице"})
    return out


SUMMARY_FIELDS = ("direct_costs_rub", "wages_rub", "machine_operation_rub",
                  "machine_operation_wages_rub", "materials_rub", "labor_hours")


def check_summary_values(tables):
    """Итоговая строка обязана быть числом. Не число — дефект первоисточника
    (например, съехавшая колонка), значение сохранено как напечатано."""
    out = []
    for t in tables:
        for r in iter_rates(t):
            for f in SUMMARY_FIELDS:
                v = r.get(f)
                if v is not None and not isinstance(v, (int, float)):
                    out.append({"type": "summary_value_not_numeric",
                                "rate": r["code"], "field": f,
                                "page": r.get("source_page"), "value": v,
                                "note": "в книге на месте числа стоит не число"})
    return out


def check_duplicates(tables):
    seen, out = {}, []
    for t in tables:
        for r in iter_rates(t):
            if not r["code"]:
                continue
            if r["code"] in seen:
                out.append({"type": "duplicate_rate_code", "rate": r["code"],
                            "pages": [seen[r["code"]], r.get("source_page")]})
            else:
                seen[r["code"]] = r.get("source_page")
    return out


def check_coefficient_targets(coeffs):
    out = []
    for it in coeffs["items"]:
        miss = it["applies_to"].get("missing_rate_codes") or []
        if miss:
            out.append({"type": "coefficient_target_missing", "clause": it["clause"],
                        "raw": it["applies_to"]["raw"], "missing": miss,
                        "note": "в графе 2 указаны расценки, которых нет в сборнике"})
        if not it["applies_to"].get("expanded_rate_codes"):
            out.append({"type": "coefficient_without_targets", "clause": it["clause"],
                        "raw": it["applies_to"]["raw"]})
    return out


def run(tables, coeffs):
    return (check_direct_costs(tables) + check_rate_sequences(tables)
            + check_summary_values(tables)
            + check_duplicates(tables) + check_coefficient_targets(coeffs))
