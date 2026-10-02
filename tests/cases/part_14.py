"""Проверки движка Aurora, часть 14: обзор версии 1.148.1 — навыки и MCP, дубли, запись и удаление.

Каркас и помощники — tests/harness.py. Здесь каждая проверка — на одну находку обзора.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from harness import (  # noqa: F401
    KIT,
    SCRIPTS,
    card,
    make_project,
    run,
    set_home,
    test,
)


@test
def test_mcp_ask_never_writes_a_conversation_into_the_base(tmp: Path):
    """`kb_ask` через MCP не оставляет в базе журнал разговора: MCP только читает.

    Инструмент звал `agent_runner.py --task ask` без `--no-journal`, а команда дописывает
    разговор в `AuroraKnowledgeDB/meta/ask/` — каждый вопрос чужого ассистента рождал файл в
    базе и в рабочем дереве git. Два противоречия сразу: документ сервера обещает «писать в
    базу через MCP нельзя», а следующая пишущая команда отказывалась идти по грязному дереву.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    seen = []
    real = M.run
    try:
        M.run = lambda project, script, args, timeout=120: seen.append((script, list(args))) or "ответ"
        assert M.call_tool(str(tmp), "kb_ask", {"question": "Что такое заявка?"}) == "ответ"
    finally:
        M.run = real
    script, args = seen[0]
    assert script == "agent_runner.py" and "ask" in args, seen
    assert "--no-journal" in args, \
        f"вопрос через MCP запишется в журнал разговоров базы: {args}"


@test
def test_mcp_survives_a_line_that_is_not_a_json_rpc_object(tmp: Path):
    """Строка, не похожая на запрос JSON-RPC, не обрывает сессию с ассистентом.

    JSON, который не объект (`[]`, `5`, `null`), и пакет запросов — массив объектов — падали
    на `msg.get` с `AttributeError`: сервер выходил, и ассистент терял базу посреди работы.
    Пакет теперь разбирается по элементам, всё остальное — ответ «Invalid Request» с
    `id: null`, как велит JSON-RPC, и следующие строки обслуживаются как обычно.
    """
    root = make_project(tmp)
    lines = ["[1, 2]", "5", "null", "[]", '"строка"',
             json.dumps([{"jsonrpc": "2.0", "id": 7, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 8, "method": "ping"}]),
             json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})]
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    assert cp.returncode == 0, f"сервер упал на кривой строке:\n{cp.stderr[-400:]}"
    got = [json.loads(l) for l in cp.stdout.splitlines() if l.strip()]
    answered = {m["id"] for m in got if m.get("id") is not None}
    assert {7, 8, 9} <= answered, f"запросы после кривых строк остались без ответа: {got}"
    bad = [m for m in got if m.get("id") is None]
    assert bad and all(m["error"]["code"] == -32600 for m in bad), \
        f"кривые строки не получили Invalid Request: {bad}"
    assert len(bad) == 6, f"ждали по ответу на 1, 2, 5, null, [] и строку ({len(bad)}): {bad}"
