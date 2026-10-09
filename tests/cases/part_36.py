"""Проверки движка Aurora, часть 36: пульс шага в Консоли (1.170.0).

Каркас и помощники — tests/harness.py. Ответ модели приходит целиком, и шаг молчит
десятки минут: «выполняется» без изменений не отличало «ждёт модель» от «завис». PRJ-C
09.10.2026: шлюз отдавал 4 токена в секунду вместо 45, двадцать минут в Консоли не менялось
ни слова, и человек спрашивал, ждать или перезапускать. Движок пишет идущие запросы к
моделям в папку прогона, сервер выносит вердикт, Консоль показывает строку пульса.
"""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import os
import sys
import threading
import time

from harness import KIT, SCRIPTS, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_a_model_request_is_in_the_pulse_while_it_waits(tmp: Path):
    """Запрос к модели на время ожидания лежит в `pulse-<pid>.json` папки прогона: шлюз,
    модель, срок; ответ убирает его и считает ответ или сбой. Без папки прогона файла нет."""
    _path()
    import agent_core as AG
    seen = {}
    saved = (AG._transport, os.environ.get("AURORA_RUN_DIR"), dict(AG._PULSE))
    pulse = tmp / f"pulse-{os.getpid()}.json"

    def slow(kind, backend, payload, timeout):
        seen["during"] = json.loads(pulse.read_text(encoding="utf-8")) if pulse.exists() else None
        if payload.get("fail"):
            return 503, None, "HTTP 503", 0.1
        return 200, {"choices": []}, "", 0.1
    try:
        AG._transport = slow
        AG._PULSE.update(seq=0, calls={}, answered=0, failed=0, last_answer=0.0, last_fail=0.0,
                         why="", path=None)
        os.environ["AURORA_RUN_DIR"] = str(tmp)
        backend = {"n": 1, "name": "Шлюз №1", "url": "http://example.com/v1", "key": ""}
        AG.default_transport("chat", backend, {"model": "m-1", "role": "worker"}, 1200)
        call = seen["during"]["inflight"]
        assert len(call) == 1 and call[0]["model"] == "m-1" and call[0]["limit"] == 1200, call
        assert call[0]["name"] == "Шлюз №1" and call[0]["role"] == "worker", call
        AG.default_transport("chat", backend, {"model": "m-1", "fail": True}, 1200)
        after = json.loads(pulse.read_text(encoding="utf-8"))
        assert after["inflight"] == [] and after["answered"] == 1 and after["failed"] == 1, after
        assert after["last_answer"] and "503" in after["why"], after
        AG._pulse_drop()
        assert not pulse.exists(), "пульс закончившегося процесса остался в папке прогона"
        os.environ.pop("AURORA_RUN_DIR")
        AG._PULSE["path"] = None
        AG.default_transport("chat", backend, {"model": "m-1"}, 5)
        assert not list(tmp.glob("pulse-*.json")), "без панели движок намусорил пульсом"
    finally:
        AG._transport = saved[0]
        if saved[1] is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = saved[1]
        AG._PULSE.clear()
        AG._PULSE.update(saved[2])


class _Proc:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


def _job(tmp: Path, now: float, quiet: float, alive=True, parent="r1") -> dict:
    project = tmp / "proj"
    run = project / ".aurora" / "runs" / "run-1"
    run.mkdir(parents=True, exist_ok=True)
    return {"id": "j1", "project": str(project), "run_id": "run-1", "proc": _Proc(alive),
            "started": now - 1500, "last_out": now - quiet, "parent": parent, "done": False}


def _pulse(job: dict, pid: int, calls: list, last_answer: float = 0.0, answered=0, failed=0):
    folder = Path(job["project"]) / ".aurora" / "runs" / job["run_id"]
    (folder / f"pulse-{pid}.json").write_text(json.dumps(
        {"pid": pid, "inflight": calls, "answered": answered, "failed": failed,
         "last_answer": last_answer}), encoding="utf-8")


