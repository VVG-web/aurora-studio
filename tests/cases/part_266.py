"""Проверки части 266: локальный API `/api/usage` дорожки C.

API отдаёт точный и приблизительный блоки раздельно, принимает окно и фильтры,
не пускает удалённые токены и не раскрывает ключи, пути, промпты и URL. Пустой
проект отвечает `200` с причиной в `quality.empty_reason`, неверные параметры —
`400`. `days=0` ограничен 90 днями, `compare` выключен для `days=0` и окон
длиннее 45 дней, `limit` обрезает таблицы, но не итог.
"""
from __future__ import annotations

import http.client
import importlib
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import KIT, SCRIPTS, make_project, test

for _path in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

UM = importlib.import_module("usage_metrics")
CK = importlib.import_module("aurora_cockpit")
G = importlib.import_module("aurora_guard")

NOW = time.time()


def _stamp(delta: timedelta) -> str:
    return (datetime.fromtimestamp(NOW, timezone.utc) - delta).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _exact(root: Path, run: str, *, at: str | None = None, project: str = "Test",
           project_id: str = "Test", task: str = "build", kind: str = "agent",
           provider: str = "gw", model: str = "m1", role: str = "worker",
           tokens_in: int = 100, tokens_out: int = 50, calls: int = 2,
           schema: int = 2, by_cell: bool = True) -> Path:
    at = at or _stamp(timedelta(minutes=5))
    payload = {
        "schema": schema, "at": at, "started": at, "project": project,
        "project_id": project_id, "run": run, "kind": kind, "task": task,
        "calls": calls, "calls_failed": 0, "requests": calls,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "tokens_failed_in": 0, "tokens_failed_out": 0,
        "cached_in": 0, "reasoning_out": 0, "cache_hits": 0,
        "usage_missing_calls": 0, "gen_seconds": 1, "estimated": False, "errors": 0,
        "by_role": {}, "by_model": {}, "by_pair": {},
    }
    if by_cell:
        payload["by_cell"] = [{
            "provider": provider, "provider_name": provider, "role": role,
            "model": model, "calls": calls, "calls_failed": 0,
            "requests": calls, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cached_in": 0, "reasoning_out": 0, "gen_seconds": 1,
        }]
    path = root / "AuroraKnowledgeDB" / "meta" / "usage.d" / "u-m" / f"{run}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class _Server:
    def __init__(self, roots):
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CK.Handler)
        self.server.roots = list(roots)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                          timeout=30)
        conn.request("GET", path, headers={"X-Aurora-Token": CK.TOKEN})
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        conn.close()
        return response.status, body

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _json_get(server: _Server, path: str):
    status, body = server.get(path)
    return status, json.loads(body), body


@test
def test_usage_api_empty_is_200_with_empty_reason(tmp: Path):
    """Пустой проект: форма schema 2 и честная причина, а не 500."""
    root = make_project(tmp)
    server = _Server([str(tmp)])
    try:
        status, data, _body = _json_get(server, "/api/usage?days=7&tz=UTC")
        assert status == 200, (status, data)
        assert data["schema"] == 2, data
        assert data["quality"]["empty"] is True, data["quality"]
        assert data["quality"]["empty_reason"] == "no_records", data["quality"]
        assert data["totals"]["tokens_total"] == 0, data["totals"]
        assert data["by_project"] == [] and data["series"] == [], data
    finally:
        server.close()
    assert root.is_dir()


@test
def test_usage_api_bad_parameters_are_400(tmp: Path):
    """Неверные параметры: 400 и `bad_request`, без падения сервера."""
    make_project(tmp)
    server = _Server([str(tmp)])
    try:
        cases = ("days=2", "grain=week", "limit=0", "tz=No/Such_Zone",
                 "from=2026-11-03&to=2026-11-01", "limit=101",
                 "to=9999-12-31", "to=0001-01-01", "task=sk-live-secret")
        for query in cases:
            status, data, body = _json_get(server, f"/api/usage?{query}")
            assert status == 400, (query, status, body[:300])
            assert data["code"] == "bad_request", (query, data)
    finally:
        server.close()


