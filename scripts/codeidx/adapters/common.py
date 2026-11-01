"""Общие приёмы построчных извлекателей: комментарии, аннотации, границы тел, пути."""
from __future__ import annotations

import re

STRING_RX = re.compile(r"""['"]([^'"]{1,300})['"]""")


def strip(line: str) -> str:
    return line.strip()


def is_comment(line: str) -> bool:
    s = strip(line)
    return not s or s.startswith(("//", "*", "/*", "#"))


def annotations(lines, idx: int) -> list:
    """Аннотации перед строкой `idx` (0-based), без комментариев."""
    out = []
    j = idx - 1
    while j >= 0 and j >= idx - 8:
        s = strip(lines[j])
        if s.startswith("@"):
            out.append(s)
        elif s and not s.startswith(("//", "*", "/*")):
            break
        j -= 1
    return list(reversed(out))


def brace_end(lines, start: int) -> int:
    """Строка (1-based), где заканчивается тело, открытое в `start` (0-based)."""
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', lines[i])
        for ch in line:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return i + 1
    return len(lines)


def join_path(prefix: str, path: str) -> str:
    """Склеить префикс класса и путь метода: `/api` + `/orders` → `/api/orders`."""
    return "/" + "/".join(x for x in (prefix.strip("/"), path.strip("/")) if x)


def quoted(text: str) -> str:
    m = STRING_RX.search(text or "")
    return m.group(1) if m else ""
