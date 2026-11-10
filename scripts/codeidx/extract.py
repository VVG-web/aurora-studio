#!/usr/bin/env python3
"""extract.py — выбор адаптера по языку и общие метки требований.

Здесь нет разбора конкретного языка: только диспетчер и то, что одинаково для всех файлов.
Метки `REQ:`, `US-…`, `ALG-…` и известные ключи проекта попадают в `reqtag` независимо от
языка: это единственный канал, по которому код сам говорит, к какому требованию относится.
"""
from __future__ import annotations

import re

try:
    from .adapters import optional, spec_files, sql_files, stdlib_jvm, stdlib_misc, stdlib_py, stdlib_swift, stdlib_web
except ImportError:  # запуск файла напрямую
    from adapters import optional, spec_files, sql_files, stdlib_jvm, stdlib_misc, stdlib_py, stdlib_swift, stdlib_web  # type: ignore

SRC = "stdlib"
REQ_RX = re.compile(r"\b(?:REQ|REQUIREMENT)\s*:\s*([A-Za-z][A-Za-z0-9_.-]{1,40})")
ART_RX = re.compile(r"\b(?:ALG|AC|CON|MAP|SPR)-\d+(?:\.\d+)*\b")
STORY_RX = re.compile(r"\bUS-\d+(?:\.\d+)*\b")


def _kind(key: str) -> str:
    if key.startswith("US-"):
        return "story"
    if key.startswith(("ALG-", "AC-", "CON-", "MAP-", "SPR-")):
        return "artifact"
    return "jira"


def _symbol_at(records, line: int) -> str:
    for rec in records:
        if rec.get("rec") != "symbol":
            continue
        start = int(rec.get("line_start") or 0)
        end = int(rec.get("line_end") or start)
        if start <= line <= end:
            return rec.get("id", "")
    return ""


def reqtags(path: str, text: str, records, keys=()) -> list:
    """Метки требований: `REQ:`, коды артефактов, номера историй и известные ключи проекта."""
    key_rx = None
    known = {str(k).upper() for k in keys if str(k).strip()}
    if known:
        key_rx = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(known)) + r")-\d+\b")
    out = []
    for line_no, line in enumerate(text.splitlines(), 1):
        found = []
        for m in REQ_RX.finditer(line):
            found.append((m.group(1), "req"))
        for m in ART_RX.finditer(line):
            found.append((m.group(0), _kind(m.group(0))))
        for m in STORY_RX.finditer(line):
            found.append((m.group(0), "story"))
        if key_rx:
            for m in key_rx.finditer(line):
                found.append((m.group(0), "jira"))
        seen = set()
        for key, kind in found:
            if key in seen:
                continue
            seen.add(key)
            out.append({"rec": "reqtag", "file": path, "key": key, "line": line_no,
                        "symbol": _symbol_at(records, line_no), "kind": kind})
    return out


def extract_file(path: str, text: str, lang: str, role: str = "src", keys=()) -> list:
    """Все записи файла. Неизвестный язык — пустой список, а не ошибка."""
    if not text.strip():
        return []
    records = []
    if lang == "Python":
        records = stdlib_py.extract(path, text, role)
    elif lang in ("Java", "Kotlin", "C#", "Go"):
        records = stdlib_jvm.extract(path, text, role, lang)
    elif lang in ("TypeScript", "JavaScript", "Vue", "Svelte"):
        records = stdlib_web.extract(path, text, role, lang)
    elif lang == "Swift":
        records = stdlib_swift.extract(path, text, role, lang)
    elif lang in ("SQL", "PL/SQL"):
        records = sql_files.extract(path, text, role)
    elif lang in ("YAML", "Properties", "JSON", "TOML", "INI", "Maven POM", "Gradle",
                  "Python project", "npm", "Cargo", "Go module"):
        records = stdlib_misc.extract(path, text, role)
    elif lang in ("Protocol Buffers", "GraphQL"):
        records = spec_files.extract(path, text, role)
    for rec in records:
        rec.setdefault("file", path)
        rec.setdefault("src", SRC)
        rec.setdefault("conf", "EXTRACTED")
    # Необязательные адаптеры (graphify, tree-sitter, ast-grep, lizard) добавляют записи
    # только если их команда задана в окружении; ошибка слота не роняет файл.
    try:
        records.extend(optional.all(path, text, lang, role))
    except Exception:  # noqa: BLE001 — адаптер не вправе уронить индекс
        pass
    if role in ("src", "test"):
        records.extend(reqtags(path, text, records, keys))
    return records
