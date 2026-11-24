#!/usr/bin/env python3
"""check_portability.py — правила, которые ловят платформенные грабли до CI.

Проверка смотрит только Python и работает по изменённым строкам: старые
исключения не мешают, а новая строка с известным дефектом валит preflight.

  python3 scripts/check_portability.py --changed origin/master
  python3 scripts/check_portability.py --staged
  python3 scripts/check_portability.py --all

Правила:
  relpath-start  `os.path.relpath(path, start)` без realpath/abspath у start:
                 на Windows относительный start и абсолютный path дают
                 `../../runneradmin/...` вместо пути внутри проекта.
  plan-newline   `write_text_atomic(plan..., ...)` без `newline=""`: текстовый
                 режим превращает LF в CRLF, а хеш плана считается по LF.
  tight-clock    `abs(..._ms...) <= N` при N <= 1000: wall-clock тест на медленном
                 раннере добавляет секунду и падает без дефекта в коде.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import aurora_common as AC  # noqa: F401 — точка входа движка: защита версии Python

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = ("scripts", "cockpit", "tests")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_abs_start(node: ast.AST) -> bool:
    """start уже приведён к абсолютному пути или разрешён."""
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if name in {"realpath", "abspath"}:
        return True
    return name == "resolve"


def _has_newline(node: ast.Call) -> bool:
    return any(kw.arg == "newline" for kw in node.keywords)


def check_tree(tree: ast.AST, path: Path) -> list[dict]:
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "relpath" and len(node.args) >= 2 and not _is_abs_start(node.args[1]):
            out.append({"path": str(path), "line": node.lineno, "rule": "relpath-start",
                        "message": "start у relpath не realpath/abspath: на Windows путь уедет за проект"})
        if name == "write_text_atomic" and node.args:
            first = node.args[0]
            label = first.id if isinstance(first, ast.Name) else (
                first.attr if isinstance(first, ast.Attribute) else "")
            if "plan" in label.casefold() and not _has_newline(node):
                out.append({"path": str(path), "line": node.lineno, "rule": "plan-newline",
                            "message": "план пишется без newline=\"\": CRLF сломает хеш плана"})
    return out


def check_compare(node: ast.Compare, path: Path, source: str) -> dict | None:
    """`abs(<...>_ms...) <= N` с маленьким N: хрупкий wall-clock тест."""
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.LtE):
        return None
    left = node.left
    if not (isinstance(left, ast.Call) and _call_name(left) == "abs"):
        return None
    segment = ast.get_source_segment(source, left) or ""
    if "_ms" not in segment and "time" not in segment.casefold():
        return None
    right = node.comparators[0]
    if isinstance(right, ast.Constant) and isinstance(right.value, int) and right.value <= 1000:
        return {"path": str(path), "line": node.lineno, "rule": "tight-clock",
                "message": "wall-clock допуск <= 1000 мс: на медленном раннере тест упадёт без дефекта"}
    return None


def check_source(path: Path) -> list[dict]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return []
    out = check_tree(tree, path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            finding = check_compare(node, path, source)
            if finding:
                out.append(finding)
    return out


def _python_files(paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        base = ROOT / raw
        if base.is_file() and base.suffix == ".py":
            files.append(base)
        elif base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def _changed_ranges(base: str, staged: bool) -> dict[str, set[int]]:
    cmd = ["git", "-C", str(ROOT), "diff", "--unified=0", "--no-color"]
    if staged:
        cmd.append("--cached")
    elif base:
        cmd.append(base)
    cmd += ["--", "*.py"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"git diff {base or '--staged'}: {proc.stderr.strip()}")
    ranges: dict[str, set[int]] = {}
    current = ""
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            ranges.setdefault(current, set())
        elif line.startswith("@@") and current:
            # @@ -a,b +c,d @@
            tail = line.split("+", 1)[1].split("@@", 1)[0].strip()
            start_s, _, count_s = tail.partition(",")
            start, count = int(start_s), int(count_s or "1")
            ranges[current].update(range(start, start + max(0, count)))
    return ranges


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="все файлы (ручной аудит)")
    group.add_argument("--changed", metavar="BASE", help="только добавленные строки против BASE")
    group.add_argument("--staged", action="store_true", help="только добавленные строки в индексе")
    args = ap.parse_args(argv)

    if args.all:
        files = _python_files(DEFAULT_DIRS)
        ranges = None
    else:
        base = args.changed or (EMPTY_TREE if args.staged else "")
        if args.staged:
            base = ""
        try:
            ranges = _changed_ranges(base, args.staged)
        except SystemExit as exc:
            print(f"⛔ {exc}", file=sys.stderr)
            return 1
        files = [ROOT / rel for rel in ranges]

    findings: list[dict] = []
    for path in files:
        if not path.is_file():
            continue
        key = path.relative_to(ROOT).as_posix()
        for finding in check_source(path):
            if ranges is not None and finding["line"] not in ranges.get(key, set()):
                continue
            findings.append(finding)

    if not findings:
        print("✅ платформенные правила соблюдены")
        return 0
    for finding in findings:
        print(f"⛔ {finding['path']}:{finding['line']}: {finding['rule']}: {finding['message']}",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
