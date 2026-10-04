"""Расписание панели — раздел «Cron»: маршруты и команды по времени, цепочками.

Задание расписания — цепочка шагов. Шаг — маршрут из «Быстрого старта» или отдельная
команда, в одном проекте или во всех. В назначенное время панель проходит цепочку сама, без
открытой страницы: маршрут ведёт `route_runner` по тем же правилам, что кнопка «Пройти».
Раздел заведён ради такого: «в 20:00 пройти „Обновить базу“ и „Починить базу“ во всех
проектах».

Правила, которые не очевидны из кода:

- Цепочка одна за раз. Две цепочки рядом — это два пишущих прогона над одной базой и
  удвоенная нагрузка на шлюзы моделей; второе подошедшее задание встаёт в очередь.
- Проект занят (в нём идёт команда панели или пишущий прогон агента) — шаг ждёт до двух
  часов, потом пропускается с пометкой. Ночной маршрут поверх ручной работы человека
  испортил бы обе.
- Время, пропущенное, пока панель не работала, не догоняется позже чем через `GRACE`: ночной
  разбор посреди рабочего дня, потому что панель подняли в десять утра, — сюрприз хуже
  пропуска. Пропуск пишется в историю.
- Панель перезапустили посреди цепочки (обновление кита) — новая панель продолжает её с
  прерванного шага, если перерыв короче `RESUME_WINDOW`. Маршрут при этом пропускает уже
  пройденные шаги — так же, как «Продолжить маршрут».
- Пишущая команда вне маршрута фиксируется в git сразу: незакоммиченное следующий маршрут
  смешал бы со своей работой, а чекпойнт агента записал бы его работой человека.

Хранится в домашней папке — `~/.aurora/cron-tasks.json`, история в `~/.aurora/cron-runs/`,
как и список корней проектов: расписание — свойство машины, а не кита и не проекта, и
переживает переустановку кита.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

import route_runner as RR

TICK_S = 20                       # как часто панель сверяется с расписанием
GRACE = timedelta(minutes=15)     # опоздание, которое ещё догоняем
RESUME_WINDOW = 30 * 60           # перерыв, после которого цепочку не продолжаем
BUSY_WAIT_S = 2 * 3600            # сколько шаг ждёт занятый проект
BUSY_POLL_S = 30
KEEP_RUNS = 60                    # столько прогонов цепочек храним в истории
LOG_KEEP = 400                    # строк журнала на шаг цепочки
MAX_STEPS = 50
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALL = "*"


def home() -> str:
    return os.path.join(os.path.expanduser("~"), ".aurora")


def tasks_file() -> str:
    return os.path.join(home(), "cron-tasks.json")


def state_file() -> str:
    return os.path.join(home(), "cron-state.json")


def runs_dir() -> str:
    return os.path.join(home(), "cron-runs")


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")


def load_tasks() -> list:
    return [t for t in _read_json(tasks_file(), {}).get("tasks", []) if isinstance(t, dict)]


def save_tasks(tasks: list) -> None:
    _write_json(tasks_file(), {"tasks": tasks})


# ------------------------------------------------------------------ задание

def clean_task(ck, raw: dict, projects: list) -> tuple:
    """Проверить задание из формы. → (задание, ошибка). Ошибка — код для каталога строк.

    Проверяем всё, что потом пойдёт в запуск: маршрут есть в сценариях, команда запускается
    панелью и флаги объявлены ею, проект — из найденных. Ночью исправлять опечатку некому.
    """
    if not isinstance(raw, dict):
        return None, "bad_task"
    name = " ".join(str(raw.get("name") or "").split())[:120]
    if not name:
        return None, "no_name"
    when = str(raw.get("time") or "")
    if not TIME_RE.match(when):
        return None, "bad_time"
    days = sorted({int(d) for d in raw.get("days") or [] if str(d).isdigit() and 1 <= int(d) <= 7})
    date = str(raw.get("date") or "")
    if date and not DATE_RE.match(date):
        return None, "bad_date"
    known = {p["path"] for p in projects}
    routes = {s["id"] for s in ck.scenarios()}
    steps = []
    for st in (raw.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(st, dict):
            return None, "bad_step"
        project = str(st.get("project") or "")
        if project != ALL and project not in known:
            return None, "bad_project"
        if st.get("kind") == "route":
            if st.get("route") not in routes:
                return None, "bad_route"
            steps.append({"kind": "route", "project": project, "route": st["route"],
                          "write": bool(st.get("write", True))})
        elif st.get("kind") == "command":
            row = ck.command_by_name(str(st.get("cmd") or ""))
            if not row or not row.get("runnable"):
                return None, "bad_command"
            args = [str(a) for a in (st.get("args") or []) if str(a).strip()]
            allowed = set(row["flags"]) | {"--apply", "--allow-dirty", "--force", "--json"}
            if any(a.startswith("--") and a.split("=")[0] not in allowed for a in args):
                return None, "bad_flag"
            steps.append({"kind": "command", "project": project, "cmd": row["cmd"],
                          "args": args})
        else:
            return None, "bad_step"
    if not steps:
        return None, "no_steps"
    tid = str(raw.get("id") or "")
    if not re.match(r"^[0-9a-f]{8}$", tid):
        tid = secrets.token_hex(4)
    return {"id": tid, "name": name, "enabled": bool(raw.get("enabled", True)),
            "time": when, "days": days, "date": date,
            "order": "steps" if raw.get("order") == "steps" else "projects",
            "on_fail": "stop" if raw.get("on_fail") == "stop" else "next",
            "lang": str(raw.get("lang") or ck.DEFAULT_LANG)[:8],
            "steps": steps}, ""


def slot(task: dict, day: datetime) -> datetime | None:
    """Время запуска задания в этот день — или None, если в этот день оно не идёт."""
    m = TIME_RE.match(task.get("time") or "")
    if not m:
        return None
    if task.get("date"):
        if day.strftime("%Y-%m-%d") != task["date"]:
            return None
    elif task.get("days") and day.isoweekday() not in task["days"]:
        return None
    return day.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)


def mark_past(task: dict, now: datetime | None = None) -> None:
    """Сегодняшнее время задания уже прошло — считать его отработанным.

    Иначе задание на 06:00, заведённое в десять утра, на первом же тике записалось бы в
    историю пропущенным, хотя пропускать было нечего: его тогда ещё не было.
    """
    now = now or datetime.now()
    s = slot(task, now)
    if s and s <= now:
        state = _read_json(state_file(), {})
        state.setdefault("fired", {})[task["id"]] = now_iso(s)
        _write_json(state_file(), state)


def next_run(task: dict, now: datetime | None = None) -> str:
    """Ближайший запуск — для экрана. Пусто: выключено или разовое уже прошло."""
    now = now or datetime.now()
    if not task.get("enabled"):
        return ""
    for d in range(0, 8):
        s = slot(task, now + timedelta(days=d))
        if s and s > now:
            return now_iso(s)
    return ""


def expand(task: dict, projects: list) -> list:
    """Цепочка → шаги по проектам. «Все проекты» раскрываются в порядке имён.

    Порядок «по проектам» (по умолчанию) доводит один проект до конца и берётся за
    следующий: ночь кончилась раньше цепочки — готовы целые проекты, а не по полмаршрута в
    каждом. «По шагам» — сначала первый шаг везде, потом второй.
    """
    names = {p["path"]: p["name"] for p in projects}

    def item(st: dict, path: str) -> dict:
        it = {"kind": st["kind"], "project": path,
              "project_name": names.get(path) or os.path.basename(path),
              "status": "pending", "note": "", "started": "", "finished": "",
              "jobs": [], "done": [], "log": []}
        if st["kind"] == "route":
            it.update(route=st["route"], write=st.get("write", True))
        else:
            it.update(cmd=st["cmd"], args=list(st.get("args") or []))
        if path not in names:
            it.update(status="skipped", note="no_project")
        return it

    every = [p["path"] for p in projects]
    out = []
    if task.get("order") == "steps":
        for st in task["steps"]:
            for path in (every if st["project"] == ALL else [st["project"]]):
                out.append(item(st, path))
    else:
        order = list(every) + [st["project"] for st in task["steps"]
                               if st["project"] != ALL and st["project"] not in every]
        for path in order:
            for st in task["steps"]:
                if st["project"] in (ALL, path):
                    out.append(item(st, path))
    return out


# ----------------------------------------------------------------- прогоны

def run_path(run_id: str) -> str:
    return os.path.join(runs_dir(), run_id + ".json")


def save_run(run: dict) -> None:
    run["beat"] = time.time()
    _write_json(run_path(run["id"]), run)


def list_runs(limit: int = 20) -> list:
    try:
        names = sorted((f for f in os.listdir(runs_dir()) if f.endswith(".json")), reverse=True)
    except OSError:
        return []
    out = []
    for f in names[:limit]:
        run = _read_json(os.path.join(runs_dir(), f), {})
        if run:
            out.append(run)
    return out


def trim_runs() -> None:
    try:
        names = sorted(f for f in os.listdir(runs_dir()) if f.endswith(".json"))
    except OSError:
        return
    for f in names[:-KEEP_RUNS]:
        try:
            os.remove(os.path.join(runs_dir(), f))
        except OSError:
            pass


def brief(run: dict) -> dict:
    """Прогон без журналов — для списка: журналы шагов тянутся отдельно, по одному."""
    items = [{k: v for k, v in it.items() if k != "log"} for it in run.get("items", [])]
    return {**{k: v for k, v in run.items() if k != "items"}, "items": items}


# --------------------------------------------------------------- планировщик

class Scheduler:
    """Живёт в процессе панели: тикает раз в `TICK_S`, ведёт одну цепочку за раз."""

    def __init__(self, ck, projects_fn):
        self.ck = ck
        self.projects_fn = projects_fn       # → список проектов панели (find_projects)
        self.lock = threading.RLock()
        self.queue = []                      # [(task_id, trigger, resume)]
        self.current = None                  # прогон, который идёт
        self.stop_event = threading.Event()
        self.thread = None

    # ---------------------------------------------------------- жизнь

    def start(self) -> None:
        self.recover()
        threading.Thread(target=self._loop, daemon=True, name="aurora-cron").start()

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — тик не должен убивать планировщик
                print(f"cron: тик не прошёл — {type(e).__name__}: {e}", flush=True)
            time.sleep(TICK_S)

    def recover(self) -> None:
        """Прогоны, оставшиеся «идущими» после смерти прежней панели: свежий — продолжить,
        старый — отметить прерванным."""
        for run in list_runs(KEEP_RUNS):
            if run.get("status") != "running":
                continue
            fresh = time.time() - float(run.get("beat") or 0) <= RESUME_WINDOW
            run["status"] = "interrupted"
            run["finished"] = now_iso()
            for it in run.get("items", []):
                if it.get("status") == "running":
                    it["status"], it["note"] = "interrupted", "panel_restart"
            save_run(run)
            if fresh and run.get("task") and not run.get("resumed_by"):
                self.queue.append((run["task"], "resume", run["id"]))

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        tasks = load_tasks()
        fired = _read_json(state_file(), {}).get("fired", {})
        changed = False
        for task in tasks:
            if not task.get("enabled"):
                continue
            s = slot(task, now)
            if not s or s > now or fired.get(task["id"]) == now_iso(s):
                continue
            fired[task["id"]] = now_iso(s)
            changed = True
            if now - s <= GRACE:
                self.enqueue(task["id"], "schedule")
            else:
                self._missed(task, s)
            if task.get("date"):
                task["enabled"] = False      # разовое отработало — выключаем
                save_tasks(tasks)
        if changed:
            _write_json(state_file(), {"fired": fired})
        self._next()

    def _missed(self, task: dict, s: datetime) -> None:
        run = {"id": datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
               "task": task["id"], "name": task["name"], "trigger": "schedule",
               "status": "missed", "slot": now_iso(s), "started": now_iso(),
               "finished": now_iso(), "items": []}
        save_run(run)
        trim_runs()

    def enqueue(self, task_id: str, trigger: str = "manual", resume: str = "") -> dict:
        with self.lock:
            busy = [q[0] for q in self.queue] + ([self.current["task"]] if self.current else [])
            if task_id in busy:
                return {"ok": False, "error": "queued"}
            self.queue.append((task_id, trigger, resume))
        self._next()
        return {"ok": True}

    def _next(self) -> None:
        with self.lock:
            if self.current or not self.queue:
                return
            task_id, trigger, resume = self.queue.pop(0)
            task = next((t for t in load_tasks() if t["id"] == task_id), None)
            if not task:
                return
            run = self._new_run(task, trigger, resume)
            self.current = run
            self.stop_event = threading.Event()
        threading.Thread(target=self._run, args=(task, run), daemon=True,
                         name="aurora-cron-run").start()

    def stop(self) -> dict:
        with self.lock:
            self.queue.clear()
            if not self.current:
                return {"ok": False, "error": "nothing"}
            self.stop_event.set()
        return {"ok": True}

    # -------------------------------------------------------- прогон

    def _new_run(self, task: dict, trigger: str, resume: str) -> dict:
        run = {"id": datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
               "task": task["id"], "name": task["name"], "trigger": trigger,
               "status": "running", "started": now_iso(), "finished": "",
               "on_fail": task.get("on_fail", "next"), "items": []}
        prev = _read_json(run_path(resume), {}) if resume else {}
        if prev.get("items"):
            # Продолжение: пройденное остаётся пройденным, прерванный маршрут пропустит
            # шаги, которые успел закончить.
            run["resumes"] = resume
            prev["resumed_by"] = run["id"]
            _write_json(run_path(resume), prev)
            for it in prev["items"]:
                it = dict(it)
                if it["status"] in ("pending", "interrupted", "running"):
                    it.update(status="pending", note="", log=[])
                run["items"].append(it)
        else:
            run["items"] = expand(task, self.projects_fn())
        save_run(run)
        trim_runs()
        return run

    def _busy(self, project: str) -> bool:
        act = self.ck.project_activity(project)
        return bool(act.get("running")) or bool((act.get("agent") or {}).get("alive"))

    def _wait_free(self, project: str) -> bool:
        end = time.time() + BUSY_WAIT_S
        while self._busy(project):
            if self.stop_event.is_set() or time.time() > end:
                return False
            time.sleep(BUSY_POLL_S)
        return True

    def _run(self, task: dict, run: dict) -> None:
        ck = self.ck
        mark = "cron-" + run["id"]
        ck.mark_running(mark, "cron: " + task["name"], "", True)
        lang = task.get("lang") or ck.DEFAULT_LANG
        halted = ""
        try:
            for it in run["items"]:
                if it["status"] != "pending":
                    continue
                if halted:
                    it.update(status="skipped", note=halted)
                    continue
                if self.stop_event.is_set():
                    halted = "stopped"
                    it.update(status="skipped", note="stopped")
                    continue
                self._item(task, run, it, lang)
                save_run(run)
                if it["status"] == "stopped":
                    halted = "stopped"
                elif it["status"] in ("failed", "stall", "offline") \
                        and task.get("on_fail") == "stop":
                    halted = "chain_stopped"
            st = [it["status"] for it in run["items"]]
            run["status"] = ("stopped" if halted == "stopped"
                             else "passed" if all(s in ("passed",) for s in st)
                             else "failed")
        except Exception as e:  # noqa: BLE001 — прогон должен закончиться записью, а не тишиной
            run["status"], run["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            run["finished"] = now_iso()
            save_run(run)
            ck.mark_running(mark, "", "", False)
            with self.lock:
                self.current = None
            self._next()

    def _item(self, task: dict, run: dict, it: dict, lang: str) -> None:
        ck = self.ck

        def log(text: str) -> None:
            it["log"].append(text)
            if len(it["log"]) > LOG_KEEP:
                del it["log"][:-LOG_KEEP]
            # Экран читает прогон с диска: запись атомарная, а читать живой словарь из
            # другого потока — значит ловить его посреди изменения. Чаще раза в две секунды
            # писать незачем.
            if time.time() - run.get("beat", 0) > 2:
                save_run(run)

        def on_job(job_id: str, cmd: str) -> None:
            it["job"] = job_id
            it["jobs"].append({"cmd": cmd, "job": job_id})
            save_run(run)

        def on_step(sig: str, rc: int) -> None:
            if it["jobs"]:
                it["jobs"][-1]["rc"] = rc
            if rc == 0 and sig not in it["done"]:
                it["done"].append(sig)
            save_run(run)

        it.update(status="running", started=now_iso(), note="")
        save_run(run)
        project = it["project"]
        if not self._wait_free(project):
            it.update(status="stopped" if self.stop_event.is_set() else "skipped",
                      note="" if self.stop_event.is_set() else "busy", finished=now_iso())
            return
        if it["kind"] == "route":
            sc = RR.scenario(ck, it["route"])
            if not sc:
                it.update(status="failed", note="no_route", finished=now_iso())
                return
            resume = {"skipSigs": it["done"]} if it.get("done") else None
            res = RR.RouteRun(ck, project, sc, it.get("write", True), lang=lang, log=log,
                              stop=self.stop_event, on_job=on_job, on_step=on_step,
                              resume=resume).run()
            it["title"] = sc["title"]
            it["run_id"] = res.get("run_id", "")
            status = {"passed": "passed", "stall": "stall", "offline": "offline",
                      "stopped": "stopped"}.get(res["reason"], "failed")
            it.update(status=status, failed=res.get("failed", ""), note=res.get("note", ""),
                      finished=now_iso())
            return
        gap = ck.version_gap(project)
        if gap:
            log(gap)
            it.update(status="failed", note="engine_behind", finished=now_iso())
            return
        log("▸ " + (it["cmd"] + " " + " ".join(it["args"])).strip())
        res = RR.run_job(ck, project, it["cmd"], it["args"], stop=self.stop_event,
                         on_job=on_job)
        for line in res["lines"][-LOG_KEEP:]:
            log(line)
        on_step((it["cmd"] + " " + " ".join(it["args"])).strip(), res["rc"])
        rc = res["rc"]
        it["rc"] = rc
        if res.get("stopped"):
            it.update(status="stopped", finished=now_iso())
            return
        if rc >= 2 or rc < 0:
            it.update(status="failed", note=res.get("refused") or "", finished=now_iso())
            return
        it.update(status="passed", note="found" if rc == 1 else "", finished=now_iso())
        if "--apply" in it["args"] and RR.job_project(ck, project, it["cmd"]) == project:
            t = RR.Texts(ck, lang)
            saved = ck.git_commit(project, t("cron.commit", name=task["name"], cmd=(
                it["cmd"] + " " + " ".join(it["args"])).strip()), None, True)
            if saved.get("ok"):
                it["commit"] = saved["commit"]

    # ------------------------------------------------------- для экрана

    def state(self) -> dict:
        with self.lock:
            cur = brief(self.current) if self.current else None
            queue = [q[0] for q in self.queue]
        tasks = load_tasks()
        runs = list_runs(30)
        last = {}
        for r in runs:
            last.setdefault(r.get("task"), {"id": r["id"], "status": r.get("status"),
                                            "started": r.get("started"),
                                            "finished": r.get("finished")})
        for t in tasks:
            t["next"] = next_run(t)
            t["last"] = last.get(t["id"])
        return {"tasks": tasks, "current": cur, "queue": queue,
                "runs": [brief(r) for r in runs], "now": now_iso()}
