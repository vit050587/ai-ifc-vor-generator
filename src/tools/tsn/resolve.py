"""Применение поправочных коэффициентов §3 к расценке.

Правила чтения таблицы коэффициентов:
  гр.3 (labor_and_wages)      -> заработная плата рабочих И затраты труда;
  гр.4 (machine_operation)    -> эксплуатация машин целиком, вместе с
                                 «в том числе заработная плата» машинистов;
  гр.5 (material_consumption) -> расход материалов. Если у пункта заполнено
                                 applies_to_resource, коэффициент относится
                                 только к названному ресурсу, а не ко всей
                                 статье, и итог по материалам пересчитать
                                 нельзя — нужны цены ресурсов.
Прямые затраты пересчитываются как сумма трёх статей.
"""
import json, sys

ART = {
    "labor_and_wages": ["wages_rub", "labor_hours"],
    "machine_operation": ["machine_operation_rub", "machine_operation_wages_rub"],
    "material_consumption": ["materials_rub"],
}


def load(out_dir="out", n=None):
    from src.tools.tsn.tsn_cfg import N
    with open(f"{out_dir}/tsn_3_{n or N}_full.json", encoding="utf-8") as f:
        d = json.load(f)
    rates = {}
    for t in d["tables"]:
        for v in t["variants"]:
            for r in v["rates"]:
                key = r.get("code_normalized") or r["code"]
                rates[key] = dict(r, table_code=t["table_code"],
                                  table_title=t["title"],
                                  unit_of_measure=t["unit_of_measure"],
                                  variant_name=v["variant_name"])
    clauses = {}
    for it in d["coefficients_table"]["items"]:
        clauses.setdefault(it["clause"], []).append(it)
    return rates, clauses


def applicable(rate_code, rates, clauses):
    """Пункты §3, применимые к расценке."""
    r = rates.get(rate_code)
    if r is None:
        return []
    out = []
    for key in r.get("applicable_coefficient_clauses", []):
        clause = key.split("/")[0]
        for it in clauses.get(clause, []):
            if rate_code in it["applies_to"]["expanded_rate_codes"]:
                out.append(it)
    return out


def apply(rate_code, clause, rates, clauses):
    """Пересчёт расценки с одним пунктом коэффициентов."""
    r = rates[rate_code]
    item = next(it for it in applicable(rate_code, rates, clauses)
                if it["clause"] == clause)
    coefs = item["coefficients"]
    calc, notes = {}, []
    for group, fields in ART.items():
        k = coefs.get(group)
        for f in fields:
            base = r.get(f)
            if not isinstance(base, (int, float)):
                continue
            if k is None:
                calc[f] = {"base": base, "coefficient": None, "adjusted": base}
            else:
                calc[f] = {"base": base, "coefficient": k,
                           "adjusted": round(base * k, 2)}
    if coefs.get("material_consumption") is not None and item.get("applies_to_resource"):
        calc.pop("materials_rub", None)
        notes.append(
            f"коэффициент {coefs['material_consumption']} относится только к ресурсу "
            f"«{item['applies_to_resource']}»; итог по материалам в рублях "
            f"пересчитать нельзя — нужны цены ресурсов")
    parts = [calc.get(f, {}).get("adjusted") for f in
             ("wages_rub", "machine_operation_rub", "materials_rub")]
    direct = (round(sum(p for p in parts if isinstance(p, (int, float))), 2)
              if all(isinstance(p, (int, float)) for p in parts) else None)
    return {
        "rate": rate_code,
        "table": f"{r['table_code']} {r['table_title']}",
        "variant": r.get("variant_name"),
        "group_value": r.get("group_value"),
        "unit_of_measure": r.get("unit_of_measure"),
        "clause": item["clause"],
        "condition": item["condition"],
        "applies_to_resource": item.get("applies_to_resource"),
        "coefficients": coefs,
        "direct_costs_rub": {"base": r.get("direct_costs_rub"), "adjusted": direct},
        "articles": calc,
        "notes": notes,
    }


if __name__ == "__main__":
    rates, clauses = load()
    code = sys.argv[1]
    items = applicable(code, rates, clauses)
    if len(sys.argv) > 2:
        print(json.dumps(apply(code, sys.argv[2], rates, clauses),
                         ensure_ascii=False, indent=2))
    else:
        r = rates[code]
        print(f"{code}: {r['table_title']} / {r.get('variant_name')} "
              f"/ {r.get('group_value')}")
        print(f"  прямые затраты {r.get('direct_costs_rub')} руб. "
              f"за {r.get('unit_of_measure')}")
        print(f"  применимые пункты §3: "
              f"{[i['clause'] for i in items] or 'нет'}")
