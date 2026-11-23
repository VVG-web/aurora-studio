"""Проверки части 252: MCP в панели, режим `aurora-remote`, токены и подтверждения.

Все проверки работают во временном ките и временной домашней папке. Настоящие
`local/mcp.json`, `local/mcp-tokens.json`, `cockpit/.session.json` и конфиги
ассистентов не читаются и не пишутся. Панель поднимается на петлевом порту и отвечает
своему же клиенту; опасные действия (`/api/run`, маршрут, cron, restart) подменены,
чтобы тест ничего не запускал.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import KIT, SCRIPTS, assert_private_mode, run_node, set_home, test

# Python 3.10: tomli ставит CI-джоб legacy-310; на 3.11+ это stdlib tomllib.
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

import aurora_common as AC  # noqa: E402
import aurora_guard as G  # noqa: E402
import aurora_mcp as AM  # noqa: E402
import harness_mcp as HM  # noqa: E402
import mcp_tokens as MT  # noqa: E402


def _kit(tmp: Path, name: str = "kit") -> Path:
    root = tmp / name
    (root / "local").mkdir(parents=True, exist_ok=True)
    return root


def _project(root: Path, name: str = "alpha") -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "aurora.config.yaml").write_text(
        f'project:\n  name: "{name}"\n  slug: "{name}"\n', encoding="utf-8")
    return path


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_mode(kit: Path, mode: str, projects: dict | None = None) -> None:
    remote = {"mode": mode}
    if projects is not None:
        remote["projects"] = projects
    _write_json(kit / "local" / "mcp.json", {"mcpServers": {}, "auroraRemote": remote})


def _tokens_env(kit: Path):
    old = os.environ.get("AURORA_MCP_TOKENS_FILE")
    os.environ["AURORA_MCP_TOKENS_FILE"] = str(kit / "local" / "mcp-tokens.json")
    return old


def _restore_env(old) -> None:
    if old is None:
        os.environ.pop("AURORA_MCP_TOKENS_FILE", None)
    else:
        os.environ["AURORA_MCP_TOKENS_FILE"] = old


def _token(kit: Path, scopes=None, projects=None, name="Ноутбук") -> str:
    scopes = ["remote:read", "remote:write", "admin"] if scopes is None else scopes
    projects = ["alpha"] if projects is None else projects
    return MT.new_token("remote", name, "ivanov@corp", scopes, projects, 90,
                        kit=str(kit))["token"]


def _session(ctx_kind="session", scopes=None, projects=None, vpn=False):
    return G.AuthContext(kind=ctx_kind, scopes=frozenset(scopes or MT.SCOPES),
                         projects=tuple(projects or ("*",)), vpn=vpn, source="session")


def _panel(tmp: Path, kit: Path, root: Path):
    ck = importlib.import_module("aurora_cockpit")
    saved = {"kit": ck.KIT, "configs": ck.aurora_mcp_configs, "cache": dict(ck.CACHE),
             "tokens_kit": ck._tokens_kit, "confirm_limiter": ck.MCP_CONFIRM_LIMITER,
             "token_limiter": ck.TOKEN_LIMITER}
    ck.KIT = str(kit)
    ck._tokens_kit = lambda: str(kit)
    ck.aurora_mcp_configs = lambda: {}
    ck.CACHE["aurora_mcp"] = {}
    ck.MCP_CONFIRM_LIMITER = G.RateLimiter({})
    ck.TOKEN_LIMITER = G.RateLimiter({})
    ck.MCP_CONFIRM_REQUESTS.clear()
    ck.MCP_CONFIRMATIONS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(root)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def close():
        srv.shutdown()
        srv.server_close()
        ck.KIT = saved["kit"]
        ck.aurora_mcp_configs = saved["configs"]
        ck._tokens_kit = saved["tokens_kit"]
        ck.CACHE.clear()
        ck.CACHE.update(saved["cache"])
        ck.MCP_CONFIRM_LIMITER = saved["confirm_limiter"]
        ck.TOKEN_LIMITER = saved["token_limiter"]
        ck.MCP_CONFIRM_REQUESTS.clear()
        ck.MCP_CONFIRMATIONS.clear()
    return ck, srv, close


def _call(srv):
    def request(method: str, path: str, headers=None, body=None):
        conn = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", "replace")
        return response.status, {k.lower(): v for k, v in response.getheaders()}, raw
    return request


def _json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _headers(token: str, session: str = "") -> dict:
    out = {"Content-Type": "application/json"}
    if session:
        out["X-Aurora-Token"] = session
    else:
        out["Authorization"] = f"Bearer {token}"
    return out


def _request_confirmation(ck, call, token: str, tool: str, project: str, args: dict) -> str:
    digest = AC.confirmation_digest(tool, project, args)
    status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                           json.dumps({"tool": tool, "project": project,
                                       "args_digest": digest, "summary": "проверка"}))
    assert status == 200, (status, raw)
    request_id = _json(raw)["request_id"]
    status, _h, raw = call("POST", "/api/mcp/confirm/approve",
                           _headers("", ck.TOKEN),
                           json.dumps({"request_id": request_id}))
    assert status == 200, (status, raw)
    confirm_id = _json(raw).get("confirm_id")
    assert confirm_id, raw
    return confirm_id


@test
def test_mcp_cockpit_remote_mode_safe_broken_full_and_projects(tmp: Path):
    """Нет файла и битый файл дают safe; full читается; переопределение проекта видно."""
    kit = _kit(tmp)
    ck = importlib.import_module("aurora_cockpit")
    old = ck.KIT
    ck.KIT = str(kit)
    try:
        state = ck.remote_mode_state()
        assert state["mode"] == "safe" and state["stored"] == "safe", state
        assert state["source"] == "default", state
        assert state["deny"] == list(AC.MCP_SAFE_DENY_COMMANDS), state["deny"]
        assert len(state["deny"]) == 10, state["deny"]
        assert "ship:release" in state["deny"]
        assert set(AC.MCP_SAFE_DENY_COMMANDS) == set(AC.MCP_IRREVERSIBLE_COMMANDS)

        (kit / "local" / "mcp.json").write_text("{это не json", encoding="utf-8")
        broken = ck.remote_mode_state()
        assert broken["mode"] == "safe" and broken["source"] == "broken", broken
        assert broken.get("reason"), broken

        _write_mode(kit, "full", {"alpha": "safe"})
        full = ck.remote_mode_state()
        assert full["mode"] == "full" and full["stored"] == "full", full
        assert full["source"] == "file" and full["projects"] == {"alpha": "safe"}, full
        assert AC.kit_mcp_mode("alpha", kit=str(kit)) == ("safe", "project")

        admin = ck.remote_mode_state(_session(vpn=True))
        assert admin["admin_network"] is True, admin
    finally:
        ck.KIT = old


@test
def test_mcp_cockpit_set_remote_mode_preserves_file(tmp: Path):
    """Переключение режима сохраняет серверы, права и незнакомые ключи, делает .bak и 0600."""
    kit = _kit(tmp)
    path = kit / "local" / "mcp.json"
    _write_json(path, {"mcpServers": {"x": {"command": "y"}},
                       "permissions": {"jira_write": True}, "custom": {"a": 1}})
    ck = importlib.import_module("aurora_cockpit")
    old = ck.KIT
    ck.KIT = str(kit)
    try:
        result = ck.set_remote_mode({"action": "set_remote_mode", "mode": "full",
                                     "project": ""}, _session())
        assert result.get("ok") is True, result
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcpServers"] == {"x": {"command": "y"}}, data
        assert data["permissions"] == {"jira_write": True}, data
        assert data["custom"] == {"a": 1}, data
        assert data["auroraRemote"]["mode"] == "full", data
        assert_private_mode(path)
        bak = path.with_name(path.name + ".bak")
        assert bak.is_file() and '"auroraRemote"' not in bak.read_text(encoding="utf-8")
        assert_private_mode(bak)

        project = ck.set_remote_mode({"mode": "safe", "project": "alpha"}, _session())
        assert project.get("ok") is True, project
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["auroraRemote"]["mode"] == "full", data
        assert data["auroraRemote"]["projects"] == {"alpha": "safe"}, data

        remote = ck.set_remote_mode({"mode": "safe", "project": ""},
                                    _session(ctx_kind="remote", projects=["alpha"]))
        assert (remote.get("code"), remote.get("_status")) == ("endpoint", 403), remote
        assert json.loads(path.read_text(encoding="utf-8"))["auroraRemote"]["mode"] == "full"

        assert ck.set_remote_mode({"mode": "root"}, _session()).get("code") == "bad_request"
        assert ck.set_remote_mode({"mode": "full", "project": "*"},
                                  _session()).get("code") == "invalid_project"

        path.write_text("{битый", encoding="utf-8")
        broken = ck.set_remote_mode({"mode": "safe", "project": ""}, _session())
        assert broken.get("code") == "bad_request", broken
    finally:
        ck.KIT = old


@test
def test_mcp_cockpit_remote_token_cannot_set_mode_http(tmp: Path):
    """HTTP: сессия меняет режим, удалённый токен получает 403 endpoint; прокси и VPN честны."""
    kit = _kit(tmp)
    root = tmp / "root"
    _project(root)
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    try:
        token = _token(kit, ["remote:read", "remote:write", "admin"])
        status, _h, raw = call("GET", "/api/mcp/kit", _headers("", ck.TOKEN))
        state = _json(raw)
        assert status == 200 and state.get("remote"), (status, raw)
        assert state["remote"]["mode"] == "safe", state["remote"]
        assert len(state["remote"]["deny"]) == 10, state["remote"]
        assert "ship:release" in state["remote"]["deny"]
        assert set(AC.MCP_SAFE_DENY_COMMANDS) == set(AC.MCP_IRREVERSIBLE_COMMANDS)

        status, _h, raw = call("POST", "/api/mcp/kit", _headers(token),
                               json.dumps({"action": "set_remote_mode", "mode": "full",
                                           "project": ""}))
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)

        status, _h, raw = call("POST", "/api/mcp/kit", _headers("", ck.TOKEN),
                               json.dumps({"action": "set_remote_mode", "mode": "full",
                                           "project": ""}))
        assert status == 200 and _json(raw).get("ok") is True, (status, raw)
        assert _json(raw)["remote"]["mode"] == "full", raw

        status, _h, raw = call("GET", "/api/mcp/kit",
                               {**_headers("", ck.TOKEN), "X-Aurora-Admin-Network": "1"})
        assert status == 200 and _json(raw)["remote"]["admin_network"] is True, (status, raw)

        spoof = {"X-Forwarded-For": "198.51.100.7, 10.0.0.1"}
        assert G.client_ip(spoof, "127.0.0.1") == "198.51.100.7"
        assert G.client_ip(spoof, "10.0.0.8") == "10.0.0.8"
        assert G.admin_network({"X-Aurora-Admin-Network": "1"}, "10.0.0.8") is False
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_confirm_queue_and_token_boundary(tmp: Path):
    """Очередь подтверждений видит сессия; удалённый токен не читает и не подтверждает."""
    kit = _kit(tmp)
    root = tmp / "root"
    project = _project(root)
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    try:
        token = _token(kit)
        digest = AC.confirmation_digest("remote_run_command", "alpha",
                                        {"cmd": "kit:update", "args": [], "resume": False})
        status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                               json.dumps({"tool": "remote_run_command", "project": "alpha",
                                           "args_digest": digest, "summary": "обновить движок"}))
        request = _json(raw)
        assert status == 200 and request.get("request_id"), (status, raw)
        assert request["args_digest"] == digest, request

        status, _h, raw = call("GET", "/api/mcp/confirmations", _headers(token))
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)

        status, _h, raw = call("GET", "/api/mcp/confirmations", _headers("", ck.TOKEN))
        pending = _json(raw).get("pending") or []
        assert status == 200 and len(pending) == 1, (status, raw)
        assert pending[0]["tool"] == "remote_run_command", pending
        assert pending[0]["project"] == "alpha" and pending[0]["args_digest"] == digest, pending
        assert pending[0]["token_id"] and pending[0]["user"] == "ivanov@corp", pending

        status, _h, raw = call("POST", "/api/mcp/confirm/approve", _headers(token),
                               json.dumps({"request_id": request["request_id"]}))
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)

        status, _h, raw = call("POST", "/api/mcp/confirm/approve", _headers("", ck.TOKEN),
                               json.dumps({"request_id": request["request_id"]}))
        approved = _json(raw)
        assert status == 200 and approved.get("confirm_id"), (status, raw)
        assert approved["args_digest"] == digest, approved

        status, _h, raw = call("POST", "/api/mcp/confirm/approve", _headers("", ck.TOKEN),
                               json.dumps({"request_id": request["request_id"]}))
        assert status == 404 and _json(raw).get("code") == "not_found", (status, raw)

        status, _h, raw = call("POST", "/api/mcp/confirm/reject", _headers(token),
                               json.dumps({"request_id": "x"}))
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)

        audit = list((kit / "local" / "audit").glob("access-*.jsonl"))
        assert len(audit) == 1, audit
        lines = audit[0].read_text(encoding="utf-8")
        assert "mcp_confirm" in lines and "approve" in lines, lines
        assert token not in lines and "обновить движок" not in lines, lines
        assert str(project) not in lines, lines
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_confirm_one_use_mismatch_ttl_and_reject(tmp: Path):
    """confirm_id одноразовый; другой digest или проект не совпадает; TTL; отказ."""
    kit = _kit(tmp)
    root = tmp / "root"
    project = _project(root)
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    started = []
    try:
        token = _token(kit, projects=["alpha", "other"])
        with patch.object(ck, "start_job", lambda *_a, **_k: started.append(_a) or "job1"), \
                patch.object(ck, "command_by_name",
                             lambda cmd: {"cmd": cmd, "runnable": True, "ns": "kit",
                                          "flags": [], "fixed_flags": [], "script": "x.py"}), \
                patch.object(ck, "version_gap", lambda *_a, **_k: ""), \
                patch.object(ck, "net_enabled", lambda: False):
            confirm_id = _request_confirmation(
                ck, call, token, "remote_run_command", "alpha",
                {"cmd": "kit:update", "args": [], "resume": False})
            body = json.dumps({"project": str(project), "cmd": "kit:update", "args": [],
                               "resume": False, "confirm_id": confirm_id})
            status, _h, raw = call("POST", "/api/run", _headers(token), body)
            assert status == 200 and started, (status, raw)
            status, _h, raw = call("POST", "/api/run", _headers(token), body)
            assert status == 409 and _json(raw).get("code") == "confirmation_expired", (status, raw)

            conflict = _request_confirmation(
                ck, call, token, "remote_run_command", "alpha",
                {"cmd": "kit:update", "args": [], "resume": False})
            wrong = json.dumps({"project": str(project), "cmd": "git:push", "args": [],
                                "resume": False, "confirm_id": conflict})
            status, _h, raw = call("POST", "/api/run", _headers(token), wrong)
            assert status == 409 and _json(raw).get("code") == "confirmation_mismatch", (status, raw)
            right = json.dumps({"project": str(project), "cmd": "kit:update", "args": [],
                                "resume": False, "confirm_id": conflict})
            status, _h, raw = call("POST", "/api/run", _headers(token), right)
            assert status == 200, (status, raw)

            other = _request_confirmation(
                ck, call, token, "remote_run_command", "other",
                {"cmd": "kit:update", "args": [], "resume": False})
            wrong_project = json.dumps({"project": str(project), "cmd": "kit:update",
                                        "args": [], "resume": False, "confirm_id": other})
            status, _h, raw = call("POST", "/api/run", _headers(token), wrong_project)
            assert status == 409 and _json(raw).get("code") == "confirmation_mismatch", (status, raw)

            missing = json.dumps({"project": str(project), "cmd": "kit:update", "args": [],
                                  "resume": False})
            status, _h, raw = call("POST", "/api/run", _headers(token), missing)
            assert status == 409 and _json(raw).get("code") == "confirmation_required", (status, raw)

            old_ttl = ck.MCP_CONFIRM_TTL
            ck.MCP_CONFIRM_TTL = -1
            try:
                digest = AC.confirmation_digest("remote_run_command", "alpha",
                                                {"cmd": "kit:update", "args": [],
                                                 "resume": False})
                status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                                       json.dumps({"tool": "remote_run_command",
                                                   "project": "alpha",
                                                   "args_digest": digest,
                                                   "summary": "истёкшая"}))
                assert status == 200, (status, raw)
                request_id = _json(raw)["request_id"]
                status, _h, raw = call("POST", "/api/mcp/confirm/approve",
                                       _headers("", ck.TOKEN),
                                       json.dumps({"request_id": request_id}))
                assert status == 409 and _json(raw).get("code") == "confirmation_expired", \
                    (status, raw)
            finally:
                ck.MCP_CONFIRM_TTL = old_ttl

            digest = AC.confirmation_digest("remote_run_command", "alpha",
                                            {"cmd": "kit:update", "args": [],
                                             "resume": False})
            status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                                   json.dumps({"tool": "remote_run_command", "project": "alpha",
                                               "args_digest": digest, "summary": "отклонить"}))
            request_id = _json(raw)["request_id"]
            status, _h, raw = call("POST", "/api/mcp/confirm/reject",
                                   _headers("", ck.TOKEN),
                                   json.dumps({"request_id": request_id}))
            assert status == 200 and _json(raw).get("ok") is True, (status, raw)
            status, _h, raw = call("POST", "/api/mcp/confirm/approve",
                                   _headers("", ck.TOKEN),
                                   json.dumps({"request_id": request_id}))
            assert status == 404 and _json(raw).get("code") == "not_found", (status, raw)

            _write_mode(kit, "safe")
            safe = json.dumps({"project": str(project), "cmd": "kit:update", "args": [],
                               "resume": False})
            status, _h, raw = call("POST", "/api/run", _headers(token), safe)
            assert status == 403 and _json(raw).get("code") == "safe_mode", (status, raw)
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_route_cron_restart_consume(tmp: Path):
    """Маршрут, cron save и restart тоже проверяют и гасят один и тот же confirm_id."""
    kit = _kit(tmp)
    root = tmp / "root"
    project = _project(root)
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    seen = {}
    try:
        token = _token(kit)
        def fake_route(*args, **_kwargs):
            seen["route"] = args[1]
            return {"route": "r1"}

        def fake_cron(*args, **_kwargs):
            seen["cron"] = args[0]
            return {"ok": True}

        with patch.object(ck, "all_scenarios",
                          lambda: [{"id": "danger", "destructive": True, "steps": []}]), \
                patch.object(ck, "route_start", fake_route), \
                patch.object(ck, "cron_action", fake_cron), \
                patch.object(ck, "restart_self", lambda *_a, **_k: {"error": "test"}), \
                patch.object(ck, "version_gap", lambda *_a, **_k: ""), \
                patch.object(ck, "net_enabled", lambda: False):
            route_id = _request_confirmation(
                ck, call, token, "remote_run_route", "alpha",
                {"scId": "danger", "write": True, "net": "", "resume": False})
            route_body = json.dumps({"project": str(project), "scId": "danger",
                                     "write": True, "net": "", "confirm_id": route_id})
            status, _h, raw = call("POST", "/api/route/run", _headers(token), route_body)
            assert status == 200 and seen.get("route") == "danger", (status, raw, seen)
            status, _h, raw = call("POST", "/api/route/run", _headers(token), route_body)
            assert status == 409 and _json(raw).get("code") == "confirmation_expired", (status, raw)

            cron_id = _request_confirmation(
                ck, call, token, "remote_cron", "alpha",
                {"action": "save", "task": {"id": "t1"}})
            cron_body = json.dumps({"project": str(project), "action": "save",
                                    "task": {"id": "t1"}, "confirm_id": cron_id})
            status, _h, raw = call("POST", "/api/cron/save", _headers(token), cron_body)
            assert status == 200 and seen.get("cron") == "save", (status, raw, seen)
            status, _h, raw = call("POST", "/api/cron/save", _headers(token), cron_body)
            assert status == 409 and _json(raw).get("code") == "confirmation_expired", (status, raw)

            restart_id = _request_confirmation(ck, call, token, "remote_restart", "", {})
            status, _h, raw = call("POST", "/api/restart", _headers(token),
                                   json.dumps({"confirm_id": restart_id}))
            assert status == 200 and "test" in raw, (status, raw)
            status, _h, raw = call("POST", "/api/restart", _headers(token),
                                   json.dumps({"confirm_id": restart_id}))
            assert status == 409 and _json(raw).get("code") == "confirmation_expired", (status, raw)
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_confirm_rate_limit_and_audit(tmp: Path):
    """Предел частоты даёт 429 и Retry-After; аудит не содержит секрета токена."""
    kit = _kit(tmp)
    root = tmp / "root"
    _project(root)
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    try:
        token = _token(kit)
        ck.MCP_CONFIRM_LIMITER = G.RateLimiter({"confirm:request:token": (1, 60),
                                                "confirm:request:ip": (1, 60)})
        first = AC.confirmation_digest("remote_run_command", "alpha",
                                       {"cmd": "kit:update", "args": [], "resume": False})
        status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                               json.dumps({"tool": "remote_run_command", "project": "alpha",
                                           "args_digest": first, "summary": "первый"}))
        assert status == 200, (status, raw)
        second = AC.confirmation_digest("remote_run_command", "alpha",
                                        {"cmd": "git:push", "args": [], "resume": False})
        status, headers, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                                    json.dumps({"tool": "remote_run_command", "project": "alpha",
                                                "args_digest": second, "summary": "второй"}))
        assert status == 429 and _json(raw).get("code") == "rate_limited", (status, raw)
        assert int(headers.get("retry-after") or 0) >= 1, headers

        audit = list((kit / "local" / "audit").glob("access-*.jsonl"))
        assert audit, "аудит доступа не создан"
        text = audit[0].read_text(encoding="utf-8")
        assert token not in text and token.split("_", 3)[3] not in text, text
        assert "confirm" in text and "request" in text, text
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_token_api_one_time_secret_and_wildcard(tmp: Path):
    """HTTP: секрет один раз, список без секрета, звёздочка отклонена, вращение и отзыв."""
    kit = _kit(tmp)
    root = tmp / "root"
    _project(root)
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    session = _headers("", ck.TOKEN)
    try:
        body = json.dumps({"kind": "remote", "name": "Ноутбук", "user": "ivanov@corp",
                           "scopes": ["remote:read", "remote:write"],
                           "projects": ["alpha"], "expires_days": 30})
        status, headers, raw = call("POST", "/api/mcp/tokens/create", session, body)
        created = _json(raw)
        assert status == 200 and created.get("token"), (status, raw)
        assert headers.get("cache-control") == "no-store", headers
        assert "hash" not in created["record"], created["record"]
        token = created["token"]

        status, _h, raw = call("GET", "/api/mcp/tokens?kind=remote", session)
        listed = _json(raw).get("tokens") or []
        assert status == 200 and len(listed) == 1, (status, raw)
        assert token not in raw and "hash" not in raw, raw
        assert listed[0]["prefix"] == created["record"]["prefix"], listed

        wildcard = json.dumps({"kind": "remote", "name": "Все", "user": "u",
                               "scopes": ["remote:read"], "projects": ["*"]})
        status, _h, raw = call("POST", "/api/mcp/tokens/create", session, wildcard)
        assert status == 400 and _json(raw).get("code") == "invalid_project", (status, raw)

        status, _h, raw = call("POST", "/api/mcp/tokens/rotate", session,
                               json.dumps({"id": created["record"]["id"]}))
        rotated = _json(raw)
        assert status == 200 and rotated.get("token"), (status, raw)
        assert MT.verify(token, kit=str(kit)) is None, "старый секрет не умер"
        assert MT.verify(rotated["token"], kit=str(kit))["id"] == created["record"]["id"]

        status, _h, raw = call("POST", "/api/mcp/tokens/revoke", session,
                               json.dumps({"id": created["record"]["id"]}))
        assert status == 200 and _json(raw).get("ok") is True, (status, raw)
        assert MT.verify(rotated["token"], kit=str(kit)) is None, "отзыв не мгновенный"
        status, _h, raw = call("GET", "/api/mcp/tokens?kind=remote", session)
        assert _json(raw)["tokens"][0]["status"] == "revoked", raw
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_harness_two_servers_all_formats(tmp: Path):
    """26 форматов получают оба сервера; continue, codex, vscode, chatbox, jcode особые."""
    home = tmp / "home"
    home.mkdir()
    restore = set_home(home)
    old_home, old_backups, old_running = HM.home, HM.backups_root, HM.app_running
    HM.home = lambda: str(home)
    HM.backups_root = lambda: home / ".aurora" / "harness-backups"
    HM.app_running = lambda _names: False
    ck = importlib.import_module("aurora_cockpit")
    old_kit = ck.KIT
    kit = _kit(tmp)
    ck.KIT = str(kit)
    try:
        # Старая запись aurora не должна быть перезаписана при подключении управления.
        _write_json(home / ".cursor" / "mcp.json",
                    {"mcpServers": {"aurora": {"command": "custom",
                                               "args": ["/x/aurora_mcp.py"],
                                               "custom": 1}}})
        failures = []
        for harness in HM.catalog():
            for server in HM.SERVERS:
                try:
                    result = HM.add(harness["id"], server=server)
                    if not result.get("ok"):
                        failures.append((harness["id"], server, result))
                except Exception as error:      # noqa: BLE001 — причина попадает в отчёт
                    failures.append((harness["id"], server, str(error)))
        assert not failures, failures

        state = ck.harness_state(False)
        rows = state["harnesses"]
        assert len(rows) == 2 * len(HM.catalog()), len(rows)
        by_id = {}
        for row in rows:
            by_id.setdefault(row["id"], []).append(row["server"])
        assert all(sorted(servers) == sorted(HM.SERVERS) for servers in by_id.values()), by_id
        assert set(state["servers"]) == set(HM.SERVERS), state["servers"]

        cursor = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        assert cursor["mcpServers"]["aurora"]["command"] == "custom", cursor
        assert cursor["mcpServers"]["aurora"]["custom"] == 1, cursor
        assert "aurora-remote" in cursor["mcpServers"], cursor

        continue_dir = home / ".continue" / "mcpServers"
        assert (continue_dir / "aurora.yaml").is_file(), list(continue_dir.glob("*"))
        assert (continue_dir / "aurora-remote.yaml").is_file(), list(continue_dir.glob("*"))

        codex = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
        assert set(codex["mcp_servers"]) == set(HM.SERVERS), codex["mcp_servers"]

        vscode = json.loads(Path(HM.config_path(HM.harness("vscode")))
                            .read_text(encoding="utf-8"))
        assert vscode["servers"]["aurora"]["type"] == "stdio", vscode
        assert vscode["servers"]["aurora-remote"]["type"] == "stdio", vscode

        chatbox = json.loads(Path(HM.config_path(HM.harness("chatbox")))
                             .read_text(encoding="utf-8"))
        names = {row.get("name") for row in chatbox["settings"]["mcp"]["servers"]}
        assert set(HM.SERVERS) <= names, names

        jcode = json.loads((home / ".jcode" / "mcp.json").read_text(encoding="utf-8"))
        assert jcode["mcpServers"]["aurora"]["timeout_secs"] == 1800, jcode
        assert jcode["mcpServers"]["aurora-remote"]["timeout_secs"] == 300, jcode
    finally:
        ck.KIT = old_kit
        HM.home, HM.backups_root, HM.app_running = old_home, old_backups, old_running
        restore()


@test
def test_mcp_cockpit_harness_backups_restore_and_legacy(tmp: Path):
    """Копии по серверу; возврат одного не убирает второй; старая общая копия предупреждает."""
    home = tmp / "home"
    home.mkdir()
    restore = set_home(home)
    old_home, old_backups, old_running = HM.home, HM.backups_root, HM.app_running
    HM.home = lambda: str(home)
    HM.backups_root = lambda: home / ".aurora" / "harness-backups"
    HM.app_running = lambda _names: False
    ck = importlib.import_module("aurora_cockpit")
    old_kit = ck.KIT
    kit = _kit(tmp)
    ck.KIT = str(kit)
    try:
        HM.add("cursor", server="aurora")
        HM.add("cursor", server="aurora-remote")
        path = home / ".cursor" / "mcp.json"
        assert len(HM.list_backups("cursor", "aurora")) == 1, HM.list_backups("cursor")
        assert len(HM.list_backups("cursor", "aurora-remote")) == 1, HM.list_backups("cursor")

        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["aurora"]["args"] = ["/x/aurora_mcp.py", "--changed"]
        _write_json(path, data)
        restored = HM.restore("cursor", server="aurora")
        assert restored["ok"] is True and restored["legacy_shared"] is False, restored
        after = json.loads(path.read_text(encoding="utf-8"))
        # Копия aurora снята до его подключения: возврат убирает aurora, но не управление.
        assert "aurora" not in after["mcpServers"], after
        assert "aurora-remote" in after["mcpServers"], after
        assert restored.get("preserved") == ["aurora-remote"], restored

        HM.add("cursor", server="aurora")
        remote_backup = HM.backup("cursor", str(path), "проверка", "aurora-remote")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["mcpServers"]["aurora-remote"]["args"] = ["/x/aurora_remote_mcp.py", "--changed"]
        _write_json(path, data)
        restored = HM.restore("cursor", stamp=remote_backup["stamp"], server="aurora-remote")
        assert restored["ok"] is True, restored
        after = json.loads(path.read_text(encoding="utf-8"))
        assert "aurora" in after["mcpServers"], after
        assert "--changed" not in after["mcpServers"]["aurora-remote"]["args"], after

        legacy_dir = home / ".aurora" / "harness-backups" / "cursor"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_copy = legacy_dir / "legacy-copy.json"
        _write_json(legacy_copy, {"mcpServers": {"aurora": {"command": "old", "args": []}}})
        _write_json(legacy_dir / "backups.json",
                    [{"stamp": "legacy-1", "path": str(path), "why": "старая версия",
                      "created": False, "backup": str(legacy_copy)}])
        legacy = HM.restore("cursor", stamp="legacy-1", server="aurora")
        assert legacy["legacy_shared"] is True and legacy.get("warning"), legacy

        old_payload = ck.harness_action("add", {"id": "pi"})
        assert old_payload.get("ok") is True and old_payload.get("server") == "aurora", old_payload
        pi = HM.config_path(HM.harness("pi"))
        text = Path(pi).read_text(encoding="utf-8")
        assert HM.has_server(HM.harness("pi"), pi, "aurora") is True, text
        assert HM.has_server(HM.harness("pi"), pi, "aurora-remote") is False, text

        refused = ck.harness_action("add", {"id": "pi", "server": "nope"})
        assert refused.get("ok") is False and refused.get("error"), refused
    finally:
        ck.KIT = old_kit
        HM.home, HM.backups_root, HM.app_running = old_home, old_backups, old_running
        restore()


@test
def test_mcp_cockpit_client_configs_and_jira_mode(tmp: Path):
    """client_configs знает оба сервера; jira_publish без dry_run подчиняется режиму."""
    configs = AM.client_configs()
    assert set(configs["opencode"]["mcp"]) == {"aurora", "aurora-remote"}, configs
    assert set(configs["mcpServers"]["mcpServers"]) == {"aurora", "aurora-remote"}, configs
    remote_args = configs["mcpServers"]["mcpServers"]["aurora-remote"]["args"]
    assert remote_args and remote_args[0].endswith("aurora_remote_mcp.py"), remote_args

    calls = []

    def fake_run(project, script, args, **_kwargs):
        calls.append((project, script, list(args)))
        return '{"ok": true, "steps": []}'

    publish = AM.t_atlassian("jira_publish")
    with patch.object(AM, "run", fake_run):
        with patch.object(AM.aurora_common, "kit_mcp_mode",
                          return_value=("safe", "file")):
            assert publish(str(tmp), {"issue": "ABC-1", "dry_run": True})
            try:
                publish(str(tmp), {"issue": "ABC-1"})
            except AM.ToolError as error:
                assert "безопасный режим" in str(error), error
            else:
                raise AssertionError("jira_publish прошёл в safe без dry_run")
        with patch.object(AM.aurora_common, "kit_mcp_mode",
                          return_value=("full", "file")):
            assert publish(str(tmp), {"issue": "ABC-1"})
    assert len(calls) == 2, calls
    assert "не только читает" in (AM.run.__doc__ or ""), AM.run.__doc__
    assert "Ни один инструмент не пишет" not in (AM.run.__doc__ or "")


# --------------------------------------------------------------- панель: доступ агента
#
# Раздел «Доступ агента» живёт в ядре панели (`cockpit/ui/panel.js`), а не в модуле
# установки: режим safe/full и очередь подтверждений нужны на каждой странице «Установка».
# Модель режима вынесена чистой функцией и проверяется под node; HTTP-часть проверяет,
# что удалённый токен не читает токены и не переключает режим, а сессия панели может.

_MCP_PANEL_KEYS = (
    "mcpk.access_title", "mcpk.access_about", "mcpk.mode_group",
    "mcpk.mode_effective", "mcpk.mode_source", "mcpk.mode_stored",
    "mcpk.mode_full", "mcpk.mode_safe", "mcpk.mode_safe_about",
    "mcpk.mode_full_warn", "mcpk.mode_full_ask", "mcpk.mode_full_done",
    "mcpk.mode_safe_done", "mcpk.mode_failed", "mcpk.mode_switching",
    "mcpk.mode_reason", "mcpk.mode_projects", "mcpk.mode_deny",
    "mcpk.source.file", "mcpk.source.project", "mcpk.source.default",
    "mcpk.source.broken", "mcpk.source.isolated", "mcpk.source.none",
    "mcpk.badge_remote", "mcpk.badge_vpn", "mcpk.badge_admin",
    "mcpk.local_only", "mcpk.tokens_title", "mcpk.tokens_active",
    "mcpk.tokens_none", "mcpk.tokens_error", "mcpk.token_scopes",
    "mcpk.confirm_title", "mcpk.confirm_pending", "mcpk.confirm_empty",
    "mcpk.confirm_approve", "mcpk.confirm_reject", "mcpk.confirm_approved",
    "mcpk.confirm_rejected", "mcpk.confirm_failed", "mcpk.confirm_once",
    "mcpk.confirm_error", "mcpk.kit_error", "mcpk.busy", "mcpk.loading",
)


def _panel_source() -> str:
    return (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")


def _catalog(lang: str) -> dict:
    return json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))


def _node_json(code: str):
    node = shutil.which("node")
    if not node:
        return None
    with tempfile.TemporaryDirectory(prefix="aurora-node-") as tmp:
        cp = run_node(tmp, code, name="panel.mjs")
    assert cp is not None, "node not found"
    assert cp.returncode == 0, cp.stderr[:800]
    return json.loads(cp.stdout)


@test
def test_mcp_cockpit_panel_access_model_safe_full_and_badges(tmp: Path):
    """Модель раздела: safe по умолчанию, full красный, VPN и admin badges, чужой токен."""
    js = _panel_source()
    start = js.index("/* MCPK_ACCESS_MODEL_START */")
    end = js.index("/* MCPK_ACCESS_MODEL_END */") + len("/* MCPK_ACCESS_MODEL_END */")
    fn = js[start:end]
    safe_kit = {"remote": {"mode": "safe", "stored": "safe", "source": "default",
                           "deny": ["git:push"], "projects": {}}}
    full_kit = {"remote": {"mode": "full", "stored": "full", "source": "file",
                           "admin_network": True, "deny": ["git:push"],
                           "projects": {"alpha": "safe"},
                           "reason": "в файле записан полный доступ"}}
    tokens = {"tokens": [
        {"id": "a", "name": "Ноутбук", "status": "active",
         "scopes": ["remote:read", "admin"]},
        {"id": "b", "name": "Старый", "status": "revoked", "scopes": ["admin"]},
    ]}
    conf = {"pending": [{"request_id": "r1", "tool": "remote_run_command",
                         "project": "alpha"}]}
    cases = [
        {"kit": safe_kit, "tokens": {"tokens": []}, "conf": {"pending": []}},
        {"kit": full_kit, "tokens": tokens, "conf": conf},
        {"kit": full_kit,
         "tokens": {"error": "только сессия панели", "code": "endpoint"},
         "conf": {"error": "только сессия панели", "code": "endpoint"}},
        {"kit": {"remote": {"mode": "safe", "source": "broken",
                            "reason": "файл не разобран"}},
         "tokens": {"tokens": []}, "conf": {"pending": []}},
        {"kit": {}, "tokens": {}, "conf": {}},
    ]
    got = _node_json(fn + "\nconst cases = " + json.dumps(cases, ensure_ascii=False) + ";\n"
                     "console.log(JSON.stringify(cases.map(c =>"
                     " mcpAccessModel(c.kit, c.tokens, c.conf))));")
    if got is None:
        return
    safe, full, remote, broken, empty = got
    assert safe["mode"] == "safe" and safe["stored"] == "safe", safe
    assert safe["source"] == "default" and safe["canManage"] is True, safe
    assert safe["adminNetwork"] is False and safe["activeTokens"] == 0, safe

    assert full["mode"] == "full" and full["stored"] == "full", full
    assert full["source"] == "file" and full["canManage"] is True, full
    assert full["adminNetwork"] is True, full
    assert full["activeTokens"] == 1 and full["adminTokens"] == 1, full
    assert full["pending"] == 1 and full["projects"] == {"alpha": "safe"}, full
    assert full["reason"], full

    assert remote["mode"] == "full" and remote["canManage"] is False, remote
    assert remote["remoteCaller"] is True, remote
    assert remote["tokenError"] and remote["confirmError"], remote

    assert broken["mode"] == "safe" and broken["source"] == "broken", broken
    assert broken["reason"] == "файл не разобран", broken
    assert empty["mode"] == "safe" and empty["source"] == "default", empty
    assert empty["canManage"] is True, empty


@test
def test_mcp_cockpit_panel_access_renders_safe_full_remote_and_loading(tmp: Path):
    """Карточка раздела: safe и full, красное предупреждение, badges, disabled и busy."""
    js = _panel_source()
    model_start = js.index("/* MCPK_ACCESS_MODEL_START */")
    model_end = js.index("/* MCPK_ACCESS_MODEL_END */") + \
        len("/* MCPK_ACCESS_MODEL_END */")
    render_start = js.index("/* MCPK_ACCESS_RENDER_START */")
    render_end = js.index("/* MCPK_ACCESS_RENDER_END */") + \
        len("/* MCPK_ACCESS_RENDER_END */")
    model_fn = js[model_start:model_end]
    render_fn = js[render_start:render_end]
    strings = _catalog("ru")
    code = (
        "const S = " + json.dumps(strings, ensure_ascii=False) + ";\n"
        "const t = (key, vars={}) => {\n"
        "  const s = S[key];\n"
        "  if (s === undefined) throw new Error('missing key ' + key);\n"
        "  let out = String(s);\n"
        "  for (const [k,v] of Object.entries(vars || {}))\n"
        "    out = out.replaceAll('{' + k + '}', String(v));\n"
        "  return out;\n"
        "};\n"
        "function el(tag, attrs={}, ...kids){\n"
        "  const n = {type:'node', tag, attrs:{}, kids:[], class: attrs.class || ''};\n"
        "  for (const [k,v] of Object.entries(attrs))\n"
        "    if (v !== null && v !== undefined) n.attrs[k] = v;\n"
        "  for (const kid of kids.flat()) if (kid !== null && kid !== undefined)\n"
        "    n.kids.push(typeof kid === 'object' ? kid : String(kid));\n"
        "  n.append = (...xs) => { for (const x of xs)\n"
        "    if (x !== null && x !== undefined) n.kids.push(x); };\n"
        "  return n;\n"
        "}\n"
        "const MCPK = {access: {data: null, model: null, busy: '', error: '',"
        " confirmId: ''}};\n"
        "const copyButton = () => el('button', {class:'btn sm'}, 'copy');\n"
        + model_fn + "\n" + render_fn + "\n"
        "function summary(model, data, busy, confirmId){\n"
        "  MCPK.access.data = data; MCPK.access.model = model;\n"
        "  MCPK.access.busy = busy || '';\n"
        "  MCPK.access.confirmId = confirmId || '';\n"
        "  const card = mcpAccessCard(model);\n"
        "  const nodes = [], texts = [];\n"
        "  const walk = n => {\n"
        "    if (typeof n !== 'object' || n === null) return;\n"
        "    nodes.push(n);\n"
        "    for (const kid of n.kids || [])\n"
        "      if (typeof kid === 'string') texts.push(kid); else walk(kid);\n"
        "  };\n"
        "  walk(card);\n"
        "  const pressed = nodes.filter(n => n.attrs &&"
        " n.attrs['aria-pressed'] !== undefined).map(n => ({\n"
        "    text: n.kids.join(' '), pressed: n.attrs['aria-pressed'],\n"
        "    disabled: n.attrs.disabled !== undefined,\n"
        "    aria: n.attrs['aria-disabled'], class: n.class}));\n"
        "  return {text: texts.join(' | '),\n"
        "    danger: nodes.filter(n => String(n.class || '').includes('dangerbox')).length,\n"
        "    ariaBusy: card.attrs['aria-busy'], pressed,\n"
        "    chips: nodes.filter(n => String(n.class || '').startsWith('chip'))\n"
        "      .map(n => n.kids.join(' '))};\n"
        "}\n"
        "const safeKit = {remote:{mode:'safe',stored:'safe',source:'default',"
        "deny:['git:push']}};\n"
        "const fullKit = {remote:{mode:'full',stored:'full',source:'file',"
        "admin_network:true,deny:['git:push'],projects:{alpha:'safe'}}};\n"
        "const tokens = {tokens:[{id:'a',name:'Ноутбук',status:'active',"
        "scopes:['remote:read','admin']}]};\n"
        "const conf = {pending:[{request_id:'r1',tool:'remote_run_command',"
        "project:'alpha'}]};\n"
        "const remote = {error:'только сессия панели',code:'endpoint'};\n"
        "const out = {\n"
        "  safe: summary(mcpAccessModel(safeKit, {tokens:[]}, {pending:[]}),"
        " {tokens:{tokens:[]},conf:{pending:[]}}, ''),\n"
        "  full: summary(mcpAccessModel(fullKit, tokens, conf), {tokens,conf}, ''),\n"
        "  remote: summary(mcpAccessModel(fullKit, remote, remote),"
        " {tokens:remote,conf:remote}, ''),\n"
        "  busy: summary(mcpAccessModel(safeKit, {tokens:[]}, {pending:[]}),"
        " {tokens:{tokens:[]},conf:{pending:[]}}, 'safe'),\n"
        "  stale: summary(mcpAccessModel(fullKit, {error:'реестр недоступен',"
        " code:'internal'}, {error:'очередь недоступна', code:'internal'}),"
        " {tokens:{error:'реестр недоступен'},"
        " conf:{error:'очередь недоступна'}}, '', 'one-time-code'),\n"
        "};\n"
        "console.log(JSON.stringify(out));\n"
    )
    got = _node_json(code)
    if got is None:
        return
    safe = got["safe"]
    assert "Безопасный режим" in safe["text"], safe
    assert "Действующий режим" in safe["text"] and "источник" in safe["text"], safe
    assert safe["danger"] == 0, safe
    assert safe["pressed"][0]["pressed"] == "true", safe
    assert safe["pressed"][1]["pressed"] == "false", safe
    assert safe["pressed"][0]["disabled"] is False, safe
    assert any("удалённое управление" in chip for chip in safe["chips"]), safe

    full = got["full"]
    assert full["danger"] >= 1, full
    assert "Полный доступ разрешает необратимые действия" in full["text"], full
    assert full["pressed"][1]["pressed"] == "true", full
    assert full["pressed"][1]["disabled"] is False, full
    assert "danger" in full["pressed"][1]["class"], full
    assert any("VPN" in chip for chip in full["chips"]), full
    assert any("admin" in chip for chip in full["chips"]), full
    assert "Подтверждения" in full["text"], full
    assert "Ждут решения: 1" in full["text"], full

    remote = got["remote"]
    assert all(button["disabled"] for button in remote["pressed"]), remote
    assert all(button["aria"] == "true" for button in remote["pressed"]), remote
    assert "локальная сессия панели" in remote["text"], remote
    assert "Подтверждения" not in remote["text"], remote

    busy = got["busy"]
    assert busy["ariaBusy"] == "true", busy
    assert all(button["disabled"] for button in busy["pressed"]), busy
    assert "Переключаю" in busy["text"], busy

    stale = got["stale"]
    assert "Одноразовый код: one-time-code" in stale["text"], stale
    assert "Очередь подтверждений не прочитана" in stale["text"], stale


@test
def test_mcp_cockpit_panel_access_wires_existing_endpoints_only(tmp: Path):
    """Раздел использует готовые endpoint'ы, не ломает старые карточки и держит aria."""
    js = _panel_source()
    for token in (
        "function mcpAccessModel(", "function renderMcpAccess(",
        "function mcpAccessCard(", "function mcpModeToggle(",
        "function mcpSetMode(", "function mcpConfirmAct(",
        "mcpk.access_title", "mcpk.mode_full", "mcpk.mode_safe",
        "mcpk.mode_effective", "mcpk.mode_source", "mcpk.mode_stored",
        "mcpk.mode_full_warn", "dangerbox", "mcpk.badge_vpn",
        "mcpk.badge_admin", "mcpk.local_only", "mcpk.loading",
        "mcpk.confirm_approve", "mcpk.confirm_reject",
        "/api/mcp/tokens?kind=remote", "/api/mcp/confirmations",
        "/api/mcp/confirm/approve", "/api/mcp/confirm/reject",
        "set_remote_mode", "aria-pressed", "aria-busy", "aria-disabled",
        'role:"group"', "disabled:", "mcpAccessCard(MCPK.access.model)",
        "mcpk.title", "function renderMcpKit(", "function mcpCard(",
        "function openMcpCard(", "function openMcpPaste(", "function openMcpRaw(",
    ):
        assert token in js, f"в panel.js нет {token}"
    assert "/api/mcp/kit" in js, "раздел не читает /api/mcp/kit"
    for bad in ("/api/mcp/mode", "/api/mcp/access", "/api/mcp/panel",
                "/api/mcp/effective"):
        assert bad not in js, f"появился новый endpoint {bad}"
    node = shutil.which("node")
    if node:
        cp = subprocess.run([node, "--check", str(KIT / "cockpit/ui/panel.js")],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        assert cp.returncode == 0, cp.stderr[:600]


@test
def test_mcp_cockpit_panel_access_i18n_both_languages_and_clean(tmp: Path):
    """Все ключи раздела есть в ru и en, новых префиксов нет, длинных тире нет."""
    assert all(key.startswith("mcpk.") for key in _MCP_PANEL_KEYS), "ключ вне mcpk.*"
    ru, en = _catalog("ru"), _catalog("en")
    for lang, data in (("ru", ru), ("en", en)):
        for key in _MCP_PANEL_KEYS:
            assert key in data and str(data[key]).strip(), (lang, key)
            assert "\u2014" not in data[key] and "\u2013" not in data[key], \
                (lang, key, data[key])
    ru_keys = {key for key in ru if key.startswith("mcpk.")}
    en_keys = {key for key in en if key.startswith("mcpk.")}
    assert ru_keys == en_keys, sorted(ru_keys ^ en_keys)
    asked = set(re.findall(r'\bt\("(mcpk\.[\w.]+)"', _panel_source()))
    for key in _MCP_PANEL_KEYS:
        covered = key in asked or any(a.endswith(".") and key.startswith(a)
                                      for a in asked)
        assert covered, f"панель не спрашивает {key}"


@test
def test_mcp_cockpit_panel_access_http_remote_token_and_vpn_badges(tmp: Path):
    """HTTP: сессия читает токены и переключает safe/full, удалённый токен получает 403."""
    kit = _kit(tmp)
    root = tmp / "root"
    _project(root)
    old_env = _tokens_env(kit)
    ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    try:
        token = _token(kit, ["remote:read", "remote:write", "admin"])
        status, _h, raw = call("GET", "/api/mcp/tokens?kind=remote",
                               _headers("", ck.TOKEN))
        data = _json(raw)
        assert status == 200 and len(data.get("tokens") or []) == 1, (status, raw)
        row = data["tokens"][0]
        assert row["status"] == "active" and "admin" in row["scopes"], row

        status, _h, raw = call("GET", "/api/mcp/confirmations",
                               _headers("", ck.TOKEN))
        assert status == 200 and _json(raw).get("pending") == [], (status, raw)

        status, _h, raw = call("GET", "/api/mcp/tokens?kind=remote",
                               _headers(token))
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)

        status, _h, raw = call("GET", "/api/mcp/kit", _headers(token))
        assert status == 200 and _json(raw)["remote"]["mode"] == "safe", (status, raw)
        status, _h, raw = call("GET", "/api/mcp/kit",
                               {**_headers(token), "X-Aurora-Admin-Network": "1"})
        state = _json(raw)
        assert status == 200 and state["remote"]["admin_network"] is True, (status, raw)

        status, _h, raw = call("POST", "/api/mcp/kit", _headers(token),
                               json.dumps({"action": "set_remote_mode",
                                           "mode": "full", "project": ""}))
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)

        for mode in ("full", "safe"):
            status, _h, raw = call("POST", "/api/mcp/kit", _headers("", ck.TOKEN),
                                   json.dumps({"action": "set_remote_mode",
                                               "mode": mode, "project": ""}))
            body = _json(raw)
            assert status == 200 and body.get("ok") is True, (status, raw)
            assert body["remote"]["mode"] == mode, body
        status, _h, raw = call("GET", "/api/mcp/kit", _headers("", ck.TOKEN))
        state = _json(raw)["remote"]
        assert state["mode"] == "safe" and state["source"] == "file", state
    finally:
        close()
        _restore_env(old_env)


@test
def test_mcp_cockpit_panel_access_css_responsive_and_focusable(tmp: Path):
    """Карточка раздела: одна колонка на 320/768, две на 1440, видимый focus-visible."""
    js = _panel_source()
    start = js.index("const MCP_ACCESS_CSS = `")
    end = js.index("`;", start) + 2
    block = js[start:end]
    for token in (".mcp-access", ".mcp-access-grid", "minmax(0,1fr)",
                  "minmax(240px,1fr)", "flex-wrap", "@media (max-width:768px)",
                  ":focus-visible", "outline:2px solid var(--primary)",
                  "overflow-wrap:anywhere"):
        assert token in block, f"в стилях раздела нет {token}"
    assert "function mcpAccessStyles(" in js, "стили раздела не устанавливаются"
    assert "mcpAccessStyles();" in js and "document.head.append" in js, \
        "стили раздела не попадают в документ"
    for fixed in ("width:320px", "width:1440px", "#fff", "#ffffff"):
        assert fixed not in block, f"жёсткая ширина или цвет вне токенов: {fixed}"