@test
def test_the_pulse_tells_waiting_from_hanging(tmp: Path):
    """Вердикт пульса: свежая строка — работает; молчит, но запрос к модели в сроке — ждёт
    модель; запрос дольше срока — срок вышел; молчит без запросов — тишина, со сроком
    сторожа; процесса нет — не найден. Пульс мёртвого процесса не считается."""
    _path()
    import aurora_cockpit as ck
    import watchdog as WD
    now = time.time()
    job = _job(tmp, now, quiet=10)
    assert ck.job_pulse(job, now)["state"] == "ok"
    job = _job(tmp, now, quiet=400)
    call = {"since": now - 700, "limit": 1200, "n": 1, "name": "Шлюз №1", "model": "m-1"}
    _pulse(job, os.getpid(), [call, dict(call, since=now - 30)], last_answer=now - 900,
           answered=2, failed=9)
    p = ck.job_pulse(job, now)
    assert p["state"] == "wait" and p["calls"] == 2 and p["oldest"] == 700, p
    assert p["limit"] == 1200 and p["where"] == "Шлюз №1 · m-1" and p["answer_ago"] == 900, p
    assert (p["answered"], p["failed"]) == (2, 9), p
    _pulse(job, os.getpid(), [dict(call, since=now - 1300)])
    assert ck.job_pulse(job, now)["state"] == "late"
    _pulse(job, os.getpid(), [], last_answer=now - 20)
    assert ck.job_pulse(job, now)["state"] == "ok", "свежий ответ модели — шаг работает"
    job = _job(tmp, now, quiet=WD.QUIET - 100)
    _pulse(job, os.getpid(), [])
    assert ck.job_pulse(job, now)["state"] == "ok", "молчит меньше порога сторожа — работает"
    job = _job(tmp, now, quiet=WD.QUIET + 600)
    _pulse(job, 2 ** 22 + 12345, [call])          # процесса с таким номером нет
    p = ck.job_pulse(job, now)
    assert p["state"] == "quiet" and p["calls"] == 0, "пульс мёртвого процесса посчитан"
    assert p["watchdog"] and p["watchdog_in"] == WD.HARD - WD.QUIET - 600, p
    assert ck.job_pulse(_job(tmp, now, quiet=5, alive=False), now)["state"] == "gone"


@test
def test_every_console_stream_carries_and_draws_the_pulse(tmp: Path):
    """Пульс приходит с заданием, маршрутом и цепочкой и рисуется во всех четырёх потоках
    Консоли; строка — под консолью, тексты — в каталогах."""
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'reply["pulse"] = None if reply["done"] else job_pulse(job)' in src
    assert '"pulse": None if run.done_flag else pulse_of(run.job)' in src
    assert 'got["pulse"] = cron_pulse()' in src
    ui = ui_source()
    assert 'id="consolePulse"' in ui and "function drawPulse(" in ui
    for where in ("drawPulse((d && !d.done && !d.error) ? d.pulse : null, me)",   # команда
                  "drawPulse(d.done ? null : d.pulse, me);",                       # маршрут, подключение
                  "drawPulse(d.run ? d.pulse : null, me);"):                       # цепочка
        assert where in ui, f"поток Консоли без пульса: {where}"
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("pulse.ok", "pulse.wait", "pulse.late", "pulse.quiet", "pulse.gone",
                    "pulse.calls", "pulse.checked"):
            assert cat.get(key), f"{lang}: нет строки {key}"


