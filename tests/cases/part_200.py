#!/usr/bin/env python3
"""part_200 — merge build_failures и проверка драйверов doctor (1.209.2)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness import SCRIPTS, run, test

sys.path.insert(0, str(SCRIPTS))
import aurora_doctor as AD  # noqa: E402
import install_aurora as IA  # noqa: E402


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _merge(tmp: Path, base: dict, ours: dict, theirs: dict) -> dict:
    b, o, t = tmp / "base.json", tmp / "ours.json", tmp / "theirs.json"
    _write(b, base)
    _write(o, ours)
    _write(t, theirs)
    run("merge_failures.py", str(b), str(o), str(t), "7", "build_failures.json",
        cwd=tmp, expect_rc=0)
    return json.loads(o.read_text(encoding="utf-8"))


@test
def test_build_failures_merge_unions_keys(tmp: Path):
    """Разные источники у двух участников сохраняются оба."""
    base = {"a.md": {"hash": "a", "count": 1, "at": "2026-01-01"}}
    ours = {**base, "b.md": {"hash": "b", "count": 1, "at": "2026-01-02"}}
    theirs = {**base, "c.md": {"hash": "c", "count": 1, "at": "2026-01-03"}}
    merged = _merge(tmp, base, ours, theirs)
    assert set(merged) == {"a.md", "b.md", "c.md"}, merged


@test
def test_build_failures_merge_prefers_newer_at(tmp: Path):
    """Один источник упал у обоих — побеждает более поздняя запись."""
    base = {"x.md": {"hash": "x", "count": 1, "at": "2026-01-01"}}
    ours = {"x.md": {"hash": "x", "count": 2, "at": "2026-01-02"}}
    theirs = {"x.md": {"hash": "x", "count": 3, "at": "2026-01-03"}}
    assert _merge(tmp, base, ours, theirs)["x.md"]["count"] == 3


@test
def test_build_failures_merge_honors_deletion(tmp: Path):
    """Удаление записи одной стороной сохраняется, если вторая не меняла её."""
    base = {"x.md": {"hash": "x", "count": 1, "at": "2026-01-01"}}
    ours = {}
    theirs = {"x.md": {"hash": "x", "count": 1, "at": "2026-01-01"}}
    assert _merge(tmp, base, ours, theirs) == {}


@test
def test_build_failures_bad_json_returns_conflict(tmp: Path):
    """Битый JSON — код 1, Git оставляет конфликт человеку."""
    b, o, t = tmp / "base.json", tmp / "ours.json", tmp / "theirs.json"
    _write(b, {})
    o.write_text("{", encoding="utf-8")
    _write(t, {})
    run("merge_failures.py", str(b), str(o), str(t), "7", "build_failures.json",
        cwd=tmp, expect_rc=1)


@test
def test_gitattributes_has_build_failures_rule(_t):
    """Правило build_failures едет в проекты вместе с .gitattributes."""
    assert "AuroraKnowledgeDB/meta/build_failures.json merge=aurora-build-failures" in IA.GITATTRIBUTES_MERGE_BLOCK


@test
def test_install_configures_build_failures_driver(tmp: Path):
    """Установка настраивает драйвер build_failures."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert IA.configure_merge_driver(repo) is True
    value = subprocess.run(["git", "-C", str(repo), "config", "--get",
                            "merge.aurora-build-failures.driver"],
                           capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert "merge_failures.py" in value, value


@test
def test_doctor_reports_missing_merge_driver(tmp: Path):
    """Doctor называет отсутствующий git driver и молчит после настройки."""
    old = AD.ROOT
    AD.ROOT = tmp
    try:
        warnings = AD.check_merge_drivers()
        assert any("merge.aurora-manifest.driver" in w for w in warnings), warnings
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        (tmp / ".gitattributes").write_text(IA.GITATTRIBUTES_MERGE_BLOCK, encoding="utf-8")
        assert IA.configure_merge_driver(tmp) is True
        assert AD.check_merge_drivers() == []
    finally:
        AD.ROOT = old
