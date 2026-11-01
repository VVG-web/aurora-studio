#!/usr/bin/env python3
"""sql_files.py — DDL и запросы SQL без внешнего парсера.

DDL разбирается по скобкам, а не регуляркой: `VARCHAR(64)` и вложенные ограничения не должны
ломать разбор. Запросы отдают имена таблиц через общий `kb_code_graph.parse_sql`, если он есть;
иначе простой запасной поиск после FROM/JOIN/UPDATE/INTO. Колонки, PK, FK и уникальность
попадают в `db_columns`; схема не додумывается.
"""
from __future__ import annotations

import re

try:
    from .. import model
except ImportError:  # запуск файла напрямую
    import model  # type: ignore

SRC = "stdlib-sql"
CREATE_RX = re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.$]*)\s*\(", re.I)
UNIQUE_RX = re.compile(
    r"\bCREATE\s+UNIQUE\s+INDEX\s+\w+\s+ON\s+([A-Za-z_][\w.$]*)\s*\(([^)]*)\)", re.I)
ALTER_RX = re.compile(r"\bALTER\s+TABLE\s+([A-Za-z_][\w.$]*)\s+ADD\s+(?:COLUMN\s+)?([A-Za-z_]\w*)\s+([^;]+)", re.I)
QUERY_RX = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+([A-Za-z_][\w.$]*)", re.I)
NOT_TABLE = {"select", "where", "set", "values", "dual", "table", "into"}


def _clean(text: str) -> str:
    text = re.sub(r"--[^\n]*", " ", text)
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def _matching_paren(text: str, start: int) -> int:
    depth = 0
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _split_top(body: str) -> list:
    parts, cur, depth, quote = [], [], 0, ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _column(part: str, table: str, line: int, source: str, records):
    if not part or re.match(r"^(CONSTRAINT|PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|KEY)\b", part, re.I):
        return
    m = re.match(r"([A-Za-z_]\w*)\s+([A-Za-z][\w()\s,]*?)(?=\s+(?:NOT|NULL|PRIMARY|UNIQUE|REFERENCES|DEFAULT|CHECK|GENERATED)\b|$)", part, re.I)
    if not m:
        return
    name, typ = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
    upper = part.upper()
    fk = ""
    fm = re.search(r"REFERENCES\s+([A-Za-z_][\w.$]*)\s*\(([^)]*)\)", part, re.I)
    if fm:
        fk = f"{fm.group(1)}({fm.group(2)})"
    records.append({"rec": "column", "file": "", "table": table, "name": name, "type": typ,
                    "pk": "PRIMARY KEY" in upper, "fk": fk, "nullable": "NOT NULL" not in upper,
                    "unique": "UNIQUE" in upper, "source": source, "line": line})


def _ddl(text: str, source: str, records):
    for m in CREATE_RX.finditer(text):
        table = m.group(1).split(".")[-1].lower()
        open_at = text.find("(", m.start())
        close_at = _matching_paren(text, open_at)
        line = text.count("\n", 0, m.start()) + 1
        records.append({"rec": "table", "file": "", "table": table, "source": source, "line": line})
        for part in _split_top(text[open_at + 1:close_at]):
            _column(part, table, line, source, records)
    for m in UNIQUE_RX.finditer(text):
        table = m.group(1).split(".")[-1].lower()
        line = text.count("\n", 0, m.start()) + 1
        for col in [c.strip() for c in m.group(2).split(",") if c.strip()]:
            records.append({"rec": "column", "file": "", "table": table, "name": col.strip('"` '),
                            "type": "", "pk": False, "fk": "", "nullable": True, "unique": True,
                            "source": source, "line": line})
    for m in ALTER_RX.finditer(text):
        table = m.group(1).split(".")[-1].lower()
        line = text.count("\n", 0, m.start()) + 1
        _column(f"{m.group(2)} {m.group(3)}", table, line, source, records)


def _queries(text: str, records):
    tables = set()
    try:
        import kb_code_graph as KCG  # noqa: PLC0415 — модуль движка лежит рядом
        found, _pairs = KCG.parse_sql([("sql", text)])
        tables = set(found)
    except Exception:  # noqa: BLE001 — старый движок без parse_sql
        for m in QUERY_RX.finditer(text):
            name = m.group(1).split(".")[-1].lower()
            if name not in NOT_TABLE and len(name) > 3:
                tables.add(name)
    for name in sorted(tables):
        records.append({"rec": "table", "file": "", "table": name, "source": "query", "line": 0})


def extract(path: str, text: str, role: str = "src") -> list:
    source = "migration" if "migration" in model.posix(path).lower() else "ddl"
    clean = _clean(text)
    records = []
    _ddl(clean, source, records)
    _queries(clean, records)
    for rec in records:
        rec["file"] = path
    return records