@test
def test_a_dead_vector_gateway_is_reported_once_by_many_threads(tmp: Path):
    """Шестнадцать потоков узнают упавший шлюз векторов разом — строка о нём одна, с номером
    шлюза, а не шестнадцать одинаковых с его адресом."""
    _path()
    import kb_embed as E
    saved = (E.endpoints, E.AG.http_json, E.load_index)
    gate = threading.Barrier(16)

    def dead(url, body, key, timeout):
        try:
            gate.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return None, None, "URLError: timed out", 0
    E.endpoints = lambda cfg: [{"url": "http://example.com/v1", "key": "", "n": 1}]
    E.AG.http_json = dead
    E.load_index = lambda: {}
    E.DEAD.clear()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            threads = [threading.Thread(target=E.embed, args=(["вопрос"], {"request_timeout": 1}, "m"))
                       for _ in range(16)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(10)
        lines = [ln for ln in err.getvalue().splitlines() if "до конца прогона без него" in ln]
        assert len(lines) == 1, f"строк об упавшем шлюзе: {len(lines)}"
        assert "№1" in lines[0] and "example.com" not in lines[0], lines[0]
    finally:
        E.endpoints, E.AG.http_json, E.load_index = saved
        E.DEAD.clear()


# ---------------------------------------------------------------- «довести до конца»
# Шаги маршрута и цепочки (`AURORA_PERSIST=1`) идут без потолков по времени: вызов модели не
# сдаётся по сроку и обрыву, срок запроса на медленном шлюзе растёт, бюджета шага нет, маршрут
# ждёт сеть и перезапускает зависший шаг без предела. PRJ-C 09.10.2026: «система должна сама
# ловить сбои, перезапускаться и доводить прогон до конца, если только человек не прервёт».

ANSWER = {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}],
          "usage": {"completion_tokens": 10}}


def _ring(AG):
    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gw.example.com/v1",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    cfg["persist"] = True
    for d in (AG.DOWN, AG.SLOW, AG.LAST_OK):
        d.clear()
    return cfg


@test
def test_a_slow_gateway_gets_more_time_instead_of_a_failure(tmp: Path):
    """Шлюз не уложился в срок — в маршруте это не сбой источника: запрос повторяется, и
    срок на этом шлюзе каждый раз вдвое больше; ответ приходит, медленный шлюз не в карантине.
    В строках хода — запрос, повтор с новым сроком и ответ, с именем источника."""
    _path()
    import agent_core as AG
    cfg = _ring(AG)
    seen = []

    def slow(kind, b, payload, timeout):
        if kind == "slots":
            return 404, None, "нет /slots", 0.0
        seen.append(timeout)
        return (None, None, "timed out", 0.0) if len(seen) < 3 else (200, ANSWER, "", 2.0)
    out = io.StringIO()
    saved = os.environ.get("AURORA_RUN_DIR")
    try:
        os.environ["AURORA_RUN_DIR"] = str(tmp)
        AG.work_on("Sources/Confluence/US-1.md")
        with contextlib.redirect_stdout(out):
            r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=slow,
                             deadline=time.time() + 1, sleep=lambda s: None, request_timeout=100)
    finally:
        AG.work_on("")
        if saved is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = saved
    assert r["ok"] and seen == [100, 200, 400], (seen, r.get("log"))
    assert 1 not in AG.DOWN and 1 not in AG.SLOW, "медленный, но ответивший шлюз выключен"
    text = out.getvalue()
    assert "↗ US-1.md · разбор: запрос → №1 m" in text, text
    assert "⟳ US-1.md · разбор: №1 m не ответил за 1 мин 40 с — повторю" in text, text
    assert "✓ US-1.md · разбор: ответ №1 m за 2 с" in text, text


