"""Автоматика Git проектов: события панели и тик раз в минуту (фреймворк «Аврора»).

Что делать, решает настройка проекта (`git_sync`, раздел «Git»): обновлять при открытии,
по расписанию, перед маршрутом; отправлять после маршрута, после фиксации, по расписанию.
Сама работа — обычные задания панели (`git:update --auto=…`, `git:push --auto=…`): они видны
в «Консоли» и в истории запусков, а итог каждого пишется в журнал проекта. Тихих отказов
нет: упавшая автоматика ставит отметку на пункт меню «Git», пока следующая не пройдёт.

Проект, в котором идёт работа (маршрут, команда, агент), автоматика не трогает: обновление
посреди прогона подменило бы файлы под агентом. Отложенное «при открытии» выполняется,
когда работа закончится; остальное — на следующем тике.
"""
from __future__ import annotations

import os
import threading
import time

import git_sync as GS

TICK_S = 60


class GitAuto:
    """Живёт в процессе панели рядом с расписанием."""

    def __init__(self, ck, projects_fn, clock=None):
        self.ck = ck
        self.projects_fn = projects_fn         # → список проектов панели (find_projects)
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.pending_open: set = set()         # «при открытии», отложенное занятостью
        self.backoff_wait: dict = {}           # (проект, действие) → (начало, папка прогона)
        self.started = False

    def start(self) -> None:
        self.started = True
        threading.Thread(target=self._loop, daemon=True, name="aurora-git-auto").start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — тик не должен убивать автоматику
                print(f"git-auto: тик не прошёл — {type(e).__name__}: {e}", flush=True)
            time.sleep(TICK_S)

    # ---------------------------------------------------------- решения

    def busy(self, project: str) -> bool:
        act = self.ck.project_activity(project)
        return bool(act.get("running")) or bool((act.get("agent") or {}).get("alive"))

    @staticmethod
    def wanted(s: dict | None, action: str, trigger: str) -> bool:
        group = s.get("auto_update" if action == "update" else "auto_push") if s else None
        return bool(group and group["enabled"] and trigger in group["on"])

    def fire(self, project: str, action: str, trigger: str) -> str:
        """Запустить действие заданием панели. → номер задания или пусто."""
        GS.write_state(project, **{f"auto_{action}_at": time.time()})
        try:
            job_id = self.ck.start_job(project, f"git:{action}", [f"--auto={trigger}"])
            if job_id:
                lock = getattr(self.ck, "JOBS_LOCK", None)
                jobs = getattr(self.ck, "JOBS", {})
                if lock is not None:
                    with lock:
                        job = jobs.get(job_id) or {}
                else:
                    job = jobs.get(job_id) or {}
                run_dir = str(job.get("run_dir") or "")
                if run_dir:
                    GS.write_state(project, **{f"auto_{action}_run_dir": run_dir})
            return job_id
        except ValueError as e:
            GS.record(project, GS.result(action, False, prob=GS.problem("unknown", str(e))),
                      trigger)
            return ""

    def event(self, project: str, event: str) -> dict:
        """Событие страницы. Сейчас одно: проект открыли."""
        if event != "open":
            return {"ok": False, "error": "unknown_event"}
        s = GS.load_saved(project)
        if not self.wanted(s, "update", "open"):
            return {"ok": True, "fired": False}
        st = GS.read_state(project)
        if time.time() - float(st.get("open_at") or 0) < GS.OPEN_EVERY_S:
            return {"ok": True, "fired": False, "why": "recent"}
        GS.write_state(project, open_at=time.time())
        if self.busy(project):
            with self.lock:
                self.pending_open.add(project)
            GS.record(project, GS.result("update", True, "отложено: в проекте идёт работа — "
                                                         "обновлю, когда она закончится",
                                         skipped="busy"), "open")
            return {"ok": True, "fired": False, "why": "busy"}
        return {"ok": True, "fired": True, "job": self.fire(project, "update", "open")}

    def _due(self, st: dict, key: str, every_min: int) -> bool:
        return time.time() - float(st.get(key) or 0) >= every_min * 60

    def _backoff(self, project: str, st: dict, action: str) -> bool:
        """Упавшая автоматика ждёт: иначе отказ входа повторялся бы каждую минуту."""
        now = self.clock()
        fail_at = float(st.get(f"auto_{action}_fail_at") or 0)
        key = (project, action)
        if now - fail_at < GS.FAIL_BACKOFF_S:
            if key not in self.backoff_wait:
                self.backoff_wait[key] = (
                    fail_at, str(st.get(f"auto_{action}_run_dir") or ""))
            return True
        pending = self.backoff_wait.pop(key, None)
        if pending and pending[1]:
            start = max(0.0, float(pending[0]))
            end = min(now, start + GS.FAIL_BACKOFF_S)
            if end > start:
                self._wait_span(pending[1], start, end, action)
        return False

    @staticmethod
    def _wait_span(run_dir: str, start: float, end: float, action: str) -> None:
        """Отложенное действие — ожидание `other`, а не работа проекта."""
        try:
            import timing as T
            T.Tracer(run_dir=run_dir, run_id=os.path.basename(
                os.path.normpath(run_dir))).note_span(
                "wait", start=start, end=end, service="git_remote",
                wait_class="other", cause="backoff", reason=f"git_{action}_backoff",
                dur_ms=max(0.0, end - start) * 1000)
        except Exception:  # noqa: BLE001 — замер не должен ломать автоматику
            pass

    def tick(self, now: float | None = None) -> list:
        """Один проход по проектам. → [(проект, действие, событие)] — что запущено."""
        fired = []
        for p in self.projects_fn() or []:
            path = p["path"] if isinstance(p, dict) else p
            try:
                fired += self._project(path)
            except Exception as e:  # noqa: BLE001 — один проект не роняет остальные
                print(f"git-auto: {path}: {type(e).__name__}: {e}", flush=True)
        return fired

    def _project(self, path: str) -> list:
        s = GS.load_saved(path)
        if not s or not (s["auto_update"]["enabled"] or s["auto_push"]["enabled"]):
            return []
        st = GS.read_state(path)
        busy = self.busy(path)
        out = []
        if busy:
            # Следим за HEAD и во время работы: иначе «после фиксации» сработало бы на
            # первом же свободном тике, без выдержки после последней фиксации маршрута.
            self._watch_head(path, st)
            return out
        with self.lock:
            opened = path in self.pending_open
            self.pending_open.discard(path)
        if opened and self.wanted(s, "update", "open"):
            self.fire(path, "update", "open")
            return [(path, "update", "open")]
        up = s["auto_update"]
        if self.wanted(s, "update", "interval") and self._due(st, "auto_update_at", up["every_min"]) \
                and not self._backoff(path, st, "update"):
            self.fire(path, "update", "interval")
            return [(path, "update", "interval")]
        ps = s["auto_push"]
        if self.wanted(s, "push", "interval") and self._due(st, "auto_push_at", ps["every_min"]) \
                and not self._backoff(path, st, "push"):
            self.fire(path, "push", "interval")
            return [(path, "push", "interval")]
        if self.wanted(s, "push", "after_commit"):
            head = self._watch_head(path, st)
            settled = time.time() - float(st.get("seen_at") or time.time()) >= GS.DEBOUNCE_S
            retry = time.time() - float(st.get("auto_push_at") or 0) >= GS.FAIL_BACKOFF_S
            if head and head == st.get("seen_head") and head != st.get("pushed_head") and settled \
                    and (head != st.get("push_tried_head") or retry):
                GS.write_state(path, push_tried_head=head)
                self.fire(path, "push", "after_commit")
                out.append((path, "push", "after_commit"))
        return out

    def _watch_head(self, path: str, st: dict) -> str:
        head = GS.git_ok(path, "rev-parse", "HEAD")
        if head and head != st.get("seen_head"):
            GS.write_state(path, seen_head=head, seen_at=time.time())
            st.update(seen_head=head, seen_at=time.time())
        return head
