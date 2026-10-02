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