@test
def test_a_dropped_connection_is_waited_out_and_only_settings_end_a_call(tmp: Path):
    """Обрыв связи в маршруте — ожидание и повтор, а не сбой; без маршрута — прежний ответ
    «повторять незачем». Конец вызова в маршруте — только там, где ожидание не поможет:
    модель круг за кругом отвечает пусто."""
    _path()
    import agent_core as AG
    cfg = _ring(AG)
    saved = AG.DOWN_FOR
    calls = []

    def flaky(kind, b, payload, timeout):
        if kind == "slots":
            return 404, None, "нет /slots", 0.0
        calls.append(1)
        return (None, None, "Connection refused", 0.0) if len(calls) < 3 else (200, ANSWER, "", 1.0)
    try:
        AG.DOWN_FOR = 0                       # карантин в тесте не ждём
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=flaky,
                         sleep=lambda s: None)
        assert r["ok"] and len(calls) == 3, r.get("log")
        cfg.pop("persist")
        calls.clear()
        AG.DOWN.clear()
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=flaky,
                         deadline=time.time() + 5, sleep=lambda s: None)
        assert not r["ok"] and len(calls) == 1, "без маршрута обрыв вдруг стал ожиданием"
        cfg["persist"] = True
        empty = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        calls.clear()
        AG.DOWN.clear()

        def blank(kind, b, payload, timeout):
            if kind == "slots":
                return 404, None, "нет /slots", 0.0
            calls.append(1)
            return 200, empty, "", 0.5
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=blank,
                         sleep=lambda s: None)
        assert not r["ok"] and len(calls) == AG.PERSIST_EMPTY_RINGS, (len(calls), r["log"][-2:])
    finally:
        AG.DOWN_FOR = saved
        AG.DOWN.clear()


@test
def test_route_steps_run_without_a_time_budget(tmp: Path):
    """Шагу маршрута панель ставит `AURORA_PERSIST=1`: у агента нет бюджета по времени, строка
    начала говорит «без предела»; у кнопки — прежний бюджет."""
    _path()
    import agent_core as AG
    saved = os.environ.get("AURORA_PERSIST")
    try:
        os.environ.pop("AURORA_PERSIST", None)
        cfg = AG.apply_persist({"budget_min": 20})
        assert cfg["budget_min"] == 20 and AG.budget_text(cfg) == "бюджет 20 мин"
        os.environ["AURORA_PERSIST"] = "1"
        cfg = AG.apply_persist({"budget_min": 20})
        assert cfg["persist"] and cfg["budget_min"] >= 10 ** 6, cfg
        assert "без предела" in AG.budget_text(cfg)
    finally:
        if saved is None:
            os.environ.pop("AURORA_PERSIST", None)
        else:
            os.environ["AURORA_PERSIST"] = saved
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    at = src.index('env["AURORA_PERSIST"] = "1"')
    assert "if parent:" in src[at - 300:at], "«довести до конца» не только у шагов маршрута"
    runner = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert "бюджет {cfg['budget_min']} мин\"" not in runner.replace("исчерпан", ""), \
        "строка начала шага печатает бюджет мимо `budget_text`"


@test
def test_the_watchdog_leaves_a_step_that_waits_for_a_model(tmp: Path):
    """Шаг молчит дольше часа, но пульс говорит «ждёт модель» — сторож его не снимает;
    молчит без запросов — снимает. Маршрут узнаёт по итогу агента, что работа шла."""
    _path()
    import watchdog as WD
    import route_runner as RR
    state = {"s": "wait"}

    class Ck:
        @staticmethod
        def job_pulse(job):
            return {"state": state["s"]}
    now = 1_000_000.0
    w = WD.Watchdog(Ck, probe_fn=lambda project: {"ok": True, "down": []}, clock=lambda: now)
    job = {"id": "j", "project": "", "out": [], "last_out": now - WD.HARD - 60, "started": 0}
    assert w.verdict(job) == "", "сторож снял шаг, который ждёт ответа модели в сроке"
    state["s"] = "quiet"
    assert w.verdict(job), "молчащий без запросов шаг сторож оставил висеть"
    assert RR.made_progress(["  Документы: обработано 2 · пропущено 0"]), \
        "итог агента не засчитан работой — маршрут примет медленный шлюз за обрыв сети"
    assert not RR.looks_offline(["    ⚠ US-1.md · разбор: №1 m: URLError: timed out — подожду и "
                                 "повторю"]), "пережитый шагом обрыв принят за обрыв сети"
    assert RR.looks_offline(["  сбой: URLError: timed out"]), "настоящий обрыв больше не виден"
