#!/usr/bin/env python3
"""card_write_sites.py — храповик мест записи файлов (шаблоны карточек, CT-09).

Зачем. Карточки базы пишут десятки функций в двадцати скриптах: центрального писателя не было, и
«обязательность шаблона» нельзя было включить одной правкой. Теперь единственный вход записи карточки —
`card_types.write_card_text`, а этот инструмент не даёт появиться новому прямому месту записи.

Как считается. По AST обходятся `scripts/*.py`, `scripts/agents/*.py`, `cockpit/*.py`, `aurora.py` и `reports/*.py`; место
записи — функция, в теле которой есть вызов, пишущий файл: `open(…, "w"|"wb"|"a"|"x")`, `.write_text`,
`.write_bytes`, `write_if_changed`, `replace_file`, `write_text_atomic`, `os.rename`/`os.replace`,
`shutil.move`/`copy*`. Ключ места — `файл:функция` (вложенная — `внешняя.вложенная`, метод — `Класс.метод`).

Список допускаемых мест — `tests/card_write_sites.json`: на каждое место `kind` — `card` (пишет файл карточки базы
напрямую) или `other` (пишет не карточку: состояние, журнал, конфиг, карту, документ) — и `reason`, почему так.
Проверка краснеет, когда: (а) в коде есть место записи, которого нет в файле; (б) в файле есть место, которого в
коде уже нет; (в) мест `card` больше, чем `card_max` в файле. Снизили число — подсказка «обновите планку».

  python3 tests/card_write_sites.py --check              # сверить код и файл (код возврата 1 — расхождение)
  python3 tests/card_write_sites.py --snapshot           # что нашёл обход (JSON; новые места — kind: "?")
  python3 tests/card_write_sites.py --snapshot --write   # дописать новые места в файл (kind: "?" — потом назвать)

Модуль не часть кита (в engine_manifest не попадает); только стандартная библиотека.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
SITES = HERE / "card_write_sites.json"

SCAN_GLOBS = ("scripts/*.py", "scripts/agents/*.py", "cockpit/*.py", "reports/*.py", "aurora.py")
KINDS = ("card", "other")

# Вызовы по имени (`write_if_changed(...)`, `aurora_common.replace_file(...)`).
WRITE_FUNCS = frozenset(("write_if_changed", "replace_file", "write_text_atomic"))
# Методы и функции модулей, которые пишут или переносят файл. Голый `.rename(` не считается: так называется и метод
# планировщика ремонта (`Plan.rename`), а перенос файла по-настоящему — `os.rename`/`shutil.move`.
WRITE_ATTRS = frozenset(("write_text", "write_bytes"))
WRITE_QUALIFIED = frozenset(("os.replace", "os.rename", "shutil.move", "shutil.copy", "shutil.copy2",
                             "shutil.copyfile", "shutil.copytree"))
_MODE_RE = re.compile(r"^[rwxabt+U]{1,4}$")


def _dotted(node) -> str:
    """`os.path.join` → "os.path.join"; всё, что не цепочка имён, → ""."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _writes_mode(value) -> bool:
    return isinstance(value, str) and bool(_MODE_RE.match(value)) and any(c in value for c in "wax+")


def _mode_arg(call: ast.Call, positions: tuple):
    for i in positions:
        if i < len(call.args) and isinstance(call.args[i], ast.Constant) and isinstance(call.args[i].value, str):
            if _MODE_RE.match(call.args[i].value):
                return call.args[i].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def is_write_call(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name):
        if fn.id in WRITE_FUNCS:
            return True
        if fn.id == "open":
            return _writes_mode(_mode_arg(call, (1,)))
        return False
    if isinstance(fn, ast.Attribute):
        if fn.attr in WRITE_FUNCS or fn.attr in WRITE_ATTRS:
            return True
        if fn.attr == "open":                              # Path.open("w") и io.open(path, "w")
            return _writes_mode(_mode_arg(call, (0, 1)))
        return _dotted(fn) in WRITE_QUALIFIED
    return False


