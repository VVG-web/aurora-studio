#!/usr/bin/env python3
"""query.py — поиск по индексу кода без модели.

Ищем по смыслу и по имени: символы, маршруты, таблицы и колонки, топики, конфигурацию,
строковые константы, тесты, метки требований и коммиты. Ранжирование простое и объяснимое:
точное совпадение, затем начало, затем вхождение. Никаких «похожих» догадок: пустая выдача —
нормальный ответ, а не ошибка.
"""
from __future__ import annotations

import re

KINDS = ("symbol", "file", "route", "table", "column", "topic", "config", "string", "test", "reqtag", "commit")
TYPE_ORDER = {k: i for i, k in enumerate(KINDS)}


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _prefix(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _terms(query: str) -> list:
    return [t for t in re.split(r"\s+", (query or "").strip()) if t]


def _where(columns: list, terms: list) -> tuple:
    hay = " || ' ' || ".join(f"COALESCE({c}, '')" for c in columns)
    parts, params = [], []
    for term in terms:
        parts.append(f"LOWER({hay}) LIKE LOWER(?) ESCAPE '\\'")
        params.append(_like(term))
    return " AND ".join(parts), params


def _rank(column: str, term: str) -> tuple:
    return (f"CASE WHEN LOWER({column}) = LOWER(?) THEN 0 "
            f"WHEN LOWER({column}) LIKE LOWER(?) ESCAPE '\\' THEN 1 ELSE 2 END", [term, _prefix(term)])


def _result(kind: str, name: str, file: str, line, detail: str = "", rank: int = 2) -> dict:
    return {"type": kind, "name": name, "file": file or "", "line": int(line or 0),
            "detail": detail, "_rank": rank, "_type": TYPE_ORDER.get(kind, 99)}


def _query_one(store, kind: str, terms: list, limit: int) -> list:
    """Записи одного вида. SQL собирается из белого списка колонок, не из ввода."""
    conn = store.conn
    out = []
    if kind == "symbol":
        where, params = _where(["name", "qname", "doc", "sig", "annotations"], terms)
        rank, rp = _rank("name", terms[0])
        sql = (f"SELECT {rank} AS r, name, qname, file, line_start, kind, doc FROM symbols "
               f"WHERE {where} ORDER BY r, name LIMIT ?")
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("symbol", row["qname"] or row["name"], row["file"], row["line_start"],
                               row["kind"] + (": " + row["doc"][:80] if row["doc"] else ""), row["r"]))
    elif kind == "file":
        where, params = _where(["path", "lang", "role"], terms)
        rank, rp = _rank("path", terms[0])
        sql = (f"SELECT {rank} AS r, path, lang, role, skipped, lines FROM files "
               f"WHERE {where} ORDER BY r, path LIMIT ?")
        for row in conn.execute(sql, (*rp, *params, limit)):
            detail = " ".join(x for x in (row["lang"], row["role"], row["skipped"]) if x)
            out.append(_result("file", row["path"], row["path"], 0, detail, row["r"]))
    elif kind == "route":
        where, params = _where(["method", "path", "request", "response", "handler"], terms)
        rank, rp = _rank("path", terms[0])
        sql = f"SELECT {rank} AS r, method, path, file, line, request, response FROM routes WHERE {where} ORDER BY r, path LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("route", f"{row['method']} {row['path']}", row["file"], row["line"],
                               " ".join(x for x in (row["request"], row["response"]) if x), row["r"]))
    elif kind == "table":
        where, params = _where(["table_name", "source"], terms)
        rank, rp = _rank("table_name", terms[0])
        sql = f"SELECT {rank} AS r, table_name, source, file, line FROM db_tables WHERE {where} ORDER BY r, table_name LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("table", row["table_name"], row["file"], row["line"], row["source"], row["r"]))
    elif kind == "column":
        where, params = _where(["table_name", "name", "type", "fk"], terms)
        rank, rp = _rank("name", terms[0])
        sql = (f"SELECT {rank} AS r, table_name, name, type, file, line FROM db_columns "
               f"WHERE {where} ORDER BY r, table_name, name LIMIT ?")
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("column", row["name"], row["file"], row["line"],
                               f"{row['table_name']} {row['type']}".strip(), row["r"]))
    elif kind == "topic":
        where, params = _where(["name", "role", "payload", "group_name"], terms)
        rank, rp = _rank("name", terms[0])
        sql = f"SELECT {rank} AS r, name, role, file, line FROM topics WHERE {where} ORDER BY r, name LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("topic", row["name"], row["file"], row["line"], row["role"], row["r"]))
    elif kind == "config":
        where, params = _where(["key", "default_value", "profile"], terms)
        rank, rp = _rank("key", terms[0])
        sql = f"SELECT {rank} AS r, key, default_value, file, line FROM configs WHERE {where} ORDER BY r, key LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("config", row["key"], row["file"], row["line"], row["default_value"], row["r"]))
    elif kind == "string":
        where, params = _where(["text", "symbol"], terms)
        rank, rp = _rank("text", terms[0])
        sql = f"SELECT {rank} AS r, text, symbol, file, line FROM strings WHERE {where} ORDER BY r, text LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("string", row["text"], row["file"], row["line"], "", row["r"]))
    elif kind == "test":
        where, params = _where(["symbol", "target", "file"], terms)
        rank, rp = _rank("target", terms[0])
        sql = f"SELECT {rank} AS r, symbol, target, file, line FROM tests WHERE {where} ORDER BY r, target LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("test", row["target"] or row["symbol"], row["file"], row["line"], "тест", row["r"]))
    elif kind == "reqtag":
        where, params = _where(["key", "kind", "file"], terms)
        rank, rp = _rank("key", terms[0])
        sql = f"SELECT {rank} AS r, key, kind, file, line FROM reqtags WHERE {where} ORDER BY r, key LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("reqtag", row["key"], row["file"], row["line"], row["kind"], row["r"]))
    elif kind == "commit":
        where, params = _where(["subject", "sha"], terms)
        rank, rp = _rank("subject", terms[0])
        sql = f"SELECT {rank} AS r, sha, subject, ts FROM commits WHERE {where} ORDER BY r, ts DESC LIMIT ?"
        for row in conn.execute(sql, (*rp, *params, limit)):
            out.append(_result("commit", row["sha"][:12], "", 0, row["subject"], row["r"]))
    return out


def find(store, query: str, kind: str = "", limit: int = 50) -> list:
    """Найти записи по строке. `kind` — один вид или пусто (все)."""
    terms = _terms(query)
    if not terms or limit <= 0:
        return []
    kinds = [kind] if kind in KINDS else list(KINDS)
    out = []
    for k in kinds:
        out.extend(_query_one(store, k, terms, max(1, limit)))
    seen = set()
    uniq = []
    for rec in out:
        key = (rec["type"], rec["name"], rec["file"], rec["line"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(rec)
    uniq.sort(key=lambda r: (r["_rank"], r["_type"], r["name"].lower(), r["file"], r["line"]))
    for rec in uniq:
        rec.pop("_rank", None)
        rec.pop("_type", None)
    return uniq[:limit]
