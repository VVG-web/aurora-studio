"""Проверки движка Aurora, часть 112. Каркас и помощники — tests/harness.py.

Один маршрут на проект (1.188.1). 20.10.2026 расписание начало «Пересобрать базу с нуля» в проекте, где кнопкой шло
«Обновить базу»: между шагами маршрута проект «свободен» (нет ни команды, ни замка агента), и снос базы пришёлся на
хвост идущего маршрута. Теперь маршрут держит замок `route.lock` на всю длину, расписание считает идущий маршрут
занятостью, кнопка видит чужой маршрут по замку, а опасный маршрут расписание после сбоя с начала не повторяет.
Проекты и замки — только во временных папках.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _ck():
    _path()
    import aurora_cockpit as ck
    return ck


def _rr():
    _path()
    import route_runner as RR
    return RR


def _empty_route(ck, project, rid="t"):
    RR = _rr()
    sc = {"id": rid, "title": "Проверочный", "when": "", "group": "база", "steps": []}
    return RR.RouteRun(ck, str(project), sc, False)


def _dead_pid() -> int:
    """Номер процесса, которого уже нет: замок с ним — след упавшей панели."""
    cp = subprocess.Popen([sys.executable, "-c", "pass"])
    cp.wait()
    return cp.pid


def _write_lock(project: Path, **rec) -> Path:
    RR = _rr()
    path = Path(RR.route_lock_path(str(project)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return path


@test
def test_a_route_holds_the_project_lock_and_a_second_route_is_refused(tmp: Path):
    """Пока первый маршрут держит замок, второй в том же проекте не начинается: отказ с названием держателя,
    ни одного шага, а замок первого остаётся нетронутым (оба маршрута одной панели делят pid)."""
    ck = _ck()
    root = make_project(tmp)
    first = _empty_route(ck, root, "update")
    assert first._lock() == {} and first.locked
    held = _rr().route_holder(str(root))
    assert held["run"] == first.run_id and held["route"] == "update" and held["pid"] == os.getpid(), held
    second = _empty_route(ck, root, "fix")
    res = second.safe_run()
    assert res["ok"] is False and res["reason"] == "failed" and second.no_run, res
    assert "уже идёт маршрут" in res["note"] and "Проверочный" in res["note"], res["note"]
    assert not second.locked and second.done_flag
    # второй отказ не снял замок первого, хотя pid у них один
    assert _rr().route_holder(str(root)).get("run") == first.run_id
    first._unlock()
    assert not _rr().route_holder(str(root)) and not Path(_rr().route_lock_path(str(root))).exists()


@test
def test_a_finished_route_releases_the_lock_and_a_dead_holder_does_not_block(tmp: Path):
    """Прошедший маршрут замок снимает; замок упавшей панели (процесса уже нет) маршрут забирает и идёт."""
    ck = _ck()
    root = make_project(tmp)
    run = _empty_route(ck, root)
    res = run.safe_run()
    assert res["reason"] != "failed" or "уже идёт" not in res.get("note", ""), res
    assert run.done_flag and not Path(_rr().route_lock_path(str(root))).exists(), "замок не снят в конце маршрута"
    _write_lock(root, pid=_dead_pid(), run="old", route="update", title="Старый", since="2026-10-20T00:00:00Z")
    assert not _rr().route_holder(str(root)), "мёртвый держатель считается живым"
    again = _empty_route(ck, root)
    again.safe_run()
    assert "уже идёт" not in again.result.get("note", ""), again.result
    assert not Path(_rr().route_lock_path(str(root))).exists()


@test
def test_activity_sees_routes_of_this_panel_and_foreign_ones_by_the_lock(tmp: Path):
    """`project_activity` называет идущие маршруты: из реестра этой панели и чужие — по замку на диске (терминал,
    другая панель). Это то, что видят расписание и кнопка; пока маршрута нет — список пуст."""
    ck = _ck()
    root = make_project(tmp)
    assert ck.project_activity(str(root))["live_routes"] == []

    class _Run:                       # заглушка идущего маршрута в реестре этой панели
        sc = {"id": "update", "title": "Обновить базу"}
        write = True
        done_flag = False
    with ck.ROUTES_LOCK:
        ck.ROUTES["t-route"] = {"id": "t-route", "project": str(root), "run": _Run(), "started": 0}
    try:
        live = ck.project_activity(str(root))["live_routes"]
        assert [(r["scId"], r["title"]) for r in live] == [("update", "Обновить базу")], live
    finally:
        with ck.ROUTES_LOCK:
            ck.ROUTES.pop("t-route", None)
    _write_lock(root, pid=os.getppid(), run="r1", route="fix", title="Починить базу", since="2026-10-29T08:00:00Z")
    live = ck.project_activity(str(root))["live_routes"]
    assert len(live) == 1 and live[0]["foreign"] and live[0]["scId"] == "fix", live


@test
def test_the_scheduler_treats_a_running_route_as_busy(tmp: Path):
    """Идущий маршрут — занятость проекта: расписание ждёт, а не начинает второй маршрут в щели между шагами."""
    _path()
    import cron

    class _Ck:
        DEFAULT_LANG = "ru"
        act = {}

        def mark_running(self, *a):
            pass

        def project_activity(self, project):
            return self.act
    ck = _Ck()
    sched = cron.Scheduler(ck, lambda: [])
    assert sched._busy("/p/A") is False
    ck.act = {"running": [], "agent": None, "route": None, "live_routes": [{"scId": "update", "title": "Обновить базу"}]}
    assert sched._busy("/p/A") is True
    line = cron.busy_line(ck.act)
    assert "идёт маршрут" in line and "Обновить базу" in line, line
    ck.act = {"running": [], "agent": {"alive": False}, "route": {"title": "остановлен"}, "live_routes": []}
    assert sched._busy("/p/A") is False, "остановленный маршрут (ждёт «Продолжить») не занимает проект"


@test
def test_the_button_refuses_a_route_started_outside_this_panel(tmp: Path):
    """Кнопка видит маршрут из терминала или другой панели по замку и не начинает второй; ничего не запускается."""
    ck = _ck()
    root = make_project(tmp)
    _write_lock(root, pid=os.getppid(), run="r1", route="update", title="Обновить базу", since="2026-10-29T08:00:00Z")
    before = len(ck.ROUTES)
    res = ck.route_start(str(root), "update", False)
    assert res.get("foreign") and "вне этой панели" in res["error"] and "Обновить базу" in res["error"], res
    assert len(ck.ROUTES) == before, "маршрут всё равно зарегистрирован"


@test
def test_only_the_rebuild_route_is_marked_destructive(tmp: Path):
    """В `scenarios.txt` пометку «опасный» несёт только «Пересобрать базу с нуля»; остальные маршруты — обычные."""
    ck = _ck()
    flags = {s["id"]: s["destructive"] for s in ck.all_scenarios()}
    assert flags.get("rebuild") is True, flags
    assert [k for k, v in flags.items() if v] == ["rebuild"], flags
    head = [ln for ln in (COCKPIT / "scenarios.txt").read_text(encoding="utf-8").splitlines() if ln.startswith("[rebuild]")][0]
    assert head.rstrip().endswith("| опасный"), head[-60:]


class _CronCk:
    DEFAULT_LANG = "ru"

    def mark_running(self, *a):
        pass

    def project_activity(self, project):
        return {}

    def all_scenarios(self):
        return [{"id": "rebuild", "title": "Пересобрать", "destructive": True, "steps": []},
                {"id": "update", "title": "Обновить", "destructive": False, "steps": []},
                {"id": "fix", "title": "Починить", "destructive": False, "steps": []}]


@test
def test_the_chain_never_reruns_a_destructive_route_from_the_start(tmp: Path):
    """Упавшая «Пересобрать» в конце цепочки не запускается заново (там первый шаг — снос); остальные пункты проекта
    повторяются, как прежде. Итог пересборки остаётся тем, чем он был, — без пометки повтора."""
    _path()
    import cron
    restore = set_home(tmp / "home")
    try:
        projects = [{"path": "/p/A", "name": "A"}, {"path": "/p/B", "name": "B"}]
        task = {"id": "t1", "name": "Ночь", "order": "projects", "on_fail": "next", "lang": "ru",
                "steps": [{"kind": "route", "project": "/p/B", "route": "rebuild"},
                          {"kind": "route", "project": "/p/B", "route": "update"},
                          {"kind": "route", "project": "/p/A", "route": "update"}]}
        calls = []
        sched = cron.Scheduler(_CronCk(), lambda: projects)
        outcomes = {("B", "rebuild"): ["stall"], ("B", "update"): ["passed"], ("A", "update"): ["passed"]}

        def fake_item(task, run, it, lang):
            key = (it["project"][-1], it["route"])
            calls.append(key)
            it.update(status=outcomes[key][0], finished=cron.now_iso())
        sched._item = fake_item
        run = sched._new_run(task, "manual", "")
        sched._run(task, run)
        assert calls == [("B", "rebuild"), ("A", "update"), ("B", "update")], calls
        by = {(i["project"][-1], i["route"]): i for i in run["items"]}
        assert by[("B", "rebuild")]["status"] == "stall" and not by[("B", "rebuild")].get("retry"), by[("B", "rebuild")]
        assert by[("B", "update")]["status"] == "passed" and by[("B", "update")]["retry"], by[("B", "update")]
        assert run["status"] == "failed", run["status"]
    finally:
        restore()
