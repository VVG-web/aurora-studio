"""Проверки части 259: ACL проекта, safe-режим проекта и токены в текстах ошибок.

Каждая проверка поднимает настоящую HTTP-панель на петлевом порту и говорит с ней
обычным клиентом: именно этот путь был открыт удалённому токену. Опасные действия
(`start_job`, маршрут, расписание, перезапуск) подменены — ни один процесс не
запускается, ни одна чужая настройка машины не читается.

Закрываются четыре P1 из аудита интеграции wave1a:

* P1-1 — удалённый токен с `projects=["alpha"]` не читает и не меняет чужой проект
  через allowlist;
* P1-2 — задание расписания принадлежит проектам своих шагов; чужие задания не
  видны и не правятся, `runs`/`current`/`queue` фильтруются;
* P1-3 — `safe` проекта сильнее общего `full` на HTTP-границе;
* P1-4 — `aur_rm_…`/`aur_kn_…` вырезаются из ошибок прогона и диагностики без
  имени поля.

Отдельно закрывается P1 аудита M1: расписание не запускает `ship:release` ни
прямым шагом, ни шагом маршрута, а удалённые `start`/`toggle` такого задания
требуют `full`, `admin` и подтверждения.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from harness import set_home, test
from cases.part_252 import (_headers, _json, _kit, _panel, _project,
                            _request_confirmation, _restore_env, _token, _tokens_env,
                            _write_mode, _call)

import aurora_common as AC  # noqa: E402
import aurora_guard as G  # noqa: E402
import aurora_remote_mcp as RM  # noqa: E402
import aurora_cockpit_client as CC  # noqa: E402
import cron as CRON  # noqa: E402
import diagnostics as D  # noqa: E402
import mcp_tokens as MT  # noqa: E402

ck = importlib.import_module("aurora_cockpit")


def _p(path: Path) -> str:
    return quote(str(path), safe="")


def _body(raw: str) -> dict:
    got = _json(raw)
    return got if isinstance(got, dict) else {}


def _home(tmp: Path):
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home, set_home(home)


def _other_run(project: Path, run_id: str = "20260101-000000") -> Path:
    run = project / ".aurora" / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "meta.json").write_text(json.dumps({
        "id": run_id, "kind": "command", "title": "secret-other-command",
        "started": "2026-01-01T00:00:00Z", "finished": "2026-01-01T00:00:01Z",
        "status": "done", "rc": 0, "seconds": 1,
    }), encoding="utf-8")
    (run / "console.log").write_text("secret-other-output\n", encoding="utf-8")
    (run / "failures.jsonl").write_text(json.dumps({
        "at": "1", "kind": "stage", "error": "needle-other-error",
    }) + "\n", encoding="utf-8")
    return run


def _cron_task(task_id: str, project_dir, command: str = "kb:links") -> dict:
    return {"id": task_id, "name": "задание " + task_id, "enabled": True, "net": "",
            "time": "10:00", "days": [1], "date": "", "order": "projects",
            "on_fail": "next", "lang": "ru",
            "steps": [{"kind": "command", "project": str(project_dir),
                       "cmd": command, "args": []}]}


# --------------------------------------------------------------- P1-1: чтение
@test
def test_security_fix_acl_read_endpoints_reject_foreign_project(tmp: Path):
    """Чужой проект закрыт на всех allowlist-путях чтения, в ответе нет его данных."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    other = _project(root, "other")
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    token = _token(kit, scopes=["remote:read"], projects=["alpha"])
    _other_run(other)
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    try:
        headers = _headers(token)
        endpoints = [
            "/api/history?project=" + _p(other),
            "/api/history/run?project=" + _p(other) + "&id=20260101-000000",
            "/api/run/logs?project=" + _p(other),
            "/api/run/file?project=" + _p(other) + "&run=20260101-000000",
            "/api/run/steps?project=" + _p(other) + "&run=20260101-000000",
            "/api/health?project=" + _p(other),
            "/api/artifacts?project=" + _p(other),
            "/api/report?project=" + _p(other),
            "/api/kinds?project=" + _p(other),
            "/api/agent?project=" + _p(other),
            "/api/net/state?project=" + _p(other),
        ]
        for path in endpoints:
            status, _h, raw = call("GET", path, headers)
            assert status == 403, (path, status, raw)
            assert _body(raw).get("code") == "project", (path, raw)
            assert "secret-other" not in raw and "needle-other" not in raw, (path, raw)

        status, _h, raw = call("GET", "/api/history?project=" + _p(alpha), headers)
        assert status == 200 and _body(raw).get("runs") == [], (status, raw)
    finally:
        close()
        _restore_env(old_env)
        restore_home()


