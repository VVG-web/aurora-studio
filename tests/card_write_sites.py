#!/usr/bin/env python3
"""card_write_sites.py — храповик мест записи файлов (шаблоны карточек, CT-09).

Зачем. Карточки базы пишут десятки функций в двадцати скриптах: центрального писателя не было, и
«обязательность шаблона» нельзя было включить одной правкой. Теперь единственный вход записи карточки —
`card_types.write_card_text`, а этот инструмент не даёт появиться новому прямому месту записи.

Как считается. По AST обходятся все `.py` под `scripts/`, `cockpit/` (кроме `vendor/`), `reports/` и `aurora.py`;
место записи — функция, в теле которой есть вызов, пишущий файл: `open(…)` с режимом записи (режим не литералом
считается записью), `.write_text`, `.write_bytes`, `write_if_changed`, `replace_file`, `write_text_atomic`,
`os.rename`/`os.replace`/`os.truncate`, `os.open` с флагами записи, `shutil.move`/`copy*` (`copyfile`, `copy2`, `copytree`
и под именем без модуля после `from … import`). Ключ места — `файл:функция` (вложенная — `внешняя.вложенная`, метод — `Класс.метод`); на месте
записано и число вызовов записи в нём (`calls`): новый вызов в уже названной функции — тоже новое место.

Список допускаемых мест — `tests/card_write_sites.json`: на каждое место `kind` — `card` (пишет файл карточки базы
напрямую) или `other` (пишет не карточку: состояние, журнал, конфиг, документ, сам вход записи) — и `reason`, почему
так. Проверка краснеет, когда: (а) в коде есть место записи, которого нет в файле, или в нём стало больше вызовов
записи; (б) в файле есть место, которого в коде уже нет; (в) число мест `card` не равно `card_max` (выросло — прямая
запись карточки не допускается; снизилось — планку надо затянуть, иначе вернуть десять мест будет можно); (г) файл
не разобрался (`SyntaxError`) — молча пропустить его значило бы потерять его места.

  python3 tests/card_write_sites.py --check              # сверить код и файл (код возврата 1 — расхождение)
  python3 tests/card_write_sites.py --snapshot           # что нашёл обход (JSON; новые места — kind: "?")
  python3 tests/card_write_sites.py --snapshot --write   # дописать новые места в файл (kind: "?" — потом назвать)
  python3 tests/card_write_sites.py --lines              # все строки записи на каждое место (чтобы назвать место верно)

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

SCAN_DIRS = ("scripts", "cockpit", "reports")
SCAN_FILES = ("aurora.py",)
SKIP_PARTS = ("__pycache__", "vendor")
KINDS = ("card", "other")

# Вызовы по имени (`write_if_changed(...)`, `aurora_common.replace_file(...)`, а после `from shutil import move` — `move(...)`).
WRITE_FUNCS = frozenset(("write_if_changed", "replace_file", "write_text_atomic", "copyfile", "copy2", "copytree"))
# Методы и функции модулей, которые пишут или переносят файл. Голый `.rename(` не считается: так называется и метод
# планировщика ремонта (`Plan.rename`), а перенос файла по-настоящему — `os.rename`/`shutil.move`.
WRITE_ATTRS = frozenset(("write_text", "write_bytes"))
WRITE_QUALIFIED = frozenset(("os.replace", "os.rename", "os.truncate", "shutil.move", "shutil.copy", "shutil.copy2",
                             "shutil.copyfile", "shutil.copytree"))
OS_WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC")
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


def _opens_for_writing(call: ast.Call) -> bool:
    """`open(...)` пишет? Режим-литерал — по его знакам; режим не литералом (переменная, условие) — считается
    записью: безопаснее назвать лишнее место, чем пропустить настоящее. Первый аргумент без кавычек — путь, не режим."""
    for i, node in enumerate(call.args[:2]):
        literal = isinstance(node, ast.Constant) and isinstance(node.value, str)
        if literal and _MODE_RE.match(node.value):
            if _writes_mode(node.value):
                return True
        elif not literal and i == 1:
            return True
    for kw in call.keywords:
        if kw.arg == "mode":
            if not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)) or _writes_mode(kw.value.value):
                return True
    return False


def is_write_call(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name):
        if fn.id in WRITE_FUNCS:
            return True
        return fn.id == "open" and _opens_for_writing(call)
    if isinstance(fn, ast.Attribute):
        if fn.attr in WRITE_ATTRS or fn.attr in ("write_if_changed", "replace_file", "write_text_atomic"):
            return True
        if _dotted(fn) == "os.open":                       # os.open(путь, флаги): пишет, если среди флагов есть запись
            return len(call.args) > 1 and any(f in ast.unparse(call.args[1]) for f in OS_WRITE_FLAGS)
        if fn.attr == "open":                              # Path.open("w") и io.open(path, "w")
            return _opens_for_writing(call)
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
            self.found.setdefault(name, []).append(node.lineno)
        self.generic_visit(node)


def _files(root: Path) -> list:
    out = [root / name for name in SCAN_FILES if (root / name).is_file()]
    for folder in SCAN_DIRS:
        base = root / folder
        if base.is_dir():
            out += [p for p in base.rglob("*.py") if p.is_file() and not any(x in p.parts for x in SKIP_PARTS)]
    return sorted(set(out))


def scan_lines(root: Path = KIT) -> dict:
    """{"файл:функция": [строки всех вызовов записи]}; файл, который не разобрался, — ключ "файл:<SyntaxError>"."""
    out = {}
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as e:
            out[f"{rel}:<SyntaxError>"] = [e.lineno or 0]
            continue
        sc = _Scopes()
        sc.visit(tree)
        for name, lines in sc.found.items():
            out[f"{rel}:{name}"] = sorted(lines)
    return out


def scan(root: Path = KIT) -> dict:
    """{"файл:функция": первая строка записи}."""
    return {key: lines[0] for key, lines in scan_lines(root).items()}


def load(path: Path = SITES) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("card_max", 0)
    data.setdefault("sites", {})
    return data


def verdict(found: dict, data: dict, lines: "dict | None" = None) -> list:
    """Список расхождений между кодом и файлом; пусто — всё сходится. `lines` — все строки записи по местам
    (для подсчёта вызовов); без него число вызовов не сверяется."""
    sites = data.get("sites", {})
    problems = []
    for key in sorted(found):
        if key.endswith(":<SyntaxError>"):
            problems.append(f"{key.split(':', 1)[0]}: файл не разобрался (строка {found[key]}) — места записи в нём "
                            "не видны; починить синтаксис")
    for key in sorted(set(found) - set(sites)):
        if key.endswith(":<SyntaxError>"):
            continue
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
        if lines is not None and key in lines and (rec or {}).get("calls") is not None:
            if len(lines[key]) > int((rec or {}).get("calls")):
                problems.append(f"{key}: вызовов записи стало {len(lines[key])} вместо {rec['calls']} "
                                f"(строки {', '.join(map(str, lines[key]))}) — новая запись в названной функции "
                                "требует нового названия: обновите calls и reason после проверки, что это не карточка")
    cards = [k for k, r in sites.items() if (r or {}).get("kind") == "card"]
    if len(cards) > int(data.get("card_max", 0)):
        problems.append(f"мест kind: card {len(cards)}, а планка card_max {data.get('card_max', 0)}: "
                        "новое прямое место записи карточки не допускается — " + ", ".join(sorted(cards)[:5]))
    elif len(cards) < int(data.get("card_max", 0)):
        problems.append(f"мест kind: card {len(cards)} меньше планки card_max {data.get('card_max')}: затяните планку "
                        "(python3 tests/card_write_sites.py --snapshot --write), иначе места можно будет вернуть")
    return problems


def hints(found: dict, data: dict) -> list:
    return []


def snapshot(found: dict, data: dict, lines: "dict | None" = None) -> dict:
    """Файл по текущему обходу: прежние названия сохраняются, новые места получают kind: "?"."""
    sites = {}
    for key in sorted(found):
        if key.endswith(":<SyntaxError>"):
            continue
        old = data.get("sites", {}).get(key)
        rec = dict(old) if old else {"kind": "?", "reason": ""}
        if lines is not None and key in lines:
            rec["calls"] = len(lines[key])
        sites[key] = rec
    cards = sum(1 for r in sites.values() if r.get("kind") == "card")
    return {"card_max": cards, "sites": sites}


def main(argv: list) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    lines = scan_lines()
    found = {k: v[0] for k, v in lines.items()}
    data = load()
    if "--lines" in argv:
        for key, ls in sorted(lines.items()):
            print(f"{key}: {', '.join(map(str, ls))}")
        return 0
    if "--snapshot" in argv:
        snap = snapshot(found, data, lines)
        text = json.dumps(snap, ensure_ascii=False, indent=1) + "\n"
        if "--write" in argv:
            SITES.write_text(text, encoding="utf-8", newline="\n")
            print(f"записано: {SITES.relative_to(KIT).as_posix()} · мест {len(snap['sites'])} · card_max {snap['card_max']}")
        else:
            sys.stdout.write(text)
        return 0
    problems = verdict(found, data, lines)
    for p in problems:
        print("-", p)
    cards = sum(1 for r in data["sites"].values() if (r or {}).get("kind") == "card")
    print(f"мест записи: {len(found)} · из них карточки напрямую: {cards} (планка {data.get('card_max', 0)})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
