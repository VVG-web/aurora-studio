"""Маршрут панели — один исполнитель для кнопки «Пройти», расписания («Cron») и терминала.

До 1.152.0 кнопка вела маршрут из открытой страницы: закрыли вкладку — маршрут встал, а от
прогона в архиве оставались только выводы отдельных шагов, по строке на шаг. Теперь маршрут
всегда идёт в процессе панели (или `aurora.py route`), а страница только смотрит:

- шаги по порядку; остановка на коде 2 и выше и на убитом процессе (код меньше нуля), а на
  шаге-воротах (флаг `--gate` в scenarios.txt) — ещё и на коде 1: «ворота не пройдены»;
- блок «цикл:» повторяется оборотами: остаток работы считается по видам, каждый оборот
  фиксируется в git, застой и предохранитель в 12 оборотов останавливают маршрут;
- шаг, упавший по сети, ждёт бэкенды: 15 минут между попытками, 30 секунд, если шаг всё
  же сделал работу, — до восьми попыток; «Проверить сейчас» будит ожидание. При включённой проверке
  соединений (`conn_gate`) шагу, которому не хватает связи, не дают начаться: политика прогона
  (`fail_fast`, `partial`, `queue`, `partial_queue`) решает — остановить, пропустить, дождаться или
  отложить на конец маршрута; ожидание идёт по проверке связи, а не вслепую;
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
import secrets
import threading
import time
import traceback

from datetime import datetime, timezone

CYCLE_LIMIT = 12                 # дюжина оборотов — уже симптом, а не работа
OFFLINE_RETRY_S = 15 * 60        # между попытками достучаться до бэкендов
FLAKY_RETRY_S = 30               # шлюз мигает, а не лежит: пробуем сразу
# Потолка попыток нет (1.170.0): маршрут ждёт сеть, пока она не вернётся или человек не
# нажмёт «не ждать». Прежние 8 попыток (≈ 2 часа) заканчивали ночной прогон к утру ничем.
POLL_S = 1.0
NET_POLL_S = 5.0         # ожидание связи: вердикт обновляется раз в столько секунд (сводка — из кэша проверок)
JOB_KEEP_LINES = 4000    # строк вывода задания в памяти; полный текст — в console.log прогона


def net_gate():
    """Проверка соединений (`conn_gate`) либо None: у старого кита модуля нет — маршрут идёт как раньше."""
    try:
        import conn_gate
    except ImportError:
        return None
    return conn_gate


def job_push(job: dict, line: str) -> None:
    """Добавить строку вывода задания; при переполнении срезать голову и считать срезанное.

    Читатели ведут курсор сквозным номером строки (`job_slice`): без счётчика `base`
    курсор, дошедший до предела, видел пустой хвост, и живой вывод замирал после 4000-й
    строки. Вызывать под `JOBS_LOCK`.
    """
    job["out"].append(line)
    over = len(job["out"]) - JOB_KEEP_LINES
    if over > 0:
        del job["out"][:over]
        job["base"] = job.get("base", 0) + over


def job_slice(job: dict, since: int) -> tuple:
    """Строки вывода со сквозного номера `since`. → (строки, следующий номер).

    Если читатель отстал и голова уже срезана, он получает самое старое из оставшегося:
    пропущенное лежит в `console.log` прогона. Следующий номер считается от взятого среза,
    а не от нового `len(out)`: сторож и «Прервать» дописывают в `out` мимо замка, и строка,
    пришедшая между срезом и подсчётом, иначе была бы засчитана непрочитанной.
    """
    base = job.get("base", 0)
    cut = max(0, since - base)
    part = job["out"][cut:]
    return part, base + cut + len(part)


# Шаг сделал работу — значит, бэкенды отвечают, просто через раз. Числа, которые печатают
# сами шаги о сделанном; те же, что у кнопки (`DID_WORK` в panel.js), — сверяет автотест.
DID_WORK = (r"переписано:\s*([1-9]\d*)", r"разобрано\s+([1-9]\d*)\s+из",   # данные движка
            r"уточнено:\s*([1-9]\d*)", r"тезисов:\s*([1-9]\d*)",            # данные движка
            # итог шага агента: «Документы: обработано 2 · …», «Карт … записано: 76»
            r"обработано\s+([1-9]\d*)", r"записано:\s*([1-9]\d*)")          # данные движка
ENGINE_CMDS = ("kit:skills",)
CYCLE_START, CYCLE_END = "цикл:", "конец цикла"                             # данные движка


def offline_signs() -> tuple:
    """Признаки обрыва сети — из движка: список один на агент, кнопку и расписание."""
    from agent_runner import OFFLINE_SIGNS
    return OFFLINE_SIGNS


# Строки хода агента (1.170.0, `agent_core.say_live`): «⚠ обрыв — подожду и повторю», «⟳ не
# ответил — повторю», «⏳ лимит — жду». Это пережитые обрывы, а не обрыв: шаг их уже переждал.
# Считать по ним сеть упавшей значило бы отправлять дошедший до конца шаг ждать сеть заново.
LIVE_LINE = re.compile(r"^ {4}[↗⟳⚠…✓⏳] ")


def looks_offline(lines) -> bool:
    if isinstance(lines, str):
        lines = lines.splitlines()
    text = "\n".join(line for line in lines or [] if not LIVE_LINE.match(line)).lower()
    return any(s in text for s in offline_signs())


def is_gate(args) -> bool:
    """Шаг — ворота качества, если среди его флагов в scenarios.txt стоит `--gate` (или `--gate=fail`).

    `--gate=warn` — режим внедрения: код 1 такого шага маршрут не останавливает, воротами он не считается."""
    return any(a in ("--gate", "--gate=fail") for a in args or [])


def halts(args, rc) -> bool:
    """Останавливает ли код `rc` шага с флагами `args` маршрут. Одно правило для всех мест.

    Коды возврата: 0 — ок, 1 — замечания (у обычного шага — «нашла, что чинить», маршрут идёт
    дальше; у ворот — «ворота не пройдены», маршрут встаёт), 2 — сбой выполнения, всё остальное
    от 2 и выше и отрицательный (убитый процесс) — тоже сбой."""
    return rc >= 2 or rc < 0 or (rc == 1 and is_gate(args))


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
        m = re.search(r"Источников в плане:\s*(?:\d+|н/д)\s*→\s*(\d+)", line)      # данные движка
        if m:
            out["источники"] = int(m.group(1))                                  # данные движка
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
                    "cycleIdx": idx if in_cycle else 0, "pos": len(out),
                    "args": [f for f in (st.get("flags") or []) if write or f != "--apply"]})
    return out


def pending_steps(steps: list, skip) -> list:
    """Шаги плана, которые ещё предстоят: пройденные в прошлый раз (`skipSigs` продолжения) не в счёт, а шаги цикла идут
    всегда. Ключ шага — тот же, что у `RouteRun.step_key`: подпись, а у повторяющейся подписи — с позицией."""
    skip = set(skip or [])
    sigs = [(s["cmd"] + " " + " ".join(s["args"])).strip() for s in steps]
    out = []
    for st, sig in zip(steps, sigs):
        key = f"{sig} @{st['pos']}" if sigs.count(sig) > 1 and "pos" in st else sig
        if st["cycle"] or key not in skip:
            out.append(st)
    return out


def all_routes(ck) -> list:
    """Все маршруты кита, а не только видимые в профиле: профиль прячет, но не запрещает запуск."""
    return (getattr(ck, "all_scenarios", None) or ck.scenarios)()


def scenario(ck, sc_id: str) -> dict | None:
    return next((s for s in all_routes(ck) if s.get("id") == sc_id), None)


def job_project(ck, project: str, cmd: str) -> str:
    """Где выполнять команду: движковые (`dev:`, `kit:skills`) — в дереве кита, как у кнопки."""
    row = ck.command_by_name(cmd) or {}
    if row.get("ns") == "dev" or cmd in ENGINE_CMDS:
        return ck.KIT
    return project


def run_job(ck, project: str, cmd: str, args: list, stop=None, on_job=None,
            parent: str = "", on_lines=None, net: str = "") -> dict:
    """Запустить команду заданием панели и дождаться конца. → {rc, lines, refused, stopped}.

    Задание то же, что у кнопки: оно видно в «Консоли», пишет архив прогона и журнал
    запусков, а «Прервать» в панели его останавливает. `parent` — прогон, частью которого
    шаг идёт (маршрут, цепочка расписания): история покажет его внутри родителя.
    `on_lines(строки)` получает вывод по мере появления — для журнала маршрута. `net` — политика
    прогона без связи: процесс шага узнаёт её из окружения (`AURORA_NET_POLICY`).
    """
    where = job_project(ck, project, cmd)
    try:
        job_id = ck.start_job(where, cmd, list(args), parent=parent, **({"net_policy": net} if net else {}))
    except ValueError as e:
        return {"rc": 2, "lines": [str(e)], "refused": str(e)}
    if on_job:
        on_job(job_id, cmd)
    since, lines, asked, accepted = 0, [], False, False
    while True:
        with ck.JOBS_LOCK:
            job = ck.JOBS.get(job_id)
            if not job:
                return {"rc": 2, "lines": lines}
            new, since = job_slice(job, since)
            done, rc, run_id = job["done"], job["rc"], job.get("run_id", "")
            hung = job.get("hung", "")
        lines += new
        if new and on_lines:
            on_lines(new)
        if done:
            break
        if stop is not None and stop.is_set() and not accepted:
            asked = True
            got = ck.stop_job(job_id)
            # Процесс шага мог ещё не родиться (`stop_job` отвечает ошибкой): повторяем на каждом опросе, пока задание не
            # примет остановку или не кончится. Подставная панель без ответа — принято (одна попытка, как раньше).
            accepted = not isinstance(got, dict) or bool(got.get("ok"))
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
        try:
            from aurora_common import replace_file
        except ImportError:                   # панель без движка рядом — подмена без повторов
            replace_file = os.replace
        replace_file(tmp, os.path.join(base, "meta.json"))   # Windows: занятый читателем — с повторами
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

    net, net_policy = "", ""        # политика без связи: запрошенная и действующая; пусто — проверка соединений не вмешивается
    no_run = False                  # маршрут отказал до первого шага: ни коммита, ни пометки «маршрут идёт»

    def __init__(self, ck, project: str, sc: dict, write: bool = True, lang: str = "ru",
                 log=None, stop=None, on_job=None, on_step=None, resume: dict | None = None,
                 trigger: str = "cli", parent: str = "", net: str = ""):
        self.ck, self.project, self.sc, self.write = ck, project, sc, write
        # Политика прогона без связи: задание Cron или выбор человека в диалоге; пусто — из настройки кита.
        self.net, self.net_policy = net, ""
        self.deferred: list = []               # шаги, которым не хватило связи: повторим после восстановления
        self.deferred_blocks: list = []        # то же для блоков цикла: (начало, конец)
        self.net_skipped: list = []            # пропущенные по политике partial
        self.net_deferred: list = []           # отложенное передано расписанию: цепочка повторит его в конце
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
        self.sig_count: dict = {}              # сколько раз подпись встречается в плане
        self.done_keys: set = set()            # ключи шагов, прошедших с кодом 0
        at = resume.get("cycleAt") or None
        self.cycle_at = at if at and (at.get("inLap") or 0) > 1 else None
        self.offline_carry = int(resume.get("attempts") or 0)
        self.events, self.summary = [], []
        self.done = self.lap = self.in_lap = 0
        self.failed = self.refused = ""
        self.gate_failed = ""                  # шаг-ворота, не пропустивший маршрут (код 1)
        self.last_res: dict = {}               # последний ответ шага в ожидании сети
        self.stopped = self.stalled = False
        # Продвинулся ли маршрут: каждый шаг агента и каждый оборот фиксируют работу в git,
        # и сдвиг HEAD — честный признак, что база менялась. По нему расписание отличает
        # «застрял на хвосте, проект обновлён» от «не обновилось ничего» (6.10.2026).
        self.head0 = _head(project)
        # Хвост в две пары шестнадцатеричных знаков: маршруты двух проектов, стартовавшие в одну
        # секунду, иначе делили ключ реестра ROUTES и затирали друг друга. Суффикс «-route» остаётся.
        self.run_id = f"{_slug()}-{secrets.token_hex(2)}-route"
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

    # ------------------------------------------------------- связь: хватает ли её шагу

    def _net_svc(self):
        try:
            import connectivity as CN
            return CN.service(getattr(self.ck, "KIT", None))
        except Exception:  # noqa: BLE001 — связь — подсказка: сбой разбора не останавливает маршрут
            return None

    def _verdict(self, cmd: str, hold: bool = False) -> dict:
        """Хватает ли связи команде сейчас. Выключенная проверка и сбой самой проверки — «хватает». `hold` — в ожидании:
        последнее известное «недоступно» держится, пока не придёт положительный ответ."""
        G, svc = net_gate(), self._net_svc()
        if G is None or svc is None:
            return {"ok": True, "blocked": []}
        try:
            return G.check_command(cmd, self.project, svc=svc, hold=hold)
        except Exception:  # noqa: BLE001
            return {"ok": True, "blocked": []}

    def _why(self, v: dict) -> str:
        G = net_gate()
        return "; ".join(G.reason_line(r) for r in v.get("blocked") or []) if G else ""

    def _woken(self) -> bool:
        """«Проверить сейчас» во время ожидания: перепроверить немедленно."""
        if self.wake.is_set():
            self.wake.clear()
            return True
        return False

    def _set_net_wait(self, cmd: str, why: str) -> None:
        """Метка ожидания связи для страницы: время `at` не меняется, пока ждём, — страница не перерисовывает строку."""
        same = self.wait.get("net") is not None and self.wait.get("cmd") == cmd
        self.wait = {"cmd": cmd, "attempt": 0, "at": self.wait["at"] if same else _iso(), "net": why}

    def _net_wait(self, st: dict, v: dict | None = None) -> bool:
        """Ждать, пока связи хватит шагу. Предела нет — маршрут доводит дело до конца; «Остановить» прерывает.
        → дождались ли (False — остановил человек)."""
        G = net_gate()
        cmd = (st["cmd"] + " " + " ".join(st["args"])).strip()
        v = v or self._verdict(st["cmd"])
        started, said = time.time(), [0.0]

        def on_wait(verdict, _secs):
            now = time.time()
            self._set_net_wait(cmd, self._why(verdict))
            if now - said[0] >= 600:             # в журнал — раз в десять минут, а не на каждый опрос
                said[0] = now
                self.say("warn", self.t("route.net_wait_line", cmd=cmd, why=self._why(verdict)))
        on_wait(v, 0)
        ok = G.wait_restored(lambda: self._verdict(st["cmd"], hold=True), stop=self.stop, poll=NET_POLL_S, on_wait=on_wait,
                             woken=self._woken)
        self.wait = {}
        if ok:
            self.say("ok", self.t("route.net_restored", cmd=cmd, took=self._dur(time.time() - started)))
        return ok

    def _net_step(self, st: dict):
        """Шагу не хватает связи? → None (идти дальше) либо результат шага без запуска. Политика решает: отказ,
        пропуск, ожидание или откладывание на конец маршрута."""
        G = net_gate()
        if G is None:
            return None
        v = self._verdict(st["cmd"])
        if v.get("ok", True):
            return None
        return self._net_apply(st, v)

    def _net_apply(self, st: dict, v: dict):
        """Применить политику прогона к вердикту «связи не хватает». → None (дождались, идти дальше) либо результат шага
        без запуска. Вердикт даёт предзапуск (`_net_step`) или отказ самого скрипта источника (`AURORA-NET`)."""
        G = net_gate()
        why, cmd = self._why(v), st["cmd"]
        act = G.decide(self.net_policy, v)
        if act == "fail":
            self.say("err", self.t("route.net_fail", cmd=cmd, why=why))
            self.failed, self.refused, self.offline = cmd, G.blocked_text(cmd, v), True
            return {"rc": 2, "lines": [], "ended": "net"}
        if act == "skip":
            self.say("warn", self.t("route.net_skip", cmd=cmd, why=why))
            self.net_skipped.append(cmd)
            return {"rc": 0, "skipped": True, "net": True, "lines": []}
        if act == "defer":
            self.say("warn", self.t("route.net_defer", cmd=cmd, why=why))
            self.deferred.append(st)
            return {"rc": 0, "skipped": True, "net": True, "deferred": True, "lines": []}
        if not self._net_wait(st, v):
            self.stopped, self.failed = True, self.failed or cmd
            return {"rc": 0, "lines": [], "ended": "stopped"}
        return None

    def _net_precheck(self, steps: list) -> bool:
        """До первого шага: `fail_fast` — не начинать, если чего-то не хватает; `queue` — дождаться всего и идти целиком.
        → False, если маршрут дальше не идёт."""
        G = net_gate()
        if G is None or self.net_policy not in ("fail_fast", "queue"):
            return True
        steps = pending_steps(steps, self.skip)          # при продолжении пройденное связи не требует
        cmds = [st["cmd"] for st in steps]
        lacking: list = []                     # какие команды маршрута остались без связи (по последней проверке)

        def check(hold: bool = False) -> dict:
            try:
                rows = G.check_steps(cmds, self.project, svc=self._net_svc(), hold=hold)
            except Exception:  # noqa: BLE001
                return {"ok": True, "blocked": []}
            lacking[:] = [x["cmd"] for x in rows if not x["ok"]]
            bad = [r for x in rows if not x["ok"] for r in x["blocked"]]
            return {"ok": not bad, "blocked": bad}
        v = check()
        if v["ok"]:
            return True
        why = self._why(v)
        if self.net_policy == "fail_fast":
            self.say("err", self.t("route.net_fail_all", why=why))
            self.failed, self.refused, self.offline = (lacking[0] if lacking else steps[0]["cmd"]), why, True
            self.no_run = True
            return False
        self.say("warn", self.t("route.net_queue_line", why=why))
        started = time.time()
        ok = G.wait_restored(lambda: check(hold=True), stop=self.stop, poll=NET_POLL_S, woken=self._woken,
                             on_wait=lambda verdict, _s: self._set_net_wait(steps[0]["cmd"] if steps else "", self._why(verdict)))
        self.wait = {}
        if not ok:
            self.stopped, self.failed = True, self.failed or (steps[0]["cmd"] if steps else "")
            self.no_run = True
            return False
        self.say("ok", self.t("route.net_restored", cmd=self.sc.get("title", ""), took=self._dur(time.time() - started)))
        return True

    def _run_deferred(self, steps: list) -> bool:
        """Отложенное — после восстановления связи, по очереди, как стояло в маршруте. → False, если маршрут дальше не идёт."""
        self.say("head", self.t("route.net_retry_head", n=len(self.deferred) + len(self.deferred_blocks)))
        self.net_policy = "queue"             # повтор уже ждёт, а не откладывает снова
        # Порядок плана сохраняется: отложенный шаг не обгоняет блок цикла, стоявший перед ним (публикация раньше сборки).
        todo = sorted([(st["pos"], False, st) for st in self.deferred] + [(a, True, (a, b)) for a, b in self.deferred_blocks],
                      key=lambda x: x[0])
        for _pos, is_block, item in todo:
            if is_block:
                if not self._cycle(steps, item[0], item[1]):
                    return False
                self.lap = 0
                continue
            st = item
            if self._stopping():
                self.stopped, self.failed = True, self.failed or st["cmd"]
                return False
            v = self._verdict(st["cmd"], hold=True)
            if not v.get("ok", True) and not self._net_wait(st, v):
                self.stopped, self.failed = True, self.failed or st["cmd"]
                return False
            res = self.run_one(st, retry=True)
            if res.get("ended") or self._halted(st, res):
                return False
        return True

    def exec_step(self, cmd: str, args: list) -> dict:
        def on_job(job_id, name):
            self.job = job_id
            self.on_job(job_id, name)

        def on_lines(lines):
            for line in lines:
                # машинные строки итога и вердикта ворот консоли не показываются
                if not str(line).startswith((self.ck.RS.MARK, self.ck.RS.MARK_GATE)):
                    self.say("out", line)
        res = run_job(self.ck, self.project, cmd, args, stop=self.stop, on_job=on_job,
                      parent=self.run_id, on_lines=on_lines, net=self.net_policy)
        self.job = ""
        if res.get("run_id"):
            self.children.append(res["run_id"])
        if res.get("refused"):
            self.say("err", "■ " + self.t("step.not_started", why=res["refused"]))
        if res.get("stopped"):
            self.stopped = True
        self._read_gate(cmd, args, res)
        return res

    def _read_gate(self, cmd: str, args: list, res: dict) -> None:
        """Вердикт ворот из машинной строки `AURORA-GATE`: в журнал и в запись шага.

        Строка — деталь: шаг без неё (скрипт прошлого выпуска, шаг без адаптера) решает
        кодом выхода. Нарушенные пункты (id и detail) идут в журнал списком — сама строка
        консоли скрыта. У шага-ворот расхождение «код 0, вердикт fail» читается как fail-closed:
        код становится 1 («ворота не пройдены»), а предупреждение `warn` при коде 0 маршрут не
        останавливает (режим внедрения)."""
        gates = self.ck.RS.parse_gate(res.get("lines"))
        if not gates:
            return
        verdict = self.ck.RS.gate_verdict(gates)
        res["gate_verdict"] = verdict
        res["gate_items"] = [dict(it, gate=g["gate"]) for g in gates for it in g["items"]]
        name = gates[0]["gate"] or cmd
        for g in gates:
            if g["verdict"] == "pass":
                continue
            self.say("err" if g["verdict"] == "fail" else "warn",
                     self.t("route.gate_" + g["verdict"], gate=g["gate"] or cmd,
                            n=g["total"]))
            for it in g["items"]:
                self.say("out", self.t("route.gate_item", id=it["id"], detail=it["detail"]))
            if g["total"] > len(g["items"]):      # список обрезан потолком: полное число — в заголовке
                self.say("out", self.t("route.gate_cut", shown=len(g["items"]), total=g["total"]))
        if is_gate(args) and res["rc"] == 0 and verdict == "fail":
            res["rc"] = 1
            res["gate_mismatch"] = True
            self.say("warn", self.t("route.gate_mismatch", gate=name))

    def _offline(self, res: dict) -> bool:
        """Шаг упал по сети? Код 1 с признаками обрыва — да, кроме шага, который сам назвал
        вердикт строкой `AURORA-GATE`: ворота, объявившие итог, отработали."""
        return (res["rc"] == 1 and not res.get("gate_verdict")
                and looks_offline(res["lines"]))

    def step_key(self, st: dict) -> str:
        """Ключ шага в списке «сделано». Подпись «команда + флаги» в маршруте не уникальна:
        цикл и хвост после него зовут одно и то же (`kb:links --cards --apply`), и по
        голой подписи продолжение пропускало хвост как «уже сделанный» (1.170.1). Повторяющейся
        подписи добавляется позиция шага в плане; уникальная остаётся как была — старые
        записи продолжения читаются, а неоднозначные из них шаг просто повторит (шаги
        идемпотентны)."""
        sig = (st["cmd"] + " " + " ".join(st["args"])).strip()
        if self.sig_count.get(sig, 1) > 1 and "pos" in st:
            return f"{sig} @{st['pos']}"
        return sig

    def _event(self, st: dict, start: float, end: float, res: dict) -> None:
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ev = {"cmd": st["cmd"], "args": st["args"], "start": iso(start), "end": iso(end),
              "duration_s": round(end - start), "rc": res["rc"]}
        if res.get("refused"):
            ev["note"] = res["refused"]
        self.events.append(ev)
        key = self.step_key(st)
        if res["rc"] == 0:
            self.done_keys.add(key)
        self.on_step(key, res["rc"])
        self._progress()

    def _state(self, reason: str) -> dict:
        return {"scId": self.sc["id"], "runId": self.run_id, "title": self.sc["title"],
                "write": self.write, "reason": reason, "step": self.done,
                "cycleAt": ({"lap": self.lap, "inLap": self.in_lap}
                            if self.lap and not self.stalled else None),
                "done": sorted(self.skip | self.done_keys),
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}

    def _progress(self) -> None:
        """Сделанное — после каждого шага: перезапуск панели посреди маршрута убивает его, и
        «Продолжить маршрут» обязан знать, с какого места. Конец маршрута запись заменит."""
        try:
            self.ck.write_route_state(self.project, self._state("interrupted"))
        except Exception:  # noqa: BLE001 — запись продолжения не повод ронять шаг
            pass

    def _wait_network(self, st: dict) -> str:
        """Шаг упал по сети: ждём бэкенды и повторяем, пока не вернутся. → continue | failed |
        gated | stopped (gated — ворота ответили кодом 1)."""
        attempt = self.offline_carry + 1
        self.offline_carry = 0
        wait = OFFLINE_RETRY_S
        cmd = (st["cmd"] + " " + " ".join(st["args"])).strip()
        while True:
            known = self._verdict(st["cmd"])
            if not known.get("ok", True):
                # Проверка соединений знает причину (профиль или интернет лежит): ждём восстановления по ней, а не
                # вслепую по пятнадцать минут, и повторяем шаг сразу, как связь вернулась.
                self.offline_attempts = attempt
                if not self._net_wait(st, known):
                    return "stopped"
            else:
                when = datetime.fromtimestamp(time.time() + wait).strftime("%H:%M")
                self.wait = {"cmd": cmd, "attempt": attempt, "at": _iso(time.time() + wait)}
                self.offline_attempts = attempt
                self.say("warn", self.t("route.wait_line", cmd=cmd, n=attempt, time=when))
                slept = self._sleep(wait)
                self.wait = {}
                if not slept:
                    return "stopped"
            start = time.time()
            res = self.exec_step(st["cmd"], st["args"])
            self.last_res = res
            self._event(st, start, time.time(), res)
            # Сеть раньше ворот: код 1 с признаками обрыва — не вердикт, а повод ждать дальше,
            # как и при первом запуске шага.
            if self._offline(res):
                flaky = made_progress(res["lines"])
                if not flaky:
                    attempt += 1
                wait = FLAKY_RETRY_S if flaky else OFFLINE_RETRY_S
                continue
            if halts(st["args"], res["rc"]):
                if res["rc"] == 1 and not self.stopped:
                    return "gated"
                return "stopped" if self.stopped else "failed"
            return "continue"

    def run_one(self, st: dict, retry: bool = False) -> dict:
        sig = (st["cmd"] + " " + " ".join(st["args"])).strip()
        if not retry and self.skip and not st["cycle"] and self.step_key(st) in self.skip:
            self.done += 1
            self.say("note", self.t("route.skip_step", step=self.done, of=self.total,
                                    cmd=st["cmd"]))
            return {"rc": 0, "skipped": True, "lines": []}
        if not retry and self.cycle_at and st["cycle"] and self.lap == 1 \
                and st["cycleIdx"] < self.cycle_at["inLap"]:
            self.in_lap = st["cycleIdx"]
            self.say("note", self.t("route.skip_lap_step", step=st["cycleIdx"],
                                    of=self.cycle_size, cmd=st["cmd"]))
            return {"rc": 0, "skipped": True, "lines": []}
        if st["cycle"]:
            self.in_lap = st["cycleIdx"]
        elif not retry:
            self.done += 1
        lap = self.t("route.lap_mark", lap=self.lap) if self.lap else ""
        where = (self.t("route.where_lap", step=self.in_lap, of=self.cycle_size) if self.lap
                 else self.t("route.where", step=self.done, of=self.total))
        self.bar = {"done": self.done, "total": self.total, "lap": self.lap,
                    "inLap": self.in_lap, "cycleSize": self.cycle_size, "cmd": st["cmd"],
                    "since": _iso()}
        self.say("head", self.t("route.step_head", where=where, lap=lap, cmd=sig, why=st["why"]))
        # Связи не хватает — шаг не начинается (политика прогона): иначе он упал бы таймаутом через минуты.
        gated = None if retry else self._net_step(st)
        if gated is not None:
            return gated
        if self._stopping():                  # «Остановить» пришло, пока ждали связь, — шаг не начинается
            self.stopped, self.failed = True, self.failed or st["cmd"]
            return {"rc": 0, "lines": [], "ended": "stopped"}
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
        # Без потолка перезапусков (1.170.0): маршрут доводит дело до конца, пока человек не
        # прервёт. Сторож снимает шаг не чаще, чем раз в несколько минут тишины, так что
        # повторы сами по себе редкие.
        tries = 0
        while res.get("hung") and not self.stopped:
            tries += 1
            self.say("warn", self.t("route.hung_restart", why=res["hung"], n=tries))
            res = self.exec_step(st["cmd"], st["args"])
        # Скрипт источника сам отказал по связи (`AURORA-NET`: она пропала между проверкой и первым запросом): это не
        # поломка шага, а тот же вердикт «связи не хватает» — политика решает, ждать, откладывать или пропустить.
        G, refusals = net_gate(), 0
        while G is not None and self.net_policy and res["rc"] != 0 and not self.stopped:
            mark = G.parse_marker(res["lines"])
            if not mark:
                break
            out = self._net_apply(st, {"ok": False, "blocked": mark["blocked"]})
            if out is not None:
                return out
            refusals += 1
            # Проверка маршрута и скрипт спорят о связи: пауза растёт, чтобы не запускать скрипт впустую раз в пять секунд.
            if not self._sleep(min(300.0, NET_POLL_S * 2 ** min(refusals, 6))):
                self.stopped, self.failed = True, self.failed or st["cmd"]
                return {"rc": 0, "lines": [], "ended": "stopped"}
            res = self.exec_step(st["cmd"], st["args"])
        end = time.time()
        self.prev_took = end - start
        self._event(st, start, end, res)
        if res.get("refused"):
            self.refused = res["refused"]
        # Хвост вывода шага, «нашедшего, что чинить», — для кнопок «Починить» в итоге.
        self.summary.append({"cmd": st["cmd"], "rc": res["rc"], "gate": is_gate(st["args"]),
                             "summary": self.ck.RS.parse(res["lines"]),
                             "tail": res["lines"][-300:] if res["rc"] == 1 else [],
                             **self._gate_fields(res)})
        if self._offline(res) and not self.stopped:
            how = self._wait_network(st)
            if how == "continue":
                self.summary[-1].update(rc=0, **self._gate_fields(self.last_res))
                return {"rc": 0, "lines": res["lines"]}
            if how == "stopped":
                self.stopped = True
                self.failed = st["cmd"]
                return {"rc": 0, "lines": res["lines"], "ended": how}
            if how == "gated":
                lines = self.last_res.get("lines") or res["lines"]
                self.summary[-1].update(rc=1, tail=lines[-300:], **self._gate_fields(self.last_res))
                return {"rc": 1, "lines": lines}
            return {"rc": 2, "lines": res["lines"]}
        return res

    @staticmethod
    def _gate_fields(res: dict) -> dict:
        """Вердикт ворот для записи шага: пусто, если шаг его не назвал."""
        return ({"gate_verdict": res["gate_verdict"], "gate_items": res.get("gate_items") or []}
                if res.get("gate_verdict") else {})

    def _halted(self, st: dict, res: dict) -> bool:
        """Остановил ли этот шаг маршрут. Заодно помнит, что остановили именно ворота."""
        if not halts(st["args"], res["rc"]):
            return False
        self.failed = st["cmd"]
        if res["rc"] == 1:
            self.gate_failed = st["cmd"]
        return True

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
        for s in steps:
            sig = (s["cmd"] + " " + " ".join(s["args"])).strip()
            self.sig_count[sig] = self.sig_count.get(sig, 0) + 1
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
        G, svc = net_gate(), self._net_svc()
        # Политика нужна только включённой проверке: выключенная ничего не решает и в окружение шага не попадает.
        self.net_policy = G.policy_for(self.net, svc) if (G and svc and svc.enabled) else ""
        if not self._net_precheck(steps):
            return self._finish(self.head0 or "", started)
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
                if self._halted(steps[i], res):
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
        if (self.deferred or self.deferred_blocks) and not (self._stopping() or self.failed or self.stopped or self.stalled):
            if self.trigger == "cron" and self.parent:
                # Повтор — дело цепочки расписания: она вернётся к проекту в конце, а остальные проекты не ждут связи.
                self.net_deferred = [st["cmd"] for st in self.deferred] + [steps[a]["cmd"] for a, _b in self.deferred_blocks]
                self.say("warn", self.t("route.net_handed", n=len(self.net_deferred),
                                        list=", ".join(dict.fromkeys(self.net_deferred))))
            else:
                self._run_deferred(steps)
        return self._finish(head, started)

    def _cycle(self, steps: list, i: int, end: int) -> bool:
        """Блок цикла оборотами. → False, если маршрут дальше не идёт."""
        t = self.t
        prev = {}
        for self.lap in range(1, CYCLE_LIMIT + 1):
            kinds, blocked_lap = {}, False
            for k in range(i, end):
                if self._stopping():
                    self.stopped, self.failed = True, steps[k]["cmd"]
                    return False
                res = self.run_one(steps[k])
                if res.get("ended"):
                    return False
                if res.get("net"):
                    # Шагу не хватило связи: цикл не крутится вхолостую. Отложенный блок повторится целиком после
                    # восстановления (шаги идемпотентны), пропущенный — идёт дальше без него.
                    blocked_lap = True
                    if res.get("deferred"):
                        self.deferred = [d for d in self.deferred if d is not steps[k]]
                        if (i, end) not in self.deferred_blocks:
                            self.deferred_blocks.append((i, end))
                    continue
                if self._halted(steps[k], res):
                    return False
                kinds.update(left_by_kind(res["lines"]))
            if blocked_lap:
                self.say("note", t("route.net_cycle_stop"))
                return True
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
        # gated — ворота качества ответили кодом 1: не сбой, а вердикт; «Продолжить» работает как
        # после failed. Старые записи с failed читаются как раньше.
        gated = bool(self.failed and self.gate_failed == self.failed
                     and not (self.stalled or self.offline or self.stopped))
        reason = ("stall" if self.stalled else "offline" if self.offline
                  else "stopped" if self.stopped else "gated" if gated
                  else "failed" if self.failed else "passed")
        # Шаги, которых не хватило связи (пропущены по partial или отданы расписанию), — маршрут частичный: он остаётся
        # «продолжаемым», не отмечает базу обновлённой и не уходит в push, как законченный.
        partial = reason == "passed" and bool(self.net_skipped or self.net_deferred)
        if self.net_skipped:
            self.say("warn", t("route.net_skipped", n=len(self.net_skipped), list=", ".join(dict.fromkeys(self.net_skipped))))
        # Итоговая фраза — та, что страница писала в конце консоли: теперь она в журнале и
        # видна в истории так же, как была видна вживую.
        if self.stalled:
            self.say("err", t("route.stalled_why"))
        elif bad:
            self.say("err", t("route.stopped_by_you", cmd=bad) if self.stopped
                     else t("route.gated_why", cmd=bad) if gated
                     else t("route.stopped_on", cmd=bad,
                            why=(self.refused + ". ") if self.refused else t("route.cmd_failed")))
        elif partial:
            self.say("warn", t("route.passed_partly", title=self.sc["title"], steps=t("route.steps_n", n=self.done),
                               n=len(set(self.net_skipped + self.net_deferred))))
        else:
            self.say("ok", t("route.passed", title=self.sc["title"],
                             steps=t("route.steps_n", n=self.done)))
        # Дата обновления базы — общая для команды (1.168.0): «Обновить базу» и следом
        # «Починить базу», пройденные целиком. Пишется до итогового коммита и уходит в него.
        if self.write and reason == "passed" and not partial and self.sc.get("id") in ("update", "fix"):
            self._kb_updated()
        if self.write and not self.no_run:
            how = (t("route.how_stalled") if self.stalled
                   else t("route.how_gated", cmd=bad) if gated
                   else t("route.how_stopped", cmd=bad) if bad
                   else t("route.how_partial") if partial else t("route.how_passed"))
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
            if x["rc"] == 1 and not (x.get("gate") and self.gate_failed == x["cmd"]):
                times[x["cmd"]] = times.get(x["cmd"], 0) + 1
        for cmd, n in times.items():
            self.say("warn", t("route.found", cmd=cmd)
                     + (t("route.found_times", n=n) if n > 1 else ""))
        if reason == "passed" and not partial:
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
        if reason == "passed" and not partial:
            ck.clear_route_state(self.project)
        else:
            state = self._state("offline" if partial else reason)
            if reason == "offline" or partial:
                state["attempts"] = getattr(self, "offline_attempts", 0)
            ck.write_route_state(self.project, state)
        head = _head(self.project)
        res = {"ok": reason == "passed", "reason": reason, "failed": self.failed,
               "gated": reason == "gated",
               "progressed": bool(head and head != self.head0), "net_skipped": list(self.net_skipped),
               "net_deferred": list(self.net_deferred), "partial": partial,
               "note": self.refused, "steps": self.done, "lines": lines,
               "run_id": self.run_id,
               "found": [{"cmd": x["cmd"], "lines": x["tail"]} for x in self.summary
                         if x["rc"] == 1 and x.get("tail")]}
        return self._close(res)

    def safe_run(self) -> dict:
        """`run()`, которое всегда заканчивается записью, а не исключением. Кнопка и расписание
        вызывают его, а не `run()`: непойманное исключение оставляло маршрут «идущим»
        (`done_flag` ложный, `meta.json` в статусе running), и кнопка «Пройти» отвечала «в
        проекте уже идёт маршрут» до перезапуска панели."""
        try:
            return self.run()
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "reason": "failed", "failed": "", "note": f"{type(e).__name__}: {e}",
                   "steps": self.done, "lines": [], "run_id": self.run_id, "found": []}
            try:                    # причина и хвост стека — в журнал (и в пункт Cron через `log`)
                t = getattr(self, "t", None)
                head = (t("route.crashed") if t else "■ Маршрут упал: ")
                self.say("err", f"{head}{type(e).__name__}: {e}")
                for line in traceback.format_exc().strip().splitlines()[-6:]:
                    self.say("err", line)
            except Exception:  # noqa: BLE001 — журнал не пишется: закрыть прогон всё равно
                pass
            try:
                return self._close(res)
            except Exception:  # noqa: BLE001 — не записалось: хотя бы снять признак «идёт»
                self.result, self.done_flag = res, True
                return res

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
    return RouteRun(ck, project, sc, write, **kw).safe_run()


def main(argv=None) -> int:
    """`aurora.py route <проект> <маршрут> [--apply]` — маршрут из терминала, без страницы панели.

    Без `--apply` — как кнопка «Посмотреть»: те же шаги без записи. Код выхода: 0 — пройден,
    1 — остановлен (застой, человек, сеть, ворота качества не пройдены — причина gated),
    2 — шаг не отработал (причина failed).
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
        ids = ", ".join(s["id"] for s in all_routes(ck))
        print(f"Маршрута «{a.route}» нет. Есть: {ids}", file=sys.stderr)
        return 2
    res = run_route(ck, project, a.route, a.apply, lang=a.lang,
                    log=lambda text: print(text, flush=True))
    print(f"\n{'✅' if res['ok'] else '■'} {res['reason']}"
          + (f" · {res['failed']}" if res.get("failed") else ""), flush=True)
    # Ворота, не пропустившие маршрут, — вердикт, а не сбой выполнения: выход 1, как у остановки.
    return 0 if res["ok"] else 2 if res["reason"] == "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
