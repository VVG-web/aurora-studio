#!/usr/bin/env python3
"""part_195 — регрессия работы с кодом: кэш, поиск, вердикты, Clean Room, health, два репозитория.

Эти сценарии закрывают то, что уже ломалось или могло сломаться незаметно:
кэш разбора после смены версии извлекателя, пустой запрос поиска, частичный и
ненайденный вердикты, испорченный паспорт Clean Room, отсутствие клона/индекса
и мульти-репозиторный граф.
"""
from __future__ import annotations

import json
import sqlite3
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


def _java_service(class_name: str, prefix: str, method: str, route: str,
                  topic: str, table: str) -> dict:
    """Java-контроллер, слушатель и SQL-таблица для одного репозитория."""
    return {
        f"src/{class_name}.java": (
            "package demo;\n"
            "@RestController\n"
            f"@RequestMapping(\"{prefix}\")\n"
            f"public class {class_name} {{\n"
            f"  @{method}(\"{route}\")\n"
            "  public String call() { return \"ok\"; }\n"
            "}\n"),
        "src/Listener.java": (
            "package demo;\n"
            "public class Listener {\n"
            f"  @KafkaListener(topics = \"{topic}\")\n"
            "  public void on(String event) {\n"
            "  }\n"
            "}\n"),
        "db/schema.sql": f"CREATE TABLE {table} (id INT);\n",
    }


@test
def test_code_index_reuses_cache_and_invalidates_on_extractor_bump(_t):
    """Смена версии извлекателя перечитывает файл и возвращает маршрут, а не старый кэш."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files=_java_service("OrderController", "/api", "GetMapping", "/orders",
                                  "orders.accepted", "order_log"))
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})

    first = json.loads(run("code_index.py", "--project", str(root), "--index", "--apply",
                           "--json", cwd=root, expect_rc=0).stdout)["repos"][0]
    assert first["counts"]["routes"] == 1, first
    second = json.loads(run("code_index.py", "--project", str(root), "--index", "--apply",
                            "--json", cwd=root, expect_rc=0).stdout)["repos"][0]
    assert second["reused"] >= 1 and second["counts"]["routes"] == 1, second

    db = root / ".aurora" / "cache" / "code" / "orders" / "index.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("UPDATE files SET extract_ver='extract-v0' WHERE path='src/OrderController.java'")
        con.execute("DELETE FROM routes WHERE file='src/OrderController.java'")
        con.commit()

    third = json.loads(run("code_index.py", "--project", str(root), "--index", "--apply",
                           "--json", cwd=root, expect_rc=0).stdout)["repos"][0]
    assert third["parsed"] >= 1, "кэш не инвалидирован после смены версии извлекателя: " + str(third)
    assert third["counts"]["routes"] == 1, "маршрут не восстановлен после смены версии: " + str(third)


@test
def test_code_find_empty_query_and_missing_index(_t):
    """Пустой запрос ничего не находит; отсутствующий индекс даёт missing и код 2."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files=_java_service("OrderController", "/api", "GetMapping", "/orders",
                                  "orders.accepted", "order_log"))
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})

    missing = json.loads(run("code_find.py", "--project", str(root), "--query", "", "--json",
                             cwd=root, expect_rc=2).stdout)
    assert missing["missing"] == ["orders"] and missing["results"] == [], missing

    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    empty = json.loads(run("code_find.py", "--project", str(root), "--query", "", "--json",
                           cwd=root, expect_rc=0).stdout)
    assert empty["results"] == [] and empty["missing"] == [], empty
    by_kind = json.loads(run("code_find.py", "--project", str(root), "--query", "",
                             "--kind", "route", "--json", cwd=root, expect_rc=0).stdout)
    assert by_kind["results"] == [], by_kind


@test
def test_code_check_partial_and_not_found_verdicts(_t):
    """Найденный маршрут без таблицы — partial; критерий без следов в коде — not_found."""
    root = make_project(_t)
    _write(root / "Sources/Confluence/US-1.1.md", """---
title: "US-1.1 Приём заявки"
---
# US-1.1 Приём заявки

## Критерии приёмки

1. POST /api/orders принимает заявку и пишет в order_log.
2. POST /api/orders принимает 42 заявки.
3. Экспорт отчёта в Excel.
""")
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files=_java_service("OrderController", "/api", "GetMapping", "/orders",
                                  "orders.accepted", "order_log"))
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)

    report = json.loads(run("code_check.py", "--project", str(root), "--story", "US-1.1",
                            "--json", cwd=root, expect_rc=0).stdout)
    assert report["found"] and report["summary"]["total"] == 3, report["summary"]
    by_n = {c["n"]: c for c in report["criteria"]}
    assert by_n[1]["verdict"] == "found", by_n[1]
    assert by_n[2]["verdict"] == "partial" and by_n[2]["missing"], by_n[2]
    assert by_n[3]["verdict"] == "not_found", by_n[3]


