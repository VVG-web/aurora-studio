"""Проверки движка Aurora, часть 91. Каркас и помощники — tests/harness.py.

Храповик слоя ролей (BL-DEC-06): путь зеркала по названию продукта в коде ядра не должен
появляться снова. Модель и сеть не используются.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Путь зеркала `Sources/<продукт>` в коде ядра — то место, где замена коннектора стоит правки ядра.
# Считаем строковые константы кода (не докстроки), в которых он встречается; скрипты выгрузки самих
# коннекторов — код коннекторов и не ядро. Число по файлу может только падать: снизилось — поправьте
# планку ниже (так достигнутое не откатят), выросло — путь надо брать у `service_dirs`, а не писать.
MIRROR_LITERAL = re.compile(r"Sources[/\\](?:JIRA|Confluence)\b")
CONNECTOR_SCRIPTS = {"jira_export.py", "confluence_export.py", "atlassian_ops.py", "jira_status.py"}
MIRROR_LITERALS_BASELINE = {
    "scripts/aurora_common.py": 2,      # прежние папки на случай, когда реестр источников не ответил вовсе
    "scripts/aurora_setup.py": 2,       # умолчания исторических ключей `paths:` в форме настроек
    "scripts/kb_remap.py": 6,           # разовый ремонт ссылок на старые имена файлов Jira
    "scripts/build_plan.py": 1,         # подсказка `--group` в тексте отчёта
    "reports/analyst/paths.py": 1,      # отчёты аналитика
}


def _mirror_literals(kit: Path) -> dict:
    out = {}
    paths = [p for d in ("scripts", "cockpit", "reports") for p in sorted((kit / d).rglob("*.py"))
             if "vendor" not in p.parts and p.name not in CONNECTOR_SCRIPTS] + [kit / "aurora.py"]
    for p in paths:
        if not p.is_file():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                b = n.body
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                        and isinstance(b[0].value.value, str):
                    docs.add(id(b[0].value))
        count = sum(1 for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
                    and MIRROR_LITERAL.search(n.value))
        if count:
            out[p.relative_to(kit).as_posix()] = count
    return out


@test
def test_core_does_not_grow_mirror_path_literals(_t):
    """Ядро не знает `Sources/<продукт>`: число литералов в файле не растёт сверх планки (храповик)."""
    got = _mirror_literals(KIT)
    grown = {f: (n, MIRROR_LITERALS_BASELINE.get(f, 0)) for f, n in got.items()
             if n > MIRROR_LITERALS_BASELINE.get(f, 0)}
    assert not grown, ("в ядре появился путь зеркала по названию продукта — берите папку у "
                       f"aurora_common.service_dirs(<роль>): {grown}")
    lower = {f: (got.get(f, 0), n) for f, n in MIRROR_LITERALS_BASELINE.items() if got.get(f, 0) < n}
    assert not lower, f"литералов стало меньше — опустите планку MIRROR_LITERALS_BASELINE: {lower}"


@test
def test_mirror_literal_counter_sees_code_but_not_docstrings(tmp: Path):
    """Сам счётчик: литерал в коде и в f-строке считается, в докстроке и в сторонних файлах — нет."""
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "core.py").write_text(
        '"""Читает Sources/JIRA — это докстрока."""\n'
        'A = "Sources/Confluence/x"\n'
        'def f(k):\n    """Sources/JIRA в докстроке функции."""\n    return f"Sources/JIRA/{k}.md"\n',
        encoding="utf-8")
    (tmp / "scripts" / "jira_export.py").write_text('P = "Sources/JIRA"\n', encoding="utf-8")
    (tmp / "scripts" / "other.py").write_text('P = "Sources/Web"\n', encoding="utf-8")
    assert _mirror_literals(tmp) == {"scripts/core.py": 2}, _mirror_literals(tmp)
