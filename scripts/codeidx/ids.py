#!/usr/bin/env python3
"""ids.py — стабильные идентификаторы записей индекса.

Идентификатор не зависит от порядка обхода и машины: он считается из смысловых частей
(путь, имя, строка), а не из счётчика. Повторная сборка на той же ревизии даёт те же ID,
поэтому ссылки «критерий → символ» и кэш разбора переживают переиндексацию.
"""
from __future__ import annotations

import hashlib
import json


def _digest(*parts) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def record_id(kind: str, *parts) -> str:
    """ID записи: `<kind>:<20 hex>`. `kind` — короткий (`f`, `s`, `e`, `r`, `t`)."""
    return f"{kind}:{_digest(kind, *parts)[:20]}"


def file_id(path: str) -> str:
    return record_id("f", path.replace("\\", "/"))


def symbol_id(path: str, qname: str, line_start: int) -> str:
    """Символ: путь, полное имя и начало. Строка входит в ID: одинаковые имена в разных местах — разные символы."""
    return record_id("s", path.replace("\\", "/"), qname, int(line_start))


def edge_id(path: str, src: str, rel: str, dst: str, line: int) -> str:
    return record_id("e", path.replace("\\", "/"), src, rel, dst, int(line))


def route_id(path: str, method: str, route: str, line: int) -> str:
    return record_id("r", path.replace("\\", "/"), method.upper(), route, int(line))


def table_id(name: str, source: str = "") -> str:
    return record_id("t", name.lower(), source)


def canonical_json(value) -> str:
    """Одинаковый JSON для одинаковых данных: ключи по порядку, без пробелов, UTF-8 как есть."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(value) -> str:
    """Хэш произвольной записи: кэш разбора и проверка «ничего не изменилось»."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
