"""Проверки движка Aurora, часть 242: раздел «Замеры» — сеть и токены (1.231.7).

Каркас и помощники — tests/harness.py. «Замеры» — kit-level раздел после Cron и перед
Справкой: снимки сети/VPN по прогонам и расход токенов по проектам, ролям и моделям.
Это не «Отчёты» проекта (те — дашборды Jira/Confluence для руководителя).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
import unittest.mock
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, panel_sources, test  # noqa: F401

for _p in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@test
def test_metrics_section_sits_after_cron_and_needs_no_project(_t):
    """Манифест: machine/36 — между Cron (35) и Справкой (40), без выбранного проекта."""
    man = json.loads((KIT / "cockpit/modules/metrics/module.json").read_text(encoding="utf-8"))
    assert man["id"] == "metrics" and man["group"] == "machine" and man["order"] == 36, man
    kit_version = (KIT / "VERSION").read_text(encoding="utf-8").strip()
    assert man["needs"] == [] and man["for"] == kit_version, man
    assert man["name"]["ru"] == "Замеры", man
    src = panel_sources()
    for need in ("/api/metrics", "metrics.net_title", "metrics.tokens_title", "metrics.col.model"):
        assert need in src, need


@test
def test_metrics_columns_lifecycle_and_write_site_are_declared(_t):
    """Таблица «Замеров» берёт подписи из каталога, раздел объявил жизнь, а запись замера
    названа в реестре мест записи — иначе падают три разные проверки сразу.

    Случай из 1.231.7: ключи были `metrics.col_*`, а тест каталогов видит только приставку с
    точкой (`t("metrics.col." + name)`) и считал `metrics.col_` отсутствующим ключом. Ещё
    раздел забыл `export const lifecycle = 1`, а `scripts/usage_log.py:record` не был назван
    в `tests/card_write_sites.json`.
    """
    folder = KIT / "cockpit/modules/metrics"
    js = (folder / "view.js").read_text(encoding="utf-8")
    assert re.search(r"^export const lifecycle\s*=\s*1\s*;", js, re.M), "раздел не объявил lifecycle"
    assert 't("metrics.col." +' in js, "динамическая приставка колонок должна оканчиваться точкой"
    cols = set(re.findall(r'"metrics\.col\.([a-z_]+)"', js))
    assert cols, "колонки не названы"
    for lang in ("ru", "en"):
        catalog = json.loads((folder / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for col in sorted(cols):
            assert f"metrics.col.{col}" in catalog, (lang, col)
    sites = json.loads((KIT / "tests/card_write_sites.json").read_text(encoding="utf-8"))
    rec = sites["sites"].get("scripts/usage_log.py:record") or {}
    assert rec.get("kind") == "other" and rec.get("git") == "shard" and rec.get("reason"), rec


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
def test_usage_log_timestamps_are_utc_and_run_ids_cannot_escape(tmp: Path):
    """Время замера — в UTC с `Z`, а имя прогона не может вывести запись за папку `usage.d`.

    Случай из 1.231.7: `usage_log` писал местное время, и проверка «время в UTC, показывается
    в местном» краснела. Заодно держим границу имени: `../escape` — не прогон, а попытка
    записать мимо общей части.
    """
    import usage_log as UL
    root = make_project(tmp)
    started = 1_700_000_000.0                       # 2023-11-14T22:13:20Z
    UL.record(str(root), "run-utc", {"started": started, "calls": 1})
    folder = UL._folder(str(root))
    rec = json.loads(next(folder.glob("run-utc.json")).read_text(encoding="utf-8"))
    assert rec["at"].endswith("Z"), rec["at"]
    assert rec["started"] == "2023-11-14T22:13:20Z", rec["started"]
    assert UL.summary([str(root)], days=7, now=time.time())["since"].endswith("Z")
    assert UL.record(str(root), "../escape", {"calls": 1}) == {}
    assert not (folder.parent / "escape.json").exists()


@test
def test_usage_log_keeps_participants_apart(tmp: Path):
    """Два участника и две машины пишут в разные папки: общий git-файл не конфликтует."""
    import usage_log as UL
    root = make_project(tmp)
    real_actor, real_machine = UL.actor, UL._machine_id
    try:
        UL.actor, UL._machine_id = (lambda _p: ("u1", "U1")), (lambda: "m1")
        first = UL._folder(str(root))
        UL.actor, UL._machine_id = (lambda _p: ("u2", "U2")), (lambda: "m2")
        second = UL._folder(str(root))
    finally:
        UL.actor, UL._machine_id = real_actor, real_machine
    assert first != second, (first, second)
    assert (first.name, second.name) == ("u1-m1", "u2-m2"), (first, second)
    assert first.parent == second.parent and first.parent.name == "usage.d", (first, second)


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


@test
def test_metrics_state_accepts_project_cards_from_the_panel(tmp: Path):
    """`/api/metrics` передаёт карточки `find_projects`, а не строки путей.

    Случай 1.231.7: открытие вкладки «Замеры» показывало «отчёты сети не прочитаны:
    TypeError ... not 'dict'» — `Path()` получал карточку проекта. Проверяем оба вида:
    карточки от панели и строки, которыми пользуются тесты и CLI.
    """
    import aurora_cockpit as ck
    root = make_project(tmp)
    cards = ck.find_projects([str(tmp)])
    assert cards and cards[0]["path"] == str(root), cards
    for projects in (cards, [str(root)]):
        got = ck.metrics_state(projects, days=7)
        assert got["error"] == "", got
        assert not (got.get("tokens") or {}).get("error"), got["tokens"]
        assert got["days"] == 7 and got["tokens"]["records"] == 0, got


@test
def test_usage_log_schema2_round_trip(tmp: Path):
    """schema 2 пишет все поля, nullable остаётся null, project_id берётся из slug."""
    import usage_log as UL
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Test"\n  slug: "prj-a"\n', encoding="utf-8")
    rec = UL.record(str(root), "run-schema2", {
        "kind": "agent", "task": "distill",
        "started": 1_700_000_000.0, "seconds": 12.5, "estimated": True,
        "calls": 3, "calls_failed": 1, "requests": 4,
        "tokens_in": 30, "tokens_out": 12,
        "tokens_failed_in": 2, "tokens_failed_out": 1,
        "cached_in": 0, "reasoning_out": None,
        "cache_hits": 2, "usage_missing_calls": 1,
        "gen_seconds": 4.25, "errors": 2,
        "by_cell": [{
            "provider": "gw1", "provider_name": "Gateway",
            "role": "worker", "model": "m1",
            "calls": 3, "calls_failed": 1, "requests": 4,
            "tokens_in": 30, "tokens_out": 12,
            "cached_in": 0, "reasoning_out": None, "gen_seconds": 4.25,
        }],
        "by_role": {"worker": {"calls": 3, "tokens_in": 30, "tokens_out": 12}},
    })
    assert rec["schema"] == 2 and rec["project_id"] == "prj-a", rec
    assert rec["started"] == "2023-11-14T22:13:20Z", rec["started"]
    assert rec["cached_in"] == 0 and rec["reasoning_out"] is None, rec
    assert rec["by_cell"][0]["provider"] == "gw1" and rec["by_cell"][0]["calls"] == 3, rec
    stored = json.loads((UL._folder(str(root)) / "run-schema2.json").read_text(encoding="utf-8"))
    assert stored == rec, (stored, rec)
    text = json.dumps(stored, ensure_ascii=False)
    assert '"key"' not in text and '"cost"' not in text and '"currency"' not in text, text
    missing = UL.record(str(root), "run-missing-usage",
                        {"calls": 1, "usage_missing_calls": 1})
    assert missing["estimated"] is True, missing


@test
def test_usage_log_reads_both_schemas_and_dedups_project_run(tmp: Path):
    """iter_records читает schema 1 и 2, summary схлопывает (project_id, run) по новому at."""
    import usage_log as UL
    root = make_project(tmp)
    folder = UL._folder(str(root))
    other = folder.parent / "u2-m2"
    other.mkdir(parents=True)
    now = time.time()
    _write(other / "legacy.json", {
        "schema": 1, "at": _stamp(now - 3 * 86400), "project": "Test",
        "run": "legacy", "calls": 1, "tokens_in": 1, "tokens_out": 0,
    })
    _write(folder / "dup-old.json", {
        "schema": 2, "at": _stamp(now - 2 * 86400), "project": "Test",
        "project_id": "prj-a", "run": "dup", "tokens_in": 2, "tokens_out": 1,
    })
    _write(other / "dup-new.json", {
        "schema": 2, "at": _stamp(now - 86400), "project": "Test",
        "project_id": "prj-a", "run": "dup", "tokens_in": 9, "tokens_out": 4,
    })
    (folder / "broken.json").write_text("{oops", encoding="utf-8")
    records = UL.iter_records(str(root))
    assert {r["schema"] for r in records} == {1, 2}, records
    assert records[0]["run"] == "dup" and records[0]["tokens_in"] == 9, records
    assert all("path" not in json.dumps(r) for r in records), records
    assert next(r for r in records if r["run"] == "legacy")["project_id"] == "Test"
    got = UL.summary([str(root)], days=7, now=now)
    assert got["records"] == 2, got
    assert got["totals"]["tokens_in"] == 10, got["totals"]
    assert got["totals"]["tokens_out"] == 4, got["totals"]


@test
def test_usage_log_prune_uses_at_not_mtime(tmp: Path):
    """Свежий at защищает запись от prune, старый at удаляет даже при свежем mtime."""
    import usage_log as UL
    root = make_project(tmp)
    folder = UL._folder(str(root))
    now = time.time()
    fresh = folder / "fresh-at.json"
    stale = folder / "stale-at.json"
    _write(fresh, {"schema": 2, "at": _stamp(now), "project": "Test", "run": "fresh"})
    _write(stale, {"schema": 2, "at": _stamp(now - 100 * 86400),
                   "project": "Test", "run": "stale"})
    os.utime(fresh, (now - 100 * 86400, now - 100 * 86400))
    os.utime(stale, (now, now))
    UL.record(str(root), "trigger", {"kind": "agent", "calls": 1})
    assert fresh.exists(), "свежий at удалён из-за старого mtime"
    assert not stale.exists(), "старый at остался из-за свежего mtime"


@test
def test_usage_summary_counts_by_cell_without_base_multiplication(tmp: Path):
    """Итог считается по by_cell, а legacy by_role без пар не размножает base."""
    import usage_log as UL
    root = make_project(tmp)
    UL.record(str(root), "cells", {
        "calls": 3, "calls_failed": 1, "requests": 4,
        "tokens_in": 30, "tokens_out": 12,
        "tokens_failed_in": 2, "tokens_failed_out": 1,
        "cached_in": 5, "reasoning_out": 2, "gen_seconds": 7,
        "by_cell": [
            {"provider": "gw1", "provider_name": "Gateway", "role": "worker",
             "model": "m1", "calls": 2, "calls_failed": 1, "requests": 3,
             "tokens_in": 20, "tokens_out": 8, "cached_in": 5,
             "reasoning_out": 2, "gen_seconds": 5},
            {"provider": "gw2", "provider_name": "Other", "role": "critic",
             "model": "m2", "calls": 1, "calls_failed": 0, "requests": 1,
             "tokens_in": 10, "tokens_out": 4, "cached_in": None,
             "reasoning_out": None, "gen_seconds": 2},
        ],
    })
    got = UL.summary([str(root)], days=7, now=time.time())
    totals = got["totals"]
    assert totals["calls"] == 3 and totals["tokens_in"] == 30, totals
    assert totals["tokens_total"] == 42 and totals["tokens_failed"] == 3, totals
    assert totals["cached_in"] == 5 and totals["reasoning_out"] == 2, totals
    assert len(got["rows"]) == 2 and got["old"] == 0, got
    assert sum(r["tokens_in"] for r in got["rows"]) == totals["tokens_in"], got
    assert {r["provider"] for r in got["rows"]} == {"gw1", "gw2"}, got
    UL.record(str(root), "legacy", {
        "calls": 1, "tokens_in": 7, "tokens_out": 3,
        "by_role": {"writer": {"calls": 1, "tokens_in": 7, "tokens_out": 3}},
        "by_model": {"m3": {"calls": 1, "tokens_in": 7, "tokens_out": 3}},
    })
    got = UL.summary([str(root)], days=7, now=time.time())
    legacy = [r for r in got["rows"] if r["role"] == "writer"]
    assert len(legacy) == 1 and legacy[0]["tokens_in"] == 7, legacy


@test
def test_agent_core_records_schema2_cells_and_failed_tokens(tmp: Path):
    """Писатель считает failed-токены отдельно, by_cell и usage_missing_calls."""
    import copy
    AG = importlib.import_module("agent_core")
    RS = importlib.import_module("run_summary")
    saved = copy.deepcopy(AG.USAGE)
    saved_sem = AG._slot_semaphore
    saved_cache = os.environ.get("AURORA_AGENT_CACHE")

    class _Sem:
        def acquire(self, *a, **k):
            return True

        def release(self):
            pass

    def reset():
        AG.USAGE.clear()
        AG.USAGE.update(copy.deepcopy(saved))
        AG.USAGE.update(calls=0, calls_failed=0, failed=0, requests=0,
                        tokens_in=0, tokens_out=0, tokens_failed_in=0,
                        tokens_failed_out=0, usage_missing_calls=0,
                        cached_in=None, reasoning_out=None, gen_seconds=0.0,
                        errors={}, by_role={}, by_model={}, by_pair={}, by_cell={})
        AG.DOWN.clear()
        AG.DOWN_WHY.clear()
        AG.COOL.clear()
        AG.SLOW.clear()
        AG.LAST_OK.clear()

    try:
        os.environ["AURORA_AGENT_CACHE"] = "0"
        AG._slot_semaphore = lambda *_a, **_k: _Sem()
        reset()
        calls = []
        cfg = {"backends": [{"n": 1, "url": "http://a", "key": "", "provider": "gw1",
                              "name": "Gateway", "model": "m", "models": {}}],
               "request_timeout": 100, "thinking": False}

        def transport(kind, _backend, _payload, _timeout):
            if kind == "slots":
                return 200, {"slots_idle": 1}, "", 0.0
            calls.append(1)
            if len(calls) == 1:
                return (200, {"choices": [{"message": {"content": ""}}],
                              "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
                        "", 0.01)
            return (200, {"choices": [{"message": {"content": "ответ"}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                                    "prompt_tokens_details": {"cached_tokens": 3},
                                    "completion_tokens_details": {"reasoning_tokens": 2}}},
                    "", 0.3)

        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                         transport=transport, deadline=time.time() + 30,
                         sleep=lambda _s: None)
        assert r["ok"], r
        u = AG.USAGE
        assert (u["calls"], u["calls_failed"], u["requests"]) == (1, 0, 2), u
        assert (u["tokens_in"], u["tokens_out"]) == (10, 4), u
        assert (u["tokens_failed_in"], u["tokens_failed_out"]) == (2, 1), u
        assert u["cached_in"] == 3 and u["reasoning_out"] == 2, u
        assert u["usage_missing_calls"] == 0, u
        cell = u["by_cell"].get(("gw1", "worker", "m"))
        assert cell and cell["calls"] == 1 and cell["requests"] == 2, u["by_cell"]
        summary = RS.from_agent([], u, 1.0, None)
        assert summary["calls"] == 1 and summary["calls_failed"] == 0, summary
        assert summary["model_calls"] == 1 and summary["requests"] == 2, summary
        assert summary["tokens_failed_in"] == 2 and summary["by_cell"], summary

        reset()

        def no_usage(kind, _backend, _payload, _timeout):
            if kind == "slots":
                return 200, {}, "", 0.0
            return 200, {"choices": [{"message": {"content": "ответ"}}]}, "", 0.1

        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                         transport=no_usage, deadline=time.time() + 30,
                         sleep=lambda _s: None)
        assert r["ok"] and AG.USAGE["usage_missing_calls"] == 1, AG.USAGE
        assert RS.from_agent([], AG.USAGE, 1.0, None)["estimated"], AG.USAGE
    finally:
        AG.USAGE.clear()
        AG.USAGE.update(saved)
        AG._slot_semaphore = saved_sem
        if saved_cache is None:
            os.environ.pop("AURORA_AGENT_CACHE", None)
        else:
            os.environ["AURORA_AGENT_CACHE"] = saved_cache


@test
def test_log_usage_passes_schema2_fields_to_usage_d(tmp: Path):
    """log_usage передаёт started, seconds, requests, by_cell и failed-токены."""
    AR = importlib.import_module("agent_runner")
    RS = importlib.import_module("run_summary")
    UL = importlib.import_module("usage_log")
    root = make_project(tmp)
    summary = RS.from_agent([], {
        "calls": 2, "calls_failed": 1, "requests": 4,
        "tokens_in": 20, "tokens_out": 9, "tokens_failed_in": 1,
        "tokens_failed_out": 2, "cache_hits": 3, "gen_seconds": 6.5,
        "by_cell": [{"provider": "gw1", "provider_name": "Gateway",
                      "role": "worker", "model": "m", "calls": 2,
                      "calls_failed": 1, "requests": 4, "tokens_in": 20,
                      "tokens_out": 9, "gen_seconds": 6.5}],
    }, 12.5, None)
    AR.log_usage(str(root), "build", summary, started=1_700_000_000.0,
                 seconds=12.5, run_id="run-log")
    rec = json.loads((UL._folder(str(root)) / "run-log.json").read_text(encoding="utf-8"))
    assert rec["schema"] == 2 and rec["started"] == "2023-11-14T22:13:20Z", rec
    assert rec["seconds"] == 12.5 and rec["requests"] == 4, rec
    assert (rec["calls"], rec["calls_failed"]) == (2, 1), rec
    assert rec["tokens_failed_in"] == 1 and rec["tokens_failed_out"] == 2, rec
    assert rec["by_cell"] and rec["by_cell"][0]["provider"] == "gw1", rec
    assert "key" not in json.dumps(rec).lower(), rec

    AR.log_usage(str(root), "build", {"model_calls": 3, "model_failed": 1,
                                      "tokens_in": 5, "tokens_out": 2},
                 started=1_700_000_100.0, seconds=2.0, run_id="run-old")
    old = json.loads((UL._folder(str(root)) / "run-old.json").read_text(encoding="utf-8"))
    assert (old["calls"], old["calls_failed"]) == (2, 1), old
