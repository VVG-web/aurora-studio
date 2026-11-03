#!/usr/bin/env python3
"""part_190 — граф кода берёт таблицы, маршруты и топики из индекса (1.203.1)."""
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
def test_code_graph_takes_routes_topics_tables_from_index(_t):
    """Индекс кода отдаёт маршруты, топики и таблицы в code.json и graph.json."""
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
    run("kb_code_graph.py", "--root", str(root), "--apply", cwd=root)

    code = json.loads((root / "AuroraKnowledgeDB/meta/graphify/code.json").read_text(encoding="utf-8"))
    assert code["index"]["repos"] == ["orders"], code["index"]
    assert any(r["method"] == "GET" and r["path"] == "/api/orders" for r in code["routes"]), code["routes"]
    assert any(t["name"] == "orders.accepted" for t in code["topics"]), code["topics"]
    assert "order_log" in code["tables"], code["tables"]

    run("kb_graph.py", "--export", cwd=root)
    graph = json.loads((root / "AuroraKnowledgeDB/meta/graphify/graph.json").read_text(encoding="utf-8"))
    kinds = {n.get("kind") for n in graph["nodes"]}
    assert "route" in kinds and "topic" in kinds, kinds
    labels = {n.get("label") for n in graph["nodes"]}
    assert "GET /api/orders" in labels and "orders.accepted" in labels, labels
