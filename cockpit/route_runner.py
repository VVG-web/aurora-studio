"""Маршрут без браузера — для расписания (раздел «Cron») и командной строки.

Кнопка «Пройти» ведёт маршрут из открытой страницы панели (`runRoute` в cockpit/ui/panel.js):
закрыли вкладку — маршрут встал на текущем шаге. Ночному расписанию так нельзя: в 20:00
вкладки может не быть вовсе. Здесь те же правила, что у кнопки, но в процессе панели:

- шаги по порядку; остановка на коде 2 и выше и на убитом процессе (код меньше нуля);
- блок «цикл:» повторяется оборотами: остаток работы считается по видам, каждый оборот
  фиксируется в git, застой и предохранитель в 12 оборотов останавливают маршрут;
- шаг, упавший по сети, ждёт бэкенды: 15 минут между попытками, 30 секунд, если шаг всё
  же сделал работу, — до восьми попыток;
- хвост маршрута фиксируется в git, итог складывает движок (`run_summary`), события шагов и
  состояние остановленного маршрута пишутся туда же, куда их пишет кнопка, — поэтому
  «Продолжить маршрут» в панели работает и после ночного прогона.

Модуль панели приходит параметром `ck`, а не импортом: панель запущена как `__main__`, и
`import aurora_cockpit` отсюда поднял бы вторую копию модуля с пустым списком заданий.
"""
from __future__ import annotations

import json
import os
import re
import time
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


