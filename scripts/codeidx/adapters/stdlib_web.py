#!/usr/bin/env python3
"""stdlib_web.py — TypeScript/JavaScript: классы, интерфейсы, маршруты и поля форм.

Поддержаны NestJS-декораторы, Express/Fastify, FastAPI-подобные декораторы, Flask-route и
комментарии вида `// GET /path`. Это T0: границы методов по скобкам, вызовы по именам,
без вывода типов. Всё неподтверждённое помечается `INFERRED`.
"""
from __future__ import annotations

import re

try:
    from .. import ids, model
    from .common import annotations, brace_end, is_comment, join_path, quoted, strip
except ImportError:  # запуск файла напрямую
    import ids  # type: ignore
    import model  # type: ignore
    from common import annotations, brace_end, is_comment, join_path, quoted, strip  # type: ignore

SRC = "stdlib-web"
CLASS_RX = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(class|interface)\s+(\w+)")
FUNC_RX = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")
ARROW_RX = re.compile(
    r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*(?::\s*[^=]+)?=>")
METHOD_RX = re.compile(
    r"^\s*(?:(?:public|private|protected|static|async|readonly|override)\s+)*"
    r"(\w+)\s*\(([^)]*)\)\s*(?::\s*([\w<>\[\]|, .]+))?\s*\{?\s*$")
FIELD_RX = re.compile(r"^\s*(\w+)\??\s*:\s*([\w<>\[\]|, .]+)\s*;")
DECOR_RX = re.compile(r"^\s*@(\w+)(?:\(([^)]*)\))?\s*$")
ROUTE_CALL_RX = re.compile(
    r"\b(?:app|router|server|fastify|api)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)")
COMMENT_ROUTE_RX = re.compile(r"^\s*//\s*(GET|POST|PUT|DELETE|PATCH)\s+(/\S+)", re.I)
CONTROL = {"if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "try", "constructor"}


def _class_prefix(anns) -> str:
    for a in anns:
        if a.startswith("@Controller") or a.startswith("@Path"):
            val = quoted(a)
            if val:
                return val
    return ""


def _route_ann(anns):
    mapping = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH"}
    for a in anns:
        m = re.match(r"@(\w+)", a)
        if not m:
            continue
        if m.group(1) in mapping:
            return mapping[m.group(1)], quoted(a)
        if m.group(1) == "route":
            method = "GET"
            mm = re.search(r"methods\s*=\s*\[([^\]]*)\]", a)
            if mm:
                first = quoted(mm.group(1))
                if first:
                    method = first.upper()
            return method, quoted(a)
    return None


def extract(path: str, text: str, role: str = "src", lang: str = "TypeScript") -> list:
    lines = text.splitlines()
    records = []
    classes = []
    pending_comment = None

    def add(rec):
        records.append(rec)

    def sym(name, kind, parent, line, end, sig="", anns=None):
        qname = f"{parent}.{name}" if parent else name
        rec = {"rec": "symbol", "file": path, "id": ids.symbol_id(path, qname, line),
               "kind": kind, "name": name, "qname": qname, "line_start": line, "line_end": end,
               "sig": sig, "doc": "", "visibility": "private" if name.startswith("_") else "public",
               "annotations": anns or [], "parent": parent,
               "frag_hash": model.text_hash("\n".join(lines[max(0, line - 1):min(len(lines), end)]))}
        add(rec)
        return rec

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        cm = COMMENT_ROUTE_RX.match(line)
        if cm:
            pending_comment = {"rec": "route", "file": path,
                               "id": ids.route_id(path, cm.group(1).upper(), cm.group(2), i + 1),
                               "method": cm.group(1).upper(), "path": cm.group(2), "handler": "",
                               "request": "", "response": "", "auth": "", "status_codes": [], "line": i + 1}
            add(pending_comment)
            continue
        m = CLASS_RX.match(line)
        if m:
            kind, name = m.group(1), m.group(2)
            anns = annotations(lines, i)
            end = brace_end(lines, i)
            cls = sym(name, kind, "", i + 1, end, strip(line), anns)
            classes.append({"name": name, "qname": cls["qname"], "start": i + 1, "end": end, "anns": anns})
            continue
        m = FUNC_RX.match(line) or ARROW_RX.match(line)
        if m:
            name = m.group(1)
            anns = annotations(lines, i)
            end = brace_end(lines, i)
            rec = sym(name, "function", "", i + 1, end, strip(line), anns)
            _maybe_route(records, rec, anns, "", i + 1, path, pending_comment)
            pending_comment = None
            continue
        # поля интерфейсов и классов
        fm = FIELD_RX.match(line)
        if fm:
            parent = next((c["qname"] for c in classes if c["start"] <= i + 1 <= c["end"]), "")
            sym(fm.group(1), "field", parent, i + 1, i + 1, strip(line), [])
            continue
        if is_comment(line):
            continue
        # методы классов
        cls = next((c for c in classes if c["start"] <= i + 1 <= c["end"]), None)
        if not cls:
            continue
        m = METHOD_RX.match(line)
        if not m or m.group(1) in CONTROL:
            continue
        name, args, ret = m.group(1), m.group(2), (m.group(3) or "").strip()
        anns = annotations(lines, i)
        end = brace_end(lines, i)
        rec = sym(name, "method", cls["qname"], i + 1, end, strip(line), anns)
        _maybe_route(records, rec, anns, _class_prefix(cls["anns"]), i + 1, path, pending_comment)
        pending_comment = None
        body = "\n".join(lines[i:end])
        for cm in ROUTE_CALL_RX.finditer(body):
            add({"rec": "route", "file": path, "id": ids.route_id(path, cm.group(1).upper(), cm.group(2), i + 1),
                 "method": cm.group(1).upper(), "path": cm.group(2), "handler": rec["id"],
                 "request": args, "response": ret, "auth": "", "status_codes": [], "line": i + 1})
        if role == "test" or "/tests/" in model.posix(path) or model.posix(path).startswith("tests/"):
            add({"rec": "test", "file": path, "symbol": rec["id"], "target": "", "line": i + 1})

    # вызовы серверных маршрутов вне методов
    known_routes = {r["id"] for r in records if r.get("rec") == "route"}
    for i, line in enumerate(lines, 1):
        for cm in ROUTE_CALL_RX.finditer(line):
            rid = ids.route_id(path, cm.group(1).upper(), cm.group(2), i)
            if rid not in known_routes:
                known_routes.add(rid)
                add({"rec": "route", "file": path, "id": rid,
                     "method": cm.group(1).upper(), "path": cm.group(2), "handler": "",
                     "request": "", "response": "", "auth": "", "status_codes": [], "line": i})
        im = re.match(r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]", line)
        if im:
            add({"rec": "edge", "file": path, "id": ids.edge_id(path, ids.file_id(path), "imports", im.group(1), i),
                 "from": ids.file_id(path), "to": im.group(1), "rel": "imports", "line": i, "conf": "EXTRACTED"})
        tm = re.match(r"^\s*(?:describe|it|test)\s*\(\s*['\"]([^'\"]+)", line)
        if tm:
            add({"rec": "test", "file": path, "symbol": "", "target": tm.group(1), "line": i})
    return records


def _maybe_route(records, rec, anns, prefix, line, path, pending):
    route = _route_ann(anns)
    if route:
        method, route_path = route
        full = join_path(prefix, route_path)
        records.append({"rec": "route", "file": path, "id": ids.route_id(path, method, full, line),
                        "method": method, "path": full, "handler": rec["id"], "request": "",
                        "response": "", "auth": "", "status_codes": [], "line": line})
    elif pending is not None and not pending.get("handler"):
        pending["handler"] = rec["id"]
