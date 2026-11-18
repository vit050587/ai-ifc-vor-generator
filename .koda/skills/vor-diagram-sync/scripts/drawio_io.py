
#!/usr/bin/env python3
"""drawio_io.py — чтение, валидация и сохранение .drawio без GUI.

Поддерживает:
  * несжатый XML (<mxGraphModel> внутри <diagram>);
  * сжатый payload (base64 + raw deflate + URL-encode) — как сохраняет draw.io;
  * многолистовые файлы (<mxfile><diagram>...).

Комментарии синхронизации хранятся в атрибутах `mxCell` (не в `value`, чтобы
не ломать вид диаграммы):

  syncKey      — стабильный идентификатор сущности из `docs/readme-blocks.json`
  syncComment  — комментарий LLM к сущности (единственный источник сопоставления)
  syncAliases  — алиасы через ` | `
  syncSource   — раздел/строка README, откуда взята сущность
  syncAt       — когда комментарий проставлен

CLI:
  python drawio_io.py dump FILE        # JSON: листы, вершины, рёбра, атрибуты
  python drawio_io.py validate FILE    # проверка структуры; exit 1 при ошибках
  python drawio_io.py labels FILE      # id + подписи (для diff)
  python drawio_io.py annotations FILE # id + syncKey/syncComment узлов
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.parse
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path


def _decompress(payload: str) -> str:
    """base64 → raw deflate → URL-decode."""
    data = base64.b64decode(payload)
    try:
        xml = zlib.decompress(data, -15)          # raw deflate
    except zlib.error:
        xml = zlib.decompress(data)               # zlib header fallback
    return urllib.parse.unquote(xml.decode("utf-8"))


def load(path: str | Path) -> dict:
    """Читает файл. Сжатые листы распаковывает и ВСТРАИВАЕТ модель обратно
    в <diagram>, чтобы последующий save() сохранил правки."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    root = ET.fromstring(raw)
    pages = []
    for diagram in root.findall("diagram"):
        model = diagram.find("mxGraphModel")
        if model is None:
            payload = (diagram.text or "").strip()
            if not payload:
                continue
            model = ET.fromstring(_decompress(payload))
            diagram.text = None
            diagram.append(model)                 # делаем дерево редактируемым
        pages.append({"id": diagram.get("id"), "name": diagram.get("name"), "model": model})
    return {"root": root, "pages": pages}


def _geom(cell: ET.Element) -> dict | None:
    g = cell.find("mxGeometry")
    if g is None:
        return None
    out = {"as": g.get("as")}
    for key in ("x", "y", "width", "height", "relative"):
        if g.get(key) is not None:
            out[key] = g.get(key)
    pts = g.find("Array[@as='points']")
    if pts is not None:
        out["points"] = [{"x": p.get("x"), "y": p.get("y")} for p in pts.findall("mxPoint")]
    return out


def cells(page: dict) -> list[dict]:
    root = page["model"].find("root")
    return [{
        "id": c.get("id"),
        "value": c.get("value", ""),
        "style": c.get("style", ""),
        "vertex": c.get("vertex") == "1",
        "edge": c.get("edge") == "1",
        "parent": c.get("parent"),
        "source": c.get("source"),
        "target": c.get("target"),
        "geometry": _geom(c),
        "attrs": {k: v for k, v in c.attrib.items() if k not in KNOWN_CELL_ATTRS},
    } for c in root.findall("mxCell")]


#: Стандартные атрибуты mxCell — всё остальное считается комментарием
#: синхронизации (syncKey/syncComment/syncAliases/syncSource/syncAt).
KNOWN_CELL_ATTRS = {
    "id", "value", "style", "parent", "vertex", "edge", "source", "target",
    "visible", "collapsed", "connectable", "tooltip", "placeholders",
    "extendedBounds", "mxGraphModel",
}


def find_cell(doc: dict, page_name: str | None, cell_id: str):
    """Вернуть (page, mxCell) по имени листа (или любому листу при None)."""
    for page in doc["pages"]:
        if page_name and (page["name"] or page["id"]) != page_name:
            continue
        root = page["model"].find("root")
        if root is None:
            continue
        for cell in root.findall("mxCell"):
            if cell.get("id") == cell_id:
                return page, cell
    return None, None


