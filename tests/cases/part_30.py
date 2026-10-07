"""Проверки движка Aurora, часть 30: Мостик и «Здоровье» рисуются сразу и заполняются по
мере прихода чисел; Cron говорит, кого ждёт (1.165.0).

Каркас и помощники — tests/harness.py. После обновления кита Мостик полминуты стоял пустым:
`/api/state` ждал сборку реестра команд (`--help` полусотни скриптов). «Здоровье» крупной
базы считалось минуту подряд — линтер всей базы и дела человеку — и всё это время страница
была пустой. Пункт цепочки, ждавший проект, занятый прогоном прежней цепочки, стоял «идёт»
без единой строки.
"""
from __future__ import annotations

from pathlib import Path
import sys

from harness import KIT, SCRIPTS, make_project, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_health_comes_in_parts_and_whole_health_is_the_same(tmp: Path):
    """Часть здоровья — только её ключи и имя проекта; без `part` — все части, те же ключи,
    что и прежде, посчитанные одновременно; неизвестная часть — пусто, а не ошибка."""
    _path()
    import aurora_cockpit as ck
    root = str(make_project(tmp))
    full = ck.health(root)
    keys = {"project", "stats", "lint", "doctor", "mirrors", "build", "agent", "sources", "runs",
            "trace", "todo", "source_health", "index", "ping", "unfinished", "corrections",
            "retrieval"}
    assert set(full) == keys, sorted(set(full) ^ keys)
    got = {}
    for part in ck.HEALTH_PARTS:
        r = ck.health(root, part=part)
        assert r["project"] == root and r["parts"] == [part], r
        got.update({k: v for k, v in r.items() if k not in ("project", "parts")})
    assert set(got) | {"project"} == keys, "части вместе не дают всего здоровья"
    two = ck.health(root, part="doctor,build")
    assert set(two) == {"project", "parts", "doctor", "build"}, sorted(two)
    assert set(ck.health(root, part="nope")) == {"project", "parts"}


@test
def test_the_bridge_draws_at_once_and_fills_in(_t):
    """Мостик: плитки сводки с подписями — до первого ответа; состояние — двумя половинами
    (`part=core` сразу, реестр и окружение — в фоне); здоровье проектов — частями, сначала
    быстрые по всем проектам, потом долгие; команды ждут реестр, а не падают «нет такой»."""
    ui = ui_source()
    boot = ui[ui.index("async function boot("):ui.index("/* ---------------- мостик ---------------- */")]
    assert boot.index("drawGlobalMetrics();") < boot.index('await api("/api/state?part=core")')
    assert '"/api/state?part=extra"' in boot and "S.extraP" in boot
    assert 'await api("/api/state")' not in boot, "Мостик снова ждёт реестр команд"
    pre = ui[ui.index("async function prefetchHealth("):]
    pre = pre[:pre.index("\n}\n")]
    assert pre.index("HEALTH_QUICK") < pre.index("HEALTH_SLOW")
    for fn in ("async function openRun(", "const goCmd = "):
        body = ui[ui.index(fn):ui.index(fn) + 300]
        assert "await S.extraP" in body, f"{fn} не ждёт реестр команд"
    load = ui[ui.index("function loadHealth("):]
    load = load[:load.index("\n}\n")]
    assert '"&part=" + part' in load and "r.project === p.path" in load
    assert '/api/health?project=" + encodeURIComponent(p.path));' not in ui, \
        "здоровье снова спрашивается целиком и одним ответом"


