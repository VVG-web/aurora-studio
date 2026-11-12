#!/usr/bin/env python3
"""context_store.py - хранилище контекста и инструменты памяти Авроры.

Полный текст карточек, файлов и результатов инструментов живёт в SQLite.
В промпт уходит оглавление и краткие конспекты; точный фрагмент достаётся
через `ctx_get`. Это позволяет модели видеть широту, не отправляя в окно все
40 полных карточек.

Хранилище проекта:
- `agent:make`: `Workspaces/<сессия>/context/context.sqlite`;
- `agent:ask`: `.aurora/cache/ask/context/<thread>.sqlite`.

Модуль не ходит в сеть и не зависит от Pydantic AI. Компрессор передаётся
функцией; без неё используется детерминированный экстрактивный конспект.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1
PROTECTED_HEAD = "### Защищённые фрагменты (не сокращать)"
LAW_WORDS = re.compile(
    r"(?i)(закон|законодатель|норматив|постановлен|распоряжен|приказ|кодекс|"
    r"статья|статьи|пункт|подпункт|гост|снип|санпин|регламент|правил|"
    r"№\s*\d|номер\s+\d)")
QUOTE_RE = re.compile(r"«[^»\n]{12,}»|„[^“”\n]{12,}“|“[^”\n]{12,}”")


def protected_excerpts(text: str, limit: int = 24) -> list[str]:
    """Фрагменты, которые нельзя пересказывать при сжатии.

    Это не «важные слова», а проверяемые единицы: цитаты в кавычках, строки
    цитат (`> ...`), предложения с реквизитами закона/нормы и явной пометкой
    `дословно`/`cite`. Компрессор может потерять их, поэтому они идут в
    конспект отдельным блоком дословно. Список ограничен, чтобы защита не
    превратилась в копию всего контекста.
    """
    text = str(text or "")
    out: list[str] = []

    def add(value: str) -> None:
        value = " ".join(str(value or "").split()).strip(" -")
        if len(value) >= 8 and value not in out:
            out.append(value)

    for m in QUOTE_RE.finditer(text):
        add(m.group(0))
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            add(stripped.lstrip("> ").strip())
        if re.search(r"(?i)(дословн|цитат|cite|cited)", stripped):
            add(stripped)
    for sentence in re.split(r"(?<=[.!?])\s+|\n{2,}", text):
        if LAW_WORDS.search(sentence):
            add(sentence)
        if len(out) >= max(1, int(limit)):
            break
    return out[:max(1, int(limit))]


def with_protected(summary: str, text: str) -> str:
    """Дописать к конспекту дословные фрагменты, которые нельзя терять."""
    fragments = protected_excerpts(text)
    if not fragments:
        return str(summary or "")
    tail = "\n".join(f"- {x}" for x in fragments)
    head = str(summary or "").rstrip()
    return (head + "\n\n" if head else "") + PROTECTED_HEAD + "\n" + tail
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'card',
  source TEXT NOT NULL DEFAULT '',
  trust TEXT NOT NULL DEFAULT 'draft',
  hash TEXT NOT NULL DEFAULT '',
  tokens INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  parent_id TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);
CREATE TABLE IF NOT EXISTS pinned (
  id TEXT PRIMARY KEY,
  added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (
  key TEXT PRIMARY KEY,
  level TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


def _tokens(text: str) -> int:
    cyr = sum(1 for ch in str(text or "") if "\u0400" <= ch <= "\u04ff")
    other = len(str(text or "")) - cyr
    return max(1, int(cyr / 2.2 + other / 3.5))


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def _one_line(text: str, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit].rstrip(" ,.;:") + ("..." if len(value) > limit else "")


def _first_sentence(text: str, limit: int = 400) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("---", "```", ">", "#")):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        m = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)
        if m and m[0].strip():
            return _one_line(m[0], limit)
    return _one_line(text, limit)


def _section(text: str, name: str, max_chars: int = 0) -> str:
    """Раздел `## name` из карточки или пакета; вся карточка, если раздела нет."""
    body = str(text or "")
    if not name:
        return body[:max_chars] if max_chars else body
    m = re.search(rf"(?m)^##\s+{re.escape(name)}\s*$", body)
    if not m:
        return body[:max_chars] if max_chars else body
    tail = body[m.end():]
    nxt = re.search(r"(?m)^##\s+", tail)
    part = body[m.start():m.end() + (nxt.start() if nxt else len(tail))]
    return part[:max_chars] if max_chars else part


def _split_cards(pack: str) -> tuple[str, list[dict]]:
    """Пак -> (преамбула, карточки). Заголовок блока: `## имя`."""
    text = str(pack or "")
    # Карточка пака начинается с `---` и `## имя`. Раздел внутри тела карточки
    # (`## Описание`) тоже начинается с `##`, но без `---`: иначе он стал бы
    # отдельной карточкой и TOC раздулся бы разделами.
    starts = list(re.finditer(r"(?m)^---\s*\n+\s*(##\s+.+?)\s*$", text))
    if starts:
        preamble = text[:starts[0].start()]
    else:
        starts = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
        preamble = text[:starts[0].start()] if starts else text
    cards = []
    for i, m in enumerate(starts):
        start = m.start(1) if m.groups() else m.start()
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[start:end].rstrip()
        head = m.group(1).strip().lstrip("#").strip()
        # В старом формате встречался `имя — title`; имя всегда до разделителя.
        stem = re.split(r"\s+[—-]\s+", head, maxsplit=1)[0].strip()
        if not stem or stem.startswith("Оглавление"):
            continue
        title = ""
        tm = re.search(r"(?m)^Название:\s*(.+)$", block)
        if tm:
            title = tm.group(1).strip()
        trust = ""
        tr = re.search(r"(?mi)^\*\*Доверие:\*\*\s*(\w+)", block)
        if tr:
            trust = tr.group(1).strip().lower()
        cards.append({"id": stem, "title": title or stem, "text": block, "trust": trust})
    return preamble, cards


class ContextStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def put(self, item_id: str, text: str, kind: str = "card", source: str = "",
            trust: str = "draft", summary: str = "", parent_id: str = "") -> str:
        item_id = str(item_id or "").strip()
        if not item_id:
            item_id = "item-" + _hash(text)[:12]
        text = str(text or "")
        summary = str(summary or "").strip() or _first_sentence(text)
        now = time.time()
        self.conn.execute(
            """INSERT INTO items(id, kind, source, trust, hash, tokens, summary, text, parent_id, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 kind=excluded.kind, source=excluded.source, trust=excluded.trust,
                 hash=excluded.hash, tokens=excluded.tokens, summary=excluded.summary,
                 text=excluded.text, parent_id=excluded.parent_id, updated_at=excluded.updated_at""",
            (item_id, kind, source, trust, _hash(text), _tokens(text), summary, text, parent_id, now))
        self.conn.commit()
        return item_id

    def put_pack(self, pack: str, source: str = "ctx_pack") -> list[str]:
        _preamble, cards = _split_cards(pack)
        ids = []
        for card in cards:
            desc = _section(card["text"], "Описание")
            gist = _first_sentence(desc) if desc else card.get("title", "")
            if card.get("title") and card["title"] not in gist:
                gist = f"{card['title']}: {gist}" if gist else card["title"]
            ids.append(self.put(card["id"], card["text"], "card", source,
                                card.get("trust") or "draft", gist))
        return ids

    def index_cards(self, cwd: str | os.PathLike) -> list[str]:
        """Проиндексировать существующую базу проекта без изменения файлов.

        Пакеты индексируются через `put_pack`, отдельные карточки — здесь. Полный
        текст остаётся в SQLite, исходные markdown-файлы не переписываются.
        """
        root = Path(cwd) / "AuroraKnowledgeDB"
        if not root.is_dir():
            return []
        ids = []
        for path in sorted(root.rglob("*.md")):
            if "/meta/" in path.as_posix() or path.name.startswith("_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            trust = ""
            m = re.search(r"(?mi)^\*\*Доверие:\*\*\s*(\w+)|^trust:\s*(\w+)", text)
            if m:
                trust = (m.group(1) or m.group(2) or "").lower()
            ids.append(self.put(path.stem, text, "card", str(path.relative_to(root)),
                                trust or "draft"))
        return ids

    def item(self, item_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (str(item_id),)).fetchone()
        return dict(row) if row else None

    def get(self, item_id: str, section: str = "", max_chars: int = 0) -> dict:
        row = self.item(item_id)
        if not row:
            return {"ok": False, "id": item_id, "why": "нет такой записи"}
        text = _section(row["text"], section, max_chars=max_chars)
        return {"ok": True, **{k: row[k] for k in ("id", "kind", "source", "trust", "tokens", "summary")},
                "section": section, "text": text}

    def search(self, query: str, kinds: list[str] | None = None, limit: int = 8) -> list[dict]:
        words = [w.lower() for w in re.findall(r"[\wА-Яа-яЁё-]{2,}", str(query or ""))]
        kinds = [k for k in (kinds or []) if k]
        where = " WHERE kind IN (%s)" % ",".join("?" for _ in kinds) if kinds else ""
        rows = self.conn.execute("SELECT * FROM items" + where, kinds).fetchall()
        scored = []
        for row in rows:
            text = (row["text"] or "").lower()
            summary = (row["summary"] or "").lower()
            score = sum(text.count(w) + 3 * summary.count(w) for w in words)
            if not words:
                score = 1
            if score:
                scored.append((score, row["tokens"] or 0, row))
        scored.sort(key=lambda x: (-x[0], x[1], x[2]["id"]))
        out = []
        for score, _tokens, row in scored[:max(1, int(limit))]:
            out.append({"id": row["id"], "kind": row["kind"], "trust": row["trust"],
                        "tokens": row["tokens"], "summary": row["summary"], "score": score})
        return out

    def expand(self, item_id: str) -> dict:
        row = self.item(item_id)
        if not row:
            return {"ok": False, "id": item_id, "why": "нет такой записи"}
        parent = self.item(row["parent_id"]) if row["parent_id"] else None
        children = [dict(r) for r in self.conn.execute(
            "SELECT id,kind,trust,tokens,summary FROM items WHERE parent_id=? ORDER BY id", (row["id"],))]
        return {"ok": True, "id": row["id"], "parent": parent, "children": children,
                "text": row["text"]}

    def pin(self, item_id: str) -> bool:
        if not self.item(item_id):
            return False
        self.conn.execute("INSERT OR REPLACE INTO pinned(id,added_at) VALUES(?,?)", (item_id, time.time()))
        self.conn.commit()
        return True

    def drop(self, item_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM pinned WHERE id=?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def working_set(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT i.* FROM pinned p JOIN items i ON i.id=p.id ORDER BY p.added_at, i.id").fetchall()
        return [dict(r) for r in rows]

    def toc(self, ids: list[str] | None = None, limit: int = 0) -> str:
        if ids is None:
            rows = self.conn.execute("SELECT * FROM items ORDER BY kind,id").fetchall()
        else:
            rows = [self.conn.execute("SELECT * FROM items WHERE id=?", (i,)).fetchone() for i in ids]
            rows = [r for r in rows if r]
        if limit:
            rows = rows[:limit]
        lines = ["| id | тип | доверие | токенов | суть |", "|---|---|---|---:|---|"]
        for row in rows:
            lines.append(f"| [[{row['id']}]] | {row['kind']} | {row['trust']} | "
                         f"{row['tokens']} | {_one_line(row['summary'], 140)} |")
        return "\n".join(lines)

    def planner_pack(self, pack: str, max_full: int = 10, include_preamble: bool = True) -> str:
        """Оглавление всех карточек + полные первые `max_full` карточек."""
        preamble, cards = _split_cards(pack)
        if not cards:
            return pack
        full = cards[:max(0, int(max_full))]
        rest = cards[len(full):]
        out = [preamble.rstrip()] if include_preamble and preamble.strip() else []
        out += [c["text"] for c in full]
        if rest:
            out.append("\n## Оглавление остальных карточек\n")
            out.append(self.toc([c["id"] for c in rest]))
            out.append("\n_Полный текст любой карточки: ctx_get(id)._\n")
        return "\n".join(out).strip() + "\n"

    def cited_pack(self, pack: str, draft: str, max_full: int = 8) -> str:
        """Карточки, на которые ссылается черновик, плюс оглавление остальных."""
        _preamble, cards = _split_cards(pack)
        by_id = {c["id"]: c for c in cards}
        cited = []
        for ref in re.findall(r"\[\[([^\]|#]+)", str(draft or "")):
            ref = ref.strip()
            if ref in by_id and by_id[ref] not in cited:
                cited.append(by_id[ref])
        for card in cards:
            if len(cited) >= max_full:
                break
            if card not in cited:
                cited.append(card)
        selected = cited[:max_full]
        return "\n".join(c["text"] for c in selected).strip() + "\n"

    def _cache_key(self, ids: list[str], level: str, target: int, model: str) -> str:
        body = "\n".join(f"{i}:{self.item(i).get('hash', '')}" for i in sorted(ids) if self.item(i))
        return _hash(f"{SCHEMA_VERSION}|protect-1|{level}|{target}|{model}|{body}")

    def _cached(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT summary_json FROM summaries WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["summary_json"])
        except (TypeError, ValueError):
            return None

    def summarize(self, ids: list[str], compressor=None, level: str = "card",
                  target_chars: int = 2000, model: str = "extractive") -> dict:
        """Конспект записей с кэшем. Компрессор: callable(text, target_chars) -> dict|str."""
        ids = [i for i in dict.fromkeys(ids) if self.item(i)]
        target_chars = max(256, int(target_chars or 2000))
        items = [self.item(i) for i in ids]
        text = "\n\n".join(f"## {i['id']}\n{i['text']}" for i in items if i)
        key = self._cache_key(ids, level, target_chars, model)
        cached = self._cached(key)
        if cached:
            cached["summary"] = with_protected(cached.get("summary"), text)
            cached["cached"] = True
            return cached
        result = None
        if compressor:
            try:
                got = compressor(text, target_chars)
                if isinstance(got, dict):
                    result = got
                elif isinstance(got, str) and got.strip():
                    result = {"summary": got.strip()}
            except Exception:
                result = None
        if not result:
            result = self._extractive(items, target_chars)
        result["summary"] = with_protected(result.get("summary"), text)
        result.setdefault("claims", [])
        result.setdefault("open_questions", [])
        result["protected"] = protected_excerpts(text)
        result["tokens"] = _tokens(result.get("summary") or "")
        result["ids"] = ids
        result["level"] = level
        result["cached"] = False
        self.conn.execute(
            "INSERT OR REPLACE INTO summaries(key,level,schema_version,model,summary_json,created_at) VALUES(?,?,?,?,?,?)",
            (key, level, SCHEMA_VERSION, model, json.dumps(result, ensure_ascii=False), time.time()))
        self.conn.commit()
        return result

    def _extractive(self, items: list[dict], target_chars: int) -> dict:
        """Детерминированный конспект: заголовки, первые фразы и строки с числами/ссылками."""
        parts, claims = [], []
        for item in items:
            lines = [ln.strip() for ln in (item["text"] or "").splitlines() if ln.strip()]
            picked = []
            for line in lines:
                if line.startswith(("---", "```", ">")):
                    continue
                if (line.startswith("#") or re.search(r"\d|\[\[|NEEDLE|ИГЛА", line)
                        or len(picked) < 2):
                    picked.append(line)
                if len(picked) >= 8:
                    break
            summary = _one_line(" ".join(picked), max(120, target_chars // max(1, len(items))))
            parts.append(f"[[{item['id']}]] {summary}")
            for line in picked:
                if re.search(r"\d|\[\[|NEEDLE|ИГЛА", line):
                    claims.append({"text": _one_line(line, 300), "sources": [item["id"]],
                                   "confidence": "extractive"})
        text = "\n".join(parts)[:target_chars]
        return {"summary": text, "claims": claims[:20], "open_questions": []}

    def maybe_compress(self, ids: list[str], win: int, config: dict, compressor=None,
                       level: str = "pack") -> dict | None:
        """Сжать, если объём достиг порога. Возвращает результат или None."""
        import context_budget as CB
        used = sum((self.item(i) or {}).get("tokens", 0) for i in ids)
        if not CB.should_compress(used, win, config):
            return None
        target = CB.target_chars(win, config)
        return self.summarize(ids, compressor=compressor, level=level, target_chars=target)


def make_store_path(cwd: str | os.PathLike, sid: str) -> Path:
    return Path(cwd) / "Workspaces" / str(sid) / "context" / "context.sqlite"


def ask_store_path(cwd: str | os.PathLike, thread: str = "default") -> Path:
    clean = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", str(thread or "default"))[:80]
    return Path(cwd) / ".aurora" / "cache" / "ask" / "context" / f"{clean}.sqlite"


def open_make_store(cwd: str | os.PathLike, sid: str) -> ContextStore:
    return ContextStore(make_store_path(cwd, sid))


def open_ask_store(cwd: str | os.PathLike, thread: str = "default") -> ContextStore:
    return ContextStore(ask_store_path(cwd, thread))


def tool_result(store: ContextStore, result: str, kind: str = "tool", source: str = "") -> dict:
    """Сохранить результат инструмента целиком и вернуть краткую ссылку для промпта."""
    item_id = "tool-" + _hash(result)[:16]
    store.put(item_id, result, kind=kind, source=source, trust="tool",
              summary=_first_sentence(result, 300))
    return {"id": item_id, "summary": _first_sentence(result, 300), "tokens": _tokens(result)}
