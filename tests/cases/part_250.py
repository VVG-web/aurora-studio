"""Проверки части 250: реестр токенов MCP и общий слой доступа.

Все проверки работают во временном ките: настоящий `<kit>/local/mcp-tokens.json`,
`local/mcp.json` и `cockpit/.session.json` не читаются и не пишутся. Сеть не нужна —
панель поднимается на петлевом порту и отвечает своему же временному клиенту.
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from harness import KIT, SCRIPTS, assert_private_mode, test

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

import aurora_guard as G  # noqa: E402
import mcp_tokens as MT  # noqa: E402


def _kit(tmp: Path, name: str = "kit") -> Path:
    root = tmp / name
    (root / "local").mkdir(parents=True)
    return root


def _registry(kit: Path) -> Path:
    return kit / "local" / "mcp-tokens.json"


def _create(kit: Path, kind: str = "knowledge", scopes=None, projects=None,
            name: str = "Ноутбук", expires_days: int = 90) -> dict:
    scopes = ["knowledge:read"] if scopes is None else scopes
    projects = ["alpha"] if projects is None else projects
    return MT.new_token(kind, name, "ivanov@corp", scopes, projects, expires_days,
                        kit=str(kit))


def _refusal(code: str, fn, *args, **kwargs) -> MT.TokenError:
    try:
        fn(*args, **kwargs)
    except MT.TokenError as error:
        assert error.code == code, (code, error.code, error.message)
        return error
    raise AssertionError(f"ждали отказ {code}, вызов прошёл")


def _project(root: Path, name: str = "alpha") -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "aurora.config.yaml").write_text(
        f'project:\n  name: "{name}"\n  slug: "{name}"\n', encoding="utf-8")
    return path


def _server(ck, root: Path):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(root)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _call(srv):
    def request(method: str, path: str, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
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


@test
def test_mcp_tokens_lifecycle_keeps_only_a_hash_and_lists_no_secret(tmp: Path):
    """Выпуск, проверка, вращение и отзыв; в файле нет секрета, в списке нет хэша."""
    kit = _kit(tmp)
    first = _create(kit)
    token, record = first["token"], first["record"]
    path = _registry(kit)
    assert token.startswith(MT.TOKEN_PREFIX_KN), token[:12]
    assert record["prefix"] == MT.TOKEN_PREFIX_KN + record["id"] + "_", record
    assert record["kind"] == "knowledge" and record["scopes"] == ["knowledge:read"], record
    assert "hash" not in record, "хэш не отдаётся наружу"
    assert_private_mode(path)

    stored = path.read_text(encoding="utf-8")
    secret = token.split("_", 3)[3]
    assert secret not in stored, "секрет лёг в файл"
    assert record["prefix"] in stored and "sha256:" in stored, stored

    listed = MT.list_tokens(kit=str(kit))
    assert len(listed) == 1 and listed[0]["status"] == "active", listed
    blob = json.dumps(listed, ensure_ascii=False)
    assert token not in blob and "hash" not in blob and secret not in blob, blob
    assert MT.verify(token, kit=str(kit))["id"] == record["id"]

    other = _create(kit, name="Второй ноутбук")
    rotated = MT.rotate(record["id"], kit=str(kit))
    assert MT.verify(token, kit=str(kit)) is None, "старый секрет обязан умереть сразу"
    assert MT.verify(rotated["token"], kit=str(kit))["id"] == record["id"]
    assert MT.verify(other["token"], kit=str(kit))["id"] == other["record"]["id"], \
        "вращение задело соседний токен"
    assert len(MT.list_tokens(kit=str(kit))) == 2, "вращение плодит или теряет записи"
    assert rotated["record"]["rotated_count"] == 1, rotated["record"]
    assert Path(str(path) + ".bak").is_file(), "прежний файл не сохранён в .bak"

    revoked = MT.revoke(record["id"], kit=str(kit))
    assert MT.verify(rotated["token"], kit=str(kit)) is None, "отзыв не мгновенный"
    assert revoked["record"]["revoked_at"], revoked["record"]
    rows = MT.list_tokens(kit=str(kit))
    assert len(rows) == 2, rows
    assert {row["id"]: row["status"] for row in rows}[record["id"]] == "revoked", rows
    assert {row["id"]: row["status"] for row in rows}[other["record"]["id"]] == "active", rows


@test
def test_mcp_tokens_reject_wildcard_projects_for_every_kind(tmp: Path):
    """`["*"]` запрещён и знанию, и удалённому администратору; пусто — значит ни одного."""
    kit = _kit(tmp)
    for kind, scopes in (("knowledge", ["knowledge:read"]), ("remote", ["admin"])):
        error = _refusal("invalid_project", MT.new_token, kind, "x", "u", scopes, ["*"], 90,
                         kit=str(kit))
        assert error.status == 400, error.status
    empty = _create(kit, projects=[])
    assert empty["record"]["projects"] == [], empty["record"]
    assert MT.check_project(empty["record"], "alpha") is False
    assert MT.check_project(empty["record"], "") is False


@test
def test_mcp_tokens_reject_kind_and_scope_mismatch(tmp: Path):
    """Права одного вида не выдаются другому; неизвестное право и пустой набор — отказ."""
    kit = _kit(tmp)
    _refusal("bad_request", MT.new_token, "knowledge", "x", "u", ["remote:read"], ["alpha"],
             90, kit=str(kit))
    _refusal("bad_request", MT.new_token, "remote", "x", "u", ["knowledge:read"], ["alpha"],
             90, kit=str(kit))
    _refusal("bad_request", MT.new_token, "knowledge", "x", "u", [], ["alpha"], 90,
             kit=str(kit))
    _refusal("bad_request", MT.new_token, "remote", "x", "u", ["root"], ["alpha"], 90,
             kit=str(kit))
    _refusal("bad_request", MT.new_token, "robot", "x", "u", ["admin"], ["alpha"], 90,
             kit=str(kit))
    assert MT.scopes_for_kind("knowledge") == {"knowledge:read"}
    assert MT.scopes_for_kind("remote") == {"remote:read", "remote:write", "admin"}
    _refusal("bad_request", MT.scopes_for_kind, "robot")


@test
def test_mcp_tokens_last_used_is_written_at_most_once_per_minute(tmp: Path):
    """Отметка применения обновляется не чаще раза в 60 секунд на запись."""
    kit = _kit(tmp)
    base = 1_800_000_000.0
    with patch.object(MT, "_now", return_value=base):
        record = _create(kit)["record"]
        MT.touch(record["id"], ip="10.0.0.1", kit=str(kit))
        first = MT.last_used(record["id"], kit=str(kit))
        assert first["last_used_ip"] == "10.0.0.1" and first["last_used_at"], first
    with patch.object(MT, "_now", return_value=base + 30):
        MT.touch(record["id"], ip="10.0.0.2", kit=str(kit))
        second = MT.last_used(record["id"], kit=str(kit))
        assert second == first, "отметка обновилась раньше 60 секунд"
    with patch.object(MT, "_now", return_value=base + 61):
        MT.touch(record["id"], ip="10.0.0.3", kit=str(kit))
        third = MT.last_used(record["id"], kit=str(kit))
        assert third["last_used_ip"] == "10.0.0.3", third
        assert third["last_used_at"] != first["last_used_at"], third


@test
def test_mcp_tokens_project_acl_and_scope_helpers(tmp: Path):
    """ACL записи и общие проверки прав: путь панели сравнивается по хвосту."""
    record = {"projects": ["alpha"]}
    assert MT.check_project(record, "alpha") is True
    assert MT.check_project(record, str(tmp / "alpha")) is True
    assert MT.check_project(record, str(tmp / "other")) is False
    assert MT.check_project(record, "*") is False

    remote = G.AuthContext(kind="remote", scopes=frozenset({"remote:read"}),
                           projects=("alpha",))
    assert G.project_ok(remote, str(tmp / "alpha")) is True
    assert G.project_ok(remote, str(tmp / "other")) is False
    assert G.visible_projects(remote, ["alpha", str(tmp / "alpha"), str(tmp / "other")]) \
        == ["alpha", str(tmp / "alpha")]
    session = G.AuthContext(kind="session", projects=("*",),
                            scopes=frozenset(MT.SCOPES))
    assert G.project_ok(session, str(tmp / "other")) is True
    try:
        G.require_project(remote, str(tmp / "other"))
    except G.AuthError as error:
        assert (error.status, error.code) == (403, "project"), (error.status, error.code)
    else:
        raise AssertionError("чужой проект пропущен")
    try:
        G.require_project(remote, "")
    except G.AuthError as error:
        assert (error.status, error.code) == (400, "invalid_project"), (error.status, error.code)
    else:
        raise AssertionError("пустой проект пропущен")
    try:
        G.require_scope(remote, "admin")
    except G.AuthError as error:
        assert (error.status, error.code) == (403, "scope"), (error.status, error.code)
    else:
        raise AssertionError("чужое право пропущено")
    try:
        G.require_scope(None, "admin")
    except G.AuthError as error:
        assert (error.status, error.code) == (401, "token"), (error.status, error.code)
    else:
        raise AssertionError("без контекста доступ открыт")


@test
def test_mcp_tokens_malformed_registry_fails_closed_for_remote_only(tmp: Path):
    """Битый файл закрывает удалённые токены; локальная сессия панели продолжает работать."""
    kit = _kit(tmp)
    token = _create(kit)["token"]
    _registry(kit).write_text("{это не json", encoding="utf-8")
    assert MT.verify(token, kit=str(kit)) is None, "битый реестр принял токен"
    _refusal("internal", MT.list_tokens, kit=str(kit))
    assert "не разобран" in MT.registry_problem(kit=str(kit))
    session = G.authenticate({"X-Aurora-Token": "S"}, "127.0.0.1",
                             session_token="S", kit=str(kit))
    assert session is not None and session.kind == "session", session
    assert session.projects == ("*",), session.projects


@test
def test_mcp_tokens_active_limits_per_kind_and_per_kit(tmp: Path):
    """20 активных на вид и 100 на кит: предел проверяется до записи."""
    kit = _kit(tmp, "limits")
    for index in range(MT.MAX_ACTIVE_PER_KIND):
        _create(kit, name=f"k{index}")
    _refusal("conflict", MT.new_token, "knowledge", "лишний", "u", ["knowledge:read"],
             ["alpha"], 90, kit=str(kit))

    kit2 = _kit(tmp, "limits-kit")
    for index in range(MT.MAX_ACTIVE_PER_KIND):
        _create(kit2, name=f"k{index}")
    with patch.object(MT, "MAX_ACTIVE_PER_KIT", MT.MAX_ACTIVE_PER_KIND + 1):
        _create(kit2, kind="remote", scopes=["remote:read"], name="r0")
        _refusal("conflict", MT.new_token, "remote", "r1", "u", ["remote:read"],
                 ["alpha"], 90, kit=str(kit2))


@test
def test_mcp_tokens_guard_rate_limits_proxy_and_audit(tmp: Path):
    """Предел частоты, доверие прокси и журнал без секретов, тел и строк запроса."""
    limiter = G.RateLimiter({"create": (2, 60)})
    key = ("create", "t1", "127.0.0.1", "/api/mcp/tokens/create")
    assert limiter.allow(key)[0] is True
    assert limiter.allow(key)[0] is True
    allowed, retry = limiter.allow(key)
    assert allowed is False and retry >= 1, (allowed, retry)
    assert limiter.allow(("other", "t1", "127.0.0.1", "/x"))[0] is True

    assert G.trusted_proxy("127.0.0.1") is True
    assert G.trusted_proxy("::1") is True
    assert G.trusted_proxy("10.0.0.8") is False
    forwarded = {"X-Forwarded-For": "198.51.100.7, 10.0.0.1"}
    assert G.client_ip(forwarded, "127.0.0.1") == "198.51.100.7"
    assert G.client_ip(forwarded, "10.0.0.8") == "10.0.0.8"
    admin = {"X-Aurora-Admin-Network": "1"}
    assert G.admin_network(admin, "127.0.0.1") is True
    assert G.admin_network(admin, "10.0.0.8") is False

    path = tmp / "audit" / "mcp-tokens.jsonl"
    G.audit_write(path, {"tool": "mcp_tokens", "result": "created",
                         "token": "aur_kn_abc_secret", "secret": "shh",
                         "body": {"prompt": "не писать"},
                         "url": "https://panel.example/api?token=abc", "project": "alpha"})
    line = path.read_text(encoding="utf-8").strip()
    assert "aur_kn_" not in line and "shh" not in line and "prompt" not in line, line
    assert "?token=" not in line and '"body"' not in line, line
    assert '"project": "alpha"' in line and '"result": "created"' in line, line
    assert_private_mode(path)


@test
def test_mcp_tokens_authenticate_and_exact_remote_allowlist(tmp: Path):
    """Сессия, знание и удалённый токен; allowlist сравнивает метод и путь без запроса."""
    kit = _kit(tmp)
    token = _create(kit, kind="remote", scopes=["remote:read", "remote:write"])["token"]
    remote = G.authenticate({"Authorization": f"Bearer {token}"}, "127.0.0.1", kit=str(kit))
    assert remote is not None and remote.kind == "remote", remote
    assert remote.scopes == frozenset({"remote:read", "remote:write"}), remote.scopes
    assert remote.projects == ("alpha",) and remote.source == "header", remote

    assert G.remote_allowed(remote, "GET", "/api/jobs?project=alpha") is True
    assert G.remote_allowed(remote, "GET", "/api/mcp/tokens") is False
    assert G.remote_allowed(remote, "POST", "/api/mcp/kit") is False
    assert G.remote_allowed(remote, "POST", "/api/mcp/confirm/request") is True
    assert G.remote_allowed(remote, "POST", "/api/run?x=1") is True
    assert G.remote_allowed(remote, "DELETE", "/api/jobs") is False
    assert G.remote_allowed(remote, "GET", "/api/nope") is False
    assert G.remote_allowed(remote, "GET", "/api/mcp/confirmations") is False
    for method, path in G.REMOTE_ALLOWED:
        assert G.remote_allowed(remote, method, path) is True, (method, path)
    assert G.endpoint_scope("POST", "/api/cron/save") == "admin"
    assert G.endpoint_scope("POST", "/api/run") == "remote:write"
    assert G.endpoint_scope("GET", "/api/ping") == "remote:read"

    session = G.authenticate({"X-Aurora-Token": "S"}, "127.0.0.1",
                             session_token="S", kit=str(kit))
    assert session is not None and session.kind == "session", session
    assert session.projects == ("*",) and session.scopes == frozenset(MT.SCOPES), session
    assert G.remote_allowed(session, "GET", "/api/mcp/tokens") is True
    assert G.authenticate({"X-Aurora-Token": "нет"}, "127.0.0.1", kit=str(kit)) is None

    knowledge = _create(kit, scopes=["knowledge:read"])["token"]
    known = G.authenticate({"X-Aurora-Token": knowledge}, "127.0.0.1", kit=str(kit))
    assert known is not None and known.kind == "knowledge", known
    assert G.remote_allowed(known, "GET", "/api/ping") is False, "знание не ходит в панель"


@test
def test_mcp_tokens_api_is_session_only_and_lists_no_secret(tmp: Path):
    """HTTP: сессия выпускает и читает токены; удалённый токен получает `403 endpoint`."""
    root = tmp / "root"
    project = _project(root)
    kit = _kit(tmp)
    token_path = _registry(kit)
    old_env = os.environ.get("AURORA_MCP_TOKENS_FILE")
    os.environ["AURORA_MCP_TOKENS_FILE"] = str(token_path)
    ck = importlib.import_module("aurora_cockpit")
    old_limiter = ck.TOKEN_LIMITER
    ck.TOKEN_LIMITER = G.RateLimiter({})   # предел проверяется отдельной проверкой
    srv = _server(ck, root)
    call = _call(srv)
    session = {"X-Aurora-Token": ck.TOKEN, "Content-Type": "application/json"}
    try:
        body = json.dumps({"kind": "knowledge", "name": "Ноутбук", "user": "ivanov@corp",
                           "scopes": ["knowledge:read"], "projects": ["alpha"],
                           "expires_days": 30})
        status, headers, raw = call("POST", "/api/mcp/tokens/create", session, body)
        created = _json(raw)
        assert status == 200 and created.get("token"), (status, raw)
        assert headers.get("cache-control") == "no-store", headers
        assert "hash" not in created["record"], created["record"]
        token = created["token"]

        status, _headers, raw = call("GET", "/api/mcp/tokens", session)
        listed = _json(raw).get("tokens") or []
        assert status == 200 and len(listed) == 1, (status, raw)
        assert token not in raw and "hash" not in raw, raw
        assert listed[0]["prefix"] == created["record"]["prefix"], listed[0]

        remote = _create(kit, kind="remote", scopes=["remote:read"],
                         projects=["other"], name="R")["token"]
        bearer = {"Authorization": f"Bearer {remote}"}
        status, _headers, raw = call("GET", "/api/mcp/tokens", bearer)
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)
        status, _headers, raw = call("POST", "/api/mcp/kit", {**bearer,
                                                             "Content-Type": "application/json"}, "{}")
        assert status == 403 and _json(raw).get("code") == "endpoint", (status, raw)
        status, _headers, raw = call("GET", f"/api/jobs?project={project}", bearer)
        assert status == 403 and _json(raw).get("code") == "project", (status, raw)

        allowed = _create(kit, kind="remote", scopes=["remote:read"],
                          projects=["alpha"], name="R2")["token"]
        status, _headers, raw = call("GET", f"/api/jobs?project={project}",
                                     {"Authorization": f"Bearer {allowed}"})
        assert status == 200 and _json(raw).get("jobs") == [], (status, raw)

        status, _headers, raw = call("GET", "/api/ping",
                                     {"X-Aurora-Token": "aur_kn_bad_secret"})
        assert status == 401 and _json(raw).get("code") == "token", (status, raw)
    finally:
        srv.shutdown()
        ck.TOKEN_LIMITER = old_limiter
        if old_env is None:
            os.environ.pop("AURORA_MCP_TOKENS_FILE", None)
        else:
            os.environ["AURORA_MCP_TOKENS_FILE"] = old_env


@test
def test_mcp_tokens_api_returns_429_with_retry_after(tmp: Path):
    """Второй выпуск сверх предела — 429 и `Retry-After`, а не тихая запись."""
    root = tmp / "root"
    _project(root)
    kit = _kit(tmp)
    old_env = os.environ.get("AURORA_MCP_TOKENS_FILE")
    os.environ["AURORA_MCP_TOKENS_FILE"] = str(_registry(kit))
    ck = importlib.import_module("aurora_cockpit")
    old_limiter = ck.TOKEN_LIMITER
    ck.TOKEN_LIMITER = G.RateLimiter({"tokens:create:user": (1, 60),
                                      "tokens:create:ip": (1, 60)})
    srv = _server(ck, root)
    call = _call(srv)
    session = {"X-Aurora-Token": ck.TOKEN, "Content-Type": "application/json"}
    body = json.dumps({"kind": "knowledge", "name": "N", "user": "u",
                       "scopes": ["knowledge:read"], "projects": ["alpha"]})
    try:
        status, _headers, _raw = call("POST", "/api/mcp/tokens/create", session, body)
        assert status == 200, status
        status, headers, raw = call("POST", "/api/mcp/tokens/create", session, body)
        assert status == 429 and _json(raw).get("code") == "rate_limited", (status, raw)
        assert int(headers.get("retry-after") or 0) >= 1, headers
    finally:
        srv.shutdown()
        ck.TOKEN_LIMITER = old_limiter
        if old_env is None:
            os.environ.pop("AURORA_MCP_TOKENS_FILE", None)
        else:
            os.environ["AURORA_MCP_TOKENS_FILE"] = old_env


@test
def test_mcp_tokens_isolated_tests_refuse_the_real_registry(tmp: Path):
    """В изолированном прогоне обращение к настоящему реестру кита — ошибка, не запись."""
    old_env = os.environ.pop("AURORA_MCP_TOKENS_FILE", None)
    try:
        for call in (lambda: MT.load_registry(),
                     lambda: MT.save_registry({"version": 1, "tokens": []})):
            try:
                call()
            except RuntimeError as error:
                assert "refusing real token registry" in str(error), error
            else:
                raise AssertionError("настоящий реестр кита доступен тесту")
    finally:
        if old_env is not None:
            os.environ["AURORA_MCP_TOKENS_FILE"] = old_env