@test
def test_health_tiles_stand_before_their_numbers(_t):
    """«Здоровье»: каждая плитка стоит с подписью с первого кадра и ждёт свою часть;
    «Зеркала» и «Установка» ждут только своих частей; цвет до прихода доктора и линтера
    не выдумывается."""
    view = (COCKPIT / "modules/health/view.js").read_text(encoding="utf-8")
    for key, part in (("blockers", "doctor"), ("trust", "stats"), ("errors", "lint"),
                      ("left_to_human", "todo"), ("freshness", "build"), ("index", "files")):
        assert f'card("{part}", {{title: t("health.{key}"),' in view, (key, part)
    assert 'ctx.ui.has(h, part) ? ctx.ui.metricCard(o)' in view
    assert 'val("lint", h.lint.errors' in view and 'if (!has("doctor"))' in view
    mirrors = (COCKPIT / "modules/mirrors/view.js").read_text(encoding="utf-8")
    assert 'ctx.ui.has(ctx.health, "mirrors")' in mirrors
    install = (COCKPIT / "modules/install/view.js").read_text(encoding="utf-8")
    assert 'ctx.ui.has(h, "lint")' in install and 'ctx.ui.has(h, "doctor")' in install
    ui = ui_source()
    aura = ui[ui.index("function auraWhy("):]
    aura = aura[:aura.index("\n}\n")]
    assert '(docOk && lintOk ? "green" : "")' in aura, "зелёный — до прихода чисел"
    assert "Number.isFinite(h.lint.errors)" in aura, "линтер без числа снова «undefined ошибок»"


@test
def test_a_chain_item_says_whom_it_waits_for(tmp: Path):
    """Пункт цепочки ждёт занятый проект — и пишет в журнал, кого: прогон движка, пережив-
    ший перезапуск панели, или команду этой панели; освободился — пишет, что продолжает."""
    _path()
    import cron as CRON
    act = {"agent": {"task": "extract", "pid": 4242, "since": "2026-10-07T17:54:01Z", "alive": True}}
    line = CRON.busy_line(act)
    assert "agent:extract" in line and "4242" in line and "перезапуск панели" in line, line
    assert "agent:distill" in CRON.busy_line({"running": [{"cmd": "agent:distill"}]})
    assert "в панели идёт команда" in CRON.busy_line({"running": [1], "agent": None}), \
        "запись задания не того вида роняет поток цепочки"

    class Ck:
        calls = 0

        def project_activity(self, project):
            Ck.calls += 1
            return act if Ck.calls < 3 else {}
    sch = CRON.Scheduler(Ck(), lambda: [])
    said = []
    saved = CRON.BUSY_POLL_S
    CRON.BUSY_POLL_S = 0
    try:
        assert sch._wait_free("/p", said.append) is True
    finally:
        CRON.BUSY_POLL_S = saved
    assert len(said) == 2 and "agent:extract" in said[0] and "свободен" in said[1], said
    ui = ui_source()
    assert 't("poll.job_orphan"' in ui, "Консоль молчит о прогоне, пережившем перезапуск"


@test
def test_chatbox_gets_aurora_only_while_closed(tmp: Path):
    """Chatbox — чат-клиент с MCP: найден по папке настроек, Аврора дописывается в конец
    `settings.mcp.servers`, прочее не трогается, отступ табуляцией сохраняется, повтор не
    дописывает второй раз, «вернуть» восстанавливает файл как был. Пока Chatbox открыт —
    отказ: при выходе он переписывает файл, и запись пропала бы."""
    import json
    _path()
    from harness import set_home
    restore_home = set_home(tmp)
    try:
        import harness_mcp as HM
        cfg = Path(HM.config_path(HM.harness("chatbox")))
        cfg.parent.mkdir(parents=True, exist_ok=True)
        before = json.dumps({"configs": {"uuid": "u-1"}, "settings": {
            "theme": 1, "mcp": {"servers": [{"id": "s-1", "name": "notes", "enabled": True,
                                             "transport": {"type": "stdio", "command": "notes-mcp",
                                                           "args": []}}],
                                "enabledBuiltinServers": []}}, "configVersion": 15},
                            ensure_ascii=False, indent="\t")
        cfg.write_text(before, encoding="utf-8")
        row = next(r for r in HM.detect(versions=False) if r["id"] == "chatbox")
        assert row["found"] and row["aurora"] is False, row
        saved = HM.app_running
        HM.app_running = lambda names: True
        try:
            HM.add("chatbox")
            raise AssertionError("подключил при открытом Chatbox")
        except HM.HarnessError as e:
            assert "закройте" in str(e)
        finally:
            HM.app_running = saved
        assert cfg.read_text(encoding="utf-8") == before, "отказ всё-таки тронул файл"
        HM.app_running = lambda names: False
        try:
            assert HM.add("chatbox")["ok"]
            after = cfg.read_text(encoding="utf-8")
            data = json.loads(after)
            servers = data["settings"]["mcp"]["servers"]
            assert [s["name"] for s in servers] == ["notes", "aurora"], servers
            assert servers[1]["transport"]["type"] == "stdio" and servers[1]["enabled"] is True
            assert after.startswith("{\n\t"), "отступ стал не таким, как у Chatbox"
            data["settings"]["mcp"]["servers"] = servers[:1]
            assert data == json.loads(before), "правка задела чужие настройки"
            assert "уже подключена" in HM.add("chatbox")["done"]
            assert HM.restore("chatbox")["ok"] and cfg.read_text(encoding="utf-8") == before
        finally:
            HM.app_running = saved
    finally:
        restore_home()
    cat = (COCKPIT / "modules/install/i18n/en.json").read_text(encoding="utf-8")
    assert "install.hs_note.chatbox" in cat


