#!/usr/bin/env python3
"""sync_check.py — манифест синхронизации README ↔ .drawio по LLM-комментариям.

Источник сущностей — `docs/readme-blocks.json` (его готовит
`readme_llm_blocks.py`: README → блоки → Qwen3.8 → `syncKey` + `comment` +
`aliases`).

Сопоставление сущностей с узлами диаграммы выполняется **только по
комментариям**:

  * `syncKey` / `syncComment` / `syncAliases` — атрибуты, закреплённые на
    узле командой `annotate`;
  * `comment` / `aliases` / `title` сущности из `docs/readme-blocks.json`.

Сырого текста README (заголовки, ячейки таблиц, backticks) в сопоставлении
больше нет — именно из-за него прежняя версия давала ложные привязки.

Команды:
  bootstrap — первичная привязка сущностей к узлам + манифест v2
  annotate  — закрепить комментарии на узлах .drawio (syncKey/syncComment/...)
  check     — что изменилось с последней синхронизации (exit 2 при изменениях)
  stamp     — зафиксировать текущее состояние после правки диаграммы
  report    — таблица «сущность → узел» для ревью
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# Гарантируем, что drawio_io найдётся при запуске из любой директории
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drawio_io  # noqa: E402

SCHEMA_VERSION = 2
DEFAULT_MIN_SCORE = 0.62
CONTAINER_MIN_SCORE = 0.90  # swimlane/рамки веток матчим только по явным названиям

#: Виды сущностей уровня бизнес-процесса: им нужен узел на диаграмме.
PROCESS_KINDS = {"mode", "stage", "step", "artifact", "external", "role"}

#: Служебные слова, которые не помогают отличить одну сущность от другой.
STOPWORDS = {
    "этап", "шаг", "модуль", "пункт", "раздел", "режим", "сервис", "файл",
    "файлы", "данные", "работы", "работа", "работ", "элемент", "элементы",
    "описание", "общее", "общий", "общая", "прочее", "прочий", "новое",
    "использование", "используется", "подробнее", "пример", "примеры",
    "и", "в", "на", "для", "по", "с", "из", "к", "о", "об", "при", "или",
    "как", "что", "это", "все", "всё", "не", "без", "до", "от", "у",
    "же", "ли", "бы", "то", "та", "те", "он", "она", "они", "его", "её",
    "ее", "их", "мы", "вы", "где", "когда", "если", "так", "там", "тут",
    "блок", "узел", "схема", "диаграмма", "комментарий", "интерфейс",
    "обработка", "сохранение", "выбор", "пользователь", "вход", "тип",
}


# --- утилиты ---------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@lru_cache(maxsize=20000)
def norm(value: str) -> str:
    """Нормализация подписи: без разметки, регистра и пунктуации."""
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@lru_cache(maxsize=20000)
def words(value: str) -> tuple[str, ...]:
    """Слова подписи без нумерации («5. Результат» → ('результат',))."""
    return tuple(w for w in norm(value).split() if not w.isdigit())


#: Основы служебных слов (первые 4 символа) — быстрый фильтр без морфологии.
STOP_STEMS = {s[:4] for s in STOPWORDS if len(s) >= 4}


@lru_cache(maxsize=20000)
def tokens(value: str) -> frozenset[str]:
    """Значимые слова подписи (без нумерации и служебных слов, с морфологией)."""
    out: set[str] = set()
    for word in words(value):
        if len(word) < 2:
            continue
        if word in STOPWORDS or (len(word) >= 4 and word[:4] in STOP_STEMS):
            continue
        out.add(word)
    return frozenset(out)


def contains_words(haystack: list[str], needle: list[str]) -> bool:
    """Содержит ли последовательность слов `haystack` подряд идущие `needle`."""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    for index, word in enumerate(haystack):
        if word != first:
            continue
        if haystack[index:index + len(needle)] == needle:
            return True
    return False


@lru_cache(maxsize=40000)
def token_match(left: str, right: str) -> bool:
    """Слабая морфология без зависимостей: «стены» ↔ «стен», «чертежа» ↔ «чертежей».

    Сравниваем общий префикс: минимум 4 символа и не меньше 70 % короткого
    слова. Этого достаточно для русских падежных форм и не склеивает
    разные слова («форма» ↔ «формирование» отсекается по доле префикса).
    """
    if left == right:
        return True
    shortest = min(len(left), len(right))
    if shortest < 4:
        return False
    prefix = 0
    for a, b in zip(left, right):
        if a != b:
            break
        prefix += 1
    return prefix >= 4 and prefix / shortest >= 0.7


def common_tokens(left: set[str], right: set[str]) -> set[str]:
    """Пересечение с учётом `token_match`."""
    common = set(left & right)
    rest_left = set(left) - common
    rest_right = set(right) - common
    for a in list(rest_left):
        for b in rest_right:
            if token_match(a, b):
                common.add(a)
                rest_right.discard(b)
                break
    return common


def comment_hash(entity: dict) -> str:
    payload = "\x00".join([
        entity.get("comment", ""),
        "|".join(sorted(entity.get("aliases") or [])),
    ])
    return sha256_text(payload)[:16]


# --- чтение источников -----------------------------------------------------


def load_entities(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"Нет файла сущностей: {p}\n"
            "Сначала выполните: readme_llm_blocks.py --readme README.md "
            "--out docs/readme-blocks.json"
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    entities = data.get("entities")
    if not isinstance(entities, list) or not entities:
        raise SystemExit(
            f"В {p} нет непустого списка `entities` — пересоберите файл "
            "через readme_llm_blocks.py."
        )
    for entity in entities:
        for field in ("syncKey", "title", "comment"):
            if not entity.get(field):
                raise SystemExit(f"В {p} у сущности нет поля `{field}`: {entity!r}")
        entity.setdefault("aliases", [])
    return data


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mark_containers(nodes: list[dict]) -> None:
    """Пометить контейнеры (swimlane, рамки веток): в них лежат другие узлы."""
    rects = []
    for node in nodes:
        geometry = node.get("geometry") or {}
        x, y = _float(geometry.get("x")), _float(geometry.get("y"))
        w, h = _float(geometry.get("width")), _float(geometry.get("height"))
        if None in (x, y, w, h):
            continue
        rects.append((node, x, y, w, h))
    for node, x, y, w, h in rects:
        for other, ox, oy, ow, oh in rects:
            if other is node:
                continue
            center_x, center_y = ox + ow / 2, oy + oh / 2
            if x <= center_x <= x + w and y <= center_y <= y + h and w * h > ow * oh:
                node["container"] = True
                break


def load_ignore(path: str | Path | None) -> dict:
    """Списки узлов-исключений из `docs/diagram-sync-ignore.json`.

    Возвращает словарь:
      * `ids` / `labels` — легенда и оформление: узлы вообще не участвуют
        в сопоставлении и не попадают в отчёты;
      * `no_entity_ids` / `no_entity_labels` — узлы, которые сознательно
        оставлены **без сущности**: в отчётах они видны как «pin», но
        сопоставление к ним не применяется (иначе слабое правило вернёт
        ложную привязку при каждом `stamp`).
    """
    empty = {"ids": set(), "labels": set(),
             "no_entity_ids": set(), "no_entity_labels": set(), "bindings": {}}
    if not path:
        return empty
    p = Path(path)
    if not p.exists():
        return empty
    data = json.loads(p.read_text(encoding="utf-8"))
    no_entity = data.get("noEntity") or {}
    return {
        "ids": set(data.get("ids") or []),
        "labels": {norm(label) for label in data.get("labels") or []},
        "no_entity_ids": set(no_entity.get("ids") or []),
        "no_entity_labels": {norm(label) for label in no_entity.get("labels") or []},
        "bindings": {str(k): str(v) for k, v in (data.get("bindings") or {}).items()},
    }


def diagram_nodes(diagram: str | Path, ignore_path: str | Path | None = None) -> list[dict]:
    """Узлы диаграммы вместе с закреплёнными на них комментариями."""
    ignore = load_ignore(ignore_path)
    ignore_ids, ignore_labels = ignore["ids"], ignore["labels"]
    nodes = []
    for page in drawio_io.dump(diagram):
        for cell in page["cells"]:
            if not cell["vertex"]:
                continue
            style = (cell.get("style") or "").strip()
            # Оформление (заголовки страницы и подписи) — не узлы процесса.
            if style.startswith("text;"):
                continue
            label = re.sub(r"<[^>]+>", " ", cell["value"] or "")
            label = re.sub(r"\s+", " ", label).strip()
            if not label:
                continue
            if cell["id"] in ignore_ids or norm(label) in ignore_labels:
                continue
            attrs = cell.get("attrs") or {}
            # Узел, сознательно оставленный без сущности (ложная привязка
            # снята вручную): сопоставление к нему не применяется.
            pinned = (cell["id"] in ignore["no_entity_ids"]
                      or norm(label) in ignore["no_entity_labels"])
            nodes.append({
                "id": cell["id"],
                "page": page["page"],
                "label": label,
                "geometry": cell.get("geometry") or {},
                "no_entity": pinned,
                "bound_key": ignore["bindings"].get(cell["id"]),
                "syncKey": (attrs.get("syncKey") or "").strip(),
                "syncComment": (attrs.get("syncComment") or "").strip(),
                "syncAliases": (attrs.get("syncAliases") or "").strip(),
            })
    _mark_containers(nodes)
    return nodes


# --- сопоставление по комментариям ----------------------------------------


def score_pair(entity: dict, node: dict) -> tuple[float, str]:
    """Оценка «сущность ↔ узел» только по комментариям и алиасам.

    Правила намеренно консервативные: совпадение должно быть пословным
    (подстрока не считается — иначе «Форма» цепляется к «Платформа»),
    а однословные алиасы короче 10 символов принимаются только как полное
    совпадение с подписью узла.
    """
    label_words = words(node["label"])
    node_comment = norm(" ".join(filter(None, [node["syncComment"], node["syncAliases"]])))
    node_text = " ".join(filter(None, [node["label"], node["syncComment"], node["syncAliases"]]))

    if node["syncKey"] and node["syncKey"] == entity["syncKey"]:
        return 1.0, "syncKey"

    best_score, best_method = 0.0, ""
    candidates = [("title", entity["title"])] + [
        ("alias", alias) for alias in entity.get("aliases") or []
    ]
    for kind, value in candidates:
        candidate_words = words(value)
        candidate_norm = " ".join(candidate_words)
        if len(candidate_norm) < 3:
            continue
        if candidate_words == label_words:
            return 0.97, f"{kind}:{value}"

        strong = len(candidate_words) >= 2 or len(candidate_norm) >= 10
        shorter = candidate_words if len(candidate_words) <= len(label_words) else label_words
        meaningful = any(w not in STOPWORDS and not w.isdigit() for w in shorter)
        if strong and meaningful and (contains_words(label_words, candidate_words)
                                      or contains_words(candidate_words, label_words)):
            coverage = min(len(candidate_words), len(label_words)) / max(
                len(candidate_words), len(label_words))
            score = 0.62 + 0.33 * coverage
            if score > best_score:
                best_score, best_method = score, f"{kind}~{value}"

        if node_comment and strong and meaningful and contains_words(
                node_comment.split(), candidate_words):
            if 0.90 > best_score:
                best_score, best_method = 0.90, f"{kind}~comment:{value}"

    node_tokens = tokens(node_text)
    entity_tokens = tokens(entity["title"] + " " + " ".join(entity.get("aliases") or []))
    if node_tokens and entity_tokens:
        common = common_tokens(node_tokens, entity_tokens)
        if common:
            cover_node = len(common) / len(node_tokens)
            cover_entity = len(common) / len(entity_tokens)
            # Одно совпавшее слово ничего не доказывает («Обработка ЦС» ↔
            # «Обработка ИИ»), поэтому требуем минимум два общих слова и
            # покрытие подписи узла.
            if len(common) >= 2 and cover_node >= 0.6:
                score = 0.55 + 0.25 * cover_node + 0.15 * cover_entity
                if score > best_score:
                    best_score = score
                    best_method = "tokens:" + ",".join(sorted(common))

    # Комментарий LLM — тоже источник сопоставления (но слабее названий):
    # он объясняет сущность словами, которые часто и стоят на узле.
    comment_tokens = tokens(entity["comment"])
    if node_tokens and comment_tokens:
        common_comment = common_tokens(node_tokens, comment_tokens)
        if len(common_comment) >= 2:
            cover_node = len(common_comment) / len(node_tokens)
            if cover_node >= 0.7:
                score = 0.55 + 0.20 * cover_node
                if score > best_score:
                    best_score = score
                    best_method = "comment:" + ",".join(sorted(common_comment))

    return best_score, best_method


def match_entities(entities: list[dict], nodes: list[dict],
                   min_score: float = DEFAULT_MIN_SCORE) -> tuple[dict, list[dict], list[dict]]:
    """Жадно развести сущности по узлам (1:1) по убыванию оценки.

    Контейнеры (swimlane, рамки веток процесса) матчатся только по явным
    названиям (`CONTAINER_MIN_SCORE`), иначе они перетягивают на себя
    случайные сущности из-за крупных подписей.
    """
    by_key = {e["syncKey"]: e for e in entities}
    matched: dict[str, dict] = {}
    used_nodes: set[tuple[str, str]] = set()

    # 1) Явные привязки из `bindings` — сильнее любого авто-сопоставления.
    for node in nodes:
        key = node.get("bound_key")
        if not key:
            continue
        if key not in by_key:
            raise SystemExit(
                f"bindings: узел {node['id']} привязан к несуществующей "
                f"сущности `{key}` (проверьте docs/diagram-sync-ignore.json)"
            )
        matched[key] = {
            "cellId": node["id"],
            "page": node["page"],
            "nodeLabel": node["label"],
            "score": 1.0,
            "method": "pinned",
        }
        used_nodes.add((node["page"], node["id"]))

    pairs = []
    for entity in entities:
        if entity["syncKey"] in matched:
            continue
        for node in nodes:
            if node.get("no_entity") or node.get("bound_key"):
                continue      # узел сознательно оставлен без сущности / занят pin
            score, method = score_pair(entity, node)
            # Контейнеры (swimlane, рамки веток) по слабым правилам не матчим:
            # крупная подпись иначе перетягивает случайные сущности.
            required = min_score
            if node.get("container") and method.startswith("comment:"):
                required = CONTAINER_MIN_SCORE
            if score >= required:
                pairs.append((score, entity, node, method))
    pairs.sort(key=lambda p: (-p[0], p[1].get("line") or 0, p[2]["id"]))

    for score, entity, node, method in pairs:
        if entity["syncKey"] in matched:
            continue
        node_key = (node["page"], node["id"])
        if node_key in used_nodes:
            continue
        matched[entity["syncKey"]] = {
            "cellId": node["id"],
            "page": node["page"],
            "nodeLabel": node["label"],
            "score": round(score, 3),
            "method": method,
        }
        used_nodes.add(node_key)

    unmatched = [
        {
            "syncKey": e["syncKey"],
            "title": e["title"],
            "kind": e.get("kind"),
            "line": e.get("line"),
            "evidence": e.get("evidence"),
        }
        for e in entities if e["syncKey"] not in matched
    ]
    orphans = [
        {"id": n["id"], "page": n["page"], "label": n["label"]}
        for n in nodes
        if (n["page"], n["id"]) not in used_nodes and not n.get("no_entity")
    ]
    return matched, unmatched, orphans


# --- манифест --------------------------------------------------------------


def build_manifest(a, entities_data: dict, entities: list[dict], nodes: list[dict]) -> dict:
    matched, unmatched, orphans = match_entities(entities, nodes, a.min_score)
    records = {}
    # В манифест попадают ВСЕ сущности (и привязанные, и нет): иначе `check`
    # на каждом запуске считал бы непривязанные «новыми».
    for entity in entities:
        key = entity["syncKey"]
        hit = matched.get(key)
        records[key] = {
            "cellId": hit["cellId"] if hit else None,
            "page": hit["page"] if hit else None,
            "nodeLabel": hit["nodeLabel"] if hit else None,
            "score": hit["score"] if hit else 0.0,
            "method": hit["method"] if hit else "unmatched",
            "title": entity["title"],
            "kind": entity.get("kind"),
            "evidence": entity.get("evidence"),
            "commentHash": comment_hash(entity),
            "comment": entity["comment"],
        }
    readme_path = entities_data.get("readme", {}).get("path", "README.md")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": now(),
        "diagram": {"path": a.diagram, "sha256": sha256_file(a.diagram)},
        "entities": records,
        "unmatched": unmatched,
        "orphans": orphans,
        "pinned": [
            {"id": n["id"], "page": n["page"], "label": n["label"]}
            for n in nodes if n.get("no_entity")
        ],
        "blocks": {
            "path": a.blocks,
            "sha256": sha256_file(a.blocks),
            "model": entities_data.get("model"),
            "promptVersion": entities_data.get("promptVersion"),
            "generatedAt": entities_data.get("generatedAt"),
        },
        "readme": {
            "path": readme_path,
            "sha256": entities_data.get("readme", {}).get("sha256"),
        },
    }


def load_manifest(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save_manifest(path: str | Path, manifest: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require_manifest(path: str | Path) -> dict:
    manifest = load_manifest(path)
    if not manifest:
        raise SystemExit(f"Нет манифеста {path}. Сначала: sync_check.py bootstrap ...")
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise SystemExit(
            f"Манифест {path} старой версии ({manifest.get('schemaVersion')}); "
            "он построен по сырому тексту README. Пересоберите: "
            "sync_check.py bootstrap ..."
        )
    return manifest


# --- команды ---------------------------------------------------------------


def _load_all(a):
    entities_data = load_entities(a.blocks)
    entities = entities_data["entities"]
    nodes = diagram_nodes(a.diagram, a.ignore)
    return entities_data, entities, nodes


def split_unmatched(unmatched: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разделить «без узла» на процессные сущности и детали реализации."""
    process = [u for u in unmatched if u.get("kind") in PROCESS_KINDS]
    details = [u for u in unmatched if u.get("kind") not in PROCESS_KINDS]
    return process, details


