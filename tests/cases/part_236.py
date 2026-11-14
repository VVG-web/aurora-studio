#!/usr/bin/env python3
"""part_236 — ресурсный планировщик и параллельные задачи панели.

Проверки не запускают настоящую панель: задания подменены медленным скриптом,
маршрут — временным сценарием. Живая сеть, модель и git не нужны.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "cockpit"))
sys.path.insert(0, str(KIT / "scripts"))
import task_scheduler as TS  # noqa: E402


def _task(task_id: str, project: str, resources: tuple, **kw) -> TS.Task:
    return TS.Task(id=task_id, project=os.path.realpath(project), resources=resources, **kw)


@test
def test_independent_tasks_run_and_conflicting_ones_queue(tmp: Path):
    started: list[str] = []
    s = TS.TaskScheduler(max_parallel=4, rules={})
    a = _task("a", str(tmp), ("kb-write",), start=lambda: started.append("a"))
    b = _task("b", str(tmp), ("code:repo-a",), start=lambda: started.append("b"))
    c = _task("c", str(tmp), ("kb-write",), start=lambda: started.append("c"))
    assert s.submit(a) == "running"
    assert s.submit(b) == "running", "разные ресурсы не должны ждать друг друга"
    assert s.submit(c) == "queued", "второй писатель в базу должен ждать"
    assert started == ["a", "b"], started
    assert s.snapshot()["queued"][0]["id"] == "c"
    s.release("a")
    assert started == ["a", "b", "c"], started
    assert s.snapshot()["queued"] == []
    s.release("b")
    s.release("c")


@test
def test_a_route_child_inherits_the_parent_resource(tmp: Path):
    s = TS.TaskScheduler(max_parallel=4, rules={})
    parent = _task("route", str(tmp), ("kb-write",))
    child = _task("step", str(tmp), ("kb-write",), parent="route")
    other = _task("manual", str(tmp), ("kb-write",))
    assert s.submit(parent) == "running"
    assert s.submit(child) == "running", "шаг маршрута не должен ждать собственный ресурс"
    assert s.submit(other) == "queued", "ручной писатель должен ждать маршрут"
    s.release("step")
    s.release("route")
    assert s.snapshot()["queued"] == []
    s.release("manual")


@test
def test_parallel_limit_is_explicit(tmp: Path):
    s = TS.TaskScheduler(max_parallel=1, rules={})
    a = _task("a", str(tmp), ("code:a",))
    b = _task("b", str(tmp), ("code:b",))
    assert s.submit(a) == "running"
    assert s.submit(b) == "queued"
    assert "лимит" in s.snapshot()["queued"][0]["conflict"]
    s.release("a")
    assert s.snapshot()["active"][0]["id"] == "b"
    s.release("b")


@test
def test_code_sync_locks_are_per_repository(tmp: Path):
    import code_sync as CS
    from aurora_common import lock_release, lock_take

    root = tmp / "project"
    (root / ".aurora" / "state").mkdir(parents=True)
    a_lock = CS.lock_file(str(root), "A")
    b_lock = CS.lock_file(str(root), "B")
    assert a_lock != b_lock and a_lock.name != "code.lock"
    taken, _ = lock_take(str(a_lock), {"task": "test"})
    assert taken
    try:
        locks, why = CS._take_sync_locks(str(root), ["A", "B"], "B")
        assert locks and not why, why
        for p in locks:
            lock_release(str(p))
        locks, why = CS._take_sync_locks(str(root), ["A", "B"], "A")
        assert not locks and "A" in why, why
        locks, why = CS._take_sync_locks(str(root), ["A", "B"], "")
        assert not locks and "A" in why, why
    finally:
        lock_release(str(a_lock))


@test
def test_a_job_sees_an_external_agent_lock(tmp: Path):
    import aurora_common as AC
    root = tmp / "project"
    (root / ".aurora" / "state").mkdir(parents=True)
    lock = root / ".aurora" / "state" / "agent.lock"
    taken, _ = AC.lock_take(str(lock), {"task": "cli"})
    assert taken
    try:
        s = TS.TaskScheduler(max_parallel=4, rules={})
        task = _task("job", str(root), ("kb-write",))
        assert "agent.lock" in s.preview(task), s.preview(task)
    finally:
        AC.lock_release(str(lock))


@test
def test_an_agent_step_still_sees_the_external_agent_lock(tmp: Path):
    import aurora_common as AC
    root = tmp / "project"
    (root / ".aurora" / "state").mkdir(parents=True)
    lock = root / ".aurora" / "state" / "agent.lock"
    s = TS.TaskScheduler(max_parallel=4, rules={})
    parent = _task("route", str(root), ("kb-write",))
    assert s.submit(parent) == "running", s.snapshot()
    taken, _ = AC.lock_take(str(lock), {"task": "cli"})
    assert taken, "внешний замок агента не взят"
    try:
        step = TS.Task(id="step", project=os.path.realpath(root), resources=(),
                       parent="route", spec={"cmd": "agent:build", "args": ["--apply"]})
        assert "agent.lock" in s.preview(step), s.preview(step)
    finally:
        s.release("route")
        AC.lock_release(str(lock))


@test
def test_queue_is_saved_without_route_children(tmp: Path):
    store = tmp / "queue.json"
    s = TS.TaskScheduler(max_parallel=1, store_path=store, rules={})
    blocker = _task("blocker", str(tmp), ("kb-write",))
    queued = TS.Task(id="queued", project=os.path.realpath(tmp), resources=("kb-write",),
                     spec={"project": os.path.realpath(tmp), "cmd": "kb:links",
                           "args": ["--apply"], "kind": "job", "parent": ""})
    child = TS.Task(id="child", project=os.path.realpath(tmp), resources=("kb-write",),
                    parent="route-x", spec={"project": os.path.realpath(tmp), "cmd": "kb:links",
                                            "args": ["--apply"], "kind": "job", "parent": "route-x"})
    assert s.submit(blocker) == "running"
    assert s.submit(queued) == "queued"
    assert s.submit(child) == "queued"
    rows = TS.TaskScheduler.read_store(store)
    assert [r["id"] for r in rows] == ["queued"], rows
    assert rows[0]["spec"]["cmd"] == "kb:links"
    s.release("blocker")
    s.release("queued")
    s.release("child")


@test
def test_queued_job_pulse_names_the_resource(_t):
    import aurora_cockpit as CK
    pulse = CK.job_pulse({"state": "queued", "queued_at": time.time() - 5,
                          "queue_reason": "kb-write: route r1"})
    assert pulse["state"] == "queued" and "kb-write" in pulse["queue_reason"], pulse


@test
def test_panel_and_server_wire_the_task_queue(_t):
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    py = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"/api/task/check"' in js and '"/api/task/stop"' in js, "панель не спрашивает ресурс"
    assert 'u.path == "/api/tasks"' in py and "TASKS.snapshot" in py, "сервер не отдаёт очередь"
    assert 'live.job_queued' in js and 'live.queue_reason' in js, "очередь не видна в Консоли"


def _panel(tmp: Path):
    """Подменить панель так, чтобы задания шли медленным скриптом."""
    import aurora_cockpit as CK
    root = make_project(tmp)
    script = tmp / "slow.py"
    script.write_text(
        "import time\n"
        "print('start', time.time(), flush=True)\n"
        "time.sleep(0.8)\n"
        "print('done', time.time(), flush=True)\n",
        encoding="utf-8")
    row = {"runnable": True, "flags": ["--apply", "--cards", "--repo"],
           "fixed_flags": [], "script": "slow.py", "ns": "kb"}
    saved = (CK.command_by_name, CK.script_path, CK.write_runlog, CK.mark_running,
             CK.engine_fallback_note, CK.RUNNING, CK.TASKS)
    old_jobs = dict(CK.JOBS)
    try:
        CK.command_by_name = lambda _name: row
        CK.script_path = lambda _project, _name: str(script)
        CK.write_runlog = lambda *a, **k: None
        CK.mark_running = lambda *a, **k: None
        CK.engine_fallback_note = lambda *a, **k: ""
        CK.RUNNING = str(tmp / "running.json")
        CK.TASKS = TS.TaskScheduler(max_parallel=4, rules={})
        with CK.JOBS_LOCK:
            CK.JOBS.clear()
        yield CK, root
    finally:
        (CK.command_by_name, CK.script_path, CK.write_runlog, CK.mark_running,
         CK.engine_fallback_note, CK.RUNNING, CK.TASKS) = saved
        with CK.JOBS_LOCK:
            CK.JOBS.clear()
            CK.JOBS.update(old_jobs)


def _wait(CK, ids, timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with CK.JOBS_LOCK:
            if all(CK.JOBS[i]["done"] for i in ids):
                return
        time.sleep(0.05)
    with CK.JOBS_LOCK:
        rows = {i: (CK.JOBS[i]["state"], CK.JOBS[i]["done"], CK.JOBS[i]["out"]) for i in ids}
    raise AssertionError(f"задания не завершились: {rows}")


@test
def test_two_commands_of_one_project_run_together(tmp: Path):
    for CK, root in _panel(tmp):
        a = CK.start_job(str(root), "kb:links", ["--apply"])
        b = CK.start_job(str(root), "code:sync", ["--apply", "--repo=A"])
        assert a != b
        with CK.JOBS_LOCK:
            assert CK.JOBS[a]["state"] == "running" and CK.JOBS[b]["state"] == "running"
        _wait(CK, [a, b])
        starts = []
        for jid in (a, b):
            with CK.JOBS_LOCK:
                for line in CK.JOBS[jid]["out"]:
                    if line.startswith("start "):
                        starts.append(float(line.split()[1]))
        assert len(starts) == 2, starts
        assert max(starts) - min(starts) < 0.5, f"задания шли не параллельно: {starts}"


@test
def test_manual_job_does_not_reuse_a_route_step(tmp: Path):
    for CK, root in _panel(tmp):
        parent = CK.start_job(str(root), "kb:lint", [], parent="route-x")
        manual = CK.start_job(str(root), "kb:lint", [])
        assert parent != manual, "ручной запуск получил задание шага маршрута"
        _wait(CK, [parent, manual])


@test
def test_queued_job_is_stopped_without_touching_the_runner(tmp: Path):
    for CK, root in _panel(tmp):
        first = CK.start_job(str(root), "kb:links", ["--apply"])
        second = CK.start_job(str(root), "kb:links", ["--apply", "--cards"])
        assert first != second
        with CK.JOBS_LOCK:
            assert CK.JOBS[second]["state"] == "queued", CK.JOBS[second]["state"]
        res = CK.stop_job(second)
        assert res.get("ok") and res.get("queued"), res
        with CK.JOBS_LOCK:
            assert CK.JOBS[second]["done"] and CK.JOBS[second]["rc"] == -15
            assert not CK.JOBS[first]["done"], "остановка очереди задела идущую задачу"
        _wait(CK, [first])


@test
def test_route_waits_for_a_writer_and_starts_after_it(tmp: Path):
    for CK, root in _panel(tmp):
        job = CK.start_job(str(root), "kb:links", ["--apply"])
        got: list[bool] = []

        def route(CK=CK, root=root, got=got):
            got.append(CK.task_acquire("route-test", str(root), ("kb-write",), kind="route"))

        th = threading.Thread(target=route, daemon=True)
        th.start()
        time.sleep(0.2)
        assert got == [], "маршрут начался поверх писателя"
        CK.stop_job(job)
        th.join(5)
        assert got == [True], got
        CK.task_release("route-test")


@test
def test_route_runner_waits_for_a_kb_writer(tmp: Path):
    for CK, root in _panel(tmp):
        import route_runner as RR
        job = CK.start_job(str(root), "kb:links", ["--apply"])
        sc = {"id": "t", "title": "Проверочный", "when": "", "group": "база", "steps": []}
        run = RR.RouteRun(CK, str(root), sc, False)
        res: dict = {}

        def go(run=run, res=res):
            res.update(run.safe_run())

        th = threading.Thread(target=go, daemon=True)
        th.start()
        time.sleep(0.2)
        assert not run.done_flag, "маршрут начался поверх писателя"
        assert any("Ожидание ресурса" in str(e.get("s")) for e in run.journal.entries), \
            "маршрут не сказал, чего ждёт"
        CK.stop_job(job)
        th.join(5)
        assert run.done_flag, "маршрут не освободился после писателя"
        assert res.get("ok") is not False, res


@test
def test_route_and_code_clone_run_together(tmp: Path):
    for CK, root in _panel(tmp):
        import route_runner as RR
        sc = {"id": "t", "title": "Проверочный", "when": "", "group": "база",
              "steps": [{"cmd": "kb:links", "why": "", "flags": ["--apply"],
                         "manual": False, "cycle": ""}]}
        run = RR.RouteRun(CK, str(root), sc, True)
        res: dict = {}

        def go(run=run, res=res):
            res.update(run.safe_run())

        th = threading.Thread(target=go, daemon=True)
        th.start()
        clone = CK.start_job(str(root), "code:sync", ["--apply", "--repo=A"])
        th.join(10)
        _wait(CK, [clone])
        assert run.done_flag and res.get("ok") is not False, res
        starts = []
        for e in run.journal.entries:
            s = str(e.get("s") or "")
            if s.startswith("start "):
                starts.append(float(s.split()[1]))
        with CK.JOBS_LOCK:
            for line in CK.JOBS[clone]["out"]:
                if line.startswith("start "):
                    starts.append(float(line.split()[1]))
        assert len(starts) == 2, starts
        assert max(starts) - min(starts) < 0.5, f"маршрут и клон шли не параллельно: {starts}"
