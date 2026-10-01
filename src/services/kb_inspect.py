"""
Проверка подбора работ по одному типу элемента (последний запуск АР).

    python -m src.services.kb_inspect list          # список типов элементов
    python -m src.services.kb_inspect 5             # разбор типа №5 (с кэшем)
    python -m src.services.kb_inspect 5 nocache     # разбор заново, без кэша
"""
import glob
import json
import os
import sys
import textwrap

from src.services import perechen_kb as kb
from src.services import perechen_pipeline as pp


def _latest():
    path = sorted(glob.glob("/app/outputs/*/run_*/Подобранные_таблицы_работ.json"),
                  key=os.path.getmtime)[-1]
    return path, os.path.dirname(path)


def collect_types():
    tables_path, run_dir = _latest()
    data = json.load(open(tables_path, encoding="utf-8"))
    elements = data.get("elements") or []
    groups = json.load(open(os.path.join(run_dir, "filtered_elements_grouped_AR.json"),
                            encoding="utf-8"))
    per_el = pp._element_quantities(run_dir)
    types = {}
    for node, path in pp._leaves(groups if isinstance(groups, list) else [groups]):
        part = pp._part_from_path(path)
        for i in node.get("indices") or []:
            if not isinstance(i, int) or not 0 <= i < len(elements):
                continue
            el = elements[i].get("element", {})
            key = (part, kb.type_key(el.get("name")), str(el.get("material") or ""))
            t = types.setdefault(key, {"part": part, "name": key[1], "material": key[2], "el": el,
                                       "q": {"volume": 0.0, "area": 0.0, "rebar_kg": 0.0, "count": 0}})
            t["q"]["count"] += 1
            if i < len(per_el):
                for k in ("volume", "area", "rebar_kg"):
                    t["q"][k] += per_el[i][k]
    order = {p: n for n, p in enumerate(pp.PART_ORDER)}
    lst = sorted(types.values(), key=lambda t: (order.get(t["part"], 9), t["name"]))
    return tables_path, data, run_dir, lst


def _wrap(text, indent="     "):
    return "\n".join(textwrap.wrap(str(text), 110, initial_indent=indent, subsequent_indent=indent))


def main():
    args = sys.argv[1:]
    tables_path, data, run_dir, types = collect_types()
    print(f"Запуск: {run_dir}\n", flush=True)

    if not args or args[0] == "list":
        print(f"{'№':>3} | {'часть':<9} | {'шт':>4} | {'V, м3':>8} | {'S, м2':>9} | {'арм, кг':>8} | элемент (материал)")
        print("-" * 130)
        for n, t in enumerate(types, 1):
            q = t["q"]
            print(f"{n:>3} | {t['part']:<9} | {q['count']:>4} | {q['volume']:>8.2f} | {q['area']:>9.2f} | "
                  f"{q['rebar_kg']:>8.0f} | {t['name'][:60]} ({t['material'][:30]})")
        return

    n = int(args[0])
    t = types[n - 1]
    el, q = t["el"], {k: round(v, 3) if isinstance(v, float) else v for k, v in t["q"].items()}
    height = kb.building_height_for_run(tables_path, data)
    print("=" * 110)
    print(f"ТИП №{n}: {t['name']}")
    print(f"  часть здания: {t['part']} | IFC: {el.get('ifc_class')} ({el.get('predefined_type') or '-'}) "
          f"| МССК: {el.get('mssk_code') or '-'}")
    print(f"  материал: {t['material']} | способ: {el.get('construction_method') or '-'} "
          f"| толщина по имени: {kb.element_thickness(el)} мм")
    print(f"  количество: {q['count']} шт | объём {q['volume']} м3 | площадь {q['area']} м2 "
          f"| арматура {q['rebar_kg']} кг | высота здания {height} м")
    print("=" * 110, flush=True)

    res = kb.pick_for_element(el, t["part"], kb.load_perechen(), height,
                              use_cache="nocache" not in args)
    print(f"\nСТАТУС: {res['status']}")
    if res["subsection"]:
        print(f"КОНСТРУКЦИЯ: {res['subsection']['section']} → {res['subsection']['title']}")
    print("ОБОСНОВАНИЕ:")
    for part_reason in str(res["reason"]).split(" | "):
        print(_wrap(part_reason))

    if not res["works"]:
        return
    print(f"\n{'шифр':<12} {'ед.':<10} {'объём':>9} {'стоимость':>12}  наименование")
    print("-" * 110)
    total, seen = 0.0, set()
    for w in res["works"]:
        if all(r["pressmark"] in seen for r in w["rates"]):
            continue
        print(f"  ▸ {w['name'][:90]}")
        for r in w["rates"]:
            if r["pressmark"] in seen:
                continue
            seen.add(r["pressmark"])
            vol = pp._volume(r, el, q)
            *_, cost = pp._costs(r, vol)
            total += cost or 0
            mark = "рес." if r["is_resource"] else ""
            print(f"  {r['pressmark']:<12} {r['unit']:<10} {vol if vol is not None else '—':>9} "
                  f"{cost if cost is not None else '—':>12}  {mark} {kb._full_title(r)[:75]}")
    cure = pp._curing_rate(el, seen)
    if cure:
        vol = pp._volume(cure, el, q)
        *_, cost = pp._costs(cure, vol)
        total += cost or 0
        print(f"  ▸ Уход за бетоном (добавлено правилом)")
        print(f"  {cure['pressmark']:<12} {cure['unit']:<10} {vol if vol is not None else '—':>9} "
              f"{cost if cost is not None else '—':>12}   {cure['title'][:75]}")
    print("-" * 110)
    print(f"  ИТОГО по расценкам: {round(total, 2)} руб.")


if __name__ == "__main__":
    main()
