"""Проверки движка Aurora, часть 25: обход Pydantic AI виден сразу, сбои — с причинами,
блокеры doctor — отчётом (1.161.0).

Каркас и помощники — tests/harness.py. С 1.139.0 по 1.160.0 адаптер четыре раза уходил из
строя, и каждый раз движок тихо продолжал прямым HTTP: человек узнавал об этом по боту,
который «не видит MCP». Перепроверка PRJ-C 07.10.2026 дала десять сбоев без единой
причины в журнале. На «Здоровье» «блокеры doctor: 2» не говорили, что сломано и куда нажать.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _mods():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    return importlib.import_module("agent_core"), importlib.import_module("agent_runner")


class _Env:
    """Переменные окружения на время проверки — и обратно как было."""

    def __init__(self, **kv):
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _cfg(AG):
    return AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gw.example.com/v1",
                            "AURORA_AGENT_BACKEND_1_MODEL": "m"})


@test
def test_a_call_that_bypasses_pydantic_ai_is_loud_and_tool_calls_are_refused(tmp: Path):
    """Pydantic AI выбран, а ответ пришёл прямым HTTP: строка в выводе, тревога для панели,
    запись в журнале сбоев и строка итога. Вызов с инструментами без адаптера не принят;
    первый же вызов этого пути через адаптер тревогу снимает."""
    AG, _R = _mods()
    import run_summary as RS
    restore = set_home(tmp / "home")
    run = tmp / "run"
    run.mkdir()
    saved = (AG.pydantic_transport, AG.http_json, dict(AG.ADAPTER), dict(AG.USAGE),
             dict(AG.RUN_TASK), set(AG._BYPASS_SAID), dict(AG.FAILS))
    plain = {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}]}
    try:
        with _Env(AURORA_RUN_DIR=str(run), AURORA_AGENT_CACHE="0"):
            AG.RUN_TASK["name"] = "agent:test"
            AG.USAGE.update(bypass=0, bypass_why="")
            AG.pydantic_transport = lambda b, p, t: (None, None, AG.ADAPTER_FAIL + "venv не установлен", 0.0)
            AG.http_json = lambda url, body, key, timeout: (200, plain, "", 0.1)
            cfg = _cfg(AG)
            assert cfg["adapter"] == "pydantic_ai"
            r = AG.call_role(cfg, "worker", [{"role": "user", "content": "вопрос 1"}])
            assert r["ok"] and r["via"] == "http" and "venv не установлен" in r["bypass"], r
            assert AG.USAGE["bypass"] == 1
            alarm = AG.read_alarm()
            assert "agent:test" in alarm and "venv" in alarm["agent:test"]["why"], alarm
            recs = [json.loads(x) for x in (run / AG.FAIL_FILE).read_text(encoding="utf-8").splitlines()]
            assert any(x["stage"] == "мимо Pydantic AI" for x in recs), recs
            lines = RS.render(RS.from_agent([], AG.USAGE, 1.0, None))
            assert any("Мимо Pydantic AI" in x for x in lines), lines

            t = AG.call_role(cfg, "planner", [{"role": "user", "content": "вопрос 2"}], tools=True)
            assert not t["ok"] and t.get("bypass"), "вызов с инструментами принят без адаптера"
            assert any("⛔" in x for x in t["log"]), t["log"]

            AG.pydantic_transport = lambda b, p, t: (200, dict(plain, aurora={"via": "pydantic_ai"}), "", 0.1)
            ok = AG.call_role(cfg, "worker", [{"role": "user", "content": "вопрос 3"}])
            assert ok["via"] == "pydantic_ai" and not ok["bypass"]
            assert "agent:test" not in AG.read_alarm(), "тревога не снялась после вызова через адаптер"
    finally:
        (AG.pydantic_transport, AG.http_json) = saved[:2]
        for d, old in ((AG.ADAPTER, saved[2]), (AG.USAGE, saved[3]), (AG.RUN_TASK, saved[4]),
                       (AG.FAILS, saved[6])):
            d.clear()
            d.update(old)
        AG._BYPASS_SAID.clear()
        AG._BYPASS_SAID.update(saved[5])
        restore()


@test
def test_the_adapter_follows_the_call_settings_not_a_process_switch(tmp: Path):
    """Какой адаптер нужен вызову, решает его настройка: переключатель процесса, оставшийся
    прямым HTTP, больше не уводит вызов мимо Pydantic AI (так было с 1.153.0 по 1.160.0)."""
    AG, _R = _mods()
    seen = []
    saved = (AG.pydantic_transport, dict(AG.ADAPTER))
    try:
        AG.ADAPTER.update(name="openai_compat", fallback_why="")
        AG.pydantic_transport = lambda b, p, t: (seen.append(p) or (
            200, {"choices": [{"message": {"content": "ок"}}], "aurora": {"via": "pydantic_ai"}},
            "", 0.1))
        with _Env(AURORA_AGENT_CACHE="0"):
            r = AG.call_role(_cfg(AG), "worker", [{"role": "user", "content": "x"}])
        assert seen and r["via"] == "pydantic_ai", "вызов ушёл мимо адаптера по переключателю"
        assert "adapter" in AG.ADAPTER_ONLY, "служебное поле ушло бы в HTTP-запрос"
    finally:
        AG.pydantic_transport = saved[0]
        AG.ADAPTER.clear()
        AG.ADAPTER.update(saved[1])


@test
def test_a_failure_lands_in_the_run_journal_with_the_whole_call(tmp: Path):
    """Сбой шага — запись в `failures.jsonl` папки прогона: причина, ход вызова, размер
    запроса и сам ответ, если он не разобран. Перепроверка «без опоры» печатает причину в
    строке хода и перечисляет сбои в отчёте — 07.10.2026 их было десять и без причин."""
    AG, R = _mods()
    run = tmp / "run"
    run.mkdir()
    root = make_project(tmp)
    card = root / "AuroraKnowledgeDB/Concepts/Карточка.md"
    card.write_text("---\ntitle: Карточка\nunsupported: 2\n---\nТезис.\n\n"
                    + R.QUOTES + "\n\nИсточник дословно.\n", encoding="utf-8")
    saved = (dict(AG.FAILS), dict(AG.RUN_TASK))
    cwd = os.getcwd()
    try:
        with _Env(AURORA_RUN_DIR=str(run)):
            AG.RUN_TASK["name"] = "agent:distill --recheck"
            answer = {"ok": True, "text": "мусор " * 600, "model": "m", "backend": 1,
                      "finish": "length", "log": ["№1 m: ответ пришёл"],
                      "req": {"role": "qa", "prompt_chars": 12000, "request_timeout": 300}}
            step = R.fail_step({"card": "X.md"}, "модель ответила не JSON", answer)
            assert step["status"] == "сбой" and step["note"] == "модель ответила не JSON"
            rec = json.loads((run / AG.FAIL_FILE).read_text(encoding="utf-8").splitlines()[-1])
            assert rec["subject"] == "X.md" and rec["stage"] == "ответ модели", rec
            call = rec["call"]
            assert call["finish"] == "length" and call["prompt_chars"] == 12000
            assert call["answer_chars"] == len(answer["text"]) and "вырезано" in call["answer"]

            os.chdir(root)
            fake = lambda cfg, role, messages, **kw: {"ok": False, "timed_out": True,  # noqa: E731
                                                      "log": ["№1 m: не уложился в 300 с",
                                                              "никто не уложился в срок"]}
            res = R.recheck_unsupported(_cfg(AG), str(root), False, call=fake)
            assert res["failed"] == 1 and "не уложился" in res["steps"][0]["note"], res
            text = R.report_recheck(res, False)
            assert "## Сбои проверки" in text and "не уложился" in text, text
            recs = [json.loads(x) for x in (run / AG.FAIL_FILE).read_text(encoding="utf-8").splitlines()]
            assert any(x["subject"].endswith("Карточка.md") and x["call"]["timed_out"] for x in recs)
    finally:
        os.chdir(cwd)
        AG.FAILS.clear()
        AG.FAILS.update(saved[0])
        AG.RUN_TASK.clear()
        AG.RUN_TASK.update(saved[1])


@test
def test_the_route_log_names_how_long_the_previous_step_took(tmp: Path):
    """«Предыдущий шаг занял» — длительность прошлого шага, а не пауза между шагами: до
    1.161.0 журнал писал «0с» у каждого шага, в том числе после пятиминутного связывания."""
    _mods()
    import route_runner as RR
    clock = {"now": 1000.0}

    class FakeTime:
        @staticmethod
        def time():
            return clock["now"]

        @staticmethod
        def sleep(s):
            clock["now"] += s

    said = []
    run = RR.RouteRun.__new__(RR.RouteRun)
    run.skip, run.cycle_at, run.lap, run.in_lap, run.done, run.total = set(), None, 0, 0, 0, 2
    run.cycle_size, run.summary, run.stopped, run.prev_took = 0, [], False, None
    run.say = lambda kind, text: said.append(text)
    run.t = lambda key, **kw: f"{key}:{kw.get('dur', '')}"
    run._event = lambda *a: None

    class Ck:
        class RS:
            parse = staticmethod(lambda lines: [])
    run.ck = Ck

    def step(cmd, args):
        clock["now"] += 300            # шаг идёт пять минут
        return {"rc": 0, "lines": []}
    run.exec_step = step
    saved = RR.time
    RR.time = FakeTime
    try:
        run.run_one({"cmd": "agent:relink", "args": [], "cycle": False, "why": ""})
        clock["now"] += 1              # между шагами — секунда
        run.run_one({"cmd": "kb:links", "args": [], "cycle": False, "why": ""})
    finally:
        RR.time = saved
    took = [x for x in said if "route.prev_took" in x]
    assert took and took[-1].endswith(":" + run._dur(300)), said


@test
def test_doctor_blockers_say_why_and_the_health_page_opens_a_report(tmp: Path):
    """Блокер doctor — «что → как исправить» и строка `WHY:` за ним; сервер кладёт причины
    рядом с находками (`blocker_why`), английская панель их переводит, а «Здоровье»
    открывает отчёт по плитке «Блокеры» и по строке «блокеры doctor» в цвете проекта."""
    root = make_project(tmp)
    (root / "stray-folder").mkdir()
    (root / "stray-folder" / "x.md").write_text("x", encoding="utf-8")
    out = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_doctor.py")],
                         cwd=str(root), capture_output=True, text=True, encoding="utf-8").stdout
    lines = out.splitlines()
    at = next(i for i, x in enumerate(lines) if x.startswith("ERROR: папки верхнего уровня"))
    assert lines[at + 1].startswith("WHY: движок не знает"), lines[at:at + 2]
    _mods()
    import aurora_cockpit as ck
    got = ck.translate_payload({"doctor": {"errors": [lines[at][7:]],
                                           "blocker_why": [lines[at + 1][5:]]}}, "en")
    assert got["doctor"]["blocker_why"][0].startswith("the engine does not know"), got
    view = (COCKPIT / "modules/health/view.js").read_text(encoding="utf-8")
    for token in ("function showBlockers(", "function findingRow(", 't("health.blockers")',
                  'x.code === "blockers"', "blocker_why"):
        assert token in view, f"«Здоровье» потеряло: {token}"
    panel = (COCKPIT / "ui/panel.js").read_text(encoding="utf-8")
    assert 'code:"blockers"' in panel, "строка блокеров в цвете проекта не помечена"


@test
def test_the_panel_shows_the_adapter_alarm_on_every_page(tmp: Path):
    """Тревога «мимо Pydantic AI» доходит до панели: `/api/state` и своя точка отдают её
    строками, страница рисует красную полосу и опрашивает её сама, в любом разделе."""
    AG, _R = _mods()
    restore = set_home(tmp / "home")
    try:
        import aurora_cockpit as ck
        assert ck.adapter_alarm() == []
        AG._write_alarm({"bot:ревью": {"at": "2026-10-07T07:00:00Z", "why": "venv не установлен",
                                       "project": "/x/PRJ", "calls": 3}})
        rows = ck.adapter_alarm()
        assert rows == [{"path": "bot:ревью", "at": "2026-10-07T07:00:00Z",
                         "why": "venv не установлен", "project": "PRJ", "calls": 3}], rows
    finally:
        restore()
    panel = (COCKPIT / "ui/panel.js").read_text(encoding="utf-8")
    assert "function drawAdapterAlarm(" in panel and '"/api/adapter/alarm"' in panel
    assert "drawAdapterAlarm(S.state.adapter_alarm)" in panel, "полосы нет при загрузке"
