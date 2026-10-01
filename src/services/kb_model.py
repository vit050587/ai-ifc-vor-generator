"""
Проверка подбора работ по типам элементов ВСЕЙ модели (последняя сессия),
без запуска обработки в браузере.

    python -m src.services.kb_model list              # все типы элементов модели
    python -m src.services.kb_model list стен         # только типы, где в имени/материале есть «стен»
    python -m src.services.kb_model 12                # разбор типа №12 (с кэшем)
    python -m src.services.kb_model 12 nocache        # разбор заново
"""
import glob
import os
import sys
import textwrap

import pandas as pd

from src.services import perechen_kb as kb
from src.services import perechen_pipeline as pp

EXCEL = os.path.join("original", "ДЛЯ_СМЕТЧИКА_исправленный.xlsx")


def _latest_session() -> str:
    files = glob.glob(os.path.join("/app/outputs", "*", EXCEL))
    if not files:
        sys.exit("Нет загруженных моделей: загрузите IFC в браузере (шаг 1)")
    return os.path.dirname(os.path.dirname(max(files, key=os.path.getmtime)))


def _part(row) -> str:
    t = f"{row.get('Тип_этажа', '')} {row.get('Этаж', '')}".lower()
    if "цокол" in t:
        return "Цоколь"
    if "подзем" in t:
        return "Подземная"
    return "Надземная"


def collect_types(session: str):
    path = os.path.join(session, EXCEL)
    try:
        df = pd.read_excel(path, sheet_name="Данные")
    except Exception:
        df = pd.read_excel(path)
    df.columns = [str(c) for c in df.columns]
    per_el = pp._element_quantities(os.path.join(session, "original"),
                                    "ДЛЯ_СМЕТЧИКА_исправленный.xlsx")
    layer_col = next((c for c in df.columns if "IfcMaterialLayer" in c), None)

    def val(row, col):
        v = row.get(col)
        return "" if v is None or str(v) in ("nan", "-") else str(v)

    types = {}
    for i, (_, r) in enumerate(df.iterrows()):
        name = val(r, "Имя")
        material = val(r, layer_col) if layer_col and val(r, layer_col) else val(r, "Материал")
        part = _part(r)
        key = (part, kb.type_key(name), material, val(r, "Тип элемента"))
        t = types.setdefault(key, {
            "part": part, "name": key[1], "material": material,
            "el": {"name": name, "ifc_class": val(r, "Тип элемента"), "material": material,
                   "mssk_code": val(r, "Код мсск"), "predefined_type": val(r, "PredefinedType"),
                   "construction_method": val(r, "ConstructionMethod"), "storey": val(r, "Этаж")},
            "q": {"volume": 0.0, "area": 0.0, "rebar_kg": 0.0, "fw_hint": 0.0, "count": 0}})
        t["q"]["count"] += 1
        if i < len(per_el):
            for k in ("volume", "area", "rebar_kg", "fw_hint"):
                t["q"][k] += per_el[i].get(k, 0.0)
    order = {p: n for n, p in enumerate(pp.PART_ORDER)}
    return sorted(types.values(), key=lambda t: (order.get(t["part"], 9), -t["q"]["volume"],
                                                 -t["q"]["area"], t["name"]))


def _wrap(text, indent="     "):
    return "\n".join(textwrap.wrap(str(text), 110, initial_indent=indent, subsequent_indent=indent))


