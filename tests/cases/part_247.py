"""Проверки части 247: спаны LLM, timing.d и итог прогона (этап 2).

Сети, моделей и настоящего домашнего каталога проверки не касаются: транспорт подменён,
папка прогона — временная. Проверяются точные интервалы слота, паузы, попытки, TTFT,
сводка качества и запись одной строки в `timing.d`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test

sys.path.insert(0, str(SCRIPTS))


def _timing():
    return importlib.import_module("timing")


def _agent():
    return importlib.import_module("agent_core")


def _summary():
    return importlib.import_module("run_summary")


class _Env:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for key, value in self.kw.items():
            self.old[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *args):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _SlowSem:
    """Семафор с ожиданием: проверка видит slot_wait_ms, не занимая настоящий шлюз."""

    def acquire(self, blocking=False, timeout=None, project=None):
        if blocking:
            return True
        time.sleep(0.01)
        return True

    def release(self):
        return None


class _FastSem:
    def acquire(self, blocking=False, timeout=None, project=None):
        return True

    def release(self):
        return None


def _cfg(AG):
    return {"backends": [{"n": 1, "url": "http://a", "key": "", "model": "m", "models": {}}],
            "request_timeout": 100, "thinking": False}


@test
def test_tracer_writes_spans_and_summarizes(tmp: Path):
    """Tracer пишет JSONL, а summary считает работу, простой и неизвестное без двойного счёта."""
    T = _timing()
    run_dir = tmp / "run"
    run_dir.mkdir()
    run = T.Tracer(run_dir=str(run_dir), run_id="run-1")
    base = time.time()
    run.note_span("call", start=base, dur_ms=1000, service="llm", role="worker")
    run.note_span("wait", start=base + 0.1, dur_ms=200, service="llm",
                  wait_class="resource", reason="slot")
    run.note_span("wait", start=base + 1.0, dur_ms=300, service="network",
                  wait_class="service", cause="network", reason="net_wait")
    rows = [json.loads(line) for line in (run_dir / "timing.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [row["kind"] for row in rows] == ["call", "wait", "wait"]
    assert all(row["v"] == 1 and row["type"] == "span" and row["run_id"] == "run-1"
               for row in rows)
    summary = run.summary(active_start=base, active_end=base + 2)
    assert summary["work_ms"] == 800, summary
    assert summary["slot_wait_ms"] == 200, summary
    assert summary["net_wait_ms"] == 300, summary
    assert summary["wait_by_service"]["network"] == 300, summary
    assert T.timing_problems(summary) == [], summary


@test
def test_call_role_records_slot_backoff_and_stream_ttft(tmp: Path):
    """Слот, пауза и TTFT попадают в call/attempt/wait, успешный ответ их не теряет."""
    AG = _agent()
    restore_home = set_home(tmp / "home")
    (tmp / "run").mkdir()
    saved_sem = AG._slot_semaphore
    try:
        with _Env(AURORA_RUN_DIR=str(tmp / "run"), AURORA_AGENT_CACHE="0"):
            AG.TIMING.reset()
            AG.DOWN.clear()
            AG.DOWN_WHY.clear()
            AG.COOL.clear()
            AG.SLOW.clear()
            AG.LAST_OK.clear()
            AG.LAST_OK[1] = time.time()
            AG.USAGE.update(calls=0, failed=0, tokens_in=0, tokens_out=0,
                            gen_seconds=0.0, errors={}, attempts=0, waited=0.0,
                            slot_wait_ms=0.0, backoff_ms=0.0, net_wait_ms=0.0,
                            by_service={})
            AG._slot_semaphore = lambda _backend, _cfg: _SlowSem()
            calls = []
            sleeps = []

            def transport(kind, backend, payload, timeout):
                if kind == "slots":
                    return 200, {"slots_idle": 1}, "", 0.0
                calls.append(1)
                if len(calls) == 1:
                    time.sleep(0.03)
                    return None, None, "TimeoutError: timed out", 0.03
                return (200, {"choices": [{"message": {"content": "ответ"}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                              "aurora": {"via": "pydantic_ai", "latency_ms": 300,
                                         "ttft_ms": 120, "tps_basis": "stream"}},
                        "", 0.3)

            r = AG.call_role(_cfg(AG), "worker", [{"role": "user", "content": "?"}],
                             transport=transport, deadline=time.time() + 30,
                             sleep=lambda seconds: (sleeps.append(seconds),
                                                    time.sleep(0.02)))
            assert r["ok"] and r["timing"], r
            assert r["timing"]["slot_wait_ms"] >= 5, r["timing"]
            assert r["timing"]["backoff_ms"] >= 9000, r["timing"]
            assert r["timing"]["ttft_ms"] == 120, r["timing"]
            assert r["timing"]["tps_basis"] == "stream", r["timing"]
            assert r["timing"]["attempts"] == 2, r["timing"]
            assert r["timing"]["service_wait_ms"] >= 10, r["timing"]
            assert r["timing"]["work_ms"] >= 250, r["timing"]
            assert sleeps and sleeps[0] == AG.RING_PAUSE, sleeps
            assert AG.USAGE["slot_wait_ms"] >= 5 and AG.USAGE["backoff_ms"] >= 9000, AG.USAGE
            assert AG.USAGE["attempts"] == 2, AG.USAGE
            spans = [s for s in AG.TIMING.snapshot() if s.kind == "attempt"]
            assert len(spans) == 2 and spans[-1].metrics["ttft_ms"] == 120, spans
    finally:
        AG._slot_semaphore = saved_sem
        restore_home()


@test
def test_call_role_without_stream_keeps_ttft_null(tmp: Path):
    """Обычный ответ не подменяет TTFT полным временем и помечает базу request."""
    AG = _agent()
    restore_home = set_home(tmp / "home")
    saved_sem = AG._slot_semaphore
    try:
        with _Env(AURORA_RUN_DIR=str(tmp / "run"), AURORA_AGENT_CACHE="0"):
            (tmp / "run").mkdir(exist_ok=True)
            AG.TIMING.reset()
            AG.DOWN.clear()
            AG.USAGE.update(calls=0, failed=0, errors={}, attempts=0)
            AG._slot_semaphore = lambda _backend, _cfg: _FastSem()

            def transport(kind, backend, payload, timeout):
                if kind == "slots":
                    return 200, {}, "", 0.0
                return (200, {"choices": [{"message": {"content": "ответ"}}],
                              "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
                        "", 0.05)

            r = AG.call_role(_cfg(AG), "worker", [{"role": "user", "content": "?"}],
                             transport=transport, deadline=time.time() + 30,
                             sleep=lambda _seconds: None)
            assert r["ok"], r
            assert r["timing"]["ttft_ms"] is None, r["timing"]
            assert r["timing"]["tps_basis"] == "request", r["timing"]
            assert r["timing"]["latency_ms"] == 50, r["timing"]
    finally:
        AG._slot_semaphore = saved_sem
        restore_home()


@test
def test_call_role_waits_for_network_and_counts_it(tmp: Path):
    """Политика queue: ожидание связи — отдельный wait, а не молчание модели."""
    AG = _agent()
    restore_home = set_home(tmp / "home")
    saved_gate, saved_sem = AG._net_gate, AG._slot_semaphore
    try:
        with _Env(AURORA_RUN_DIR=str(tmp / "run"), AURORA_AGENT_CACHE="0"):
            (tmp / "run").mkdir(exist_ok=True)
            AG.TIMING.reset()
            AG.DOWN.clear()
            AG.DOWN_WHY.clear()
            AG.COOL.clear()
            AG.SLOW.clear()
            AG._slot_semaphore = lambda _backend, _cfg: _FastSem()

            class Gate:
                PULSE_EVERY = 60.0

                def __init__(self):
                    self.down = True
                    self.waits = 0

                def policy_for(self):
                    return "queue"

                def providers_down(self, fresh=False, hold=False):
                    return {"p1": {"why": "connection"}} if self.down else {}

                def provider_reason(self, _info):
                    return "нет связи"

                def wait_restored(self, _probe, poll=0, on_wait=None):
                    self.waits += 1
                    time.sleep(0.02)
                    self.down = False

            gate = Gate()
            AG._net_gate = lambda: gate

            def transport(kind, backend, payload, timeout):
                if kind == "slots":
                    return 200, {}, "", 0.0
                return (200, {"choices": [{"message": {"content": "ответ"}}],
                              "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
                        "", 0.01)

            cfg = _cfg(AG)
            cfg["persist"] = True
            cfg["backends"][0]["provider"] = "p1"
            r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                             transport=transport, deadline=time.time() + 30,
                             sleep=lambda _seconds: None)
            assert r["ok"] and gate.waits == 1, r
            assert r["timing"]["net_wait_ms"] >= 10, r["timing"]
            assert r["timing"]["wait_by_service"].get("network", 0) >= 10, r["timing"]
            assert AG.USAGE["net_wait_ms"] >= 10, AG.USAGE
    finally:
        AG._net_gate, AG._slot_semaphore = saved_gate, saved_sem
        restore_home()


@test
def test_cache_hit_keeps_explicit_cache_timing(tmp: Path):
    """Кэш не должен превращаться в «работу» с пустой сводкой вокруг спана."""
    AG = _agent()
    saved = (AG._cache_on, AG._cache_key, AG._cache_get)
    try:
        AG.TIMING.reset()
        AG._cache_on = lambda: True
        AG._cache_key = lambda *_a, **_k: "hit"
        AG._cache_get = lambda _key: {
            "ok": True, "text": "из кэша", "seconds": 0.0, "tokens_in": 0,
            "tokens_out": 0, "tools_called": [], "timing": {
                "v": 1, "scope": "call", "cached": True, "source": "cache",
                "active_ms": 0, "total_ms": 0, "queue_ms": 0, "work_ms": 0,
                "service_wait_ms": 0, "other_wait_ms": 0, "unknown_ms": 0,
                "quality": {"wait_known": True, "approx": False, "legacy": False,
                            "queue_separated": True}}}
        r = AG.call_role(_cfg(AG), "worker", [{"role": "user", "content": "?"}])
        assert r["cached"] and r["timing"]["source"] == "cache", r
    finally:
        AG._cache_on, AG._cache_key, AG._cache_get = saved


@test
def test_timing_d_writes_one_normalized_row_and_prunes(tmp: Path):
    """timing.d хранит сводку и качество сервисов; старые строки убираются по сроку."""
    T = _timing()
    spans = [
        {"kind": "call", "service": "llm", "status": "ok",
         "metrics": {"latency_ms": 4000, "ttft_ms": 500, "tokens_in": 10,
                     "tokens_out": 100, "tps_basis": "stream"}},
        {"kind": "attempt", "service": "llm", "status": "ok", "dur_ms": 4000,
         "metrics": {"latency_ms": 4000, "tokens_out": 100}},
        {"kind": "call", "service": "llm", "status": "error",
         "metrics": {"latency_ms": 31000}, "reason": "timeout"},
    ]
    services = T.service_metrics(spans)
    assert services["llm"]["calls"] == 2 and services["llm"]["ok"] == 1, services
    assert services["llm"]["latency_ms"]["n"] == 2, services
    assert services["llm"]["ttft_ms"]["avg"] == 500, services
    assert services["llm"]["tps"] == 25.0, services
    assert services["llm"]["slow_calls"] == 0, services
    slow = T.service_metrics([{"kind": "call", "service": "llm", "status": "ok",
                               "metrics": {"latency_ms": 31000, "tokens_out": 100}}])
    assert slow["llm"]["slow_calls"] == 1 and slow["llm"]["tps"] == 3.2, slow
    project = tmp / "project"
    (project / "AuroraKnowledgeDB").mkdir(parents=True)
    path = T.record(str(project), "run-1", {"v": 1, "active_ms": 1000},
                    services, kind="agent", task="build", actor_id="u1", machine="m1")
    assert path and Path(path).is_file(), path
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["run"] == "run-1"
    assert data["timing"]["active_ms"] == 1000 and data["services"]["llm"]["calls"] == 2
    old = Path(path).parent / "old.json"
    old.write_text("{}", encoding="utf-8")
    os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    assert T.prune(str(project), days=1) == 1 and not old.exists()


@test
def test_run_summary_prints_two_numbers_and_llm_quality(tmp: Path):
    """Итог агента печатает работу/ожидание и качество доступа; без данных — не ноль."""
    RS = _summary()
    timing = {
        "v": 1, "active_ms": 720000, "total_ms": 720000, "queue_ms": 0,
        "work_ms": 600000, "service_wait_ms": 120000, "other_wait_ms": 0,
        "unknown_ms": 0, "coverage": 1.0, "source": "spans", "confidence": "exact",
        "quality": {"wait_known": True, "approx": False, "legacy": False,
                    "queue_separated": True},
        "parallel": {"slots_available_avg": 3.2},
        "services": {"llm": {"calls": 1, "ok": 1, "tps": 32.4,
                             "latency_ms": {"avg": 4200}, "ttft_ms": {"avg": None}}},
    }
    s = RS.from_agent([], {"calls": 1}, 720.0, None, timing=timing)
    text = "\n".join(RS.render(s))
    assert "Работа и ожидание: 10 мин (+ 2 мин ожидания)" in text, text
    assert "Качество доступа LLM" in text and "32.4 ток/с" in text, text
    assert "4200 мс" in text and "TTFT нет данных" in text, text
    assert RS.parse([RS.emit(s)]) == [s], RS.emit(s)
    no_data = dict(timing, services={}, parallel={})
    text2 = "\n".join(RS.render(RS.from_agent([], {}, 1.0, None, timing=no_data)))
    assert "доступно потоков нет данных" in text2 and "TTFT нет данных" in text2, text2


@test
def test_adapter_stream_path_is_explicit(tmp: Path):
    """Адаптер пробует run_stream, а TTFT без потока оставляет null, не равным latency."""
    src = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    for token in ("run_stream(", "stream_text(delta=True", "ttft_ms", "tps_basis",
                  "except Exception"):
        assert token in src, f"в адаптере нет {token}"
    core = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    assert '"stream": bool(payload.get("stream"))' in core
    assert "metrics.setdefault(\"ttft_ms\", None)" in core


@test
def test_record_engine_run_writes_timing_through_the_real_function(tmp: Path):
    """P2-2: настоящий record_engine_run пишет timing, а не только helper."""
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    project = tmp / "project"
    (project / ".aurora" / "runs").mkdir(parents=True)
    ck.record_engine_run(str(project), ["движок подготовлен"], {"ok": True},
                         "2026-11-21T10:00:00Z", 12.0)
    archives = ck.run_archive(str(project), limit=1)
    assert archives, "record_engine_run не создал прогон"
    meta = json.loads((Path(archives[0]["path"]).parent / "meta.json").read_text(
        encoding="utf-8"))
    assert meta["seconds"] == 12 and meta["timing"]["v"] == 1, meta
    assert meta["timing"]["active_ms"] == 12000, meta["timing"]
    assert meta["timing"]["quality"]["wait_known"] is False, meta["timing"]


@test
def test_runner_uses_one_run_id_and_writes_timing(tmp: Path):
    """Имя прогона у usage.d и timing.d одно; helper действительно пишет файл."""
    sys.path.insert(0, str(KIT / "scripts"))
    AR = importlib.import_module("agent_runner")
    AG = _agent()
    run_dir = tmp / "run"
    run_dir.mkdir()
    (run_dir / "timing.jsonl").write_text("", encoding="utf-8")
    with _Env(AURORA_RUN_DIR=str(run_dir)):
        assert AR.run_id_for("build") == "run", AR.run_id_for("build")
        AG.TIMING.reset()
        AG.TIMING.note_span("call", service="llm", status="ok", dur_ms=1500,
                            metrics={"latency_ms": 1500})
        timing = AR.timing_summary(time.time() - 2, time.time())
        assert timing["work_ms"] == 1500 and "llm" in timing["services"], timing
        project = tmp / "project"
        (project / "AuroraKnowledgeDB").mkdir(parents=True)
        AR.log_timing(str(project), "build", AR.run_id_for("build"), timing,
                      timing["services"])
        second = tmp / "run2"
        second.mkdir()
        with _Env(AURORA_RUN_DIR=str(second)):
            AG.TIMING.reset()
            AG.TIMING.note_span("call", service="llm", dur_ms=10)
        assert (second / "timing.jsonl").is_file(), "reset не перечитал папку прогона"
    rows = list((project / "AuroraKnowledgeDB/meta/timing.d").glob("*/run.json"))
    assert rows, "helper не записал timing.d"
