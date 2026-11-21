"""Проверки части 248: токены вызова на всём пути и потоки адаптера (этап 2).

Первая проверка идёт через настоящий `call_role`: две попытки дают usage, и сумма
токенов должна дойти до `service_metrics`, `timing.d` и итога прогона. Вторая и
третья поднимают локальный OpenAI-совместимый сервер и запускают настоящий адаптер
Pydantic AI: поток с SSE даёт TTFT, отказ 400 на поток откатывается на обычный
запрос, а 401 — нет. Внешней сети и моделей нет.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, live_adapter_python, set_home, test

sys.path.insert(0, str(SCRIPTS))

from cases.part_247 import _agent, _cfg, _Env, _FastSem, _timing


class _Gateway:
    """Локальный OpenAI-совместимый шлюз: режим задаёт ответ на `stream=true`."""

    def __init__(self, mode: str):
        self.mode, self.seen = mode, []
        gw = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(size) or b"{}")
                gw.seen.append(bool(req.get("stream")))
                if req.get("stream") and gw.mode == "reject":
                    raw = json.dumps({"error": {"message": "stream is not supported"}}).encode(
                        "utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if req.get("stream") and gw.mode == "auth":
                    raw = json.dumps({"error": {"message": "bad key"}}).encode("utf-8")
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if req.get("stream") and gw.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    for text in ("При", "вет", "!"):
                        time.sleep(0.03)
                        chunk = {
                            "id": "1", "object": "chat.completion.chunk", "created": 1,
                            "model": req.get("model") or "m",
                            "choices": [{"index": 0, "delta": {"content": text},
                                         "finish_reason": None}],
                        }
                        self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False)
                                          + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    last = {
                        "id": "1", "object": "chat.completion.chunk", "created": 1,
                        "model": req.get("model") or "m",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    }
                    self.wfile.write(("data: " + json.dumps(last, ensure_ascii=False)
                                      + "\n\n").encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                body = {
                    "id": "1", "object": "chat.completion", "created": 1,
                    "model": req.get("model") or "m",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "целиком"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


_ADAPTER = r'''
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
import pydantic_ai_adapter as AD

async def go():
    run_task, _ = AD.runtime()
    base = {"url": sys.argv[2], "key": "", "model": "m", "timeout": 10,
            "messages": [{"role": "user", "content": "скажи привет"}]}
    out = await run_task(dict(base, stream=True))
    print("RESULT " + json.dumps({k: out.get(k) for k in
          ("ok", "text", "ttft_ms", "tps_basis", "status", "error")}, ensure_ascii=False))
asyncio.run(go())
'''


def _run_adapter(mode: str) -> tuple[dict, _Gateway]:
    gw = _Gateway(mode)
    vpy = live_adapter_python()
    if vpy is None:
        gw.close()
        return {}, gw
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env.update(NO_PROXY="127.0.0.1", PYTHONIOENCODING="utf-8")
    try:
        cp = subprocess.run(
            [str(vpy), "-c", _ADAPTER, str(KIT / "scripts" / "agents"), gw.url],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, stdin=subprocess.DEVNULL, check=False)
    finally:
        gw.close()
    assert cp.returncode == 0, cp.stderr[-800:]
    lines = [line for line in cp.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, cp.stdout[-800:] + cp.stderr[-400:]
    return json.loads(lines[-1][len("RESULT "):]), gw


@test
def test_call_role_tokens_reach_timing_d_and_summary(tmp: Path):
    """Токены двух попыток приходят в call, services, timing.d и строку скорости."""
    AG, T = _agent(), _timing()
    RS = importlib.import_module("run_summary")
    restore_home = set_home(tmp / "home")
    project, run_dir = tmp / "project", tmp / "run"
    (project / "AuroraKnowledgeDB").mkdir(parents=True)
    run_dir.mkdir()
    saved_sem = AG._slot_semaphore
    try:
        with _Env(AURORA_RUN_DIR=str(run_dir), AURORA_AGENT_CACHE="0"):
            AG.TIMING.reset()
            for d in (AG.DOWN, AG.DOWN_WHY, AG.COOL, AG.SLOW, AG.LAST_OK):
                d.clear()
            AG.LAST_OK[1] = time.time()
            AG.USAGE.update(calls=0, failed=0, tokens_in=0, tokens_out=0,
                            gen_seconds=0.0, errors={}, attempts=0, waited=0.0,
                            slot_wait_ms=0.0, backoff_ms=0.0, net_wait_ms=0.0,
                            by_service={})
            AG._slot_semaphore = lambda _backend, _cfg: _FastSem()
            calls = []

            def transport(kind, backend, payload, timeout):
                if kind == "slots":
                    return 200, {}, "", 0.0
                calls.append(1)
                if len(calls) == 1:
                    # Пустой ответ не отменяет расхода: попытка заплатила 2/1 токена.
                    return (200, {"choices": [{"message": {"content": ""},
                                               "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
                            "", 0.01)
                return (200, {"choices": [{"message": {"content": "ответ"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                              "aurora": {"via": "pydantic_ai", "latency_ms": 300,
                                         "ttft_ms": 120, "tps_basis": "stream"}},
                        "", 0.3)

            started = time.time()
            r = AG.call_role(_cfg(AG), "worker", [{"role": "user", "content": "?"}],
                             transport=transport, deadline=time.time() + 30,
                             sleep=lambda _seconds: None)
            ended = time.time()
            assert r["ok"] and len(calls) == 2, r
            assert r["tokens_in"] == 10 and r["tokens_out"] == 4, r
            assert r["timing"]["tokens_in"] == 12, r["timing"]
            assert r["timing"]["tokens_out"] == 5, r["timing"]
            assert r["timing"]["tps_basis"] == "stream", r["timing"]

            services = T.service_metrics(AG.TIMING.snapshot())
            llm = services["llm"]
            assert llm["tokens_in"] == 12 and llm["tokens_out"] == 5, llm
            assert llm["tps"] == 16.7 and llm["tps_basis"] == "stream", llm

            timing = AG.TIMING.summary(active_start=started, active_end=ended)
            timing["services"] = services
            path = T.record(str(project), "run-1", timing, services,
                            actor_id="tester", machine="local")
            assert path, "timing.d не записан"
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            assert payload["services"]["llm"]["tokens_in"] == 12, payload
            assert payload["services"]["llm"]["tokens_out"] == 5, payload
            assert payload["services"]["llm"]["tps_basis"] == "stream", payload

            summary = RS.from_agent([], {"calls": 1, "tokens_in": 14, "tokens_out": 5},
                                    1.0, None, timing=timing)
            lines = "\n".join(RS.render(summary))
            assert "16.7 ток/с" in lines, lines
            assert "скорость нет данных" not in lines, lines
    finally:
        AG._slot_semaphore = saved_sem
        restore_home()


@test
def test_adapter_stream_rejection_falls_back_to_plain_request(_tmp: Path):
    """Провайдер ответил 400 на `stream=true`: адаптер повторяет без потока, TTFT пуст."""
    out, gw = _run_adapter("reject")
    if not out:
        return
    assert out["ok"] is True, out
    assert out["text"] == "целиком", out
    assert out["ttft_ms"] is None and out["tps_basis"] == "request", out
    assert gw.seen == [True, False], gw.seen


@test
def test_adapter_stream_success_keeps_ttft_and_stream_basis(_tmp: Path):
    """Настоящий SSE-поток: первый чанк даёт TTFT, база скорости — stream."""
    out, gw = _run_adapter("stream")
    if not out:
        return
    assert out["ok"] is True, out
    assert out["text"] == "Привет!" or out["text"] == "Привет", out
    assert isinstance(out["ttft_ms"], (int, float)), out
    assert out["ttft_ms"] >= 0 and out["tps_basis"] == "stream", out
    assert gw.seen == [True], gw.seen


@test
def test_adapter_auth_rejection_does_not_fall_back(_tmp: Path):
    """401 — отказ ключу, а не потоку: обычного запроса быть не должно."""
    out, gw = _run_adapter("auth")
    if not out:
        return
    assert out["ok"] is False and out["status"] == 401, out
    assert gw.seen == [True], gw.seen
