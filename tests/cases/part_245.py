"""Проверки движка Aurora, часть 245: смерть процесса адаптера и что о ней остаётся.

Разбор архива диагностики с Windows (`windows-pydantic-pipe.md`, 19.11.2026) упёрся в
слепоту: тревога `process_died.pipe` несла только класс исключения (`BrokenPipeError`),
а код возврата процесса, его stderr и число ожидающих вызовов нигде не сохранялись. По
такой строке нельзя отличить нативное падение (access violation) от внешнего убийства
процесса, и `fix=install` ведёт в «Установку», хотя venv цел.

Здесь проверяется то, что должно остаться после смерти процесса:

1. Причина вызова несёт `code` (код возврата), `pid` и хвост `stderr` — по ним следующий
   разбор скажет, что именно случилось.
2. Строка тревоги `bot:*` хранит те же `code`/`stderr` в `args` и считает `calls` по
   вызовам, а не по смертям: девять параллельных вызовов, заставших одну мёртвую трубу,
   дают `calls = 9` и **одну** смерть (`_HUB["deaths"] = 1`).
3. `fix` у `process_died.pipe` больше не `install`: устанавливать при разовом падении
   нечего. `install` остаётся там, где лечится установкой (`import_failed.*`,
   `process_died.start`, `process_start`, `incompatible`).
4. Тревога не плодит строк: повторное падение того же пути обновляет `at` и растит
   `calls`, а успешный вызов того же пути через адаптер снимает её (`clear_bypass`),
   не трогая чужие пути.

Подставной адаптер — настоящий процесс на `sys.executable`: печатает `hello`, пишет
`boom` в stderr и выходит с кодом 3. Девять параллельных вызовов идут через подменённый
`adapter_hub`, потому что гонку «жив на проверке — мёртв на записи» настоящим процессом
детерминированно не поймать; проверяется учёт движка, а не расписание ОС.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test

KEY = "bot:тест-трубы-245"
KEY_ONE = "bot:тест-трубы-245-один"
KEY_MANY = "bot:тест-трубы-245-много"

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


@test
def test_pipe_death_keeps_exit_code_and_stderr(tmp: Path):
    """Смерть процесса адаптера оставляет код возврата и stderr, а не только класс исключения.

    На Windows 16.11.2026 тревога сказала «труба закрыта (BrokenPipeError)» — и всё. Ни
    кода возврата, ни последних строк stderr: разбор `windows-pydantic-pipe.md` не смог
    отличить падение в нативном коде от внешнего убийства процесса. Здесь подставной
    адаптер умирает с кодом 3 и пишет `boom`: причина вызова обязана назвать и то, и другое,
    а журнал адаптера — сохранить строку stderr (он это умеет и сегодня).
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
    AG._HUB.update(proc=None, pending={}, broken="", deaths=0, version="")
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
        # это и случилось 16.11. Запись в трубу обязана вернуть причину с кодом и stderr.
        AG.adapter_hub = lambda: (proc, AG._HUB["pending"])
        st, body, err, _dt = AG.pydantic_transport(
            {"n": 1, "url": "http://gw.example.com/v1", "key": ""},
            {"model": "m", "messages": [{"role": "user", "content": "?"}]}, 5)
        assert st is None and body is None and err is not None, (st, body, err)
        cause = getattr(err, "cause", None)
        assert cause is not None, f"причина не доехала до кольца: {err}"
        assert (cause.kind, cause.sub) == ("process_died", "pipe"), (cause.kind, cause.sub, cause)
        assert cause.args.get("code") == 3, \
            f"код возврата не попал в причину: {cause.args} — по нему и отличают падение от убийства"
        assert str(cause.args.get("stderr") or "").strip(), \
            f"хвост stderr не попал в причину: {cause.args}"
        assert "boom" in str(cause.args.get("stderr")), cause.args.get("stderr")

        # fix — не «Установка»: venv цел, устанавливать нечего.
        assert AG.CAUSES["process_died.pipe"]["fix"] != "install", \
            "process_died.pipe по-прежнему ведёт в «Установку» — причина неточна"
        # Одиночная смерть самовосстанавливается: тревога — только с ALARM_AFTER-го события
        # подряд. Иначе разовый сбой вешает красную полосу на неделю (случай 16.11).
        threshold = int(getattr(AG, "ALARM_AFTER", 3))
        for _ in range(max(0, threshold - 1)):
            AG.note_bypass(cause, answered=False)
        if threshold > 1:
            assert AG.read_alarm().get(KEY_ONE) is None, \
                "тревога поднялась раньше порога — одиночная смерть снова висит полосой"
        AG.note_bypass(cause, answered=False)
        row = AG.read_alarm().get(KEY_ONE)
        assert row is not None, "тревога не записалась после порога"
        assert row["args"].get("code") == 3, f"в строке тревоги нет кода возврата: {row}"
        assert "boom" in str(row["args"].get("stderr") or ""), f"в строке тревоги нет stderr: {row}"
        assert int(row.get("calls") or 0) == threshold, \
            f"счётчик вызовов тревоги {row.get('calls')}, а порог {threshold}"
        assert row.get("fix") != "install", f"панель снова предложит «Установку»: {row}"

        # Следующий вызов поднимает новый процесс: смерть не оставляет хаб мёртвым навсегда.
        AG.adapter_hub = saved[2]
        AG._HUB.update(proc=None, pending={}, broken="", deaths=0)
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
        AG._HUB.update(proc=None, pending={}, broken="", deaths=0, version="")
        restore_home()


