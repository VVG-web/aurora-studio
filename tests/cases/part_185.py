#!/usr/bin/env python3
"""part_185 — история коммитов и ключи задач (CR-10).

Проверки на настоящем git: ключи только известные, авторы не хранятся, переписанная
история заменяет старую, повтор на той же ревизии ничего не пересобирает.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402


def _config(root: Path, repos: dict, extra: str = "") -> None:
    path = root / "aurora.config.yaml"
    text = path.read_text(encoding="utf-8") + extra
    path.write_text(CR.write_block(text, repos), encoding="utf-8")


def _cli(root: Path, *args, expect_rc=0):
    return run("code_index.py", "--project", str(root), *args, cwd=root, expect_rc=expect_rc)


def _db(root: Path, name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(root / ".aurora" / "cache" / "code" / name / "index.sqlite"))
    conn.row_factory = sqlite3.Row
    return conn


@test
def test_history_keys_and_files(_t):
    """BENCH-1 возвращает два коммита и их файлы; SHA-256 и UTF-8 ключами не считаются."""
    root = make_project(_t)
    repo = CF.build("order-intake", root / "Code" / "order-intake")
    CF.commit(repo, "UTF-8 SHA-256: encodings are not task keys", "2026-02-01T00:00:00+00:00",
              files={"docs/encoding.md": "utf-8\n"})
    _config(root, {"order-intake": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}},
            extra="jira_keys: [BENCH]\n")
    _cli(root, "--index", "--apply")
    with _db(root, "order-intake") as conn:
        commits = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        assert commits == 4, commits
        keys = {r["key"] for r in conn.execute("SELECT key FROM commit_keys")}
        assert {"BENCH-1", "BENCH-2", "US-1.1", "US-1.2"} <= keys, keys
        assert "SHA-256" not in keys and "UTF-8" not in keys, keys
        rows = [dict(r) for r in conn.execute(
            "SELECT c.sha, f.path FROM commits c JOIN commit_keys k ON k.sha=c.sha "
            "LEFT JOIN commit_files f ON f.sha=c.sha WHERE k.key='BENCH-1' ORDER BY c.ts, f.path")]
        shas = {r["sha"] for r in rows}
        assert len(shas) == 2, rows
        paths = {r["path"] for r in rows}
        assert "src/main/java/com/example/orders/OrderController.java" in paths, paths
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(commits)")}
        assert "author" not in cols and "email" not in cols, cols


@test
def test_history_rewritten_history_replaces_old(_t):
    """Force-push: старый коммит исчезает, новый появляется."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "rewrite")
    first = CF.commit(repo, "BENCH-1: first", "2026-01-01T00:00:00+00:00",
                      files={"src/a.py": "a=1\n"})
    old_second = CF.commit(repo, "BENCH-1: second", "2026-01-02T00:00:00+00:00",
                           files={"src/a.py": "a=2\n"})
    _config(root, {"rewrite": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}},
            extra="jira_keys: [BENCH]\n")
    _cli(root, "--index", "--apply")
    with _db(root, "rewrite") as conn:
        assert {r["sha"] for r in conn.execute("SELECT sha FROM commits")} == {
            first, old_second}
    CF.git(repo, "reset", "-q", "--hard", first)
    second = CF.commit(repo, "BENCH-1: rewritten", "2026-01-03T00:00:00+00:00",
                       files={"src/a.py": "a=3\n"})
    _cli(root, "--index", "--apply")
    with _db(root, "rewrite") as conn:
        shas = {r["sha"] for r in conn.execute("SELECT sha FROM commits")}
        assert shas == {first, second}, shas
        assert old_second not in shas, shas
