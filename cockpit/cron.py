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
- Проект — единица цепочки. Шаг проекта провалился (команда упала, шлюзы не отвечают,
  маршрут не сдвинул базу ни на коммит) — остальные шаги ЭТОГО проекта откладываются
  («Починить» после несостоявшегося «Обновить» бессмысленна), цепочка идёт к следующему
  проекту, а в конце возвращается к отложенным один раз и продолжает с места остановки.
  Застой на хвосте, когда база за маршрут менялась, — «частично», не провал: проект
  обновлён, следующие его шаги идут. Срока у шага нет: долгое размышление модели — не
  повод бросать работу, которую потом придётся начинать заново.
- Проекты идут в том порядке, в каком впервые встречаются в шагах задания; шаг «все
  проекты» ставит их в порядке панели там, где стоит сам.
- Время, пропущенное, пока панель не работала, не догоняется позже чем через `GRACE`: ночной
  разбор посреди рабочего дня, потому что панель подняли в десять утра, — сюрприз хуже
  пропуска. Пропуск пишется в историю.
- Панель перезапустили посреди цепочки (обновление кита) — новая панель продолжает её с
  прерванного шага, если перерыв короче `RESUME_WINDOW`. Маршрут при этом пропускает уже
  пройденные шаги — так же, как «Продолжить маршрут».
- Пишущая команда вне маршрута фиксируется в git сразу: незакоммиченное следующий маршрут
  смешал бы со своей работой, а чекпойнт агента записал бы его работой человека.

Боты проектов с расписанием (`bots/*.md`, поле `cron`) — тоже задания расписания: их
источник — файл бота, и правят их в разделе «Боты». Планировщик читает их на каждом тике,
запускает `bot:run` по выражению cron и держит в истории наравне с остальными.

Хранится в домашней папке — `~/.aurora/cron-tasks.json`, история в `~/.aurora/cron-runs/`,
как и список корней проектов: расписание — свойство машины, а не кита и не проекта, и
переживает переустановку кита.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta

import route_runner as RR

try:
    import bots as BOTS               # движок: боты проектов (scripts/bots.py)
except ImportError:                   # кит без ботов — расписание работает и так
    BOTS = None

TICK_S = 20                       # как часто панель сверяется с расписанием
GRACE = timedelta(minutes=15)     # опоздание, которое ещё догоняем
RESUME_WINDOW = 30 * 60           # перерыв, после которого цепочку не продолжаем
BUSY_WAIT_S = 2 * 3600            # сколько шаг ждёт занятый проект
BUSY_POLL_S = 30
KEEP_RUNS = 60                    # столько прогонов цепочек храним в истории
OWNER_STALE = 5 * 60              # владелец расписания без отметки дольше — считается ушедшим
STREAM_KEEP = 5000                # строк потока идущей цепочки для Консоли (1.167.0)
FINAL_SAVE_PAUSE = 1.0            # пауза перед второй попыткой записать итог прогона, с
LOG_KEEP = 400                    # строк журнала на шаг цепочки
MAX_STEPS = 50
# Итоги шага, после которых проект откладывается: дальше его шаги смысла не имеют.
# gated — ворота качества не пропустили маршрут (вердикт, не сбой): к проекту вернёмся в конце.
BAD = ("failed", "stall", "offline", "gated")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALL = "*"
NET_POLICIES = ("fail_fast", "partial", "queue", "partial_queue")      # данные движка: `connectivity.POLICIES`


def home() -> str:
    return os.path.join(os.path.expanduser("~"), ".aurora")


def tasks_file() -> str:
    return os.path.join(home(), "cron-tasks.json")


def state_file() -> str:
    return os.path.join(home(), "cron-state.json")


def runs_dir() -> str:
    return os.path.join(home(), "cron-runs")


def busy_line(act: dict) -> str:
    """Чем занят проект, — строкой журнала пункта цепочки."""
    wait = f"жду, пока освободится: не дольше {BUSY_WAIT_S // 3600} ч, проверяю раз в {BUSY_POLL_S} с"
    running = act.get("running") or []
    if running:
        first = running[0] if isinstance(running[0], dict) else {}
        return f"⏳ проект занят: в панели идёт {first.get('cmd') or 'команда'} — {wait}"
    ag = act.get("agent") or {}
    since = str(ag.get("since") or "")
    try:
        since = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        pass
    return (f"⏳ проект занят: идёт прогон движка agent:{ag.get('task') or '?'} (процесс "
            f"{ag.get('pid') or '?'}, с {since or '?'}), запущенный не этой цепочкой — например, "
            f"шаг прежней цепочки, переживший перезапуск панели, или команда из терминала. "
            f"{wait[0].upper() + wait[1:]}")


