"""Проверки движка Aurora, часть 245: смерть процесса адаптера и что о ней остаётся.

Разбор архива диагностики с Windows (`windows-pydantic-pipe.md`, 19.11.2026) упёрся в
слепоту: тревога `process_died.pipe` несла только класс исключения (`BrokenPipeError`),
а код возврата процесса, его stderr и число ожидающих вызовов нигде не сохранялись. По
такой строке нельзя отличить нативное падение (access violation) от внешнего убийства
процесса, и `fix=install` ведёт в «Установку», хотя venv цел.

Второй разбор (p8, 19.11.2026) нашёл счёт: тревога считала **вызовы**, а не смерти. Девять
вызовов одной пачки, заставших уже закрытую трубу, давали `calls: 9` и взводили порог
`ALARM_AFTER` от одной смерти — красная полоса на всю машину из-за одного падения.

Здесь проверяется то, что должно остаться после смерти процесса:

1. Причина вызова несёт `code` (код возврата), `pid`, число ожидавших `pending` и хвост
   `stderr` — по ним следующий разбор скажет, что именно случилось.
2. Порог тревоги считается **по смертям**: девять параллельных вызовов, заставших одну
   мёртвую трубу, — это одна смерть и ни одной тревоги; тревога поднимается с третьей
   смерти подряд (`ALARM_AFTER`), а не с третьего вызова.
3. `fix` у `process_died.pipe` больше не `install`: устанавливать при разовом падении
   нечего. `install` остаётся там, где лечится установкой (`import_failed.*`,
   `process_died.start`, `process_start`, `incompatible`).
4. Строка тревоги одна на путь: повторная смерть обновляет её, а не плодит копии. Успешный
   вызов того же пути через адаптер снимает тревогу и обнуляет серию смертей, чужой путь
   не трогает.
5. Скрытие («на сутки») прячет тревогу от панели, но не выключает проверку; просроченная
   тревога (`ADAPTER_ALARM_TTL`) снимается сама, а свежая — видна.

Подставной адаптер — настоящий процесс на `sys.executable`: печатает `hello`, пишет
`boom` в stderr и выходит с кодом 3. Вызовы в мёртвую трубу идут через подменённый
`adapter_hub`, потому что гонку «жив на проверке — мёртв на записи» настоящим процессом
детерминированно не поймать; проверяется учёт движка, а не расписание ОС. Смерти считает
сам движок в `_HUB["deaths"]`, а проверка лишь ставит ему мёртвый процесс.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test

KEY_ONE = "bot:тест-трубы-245-один"
KEY_MANY = "bot:тест-трубы-245-много"

BACKEND = {"n": 1, "url": "http://gw.example.com/v1", "key": ""}
PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "?"}]}
BOOM = "boom: adapter died before the task"

# Подставной адаптер: приветствие, строка в stderr, короткая пауза и выход с кодом 3.
# Пауза нужна, чтобы движок успел прочитать hello и вернуть процесс до его смерти —
# иначе проверка ловила бы `process_died.start`, а не закрытую трубу.
FAKE_ADAPTER = '''\
import json, os, sys, time
sys.stdout.write(json.dumps({"ready": True, "version": "2.66.0"}) + "\\n")
sys.stdout.flush()
sys.stderr.write("boom: adapter died before the task\\n")
sys.stderr.flush()
time.sleep(0.15)
os._exit(3)
'''


def _engine():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import agent_core as AG
    return AG


def _panel():
    """Панель: тот же разбор строк тревоги, каким её видит человек в красной полосе."""
    import aurora_cockpit as ck
    return ck


class _DeadPipe:
    """stdin мёртвого процесса: запись задания рвётся так же, как у закрытой трубы."""

    def write(self, _line):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass

    def close(self):
        pass


class _DeadProc:
    """Уже умерший процесс адаптера: `poll()` знает код, запись в трубу рвётся."""

    def __init__(self, code=3, pid=4242):
        self.pid = pid
        self.stdin = _DeadPipe()
        self.stderr = io.StringIO("")
        self.stdout = io.StringIO("")
        self._code = code
        self.killed = False

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        return self._code

    def kill(self):
        self.killed = True

    def close(self):
        pass


def _log_lines(AG) -> list:
    try:
        return AG.adapter_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _wait(predicate, seconds=5.0, step=0.05) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _death_counted(AG, proc) -> bool:
    with AG._HUB_LOCK:
        return any(p is proc for p in (AG._HUB.get("dead") or {}).values())


def _hub_reset(AG, proc=None) -> None:
    """Чистое состояние хаба: ноль смертей, новое кольцо stderr, пустое ожидание.

    Кит 1.231.12 держит кольца stderr по поколениям процессов (`stderr_gen`), а прежний —
    одно кольцо: новое поколение берём у самого движка, если он это умеет, иначе чистим
    единственное. Иначе хвост stderr из соседней проверки уехал бы в причину этой.

    Читатель прежнего процесса — отдельный поток: он может досчитать смерть уже после
    сброса счётчика. Ждём его stderr-поток и учёт смерти вне `_HUB_LOCK`, иначе замок
    не даст читателю завершиться, а смерть из прошлой проверки попадёт в новую серию.
    """
    with AG._HUB_LOCK:
        old = AG._HUB.get("proc")
    stderr_reader = getattr(old, "_aurora_stderr_thread", None)
    if stderr_reader is not None:
        stderr_reader.join(5)
        assert not stderr_reader.is_alive(), "stderr-читатель прежнего процесса не завершился"
        assert _wait(lambda: _death_counted(AG, old), 5), \
            "читатель прежнего процесса не успел учесть смерть до сброса"
    new_gen = getattr(AG, "_hub_stderr_new_generation_locked", None)
    with AG._HUB_LOCK:
        gen = int(new_gen() if callable(new_gen) else AG._HUB.get("stderr_gen") or 0)
        AG._HUB.update(proc=proc, pending={}, broken="", deaths=0, version="2.66.0")
        if "proc_gen" in AG._HUB:
            AG._HUB["proc_gen"] = gen
    buf = AG._HUB.get("stderr")
    if hasattr(buf, "clear"):
        buf.clear()


def _hub_put(AG, proc) -> None:
    """Поставить хабу очередной процесс, не трогая счёт смертей: они копятся за прогон."""
    AG._HUB.update(proc=proc, pending={}, broken="", version="2.66.0")


def _piped(AG):
    """Один вызов в мёртвую трубу; хаб уже подменён. → причина отказа."""
    _st, _body, err, _dt = AG.pydantic_transport(BACKEND, PAYLOAD, 5)
    assert err is not None and getattr(err, "cause", None) is not None, err
    return err.cause


def _death(AG, pid, code=3, stderr=BOOM):
    """Одна смерть процесса: хаб отдаёт мёртвый процесс, вызов рвётся на записи.

    `deaths` считает сам движок (`_hub_count_death_locked`): первый писавший увеличивает
    счётчик, остальные видят уже посчитанную смерть. → (процесс, причина отказа).
    """
    proc = _DeadProc(code=code, pid=pid)
    _hub_put(AG, proc)
    AG._hub_stderr_add(stderr)
    AG.adapter_hub = lambda: (proc, AG._HUB["pending"])
    return proc, _piped(AG)


def _shown(ck) -> list:
    """Строки панели: она при показе снимает просроченные тревоги, поэтому спрашиваем один раз."""
    return ck.adapter_alarm()


def _keys(rows, key) -> list:
    return [row["key"] for row in rows if row["key"] == key]


def _threshold(AG) -> int:
    return int(getattr(AG, "ALARM_AFTER", 3))


@test
def test_pipe_death_keeps_exit_code_and_stderr(tmp: Path):
    """Смерть процесса адаптера оставляет код, pid, число ожидавших и stderr, а не класс ошибки.

    На Windows 16.11.2026 тревога сказала «труба закрыта (BrokenPipeError)» — и всё. Ни
    кода возврата, ни последних строк stderr: разбор `windows-pydantic-pipe.md` не смог
    отличить падение в нативном коде от внешнего убийства процесса. Здесь подставной
    адаптер умирает с кодом 3 и пишет `boom`: причина вызова обязана назвать и то, и другое,
    а журнал адаптера — сохранить строку stderr (он это умеет и сегодня).

    Заодно проверяется, что одна смерть не поднимает тревогу, сколько бы вызовов её ни
    застало: p8 нашёл, что счёт шёл по вызовам, и одна пачка вешала полосу.
    """
    AG = _engine()
    restore_home = set_home(tmp / "home")
    fake = tmp / "fake_adapter.py"
    fake.write_text(FAKE_ADAPTER, encoding="utf-8")
    before = len(_log_lines(AG))
    saved = (AG._adapter_argv, AG.adapter_selfcheck, AG.adapter_hub,
             AG.record_failure, AG.run_path)
    procs = []
    AG._adapter_argv = lambda: [sys.executable, str(fake)]
    # Самопроверка совместимости — не предмет этой проверки: подставной адаптер её не знает.
    AG.adapter_selfcheck = lambda version="", force=False: {"ok": True, "problems": []}
    AG.record_failure = lambda *a, **k: None      # журнал сбоев прогона здесь не нужен
    AG.run_path = lambda: KEY_ONE
    _hub_reset(AG)
    AG._write_alarm({})
    try:
        proc, _pending = AG.adapter_hub()
        assert proc is not None, "подставной адаптер не поднялся"
        procs.append(proc)
        assert _wait(lambda: proc.poll() is not None), "подставной адаптер не умер за 5 с"
        assert proc.poll() == 3, f"подставной адаптер вышел с кодом {proc.poll()}, а не 3"
        # Поток движка читает stderr и кладёт строки в журнал: к моменту разбора они уже там.
        log = "\n".join(_log_lines(AG)[before:])
        assert "boom" in log, f"stderr процесса не попал в журнал адаптера:\n{log}"

        # Гонка «жив на проверке — мёртв на записи»: хаб отдаёт уже умерший процесс, как
        # это и случилось 16.11. Запись в трубу обязана вернуть причину с фактами.
        AG.adapter_hub = lambda: (proc, AG._HUB["pending"])
        cause = _piped(AG)
        assert (cause.kind, cause.sub) == ("process_died", "pipe"), (cause.kind, cause.sub, cause)
        assert cause.args.get("code") == 3, \
            f"код возврата не попал в причину: {cause.args} — по нему и отличают падение от убийства"
        assert int(cause.args.get("pid") or 0) == int(proc.pid), \
            f"pid не попал в причину: {cause.args}"
        assert isinstance(cause.args.get("pending"), int), \
            f"число ожидавших вызовов не попало в причину: {cause.args}"
        assert str(cause.args.get("stderr") or "").strip(), \
            f"хвост stderr не попал в причину: {cause.args}"
        assert "boom" in str(cause.args.get("stderr")), cause.args.get("stderr")
        assert int(cause.args.get("deaths") or 0) == 1, \
            f"движок насчитал смертей {cause.args.get('deaths')}, а была одна: {cause.args}"

        # fix — не «Установка»: venv цел, устанавливать нечего.
        assert AG.CAUSES["process_died.pipe"]["fix"] != "install", \
            "process_died.pipe по-прежнему ведёт в «Установку» — причина неточна"

        # Одна смерть — не повод для тревоги, сколько бы вызовов её ни застало. До p8 здесь
        # считались вызовы, и третья нить одной пачки вешала красную полосу на машину.
        for _ in range(_threshold(AG) + 5):
            AG.note_bypass(cause, answered=False)
        assert AG.read_alarm().get(KEY_ONE) is None, \
            f"одна смерть подняла тревогу: {AG.read_alarm().get(KEY_ONE)}"
        ck = _panel()
        rows = _shown(ck)
        assert _keys(rows, KEY_ONE) == [], rows

        # Следующий вызов поднимает новый процесс: смерть не оставляет хаб мёртвым навсегда.
        AG.adapter_hub = saved[2]
        _hub_reset(AG)
        proc2, _p2 = AG.adapter_hub()
        assert proc2 is not None, "после смерти процесса новый не поднялся"
        procs.append(proc2)
        starts = [ln for ln in _log_lines(AG)[before:] if " start pid=" in ln]
        assert len(starts) >= 2, f"в журнале не два запуска, а {len(starts)}: {starts}"
    finally:
        for p in procs:
            # Трубы закрываем сами: буфер убитого процесса иначе пытается сбросить
            # остаток задания в мёртвую трубу при сборке мусора и шумит в вывод прогона.
            for pipe in ("stdin", "stdout", "stderr"):
                try:
                    getattr(p, pipe).close()
                except (OSError, ValueError):
                    pass
            try:
                p.kill()
            except OSError:
                pass
        try:
            data = AG.read_alarm()
            data.pop(KEY_ONE, None)
            AG._write_alarm(data)
            AG.clear_bypass()          # сбросить и серию pipe-смертей этого пути
        except Exception:  # noqa: BLE001, S110 — уборка не повод ронять проверку
            pass
        AG._adapter_argv, AG.adapter_selfcheck, AG.adapter_hub, \
            AG.record_failure, AG.run_path = saved
        _hub_reset(AG)
        restore_home()


@test
def test_nine_calls_one_death_do_not_arm_and_three_deaths_do(tmp: Path):
    """Порог — по смертям: девять вызовов одной пачки тревогу не поднимают, три смерти подряд — да.

    В архиве с Windows стояло `calls: 9`: это девять вызовов одной пачки, заставших уже
    закрытую трубу, а не девять падений (p8). Считать смерть надо по процессу, иначе один
    сбой выглядит как девять и порог срабатывает от одной пачки. Тревога при этом должна
    обновляться, а не размножаться, и сниматься успешным вызовом того же пути через
    адаптер — чужой путь при этом не трогается.
    """
    AG = _engine()
    ck = _panel()
    restore_home = set_home(tmp / "home")
    saved = (AG.adapter_hub, AG.record_failure, AG.run_path)
    AG.record_failure = lambda *a, **k: None
    AG.run_path = lambda: KEY_MANY
    AG._write_alarm({})
    AG.clear_bypass()
    _hub_reset(AG)
    calls_n = max(9, _threshold(AG))
    proc = _DeadProc(pid=4242)
    _hub_put(AG, proc)
    AG._hub_stderr_add(BOOM)
    AG.adapter_hub = lambda: (proc, AG._HUB["pending"])
    errors = []
    try:
        def call():
            cause = _piped(AG)
            errors.append(cause)
            AG.note_bypass(cause, answered=False)

        threads = [threading.Thread(target=call) for _ in range(calls_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert len(errors) == calls_n, f"с причиной вернулись {len(errors)} из {calls_n} вызовов"
        assert all(c.kind == "process_died" and c.sub == "pipe" for c in errors), \
            {(c.kind, c.sub) for c in errors}
        assert AG._HUB["deaths"] == 1, \
            f"одна смерть процесса посчитана как {AG._HUB['deaths']} — порог сработает от одной пачки"
        assert {int(c.args.get("deaths") or 0) for c in errors} == {1}, \
            f"вызовы одной пачки говорят о разных смертях: {[c.args.get('deaths') for c in errors]}"

        # Одна смерть на девять вызовов — тревоги нет ни в файле, ни на полосе панели.
        assert AG.read_alarm().get(KEY_MANY) is None, \
            f"девять вызовов взвели тревогу по одной смерти: {AG.read_alarm().get(KEY_MANY)}"
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows

        # Вторая смерть — ещё не порог: серия продолжается, но тревоги нет.
        _proc2, cause2 = _death(AG, pid=4201)
        assert int(cause2.args.get("deaths") or 0) == 2, cause2.args
        AG.note_bypass(cause2, answered=False)
        assert AG.read_alarm().get(KEY_MANY) is None, \
            f"тревога поднялась на второй смерти: {AG.read_alarm().get(KEY_MANY)}"
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows

        # Третья смерть подряд — порог: строка тревоги одна и несёт факты последней смерти.
        proc3, cause3 = _death(AG, pid=4202, stderr="boom: third death")
        assert int(cause3.args.get("deaths") or 0) == _threshold(AG), cause3.args
        AG.note_bypass(cause3, answered=False)
        row = AG.read_alarm().get(KEY_MANY)
        assert row is not None, "тревога не записалась на третьей смерти подряд"
        args = row.get("args") or {}
        assert args.get("code") == 3, f"в строке тревоги нет кода возврата: {row}"
        assert int(args.get("pid") or 0) == proc3.pid, f"в строке тревоги не тот pid: {row}"
        assert isinstance(args.get("pending"), int), f"в строке тревоги нет pending: {row}"
        assert "third death" in str(args.get("stderr") or ""), f"в строке тревоги нет stderr: {row}"
        assert int(args.get("deaths") or 0) == _threshold(AG), \
            f"в строке тревоги не число смертей: {row}"
        assert row.get("fix") != "install", f"панель снова предложит «Установку»: {row}"
        shown = _shown(ck)
        assert _keys(shown, KEY_MANY) == [KEY_MANY], shown
        assert shown[0]["args"].get("pid") == proc3.pid, shown[0]

        # Четвёртая смерть обновляет ту же строку: тревога не размножается.
        _proc4, cause4 = _death(AG, pid=4203)
        AG.note_bypass(cause4, answered=False)
        again = AG.read_alarm()
        assert list(again) == [KEY_MANY], f"тревога размножилась: {list(again)}"
        rows = _shown(ck)
        assert len(_keys(rows, KEY_MANY)) == 1, rows

        # Чужой путь тревогу не снимает, свой — снимает успешный вызов через адаптер.
        other = AG.Why("process_died", "pipe", exc="BrokenPipeError", code=3, stderr="boom")
        data = AG.read_alarm()
        data["bot:чужой-245"] = {**other.row(), "at": AG.utc_stamp(),
                                 "project": "/x/PRJ", "path": "bot:чужой-245", "calls": 1}
        AG._write_alarm(data)
        AG.clear_bypass()
        left = AG.read_alarm()
        assert KEY_MANY not in left, "успешный вызов не снял тревогу своего пути"
        assert "bot:чужой-245" in left, "снялась чужая тревога"
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows

        # После успеха серия начинается заново: новая смерть идёт в прогоне уже пятым номером,
        # но в серии она первая — иначе снятая тревога возвращалась бы от любого следующего сбоя.
        _proc5, cause5 = _death(AG, pid=4204)
        assert int(cause5.args.get("deaths") or 0) > _threshold(AG), cause5.args
        AG.note_bypass(cause5, answered=False)
        assert AG.read_alarm().get(KEY_MANY) is None, \
            f"после успешного вызова одна смерть снова подняла тревогу: {AG.read_alarm().get(KEY_MANY)}"
    finally:
        AG.adapter_hub, AG.record_failure, AG.run_path = saved
        _hub_reset(AG)
        try:
            data = AG.read_alarm()
            for k in (KEY_MANY, "bot:чужой-245"):
                data.pop(k, None)
            AG._write_alarm(data)
            AG.clear_bypass()
            AG.clear_alarm_snooze(KEY_MANY)
        except Exception:  # noqa: BLE001, S110
            pass
        restore_home()


@test
def test_pipe_death_alarm_hides_on_snooze_and_expires_by_ttl(tmp: Path):
    """Скрытие прячет тревогу от панели, не выключая проверку; просроченная тревога снимается сама.

    «Скрыть» — обещание человеку: полосы не будет сутки, но проверка идёт своим чередом, и
    новая смерть снова ложится в файл (панель её просто не показывает, пока срок не вышел).
    Тревога адаптера — не приговор на неделю: если успешного прохода через адаптер так и не
    случилось, панель перестаёт её показывать по сроку `ADAPTER_ALARM_TTL` и снимает запись.
    Обе проверки идут через ту же функцию панели, что рисует красную полосу: важно, что
    видит человек, а не внутренний помощник.
    """
    AG = _engine()
    ck = _panel()
    restore_home = set_home(tmp / "home")
    saved = (AG.adapter_hub, AG.record_failure, AG.run_path)
    AG.record_failure = lambda *a, **k: None
    AG.run_path = lambda: KEY_MANY
    AG._write_alarm({})
    AG.clear_bypass()
    _hub_reset(AG)
    try:
        for n in range(1, _threshold(AG) + 1):
            _proc, cause = _death(AG, pid=4300 + n)
            AG.note_bypass(cause, answered=False)
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [KEY_MANY], rows
        assert KEY_MANY in AG.read_alarm(), AG.read_alarm()

        # Скрытие: строки нет в файле, панель её не показывает, ключ числится скрытым.
        assert ck.forget_link_alarm(KEY_MANY) is True, "тревога бота не скрывается"
        assert KEY_MANY not in AG.read_alarm(), AG.read_alarm()
        assert AG.alarm_snoozed(KEY_MANY) is True, AG.read_alarm_snooze()
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows

        # Смерть повторилась, пока человек не хочет видеть полосу: проверка не выключена —
        # запись снова в файле, а панель прячет её до срока.
        _proc2, cause2 = _death(AG, pid=4400)
        AG.note_bypass(cause2, answered=False)
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows
        # Срок вышел или человек снял скрытие — та же запись снова видна.
        AG.clear_alarm_snooze(KEY_MANY)
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [KEY_MANY], rows

        # Успешный вызов снимает тревогу: полосы нет и в файле, и на панели.
        AG.clear_bypass()
        assert KEY_MANY not in AG.read_alarm(), AG.read_alarm()
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows

        # Просроченная тревога: метка старше срока — панель не показывает и снимает запись.
        ttl = int(getattr(AG, "ADAPTER_ALARM_TTL", 12 * 3600))
        old_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ttl - 120))
        stale = AG.Why("process_died", "pipe", exc="BrokenPipeError", code=3,
                       pid=4500, pending=0, stderr=BOOM, deaths=_threshold(AG))
        AG._write_alarm({KEY_MANY: {**stale.row(), "at": old_at, "project": "/x/PRJ",
                                    "path": KEY_MANY, "calls": _threshold(AG)}})
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [], rows
        assert KEY_MANY not in AG.read_alarm(), "просроченная тревога осталась в файле"

        # Свежая тревога того же пути видна: срок считает возраст, а не сам факт записи.
        AG._write_alarm({KEY_MANY: {**stale.row(), "at": AG.utc_stamp(), "project": "/x/PRJ",
                                    "path": KEY_MANY, "calls": _threshold(AG)}})
        rows = _shown(ck)
        assert _keys(rows, KEY_MANY) == [KEY_MANY], rows
    finally:
        AG.adapter_hub, AG.record_failure, AG.run_path = saved
        _hub_reset(AG)
        try:
            AG._write_alarm({})
            AG.clear_bypass()
            AG.clear_alarm_snooze(KEY_MANY)
        except Exception:  # noqa: BLE001, S110
            pass
        restore_home()