def show(n: int, t, height, use_cache: bool) -> None:
    el = t["el"]
    q = {k: round(v, 3) if isinstance(v, float) else v for k, v in t["q"].items()}
    print("=" * 110)
    print(f"ТИП №{n}: {t['name']}")
    print(f"  часть здания: {t['part']} | IFC: {el['ifc_class']} ({el['predefined_type'] or '-'}) "
          f"| МССК: {el['mssk_code'] or '-'} | этаж: {el['storey']}")
    print(f"  материал: {t['material']} | толщина по имени: {kb.element_thickness(el)} мм")
    print(f"  количество: {q['count']} шт | объём {q['volume']} м3 | площадь {q['area']} м2 "
          f"| арматура {q['rebar_kg']} кг | высота здания {height} м")
    print("=" * 110, flush=True)

    res = kb.pick_for_element(el, t["part"], kb.load_perechen(), height, use_cache=use_cache)
    print(f"\nСТАТУС: {res['status']}")
    if res["subsection"]:
        print(f"КОНСТРУКЦИЯ: {res['subsection']['section']} → {res['subsection']['title']}")
    print("ОБОСНОВАНИЕ:")
    for piece in str(res["reason"]).split(" | "):
        print(_wrap(piece))
    if not res["works"]:
        return

    rows = pp.finalize_rows(el, q, res["works"], kb.load_perechen())
    print(f"\n{'шифр':<12} {'ед.':<10} {'объём':>9} {'стоимость':>12}  наименование")
    print("-" * 110)
    total, last = 0.0, None
    for r in rows:
        if r["work"] != last:
            print(f"  ▸ {r['work'][:90]}")
            last = r["work"]
        total += r["total"] or 0
        vol = r["volume"] if r["volume"] is not None else "—"
        cost = r["total"] if r["total"] is not None else "—"
        mark = "рес." if r["is_resource"] else "    "
        print(f"  {r['pressmark']:<12} {r['unit']:<10} {vol:>9} {cost:>12}  {mark} {r['title'][:75]}")
    print("-" * 110)
    print(f"  ИТОГО по расценкам: {round(total, 2)} руб.")


def main():
    args = sys.argv[1:]
    session = _latest_session()
    types = collect_types(session)
    print(f"Модель: {session} | типов элементов: {len(types)}\n", flush=True)

    if not args or args[0] == "list":
        flt = " ".join(args[1:]).lower()
        print(f"{'№':>3} | {'часть':<9} | {'шт':>5} | {'V, м3':>8} | {'S, м2':>9} | элемент (материал)")
        print("-" * 125)
        for n, t in enumerate(types, 1):
            if flt and flt not in f"{t['name']} {t['material']}".lower():
                continue
            q = t["q"]
            print(f"{n:>3} | {t['part']:<9} | {q['count']:>5} | {q['volume']:>8.2f} | {q['area']:>9.2f} | "
                  f"{t['name'][:60]} ({t['material'][:35]})")
        return

    height = kb.building_height_for_run(os.path.join(session, "x", "y.json"), {})
    if args[0] == "all":
        subs = kb.load_perechen()
        grand = 0.0
        print(f"{'№':>3} | {'часть':<9} | {'статус':<7} | {'руб.':>13} | стр | конструкция / предупреждения | элемент")
        print("-" * 150)
        for n, t in enumerate(types, 1):
            el = t["el"]
            q = {k: round(v, 3) if isinstance(v, float) else v for k, v in t["q"].items()}
            try:
                res = kb.pick_for_element(el, t["part"], subs, height)
                rows = pp.finalize_rows(el, q, res["works"], subs)
            except Exception as exc:
                print(f"{n:>3} | {t['part']:<9} | ОШИБКА  | {exc}", flush=True)
                continue
            total = sum(r["total"] or 0 for r in rows)
            grand += total
            warn = []
            if not rows:
                warn.append("НЕТ РАБОТ")
            empty = [r["pressmark"] for r in rows if r["volume"] is None and not r["is_resource"]]
            if empty:
                warn.append("без объёма: " + ",".join(empty))
            sub = (res["subsection"] or {}).get("title", "-")
            print(f"{n:>3} | {t['part']:<9} | {res['status']:<7} | {total:>13,.0f} | {len(rows):>3} | "
                  f"{sub[:40]}{' ⚠ ' + '; '.join(warn) if warn else ''} | {t['name'][:45]}", flush=True)
        print("-" * 150)
        print(f"ИТОГО по модели: {grand:,.0f} руб.")
        return

    n = int(args[0])
    show(n, types[n - 1], height, use_cache="nocache" not in args)


if __name__ == "__main__":
    main()
