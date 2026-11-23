"""Проверки части 255: сервисные метрики из спанов и оконная сводка (S4-METRICS).

Здесь сходятся правила раздела 4 контракта: `calls` и задержки берутся с листовых
`call`, попытки и HTTP-разряды с `attempt`, байты не складываются дважды, страницы
и задачи приходят только с фазы. Гистограммы, малая выборка, повторы записей и
`window_metrics` проверяются на синтетических спанах; панель — под node.
"""
from __future__ import annotations

import http.client
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import KIT, SCRIPTS, isolated_registry, make_project, test

for _path in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import importlib  # noqa: E402

_SEQ = itertools.count(1)


def _timing():
    return importlib.import_module("timing")


def _cockpit():
    return importlib.import_module("aurora_cockpit")


def _span(kind: str, service: str, **kw) -> dict:
    row = {
        "v": 1, "type": "span", "run_id": kw.pop("run_id", "r1"),
        "span_id": kw.pop("span_id", f"s{next(_SEQ)}"), "parent_id": "",
        "kind": kind, "name": "", "service": service, "cause": "",
        "start": "2026-11-21T10:00:00.000Z", "end": "2026-11-21T10:00:01.000Z",
        "dur_ms": 1000.0, "status": "ok", "reason": "", "attempt": 0,
        "source": "live", "confidence": "exact", "metrics": {},
    }
    metrics = kw.pop("metrics", None)
    if isinstance(metrics, dict):
        row["metrics"].update(metrics)
    row.update(kw)
    return row


def _attempt(parent: dict, number: int, status: str, **kw) -> dict:
    return _span("attempt", parent["service"], parent_id=parent["span_id"],
                 attempt=number, status=status, **kw)


def _ago(**delta) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(**delta)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _timing_summary(truncated: bool = False) -> dict:
    return {
        "v": 1, "active_ms": 1000, "work_ms": 900, "service_wait_ms": 100,
        "queue_ms": 0, "other_wait_ms": 0, "unknown_ms": 0, "coverage": 1.0,
        "source": "spans", "confidence": "exact",
        "quality": {"wait_known": True, "approx": False, "legacy": False,
                    "truncated": truncated},
    }


def _stat(values, with_hist: bool = True) -> dict:
    ordered = sorted(float(value) for value in values)
    n = len(ordered)
    hist = {}
    for value in ordered:
        _timing().hist_add(hist, value)
    return {
        "n": n, "avg": round(sum(ordered) / n, 1) if n else None,
        "p50": None, "p95": None, "max": ordered[-1] if n else None,
        "approx": False, "hist": hist if with_hist else {},
    }


