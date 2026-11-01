#!/usr/bin/env python3
"""stdlib_py.py — извлекатель Python на стандартном `ast`.

Точные диапазоны строк и настоящие имена: Python единственный язык, где T0 не гадает.
Классы, функции, методы, поля классов, константы, импорты, простые вызовы, декораторы-
маршруты и тестовые функции. Вложенные лямбды и динамические атрибуты не извлекаются.
"""
from __future__ import annotations

import ast
import re

try:
    from .. import ids, model
except ImportError:  # запуск файла напрямую
    import ids  # type: ignore
    import model  # type: ignore

SRC = "stdlib-py"
ROUTE_DECORATORS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
STATUS_NAMES = {"OK": 200, "CREATED": 201, "NO_CONTENT": 204, "BAD_REQUEST": 400,
                "UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404, "CONFLICT": 409}


def _module_name(path: str) -> str:
    rel = model.posix(path)
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = [p for p in rel.split("/") if p]
    if parts and parts[0] in ("src", "app", "lib"):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _doc(node) -> str:
    try:
        return (ast.get_docstring(node) or "")[:model.MAX_DOC_CHARS]
    except (TypeError, ValueError):
        return ""


def _decorators(node) -> list:
    out = []
    for d in getattr(node, "decorator_list", []) or []:
        if isinstance(d, ast.Call):
            d = d.func
        if isinstance(d, ast.Name):
            out.append(d.id)
        elif isinstance(d, ast.Attribute):
            out.append(d.attr)
    return out


def _annotation(node) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation(node.value)
    if isinstance(node, ast.Constant):
        return str(node.value)
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — старый Python без unparse
        return ""


def _sig(node) -> str:
    try:
        return ast.unparse(node.args)
    except Exception:  # noqa: BLE001
        return ""


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") and not name.startswith("__") else "public"


def _source(text: str, node) -> str:
    try:
        return ast.get_source_segment(text, node) or ""
    except Exception:  # noqa: BLE001
        return ""


def _string_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_from_decorator(d, text: str):
    """(метод, путь, коды) из декоратора или None."""
    if not isinstance(d, ast.Call):
        return None
    name = d.func.attr if isinstance(d.func, ast.Attribute) else getattr(d.func, "id", "")
    if name not in ROUTE_DECORATORS and name != "route":
        return None
    path = ""
    methods = []
    for i, arg in enumerate(d.args):
        val = _string_value(arg)
        if val is not None and i == 0:
            path = val
    for kw in d.keywords:
        if kw.arg == "methods":
            vals = getattr(kw.value, "elts", [])
            methods = [_string_value(v) or "" for v in vals]
        if kw.arg == "path" and isinstance(kw.value, ast.Constant):
            path = str(kw.value.value)
    method = ROUTE_DECORATORS.get(name) or (methods[0].upper() if methods else "GET")
    return method, path, []


def _status_codes(source: str) -> list:
    codes = set()
    for m in re.finditer(r"status_code\s*=\s*(\d{3})", source):
        codes.add(int(m.group(1)))
    for m in re.finditer(r"HTTPStatus\.([A-Z_]+)", source):
        if m.group(1) in STATUS_NAMES:
            codes.add(STATUS_NAMES[m.group(1)])
    for m in re.finditer(r"\babort\((\d{3})", source):
        codes.add(int(m.group(1)))
    return sorted(codes)


def _symbol(path: str, module: str, node, kind: str, parent: str, text: str, name: str = "") -> dict:
    nm = name or getattr(node, "name", "")
    qname = f"{parent}.{nm}" if parent else (f"{module}.{nm}" if module else nm)
    start = int(getattr(node, "lineno", 0) or 0)
    end = int(getattr(node, "end_lineno", start) or start)
    return {
        "rec": "symbol", "file": path, "id": ids.symbol_id(path, qname, start), "kind": kind,
        "name": nm, "qname": qname, "line_start": start, "line_end": end,
        "sig": _sig(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "",
        "doc": _doc(node), "visibility": _visibility(nm),
        "annotations": _decorators(node), "parent": parent,
        "frag_hash": model.text_hash(_source(text, node)),
    }


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, text: str):
        self.path = path
        self.text = text
        self.module = _module_name(path)
        self.class_stack = []
        self.func_stack = []
        self.records = []
        self.seen_edges = set()

    # ------------------------------------------------------------- общие

    def _add(self, rec):
        self.records.append(rec)

    def _edge(self, src_id: str, dst: str, rel: str, line: int, conf: str = "EXTRACTED"):
        if not dst:
            return
        key = (src_id, rel, dst, int(line))
        if key in self.seen_edges:
            return
        self.seen_edges.add(key)
        self._add({"rec": "edge", "file": self.path, "id": ids.edge_id(self.path, src_id, rel, dst, line),
                   "from": src_id, "to": dst, "rel": rel, "line": int(line), "conf": conf})

    # ------------------------------------------------------------- классы и функции

    def visit_ClassDef(self, node):
        parent = self.class_stack[-1] if self.class_stack else ""
        rec = _symbol(self.path, self.module, node, "class", parent, self.text)
        self._add(rec)
        for base in node.bases:
            self._edge(rec["id"], _annotation(base), "extends", node.lineno, "INFERRED")
        self.class_stack.append(rec["qname"])
        for stmt in node.body:
            if isinstance(stmt, (ast.AnnAssign, ast.Assign)):
                self._class_field(rec, stmt)
        self.generic_visit(node)
        self.class_stack.pop()

    def _class_field(self, class_rec: dict, stmt):
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name, line = stmt.target.id, stmt.lineno
            field_rec = _symbol(self.path, self.module, stmt, "field", class_rec["qname"], self.text, name)
            self._add(field_rec)
            self._table_field(class_rec, name, line, _annotation(stmt.annotation), stmt)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if not isinstance(target, ast.Name):
                    continue
                val = _string_value(stmt.value)
                kind = "const" if target.id.isupper() else "field"
                field_rec = _symbol(self.path, self.module, stmt, kind, class_rec["qname"], self.text, target.id)
                self._add(field_rec)
                if val is not None:
                    self._add({"rec": "string", "file": self.path, "text": val[:model.MAX_LITERAL_CHARS],
                               "line": stmt.lineno, "symbol": field_rec["id"]})
                    if target.id == "__tablename__":
                        self._table(class_rec, val, stmt.lineno)

    def _table(self, class_rec: dict, name: str, line: int):
        self._add({"rec": "table", "file": self.path, "table": name, "source": "entity", "line": line})

    def _table_field(self, class_rec: dict, name: str, line: int, typ: str, stmt):
        table = self._table_of(class_rec)
        if not table:
            return
        self._add({"rec": "column", "file": self.path, "table": table, "name": name, "type": typ,
                   "pk": name == "id", "nullable": True, "unique": False, "source": "entity", "line": line})

    def _table_of(self, class_rec: dict):
        for rec in self.records:
            if rec.get("rec") == "table" and rec.get("file") == self.path:
                line = int(rec.get("line") or 0)
                start = int(class_rec.get("line_start") or 0)
                end = int(class_rec.get("line_end") or start)
                if start <= line <= end:
                    return rec.get("table", "")
        return ""

    def visit_FunctionDef(self, node):
        self._function(node)

    def visit_AsyncFunctionDef(self, node):
        self._function(node)

    def _function(self, node):
        parent = self.class_stack[-1] if self.class_stack else ""
        kind = "method" if self.class_stack else "function"
        rec = _symbol(self.path, self.module, node, kind, parent, self.text)
        self._add(rec)
        source = _source(self.text, node)
        for d in node.decorator_list:
            route = _route_from_decorator(d, self.text)
            if route:
                method, path, _codes = route
                self._add({
                    "rec": "route", "file": self.path,
                    "id": ids.route_id(self.path, method, path, node.lineno),
                    "method": method, "path": path or "/", "handler": rec["id"],
                    "request": _annotation(node.args.args[0].annotation) if node.args.args else "",
                    "response": _annotation(node.returns),
                    "auth": ",".join(a for a in _decorators(node) if re.search(r"auth|role|permission|login", a, re.I)),
                    "status_codes": _status_codes(source), "line": node.lineno,
                })
                self._add({"rec": "string", "file": self.path, "text": path[:model.MAX_LITERAL_CHARS],
                           "line": node.lineno, "symbol": rec["id"]})
        if kind == "method":
            self._edge(rec["id"], parent, "uses_type", node.lineno, "INFERRED")
        if node.name.startswith("test_") or "/tests/" in model.posix(self.path) or model.posix(self.path).startswith("tests/"):
            target = ""
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    target = _annotation(child.func)
                    if target:
                        break
            self._add({"rec": "test", "file": self.path, "symbol": rec["id"], "target": target,
                       "line": node.lineno})
        self.func_stack.append(rec["id"])
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = _annotation(child.func)
                if name:
                    self._edge(rec["id"], name, "calls", getattr(child, "lineno", node.lineno), "INFERRED")
        self.func_stack.pop()
        self.generic_visit(node)

    # ------------------------------------------------------------- импорты и константы

    def visit_Import(self, node):
        for alias in node.names:
            self._edge(ids.file_id(self.path), alias.name, "imports", node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            self._edge(ids.file_id(self.path), node.module, "imports", node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node):
        if not self.func_stack and not self.class_stack:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    rec = _symbol(self.path, self.module, node, "const", "", self.text, target.id)
                    self._add(rec)
                    val = _string_value(node.value)
                    if val is not None:
                        self._add({"rec": "string", "file": self.path, "text": val[:model.MAX_LITERAL_CHARS],
                                   "line": node.lineno, "symbol": rec["id"]})
        self.generic_visit(node)


def extract(path: str, text: str, role: str = "src") -> list:
    tree = ast.parse(text)
    visitor = _Visitor(path, text)
    visitor.visit(tree)
    return visitor.records
