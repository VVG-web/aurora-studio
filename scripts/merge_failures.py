#!/usr/bin/env python3
"""merge_failures.py — merge `build_failures.json` по ключам источников.

`build_failures.json` — словарь «путь источника → запись о сбое разбора». Два
участника, у которых не разобрались разные источники, дописывают разные ключи;
построчный merge даёт ложный конфликт на общем файле.

Правила:
  • ключ только у одной стороны — берём его;
  • обе стороны меняли запись — более поздний `at`/`updated`/`processed`;
  • удаление при неизменной второй стороне сохраняется;
  • битый JSON — код 1, Git оставляет конфликт человеку.

  python3 .aurora/scripts/merge_failures.py %O %A %B %L %P
"""
from __future__ import annotations

import sys

try:  # защита версии Python
    import aurora_common  # noqa: F401
except ImportError:
    pass

from merge_common import read_json, write_json


def _newer(left, right) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ("at", "updated", "processed", "date", "mtime"):
        a, b = str(left.get(key) or ""), str(right.get(key) or "")
        if a != b:
            return a > b
    return False


def _merge(base: dict, ours: dict, theirs: dict) -> dict:
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


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        print("merge_failures: нужны base ours theirs", file=sys.stderr)
        return 1
    try:
        base = read_json(args[0], "build_failures")
        ours = read_json(args[1], "build_failures")
        theirs = read_json(args[2], "build_failures")
    except (OSError, ValueError) as exc:
        print(f"merge_failures: {exc}", file=sys.stderr)
        return 1
    write_json(args[1], _merge(base, ours, theirs), ".failures-merge-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
