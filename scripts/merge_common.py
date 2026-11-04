#!/usr/bin/env python3
"""merge_common.py — общие чтение и атомарная запись для git merge-драйверов."""
from __future__ import annotations

import json
import os
import tempfile

try:  # защита версии Python
    import aurora_common  # noqa: F401
except ImportError:
    pass


def read_json(path: str, label: str = "merge") -> dict:
    """JSON-объект из файла; отсутствие/пустой файл — {}; не объект — ValueError."""
    if not path or not os.path.isfile(path):
        return {}
    raw = open(path, encoding="utf-8").read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: {label} не словарь")
    return data


def write_json(path: str, data: dict, prefix: str = ".merge-") -> None:
    """Записать JSON атомарно: временный файл рядом и подмена."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
