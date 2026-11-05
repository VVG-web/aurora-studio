#!/usr/bin/env python3
"""stdlib_jvm.py — извлекатель Java, Kotlin, C# и Go без внешних парсеров.

Разбор построчный: пакет, импорты, классы и интерфейсы, методы, аннотации, поля сущностей,
маршруты Spring/Nest-подобных фреймворков, топики Kafka и ключи конфигурации. Границы тел
считаются по скобкам; вложенные лямбды и динамические вызовы не разбираются. Точность
средняя, но для T0 достаточно: всё, что не подтверждено, помечается `INFERRED`.
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

SRC = "stdlib-jvm"
CONTROL = {"if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "try", "synchronized"}
CLASS_RX = re.compile(
    r"^\s*(?:(?:public|private|protected|abstract|final|static|sealed|open|data|internal|partial)\s+)*"
    r"\b(class|interface|enum|record)\s+(\w+)")
JAVA_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp|async)\s+)+"
    r"([\w<>\[\],.?\s]+?)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws [^{;]+)?\s*(?:\{.*\}|;|\{)?\s*$")
JAVA_METHOD_ANY = re.compile(
    r"^\s*([\w<>\[\],.?]+)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws [^{;]+)?\s*(?:\{.*\}|;|\{)?\s*$")
CTOR_RX = re.compile(r"^\s*(?:public|private|protected)\s+(\w+)\s*\(([^)]*)\)\s*\{?\s*$")
KOTLIN_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline|operator)\s+)*"
    r"fun\s+(?:<[^>]+>\s+)?(\w+)\s*\(([^)]*)\)\s*(?::\s*([\w<>?.\[\]]+))?\s*\{?\s*$")
GO_METHOD = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(([^)]*)\)\s*([^{]*)\{?\s*$")
FIELD_RX = re.compile(
    r"^\s*(?:public|private|protected|internal|static|final|readonly|\s)*"
    r"([\w<>\[\],.?]+)\s+(\w+)\s*(?:=.*)?;\s*$")
CONST_RX = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|const|val|let)\s+)+"
    r"([\w<>\[\],.?]+)\s+(\w+)\s*=\s*(.+?);?\s*$")
VALUE_RX = re.compile(r"\$\{([A-Za-z0-9_.-]+)(?::([^}]*))?\}")
TABLE_RX = re.compile(r'@Table\s*\(([^)]*)\)')
COLUMN_RX = re.compile(r'@Column\s*\(([^)]*)\)')
BODY_ANN = re.compile(r"@(RequestBody|PathVariable|RequestParam)\b(?:\s+([\w<>\[\],.?]+))?")


def _snake(name: str) -> str:
    out = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return out.replace("__", "_")


def _class_at(classes, line: int):
    for cls in classes:
        if cls["start"] <= line <= cls["end"]:
            return cls
    return None


def _package(lines) -> str:
    for line in lines:
        m = re.match(r"^\s*package\s+([\w.]+)\s*;?", line)
        if m:
            return m.group(1)
    return ""


def _class_prefix(anns) -> str:
    for a in anns:
        if a.startswith("@RequestMapping") or a.startswith("@Path") or a.startswith("@Controller"):
            val = quoted(a)
            if val:
                return val
    return ""


def _route_ann(anns):
    """(метод, путь, коды) из аннотаций метода или None."""
    for a in anns:
        name = re.match(r"@(\w+)", a)
        if not name:
            continue
        n = name.group(1)
        mapping = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
                   "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}
        if n in mapping:
            return mapping[n], quoted(a), []
        if n == "RequestMapping":
            method = "GET"
            m = re.search(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)", a)
            if m:
                method = m.group(1)
            path = quoted(a)
            return method, path, []
    return None


def _status_codes(body: str, anns) -> list:
    codes = set()
    for name, code in (("badRequest", 400), ("notFound", 404), ("created", 201),
                       ("noContent", 204), ("ok", 200), ("conflict", 409)):
        if re.search(rf"\b{name}\s*\(", body):
            codes.add(code)
    for m in re.finditer(r"HttpStatus\.(\w+)", body + " ".join(anns)):
        from_status = {"OK": 200, "CREATED": 201, "NO_CONTENT": 204, "BAD_REQUEST": 400,
                       "UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404, "CONFLICT": 409}
        if m.group(1) in from_status:
            codes.add(from_status[m.group(1)])
    return sorted(codes)


def _table_name(anns, cls_name: str) -> tuple:
    for a in anns:
        if a.startswith("@Table"):
            m = TABLE_RX.search(a)
            if m:
                nm = quoted(m.group(1))
                if nm:
                    return nm, True
    if any(a.startswith("@Entity") for a in anns):
        return _snake(cls_name), True
    return "", False


def _column_name(anns, field: str) -> tuple:
    for a in anns:
        if a.startswith("@Column"):
            m = COLUMN_RX.search(a)
            if m:
                nm = quoted(m.group(1))
                if nm:
                    return nm, m.group(1)
    return _snake(field), ""


def _const_values(lines) -> dict:
    out = {}
    for line in lines:
        m = CONST_RX.match(line)
        if m:
            val = quoted(m.group(3))
            if val:
                out[m.group(2)] = val
    return out


def _calls(body: str):
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
        name = m.group(1)
        if name not in CONTROL and name not in ("if", "for", "while", "switch", "catch", "return", "new"):
            yield name, body.count("\n", 0, m.start()) + 1


def extract(path: str, text: str, role: str = "src", lang: str = "Java") -> list:
    lines = text.splitlines()
    package = _package(lines)
    records = []
    classes = []
    constants = _const_values(lines)
    imports = []

    def add(rec):
        records.append(rec)

    def sym(name, kind, parent, line, end, sig="", anns=None):
        qname = f"{parent}.{name}" if parent else (f"{package}.{name}" if package else name)
        rec = {"rec": "symbol", "file": path, "id": ids.symbol_id(path, qname, line),
               "kind": kind, "name": name, "qname": qname, "line_start": line, "line_end": end,
               "sig": sig, "doc": "", "visibility": "private" if name.startswith("_") else "public",
               "annotations": anns or [], "parent": parent,
               "frag_hash": model.text_hash("\n".join(lines[max(0, line - 1):min(len(lines), end)]))}
        add(rec)
        return rec

    # импорты
    for i, line in enumerate(lines, 1):
        m = re.match(r"^\s*import\s+([\w.*]+)\s*;?", line)
        if m:
            imports.append(m.group(1))

    # классы
    for i, line in enumerate(lines):
        if is_comment(line):
            continue
        m = CLASS_RX.match(line)
        if not m:
            continue
        kind, name = m.group(1), m.group(2)
        anns = annotations(lines, i)
        end = brace_end(lines, i)
        cls = sym(name, kind, "", i + 1, end, strip(line), anns)
        class_info = {"name": name, "qname": cls["qname"], "start": i + 1, "end": end, "anns": anns}
        classes.append(class_info)
        for base in re.findall(r"(?:extends|implements)\s+([\w<>.,]+)", line):
            add({"rec": "edge", "file": path, "id": ids.edge_id(path, cls["id"], "extends", base, i + 1),
                 "from": cls["id"], "to": base, "rel": "extends", "line": i + 1, "conf": "INFERRED"})
        table, is_entity = _table_name(anns, name)
        if is_entity:
            add({"rec": "table", "file": path, "table": table, "source": "entity", "line": i + 1})
            class_info["table"] = table

    # поля сущностей и DTO
    for cls in classes:
        table = cls.get("table", "")
        for i in range(cls["start"], min(cls["end"], len(lines))):
            line = lines[i]
            if is_comment(line) or "(" in line or CLASS_RX.match(line):
                continue
            fm = FIELD_RX.match(line)
            if not fm:
                continue
            typ, name = fm.group(1).strip(), fm.group(2)
            if typ in ("return", "new", "throw"):
                continue
            anns = annotations(lines, i)
            sym(name, "field", cls["qname"], i + 1, i + 1, strip(line), anns)
            if table:
                col_name, col_attrs = _column_name(anns, name)
                add({"rec": "column", "file": path, "table": table, "name": col_name, "type": typ,
                     "pk": bool(any(a.startswith("@Id") for a in anns)),
                     "fk": quoted(next((a for a in anns if a.startswith("@JoinColumn")), "")),
                     "nullable": "nullable = false" not in col_attrs,
                     "unique": "unique = true" in col_attrs,
                     "source": "entity", "line": i + 1})

    # методы
    for i, line in enumerate(lines):
        if is_comment(line):
            continue
        cls = _class_at(classes, i + 1)
        name = ret = _args = ""
        m = KOTLIN_METHOD.match(line) if lang == "Kotlin" or " fun " in line else None
        if m:
            name, _args, ret = m.group(1), m.group(2), (m.group(3) or "")
        else:
            m = GO_METHOD.match(line) if line.lstrip().startswith("func ") else None
            if m:
                name, _args, ret = m.group(1), m.group(2), m.group(3).strip()
            else:
                m = JAVA_METHOD.match(line)
                if m:
                    ret, name, _args = m.group(1).strip(), m.group(2), m.group(3)
                else:
                    m = CTOR_RX.match(line)
                    if m and cls and m.group(1) == cls["name"]:
                        name, _args, ret = m.group(1), m.group(2), ""
                    elif m is None:
                        m = JAVA_METHOD_ANY.match(line)
                        if m:
                            ret, name, _args = m.group(1).strip(), m.group(2), m.group(3)
        if not name or name in CONTROL or ret.strip() in CONTROL:
            continue
        anns = annotations(lines, i)
        end = brace_end(lines, i)
        if strip(line).endswith(";") and "{" not in line:
            end = i + 1
        parent = cls["qname"] if cls else ""
        rec = sym(name, "method" if parent else "function", parent, i + 1, end, strip(line), anns)
        body = "\n".join(lines[i:end])
        # маршруты Spring и подобных
        route = _route_ann(anns)
        if route:
            method, route_path, _ = route
            prefix = _class_prefix(cls["anns"]) if cls else ""
            full = join_path(prefix, route_path)
            req = BODY_ANN.search(body)
            add({"rec": "route", "file": path, "id": ids.route_id(path, method, full, i + 1),
                 "method": method, "path": full, "handler": rec["id"],
                 "request": req.group(2) if req and req.group(2) else "",
                 "response": ret, "auth": _auth(anns),
                 "status_codes": _status_codes(body, anns), "line": i + 1})
            add({"rec": "string", "file": path, "text": full[:model.MAX_LITERAL_CHARS],
                 "line": i + 1, "symbol": rec["id"]})
        # ключи конфигурации
        for m in VALUE_RX.finditer(body):
            add({"rec": "config", "file": path, "key": m.group(1), "default": m.group(2) or "",
                 "profile": "", "line": i + 1 + body.count("\n", 0, m.start()), "read_by": [rec["id"]]})
        # топики
        for m in re.finditer(r"(?:send|publish|emit)\s*\(\s*([A-Za-z_]\w*|\"[^\"]+\")", body):
            topic = m.group(1).strip('"')
            topic = constants.get(topic, topic)
            if topic and not topic.startswith("$"):
                add({"rec": "topic", "file": path, "name": topic, "role": "producer",
                     "symbol": rec["id"], "payload": "", "group": "", "line": i + 1})
        listener_text = "\n".join(anns) + "\n" + body
        for m in re.finditer(r"@KafkaListener\s*\(([^)]*)\)", listener_text):
            topic = quoted(m.group(1))
            if topic:
                add({"rec": "topic", "file": path, "name": topic, "role": "consumer",
                     "symbol": rec["id"], "payload": "", "group": "", "line": i + 1})
        # вызовы
        for called, _ln in _calls(body):
            add({"rec": "edge", "file": path, "id": ids.edge_id(path, rec["id"], "calls", called, i + 1),
                 "from": rec["id"], "to": called, "rel": "calls", "line": i + 1, "conf": "INFERRED"})
        # тесты
        if role == "test" or "/test/" in model.posix(path) or model.posix(path).startswith("test/"):
            if any(a.startswith("@Test") for a in anns):
                target = re.sub(r"(Test|Tests)$", "", cls["name"] if cls else "")
                add({"rec": "test", "file": path, "symbol": rec["id"], "target": target, "line": i + 1})

    # константы и строки
    for i, line in enumerate(lines, 1):
        if is_comment(line):
            continue
        m = CONST_RX.match(line)
        if not m:
            continue
        typ, name, value = m.group(1), m.group(2), m.group(3)
        rec = sym(name, "const", "", i, i, strip(line), [])
        if quoted(value):
            add({"rec": "string", "file": path, "text": quoted(value)[:model.MAX_LITERAL_CHARS],
                 "line": i, "symbol": rec["id"]})

    for imp in imports:
        add({"rec": "edge", "file": path, "id": ids.edge_id(path, ids.file_id(path), "imports", imp, 1),
             "from": ids.file_id(path), "to": imp, "rel": "imports", "line": 1, "conf": "EXTRACTED"})
    return records


def _auth(anns) -> str:
    for a in anns:
        if re.match(r"@(PreAuthorize|Secured|RolesAllowed|RequiresRoles)", a):
            return quoted(a) or a
    return ""