def _service(calls: int, values, with_hist: bool = True, basis: str = "response",
             probe=None, truncated: bool = False) -> dict:
    return {
        "calls": calls, "service_calls_ok": calls, "attempts": calls, "ok": calls,
        "errors": {}, "latency_ms": _stat(values, with_hist),
        "ttft_ms": _stat([]), "slot_wait_ms": _stat([]), "backoff_ms": _stat([]),
        "tokens_in": 0, "tokens_out": 0, "tps": None, "tps_basis": "request",
        "slow_calls": 0, "bytes": 0, "bytes_basis": basis, "bytes_total": 0,
        "pages": 0, "issues": 0, "http_4xx": 0, "http_5xx": 0, "timeouts": 0,
        "retries": 0, "auth_errors": 0, "content_errors": 0, "rate_limited": 0,
        "probe_ok_rate": probe or {"n": 0, "ok": 0, "rate": None, "approx": False},
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


@test
def test_s4_metrics_histograms_are_fixed_and_small_samples_hide_percentiles(_tmp: Path):
    """Границы бакетов фиксированы; n < 20 не даёт квантилей, а `inf` не исчезает."""
    T = _timing()
    expected = (0, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000,
                60000, 120000, 300000, 600000, 1800000, float("inf"))
    assert T.HIST_BUCKETS_MS == expected, T.HIST_BUCKETS_MS

    hist = {}
    for value in (0, 5, 10, 11, 2_000_000):
        T.hist_add(hist, value)
    assert hist == {"0": 1, "10": 2, "25": 1, "inf": 1}, hist
    T.hist_add(hist, None)
    T.hist_add(hist, "12")
    assert hist["25"] == 1, "нечисловое значение не должно менять гистограмму"

    small = T.hist_stats(hist)
    assert small == {"n": 5, "p50": None, "p95": None, "approx": True}, small
    assert "avg" not in small and "max" not in small, small

    merged = {}
    T.hist_merge(merged, hist)
    T.hist_merge(merged, {"100": 20, "250": 5})
    stats = T.hist_stats(merged)
    assert stats["n"] == 30 and stats["approx"] is True, stats
    assert stats["p50"] == 100.0 and stats["p95"] == 250.0, stats


@test
def test_s4_metrics_confluence_counts_leaves_phase_totals_and_bytes_once(_tmp: Path):
    """500 страниц: вызовы, 4xx/5xx, байты и страницы без двойного счёта."""
    T = _timing()
    phase = _span("phase", "confluence", name="confluence.export", operation="export",
                  metrics={"pages": 500, "bytes_total": 5_400_000, "calls_total": 8})
    read = _span("call", "confluence", name="http.get", operation="get",
                 metrics={"latency_ms": 800, "http_status": 200})
    missing = _span("call", "confluence", name="http.get", operation="get",
                    status="error", reason="http_404",
                    metrics={"latency_ms": 900, "http_status": 404})
    send = _span("call", "confluence", name="http.send", operation="send",
                 metrics={"latency_ms": 1200, "http_status": 200, "retries": 1,
                          "bytes": 9999})
    rows = [
        phase, read, missing, send,
        _attempt(send, 1, "unavailable", name="http.send", operation="send",
                 reason="http_5xx", metrics={"latency_ms": 1000, "http_status": 500,
                                             "bytes": 1000}),
        _attempt(send, 2, "ok", name="http.send", operation="send",
                 metrics={"latency_ms": 200, "http_status": 200, "bytes": 2000}),
    ]
    rec = T.service_metrics(rows)["confluence"]
    assert rec["calls"] == 3 and rec["ok"] == 2, rec
    assert rec["service_calls"] == 3 and rec["service_calls_ok"] == 2, rec
    assert rec["errors"] == {"http_404": 1}, rec["errors"]
    assert rec["attempts"] == 4, rec["attempts"]
    assert rec["retries"] == 1, rec["retries"]
    assert rec["http_4xx"] == 1 and rec["http_5xx"] == 1, rec
    assert rec["bytes"] == 3000, "байты вызова с попытками не складываются с листом"
    assert rec["bytes_basis"] == "response", rec["bytes_basis"]
    assert rec["bytes_total"] == 5_400_000 and rec["pages"] == 500, rec
    assert rec["issues"] == 0, rec
    assert rec["latency_ms"]["n"] == 3 and rec["latency_ms"]["avg"] == 966.7, rec
    assert rec["latency_ms"]["p50"] is None and rec["latency_ms"]["p95"] is None, rec
    assert rec["latency_ms"]["hist"]["1000"] == 2, rec["latency_ms"]["hist"]
    assert rec["latency_ms"]["hist"]["2500"] == 1, rec["latency_ms"]["hist"]


@test
def test_s4_metrics_llm_waits_and_attempts_are_not_doubled(_tmp: Path):
    """LLM: slot/backoff с wait-спанов, ttft и tps_basis, попытки не из двух мест."""
    T = _timing()
    call = _span("call", "llm", name="llm.worker", role="worker", operation="call",
                 metrics={"latency_ms": 2000, "ttft_ms": 500, "tokens_in": 100,
                          "tokens_out": 50, "tps_basis": "stream", "attempts": 2,
                          "slot_wait_ms": 999})
    rows = [
        call,
        _attempt(call, 1, "unavailable", name="llm.chat", operation="chat",
                 reason="http_5xx", metrics={"latency_ms": 1000}),
        _attempt(call, 2, "ok", name="llm.chat", operation="chat",
                 metrics={"latency_ms": 2000}),
        _span("wait", "llm", name="llm.slot", reason="slot", wait_class="resource",
              dur_ms=300, metrics={"slot_wait_ms": 300}),
        _span("wait", "llm", name="llm.backoff", reason="backoff", wait_class="pacing",
              dur_ms=1000, metrics={"backoff_ms": 1000}),
    ]
    rec = T.service_metrics(rows)["llm"]
    assert rec["calls"] == 1 and rec["attempts"] == 2, rec
    assert rec["retries"] == 1, "повтор считается по попыткам, а не по полю call"
    assert rec["http_5xx"] == 1 and rec["timeouts"] == 0, rec
    assert rec["latency_ms"]["n"] == 1 and rec["latency_ms"]["avg"] == 2000, rec
    assert rec["ttft_ms"]["n"] == 1 and rec["ttft_ms"]["avg"] == 500, rec
    assert rec["slot_wait_ms"]["n"] == 1 and rec["slot_wait_ms"]["avg"] == 300, rec
    assert rec["backoff_ms"]["n"] == 1 and rec["backoff_ms"]["avg"] == 1000, rec
    assert rec["tokens_in"] == 100 and rec["tokens_out"] == 50, rec
    assert rec["tps"] == 25.0 and rec["tps_basis"] == "stream", rec


@test
def test_s4_metrics_web_embeddings_and_probe_fields(_tmp: Path):
    """Веб без call меряется попытками; эмбеддинги дают request-байты; пробы дают rate."""
    T = _timing()
    web = [
        _span("attempt", "web", name="http.get", operation="get", attempt=1,
              status="timeout", reason="timeout",
              metrics={"latency_ms": 3000, "bytes": 0}),
        _span("attempt", "web", name="http.get", operation="get", attempt=2,
              metrics={"latency_ms": 500, "bytes": 400, "http_status": 200}),
        _span("attempt", "web", name="http.get", operation="get", attempt=3,
              metrics={"latency_ms": 600, "bytes": 500, "http_status": 200}),
        _span("attempt", "web", name="http.get", operation="get", attempt=1,
              metrics={"latency_ms": 200, "bytes": 100, "http_status": 200}),
        _span("phase", "web", name="web.export", operation="export",
              metrics={"pages": 20, "bytes_total": 1000, "calls_total": 4}),
    ]
    rec = T.service_metrics(web)["web"]
    assert rec["calls"] == 0 and rec["attempts"] == 4, rec
    assert rec["retries"] == 2, rec["retries"]
    assert rec["bytes"] == 1000 and rec["bytes_basis"] == "response", rec
    assert rec["latency_ms"]["n"] == 4, "без call задержка берётся из попыток"
    assert rec["pages"] == 20 and rec["bytes_total"] == 1000, rec
    assert rec["timeouts"] == 1, rec

    embed = _span("call", "embeddings", name="embeddings.embed", operation="embed",
                  metrics={"tokens_in": 10, "retries": 1, "vectors": 2, "model": "m"})
    rows = [
        embed,
        _attempt(embed, 1, "auth", name="embeddings.attempt", operation="embed",
                 reason="http_401", metrics={"http_status": 401, "bytes": 50,
                                             "bytes_basis": "request",
                                             "latency_ms": 100}),
        _attempt(embed, 2, "ok", name="embeddings.attempt", operation="embed",
                 metrics={"http_status": 200, "bytes": 60, "bytes_basis": "request",
                          "latency_ms": 200}),
    ]
    rec = T.service_metrics(rows)["embeddings"]
    assert rec["calls"] == 1 and rec["attempts"] == 2 and rec["retries"] == 1, rec
    assert rec["bytes"] == 110 and rec["bytes_basis"] == "request", rec
    assert rec["tokens_in"] == 10, rec
    assert rec["auth_errors"] == 1, rec

    probes = [
        _span("call", "network", name="probe", operation="probe",
              metrics={"latency_ms": 10, "probe_ms": 10}),
        _span("call", "network", name="probe", operation="probe",
              metrics={"latency_ms": 20, "probe_ms": 20}),
        _span("call", "network", name="probe", operation="probe",
              metrics={"latency_ms": 30, "probe_ms": 30}),
        _span("call", "network", name="probe", operation="probe", status="timeout",
              reason="timeout", metrics={"latency_ms": 5000, "probe_ms": 5000}),
    ]
    rec = T.service_metrics(probes)["network"]
    assert rec["calls"] == 4 and rec["ok"] == 3, rec
    assert rec["probe_ok_rate"] == {"n": 4, "ok": 3, "rate": 0.75, "approx": False}, rec
    assert rec["timeouts"] == 1, rec


@test
def test_s4_metrics_nested_calls_and_duplicate_spans_count_once(_tmp: Path):
    """Верхний вызов того же сервиса не второй вызов; дубль span_id не считается дважды."""
    T = _timing()
    parent = _span("call", "jira", name="http.send",
                   metrics={"latency_ms": 1000, "tokens_in": 10})
    child = _span("call", "jira", name="http.get", parent_id=parent["span_id"],
                  metrics={"latency_ms": 500, "tokens_in": 5})
    rec = T.service_metrics([parent, child])["jira"]
    assert rec["calls"] == 1 and rec["latency_ms"]["n"] == 1, rec
    assert rec["tokens_in"] == 5, rec

    outer = _span("call", "jira", metrics={"latency_ms": 1200})
    inner = _span("call", "confluence", parent_id=outer["span_id"],
                  metrics={"latency_ms": 700})
    services = T.service_metrics([outer, inner])
    assert services["jira"]["calls"] == 1 and services["confluence"]["calls"] == 1, services

    first = _span("call", "llm", span_id="dup", metrics={"latency_ms": 100,
                                                          "tokens_in": 1})
    second = _span("call", "llm", span_id="dup", metrics={"latency_ms": 900,
                                                           "tokens_in": 99})
    rec = T.service_metrics([first, second])["llm"]
    assert rec["calls"] == 1 and rec["latency_ms"]["max"] == 100, rec
    assert rec["tokens_in"] == 1, rec


@test
def test_s4_metrics_summary_counts_all_calls_and_normalizes_other_wait(_tmp: Path):
    """`service_calls` — все логические вызовы, `other` в ожидании читается как `resource`."""
    T = _timing()
    rows = [
        _span("call", "llm", metrics={"latency_ms": 100}),
        _span("call", "llm", status="error", reason="timeout",
              metrics={"latency_ms": 200}),
        _span("wait", "llm", wait_class="other", reason="lock", dur_ms=500,
              start="2026-11-21T10:00:00.000Z", end="2026-11-21T10:00:00.500Z"),
    ]
    summary = T.summarize(rows)
    assert summary["service_calls"] == 2, summary
    assert summary["service_calls_ok"] == 1, summary
    assert summary["other_by_kind"].get("resource") == 500.0, summary["other_by_kind"]
    assert "other" not in summary["other_by_kind"], summary["other_by_kind"]


@test
def test_s4_metrics_records_dedup_by_key_and_keep_record_id(tmp: Path):
    """`record_id` даёт отдельный файл; повтор ключа заменяет, а не суммируется."""
    T = _timing()
    root = make_project(tmp)
    timing = {"v": 1, "active_ms": 100}
    T.record(str(root), "run-1", timing, {"llm": {"calls": 1}}, actor_id="u",
             machine="m", at="2026-11-21T10:00:00Z", record_id="confluence")
    T.record(str(root), "run-1", timing, {"jira": {"calls": 2}}, actor_id="u",
             machine="m", at="2026-11-21T10:00:01Z", record_id="jira")
    folder = root / "AuroraKnowledgeDB" / "meta" / "timing.d" / "u-m"
    assert sorted(path.name for path in folder.glob("*.json")) == [
        "run-1-confluence.json", "run-1-jira.json"]
    records = T.iter_records([str(root)])
    assert [item["record_id"] for item in records] == ["jira", "confluence"], records

    T.record(str(root), "run-1", timing, {"llm": {"calls": 7}}, actor_id="u",
             machine="m", at="2026-11-21T10:00:02Z", record_id="confluence")
    records = T.iter_records([str(root)])
    confluence = [item for item in records if item["record_id"] == "confluence"]
    assert len(confluence) == 1, records
    assert confluence[0]["services"]["llm"]["calls"] == 7, confluence

    for part, calls in (("a-m", 1), ("b-m", 2)):
        _write_json(root / "AuroraKnowledgeDB" / "meta" / "timing.d" / part / "run-2.json",
                    {"schema": 1, "run": "run-2", "at": "2026-11-21T09:00:00Z",
                     "project": root.name, "services": {"llm": {"calls": calls}},
                     "timing": timing})
    tie = [item for item in T.iter_records([str(root)]) if item["run"] == "run-2"]
    assert len(tie) == 1 and tie[0]["services"]["llm"]["calls"] == 2, tie

    T.record(str(root), "run-old", timing, {"llm": {"calls": 9}}, actor_id="u",
             machine="m", at=_ago(days=40))
    recent = T.iter_records([str(root)], since=__import__("time").time() - 7 * 86400)
    assert "run-old" not in [item["run"] for item in recent], recent


@test
def test_s4_metrics_read_spans_beyond_limit_and_record_run(tmp: Path):
    """`read_spans` читает больше 5000 строк; `record_run` пишет один полный рекорд."""
    T = _timing()
    root = make_project(tmp)
    run_dir = root / "runs" / "r-1"
    run_dir.mkdir(parents=True)
    lines = [json.dumps(_span("call", "early", run_id="r-1",
                              metrics={"latency_ms": 1000}))]
    for index in range(5000):
        lines.append(json.dumps(_span("wait", "llm", run_id="r-1",
                                      span_id=f"w{index}", reason="backoff",
                                      dur_ms=10, metrics={"backoff_ms": 10})))
    lines.append(json.dumps(_span("call", "elsewhere", run_id="other",
                                  metrics={"latency_ms": 1000})))
    (run_dir / "timing.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert len(T.read_spans(str(run_dir))) == 5002, "ранние строки не теряются"
    assert T.read_spans(str(run_dir / "timing.jsonl")) == T.read_spans(str(run_dir))
    assert T.record_run(str(root), "no-such-run", str(run_dir)) == ""

    path = T.record_run(str(root), "r-1", str(run_dir), kind="job", task="sync",
                        actor_id="u", machine="m")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["run"] == "r-1" and payload["kind"] == "job", payload
    assert payload["record_id"] == "", payload
    assert payload["services"]["early"]["calls"] == 1, payload["services"]
    assert "elsewhere" not in payload["services"], payload["services"]
    assert payload["services"]["llm"]["backoff_ms"]["n"] == 5000, payload["services"]
    assert "services" not in payload["timing"], payload["timing"]


@test
def test_s4_metrics_window_merges_records_and_reports_percentiles_honestly(tmp: Path):
    """Окно суммирует записи, взвешивает avg, а квантили не выдумывает."""
    T = _timing()
    root = make_project(tmp)
    _write_json(root / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "u-m" / "run-net.json",
                {"id": "run-net", "cmd": "agent:distill", "seconds": 60,
                 "snapshots": [{"at": _ago(hours=1), "state": "ok",
                                "requirements": {"llm": True},
                                "system_vpn": {"services": []}}]})

    T.record(str(root), "run-a", _timing_summary(), {
        "llm": _service(2, [100, 200]),
        "network": _service(2, [10, 20],
                            probe={"n": 2, "ok": 1, "rate": 0.5, "approx": False}),
    }, actor_id="u1", machine="m", at=_ago(hours=1))
    T.record(str(root), "run-b", _timing_summary(), {
        "llm": _service(3, [300, 600, 900]),
        "network": _service(3, [30, 40, 50],
                            probe={"n": 3, "ok": 3, "rate": 1.0, "approx": False}),
    }, actor_id="u1", machine="m", at=_ago(hours=2))
    T.record(str(root), "run-old", _timing_summary(), {"llm": _service(9, [1])},
             actor_id="u1", machine="m", at=_ago(days=40))
    # Тот же ключ (project, run, record_id) в другой папке участника: новее побеждает.
    T.record(str(root), "run-a", _timing_summary(), {
        "llm": _service(100, [100, 200]),
        "network": _service(2, [10, 20],
                            probe={"n": 2, "ok": 1, "rate": 0.5, "approx": False}),
    }, actor_id="u2", machine="m", at=_ago(minutes=10))

    window = T.window_metrics([str(root)], days=7)
    assert window["window"]["records"] == 2, window["window"]
    assert [row["run"] for row in window["runs"]] == ["run-a", "run-b"], window["runs"]
    llm = window["services"]["llm"]
    assert llm["calls"] == 103, llm
    assert llm["latency_ms"]["n"] == 5, llm
    assert llm["latency_ms"]["avg"] == 420.0, llm
    assert llm["latency_ms"]["max"] == 900.0, llm
    assert llm["latency_ms"]["p50"] is None and llm["latency_ms"]["p95"] is None, llm
    assert window["quality"]["approx"] is False, window["quality"]
    network = window["services"]["network"]
    assert network["calls"] == 5, network
    assert network["probe_ok_rate"] == {"n": 5, "ok": 4, "rate": 0.8,
                                        "approx": False}, network
    assert network["probe_ok_rate"]["rate"] == 0.8, network

    T.record(str(root), "run-c", _timing_summary(), {"llm": _service(20, [50] * 20)},
             actor_id="u1", machine="m", at=_ago(minutes=5))
    window = T.window_metrics([str(root)], days=7)
    llm = window["services"]["llm"]
    assert llm["latency_ms"]["n"] == 25, llm
    assert llm["latency_ms"]["p50"] is not None and llm["latency_ms"]["p95"] is not None, llm
    assert window["quality"]["approx"] is True, window["quality"]

    T.record(str(root), "run-d", _timing_summary(truncated=True), {
        "llm": _service(10, [70] * 10, with_hist=False, basis="request"),
    }, actor_id="u1", machine="m", at=_ago(minutes=1))
    window = T.window_metrics([str(root)], days=7)
    llm = window["services"]["llm"]
    assert llm["latency_ms"]["n"] == 35, llm
    assert llm["latency_ms"]["p50"] is None and llm["latency_ms"]["p95"] is None, llm
    assert llm["bytes_basis"] == "mixed", llm
    assert window["quality"]["approx"] is True, window["quality"]
    assert window["quality"]["truncated"] is True, window["quality"]
    assert window["quality"]["records"] == 4, window["quality"]
    assert window["quality"]["services"] == ["timing.d"], window
    for key in ("active_ms", "work_ms", "service_wait_ms", "other_wait_ms",
                "queue_ms", "unknown_ms", "coverage", "source", "confidence"):
        assert key in window["runs"][0], window["runs"][0]


@test
def test_s4_metrics_state_and_api_keep_cards_when_one_source_fails(tmp: Path):
    """`timing` есть в ответе; ошибка сети или timing не прячет остальные карточки."""
    T = _timing()
    ck = _cockpit()
    import net_report as NR
    root = make_project(tmp)
    _write_json(root / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "u-m" / "run-1.json",
                {"id": "run-1", "cmd": "agent:distill", "seconds": 60,
                 "snapshots": [{"at": _ago(hours=1), "state": "ok",
                                "requirements": {"llm": True},
                                "system_vpn": {"services": []}}]})

    empty = ck.metrics_state([str(root)], days=7)
    assert empty["error"] == "", empty
    assert empty["timing"]["runs"] == [] and empty["timing"]["services"] == {}, empty
    assert empty["net"] and "tokens" in empty, empty

    T.record(str(root), "run-1", _timing_summary(), {"llm": _service(2, [100, 200])},
             actor_id="u", machine="m", at=_ago(hours=1))
    got = ck.metrics_state([str(root)], days=7)
    assert got["error"] == "" and got["timing_error"] == "", got
    assert got["timing"]["runs"][0]["run"] == "run-1", got["timing"]
    assert got["timing"]["services"]["llm"]["calls"] == 2, got["timing"]

    with unittest.mock.patch.object(NR, "list_reports", side_effect=RuntimeError("boom")):
        broken_net = ck.metrics_state([str(root)], days=7)
    assert broken_net["error"] and broken_net["net_error"], broken_net
    assert broken_net["timing"]["runs"][0]["run"] == "run-1", broken_net

    with unittest.mock.patch.object(ck.T, "window_metrics",
                                    side_effect=RuntimeError("boom")):
        broken_timing = ck.metrics_state([str(root)], days=7)
    assert broken_timing["timing_error"], broken_timing
    assert broken_timing["timing"]["runs"] == [], broken_timing
    assert broken_timing["net"] and broken_timing["error"] == "", broken_timing


@test
def test_s4_metrics_run_owners_write_one_record_from_the_full_file(tmp: Path):
    """`agent_runner.log_timing` и `start_job` зовут владельца прогона с полным файлом."""
    T = _timing()
    ck = _cockpit()
    root = make_project(tmp)
    run_dir = Path(ck.runs_dir(str(root))) / "run-agent"
    run_dir.mkdir(parents=True)
    (run_dir / "timing.jsonl").write_text(
        json.dumps(_span("call", "early", run_id="run-agent",
                         metrics={"latency_ms": 100})) + "\n", encoding="utf-8")
    import agent_runner as AR
    saved = os.environ.get("AURORA_RUN_DIR")
    os.environ["AURORA_RUN_DIR"] = str(run_dir)
    try:
        AR.log_timing(str(root), "build", "run-agent", {"v": 1, "active_ms": 1},
                      {"llm": {"calls": 0}})
    finally:
        if saved is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = saved
    record = next(item for item in T.iter_records([str(root)])
                  if item["run"] == "run-agent")
    assert record["kind"] == "agent" and record["task"] == "build", record
    assert record["services"]["early"]["calls"] == 1, record
    assert "llm" not in record["services"], "снимок памяти не должен подменять файл"

    real_popen, real_running = ck.subprocess.Popen, ck.RUNNING
    restore_registry = isolated_registry(ck, tmp)
    real_record_run = ck.T.record_run
    calls = []

    def spy(*args, **kw):
        calls.append((args, kw))
        return ""

    def broken(*_args, **_kw):
        raise OSError("интерпретатор не найден")

    ck.subprocess.Popen = broken
    ck.RUNNING = str(tmp / "running.json")
    ck.T.record_run = spy
    try:
        job_id = ck.start_job(str(root), "kb:lint", [])
        job = ck.JOBS[job_id]
        for _ in range(200):
            if job["done"]:
                break
            time.sleep(0.05)
    finally:
        ck.subprocess.Popen, ck.RUNNING = real_popen, real_running
        ck.T.record_run = real_record_run
        restore_registry()
    assert job["done"], job
    assert calls and calls[0][1].get("kind") == "job", calls
    assert str(calls[0][0][1]) == job["run_id"], calls


@test
def test_s4_metrics_api_returns_200_with_and_without_timing_d(tmp: Path):
    """Настоящий сервер панели: `/api/metrics` отвечает 200 и отдаёт timing."""
    T = _timing()
    ck = _cockpit()
    from http.server import ThreadingHTTPServer
    root = make_project(tmp)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    server.roots = [str(tmp)]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path="/api/metrics?days=7&lang=ru"):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                          timeout=60)
        conn.request("GET", path,
                     headers={"X-Aurora-Token": ck.TOKEN})
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        conn.close()
        return response.status, body

    try:
        status, body = get()
        assert status == 200, (status, body[:400])
        data = json.loads(body)
        assert data["timing"]["runs"] == [] and data["timing"]["services"] == {}, data
        assert "net" in data and "tokens" in data and data["error"] == "", data
        status, body = get("/api/usage?days=7&tz=UTC")
        assert status == 200, (status, body[:400])
        usage = json.loads(body)
        assert usage["schema"] == 2, usage
        assert usage["quality"]["empty_reason"] == "no_records", usage["quality"]

        T.record(str(root), "run-api", _timing_summary(),
                 {"llm": _service(2, [100, 200])}, actor_id="u", machine="m",
                 at=_ago(hours=1))
        status, body = get()
        assert status == 200, (status, body[:400])
        data = json.loads(body)
        assert data["timing"]["runs"][0]["run"] == "run-api", data["timing"]
        assert data["timing"]["services"]["llm"]["calls"] == 2, data["timing"]
    finally:
        server.shutdown()
        server.server_close()


