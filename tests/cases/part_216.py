#!/usr/bin/env python3
"""part_216 - интеграция бюджета, стадий и инструментов памяти.

Проверяем, что при context_architecture.enabled:
- планировщик получает TOC и ограниченный рабочий набор;
- tools и MCP по умолчанию выключены, если задача их не просит;
- input_limit и context_store доходят до вызова;
- «Спросить» получает тот же сокращённый рабочий набор.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import KIT, card, kit_with_mcp, make_project, test

sys.path.insert(0, str(KIT / "scripts"))


def _project(tmp: Path) -> Path:
    root = make_project(tmp)
    (root / "Templates" / "AC.md").write_text(
        "# AC\n\n## Критерии приёмки\n\n## История изменений\n", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + """
context_architecture:
  enabled: true
  compression:
    enabled: true
    threshold: 0.50
    threshold_tokens: 0
    target_ratio: 0.25
  reserves:
    planner: 2000
    worker: 2000
    critic: 1000
    qa: 1000
    tools: 1000
    safety: 500
artifacts:
  ac:
    title: "AC"
    template: "Templates/AC.md"
    out: "Artifacts/ac"
    mcp: "mcp-atlassian"
""", encoding="utf-8")
    for i in range(15):
        card(root, f"Concepts/Заявка-{i}.md",
             f"Заявка номер {i} проходит согласование. Срок {10 + i} дней. [[Заявка-{(i + 1) % 15}]]",
             status="knowledge", kind="knowledge")
    return root


@test
def test_context_aware_planner_uses_toc_without_tools_or_mcp(_t):
    tmp = Path(_t)
    root = _project(tmp)
    kit_with_mcp(tmp, {"mcp-atlassian": {"command": "npx"}})
    import importlib
    R = importlib.import_module("agent_runner")
    cfg = __import__("agent_core").parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    seen = {}
    calls = []

    def fake(cfg_, role, messages, **kw):
        calls.append((role, kw))
        seen[role] = messages[0]["content"]
        if role == "planner":
            return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                    "text": json.dumps({"questions": [{"q": "Какой срок?", "why": "",
                                                       "rec": "10 дней"}]})}
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [], "text": "# Док"}

    res = R.run_make(cfg, str(root), "ac", "Сделай критерии приёмки по заявке", "", "", False,
                     call=fake, momus=False)
    assert res["ok"] and res["stage"] == "planning", res
    planner_kw = next(kw for role, kw in calls if role == "planner")
    assert planner_kw.get("tools") is False, planner_kw
    assert planner_kw.get("mcp_active") == [], planner_kw
    assert int(planner_kw.get("input_limit") or 0) > 0, planner_kw
    store_path = Path(planner_kw.get("context_store") or "")
    assert store_path.is_file(), store_path
    assert "## Оглавление остальных карточек" in seen["planner"], seen["planner"][:500]
    assert "## Заявка-0" in seen["planner"], "первая карточка не попала в рабочий набор"
    assert seen["planner"].count("## Заявка-") <= 12, "в промпт попал весь пак"


@test
def test_context_aware_ask_uses_reduced_pack_and_limit(_t):
    tmp = Path(_t)
    root = _project(tmp)
    import importlib
    R = importlib.import_module("agent_runner")
    cfg = __import__("agent_core").parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    seen = {}

    def fake(cfg_, role, messages, **kw):
        seen[role] = (messages[0]["content"], kw)
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                "text": "Ответ [[Заявка-0]]"}

    res = R.run_ask(cfg, str(root), "Какой срок у заявки?", "ask", 40, call=fake,
                    momus=False)
    assert res["ok"], res
    prompt, kw = seen["worker"]
    assert "## Оглавление остальных карточек" in prompt, prompt[:500]
    assert int(kw.get("input_limit") or 0) > 0, kw
    assert res.get("context", {}).get("ask_tokens", 0) > 0, res.get("context")


@test
def test_context_aware_worker_uses_stage_pack(_t):
    tmp = Path(_t)
    root = _project(tmp)
    import importlib
    R = importlib.import_module("agent_runner")
    cfg = __import__("agent_core").parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    seen = {}
    calls = []

    def fake(cfg_, role, messages, **kw):
        calls.append((role, kw, messages[0]["content"]))
        seen[role] = messages[0]["content"]
        if role == "planner":
            return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                    "text": json.dumps({"questions": [], "plan": "1. Критерии"})}
        if role == "critic":
            return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                    "text": json.dumps({"ok": True, "issues": []})}
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                "text": "# Док\n\n[[Заявка-0]]"}

    res = R.run_make(cfg, str(root), "ac", "Сделай критерии приёмки по заявке", "", "", True,
                     call=fake, momus=False)
    assert res["ok"] and res["stage"] == "done", res
    worker_call = next(item for item in calls if item[0] == "worker")
    assert "## Оглавление остальных карточек" in worker_call[2], worker_call[2][:500]
    assert worker_call[2].count("## Заявка-") <= 14, "документу отдан весь пак"
    assert int(worker_call[1].get("input_limit") or 0) > 0, worker_call[1]


@test
def test_per_request_limit_is_wired_in_adapter_and_core(_t):
    core = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    adapter = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert 'payload["input_limit"]' in core and "context_store" in core
    assert "per_request_input_tokens_limit" in adapter
    assert "register_tools(agent, task[\"tools\"], task.get(\"context_store\")" in adapter
    for name in ("ctx_search", "ctx_get", "ctx_expand", "ctx_pin", "ctx_drop", "ctx_summarize"):
        assert f"def {name}(" in adapter, name