@test
def test_usage_api_filters_window_grain_and_limit(tmp: Path):
    """Фильтры меняют итог, limit режет таблицу, но не totals, grain даёт час."""
    root = make_project(tmp)
    _exact(root, "run-a", project_id="alpha", tokens_in=100, tokens_out=50)
    _exact(root, "run-b", project_id="beta", provider="other", model="m2",
           role="critic", task="review", kind="job", tokens_in=20, tokens_out=10)
    _exact(root, "run-old", at=_stamp(timedelta(days=100)), tokens_in=999, tokens_out=999)
    today = datetime.fromtimestamp(NOW, timezone.utc).date()
    frm, to = str(today - timedelta(days=2)), str(today)
    base = {"from": frm, "to": to, "tz": "UTC", "limit": "1"}

    got = UM.query([str(root)], base, now=NOW)
    assert got["totals"]["runs"] == 2, got["totals"]
    assert got["totals"]["tokens_total"] == 180, got["totals"]
    assert len(got["by_project"]) == 1, got["by_project"]
    assert got["quality"]["truncated"] is True, got["quality"]
    assert got["series"][0]["bucket"] == str(today), got["series"]
    for key in ("last_at", "delta_pct", "spark"):
        assert key in got["by_project"][0], got["by_project"][0]
    assert got["by_project"][0]["spark"], got["by_project"][0]
    assert "last_at" in got["by_model"][0], got["by_model"][0]
    assert "projects" in got["by_provider"][0], got["by_provider"][0]
    assert "last_at" in got["by_provider"][0], got["by_provider"][0]

    filtered = UM.query([str(root)], {**base, "project": "alpha", "model": "m1"},
                        now=NOW)
    assert filtered["totals"]["tokens_total"] == 150, filtered["totals"]
    assert filtered["filters"]["project"] == ["alpha"], filtered["filters"]
    assert all(row["project_id"] == "alpha" for row in filtered["by_project"])

    hourly = UM.query([str(root)], {**base, "grain": "hour"}, now=NOW)
    assert hourly["window"]["grain"] == "hour", hourly["window"]
    assert hourly["series"] and "hour" in hourly["series"][0], hourly["series"]

    none = UM.query([str(root)], {**base, "project": "missing"}, now=NOW)
    assert none["quality"]["empty_reason"] == "filter", none["quality"]


@test
def test_usage_api_days_zero_and_compare_rules(tmp: Path):
    """days=0 держит 90 дней; compare выключен для days=0 и окон длиннее 45 дней."""
    root = make_project(tmp)
    _exact(root, "run-now", at=_stamp(timedelta(days=1)), tokens_in=10, tokens_out=5)
    _exact(root, "run-prev", at=_stamp(timedelta(days=8)), tokens_in=7, tokens_out=3)
    _exact(root, "run-old", at=_stamp(timedelta(days=100)), tokens_in=999, tokens_out=999)

    got = UM.query([str(root)], {"days": "7", "tz": "UTC", "compare": "1"}, now=NOW)
    assert got["previous"] is not None, got
    assert got["previous"]["totals"]["runs"] == 1, got["previous"]
    assert got["previous"]["totals"]["tokens_total"] == 10, got["previous"]
    assert got["by_project"][0]["delta_pct"] == 50.0, got["by_project"][0]

    zero = UM.query([str(root)], {"days": "0", "tz": "UTC", "compare": "1"}, now=NOW)
    assert zero["window"]["days"] == 0, zero["window"]
    assert zero["totals"]["tokens_total"] == 25, zero["totals"]
    assert zero["previous"] == {"reason": "days=0"}, zero["previous"]

    long = UM.query([str(root)], {"days": "90", "tz": "UTC", "compare": "1"}, now=NOW)
    assert long["previous"] == {"reason": "days>45"}, long["previous"]


