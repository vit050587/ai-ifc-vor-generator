"""Сборка ЛЮБОГО сборника ТСН в JSON: нормы, коэффициенты и связка между ними.

Запуск:  python build.py "ТСН-2001.3-6 Сборник 6. ....pdf" [номер_сборника]
Номер сборника определяется по первой странице PDF; можно задать вторым аргументом.
Результат: out/tsn_3_<N>_full.json (+ coefficients, norms, validation_report).
"""
import json, os, sys, datetime, subprocess

if __name__ == "__main__":
    _pdf = sys.argv[1] if len(sys.argv) > 1 else "ТСН-2001.3-15 Сборник 15. Отделочные работы.pdf"
    if len(sys.argv) > 2:
        os.environ["TSN_N"] = sys.argv[2]
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from src.tools.tsn.tsn_cfg import detect as _detect
        os.environ["TSN_N"] = _detect(_pdf)[0]
        # Номер сборника (TSN_N) читается модулем tsn_cfg при импорте, поэтому
        # сбрасываем оба его имени, чтобы он перечитал переменную окружения.
        sys.modules.pop("src.tools.tsn.tsn_cfg", None)
        sys.modules.pop("tsn_cfg", None)

import pdfplumber
import src.tools.tsn.tsn_cfg as tsn_cfg
import src.tools.tsn.refs as refs_mod
from src.tools.tsn.assemble import build as build_norms, iter_rates
from src.tools.tsn.parse_coeffs import parse as parse_coeffs
import validate

OUT = "out"


def unquarantine(path):
    """Снять карантин Gatekeeper с созданных файлов.

    macOS вешает com.apple.quarantine на всё, что создал процесс из
    приложения под карантином, и Finder потом ругается на наши же JSON."""
    if sys.platform != "darwin":
        return
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", path],
                   check=False, capture_output=True)


def document_meta(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        title = (pdf.metadata or {}).get("Title", "")
    n, ttl, code = tsn_cfg.detect(pdf_path)
    return {
        "code": code,
        "title": ttl,
        "chapter": "Глава 3. Строительные работы",
        "collection_number": int(n) if str(n).isdigit() else n,
        "source_pdf": os.path.basename(pdf_path),
        "source_title": title,
        "extracted_at": datetime.date.today().isoformat(),
    }


def link(tables, coeffs):
    """Разворачивает ссылки коэффициентов в шифры расценок и вешает обратную ссылку."""
    def rate_key(r):
        return r.get("code_normalized") or r["code"]
    index = {t["table_code"]: [rate_key(r) for r in iter_rates(t)] for t in tables}
    back = {}
    for it in coeffs["items"]:
        codes, missing = refs_mod.expand(it["applies_to"]["refs"], index)
        it["applies_to"]["expanded_rate_codes"] = codes
        it["applies_to"]["missing_rate_codes"] = missing
        clause_key = it["clause"] + ("/вариант" if it.get("variant_of") else "")
        for c in codes:
            back.setdefault(c, []).append(clause_key)
    for t in tables:
        for r in iter_rates(t):
            r["applicable_coefficient_clauses"] = back.get(rate_key(r), [])
    return coeffs


def main(pdf_path):
    tables, norm_issues = build_norms(pdf_path)
    coeffs = parse_coeffs(pdf_path)
    coeff_issues = coeffs.pop("issues")
    link(tables, coeffs)

    report = {
        "extraction_issues": norm_issues + coeff_issues,
        "validation_findings": validate.run(tables, coeffs),
    }
    doc = document_meta(pdf_path)
    import src.tools.tsn.tech_part as tech_part
    doc["technical_part"] = tech_part.parse(pdf_path)   # общие указания + правила исчисления объёмов
    os.makedirs(f"{OUT}/tables", exist_ok=True)

    def dump(obj, name):
        with open(f"{OUT}/{name}", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return os.path.getsize(f"{OUT}/{name}")

    N = tsn_cfg.N
    if str(N) != str(doc["collection_number"]):
        print(f"ПРЕДУПРЕЖДЕНИЕ: разбор идёт с номером TSN_N={N}, "
              f"а в PDF определён сборник {doc['collection_number']}", file=sys.stderr)
    dump({"document": doc, "coefficients_table": coeffs}, f"tsn_3_{N}_coefficients.json")
    dump({"document": doc, "tables": tables}, f"tsn_3_{N}_norms.json")
    dump({"document": doc, "coefficients_table": coeffs, "tables": tables},
         f"tsn_3_{N}_full.json")
    dump(report, "validation_report.json")

    import src.tools.tsn.resolve as resolve
    rates_idx, clauses = {}, {}
    for t in tables:
        for r in iter_rates(t):
            rates_idx[r.get("code_normalized") or r["code"]] = dict(
                r, table_code=t["table_code"], table_title=t["title"],
                unit_of_measure=t["unit_of_measure"])
    for it in coeffs["items"]:
        clauses.setdefault(it["clause"], []).append(it)
    try:   # пример применения коэффициента — первая расценка с пунктом §3
        ex = next((c, k.split("/")[0]) for c, r in rates_idx.items()
                  for k in r.get("applicable_coefficient_clauses", []))
        dump(resolve.apply(ex[0], ex[1], rates_idx, clauses), "link_example.json")
    except StopIteration:
        pass
    for t in tables:
        dump({"document": doc, "table": t}, f"tables/{t['table_code']}.json")

    unquarantine(OUT)
    n_rates = sum(1 for t in tables for _ in iter_rates(t))
    print(f"{doc['code']} {doc['title']}: таблиц {len(tables)}, расценок {n_rates}, "
          f"пунктов коэффициентов {len(coeffs['items'])}")
    print(f"техническая часть: общих указаний {len(doc['technical_part']['general'])}, "
          f"правил исчисления объёмов {len(doc['technical_part']['volume_rules'])}")
    print(f"замечаний разбора {len(report['extraction_issues'])}, "
          f"находок валидации {len(report['validation_findings'])}")
    return tables, coeffs, report


if __name__ == "__main__":
    main(_pdf)
