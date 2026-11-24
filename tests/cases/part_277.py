"""Проверки движка Aurora, часть 277. Каркас и помощники — tests/harness.py.

Платформенные правила 1.231.20: статический чекер ловит relpath со смешанными
путями, план без `newline=""` и хрупкий wall-clock допуск; атомарная запись
сохраняет LF, по которому считается хеш.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from harness import SCRIPTS, test


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


@test
def test_portability_checker_catches_known_mistakes(tmp: Path):
    """Три класса ошибок, которые уже стоили выпусков, валят чекер."""
    CP = _mod("check_portability")
    bad = tmp / "bad.py"
    bad.write_text(
        "import os\n"
        "from aurora_common import write_text_atomic\n"
        "root = '.'\n"
        "plan_path = 'Scripts/Generated/opz.docx.json'\n"
        "text = '{}\\n'\n"
        "total_ms = 120_000\n"
        "os.path.relpath('/abs/plan.json', root)\n"
        "write_text_atomic(plan_path, text)\n"
        "assert abs(total_ms - 300_000) <= 100\n",
        encoding="utf-8")
    rules = {finding["rule"] for finding in CP.check_source(bad)}
    assert {"relpath-start", "plan-newline", "tight-clock"} <= rules, rules


@test
def test_portability_checker_accepts_fixed_code(tmp: Path):
    """Правильные формы не дают находок: realpath у start, newline у плана."""
    CP = _mod("check_portability")
    good = tmp / "good.py"
    good.write_text(
        "import os\n"
        "from aurora_common import write_text_atomic\n"
        "root = '.'\n"
        "plan_path = 'Scripts/Generated/opz.docx.json'\n"
        "text = '{}\\n'\n"
        "os.path.relpath('/abs/plan.json', os.path.realpath(root))\n"
        "write_text_atomic(plan_path, text, newline='')\n"
        "assert abs(120_000 - 300_000) <= 2_000\n",
        encoding="utf-8")
    assert CP.check_source(good) == []


@test
def test_atomic_write_keeps_lf_for_hash(tmp: Path):
    """`newline=""` не переводит LF в CRLF: хеш плана считается по тем же байтам."""
    AC = _mod("aurora_common")
    target = tmp / "plan.json"
    text = '{"kind": "opz",\n "version": 1}\n'
    AC.write_text_atomic(str(target), text, newline="")
    assert target.read_bytes() == text.encode("utf-8")
