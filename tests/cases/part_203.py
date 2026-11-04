#!/usr/bin/env python3
"""part_203 — agent:codecheck: модельный арбитр только для спорных критериев (1.210.1)."""
from __future__ import annotations

import sys

from harness import SCRIPTS, test

sys.path.insert(0, str(SCRIPTS))
import code_check as CC  # noqa: E402


def _report(verdict: str = "partial") -> dict:
    return {
        "story": "US-1",
        "found": True,
        "criteria": [{
            "id": "US-1/criteria/1",
            "text": "POST /api/orders пишет в audit_log.",
            "verdict": verdict,
            "evidence": [{"file": "src/X.java", "line": 1, "kind": "route",
                          "quote": "POST /api/orders"}],
            "missing": ["audit_log"],
        }],
        "summary": {"total": 1, "found": 0, "partial": 1, "not_found": 0, "repos": ["orders"]},
    }


def _ask(text: str):
    def ask(_messages, _deadline):
        return {"ok": True, "text": text, "error": ""}
    return ask


@test
def test_agent_accepts_found_with_valid_citation(_t):
    """Модель подтверждает found цитатой из доказательств — final_verdict найден."""
    report = CC.judge(_report(), _ask(
        '{"verdict":"found","citations":["src/X.java:1"],"why":"route covers it"}'))
    crit = report["criteria"][0]
    assert crit["agent"]["accepted"] and crit["final_verdict"] == "found", crit
    assert report["summary"]["found"] == 1 and report["summary"]["partial"] == 0
    assert report["agent"] == {"calls": 1, "accepted": 1}


@test
def test_agent_rejects_invented_citation(_t):
    """Выдуманная координата не принимается: остаётся детерминированный partial."""
    report = CC.judge(_report(), _ask(
        '{"verdict":"found","citations":["src/Y.java:9"],"why":"invented"}'))
    crit = report["criteria"][0]
    assert not crit["agent"]["accepted"] and crit["final_verdict"] == "partial", crit
    assert report["summary"]["partial"] == 1


@test
def test_agent_rejects_invalid_json(_t):
    """Не JSON — ошибка в agent, вердикт не меняется."""
    report = CC.judge(_report(), _ask("не json"))
    crit = report["criteria"][0]
    assert "не JSON" in crit["agent"]["error"], crit
    assert crit["final_verdict"] == "partial"


@test
def test_agent_skips_deterministic_found(_t):
    """Детерминированный found модель не трогает и не вызывает."""
    def ask(_messages, _deadline):
        raise AssertionError("модель не должна вызываться для found")
    report = CC.judge(_report("found"), ask)
    assert report["agent"]["calls"] == 0
    assert report["criteria"][0]["final_verdict"] == "found"


@test
def test_agent_respects_max_calls(_t):
    """Лимит вызовов: второй спорный критерий помечается skipped."""
    report = _report()
    report["criteria"].append({**report["criteria"][0], "id": "US-1/criteria/2"})
    report["summary"]["total"] = 2
    result = CC.judge(report, _ask(
        '{"verdict":"partial","citations":[],"why":"no"}'), max_calls=1)
    assert result["agent"]["calls"] == 1, result["agent"]
    assert result["criteria"][1]["agent"]["skipped"] == "max_calls"
