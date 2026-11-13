#!/usr/bin/env python3
"""part_217 - регрессии найденных проблем контекстного слоя.

1. tool_limit не повторяет тот же tool-цикл: планировщик делает второй вызов
   без инструментов и без MCP.
2. Контекстные регрессионные тесты зарегистрированы в общем прогоне, поэтому
   релизный гейт запускает их, а не только ручной набор.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import KIT, test

sys.path.insert(0, str(KIT / "scripts"))


@test
def test_tool_limit_retries_planner_without_tools(_t):
    from cases.part_216 import _project
    import agent_core as A
    import agent_runner as R

    root = _project(Path(_t))
    cfg = A.parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    calls = []

    def fake(cfg_, role, messages, **kw):
        calls.append((role, kw, messages[0]["content"]))
        if role == "planner" and len([c for c in calls if c[0] == "planner"]) == 1:
            return {"ok": True, "text": "", "finish": "tool_limit",
                    "backend": 1, "model": "m", "tps": 9, "log": []}
        if role == "planner":
            return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                    "text": json.dumps({"questions": [{"q": "Какой срок?", "why": "",
                                                       "rec": "10 дней"}]})}
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [], "text": ""}

    res = R.run_make(cfg, str(root), "ac", "Найди критерии приёмки по заявке", "", "", False,
                     call=fake, momus=False)
    assert res["ok"] and res["stage"] == "planning", res
    planners = [c for c in calls if c[0] == "planner"]
    assert len(planners) == 2, planners
    assert planners[0][1].get("tools") is True, planners[0][1]
    assert planners[1][1].get("tools") is False, planners[1][1]
    assert planners[1][1].get("mcp_active") == [], planners[1][1]
    # input_limit — ограничение Pydantic AI; прямой HTTP его не исполняет.
    # С ним повтор снова потребовал бы адаптер и не дошёл до модели.
    assert int(planners[1][1].get("input_limit") or 0) == 0, planners[1][1]


@test
def test_context_regressions_are_registered_in_release_run(_t):
    source = (KIT / "tests/run_tests.py").read_text(encoding="utf-8")
    for part in ("part_214", "part_215", "part_216", "part_217"):
        assert f"'{part}'" in source, f"{part} не зарегистрирован в общем прогоне"
