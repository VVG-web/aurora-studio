#!/usr/bin/env python3
"""merge_manifest.py — трёхсторонний merge `AuroraKnowledgeDB/meta/manifest.json`.

Git вызывает этот драйвер при конфликте манифеста разбора. Манифест — не текст, а
учёт источников: два участника почти всегда меняют разные ключи `sources`, поэтому
обычный построчный merge даёт конфликт на каждой параллельной правке.

Правила:
  • `sources` — по ключам. Если сторона не меняла запись, побеждает изменение другой;
    если обе меняли — запись с более поздним `processed`; при равенстве — наша.
    Удаление при неизменной второй стороне сохраняется.
  • `cards` — объединение по каноническому JSON, детерминированная сортировка.
  • `updated` — максимум, `version` — максимум; остальные top-level поля — по тому же
    правилу «кто менял».
  • Битый JSON — код 1: Git оставляет конфликт, данные не теряются.

  python3 .aurora/scripts/merge_manifest.py %O %A %B %L %P
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

try:  # защита версии Python: на старом интерпретаторе — понятный текст, не traceback
    import aurora_common  # noqa: F401
except ImportError:
    pass


def _read(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    raw = open(path, encoding="utf-8").read()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: манифест не словарь")
    return data


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _newer(left, right) -> bool:
    """Левая запись новее правой по `processed`/`updated`/`mtime`; иначе False."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ("processed", "updated", "mtime", "date"):
        a, b = str(left.get(key) or ""), str(right.get(key) or "")
        if a != b:
            return a > b
    return False


def _merge_sources(base: dict, ours: dict, theirs: dict) -> dict:
    out = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        if o == t:
            value = o
        elif o == b:
            value = t
        elif t == b:
            value = o
        elif o is None:
            value = t
        elif t is None:
            value = o
        elif _newer(o, t):
            value = o
        else:
            value = t
        if value is not None:
            out[key] = value
    return out


def _merge_cards(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if not isinstance(ours, list) or not isinstance(theirs, list):
        return ours
    unique = {_canonical(item): item for item in [*ours, *theirs]}
    return [unique[key] for key in sorted(unique)]


def _merge_value(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        return _merge_dict(base if isinstance(base, dict) else {}, ours, theirs)
    return ours


def _merge_dict(base: dict, ours: dict, theirs: dict) -> dict:
    out = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        if key == "sources":
            out[key] = _merge_sources(base.get(key) or {}, ours.get(key) or {}, theirs.get(key) or {})
        elif key == "cards":
            out[key] = _merge_cards(base.get(key), ours.get(key), theirs.get(key))
        elif key == "version":
            values = [v for v in (base.get(key), ours.get(key), theirs.get(key)) if isinstance(v, int)]
            out[key] = max(values) if values else ours.get(key, theirs.get(key))
        elif key == "updated":
            values = [str(v) for v in (base.get(key), ours.get(key), theirs.get(key)) if v]
            out[key] = max(values) if values else ours.get(key, theirs.get(key))
        else:
            out[key] = _merge_value(base.get(key), ours.get(key), theirs.get(key))
    return out


def _write(path: str, data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".manifest-merge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        print("merge_manifest: нужны base ours theirs", file=sys.stderr)
        return 1
    base_path, ours_path, theirs_path = args[0], args[1], args[2]
    try:
        base = _read(base_path)
        ours = _read(ours_path)
        theirs = _read(theirs_path)
    except (OSError, ValueError) as exc:
        print(f"merge_manifest: {exc}", file=sys.stderr)
        return 1
    merged = _merge_dict(base, ours, theirs)
    _write(ours_path, merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
