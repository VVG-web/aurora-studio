"""Проверки части 251: `aurora-remote`, клиент панели и политика safe/full.

Настоящая панель не поднимается и реальные конфиги не читаются: HTTP-панель подставная,
токен — строка в тесте, `local/mcp.json` лежит во временном ките. Проверяются 17 схем,
ACL до вызова панели, девять запрещённых команд, destructive-маршруты, подтверждение
человека на fake panel, `remote_results`, `resume`, границы журнала и утечка токена.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import types
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

# aurora_guard.py принадлежит MCP-TOKENS. Если его ещё нет в ветке, для тестов
# подставляется минимальный fake: права и ACL проверяются по тем же правилам контракта,
# а журнал в тестах собирает сам сервер через параметр audit.
try:
    import aurora_guard  # noqa: F401
except ImportError:
    _guard = types.ModuleType("aurora_guard")

    class _AuthError(Exception):
        def __init__(self, status, code, message):
            self.status, self.code, self.message = status, code, message
            super().__init__(message)

    class _AuthContext:
        def __init__(self, kind="remote", token_id="", user="", scopes=frozenset(),
                     projects=(), client_ip="", vpn=False, mode="safe", source=""):
            self.kind = kind
            self.token_id = token_id
            self.user = user
            self.scopes = frozenset(scopes or ())
            self.projects = tuple(projects or ())
            self.client_ip = client_ip
            self.vpn = bool(vpn)
            self.mode = mode
            self.source = source

    class _RateLimiter:
        def __init__(self, limits=None):
            self._limits = limits or {}

        def allow(self, key, cost=1):
            return True, 0

    def _project_ok(ctx, project):
        return "*" in (ctx.projects or ()) or str(project or "") in (ctx.projects or ())

    def _require_scope(ctx, scope):
        if scope not in (ctx.scopes or ()):
            raise _AuthError(403, "scope", "нет права %s" % scope)

    def _audit_write(path, record):
        return None

    _guard.AuthError = _AuthError
    _guard.AuthContext = _AuthContext
    _guard.RateLimiter = _RateLimiter
    _guard.project_ok = _project_ok
    _guard.require_scope = _require_scope
    _guard.audit_write = _audit_write
    sys.modules["aurora_guard"] = _guard

import aurora_common as AC  # noqa: E402
import aurora_cockpit_client as CC  # noqa: E402
import aurora_remote_mcp as RM  # noqa: E402

TOKEN = "test-token-not-for-logs"
ALL = frozenset({"remote:read", "remote:write", "admin"})
DENY = set(AC.MCP_SAFE_DENY_COMMANDS)


def _canon(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _sha(value) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


class _Panel:
    """Подставная панель: только те endpoint'ы, которые нужны инструментам."""

    def __init__(self, tmp: Path, mode="safe", token=TOKEN):
        self.tmp = Path(tmp)
        self.mode = mode
        self.mode_overrides = {}
        self.token = token
        self.alpha = str(self.tmp / "alpha")
        self.other = str(self.tmp / "other")
        self.requests = []
        self.confirmations = {}
        self.counter = 0
        self.conflict = False
        self.run_statuses = ["done", "failed", "done"]
        self.fail_path = ""
        self.fail_message = ""
        self.net_blocked = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _headers_ok(self):
                got = self.headers.get("X-Aurora-Token") or ""
                if not got:
                    auth = self.headers.get("Authorization") or ""
                    if auth.startswith("Bearer "):
                        got = auth[len("Bearer "):]
                return got == outer.token

            def _send_json(self, payload, code=200):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_text(self, text, code=200):
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_zip(self, blob):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def _query(self):
                from urllib.parse import parse_qs, urlparse
                return urlparse(self.path).path, parse_qs(urlparse(self.path).query)

            def _body(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                path, query = self._query()
                outer.requests.append({"method": "GET", "path": path, "query": query,
                                       "body": None, "headers": dict(self.headers)})
                if not self._headers_ok():
                    self._send_json({"error": "сессия панели закончилась", "code": "session"}, 403)
                    return
                if outer.fail_path == path:
                    self._send_json({"error": outer.fail_message, "code": "internal"}, 500)
                    return
                if path == "/api/ping":
                    self._send_json({"app": "aurora-cockpit", "kit": "1.231.14",
                                     "pid": 4242, "ready": True})
                elif path == "/api/mcp/kit":
                    if outer.mode is None:
                        self._send_json({})
                    else:
                        self._send_json({"remote": {
                            "mode": outer.mode, "stored": outer.mode,
                            "projects": outer.mode_overrides,
                            "source": "file", "deny": sorted(DENY), "admin_network": False}})
                elif path == "/api/state":
                    part = (query.get("part") or [""])[0]
                    projects = [
                        {"name": "alpha", "path": outer.alpha, "activity": {"state": "idle"}},
                        {"name": "other", "path": outer.other, "activity": {"state": "busy"}},
                    ]
                    if part == "extra":
                        self._send_json({"commands": [
                            {"cmd": "kb:links", "ns": "kb", "what": "ссылки", "runnable": True},
                            {"cmd": "git:push", "ns": "git", "what": "отправка", "runnable": True},
                        ], "commands_total": 2})
                    else:
                        self._send_json({"kit": {"version": "1.231.14"}, "projects": projects})
                elif path == "/api/history":
                    statuses = outer.run_statuses
                    runs = [{"id": "r1", "kind": "command", "title": "kb:links",
                             "status": statuses[0], "started": "2026-11-21T10:00:00Z",
                             "finished": "2026-11-21T10:01:00Z", "seconds": 60,
                             "timing": {"v": 1, "total_ms": 60000}, "cmd": "kb:links"},
                            {"id": "r2", "kind": "command", "title": "git:push",
                             "status": statuses[1], "started": "2026-11-21T09:00:00Z",
                             "finished": "2026-11-21T09:01:00Z", "seconds": 60,
                             "timing": {"v": 1, "total_ms": 60000}, "cmd": "git:push"},
                            {"id": "r3", "kind": "route", "title": "Обновить",
                             "scId": "update", "status": statuses[2],
                             "started": "2026-11-21T08:00:00Z",
                             "finished": "2026-11-21T08:01:00Z", "seconds": 60,
                             "timing": {"v": 1, "total_ms": 60000}}]
                    self._send_json({"runs": runs})
                elif path == "/api/history/run":
                    run_id = (query.get("id") or [""])[0]
                    if run_id == "missing":
                        self._send_json({"error": "прогон не найден"})
                    else:
                        self._send_json({"entries": [{"k": "out", "s": "hello"}],
                                         "meta": {"status": "done",
                                                  "started": "2026-11-21T10:00:00Z",
                                                  "finished": "2026-11-21T10:01:00Z",
                                                  "seconds": 60,
                                                  "timing": {"v": 1, "total_ms": 60000}}})
                elif path == "/api/run/logs":
                    self._send_json({"archive": [{"id": "r1"}, {"id": "r2"},
                                                 {"id": "big"}]})
                elif path == "/api/run/file":
                    run_id = (query.get("run") or [""])[0]
                    if run_id == "missing":
                        self._send_json({"error": "архив прогона не найден"})
                    elif run_id == "big":
                        self._send_json({"text": "\n".join("line %03d" % i for i in range(500))})
                    else:
                        self._send_json({"text": "one\ntwo\nthree"})
                elif path == "/api/run/errors":
                    run_id = (query.get("run") or [""])[0]
                    if run_id == "missing":
                        self._send_json({"error": "прогон не найден", "code": "not_found"}, 404)
                    elif run_id == "empty":
                        self._send_json({"errors": [], "count": 0})
                    else:
                        self._send_json({"errors": [{"at": "2026-11-21T10:00:30Z",
                                                     "kind": "adapter",
                                                     "message": "boom"}], "count": 1})
                elif path == "/api/jobs":
                    project = (query.get("project") or [""])[0]
                    self._send_json({"jobs": [{"id": "j1", "cmd": "kb:links", "args": [],
                                               "started": 1, "project": project}]})
                elif path == "/api/live":
                    self._send_json({"routes": [
                        {"id": "route-1", "project": outer.alpha, "scId": "update"},
                        {"id": "route-other", "project": outer.other, "scId": "rebuild"},
                    ], "jobs": [], "cron": None, "tasks": {}})
                elif path == "/api/tasks":
                    project = (query.get("project") or [""])[0]
                    self._send_json({"active": [{"id": "t1", "project": project}],
                                     "queued": [], "max_parallel": 4})
                elif path == "/api/threads":
                    self._send_json({"rows": [{"project": "alpha", "label": "worker"},
                                              {"project": "other", "label": "other"}]})
                elif path == "/api/scenarios":
                    self._send_json({"scenarios": [
                        {"id": "update", "title": "Обновить", "write": True,
                         "destructive": False, "steps": [{"cmd": "kb:links"}]},
                        {"id": "fix", "title": "Починить", "write": True,
                         "destructive": False, "steps": [{"cmd": "kb:links"}]},
                        {"id": "write", "title": "Написать", "write": True,
                         "destructive": False, "steps": [{"cmd": "kb:links"}]},
                        {"id": "rebuild", "title": "Пересобрать", "write": True,
                         "destructive": True, "steps": []},
                        {"id": "deliver", "title": "Выпустить", "write": True,
                         "destructive": False, "steps": [{"cmd": "git:push"}]},
                    ]})
                elif path == "/api/cron":
                    self._send_json({"tasks": [
                        {"id": "c1", "project": outer.alpha, "name": "ночной", "enabled": True},
                        {"id": "c2", "project": outer.other, "name": "чужой", "enabled": True},
                        {"id": "c3", "name": "глобальный", "enabled": True},
                    ], "projects": [{"name": "alpha", "path": outer.alpha},
                                    {"name": "other", "path": outer.other}],
                        "routes": [], "bots": {outer.alpha: [{"id": "bot-1"}, {"id": "b1"}],
                                               outer.other: [{"id": "bot-2"}, {"id": "b2"}]}})
                elif path == "/api/artifacts":
                    kind = (query.get("kind") or [""])[0]
                    self._send_json({"files": [{"name": "a.md", "rel": "Reports/a.md",
                                                "kind": kind, "size": 10}]})
                elif path == "/api/report":
                    self._send_json({"reports": [{"id": "analyst", "stamp": "",
                                                  "output": {"path": "Reports/analyst.html",
                                                             "exists": True}}]})
                elif path == "/api/report/file":
                    self._send_text("<html>report</html>")
                elif path == "/api/kinds":
                    self._send_json({"kinds": {"report": {"out": "Reports"}}})
                elif path == "/api/job":
                    self._send_json({"id": "j1", "lines": [], "next": 0, "done": True})
                elif path == "/api/route/live":
                    self._send_json({"id": "route-1", "lines": [], "next": 0})
                elif path == "/api/health":
                    self._send_json({"ok": True, "project": (query.get("project") or [""])[0]})
                elif path == "/api/agent":
                    self._send_json({"agent": "ok"})
                elif path == "/api/net/state":
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "неизвестный маршрут", "code": "not_found"}, 404)

            def do_POST(self):
                path, query = self._query()
                body = self._body()
                outer.requests.append({"method": "POST", "path": path, "query": query,
                                       "body": body, "headers": dict(self.headers)})
                if not self._headers_ok():
                    self._send_json({"error": "сессия панели закончилась", "code": "session"}, 403)
                    return
                if outer.fail_path == path:
                    self._send_json({"error": outer.fail_message, "code": "internal"}, 500)
                    return
                if path == "/api/diagnostics":
                    blob = io.BytesIO()
                    with zipfile.ZipFile(blob, "w") as archive:
                        archive.writestr("summary.md", "# Диагностика\nстрока\n")
                        archive.writestr("state.json", "{}")
                    self._send_zip(blob.getvalue())
                elif path == "/api/mcp/confirm/request":
                    outer.counter += 1
                    request_id = "req-%d" % outer.counter
                    outer.confirmations[request_id] = {
                        "tool": body.get("tool"), "project": body.get("project"),
                        "digest": body.get("args_digest"), "used": False, "confirm_id": "",
                    }
                    self._send_json({"request_id": request_id,
                                     "expires_at": "2026-11-21T10:05:00Z",
                                     "args_digest": body.get("args_digest")})
                elif path == "/api/task/check":
                    self._send_json({"resources": [], "can_start": not outer.conflict,
                                     "conflict": "занято" if outer.conflict else ""})
                elif path == "/api/run":
                    verdict = outer._consume("remote_run_command", body)
                    if isinstance(verdict, tuple):
                        self._send_json({"error": verdict[1], "code": verdict[0]}, 409)
                        return
                    self._send_json({"job": "job-1", "state": "running"})
                elif path == "/api/route/run":
                    if outer.net_blocked:
                        self._send_json({"net_blocked": {"choices": ["queue", "cancel"]}}, 409)
                        return
                    verdict = outer._consume("remote_run_route", body)
                    if isinstance(verdict, tuple):
                        self._send_json({"error": verdict[1], "code": verdict[0]}, 409)
                        return
                    self._send_json({"route": "route-1"})
                elif path == "/api/restart":
                    verdict = outer._consume("remote_restart", body)
                    if isinstance(verdict, tuple):
                        self._send_json({"error": verdict[1], "code": verdict[0]}, 409)
                        return
                    self._send_json({"ok": True, "started": True})
                elif path in ("/api/cron/save", "/api/cron/delete"):
                    verdict = outer._consume("remote_cron", body)
                    if isinstance(verdict, tuple):
                        self._send_json({"error": verdict[1], "code": verdict[0]}, 409)
                        return
                    self._send_json({"ok": True})
                elif path in ("/api/cron/start", "/api/cron/stop", "/api/cron/toggle"):
                    self._send_json({"ok": True})
                elif path in ("/api/job/stop", "/api/task/stop", "/api/route/stop",
                              "/api/ask/control", "/api/make/control"):
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "неизвестный маршрут", "code": "not_found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def approve(self, request_id):
        item = self.confirmations[request_id]
        item["confirm_id"] = "confirm-" + request_id
        return item["confirm_id"]

    def _slug(self, project_path):
        if project_path == self.alpha:
            return "alpha"
        if project_path == self.other:
            return "other"
        return ""

    def _consume(self, tool, body):
        """None — подтверждение не нужно; (code, message) — отказ; True — принято."""
        confirm_id = str(body.get("confirm_id") or "")
        if not confirm_id:
            return True
        item = next((row for row in self.confirmations.values()
                     if row.get("confirm_id") == confirm_id), None)
        if item is None or item.get("used"):
            return "confirmation_expired", "подтверждение истекло"
        if item.get("tool") != tool:
            return "confirmation_mismatch", "подтверждение не совпало"
        project = str(item.get("project") or "")
        if tool == "remote_run_command":
            action = {"cmd": body.get("cmd"), "args": body.get("args") or [],
                      "resume": bool(body.get("resume"))}
        elif tool == "remote_run_route":
            action = {k: v for k, v in body.items() if k not in ("project", "confirm_id")}
            action["resume"] = bool(action.get("resume"))
            action["write"] = bool(action.get("write"))
            action["net"] = str(action.get("net") or "")
        elif tool == "remote_cron":
            action = {k: v for k, v in body.items() if k not in ("project", "confirm_id")}
        else:
            action = {}
        expected = AC.confirmation_digest(tool, project, action)
        if item.get("digest") != expected:
            return "confirmation_mismatch", "подтверждение не совпало"
        item["used"] = True
        return True

    def calls(self, path):
        return [row for row in self.requests if row["path"] == path]


