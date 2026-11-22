"""Проверки части 254: спаны веб-выгрузки, проверок связи и эмбеддингов (CONTRACT.md v2).

Сеть только локальная: HTTP-сервер в тесте отдаёт 200, 304, 403, 404, 429, 500 и
обрывает соединение. Проверяются точные имена спанов, сервисы, статусы, причины,
байты запроса и ответа, задержки и число попыток по разделу 4 контракта.
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import REGISTRY, SCRIPTS, set_home, test

sys.path.insert(0, str(SCRIPTS))


def _timing():
    import importlib
    return importlib.import_module("timing")


def _rows(run_dir) -> list:
    path = Path(run_dir) / "timing.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _trace(tmp: Path, name: str):
    T = _timing()
    run_dir = tmp / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return T.Tracer(run_dir=str(run_dir), run_id=name), run_dir


def _kind(rows: list, kind: str) -> list:
    return [row for row in rows if row.get("kind") == kind]


def _one(rows: list, **match) -> dict:
    found = [row for row in rows
             if all(row.get(key) == value for key, value in match.items())]
    assert len(found) == 1, (match, found)
    return found[0]


def _by_http(rows: list, code: int) -> dict:
    found = [row for row in rows if row.get("metrics", {}).get("http_status") == code]
    assert len(found) == 1, (code, found)
    return found[0]


@contextlib.contextmanager
def _server():
    state = {"hits": 0, "flaky": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _reset(self) -> None:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

        def do_GET(self):
            state["hits"] += 1
            path = self.path.split("?", 1)[0]
            if path == "/ok":
                self._send(200, b"hello")
            elif path == "/not-modified":
                self.send_response(304)
                self.end_headers()
            elif path == "/forbidden":
                self._send(403, b"no")
            elif path == "/missing":
                self._send(404, b"missing")
            elif path == "/throttle":
                self._send(429, b"slow down")
            elif path == "/boom":
                self._send(500, b"boom")
            elif path == "/reset":
                self._reset()
            elif path == "/flaky":
                state["flaky"] += 1
                if state["flaky"] < 3:
                    self._reset()
                else:
                    self._send(200, b"recovered")
            else:
                self._send(404, b"")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1], state
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(2)


@contextlib.contextmanager
def _private(module, port: int):
    old = list(getattr(module, "ALLOW_PRIVATE", []))
    module.ALLOW_PRIVATE = [f"127.0.0.1:{port}"]
    try:
        yield
    finally:
        module.ALLOW_PRIVATE = old


@contextlib.contextmanager
def _web_tracer(tracer):
    import web_export as W
    old = W._TRACER
    W._TRACER = tracer
    try:
        yield W
    finally:
        W._TRACER = old


@contextlib.contextmanager
def _run_dir(path: Path):
    old = os.environ.get("AURORA_RUN_DIR")
    os.environ["AURORA_RUN_DIR"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = old


# --------------------------------------------------------------------- web_export

@test
def test_web_get_maps_statuses_reasons_and_bytes(tmp: Path):
    """200, 304, 403, 404, 429, 500 и reset дают точные status, reason, http_status и bytes."""
    import web_export as W

    tracer, run_dir = _trace(tmp, "web")
    with _server() as (port, _state), _private(W, port), _web_tracer(tracer), \
            patch.object(W.time, "sleep", lambda *_a: None):
        base = f"http://127.0.0.1:{port}"
        assert W._get(base + "/ok?token=TOPSECRET", "", 2, meta={}) == (b"hello", "", "")
        assert W._get(base + "/not-modified", "", 2,
                      headers={"If-None-Match": "x"}, meta={})[2] == W.NOT_MODIFIED
        W._get(base + "/forbidden", "", 2, meta={})
        W._get(base + "/missing", "", 2, meta={})
        W._get(base + "/throttle", "", 2, meta={})
        W._get(base + "/boom", "", 2, meta={})
        W._get(base + "/reset", "", 2, meta={})
    rows = _rows(run_dir)
    attempts = _kind(rows, "attempt")
    assert len(attempts) == 9, attempts
    assert all(r["name"] == "http.get" and r["service"] == "web"
               and r["operation"] == "get" for r in attempts), attempts
    ok = _by_http(attempts, 200)
    assert ok["status"] == "ok" and ok.get("reason", "") == "", ok
    assert ok["metrics"]["bytes"] == 5 and ok["metrics"]["bytes_basis"] == "response", ok
    assert ok["metrics"]["attempt"] == 1 and ok["metrics"]["retry_wait_ms"] == 0, ok
    assert ok["metrics"]["latency_ms"] >= 0 and ok["dur_ms"] >= ok["metrics"]["latency_ms"], ok
    not_modified = _by_http(attempts, 304)
    assert not_modified["status"] == "ok" and not_modified.get("reason", "") == "", not_modified
    assert not_modified["metrics"]["bytes"] == 0, not_modified
    forbidden = _by_http(attempts, 403)
    assert forbidden["status"] == "auth" and forbidden["reason"] == "http_403", forbidden
    assert "bytes" not in forbidden["metrics"], forbidden
    missing = _by_http(attempts, 404)
    assert missing["status"] == "error" and missing["reason"] == "http_404", missing
    throttled = _by_http(attempts, 429)
    assert throttled["status"] == "throttled" and throttled["reason"] == "http_429", throttled
    boom = _by_http(attempts, 500)
    assert boom["status"] == "unavailable" and boom["reason"] == "http_5xx", boom
    resets = [r for r in attempts if r.get("reason") == "reset"]
    assert len(resets) == 3 and resets[-1]["metrics"]["http_status"] == 0, resets
    text = json.dumps(rows, ensure_ascii=False)
    assert "TOPSECRET" not in text and "?" not in text, "секрет или строка запроса попали в спаны"


@test
def test_web_get_retries_and_planned_retry_wait(tmp: Path):
    """Сброс повторяется, retry_wait_ms — план RETRY_WAIT * attempt, а не измеренные часы."""
    import web_export as W

    tracer, run_dir = _trace(tmp, "web")
    meta: dict = {}
    with _server() as (port, state), _private(W, port), _web_tracer(tracer), \
            patch.object(W.time, "sleep", lambda *_a: None):
        data, _enc, err = W._get(f"http://127.0.0.1:{port}/flaky", "", 2, meta=meta)
    assert data == b"recovered" and err == "", (data, err)
    assert state["hits"] == 3, state
    attempts = _kind(_rows(run_dir), "attempt")
    assert len(attempts) == 3, attempts
    assert [r["status"] for r in attempts] == ["unavailable", "unavailable", "ok"], attempts
    assert [r["metrics"]["retry_wait_ms"] for r in attempts] == [5000, 10000, 0], attempts
    assert "bytes" in attempts[2]["metrics"] and attempts[2]["metrics"]["bytes"] == 9, attempts
    assert meta["_attempts"] == 3 and meta["_status"] == "ok" and meta["_bytes"] == 9, meta
    assert meta["_http_status"] == 200 and meta["_retry_wait_ms"] == 0, meta


@test
def test_web_get_reset_exhausts_retries(tmp: Path):
    """После третьего обрыва ошибка старая, а спаны показывают ровно три попытки."""
    import web_export as W

    tracer, run_dir = _trace(tmp, "web")
    with _server() as (port, _state), _private(W, port), _web_tracer(tracer), \
            patch.object(W.time, "sleep", lambda *_a: None):
        data, _enc, err = W._get(f"http://127.0.0.1:{port}/reset", "", 2, meta={})
    assert data == b"" and err.endswith("(попыток: 3)"), err
    attempts = _kind(_rows(run_dir), "attempt")
    assert len(attempts) == 3, attempts
    assert all(r["status"] == "unavailable" and r["reason"] == "reset" for r in attempts), attempts
    assert all("bytes" not in r["metrics"] for r in attempts), attempts


@test
def test_web_fetch_call_keeps_bytes_on_attempt_only(tmp: Path):
    """fetch открывает http.fetch-вызов, bytes остаются на листе http.get."""
    import web_export as W

    tracer, run_dir = _trace(tmp, "web")
    meta: dict = {}
    with _server() as (port, _state), _private(W, port), _web_tracer(tracer):
        text, err = W.fetch(f"http://127.0.0.1:{port}/ok", meta=meta)
    assert text == "hello" and err == "", (text, err)
    rows = _rows(run_dir)
    call = _one(rows, kind="call", name="http.fetch")
    assert call["service"] == "web" and call["operation"] == "fetch", call
    assert call["status"] == "ok" and call.get("reason", "") == "", call
    assert call["metrics"]["http_status"] == 200, call
    assert call["metrics"]["latency_ms"] >= 0, call
    assert "bytes" not in call["metrics"], call
    attempt = _one(rows, kind="attempt", name="http.get")
    assert attempt["metrics"]["bytes"] == 5, attempt


@test
def test_web_fetch_asset_call_span(tmp: Path):
    """fetch_asset открывает http.fetch с operation fetch_asset и сохраняет файл."""
    import web_export as W

    tracer, run_dir = _trace(tmp, "asset")
    out = tmp / "mirror"
    with _server() as (port, _state), _private(W, port), _web_tracer(tracer):
        name, err = W.fetch_asset(f"http://127.0.0.1:{port}/ok", str(out), True, "файл")
    assert name and err == "", (name, err)
    assert (out / W.ASSET_DIR / name).read_bytes() == b"hello"
    call = _one(_rows(run_dir), kind="call", name="http.fetch")
    assert call["operation"] == "fetch_asset" and call["metrics"]["http_status"] == 200, call
    assert "bytes" not in call["metrics"], call


@test
def test_web_fetch_304_call_reason_empty(tmp: Path):
    """304 — ответ «не менялась»: call http.fetch получает ok и пустую причину."""
    import web_export as W

    tracer, run_dir = _trace(tmp, "web")
    with _server() as (port, _state), _private(W, port), _web_tracer(tracer):
        text, err = W.fetch(f"http://127.0.0.1:{port}/not-modified",
                            known={"etag": "v1"})
    assert text == "" and err == W.NOT_MODIFIED, (text, err)
    call = _one(_rows(run_dir), kind="call", name="http.fetch")
    assert call["status"] == "ok" and call.get("reason", "") == "", call
    assert call["metrics"]["http_status"] == 304, call


@test
def test_web_run_phase_span_and_verify_silence(tmp: Path):
    """run даёт фазу web.export с totals, а --verify не пишет спанов."""
    import web_export as W

    run_dir = tmp / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(out=str(tmp / "out"), apply=False, prune=False,
                              verify=False, force=True, )
    with _server() as (port, _state), _run_dir(run_dir):
        saved_cfg, saved_pause = W.config_text, W.PAUSE
        W.config_text = lambda: (
            f"web:\n  allow_private: [127.0.0.1]\n  pages:\n"
            f"    - url: http://127.0.0.1:{port}/ok\n      trusted: true\n")
        W.PAUSE = 0
        try:
            assert W.run(args) == 0
            assert W._TRACER is None, "run не снял приватный трассер"
            phase = _one(_rows(run_dir), kind="phase", name="web.export")
            assert phase["service"] == "web" and phase["operation"] == "export", phase
            assert phase["metrics"]["pages"] == 1, phase
            assert phase["metrics"]["calls_total"] >= 1, phase
            assert phase["metrics"]["bytes_total"] >= 5, phase
            # --verify: тот же конфиг, но без спанов
            (run_dir / "timing.jsonl").unlink()
            argv = sys.argv
            sys.argv = ["web_export.py", "--verify", "--out", str(tmp / "verified")]
            try:
                assert W.main() == 0
            finally:
                sys.argv = argv
            assert not (run_dir / "timing.jsonl").exists(), "--verify написал спаны"
        finally:
            W.config_text, W.PAUSE = saved_cfg, saved_pause


@test
def test_web_without_or_with_broken_tracer(tmp: Path):
    """Без трассера спанов нет; сломанный трассер не меняет результат."""
    import web_export as W

    class Boom:
        def span(self, *_args, **_kwargs):
            raise RuntimeError("tracer is broken")

    with _server() as (port, _state), _private(W, port):
        with _web_tracer(None):
            data, _enc, err = W._get(f"http://127.0.0.1:{port}/ok", "", 2)
        assert data == b"hello" and err == "", (data, err)
        with _web_tracer(Boom()):
            data, _enc, err = W._get(f"http://127.0.0.1:{port}/ok", "", 2)
        assert data == b"hello" and err == "", (data, err)
    assert not list(tmp.rglob("timing.jsonl")), "модуль написал спаны без трассера"


@test
def test_web_signatures_match_contract(tmp: Path):
    """Публичные сигнатуры web_export не получили tracer-параметров."""
    import web_export as W

    assert list(inspect.signature(W._get).parameters) == [
        "url", "token", "timeout", "headers", "meta", "max_bytes"]
    assert list(inspect.signature(W.fetch).parameters) == ["url", "token", "known", "meta"]
    assert list(inspect.signature(W.fetch_asset).parameters) == [
        "url", "out_dir", "apply", "label"]
    assert list(inspect.signature(W.run).parameters) == ["a"]
    assert W._TRACER is None


# ------------------------------------------------------------------- connectivity

@test
def test_probe_call_spans_map_status_and_latency(tmp: Path):
    """run_probe даёт call probe с probe_ms, latency_ms, code и http_status."""
    import connectivity as C

    tracer, run_dir = _trace(tmp, "probe")
    with _server() as (port, _state):
        base = f"http://127.0.0.1:{port}"
        ok = C.run_probe({"id": "ok", "type": "http", "url": base + "/ok?token=TOPSECRET",
                          "expect_body": "hello"}, 2, tracer=tracer)
        forbidden = C.run_probe({"id": "forbidden", "type": "http",
                                 "url": base + "/forbidden"}, 2, tracer=tracer)
        missing = C.run_probe({"id": "missing", "type": "http",
                               "url": base + "/missing"}, 2, tracer=tracer)
        throttle = C.run_probe({"id": "throttle", "type": "http",
                                "url": base + "/throttle"}, 2, tracer=tracer)
        boom = C.run_probe({"id": "boom", "type": "http",
                            "url": base + "/boom"}, 2, tracer=tracer)
        reset = C.run_probe({"id": "reset", "type": "http",
                             "url": base + "/reset"}, 2, tracer=tracer)
    assert ok["ok"] and ok["http_status"] == 200, ok
    rows = _rows(run_dir)
    spans = _kind(rows, "call")
    assert len(spans) == 6, spans
    assert all(r["name"] == "probe" and r["service"] == "network"
               and r["operation"] == "probe" for r in spans), spans
    span_ok = _one(spans, status="ok")
    assert span_ok["metrics"]["probe_ms"] == ok["ms"], span_ok
    assert span_ok["metrics"]["latency_ms"] == ok["ms"], span_ok
    assert span_ok["metrics"]["http_status"] == 200, span_ok
    assert _one(spans, reason="http_403")["status"] == "auth"
    assert _one(spans, reason="http_404")["status"] == "error"
    assert _one(spans, reason="http_429")["status"] == "throttled"
    assert _one(spans, reason="http_5xx")["status"] == "unavailable"
    assert _one(spans, reason="reset")["status"] == "unavailable"
    assert not forbidden["ok"] and not missing["ok"] and not throttle["ok"] and not boom["ok"]
    assert not reset["ok"]
    assert "TOPSECRET" not in json.dumps(rows, ensure_ascii=False), rows


@test
def test_run_probes_phase_and_late_span(tmp: Path):
    """probe.batch — фаза, а late и занятый probe получают call probe с reason late."""
    import connectivity as C

    tracer, run_dir = _trace(tmp, "probes")
    cfg = {"settings": {"timeout": 1}}
    state = {"release": threading.Event()}

    def slow(_p, _timeout):
        state["release"].wait(1)
        return {"ok": True, "code": "ok", "ms": 1000, "detail": ""}

    busy = threading.Thread(target=lambda: state["release"].wait(1), daemon=True)
    busy.start()
    old_threads = dict(C._PROBE_THREADS)
    try:
        C._PROBE_THREADS["busy"] = busy
        late = C.run_probes([{"id": "late", "type": "tcp", "host": "h", "port": 1}],
                            cfg, budget=0.05, checkers={"tcp": slow}, tracer=tracer)
        skipped = C.run_probes([{"id": "busy", "type": "tcp", "host": "h", "port": 1}],
                               cfg, budget=0.05, checkers={"tcp": slow}, tracer=tracer)
    finally:
        state["release"].set()
        busy.join(2)
        C._PROBE_THREADS.clear()
        C._PROBE_THREADS.update(old_threads)
    assert late["late"]["code"] == "late" and late["late"]["late"], late
    assert skipped["busy"]["code"] == "late" and skipped["busy"]["late"], skipped
    rows = _rows(run_dir)
    phases = _kind(rows, "phase")
    assert len(phases) == 2, phases
    assert all(p["name"] == "probe.batch" and p["service"] == "network"
               and p["metrics"]["probes"] == 1 and p["metrics"]["budget_ms"] == 50
               for p in phases), phases
    late_spans = [r for r in _kind(rows, "call") if r.get("reason") == "late"]
    assert len(late_spans) == 2, late_spans
    for span in late_spans:
        assert span["status"] == "timeout" and span["attempt"] == 1, span
        assert span["metrics"]["probe_ms"] == 50, span
        assert span["metrics"]["latency_ms"] == 50, span
        assert span["metrics"]["code"] == "late", span


@test
def test_probe_without_tracer_writes_nothing(tmp: Path):
    """Без tracer проверка отвечает, timing.jsonl не появляется."""
    import connectivity as C

    with _server() as (port, _state):
        res = C.run_probes([{"id": "ok", "type": "http",
                             "url": f"http://127.0.0.1:{port}/ok"}],
                           {"settings": {"timeout": 2}}, budget=3)
    assert res["ok"]["ok"] and res["ok"]["http_status"] == 200, res
    assert not list(tmp.rglob("timing.jsonl")), "модуль написал спаны без трассера"


def _service(tmp: Path, tracer=None, checker=None, name="state.json"):
    import connectivity as C

    cfg = {"enabled": True,
           "settings": {"interval": 60, "timeout": 2, "fail_after": 3, "ok_after": 2},
           "policy": {"auto": "partial_queue"},
           "profiles": [{"name": "p", "aggregate": "any_healthy", "quorum": 1,
                         "fail_after": 3, "ok_after": 2}],
           "internet": {},
           "probes": [{"id": "p1", "profile": "p", "type": "tcp", "host": "h", "port": 1}]}
    svc = C.AvailabilityService(
        providers_fn=list, state_file=str(tmp / name), tracer=tracer,
        checkers={"tcp": checker or (lambda _p, _t: {"ok": True, "code": "ok",
                                                     "ms": 5, "detail": ""})})
    svc._cfg, svc._cfg_key = cfg, None
    return svc


@test
def test_snapshot_timing_is_one_shot(tmp: Path):
    """timing живёт на снимке после прогона, затем снимается; без трассера ключа нет."""
    tracer, _run_dir = _trace(tmp, "snap")
    good = _service(tmp, tracer=tracer, name="state1.json")
    snap = good.check(force=True)
    timing = snap["timing"]
    assert timing["v"] == 1 and timing["source"] == "spans", timing
    assert timing["quality"]["wait_known"] is True and timing["active_ms"] is not None, timing
    assert abs(timing["work_ms"] + timing["service_wait_ms"]
               + timing["other_wait_ms"] + timing["unknown_ms"] - timing["active_ms"]) <= 1, timing
    assert "timing" not in good.snapshot(), "снимок без прогона показал старое timing"

    failed = _service(tmp, tracer=tracer, name="state2.json",
                      checker=lambda _p, _t: {"ok": False, "code": "timeout",
                                              "ms": 20, "detail": ""})
    snap2 = failed.check(force=True)
    assert snap2["timing"]["service_wait_ms"] >= 20, snap2["timing"]

    plain = _service(tmp, name="state3.json")
    snap3 = plain.check(force=True)
    assert "timing" not in snap3, "без трассера снимок выдумал timing"


@test
def test_connectivity_signatures_keyword_only(tmp: Path):
    """tracer и parent_id стоят после `*`; старые позиционные вызовы целы."""
    import connectivity as C

    params = inspect.signature(C.run_probe).parameters
    assert params["tracer"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["parent_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(C.run_probes).parameters)[:4] == [
        "probes", "cfg", "budget", "checkers"]
    assert inspect.signature(C.run_probes).parameters["tracer"].kind is \
        inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------- kb_embed

@test
def test_embed_spans_match_contract(tmp: Path):
    """embeddings.embed и embeddings.attempt: точные байты запроса, статус, модель, vectors."""
    import kb_embed as E

    restore = set_home(tmp / "home")
    tracer, run_dir = _trace(tmp, "embed")
    payloads = []

    def fake_http(url, payload, key, timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            return 500, None, "HTTP 500", 0.05
        return 200, {"data": [{"index": 0, "embedding": [1.0, 0.0]}],
                     "usage": {"prompt_tokens": 7}}, "", 0.07

    try:
        with patch.object(E, "load_index", lambda: {"dim": 2}), \
                patch.object(E, "endpoints", lambda _cfg: [
                    {"url": "http://a:1", "key": "", "n": 1},
                    {"url": "http://b:2", "key": "", "n": 2}]), \
                patch.object(E.AG, "http_json", fake_http):
            E.DEAD.clear()
            got = E.embed(["привет"], {"request_timeout": 1}, "m", tracer=tracer)
    finally:
        E.DEAD.clear()
        restore()
    assert got == [[1.0, 0.0]], got
    rows = _rows(run_dir)
    attempts = _kind(rows, "attempt")
    call = _one(rows, kind="call", name="embeddings.embed")
    assert len(attempts) == 2, attempts
    assert all(r["name"] == "embeddings.attempt" and r["service"] == "embeddings"
               and r["operation"] == "embed" for r in attempts), attempts
    first, second = attempts
    assert first["metrics"]["http_status"] == 500 and first["metrics"]["backend_index"] == 0, first
    assert second["metrics"]["http_status"] == 200 and second["metrics"]["backend_index"] == 1, second
    assert first["metrics"]["latency_ms"] == 50 and second["metrics"]["latency_ms"] == 70, attempts
    expected = len(json.dumps({"model": "m", "input": ["привет"]},
                              ensure_ascii=False).encode("utf-8"))
    assert first["metrics"]["bytes"] == expected == second["metrics"]["bytes"], attempts
    assert first["metrics"]["bytes_basis"] == "request", first
    assert "bytes_in" not in first["metrics"] and "bytes_out" not in first["metrics"], first
    assert call["metrics"]["vectors"] == 1 and call["metrics"]["tokens_in"] == 7, call
    assert call["metrics"]["model"] == "m" and call["metrics"]["retries"] == 1, call
    assert call["metrics"]["latency_ms"] == 120, call
    assert "bytes" not in call["metrics"], call


@test
def test_embed_maps_429_and_reset(tmp: Path):
    """429 и обрыв транспорта попадают в статусы и причины контракта."""
    import kb_embed as E

    restore = set_home(tmp / "home")
    tracer, run_dir = _trace(tmp, "embed")
    replies = [(429, None, "HTTP 429", 0.01),
               (None, None, "ConnectionResetError: reset", 0.02)]

    def fake_http(_url, _payload, _key, _timeout):
        return replies.pop(0)

    try:
        with patch.object(E, "load_index", lambda: {"dim": 1}), \
                patch.object(E, "endpoints", lambda _cfg: [
                    {"url": "http://a:1", "key": "", "n": 1}]), \
                patch.object(E.AG, "http_json", fake_http):
            E.DEAD.clear()
            assert E.embed(["x"], {"request_timeout": 1}, "m", tracer=tracer) == []
            E.DEAD.clear()
            assert E.embed(["y"], {"request_timeout": 1}, "m", tracer=tracer) == []
    finally:
        E.DEAD.clear()
        restore()
    calls = _kind(_rows(run_dir), "call")
    assert len(calls) == 2, calls
    assert calls[0]["status"] == "throttled" and calls[0]["reason"] == "http_429", calls
    assert calls[1]["status"] == "unavailable" and calls[1]["reason"] == "reset", calls


@test
def test_embed_without_tracer_writes_nothing(tmp: Path):
    """Без tracer вектора считаются, timing.jsonl не появляется."""
    import kb_embed as E

    restore = set_home(tmp / "home")
    try:
        with patch.object(E, "load_index", lambda: {"dim": 1}), \
                patch.object(E, "endpoints", lambda _cfg: [
                    {"url": "http://a:1", "key": "", "n": 1}]), \
                patch.object(E.AG, "http_json",
                             lambda *_a: (200, {"data": [{"index": 0,
                                                          "embedding": [1.0]}]}, "", 0.01)):
            E.DEAD.clear()
            assert E.embed(["x"], {"request_timeout": 1}, "m") == [[1.0]]
    finally:
        E.DEAD.clear()
        restore()
    assert not list(tmp.rglob("timing.jsonl")), "модуль написал спаны без трассера"


@test
def test_embed_signature_keyword_only(tmp: Path):
    """tracer у embed стоит после `*`, старый вызов без него цел."""
    import kb_embed as E

    params = inspect.signature(E.embed).parameters
    assert params["tracer"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(params)[:4] == ["texts", "cfg", "model", "partial"]


# --------------------------------------------------------------------- kb_index

@test
def test_kb_index_stays_uninstrumented(tmp: Path):
    """Контракт запрещает инструментировать kb_index: ни спанов, ни timing.d."""
    import kb_index as K

    kb = tmp / "AuroraKnowledgeDB"
    (kb / "Glossary").mkdir(parents=True)
    (kb / "Glossary" / "alpha.md").write_text(
        "---\nstatus: active\nkind: note\n---\n# Alpha\nТекст.\n", encoding="utf-8")
    run_dir = tmp / "run"
    argv = sys.argv
    try:
        with patch.object(K, "KB_ROOT", str(kb)), _run_dir(run_dir):
            sys.argv = ["kb_index.py", "--apply"]
            assert K.main() == 0
    finally:
        sys.argv = argv
    assert (kb / "Glossary" / "_index.md").is_file()
    assert not (run_dir / "timing.jsonl").exists(), "kb_index начал писать спаны"


if __name__ == "__main__":
    import shutil
    import tempfile
    import traceback

    from harness import forget_engine_state, run_cleanups

    passed = failed = 0
    for _name, _fn in list(REGISTRY):
        _td = Path(tempfile.mkdtemp(prefix="part254-"))
        try:
            forget_engine_state()
            _fn(_td)
            passed += 1
            print(f"  ✅ {_name}")
        except Exception as _exc:  # noqa: BLE001 — прямой запуск показывает все падения
            failed += 1
            traceback.print_exc()
            print(f"  ❌ {_name}: {_exc}")
        finally:
            run_cleanups()
            shutil.rmtree(_td, ignore_errors=True)
    print(f"Пройдено: {passed}/{passed + failed}")
    raise SystemExit(1 if failed else 0)