def cmd_bootstrap(a) -> int:
    entities_data, entities, nodes = _load_all(a)
    manifest = build_manifest(a, entities_data, entities, nodes)
    save_manifest(a.manifest, manifest)
    matched = {k: v for k, v in manifest["entities"].items() if v.get("cellId")}
    process, details = split_unmatched(manifest["unmatched"])
    print(
        f"bootstrap: сущностей {len(entities)}, привязано {len(matched)}, "
        f"без узла {len(manifest['unmatched'])} "
        f"(процессных {len(process)}, деталей реализации {len(details)}), "
        f"узлов без сущности {len(manifest['orphans'])}"
    )
    low = [r for r in matched.values() if r["score"] < 0.75 and r["method"] != "syncKey"]
    if low:
        print(f"\nСлабые привязки ({len(low)}) — проверьте вручную:")
        for record in sorted(low, key=lambda r: r["score"])[:15]:
            print(f"  - {record['title']!r} → {record['nodeLabel']!r} "
                  f"({record['score']}, {record['method']})")
    print("\nДальше: sync_check.py annotate ... (закрепить комментарии на узлах)")
    print(f"Манифест: {a.manifest}")
    return 0


def cmd_annotate(a) -> int:
    entities_data, entities, nodes = _load_all(a)
    by_key = {e["syncKey"]: e for e in entities}
    matched, unmatched, _ = match_entities(entities, nodes, a.min_score)

    doc = drawio_io.load(a.diagram)
    stamp = now()
    changed = 0
    for key, hit in matched.items():
        entity = by_key[key]
        attrs = {
            "syncKey": key,
            "syncComment": entity["comment"],
            "syncAliases": " | ".join(entity.get("aliases") or []) or None,
            "syncSource": entity.get("evidence") or entity.get("section") or None,
            "syncAt": stamp,
        }
        if a.dry_run:
            print(f"[dry-run] {hit['cellId']} {hit['nodeLabel']!r} ← {key}")
            continue
        if drawio_io.set_cell_attrs(doc, hit["page"], hit["cellId"], attrs):
            changed += 1

    if a.dry_run:
        print(f"dry-run: привязано {len(matched)}, без узла {len(unmatched)}; файл не изменён")
        return 0

    pruned = 0
    if a.prune:
        stale = {n["syncKey"] for n in nodes if n["syncKey"]} - set(matched)
        for node in nodes:
            if node["syncKey"] in stale:
                drawio_io.set_cell_attrs(doc, node["page"], node["id"], {
                    "syncKey": None, "syncComment": None, "syncAliases": None,
                    "syncSource": None, "syncAt": None,
                })
                pruned += 1

    drawio_io.save(a.diagram, doc["root"])
    errors = drawio_io.validate(a.diagram)
    if errors:
        print("ОШИБКА валидации после записи:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    manifest = build_manifest(a, entities_data, entities, diagram_nodes(a.diagram, a.ignore))
    save_manifest(a.manifest, manifest)
    print(f"annotate: комментарии закреплены на {changed} узлах; без узла {len(unmatched)}")
    if pruned:
        print(f"annotate: сняты устаревшие комментарии с {pruned} узлов (--prune)")
    process, details = split_unmatched(unmatched)
    if process:
        print("Процессные сущности без узла (добавьте узел или расширьте aliases):")
        for item in process[:20]:
            print(f"  - {item['syncKey']}: {item['title']}")
    if details:
        print(f"Деталей реализации без узла: {len(details)} — для схемы процесса это норма.")
    print(f"Манифест: {a.manifest}")
    return 0


def cmd_check(a) -> int:
    manifest = require_manifest(a.manifest)
    entities_data = load_entities(a.blocks)
    entities = entities_data["entities"]
    nodes = diagram_nodes(a.diagram, a.ignore)
    matched, unmatched, _ = match_entities(entities, nodes, a.min_score)

    problems: list[str] = []

    readme_sha = entities_data.get("readme", {}).get("sha256")
    if readme_sha and manifest.get("readme", {}).get("sha256") != readme_sha:
        problems.append(
            "README изменился после последней сборки сущностей — "
            "пересоберите docs/readme-blocks.json (readme_llm_blocks.py)."
        )
    if manifest.get("diagram", {}).get("sha256") != sha256_file(a.diagram):
        problems.append("Диаграмма менялась после последней синхронизации.")

    known = manifest.get("entities", {})
    known_keys = set(known)
    current_keys = {e["syncKey"] for e in entities}
    added = [e for e in entities if e["syncKey"] not in known_keys]
    gone = [key for key in known if key not in current_keys]
    changed = [
        e for e in entities
        if e["syncKey"] in known and known[e["syncKey"]].get("commentHash") != comment_hash(e)
    ]
    moved = [
        (e, matched[e["syncKey"]])
        for e in entities
        if e["syncKey"] in matched
        and known.get(e["syncKey"], {}).get("cellId")
        and known[e["syncKey"]]["cellId"] != matched[e["syncKey"]]["cellId"]
    ]

    if not (problems or added or gone or changed or moved):
        print("OK: README, сущности и диаграмма согласованы.")
        return 0

    print("## Изменения\n")
    for problem in problems:
        print(f"- {problem}")

    if added:
        added_process, added_details = split_unmatched(added)
        print(f"\n### Новые сущности ({len(added)}) — нужен узел или объяснение\n")
        for entity in added_process:
            print(f"- `{entity['syncKey']}` — {entity['title']} "
                  f"({entity.get('kind')}, строка {entity.get('line')})")
        if added_details:
            print(f"\nДетали реализации (узлы обычно не нужны, {len(added_details)}):")
            for entity in added_details[:20]:
                print(f"- `{entity['syncKey']}` — {entity['title']} ({entity.get('kind')})")
    if gone:
        print(f"\n### Исчезли сущности ({len(gone)}) — проверить, не устарели ли узлы\n")
        for key in gone:
            record = known[key]
            print(f"- `{key}` — {record.get('title')} (узел {record.get('cellId')})")
    if changed:
        print(f"\n### Изменились комментарии ({len(changed)}) — обновить атрибуты узлов\n")
        for entity in changed:
            cell = known[entity["syncKey"]].get("cellId") or "—"
            print(f"- `{entity['syncKey']}` → узел {cell}: {entity['comment'][:120]}")
    if moved:
        print(f"\n### Изменилась привязка ({len(moved)}) — проверить соответствие\n")
        for entity, hit in moved:
            old = known[entity["syncKey"]].get("cellId")
            print(f"- `{entity['syncKey']}`: {old} → {hit['cellId']} "
                  f"({hit['score']}, {hit['method']})")

    process, details = split_unmatched(unmatched)
    print(f"\nБез узла: {len(unmatched)} (процессных {len(process)}, "
          f"деталей реализации {len(details)}). "
          f"Дальше: правка .drawio → `annotate` (или `stamp`, если узел уже верный).")
    return 2


def cmd_stamp(a) -> int:
    entities_data, entities, nodes = _load_all(a)
    manifest = build_manifest(a, entities_data, entities, nodes)
    save_manifest(a.manifest, manifest)
    bound = sum(1 for v in manifest["entities"].values() if v.get("cellId"))
    print(f"stamp: зафиксировано сущностей {len(entities)}, "
          f"привязано {bound}, "
          f"без узла {len(manifest['unmatched'])}, "
          f"pin без сущности {len(manifest.get('pinned') or [])} → {a.manifest}")
    return 0


def cmd_report(a) -> int:
    manifest = require_manifest(a.manifest)
    records = {k: v for k, v in manifest.get("entities", {}).items() if v.get("cellId")}
    print(f"Сущностей привязано: {len(records)}; "
          f"без узла: {len(manifest.get('unmatched', []))}; "
          f"узлов без сущности: {len(manifest.get('orphans', []))}\n")
    print("| syncKey | сущность | узел | оценка | метод |")
    print("|---|---|---|---|---|")
    for key, record in sorted(records.items(), key=lambda kv: -kv[1]["score"]):
        print(f"| `{key}` | {record.get('title')} | {record.get('nodeLabel')} "
              f"({record.get('cellId')}) | {record.get('score')} | {record.get('method')} |")
    if a.verbose:
        process, details = split_unmatched(manifest.get("unmatched", []))
        if process:
            print("\nБез узла (процессные сущности):")
            for item in process:
                print(f"- `{item['syncKey']}` — {item['title']} (строка {item.get('line')})")
        if details:
            print(f"\nБез узла (детали реализации, {len(details)}) — обычно не нужны на схеме.")
        orphans = manifest.get("orphans", [])
        if orphans:
            print("\nУзлы без сущности:")
            for item in orphans:
                print(f"- {item['id']}: {item['label']}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Синхронизация README ↔ draw.io по LLM-комментариям")
    sub = ap.add_subparsers(dest="cmd", required=True)

    commands = {
        "bootstrap": cmd_bootstrap,
        "annotate": cmd_annotate,
        "check": cmd_check,
        "stamp": cmd_stamp,
        "report": cmd_report,
    }
    for name, function in commands.items():
        p = sub.add_parser(name)
        p.add_argument("--readme", default="README.md",
                       help="оставлен для совместимости CLI; сущности берутся из --blocks")
        p.add_argument("--diagram", required=True)
        p.add_argument("--blocks", default="docs/readme-blocks.json")
        p.add_argument("--manifest", default="docs/diagram-sync.json")
        p.add_argument("--ignore", default="docs/diagram-sync-ignore.json",
                       help="узлы-легенда/оформление, исключаемые из сопоставления")
        p.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--prune", action="store_true",
                       help="annotate: снять sync*-атрибуты с узлов без сущности")
        p.add_argument("--verbose", action="store_true")
        p.set_defaults(func=function)

    a = ap.parse_args()
    try:
        sys.exit(a.func(a))
    except BrokenPipeError:      # вывод в head/less
        sys.exit(0)


if __name__ == "__main__":
    main()
