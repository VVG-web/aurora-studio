#!/usr/bin/env python3
"""part_198 — merge lint_baseline и lint_residue (1.209.0)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness import SCRIPTS, run, test

sys.path.insert(0, str(SCRIPTS))
import install_aurora as IA  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _baseline(tmp: Path, base: str, ours: str, theirs: str) -> str:
    b, o, t = tmp / "base.txt", tmp / "ours.txt", tmp / "theirs.txt"
    _write(b, base)
    _write(o, ours)
    _write(t, theirs)
    run("merge_lint.py", "baseline", str(b), str(o), str(t), "7", "lint_baseline.txt",
        cwd=tmp, expect_rc=0)
    return o.read_text(encoding="utf-8").strip()


def _residue(tmp: Path, base: list, ours: list, theirs: list) -> list:
    b, o, t = tmp / "base.json", tmp / "ours.json", tmp / "theirs.json"
    _write(b, json.dumps(base, ensure_ascii=False))
    _write(o, json.dumps(ours, ensure_ascii=False))
    _write(t, json.dumps(theirs, ensure_ascii=False))
    run("merge_lint.py", "residue", str(b), str(o), str(t), "7", "lint_residue.json",
        cwd=tmp, expect_rc=0)
    return json.loads(o.read_text(encoding="utf-8"))


@test
def test_lint_baseline_merge_takes_stricter_minimum(tmp: Path):
    """Храповик разрешает только снижать планку: merge берёт минимум."""
    assert _baseline(tmp, "10\n", "8\n", "12\n") == "8"
    assert _baseline(tmp, "10\n", "12\n", "8\n") == "8"


@test
def test_lint_baseline_merge_handles_missing_side(tmp: Path):
    """Нет файла с одной стороны — берётся значение другой; нет обеих — 0."""
    assert _baseline(tmp, "", "5\n", "") == "5"
    assert _baseline(tmp, "", "", "") == "0"


@test
def test_lint_residue_merge_unions_and_sorts(tmp: Path):
    """Остатки двух участников объединяются, дубликаты убираются, порядок стабилен."""
    merged = _residue(tmp, ["a"], ["a", "b"], ["a", "c"])
    assert merged == ["a", "b", "c"], merged


@test
def test_lint_merge_bad_input_returns_conflict(tmp: Path):
    """Битый residue — код 1, Git оставляет конфликт человеку."""
    b, o, t = tmp / "base.json", tmp / "ours.json", tmp / "theirs.json"
    _write(b, "[]")
    _write(o, "[")
    _write(t, "[]")
    run("merge_lint.py", "residue", str(b), str(o), str(t), "7", "lint_residue.json",
        cwd=tmp, expect_rc=1)


@test
def test_gitattributes_has_lint_merge_rules(_t):
    """Оба правила lint-файлов едут в проекты вместе с .gitattributes."""
    block = IA.GITATTRIBUTES_MERGE_BLOCK
    assert "AuroraKnowledgeDB/meta/lint_baseline.txt merge=aurora-lint-baseline" in block
    assert "AuroraKnowledgeDB/meta/lint_residue.json merge=aurora-lint-residue" in block


@test
def test_install_configures_lint_merge_drivers(tmp: Path):
    """Установка настраивает оба lint-драйвера, повтор идемпотентен."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert IA.configure_merge_driver(repo) is True
    for key in ("merge.aurora-lint-baseline.driver", "merge.aurora-lint-residue.driver"):
        value = subprocess.run(["git", "-C", str(repo), "config", "--get", key],
                               capture_output=True, text=True, encoding="utf-8").stdout.strip()
        assert "merge_lint.py" in value, (key, value)
    assert IA.configure_merge_driver(repo) is True
