#!/usr/bin/env python3
"""measure.py — замер stdlib против необязательных адаптеров на одном или нескольких файлах.

  python3 scripts/codeidx/measure.py --file path [--file path ...] --repeat 3 [--json]

Меряет время и число записей. Без команд `AURORA_CODE_*` второй столбец равен первому:
адаптеры не установлены. Замер не доказывает правильность разбора, только цену вызова.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codeidx import extract, model  # noqa: E402
from codeidx.adapters import optional  # noqa: E402

ENV = ("AURORA_CODE_GRAPHIFY", "AURORA_CODE_TREESITTER", "AURORA_CODE_ASTGREP", "AURORA_CODE_LIZARD")


def _run(path: str, text: str, lang: str, repeat: int) -> tuple:
    best = None
    rows = []
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        rows = extract.extract_file(path, text, lang, "src")
        spent = (time.perf_counter() - started) * 1000.0
        best = spent if best is None else min(best, spent)
    return round(best or 0.0, 2), len(rows)


def _without_optional(fn):
    saved = {k: os.environ.pop(k, None) for k in ENV}
    try:
        return fn()
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер stdlib и необязательных адаптеров")
    ap.add_argument("--file", action="append", required=True, help="файл для замера (можно несколько)")
    ap.add_argument("--repeat", type=int, default=3, help="повторов, берётся лучшее время")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args()
    out = []
    for name in a.file:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"measure: {name}: {exc}", file=sys.stderr)
            return 1
        lang = model.language_of(str(path))
        stdlib_ms, stdlib_n = _without_optional(
            lambda path=path, text=text, lang=lang: _run(str(path), text, lang, a.repeat))
        adapter_ms, adapter_n = _run(str(path), text, lang, a.repeat)
        out.append({"file": str(path), "lang": lang, "repeat": max(1, a.repeat),
                    "stdlib_ms": stdlib_ms, "stdlib_records": stdlib_n,
                    "adapter_ms": adapter_ms, "adapter_records": adapter_n,
                    "enabled": optional.enabled()})
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print("| файл | язык | stdlib, мс | записей | адаптеры, мс | записей | слоты |")
    print("|---|---|---:|---:|---:|---:|---|")
    for row in out:
        print(f"| {row['file']} | {row['lang'] or '—'} | {row['stdlib_ms']} | "
              f"{row['stdlib_records']} | {row['adapter_ms']} | {row['adapter_records']} | "
              f"{', '.join(row['enabled']) or 'нет'} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
