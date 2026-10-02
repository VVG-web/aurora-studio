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
        M.run = lambda project, script, args, timeout=120, **kw: seen.append((script, list(args))) or "ответ"
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


@test
def test_mcp_reports_a_failed_tool_as_an_error_not_as_knowledge(tmp: Path):
    """Отказ инструмента приходит с `isError: true` и внятным текстом, а не как «знание».

    Неизвестный инструмент, карточка, которой нет, пустой запрос, режим вне перечня и
    сбой скрипта отдавались обычным текстом, и ассистент читал их как ответ базы. А
    недопустимый `mode` доезжал до argparse и возвращался целой справкой `usage:` — чтобы
    понять, что не так, надо было читать флаги чужого скрипта. Перечень режимов теперь
    проверяется на входе, и в тексте названы допустимые.
    """
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified",
         type="concept")
    calls = [("unknown", {}),
             ("kb_card", {"name": "Нет-такой-карточки"}),
             ("kb_card", {"name": ""}),
             ("kb_search", {"query": "   "}),
             ("kb_context", {"topic": "обеспечение", "mode": "выдумка"}),
             ("kb_context", {"topic": ""}),
             ("kb_ask", {"question": ""}),
             ("kb_search", {"query": "обеспечение"}),
             ("kb_card", {"name": "Обеспечение"})]
    lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
             for i, (name, args) in enumerate(calls, 1)]
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=180)
    res = {m["id"]: m["result"] for m in map(json.loads, filter(str.strip, cp.stdout.splitlines()))}
    assert len(res) == len(calls), f"ответили не на все вызовы: {sorted(res)}\n{cp.stderr[-300:]}"

    def text(i):
        return res[i]["content"][0]["text"]
    for i, why in ((1, "неизвестный инструмент"), (2, "нет карточки"), (3, "пустое имя"),
                   (4, "пустой запрос"), (5, "режим вне перечня"), (6, "пустая тема"),
                   (7, "пустой вопрос")):
        assert res[i].get("isError") is True, f"{why}: отказ без isError — {res[i]}"
    assert "usage:" not in text(5), f"режим вне перечня вернул справку argparse: {text(5)[:200]}"
    assert "generate" in text(5) and "review" in text(5), \
        f"в отказе не названы допустимые режимы: {text(5)}"
    assert "kb_search" in text(2), "про карточку, которой нет, не подсказано, чем искать"
    # код 1 у движка — «отработала и нашла, что сказать»: по теме ничего нет — это ответ базы, а не сбой;
    # у `kb_ask` код 1 значит «ни одна модель не ответила», и там это отказ
    fake = tmp / "fake"
    (fake / ".opencode/scripts").mkdir(parents=True)
    # Тексты латиницей: эти скрипты не переключают вывод на UTF-8, как движок, и на Windows
    # труба в cp1252 не пропустила бы русский (см. тест о кодировке дочерних процессов).
    (fake / ".opencode/scripts/exit1.py").write_text("print('nothing found'); raise SystemExit(1)\n",
                                                     encoding="utf-8")
    (fake / ".opencode/scripts/exit2.py").write_text("print('broken'); raise SystemExit(2)\n",
                                                     encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    assert M.run(str(fake), "exit1.py", []) == "nothing found", "код 1 принят за сбой"
    for script, kw in (("exit2.py", {}), ("exit1.py", {"fail_from": 1})):
        try:
            M.run(str(fake), script, [], **kw)
        except M.ToolError as e:
            assert "broken" in str(e) or "nothing found" in str(e), str(e)
        else:
            raise AssertionError(f"{script} {kw}: сбой не стал отказом")
    assert not res[8].get("isError") and "Обеспечение" in text(8), f"обычный поиск: {res[8]}"
    assert not res[9].get("isError") and "Правила обеспечения" in text(9), f"обычная карточка: {res[9]}"
