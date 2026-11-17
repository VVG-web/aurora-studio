"""Проверки движка Aurora, часть 242: раздел «Замеры» — сеть и токены (1.231.7).

Каркас и помощники — tests/harness.py. «Замеры» — kit-level раздел после Cron и перед
Справкой: снимки сети/VPN по прогонам и расход токенов по проектам, ролям и моделям.
Это не «Отчёты» проекта (те — дашборды Jira/Confluence для руководителя).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, panel_sources, test  # noqa: F401

for _p in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@test
def test_metrics_section_sits_after_cron_and_needs_no_project(_t):
    """Манифест: machine/36 — между Cron (35) и Справкой (40), без выбранного проекта."""
    man = json.loads((KIT / "cockpit/modules/metrics/module.json").read_text(encoding="utf-8"))
    assert man["id"] == "metrics" and man["group"] == "machine" and man["order"] == 36, man
    assert man["needs"] == [] and man["for"] == "1.231.7", man
    assert man["name"]["ru"] == "Замеры", man
    src = panel_sources()
    for need in ("/api/metrics", "metrics.net_title", "metrics.tokens_title", "metrics.col.model"):
        assert need in src, need


@test
def test_net_reports_are_listed_across_projects_without_paths(tmp: Path):
    """Снимки сети читаются по всем проектам; в строке только имя проекта, без путей и адресов."""
    import net_report as NR
    (tmp / "a").mkdir()
    (tmp / "b").mkdir()
    a, b = make_project(tmp / "a"), make_project(tmp / "b")
    snap = {
        "id": "run-1", "cmd": "agent:distill", "seconds": 720,
        "snapshots": [{
            "at": "2026-11-17T10:00:00Z", "elapsed_s": 0, "state": "offline",
            "requirements": {"llm": True},
            "system_vpn": {"services": [{"name": "TestVPN", "kind": "vpn"}]},
            "profiles": [{"name": "Probe", "state": "offline", "required": True}],
            "providers": [{"id": "gw", "name": "Шлюз", "state": "offline", "required": True}],
            "blocked": [{"code": "profile_offline", "name": "Probe"}],
        }],
    }
    _write(a / "AuroraKnowledgeDB/meta/net-runs.d/u-m/run-1.json", snap)
    _write(b / "AuroraKnowledgeDB/meta/net-runs.d/u-m/run-2.json", dict(snap, id="run-2",
                                                                       cmd="kb:embed"))
    rows = NR.list_reports([str(a), str(b)], limit=10)
    assert {r["project"] for r in rows} == {a.name, b.name}, rows
    assert {r["run"] for r in rows} == {"run-1", "run-2"}, rows
    got = next(r for r in rows if r["run"] == "run-1")
    assert got["vpn"] == ["TestVPN"] and got["blocked_s"] >= 0, got
    text = json.dumps(rows, ensure_ascii=False)
    assert str(tmp) not in text and "AuroraKnowledgeDB" not in text, text


@test
def test_usage_log_counts_tokens_by_project_role_and_model(tmp: Path):
    """Запись прогона → сводка по проекту, роли и модели; суммы сходятся с итогом."""
    import usage_log as UL
    root = make_project(tmp)
    UL.record(str(root), "run-1", {
        "kind": "agent", "task": "distill", "seconds": 12,
        "calls": 3, "tokens_in": 300, "tokens_out": 90, "errors": 0,
        "by_role": {"planner": {"calls": 2, "tokens_in": 200, "tokens_out": 60, "seconds": 8},
                    "qa": {"calls": 1, "tokens_in": 100, "tokens_out": 30, "seconds": 4}},
        "by_model": {"m1": {"calls": 2, "tokens_in": 200, "tokens_out": 60, "seconds": 8},
                     "m2": {"calls": 1, "tokens_in": 100, "tokens_out": 30, "seconds": 4}},
        "by_pair": {"planner|m1": {"calls": 2, "tokens_in": 200, "tokens_out": 60, "seconds": 8},
                    "qa|m2": {"calls": 1, "tokens_in": 100, "tokens_out": 30, "seconds": 4}},
    })
    got = UL.summary([str(root)], days=7, now=time.time())
    assert got["records"] == 1 and got["old"] == 0, got
    assert got["totals"]["tokens_in"] == 300 and got["totals"]["tokens_out"] == 90, got
    rows = {(r["project"], r["role"], r["model"]) for r in got["rows"]}
    assert rows == {(root.name, "planner", "m1"), (root.name, "qa", "m2")}, rows
    assert {r["name"] for r in got["roles"]} == {"planner", "qa"}, got["roles"]
    assert {r["name"] for r in got["models"]} == {"m1", "m2"}, got["models"]
    # старый прогон без разбивки — только итог, без выдуманных ролей
    UL.record(str(root), "run-old", {"kind": "agent", "task": "build", "calls": 1,
                                     "tokens_in": 10, "tokens_out": 5})
    got = UL.summary([str(root)], days=7, now=time.time())
    assert got["old"] == 1 and got["records"] == 2, got
    assert got["totals"]["tokens_in"] == 310, got
    # история хранится 90 дней: следующая запись убирает старое
    folder = UL._folder(str(root))
    old = folder / "old.json"
    old.write_text("{}", encoding="utf-8")
    os.utime(old, (time.time() - 91 * 86400, time.time() - 91 * 86400))
    UL.record(str(root), "run-new", {"kind": "agent", "task": "distill", "calls": 0})
    assert not old.exists(), "запись старше 90 дней осталась"
    text = json.dumps(got, ensure_ascii=False)
    assert str(tmp) not in text and "prompt" not in text and "key" not in text.lower(), text


@test
def test_metrics_state_reads_net_and_tokens_without_project(tmp: Path):
    """API «Замеров» работает без выбранного проекта и отдаёт обе части."""
    import aurora_cockpit as ck
    import net_report as NR
    import usage_log as UL
    root = make_project(tmp)
    _write(root / "AuroraKnowledgeDB/meta/net-runs.d/u-m/run-1.json", {
        "id": "run-1", "cmd": "agent:distill", "seconds": 60,
        "snapshots": [{"at": "2026-11-17T10:00:00Z", "state": "ok",
                       "requirements": {"llm": True}, "system_vpn": {"services": []}}],
    })
    UL.record(str(root), "run-1", {"kind": "agent", "task": "distill", "calls": 1,
                                   "tokens_in": 10, "tokens_out": 2,
                                   "by_pair": {"planner|m1": {"calls": 1, "tokens_in": 10,
                                                             "tokens_out": 2}}})
    with __import__("unittest").mock.patch.object(NR, "list_reports",
                                                  wraps=NR.list_reports):
        got = ck.metrics_state([str(root)], days=7)
    assert got["net"] and got["tokens"]["records"] == 1, got
    assert got["tokens"]["rows"][0]["role"] == "planner", got
    assert str(tmp) not in json.dumps(got, ensure_ascii=False), got