@test
def test_usage_api_key_present_and_no_secrets(tmp: Path):
    """key_present true/false/null, сам ключ, пути и промпты в ответ не попадают."""
    root = make_project(tmp)
    _exact(root, "run-key", provider="gw", tokens_in=10, tokens_out=5)
    _exact(root, "run-fallback", provider="nokey", model="m2", tokens_in=3, tokens_out=2)
    providers = {"providers": [
        {"id": "gw", "key": "sk-live-secret"},
        {"id": "nokey", "key": ""},
    ]}
    server = _Server([str(tmp)])
    try:
        with mock.patch.object(CK, "models_data", return_value=providers):
            status, data, body = _json_get(
                server, "/api/usage?days=7&tz=UTC&from=" +
                str(datetime.fromtimestamp(NOW, timezone.utc).date() - timedelta(days=2)) +
                "&to=" + str(datetime.fromtimestamp(NOW, timezone.utc).date()))
        assert status == 200, (status, body[:300])
        by_id = {row["provider"]: row["key_present"] for row in data["by_provider"]}
        assert by_id == {"gw": True, "nokey": False}, data["by_provider"]
        assert "sk-live-secret" not in body, body
        for forbidden in ("key_seq", "api_key", "cost", "currency", "prompt", "http://", "https://"):
            assert forbidden not in body, forbidden
        assert str(root) not in body and str(root.parent) not in body, body
    finally:
        server.close()


@test
def test_usage_api_bad_json_and_engine_old(tmp: Path):
    """Битый JSON не роняет ответ; проект без usage_log получает engine_old."""
    root = make_project(tmp)
    _exact(root, "run-good", tokens_in=1, tokens_out=1)
    bad = root / "AuroraKnowledgeDB/meta/usage.d/u-m/bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    got = UM.query([str(root)], {"days": "7", "tz": "UTC"}, now=NOW)
    assert got["quality"]["records_bad"] == 1, got["quality"]
    assert got["totals"]["tokens_total"] == 2, got["totals"]

    (tmp / "old").mkdir()
    old = make_project(tmp / "old")
    (old / ".aurora/scripts/usage_log.py").unlink()
    old_got = UM.query([str(old)], {"days": "7", "tz": "UTC"}, now=NOW)
    assert old_got["quality"]["empty_reason"] == "engine_old", old_got["quality"]


@test
def test_usage_api_all_old_and_no_calls_reasons(tmp: Path):
    """Старая запись без разбивки и запись без вызовов называют свою причину."""
    root = make_project(tmp)
    _exact(root, "run-old-schema", tokens_in=10, tokens_out=5, calls=1,
           schema=1, by_cell=False)
    got = UM.query([str(root)], {"days": "7", "tz": "UTC"}, now=NOW)
    assert got["quality"]["empty_reason"] == "all_old", got["quality"]

    (tmp / "no-calls").mkdir()
    empty_calls = make_project(tmp / "no-calls")
    _exact(empty_calls, "run-no-calls", tokens_in=0, tokens_out=0, calls=0)
    no_calls = UM.query([str(empty_calls)], {"days": "7", "tz": "UTC"}, now=NOW)
    assert no_calls["quality"]["empty_reason"] == "no_calls", no_calls["quality"]


@test
def test_usage_api_task_text_is_not_echoed(tmp: Path):
    """Свободный текст задачи, путь и ключ не попадают в by_task и фильтры."""
    root = make_project(tmp)
    _exact(root, "run-raw", task="Разобрать секретный документ")
    got = UM.query([str(root)], {"days": "7", "tz": "UTC"}, now=NOW)
    body = json.dumps(got, ensure_ascii=False)
    assert "секретный" not in body and "Разобрать" not in body, body
    assert all(row["task"] in ("-", "build") for row in got["by_task"]), got["by_task"]
    try:
        UM.query([str(root)], {"days": "7", "task": "sk-live-secret"}, now=NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("небезопасный фильтр принят")


@test
def test_usage_api_is_not_available_to_remote_tokens(_tmp: Path):
    """Новый маршрут остаётся локальным: его нет в точном remote allowlist."""
    assert "/api/usage" not in G.REMOTE_READ_PATHS, G.REMOTE_READ_PATHS
    assert ("GET", "/api/usage") not in G.REMOTE_ALLOWED, G.REMOTE_ALLOWED