@test
def test_s4_metrics_ui_hides_p95_for_small_samples_and_keeps_token_card_clean(tmp: Path):
    """Панель: n<20 без p95, ~ для оценки, фильтры есть, токены без wait и tps."""
    node = shutil.which("node")
    if not node:
        return
    view = KIT / "cockpit" / "modules" / "metrics" / "view.js"
    harness = (
        "const m = await import(" + json.dumps(view.as_uri()) + ");\n"
        "const low = m.statCells({n:5, avg:100, p50:null, p95:null, max:900, approx:false});\n"
        "const big = m.statCells({n:25, avg:100, p50:90, p95:300, max:900, approx:true});\n"
        "const missing = m.statCells({n:25, avg:100, p50:null, p95:null, max:900, approx:true});\n"
        "const keys = m.serviceKeys({web:{}, llm:{}, zzz:{}});\n"
        "const runs = m.filterRuns([{project:'A'},{project:'B'}], 'A');\n"
        "const services = m.filterServices({llm:{calls:1}, web:{calls:2}}, 'llm');\n"
        "console.log(JSON.stringify({low, big, missing, keys, runs, services,\n"
        "  notes:[m.runNote({work_ms:null, confidence:'unknown'}),\n"
        "         m.runNote({work_ms:10, source:'legacy'}),\n"
        "         m.runNote({work_ms:10, service_wait_ms:5}),\n"
        "         m.runNote({work_ms:10, service_wait_ms:0})]}));\n"
    )
    script = tmp / "metrics_view.mjs"
    script.write_text(harness, encoding="utf-8")
    cp = subprocess.run([node, str(script)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[:800]
    got = json.loads(cp.stdout)
    assert got["low"]["p50"] is None and got["low"]["p95"] is None, got["low"]
    assert got["low"]["avg"] == 100 and got["low"]["max"] == 900, got["low"]
    assert got["low"]["notes"] == ["low"], got["low"]
    assert got["big"]["p50"] == 90 and got["big"]["p95"] == 300, got["big"]
    assert got["big"]["approx"] is True and got["big"]["notes"] == ["approx"], got["big"]
    assert "missing" in got["missing"]["notes"], got["missing"]
    assert got["keys"] == ["llm", "web", "zzz"], got["keys"]
    assert len(got["runs"]) == 1 and got["runs"][0]["project"] == "A", got["runs"]
    assert list(got["services"]) == ["llm"], got["services"]
    assert got["notes"] == ["no_data", "estimate", "wait", "work"], got["notes"]

    source = view.read_text(encoding="utf-8")
    token_source = source[source.index("function tokenCard"):source.index("function draw(")]
    assert "wait_ms" not in token_source and "tps" not in token_source, token_source
    for needle in ("function runsCard", "function servicesCard", "filter_project",
                   "filter_service", "overflow-x:auto", "flex-wrap:wrap",
                   "ctx.ui.timingChip", "metrics.col.", "basis"):
        assert needle in source, needle
    assert "outage_s" not in source, "второе число сети не входит в stage 4"

    ru = json.loads((KIT / "cockpit/modules/metrics/i18n/ru.json").read_text(
        encoding="utf-8"))
    en = json.loads((KIT / "cockpit/modules/metrics/i18n/en.json").read_text(
        encoding="utf-8"))
    assert set(ru) == set(en), (set(ru) ^ set(en))
    for key in ("metrics.runs_title", "metrics.services_title", "metrics.timing_empty",
                "metrics.low_n", "metrics.approx", "metrics.filter_project",
                "metrics.filter_service", "metrics.col.basis", "metrics.no_data",
                "metrics.detail.slot", "metrics.detail.ttft", "metrics.detail.tps"):
        assert ru.get(key, "").strip() and en.get(key, "").strip(), key
        assert "—" not in ru[key] and "–" not in ru[key], key
        assert "—" not in en[key] and "–" not in en[key], key