class _Scopes(ast.NodeVisitor):
    def __init__(self):
        self.stack: list = []
        self.found: dict = {}

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def visit_Call(self, node):
        if is_write_call(node):
            name = ".".join(self.stack) or "<module>"
            self.found.setdefault(name, node.lineno)
        self.generic_visit(node)


def scan(root: Path = KIT) -> dict:
    """{"файл:функция": первая строка записи}."""
    out = {}
    files = sorted({p for pattern in SCAN_GLOBS for p in root.glob(pattern) if p.is_file()})
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError:
            continue
        sc = _Scopes()
        sc.visit(tree)
        for name, line in sc.found.items():
            out[f"{rel}:{name}"] = line
    return out


def load(path: Path = SITES) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("card_max", 0)
    data.setdefault("sites", {})
    return data


def verdict(found: dict, data: dict) -> list:
    """Список расхождений между кодом и файлом; пусто — всё сходится."""
    sites = data.get("sites", {})
    problems = []
    for key in sorted(set(found) - set(sites)):
        problems.append(f"новое место записи не названо в card_write_sites.json: {key} (строка {found[key]}) — "
                        "карточку пишут через card_types.write_card_text; не карточку — kind: other и причина")
    for key in sorted(set(sites) - set(found)):
        problems.append(f"в card_write_sites.json есть место, которого в коде нет: {key} — уберите запись")
    for key, rec in sorted(sites.items()):
        kind = (rec or {}).get("kind")
        if kind not in KINDS:
            problems.append(f"{key}: kind «{kind}» — ожидается card или other")
        elif kind == "other" and not str((rec or {}).get("reason", "")).strip():
            problems.append(f"{key}: у места kind: other нет reason")
    cards = [k for k, r in sites.items() if (r or {}).get("kind") == "card"]
    if len(cards) > int(data.get("card_max", 0)):
        problems.append(f"мест kind: card {len(cards)}, а планка card_max {data.get('card_max', 0)}: "
                        "новое прямое место записи карточки не допускается — " + ", ".join(sorted(cards)[:5]))
    return problems


def hints(found: dict, data: dict) -> list:
    cards = [k for k, r in data.get("sites", {}).items() if (r or {}).get("kind") == "card"]
    if len(cards) < int(data.get("card_max", 0)):
        return [f"мест kind: card стало {len(cards)} вместо {data.get('card_max')}: обновите card_max "
                f"(python3 tests/card_write_sites.py --snapshot --write)"]
    return []


def snapshot(found: dict, data: dict) -> dict:
    """Файл по текущему обходу: прежние названия сохраняются, новые места получают kind: "?"."""
    sites = {}
    for key in sorted(found):
        old = data.get("sites", {}).get(key)
        sites[key] = old if old else {"kind": "?", "reason": ""}
    cards = sum(1 for r in sites.values() if r.get("kind") == "card")
    return {"card_max": min(int(data.get("card_max", cards)), cards) if data.get("sites") else cards,
            "sites": sites}


def main(argv: list) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    found, data = scan(), load()
    if "--snapshot" in argv:
        snap = snapshot(found, data)
        text = json.dumps(snap, ensure_ascii=False, indent=1) + "\n"
        if "--write" in argv:
            SITES.write_text(text, encoding="utf-8", newline="\n")
            print(f"записано: {SITES.relative_to(KIT).as_posix()} · мест {len(snap['sites'])}")
        else:
            sys.stdout.write(text)
        return 0
    problems = verdict(found, data)
    for p in problems:
        print("-", p)
    for h in hints(found, data):
        print("подсказка:", h)
    cards = sum(1 for r in data["sites"].values() if (r or {}).get("kind") == "card")
    print(f"мест записи: {len(found)} · из них карточки напрямую: {cards} (планка {data.get('card_max', 0)})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
