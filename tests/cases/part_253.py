"""Проверки части 253: замер запросов Confluence и Jira (S4-ATL).

Каркас — tests/harness.py. Настоящая сеть не нужна: HTTP-сервер поднимается на петле,
а экспорт Jira/Confluence проверяется подставными клиентом и зеркалом. Проверяются ровно
спаны, которые лейн выпускает: имя, сервис, операция, статус, причина, код, байты,
попытки, повторы и итоги фазы. `timing.record` и `service_metrics` не трогаются.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import os
import socket
import struct
import sys
import threading
from pathlib import Path

from harness import SCRIPTS, test

sys.path.insert(0, str(SCRIPTS))

SECRET = "Bearer top-secret-token"


def _mod(name: str):
    return importlib.import_module(name)


@contextlib.contextmanager
def _run_dir(path: Path):
    """Папка прогона на время проверки: `Tracer` пишет `timing.jsonl` только при ней."""
    path.mkdir(parents=True, exist_ok=True)
    old_dir = os.environ.get("AURORA_RUN_DIR")
    old_id = os.environ.get("AURORA_RUN_ID")
    os.environ["AURORA_RUN_DIR"] = str(path)
    os.environ["AURORA_RUN_ID"] = "s4-atl-test"
    try:
        yield path / "timing.jsonl"
    finally:
        for key, old in (("AURORA_RUN_DIR", old_dir), ("AURORA_RUN_ID", old_id)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _spans(log: Path) -> list:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _of_kind(rows: list, kind: str) -> list:
    return [row for row in rows if row.get("kind") == kind]


class _Server:
    """Настоящий HTTP-сервер на петле: очередь кодов по пути, тело и обрыв связи."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self
        self.routes: dict = {}
        self.hits: list = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                outer.serve(self)

            def do_POST(self):
                outer.serve(self)

            def do_PUT(self):
                outer.serve(self)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def serve(self, handler) -> None:
        path = handler.path.split("?", 1)[0]
        self.hits.append((handler.command, path))
        length = int(handler.headers.get("Content-Length") or 0)
        if length:
            handler.rfile.read(length)
        route = self.routes.get(path)
        if route == "reset":
            try:
                handler.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
            handler.close_connection = True
            handler.connection.close()
            return
        if isinstance(route, list):
            code = route.pop(0) if len(route) > 1 else route[0]
            body = b'{"ok": true}'
        elif isinstance(route, tuple):
            code, body = route
        else:
            code, body = 200, b'{"ok": true}'
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@contextlib.contextmanager
def _server():
    server = _Server()
    try:
        yield server
    finally:
        server.close()


@contextlib.contextmanager
def _quiet_sleep():
    """Повтор не должен спать по-настоящему: проверяем счёт попыток, а не паузу."""
    import time
    real = time.sleep
    time.sleep = lambda seconds: None
    try:
        yield
    finally:
        time.sleep = real


def _call_and_catch(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — проверяем и сам факт отказа
        return e
    raise AssertionError("запрос должен был отказать")


@test
def test_s4_atl_rest_get_200_writes_one_call_span_with_code_bytes_and_latency(tmp: Path):
    """GET 200: один `call` http.get, сервис confluence, код, точные байты и время ответа."""
    SC = _mod("sources_core")
    with _server() as server:
        server.routes["/page"] = (200, b'{"id": "7"}')
        with _run_dir(tmp / "run") as log:
            api = SC.RestApi(server.url(), SECRET, service="confluence")
            assert api.get("/page?token=top-secret-token") == {"id": "7"}
        rows = _spans(log)
        text = log.read_text(encoding="utf-8")
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["kind"], row["name"], row["service"], row["operation"]) == \
        ("call", "http.get", "confluence", "get"), row
    assert row["status"] == "ok" and row.get("reason", "") == "", row
    assert row["metrics"]["http_status"] == 200, row
    assert row["metrics"]["bytes"] == len(b'{"id": "7"}'), row
    assert isinstance(row["metrics"]["latency_ms"], (int, float)), row
    assert not _of_kind(rows, "attempt"), "GET не повторяется: попыток быть не должно"
    for forbidden in (SECRET, "top-secret-token", "127.0.0.1", "http://", "?token"):
        assert forbidden not in text, (forbidden, text)
    assert api._calls == 1 and api._bytes == len(b'{"id": "7"}')


