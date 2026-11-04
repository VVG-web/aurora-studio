#!/usr/bin/env python3
"""part_202 — code:check 2.0: имена, fail-on и устаревший индекс (1.210.0)."""
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _story(root: Path, criteria: list) -> None:
    body = "\n".join(f"{n}. {text}" for n, text in enumerate(criteria, 1))
    _write(root / "Sources/Confluence/US-1.1.md",
           "---\ntitle: \"US-1.1 Приём заявки\"\n---\n# US-1.1 Приём заявки\n\n"
           "## Критерии приёмки\n\n" + body + "\n")


def _repo(root: Path, files: dict) -> Path:
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files=files)
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    return repo


@test
def test_code_check_missing_table_identifier_is_partial(tmp: Path):
    """Критерий называет audit_log, а в индексе только order_log — partial, не found."""
    root = make_project(tmp)
    _story(root, ["POST /api/orders принимает заявку и пишет в audit_log."])
    _repo(root, {
        "src/OrderController.java": (
            "package demo;\n@RestController\n@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  @PostMapping(\"/orders\")\n  public String create() { return \"ok\"; }\n}\n"),
        "db/schema.sql": "CREATE TABLE order_log (id INT);\n",
    })
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    report = json.loads(run("code_check.py", "--project", str(root), "--story", "US-1.1",
                            "--json", cwd=root, expect_rc=0).stdout)
    crit = report["criteria"][0]
    assert crit["verdict"] == "partial", crit
    assert "audit_log" in crit["missing"], crit["missing"]

    run("code_check.py", "--project", str(root), "--story", "US-1.1", "--fail-on", "partial",
        cwd=root, expect_rc=1)
    run("code_check.py", "--project", str(root), "--story", "US-1.1", "--fail-on", "not_found",
        cwd=root, expect_rc=0)


@test
def test_code_check_missing_route_identifier_is_partial(tmp: Path):
    """Критерий называет маршрут, которого нет в индексе, — partial по таблице и missing пути."""
    root = make_project(tmp)
    _story(root, ["POST /api/missing пишет в order_log."])
    _repo(root, {
        "db/schema.sql": "CREATE TABLE order_log (id INT);\n",
    })
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    report = json.loads(run("code_check.py", "--project", str(root), "--story", "US-1.1",
                            "--json", cwd=root, expect_rc=0).stdout)
    crit = report["criteria"][0]
    assert crit["verdict"] == "partial", crit
    assert "/api/missing" in crit["missing"], crit["missing"]


@test
def test_code_check_missing_quoted_identifier_is_partial(tmp: Path):
    """Цитата в backticks без строки в индексе — missing, а не молчаливое found."""
    root = make_project(tmp)
    _story(root, ["POST /api/orders возвращает код `AUDIT_CODE`."])
    _repo(root, {
        "src/OrderController.java": (
            "package demo;\n@RestController\n@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  @PostMapping(\"/orders\")\n  public String create() { return \"ok\"; }\n}\n"),
    })
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    report = json.loads(run("code_check.py", "--project", str(root), "--story", "US-1.1",
                            "--json", cwd=root, expect_rc=0).stdout)
    crit = report["criteria"][0]
    assert crit["verdict"] == "partial", crit
    assert "AUDIT_CODE" in crit["missing"], crit["missing"]


@test
def test_code_check_reports_stale_index(tmp: Path):
    """Новый коммит без переиндексации даёт stale в отчёте, но не меняет код 0 по умолчанию."""
    root = make_project(tmp)
    _story(root, ["POST /api/orders принимает заявку."])
    repo = _repo(root, {
        "src/OrderController.java": (
            "package demo;\n@RestController\n@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  @PostMapping(\"/orders\")\n  public String create() { return \"ok\"; }\n}\n"),
    })
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    CF.commit(repo, "next", "2026-01-02T00:00:00+00:00",
              files={"src/Next.java": "class Next {}\n"})
    report = json.loads(run("code_check.py", "--project", str(root), "--story", "US-1.1",
                            "--json", cwd=root, expect_rc=0).stdout)
    assert report["stale"] and report["stale"][0]["repo"] == "orders", report["stale"]