def set_cell_attrs(doc: dict, page_name: str | None, cell_id: str,
                   attrs: dict[str, str | None]) -> bool:
    """Записать/удалить атрибуты-комментарии у ячейки.

    `attrs` со значением None удаляет атрибут. Возвращает True, если ячейка
    найдена.
    """
    page, cell = find_cell(doc, page_name, cell_id)
    if cell is None:
        return False
    for key, value in attrs.items():
        if value is None:
            cell.attrib.pop(key, None)
        else:
            cell.set(key, value)
    return True


def annotations(path: str | Path) -> list[dict]:
    """Комментарии синхронизации, закреплённые на узлах диаграммы."""
    out = []
    for page in dump(path):
        for c in page["cells"]:
            if not c["vertex"]:
                continue
            attrs = c.get("attrs") or {}
            if not any(k.startswith("sync") for k in attrs):
                continue
            out.append({
                "page": page["page"],
                "id": c["id"],
                "label": re.sub(r"<[^>]+>", " ", c["value"] or "").strip(),
                "syncKey": attrs.get("syncKey"),
                "syncComment": attrs.get("syncComment"),
                "syncAliases": attrs.get("syncAliases"),
                "syncSource": attrs.get("syncSource"),
                "syncAt": attrs.get("syncAt"),
            })
    return out


def dump(path: str | Path) -> list[dict]:
    doc = load(path)
    return [{"page": p["name"] or p["id"], "cells": cells(p)} for p in doc["pages"]]


def validate(path: str | Path) -> list[str]:
    doc = load(path)
    errors: list[str] = []
    for page in doc["pages"]:
        name = page["name"] or page["id"]
        cs = cells(page)
        ids = [c["id"] for c in cs]
        if ids[:2] != ["0", "1"]:
            errors.append(f"{name}: первые два mxCell должны быть id=0 и id=1 (сейчас {ids[:2]})")
        if len(ids) != len(set(ids)):
            errors.append(f"{name}: повторяющиеся id")
        idset = set(ids)
        for c in cs:
            cid = c["id"]
            if cid != "0" and c["parent"] not in idset:
                errors.append(f"{name}: {cid}: parent={c['parent']} не существует")
            if c["vertex"] and c["edge"]:
                errors.append(f"{name}: {cid}: vertex=1 и edge=1 одновременно")
            if c["vertex"]:
                g = c["geometry"]
                if not g or g.get("as") != "geometry":
                    errors.append(f"{name}: {cid}: у вершины нет <mxGeometry as='geometry'>")
                elif None in (g.get("width"), g.get("height")):
                    # x/y в draw.io необязательны: отсутствие = 0.
                    errors.append(f"{name}: {cid}: неполная геометрия вершины")
            if c["edge"]:
                if c["geometry"] is None:
                    errors.append(f"{name}: {cid}: у ребра нет <mxGeometry>")
                for end in ("source", "target"):
                    ref = c[end]
                    if ref and ref not in idset:
                        errors.append(f"{name}: {cid}: {end}={ref} не существует")
    return errors


def save(path: str | Path, root: ET.Element) -> None:
    """Сохраняет несжатый XML (draw.io его читает; AI не должен писать сжатое)."""
    Path(path).write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
        encoding="utf-8",
    )


def labels(path: str | Path) -> list[dict]:
    out = []
    for page in dump(path):
        for c in page["cells"]:
            if c["vertex"] and (c["value"] or "").strip():
                out.append({"page": page["page"], "id": c["id"], "label": c["value"]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="draw.io reader/validator")
    ap.add_argument("cmd", choices=("dump", "validate", "labels", "annotations"))
    ap.add_argument("file")
    a = ap.parse_args()
    if a.cmd == "dump":
        print(json.dumps(dump(a.file), ensure_ascii=False, indent=2))
    elif a.cmd == "labels":
        print(json.dumps(labels(a.file), ensure_ascii=False, indent=2))
    elif a.cmd == "annotations":
        print(json.dumps(annotations(a.file), ensure_ascii=False, indent=2))
    else:
        errs = validate(a.file)
        if errs:
            print("\n".join(f"ERROR: {e}" for e in errs), file=sys.stderr)
            sys.exit(1)
        print("OK: структура .drawio валидна")


if __name__ == "__main__":
    main()