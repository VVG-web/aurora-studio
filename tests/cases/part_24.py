"""Проверки движка Aurora, часть 24: боты с инструментами и своей моделью, проект — единица
расписания (1.160.0).

Каркас и помощники — tests/harness.py. С 1.153.0 настройка моделей шла мимо переключателя
адаптера, и каждый вызов — «Спросить», «Продуктивность», боты — шёл прямым HTTP без
инструментов и MCP; бот при этом записывал прогон удачным. Расписание пропускало проект по
застою на хвосте, хотя база за маршрут обновилась, и бросало проект без возврата.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import sys

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _mods():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    return importlib.import_module("agent_core"), importlib.import_module("bots")


@test
def test_the_models_setup_switches_the_tools_adapter_on(_t):
    """`config()` (одна настройка моделей на кит) включает адаптер Pydantic AI: без него вызов
    идёт прямым HTTP, и у модели нет ни инструментов, ни MCP."""
    AG, _B = _mods()
    import model_config as MC
    saved = (os.environ.pop("AURORA_TESTS_ISOLATED", None), MC.load, MC.to_config,
             dict(AG.ADAPTER))
    MC.load = lambda kit, env: {}
    MC.to_config = lambda data: {"adapter": "pydantic_ai", "backends": []}
    AG.ADAPTER.update(name="openai_compat", fallback_why="старое")
    try:
        AG.config()
        assert AG.ADAPTER["name"] == "pydantic_ai", "настройка моделей не включила адаптер"
        assert AG.ADAPTER["fallback_why"] == ""
    finally:
        if saved[0] is not None:
            os.environ["AURORA_TESTS_ISOLATED"] = saved[0]
        MC.load, MC.to_config = saved[1], saved[2]
        AG.ADAPTER.clear()
        AG.ADAPTER.update(saved[3])


def _bot_project(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / "aurora.config.yaml").write_text("project:\n  name: Demo\n", encoding="utf-8")
    (p / "mcp.json").write_text(json.dumps({"mcpServers": {"mcp-atlassian": {"command": "echo"}}}),
                                encoding="utf-8")
    return p


@test
def test_a_bot_that_used_no_tools_is_not_a_success(tmp: Path):
    """Бот с MCP, ответивший мимо адаптера или без единого вызова инструмента, — не «отработал»:
    причина с советом, отчёт называет модель, путь вызова и вызванные инструменты; роль модели
    бот выбирает свою, неизвестная роль — замечание."""
    AG, B = _mods()
    p = _bot_project(tmp)
    r = B.create(p, "Ревью", {"mcp": ["mcp-atlassian"], "role": "bots"}, "Найди задачи Jira")
    meta = B.read(p, r["file"])["meta"]
    assert meta["role"] == "bots", "роль бота не сохранилась в файле"
    answer = {}
    seen = {}

    def fake(cfg, role, messages, **kw):
        seen["role"] = role
        return {"ok": True, "text": "Сделал", "model": "m", **answer}
    saved = (AG.config, AG.role_chain, AG.venv_status, AG.call_role, B.llm_roles)
    AG.config = lambda: {"adapter": "pydantic_ai", "budget_min": 1}
    AG.role_chain = lambda cfg, role: [1]
    AG.venv_status = lambda: (True, "")
    AG.call_role = fake
    B.llm_roles = lambda: [{"id": "worker", "name": "Разбор", "models": ["m"]},
                           {"id": "bots", "name": "Боты", "models": ["m2"]}]
    try:
        answer.update(via="http", tools_called=[])
        res = B.run(p, r["file"], say=lambda s: None)
        assert not res["ok"] and res["problem"]["code"] == "no_tools", res
        assert seen["role"] == "bots", "бот работал не своей ролью модели"
        answer.update(via="pydantic_ai", tools_called=[])
        res = B.run(p, r["file"], say=lambda s: None)
        assert res["problem"]["code"] == "no_tool_calls", res
        answer.update(tools_called=["mcp-atlassian_jira_search", "mcp-atlassian_jira_search"])
        res = B.run(p, r["file"], say=lambda s: None)
        assert res["ok"], res
        report = (p / res["report"]).read_text(encoding="utf-8")
        assert "роль «bots»" in report and "mcp-atlassian_jira_search ×2" in report, report
        assert "pydantic_ai" in report
        probs = B.validate(p, {**meta, "role": "нет-такой"}, "x", mcp=["mcp-atlassian"], skills=[])
        assert {"field": "role", "code": "role_unknown", "detail": "нет-такой"} in probs
        assert "role_unknown" in B.BLOCKING
    finally:
        AG.config, AG.role_chain, AG.venv_status, AG.call_role, B.llm_roles = saved
    text = B.INSTRUCTIONS
    assert "УЖЕ подключены" in text and "переменных окружения" in text, \
        "бот не знает, что его MCP уже подключены и чего у него нет"


@test
def test_a_card_parked_for_a_human_is_not_left_work(tmp: Path):
    """Карточка, отложенная тезисами «человеку», пока она та же, не стоит в очереди: иначе
    «осталось: 1» держит маршрут, и он кончается застоем при обновлённой базе."""
    AG, _B = _mods()
    import agent_runner as R
    root = make_project(tmp)
    card = root / "AuroraKnowledgeDB/Concepts/Словарь.md"
    card.write_text("---\ntitle: Словарь\nkind: knowledge\n---\n# Словарь\n\nЗнание о словаре.\n",
                    encoding="utf-8")
    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    assert card.as_posix() in R.distill_queue(cfg, str(root))
    R.park_card(str(root), str(card))
    assert card.as_posix() not in R.distill_queue(cfg, str(root)), "отложенная карточка — снова работа"
    card.write_text(card.read_text(encoding="utf-8") + "\nНовое.\n", encoding="utf-8")
    assert card.as_posix() in R.distill_queue(cfg, str(root)), "изменённая карточка не вернулась"
    assert "человеку" in R.PARKED_STATUSES


class _Ck:
    DEFAULT_LANG = "ru"

    def mark_running(self, *a):
        pass

    def project_activity(self, project):
        return {}


@test
def test_a_failed_project_is_deferred_and_retried_at_the_end(tmp: Path):
    """Проект — единица цепочки: провал шага откладывает остальные шаги проекта, цепочка идёт
    дальше и в конце один раз возвращается к нему; проекты — в порядке шагов задания."""
    _AG, _B = _mods()
    import cron
    restore = set_home(tmp / "home")
    try:
        projects = [{"path": "/p/A", "name": "A"}, {"path": "/p/B", "name": "B"}]
        task = {"id": "t1", "name": "Ночь", "order": "projects", "on_fail": "next", "lang": "ru",
                "steps": [{"kind": "route", "project": "/p/B", "route": "update"},
                          {"kind": "route", "project": "/p/B", "route": "fix"},
                          {"kind": "route", "project": "/p/A", "route": "update"},
                          {"kind": "route", "project": "/p/A", "route": "fix"}]}
        items = cron.expand(task, projects)
        assert [(i["project"], i["route"]) for i in items][:2] == [("/p/B", "update"), ("/p/B", "fix")], \
            "проекты идут не в порядке шагов задания"

        def chain(outcomes):
            calls = []
            sched = cron.Scheduler(_Ck(), lambda: projects)

            def fake_item(task, run, it, lang):
                key = (it["project"][-1], it["route"])
                calls.append(key)
                st = outcomes[key].pop(0) if len(outcomes[key]) > 1 else outcomes[key][0]
                it.update(status=st, finished=cron.now_iso())
            sched._item = fake_item
            run = sched._new_run(task, "manual", "")
            sched._run(task, run)
            return calls, run

        calls, run = chain({("B", "update"): ["stall", "passed"], ("B", "fix"): ["passed"],
                            ("A", "update"): ["passed"], ("A", "fix"): ["passed"]})
        assert calls == [("B", "update"), ("A", "update"), ("A", "fix"), ("B", "update"), ("B", "fix")], \
            calls
        by = {(i["project"][-1], i["route"]): i for i in run["items"]}
        assert by[("B", "update")]["status"] == "passed" and by[("B", "update")]["retry"]
        assert by[("B", "update")]["first"]["status"] == "deferred"
        assert run["status"] == "passed", run["status"]

        calls, run = chain({("B", "update"): ["failed"], ("B", "fix"): ["passed"],
                            ("A", "update"): ["passed"], ("A", "fix"): ["partial"]})
        assert ("B", "fix") not in calls, "«Починить» запущена после несостоявшегося «Обновить»"
        by = {(i["project"][-1], i["route"]): i for i in run["items"]}
        assert by[("B", "fix")]["status"] == "skipped" and by[("B", "fix")]["note"] == "after_retry_failure"
        assert by[("A", "fix")]["status"] == "partial" and run["status"] == "failed"
    finally:
        restore()


@test
def test_a_stall_after_progress_is_partial_not_a_failure(tmp: Path):
    """Застой на хвосте, когда база за маршрут сдвинулась, — «частично»: проект обновлён и его
    следующие шаги идут. Срабатывание, пока то же задание идёт, пишется в историю."""
    _AG, _B = _mods()
    import cron
    restore = set_home(tmp / "home")
    saved = (cron.RR.scenario, cron.RR.RouteRun)
    try:
        class Route:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                return {"reason": "stall", "progressed": self.progressed, "failed": "",
                        "note": "", "run_id": "r1"}
        cron.RR.scenario = lambda ck, rid: {"id": rid, "title": "Обновить базу"}
        cron.RR.RouteRun = Route
        sched = cron.Scheduler(_Ck(), lambda: [])
        for moved, want in ((True, "partial"), (False, "stall")):
            Route.progressed = moved
            it = {"kind": "route", "project": "/p/A", "route": "update", "status": "pending",
                  "note": "", "jobs": [], "done": [], "log": []}
            sched._item({"name": "Ночь"}, {"id": "x", "items": [it]}, it, "ru")
            assert it["status"] == want, (moved, it["status"])
        sched._missed({"id": "t1", "name": "Ночь"}, datetime.now(), "already_running")
        assert any(r.get("note") == "already_running" for r in cron.list_runs()), \
            "пропуск при идущем задании не записан"
    finally:
        cron.RR.scenario, cron.RR.RouteRun = saved
        restore()
