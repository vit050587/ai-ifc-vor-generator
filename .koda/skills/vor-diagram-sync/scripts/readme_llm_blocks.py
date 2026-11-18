#!/usr/bin/env python3
"""readme_llm_blocks.py — блоки README + LLM-комментарии (Qwen3.8 через Ollama).

Зачем: прежний `sync_check.py` вытаскивал «сущности» эвристикой (заголовки,
первые колонки таблиц, все идентификаторы в backticks) и сопоставлял их с
узлами диаграммы через `difflib` по сырому тексту. Из-за этого в манифест
попадали сотни мусорных сущностей и появлялись ложные привязки.

Новый порядок:

1. README детерминированно режется на смысловые блоки (заголовок задаёт
   раздел, абзацы/списки/таблицы/код — отдельные блоки).
2. Блоки батчами уходят в локальную LLM (`qwen3.8:27b` через Ollama) с
   промптом `prompts/readme_blocks_prompt.md`. Модель оставляет аннотации
   только тем блокам, которые реально являются частью бизнес-процесса:
   `kind`, `title`, `syncKey`, `comment`, `aliases`.
3. Аннотации кешируются по хешу текста блока + модели + версии промпта,
   поэтому повторный запуск не дёргает LLM для неизменённых блоков.
4. Результат — `docs/readme-blocks.json`: единственный источник сущностей
   для `sync_check.py` (сопоставление с узлами идёт **только** по
   комментарию/алиасам/`syncKey`, а не по сырому тексту README).

CLI:
  python3 readme_llm_blocks.py --split-only        # только нарезка (без LLM)
  python3 readme_llm_blocks.py --limit 8           # пилотный прогон
  python3 readme_llm_blocks.py                     # полный прогон с кешем
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- константы -------------------------------------------------------------

PROMPT_VERSION = "v1"
DEFAULT_MODEL = "qwen3.8:27b"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_OUT = "docs/readme-blocks.json"
DEFAULT_RAW = "docs/readme-blocks-raw.json"
DEFAULT_CACHE = "docs/readme-blocks-cache.json"
DEFAULT_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "readme_blocks_prompt.md"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
TABLE_RE = re.compile(r"^\s*\|")

MAX_BLOCK_CHARS = 1800      # сколько текста блока реально уходит в LLM
MAX_TABLE_ROWS = 30         # длинные таблицы режутся на части
MAX_LIST_LINES = 25         # длинные списки режутся на части
MIN_BLOCK_CHARS = 30        # мелочь (подписи, пустые абзацы) не отправляем

SKIP_SECTIONS = {"содержание", "оглавление"}

KINDS = {
    "mode", "stage", "step", "module", "artifact", "api",
    "data", "model", "external", "rule", "role",
}

SYNCKEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")

# Простая транслитерация для нормализации syncKey (модель иногда отдаёт кириллицу)
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


# --- утилиты ---------------------------------------------------------------


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_env(path: str | Path = ".env") -> dict[str, str]:
    """Минимальный парсер .env (без зависимостей от основного сервиса)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def translit_slug(value: str) -> str:
    lowered = value.strip().lower().replace("ё", "е")
    chars = [TRANSLIT.get(ch, ch) for ch in lowered]
    slug = re.sub(r"[^a-z0-9]+", "-", "".join(chars)).strip("-")
    return slug[:60]


# --- нарезка README на блоки ----------------------------------------------


def _block_id(section: str, kind: str, text: str) -> str:
    return "b" + sha256_text(f"{section}\x00{kind}\x00{text}")[:8]


def _push(blocks: list[dict], kind: str, text: str, line: int, section: str) -> None:
    text = text.strip("\n").strip()
    if not text or len(re.sub(r"\s+", " ", text)) < MIN_BLOCK_CHARS:
        return
    truncated = len(text) > MAX_BLOCK_CHARS
    if truncated:
        text = text[:MAX_BLOCK_CHARS].rstrip() + " …[фрагмент обрезан]"
    blocks.append({
        "blockId": _block_id(section, kind, text),
        "kind": kind,
        "section": section,
        "line": line,
        "chars": len(text),
        "truncated": truncated,
        "hash": sha256_text(f"{section}\x00{kind}\x00{text}")[:16],
        "text": text,
    })