class _Env:
    def __init__(self, tmp: Path, *, panel_mode="safe", local_mode="safe",
                 scopes=ALL, projects=("alpha",), commands=None, conflict=False,
                 rate_limiter=None, token=TOKEN):
        self.tmp = Path(tmp)
        self.kit = self.tmp / "kit"
        (self.kit / "local").mkdir(parents=True, exist_ok=True)
        (self.tmp / "alpha").mkdir(exist_ok=True)
        (self.tmp / "other").mkdir(exist_ok=True)
        if local_mode is not None:
            (self.kit / "local" / "mcp.json").write_text(
                json.dumps({"auroraRemote": {"mode": local_mode}}), encoding="utf-8")
        self.panel = _Panel(self.tmp, mode=panel_mode, token=token)
        self.panel.conflict = conflict
        self.client = CC.CockpitClient(base_url=self.panel.url, token=token, kit=self.kit)
        self.ctx = RM.AuthContext(kind="remote", token_id="tok-1", user="tester",
                                  scopes=frozenset(scopes), projects=tuple(projects),
                                  client_ip="127.0.0.1", mode=local_mode or "safe")
        self.audit = []
        self.server = RM.RemoteServer(
            self.client, self.ctx, kit=self.kit,
            commands=set(commands) if commands is not None else set(DENY) | {"kb:links"},
            audit=lambda path, record: self.audit.append(record),
            rate_limiter=rate_limiter)
        self.alpha = str(self.tmp / "alpha")
        self.other = str(self.tmp / "other")

    def call(self, name, **kwargs):
        payload = dict(kwargs)
        envelope = self.server.call_tool(name, payload)
        if name in RM.TOOL_SCHEMAS:
            errors = RM.validate_schema(RM.TOOL_SCHEMAS[name]["outputSchema"], envelope)
            assert not errors, (name, errors, envelope)
        return envelope

    def close(self):
        self.panel.close()