def owner_file() -> str:
    return os.path.join(home(), "cron-owner.json")


def _alive(pid: int) -> bool:
    # Общая проверка движка: на Windows `os.kill(pid, 0)` убивает процесс, а не проверяет,
    # поэтому своей запасной копии на `os.kill` здесь нет.
    from aurora_common import pid_alive
    return pid_alive(pid)


def _read_json(path: str, default):
    """Состояние Cron. Повреждённый файл не молча «пуст»: ядро надёжности переносит его в `.corrupt` и говорит об
    этом (расписание и история не затираются следующей записью без следа)."""
    import aurora_fs
    return aurora_fs.read_state(path, default)


def _write_json(path: str, data) -> None:
    """Записать состояние Cron целиком или никак: уникальный временный файл рядом, `fsync`, подмена с повтором на
    Windows (`aurora_fs.write_atomic`)."""
    import aurora_fs
    aurora_fs.write_json(path, data)


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
    routes = {s["id"] for s in RR.all_routes(ck)}
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
        elif st.get("kind") == "bot":
            # Бот проекта (1.163.0): «*» — все боты проекта; с «все проекты» — все боты всех
            # проектов по очереди. Конкретный бот проверяется сейчас: ночью некому.
            bot = str(st.get("bot") or "*").replace("\\", "/")
            if bot != "*":
                if project == ALL or not re.match(r"^bots/[^/]+\.md$", bot) \
                        or not os.path.isfile(os.path.join(project, bot)):
                    return None, "bad_bot"
            steps.append({"kind": "bot", "project": project, "bot": bot})
        else:
            return None, "bad_step"
    if not steps:
        return None, "no_steps"
    tid = str(raw.get("id") or "")
    if not re.match(r"^[0-9a-f]{8}$", tid):
        tid = secrets.token_hex(4)
    # Политика прогона без связи (проверка соединений): пусто — по настройке кита (по умолчанию `partial_queue`).
    net = str(raw.get("net") or "")
    if net and net not in NET_POLICIES:
        return None, "bad_net"
    return {"id": tid, "name": name, "enabled": bool(raw.get("enabled", True)), "net": net,
            "time": when, "days": days, "date": date,
            "order": "steps" if raw.get("order") == "steps" else "projects",
            "on_fail": "stop" if raw.get("on_fail") == "stop" else "next",
            "lang": str(raw.get("lang") or ck.DEFAULT_LANG)[:8],
            "steps": steps}, ""


def bot_tasks(projects: list, lang: str = "ru") -> list:
    """Боты проектов с расписанием — задания расписания. Источник — файлы `bots/*.md`."""
    if BOTS is None:
        return []
    out = []
    for p in projects or []:
        try:
            rows = BOTS.list_bots(p["path"], with_checks=False)
        except Exception:  # noqa: BLE001 — битый бот одного проекта не гасит расписание
            continue
        for b in rows:
            meta = b["meta"]
            if not meta.get("cron"):
                continue
            _spec, err = BOTS.parse_cron(meta["cron"])
            out.append({
                "id": BOTS.bot_id(p["path"], b["file"]), "source": "bot", "name": meta["name"],
                "bot": b["file"], "project": p["path"], "project_name": p.get("name", ""),
                "cron": meta["cron"], "cron_error": err,
                "enabled": bool(meta.get("enabled")) and not err,
                "time": "", "days": [], "date": "", "order": "projects", "on_fail": "next",
                "lang": lang, "bot_last": b.get("last") or {},
                "steps": [{"kind": "command", "project": p["path"], "cmd": "bot:run",
                           "args": [f"--bot={b['file']}", "--trigger=cron"]}]})
    return out


def bot_slot(task: dict, now: datetime) -> datetime | None:
    """Последняя минута расписания бота не позже `now` в пределах `GRACE` — или None.

    Тик раз в 20 секунд, но тик может и запоздать (панель была занята), а выражение cron
    срабатывает на одной минуте: смотрим назад на опоздание, которое ещё догоняем."""
    if BOTS is None or not task.get("enabled"):
        return None
    spec, err = BOTS.parse_cron(task.get("cron") or "")
    if err:
        return None
    t = now.replace(second=0, microsecond=0)
    edge = now - GRACE
    while t >= edge:
        if BOTS.matches(spec, t):
            return t
        t -= timedelta(minutes=1)
    return None


