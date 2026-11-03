#!/usr/bin/env python3
"""part_192 — проверка критериев истории по индексу кода (1.204.0)."""
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


@test
def test_code_check_finds_route_table_topic_and_string(_t):
    """Критерии получают доказательства из маршрутов, таблиц, топиков и строк индекса."""
    root = make_project(_t)
    _write(root / "Sources/Confluence/US-1.1.md", """---
title: "US-1.1 Приём заявки"
---
# US-1.1 Приём заявки

## Критерии приёмки

1. POST /api/orders принимает заявку и пишет в order_log.
2. Дубль отклоняется с кодом `DUPLICATE_ORDER`.
3. Событие уходит в топик orders.accepted.
""")
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files={
        "src/OrderController.java": (
            "package demo;\n"
            "@RestController\n"
            "@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  private static final String DUPLICATE = \"DUPLICATE_ORDER\";\n"
            "  @PostMapping(\"/orders\")\n"
            "  public String create() {\n"
            "    return DUPLICATE;\n"
            "  }\n"
            "}\n"),
        "src/OrderListener.java": (
            "package demo;\n"
            "public class OrderListener {\n"
            "  @KafkaListener(topics = \"orders.accepted\")\n"
            "  public void on(String event) {\n"
            "  }\n"
            "}\n"),
        "db/schema.sql": "CREATE TABLE order_log (id INT);\n",
    })
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    out = run("code_check.py", "--project", str(root), "--story", "US-1.1", "--json",
              cwd=root, expect_rc=0)
    report = json.loads(out.stdout)
    assert report["found"] and report["summary"]["total"] == 3, report["summary"]
    by_text = {c["text"]: c for c in report["criteria"]}
    route = next(c for c in report["criteria"] if "POST" in c["text"])
    assert any(e["kind"] == "route" for e in route["evidence"]), route
    dup = next(c for c in report["criteria"] if "DUPLICATE_ORDER" in c["text"])
    assert dup["verdict"] == "found" and any(e["kind"] == "string" for e in dup["evidence"]), dup
    topic = next(c for c in report["criteria"] if "orders.accepted" in c["text"])
    assert any(e["kind"] == "topic" for e in topic["evidence"]), topic
    assert by_text and len(by_text) == 3
