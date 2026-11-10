#!/usr/bin/env python3
"""stdlib_swift.py — Swift без внешнего парсера: типы, функции, поля, UI-точки.

Построчный разбор нужен для проектов, где модель и база описаны Swift-кодом. Он не
претендует на полноту компилятора: границы тел считаются по скобкам, а всё, что не
подтверждено, остаётся именем и ссылкой на строку.
"""
from __future__ import annotations

import re

try:
    from .. import ids, model
    from .common import annotations, brace_end, is_comment, strip
except ImportError:  # запуск файла напрямую
    import ids  # type: ignore
    import model  # type: ignore
    from common import annotations, brace_end, is_comment, strip  # type: ignore

SRC = "stdlib-swift"
MODS = r"(?:public|private|fileprivate|internal|open|final|indirect|nonisolated|static|class|mutating|nonmutating|override|required|convenience)"
TYPE_RX = re.compile(
    rf"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:{MODS}\s+)*"
    r"\b(class|struct|enum|protocol|actor|extension)\s+([A-Za-z_]\w*)")
FUNC_RX = re.compile(
    rf"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:{MODS}\s+)*"
    r"\bfunc\s+([A-Za-z_]\w*)(?:\s*<[^>]+>)?\s*\(([^)]*)\)\s*"
    r"(?:async\s*)?(?:throws\s*)?(?:->\s*[^{]+)?")
INIT_RX = re.compile(rf"^\s*(?:{MODS}\s+)*\b(init|deinit)\s*\(([^)]*)\)")
FIELD_RX = re.compile(
    rf"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:{MODS}\s+)*"
    r"\b(var|let)\s+([A-Za-z_]\w*)\s*:\s*([^={{\n]+)")
APP_STORAGE_RX = re.compile(r"@AppStorage\s*\(\s*\"([^\"]+)\"")
UI_RX = re.compile(r"\b(?:MenuBarExtra|WindowGroup|Window|Settings|NavigationStack|NSWindow|"
                   r"TextField|SecureField|Picker|Toggle|Button|Menu|List|Form|TabView)\b")


def _kind(kind: str, base: str, body: str = "") -> str:
    if kind in ("struct", "class") and re.search(r":\s*[^,{]*\bView\b", base):
        return "view"
    if kind == "struct" and re.search(r":\s*[^,{]*\bApp\b", base):
        return "app"
    if kind == "extension":
        return "extension"
    return kind


def extract(path: str, text: str, role: str = "src", lang: str = "Swift") -> list:
    lines = text.splitlines()
    records = []
    types = []

    def add(rec):
        records.append(rec)

    def sym(name, kind, parent, line, end, sig="", anns=None):
        qname = f"{parent}.{name}" if parent else name
        rec = {"rec": "symbol", "file": path, "id": ids.symbol_id(path, qname, line),
               "kind": kind, "name": name, "qname": qname, "line_start": line,
               "line_end": end, "sig": sig, "doc": "",
               "visibility": "private" if re.search(r"\bprivate\b", sig) else "public",
               "annotations": anns or [], "parent": parent,
               "frag_hash": model.text_hash("\n".join(lines[max(0, line - 1):min(len(lines), end)]))}
        add(rec)
        return rec

    # импорты — это зависимости модуля, а не отдельные символы.
    for line in lines:
        m = re.match(r"^\s*import\s+([\w.]+)", line)
        if m:
            add({"rec": "edge", "file": path, "id": ids.edge_id(path, "module", "import", m.group(1), 1),
                 "from": "module", "to": m.group(1), "rel": "import", "line": 1,
                 "conf": "EXTRACTED"})

    for i, line in enumerate(lines):
        if is_comment(line):
            continue
        anns = annotations(lines, i)
        m = TYPE_RX.match(line)
        if m:
            kind, name = _kind(m.group(1), line), m.group(2)
            end = brace_end(lines, i)
            rec = sym(name, kind, "", i + 1, end, strip(line), anns)
            types.append({"name": name, "qname": rec["qname"], "start": i + 1,
                          "end": end, "kind": kind})
            for base in re.findall(r"(?::|,)\s*([A-Za-z_]\w*)\s*(?:[,<{]|$)", line):
                if base not in ("where",):
                    add({"rec": "edge", "file": path,
                         "id": ids.edge_id(path, rec["id"], "conforms", base, i + 1),
                         "from": rec["id"], "to": base, "rel": "conforms",
                         "line": i + 1, "conf": "INFERRED"})
            continue
        # ближайший внешний тип: методы и поля принадлежат ему.
        parent = next((t["qname"] for t in reversed(types)
                       if t["start"] <= i + 1 <= t["end"]), "")
        m = FUNC_RX.match(line)
        if m:
            end = brace_end(lines, i)
            sig = strip(line)
            kind = "function" if not parent else "method"
            if role == "test" and m.group(1).startswith("test"):
                add({"rec": "test", "file": path, "symbol": m.group(1), "target": parent,
                     "line": i + 1})
            sym(m.group(1), kind, parent, i + 1, end, sig, anns)
            continue
        m = INIT_RX.match(line)
        if m and parent:
            sym(m.group(1), "initializer", parent, i + 1, brace_end(lines, i), strip(line), anns)
            continue
        m = FIELD_RX.match(line)
        if m and parent:
            kind = "state" if any(a.startswith("@State") or a.startswith("@Published")
                                  or a.startswith("@Binding") for a in anns) else "field"
            sym(m.group(2), kind, parent, i + 1, i + 1, strip(line), anns)
            continue
        for key in APP_STORAGE_RX.findall(line):
            add({"rec": "config", "file": path, "key": key, "default": "",
                 "profile": "", "line": i + 1, "read_by": []})
        if UI_RX.search(line):
            # UI-конструкции не имеют объявления символа; строка остаётся ссылкой
            # для экранного вида, а не выдуманным методом.
            add({"rec": "ui", "file": path, "name": UI_RX.search(line).group(0),
                 "line": i + 1, "symbol": parent or "", "role": role})
    return records