@test
def test_nine_calls_one_death_and_alarm_is_cleared_by_a_successful_call(tmp: Path):
    """Девять вызовов в мёртвую трубу — одна смерть и девять `calls`; успех снимает тревогу.

    В архиве с Windows стояло `calls: 9`: это девять вызовов одной пачки, заставших уже
    закрытую трубу, а не девять падений. Считать смерть надо по процессу, иначе один сбой
    выглядит как девять и порог «адаптер больше не зовём» (`ADAPTER_DEATHS = 3`) срабатывает
    от одной пачки. Тревога при этом должна обновляться, а не размножаться, и сниматься
    успешным вызовом того же пути через адаптер — чужой путь не трогается.
    """
    AG = _engine()
    restore_home = set_home(tmp / "home")
    saved = (AG.adapter_hub, AG.record_failure, AG.run_path)
    AG.record_failure = lambda *a, **k: None
    AG.run_path = lambda: KEY_MANY
    AG._write_alarm({})
    proc = _DeadProc()
    calls_n = max(9, int(getattr(AG, "ALARM_AFTER", 3)))
    AG._HUB.update(proc=proc, pending={}, broken="", deaths=0, version="2.66.0")
    AG.adapter_hub = lambda: (proc, AG._HUB["pending"])
    backend = {"n": 1, "url": "http://gw.example.com/v1", "key": ""}
    errors = []
    try:
        def call():
            _st, _body, err, _dt = AG.pydantic_transport(
                backend, {"model": "m", "messages": [{"role": "user", "content": "?"}]}, 5)
            assert err is not None and getattr(err, "cause", None) is not None, err
            errors.append(err.cause)
            AG.note_bypass(err.cause, answered=False)

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
        row = AG.read_alarm().get(KEY_MANY)
        assert row is not None, "тревога не записалась"
        assert int(row.get("calls") or 0) == calls_n, \
            f"вызовов в тревоге {row.get('calls')}, а не {calls_n}"

        # Повтор обновляет ту же строку: новая метка, счётчик растёт, ключ один.
        AG.note_bypass(errors[0], answered=False)
        again = AG.read_alarm()
        assert list(again) == [KEY_MANY], f"тревога размножилась: {list(again)}"
        assert int(again[KEY_MANY].get("calls") or 0) == calls_n + 1, again[KEY_MANY]

        # Строка читается панелью обратно тем же разбором — с кодом и stderr.
        cause = AG.alarm_cause(again[KEY_MANY])
        assert (cause.kind, cause.sub) == ("process_died", "pipe"), (cause.kind, cause.sub)
        assert cause.args.get("code") == 3, cause.args
        # Хвост stderr едет вместе с причиной; пустой он здесь потому, что подставной
        # процесс не писал в stderr (настоящий случай — в первой проверке).
        assert "stderr" in cause.args, cause.args

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
    finally:
        AG.adapter_hub, AG.record_failure, AG.run_path = saved
        AG._HUB.update(proc=None, pending={}, broken="", deaths=0, version="")
        try:
            data = AG.read_alarm()
            for k in (KEY_MANY, "bot:чужой-245"):
                data.pop(k, None)
            AG._write_alarm(data)
        except Exception:  # noqa: BLE001, S110
            pass
        restore_home()