def _split_table(rows: list[str], start_line: int) -> list[tuple[str, int]]:
    """Длинную таблицу режем по строкам, повторяя шапку."""
    if len(rows) <= MAX_TABLE_ROWS:
        return [("\n".join(rows), start_line)]
    header = rows[:2] if len(rows) > 2 and re.match(r"^\s*\|[\s\-:|]+\|?\s*$", rows[1]) else rows[:1]
    body = rows[len(header):]
    step = MAX_TABLE_ROWS - len(header)
    chunks: list[tuple[str, int]] = []
    for offset in range(0, len(body), step):
        part = header + body[offset:offset + step]
        chunks.append(("\n".join(part), start_line + len(header) + offset))
    return chunks


def _split_list(lines: list[str], start_line: int) -> list[tuple[str, int]]:
    if len(lines) <= MAX_LIST_LINES:
        return [("\n".join(lines), start_line)]
    chunks: list[tuple[str, int]] = []
    buf: list[str] = []
    for idx, line in enumerate(lines):
        if LIST_RE.match(line) and len(buf) >= MAX_LIST_LINES:
            chunks.append(("\n".join(buf), start_line + idx - len(buf)))
            buf = []
        buf.append(line)
    if buf:
        chunks.append(("\n".join(buf), start_line + len(lines) - len(buf)))
    return chunks


def split_blocks(text: str) -> list[dict]:
    """Разбить markdown на смысловые блоки с контекстом раздела."""
    lines = text.splitlines()
    blocks: list[dict] = []
    stack: list[tuple[int, str]] = []
    i, total = 0, len(lines)

    def section_path() -> str:
        return " / ".join(title for _, title in stack) or "(корень документа)"

    def section_skipped() -> bool:
        return bool(stack) and stack[-1][1].strip().lower() in SKIP_SECTIONS

    while i < total:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0] * 3
            start = i
            i += 1
            body: list[str] = []
            while i < total and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # закрывающий забор
            if not section_skipped():
                _push(blocks, "code", "\n".join(body), start + 1, section_path())
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = re.sub(r"<[^>]+>", " ", heading.group(2)).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            i += 1
            continue

        if TABLE_RE.match(line):
            start = i
            rows: list[str] = []
            while i < total and TABLE_RE.match(lines[i]):
                rows.append(lines[i])
                i += 1
            if not section_skipped():
                for chunk, chunk_line in _split_table(rows, start + 1):
                    _push(blocks, "table", chunk, chunk_line, section_path())
            continue

        if LIST_RE.match(line):
            start = i
            items: list[str] = []
            while i < total:
                cur = lines[i]
                if not cur.strip():
                    break
                if HEADING_RE.match(cur) or FENCE_RE.match(cur) or TABLE_RE.match(cur):
                    break
                if not (LIST_RE.match(cur) or cur.startswith((" ", "\t"))):
                    break
                items.append(cur)
                i += 1
            if not section_skipped():
                for chunk, chunk_line in _split_list(items, start + 1):
                    _push(blocks, "list", chunk, chunk_line, section_path())
            continue

        start = i
        para: list[str] = []
        while i < total:
            cur = lines[i]
            if not cur.strip():
                break
            if HEADING_RE.match(cur) or FENCE_RE.match(cur) or TABLE_RE.match(cur) or LIST_RE.match(cur):
                break
            para.append(cur)
            i += 1
        if not section_skipped():
            _push(blocks, "para", "\n".join(para), start + 1, section_path())

    return blocks


# --- LLM ------------------------------------------------------------------


class LLMError(RuntimeError):
    """Ошибка обращения к Ollama."""


