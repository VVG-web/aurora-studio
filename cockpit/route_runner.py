"""Маршрут панели — один исполнитель для кнопки «Пройти», расписания («Cron») и терминала.

До 1.152.0 кнопка вела маршрут из открытой страницы: закрыли вкладку — маршрут встал, а от
прогона в архиве оставались только выводы отдельных шагов, по строке на шаг. Теперь маршрут
всегда идёт в процессе панели (или `aurora.py route`), а страница только смотрит:

- шаги по порядку; остановка на коде 2 и выше и на убитом процессе (код меньше нуля);
- блок «цикл:» повторяется оборотами: остаток работы считается по видам, каждый оборот
  фиксируется в git, застой и предохранитель в 12 оборотов останавливают маршрут;
- шаг, упавший по сети, ждёт бэкенды: 15 минут между попытками, 30 секунд, если шаг всё
  же сделал работу, — до восьми попыток; «Проверить сейчас» будит ожидание;
- хвост маршрута фиксируется в git, итог складывает движок (`run_summary`), состояние
  остановленного маршрута пишется для «Продолжить маршрут»;
- журнал прогона — всё, что видно в консоли, пока маршрут идёт: заголовки шагов, вывод
  каждого шага, обороты, итог (`.aurora/runs/<id>/console.log` и `transcript.jsonl`).
  Шаги пишут свои папки с пометкой родителя, и история показывает маршрут одной строкой.

Модуль панели приходит параметром `ck`, а не импортом: панель запущена как `__main__`, и
`import aurora_cockpit` отсюда поднял бы вторую копию модуля с пустым списком заданий.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import watchdog as WD
from datetime import datetime, timezone

CYCLE_LIMIT = 12                 # дюжина оборотов — уже симптом, а не работа
OFFLINE_RETRY_S = 15 * 60        # между попытками достучаться до бэкендов
FLAKY_RETRY_S = 30               # шлюз мигает, а не лежит: пробуем сразу
OFFLINE_TRIES = 8                # 8 попыток ≈ 2 часа
POLL_S = 1.0

# Шаг сделал работу — значит, бэкенды отвечают, просто через раз. Числа, которые печатают
# сами шаги о сделанном; те же, что у кнопки (`DID_WORK` в panel.js), — сверяет автотест.
DID_WORK = (r"переписано:\s*([1-9]\d*)", r"разобрано\s+([1-9]\d*)\s+из",   # данные движка
            r"уточнено:\s*([1-9]\d*)", r"тезисов:\s*([1-9]\d*)")            # данные движка
ENGINE_CMDS = ("kit:skills",)
CYCLE_START, CYCLE_END = "цикл:", "конец цикла"                             # данные движка


def offline_signs() -> tuple:
    """Признаки обрыва сети — из движка: список один на агент, кнопку и расписание."""
    from agent_runner import OFFLINE_SIGNS
    return OFFLINE_SIGNS


def looks_offline(lines) -> bool:
    text = "\n".join(lines or []).lower()
    return any(s in text for s in offline_signs())


def made_progress(lines) -> bool:
    text = "\n".join(lines or [])
    return any(re.search(rx, text) for rx in DID_WORK)


def _head(project: str) -> str:
    """HEAD проекта; не под git или git не ответил — пусто."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", project, "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def left_by_kind(lines) -> dict:
    """Сколько работы осталось после оборота — по видам раздельно, как у кнопки."""
    out = {}
    for line in lines or []:
        m = re.search(r"Источников в плане:\s*(\d+)\s*→\s*(\d+)", line)      # данные движка
        if m:
            out["источники"] = int(m.group(2))                                  # данные движка
        m = re.search(r"·\s*осталось:\s*(\d+)", line)                          # данные движка
        if m:
            out["карточки"] = int(m.group(1))                                   # данные движка
    return out


def _plural(lang: str, n: int, forms: list) -> str:
    """Форма числа по правилу языка — те же one|few|many, что выбирает Intl в панели."""
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            idx = 0
        elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            idx = 1
        else:
            idx = 2
    else:
        idx = 0 if n == 1 else len(forms) - 1
    return forms[min(idx, len(forms) - 1)]


