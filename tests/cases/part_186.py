#!/usr/bin/env python3
"""part_186 — поиск по индексу кода (CR-11).

Проверяем, что `code:find` отвечает на маршруты, символы, колонки, топики, ключи и коммиты,
умеет фильтр вида, пустую выдачу без ошибки и держит скорость на большом индексе.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402
import codeidx.query as query  # noqa: E402
import codeidx.store as store_mod  # noqa: E402


def _config(root: Path, repos: dict, extra: str = "") -> None:
    path = root / "aurora.config.yaml"
    text = path.read_text(encoding="utf-8") + extra
    path.write_text(CR.write_block(text, repos), encoding="utf-8")


def _cli(script: str, root: Path, *args, expect_rc=0):
    return run(script, "--project", str(root), *args, cwd=root, expect_rc=expect_rc)


def _prepare_order(root: Path) -> None:
    repo = CF.build("order-intake", root / "Code" / "order-intake")
    _config(root, {"order-intake": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}},
            extra="jira_keys: [BENCH]\n")
    _cli("code_index.py", root, "--index", "--apply")


@test
def test_code_find_finds_contract_objects(_t):
    """Поиск находит маршрут, DTO, колонку, топик, ключ задачи и коммиты."""
    root = make_project(_t)
    _prepare_order(root)
    checks = {
        "OrderRequest": "symbol",
        "external_id": "column",
        "orders.accepted": "topic",
        "BENCH-1": "commit",
        "POST /api/orders": "route",
        "src/main/java/com/example/orders/OrderController.java": "file",
    }
    for text, kind in checks.items():
        cp = _cli("code_find.py", root, text, "--json")
        data = json.loads(cp.stdout)
        assert data["results"], (text, cp.stdout)
        assert any(r["type"] == kind for r in data["results"]), (text, kind, data["results"][:5])


@test
def test_code_find_kind_filter_and_empty(_t):
    """Фильтр вида работает, неизвестное имя даёт пустую выдачу и код 0."""
    root = make_project(_t)
    _prepare_order(root)
    cp = _cli("code_find.py", root, "orders", "--kind", "topic", "--json")
    data = json.loads(cp.stdout)
    assert data["results"] and all(r["type"] == "topic" for r in data["results"]), data["results"]
    empty = _cli("code_find.py", root, "zzz_no_such_name", "--json")
    assert json.loads(empty.stdout)["results"] == []
    human = _cli("code_find.py", root, "zzz_no_such_name")
    assert "ничего не найдено" in human.stdout, human.stdout


@test
def test_code_find_speed_on_large_index(_t):
    """Поиск по индексу 2000 файлов укладывается в 200 мс."""
    root = make_project(_t)
    repo = CF.gen_mono(root / "Code" / "small", n=2000)
    _config(root, {"small": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    _cli("code_index.py", root, "--index", "--apply")
    db = root / ".aurora" / "cache" / "code" / "small" / "index.sqlite"
    with store_mod.Store(db) as st:
        started = time.perf_counter()
        rows = query.find(st, "f00042", limit=20)
        elapsed = time.perf_counter() - started
        assert rows, "поиск не нашёл файл f00042"
        assert elapsed < 0.2, elapsed
