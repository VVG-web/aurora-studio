#!/usr/bin/env python3
"""part_194 — здоровье индексов кода: свежесть и устаревание (1.206.0)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402


def _config(root: Path, repos: dict) -> None:
    path = root / "aurora.config.yaml"
    path.write_text(CR.write_block(path.read_text(encoding="utf-8"), repos), encoding="utf-8")


@test
def test_code_health_reports_fresh_and_stale_index(_t):
    """Свежий индекс проходит --check, новый коммит без переиндексации даёт stale."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files={"src/A.java": "class A {}\n"})
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("code_health.py", "--project", str(root), "--check", cwd=root)
    data = json.loads(run("code_health.py", "--project", str(root), "--json",
                          cwd=root, expect_rc=0).stdout)
    assert data["repos"][0]["built"] and not data["repos"][0]["stale"], data

    CF.commit(repo, "next", "2026-01-02T00:00:00+00:00", files={"src/B.java": "class B {}\n"})
    stale = run("code_health.py", "--project", str(root), "--check", "--json",
                cwd=root, expect_rc=1)
    row = json.loads(stale.stdout)["repos"][0]
    assert row["stale"] and row["why"], row