@test
def test_s4_atl_rest_get_maps_every_http_failure_to_contract_status_and_reason(tmp: Path):
    """404/401/403/429/500: статус, причина и код по таблице 4.1; GET не повторяет."""
    SC = _mod("sources_core")
    cases = ((404, "error", "http_404"), (401, "auth", "http_401"),
             (403, "auth", "http_403"), (429, "throttled", "http_429"),
             (500, "unavailable", "http_5xx"))
    with _server() as server:
        for code, status, reason in cases:
            server.routes[f"/{code}"] = (code, b"{}")
            with _run_dir(tmp / f"run-{code}") as log:
                api = SC.RestApi(server.url(), SECRET, service="confluence")
                _call_and_catch(lambda c=code: api.get(f"/{c}"))
            rows = _spans(log)
            assert len(rows) == 1, (code, rows)
            row = rows[0]
            assert (row["kind"], row["name"]) == ("call", "http.get"), row
            assert (row["status"], row["reason"], row["metrics"]["http_status"]) == \
                (status, reason, code), (code, row)
            assert row["metrics"]["bytes"] == 0, row
            assert not _of_kind(rows, "attempt"), (code, "GET не повторяется")


@test
def test_s4_atl_rest_fetch_200_and_reset_are_one_call_each(tmp: Path):
    """fetch: 200 отдаёт точные байты и код; обрыв связи — `unavailable`/`reset` без кода."""
    SC = _mod("sources_core")
    body = b"attachment-body"
    with _server() as server:
        server.routes["/file"] = (200, body)
        server.routes["/reset"] = "reset"
        with _run_dir(tmp / "ok") as log_ok:
            api = SC.RestApi(server.url(), SECRET, service="jira")
            assert api.fetch("/file") == body
        with _run_dir(tmp / "reset") as log_reset:
            api = SC.RestApi(server.url(), SECRET, service="jira")
            _call_and_catch(lambda: api.fetch("/reset"))
        ok_rows, reset_rows = _spans(log_ok), _spans(log_reset)
    assert len(ok_rows) == 1 and not _of_kind(ok_rows, "attempt"), ok_rows
    ok = ok_rows[0]
    assert (ok["name"], ok["service"], ok["operation"]) == ("http.fetch", "jira", "fetch"), ok
    assert (ok["status"], ok["metrics"]["http_status"], ok["metrics"]["bytes"]) == \
        ("ok", 200, len(body)), ok
    assert len(reset_rows) == 1 and not _of_kind(reset_rows, "attempt"), reset_rows
    reset = reset_rows[0]
    assert (reset["status"], reset["reason"], reset["metrics"]["http_status"]) == \
        ("unavailable", "reset", 0), reset


