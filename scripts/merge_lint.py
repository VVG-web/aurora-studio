#!/usr/bin/env python3
"""merge_lint.py — доменный merge храповика линтера.

Два общих файла дают конфликт, когда участники чистят разные замечания:

  • `AuroraKnowledgeDB/meta/lint_baseline.txt` — число ошибок, ниже которого база
    уже не опускалась. Храповик разрешает только снижать планку, поэтому merge
    берёт минимум: более строгая база побеждает.
  • `AuroraKnowledgeDB/meta/lint_residue.json` — остаток после «Починить базу».
    Merge объединяет списки, дедуплицирует и сортирует: чужие исправления не
    теряются.

  python3 .aurora/scripts/merge_lint.py baseline %O %A %B %L %P
  python3 .aurora/scripts/merge_lint.py residue  %O %A %B %L %P
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


def _read_text(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    return open(path, encoding="utf-8").read()


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".lint-merge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _baseline(args) -> int:
    base, ours, theirs = args[0], args[1], args[2]
    values = []
    for path in (base, ours, theirs):
        raw = _read_text(path).strip()
        if not raw:
            continue
        try:
            values.append(int(raw))
        except ValueError:
            print(f"merge_lint: baseline не число: {path}", file=sys.stderr)
            return 1
    _atomic_write(ours, f"{min(values) if values else 0}\n")
    return 0


def _residue(args) -> int:
    base, ours, theirs = args[0], args[1], args[2]
    items = set()
    for path in (base, ours, theirs):
        raw = _read_text(path).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"merge_lint: residue не JSON: {path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, list):
            print(f"merge_lint: residue не список: {path}", file=sys.stderr)
            return 1
        items.update(str(item) for item in data)
    _atomic_write(ours, json.dumps(sorted(items), ensure_ascii=False, indent=1) + "\n")
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4:
        print("merge_lint: нужны режим base ours theirs", file=sys.stderr)
        return 1
    mode, rest = args[0], args[1:]
    if mode == "baseline":
        return _baseline(rest)
    if mode == "residue":
        return _residue(rest)
    print(f"merge_lint: неизвестный режим {mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
