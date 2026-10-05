"""Проверки движка Aurora, часть 18: маршрут без браузера и расписание панели («Cron»).

Каркас и помощники — tests/harness.py. Маршрут, который ведёт сервер панели
(`cockpit/route_runner.py`), обязан идти по тем же правилам, что кнопка «Пройти» в
странице; расписание (`cockpit/cron.py`) — запускать цепочки вовремя, по одной, и честно
записывать, что прошло, что нет и что пропущено.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
import re
import sys
import threading

from harness import KIT, SCRIPTS, set_home, test, why  # noqa: F401

COCKPIT = KIT / "cockpit"


def _modules():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    rr = importlib.import_module("route_runner")
    cron = importlib.import_module("cron")
    return rr, cron


class FakePanel:
    """Панель без процессов: задания отвечают заранее записанным выводом.

    `script` — {команда: [ответ, …]}, ответ — (код, [строки]); ответы берутся по очереди,
    последний повторяется. Коммиты и состояние маршрута записываются, а не делаются.
    """

    def __init__(self, tmp: Path, script: dict, routes: list, gap: str = ""):
        import run_summary
        self.RS = run_summary
        self.KIT = str(tmp / "kit")
        self.DEFAULT_LANG = "ru"
        self.JOBS, self.JOBS_LOCK = {}, threading.Lock()
        self.script, self.routes, self.gap = script, routes, gap
        self.calls, self.commits, self.route_state = [], [], None
        self.runs = tmp / "runs"
        self.busy = set()
        self.marks = {}
        self.parents = []
        strings = json.loads((COCKPIT / "i18n" / "ru.json").read_text(encoding="utf-8"))
        strings.update(json.loads((COCKPIT / "modules" / "cron" / "i18n" / "ru.json")
                                  .read_text(encoding="utf-8")))
        self.strings = strings

    def i18n_catalogue(self, lang):
        return {"strings": self.strings}

    def scenarios(self):
        return self.routes

    def command_by_name(self, name):
        if name.startswith("dev:"):
            return {"cmd": name, "ns": "dev", "runnable": True, "flags": []}
        return ({"cmd": name, "ns": name.split(":")[0], "runnable": True,
                 "flags": ["--apply", "--residue", "--cards"]} if name in self.script else None)

    def start_job(self, project, cmd, args, parent=""):
        self.calls.append((project, cmd, list(args)))
        self.parents.append(parent)
        answers = self.script[cmd]
        rc, lines = answers.pop(0) if len(answers) > 1 else answers[0]
        job_id = "j%d" % len(self.calls)
        # Код None — шаг «висит», пока его не прервут: так проверяется «Остановить».
        self.JOBS[job_id] = {"id": job_id, "out": list(lines), "done": rc is not None,
                             "rc": rc, "run_id": "", "project": project}
        return job_id

    def stop_job(self, job_id):
        job = self.JOBS[job_id]
        job.update(done=True, rc=-15)
        return {"ok": True}

    def runs_dir(self, project):
        return str(self.runs)

    def who(self, project):
        return "Тестировщик"

    def kit_version(self):
        return "9.9.9"

    def version_gap(self, project):
        return self.gap

    def git_commit(self, project, message, paths=None, skip_ratchet=False):
        self.commits.append(message)
        return {"ok": True, "commit": "c%d" % len(self.commits)}

    def write_route_state(self, project, state):
        self.route_state = state
        return {"ok": True}

    def clear_route_state(self, project):
        self.route_state = "cleared"
        return {"ok": True}

    def project_activity(self, project):
        return {"running": [1] if project in self.busy else [], "agent": None}

    def mark_running(self, job_id, name, project, on):
        if on:
            self.marks[job_id] = name
        else:
            self.marks.pop(job_id, None)


def _route(sid="fix", title="Починить базу", cycle=False):
    steps = [{"manual": False, "cmd": "kb:repair", "why": "ремонт", "flags": ["--apply"]}]
    if cycle:
        steps += [{"manual": False, "cycle": "цикл:", "why": ""},
                  {"manual": False, "cmd": "agent:build", "why": "разбор", "flags": ["--apply"]},
                  {"manual": False, "cycle": "конец цикла", "why": ""}]
    steps += [{"manual": True, "title": "человек", "why": ""},
              {"manual": False, "cmd": "kb:lint", "why": "остаток", "flags": ["--residue"]}]
    return {"id": sid, "title": title, "when": "", "group": "база", "steps": steps}


@test
def test_the_server_route_follows_the_button_rules(tmp: Path):
    """Маршрут на сервере: шаги по порядку, цикл оборотами с коммитом, хвост в git, итог.

    Кнопка «Пройти» ведёт маршрут из открытой страницы; расписание идёт без неё. Правила
    обязаны совпадать: иначе ночной прогон — другой процесс под тем же названием.
    """
    rr, _ = _modules()
    ck = FakePanel(tmp, {
        "kb:repair": [(0, ["починено"])],
        "agent:build": [(0, ["Источников в плане: 5 → 2"]), (0, ["Источников в плане: 2 → 0"])],
        "kb:lint": [(1, ["## битые ссылки: 3"])],
    }, [_route(cycle=True)])
    logs = []
    res = rr.run_route(ck, str(tmp), "fix", True, log=logs.append)
    assert res["ok"] and res["reason"] == "passed", res
    assert [c[1] for c in ck.calls] == ["kb:repair", "agent:build", "agent:build", "kb:lint"], \
        ck.calls
    assert ck.calls[0][2] == ["--apply"], "флаг --apply маршрута потерялся"
    assert any("оборот 1" in m for m in ck.commits), ck.commits
    assert any("работа кончилась за 2 оборота" in m for m in ck.commits), ck.commits
    assert ck.commits[-1].endswith("пройден"), "хвост маршрута не зафиксирован: " + str(ck.commits)
    assert ck.route_state == "cleared", "пройденный маршрут оставил «Продолжить»"
    events = (ck.runs / res["run_id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 4, events
    assert any("Итог прогона" in line for line in logs), logs[-5:]

    # «Посмотреть» — те же шаги без записи
    ck2 = FakePanel(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]}, [_route()])
    rr.run_route(ck2, str(tmp), "fix", False)
    assert all("--apply" not in c[2] for c in ck2.calls), ck2.calls


@test
def test_the_server_route_stops_where_the_button_stops(tmp: Path):
    """Отказ шага, застой цикла и отставший движок останавливают маршрут, и «Продолжить»
    после этого знает, что уже сделано."""
    rr, _ = _modules()
    ck = FakePanel(tmp, {"kb:repair": [(2, ["упало"])], "kb:lint": [(0, [])]}, [_route()])
    res = rr.run_route(ck, str(tmp), "fix", True)
    assert res["reason"] == "failed" and res["failed"] == "kb:repair", res
    assert [c[1] for c in ck.calls] == ["kb:repair"], "после отказа маршрут пошёл дальше"
    assert ck.route_state["reason"] == "failed" and ck.route_state["scId"] == "fix", ck.route_state

    ck = FakePanel(tmp, {"kb:repair": [(0, [])],
                         "agent:build": [(0, ["Источников в плане: 9 → 4"])],
                         "kb:lint": [(0, [])]}, [_route(cycle=True)])
    res = rr.run_route(ck, str(tmp), "fix", True)
    assert res["reason"] == "stall", res
    assert "kb:lint" not in [c[1] for c in ck.calls], "после застоя маршрут пошёл дальше"
    assert set(ck.route_state["done"]) == {"kb:repair --apply", "agent:build --apply"}, \
        ck.route_state

    ck = FakePanel(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]}, [_route()],
                   gap="движок проекта 1.1.0")
    res = rr.run_route(ck, str(tmp), "fix", True)
    assert res["reason"] == "failed" and not ck.calls, "маршрут пошёл по отставшему движку"

    # продолжение пропускает сделанное, как «Продолжить маршрут»
    ck = FakePanel(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]}, [_route()])
    rr.run_route(ck, str(tmp), "fix", True, resume={"skipSigs": ["kb:repair --apply"]})
    assert [c[1] for c in ck.calls] == ["kb:lint"], ck.calls


@test
def test_the_server_route_waits_for_the_network(tmp: Path):
    """Шаг, упавший по сети, повторяется после паузы, а не валит ночной маршрут."""
    rr, _ = _modules()
    saved = rr.OFFLINE_RETRY_S, rr.FLAKY_RETRY_S
    rr.OFFLINE_RETRY_S = rr.FLAKY_RETRY_S = 0
    try:
        ck = FakePanel(tmp, {"kb:repair": [(1, ["Connection refused"]), (0, ["ok"])],
                             "kb:lint": [(0, [])]}, [_route()])
        res = rr.run_route(ck, str(tmp), "fix", True)
        assert res["ok"], res
        assert [c[1] for c in ck.calls] == ["kb:repair", "kb:repair", "kb:lint"], ck.calls
        ck = FakePanel(tmp, {"kb:repair": [(1, ["timed out"])], "kb:lint": [(0, [])]},
                       [_route()])
        res = rr.run_route(ck, str(tmp), "fix", True)
        assert res["reason"] == "offline", res
        assert ck.route_state["reason"] == "offline", "панель не предложит продолжить после сети"
    finally:
        rr.OFFLINE_RETRY_S, rr.FLAKY_RETRY_S = saved


@test
def test_the_page_leaves_the_route_to_the_server(_t):
    """Маршрут ведёт один исполнитель — сервер; страница его запускает и смотрит.

    До 1.152.0 правила маршрута жили в двух местах: в странице (кнопка «Пройти») и на сервере
    (расписание). Пороги сверял тест, но две реализации одного правила всё равно
    расходятся — и закрытая вкладка останавливала маршрут кнопки. Теперь в странице нет ни
    оборотов, ни коммитов, ни ожидания сети: только запуск, журнал и кнопки.
    """
    rr, _ = _modules()
    ui = (COCKPIT / "ui" / "panel.js").read_text(encoding="utf-8")
    run = ui[ui.index("async function runRoute("):ui.index("function finishRoute(")]
    assert '"/api/route/run"' in run and "/api/route/live?id=" in run, \
        "кнопка «Пройти» ведёт маршрут не через сервер"
    for gone in ("CYCLE_LIMIT", "OFFLINE_SIGNS", "DID_WORK", "leftByKind"):
        assert gone not in ui, f"в странице снова своя логика маршрута: {gone}"
    # Ручная фиксация в странице есть, но маршрут свои обороты страницей не фиксирует.
    assert "/api/git/commit" not in run, "маршрут страницы снова коммитит обороты сам"
    from agent_runner import OFFLINE_SIGNS
    assert rr.offline_signs() == OFFLINE_SIGNS, "признаки офлайна маршрута — не движковые"


def _task(**kw):
    base = {"id": "a1b2c3d4", "name": "Ночь", "enabled": True, "time": "20:00", "days": [],
            "date": "", "order": "projects", "on_fail": "next", "lang": "ru",
            "steps": [{"kind": "route", "project": "*", "route": "fix", "write": True}]}
    base.update(kw)
    return base


@test
def test_a_schedule_task_is_checked_before_it_is_saved(tmp: Path):
    """Ночью исправлять опечатку некому: задание проверяется при сохранении."""
    _, cron = _modules()
    ck = FakePanel(tmp, {"kb:lint": [(0, [])]}, [_route()])
    projects = [{"name": "A", "path": "/p/a"}]
    task, err = cron.clean_task(ck, _task(id=""), projects)
    assert not err and re.match(r"^[0-9a-f]{8}$", task["id"]), (task, err)
    for bad, code in ((_task(name=" "), "no_name"), (_task(time="25:00"), "bad_time"),
                      (_task(steps=[]), "no_steps"),
                      (_task(steps=[{"kind": "route", "project": "*", "route": "nope"}]),
                       "bad_route"),
                      (_task(steps=[{"kind": "route", "project": "/p/x", "route": "fix"}]),
                       "bad_project"),
                      (_task(steps=[{"kind": "command", "project": "*", "cmd": "kb:nope"}]),
                       "bad_command"),
                      (_task(steps=[{"kind": "command", "project": "*", "cmd": "kb:lint",
                                     "args": ["--rm-rf"]}]), "bad_flag")):
        assert cron.clean_task(ck, bad, projects)[1] == code, (bad, code)
    task, _ = cron.clean_task(ck, _task(days=[5, 1, 9, 1], order="steps", on_fail="stop"),
                              projects)
    assert task["days"] == [1, 5] and task["order"] == "steps" and task["on_fail"] == "stop"


@test
def test_the_schedule_knows_its_days(tmp: Path):
    """День недели, разовая дата, «следующий запуск» и запуск, заведённый после своего времени."""
    _, cron = _modules()
    restore = set_home(tmp / "home")
    try:
        mon = datetime(2026, 10, 5, 21, 0)          # понедельник, после 20:00
        assert cron.slot(_task(days=[1]), mon) == mon.replace(hour=20, minute=0)
        assert cron.slot(_task(days=[2]), mon) is None
        assert cron.next_run(_task(days=[1]), mon) == "2026-10-12T20:00:00"
        assert cron.next_run(_task(), mon) == "2026-10-06T20:00:00"
        assert cron.next_run(_task(date="2026-10-05"), mon) == "", "разовое прошлое — без запуска"
        assert cron.next_run(_task(enabled=False), mon) == ""
        cron.save_tasks([_task()])
        cron.mark_past(_task(), mon)
        sched = cron.Scheduler(FakePanel(tmp, {}, []), lambda: [])
        sched.tick(mon)
        assert not sched.queue and not cron.list_runs(), \
            "задание, заведённое после своего времени, записано пропущенным"
    finally:
        restore()


def _scheduler(tmp, script, projects, tasks, **fake):
    import shutil
    _, cron = _modules()
    # каждый случай — с чистой историей: прогоны одной секунды сортируются случайным хвостом
    shutil.rmtree(cron.runs_dir(), ignore_errors=True)
    Path(cron.state_file()).unlink(missing_ok=True)
    ck = FakePanel(tmp, script, [_route(), _route("update", "Обновить базу")], **fake)
    cron.save_tasks(tasks)
    sched = cron.Scheduler(ck, lambda: projects)
    return cron, ck, sched


def _wait(sched, seconds=20):
    import time
    end = time.time() + seconds
    while time.time() < end:
        with sched.lock:
            if not sched.current and not sched.queue:
                return
        time.sleep(0.05)
    raise AssertionError("цепочка не закончилась")


@test
def test_the_schedule_runs_a_chain_through_all_projects(tmp: Path):
    """«В 20:00 пройти маршруты во всех проектах»: вовремя, по проектам, с историей."""
    restore = set_home(tmp / "home")
    try:
        projects = [{"name": "Альфа", "path": str(tmp / "a")}, {"name": "Бета", "path": str(tmp / "b")}]
        steps = [{"kind": "route", "project": "*", "route": "update", "write": True},
                 {"kind": "route", "project": "*", "route": "fix", "write": True},
                 {"kind": "command", "project": str(tmp / "b"), "cmd": "kb:lint",
                  "args": ["--residue"]}]
        cron, ck, sched = _scheduler(tmp, {"kb:repair": [(0, [])], "kb:lint": [(1, ["## x: 1"])]},
                                     projects, [_task(steps=steps)])
        at = datetime(2026, 10, 5, 20, 3)
        sched.tick(at)
        _wait(sched)
        runs = cron.list_runs()
        assert len(runs) == 1 and runs[0]["trigger"] == "schedule", runs
        run = runs[0]
        order = [(Path(it["project"]).name, it.get("route") or it.get("cmd")) for it in run["items"]]
        assert order == [("a", "update"), ("a", "fix"), ("b", "update"), ("b", "fix"),
                         ("b", "kb:lint")], order
        assert all(it["status"] == "passed" for it in run["items"]), run["items"]
        assert run["status"] == "passed" and run["finished"], run
        assert run["items"][-1]["note"] == "found", "код 1 команды — это «нашла», а не провал"
        assert not ck.marks, "цепочка не сняла отметку «идёт работа»"
        sched.tick(at + timedelta(minutes=1))
        _wait(sched)
        assert len(cron.list_runs()) == 1, "одно и то же время отработало дважды"

        # по шагам: сначала первый шаг везде
        items = cron.expand(_task(steps=steps, order="steps"), projects)
        assert [(Path(i["project"]).name, i.get("route") or i.get("cmd")) for i in items] == \
            [("a", "update"), ("b", "update"), ("a", "fix"), ("b", "fix"), ("b", "kb:lint")]
        gone = cron.expand(_task(steps=[dict(steps[2], project="/нет/такого")]), projects)
        assert gone[0]["status"] == "skipped" and gone[0]["note"] == "no_project", gone
    finally:
        restore()


@test
def test_the_schedule_does_not_catch_up_a_whole_day_late(tmp: Path):
    """Панель подняли в десять утра — ночной разбор не начинается посреди рабочего дня:
    запуск пропущен, и это видно в истории."""
    restore = set_home(tmp / "home")
    try:
        cron, ck, sched = _scheduler(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]},
                                     [{"name": "A", "path": str(tmp / "a")}],
                                     [_task(time="06:00")])
        sched.tick(datetime(2026, 10, 5, 10, 0))
        _wait(sched)
        runs = cron.list_runs()
        assert [r["status"] for r in runs] == ["missed"], runs
        assert not ck.calls, "пропущенное время всё-таки запустило цепочку"
    finally:
        restore()


@test
def test_a_failed_step_stops_the_chain_only_when_asked(tmp: Path):
    """«Шаг не прошёл — цепочка встаёт»: остальные шаги пропущены с причиной; по умолчанию
    цепочка идёт дальше. Занятый проект ждёт, потом пропускается."""
    restore = set_home(tmp / "home")
    try:
        projects = [{"name": "A", "path": str(tmp / "a")}, {"name": "B", "path": str(tmp / "b")}]
        cron, ck, sched = _scheduler(tmp, {"kb:repair": [(2, ["упало"])], "kb:lint": [(0, [])]},
                                     projects, [_task(on_fail="stop")])
        sched.enqueue("a1b2c3d4")
        _wait(sched)
        items = cron.list_runs()[0]["items"]
        assert [i["status"] for i in items] == ["failed", "skipped"], items
        assert items[1]["note"] == "chain_stopped", items

        cron, ck, sched = _scheduler(tmp, {"kb:repair": [(2, ["упало"])], "kb:lint": [(0, [])]},
                                     projects, [_task()])
        sched.enqueue("a1b2c3d4")
        _wait(sched)
        assert [i["status"] for i in cron.list_runs()[0]["items"]] == ["failed", "failed"]

        saved = cron.BUSY_WAIT_S, cron.BUSY_POLL_S
        cron.BUSY_WAIT_S, cron.BUSY_POLL_S = 0.2, 0.05
        try:
            cron, ck, sched = _scheduler(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]},
                                         projects, [_task()])
            ck.busy.add(str(tmp / "a"))
            sched.enqueue("a1b2c3d4")
            _wait(sched)
            items = cron.list_runs()[0]["items"]
            assert [(i["status"], i["note"]) for i in items] == \
                [("skipped", "busy"), ("passed", "")], items
        finally:
            cron.BUSY_WAIT_S, cron.BUSY_POLL_S = saved
    finally:
        restore()


@test
def test_a_chain_cut_by_a_panel_restart_continues(tmp: Path):
    """Панель перезапустили посреди цепочки — новая продолжает с прерванного шага, а
    пройденное не повторяет. Старый обрыв только отмечается прерванным."""
    restore = set_home(tmp / "home")
    try:
        import time
        projects = [{"name": "A", "path": str(tmp / "a")}, {"name": "B", "path": str(tmp / "b")}]
        cron, ck, sched = _scheduler(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]},
                                     projects, [_task()])
        items = cron.expand(_task(), projects)
        items[0].update(status="passed")
        items[1].update(status="running", done=["kb:repair --apply"])
        cron.save_run({"id": "20261005-200000-aaaa", "task": "a1b2c3d4", "name": "Ночь",
                       "trigger": "schedule", "status": "running", "started": "",
                       "finished": "", "items": items})
        sched.recover()
        sched._next()
        _wait(sched)
        runs = {r["id"]: r for r in cron.list_runs()}
        old = runs.pop("20261005-200000-aaaa")
        assert old["status"] == "interrupted" and old.get("resumed_by"), old
        new = list(runs.values())[0]
        assert new["trigger"] == "resume", new
        assert [i["status"] for i in new["items"]] == ["passed", "passed"], new["items"]
        assert [c[1] for c in ck.calls] == ["kb:lint"], \
            "продолжение повторило уже пройденный шаг: " + str(ck.calls)

        stale = cron.expand(_task(), projects)
        stale[0]["status"] = "running"
        run = {"id": "20261005-210000-bbbb", "task": "a1b2c3d4", "name": "Ночь",
               "trigger": "schedule", "status": "running", "started": "", "finished": "",
               "items": stale}
        cron.save_run(run)
        data = json.loads(Path(cron.run_path(run["id"])).read_text(encoding="utf-8"))
        data["beat"] = time.time() - cron.RESUME_WINDOW - 60
        Path(cron.run_path(run["id"])).write_text(json.dumps(data), encoding="utf-8")
        sched = cron.Scheduler(ck, lambda: projects)
        sched.recover()
        assert not sched.queue, "обрыв полдневной давности продолжен как свежий"
    finally:
        restore()


@test
def test_the_panel_serves_and_edits_the_schedule(tmp: Path):
    """Раздел «Cron» на месте, а правка расписания проходит через сервер панели."""
    sys.path.insert(0, str(COCKPIT))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    restore = set_home(tmp / "home")
    try:
        mods = {m["id"]: m for m in ck.modules()}
        assert "cron" in mods and not mods["cron"].get("error"), mods.get("cron")
        assert mods["cron"]["group"] == "machine"
        projects = [{"name": "A", "path": str(tmp / "a")}]
        bad = ck.cron_action("save", {"task": _task(name="")}, projects)
        assert bad == {"ok": False, "error": "no_name"}, bad
        good = ck.cron_action("save", {"task": _task(id="", steps=[
            {"kind": "route", "project": "*", "route": "fix", "write": True}])}, projects)
        assert good["ok"], good
        tid = good["task"]["id"]
        assert ck.cron_action("toggle", {"id": tid, "enabled": False}, projects)["ok"]
        import cron
        assert cron.load_tasks()[0]["enabled"] is False
        assert ck.cron_action("start", {"id": "ffffffff"}, projects)["error"] == "no_task"
        # остановке номер задания не нужен: останавливают идущую цепочку
        assert ck.cron_action("stop", {}, projects) == {"ok": False, "error": "nothing"}
        assert ck.cron_action("delete", {"id": tid}, projects)["ok"]
        assert cron.load_tasks() == []
        assert str(tmp / "home") in cron.tasks_file(), "расписание пишется мимо подменённого дома"
    finally:
        restore()


@test
def test_stop_interrupts_the_running_step_and_skips_the_rest(tmp: Path):
    """«Остановить» прерывает текущий шаг, остальные шаги цепочки не идут, а запись
    прогона говорит «остановлен», а не «не прошёл»."""
    restore = set_home(tmp / "home")
    try:
        import time
        rr, _ = _modules()
        saved = rr.POLL_S
        rr.POLL_S = 0.02
        projects = [{"name": "A", "path": str(tmp / "a")}, {"name": "B", "path": str(tmp / "b")}]
        cron, ck, sched = _scheduler(tmp, {"kb:repair": [(None, ["идёт"])], "kb:lint": [(0, [])]},
                                     projects, [_task()])
        try:
            sched.enqueue("a1b2c3d4")
            end = time.time() + 10
            while not ck.calls and time.time() < end:
                time.sleep(0.02)
            assert sched.stop()["ok"]
            _wait(sched)
        finally:
            rr.POLL_S = saved
        run = cron.list_runs()[0]
        assert run["status"] == "stopped", run["status"]
        assert [i["status"] for i in run["items"]] == ["stopped", "skipped"], run["items"]
        assert run["items"][1]["note"] == "stopped", run["items"]
        assert [c[1] for c in ck.calls] == ["kb:repair"], "после остановки цепочка пошла дальше"
    finally:
        restore()


@test
def test_the_history_shows_one_row_per_launch_and_the_whole_output(tmp: Path):
    """История консоли: одна строка — один запуск, раскрытие — весь вывод, как он шёл.

    Один маршрут «Обновить базу» давал в архиве полсотни строк — по одной на шаг, а журнал
    помнил только последний запуск каждой команды. Шаги нового маршрута помечены родителем,
    шаги маршрута прежнего вида узнаются по времени его событий.
    """
    sys.path.insert(0, str(COCKPIT))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    rr, _ = _modules()
    restore = set_home(tmp / "home")
    try:
        project = tmp / "p"
        runs = project / ".aurora" / "runs"

        def put(rid, meta=None, console="", events=None, transcript=None):
            d = runs / rid
            d.mkdir(parents=True)
            if meta is not None:
                rr.write_meta(str(d), meta)
            if console:
                (d / "console.log").write_text(console, encoding="utf-8")
            if events is not None:
                (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events),
                                                encoding="utf-8")
            if transcript is not None:
                (d / "transcript.jsonl").write_text(
                    "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in transcript),
                    encoding="utf-8")

        # прежний маршрут страницы: события и россыпь шагов без родителя
        put("20261001-100000-route", events=[
            {"cmd": "kb:repair", "start": "2026-10-01T07:00:05.000Z", "end": "2026-10-01T07:00:09.000Z"},
            {"cmd": "kb:lint", "start": "2026-10-01T07:01:00.000Z", "end": "2026-10-01T07:01:30.000Z"}])
        put("20261001-070005Z-aaaaaa", console="ремонт")
        put("20261001-070100Z-bbbbbb", console="линтер")
        # новый маршрут и его шаг; отдельная команда
        put("20261002-080000Z-route", meta={"kind": "route", "id": "20261002-080000Z-route",
            "title": "Починить базу", "scId": "fix", "status": "passed",
            "started": "2026-10-02T08:00:00Z"},
            transcript=[{"k": "head", "s": "▸ шаг 1 из 1 · kb:repair"}, {"k": "out", "s": "✅ готово"}])
        put("20261002-080001Z-cccccc", meta={"kind": "step", "parent": "20261002-080000Z-route",
                                             "status": "done", "rc": 0}, console="шаг")
        put("20261003-090000Z-dddddd", meta={"kind": "command", "title": "kb:lint --residue",
                                             "status": "done", "rc": 1,
                                             "started": "2026-10-03T09:00:00Z"}, console="⚠️ нашла")
        rows = ck.history(str(project))
        assert [(r["kind"], r["id"]) for r in rows] == [
            ("command", "20261003-090000Z-dddddd"), ("route", "20261002-080000Z-route"),
            ("route", "20261001-100000-route")], rows
        got = ck.history_run(str(project), "20261002-080000Z-route")
        assert got["entries"] == [{"k": "head", "s": "▸ шаг 1 из 1 · kb:repair"},
                                  {"k": "out", "s": "✅ готово"}], got
        assert ck.history_run(str(project), "../чужое").get("error"), "раскрылся путь мимо архива"
        # уборка считает запуски: шаги живут вместе со своим маршрутом
        saved = ck.RUNS_KEEP
        ck.RUNS_KEEP = 2
        try:
            ck.trim_runs(str(project))
        finally:
            ck.RUNS_KEEP = saved
        left = sorted(p.name for p in runs.iterdir())
        assert "20261002-080001Z-cccccc" in left and "20261002-080000Z-route" in left, left
        assert "20261003-090000Z-dddddd" in left, left
    finally:
        restore()
