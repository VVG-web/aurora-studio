"""watchdog.py — сторож шагов маршрутов и цепочек: связь вернулась, а шаг так и не ожил.

Обрыв VPN или сети посреди ночного «Обновить базу» оставлял шаг ждать ответа, который не
придёт: запрос ушёл до обрыва, соединение умерло молча, процесс висит. Кнопку «Прервать»
ночью нажать некому, и цепочка стояла до утра.

Сторож смотрит только на шаги маршрутов и цепочек (у задания есть родитель) и только на
молчащие: вывод шага — его пульс. Шаг молчит дольше QUIET — сторож спрашивает связь: модель
(первый провайдер чата) и Jira/Confluence проекта. Дальше два случая:

- связь пропадала, вернулась, а шаг за GRACE после возвращения так и не сказал ни слова —
  он ждёт ответа на запрос, отправленный до обрыва; этот ответ не придёт никогда;
- связь цела, а шаг молчит дольше HARD — это дольше любого запроса к модели (срок одного
  запроса — минуты), то есть ответа тоже не будет.

Тогда сторож снимает процесс шага вместе с дочерними (адаптер модели, git), убирает
брошенный `index.lock` и помечает задание «зависло»; маршрут и цепочка запускают шаг заново
(без потолка: маршрут и цепочка доводят дело до конца, пока человек не прервёт). Долгое
размышление модели при живой связи сторож не трогает: пока ответ возможен, работа не
бросается. С 1.170.0 он видит это по пульсу шага (`job_pulse`): запрос к модели в своём сроке
или вызов, ждущий слот шлюза, — это работа, а не зависание, сколько бы шаг ни молчал.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

QUIET = 300            # с какого молчания шага сторож начинает спрашивать связь
GRACE = 300            # сколько ждать, что шаг оживёт после возвращения связи
HARD = 3600            # молчание при живой связи — дольше любого запроса: ответа не будет
PROBE_EVERY = 60       # связь одного проекта спрашиваем не чаще раза в минуту
TICK = 30              # шаг сторожа
PROBE_TIMEOUT = 10


# Сколько секунд раннер агента получает на фиксацию сделанного (SIGTERM → `git commit`), прежде
# чем снимут дерево. SIGKILL посреди git оставляет `.git/index.lock` — следующий шаг не запишет.
STOP_GRACE = 15.0


def kill_tree(pid: int, grace: float = 0.0) -> None:
    """Снять процесс и всех его потомков: адаптер модели и git живут дочерними процессами,
    и снятый без них шаг оставил бы их держать слоты шлюза и `index.lock`.
    Путь один на панель и раннер агента — `aurora_common.kill_tree`. `grace` — дать самому
    процессу столько секунд на мягкий уход (раннер фиксирует сделанное), потом снять дерево."""
    from aurora_common import kill_tree as _kill_tree
    _kill_tree(pid, grace=grace)


def stop_tree(pid: int) -> None:
    """Остановка процесса движка по решению панели (выход, зависший шаг, брошенный процесс):
    мягко, с окном `STOP_GRACE` на коммит сделанного, потом дерево целиком."""
    kill_tree(pid, grace=STOP_GRACE)


# Метка панели в окружении процессов, которые она запускает (`aurora_cockpit.main`).
# Задания и их потомки наследуют её; по ней следующая панель находит брошенные прежней.
PANEL_MARK = "AURORA_PANEL_PID"
_MARK_RE = re.compile(r"(?:^|\s)" + PANEL_MARK + r"=(\d+)(?=\s|$)")


def marked_processes() -> list:
    """Процессы с меткой панели → [{pid, ppid, panel, command}].

    macOS — `ps -E` (окружение своих процессов), Linux — `/proc/<pid>/environ`. На Windows
    окружение чужого процесса без сторонних модулей не прочесть — там пусто, а сироты
    снимаются уходящей панелью (`stop_jobs_on_exit`)."""
    rows = []
    if sys.platform == "win32":
        return rows
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/environ", "rb") as f:
                    env = f.read().split(b"\0")
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                with open(f"/proc/{d}/stat", encoding="utf-8") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            mark = next((e.split(b"=", 1)[1] for e in env
                         if e.startswith(PANEL_MARK.encode() + b"=")), b"")
            if mark.isdigit():
                rows.append({"pid": int(d), "ppid": ppid, "panel": int(mark), "command": cmd})
        return rows
    try:
        out = subprocess.run(["ps", "-E", "-ww", "-A", "-o", "pid=,ppid=,command="],
                             capture_output=True, text=True, errors="replace", timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return rows
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        m = _MARK_RE.search(parts[2])
        if m:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "panel": int(m.group(1)),
                         "command": parts[2]})
    return rows


def describe(command: str) -> str:
    """Что за процесс — человеку: скрипт движка, задача, проект."""
    script = re.search(r"[/\\]([\w.-]+\.py)\b", command)
    task = re.search(r"--task\s+([\w-]+)", command)
    proj = re.search(r"([^/\\\s]+)[/\\]\.aurora[/\\]scripts", command)
    return ((script.group(1) if script else "процесс движка")
            + (f" --task {task.group(1)}" if task else "")
            + (f" · {proj.group(1)}" if proj else ""))


def reap_orphans(kit: str, own: int = 0, rows=None, kill=None, alive=None) -> list:
    """Снять брошенные процессы движка прежних панелей. → [{pid, panel, what}].

    Брошенный — с меткой панели, которой больше нет, и с командой движка (`.aurora/scripts`
    проекта или `scripts` кита). Не трогаем: процессы своей панели и живых панелей, саму
    панель, всё без метки — прогоны из терминала, MCP-серверы чужих ассистентов, браузер."""
    own = own or os.getpid()
    rows = marked_processes() if rows is None else rows
    kill = kill or stop_tree
    if alive is None:
        from aurora_common import pid_alive as alive
    # Пути сравниваем без разницы между «/» и «\\»: на Windows командная строка бывает и такой,
    # и такой, а кит записан своим разделителем.
    hints = ("/.aurora/scripts/", kit.replace("\\", "/").rstrip("/") + "/scripts/")
    gone, panels = [], {}
    for r in sorted(rows, key=lambda x: x["pid"]):      # родители раньше: их дерево снимет детей
        cmd = r["command"].replace("\\", "/")
        if r["pid"] == own or r["panel"] == own or "aurora_cockpit.py" in cmd:
            continue
        if not any(h in cmd for h in hints):
            continue
        if r["panel"] not in panels:
            panels[r["panel"]] = alive(r["panel"])
        if panels[r["panel"]] or not alive(r["pid"]):
            continue
        kill(r["pid"])
        gone.append({"pid": r["pid"], "panel": r["panel"], "what": describe(r["command"])})
    return gone


def stale_git_lock(project: str, older_than: float = 60) -> str:
    """Брошенный `index.lock` после снятого шага: git, убитый посреди записи индекса,
    оставляет его, и следующий же коммит падает «another git process seems to be running».
    Снимаем только старый: свежий может держать живой git. → путь снятого или пусто."""
    lock = os.path.join(project, ".git", "index.lock")
    try:
        if os.path.isfile(lock) and time.time() - os.path.getmtime(lock) > older_than:
            os.remove(lock)
            return lock
    except OSError:
        pass
    return ""


def probe(project: str) -> dict:
    """Связь, без которой шаг не может закончиться: модель и Jira/Confluence проекта.
    → {"ok": bool, "down": [что не отвечает]}. Ключей наружу нет.

    Проверка соединений включена и знает нужное делу — берём её сводку, а не опрашиваем модель и серверы второй раз;
    Jira/Confluence, чей доступ режимом не задан, опрашиваются прямо, как и без проверки."""
    down = []
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.join(os.path.dirname(here), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    net = {"known": False, "covered": []}
    try:
        import conn_gate
        net = conn_gate.watch_state(project)
    except Exception:  # noqa: BLE001 — нет проверки соединений: прямые запросы, как раньше
        pass
    if net["known"]:
        down += net["down"]
    else:
        try:
            import agent_core as AG
            import model_config as MC
            cfg = MC.to_config(MC.load(os.path.dirname(here), AG.kit_env(os.path.dirname(here))))
            chat = [b for b in cfg.get("backends") or [] if b.get("chat", True)]
            if chat:
                got = AG.models_of(chat[0], timeout=PROBE_TIMEOUT)
                if got.get("error"):
                    down.append("модель (" + (chat[0].get("name") or f"№{chat[0]['n']}") + ")")
        except Exception:  # noqa: BLE001 — сторож не падает из-за настройки моделей
            pass
    if project:
        try:
            import urllib.error
            import urllib.request
            from aurora_common import safe_urlopen
            from sources_core import UnsafeTarget, read_secret
            import atlassian_ops as AO
            import confluence_export as CE
            import jira_export as JE
            s = AO.settings(project)
            for name, base, path, prefix, api_cls in (
                    ("Jira", s["jira_url"], "/rest/api/2/serverInfo", "JIRA", JE.Api),
                    ("Confluence", s["confluence_url"], "/rest/api/space?limit=1", "CONFLUENCE",
                     CE.Api)):
                if not base or prefix in net["covered"]:
                    continue
                auth, _how = read_secret(prefix, project)
                # Запрос строит клиент источника: ключ уходит только на свой сервер, не по
                # открытому http и не по привязке машины (BL-SEC-05); редирект на другой хост
                # идёт без Authorization (`safe_urlopen`).
                api = api_cls(base, auth, project)
                try:
                    url = api._url(path)
                except (UnsafeTarget, ValueError):
                    continue    # адрес небезопасен для ключа: это не «связь упала», скажет синк
                headers = {"Accept": "application/json", "User-Agent": api.agent}
                if auth:
                    headers["Authorization"] = auth
                try:
                    with safe_urlopen(urllib.request.Request(url, headers=headers),
                                      timeout=PROBE_TIMEOUT) as r:
                        r.read(64)
                except urllib.error.HTTPError as e:
                    if e.code >= 500:
                        down.append(name)
                except (urllib.error.URLError, OSError):
                    down.append(name)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": not down, "down": down}


class Watchdog:
    """Смотрит на задания панели раз в TICK секунд. Решение — `verdict`, действие — `kill`."""

    def __init__(self, ck, probe_fn=probe, clock=time.time):
        self.ck, self.probe_fn, self.clock = ck, probe_fn, clock
        self.track: dict = {}          # id задания → {down_seen, up_since, down}
        self.probes: dict = {}         # проект → (когда, ответ)
        self.stop_event = threading.Event()

    def _probe(self, project: str) -> dict:
        now = self.clock()
        when, got = self.probes.get(project, (0, None))
        if got is None or now - when >= PROBE_EVERY:
            got = self.probe_fn(project)
            self.probes[project] = (now, got)
        return got

    def _say(self, job: dict, line: str) -> None:
        """Строка сторожа в вывод задания — как строка процесса: под замком и со счётчиком
        срезанной головы (`job_push`), иначе читатель с курсором теряет счёт."""
        from route_runner import job_push
        lock = getattr(self.ck, "JOBS_LOCK", None)
        if lock is None:
            job_push(job, line)
            return
        with lock:
            job_push(job, line)

    def verdict(self, job: dict) -> str:
        """Пусто — шаг жив или может ожить; иначе — почему ответа уже не будет."""
        now = self.clock()
        quiet = now - (job.get("last_out") or job.get("started") or now)
        st = self.track.setdefault(job["id"], {"down_seen": False, "up_since": 0.0, "down": []})
        if quiet < QUIET:
            return ""
        net = self._probe(job.get("project", ""))
        if not net["ok"]:
            if not st["down_seen"]:
                self._say(job, "⚠ сторож: шаг молчит, связь не отвечает — "
                          + ", ".join(net["down"]) + ". Жду, когда вернётся.")
            st.update(down_seen=True, up_since=0.0, down=net["down"])
            return ""
        if st["down_seen"]:
            if not st["up_since"]:
                st["up_since"] = now
                self._say(job, "⚠ сторож: связь вернулась — шаг должен ожить за "
                          f"{GRACE // 60} мин.")
            if now - st["up_since"] >= GRACE and (job.get("last_out") or 0) < st["up_since"]:
                return ("связь (" + ", ".join(st["down"]) + ") пропадала и вернулась, а шаг за "
                        f"{GRACE // 60} мин так и не ожил — ответа на запрос, отправленный до "
                        "обрыва, не будет")
            return ""
        if quiet >= HARD:
            if self._waits_for_model(job):
                return ""
            return (f"шаг молчит {int(quiet // 60)} мин при живой связи — дольше любого "
                    "запроса к модели: ответа не будет")
        return ""

    def _waits_for_model(self, job: dict) -> bool:
        """Шаг ждёт модель в сроке своего запроса или ждёт слот шлюза — это работа."""
        pulse = getattr(self.ck, "job_pulse", None)
        if not callable(pulse):
            return False
        try:
            return pulse(job).get("state") == "wait"
        except Exception:  # noqa: BLE001 — сторож не падает из-за пульса
            return False

    def kill(self, job: dict, why: str) -> None:
        job["hung"] = why
        self._say(job, f"■ сторож: шаг завис — {why}. Снимаю процесс; маршрут запустит "
                       "шаг заново.")
        proc = job.get("proc")
        if proc is not None and proc.poll() is None:
            stop_tree(proc.pid)
        lock = stale_git_lock(job.get("project", ""))
        if lock:
            self._say(job, f"■ сторож: снят брошенный {lock}")

    def tick(self) -> None:
        with self.ck.JOBS_LOCK:
            jobs = [j for j in self.ck.JOBS.values()
                    if not j.get("done") and j.get("parent") and not j.get("hung")]
        live = {j["id"] for j in jobs}
        self.track = {k: v for k, v in self.track.items() if k in live}
        for job in jobs:
            why = self.verdict(job)
            if why:
                self.kill(job, why)

    def run(self) -> None:
        while not self.stop_event.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — сторож не имеет права уронить панель
                pass

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="aurora-watchdog").start()