# ------------------------------------------------- P1-1: запись и id-only пути
@test
def test_security_fix_acl_write_and_id_endpoints_reject_foreign_project(tmp: Path):
    """Запуск, маршрут, подтверждения и остановки по id не трогают чужой проект."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    other = _project(root, "other")
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    token = _token(kit, projects=["alpha"])
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    started, stopped, routes_started, routes_stopped = [], [], [], []
    controlled = []
    built = []

    class _Tasks:
        def snapshot(self, project=""):
            return {"active": [{"id": "task-other", "project": str(other)},
                               {"id": "task-alpha", "project": str(alpha)}],
                    "queued": []}

        def preview(self, task):
            return ""

    def command(cmd):
        if cmd == "dev:tests":
            return {"cmd": cmd, "runnable": True, "ns": "dev", "flags": [],
                    "fixed_flags": [], "script": "run_tests.py"}
        return {"cmd": cmd, "runnable": True, "ns": "kb", "flags": [],
                "fixed_flags": [], "script": "kb_links.py"}

    old_tasks = srv_ck.TASKS
    srv_ck.JOBS["job-other"] = {"id": "job-other", "project": str(other)}
    srv_ck.JOBS["job-alpha"] = {"id": "job-alpha", "project": str(alpha)}
    srv_ck.ROUTES["route-other"] = {"project": str(other), "run": object()}
    srv_ck.ROUTES["route-alpha"] = {"project": str(alpha), "run": object()}
    try:
        with patch.object(srv_ck, "command_by_name", command), \
                patch.object(srv_ck, "start_job",
                             lambda project, cmd, args, **kw: started.append((project, cmd)) or "job-1"), \
                patch.object(srv_ck, "version_gap", lambda *_a, **_k: ""), \
                patch.object(srv_ck, "net_enabled", lambda: False), \
                patch.object(srv_ck, "kit_is_source", lambda: True), \
                patch.object(srv_ck, "TASKS", _Tasks()), \
                patch.object(srv_ck, "stop_job",
                             lambda job_id, lang="": stopped.append(job_id) or {"ok": True}), \
                patch.object(srv_ck, "route_start",
                             lambda *a, **k: routes_started.append(a[0]) or {"route": "r1"}), \
                patch.object(srv_ck, "route_action",
                             lambda rid, what: routes_stopped.append((rid, what)) or {"ok": True}), \
                patch.object(srv_ck, "all_scenarios",
                             lambda: [{"id": "update", "title": "Обновить", "write": True,
                                       "destructive": False,
                                       "steps": [{"cmd": "kb:links"}]}]), \
                patch.object(srv_ck, "ask_pipeline_control",
                             lambda project, payload: controlled.append(("ask", project)) or {"ok": True}), \
                patch.object(srv_ck, "make_pipeline_control",
                             lambda project, payload: controlled.append(("make", project)) or {"ok": True}), \
                patch.object(D, "build",
                             lambda project, context, run_id="", deep=False:
                             built.append(project) or ("diag.zip", b"zip")):
            def post(path, body):
                return call("POST", path, _headers(token), json.dumps(body))

            status, _h, raw = post("/api/run", {"project": str(other), "cmd": "kb:links",
                                                "args": []})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            assert started == [], started

            status, _h, raw = post("/api/run", {"project": str(alpha), "cmd": "kb:links",
                                                "args": []})
            assert status == 200 and started and started[-1][0] == str(alpha), (status, raw, started)

            status, _h, raw = post("/api/run", {"project": str(alpha), "cmd": "dev:tests",
                                                "args": []})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            assert len(started) == 1, started

            status, _h, raw = post("/api/route/run", {"project": str(other), "scId": "update"})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            assert routes_started == [], routes_started

            status, _h, raw = post("/api/route/run", {"project": str(alpha), "scId": "update"})
            assert status == 200 and routes_started == [str(alpha)], (status, raw, routes_started)

            status, _h, raw = post("/api/task/check", {"project": str(other), "cmd": "kb:links",
                                                       "args": [], "kind": "job"})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            status, _h, raw = post("/api/task/check", {"project": str(alpha), "cmd": "kb:links",
                                                       "args": [], "kind": "job"})
            assert status == 200 and _body(raw).get("can_start") is True, (status, raw)

            for path, body in (("/api/ask/control", {"project": str(other), "action": "stop"}),
                               ("/api/make/control", {"project": str(other), "action": "stop"})):
                status, _h, raw = post(path, body)
                assert status == 403 and _body(raw).get("code") == "project", (path, status, raw)
            assert controlled == [], controlled
            status, _h, raw = post("/api/ask/control", {"project": str(alpha), "action": "stop"})
            assert status == 200 and controlled[-1] == ("ask", str(alpha)), (status, raw, controlled)
            status, _h, raw = post("/api/make/control", {"project": str(alpha), "action": "stop"})
            assert status == 200 and controlled[-1] == ("make", str(alpha)), (status, raw, controlled)

            status, _h, raw = post("/api/job/stop", {"id": "job-other"})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            status, _h, raw = post("/api/job/stop", {"id": "job-alpha"})
            assert status == 200 and stopped == ["job-alpha"], (status, raw, stopped)

            status, _h, raw = post("/api/task/stop", {"id": "task-other"})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            status, _h, raw = post("/api/task/stop", {"id": "task-alpha"})
            assert status == 200 and stopped[-1] == "task-alpha", (status, raw, stopped)

            status, _h, raw = post("/api/route/stop", {"id": "route-other"})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            assert routes_stopped == [], routes_stopped
            status, _h, raw = post("/api/route/stop", {"id": "route-alpha"})
            assert status == 200 and routes_stopped == [("route-alpha", "stop")], \
                (status, raw, routes_stopped)

            status, _h, raw = post("/api/diagnostics", {"project": str(alpha),
                                                        "job_id": "job-other"})
            assert status == 403 and _body(raw).get("code") == "project", (status, raw)
            assert built == [], built
            status, _h, raw = post("/api/diagnostics", {"project": str(alpha),
                                                        "job_id": "job-alpha"})
            assert status == 200 and built == [str(alpha)], (status, raw, built)
    finally:
        srv_ck.JOBS.pop("job-other", None)
        srv_ck.JOBS.pop("job-alpha", None)
        srv_ck.ROUTES.pop("route-other", None)
        srv_ck.ROUTES.pop("route-alpha", None)
        srv_ck.TASKS = old_tasks
        close()
        _restore_env(old_env)
        restore_home()


# ------------------------------------------------------- P1-3: safe проекта
@test
def test_security_fix_project_safe_wins_over_global_full_on_http(tmp: Path):
    """`full` в общем файле не отменяет `safe` проекта для run/route/cron/restart."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    _write_mode(kit, "full", {"alpha": "safe"})
    old_env = _tokens_env(kit)
    token = _token(kit, projects=["alpha"])
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    started, routes_started = [], []
    try:
        ctx = G.authenticate({"Authorization": "Bearer " + token}, "127.0.0.1", kit=str(kit))
        assert ctx is not None and ctx.mode == "full", ctx
        assert G.effective_mode(str(alpha), kit=str(kit)) == "safe"
        assert G.effective_mode("", kit=str(kit)) == "full"

        run_digest = AC.confirmation_digest("remote_run_command", "alpha",
                                            {"cmd": "kit:update", "args": [],
                                             "resume": False})
        run_confirm = _request_confirmation(srv_ck, call, token, "remote_run_command", "alpha",
                                            {"cmd": "kit:update", "args": [], "resume": False})
        with patch.object(srv_ck, "command_by_name",
                          lambda cmd: {"cmd": cmd, "runnable": True, "ns": "kit",
                                       "flags": [], "fixed_flags": [], "script": "x.py"}), \
                patch.object(srv_ck, "start_job",
                             lambda project, cmd, args, **kw: started.append((project, cmd)) or "j"), \
                patch.object(srv_ck, "version_gap", lambda *_a, **_k: ""), \
                patch.object(srv_ck, "net_enabled", lambda: False), \
                patch.object(srv_ck, "all_scenarios",
                             lambda: [{"id": "danger", "title": "Снести", "write": True,
                                       "destructive": True, "steps": []}]), \
                patch.object(srv_ck, "route_start",
                             lambda *a, **k: routes_started.append(a[0]) or {"route": "r"}):
            status, _h, raw = call("POST", "/api/run", _headers(token), json.dumps({
                "project": str(alpha), "cmd": "kit:update", "args": [],
                "resume": False, "confirm_id": run_confirm}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)
            assert started == [], started

            route_confirm = _request_confirmation(
                srv_ck, call, token, "remote_run_route", "alpha",
                {"scId": "danger", "write": True, "net": "", "resume": False})
            status, _h, raw = call("POST", "/api/route/run", _headers(token), json.dumps({
                "project": str(alpha), "scId": "danger", "write": True, "net": "",
                "resume": False, "confirm_id": route_confirm}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)
            assert routes_started == [], routes_started

            cron_digest = AC.confirmation_digest("remote_cron", "alpha",
                                                 {"action": "save", "task": {"name": "x"}})
            status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                                   json.dumps({"tool": "remote_cron", "project": "alpha",
                                               "args_digest": cron_digest,
                                               "summary": "расписание"}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)

            status, _h, raw = call("POST", "/api/cron/save", _headers(token), json.dumps({
                "project": str(alpha), "action": "save", "task": {"name": "x"}}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)

            # Перезапуск панели не привязан к проекту: им правит общий режим (full).
            restart_confirm = _request_confirmation(srv_ck, call, token, "remote_restart", "", {})
            with patch.object(srv_ck, "restart_self", lambda *_a, **_k: {"error": "test"}):
                status, _h, raw = call("POST", "/api/restart", _headers(token),
                                       json.dumps({"confirm_id": restart_confirm}))
            assert status == 200 and "test" in raw, (status, raw)

        # Общий safe при full проекта: перезапуск закрыт, запуск в проекте разрешён.
        _write_mode(kit, "safe", {"alpha": "full"})
        assert G.effective_mode("", kit=str(kit)) == "safe"
        assert G.effective_mode(str(alpha), kit=str(kit)) == "full"
        restart_digest = AC.confirmation_digest("remote_restart", "", {})
        status, _h, raw = call("POST", "/api/mcp/confirm/request", _headers(token),
                               json.dumps({"tool": "remote_restart", "project": "",
                                           "args_digest": restart_digest,
                                           "summary": "перезапуск"}))
        assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)

        run_confirm = _request_confirmation(srv_ck, call, token, "remote_run_command", "alpha",
                                            {"cmd": "kit:update", "args": [], "resume": False})
        with patch.object(srv_ck, "command_by_name",
                          lambda cmd: {"cmd": cmd, "runnable": True, "ns": "kit",
                                       "flags": [], "fixed_flags": [], "script": "x.py"}), \
                patch.object(srv_ck, "start_job",
                             lambda project, cmd, args, **kw: started.append((project, cmd)) or "j"), \
                patch.object(srv_ck, "version_gap", lambda *_a, **_k: ""), \
                patch.object(srv_ck, "net_enabled", lambda: False):
            status, _h, raw = call("POST", "/api/run", _headers(token), json.dumps({
                "project": str(alpha), "cmd": "kit:update", "args": [],
                "resume": False, "confirm_id": run_confirm}))
            assert status == 200 and started == [(str(alpha), "kit:update")], (status, raw, started)
    finally:
        close()
        _restore_env(old_env)
        restore_home()


# --------------------------------------------------------- B4: ship:release
@test
def test_security_fix_ship_release_requires_admin_and_confirmation(tmp: Path):
    """`/api/run` не начинает `ship:release` без `admin` и подтверждения человека."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    started = []
    args = ["Artifacts/ac/x.md", "--export", "docx"]
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    row = {"cmd": "ship:release", "runnable": True, "ns": "ship",
           "flags": [], "fixed_flags": [], "script": "ship_doc.py"}
    try:
        with patch.object(srv_ck, "command_by_name", lambda cmd: row):
            token = _token(kit, scopes=["remote:read", "remote:write"],
                           projects=["alpha"])
            status, _h, raw = call("POST", "/api/run", _headers(token), json.dumps({
                "project": str(alpha), "cmd": "ship:release", "args": args,
                "resume": False, "confirm_id": ""}))
            assert status == 403 and _body(raw).get("code") == "scope", (status, raw)

            token = _token(kit, scopes=["remote:read", "remote:write", "admin"],
                           projects=["alpha"])
            status, _h, raw = call("POST", "/api/run", _headers(token), json.dumps({
                "project": str(alpha), "cmd": "ship:release", "args": args,
                "resume": False, "confirm_id": ""}))
            assert status == 409 and _body(raw).get("code") == "confirmation_required", (status, raw)
            assert started == []

            confirm = _request_confirmation(
                srv_ck, call, token, "remote_run_command", "alpha",
                {"cmd": "ship:release", "args": args, "resume": False})
        with patch.object(srv_ck, "command_by_name", lambda cmd: row), \
                patch.object(srv_ck, "start_job",
                             lambda project, cmd, cmd_args, **kw:
                             started.append((project, cmd, cmd_args)) or "job-1"), \
                patch.object(srv_ck, "version_gap", lambda *_a, **_k: ""), \
                patch.object(srv_ck, "net_enabled", lambda: False):
            status, _h, raw = call("POST", "/api/run", _headers(token), json.dumps({
                "project": str(alpha), "cmd": "ship:release", "args": args,
                "resume": False, "confirm_id": confirm}))
            assert status == 200, (status, raw)
            assert started == [(str(alpha), "ship:release", args)], started
    finally:
        close()
        _restore_env(old_env)
        restore_home()


@test
def test_security_fix_ship_release_safe_mode_blocks_admin(tmp: Path):
    """`safe` сильнее `admin`: `ship:release` не начинается и с полными правами."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    _write_mode(kit, "safe")
    old_env = _tokens_env(kit)
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    started = []
    try:
        token = _token(kit, scopes=["remote:read", "remote:write", "admin"],
                       projects=["alpha"])
        with patch.object(srv_ck, "command_by_name",
                          lambda cmd: {"cmd": cmd, "runnable": True, "ns": "ship",
                                       "flags": [], "fixed_flags": [], "script": "ship_doc.py"}), \
                patch.object(srv_ck, "start_job",
                             lambda *a, **k: started.append(a) or "job-1"):
            status, _h, raw = call("POST", "/api/run", _headers(token), json.dumps({
                "project": str(alpha), "cmd": "ship:release",
                "args": ["Artifacts/ac/x.md", "--export", "docx"],
                "resume": False, "confirm_id": ""}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)
            assert started == [], started
    finally:
        close()
        _restore_env(old_env)
        restore_home()


# -------------------------------------------------------------- P1-2: Cron
@test
def test_security_fix_cron_acl_filters_and_blocks_foreign_tasks(tmp: Path):
    """Расписание: чужое задание не видно, не перезаписывается и не запускается."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    other = _project(root, "other")
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    token = _token(kit, projects=["alpha"])
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    alpha_id, other_id, global_id = "aaaa1111", "bbbb2222", "cccc3333"
    task_alpha = _cron_task(alpha_id, alpha)
    task_other = _cron_task(other_id, other)
    task_global = {"id": global_id, "name": "все проекты", "enabled": True, "net": "",
                   "time": "10:00", "days": [1], "date": "", "order": "projects",
                   "on_fail": "next", "lang": "ru",
                   "steps": [{"kind": "command", "project": "*", "cmd": "kb:links",
                              "args": []}]}
    CRON.save_tasks([task_alpha, task_other, task_global])
    old_sched = srv_ck.SCHED
    srv_ck.SCHED = None
    scheck = srv_ck.scheduler()
    try:
        status, _h, raw = call("GET", "/api/cron", _headers(token))
        assert status == 200, (status, raw)
        got = _body(raw)
        assert [row["id"] for row in got.get("tasks") or []] == [alpha_id], got
        status, _h, raw = call("GET", "/api/cron", _headers("", srv_ck.TOKEN))
        assert status == 200, (status, raw)
        assert {row["id"] for row in _body(raw).get("tasks") or []} == {
            alpha_id, other_id, global_id}, raw

        scheck.current = {"id": "run-x", "task": other_id, "status": "running",
                          "started": "2026-01-01T00:00:00Z",
                          "items": [{"project": str(other), "status": "running"}]}
        scheck.queue = [(alpha_id, "manual", ""), (other_id, "manual", "")]
        status, _h, raw = call("GET", "/api/cron", _headers(token))
        got = _body(raw)
        assert status == 200 and got.get("current") is None, (status, raw)
        assert got.get("queue") == [alpha_id], got
        scheck.current = None
        scheck.queue = []

        # Видимый id задания не разрешает прогон: пункты ушли в чужой проект.
        for rid, items in (
                ("r-mixed", [{"project": str(other), "status": "passed"}]),
                ("r-empty", []),
                ("r-alpha", [{"project": str(alpha), "status": "passed"}])):
            CRON.save_run({"id": rid, "task": alpha_id, "name": "run", "trigger": "manual",
                           "status": "passed", "started": "2026-01-01T00:00:00Z",
                           "finished": "2026-01-01T00:00:01Z", "items": items})
        CRON.save_run({"id": "r-legacy", "task": alpha_id, "project": str(alpha),
                       "name": "run", "trigger": "manual", "status": "passed",
                       "started": "2026-01-01T00:00:00Z",
                       "finished": "2026-01-01T00:00:01Z", "items": []})
        scheck.current = {"id": "run-mixed", "task": alpha_id, "status": "running",
                          "items": [{"project": str(other), "status": "running"}]}
        status, _h, raw = call("GET", "/api/cron", _headers(token))
        got = _body(raw)
        assert status == 200 and got.get("current") is None, (status, raw)
        assert [row["id"] for row in got.get("runs") or []] == ["r-alpha"], got
        scheck.current = None

        def save_body(task, confirm_id=""):
            body = {"project": str(alpha), "action": "save", "task": task}
            if confirm_id:
                body["confirm_id"] = confirm_id
            return json.dumps(body)

        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "alpha",
                                        {"action": "save", "task": task_other})
        status, _h, raw = call("POST", "/api/cron/save", _headers(token),
                               save_body(task_other, confirm))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        assert [row["id"] for row in CRON.load_tasks()] == [alpha_id, other_id, global_id], \
            CRON.load_tasks()

        rewrite = {**task_alpha, "id": other_id}
        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "alpha",
                                        {"action": "save", "task": rewrite})
        status, _h, raw = call("POST", "/api/cron/save", _headers(token),
                               save_body(rewrite, confirm))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        saved_other = next(row for row in CRON.load_tasks() if row["id"] == other_id)
        assert saved_other["steps"][0]["project"] == str(other), saved_other

        new_task = _cron_task("dddd4444", alpha)
        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "alpha",
                                        {"action": "save", "task": new_task})
        with patch.object(srv_ck, "command_by_name",
                          lambda cmd: {"cmd": cmd, "runnable": True, "ns": "kb",
                                       "flags": [], "fixed_flags": [],
                                       "script": "kb_links.py"}):
            status, _h, raw = call("POST", "/api/cron/save", _headers(token),
                                   save_body(new_task, confirm))
        assert status == 200 and _body(raw).get("ok") is True, (status, raw)
        assert new_task["id"] in {row["id"] for row in CRON.load_tasks()}

        for action in ("toggle", "start"):
            status, _h, raw = call("POST", "/api/cron/" + action, _headers(token),
                                   json.dumps({"project": str(alpha), "id": other_id,
                                               "enabled": False}))
            assert status == 403 and _body(raw).get("code") == "project", (action, status, raw)

        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "alpha",
                                        {"action": "delete", "id": other_id})
        status, _h, raw = call("POST", "/api/cron/delete", _headers(token), json.dumps({
            "project": str(alpha), "action": "delete", "id": other_id, "confirm_id": confirm}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        assert other_id in {row["id"] for row in CRON.load_tasks()}

        scheck.current = {"id": "run-y", "task": other_id, "status": "running",
                          "items": [{"project": str(other), "status": "running"}]}
        status, _h, raw = call("POST", "/api/cron/stop", _headers(token),
                               json.dumps({"project": str(alpha)}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        scheck.current = None

        # Тот же контур через remote_cron: оба проекта видны токену, но вызов один.
        both = _token(kit, projects=["alpha", "other"], name="Оба")
        rec = MT.verify(both, kit=str(kit))
        ctx = RM.AuthContext(kind="remote", token_id=rec["id"], user=rec["user"],
                             scopes=frozenset(rec["scopes"]),
                             projects=tuple(rec["projects"]), source="header")
        client = CC.CockpitClient(base_url="http://127.0.0.1:%d" % srv.server_address[1],
                                  token=both, kit=str(kit))
        server = RM.RemoteServer(client, ctx, kit=str(kit),
                                 audit_path=str(kit / "local/audit/test.jsonl"))
        got = server.call_tool("remote_cron", {"project": "alpha", "action": "save",
                                               "job": other_id, "spec": task_alpha})
        assert got.get("ok") is False and got["error"]["code"] == "project", got
        cross_steps = {**task_alpha, "steps": [{"kind": "command", "project": str(other),
                                               "cmd": "kb:links", "args": []}]}
        got = server.call_tool("remote_cron", {"project": "alpha", "action": "save",
                                               "spec": cross_steps})
        assert got.get("ok") is False and got["error"]["code"] == "project", got
        got = server.call_tool("remote_cron", {"project": "alpha", "action": "start",
                                               "job": other_id})
        assert got.get("ok") is False and got["error"]["code"] == "project", got
        scheck.current = {"id": "run-z", "task": other_id, "status": "running",
                          "items": [{"project": str(other), "status": "running"}]}
        got = server.call_tool("remote_cron", {"project": "alpha", "action": "stop"})
        assert got.get("ok") is False and got["error"]["code"] == "project", got
        scheck.current = None

        alpha_ctx = RM.AuthContext(kind="remote", token_id="alpha-only", user="u",
                                  scopes=frozenset(rec["scopes"]),
                                  projects=("alpha",), source="header")
        alpha_server = RM.RemoteServer(client, alpha_ctx, kit=str(kit),
                                      audit_path=str(kit / "local/audit/test.jsonl"))
        filtered = alpha_server._filtered_cron({
            "tasks": [task_alpha, task_other, task_global],
            "current": {"id": "run-x", "task": alpha_id,
                        "items": [{"project": str(other)}]},
            "queue": [alpha_id, other_id],
            "runs": [{"id": "r1", "task": alpha_id, "items": [{"project": str(other)}]},
                     {"id": "r2", "task": alpha_id, "items": [{"project": str(alpha)}]},
                     {"id": "r3", "task": alpha_id, "items": []},
                     {"id": "r4", "task": alpha_id, "project": str(alpha), "items": []}],
            "projects": [{"name": "alpha", "path": str(alpha)},
                         {"name": "other", "path": str(other)}],
            "bots": {str(alpha): [{"id": "bot-1"}], str(other): [{"id": "bot-2"}]},
        })
        assert [row["id"] for row in filtered["tasks"]] == [alpha_id], filtered
        assert filtered["current"] is None, filtered
        assert filtered["queue"] == [alpha_id], filtered
        assert [row["id"] for row in filtered["runs"]] == ["r2"], filtered
        assert list(filtered["bots"]) == [str(alpha)], filtered
    finally:
        srv_ck.SCHED = old_sched
        close()
        _restore_env(old_env)
        restore_home()


# ------------------------------------- P1-3: чужой проект не обходит safe в Cron
@test
def test_security_fix_cron_cross_project_safe_bypass_rejected(tmp: Path):
    """`project=other` не правит задачу `alpha` и не обходит `safe` её шагов."""
    _home_dir, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    other = _project(root, "other")
    _write_mode(kit, "full", {"alpha": "safe", "other": "full"})
    old_env = _tokens_env(kit)
    token = _token(kit, projects=["alpha", "other"])
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    alpha_id, other_id = "aaaa1111", "bbbb2222"
    task_alpha = _cron_task(alpha_id, alpha)
    task_other = _cron_task(other_id, other)
    CRON.save_tasks([task_alpha, task_other])
    old_sched = srv_ck.SCHED
    srv_ck.SCHED = None
    scheck = srv_ck.scheduler()
    try:
        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "other",
                                        {"action": "save", "task": task_alpha})
        status, _h, raw = call("POST", "/api/cron/save", _headers(token), json.dumps({
            "project": str(other), "action": "save", "task": task_alpha,
            "confirm_id": confirm}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        saved_alpha = next(row for row in CRON.load_tasks() if row["id"] == alpha_id)
        assert saved_alpha["steps"][0]["project"] == str(alpha), saved_alpha

        mixed = {"id": "dddd4444", "name": "mixed", "time": "10:00", "days": [1],
                 "steps": [{"kind": "command", "project": str(alpha),
                            "cmd": "kb:links", "args": []},
                           {"kind": "command", "project": str(other),
                            "cmd": "kb:links", "args": []}]}
        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "other",
                                        {"action": "save", "task": mixed})
        status, _h, raw = call("POST", "/api/cron/save", _headers(token), json.dumps({
            "project": str(other), "action": "save", "task": mixed,
            "confirm_id": confirm}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        assert "dddd4444" not in {row["id"] for row in CRON.load_tasks()}

        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "other",
                                        {"action": "delete", "id": alpha_id})
        status, _h, raw = call("POST", "/api/cron/delete", _headers(token), json.dumps({
            "project": str(other), "action": "delete", "id": alpha_id,
            "confirm_id": confirm}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        assert alpha_id in {row["id"] for row in CRON.load_tasks()}

        for action in ("toggle", "start"):
            status, _h, raw = call("POST", "/api/cron/" + action, _headers(token),
                                   json.dumps({"project": str(other), "id": alpha_id,
                                               "enabled": False}))
            assert status == 403 and _body(raw).get("code") == "project", \
                (action, status, raw)

        scheck.current = {"id": "run-alpha", "task": alpha_id, "status": "running",
                          "items": [{"project": str(alpha), "status": "running"}]}
        status, _h, raw = call("POST", "/api/cron/stop", _headers(token),
                               json.dumps({"project": str(other)}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        scheck.current = {"id": "run-bad", "task": alpha_id, "status": "running",
                          "items": {"project": str(alpha), "status": "running"}}
        status, _h, raw = call("POST", "/api/cron/stop", _headers(token),
                               json.dumps({"project": str(alpha)}))
        assert status == 403 and _body(raw).get("code") == "project", (status, raw)
        scheck.current = None

        # Второй слой: даже если guard пропустил, cron_acl сам видит safe шага.
        new_alpha = _cron_task("eeee5555", alpha)
        with patch.object(srv_ck, "_mcp_cron_guard", lambda ctx, payload, action="": None):
            status, _h, raw = call("POST", "/api/cron/save", _headers(token), json.dumps({
                "project": str(alpha), "action": "save", "task": new_alpha}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)
            status, _h, raw = call("POST", "/api/cron/delete", _headers(token), json.dumps({
                "project": str(alpha), "action": "delete", "id": alpha_id}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (status, raw)
        assert "eeee5555" not in {row["id"] for row in CRON.load_tasks()}
        assert alpha_id in {row["id"] for row in CRON.load_tasks()}
    finally:
        srv_ck.SCHED = old_sched
        close()
        _restore_env(old_env)
        restore_home()


# --------------------------------------------- P1: Cron не запускает ship:release
@test
def test_security_fix_cron_release_start_requires_admin_and_confirmation(tmp: Path):
    """`/api/cron/start|toggle` не запускают задание с `ship:release` без admin и подтверждения."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    tid = "deadbeef"
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    CRON.save_tasks([_cron_task(tid, alpha, command="ship:release")])
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    enqueued = []
    old_sched = srv_ck.SCHED
    srv_ck.SCHED = None
    scheck = srv_ck.scheduler()
    scheck.enqueue = lambda task_id, trigger="manual", resume="": \
        enqueued.append((task_id, trigger)) or {"ok": True}
    try:
        token = _token(kit, scopes=["remote:read", "remote:write"], projects=["alpha"])
        for action in ("start", "toggle"):
            status, _h, raw = call("POST", "/api/cron/" + action, _headers(token), json.dumps({
                "project": str(alpha), "id": tid, "enabled": False, "confirm_id": ""}))
            assert status == 403 and _body(raw).get("code") == "scope", (action, status, raw)

        token = _token(kit, scopes=["remote:read", "remote:write", "admin"],
                       projects=["alpha"])
        for action in ("start", "toggle"):
            status, _h, raw = call("POST", "/api/cron/" + action, _headers(token), json.dumps({
                "project": str(alpha), "id": tid, "enabled": False, "confirm_id": ""}))
            assert status == 409 and _body(raw).get("code") == "confirmation_required", \
                (action, status, raw)
        assert enqueued == [], enqueued
        assert next(t for t in CRON.load_tasks() if t["id"] == tid)["enabled"] is True

        confirm = _request_confirmation(srv_ck, call, token, "remote_cron", "alpha",
                                        {"action": "start", "id": tid})
        status, _h, raw = call("POST", "/api/cron/start", _headers(token), json.dumps({
            "project": str(alpha), "id": tid, "confirm_id": confirm}))
        assert status == 200 and _body(raw).get("ok") is True, (status, raw)
        assert enqueued == [(tid, "manual")], enqueued

        _write_mode(kit, "safe")
        for action in ("start", "toggle"):
            status, _h, raw = call("POST", "/api/cron/" + action, _headers(token), json.dumps({
                "project": str(alpha), "id": tid, "enabled": False, "confirm_id": ""}))
            assert status == 403 and _body(raw).get("code") == "safe_mode", (action, status, raw)
        assert enqueued == [(tid, "manual")], enqueued
    finally:
        srv_ck.SCHED = old_sched
        close()
        _restore_env(old_env)
        restore_home()


@test
def test_security_fix_cron_scheduler_skips_release_steps(tmp: Path):
    """`Scheduler._item` не доводит до `start_job` задание с `ship:release` и маршрут с ним."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    srv_ck, srv, close = _panel(tmp, kit, root)
    started = []
    old_sched = srv_ck.SCHED
    srv_ck.SCHED = None
    scheck = srv_ck.scheduler()
    try:
        task = _cron_task("feed1234", alpha, command="ship:release")
        item = {"kind": "command", "project": str(alpha), "project_name": "alpha",
                "cmd": "ship:release", "args": ["Artifacts/ac/x.md"],
                "status": "pending", "note": "", "started": "", "finished": "",
                "jobs": [], "done": [], "log": []}
        run = {"id": "run-cron-release", "items": [item], "beat": 0}
        with patch.object(srv_ck, "start_job",
                          lambda *a, **k: started.append(a) or "job-x"):
            scheck._item(task, run, item, "ru")
        assert item["status"] == "failed" and item["note"] == "bad_command", item
        assert started == [], started

        scenario = {"id": "release-route", "title": "Выпуск",
                    "steps": [{"cmd": "ship:release", "flags": ["--apply"]}]}
        route_item = {"kind": "route", "project": str(alpha), "project_name": "alpha",
                      "route": "release-route", "write": True,
                      "status": "pending", "note": "", "started": "", "finished": "",
                      "jobs": [], "done": [], "log": []}
        route_run = {"id": "run-cron-route", "items": [route_item], "beat": 0}
        with patch.object(srv_ck, "all_scenarios", lambda: [scenario]), \
                patch.object(srv_ck, "start_job",
                             lambda *a, **k: started.append(a) or "job-x"):
            scheck._item(task, route_run, route_item, "ru")
        assert route_item["status"] == "failed" and route_item["note"] == "bad_route", \
            route_item
        assert started == [], started
    finally:
        srv_ck.SCHED = old_sched
        close()
        restore_home()


@test
def test_security_fix_cron_save_rejects_release_steps(tmp: Path):
    """`clean_task` не принимает задание с `ship:release` и маршрут с таким шагом."""
    _home_dir, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    srv_ck, srv, close = _panel(tmp, kit, root)
    projects = [{"name": "alpha", "path": str(alpha)}]
    row = {"cmd": "ship:release", "runnable": True, "ns": "ship",
           "flags": [], "fixed_flags": [], "script": "ship_doc.py"}
    try:
        task = _cron_task("11112222", alpha, command="ship:release")
        with patch.object(srv_ck, "command_by_name", lambda cmd: row):
            got = srv_ck.cron_action("save", {"task": task}, projects)
        assert got == {"ok": False, "error": "bad_command"}, got
        assert "11112222" not in {t["id"] for t in CRON.load_tasks()}

        scenario = {"id": "release-route", "title": "Выпуск",
                    "steps": [{"cmd": "ship:release"}]}
        route_task = {"id": "33334444", "name": "маршрут", "time": "10:00",
                      "days": [1], "steps": [{"kind": "route", "project": str(alpha),
                                              "route": "release-route", "write": True}]}
        with patch.object(srv_ck, "all_scenarios", lambda: [scenario]):
            got = srv_ck.cron_action("save", {"task": route_task}, projects)
        assert got == {"ok": False, "error": "bad_route"}, got
        assert "33334444" not in {t["id"] for t in CRON.load_tasks()}
    finally:
        close()
        restore_home()


# -------------------------------------------------- P1-4: токен в тексте ошибки
@test
def test_security_fix_error_text_scrubs_bare_engine_tokens(tmp: Path):
    """`aur_rm_…` и `aur_kn_…` без имени поля не уходят в `/api/run/errors` и диагностику."""
    home, restore_home = _home(tmp)
    kit = _kit(tmp)
    root = tmp / "root"
    alpha = _project(root, "alpha")
    _write_mode(kit, "full")
    old_env = _tokens_env(kit)
    token = _token(kit, scopes=["remote:read"], projects=["alpha"])
    run = alpha / ".aurora" / "runs" / "20260101-000000"
    run.mkdir(parents=True, exist_ok=True)
    remote_secret = "aur_rm_abc123_SUPERSECRET"
    knowledge_secret = "aur_kn_def456_KNOWNSECRET"
    (run / "meta.json").write_text(json.dumps({
        "id": "20260101-000000", "kind": "command", "title": "kb:links",
        "started": "2026-01-01T00:00:00Z", "finished": "2026-01-01T00:00:01Z",
        "status": "failed", "rc": 1, "seconds": 1,
    }), encoding="utf-8")
    (run / "failures.jsonl").write_text("\n".join([
        json.dumps({"at": "1", "kind": "stage", "error": "request failed: " + remote_secret}),
        json.dumps({"at": "2", "stage": "adapter", "message": "key " + knowledge_secret}),
    ]) + "\n", encoding="utf-8")
    srv_ck, srv, close = _panel(tmp, kit, root)
    call = _call(srv)
    try:
        direct = srv_ck.read_run_errors(str(alpha), "20260101-000000")
        assert direct["count"] == 2, direct
        text = json.dumps(direct, ensure_ascii=False)
        assert remote_secret not in text and knowledge_secret not in text, text
        assert "[redacted]" in text, text

        status, _h, raw = call("GET", "/api/run/errors?project=" + _p(alpha)
                               + "&run=20260101-000000", _headers(token))
        assert status == 200, (status, raw)
        assert remote_secret not in raw and knowledge_secret not in raw, raw
        assert "[redacted]" in raw, raw

        for value in ("failed with " + remote_secret,
                      "token " + knowledge_secret + " rejected",
                      {"error": remote_secret}):
            cleaned = D._text(value, project=str(alpha))
            assert remote_secret not in cleaned and knowledge_secret not in cleaned, cleaned
        cleaned = D._clean({"error": "boom " + knowledge_secret}, str(alpha))
        assert knowledge_secret not in json.dumps(cleaned, ensure_ascii=False)
    finally:
        close()
        _restore_env(old_env)
        restore_home()