@test
def test_s4_atl_rest_fetch_accepts_a_response_without_status_attribute(tmp: Path):
    """Старый подставной ответ без `.status` и `getcode` не роняет fetch: код 0, байты точные."""
    import io
    SC = _mod("sources_core")

    class Resp(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Api(SC.RestApi):
        secret_prefix = "CONFLUENCE"

        def _url(self, path):
            return "https://wiki.example.com" + path

    real = SC.safe_urlopen
    try:
        SC.safe_urlopen = lambda req, timeout=0: Resp(b"x" * 7)
        with _run_dir(tmp / "run") as log:
            api = Api("https://wiki.example.com", "")
            assert api.fetch("/f") == b"x" * 7
        rows = _spans(log)
    finally:
        SC.safe_urlopen = real
    assert len(rows) == 1, rows
    assert rows[0]["metrics"]["http_status"] == 0, rows[0]
    assert rows[0]["metrics"]["bytes"] == 7 and rows[0]["status"] == "ok", rows[0]


@test
def test_s4_atl_rest_send_retries_after_500_and_keeps_each_attempt(tmp: Path):
    """500, затем 200: один call и две попытки; у первой свой отказ, у call — итог и retries."""
    SC = _mod("sources_core")
    body = b'{"ok": true}'
    with _server() as server, _quiet_sleep():
        server.routes["/write"] = [500, 200]
        with _run_dir(tmp / "run") as log:
            api = SC.RestApi(server.url(), SECRET, service="jira")
            assert api.send("POST", "/write", {"a": 1}) == {"ok": True}
        rows = _spans(log)
    calls, attempts = _of_kind(rows, "call"), _of_kind(rows, "attempt")
    assert len(calls) == 1 and len(attempts) == 2, rows
    call = calls[0]
    assert (call["name"], call["service"], call["operation"]) == ("http.send", "jira", "send"), call
    assert call["status"] == "ok" and call.get("reason", "") == "", call
    assert call["metrics"]["http_status"] == 200 and call["metrics"]["retries"] == 1, call
    assert "bytes" not in call["metrics"], "байты живут только на попытке"
    first, second = attempts
    assert (first["attempt"], first["status"], first["reason"]) == (1, "unavailable", "http_5xx"), first
    assert (first["metrics"]["http_status"], first["metrics"]["bytes"]) == (500, 0), first
    assert (second["attempt"], second["status"]) == (2, "ok"), second
    assert (second["metrics"]["http_status"], second["metrics"]["bytes"]) == (200, len(body)), second
    assert first["parent_id"] == second["parent_id"] == call["span_id"], (first, second, call)


@test
def test_s4_atl_rest_send_uses_all_tries_for_429_and_reset_with_retries_count(tmp: Path):
    """429 и обрыв связи: все попытки, итоговый call — последний отказ, `retries = N-1`."""
    SC = _mod("sources_core")
    with _server() as server, _quiet_sleep():
        server.routes["/limit"] = (429, b"{}")
        server.routes["/reset"] = "reset"
        with _run_dir(tmp / "limit") as log_limit:
            api = SC.RestApi(server.url(), SECRET, service="jira")
            _call_and_catch(lambda: api.send("POST", "/limit", {}, tries=3))
        with _run_dir(tmp / "reset") as log_reset:
            api = SC.RestApi(server.url(), SECRET, service="jira")
            _call_and_catch(lambda: api.send("PUT", "/reset", {}, tries=2))
        limit_rows, reset_rows = _spans(log_limit), _spans(log_reset)
    for rows, tries, status, reason in ((limit_rows, 3, "throttled", "http_429"),
                                        (reset_rows, 2, "unavailable", "reset")):
        calls, attempts = _of_kind(rows, "call"), _of_kind(rows, "attempt")
        assert len(calls) == 1 and len(attempts) == tries, rows
        assert calls[0]["status"] == status and calls[0]["reason"] == reason, calls[0]
        assert calls[0]["metrics"]["retries"] == tries - 1, calls[0]
        assert [a["attempt"] for a in attempts] == list(range(1, tries + 1)), attempts
        assert all(a["status"] == status and a["reason"] == reason for a in attempts), attempts


@test
def test_s4_atl_rest_without_service_emits_no_span_and_keeps_old_positional_call(tmp: Path):
    """Старый вызов `RestApi(base, auth, root)` без сервиса работает и спанов не пишет."""
    SC = _mod("sources_core")
    with _server() as server:
        with _run_dir(tmp / "run") as log:
            api = SC.RestApi(server.url(), SECRET, str(tmp))
            assert api.service == "" and api.get("/x") == {"ok": True}
        assert not log.exists() or not _spans(log)
        with _run_dir(tmp / "named") as named:
            api = SC.RestApi(server.url(), SECRET, service="confluence")
            assert api.get("/x") == {"ok": True}
        assert _spans(named)[0]["service"] == "confluence"
    params = inspect.signature(SC.RestApi.__init__).parameters
    assert params["service"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["tracer"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["root"].default == ""


@test
def test_s4_atl_rest_infers_service_from_secret_prefix(tmp: Path):
    """Сервис выводится из привязки ключа: CONFLUENCE → confluence, JIRA → jira."""
    SC = _mod("sources_core")

    class Conf(SC.RestApi):
        secret_prefix = "CONFLUENCE"

    class Jir(SC.RestApi):
        secret_prefix = "JIRA"

    with _server() as server:
        server.routes["/x"] = (200, b"{}")
        for cls, service in ((Conf, "confluence"), (Jir, "jira")):
            with _run_dir(tmp / service) as log:
                cls(server.url(), SECRET).get("/x")
            rows = _spans(log)
            assert len(rows) == 1 and rows[0]["service"] == service, (service, rows)


@test
def test_s4_atl_a_broken_tracer_never_breaks_a_request(tmp: Path):
    """Ошибка инструментирования глотается на границе: запрос отвечает как без замера."""
    SC = _mod("sources_core")

    class Broken:
        def span(self, *args, **kwargs):
            raise RuntimeError("tracer недоступен")

    with _server() as server:
        server.routes["/x"] = [200, 200]
        with _run_dir(tmp / "run") as log:
            api = SC.RestApi(server.url(), SECRET, service="confluence", tracer=Broken())
            assert api.get("/x") == {"ok": True}
            assert api.send("POST", "/x", {}) == {"ok": True}
        assert not log.exists() or not _spans(log)


@test
def test_s4_atl_confluence_export_phase_has_pages_bytes_and_calls(tmp: Path):
    """Фаза confluence.export: pages из записей, bytes_total и calls_total из счётчиков."""
    CE = _mod("confluence_export")

    class FakeApi:
        def __init__(self, base, auth):
            self.base, self.auth = base, auth
            self._calls, self._bytes = 3, 120
            self.service = ""

        def _url(self, path):
            return self.base + path

        def page(self, page_id):
            return {"id": page_id, "ancestors": []}

    class FakeExporter:
        def __init__(self, *args, **kwargs):
            self.records = [("1", "a.md", "A", "NEW"), ("2", "b.md", "B", "NEW")]

        @staticmethod
        def new_stat():
            return {"pages": 0, "depth": 0, "parents": 0, "title": ""}

        def walk(self, page_id, ancestors):
            pass

    real_api, real_exporter = CE.Api, CE.Exporter
    CE.Api, CE.Exporter = FakeApi, FakeExporter
    try:
        with _run_dir(tmp / "run") as log:
            exp = CE.run_export({"base_url": "https://wiki.example.com", "space": "S"},
                                ["1"], str(tmp / "out"), "", False)
        rows = _spans(log)
    finally:
        CE.Api, CE.Exporter = real_api, real_exporter
    assert isinstance(exp, FakeExporter)
    assert len(rows) == 1, rows
    phase = rows[0]
    assert (phase["kind"], phase["name"], phase["service"], phase["operation"]) == \
        ("phase", "confluence.export", "confluence", "export"), phase
    assert phase["metrics"]["pages"] == 2, phase
    assert phase["metrics"]["bytes_total"] == 120, phase
    assert phase["metrics"]["calls_total"] == 3, phase
    assert "issues" not in phase["metrics"] and "calls" not in phase["metrics"]
    assert list(inspect.signature(CE.run_export).parameters) == \
        ["cfg", "roots", "out", "auth", "force"]


@test
def test_s4_atl_jira_export_phase_has_issues_bytes_and_calls(tmp: Path):
    """Фаза jira.export: issues из строк выгрузки, bytes_total и calls_total из счётчиков."""
    JE = _mod("jira_export")
    issue = {"key": "PRJ-1", "fields": {"summary": "Задача", "updated": "2026-10-19T10:00:00.000+0000",
                                        "status": {"name": "Готово"}}}

    class FakeApi:
        def __init__(self, base, auth):
            self._calls, self._bytes = 2, 64
            self.service = ""

        def epic_field(self):
            return ""

        def search(self, jql, fields, limit=0):
            return [issue]

        def issue_title(self, key):
            return ""

        def comments(self, key):
            return []

    real_api = JE.Api
    JE.Api = FakeApi
    try:
        with _run_dir(tmp / "run") as log:
            rows, written, skipped = JE.run_export(
                {"base_url": "https://jira.example.com"}, "", str(tmp / "out"),
                "project = PRJ", 0, True, False)
        spans = _spans(log)
    finally:
        JE.Api = real_api
    assert (len(rows), written, skipped) == (1, 1, 0), (rows, written, skipped)
    assert len(spans) == 1, spans
    phase = spans[0]
    assert (phase["kind"], phase["name"], phase["service"], phase["operation"]) == \
        ("phase", "jira.export", "jira", "export"), phase
    assert phase["metrics"]["issues"] == 1, phase
    assert phase["metrics"]["bytes_total"] == 64, phase
    assert phase["metrics"]["calls_total"] == 2, phase
    assert "pages" not in phase["metrics"]
    assert list(inspect.signature(JE.run_export).parameters) == \
        ["cfg", "auth", "out_dir", "jql", "limit", "force", "with_comments"]


@test
def test_s4_atl_verify_suppresses_spans_and_restores_the_flag(tmp: Path):
    """`--verify` не пишет спаны; флаг снимается в finally и при исключении."""
    CE = _mod("confluence_export")
    JE = _mod("jira_export")

    class FakeExporter:
        def __init__(self, *args, **kwargs):
            self.records: list = []

        @staticmethod
        def new_stat():
            return {"pages": 0, "depth": 0, "parents": 0, "title": ""}

        def walk(self, page_id, ancestors):
            pass

    class ConfApi:
        def __init__(self, base, auth):
            self._calls = self._bytes = 0
            self.service = ""

        def _url(self, path):
            return path

        def page(self, page_id):
            return {"ancestors": []}

    for module in (CE, JE):
        seen = []
        real_verify = module.verify
        try:
            def fake_verify(run, skip=()):
                seen.append(module._VERIFY)
                run("into")
                seen.append(module._VERIFY)
                return 0

            module.verify = fake_verify
            assert module._verify_export(lambda into: seen.append(("cb", module._VERIFY))) == 0
            assert seen == [True, ("cb", True), True], (module.__name__, seen)
            assert module._VERIFY is False, module.__name__

            def boom(run, skip=()):
                raise RuntimeError("прогон упал")

            module.verify = boom
            try:
                module._verify_export(lambda into: None)
            except RuntimeError:
                pass
            else:
                raise AssertionError("исключение должно пройти наружу")
            assert module._VERIFY is False, (module.__name__, "флаг не снят после исключения")
        finally:
            module.verify = real_verify

    real_api, real_exporter = CE.Api, CE.Exporter
    CE.Api, CE.Exporter = ConfApi, FakeExporter
    CE._VERIFY = True
    try:
        with _run_dir(tmp / "run") as log:
            CE.run_export({"base_url": "https://wiki.example.com", "space": "S"},
                          [], str(tmp / "out"), "", False)
        assert not log.exists() or not _spans(log)
    finally:
        CE._VERIFY = False
        CE.Api, CE.Exporter = real_api, real_exporter


@test
def test_s4_atl_export_survives_broken_instrumentation(tmp: Path):
    """Сломанный tracer не меняет результат экспорта ни в Confluence, ни в Jira."""
    CE, JE = _mod("confluence_export"), _mod("jira_export")

    class Broken:
        def span(self, *args, **kwargs):
            raise RuntimeError("tracer недоступен")

    class FakeExporter:
        def __init__(self, *args, **kwargs):
            self.records = []

        @staticmethod
        def new_stat():
            return {"pages": 0, "depth": 0, "parents": 0, "title": ""}

        def walk(self, page_id, ancestors):
            pass

    class ConfApi:
        def __init__(self, base, auth):
            self._calls = self._bytes = 0
            self.service = ""

        def _url(self, path):
            return path

    class JiraApi:
        def __init__(self, base, auth):
            self._calls = self._bytes = 0
            self.service = ""

        def epic_field(self):
            return ""

        def search(self, jql, fields, limit=0):
            return []

    saved = (CE.Api, CE.Exporter, CE._new_tracer, JE.Api, JE._new_tracer)
    CE.Api, CE.Exporter, CE._new_tracer = ConfApi, FakeExporter, lambda: Broken()
    JE.Api, JE._new_tracer = JiraApi, lambda: Broken()
    try:
        exp = CE.run_export({"base_url": "https://wiki.example.com", "space": "S"},
                            [], str(tmp / "c"), "", False)
        assert isinstance(exp, FakeExporter)
        rows, written, skipped = JE.run_export({"base_url": "https://jira.example.com"}, "",
                                              str(tmp / "j"), "project = PRJ", 0, True, False)
        assert (rows, written, skipped) == ([], 0, 0)
    finally:
        CE.Api, CE.Exporter, CE._new_tracer, JE.Api, JE._new_tracer = saved


@test
def test_s4_atl_span_summary_has_no_timing_problems(tmp: Path):
    """Сводка из выпущенных спанов проходит `timing.timing_problems` без замечаний."""
    SC, T = _mod("sources_core"), _mod("timing")
    with _server() as server, _quiet_sleep():
        server.routes["/ok"] = (200, b"{}")
        server.routes["/bad"] = [500, 200]
        with _run_dir(tmp / "run") as log:
            api = SC.RestApi(server.url(), SECRET, service="confluence")
            api.get("/ok")
            api.send("POST", "/bad", {}, tries=2)
        rows = _spans(log)
    assert len(rows) == 4, rows
    summary = T.summarize(rows)
    assert T.timing_problems(summary) == [], summary
    assert summary["wait_by_service"]["confluence"] > 0
    assert summary["wait_by_cause"].get("http_5xx", 0) > 0, summary["wait_by_cause"]
