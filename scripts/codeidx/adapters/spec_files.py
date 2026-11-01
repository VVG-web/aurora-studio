#!/usr/bin/env python3
"""spec_files.py — OpenAPI, protobuf и GraphQL: контракты, которые уже описаны текстом.

Это не замена разбору кода: спецификация говорит, что должно быть, а индекс кода — что есть.
Поэтому записи помечаются `conf=EXTRACTED` и используются как якоря для сверки.
"""
from __future__ import annotations

import json
import re

SRC = "stdlib-spec"
PROTO_MSG = re.compile(r"\bmessage\s+(\w+)\s*\{")
PROTO_FIELD = re.compile(r"^\s*(?:repeated\s+)?([\w.]+)\s+(\w+)\s*=\s*\d+")
PROTO_RPC = re.compile(r"\brpc\s+(\w+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)\s*\)")
GQL_TYPE = re.compile(r"^\s*(?:type|input|enum)\s+(\w+)\s*\{")
GQL_FIELD = re.compile(r"^\s*(\w+)\s*:\s*([\[\]!\w]+)")


def _json(path, text):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for route, item in (data.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in ("get", "post", "put", "delete", "patch"):
            if method not in item:
                continue
            out.append({"rec": "route", "file": path, "method": method.upper(), "path": route,
                        "handler": item[method].get("operationId", ""),
                        "request": "", "response": "", "auth": "", "status_codes": [], "line": 0})
    for name, schema in (data.get("components", {}).get("schemas") or {}).items():
        out.append({"rec": "symbol", "file": path, "kind": "class", "name": name, "qname": name,
                    "line_start": 0, "line_end": 0, "sig": "", "doc": "", "visibility": "public",
                    "annotations": [], "parent": "", "frag_hash": ""})
        for field, spec in (schema.get("properties") or {}).items():
            out.append({"rec": "symbol", "file": path, "kind": "field", "name": field,
                        "qname": f"{name}.{field}", "line_start": 0, "line_end": 0, "sig": "",
                        "doc": "", "visibility": "public", "annotations": [], "parent": name,
                        "frag_hash": ""})
    return out


def _proto(path, text):
    out = []
    for m in PROTO_MSG.finditer(text):
        name = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        out.append({"rec": "symbol", "file": path, "kind": "class", "name": name, "qname": name,
                    "line_start": line, "line_end": line, "sig": "", "doc": "", "visibility": "public",
                    "annotations": [], "parent": "", "frag_hash": ""})
    for m in PROTO_FIELD.finditer(text):
        typ, name = m.group(1), m.group(2)
        line = text.count("\n", 0, m.start()) + 1
        out.append({"rec": "symbol", "file": path, "kind": "field", "name": name, "qname": name,
                    "line_start": line, "line_end": line, "sig": typ, "doc": "", "visibility": "public",
                    "annotations": [], "parent": "", "frag_hash": ""})
    for m in PROTO_RPC.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        out.append({"rec": "route", "file": path, "method": "RPC", "path": m.group(1),
                    "handler": m.group(2), "request": m.group(2), "response": m.group(3),
                    "auth": "", "status_codes": [], "line": line})
    return out


def _graphql(path, text):
    out = []
    for m in GQL_TYPE.finditer(text):
        name = m.group(1)
        line = text.count("\n", 0, m.start()) + 1
        out.append({"rec": "symbol", "file": path, "kind": "class", "name": name, "qname": name,
                    "line_start": line, "line_end": line, "sig": "", "doc": "", "visibility": "public",
                    "annotations": [], "parent": "", "frag_hash": ""})
    for m in GQL_FIELD.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        out.append({"rec": "symbol", "file": path, "kind": "field", "name": m.group(1),
                    "qname": m.group(1), "line_start": line, "line_end": line, "sig": m.group(2),
                    "doc": "", "visibility": "public", "annotations": [], "parent": "",
                    "frag_hash": ""})
    return out


def extract(path: str, text: str, role: str = "src") -> list:
    low = path.lower()
    if low.endswith(".json"):
        return _json(path, text)
    if low.endswith(".proto"):
        return _proto(path, text)
    return _graphql(path, text)