def _run(tmp: Path, **kwargs):
    env = _Env(tmp, **kwargs)
    return env


def _ok(env, name, **args):
    got = env.call(name, **args)
    assert got.get("ok") is True, (name, got)
    return got


def _err(env, name, code, **args):
    got = env.call(name, **args)
    assert got.get("ok") is False, (name, got)
    assert got.get("error", {}).get("code") == code, (name, code, got)
    return got


# --------------------------------------------------------------------- схемы
@test
def test_mcp_remote_tools_match_contract_schemas(_tmp: Path):
    assert len(RM.TOOLS) == 17
    names = [tool["name"] for tool in RM.TOOLS]
    assert names[:10] == list(RM.READ_ONLY)
    assert names[10:] == list(RM.MANAGEMENT)
    subset = [{"name": item["name"], "inputSchema": item["inputSchema"],
               "outputSchema": item["outputSchema"]} for item in RM.SCHEMAS["tools"]]
    assert _sha(subset) == "2e6237674d034a658431b86b49b4e2097b055b48ea03822a78663be0a0791c8b"
    assert _sha(RM.SCHEMAS) == "9477659a83115115e9ed8b912c46a47ef298da4f3d310ccbeabaf3bcd6f2145b"
    for tool in RM.TOOLS:
        assert tool["inputSchema"]["additionalProperties"] is False, tool["name"]
        assert "outputSchema" in tool and "description" in tool


