#!/usr/bin/env python3
"""stdlib_misc.py — конфигурация проекта: YAML, properties, JSON, TOML, Maven/Gradle.

Ключи и значения читаются без внешних библиотек: для индекса достаточно плоского списка
«ключ → значение → строка». Вложенность YAML и TOML разворачивается в точечные ключи.
"""
from __future__ import annotations

import json
import re

try:
    from .. import model
except ImportError:  # запуск файла напрямую
    import model  # type: ignore

SRC = "stdlib-misc"
KV_RX = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]\s*(.*?)\s*$")


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return re.sub(r"\s+#.*$", "", raw).strip()


def _yaml(lines):
    out = []
    stack = []
    for i, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#") or line.strip() in ("---", "..."):
            continue
        if line.lstrip().startswith("- "):
            continue
        m = re.match(r"^(\s*)([^:#]+):(?:\s*(.*))?$", line)
        if not m:
            continue
        indent = len(m.group(1))
        key = m.group(2).strip()
        val = _value(m.group(3) or "")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        path = [k for _i, k in stack] + [key]
        if val:
            out.append((".".join(path), val, i))
        else:
            stack.append((indent, key))
    return out


def _toml(lines):
    out = []
    section = ""
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", s)
        if m:
            section = m.group(1).strip(" .")
            continue
        m = KV_RX.match(line)
        if m:
            key = f"{section}.{m.group(1)}" if section else m.group(1)
            out.append((key, _value(m.group(2)), i))
    return out


def _properties(lines):
    out = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith(("#", "!")):
            continue
        m = KV_RX.match(line)
        if m:
            out.append((m.group(1), _value(m.group(2)), i))
    return out


def _flatten(value, prefix=""):
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.extend(_flatten(v, key))
    elif isinstance(value, list):
        for idx, v in enumerate(value):
            out.extend(_flatten(v, f"{prefix}[{idx}]"))
    else:
        out.append((prefix, "" if value is None else str(value)))
    return out


def _json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return [(k, v, 0) for k, v in _flatten(data) if k]


def extract(path: str, text: str, role: str = "src") -> list:
    lines = text.splitlines()
    low = model.posix(path).lower()
    pairs = []
    if low.endswith(".json"):
        pairs = _json(text)
    elif low.endswith((".toml", ".ini")):
        pairs = _toml(lines) if low.endswith(".toml") else _properties(lines)
    elif low.endswith((".properties", ".conf", ".cfg")):
        pairs = _properties(lines)
    elif low.endswith((".yml", ".yaml")):
        pairs = _yaml(lines)
    elif low.endswith(".xml"):
        for i, line in enumerate(lines, 1):
            m = re.search(r"<(groupId|artifactId|version)>([^<]+)</", line)
            if m:
                pairs.append((f"pom.{m.group(1)}", m.group(2).strip(), i))
    else:
        pairs = _properties(lines)
    out = []
    for key, value, line in pairs:
        if key and len(key) <= 200:
            out.append({"rec": "config", "file": path, "key": key, "default": value[:model.MAX_LITERAL_CHARS],
                        "profile": "", "line": line, "read_by": []})
    return out
