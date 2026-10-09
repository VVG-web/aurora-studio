"""Проверки движка Aurora, часть 33: дата обновления базы — общая для команды и в git;
журнал запусков — вне git (1.168.0).

Каркас и помощники — tests/harness.py. Журнал запусков (`run_log.md`) менялся каждым
запуском команды: каждый восьмой коммит проекта и вечно грязное дерево. А дата обновления
базы — важное знание, которое меняется раз в день, — нигде отдельно не хранилась.
Обновлением базы считается полное завершение «Обновить базу» и следом «Починить базу».
"""
from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

from harness import KIT, SCRIPTS, set_home, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_the_base_update_date_is_update_then_fix(tmp: Path):
    """Дата двигается только «Починить» после последнего «Обновить»: одно «Обновить» — ещё
    не обновление; «Починить» без нового «Обновить» дату не трогает."""
    _path()
    import kb_updated as KU
    p = tmp / "proj"
    assert KU.mark(p, "fix", at="2026-10-01T10:00:00Z").get("updated") is None, \
        "«Починить» без «Обновить» засчитано обновлением"
    rec = KU.mark(p, "update", who="Аналитик", kit="1.168.0", at="2026-10-02T10:00:00Z")
    assert "updated" not in rec, "одно «Обновить» засчитано обновлением"
    rec = KU.mark(p, "fix", who="Аналитик", at="2026-10-02T11:00:00Z")
    assert rec["updated"] == "2026-10-02T11:00:00Z" and rec["fix"]["who"] == "Аналитик"
    assert KU.mark(p, "fix", at="2026-10-03T09:00:00Z")["updated"] == "2026-10-02T11:00:00Z", \
        "повторное «Починить» без нового «Обновить» сдвинуло дату"
    KU.mark(p, "update", at="2026-10-04T08:00:00Z")
    assert KU.read(p)["updated"] == "2026-10-02T11:00:00Z"
    assert KU.mark(p, "fix", at="2026-10-04T09:00:00Z")["updated"] == "2026-10-04T09:00:00Z"
    assert KU.mark(p, "build") is None
    saved = json.loads((p / KU.REL).read_text(encoding="utf-8"))
    assert saved["update"]["at"] == "2026-10-04T08:00:00Z"


@test
def test_the_date_comes_from_run_history_where_it_is_missing(tmp: Path):
    """Проект без файла даты получает её из истории прогонов этой машины: в счёт идут только
    пройденные с записью «Обновить» и «Починить»; файл уже есть — не трогаем."""
    _path()
    import kb_updated as KU
    runs = tmp / "proj" / ".aurora" / "runs"
    for rid, sc, ok, write, fin in (("a-route", "update", True, True, "2026-10-05T10:00:00Z"),
                                    ("b-route", "fix", False, True, "2026-10-05T11:00:00Z"),
                                    ("c-route", "fix", True, False, "2026-10-05T12:00:00Z"),
                                    ("d-route", "fix", True, True, "2026-10-05T13:00:00Z"),
                                    ("e-route", "update", True, True, "2026-10-06T10:00:00Z")):
        (runs / rid).mkdir(parents=True)
        (runs / rid / "meta.json").write_text(json.dumps(
            {"kind": "route", "id": rid, "scId": sc, "ok": ok, "write": write, "finished": fin,
             "who": "Аналитик", "kit": "1.167.0"}), encoding="utf-8")
    rec = KU.backfill(tmp / "proj", str(runs))
    assert rec["updated"] == "2026-10-05T13:00:00Z", rec
    assert rec["update"]["at"] == "2026-10-06T10:00:00Z" and rec["fix"]["run"] == "d-route", rec
    assert KU.backfill(tmp / "proj", str(runs)) is None, "существующую дату переписали историей"


@test
def test_a_passed_update_and_fix_record_the_date_in_the_route_commit(tmp: Path):
    """Маршрут сам пишет дату при успешном конце — до итогового коммита, значит, она уходит
    в git с ним. Цепочка «Обновить» → «Починить» по проекту даёт дату; одно «Обновить» —
    нет."""
    _path()
    from cases import part_18 as P18
    import kb_updated as KU
    restore = set_home(tmp / "home")
    try:
        projects = [{"name": "Альфа", "path": str(tmp / "a")}]
        task = P18._task(steps=[{"kind": "route", "project": "*", "route": "update", "write": True},
                                {"kind": "route", "project": "*", "route": "fix", "write": True}],
                         order="projects")
        cron, ck, sched = P18._scheduler(tmp, {"kb:repair": [(0, [])], "kb:lint": [(0, [])]},
                                         projects, [task])
        sched.enqueue(task["id"])
        P18._wait(sched)
        rec = KU.read(tmp / "a")
        assert rec.get("updated") and rec["updated"] == rec["fix"]["at"], rec
        assert rec["update"]["who"] == "Тестировщик" and rec["update"]["kit"] == "9.9.9", rec
        text = "\n".join(sched.live(0)["lines"])
        assert "База знаний обновлена" in text, text[-600:]
    finally:
        restore()


@test
def test_the_run_journal_leaves_git_and_the_date_shows_everywhere(tmp: Path):
    """Журнал запусков — под правилом кита в .gitignore, обновление движка снимает его с
    учёта git, оставляя файл на диске. Дату обновления базы показывают плитка проекта на
    Мостике, «Здоровье», «Спросить» и «Продуктивность»."""
    _path()
    import install_aurora as IA
    import aurora_update as AU
    assert "AuroraKnowledgeDB/meta/run_log.md" in IA.GITIGNORE_BLOCK.splitlines()
    repo = tmp / "repo"
    (repo / "AuroraKnowledgeDB" / "meta").mkdir(parents=True)
    log = repo / "AuroraKnowledgeDB" / "meta" / "run_log.md"
    log.write_text("| kb:lint | x |\n", encoding="utf-8")

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    assert AU.untrack_runlog(repo) is True
    assert log.is_file(), "журнал удалён с диска"
    assert not git("ls-files", "--", "AuroraKnowledgeDB/meta/run_log.md").stdout.strip()
    assert AU.untrack_runlog(repo) is False, "снятый журнал снимается второй раз"
    ui = ui_source()
    assert "function kbInfo(" in ui and "kbInfo(p)" in ui, "плитка проекта без даты обновления базы"
    views = {m: (COCKPIT / "modules" / m / "view.js").read_text(encoding="utf-8")
             for m in ("health", "ask", "work")}
    for m, v in views.items():
        assert "ctx.ui.kbInfo(ctx.project)" in v, f"{m}: без даты обновления базы"
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"kb_updated": KBU.read(path)' in src and '"kb_updated": KBU.read(project)' in src
