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
(не больше MAX_RESTARTS раз). Долгое размышление модели при живой связи сторож не трогает:
пока ответ возможен, работа не бросается.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

QUIET = 300            # с какого молчания шага сторож начинает спрашивать связь
GRACE = 300            # сколько ждать, что шаг оживёт после возвращения связи
HARD = 3600            # молчание при живой связи — дольше любого запроса: ответа не будет
PROBE_EVERY = 60       # связь одного проекта спрашиваем не чаще раза в минуту
TICK = 30              # шаг сторожа
MAX_RESTARTS = 2       # столько раз маршрут и цепочка перезапускают зависший шаг
PROBE_TIMEOUT = 10


def kill_tree(pid: int) -> None:
    """Снять процесс и всех его потомков: адаптер модели и git живут дочерними процессами,
    и снятый без них шаг оставил бы их держать слоты шлюза и `index.lock`."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=30)
        return
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    kids: dict = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    tree, todo = [], [pid]
    while todo:
        p = todo.pop()
        tree.append(p)
        todo += kids.get(p, [])
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in reversed(tree):          # сначала листья: родитель не успеет плодить новых
            try:
                os.kill(p, sig)
            except OSError:
                pass
        time.sleep(1.0)


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
    → {"ok": bool, "down": [что не отвечает]}. Ключей наружу нет."""
    down = []
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.join(os.path.dirname(here), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
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
            from sources_core import read_secret
            import atlassian_ops as AO
            s = AO.settings(project)
            for name, base, path, prefix in (
                    ("Jira", s["jira_url"], "/rest/api/2/serverInfo", "JIRA"),
                    ("Confluence", s["confluence_url"], "/rest/api/space?limit=1", "CONFLUENCE")):
                if not base:
                    continue
                auth, _how = read_secret(prefix, project)
                req = urllib.request.Request(base + path, headers={
                    "Authorization": auth, "Accept": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
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
                job["out"].append("⚠ сторож: шаг молчит, связь не отвечает — "
                                  + ", ".join(net["down"]) + ". Жду, когда вернётся.")
            st.update(down_seen=True, up_since=0.0, down=net["down"])
            return ""
        if st["down_seen"]:
            if not st["up_since"]:
                st["up_since"] = now
                job["out"].append("⚠ сторож: связь вернулась — шаг должен ожить за "
                                  f"{GRACE // 60} мин.")
            if now - st["up_since"] >= GRACE and (job.get("last_out") or 0) < st["up_since"]:
                return ("связь (" + ", ".join(st["down"]) + ") пропадала и вернулась, а шаг за "
                        f"{GRACE // 60} мин так и не ожил — ответа на запрос, отправленный до "
                        "обрыва, не будет")
            return ""
        if quiet >= HARD:
            return (f"шаг молчит {int(quiet // 60)} мин при живой связи — дольше любого "
                    "запроса к модели: ответа не будет")
        return ""

    def kill(self, job: dict, why: str) -> None:
        job["hung"] = why
        job["out"].append(f"■ сторож: шаг завис — {why}. Снимаю процесс; маршрут запустит "
                          "шаг заново.")
        proc = job.get("proc")
        if proc is not None and proc.poll() is None:
            kill_tree(proc.pid)
        lock = stale_git_lock(job.get("project", ""))
        if lock:
            job["out"].append(f"■ сторож: снят брошенный {lock}")

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
