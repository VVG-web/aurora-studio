#!/usr/bin/env python3
"""part_228 — параллельная работа LLM: общие места и независимые задания панели."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test

sys.path.insert(0, str(SCRIPTS))
import agent_core as AG  # noqa: E402


@test
def test_slots_are_shared_between_processes(tmp: Path):
    """Два процесса не могут занять больше мест шлюза, чем объявлено."""
    slots = tmp / "llm-slots"
    code_a = f"""
import sys, time
sys.path.insert(0, {str(SCRIPTS)!r})
import agent_core as A
s = A._SharedSlots(1, {str(slots)!r})
print('got', s.acquire(timeout=2, project='project-a'), flush=True)
time.sleep(1.5)
s.release()
"""
    code_b = f"""
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import agent_core as A
s = A._SharedSlots(1, {str(slots)!r})
print('short', s.acquire(timeout=0.3, project='project-b'), flush=True)
print('long', s.acquire(timeout=3, project='project-b'), flush=True)
s.release()
"""
    a = subprocess.Popen([sys.executable, "-c", code_a], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert a.stdout.readline().strip().startswith("got True"), "первый процесс не взял место"
        b = subprocess.run([sys.executable, "-c", code_b], capture_output=True,
                           text=True, encoding="utf-8", timeout=10, check=False)
        assert "short False" in b.stdout and "long True" in b.stdout, (b.stdout, b.stderr)
    finally:
        try:
            a.wait(timeout=5)
        except subprocess.TimeoutExpired:
            a.kill()
            a.wait(timeout=5)
    assert AG._SharedSlots(1, slots)._value == 1, "место не вернулось после процессов"


@test
def test_slots_prefer_another_project_after_release(_t):
    """Освободившееся место получает другой проект, а не второй поток того же."""
    slots = AG._FairSlots(1)
    assert slots.acquire(project="a")
    got: list[str] = []

    def wait(project: str):
        if slots.acquire(timeout=2, project=project):
            got.append(project)

    first = threading.Thread(target=wait, args=("a",))
    second = threading.Thread(target=wait, args=("b",))
    first.start()
    time.sleep(0.05)
    second.start()
    time.sleep(0.05)
    slots.release()
    second.join(timeout=3)
    assert got == ["b"], got
    slots.release()
    first.join(timeout=3)
    assert got == ["b", "a"], got
    slots.release()
    assert slots._value == 1


@test
def test_slot_semaphore_uses_the_shared_directory_when_asked(tmp: Path):
    """С явным каталогом мест процесс берёт общий пул, а не локальный."""
    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://test",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    old = os.environ.get("AURORA_LLM_SLOTS_DIR")
    os.environ["AURORA_LLM_SLOTS_DIR"] = str(tmp / "slots")
    AG._SEM.clear()
    try:
        backend = cfg["backends"][0]
        slots = AG._slot_semaphore(backend, cfg)
        assert isinstance(slots, AG._SharedSlots), type(slots)
        assert slots.acquire(project="a")
        assert not slots.acquire(blocking=False, project="b")
        slots.release()
    finally:
        AG._SEM.clear()
        if old is None:
            os.environ.pop("AURORA_LLM_SLOTS_DIR", None)
        else:
            os.environ["AURORA_LLM_SLOTS_DIR"] = old


@test
def test_call_role_uses_the_project_as_the_slot_key(_t):
    """Вызов модели передаёт в пул путь проекта, а не общий cwd панели."""
    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://test",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m",
                           "AURORA_AGENT_REQUEST_TIMEOUT": "5"})
    seen: list[str] = []

    class Slots:
        def acquire(self, blocking=True, timeout=None, project=""):
            seen.append(project)
            return True

        def release(self):
            pass

    def transport(_kind, _backend, _payload, _timeout):
        return 200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                     "usage": {"completion_tokens": 1, "prompt_tokens": 1}}, "", 0.01

    old = AG._slot_semaphore
    AG._slot_semaphore = lambda _backend, _cfg: Slots()
    try:
        res = AG._call_role(cfg, "worker", [{"role": "user", "content": "hi"}],
                            transport=transport, sleep=AG.NO_WAIT,
                            deadline=time.time() + 5, project="/tmp/project-a")
    finally:
        AG._slot_semaphore = old
    assert res.get("ok"), res
    assert seen and Path(seen[0]).name == "project-a", seen


@test
def test_two_projects_run_independent_panel_jobs(tmp: Path):
    """Разные проекты получают разные задания и идут одновременно."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as CK

    (tmp / "a").mkdir()
    (tmp / "b").mkdir()
    root_a = make_project(tmp / "a")
    root_b = make_project(tmp / "b")
    script = tmp / "slow.py"
    script.write_text("import time\ntime.sleep(0.8)\n", encoding="utf-8")
    row = {"runnable": True, "flags": [], "fixed_flags": [], "script": "slow.py",
           "cmd": "kb:lint", "ns": "kb"}
    saved = (CK.command_by_name, CK.script_path, CK.write_runlog, CK.mark_running,
             CK.engine_fallback_note, CK.RUNNING)
    saved_jobs = dict(CK.JOBS)
    try:
        CK.command_by_name = lambda _name: row
        CK.script_path = lambda _project, _name: str(script)
        CK.write_runlog = lambda *a, **k: None
        CK.mark_running = lambda *a, **k: None
        CK.engine_fallback_note = lambda *a, **k: ""
        CK.RUNNING = str(tmp / "running.json")
        with CK.JOBS_LOCK:
            CK.JOBS.clear()
        a = CK.start_job(str(root_a), "kb:lint", [])
        b = CK.start_job(str(root_b), "kb:lint", [])
        assert a != b, "разные проекты получили одно задание"
        assert CK.start_job(str(root_a), "kb:lint", []) == a, \
            "повторный запуск того же проекта создал второе задание"
        live = CK.live_now().get("jobs") or []
        assert {j["id"] for j in live} >= {a, b}, live
        end = time.time() + 10
        while time.time() < end:
            with CK.JOBS_LOCK:
                done = all(CK.JOBS[j]["done"] for j in (a, b))
            if done:
                break
            time.sleep(0.05)
        with CK.JOBS_LOCK:
            assert CK.JOBS[a]["done"] and CK.JOBS[b]["done"], "задания не завершились"
            assert CK.JOBS[a]["rc"] == 0 and CK.JOBS[b]["rc"] == 0, \
                (CK.JOBS[a]["rc"], CK.JOBS[b]["rc"])
    finally:
        (CK.command_by_name, CK.script_path, CK.write_runlog, CK.mark_running,
         CK.engine_fallback_note, CK.RUNNING) = saved
        with CK.JOBS_LOCK:
            CK.JOBS.clear()
            CK.JOBS.update(saved_jobs)