@test
def test_mcp_remote_mode_helper_is_fail_closed(tmp: Path):
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = kit / "local" / "mcp.json"
    path.write_text(json.dumps({"auroraRemote": {
        "mode": "safe", "projects": {"alpha": "full"}}}), encoding="utf-8")
    assert AC.kit_mcp_mode("alpha", kit) == ("full", "project")
    assert AC.kit_mcp_mode("other", kit) == ("safe", "file")
    path.write_text(json.dumps({"auroraRemote": {
        "mode": "full", "projects": {"alpha": "banana"}}}), encoding="utf-8")
    assert AC.kit_mcp_mode("alpha", kit) == ("safe", "broken")
    assert AC.kit_mcp_mode("other", kit) == ("full", "file")
    path.write_text(json.dumps({"auroraRemote": {
        "mode": "full", "projects": []}}), encoding="utf-8")
    assert AC.kit_mcp_mode("alpha", kit) == ("safe", "broken")
    path.write_text("{", encoding="utf-8")
    assert AC.kit_mcp_mode("alpha", kit) == ("safe", "broken")
    path.unlink()
    assert AC.kit_mcp_mode("alpha", kit) == ("safe", "default")
    assert AC.personal_kit_file(os.path.join(str(KIT), "local", "mcp-tokens.json")) is True
    assert AC.confirmation_digest("t", "p", {"b": 2, "a": 1}) == \
        AC.confirmation_digest("t", "p", {"a": 1, "b": 2})
    assert AC.confirmation_digest("t", "p", {"a": 1}) != \
        AC.confirmation_digest("t", "other", {"a": 1})


@test
def test_mcp_remote_unknown_panel_mode_is_safe(tmp: Path):
    env = _run(tmp, panel_mode=None, local_mode="full")
    try:
        got = _ok(env, "remote_status")
        assert got["meta"]["mode"] == "safe", got
        assert got["data"]["remote_mode"] == "safe", got
        assert any(row["result"] == "mode_mismatch" for row in env.audit), env.audit
    finally:
        env.close()