class OllamaClient:
    """Тонкая обёртка над /api/chat Ollama (stdlib, без внешних зависимостей)."""

    def __init__(self, base_url: str, model: str, timeout: float = 600.0,
                 num_ctx: int = 16384, num_predict: int = 3072) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict

    def chat_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMError(f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"LLM недоступна ({self.base_url}): {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"некорректный ответ Ollama: {exc}") from exc

        content = (data.get("message") or {}).get("content") or ""
        parsed = extract_json(content)
        if parsed is None:
            raise LLMError(f"LLM вернула не JSON: {content[:300]!r}")
        return parsed


def extract_json(text: str) -> dict | None:
    if not text:
        return None
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(stripped[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, dict) else None
    return None


def load_prompt(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"Нет файла промпта: {p}\n"
            "Ожидается prompts/readme_blocks_prompt.md внутри скилла."
        )
    return p.read_text(encoding="utf-8")


def split_prompt(prompt: str) -> tuple[str, str]:
    """Промпт делится на системную часть и шаблон пользовательского сообщения.

    Формат файла: `## SYSTEM` ... `## USER` ... . Если маркеров нет — весь
    файл считается пользовательским шаблоном.
    """
    if "## SYSTEM" not in prompt:
        return "", prompt
    _, rest = prompt.split("## SYSTEM", 1)
    if "## USER" in rest:
        system, user = rest.split("## USER", 1)
    else:
        system, user = rest, ""
    return system.strip(), user.strip()


def build_user_message(template: str, batch: list[dict]) -> str:
    chunks = []
    for block in batch:
        chunks.append(
            f"--- БЛОК {block['blockId']} | раздел: {block['section']} | "
            f"строка {block['line']} | тип: {block['kind']} ---\n{block['text']}"
        )
    return f"{template}\n\n" + "\n\n".join(chunks) + "\n"


# --- нормализация и слияние аннотаций -------------------------------------


def _clean_str(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_annotation(raw: dict, known_ids: set[str]) -> dict | None:
    """Проверить и привести к канону одну аннотацию модели."""
    block_id = _clean_str(raw.get("blockId") or raw.get("block_id"))
    if block_id not in known_ids:
        return None

    kind = _clean_str(raw.get("kind")).lower()
    if kind not in KINDS:
        kind = "step"

    title = _clean_str(raw.get("title"))
    if not title:
        return None
    title = title.strip(" .,:;—–-")[:120]

    sync_key = _clean_str(raw.get("syncKey") or raw.get("sync_key"))
    if not SYNCKEY_RE.match(sync_key):
        sync_key = translit_slug(sync_key or title)
    if not sync_key or len(sync_key) < 3:
        sync_key = "block-" + block_id
    sync_key = sync_key[:60].strip("-")

    comment = _clean_str(raw.get("comment"))
    if not comment:
        comment = title

    aliases_raw = raw.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [a for a in re.split(r"[,;]\s*", aliases_raw)]
    aliases = []
    for alias in aliases_raw if isinstance(aliases_raw, list) else []:
        alias = _clean_str(alias)
        if alias and alias.lower() != title.lower() and len(alias) <= 120:
            aliases.append(alias)

    evidence = _clean_str(raw.get("evidence"))

    return {
        "blockId": block_id,
        "kind": kind,
        "title": title,
        "syncKey": sync_key,
        "comment": comment[:600],
        "aliases": sorted(set(aliases))[:8],
        "evidence": evidence[:300],
    }


def title_key(title: str) -> str:
    """Ключ для отсечения смысловых дублей по названию сущности."""
    value = re.sub(r"<[^>]+>", " ", title or "").lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def merge_annotations(annotations: list[dict], blocks_by_id: dict[str, dict]) -> list[dict]:
    """Слить аннотации по syncKey, затем отсечь дубли по названию.

    Модель видит README батчами и может назвать одну и ту же сущность
    по-разному (`check-cim-model` и `common-check-bim`). Одинаковое
    нормализованное название считаем одной сущностью: остаётся более ранняя
    запись, у неё объединяются алиасы и блочные ссылки.
    """
    merged: dict[str, dict] = {}
    for ann in annotations:
        key = ann["syncKey"]
        block = blocks_by_id.get(ann["blockId"], {})
        entry = merged.get(key)
        if entry is None:
            merged[key] = {
                "syncKey": key,
                "kind": ann["kind"],
                "title": ann["title"],
                "comment": ann["comment"],
                "aliases": list(ann["aliases"]),
                "evidence": ann["evidence"] or block.get("section", ""),
                "section": block.get("section", ""),
                "line": block.get("line"),
                "blockIds": [ann["blockId"]],
            }
            continue
        if len(ann["comment"]) > len(entry["comment"]):
            entry["comment"] = ann["comment"]
        if ann["title"] and len(ann["title"]) < len(entry["title"]):
            entry["title"] = ann["title"]
        if entry["kind"] == "step" and ann["kind"] != "step":
            entry["kind"] = ann["kind"]
        entry["aliases"] = sorted(set(entry["aliases"]) | set(ann["aliases"]))[:10]
        entry["blockIds"].append(ann["blockId"])
        if block.get("line") and (entry["line"] is None or block["line"] < entry["line"]):
            entry["line"] = block["line"]
            entry["section"] = block.get("section", entry["section"])

    deduped: dict[str, dict] = {}
    for entry in sorted(merged.values(), key=lambda e: (e["line"] or 0, e["syncKey"])):
        key = title_key(entry["title"])
        if key in deduped:
            keeper = deduped[key]
            keeper["aliases"] = sorted(set(keeper["aliases"]) | set(entry["aliases"]))[:10]
            keeper["blockIds"].extend(entry["blockIds"])
            if len(entry["comment"]) > len(keeper["comment"]):
                keeper["comment"] = entry["comment"]
            continue
        deduped[key] = entry

    return sorted(deduped.values(), key=lambda e: (e["line"] or 0, e["syncKey"]))


# --- основной пайплайн -----------------------------------------------------


def load_cache(path: str | Path, model: str, prompt_hash: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"model": model, "promptHash": prompt_hash, "entries": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"model": model, "promptHash": prompt_hash, "entries": {}}
    if data.get("model") != model or data.get("promptHash") != prompt_hash:
        # Сменились модель или промпт — прежние аннотации недействительны.
        return {"model": model, "promptHash": prompt_hash, "entries": {}}
    data.setdefault("entries", {})
    return data


def save_json(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_batches(blocks: list[dict], char_budget: int, max_blocks: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for block in blocks:
        length = len(block["text"]) + 160
        if current and (size + length > char_budget or len(current) >= max_blocks):
            batches.append(current)
            current, size = [], 0
        current.append(block)
        size += length
    if current:
        batches.append(current)
    return batches


def annotate(a) -> int:
    readme_path = Path(a.readme)
    if not readme_path.exists():
        raise SystemExit(f"Нет README: {readme_path}")

    blocks = split_blocks(readme_path.read_text(encoding="utf-8"))
    blocks_by_id = {b["blockId"]: b for b in blocks}
    print(f"README: {readme_path} — блоков: {len(blocks)}")

    if a.split_only:
        save_json(a.raw, {
            "schemaVersion": 1,
            "generatedAt": now(),
            "readme": {"path": str(readme_path), "sha256": sha256_file(readme_path)},
            "stats": {"blocks": len(blocks)},
            "blocks": blocks,
        })
        by_kind: dict[str, int] = {}
        for block in blocks:
            by_kind[block["kind"]] = by_kind.get(block["kind"], 0) + 1
        print(f"нарезка (без LLM): {by_kind} → {a.raw}")
        return 0

    if a.limit:
        blocks = blocks[: a.limit]
        blocks_by_id = {b["blockId"]: b for b in blocks}
        print(f"ограничение --limit: обрабатываем {len(blocks)} блоков")

    prompt_text = load_prompt(a.prompt)
    system, user_template = split_prompt(prompt_text)
    prompt_hash = sha256_text(prompt_text)[:16]

    env = read_env(a.env)
    base_url = a.base_url or env.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL
    model = a.model or env.get("LLM_MODEL") or env.get("CHECK_MODEL_LLM_MODEL") or DEFAULT_MODEL
    client = OllamaClient(base_url, model, timeout=a.timeout, num_ctx=a.num_ctx,
                          num_predict=a.num_predict)
    print(f"LLM: {model} @ {base_url} (prompt {PROMPT_VERSION}/{prompt_hash})")

    cache = load_cache(a.cache, model, prompt_hash) if a.use_cache else {
        "model": model, "promptHash": prompt_hash, "entries": {}
    }

    pending = [b for b in blocks if b["hash"] not in cache["entries"]]
    print(f"из кеша: {len(blocks) - len(pending)}, в LLM: {len(pending)}")
    batches = make_batches(pending, a.batch_chars, a.batch_blocks)

    started = time.time()
    failed = 0
    for number, batch in enumerate(batches, 1):
        user_message = build_user_message(user_template, batch)
        parsed, error = None, None
        for attempt in (1, 2):
            try:
                parsed = client.chat_json(system, user_message)
                break
            except LLMError as exc:
                error = exc
                if attempt == 1:
                    print(f"  повтор после ошибки: {exc}", file=sys.stderr)
                    time.sleep(5)
        known_ids = {b["blockId"] for b in batch}
        annotations: list[dict] = []
        if parsed is None:
            failed += 1
            print(f"[{number}/{len(batches)}] ОШИБКА: {error}", file=sys.stderr)
            continue
        for raw in parsed.get("blocks") or parsed.get("items") or []:
            if not isinstance(raw, dict):
                continue
            ann = normalize_annotation(raw, known_ids)
            if ann:
                annotations.append(ann)
        for block in batch:
            cache["entries"][block["hash"]] = [
                ann for ann in annotations if ann["blockId"] == block["blockId"]
            ]
        print(f"[{number}/{len(batches)}] блоков {len(batch)} → сущностей {len(annotations)} "
              f"({time.time() - started:.0f} с)")

    if a.use_cache:
        save_json(a.cache, cache)

    all_annotations: list[dict] = []
    for block in blocks:
        all_annotations.extend(cache["entries"].get(block["hash"], []))

    entities = merge_annotations(all_annotations, blocks_by_id)
    result = {
        "schemaVersion": 2,
        "generatedAt": now(),
        "promptVersion": PROMPT_VERSION,
        "promptHash": prompt_hash,
        "model": model,
        "readme": {
            "path": str(readme_path),
            "sha256": sha256_file(readme_path),
            "lines": len(readme_path.read_text(encoding="utf-8").splitlines()),
        },
        "stats": {
            "blocks": len(blocks),
            "annotatedBlocks": len(all_annotations),
            "entities": len(entities),
            "failedBatches": failed,
        },
        "entities": entities,
    }
    save_json(a.out, result)

    by_kind: dict[str, int] = {}
    for entity in entities:
        by_kind[entity["kind"]] = by_kind.get(entity["kind"], 0) + 1
    print(f"итог: сущностей {len(entities)} {by_kind} → {a.out}")
    if failed:
        print(f"ВНИМАНИЕ: {failed} батч(ей) не обработаны — повторите запуск, "
              f"кеш сохранит уже полученное.", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Блоки README + LLM-комментарии (Qwen3.8)")
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--raw", default=DEFAULT_RAW, help="файл для --split-only")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    ap.add_argument("--env", default=".env")
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--num-predict", type=int, default=4096,
                    help="лимит токенов ответа (мало — JSON обрывается)")
    ap.add_argument("--batch-chars", type=int, default=7000)
    ap.add_argument("--batch-blocks", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="обработать только первые N блоков")
    ap.add_argument("--split-only", action="store_true", help="только нарезка, без LLM")
    ap.add_argument("--no-cache", dest="use_cache", action="store_false")
    a = ap.parse_args()
    sys.exit(annotate(a))


if __name__ == "__main__":
    main()
