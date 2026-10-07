"""Проверки движка Aurora, часть 31: Мостик и «Здоровье» из памяти панели; брошенные
процессы прежних панелей снимаются на старте (1.166.0).

Каркас и помощники — tests/harness.py. Здоровье считалось на каждую загрузку страницы —
минуты на шесть проектов, — и Мостик при каждом заходе «обновлялся» спиннерами. Адаптер
модели, брошенный упавшей панелью, прожил 3 ч 23 мин, держа место на шлюзе: его ответ
никто бы не прочёл.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import threading
import time

from harness import KIT, SCRIPTS, make_project, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_health_parts_come_from_panel_memory(tmp: Path):
    """Часть здоровья считается один раз и дальше отдаётся из памяти панели с временем
    счёта; `fresh` — пересчёт; ту же часть, которую уже считают, второй раз не запускаем —
    ждём готовую; здоровье целиком (скрипты, проверки) — всегда заново."""
    _path()
    import aurora_cockpit as ck
    root = str(make_project(tmp))
    calls = []
    saved = ck.HEALTH_FN["doctor"]

    def slow(project, lang):
        calls.append(time.time())
        time.sleep(0.3)
        return {"doctor": {"errors": [f"n{len(calls)}"], "warns": []}}
    ck.HEALTH_FN["doctor"] = slow
    try:
        first = ck.health(root, part="doctor")
        again = ck.health(root, part="doctor")
        assert len(calls) == 1, "часть посчитана второй раз без «Обновить»"
        assert again["doctor"] == first["doctor"] and again["at"]["doctor"] == first["at"]["doctor"]
        fresh = ck.health(root, part="doctor", fresh=True)
        assert len(calls) == 2 and fresh["at"]["doctor"] > first["at"]["doctor"]
        assert fresh["doctor"]["errors"] == ["n2"]
        got = []
        ts = [threading.Thread(target=lambda: got.append(ck.health(root, part="doctor", fresh=True)))
              for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(calls) == 3, f"одну часть считали одновременно {len(calls) - 2} раз"
        assert len({g["at"]["doctor"] for g in got}) == 1
        whole = ck.health(root)
        assert len(calls) == 4 and "at" not in whole, "здоровье целиком взято из памяти"
    finally:
        ck.HEALTH_FN["doctor"] = saved
        with ck.HEALTH_LOCK:
            for k in [k for k in ck.HEALTH_CACHE if k[0] == os.path.realpath(root)]:
                ck.HEALTH_CACHE.pop(k, None)


@test
def test_the_panel_warms_health_once_at_start(tmp: Path):
    """Прогрев на старте: быстрые части всех проектов, потом долгие — по проекту за раз; на
    странице Мостик и выбор проекта берут готовое, пересчитывают «Обновить» и команда в
    проекте; дата «обновлено» — у самого старого числа."""
    _path()
    import aurora_cockpit as ck
    order = []
    saved = dict(ck.HEALTH_FN)
    def recorder(name):
        def fn(project, lang):
            order.append((os.path.basename(project), name))
            return {name: {}}
        return fn
    for part in ck.HEALTH_PARTS:
        ck.HEALTH_FN[part] = recorder(part)
    (tmp / "a").mkdir()
    (tmp / "b").mkdir()
    try:
        ck.warm_health(lambda: [{"path": str(tmp / "a")}, {"path": str(tmp / "b")}])
    finally:
        ck.HEALTH_FN.clear()
        ck.HEALTH_FN.update(saved)
        with ck.HEALTH_LOCK:
            for k in [k for k in ck.HEALTH_CACHE if k[0].startswith(os.path.realpath(tmp))]:
                ck.HEALTH_CACHE.pop(k, None)
    quick = [i for i, (_p, part) in enumerate(order) if part in ck.HEALTH_QUICK]
    slow = [i for i, (_p, part) in enumerate(order) if part in ck.HEALTH_SLOW]
    assert len(order) == 2 * len(ck.HEALTH_PARTS) and max(quick) < min(slow), order
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert "target=warm_health" in src, "панель не прогревает здоровье на старте"
    ui = ui_source()
    pick = ui[ui.index("async function pick("):ui.index("async function pick(") + 2000]
    assert "loadHealth(p, HEALTH_ALL);" in pick, "выбор проекта снова пересчитывает здоровье"
    assert "await prefetchHealth(true);" in ui, "«Обновить» на Мостике не пересчитывает"
    assert '(force ? "&fresh=1" : "")' in ui
    assert "function drawBridgeStamp(" in ui and "healthAt(p.health)" in ui


@test
def test_a_new_panel_takes_down_processes_abandoned_by_a_dead_one(tmp: Path):
    """Брошенный процесс движка — с меткой панели, которой больше нет, — снимается новой
    панелью. Не трогаются: процессы своей и живых панелей, сама панель, процессы не движка
    (браузер, чужой MCP), всё без метки — прогон из терминала."""
    _path()
    import watchdog as WD
    killed = []
    kit = str(tmp / "kit")
    rows = [
        {"pid": 10, "panel": 500, "command": "python /p/X/.aurora/scripts/agent_runner.py --task extract"},
        {"pid": 11, "panel": 500, "command": "python /p/X/.aurora/scripts/agents/pydantic_ai_adapter.py"},
        {"pid": 12, "panel": 600, "command": "python /p/Y/.aurora/scripts/agent_runner.py --task build"},
        {"pid": 13, "panel": 500, "command": "/Applications/Browser.app/Contents/MacOS/Browser"},
        {"pid": 14, "panel": 500, "command": f"python {kit}/cockpit/aurora_cockpit.py --port 8787"},
        {"pid": 15, "panel": 700, "command": "python /p/Z/.aurora/scripts/agent_runner.py --task ask"},
        {"pid": 16, "panel": 500, "command": f"python {kit}/scripts/kb_embed.py --apply"},
    ]
    alive = {10, 11, 12, 13, 14, 15, 16, 600, 700}
    gone = WD.reap_orphans(kit, own=700, rows=rows, kill=lambda pid: killed.append(pid),
                           alive=lambda pid: pid in alive)
    assert killed == [10, 11, 16], killed
    assert gone[0]["what"] == "agent_runner.py --task extract · X", gone
    if sys.platform == "win32":
        return
    # Настоящий процесс: скрипт движка с меткой умершей панели.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    scripts = tmp / "proj" / ".aurora" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sleeper.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    env = dict(os.environ, **{WD.PANEL_MARK: str(dead.pid)})
    orphan = subprocess.Popen([sys.executable, str(scripts / "sleeper.py")], env=env)
    try:
        for _ in range(50):
            if any(r["pid"] == orphan.pid for r in WD.marked_processes()):
                break
            time.sleep(0.1)
        got = WD.reap_orphans(str(KIT))
        assert any(r["pid"] == orphan.pid for r in got), got
        orphan.wait(timeout=10)
    finally:
        if orphan.poll() is None:
            orphan.kill()
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert "os.environ[WD.PANEL_MARK] = str(os.getpid())" in src
    assert "target=reap_orphans_on_start" in src
    ui = ui_source()
    assert "showReaped(S.state.reaped)" in ui and '"aurora-reaped-seen"' in ui
