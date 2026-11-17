"""Проверки движка Aurora, часть 240: связь не снимает шаг, а приостанавливает его (1.231.7).

Каркас и помощники — tests/harness.py. Обрыв VPN посреди длинного прогона раньше снимал
процесс шага, и маршрут начинал шаг заново. Теперь сторож приостанавливает дерево
(`SIGSTOP`) и продолжает (`SIGCONT`) с того же места; снимает только если после
продолжения шаг за `GRACE` так и не ожил.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

from harness import KIT, test  # noqa: F401

import sys

for _p in (str(KIT / "cockpit"), str(KIT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _LiveProc:
    pid = 4242

    def poll(self):
        return None


def _dog(net, clock, pulse=None):
    import watchdog as WD

    class Ck:
        JOBS_LOCK = threading.Lock()
        JOBS = {}

        @staticmethod
        def job_pulse(job):
            return {"state": pulse or "quiet"}

    dog = WD.Watchdog(Ck, probe_fn=lambda project: net,
                      clock=lambda: clock["t"])
    return WD, dog, Ck


@test
def test_watchdog_pauses_and_resumes_instead_of_killing(_t):
    """Связь пропала — `suspend_tree` один раз, шаг не снят; связь вернулась — `resume_tree`
    один раз и работа продолжается."""
    import watchdog as WD
    clock = {"t": 10_000.0}
    net = {"ok": False, "down": ["модель (Шлюз)"]}
    WD, dog, Ck = _dog(net, clock)
    job = {"id": "j1", "project": "/p", "out": [], "started": clock["t"] - WD.QUIET - 5,
           "last_out": clock["t"] - WD.QUIET - 5, "parent": "route-1", "proc": _LiveProc(),
           "pausable": True}
    Ck.JOBS["j1"] = job
    seen = []
    with patch.object(WD, "suspend_tree", lambda pid: seen.append(("stop", pid)) or True), \
            patch.object(WD, "resume_tree", lambda pid: seen.append(("cont", pid)) or True), \
            patch.object(WD, "stop_tree", lambda pid: seen.append(("kill", pid))):
        dog.tick()
        assert job.get("paused") is True, job
        assert job.get("pause_why") == "модель (Шлюз)", job
        assert seen == [("stop", 4242)], seen
        assert not job.get("hung"), "шаг снят вместо паузы"
        assert any("приостановил" in x for x in job["out"]), job["out"]

        dog.tick()
        assert seen == [("stop", 4242)], f"повторная пауза без нового обрыва: {seen}"

        net.update(ok=True, down=[])
        dog.probes.clear()
        clock["t"] += 30
        dog.tick()
        assert job.get("paused") is False, job
        assert seen == [("stop", 4242), ("cont", 4242)], seen
        assert not job.get("hung"), "шаг снят сразу после продолжения"
        assert any("продолжаю" in x for x in job["out"]), job["out"]


@test
def test_watchdog_kills_only_after_a_resumed_step_stays_silent(_t):
    """После `SIGCONT` шаг молчит весь `GRACE` — только тогда снимаем и отдаём маршруту."""
    import watchdog as WD
    clock = {"t": 10_000.0}
    net = {"ok": False, "down": ["модель"]}
    WD, dog, Ck = _dog(net, clock)
    job = {"id": "j2", "project": "/p", "out": [], "started": clock["t"] - WD.QUIET - 5,
           "last_out": clock["t"] - WD.QUIET - 5, "parent": "route-1", "proc": _LiveProc()}
    Ck.JOBS["j2"] = job
    killed = []
    with patch.object(WD, "suspend_tree", lambda pid: True), \
            patch.object(WD, "resume_tree", lambda pid: True), \
            patch.object(WD, "stop_tree", lambda pid: killed.append(pid)):
        dog.tick()
        net.update(ok=True, down=[])
        dog.probes.clear()
        clock["t"] += 20
        dog.tick()
        assert not killed, "шаг снят до истечения GRACE"
        clock["t"] += WD.GRACE + 5
        dog.tick()
    assert killed == [4242], killed
    assert "пропадала и вернулась" in job["hung"], job


@test
def test_watchdog_does_not_pause_a_step_that_waits_for_a_model(_t):
    """Долгий ответ модели при живой связи — работа, а не повод для паузы."""
    import watchdog as WD
    clock = {"t": 10_000.0}
    net = {"ok": True, "down": []}
    WD, dog, Ck = _dog(net, clock, pulse="wait")
    job = {"id": "j3", "project": "/p", "out": [], "started": 0,
           "last_out": clock["t"] - WD.HARD - 60, "parent": "route-1", "proc": _LiveProc()}
    Ck.JOBS["j3"] = job
    with patch.object(WD, "suspend_tree", lambda pid: (_ for _ in ()).throw(
            AssertionError("шаг, ждущий модель, приостановили"))):
        dog.tick()
    assert not job.get("paused") and not job.get("hung")


@test
def test_stop_tree_wakes_a_paused_process_before_stopping(_t):
    """Приостановленному SIGTERM не дойдёт, пока его не разбудить: остановка сперва `SIGCONT`."""
    import watchdog as WD
    seen = []
    with patch.object(WD, "resume_tree", lambda pid: seen.append(("cont", pid)) or True), \
            patch.object(WD, "kill_tree", lambda pid, grace=0.0: seen.append(("kill", pid, grace))):
        WD.stop_tree(777)
    assert seen == [("cont", 777), ("kill", 777, WD.STOP_GRACE)], seen