def bot_next(task: dict, now: datetime | None = None) -> str:
    if BOTS is None or not task.get("enabled"):
        return ""
    spec, err = BOTS.parse_cron(task.get("cron") or "")
    runs = BOTS.next_runs(spec, now or datetime.now(), 1) if not err else []
    return now_iso(runs[0]) if runs else ""


def mark_bot_past(task: dict, now: datetime | None = None) -> None:
    """Бота только что включили или поменяли расписание — прошедшую минуту не догоняем:
    иначе бот на 9:00, сохранённый в 9:05, сработал бы сразу же."""
    s = bot_slot(task, now or datetime.now())
    if s:
        state = _read_json(state_file(), {})
        state.setdefault("fired", {})[task["id"]] = now_iso(s)
        _write_json(state_file(), state)


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
        elif st["kind"] == "bot":
            it.update(cmd="bot:run", args=[f"--bot={st['bot']}", "--trigger=cron"],
                      bot=st["bot"])
        else:
            it.update(cmd=st["cmd"], args=list(st.get("args") or []))
        if path not in names:
            it.update(status="skipped", note="no_project")
        return it

    every = [p["path"] for p in projects]

    def bots_of(path: str) -> list:
        """Боты проекта для шага «все боты»: файлы `bots/*.md`, включённые."""
        if BOTS is None:
            return []
        try:
            return [b["file"] for b in BOTS.list_bots(path, with_checks=False)
                    if b["meta"].get("enabled")]
        except Exception:  # noqa: BLE001 — битый бот одного проекта не гасит цепочку
            return []

    def open_bots(steps: list, path: str) -> list:
        out = []
        for st in steps:
            if st["kind"] == "bot" and st.get("bot") == "*":
                out += [dict(st, bot=f) for f in bots_of(path)]
            else:
                out.append(st)
        return out

    out = []
    if task.get("order") == "steps":
        for st in task["steps"]:
            for path in (every if st["project"] == ALL else [st["project"]]):
                out += [item(one, path) for one in open_bots([st], path)]
    else:
        # Порядок первого появления в шагах; шаг «все проекты» вставляет их в порядке панели
        # там, где стоит сам.
        order = []
        for st in task["steps"]:
            for p in (every if st["project"] == ALL else [st["project"]]):
                if p not in order:
                    order.append(p)
        for path in order:
            for st in task["steps"]:
                if st["project"] in (ALL, path):
                    out += [item(one, path) for one in open_bots([st], path)]
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
        self.run_thread = None               # поток идущего прогона: сторож «заклинило» смотрит на него
        self.stop_event = threading.Event()
        self.thread = None
        # Расписание и цепочки ведёт одна панель на машину: история и задания лежат в
        # `~/.aurora`, общие для всех панелей. Вторая панель (другой кит, другой порт,
        # двойной запуск) считала идущую цепочку брошенной и продолжала её параллельно —
        # 07.10.2026 так встал маршрут PRJ-C: чужой `agent:distill` занял замок базы перед
        # шагом `agent:twins`. Такая панель только показывает расписание.
        self.passive = False
        self.owner = {}
        # Поток идущей цепочки для Консоли: заголовки пунктов, вывод шагов, «жду», итоги — по
        # всем проектам подряд, с номером строки. Без него Консоль во время цепочки была
        # пустой: она показывала задания выбранного проекта, а цепочка шла по другим, и
        # между шагами заданий нет вовсе (1.167.0).
        self.stream: list = []
        self.stream_base = 0              # номер первой строки в `stream`
        self.stream_run = ""
        self.claimed = False                 # отмечается в `cron-owner.json` только взявшая

    # ---------------------------------------------------------- жизнь

    def start(self) -> None:
        if self.claim():
            self.recover()
        threading.Thread(target=self._loop, daemon=True, name="aurora-cron").start()

    def claim(self) -> bool:
        """Стать панелью, которая ведёт расписание. Занято живой панелью, отмечавшейся
        недавно, — эта остаётся наблюдателем. Отметка нужна, потому что номер процесса
        после перезагрузки машины может достаться чужой программе."""
        cur = _read_json(owner_file(), {})
        pid = int(cur.get("pid") or 0)
        fresh = time.time() - float(cur.get("beat") or 0) <= OWNER_STALE
        if pid and pid != os.getpid() and fresh and _alive(pid):
            self.passive, self.owner = True, cur
            return False
        self.passive, self.owner, self.claimed = False, {}, True
        self._beat()
        return True

    def _beat(self) -> None:
        cur = _read_json(owner_file(), {})
        _write_json(owner_file(), {"pid": os.getpid(), "beat": time.time(),
                                   "since": cur.get("since") if cur.get("pid") == os.getpid()
                                   else now_iso()})

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
            owner = int(run.get("panel") or 0)
            if owner and owner != os.getpid() and _alive(owner):
                continue                     # цепочку ведёт живая панель — не наша
            fresh = time.time() - float(run.get("beat") or 0) <= RESUME_WINDOW
            run["status"] = "interrupted"
            run["finished"] = now_iso()
            for it in run.get("items", []):
                if it.get("status") == "running":
                    it["status"], it["note"] = "interrupted", "panel_restart"
            self._save(run)
            if fresh and run.get("task") and not run.get("resumed_by"):
                self.queue.append((run["task"], "resume", run["id"]))

    def tick(self, now: datetime | None = None) -> None:
        if self.passive:
            if not self.claim():
                return                       # расписание ведёт другая панель
            self.recover()                   # прежний владелец ушёл — его цепочки наши
        elif self.claimed:
            self._beat()
        self._unwedge()
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
                if not self.enqueue(task["id"], "schedule").get("ok"):
                    self._missed(task, s, "already_running")
            else:
                self._missed(task, s)
            if task.get("date"):
                task["enabled"] = False      # разовое отработало — выключаем
                save_tasks(tasks)
        for task in bot_tasks(self.projects_fn(), self.ck.DEFAULT_LANG):
            s = bot_slot(task, now)
            if s and fired.get(task["id"]) != now_iso(s):
                fired[task["id"]] = now_iso(s)
                changed = True
                self.enqueue(task["id"], "schedule")
        if changed:
            _write_json(state_file(), {"fired": fired})
        self._next()

    def _missed(self, task: dict, s: datetime, why: str = "") -> None:
        """Время задания прошло без прогона: панель не работала или то же задание ещё шло
        (`already_running`) — второй его прогон в очередь не встаёт, но в историю пишется."""
        run = {"id": datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
               "task": task["id"], "name": task["name"], "trigger": "schedule",
               "status": "missed", "slot": now_iso(s), "started": now_iso(),
               "finished": now_iso(), "items": [], "note": why}
        self._save(run)                      # сбой записи не должен оборвать тик до `fired` и `_next`
        trim_runs()

    def enqueue(self, task_id: str, trigger: str = "manual", resume: str = "") -> dict:
        if self.passive:
            return {"ok": False, "error": "other_panel"}
        with self.lock:
            busy = [q[0] for q in self.queue] + ([self.current["task"]] if self.current else [])
            if task_id in busy:
                return {"ok": False, "error": "queued"}
            self.queue.append((task_id, trigger, resume))
        self._next()
        return {"ok": True}

    def _next(self) -> None:
        while True:
            with self.lock:
                if self.current or not self.queue:
                    return
                task_id, trigger, resume = self.queue.pop(0)
                task = self.find(task_id)
                if not task:
                    continue                   # задание убрали из расписания — разбираем остаток
                try:
                    run = self._new_run(task, trigger, resume)
                except Exception as e:  # noqa: BLE001 — сбой записи не должен застрять в очереди
                    # Прогон не создан (диск, права): это задание пропущено, остальные идут.
                    print(f"cron: прогон «{task_id}» не создан — {type(e).__name__}: {e}",
                          flush=True)
                    self.say(f"⚠ задание «{task_id}» пропущено: прогон не создан "
                             f"({type(e).__name__}: {e})")
                    continue
                self.current = run
                self.stop_event = threading.Event()
                thread = threading.Thread(target=self._run, args=(task, run), daemon=True,
                                          name="aurora-cron-run")
                self.run_thread = thread
                thread.start()          # под замком: сторож не примет ещё не начатый поток за мёртвый
            return

    def _save(self, run: dict) -> bool:
        """Записать прогон на диск, не уронив цепочку: запись — отчёт о работе, а не сама
        работа. Сбой (диск полон, файл занят читателем) уходит в Консоль и в поле
        `save_error` прогона, а цепочка идёт дальше. → записалось ли."""
        try:
            save_run(run)
            return True
        except Exception as e:  # noqa: BLE001
            run["save_error"] = f"{type(e).__name__}: {e}"
            self.say(f"⚠ запись прогона не удалась ({run['save_error']}) — цепочка идёт дальше")
            return False

    def _unwedge(self) -> None:
        """Сторож: прогон числится идущим, а его потока уже нет — освободить планировщик.
        Штатно `_run` сам снимает `current` в `finally`; сторож — страховка на случай, когда
        поток умер мимо него, иначе все следующие цепочки молча встали бы до перезапуска."""
        with self.lock:
            thread, cur = self.run_thread, self.current
            if not cur or thread is None or thread.is_alive():
                return
            self.current = None
        print(f"cron: прогон {cur.get('id')} остался «идущим» без потока — снят", flush=True)
        self.say(f"⚠ прогон {cur.get('id')} остался «идущим» без потока — планировщик освобождён сторожем")
        cur["status"] = "failed" if cur.get("status") == "running" else cur.get("status")
        cur["error"] = cur.get("error") or "run_thread_lost"
        cur["finished"] = cur.get("finished") or now_iso()
        for it in cur.get("items", []):
            if it.get("status") == "running":      # пункт не останется «идущим» в истории
                it["status"], it["note"] = "interrupted", "run_thread_lost"
        self._save(cur)
        try:                                       # отметка «идёт работа» снимается и здесь:
            self.ck.mark_running("cron-" + str(cur.get("id")), "", "", False)   # иначе обновление
        except Exception:  # noqa: BLE001                                      # кита отказывает
            pass
        self._next()

    def find(self, task_id: str) -> dict | None:
        """Задание по номеру: из расписания или бот проекта."""
        task = next((t for t in load_tasks() if t["id"] == task_id), None)
        if task or not str(task_id).startswith("bot-"):
            return task
        return next((t for t in bot_tasks(self.projects_fn(), self.ck.DEFAULT_LANG)
                     if t["id"] == task_id), None)

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
               "on_fail": task.get("on_fail", "next"), "items": [], "panel": os.getpid()}
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

    def _wait_free(self, project: str, log=None) -> bool:
        """Дождаться, пока проект освободится. Ждать молча нельзя: после перезапуска панели
        шаг прежней цепочки (07.10.2026 — `agent:extract` в PRJ-A) продолжал работать
        сиротой, а пункт новой стоял «идёт» без единой строки, и человек не знал, кому
        верить — Cron или Консоли."""
        end = time.time() + BUSY_WAIT_S
        said = False
        while self._busy(project):
            if not said and log:
                said = True
                log(busy_line(self.ck.project_activity(project)))
            if self.stop_event.is_set() or time.time() > end:
                return False
            time.sleep(BUSY_POLL_S)
        if said and log:
            log("▸ проект свободен — продолжаю")
        return True

    def _run(self, task: dict, run: dict) -> None:
        ck = self.ck
        mark = "cron-" + run["id"]
        halted = ""
        try:
            ck.mark_running(mark, "cron: " + task["name"], "", True)
            with self.lock:
                self.stream, self.stream_base, self.stream_run = [], 0, run["id"]
            self.say(f"▶ цепочка «{task['name']}» · пунктов: {len(run['items'])}"
                     + (" · продолжение после перезапуска панели" if run.get("resumes") else ""))
            lang = task.get("lang") or ck.DEFAULT_LANG
            halted = self._pass(task, run, lang, run["items"], halted)
            # Отложенные проекты — ещё раз, когда остальные готовы: провал бывает от
            # занятого шлюза или обрыва связи, а через час их уже нет. Один раз: второй
            # провал подряд — уже не случайность, и цепочка не крутится по кругу.
            again = [it for it in run["items"] if it["status"] == "deferred"]
            if again and not halted:
                retry = {it["project"] for it in again}
                for it in run["items"]:
                    if it["project"] in retry and it["status"] in BAD + ("deferred",):
                        it["first"] = {"status": it["status"], "note": it.get("note", "")}
                        it.update(status="pending", note="", retry=True)
                self._save(run)
                halted = self._pass(task, run, lang, run["items"], halted, retry=True)
            st = [it["status"] for it in run["items"]]
            run["status"] = ("stopped" if halted == "stopped"
                             else "passed" if all(s in ("passed", "partial") for s in st)
                             else "failed")
        except Exception as e:  # noqa: BLE001 — прогон должен закончиться записью, а не тишиной
            run["status"], run["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            # Что бы ни случилось с записью итога, планировщик освобождается всегда: сбой
            # `save_run` в этом месте раньше оставлял `current` занятым, и все следующие
            # цепочки молча не стартовали до перезапуска панели. Лучше потерять запись итога,
            # чем ночь работы.
            try:
                run["finished"] = now_iso()
                if not self._save(run):
                    time.sleep(FINAL_SAVE_PAUSE)          # занятость проходит за секунды
                    self._save(run)
            finally:
                try:
                    ck.mark_running(mark, "", "", False)
                finally:
                    with self.lock:
                        self.current = None
                    self._next()

    def _pass(self, task: dict, run: dict, lang: str, items: list, halted: str,
              retry: bool = False) -> str:
        """Один проход по шагам цепочки. Шаг проекта провалился — остальные шаги того же
        проекта откладываются (`deferred`), цепочка идёт дальше. → причина остановки."""
        blocked: set = set()
        for it in items:
            if it["status"] != "pending":
                continue
            if halted:
                it.update(status="skipped", note=halted)
                continue
            if self.stop_event.is_set():
                halted = "stopped"
                it.update(status="skipped", note="stopped")
                continue
            if it["project"] in blocked:
                # Повтор не откладывает дальше: второй провал проекта — его итог.
                it.update(status="skipped" if retry else "deferred",
                          note="after_retry_failure" if retry else "after_failure")
                self._save(run)
                continue
            try:
                self._item(task, run, it, lang)
            except Exception as e:  # noqa: BLE001 — сбой шага — это сбой проекта, а не цепочки
                # Непредвиденное исключение (конструктор маршрута, версия движка, запись) не
                # вправе оборвать цепочку: шаг помечается упавшим с причиной и трассировкой,
                # остальное решает правило «проект — единица цепочки» ниже.
                tail = traceback.format_exc().strip().splitlines()[-6:]
                it.setdefault("log", []).extend(tail)
                self.say(f"✖ шаг упал с исключением: {type(e).__name__}: {e}")
                it.update(status="failed", note=f"{type(e).__name__}: {e}", finished=now_iso())
            self._save(run)
            self.say(f"■ {self.item_label(it)}: {it['status']}"
                     + (f" ({it['note']})" if it.get("note") else ""))
            if it["status"] == "stopped":
                halted = "stopped"
            elif it["status"] in BAD:
                if task.get("on_fail") == "stop":
                    halted = "chain_stopped"
                else:
                    blocked.add(it["project"])
                    if not retry:              # к проекту вернёмся в конце цепочки
                        it.update(status="deferred", note=it.get("note") or it["status"],
                                  failed_as=it["status"])
                        self._save(run)
        return halted

    def _item(self, task: dict, run: dict, it: dict, lang: str) -> None:
        ck = self.ck

        def log(text: str) -> None:
            self.say(text)
            it["log"].append(text)
            if len(it["log"]) > LOG_KEEP:
                del it["log"][:-LOG_KEEP]
            # Экран читает прогон с диска: запись атомарная, а читать живой словарь из
            # другого потока — значит ловить его посреди изменения. Чаще раза в две секунды
            # писать незачем.
            if time.time() - run.get("beat", 0) > 2:
                self._save(run)

        def on_job(job_id: str, cmd: str) -> None:
            it["job"] = job_id
            it["jobs"].append({"cmd": cmd, "job": job_id})
            self._save(run)

        def on_step(sig: str, rc: int) -> None:
            if it["jobs"]:
                it["jobs"][-1]["rc"] = rc
            if rc == 0 and sig not in it["done"]:
                it["done"].append(sig)
            self._save(run)

        it.update(status="running", started=now_iso(), note="")
        self._save(run)
        self.say("")
        self.say(f"━━ {self.item_label(it)} ━━")
        project = it["project"]
        if not self._wait_free(project, log):
            it.update(status="stopped" if self.stop_event.is_set() else "skipped",
                      note="" if self.stop_event.is_set() else "busy", finished=now_iso())
            return
        if it["kind"] == "route":
            sc = RR.scenario(ck, it["route"])
            if not sc:
                it.update(status="failed", note="no_route", finished=now_iso())
                return
            resume = {"skipSigs": it["done"]} if it.get("done") else None
            net = task.get("net", "")
            if it.get("retry"):
                # Повтор в конце цепочки уже ждёт связь, а не откладывает снова.
                G, svc = RR.net_gate(), self._net_svc()
                if G is not None and svc is not None and G.policy_for(net, svc) in ("queue", "partial_queue"):
                    net = "queue"
            route = RR.RouteRun(ck, project, sc, it.get("write", True), lang=lang, log=log,
                                stop=self.stop_event, on_job=on_job, on_step=on_step,
                                resume=resume, trigger="cron", parent=run["id"], net=net)
            if hasattr(ck, "route_register"):
                ck.route_register(route, project)
            res = route.safe_run()
            it["title"] = sc["title"]
            it["run_id"] = res.get("run_id", "")
            status = {"passed": "passed", "stall": "stall", "offline": "offline",
                      "gated": "gated", "stopped": "stopped"}.get(res["reason"], "failed")
            if status == "stall" and res.get("progressed"):
                # База за маршрут менялась, не двигается только хвост — проект обновлён.
                status = "partial"
            note = res.get("note", "")
            for key in res.get("redo") or []:           # шаги, шедшие на прежних данных, повторятся вместе с отложенными
                if key in it["done"]:
                    it["done"].remove(key)
            if status == "passed" and res.get("net_deferred"):
                # Маршрут отдал отложенные шаги цепочке: она вернётся к ним в конце, когда остальные проекты пройдены.
                status, note = "deferred", "net: " + ", ".join(dict.fromkeys(res["net_deferred"]))[:200]
            elif status == "passed" and (res.get("net_skipped") or res.get("redo")):
                # Политика «сделать доступное»: часть шагов пропущена из-за связи — это частичный итог, а не чистый.
                status = "partial"
            it.update(status=status, failed=res.get("failed", ""), note=note,
                      finished=now_iso())
            return
        gap = ck.version_gap(project)
        if gap:
            log(gap)
            it.update(status="failed", note="engine_behind", finished=now_iso())
            return
        if self._net_blocks(task, it, log):
            return
        log("▸ " + (it["cmd"] + " " + " ".join(it["args"])).strip())
        res = RR.run_job(ck, project, it["cmd"], it["args"], stop=self.stop_event,
                         on_job=on_job, parent=run["id"], net=self._net_policy(task))
        tries = 0
        while res.get("hung") and not self.stop_event.is_set():
            # Снят сторожем после обрыва связи — тот же шаг заново (`watchdog`), без потолка:
            # цепочка доводит дело до конца, пока человек её не остановит (1.170.0).
            tries += 1
            log(f"⚠ шаг завис ({res['hung']}) — запускаю заново (попытка {tries})")
            res = RR.run_job(ck, project, it["cmd"], it["args"], stop=self.stop_event,
                             on_job=on_job, parent=run["id"], net=self._net_policy(task))
        if res.get("hung"):
            # Сюда доходим, только если цепочку остановили, пока шаг висел.
            res = dict(res, rc=2, refused=f"шаг завис, цепочку остановили: {res['hung']}")
        it["run_id"] = res.get("run_id", "")
        for line in res["lines"][-LOG_KEEP:]:
            log(line)
        on_step((it["cmd"] + " " + " ".join(it["args"])).strip(), res["rc"])
        rc = res["rc"]
        it["rc"] = rc
        if res.get("stopped"):
            it.update(status="stopped", finished=now_iso())
            return
        if rc >= 1 and self._net_refused(task, it, res, log):
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

    # ------------------------------------------------------- связь: хватает ли её шагу

    def _net_svc(self):
        try:
            import connectivity as CN
            svc = CN.service(getattr(self.ck, "KIT", None))
            return svc if svc.enabled else None
        except Exception:  # noqa: BLE001 — связь — подсказка: сбой разбора не останавливает цепочку
            return None

    def _net_policy(self, task: dict) -> str:
        """Политика этого задания для окружения шага; выключенная проверка — пусто."""
        svc, G = self._net_svc(), RR.net_gate()
        return G.policy_for(task.get("net", ""), svc) if (svc and G) else ""

    def _net_refused(self, task: dict, it: dict, res: dict, log) -> bool:
        """Скрипт источника сам отказал по связи (`AURORA-NET`: она пропала между проверкой и первым запросом) → True,
        статус выставлен по политике. Это не поломка шага: ждать или откладывать, а не считать упавшим."""
        G = RR.net_gate()
        mark = G.parse_marker(res["lines"]) if G is not None else {}
        if not mark:
            return False
        why = "; ".join(G.reason_line(r) for r in mark["blocked"])
        policy = G.policy_for(task.get("net", ""), self._net_svc())
        act = G.decide(policy, {"ok": False})
        status = {"fail": "offline", "skip": "partial"}.get(act, "deferred")
        if it.get("retry") and status == "deferred":
            status = "offline"                  # повтор в конце цепочки отказал снова: это итог, цепочка не крутится по кругу
        log(f"⏸ {it['cmd']}: источник отказал по связи — {why}.")
        it.update(status=status, note="net: " + why[:200], finished=now_iso())
        return True

    def _net_blocks(self, task: dict, it: dict, log) -> bool:
        """Не хватает связи этому шагу? → True: статус выставлен, шаг не запускается.

        Политика — задания (`net`) или кита: `fail_fast` — шаг не прошёл (offline); `partial` — пропущен с пометкой, остальное идёт;
        `queue` — ждём восстановления и идём; `partial_queue` — откладываем на повтор в конце цепочки (там ждём), остальное
        идёт сразу. Отложенный шаг не откладывает остальные шаги своего проекта: им связь не нужна или вердикт свой."""
        G, svc = RR.net_gate(), self._net_svc()
        if G is None or svc is None:
            return False
        try:
            v = G.check_command(it["cmd"], it["project"], svc=svc)
        except Exception:  # noqa: BLE001
            return False
        if v.get("ok", True):
            return False
        why = "; ".join(G.reason_line(r) for r in v["blocked"])
        policy = G.policy_for(task.get("net", ""), svc)
        if it.get("retry") and policy in ("queue", "partial_queue"):
            policy = "queue"                    # повтор в конце цепочки уже ждёт, а не откладывает снова
        act = G.decide(policy, v)
        cmd = (it["cmd"] + " " + " ".join(it["args"])).strip()
        if act == "fail":
            log(f"■ {cmd}: нужное недоступно — {why}. Политика — остановиться сразу.")
            it.update(status="offline", note="net: " + why[:200], finished=now_iso())
            return True
        if act == "skip":
            log(f"⏭ {cmd}: пропущен — нужное недоступно ({why}). Остальное идёт.")
            it.update(status="partial", note="net: " + why[:200], finished=now_iso())
            return True
        if act == "defer":
            log(f"⏸ {cmd}: отложен до восстановления связи — {why}. Остальное идёт, к этому вернусь в конце цепочки.")
            it.update(status="deferred", note="net: " + why[:200], finished=now_iso())
            return True
        log(f"⏳ {cmd}: нужное недоступно — {why}. Жду восстановления связи и продолжу сам.")
        started = time.time()
        said = [started]

        def on_wait(verdict, _secs):
            if time.time() - said[0] >= 600:           # в журнал — раз в десять минут, а не на каждый опрос
                said[0] = time.time()
                log(f"⏳ {cmd}: связи всё ещё нет — " + "; ".join(G.reason_line(r) for r in verdict["blocked"]))

        def verdict() -> dict:
            try:
                return G.check_command(it["cmd"], it["project"], svc=svc, hold=True)
            except Exception:  # noqa: BLE001 — сбой самой проверки не рушит шаг: связи «хватает»
                return {"ok": True, "blocked": []}
        ok = G.wait_restored(verdict, stop=self.stop_event, poll=RR.NET_POLL_S, on_wait=on_wait)
        if not ok:
            it.update(status="stopped", finished=now_iso())
            return True
        if not self._wait_free(it["project"], log):            # за время ожидания проект мог занять запуск руками
            it.update(status="stopped" if self.stop_event.is_set() else "skipped",
                      note="" if self.stop_event.is_set() else "busy", finished=now_iso())
            return True
        log(f"✓ {cmd}: связь вернулась (ждали {int(time.time() - started)} с) — продолжаю")
        return False

    # ------------------------------------------------------- для экрана

    def say(self, text: str) -> None:
        with self.lock:
            self.stream.append(str(text))
            extra = len(self.stream) - STREAM_KEEP
            if extra > 0:
                del self.stream[:extra]
                self.stream_base += extra

    def live(self, since: int = 0) -> dict:
        """Поток идущей (или последней) цепочки со строки `since`. `cut` — начало потока уже
        ушло из памяти: страница пишет, что показывает хвост."""
        with self.lock:
            start = max(0, since - self.stream_base)
            return {"run": brief(self.current) if self.current else None,
                    "stream": self.stream_run, "lines": list(self.stream[start:]),
                    "next": self.stream_base + len(self.stream),
                    "cut": since < self.stream_base}

    def item_label(self, it: dict) -> str:
        """Пункт цепочки человеку: проект и что в нём идёт."""
        kind = it.get("kind")
        if kind == "route":
            # Подпись для Консоли не вправе уронить цепочку: нет маршрута — его id.
            try:
                sc = RR.scenario(self.ck, it.get("route", ""))
            except Exception:  # noqa: BLE001
                sc = None
            what = (sc or {}).get("title") or it.get("route", "")
        elif kind == "bot":
            what = "бот " + str(it.get("bot") or "*")
        else:
            what = (str(it.get("cmd") or "") + " " + " ".join(it.get("args") or [])).strip()
        return f"{it.get('project_name') or os.path.basename(it.get('project', ''))} · {what}"

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
        for t in bot_tasks(self.projects_fn(), self.ck.DEFAULT_LANG):
            t["next"] = bot_next(t)
            t["last"] = last.get(t["id"])
            tasks.append(t)
        return {"tasks": tasks, "current": cur, "queue": queue,
                "runs": [brief(r) for r in runs], "now": now_iso(),
                "other_panel": self.owner.get("pid") if self.passive else None}