@test
def test_mcp_remote_broken_panel_override_is_safe(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        env.panel.mode_overrides = {"alpha": "banana"}
        got = _ok(env, "remote_runs", project="alpha")
        assert got["meta"]["mode"] == "safe", got
        assert any(row["result"] == "mode_mismatch" for row in env.audit), env.audit
    finally:
        env.close()


@test
def test_mcp_remote_rejects_unknown_tool_and_field(tmp: Path):
    env = _run(tmp)
    try:
        got = env.call("remote_nope")
        assert got["error"]["code"] == "invalid_argument", got
        got = env.call("remote_runs", project="alpha", bogus=1)
        assert got["error"]["code"] == "invalid_argument", got
        assert not env.panel.requests, "схема проверяется до вызова панели"
    finally:
        env.close()


# --------------------------------------------------------------------- safe/full
@test
def test_mcp_remote_safe_denies_all_nine_commands(tmp: Path):
    env = _run(tmp)
    try:
        assert set(AC.MCP_SAFE_DENY_COMMANDS) == DENY
        assert len(DENY) == 9
        for cmd in sorted(DENY):
            got = _err(env, "remote_run_command", "safe_mode", project="alpha", cmd=cmd)
            assert "details" not in got["error"], got
        assert not env.panel.calls("/api/run"), "safe не должен запускать запрещённую команду"
        assert not env.panel.calls("/api/task/check"), "проверка очереди тоже не нужна"
    finally:
        env.close()


@test
def test_mcp_remote_safe_allows_non_destructive_runs(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_run_command", project="alpha", cmd="kb:links", args=["--apply"])
        assert got["data"]["can_start"] is True, got
        post = env.panel.calls("/api/run")
        assert len(post) == 1 and post[0]["body"]["args"] == ["--apply"], post
        for route in ("update", "fix", "write"):
            got = _ok(env, "remote_run_route", project="alpha", route=route)
            assert got["data"]["route"] == "route-1", got
        assert len(env.panel.calls("/api/route/run")) == 3
    finally:
        env.close()


@test
def test_mcp_remote_safe_rejects_destructive_and_denylist_routes(tmp: Path):
    env = _run(tmp)
    try:
        _err(env, "remote_run_route", "safe_mode", project="alpha", route="rebuild")
        _err(env, "remote_run_route", "safe_mode", project="alpha", route="deliver")
        assert not env.panel.calls("/api/route/run")
    finally:
        env.close()


@test
def test_mcp_remote_full_irreversible_needs_confirmation(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        got = _err(env, "remote_run_command", "confirmation_required",
                   project="alpha", cmd="git:push", args=["--force"])
        details = got["error"]["details"]
        assert details["request_id"] and details["expires_at"] and details["args_digest"], details
        assert not env.panel.calls("/api/run"), "без confirm_id запуск не начинается"
        confirm_id = env.panel.approve(details["request_id"])
        got = _ok(env, "remote_run_command", project="alpha", cmd="git:push",
                  args=["--force"], confirm_id=confirm_id)
        assert got["data"]["job"] == "job-1", got
        body = env.panel.calls("/api/run")[-1]["body"]
        assert body["args"] == ["--force"] and body["confirm_id"] == confirm_id, body
        # Обычная команда в full не требует подтверждения.
        got = _ok(env, "remote_run_command", project="alpha", cmd="kb:links")
        assert got["data"]["job"] == "job-1", got
    finally:
        env.close()


@test
def test_mcp_remote_full_irreversible_requires_admin(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full",
               scopes=frozenset({"remote:read", "remote:write"}))
    try:
        _err(env, "remote_run_command", "scope", project="alpha", cmd="git:push")
        _err(env, "remote_run_route", "scope", project="alpha", route="rebuild")
    finally:
        env.close()


@test
def test_mcp_remote_confirmation_is_single_use(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        got = _err(env, "remote_run_command", "confirmation_required",
                   project="alpha", cmd="git:push")
        confirm_id = env.panel.approve(got["error"]["details"]["request_id"])
        _ok(env, "remote_run_command", project="alpha", cmd="git:push", confirm_id=confirm_id)
        _err(env, "remote_run_command", "confirmation_expired",
             project="alpha", cmd="git:push", confirm_id=confirm_id)
    finally:
        env.close()


@test
def test_mcp_remote_confirmation_mismatch(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        got = _err(env, "remote_run_command", "confirmation_required",
                   project="alpha", cmd="git:push")
        confirm_id = env.panel.approve(got["error"]["details"]["request_id"])
        _err(env, "remote_run_command", "confirmation_mismatch",
             project="alpha", cmd="git:update", confirm_id=confirm_id)
    finally:
        env.close()


# --------------------------------------------------------------------- запуск
@test
def test_mcp_remote_run_command_uses_argv_and_registry(tmp: Path):
    env = _run(tmp)
    try:
        _ok(env, "remote_run_command", project="alpha", cmd="kb:links",
            args=["--apply", "two words"])
        check = env.panel.calls("/api/task/check")[-1]["body"]
        run = env.panel.calls("/api/run")[-1]["body"]
        assert check["args"] == ["--apply", "two words"], check
        assert run["args"] == ["--apply", "two words"], run
        assert isinstance(run["args"], list) and "shell" not in run, run
        _err(env, "remote_run_command", "invalid_argument", project="alpha", cmd="nope")
        _err(env, "remote_run_command", "invalid_argument", project="alpha",
             cmd="kb:links; rm -rf /")
        assert len(env.panel.calls("/api/run")) == 1
    finally:
        env.close()


@test
def test_mcp_remote_run_command_conflict_still_reports_state(tmp: Path):
    env = _run(tmp, conflict=True)
    try:
        got = _err(env, "remote_run_command", "conflict", project="alpha", cmd="kb:links")
        assert got["data"] == {"job": None, "conflict": True, "can_start": False}, got
        assert not env.panel.calls("/api/run")
    finally:
        env.close()


@test
def test_mcp_remote_runs_filter_and_cursor(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_runs", project="alpha", status="failed")
        assert [row["id"] for row in got["data"]["runs"]] == ["r2"], got
        got = _ok(env, "remote_runs", project="alpha", limit=1)
        assert [row["id"] for row in got["data"]["runs"]] == ["r1"], got
        assert got["next"] == "r2" and got["meta"]["truncated"] is True, got
        got = _ok(env, "remote_runs", project="alpha", since="r1")
        assert [row["id"] for row in got["data"]["runs"]] == ["r2", "r3"], got
    finally:
        env.close()


# --------------------------------------------------------------------- результаты
@test
def test_mcp_remote_results_only_requested_keys(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_results", project="alpha", include=["run", "history"])
        assert set(got["data"]) == {"run", "history"}, got
        assert got["data"]["history"]["meta"]["timing"]["total_ms"] == 60000, got
        got = _ok(env, "remote_results", project="alpha",
                  include=["artifacts", "report", "kinds"])
        assert set(got["data"]) == {"artifacts", "report", "kinds"}, got
        assert got["data"]["artifacts"][0]["path"] == "Reports/a.md", got
        assert not got["data"]["artifacts"][0]["path"].startswith("/")
        assert got["data"]["report"]["text"] == "<html>report</html>", got
        assert got["data"]["kinds"] == ["report"], got
        got = _ok(env, "remote_results", project="alpha", include=["run"])
        assert set(got["data"]) == {"run"}, got
    finally:
        env.close()


@test
def test_mcp_remote_run_rejects_resume_but_run_actions_accept_it(tmp: Path):
    env = _run(tmp)
    try:
        _err(env, "remote_run", "invalid_argument", project="alpha", run_id="r1", resume=True)
        got = _ok(env, "remote_run_command", project="alpha", cmd="kb:links", resume=True)
        assert got["data"]["can_start"] is True, got
        assert env.panel.calls("/api/run")[-1]["body"]["resume"] == 1
        got = _ok(env, "remote_run_route", project="alpha", route="update", resume=True)
        assert got["data"]["route"] == "route-1", got
        assert env.panel.calls("/api/route/run")[-1]["body"]["resume"] == 1
    finally:
        env.close()


@test
def test_mcp_remote_resume_without_prior_run_is_not_found(tmp: Path):
    env = _run(tmp)
    try:
        env.panel.run_statuses = ["running", "running", "running"]
        _err(env, "remote_run_command", "not_found", project="alpha",
             cmd="kb:links", resume=True)
        _err(env, "remote_run_route", "not_found", project="alpha",
             route="update", resume=True)
        env.panel.run_statuses = ["done", "done", "done"]
        _err(env, "remote_run_command", "not_found", project="alpha",
             cmd="kb:links", args=["--other"], resume=True)
        _err(env, "remote_run_route", "not_found", project="alpha",
             route="other", resume=True)
        assert not env.panel.calls("/api/run")
        assert not env.panel.calls("/api/route/run")
    finally:
        env.close()


@test
def test_mcp_remote_resume_mismatch_does_not_ask_confirmation(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        env.panel.run_statuses = ["done", "done", "done"]
        _err(env, "remote_run_command", "not_found", project="alpha",
             cmd="git:update", resume=True)
        assert not env.panel.calls("/api/mcp/confirm/request"), "нечего подтверждать"
        assert not env.panel.calls("/api/run")
    finally:
        env.close()


# --------------------------------------------------------------------- журнал/ошибки
@test
def test_mcp_remote_log_caps_lines_and_bytes(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_log", project="alpha", run_id="big")
        data = got["data"]
        assert len(data["lines"]) <= 200, len(data["lines"])
        assert sum(len(line.encode("utf-8")) + 1 for line in data["lines"]) <= 32768
        assert got["next"] and got["meta"]["truncated"] is True, got
        got = _ok(env, "remote_log", project="alpha", run_id="big",
                  offset=int(got["next"]), max_bytes=100)
        assert got["data"]["lines"], got
        assert sum(len(line.encode("utf-8")) + 1 for line in got["data"]["lines"]) <= 100, got
        got = _ok(env, "remote_log", project="alpha", run_id="r1")
        assert got["data"]["lines"] == ["one", "two", "three"], got
        assert got["next"] == "" and got["meta"]["truncated"] is False, got
    finally:
        env.close()


@test
def test_mcp_remote_run_errors_helper_hides_secrets(tmp: Path):
    """Серверный helper читает только тексты ошибок: без промптов, путей и токенов."""
    import aurora_cockpit as cockpit

    project = tmp / "proj"
    project.mkdir()
    run = Path(cockpit.runs_dir(str(project))) / "20260101-000000"
    run.mkdir(parents=True)
    (run / "failures.jsonl").write_text("\n".join([
        json.dumps({"at": "1", "kind": "stage",
                    "error": "see https://example.com/run?token=abc123"}),
        "not json",
        json.dumps({"at": "2", "message": "failed at /Users/me/secret",
                    "prompt": "SECRET PROMPT"}),
        json.dumps({"ts": "3", "stage": "run",
                    "detail": {"why": "Authorization: Bearer abc123",
                               "prompt": "SECRET PROMPT"}}),
        json.dumps({"at": "4", "error": r"C:\Users\me\secret.txt and %TEMP%\x"}),
        json.dumps({"at": "/Users/me/x", "kind": "token=abc", "error": "boom"}),
    ]) + "\n", encoding="utf-8")
    got = cockpit.read_run_errors(str(project), "20260101-000000")
    assert got["count"] == 5, got
    assert got["errors"][0] == {"at": "1", "kind": "stage", "message": "see [url]"}, got
    assert got["errors"][1]["message"] == "failed at [path]", got
    text = json.dumps(got, ensure_ascii=False)
    assert "SECRET" not in text and "/Users/me" not in text and "abc123" not in text, text
    assert "secret.txt" not in text and "%TEMP%" not in text, text
    assert got["errors"][2]["kind"] == "run", got
    assert cockpit.read_run_errors(str(project), "missing")["code"] == "not_found"
    assert cockpit.read_run_errors(str(project), "20260101-000000", 1)["count"] == 1


@test
def test_mcp_remote_errors_uses_server_endpoint_and_rejects_path(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_errors", project="alpha", run_id="r1")
        assert got["data"]["count"] == 1 and got["data"]["errors"][0]["kind"] == "adapter", got
        assert env.panel.calls("/api/run/errors"), "ошибки читает сервер панели"
        _err(env, "remote_errors", "invalid_argument", project="alpha", run_id="../secret")
        _err(env, "remote_errors", "invalid_argument", project="alpha", run_id="..")
        got = _ok(env, "remote_errors", project="alpha", run_id="empty")
        assert got["data"] == {"errors": [], "count": 0}, got
        _err(env, "remote_errors", "not_found", project="alpha", run_id="missing")
    finally:
        env.close()


# --------------------------------------------------------------------- ACL
@test
def test_mcp_remote_project_acl_filters_and_blocks_before_panel(tmp: Path):
    env = _run(tmp, projects=("alpha",))
    try:
        got = _ok(env, "remote_projects")
        assert [row["name"] for row in got["data"]["projects"]] == ["alpha"], got
        before = len(env.panel.requests)
        _err(env, "remote_runs", "project", project="other")
        assert len(env.panel.requests) == before, "ACL проверяется до вызова панели"
        got = _ok(env, "remote_runs", project="alpha")
        assert got["data"]["runs"], got
        history = env.panel.calls("/api/history")[-1]
        assert history["query"]["project"] == [env.alpha], history
        got = _ok(env, "remote_queue")
        assert got["data"]["jobs"], got
        for call in env.panel.calls("/api/jobs"):
            assert call["query"]["project"] == [env.alpha], call
        assert not any(call["query"].get("project") == [env.other]
                       for call in env.panel.requests if isinstance(call.get("query"), dict))
    finally:
        env.close()


@test
def test_mcp_remote_queue_and_commands(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_queue")
        assert set(got["data"]) == {"jobs", "tasks", "threads"}, got
        assert got["data"]["threads"] == [{"project": "alpha", "label": "worker"}], got
        got = _ok(env, "remote_commands", group="kb")
        assert [c["name"] for c in got["data"]["commands"]] == ["kb:links"], got
        assert got["data"]["commands"][0]["runnable"] is True
        got = _ok(env, "remote_commands")
        assert {c["name"] for c in got["data"]["commands"]} == {"kb:links", "git:push"}, got
    finally:
        env.close()


# --------------------------------------------------------------------- режим
@test
def test_mcp_remote_server_safe_wins_over_local_full(tmp: Path):
    env = _run(tmp, panel_mode="safe", local_mode="full")
    try:
        got = _ok(env, "remote_status")
        assert got["data"]["mode"] == "safe", got
        assert got["data"]["remote_mode"] == "safe", got
        assert any(row.get("result") == "mode_mismatch" for row in env.audit), env.audit
        _err(env, "remote_run_command", "safe_mode", project="alpha", cmd="git:push")
    finally:
        env.close()


@test
def test_mcp_remote_status_reports_panel_and_mode(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        got = _ok(env, "remote_status")
        data = got["data"]
        assert data["app"] == "aurora-cockpit" and data["kit"] == "1.231.14", data
        assert data["pid"] == 4242 and data["ready"] is True and data["panel_ok"] is True
        assert data["mode"] == "full" and data["remote_mode"] == "full", data
        assert env.panel.calls("/api/ping") and env.panel.calls("/api/mcp/kit")
    finally:
        env.close()


# --------------------------------------------------------------------- cron/restart
@test
def test_mcp_remote_cron_and_restart_need_full_and_confirmation(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        _ok(env, "remote_cron", project="alpha", action="start", job="c1")
        assert env.panel.calls("/api/cron/start")
        got = _err(env, "remote_cron", "confirmation_required",
                   project="alpha", action="save", job="c1", spec={"name": "новый"})
        confirm_id = env.panel.approve(got["error"]["details"]["request_id"])
        got = _ok(env, "remote_cron", project="alpha", action="save", job="c1",
                  spec={"name": "новый"}, confirm_id=confirm_id)
        assert got["data"]["ok"] is True and got["data"]["cron"], got
        # c3 без проекта и без шагов — задание без владельца: проектному токену оно
        # не принадлежит (fail closed), поэтому в ответе остаётся только c1.
        assert [t["id"] for t in got["data"]["cron"]["tasks"]] == ["c1"], got
        body = env.panel.calls("/api/cron/save")[-1]["body"]
        assert body["project"] == env.alpha and body["task"]["id"] == "c1", body
        assert "project" not in body["task"], body
        got = _err(env, "remote_restart", "confirmation_required")
        confirm_id = env.panel.approve(got["error"]["details"]["request_id"])
        got = _ok(env, "remote_restart", confirm_id=confirm_id)
        assert got["data"] == {"ok": True, "restarting": True}, got
        body = env.panel.calls("/api/restart")[-1]["body"]
        assert body == {"confirm_id": confirm_id}, body
        _err(env, "remote_cron", "project", project="alpha", action="delete", job="c2")
        assert not any(row["body"] and row["body"].get("id") == "c2"
                       for row in env.panel.calls("/api/cron/delete")), "чужое расписание не удалено"
    finally:
        env.close()


@test
def test_mcp_remote_cron_and_restart_safe(tmp: Path):
    env = _run(tmp, panel_mode="safe", local_mode="safe")
    try:
        _err(env, "remote_cron", "safe_mode", project="alpha", action="save", job="c1")
        _err(env, "remote_restart", "safe_mode")
        _ok(env, "remote_cron", project="alpha", action="toggle", job="c1",
            spec={"enabled": False})
        assert env.panel.calls("/api/cron/toggle")[-1]["body"]["enabled"] is False
        _ok(env, "remote_cron", project="alpha", action="start", job="c1")
        assert env.panel.calls("/api/cron/start")[-1]["body"]["id"] == "c1"
        _ok(env, "remote_cron", project="alpha", action="start", job="bot-1")
        _err(env, "remote_cron", "project", project="alpha", action="start", job="bot-2")
        _err(env, "remote_cron", "not_found", project="alpha", action="start", job="bot-hidden")
    finally:
        env.close()


@test
def test_mcp_remote_destructive_route_needs_confirmation(tmp: Path):
    env = _run(tmp, panel_mode="full", local_mode="full")
    try:
        got = _err(env, "remote_run_route", "confirmation_required",
                   project="alpha", route="rebuild",
                   args={"confirm_id": "stolen", "project": "other"})
        assert got["error"]["details"]["args_digest"], got
        assert not env.panel.calls("/api/route/run"), "args.confirm_id не подменяет подтверждение"
        confirm_id = env.panel.approve(got["error"]["details"]["request_id"])
        got = _ok(env, "remote_run_route", project="alpha", route="rebuild",
                  confirm_id=confirm_id, args={"confirm_id": "stolen", "project": "other"})
        assert got["data"]["route"] == "route-1", got
        body = env.panel.calls("/api/route/run")[-1]["body"]
        assert body["scId"] == "rebuild" and body["confirm_id"] == confirm_id, body
        assert body["project"] == env.alpha, body
    finally:
        env.close()


# --------------------------------------------------------------------- клиент
@test
def test_mcp_remote_client_special_cases_and_headers(tmp: Path):
    env = _run(tmp)
    try:
        got = env.client.ping()
        assert got["kit"] == "1.231.14", got
        env.client.health(env.alpha)
        health = env.panel.calls("/api/health")[-1]
        assert health["query"]["project"] == [env.alpha], health
        env.client.restart()
        restart = env.panel.calls("/api/restart")[-1]
        assert restart["body"] == {} and restart["headers"].get("Content-Type") == "application/json"
        assert env.panel.calls("/api/ping")[-1]["headers"].get("X-Aurora-Token") == TOKEN
        blob = env.client.diagnostics(env.alpha, area="panel")
        assert blob[:2] == b"PK", blob[:10]
        env.client.state("core")
        assert env.panel.calls("/api/state")[-1]["query"]["part"] == ["core"]
        token_file = tmp / "remote.token"
        token_file.write_text(TOKEN, encoding="utf-8")
        os.chmod(token_file, 0o600)
        remote = CC.CockpitClient(base_url=env.panel.url, token_file=str(token_file), kit=env.kit)
        remote.ping()
        assert env.panel.calls("/api/ping")[-1]["headers"].get(
            "Authorization") == "Bearer " + TOKEN
        assert TOKEN not in repr(remote)
    finally:
        env.close()


@test
def test_mcp_remote_client_retries_once_on_session_expiry(tmp: Path):
    env = _run(tmp)
    try:
        session = env.kit / "cockpit"
        session.mkdir(parents=True, exist_ok=True)
        path = session / ".session.json"
        path.write_text(json.dumps({"token": "old-token", "port": env.panel.httpd.server_address[1]}),
                        encoding="utf-8")
        client = CC.CockpitClient(kit=env.kit)
        path.write_text(json.dumps({"token": TOKEN, "port": env.panel.httpd.server_address[1]}),
                        encoding="utf-8")
        got = client.ping()
        assert got["app"] == "aurora-cockpit", got
        pings = env.panel.calls("/api/ping")
        assert len(pings) >= 2, pings
    finally:
        env.close()


@test
def test_mcp_remote_token_never_leaks(tmp: Path):
    secret = "very-secret-token-123"
    env = _run(tmp, token=secret)
    try:
        env.panel.fail_path = "/api/history"
        env.panel.fail_message = "bad token " + secret
        got = env.call("remote_runs", project="alpha")
        assert got["ok"] is False, got
        dumped = json.dumps(got, ensure_ascii=False)
        assert secret not in dumped, dumped
        assert secret not in json.dumps(env.audit, ensure_ascii=False)
        assert secret not in repr(env.client)
    finally:
        env.close()


@test
def test_mcp_remote_rate_limit_returns_retry_after(tmp: Path):
    class Deny:
        def allow(self, key, cost=1):
            return False, 7

    env = _run(tmp, rate_limiter=Deny())
    try:
        got = _err(env, "remote_status", "rate_limited")
        assert got["error"]["details"]["retry_after"] == 7, got
        assert not env.panel.calls("/api/ping")
    finally:
        env.close()


# --------------------------------------------------------------------- диагностика/остановка
@test
def test_mcp_remote_diagnostics_saves_zip(tmp: Path):
    env = _run(tmp)
    home = tmp / "home"
    home.mkdir()
    restore = set_home(home)
    try:
        got = _ok(env, "remote_diagnostics", project="alpha", run_id="r1", area="panel")
        data = got["data"]
        assert data["bytes"] > 0 and data["sha256"] and data["summary_lines"], data
        path = Path(data["path"])
        assert path.is_file(), data
        assert str(path).startswith(str(home)), path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == data["sha256"]
    finally:
        restore()
        env.close()


@test
def test_mcp_remote_stop_accepts_exactly_one_id(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_stop", project="alpha", job_id="j1")
        assert got["data"] == {"ok": True, "stopped": "job"}, got
        got = _ok(env, "remote_stop", project="alpha", task_id="t1")
        assert got["data"]["stopped"] == "task", got
        got = _ok(env, "remote_stop", project="alpha", route_id="route-1")
        assert got["data"]["stopped"] == "route", got
        _err(env, "remote_stop", "invalid_argument", project="alpha")
        _err(env, "remote_stop", "invalid_argument", project="alpha",
             job_id="j1", task_id="t1")
        _err(env, "remote_stop", "not_found", project="alpha", job_id="j-other")
        _err(env, "remote_stop", "not_found", project="alpha", task_id="t-other")
        _err(env, "remote_stop", "project", project="alpha", route_id="route-other")
        _err(env, "remote_stop", "not_found", project="alpha", route_id="route-missing")
        assert not any(row["body"] and row["body"].get("id") in ("j-other", "t-other")
                       for row in env.panel.requests
                       if row["path"] in ("/api/job/stop", "/api/task/stop"))
        assert not any(row["body"] and row["body"].get("id") in ("route-other", "route-missing")
                       for row in env.panel.calls("/api/route/stop")), "чужой маршрут не остановлен"
    finally:
        env.close()


@test
def test_mcp_remote_controls(tmp: Path):
    env = _run(tmp)
    try:
        got = _ok(env, "remote_ask_control", project="alpha", action="start", sid="s1")
        assert got["data"]["sid"] == "s1" and got["data"]["action"] == "start", got
        got = _ok(env, "remote_make_control", project="alpha", action="pause",
                  sid="m1", kind="doc")
        assert got["data"]["sid"] == "m1" and got["data"]["action"] == "pause", got
        assert env.panel.calls("/api/ask/control")[-1]["body"]["sid"] == "s1"
        assert env.panel.calls("/api/make/control")[-1]["body"]["kind"] == "doc"
    finally:
        env.close()


@test
def test_mcp_remote_selftest_cli(_tmp: Path):
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_remote_mcp.py"), "--selftest"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert '"tools":17' in cp.stdout and '"selftest":"ok"' in cp.stdout, cp.stdout
    assert "selftest-token" not in cp.stdout + cp.stderr


@test
def test_mcp_remote_jsonrpc_stdio(tmp: Path):
    env = _run(tmp)
    old_in, old_out = RM.sys.stdin, RM.sys.stdout
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "remote_status", "arguments": {}}},
    ]
    try:
        RM.sys.stdin = io.StringIO("\n".join(json.dumps(row) for row in lines) + "\n")
        RM.sys.stdout = io.StringIO()
        RM.serve_stdio(env.server)
        out = RM.sys.stdout.getvalue()
    finally:
        RM.sys.stdin, RM.sys.stdout = old_in, old_out
        env.close()
    replies = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(replies) == 3, replies
    assert replies[0]["result"]["serverInfo"]["name"] == "aurora-remote", replies[0]
    assert len(replies[1]["result"]["tools"]) == 17, replies[1]
    call = replies[2]["result"]
    assert call["isError"] is False, call
    assert call["structuredContent"]["ok"] is True, call
    assert call["content"][0]["type"] == "text", call
