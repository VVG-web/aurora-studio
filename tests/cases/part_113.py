"""Проверки движка Aurora, часть 113. Каркас и помощники — tests/harness.py.

Пересборка с нуля, которую можно продолжить и отменить (1.189.0). 20–21.10.2026 «Пересобрать базу с нуля» не дошла до
конца в четырёх проектах: цикл обрывался на 12 оборотах (оборот — 24 источника) и маршрут не выполнял хвост, а повтор
расписания начинал со сноса и стирал собранное. Теперь снос ставит тег отката и отметку «пересборка идёт», повторный снос
отказывает, маршрут продолжает с цикла, остановленный на застое выполняет краткий хвост без модели, а прежнюю базу
возвращает `kb:reset --undo`. Проекты и репозитории — только во временных папках.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from cases import part_70 as P70
from cases.part_18 import FakePanel, _modules
from harness import KIT, SCRIPTS, run, test

KB = P70.KB
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _kr():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import kb_reset
    return kb_reset


class _Ident:
    """Личность для коммитов `kb:reset --undo` во временных репозиториях: у проекта в работе она есть всегда."""

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in IDENT}
        os.environ.update(IDENT)

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _base(tmp: Path, names=("Заявка", "Поставка", "Счёт")) -> Path:
    """Проект под git с машинными карточками: сброс их снесёт."""
    root = P70._project(tmp)
    for n in names:
        P70._card(root, f"Concepts/{n}.md", title=f'"{n}"')
    P70._put(root, "meta/aurora_version.txt", "1.0.0\n")
    P70._commit(root)
    return root


def _reset(root: Path, *args, expect=None):
    return run("kb_reset.py", *args, cwd=root, expect_rc=expect)


def _cards(root: Path) -> list:
    return sorted(p.name for p in (root / KB / "Concepts").glob("*.md"))


@test
def test_the_wipe_sets_a_rollback_tag_and_a_rebuild_mark(tmp: Path):
    """Снос ставит тег отката на прежнюю базу и пишет отметку «пересборка идёт»; тег указывает на коммит со всеми
    карточками, в отметке — тег, коммит и число карточек до сноса."""
    KR = _kr()
    root = _base(tmp)
    with _Ident():
        cp = _reset(root, "--apply", expect=0)
    assert "Точка возврата: тег aurora/pre-rebuild-" in cp.stdout and "kb:reset --undo --apply" in cp.stdout, cp.stdout
    assert _cards(root) == [], "карточки не снесены"
    live = KR.in_progress(str(root))
    assert live["phase"] == "building" and live["cards_before"] == 3 and live["tag"].startswith("aurora/pre-rebuild-"), live
    assert KR.tags(str(root)) == [live["tag"]]
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", live["tag"], "--", f"{KB}/Concepts"], cwd=str(root),
                            capture_output=True, text=True, encoding="utf-8").stdout.split()
    assert len(listed) == 3, listed
    assert KR.count_cards(str(root), live["tag"]) == 3 and KR.count_cards(str(root)) == 0


@test
def test_a_second_wipe_of_an_unfinished_rebuild_is_refused(tmp: Path):
    """Пока пересборка не закончена, повторный снос отказывает (код 2) и ничего не стирает; сухой проход предупреждает;
    `--restart` стирает осознанно и ставит второй тег; `--finish --apply` снимает отметку, и снос снова разрешён."""
    KR = _kr()
    root = _base(tmp)
    with _Ident():
        _reset(root, "--apply", expect=0)
        P70._card(root, "Concepts/Новая.md", title='"Новая"')
        P70._commit(root)
        cp = _reset(root, "--apply", expect=2)
        assert "прежняя пересборка не закончена" in cp.stderr and _cards(root) == ["Новая.md"], (cp.stderr, _cards(root))
        dry = _reset(root, expect=0)
        assert "Пересборка уже идёт" in dry.stdout and "--restart" in dry.stdout, dry.stdout
        _reset(root, "--apply", "--restart", expect=0)
        assert _cards(root) == [] and len(KR.tags(str(root))) == 2 and KR.in_progress(str(root))["restart"] is True
        fin = _reset(root, "--finish", expect=0)
        assert "(dry-run)" in fin.stdout and KR.in_progress(str(root)), "сухой --finish снял отметку"
        _reset(root, "--finish", "--apply", expect=0)
    st = KR.read_state(str(root))
    assert st["phase"] == "finished" and st["finished"] and not KR.in_progress(str(root)), st
    with _Ident():
        P70._card(root, "Concepts/Ещё.md", title='"Ещё"')
        P70._commit(root)
        _reset(root, "--apply", expect=0)
    assert KR.in_progress(str(root)), "после нового сноса пересборка снова должна числиться идущей"


@test
def test_undo_restores_the_previous_base_exactly_and_keeps_the_engine_stamp(tmp: Path):
    """`--undo --apply`: каталог базы становится точь-в-точь таким, каким был в теге, одним коммитом; отметка версии
    движка остаётся текущей; сухой `--undo` ничего не меняет; при несохранённых правках карточек возврат отказывает."""
    KR = _kr()
    root = _base(tmp)
    def snap():
        # Концы строк не в счёт: на Windows git отдаёт файлы с CRLF (`core.autocrlf`), и проверять приходится возврат
        # содержимого, а не настройку git.
        return {p.name: p.read_bytes().replace(b"\r\n", b"\n") for p in (root / KB / "Concepts").glob("*.md")}
    before = snap()
    with _Ident():
        _reset(root, "--apply", expect=0)
        P70._card(root, "Concepts/Пересобранная.md", title='"Пересобранная"')
        P70._commit(root)
        (root / KB / "meta" / "aurora_version.txt").write_text("9.9.9\n", encoding="utf-8")   # движок обновили после сноса
        dry = _reset(root, "--undo", expect=0)
        assert "карточек там 3, сейчас 1" in dry.stdout and "(dry-run)" in dry.stdout, dry.stdout
        assert _cards(root) == ["Пересобранная.md"], "сухой возврат что-то изменил"
        (root / KB / "Concepts" / "Пересобранная.md").write_text("правка человека\n", encoding="utf-8")
        refused = _reset(root, "--undo", "--apply", expect=2)
        assert "незакоммиченные правки" in refused.stderr, refused.stderr
        subprocess.run(["git", "checkout", "--", KB + "/Concepts/Пересобранная.md"], cwd=str(root), check=True)
        cp = _reset(root, "--undo", "--apply", expect=0)
    assert "База возвращена: карточек 3" in cp.stdout, cp.stdout
    assert snap() == before, "база вернулась не точь-в-точь"
    assert (root / KB / "meta" / "aurora_version.txt").read_text(encoding="utf-8") == "9.9.9\n", "штамп движка откатился"
    last = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8").stdout
    assert last.startswith("Возврат базы знаний до пересборки с нуля"), last
    st = KR.read_state(str(root))
    assert st["phase"] == "undone" and st["restored_from"] == st["tag"] and not KR.in_progress(str(root)), st


@test
def test_undo_and_restart_refuse_while_a_route_runs_in_the_project(tmp: Path):
    """Идущий маршрут (замок `route.lock` живого процесса) — возврат базы и перезапуск сноса отказывают: руки двух
    писателей в одной базе. Обычный снос внутри маршрута (его же замок) этой проверке не подлежит."""
    root = _base(tmp)
    with _Ident():
        _reset(root, "--apply", expect=0)
    lock = root / ".aurora" / "state" / "route.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getppid(), "run": "r1", "route": "rebuild", "title": "Пересобрать",
                                "since": "2026-10-29T08:00:00Z"}), encoding="utf-8")
    cp = _reset(root, "--undo", "--apply", expect=2)
    assert "идёт маршрут «Пересобрать»" in cp.stderr, cp.stderr
    P70._card(root, "Concepts/Новая.md", title='"Новая"')
    with _Ident():
        P70._commit(root)
        cp = _reset(root, "--apply", "--restart", expect=2)
    assert "идёт маршрут" in cp.stderr and _cards(root) == ["Новая.md"], (cp.stderr, _cards(root))
    lock.unlink()
    with _Ident():
        _reset(root, "--undo", "--apply", expect=0)
    assert _cards(root) == ["Заявка.md", "Поставка.md", "Счёт.md"], _cards(root)


@test
def test_undo_without_a_tag_says_so(tmp: Path):
    """Тега отката нет (пересборки не было) — `--undo` объясняет и отказывает кодом 2, ничего не трогая."""
    root = _base(tmp)
    cp = _reset(root, "--undo", "--apply", expect=2)
    assert "тега отката нет" in cp.stderr, cp.stderr
    assert _cards(root) == ["Заявка.md", "Поставка.md", "Счёт.md"]


@test
def test_the_snapshot_no_longer_dirties_the_tree_before_the_guard(tmp: Path):
    """Снимок «карточка → источник» пишется после охраны git: закоммиченный снимок прошлой пересборки раньше сам делал
    дерево грязным (файл переписывался до проверки), и охрана отказывала из-за файла, которому положено уйти."""
    root = _base(tmp)
    P70._put(root, "meta/trace/rebuild-snapshot.json", "{}")
    P70._commit(root)
    with _Ident():
        cp = _reset(root, "--apply", expect=0)
    assert "git-guard" not in cp.stderr and _cards(root) == [], (cp.stdout, cp.stderr)
    assert "Снимок соответствий сохранён: карточек 3" in cp.stdout, cp.stdout


@test
def test_status_says_whether_a_rebuild_is_under_way(tmp: Path):
    """`--status` и `--status --json`: идёт ли пересборка, откуда вернуть, сколько карточек было."""
    root = _base(tmp)
    assert json.loads(_reset(root, "--status", "--json", expect=0).stdout)["active"] is False
    assert "Незаконченной пересборки нет" in _reset(root, "--status", expect=0).stdout
    with _Ident():
        _reset(root, "--apply", expect=0)
    got = json.loads(_reset(root, "--status", "--json", expect=0).stdout)
    assert got["active"] is True and got["cards_before"] == 3 and got["tag"].startswith("aurora/pre-rebuild-"), got
    assert "Пересборка идёт с" in _reset(root, "--status", expect=0).stdout


# --------------------------------------------------------------------------- маршрут

def _rebuild_route():
    def st(cmd, flags, lite=False):
        return {"manual": False, "cmd": cmd, "why": "", "flags": flags, **({"lite": True} if lite else {})}
    return {"id": "rebuild", "title": "Пересобрать базу с нуля", "when": "", "group": "база", "destructive": True,
            "steps": [st("sync:audit", []), st("kb:reset", ["--apply"]),
                      {"manual": False, "cycle": "цикл:", "why": ""},
                      st("agent:build", ["--apply"]),
                      {"manual": False, "cycle": "конец цикла", "why": ""},
                      st("kb:links", ["--apply"], True), st("kb:trust", ["--apply"], True),
                      st("agent:twins", ["--apply"]), st("kb:reset", ["--finish", "--apply"])]}


def _script(build):
    return {"sync:audit": [(0, [])], "kb:reset": [(0, [])], "agent:build": build,
            "kb:links": [(0, [])], "kb:trust": [(0, [])], "agent:twins": [(0, [])]}


def _cmds(ck):
    return [c[1] + (" " + " ".join(c[2]) if c[2] else "") for c in ck.calls]


@test
def test_the_cycle_goes_past_twelve_laps_while_it_makes_progress(tmp: Path):
    """Прежние 12 оборотов обрывали большую пересборку: оборот — 24 источника. Пока за оборот остаток убывает, цикл идёт
    до конца (здесь 30 оборотов), потом выполняется весь хвост, включая шаги без пометки."""
    rr, _ = _modules()
    assert rr.CYCLE_LIMIT >= 100, "аварийный потолок снова мал: большая пересборка оборвётся на середине"
    build = [(0, [f"Источников в плане: {n + 1} → {n}"]) for n in range(29, -1, -1)]
    ck = FakePanel(tmp, _script(build), [_rebuild_route()])
    run_ = rr.RouteRun(ck, str(tmp), _rebuild_route(), True)
    res = run_.safe_run()
    assert res["ok"] is True and not run_.stalled, res
    cmds = _cmds(ck)
    assert cmds.count("agent:build --apply") == 30, cmds.count("agent:build --apply")
    assert cmds[-4:] == ["kb:links --apply", "kb:trust --apply", "agent:twins --apply", "kb:reset --finish --apply"], cmds[-4:]


@test
def test_a_stalled_rebuild_runs_the_short_tail_without_the_model_steps(tmp: Path):
    """Застой посреди цикла: выполняются только шаги с пометкой «хвост» (связи, доверие), шаги с моделью и «конец
    пересборки» — нет; вердикт остаётся «застой», а не «пройден»."""
    rr, _ = _modules()
    build = [(0, ["Источников в плане: 9 → 8"]), (0, ["Источников в плане: 8 → 8"])]
    ck = FakePanel(tmp, _script(build), [_rebuild_route()])
    run_ = rr.RouteRun(ck, str(tmp), _rebuild_route(), True)
    res = run_.safe_run()
    assert res["ok"] is False and res["reason"] == "stall", res
    cmds = _cmds(ck)
    assert cmds[-2:] == ["kb:links --apply", "kb:trust --apply"], cmds
    assert "agent:twins --apply" not in cmds and "kb:reset --finish --apply" not in cmds, cmds


@test
def test_a_failed_cycle_step_runs_the_short_tail_but_a_manual_stop_does_not(tmp: Path):
    """Сбой шага посреди цикла — краткий хвост выполняется (база остаётся согласованной); «Остановить» рукой — нет:
    остановка значит остановку. Сбой самого шага хвоста вердикт не меняет."""
    rr, _ = _modules()
    ck = FakePanel(tmp, _script([(2, ["упало"])]), [_rebuild_route()])
    res = rr.RouteRun(ck, str(tmp), _rebuild_route(), True).safe_run()
    assert res["ok"] is False and res["reason"] == "failed", res
    assert _cmds(ck)[-2:] == ["kb:links --apply", "kb:trust --apply"], _cmds(ck)
    ck = FakePanel(tmp, dict(_script([(0, ["Источников в плане: 9 → 8"])]), **{"kb:links": [(2, ["упало"])]}),
                   [_rebuild_route()])
    res = rr.RouteRun(ck, str(tmp), _rebuild_route(), True).safe_run()
    assert res["reason"] != "passed" and _cmds(ck).count("kb:links --apply") == 1, (res, _cmds(ck))
    # остановка рукой
    import threading
    stop = threading.Event()
    ck = FakePanel(tmp, _script([(0, ["Источников в плане: 9 → 8"])]), [_rebuild_route()])
    orig = ck.start_job

    def stopper(project, cmd, args, parent=""):
        job = orig(project, cmd, args, parent)
        if cmd == "agent:build":
            stop.set()
        return job
    ck.start_job = stopper
    res = rr.RouteRun(ck, str(tmp), _rebuild_route(), True, stop=stop).safe_run()
    assert res["reason"] == "stopped", res
    assert "kb:links --apply" not in _cmds(ck) and "kb:trust --apply" not in _cmds(ck), _cmds(ck)


@test
def test_a_rebuild_route_continues_an_unfinished_rebuild_instead_of_wiping(tmp: Path):
    """При отметке «пересборка идёт» маршрут пропускает шаги до цикла (проверка зеркал, снос) и идёт с цикла; без
    отметки идёт целиком; обычный (не опасный) маршрут отметку не читает."""
    KR = _kr()
    rr, _ = _modules()
    ck = FakePanel(tmp, _script([(0, ["Источников в плане: 3 → 0"])]), [_rebuild_route()])
    res = rr.RouteRun(ck, str(tmp), _rebuild_route(), True).safe_run()
    assert _cmds(ck)[:2] == ["sync:audit", "kb:reset --apply"], _cmds(ck)
    KR.write_state(str(tmp), {"schema": 1, "phase": "building", "started": "2026-10-29T08:00:00Z",
                              "tag": "aurora/pre-rebuild-x", "cards_before": 700})
    ck = FakePanel(tmp, _script([(0, ["Источников в плане: 3 → 0"])]), [_rebuild_route()])
    run_ = rr.RouteRun(ck, str(tmp), _rebuild_route(), True)
    res = run_.safe_run()
    cmds = _cmds(ck)
    assert res["ok"] is True and cmds[0] == "agent:build --apply", cmds
    assert "kb:reset --apply" not in cmds and "sync:audit" not in cmds, cmds
    assert cmds[-1] == "kb:reset --finish --apply", cmds
    plain = {"id": "update", "title": "Обновить", "when": "", "group": "база", "destructive": False,
             "steps": [{"manual": False, "cmd": "sync:audit", "why": "", "flags": []}]}
    ck = FakePanel(tmp, _script([(0, [])]), [plain])
    rr.RouteRun(ck, str(tmp), plain, True).safe_run()
    assert _cmds(ck) == ["sync:audit"], _cmds(ck)
    # «Посмотреть» (без записи) отметку не читает: ничего не пропускает
    ck = FakePanel(tmp, _script([(0, ["Источников в плане: 3 → 0"])]), [_rebuild_route()])
    rr.RouteRun(ck, str(tmp), _rebuild_route(), False).safe_run()
    assert _cmds(ck)[0] == "sync:audit", _cmds(ck)


@test
def test_the_shipped_rebuild_route_declares_its_tail_and_its_end(tmp: Path):
    """В `scenarios.txt` у «Пересобрать»: пометки «хвост» стоят у шагов без модели (связи, карты, трассировка, доверие,
    оглавления), последний шаг — `kb:reset --finish --apply`; у других маршрутов пометок нет."""
    import importlib
    _modules()
    ck = importlib.import_module("aurora_cockpit")
    routes = {s["id"]: s for s in ck.all_scenarios()}
    rb = routes["rebuild"]
    cmds = [s["cmd"] for s in rb["steps"] if s.get("lite")]
    assert cmds == ["kb:links", "kb:moc", "kb:moc", "ops:trace-table", "kb:trust", "kb:index"], cmds
    assert not any(c.startswith("agent:") or c == "kb:embed" for c in cmds), "в кратком хвосте шаг с моделью"
    real = [s for s in rb["steps"] if s.get("cmd")]
    at = [i for i, s in enumerate(real) if s["cmd"] == "kb:reset" and s["flags"] == ["--finish", "--apply"]]
    assert len(at) == 1, "нет шага, который отмечает конец пересборки"
    assert all(real[i].get("lite") is None or i < at[0] for i in range(len(real))), "хвостовой шаг стоит после конца пересборки"
    assert [s["cmd"] for s in real[at[0] + 1:]] == ["ops:stats", "ops:todo"], real[at[0] + 1:]
    for rid, sc in routes.items():
        if rid != "rebuild":
            assert not any(s.get("lite") for s in sc["steps"]), rid


@test
def test_the_health_part_shows_an_unfinished_rebuild(tmp: Path):
    """Часть здоровья `rebuild`: пусто, пока отметки нет или пересборка закончена; при начатой — активна, с тегом и
    числом карточек до сноса. Панель рисует чип на Мостике и две плитки в «Здоровье», подписи есть на обоих языках."""
    KR = _kr()
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import aurora_cockpit as ck
    assert "rebuild" in ck.HEALTH_PARTS and "rebuild" in ck.HEALTH_QUICK and "rebuild" in ck.HEALTH_FN
    from harness import make_project, ui_source
    root = make_project(tmp)
    assert ck.health(str(root), part="rebuild", fresh=True)["rebuild"] == {}
    KR.write_state(str(root), {"schema": 1, "phase": "building", "started": "2026-10-29T08:00:00Z",
                               "tag": "aurora/pre-rebuild-x", "commit": "abc1234", "cards_before": 700})
    got = ck.health(str(root), part="rebuild", fresh=True)["rebuild"]
    assert got["active"] is True and got["tag"] == "aurora/pre-rebuild-x" and got["cards_before"] == 700, got
    KR.write_state(str(root), {"schema": 1, "phase": "finished"})
    assert ck.health(str(root), part="rebuild", fresh=True)["rebuild"] == {}
    ui = ui_source()
    assert '"templates", "rebuild"]' in ui and "rebuild: {}" in ui and "rebuildHint" in ui
    tile = ui[ui.index("function renderOverview"):]
    tile = tile[:tile.index("drawActivity();")]
    assert 'has("rebuild")' in tile and "h.rebuild.active" in tile
    view = (KIT / "cockpit/modules/health/view.js").read_text(encoding="utf-8")
    assert "rb.active" in view and 'goCmd("kb:reset", ["--undo", "--apply"]' in view and 'goRoute("rebuild"' in view
    for lang in ("ru", "en"):
        core = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        mod = json.loads((KIT / f"cockpit/modules/health/i18n/{lang}.json").read_text(encoding="utf-8"))
        for k in ("overview.rebuild", "overview.rebuild_hint", "overview.rebuild_sources", "route.rebuild_resume",
                  "route.lite_tail", "route.lite_tail_step_failed"):
            assert core.get(k), (lang, k)
        for k in ("health.rebuild_title", "health.rebuild_continue", "health.rebuild_undo", "health.rebuild_back_title"):
            assert mod.get(k), (lang, k)


@test
def test_the_bridge_offers_adding_a_new_project(tmp: Path):
    """На Мостике путь к новому проекту виден сразу: кнопка в шапке, последняя плитка сетки и кнопка в пустом Мостике;
    все ведут в «Настройку кита» с раскрытой группой «Подключить новый проект» (`setup:new`); подписи — на обоих языках."""
    from harness import ui_source
    html = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert 'id="addProject"' in html and 'data-i18n="overview.add_project"' in html, "в шапке Мостика нет кнопки"
    ui = ui_source()
    go = ui[ui.index("function openNewProject()"):]
    go = go[:go.index("\n}\n")]
    assert 'SGROUPS.focus = "setup:new"' in go and 'show("setup")' in go, go
    assert '$("#addProject").onclick = openNewProject' in ui
    tile = ui[ui.index("function renderOverview"):]
    tile = tile[:tile.index("drawActivity();")]
    assert "tower add" in tile and "onclick: openNewProject" in tile, "последней плитки сетки нет"
    assert ui.count("onclick:openNewProject") >= 1, "в пустом Мостике нет кнопки"
    css = (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")
    assert ".tower.add" in css and ".tower.add::before" in css
    assert 'data-group": id' in ui and 'sgroup(box, "setup:new"' in ui, "группы «Подключить новый проект» с этим номером нет"
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        for k in ("overview.add_project", "overview.add_project_hint", "overview.add_project_about"):
            assert cat.get(k), (lang, k)