class Texts:
    """Строки каталога панели на одном языке: коммиты маршрута пишутся теми же словами,
    что у кнопки, — иначе история git читалась бы по-разному в зависимости от того, кто
    вёл маршрут."""

    def __init__(self, ck, lang: str = "ru"):
        self.lang = lang
        self.strings = ck.i18n_catalogue(lang).get("strings") or {}

    def __call__(self, key: str, **v) -> str:
        s = self.strings.get(key, key)
        if isinstance(v.get("n"), int) and "|" in s:
            s = _plural(self.lang, v["n"], s.split("|"))
        for k, val in v.items():
            s = s.replace("{" + k + "}", str(val))
        # Разметка — для страницы (<b>команда</b>); в журнал расписания идёт текст.
        return re.sub(r"<[^>]+>", "", s)


def _slug() -> str:
    """Метка прогона в UTC — как у заданий панели: архив сортируется одной хронологией."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _who(ck, project: str) -> str:
    try:
        return ck.who(project)
    except Exception:  # noqa: BLE001 — подпись не повод ронять маршрут
        return ""


def _kit(ck) -> str:
    try:
        return ck.kit_version()
    except Exception:  # noqa: BLE001
        return ""


def plan(ck, sc: dict, write: bool) -> list:
    """Шаги маршрута, которые панель умеет запустить, с разметкой цикла."""
    out, in_cycle, idx = [], False, 0
    for st in sc.get("steps") or []:
        if st.get("manual"):
            continue
        if st.get("cycle") == CYCLE_START:
            in_cycle, idx = True, 0
            continue
        if st.get("cycle") == CYCLE_END:
            in_cycle = False
            continue
        row = ck.command_by_name(st.get("cmd", ""))
        if not row or not row.get("runnable"):
            continue
        if in_cycle:
            idx += 1
        out.append({"cmd": st["cmd"], "why": st.get("why", ""), "cycle": in_cycle,
                    "cycleIdx": idx if in_cycle else 0,
                    "args": [f for f in (st.get("flags") or []) if write or f != "--apply"]})
    return out


def scenario(ck, sc_id: str) -> dict | None:
    return next((s for s in ck.scenarios() if s.get("id") == sc_id), None)


def job_project(ck, project: str, cmd: str) -> str:
    """Где выполнять команду: движковые (`dev:`, `kit:skills`) — в дереве кита, как у кнопки."""
    row = ck.command_by_name(cmd) or {}
    if row.get("ns") == "dev" or cmd in ENGINE_CMDS:
        return ck.KIT
    return project


def run_job(ck, project: str, cmd: str, args: list, stop=None, on_job=None,
            parent: str = "", on_lines=None) -> dict:
    """Запустить команду заданием панели и дождаться конца. → {rc, lines, refused, stopped}.

    Задание то же, что у кнопки: оно видно в «Консоли», пишет архив прогона и журнал
    запусков, а «Прервать» в панели его останавливает. `parent` — прогон, частью которого
    шаг идёт (маршрут, цепочка расписания): история покажет его внутри родителя.
    `on_lines(строки)` получает вывод по мере появления — для журнала маршрута.
    """
    where = job_project(ck, project, cmd)
    try:
        job_id = ck.start_job(where, cmd, list(args), parent=parent)
    except ValueError as e:
        return {"rc": 2, "lines": [str(e)], "refused": str(e)}
    if on_job:
        on_job(job_id, cmd)
    since, lines, asked = 0, [], False
    while True:
        with ck.JOBS_LOCK:
            job = ck.JOBS.get(job_id)
            if not job:
                return {"rc": 2, "lines": lines}
            new = job["out"][since:]
            since += len(new)
            done, rc, run_id = job["done"], job["rc"], job.get("run_id", "")
            hung = job.get("hung", "")
        lines += new
        if new and on_lines:
            on_lines(new)
        if done:
            break
        if stop is not None and stop.is_set() and not asked:
            asked = True
            ck.stop_job(job_id)
        time.sleep(POLL_S)
    # В памяти задания живут последние 4000 строк: длинный вывод агента обрезан с головы,
    # а итог и остаток работы шаг печатает в конце. Разбираем полный журнал прогона.
    full = os.path.join(ck.runs_dir(where), run_id, "console.log")
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    return {"rc": rc if rc is not None else 2, "lines": lines, "stopped": asked,
            "job": job_id, "run_id": run_id, "hung": hung}


class Journal:
    """Журнал прогона маршрута: то, что видно в консоли, — в памяти, в файлах и по запросу.

    Запись — {k: вид, s: текст}. Виды: `head` — заголовок шага, `out` — вывод шага,
    `note` — ход маршрута (оборот, пропуск, коммит), `warn`, `err`, `sum` — итог. По виду
    страница красит строку так же, как красила при живом прогоне.
    """
    KEEP = 30000                 # записей в памяти; файл хранит всё

    def __init__(self, base: str):
        self.base = base
        self.lock = threading.Lock()
        self.entries: list = []
        self.offset = 0          # сколько записей ушло из памяти с головы
        os.makedirs(base, exist_ok=True)
        self._log = open(os.path.join(base, "console.log"), "a", encoding="utf-8")
        self._jsonl = open(os.path.join(base, "transcript.jsonl"), "a", encoding="utf-8")

    def add(self, kind: str, text: str) -> None:
        with self.lock:
            self.entries.append({"k": kind, "s": text})
            if len(self.entries) > self.KEEP:
                drop = len(self.entries) - self.KEEP
                del self.entries[:drop]
                self.offset += drop
            try:
                self._log.write(text + "\n")
                self._jsonl.write(json.dumps({"k": kind, "s": text}, ensure_ascii=False) + "\n")
                self._log.flush()
                self._jsonl.flush()
            except (OSError, ValueError):
                pass

    def since(self, n: int) -> tuple:
        """Записи с номера `n` (сквозного) → (записи, следующий номер)."""
        with self.lock:
            start = max(0, n - self.offset)
            out = list(self.entries[start:])
            return out, self.offset + len(self.entries)

    def close(self) -> None:
        for f in (self._log, self._jsonl):
            try:
                f.close()
            except OSError:
                pass


def write_meta(base: str, meta: dict) -> None:
    """`meta.json` прогона: что это было, кто, когда, чем кончилось — строка истории."""
    try:
        os.makedirs(base, exist_ok=True)
        tmp = os.path.join(base, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(base, "meta.json"))
    except OSError:
        pass


def read_meta(base: str) -> dict:
    try:
        with open(os.path.join(base, "meta.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RouteRun:
    """Один проход маршрута по одному проекту.

    `log(text)` получает строки для человека (заголовки шагов, обороты, итог); `on_job(id,
    cmd)` — номер задания текущего шага, чтобы раздел расписания мог открыть его консоль;
    `stop` — threading.Event: человек нажал «Остановить».
    """

    def __init__(self, ck, project: str, sc: dict, write: bool = True, lang: str = "ru",
                 log=None, stop=None, on_job=None, on_step=None, resume: dict | None = None,
                 trigger: str = "cli", parent: str = ""):
        self.ck, self.project, self.sc, self.write = ck, project, sc, write
        self.t = Texts(ck, lang)
        self.lang = lang
        self.log = log or (lambda text: None)
        self.stop = stop if stop is not None else threading.Event()
        self.wake = threading.Event()          # «Проверить сейчас» во время ожидания сети
        self.on_job = on_job or (lambda job_id, cmd: None)
        self.on_step = on_step or (lambda sig, rc: None)
        self.trigger, self.parent = trigger, parent
        self.bar: dict = {}                    # полоса хода: шаг, оборот, команда
        self.wait: dict = {}                   # ожидание сети: команда, попытка, когда
        self.job = ""                          # задание текущего шага
        self.children: list = []               # папки шагов в архиве
        self.result: dict = {}
        self.done_flag = False
        self.prev_took = None                  # сколько шёл прошлый шаг — строка в журнале
        self.resumed = bool(resume.get("skipSigs") or resume.get("cycleAt")) if resume else False
        resume = resume or {}
        self.skip = set(resume.get("skipSigs") or [])
        at = resume.get("cycleAt") or None
        self.cycle_at = at if at and (at.get("inLap") or 0) > 1 else None
        self.offline_carry = int(resume.get("attempts") or 0)
        self.events, self.summary = [], []
        self.done = self.lap = self.in_lap = 0
        self.failed = self.refused = ""
        self.stopped = self.stalled = False
        # Продвинулся ли маршрут: каждый шаг агента и каждый оборот фиксируют работу в git,
        # и сдвиг HEAD — честный признак, что база менялась. По нему расписание отличает
        # «застрял на хвосте, проект обновлён» от «не обновилось ничего» (6.10.2026).
        self.head0 = _head(project)
        self.run_id = _slug() + "-route"
        self.base = os.path.join(ck.runs_dir(project), self.run_id)
        self.journal = Journal(self.base)
        self.started = time.time()
        self.meta = {"kind": "route", "id": self.run_id, "title": sc.get("title", ""),
                     "scId": sc.get("id", ""), "write": write, "trigger": trigger,
                     "parent": parent, "started": _iso(self.started),
                     "who": _who(ck, project), "kit": _kit(ck), "status": "running"}
        write_meta(self.base, self.meta)

    def say(self, kind: str, text: str) -> None:
        """Строка журнала: в консоль страницы, в файл прогона и тому, кто ведёт маршрут."""
        self.journal.add(kind, text)
        self.log(text)

    # ------------------------------------------------------------- один шаг

    def _stopping(self) -> bool:
        return bool(self.stop and self.stop.is_set())

    def _sleep(self, seconds: float) -> bool:
        """Подождать, просыпаясь на остановку. → False, если человек остановил."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stopping():
                return False
            if self.wake.is_set():
                self.wake.clear()
                break
            time.sleep(min(POLL_S, max(0.0, end - time.time())))
        return not self._stopping()

    def exec_step(self, cmd: str, args: list) -> dict:
        def on_job(job_id, name):
            self.job = job_id
            self.on_job(job_id, name)

        def on_lines(lines):
            for line in lines:
                if not str(line).startswith(self.ck.RS.MARK):    # машинная строка итога
                    self.say("out", line)
        res = run_job(self.ck, self.project, cmd, args, stop=self.stop, on_job=on_job,
                      parent=self.run_id, on_lines=on_lines)
        self.job = ""
        if res.get("run_id"):
            self.children.append(res["run_id"])
        if res.get("refused"):
            self.say("err", "■ " + self.t("step.not_started", why=res["refused"]))
        if res.get("stopped"):
            self.stopped = True
        return res

    def _event(self, st: dict, start: float, end: float, res: dict) -> None:
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ev = {"cmd": st["cmd"], "args": st["args"], "start": iso(start), "end": iso(end),
              "duration_s": round(end - start), "rc": res["rc"]}
        if res.get("refused"):
            ev["note"] = res["refused"]
        self.events.append(ev)
        self.on_step((st["cmd"] + " " + " ".join(st["args"])).strip(), res["rc"])
        self._progress()

    def _state(self, reason: str) -> dict:
        return {"scId": self.sc["id"], "runId": self.run_id, "title": self.sc["title"],
                "write": self.write, "reason": reason, "step": self.done,
                "cycleAt": ({"lap": self.lap, "inLap": self.in_lap}
                            if self.lap and not self.stalled else None),
                "done": sorted(self.skip | {(e["cmd"] + " " + " ".join(e["args"])).strip()
                                            for e in self.events if e["rc"] == 0}),
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}

    def _progress(self) -> None:
        """Сделанное — после каждого шага: перезапуск панели посреди маршрута убивает его, и
        «Продолжить маршрут» обязан знать, с какого места. Конец маршрута запись заменит."""
        try:
            self.ck.write_route_state(self.project, self._state("interrupted"))
        except Exception:  # noqa: BLE001 — запись продолжения не повод ронять шаг
            pass

    def _wait_network(self, st: dict) -> str:
        """Шаг упал по сети: ждём бэкенды и повторяем. → continue | failed | stopped | capped."""
        attempt = self.offline_carry + 1
        self.offline_carry = 0
        wait = OFFLINE_RETRY_S
        cmd = (st["cmd"] + " " + " ".join(st["args"])).strip()
        while True:
            if attempt >= OFFLINE_TRIES:
                self.wait = {}
                self.say("err", self.t("route.wait_capped", n=OFFLINE_TRIES))
                self.offline_attempts = attempt
                return "capped"
            when = datetime.fromtimestamp(time.time() + wait).strftime("%H:%M")
            self.wait = {"cmd": cmd, "attempt": attempt, "of": OFFLINE_TRIES,
                         "at": _iso(time.time() + wait)}
            self.say("warn", self.t("route.wait_line", cmd=cmd, n=attempt, of=OFFLINE_TRIES,
                                    time=when))
            slept = self._sleep(wait)
            self.wait = {}
            if not slept:
                return "stopped"
            start = time.time()
            res = self.exec_step(st["cmd"], st["args"])
            self._event(st, start, time.time(), res)
            if res["rc"] >= 2 or res["rc"] < 0:
                return "stopped" if self.stopped else "failed"
            if res["rc"] == 1 and looks_offline(res["lines"]):
                flaky = made_progress(res["lines"])
                if not flaky:
                    attempt += 1
                wait = FLAKY_RETRY_S if flaky else OFFLINE_RETRY_S
                continue
            return "continue"

    def run_one(self, st: dict) -> dict:
        sig = (st["cmd"] + " " + " ".join(st["args"])).strip()
        if self.skip and not st["cycle"] and sig in self.skip:
            self.done += 1
            self.say("note", self.t("route.skip_step", step=self.done, of=self.total,
                                    cmd=st["cmd"]))
            return {"rc": 0, "skipped": True, "lines": []}
        if self.cycle_at and st["cycle"] and self.lap == 1 \
                and st["cycleIdx"] < self.cycle_at["inLap"]:
            self.in_lap = st["cycleIdx"]
            self.say("note", self.t("route.skip_lap_step", step=st["cycleIdx"],
                                    of=self.cycle_size, cmd=st["cmd"]))
            return {"rc": 0, "skipped": True, "lines": []}
        if st["cycle"]:
            self.in_lap = st["cycleIdx"]
        else:
            self.done += 1
        lap = self.t("route.lap_mark", lap=self.lap) if self.lap else ""
        where = (self.t("route.where_lap", step=self.in_lap, of=self.cycle_size) if self.lap
                 else self.t("route.where", step=self.done, of=self.total))
        self.bar = {"done": self.done, "total": self.total, "lap": self.lap,
                    "inLap": self.in_lap, "cycleSize": self.cycle_size, "cmd": st["cmd"],
                    "since": _iso()}
        self.say("head", self.t("route.step_head", where=where, lap=lap, cmd=sig, why=st["why"]))
        start = time.time()
        # Длительность прошлого шага — единственный симптом букса на длинном маршруте.
        # Считается от начала до конца прошлого шага. До 1.161.0 здесь стояло «начало
        # этого минус конец прошлого», то есть пауза между шагами, — и журнал у каждого
        # шага писал «занял 0с», в том числе после пятиминутного связывания.
        self.say("note", self.t("route.step_started",
                                time=datetime.fromtimestamp(start).strftime("%H:%M:%S"))
                 + (self.t("route.prev_took", dur=self._dur(self.prev_took))
                    if self.prev_took is not None else self.t("route.first_step")))
        res = self.exec_step(st["cmd"], st["args"])
        # Шаг снят сторожем: связь пропадала, и ответа на запрос, ушедший до обрыва, не будет.
        # Перезапускаем тот же шаг — работа агента записывается по карточкам, синк идёт с
        # места, так что второй заход продолжает, а не начинает заново.
        tries = 0
        while res.get("hung") and tries < WD.MAX_RESTARTS and not self.stopped:
            tries += 1
            self.say("warn", self.t("route.hung_restart", why=res["hung"], n=tries,
                                    of=WD.MAX_RESTARTS))
            res = self.exec_step(st["cmd"], st["args"])
        if res.get("hung"):
            self.say("err", self.t("route.hung_gave_up", n=WD.MAX_RESTARTS))
            res = dict(res, rc=2)
        end = time.time()
        self.prev_took = end - start
        self._event(st, start, end, res)
        if res.get("refused"):
            self.refused = res["refused"]
        # Хвост вывода шага, «нашедшего, что чинить», — для кнопок «Починить» в итоге.
        self.summary.append({"cmd": st["cmd"], "rc": res["rc"],
                             "summary": self.ck.RS.parse(res["lines"]),
                             "tail": res["lines"][-300:] if res["rc"] == 1 else []})
        if res["rc"] == 1 and looks_offline(res["lines"]) and not self.stopped:
            how = self._wait_network(st)
            if how == "continue":
                self.summary[-1]["rc"] = 0
                return {"rc": 0, "lines": res["lines"]}
            if how in ("stopped", "capped"):
                self.stopped = how == "stopped"
                self.offline = how == "capped"
                self.failed = st["cmd"]
                return {"rc": 0, "lines": res["lines"], "ended": how}
            return {"rc": 2, "lines": res["lines"]}
        return res

    # -------------------------------------------------------------- маршрут

    def _dur(self, sec: float) -> str:
        sec = max(0, int(round(sec)))
        return (self.t("dur.sec", n=sec) if sec < 60
                else self.t("dur.min_sec", m=sec // 60, s=sec % 60))

    def _kb_updated(self) -> None:
        """Отметить пройденный маршрут в `AuroraKnowledgeDB/meta/kb_updated.json`. Отметка —
        не шаг маршрута: не вышла — маршрут всё равно пройден, только сказано вслух."""
        try:
            import kb_updated as KU
            who = getattr(self.ck, "who", lambda p: "")(self.project)
            kit = getattr(self.ck, "kit_version", lambda: "")()
            rec = KU.mark(self.project, self.sc["id"], who=who, kit=kit, run=self.run_id)
        except Exception as e:  # noqa: BLE001
            self.say("warn", self.t("route.kb_updated_failed", why=f"{type(e).__name__}: {e}"))
            return
        mine = rec and rec.get(self.sc["id"], {}).get("at")
        if rec and rec.get("updated") and rec["updated"] == mine:
            self.say("ok", self.t("route.kb_updated"))
        elif self.sc["id"] == "update":
            self.say("note", self.t("route.kb_update_needs_fix"))

    def _commit(self, message: str) -> dict:
        return self.ck.git_commit(self.project, message, None, True)

    def _git(self, action: str, trigger: str) -> None:
        """Автоматика Git проекта перед маршрутом и после него — если она включена.

        Шаг идёт заданием, как и остальные: его вывод — в журнале маршрута, папка — внутри
        маршрута в истории. Неудача маршрут не роняет: обновление не прошло — маршрут идёт
        на том, что есть у проекта, и говорит это вслух; отправка не прошла — работа цела
        локально, и раздел «Git» скажет, что с этим делать."""
        wanted = getattr(self.ck, "git_auto_wanted", None)
        if not self.write or not wanted or not wanted(self.project, action, trigger):
            return
        self.say("head", self.t("route.git_" + action))
        res = self.exec_step(f"git:{action}", [f"--auto={trigger}"])
        if res.get("rc"):
            self.say("warn", self.t("route.git_" + action + "_failed"))

    def run(self) -> dict:
        ck, t = self.ck, self.t
        steps = plan(ck, self.sc, self.write)
        self.total = len([s for s in steps if not s["cycle"]])
        self.cycle_size = len([s for s in steps if s["cycle"]]) or 1
        self.offline = False
        started = time.time()
        gap = ck.version_gap(self.project)
        if gap:
            # Кнопка встаёт на первом шаге с тем же отказом сервера: один прогон двумя
            # версиями движка — не «частично сработало».
            why = t("step.not_started", why="Маршрут не начат: " + gap)
            self.say("err", "■ " + why)
            res = {"ok": False, "reason": "failed", "failed": steps[0]["cmd"] if steps else "",
                   "note": why, "steps": 0, "lines": [], "run_id": self.run_id, "found": []}
            return self._close(res)
        if not self.resumed:
            self._git("update", "before_route")
        head = ck.RS.git_head(self.project)
        if self.resumed:
            self.say("ok", t("route.resumed", title=self.sc["title"]))
        i = 0
        while i < len(steps):
            if self._stopping():
                self.stopped, self.failed = True, self.failed or steps[i]["cmd"]
                break
            if not steps[i]["cycle"]:
                res = self.run_one(steps[i])
                if res.get("ended"):
                    break
                if res["rc"] >= 2 or res["rc"] < 0:
                    self.failed = steps[i]["cmd"]
                    break
                i += 1
                continue
            end = i
            while end < len(steps) and steps[end]["cycle"]:
                end += 1
            if not self._cycle(steps, i, end):
                break
            self.lap = 0
            i = end
        return self._finish(head, started)

    def _cycle(self, steps: list, i: int, end: int) -> bool:
        """Блок цикла оборотами. → False, если маршрут дальше не идёт."""
        t = self.t
        prev = {}
        for self.lap in range(1, CYCLE_LIMIT + 1):
            kinds = {}
            for k in range(i, end):
                if self._stopping():
                    self.stopped, self.failed = True, steps[k]["cmd"]
                    return False
                res = self.run_one(steps[k])
                if res.get("ended"):
                    return False
                if res["rc"] >= 2 or res["rc"] < 0:
                    self.failed = steps[k]["cmd"]
                    return False
                kinds.update(left_by_kind(res["lines"]))
            if self.cycle_at and self.lap == 1:
                continue
            name = lambda k: (t("route.kind." + k) if t("route.kind." + k) != "route.kind." + k
                              else k)
            tail = ", ".join(f"{name(k)}: {v}" for k, v in kinds.items())
            if not kinds:
                self.say("note", t("route.no_left"))
                return True
            left = sum(kinds.values())
            if left == 0:
                saved = self._commit(t("route.commit_done", title=self.sc["title"],
                                       laps=t("route.laps", n=self.lap)))
                self.say("ok", t("route.work_over", laps=t("route.laps", n=self.lap))
                         + (t("route.committed_as", commit=saved["commit"])
                            if saved.get("ok") else ""))
                return True
            moved = [k for k in kinds if k not in prev or kinds[k] < prev[k]]
            if not moved and self.lap > 1:
                self.say("warn", t("route.stalled_line", tail=tail))
                self.stalled = True
                return False
            grew = [k for k in kinds if k in prev and kinds[k] > prev[k]]
            if grew:
                self.say("note", t("route.grew", list=", ".join(
                    f"{name(k)} {prev[k]}→{kinds[k]}" for k in grew)))
            prev = dict(kinds)
            saved = self._commit(t("route.commit_lap", lap=self.lap, title=self.sc["title"],
                                   left=left))
            self.say("note", t("route.lap_saved", commit=saved["commit"], tail=tail)
                     if saved.get("ok")
                     else t("route.lap_unsaved", tail=tail, why=saved.get("error") or "?"))
            if self.lap == CYCLE_LIMIT:
                self.say("warn", t("route.limit", laps=t("route.laps", n=CYCLE_LIMIT), tail=tail))
                self.stalled = True
                return False
        return True

    def _finish(self, head: str, started: float) -> dict:
        ck, t = self.ck, self.t
        bad = self.failed or ("stall" if self.stalled else "")
        reason = ("stall" if self.stalled else "offline" if self.offline
                  else "stopped" if self.stopped else "failed" if self.failed else "passed")
        # Итоговая фраза — та, что страница писала в конце консоли: теперь она в журнале и
        # видна в истории так же, как была видна вживую.
        if self.stalled:
            self.say("err", t("route.stalled_why"))
        elif bad:
            self.say("err", t("route.stopped_by_you", cmd=bad) if self.stopped
                     else t("route.stopped_on", cmd=bad,
                            why=(self.refused + ". ") if self.refused else t("route.cmd_failed")))
        else:
            self.say("ok", t("route.passed", title=self.sc["title"],
                             steps=t("route.steps_n", n=self.done)))
        # Дата обновления базы — общая для команды (1.168.0): «Обновить базу» и следом
        # «Починить базу», пройденные целиком. Пишется до итогового коммита и уходит в него.
        if self.write and reason == "passed" and self.sc.get("id") in ("update", "fix"):
            self._kb_updated()
        if self.write:
            how = (t("route.how_stalled") if self.stalled
                   else t("route.how_stopped", cmd=bad) if bad else t("route.how_passed"))
            saved = self._commit(t("route.commit_route", title=self.sc["title"], how=how))
            if saved.get("ok"):
                self.say("note", t("route.saved", commit=saved["commit"]))
            elif "нечего фиксировать" not in (saved.get("error") or ""):      # данные движка
                self.say("warn", t("route.not_saved", why=saved.get("error") or "?"))
        totals = ck.RS.route(self.project, head, time.time() - started, self.summary,
                             lang=self.lang)
        lines = totals.get("lines") or []
        for line in lines:
            self.say("sum", line)
        # «Нашла, что чинить» — одна строка на команду, сколько бы шагов она ни заняла.
        times: dict = {}
        for x in self.summary:
            if x["rc"] == 1:
                times[x["cmd"]] = times.get(x["cmd"], 0) + 1
        for cmd, n in times.items():
            self.say("warn", t("route.found", cmd=cmd)
                     + (t("route.found_times", n=n) if n > 1 else ""))
        if reason == "passed":
            self._git("push", "after_route")
        if self.events:
            try:
                base = os.path.join(ck.runs_dir(self.project), self.run_id)
                os.makedirs(base, exist_ok=True)
                with open(os.path.join(base, "events.jsonl"), "w", encoding="utf-8") as f:
                    for ev in self.events:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except OSError:
                pass
        if reason == "passed":
            ck.clear_route_state(self.project)
        else:
            state = self._state(reason)
            if reason == "offline":
                state["attempts"] = getattr(self, "offline_attempts", OFFLINE_TRIES)
            ck.write_route_state(self.project, state)
        head = _head(self.project)
        res = {"ok": reason == "passed", "reason": reason, "failed": self.failed,
               "progressed": bool(head and head != self.head0),
               "note": self.refused, "steps": self.done, "lines": lines,
               "run_id": self.run_id,
               "found": [{"cmd": x["cmd"], "lines": x["tail"]} for x in self.summary
                         if x["rc"] == 1 and x.get("tail")]}
        return self._close(res)

    def _close(self, res: dict) -> dict:
        """Конец прогона: итог в `meta.json`, журнал закрыт, ожидающие увидят `done`."""
        self.bar, self.wait, self.job = {}, {}, ""
        self.meta.update(status=res["reason"], ok=res["ok"], failed=res.get("failed", ""),
                         note=res.get("note", ""), finished=_iso(),
                         seconds=round(time.time() - self.started), steps=self.children)
        write_meta(self.base, self.meta)
        self.journal.close()
        self.result = res
        self.done_flag = True
        return res


def run_route(ck, project: str, sc_id: str, write: bool = True, **kw) -> dict:
    sc = scenario(ck, sc_id)
    if not sc:
        return {"ok": False, "reason": "failed", "failed": "", "steps": 0, "lines": [],
                "note": f"маршрута «{sc_id}» нет в cockpit/scenarios.txt"}
    return RouteRun(ck, project, sc, write, **kw).run()


def main(argv=None) -> int:
    """`aurora.py route <проект> <маршрут> [--apply]` — маршрут из терминала, без страницы панели.

    Без `--apply` — как кнопка «Посмотреть»: те же шаги без записи. Код выхода: 0 — пройден,
    1 — остановлен (застой, человек, сеть), 2 — шаг не отработал.
    """
    import argparse
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import aurora_cockpit as ck
    ap = argparse.ArgumentParser(prog="aurora.py route", description=main.__doc__.split("\n")[0])
    ap.add_argument("project")
    ap.add_argument("route", help="id маршрута из cockpit/scenarios.txt: update, fix, …")
    ap.add_argument("--apply", action="store_true", help="пройти с записью, как «Пройти»")
    ap.add_argument("--lang", default=ck.DEFAULT_LANG)
    a = ap.parse_args(argv)
    project = os.path.abspath(os.path.expanduser(a.project))
    if not os.path.isfile(os.path.join(project, "aurora.config.yaml")):
        print(f"{project}: это не проект Авроры (нет aurora.config.yaml)", file=sys.stderr)
        return 2
    if not scenario(ck, a.route):
        ids = ", ".join(s["id"] for s in ck.scenarios())
        print(f"Маршрута «{a.route}» нет. Есть: {ids}", file=sys.stderr)
        return 2
    res = run_route(ck, project, a.route, a.apply, lang=a.lang,
                    log=lambda text: print(text, flush=True))
    print(f"\n{'✅' if res['ok'] else '■'} {res['reason']}"
          + (f" · {res['failed']}" if res.get("failed") else ""), flush=True)
    return 0 if res["ok"] else 2 if res["reason"] == "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