@test
def test_clean_room_missing_or_corrupt_passport(_t):
    """Нет паспорта, битый паспорт и пропавший документ — каждый раз код 1 и причина."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files=_java_service("OrderController", "/api", "GetMapping", "/orders",
                                  "orders.accepted", "order_log"))
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("clean_room.py", "--project", str(root), "--apply", cwd=root)
    run("clean_room.py", "--project", str(root), "--check", cwd=root, expect_rc=0)

    passport = root / "CleanRoom" / "meta" / "passport.json"
    passport.unlink()
    missing = run("clean_room.py", "--project", str(root), "--check", "--json",
                  cwd=root, expect_rc=1)
    assert "CleanRoom/meta/passport.json" in json.loads(missing.stdout)["missing"], missing.stdout

    passport.write_text("{", encoding="utf-8")
    corrupt = run("clean_room.py", "--project", str(root), "--check", "--json",
                  cwd=root, expect_rc=1)
    assert "CleanRoom/meta/passport.json" in json.loads(corrupt.stdout)["missing"], corrupt.stdout

    passport.write_text(json.dumps({
        "repos": {},
        "docs": [{"path": "CleanRoom/cr-api/missing.md", "repo": "orders",
                  "kind": "cr-api", "rev": "", "facts": 0}],
    }), encoding="utf-8")
    gone = run("clean_room.py", "--project", str(root), "--check", "--json",
               cwd=root, expect_rc=1)
    assert "CleanRoom/cr-api/missing.md" in json.loads(gone.stdout)["missing"], gone.stdout


@test
def test_code_health_missing_index_and_clone(_t):
    """Нет клона и нет индекса — разные причины, обе дают код 1."""
    root = make_project(_t)
    repo_path = root / "Code" / "orders"
    _config(root, {"orders": {"url": CF.url_of(repo_path), "branch": "main", "provider": "generic"}})

    missing = json.loads(run("code_health.py", "--project", str(root), "--check", "--json",
                             cwd=root, expect_rc=1).stdout)["repos"][0]
    assert not missing["built"] and "клона нет" in missing["why"], missing

    repo = CF.init(repo_path)
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files={"src/A.java": "class A {}\n"})
    no_index = json.loads(run("code_health.py", "--project", str(root), "--check", "--json",
                              cwd=root, expect_rc=1).stdout)["repos"][0]
    assert not no_index["built"], no_index

    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("code_health.py", "--project", str(root), "--check", cwd=root, expect_rc=0)


@test
def test_code_pipeline_two_repos_end_to_end(_t):
    """Два репозитория: поиск видит оба, граф связывает каждый со своими маршрутами и топиками."""
    root = make_project(_t)
    orders = CF.init(root / "Code" / "orders")
    CF.commit(orders, "init", "2026-01-01T00:00:00+00:00",
              files=_java_service("OrderController", "/api", "GetMapping", "/orders",
                                  "orders.accepted", "order_log"))
    billing = CF.init(root / "Code" / "billing")
    CF.commit(billing, "init", "2026-01-01T00:00:00+00:00",
              files=_java_service("InvoiceController", "/api", "GetMapping", "/invoices",
                                  "billing.issued", "invoice_log"))
    _config(root, {
        "orders": {"url": CF.url_of(orders), "branch": "main", "provider": "generic"},
        "billing": {"url": CF.url_of(billing), "branch": "main", "provider": "generic"},
    })
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)

    found = json.loads(run("code_find.py", "--project", str(root), "api", "--json",
                           cwd=root, expect_rc=0).stdout)
    assert {r["repo"] for r in found["results"]} == {"orders", "billing"}, found["results"]

    run("kb_code_graph.py", "--root", str(root), "--apply", cwd=root)
    run("kb_graph.py", "--export", "--apply", cwd=root)
    graph = json.loads((root / "AuroraKnowledgeDB/meta/graphify/graph.json").read_text(encoding="utf-8"))
    node_ids = {n["id"] for n in graph["nodes"]}
    assert {"repo:orders", "repo:billing"} <= node_ids, node_ids
    links = {(l["source"], l["relation"], l["target"]) for l in graph.get("links") or []}
    assert ("repo:orders", "содержит маршрут", "route:orders:GET /api/orders") in links, links
    assert ("repo:billing", "содержит маршрут", "route:billing:GET /api/invoices") in links, links
    assert ("repo:orders", "содержит топик", "topic:orders:orders.accepted") in links, links
    assert ("repo:billing", "содержит топик", "topic:billing:billing.issued") in links, links
