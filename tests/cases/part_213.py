#!/usr/bin/env python3
"""part_213 — LLM-арбитр проверки требований.

Проверяем opt-in слой поверх детерминированной проверки: модель видит только
доказательства, `found` принимается только с существующими цитатами, `partial`/
`not_found` требуют объяснения, ошибка модели не стирает детерминированный отчёт.
Модель в тестах подменяется, реальный шлюз не нужен.
"""
from __future__ import annotations

import json
import sys

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
import requirements_check as RQ  # noqa: E402


def _answer(verdict: str, citations=None, why: str = "") -> dict:
    return {"ok": True, "error": "", "text": json.dumps({
        "verdict": verdict, "citations": citations or [], "why": why})}


def _row(text: str, verdict: str, evidence=None) -> dict:
    return {"id": "R-1", "text": text, "source": "spec.md", "line": 1,
            "verdict": verdict, "evidence": evidence or [], "missing": ["detail"]}


@test
def test_llm_judge_accepts_only_evidence_and_explains_changes(_t):
    good = {"file": "src/orders.py", "line": 12, "kind": "symbol", "quote": "order_log"}
    result = {"documents": [{"path": "spec.md", "title": "Spec", "requirements": [
        _row("already found", "found", [good]),
        _row("needs one", "partial", [good]),
        _row("needs two", "not_found", []),
        _row("invented", "not_found", []),
        _row("over limit", "not_found", []),
    ]}], "summary": {}}
    answers = {
        "needs one": _answer("found", ["src/orders.py:12"], "подтверждено доказательством"),
        "needs two": _answer("partial", [], "в индексе есть только часть"),
        "invented": _answer("found", ["src/fake.py:99"], "выдумка"),
    }
    calls = []

    def ask(messages, deadline):
        text = messages[-1]["content"]
        calls.append(text)
        for key, value in answers.items():
            if key in text:
                return value
        return _answer("unsure", [], "нет решения")

    out = RQ.judge_requirements(result, ask=ask, max_calls=3)
    rows = out["documents"][0]["requirements"]
    assert [r["final_verdict"] for r in rows] == ["found", "found", "partial", "not_found", "not_found"], rows
    assert rows[0].get("agent") is None, "детерминированный found не должен звать модель"
    assert rows[1]["agent"]["accepted"] and rows[1]["agent"]["why"], rows[1]
    assert rows[2]["agent"]["accepted"] and rows[2]["final_verdict"] == "partial", rows[2]
    assert not rows[3]["agent"]["accepted"], rows[3]
    assert rows[4]["agent"].get("skipped") == "max_calls", rows[4]
    assert out["agent"] == {"calls": 3, "accepted": 2, "skipped": 1, "errors": 0}, out["agent"]
    assert len(calls) == 3, calls
    assert out["summary"]["found"] == 2 and out["summary"]["partial"] == 1, out["summary"]


@test
def test_llm_judge_survives_model_failure_and_invalid_json(_t):
    result = {"documents": [{"path": "spec.md", "requirements": [
        _row("model fails", "not_found", []),
        _row("bad json", "partial", []),
    ]}], "summary": {}}

    def ask(_messages, _deadline):
        return {"ok": False, "text": "", "error": "gateway down"} if "fails" in _messages[-1]["content"] \
            else {"ok": True, "text": "not json", "error": ""}

    out = RQ.judge_requirements(result, ask=ask, max_calls=10)
    rows = out["documents"][0]["requirements"]
    assert [r["final_verdict"] for r in rows] == ["not_found", "partial"], rows
    assert "gateway down" in rows[0]["agent"]["error"], rows[0]
    assert "JSON" in rows[1]["agent"]["error"], rows[1]
    assert out["agent"]["errors"] == 2, out["agent"]


@test
def test_requirements_check_agent_is_opt_in_and_keeps_deterministic_default(_t):
    root = make_project(_t)
    (root / "spec.md").write_text(
        "## Требования\n- [ ] Панель показывает процент\n- [ ] Кнопка сохраняет отчёт\n",
        encoding="utf-8")
    called = []

    def ask(_messages, _deadline):
        called.append(1)
        return _answer("partial", [], "в индексе нет нужного символа")

    deterministic = RQ.check(str(root), ["spec.md"])
    assert deterministic["summary"]["not_found"] == 2, deterministic["summary"]
    assert not deterministic.get("agent") and not called, "без --agent модель вызываться не должна"
    with_agent = RQ.check(str(root), ["spec.md"], agent=True, max_calls=1, ask=ask)
    assert called == [1], called
    assert with_agent["summary"]["partial"] == 1 and with_agent["summary"]["not_found"] == 1, with_agent["summary"]
    assert with_agent["agent"] == {"calls": 1, "accepted": 1, "skipped": 1, "errors": 0}, with_agent["agent"]
    assert with_agent["documents"][0]["requirements"][0]["final_verdict"] == "partial"
    assert with_agent["documents"][0]["requirements"][1].get("agent", {}).get("skipped") == "max_calls"


@test
def test_requirements_agent_ui_api_and_catalogs_are_wired(_t):
    view = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    html = (KIT / "cockpit/modules/work/view.html").read_text(encoding="utf-8")
    server = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for token in ("reqAgent", "agent_summary", "max_calls", "work.req.agent_why"):
        assert token in view, token
    assert 'id="reqAgent"' in html and 'id="reqAgentCalls"' in html, html
    assert 'RQ.check(project, paths, bool(payload.get("agent"))' in server, "API не передаёт agent"
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/work/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        for key in ("work.req.agent", "work.req.agent_hint", "work.req.agent_summary",
                    "work.req.checking_agent", "work.req.agent_why", "work.req.agent_error"):
            assert data.get(key), (lang, key)


@test
def test_requirements_export_includes_agent_decisions(_t):
    result = {"summary": {"total": 1, "found": 1, "partial": 0, "not_found": 0, "percent": 100},
              "documents": [{"path": "spec.md", "title": "Spec", "requirements": [
                  {**_row("one", "partial", []), "final_verdict": "found",
                   "agent": {"accepted": True, "verdict": "found", "why": "подтверждено"}}]}],
              "agent": {"calls": 1, "accepted": 1, "skipped": 0, "errors": 0}}
    md = RQ.render_md(result)
    assert "Модель: вызовов 1" in md and "LLM: подтверждено" in md, md
