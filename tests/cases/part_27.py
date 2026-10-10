"""Проверки движка Aurora, часть 27: сторож зависших после обрыва связи шагов, контекст
проекта для ботов, боты в цепочке расписания (1.163.0).

Каркас и помощники — tests/harness.py. Обрыв VPN посреди ночного маршрута оставлял шаг ждать
ответа, который не придёт, до утра. Бот знал о проекте только то, что успевал найти сам, —
«Продуктивность» же с первого шага кладёт в задание пак знаний. Цепочка расписания не умела
запускать ботов разных проектов по очереди.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


class _Proc:
    def __init__(self):
        self.pid = -1

    def poll(self):
        return 0            # уже закончился: kill_tree не нужен


@test
def test_the_watchdog_restarts_only_a_step_that_cannot_answer(_t):
    """Сторож: шаг молчит, связь пропала — ждём; связь вернулась, а шаг за GRACE не ожил —
    снять. Связь цела и шаг молчит меньше HARD — не трогать (долгое размышление модели).
    Связь цела, а молчание дольше HARD — ответа не будет."""
    _path()
    import watchdog as WD
    clock = {"t": 10_000.0}
    net = {"ok": True}

    def probe(project):
        return {"ok": net["ok"], "down": [] if net["ok"] else ["модель (Шлюз)"]}
    dog = WD.Watchdog(ck=None, probe_fn=probe, clock=lambda: clock["t"])
    dog.probes = {}
    job = {"id": "j1", "project": "/p", "out": [], "started": clock["t"], "last_out": clock["t"],
           "parent": "route-1", "proc": _Proc()}

    def at(dt):
        clock["t"] += dt
        dog.probes.clear()          # каждый шаг проверки — свежий ответ связи
        return dog.verdict(job)
    assert at(WD.QUIET - 10) == "", "молчание короче QUIET уже тревога"
    net["ok"] = False
    assert at(20) == "" and any("связь не отвечает" in x for x in job["out"])
    assert at(600) == "", "сторож снял шаг, пока связи нет — ответ ещё возможен"
    net["ok"] = True
    assert at(30) == "" and any("связь вернулась" in x for x in job["out"])
    assert at(WD.GRACE - 60) == "", "шаг снят раньше, чем успел ожить"
    why = at(120)
    assert "пропадала и вернулась" in why, why

    job2 = {"id": "j2", "project": "/p", "out": [], "started": clock["t"],
            "last_out": clock["t"], "parent": "route-1", "proc": _Proc()}
    dog.track.clear()
    clock["t"] += WD.HARD - 60
    dog.probes.clear()
    assert dog.verdict(job2) == "", "долгое молчание при живой связи принято за зависание"
    clock["t"] += 120
    dog.probes.clear()
    assert "при живой связи" in dog.verdict(job2)
    job3 = dict(job2, id="j3", last_out=clock["t"] - 10)
    assert dog.verdict(job3) == "", "шаг с недавним выводом снят"


@test
def test_kill_tree_takes_the_children_too(tmp: Path):
    """Шаг снимается вместе с потомками: адаптер модели и git — дочерние процессы."""
    _path()
    import watchdog as WD
    pidfile = tmp / "child.pid"
    code = ("import subprocess,sys,time;"
            f"c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"open(r'{pidfile}','w').write(str(c.pid));time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", code])
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    WD.kill_tree(parent.pid)
    parent.wait(timeout=10)
    from aurora_common import pid_alive
    for _ in range(50):
        if not pid_alive(child):
            break
        time.sleep(0.1)
    assert not pid_alive(child), "потомок шага пережил снятие"


@test
def test_a_hung_step_runs_again_and_a_stale_git_lock_goes(tmp: Path):
    """Маршрут запускает снятый сторожем шаг заново — без потолка (1.170.0), пока шаг не
    пройдёт или человек не остановит маршрут; брошенный `index.lock` после снятия
    убирается, свежий — нет."""
    _path()
    import route_runner as RR
    import watchdog as WD
    said = []
    run = RR.RouteRun.__new__(RR.RouteRun)
    run.skip, run.cycle_at, run.lap, run.in_lap, run.done, run.total = set(), None, 0, 0, 0, 1
    run.cycle_size, run.summary, run.stopped, run.prev_took = 0, [], False, None
    run.say = lambda kind, text: said.append((kind, text))
    run.t = lambda key, **kw: key
    run._event = lambda *a: None

    class Ck:
        class RS:
            parse = staticmethod(lambda lines: [])
    run.ck = Ck
    answers = [{"rc": -15, "lines": [], "hung": "связь пропадала"}, {"rc": 0, "lines": []}]
    run.exec_step = lambda cmd, args: answers.pop(0)
    res = run.run_one({"cmd": "agent:distill", "args": [], "cycle": False, "why": ""})
    assert res["rc"] == 0 and ("warn", "route.hung_restart") in said, said
    said.clear()
    answers = [{"rc": -15, "lines": [], "hung": "снова"}] * 5 + [{"rc": 0, "lines": []}]
    run.exec_step = lambda cmd, args: answers.pop(0)
    res = run.run_one({"cmd": "agent:distill", "args": [], "cycle": False, "why": ""})
    assert res["rc"] == 0 and said.count(("warn", "route.hung_restart")) == 5, \
        "маршрут сдался на зависшем шаге раньше, чем тот прошёл"

    def stopped(cmd, args):
        run.stopped = True              # человек нажал «Прервать», пока шаг висел
        return {"rc": -15, "lines": [], "hung": "снова"}
    run.exec_step = stopped
    res = run.run_one({"cmd": "agent:distill", "args": [], "cycle": False, "why": ""})
    assert res["rc"] < 0, "остановленный человеком маршрут продолжил перезапускать шаг"
    run.stopped = False

    git = tmp / ".git"
    git.mkdir()
    lock = git / "index.lock"
    lock.write_text("", encoding="utf-8")
    assert WD.stale_git_lock(str(tmp)) == "", "снят свежий index.lock — его может держать живой git"
    old = time.time() - 600
    import os
    os.utime(lock, (old, old))
    assert WD.stale_git_lock(str(tmp)) and not lock.exists()
    assert not hasattr(WD, "MAX_RESTARTS"), "у перезапуска зависшего шага снова потолок"


def _kb(tmp: Path) -> Path:
    root = make_project(tmp)
    (root / "AuroraKnowledgeDB/Concepts/НАКЛ.md").write_text(
        "---\ntitle: НАКЛ\nstatus: knowledge\n---\n# НАКЛ\n\nНАКЛ — товарная накладная на "
        "поставку со склада.\n", encoding="utf-8")
    (root / "AuroraKnowledgeDB/Processes/Проверка-НАКЛ-на-складе.md").write_text(
        "---\ntitle: Проверка НАКЛ на складе\nstatus: knowledge\n---\n# Проверка НАКЛ на "
        "складе\n\nСклад сверяет [[НАКЛ]] с сопроводительными документами.\n",
        encoding="utf-8")
    return root


@test
def test_a_bot_gets_project_context_before_work(tmp: Path):
    """Контекст проекта для бота: ссылки задания → понятия (моделью, иначе правилом) →
    гибридный поиск Авроры по базе проекта знаний → пак в задании. Режим off — пусто."""
    _path()
    import bot_context as BC
    root = _kb(tmp)
    body = ("Проверь историю https://wiki.example.com/pages/viewpage.action?pageId=123 и задачу "
            "ABC-46, сверь с [[НАКЛ]] и «Проверка НАКЛ на складе».")
    found = BC.links_of(body)
    assert found == {"confluence": ["123"], "jira": ["ABC-46"], "cards": ["НАКЛ"]}, found
    assert "US-3.6.21" not in BC.links_of("история US-3.6.21")["jira"], "код истории принят за задачу"
    said = []

    def model(cfg, role, messages, **kw):
        assert role == "planner" and "НАКЛ" in messages[0]["content"]
        return {"ok": True, "text": '{"concepts": ["НАКЛ", "проверка на складе"]}'}
    out = BC.gather(str(root), body, {}, model, mode="evaluate", name="Демо", say=said.append)
    assert out["concepts"] == ["НАКЛ", "проверка на складе"], out["concepts"]
    assert any(x["kind"] == "card" and x["ref"] == "НАКЛ" for x in out["links"])
    assert "## Контекст проекта «Демо»" in out["block"] and "НАКЛ" in out["block"]
    assert out["cards"] >= 1, "поиск по базе ничего не нашёл"
    assert any("не прочитана" in x for x in said), "недоступная ссылка прошла молча"
    lazy = BC.gather(str(root), body, {}, lambda *a, **k: {"ok": False}, name="Демо",
                     say=lambda s: None)
    assert lazy["concepts"] and "НАКЛ" in " ".join(lazy["concepts"]), "без модели понятий нет"
    assert BC.gather(str(root), body, {}, model, mode="off")["block"] == ""


@test
def test_a_bot_works_in_its_knowledge_project(tmp: Path):
    """Бот с полем «проект знаний»: пак — из базы того проекта, инструменты модели и сервер
    `aurora` смотрят туда же; неизвестный проект — блокирующее замечание."""
    _path()
    import agent_core as AG
    import bots as B
    (tmp / "kb").mkdir()
    kb = _kb(tmp / "kb")
    own = tmp / "own"
    own.mkdir()
    (own / "aurora.config.yaml").write_text("project:\n  name: Own\n", encoding="utf-8")
    r = B.create(own, "Проверка", {"knowledge": "kb", "context": "evaluate"},
                 "Проверь понятие [[НАКЛ]]")
    meta = B.read(own, r["file"])["meta"]
    assert meta["knowledge"] == "kb" and meta["context"] == "evaluate", meta
    seen = {}
    saved = (B.knowledge_projects, AG.config, AG.call_role, AG.venv_status, B.llm_roles)
    B.knowledge_projects = lambda: [{"slug": "kb", "name": "project", "path": str(kb)}]
    AG.config = lambda: {"adapter": "pydantic_ai", "budget_min": 1,
                         "chains": {"llm": {"worker": [{"n": 1, "model": "m"}]}}, "backends": []}
    AG.venv_status = lambda: (True, "")
    B.llm_roles = lambda: [{"id": "worker", "name": "", "models": ["m"]}]

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return {"ok": False}
        seen.update(kw, prompt=messages[1]["content"])
        return {"ok": True, "text": "сделано", "model": "m", "via": "pydantic_ai",
                "tools_called": []}
    AG.call_role = fake
    try:
        res = B.run(own, r["file"], say=lambda s: None)
        assert res["ok"], res
        assert seen["kb_root"] == str(kb), "инструменты бота смотрят не в проект знаний"
        assert "## Контекст проекта" in seen["prompt"] and "НАКЛ" in seen["prompt"]
        report = (own / res["report"]).read_text(encoding="utf-8")
        assert "- Контекст: проект" in report and "режим evaluate" in report, report
        probs = B.validate(own, {**meta, "knowledge": "нет-такого"}, "x", mcp=[], skills=[])
        assert any(p["code"] == "knowledge_unknown" for p in probs)
        assert "knowledge_unknown" in B.BLOCKING
    finally:
        B.knowledge_projects, AG.config, AG.call_role, AG.venv_status, B.llm_roles = saved
    servers = AG.mcp_config(str(own), with_aurora=True, aurora_root=str(kb))["mcpServers"]
    assert str(kb) in servers["aurora"]["args"], "сервер aurora не в проекте знаний"


@test
def test_a_chain_runs_bots_of_several_projects_in_turn(tmp: Path):
    """Шаг «бот» цепочки: конкретный бот проверяется при сохранении; «все боты» раскрываются
    во включённые боты проекта; с «все проекты» — боты всех проектов по очереди."""
    _path()
    import bots as B
    import cron
    restore = set_home(tmp / "home")
    try:
        a, b = tmp / "A", tmp / "B"
        for p, n in ((a, "A"), (b, "B")):
            p.mkdir()
            (p / "aurora.config.yaml").write_text(f"project:\n  name: {n}\n", encoding="utf-8")
        fa = B.create(a, "Ревью A", {"enabled": True}, "x")["file"]
        fb = B.create(b, "Ревью B", {"enabled": True}, "y")["file"]
        off = B.create(b, "Спящий", {"enabled": False}, "z")["file"]
        projects = [{"path": str(a), "name": "A"}, {"path": str(b), "name": "B"}]

        class Ck:
            DEFAULT_LANG = "ru"
            scenarios = staticmethod(lambda: [])
            command_by_name = staticmethod(lambda n: None)
        raw = {"name": "Боты", "time": "07:00", "steps": [{"kind": "bot", "project": "*", "bot": "*"}]}
        task, err = cron.clean_task(Ck, raw, projects)
        assert task and not err, err
        got = [(Path(i["project"]).name, i["cmd"], i["args"]) for i in cron.expand(task, projects)]
        assert got == [("A", "bot:run", [f"--bot={fa}", "--trigger=cron"]),
                       ("B", "bot:run", [f"--bot={fb}", "--trigger=cron"])], got
        assert off not in str(got), "выключенный бот попал в цепочку"
        one, err = cron.clean_task(Ck, {"name": "Один", "time": "07:00",
                                        "steps": [{"kind": "bot", "project": str(b), "bot": off}]},
                                   projects)
        assert one and one["steps"][0]["bot"] == off, "выбранный бот не сохранился"
        _, err = cron.clean_task(Ck, {"name": "Нет", "time": "07:00",
                                      "steps": [{"kind": "bot", "project": str(b),
                                                 "bot": "bots/нет.md"}]}, projects)
        assert err == "bad_bot", err
    finally:
        restore()
    src = (COCKPIT / "cron.py").read_text(encoding="utf-8")
    assert '"bad_bot"' in src and 'elif st.get("kind") == "bot"' in src
    view = (COCKPIT / "modules/cron/view.js").read_text(encoding="utf-8")
    assert '["bot", t("cron.kind_bot")]' in view and "DATA.bots" in view