@test
def test_a_leaving_panel_takes_its_jobs_with_it(tmp: Path):
    """Панель уходит (перезапуск, Ctrl+C) — её задания снимаются вместе с потомками. Иначе
    задание, чей вывод шёл в трубу панели, сиротой держит замок базы, пока ждёт модель, а
    продолженная цепочка стоит «идёт» и ждёт его до двух часов."""
    import subprocess
    import time
    _path()
    import aurora_cockpit as ck
    from aurora_common import pid_alive
    marker = tmp / "child.pid"
    code = ("import subprocess, sys, time\n"
            f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(marker)!r}, 'w').write(str(c.pid))\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code])
    for _ in range(100):
        if marker.is_file() and marker.read_text().strip():
            break
        time.sleep(0.05)
    child = int(marker.read_text())
    job = {"id": "j-exit", "done": False, "proc": proc, "project": "/p", "cmd": "agent:extract"}
    with ck.JOBS_LOCK:
        ck.JOBS["j-exit"] = job
    try:
        assert ck.stop_jobs_on_exit() == 1
        proc.wait(timeout=10)
        for _ in range(100):
            if not pid_alive(child):
                break
            time.sleep(0.05)
        assert not pid_alive(child), "потомок задания пережил уход панели"
        assert ck.stop_jobs_on_exit() == 0, "снятое задание снимается второй раз"
    finally:
        with ck.JOBS_LOCK:
            ck.JOBS.pop("j-exit", None)
        if proc.poll() is None:
            proc.kill()


@test
def test_files_show_diagrams_json_logs_and_text_on_the_right(_t):
    """«Файлы»: у схем mermaid, JSON, журналов и простого текста справа — вид, слева — тот же
    редактор. Mermaid — из поставки редактора (без сети); JSON — дерево и «Форматировать»;
    журнал — подсветка уровней и хвост длинного файла. Расширения открываются как текст."""
    _path()
    import aurora_cockpit as ck
    assert {".mmd", ".mermaid", ".log", ".jsonl", ".json", ".txt"} <= ck.TEXT_EXT
    ui = ui_source()
    for need in ('[/\\.(mmd|mermaid)$/i, "mermaid"]', '[/\\.json$/i, "json"]',
                 '[/\\.(log|jsonl)$/i, "log"]', '[/\\.txt$/i, "text"]',
                 'Vditor.mermaidRender(box, "/vendor/vditor"', "function viewJson(",
                 "function viewLog(", "const VIEW_LINES = 5000", '$("#fileFormat").onclick = formatFile'):
        assert need in ui, need
    assert (KIT / "cockpit/vendor/vditor/dist/js/mermaid/mermaid.min.js").is_file(), \
        "mermaid ушёл из поставки — схемы перестанут рисоваться без сети"
    assert 'id="fileFormat" data-help="help.files.fileFormat"' in ui
    assert 'id="filePreview"' in ui and ".file-body.split" in ui
