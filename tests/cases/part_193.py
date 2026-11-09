#!/usr/bin/env python3
"""part_193 — Clean Room: документы по коду, паспорт и устаревание (1.205.0)."""
from __future__ import annotations

import json
import os
import subprocess
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
def test_clean_room_generates_docs_passport_and_finds_stale(_t):
    """Clean Room пишет документы с [cN], паспорт с ревизией и ловит новый коммит."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files={
        "src/OrderController.java": (
            "package demo;\n"
            "@RestController\n"
            "@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  @GetMapping(\"/orders\")\n"
            "  public String list() {\n"
            "    return \"ok\";\n"
            "  }\n"
            "}\n"),
        "db/schema.sql": "CREATE TABLE order_log (id INT);\n",
    })
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("clean_room.py", "--project", str(root), "--apply", cwd=root)

    doc = root / "CleanRoom/cr-api/orders.md"
    assert doc.is_file(), doc
    text = doc.read_text(encoding="utf-8")
    assert "GET /api/orders" in text and "[c1]" in text and "Что не удалось установить" in text, text
    passport = json.loads((root / "CleanRoom/meta/passport.json").read_text(encoding="utf-8"))
    assert passport["repos"]["orders"]["rev"] and passport["docs"], passport
    assert passport["enabled"] > 0 and passport["problems"] == [], passport
    run("clean_room.py", "--project", str(root), "--check", cwd=root)

    CF.commit(repo, "next", "2026-01-02T00:00:00+00:00", files={"src/Next.java": "class Next {}\n"})
    early = run("clean_room.py", "--project", str(root), "--check", "--json",
                cwd=root, expect_rc=1)
    assert "код" in json.loads(early.stdout)["stale"][0]["why"], early.stdout
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    stale = run("clean_room.py", "--project", str(root), "--check", "--json",
                cwd=root, expect_rc=1)
    assert json.loads(stale.stdout)["stale"], stale.stdout


@test
def test_clean_room_apply_without_docs_names_reason(_t):
    """Установленный движок без шаблонов не пишет пустой паспорт, а называет причину."""
    root = make_project(_t)
    cp = subprocess.run(
        [sys.executable, str(root / ".aurora/scripts/clean_room.py"),
         "--project", str(root), "--apply"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONPATH": str(SCRIPTS)},
        timeout=60)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert "не собрал ни одного документа" in cp.stderr, cp.stderr
    assert not (root / "CleanRoom/meta/passport.json").exists()
