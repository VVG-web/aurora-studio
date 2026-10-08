"""Проверки движка Aurora, часть 32: Консоль показывает всё, что идёт на машине, и поток
цепочки расписания (1.167.0).

Каркас и помощники — tests/harness.py. Во время цепочки cron Консоль была пустой: она
показывала задания выбранного проекта, а цепочка шла по другим, и между шагами заданий нет
вовсе. Заголовки пунктов, ожидания и итоги цепочки жили только в её файле истории.
"""
from __future__ import annotations

from pathlib import Path
import sys
import time

from harness import KIT, SCRIPTS, set_home, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_a_chain_streams_into_the_console(tmp: Path):
    """Цепочка пишет поток для Консоли: начало, заголовок каждого пункта с проектом, вывод
    его шагов, итог пункта. Поток читается со строки `since`; старое уходит из памяти, и
    читающий с ушедшей строки узнаёт об этом (`cut`)."""
    _path()
    from cases import part_18 as P18
    restore = set_home(tmp / "home")
    try:
        projects = [{"name": "Альфа", "path": str(tmp / "a")}, {"name": "Бета", "path": str(tmp / "b")}]
        task = P18._task(steps=[{"kind": "route", "project": "*", "route": "fix", "write": True}])
        cron, ck, sched = P18._scheduler(tmp, {"kb:repair": [(0, ["починено: 3"])],
                                               "kb:lint": [(0, ["ошибок 0"])]}, projects, [task])
        sched.enqueue(task["id"])
        P18._wait(sched)
        live = sched.live(0)
        text = "\n".join(live["lines"])
        assert live["run"] is None and live["stream"], live
        assert text.startswith("▶ цепочка «Ночь»"), text[:80]
        assert "━━ Альфа · Починить базу ━━" in text and "━━ Бета · Починить базу ━━" in text, text
        assert "■ Альфа · Починить базу: passed" in text, text
        tail = sched.live(live["next"] - 2)
        assert tail["lines"] == live["lines"][-2:] and not tail["cut"]
        saved = cron.STREAM_KEEP
        cron.STREAM_KEEP = 3
        try:
            for i in range(5):
                sched.say(f"строка {i}")
        finally:
            cron.STREAM_KEEP = saved
        cut = sched.live(0)
        assert cut["cut"] and cut["lines"] == ["строка 2", "строка 3", "строка 4"], cut
    finally:
        restore()


@test
def test_the_console_lists_everything_running_on_the_machine(tmp: Path):
    """«Что идёт»: маршруты и команды всех проектов, а не выбранного; шаг маршрута — внутри
    маршрута, а не отдельной строкой; цепочка — с текущим пунктом и ходом."""
    _path()
    import aurora_cockpit as ck
    (tmp / "a").mkdir()
    (tmp / "a" / "aurora.config.yaml").write_text("project:\n  name: Альфа\n  slug: a\n",
                                                  encoding="utf-8")

    class Run:
        done_flag = False
        sc = {"title": "Обновить базу", "id": "update"}
        write, trigger, bar = True, "cron", {"cmd": "agent:build"}
    with ck.ROUTES_LOCK:
        ck.ROUTES["r-test"] = {"id": "r-test", "project": str(tmp / "a"), "run": Run(),
                               "started": time.time()}
    with ck.JOBS_LOCK:
        ck.JOBS["j-step"] = {"id": "j-step", "done": False, "project": str(tmp / "a"),
                             "cmd": "agent:build", "args": [], "started": time.time(), "out": [],
                             "parent": "r-test"}
        ck.JOBS["j-own"] = {"id": "j-own", "done": False, "project": str(tmp / "b"),
                            "cmd": "kb:lint", "args": [], "started": time.time(), "out": ["x"],
                            "parent": ""}
    try:
        d = ck.live_now()
        r = next(x for x in d["routes"] if x["id"] == "r-test")
        assert r["name"] == "Альфа" and r["title"] == "Обновить базу" and r["trigger"] == "cron", r
        ids = [j["id"] for j in d["jobs"]]
        assert "j-own" in ids and "j-step" not in ids, ids
        assert next(j for j in d["jobs"] if j["id"] == "j-own")["name"] == "b"
    finally:
        with ck.ROUTES_LOCK:
            ck.ROUTES.pop("r-test", None)
        with ck.JOBS_LOCK:
            ck.JOBS.pop("j-step", None)
            ck.JOBS.pop("j-own", None)


@test
def test_the_console_follows_one_stream_and_picks_it_up_by_itself(_t):
    """Консоль: писать в неё может один поток — переключились, прежний замолкает; открыли
    без своего вывода — подключается к тому, что идёт (проект, потом цепочка); над ней —
    всё, что идёт на машине, а не задания выбранного проекта."""
    ui = ui_source()
    for fn in ("async function attachJob(", "async function attachRoute(", "async function followCron("):
        body = ui[ui.index(fn):ui.index(fn) + 1500]
        assert "const me = ++FOLLOW;" in body and "me !== FOLLOW" in body, fn
    poll = ui[ui.index("async function poll("):ui.index("async function poll(") + 600]
    assert "me = ++FOLLOW" in poll and "if (me !== FOLLOW) return;" in poll
    assert "poll(id, d.next, label, me)" in ui, "продолжение прогона теряет свой номер потока"
    assert "drawLiveJobs().then(() => autoFollow())" in ui, "Консоль не подключается сама"
    live = ui[ui.index("async function drawLiveJobs("):ui.index("async function autoFollow(")]
    assert '"/api/live"' in ui and "liveAll()" in live and "followCron()" in live
    assert '/api/cron/live?since=${since}' in ui
    srv = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/cron/live"' in srv and 'u.path == "/api/live"' in srv
