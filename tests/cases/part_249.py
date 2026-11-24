"""Проверки части 249: очередь, замки, паузы и сеть в учёте ожидания (этап 3).

Маршрут и команда проверяются на каркасе панели из `part_236`: настоящий планировщик,
но вместо скрипта — короткая пауза. Cron, сторож и git-автоматика получают подменные
часы, поэтому «два часа» в проверке не занимают двух часов. Сеть и модель не нужны.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness import KIT, SCRIPTS, set_home, test

sys.path.insert(0, str(KIT / "cockpit"))
sys.path.insert(0, str(SCRIPTS))

from cases.part_236 import _panel, _wait  # noqa: E402


def _timing():
    import importlib
    return importlib.import_module("timing")


@test
def test_route_queue_is_separate_and_is_not_work(tmp: Path):
    """Очередь маршрута входит в общее время, но не в работу и не в ожидание сервиса."""
    import route_runner as RR

    for CK, root in _panel(tmp):
        first = CK.start_job(str(root), "kb:links", ["--apply"])
        time.sleep(0.2)
        sc = {"id": "t", "title": "Проверочный", "when": "", "group": "база", "steps": []}
        run = RR.RouteRun(CK, str(root), sc, False)
        result: dict = {}

        def go(run=run, result=result):
            result.update(run.safe_run())

        th = threading.Thread(target=go, daemon=True)
        th.start()
        time.sleep(0.25)
        assert not run.done_flag, "маршрут начался поверх писателя"
        CK.stop_job(first)
        th.join(10)
        _wait(CK, [first])
        assert run.done_flag, "маршрут не закрылся"
        timing = result.get("timing") or {}
        assert timing.get("queue_ms", 0) >= 150, timing
        assert timing.get("work_ms") is None, timing
        assert timing.get("service_wait_ms") is None, timing
        assert abs(timing["queue_ms"] + timing["active_ms"] - timing["total_ms"]) <= 1, timing
        assert any("Ожидание запуска" in line for line in result.get("lines") or []), \
            result.get("lines")
        meta = RR.read_meta(run.base)
        assert meta["queue_ms"] == timing["queue_ms"], meta
        assert meta["active_started"] and meta["created_at"], meta


@test
def test_route_network_wait_is_service_time_not_work(tmp: Path):
    """Ожидание сети маршрута становится service_wait и не съедает работу."""
    import route_runner as RR

    for CK, root in _panel(tmp):
        sc = {"id": "t", "title": "Проверочный", "when": "", "group": "база", "steps": []}
        run = RR.RouteRun(CK, str(root), sc, False)
        run.active_started = time.time() - 1

        class Gate:
            @staticmethod
            def check_command(_cmd, _project, svc=None, hold=False):
                return {"ok": False, "blocked": []}

            @staticmethod
            def wait_restored(check, stop=None, poll=20, on_wait=None, sleep=None,
                              clock=None, woken=None, result=None):
                time.sleep(0.12)
                if result is not None:
                    result.update(waited_s=0.12, checks=2, reason="restored")
                return True

        saved = RR.net_gate
        RR.net_gate = lambda: Gate()
        try:
            assert run._net_wait({"cmd": "kb:links", "args": ["--apply"]})
        finally:
            RR.net_gate = saved
        timing = run._timing_now()
        assert timing["service_wait_ms"] >= 100, timing
        assert timing["wait_by_service"].get("network", 0) >= 100, timing
        assert timing["work_ms"] == timing["active_ms"] - timing["service_wait_ms"], timing


@test
def test_route_child_queue_and_wait_are_not_work(tmp: Path):
    """Очередь и ожидание дочерней команды вычитаются из работы маршрута."""
    import route_runner as RR

    for CK, root in _panel(tmp):
        sc = {"id": "child", "title": "Дочерний шаг", "when": "", "group": "база",
              "steps": [{"cmd": "kb:links", "args": ["--apply"]}]}
        run = RR.RouteRun(CK, str(root), sc, False)
        run.created_at = time.time() - 300
        run.active_started = run.created_at + 10
        run._event(
            {"cmd": "kb:links", "args": ["--apply"]},
            run.active_started, run.active_started + 200,
            {"rc": 0, "timing": {"queue_ms": 120_000, "service_wait_ms": 60_000,
                                 "other_wait_ms": 30_000}})
        timing = run._timing_now()
        # total_ms и work_ms — wall-clock: они считаются от time.time() и к 300_000 добавляется
        # время самого теста (macOS CI дал 300007 при допуске 5). Точные queue_ms,
        # service_wait_ms, other_wait_ms и равенства ниже проверяются как раньше.
        assert abs(timing["total_ms"] - 300_000) <= 100, timing
        assert timing["queue_ms"] == 130_000, timing
        assert timing["active_ms"] == timing["total_ms"] - timing["queue_ms"], timing
        assert timing["service_wait_ms"] == 60_000, timing
        assert timing["other_wait_ms"] == 30_000, timing
        assert abs(timing["work_ms"] - 80_000) <= 100, timing
        assert timing["queue_ms"] + timing["active_ms"] == timing["total_ms"], timing
        result = run._close({"ok": True, "reason": "passed", "failed": "", "note": "",
                             "steps": 1, "lines": [], "run_id": run.run_id})
        meta = RR.read_meta(run.base)
        assert meta["queue_ms"] == 130_000, meta
        assert abs(meta["timing"]["work_ms"] - 80_000) <= 100, meta["timing"]
        assert result["timing"]["service_wait_ms"] == 60_000, result["timing"]
        summary = CK.RS.route(str(root), "", 300, [], timing=meta["timing"])
        assert any("Ожидание запуска" in line for line in summary.get("lines") or []), \
            summary.get("lines")
        assert any("прочее ожидание" in line for line in summary.get("lines") or []), \
            summary.get("lines")


@test
def test_route_run_job_returns_child_final_timing(tmp: Path):
    """`run_job` читает timing после того, как задание записало итоговое meta."""
    import route_runner as RR

    for CK, root in _panel(tmp):
        child = RR.run_job(CK, str(root), "kb:links", ["--apply"])
        assert isinstance(child.get("timing"), dict), child
        assert child["timing"].get("v") == 1, child["timing"]
        assert "queue_ms" in child["timing"], child["timing"]


@test
def test_command_queue_lands_in_meta_and_timing(tmp: Path):
    """Команда, ждавшая ресурс, пишет queue_ms в meta и отдельной строкой в timing."""
    for CK, root in _panel(tmp):
        first = CK.start_job(str(root), "kb:links", ["--apply"])
        time.sleep(0.15)
        second = CK.start_job(str(root), "kb:links", ["--apply", "--cards"])
        with CK.JOBS_LOCK:
            assert CK.JOBS[second]["state"] == "queued", CK.JOBS[second]
        CK.stop_job(first)
        _wait(CK, [first, second])
        with CK.JOBS_LOCK:
            run_id = CK.JOBS[second]["run_id"]
        import route_runner as RR
        meta = RR.read_meta(os.path.join(CK.runs_dir(str(root)), run_id))
        assert meta["queue_ms"] > 0, meta
        assert meta["timing"]["queue_ms"] == meta["queue_ms"], meta["timing"]
        assert meta["timing"]["queue_ms"] <= meta["seconds"] * 1000 + 1000, meta


@test
def test_cron_busy_project_is_other_wait_and_has_utc_fields(tmp: Path):
    """Два часа ожидания занятого проекта — other_wait, а не работа и не service_wait."""
    import cron

    class Ck:
        @staticmethod
        def project_activity(_project):
            return {}

    restore = set_home(tmp / "home")
    try:
        s = cron.Scheduler(Ck(), lambda: [])
        s.stop_event = threading.Event()
        calls = {"n": 0}

        def busy(*_args):
            calls["n"] += 1
            return calls["n"] <= 2

        s._busy = busy
        clock = {"t": 1000.0}

        def sleep(sec):
            clock["t"] += sec

        with patch.object(cron, "BUSY_POLL_S", 3600):
            result: dict = {}
            assert s._wait_free("p", None, "route", (), result=result,
                                clock=lambda: clock["t"], sleep=sleep)
        assert result["wait_ms"] == 7_200_000, result
        assert result["reason"] == "busy", result
        run = s._new_run({"id": "t", "name": "Задание", "on_fail": "next",
                          "steps": []}, "manual", "")
        assert run["started_utc"].endswith("Z") and run["finished_utc"] == "", run
        assert run["wait_ms"] == 0 and run["queue_ms"] == 0, run
        timing = cron.run_timing("2026-11-21T10:00:00Z",
                                 "2026-11-21T12:00:00Z", 7_200_000)
        assert timing["other_wait_ms"] == 7_200_000, timing
        assert timing["service_wait_ms"] == 0 and timing["work_ms"] == 0, timing
        assert timing["other_by_kind"] == {"lock": 7_200_000}, timing
        mixed = cron.run_timing("2026-11-21T10:00:00Z", "2026-11-21T13:00:00Z",
                                7_200_000, 1_800_000)
        assert mixed["other_wait_ms"] == 7_200_000, mixed
        assert mixed["service_wait_ms"] == 1_800_000, mixed
        assert mixed["work_ms"] == 1_800_000, mixed
        item: dict = {}
        s._note_wait(item, service=500)
        s._note_wait(item, other=200)
        assert item["timing"]["service_wait_ms"] == 500, item
        assert item["timing"]["other_wait_ms"] == 200, item
    finally:
        restore()


def _cron_case(project: Path):
    class Ck:
        DEFAULT_LANG = "ru"

        @staticmethod
        def project_activity(_project):
            return {}

        @staticmethod
        def version_gap(_project):
            return ""

        @staticmethod
        def mark_running(*_args):
            return None

    import cron
    return cron, cron.Scheduler(Ck(), lambda: []), Ck()


def _cron_run(cron, item: dict) -> dict:
    return {"id": "run-1", "task": "t", "name": "Цепочка", "trigger": "manual",
            "status": "running", "started": cron.now_iso(), "finished": "",
            "started_utc": cron.utc_iso(datetime.now(timezone.utc) - timedelta(seconds=300)),
            "finished_utc": "", "queue_ms": 0, "wait_ms": 0,
            "on_fail": "next", "items": [item], "panel": os.getpid(), "beat": 0}


@test
def test_cron_command_child_queue_is_its_own_timing(tmp: Path):
    """Очередь 120 с у команды внутри Cron остаётся очередью, а не работой."""
    restore = set_home(tmp / "home")
    try:
        cron, sched, _ck = _cron_case(tmp)
        it = {"kind": "command", "project": str(tmp), "cmd": "kb:links", "args": [],
              "status": "pending", "write": True, "log": [], "jobs": [], "done": []}
        run = _cron_run(cron, it)
        task = {"id": "t", "name": "Цепочка", "net": "", "on_fail": "next", "lang": "ru"}
        saved_job, saved_gate = cron.RR.run_job, cron.RR.net_gate
        cron.RR.run_job = lambda *_a, **_k: {
            "rc": 0, "lines": [], "run_id": "child",
            "timing": {"queue_ms": 120_000, "service_wait_ms": 60_000,
                       "other_wait_ms": 0}}
        cron.RR.net_gate = lambda: None
        try:
            sched._item(task, run, it, "ru")
            sched._pass = lambda *_a, **_k: ""
            sched._run(task, run)
        finally:
            cron.RR.run_job, cron.RR.net_gate = saved_job, saved_gate
        assert it["timing"]["queue_ms"] == 120_000, it
        assert it["wait_ms"] == 0, it
        assert run["queue_ms"] == 120_000, run
        assert run["timing"]["queue_ms"] == 120_000, run["timing"]
        assert run["timing"]["service_wait_ms"] == 60_000, run["timing"]
        assert run["timing"]["work_ms"] == 120_000, run["timing"]
    finally:
        restore()


@test
def test_cron_route_child_queue_is_its_own_timing(tmp: Path):
    """Очередь 120 с маршрута в Cron не становится other_wait."""
    restore = set_home(tmp / "home")
    try:
        cron, sched, _ck = _cron_case(tmp)
        sc = {"id": "fix", "title": "Починить", "steps": [{"cmd": "kb:links", "args": []}]}
        it = {"kind": "route", "project": str(tmp), "route": "fix", "args": [],
              "status": "pending", "write": True, "log": [], "jobs": [], "done": []}
        run = _cron_run(cron, it)
        task = {"id": "t", "name": "Цепочка", "net": "", "on_fail": "next", "lang": "ru"}

        class Route:
            def __init__(self, *_a, **_k):
                pass

            @staticmethod
            def safe_run():
                return {"ok": True, "reason": "passed", "run_id": "route-child",
                        "timing": {"queue_ms": 120_000, "service_wait_ms": 60_000,
                                   "other_wait_ms": 30_000}}

        saved = cron.RR.scenario, cron.RR.route_resources, cron.RR.RouteRun, cron.RR.net_gate
        cron.RR.scenario = lambda *_a, **_k: sc
        cron.RR.route_resources = lambda *_a, **_k: ()
        cron.RR.RouteRun = Route
        cron.RR.net_gate = lambda: None
        try:
            sched._item(task, run, it, "ru")
            sched._pass = lambda *_a, **_k: ""
            sched._run(task, run)
        finally:
            (cron.RR.scenario, cron.RR.route_resources,
             cron.RR.RouteRun, cron.RR.net_gate) = saved
        assert it["timing"]["queue_ms"] == 120_000, it
        assert 30_000 <= it["timing"]["other_wait_ms"] <= 30_100, it
        assert 30_000 <= it["wait_ms"] <= 30_100, it
        assert run["queue_ms"] == 120_000, run
        assert run["timing"]["queue_ms"] == 120_000, run["timing"]
        assert 30_000 <= run["timing"]["other_wait_ms"] <= 30_100, run["timing"]
        # work_ms считается от wall-clock: на медленном Windows-раннере сам тест
        # добавляет к total около секунды, и жёсткий допуск 100 мс падает.
        assert 89_900 <= run["timing"]["work_ms"] <= 92_000, run["timing"]
    finally:
        restore()


@test
def test_task_scheduler_exposes_queue_wait_and_conflict(tmp: Path):
    """Очередь планировщика хранит queued_at, started_at, wait_ms и причину конфликта."""
    import task_scheduler as TS

    s = TS.TaskScheduler(max_parallel=1, rules={})
    blocker = TS.Task(id="a", project=os.path.realpath(tmp), resources=("kb-write",))
    queued = TS.Task(id="b", project=os.path.realpath(tmp), resources=("kb-write",))
    assert s.submit(blocker) == "running"
    time.sleep(0.05)
    assert s.submit(queued) == "queued"
    row = s.snapshot()["queued"][0]
    assert row["queued_at"] and row["conflict"] and row["wait_ms"] == 0, row
    s.release("a")
    active = s.snapshot()["active"]
    assert active and active[0]["started_at"] and active[0]["wait_ms"] >= 40, active
    s.release("b")


@test
def test_watchdog_pause_writes_wait_span_and_ms(tmp: Path):
    """Пауза сторожа становится wait-спаном в timing.jsonl задания."""
    import watchdog as WD

    run_dir = tmp / "run"
    run_dir.mkdir()
    clock = {"t": 10_000.0}
    job = {"id": "j", "project": str(tmp), "out": [], "run_dir": str(run_dir),
           "run_id": "r1", "started": clock["t"] - 20, "last_out": clock["t"] - 20,
           "parent": "route-1", "proc": types.SimpleNamespace(pid=4242,
                                                              poll=lambda: None),
           "pausable": True}

    class Ck:
        JOBS_LOCK = threading.Lock()
        JOBS = {"j": job}

        @staticmethod
        def job_pulse(_job):
            return {"state": "quiet"}

    dog = WD.Watchdog(Ck, probe_fn=lambda _p: {"ok": False, "down": ["сеть"]},
                      clock=lambda: clock["t"])
    with patch.object(WD, "suspend_tree", lambda _pid: True), \
            patch.object(WD, "resume_tree", lambda _pid: True):
        dog.pause(job)
        clock["t"] += 600
        dog.resume(job)
    assert job["paused_ms"] == 600_000, job
    rows = [json.loads(line) for line in (run_dir / "timing.jsonl").read_text(
        encoding="utf-8").splitlines()]
    wait = next(r for r in rows if r["kind"] == "wait")
    assert wait["wait_class"] == "service" and wait["service"] == "network", wait
    assert wait["reason"] == "watchdog_pause" and wait["dur_ms"] >= 599_000, wait


@test
def test_watchdog_pause_lands_in_final_meta(tmp: Path):
    """Пауза сторожа уменьшает работу в итоговом meta.json, а не только в спане."""
    import route_runner as RR
    import watchdog as WD

    for CK, root in _panel(tmp):
        (tmp / "slow.py").write_text(
            "import time\nprint('start', flush=True)\ntime.sleep(2)\nprint('done', flush=True)\n",
            encoding="utf-8")
        job_id = CK.start_job(str(root), "kb:links", ["--apply"], parent="route-test")
        job: dict = {}
        for _ in range(100):
            with CK.JOBS_LOCK:
                job = CK.JOBS.get(job_id) or {}
            if job.get("proc") is not None:
                break
            time.sleep(0.01)
        assert job.get("proc") is not None, job
        clock = {"t": time.time()}
        dog = WD.Watchdog(CK, probe_fn=lambda _p: {"ok": False, "down": ["сеть"]},
                          clock=lambda clock=clock: clock["t"])
        with patch.object(WD, "suspend_tree", lambda _pid: True), \
                patch.object(WD, "resume_tree", lambda _pid: True):
            dog.pause(job)
            clock["t"] += 600
            dog.resume(job)
        _wait(CK, [job_id])
        with CK.JOBS_LOCK:
            run_id = CK.JOBS[job_id]["run_id"]
        meta: dict = {}
        for _ in range(100):
            meta = RR.read_meta(os.path.join(CK.runs_dir(str(root)), run_id))
            if isinstance(meta.get("timing"), dict):
                break
            time.sleep(0.01)
        assert meta["paused_ms"] >= 600_000, meta
        assert meta["timing"]["service_wait_ms"] > 0, meta["timing"]
        assert meta["timing"]["work_ms"] == 0, meta["timing"]


@test
def test_git_backoff_writes_other_wait(tmp: Path):
    """Повтор автоматики после отказа — ожидание other, а не работа проекта."""
    import git_auto as GA

    run_dir = tmp / "run"
    run_dir.mkdir()
    T = _timing()
    (run_dir / "meta.json").write_text(json.dumps({
        "id": "r1", "seconds": 1000,
        "timing": T.legacy_timing(1000, None, service_ms=0)}), encoding="utf-8")
    clock = {"t": 1000.0}
    auto = GA.GitAuto(types.SimpleNamespace(JOBS={}, JOBS_LOCK=threading.Lock()),
                      lambda: [], clock=lambda: clock["t"])
    state = {"auto_update_fail_at": 1000.0, "auto_update_run_dir": str(run_dir)}
    assert auto._backoff("p", state, "update") is True
    clock["t"] = 1900.0
    assert auto._backoff("p", state, "update") is False
    rows = [json.loads(line) for line in (run_dir / "timing.jsonl").read_text(
        encoding="utf-8").splitlines()]
    wait = next(r for r in rows if r["kind"] == "wait")
    assert wait["wait_class"] == "other" and wait["service"] == "git_remote", wait
    assert wait["dur_ms"] >= 899_000, wait
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    timing = meta["timing"]
    assert timing["other_wait_ms"] >= 899_000, timing
    assert timing["work_ms"] == 1_000_000, timing
    assert timing["total_ms"] == timing["queue_ms"] + timing["active_ms"], timing
    assert timing["other_by_kind"]["backoff"] >= 899_000, timing


@test
def test_gate_wait_returns_waited_seconds_and_net_observe_returns_timing(tmp: Path):
    """Ожидание связи отдаёт длительность, снимки — приблизительную сводку timing."""
    import conn_gate as G
    import net_observe as NO

    clock = {"t": 0.0}

    def sleep(sec):
        clock["t"] += sec

    checks = {"n": 0}

    def check():
        checks["n"] += 1
        return {"ok": clock["t"] >= 120, "reason": "network_down"}

    result: dict = {}
    assert G.wait_restored(check, poll=60, sleep=sleep, clock=lambda: clock["t"],
                           result=result)
    assert result["waited_s"] >= 120 and result["checks"] >= 3, result
    assert result["reason"] == "restored", result

    data = {"seconds": 300, "snapshots": [
        {"at": "2026-11-21T10:00:00Z", "elapsed_s": 0, "blocked": [{"code": "llm_down"}],
         "state": "offline", "requirements": {"llm": True}},
        {"at": "2026-11-21T10:05:00Z", "elapsed_s": 300, "blocked": [], "state": "online",
         "requirements": {"llm": True}},
    ]}
    summary = NO.summarize(data)
    timing = summary["timing"]
    assert timing["v"] == 1 and timing["service_wait_ms"] == 300_000, timing
    assert timing["quality"]["approx"] is True, timing
    assert timing["active_ms"] == 300_000, timing


@test
def test_legacy_other_zero_does_not_invent_service_zero(tmp: Path):
    """Если сервис не измерялся, other=0 не превращает его в известный ноль."""
    T = _timing()
    zero = T.legacy_timing(1000, None, other_ms=0)
    assert zero["service_wait_ms"] is None, zero
    assert zero["work_ms"] is None, zero
    assert zero["other_wait_ms"] == 0, zero
    assert zero["quality"]["wait_known"] is False, zero
    paused = T.legacy_timing(1000, None, other_ms=600_000)
    assert paused["service_wait_ms"] is None, paused
    assert paused["work_ms"] is None, paused
    assert paused["other_wait_ms"] == 600_000, paused
    assert paused["source"] == "other", paused


@test
def test_route_snapshot_zero_wait_stays_approximate(tmp: Path):
    """blocked_s=0 из снимка остаётся оценкой, а не точным нулём."""
    import net_observe as NO
    import route_runner as RR

    for CK, root in _panel(tmp):
        run = RR.RouteRun(CK, str(root), {"id": "t", "title": "t", "steps": []}, False)
        saved = NO.peek
        NO.peek = lambda *_a, **_k: {
            "summary": {"snapshots": 1, "blocked_s": 0, "network_required": False,
                        "reason": "ok"}}
        try:
            timing = run._timing_now()
        finally:
            NO.peek = saved
        assert timing["service_wait_ms"] == 0, timing
        assert timing["confidence"] == "approx", timing
        assert timing["quality"]["approx"] is True, timing
        assert timing["source"] == "net_snapshots", timing
        assert timing["work_ms"] == timing["active_ms"], timing


@test
def test_command_network_wait_is_recorded_in_job(tmp: Path):
    """Ожидание сети команды возвращается в job и попадёт в её timing."""
    import conn_gate as GATE

    for CK, root in _panel(tmp):
        job = {"id": "net-1", "out": [], "cancel": threading.Event(),
               "started": time.time(), "last_out": time.time()}
        saved_check, saved_wait = CK.net_check_command, GATE.wait_restored

        def wait(check, **kw):
            time.sleep(0.12)
            kw["result"].update(waited_s=0.12, checks=2, reason="restored")
            return True

        CK.net_check_command = lambda *_a, **_k: {"ok": False, "blocked": []}
        GATE.wait_restored = wait
        try:
            assert CK.net_wait_for(job, str(root), "sync:confluence")
        finally:
            CK.net_check_command, GATE.wait_restored = saved_check, saved_wait
        assert job["net_wait_ms"] >= 100, job


@test
def test_command_net_wait_lands_in_final_timing(tmp: Path):
    """Точное ожидание сети команды остаётся в её meta после запуска."""
    import conn_gate as GATE
    import route_runner as RR

    for CK, root in _panel(tmp):
        saved_check, saved_wait = CK.net_check_command, GATE.wait_restored

        def wait(check, **kw):
            time.sleep(0.12)
            kw["result"].update(waited_s=0.12, checks=2, reason="restored")
            return True

        CK.net_check_command = lambda *_a, **_k: {"ok": False, "blocked": []}
        GATE.wait_restored = wait
        try:
            job_id = CK.start_job(str(root), "kb:links", ["--apply"], net_wait=True)
            _wait(CK, [job_id])
        finally:
            CK.net_check_command, GATE.wait_restored = saved_check, saved_wait
        with CK.JOBS_LOCK:
            run_id = CK.JOBS[job_id]["run_id"]
        meta: dict = {}
        for _ in range(50):
            meta = RR.read_meta(os.path.join(CK.runs_dir(str(root)), run_id))
            if isinstance(meta.get("timing"), dict):
                break
            time.sleep(0.02)
        assert meta["timing"]["service_wait_ms"] >= 100, meta["timing"]
        assert meta["timing"]["quality"]["approx"] is False, meta["timing"]


@test
def test_net_report_reads_queue_from_meta_and_prints_it_separately(tmp: Path):
    """net:report берёт очередь из meta и не кладёт её в скобки ожидания."""
    import net_report

    project = tmp / "p"
    run_id = "20261121-101500-ab12cd-command"
    run_dir = project / ".aurora" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "net.json").write_text(json.dumps({
        "id": run_id, "cmd": "agent:distill",
        "summary": {"seconds": 600, "snapshots": 2, "blocked_s": 120,
                    "network_required": True, "reason": "network"}}), encoding="utf-8")
    T = _timing()
    timing = T.legacy_timing(600, {"snapshots": 2, "blocked_s": 120}, queue_ms=180_000)
    (run_dir / "meta.json").write_text(json.dumps({
        "id": run_id, "seconds": 600, "queue_ms": 180_000, "timing": timing}),
        encoding="utf-8")
    data = json.loads((run_dir / "net.json").read_text(encoding="utf-8"))
    out = net_report._show(data, str(project))
    assert "- ожидание запуска: 3 мин" in out, out
    assert "- длительность: 5 мин (+ ~2 мин ожидания)" in out, out
    exact = T.legacy_timing(600, None, queue_ms=60_000, service_ms=120_000)
    assert exact["active_ms"] == 540_000 and exact["total_ms"] == 600_000, exact
    assert exact["service_wait_ms"] == 120_000, exact
    assert exact["work_ms"] == 420_000, exact
    assert exact["source"] == "spans" and exact["confidence"] == "exact", exact
    assert exact["quality"]["queue_separated"] is True, exact


@test
def test_production_human_pause_is_not_service_wait(tmp: Path):
    """Пауза человека в производстве видна отдельно и не становится service_wait."""
    import make_state as MS

    run = MS.make_run(tmp, "make-1", kind="document", title="Документ")
    run.create()
    run.update_state(paused_ms=600_000)
    state = run.state()
    assert state["paused_ms"] == 600_000 and state["active_started"], state
    assert "service_wait" not in state, state
    deadline = run.deadline()
    started = MS.datetime.fromisoformat(state["active_started"].replace("Z", "+00:00")).timestamp()
    assert deadline >= started + 3600 + 600, (deadline, started)
    assert run.paused_total_ms() == 600_000, run.paused_total_ms()
    from agent_runner import make_report_data, report_make
    data = make_report_data(
        run, {"kind": "document", "spec": {"title": "Документ"}, "stages": {}},
        {}, status="done", elapsed=7200)
    timing = data["timing"]
    assert timing["other_wait_ms"] == 600_000, timing
    assert timing["service_wait_ms"] is None, timing
    assert timing["work_ms"] is None, timing
    md = report_make({"sid": "make-1", "seconds": 7200,
                      "state": {"timing": timing, "stages": {}, "metrics": {}}})
    assert "Пауза человека: 10 мин" in md, md
    assert "Ожидание внешних сервисов" not in md, md