def _rtime() -> str:
    """Метка прогона в местном времени — как `rtime()` у кнопки: архив один на оба пути."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


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


def run_job(ck, project: str, cmd: str, args: list, stop=None, on_job=None) -> dict:
    """Запустить команду заданием панели и дождаться конца. → {rc, lines, refused, stopped}.

    Задание то же, что у кнопки: оно видно в «Консоли», пишет архив прогона и журнал
    запусков, а «Прервать» в панели его останавливает.
    """
    where = job_project(ck, project, cmd)
    try:
        job_id = ck.start_job(where, cmd, list(args))
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
        lines += new
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
            "job": job_id, "run_id": run_id}


class RouteRun:
    """Один проход маршрута по одному проекту.

    `log(text)` получает строки для человека (заголовки шагов, обороты, итог); `on_job(id,
    cmd)` — номер задания текущего шага, чтобы раздел расписания мог открыть его консоль;
    `stop` — threading.Event: человек нажал «Остановить».
    """

    def __init__(self, ck, project: str, sc: dict, write: bool = True, lang: str = "ru",
                 log=None, stop=None, on_job=None, on_step=None, resume: dict | None = None):
        self.ck, self.project, self.sc, self.write = ck, project, sc, write
        self.t = Texts(ck, lang)
        self.lang = lang
        self.log = log or (lambda text: None)
        self.stop = stop
        self.on_job = on_job or (lambda job_id, cmd: None)
        self.on_step = on_step or (lambda sig, rc: None)
        resume = resume or {}
        self.skip = set(resume.get("skipSigs") or [])
        at = resume.get("cycleAt") or None
        self.cycle_at = at if at and (at.get("inLap") or 0) > 1 else None
        self.offline_carry = int(resume.get("attempts") or 0)
        self.events, self.summary = [], []
        self.done = self.lap = self.in_lap = 0
        self.failed = self.refused = ""
        self.stopped = self.stalled = False
        self.run_id = _rtime() + "-route"

    # ------------------------------------------------------------- один шаг

    def _stopping(self) -> bool:
        return bool(self.stop and self.stop.is_set())

    def _sleep(self, seconds: float) -> bool:
        """Подождать, просыпаясь на остановку. → False, если человек остановил."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stopping():
                return False
            time.sleep(min(POLL_S, max(0.0, end - time.time())))
        return not self._stopping()

    def exec_step(self, cmd: str, args: list) -> dict:
        res = run_job(self.ck, self.project, cmd, args, stop=self.stop, on_job=self.on_job)
        if res.get("refused"):
            self.log("■ " + self.t("step.not_started", why=res["refused"]))
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

    def _wait_network(self, st: dict) -> str:
        """Шаг упал по сети: ждём бэкенды и повторяем. → continue | failed | stopped | capped."""
        attempt = self.offline_carry + 1
        self.offline_carry = 0
        wait = OFFLINE_RETRY_S
        cmd = (st["cmd"] + " " + " ".join(st["args"])).strip()
        while True:
            if attempt >= OFFLINE_TRIES:
                self.log(self.t("route.wait_capped", n=OFFLINE_TRIES))
                self.offline_attempts = attempt
                return "capped"
            when = datetime.fromtimestamp(time.time() + wait).strftime("%H:%M")
            self.log(self.t("route.wait_line", cmd=cmd, n=attempt, of=OFFLINE_TRIES, time=when))
            if not self._sleep(wait):
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
            self.log(self.t("route.skip_step", step=self.done, of=self.total, cmd=st["cmd"]))
            return {"rc": 0, "skipped": True, "lines": []}
        if self.cycle_at and st["cycle"] and self.lap == 1 \
                and st["cycleIdx"] < self.cycle_at["inLap"]:
            self.in_lap = st["cycleIdx"]
            self.log(self.t("route.skip_lap_step", step=st["cycleIdx"], of=self.cycle_size,
                            cmd=st["cmd"]))
            return {"rc": 0, "skipped": True, "lines": []}
        if st["cycle"]:
            self.in_lap = st["cycleIdx"]
        else:
            self.done += 1
        lap = self.t("route.lap_mark", lap=self.lap) if self.lap else ""
        where = (self.t("route.where_lap", step=self.in_lap, of=self.cycle_size) if self.lap
                 else self.t("route.where", step=self.done, of=self.total))
        self.log(self.t("route.step_head", where=where, lap=lap, cmd=sig, why=st["why"]))
        start = time.time()
        res = self.exec_step(st["cmd"], st["args"])
        self._event(st, start, time.time(), res)
        if res.get("refused"):
            self.refused = res["refused"]
        self.summary.append({"cmd": st["cmd"], "rc": res["rc"],
                             "summary": self.ck.RS.parse(res["lines"])})
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

    def _commit(self, message: str) -> dict:
        return self.ck.git_commit(self.project, message, None, True)

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
            self.log("■ " + why)
            return {"ok": False, "reason": "failed", "failed": steps[0]["cmd"] if steps else "",
                    "note": why, "steps": 0, "lines": [], "run_id": self.run_id}
        head = ck.RS.git_head(self.project)
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
                self.log(t("route.no_left"))
                return True
            left = sum(kinds.values())
            if left == 0:
                saved = self._commit(t("route.commit_done", title=self.sc["title"],
                                       laps=t("route.laps", n=self.lap)))
                self.log(t("route.work_over", laps=t("route.laps", n=self.lap))
                         + (t("route.committed_as", commit=saved["commit"])
                            if saved.get("ok") else ""))
                return True
            moved = [k for k in kinds if k not in prev or kinds[k] < prev[k]]
            if not moved and self.lap > 1:
                self.log(t("route.stalled_line", tail=tail))
                self.stalled = True
                return False
            grew = [k for k in kinds if k in prev and kinds[k] > prev[k]]
            if grew:
                self.log(t("route.grew", list=", ".join(f"{name(k)} {prev[k]}→{kinds[k]}"
                                                         for k in grew)))
            prev = dict(kinds)
            saved = self._commit(t("route.commit_lap", lap=self.lap, title=self.sc["title"],
                                   left=left))
            self.log(t("route.lap_saved", commit=saved["commit"], tail=tail) if saved.get("ok")
                     else t("route.lap_unsaved", tail=tail, why=saved.get("error") or "?"))
            if self.lap == CYCLE_LIMIT:
                self.log(t("route.limit", laps=t("route.laps", n=CYCLE_LIMIT), tail=tail))
                self.stalled = True
                return False
        return True

    def _finish(self, head: str, started: float) -> dict:
        ck, t = self.ck, self.t
        bad = self.failed or ("stall" if self.stalled else "")
        reason = ("stall" if self.stalled else "offline" if self.offline
                  else "stopped" if self.stopped else "failed" if self.failed else "passed")
        if self.write:
            how = (t("route.how_stalled") if self.stalled
                   else t("route.how_stopped", cmd=bad) if bad else t("route.how_passed"))
            saved = self._commit(t("route.commit_route", title=self.sc["title"], how=how))
            if saved.get("ok"):
                self.log(t("route.saved", commit=saved["commit"]))
            elif "нечего фиксировать" not in (saved.get("error") or ""):      # данные движка
                self.log(t("route.not_saved", why=saved.get("error") or "?"))
        totals = ck.RS.route(self.project, head, time.time() - started, self.summary,
                             lang=self.lang)
        lines = totals.get("lines") or []
        for line in lines:
            self.log(line)
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
            state = {"scId": self.sc["id"], "runId": self.run_id, "title": self.sc["title"],
                     "write": self.write, "reason": reason, "step": self.done,
                     "cycleAt": ({"lap": self.lap, "inLap": self.in_lap}
                                 if self.lap and not self.stalled else None),
                     "done": sorted(self.skip | {(e["cmd"] + " " + " ".join(e["args"])).strip()
                                                 for e in self.events if e["rc"] == 0}),
                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            if reason == "offline":
                state["attempts"] = getattr(self, "offline_attempts", OFFLINE_TRIES)
            ck.write_route_state(self.project, state)
        return {"ok": reason == "passed", "reason": reason, "failed": self.failed,
                "note": self.refused, "steps": self.done, "lines": lines,
                "run_id": self.run_id}


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
