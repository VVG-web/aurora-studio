"""Проверки движка Aurora, часть 22: движок в `.aurora/`, харнессы у каждого свои,
описания папок проекта (1.158.0).

Каркас и помощники — tests/harness.py. До 1.158.0 движок жил в `.opencode/` — папке
харнесса OpenCode — и ездил по git проекта. Переезд раскладывает её по смыслу и ничего
чужого не удаляет; в каждой папке схемы лежит `README.md` от кита, и движок не считает
его содержимым.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401


def _u():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    return importlib.import_module("aurora_update")


def _legacy(tmp: Path) -> Path:
    """Проект до 1.158.0: движок в `.opencode/`, а в нём — всё, что там бывает."""
    root = make_project(tmp)
    os.rename(root / ".aurora", root / ".opencode")
    old = root / ".opencode"
    (old / "scripts" / "ph5_build.py").write_text("print('свой скрипт проекта')\n", encoding="utf-8")
    (old / "scripts" / "kb_verify.py").write_text("# выведен из кита\n", encoding="utf-8")
    sk = old / "skills" / "jira-export-Test"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: jira-export-Test\n---\n"
                                 "python3 .opencode/scripts/jira_export.py\n", encoding="utf-8")
    (old / "skills" / ".DS_Store").write_bytes(b"\0")
    (old / "state").mkdir()
    (old / "state" / "last_route.json").write_text("{}", encoding="utf-8")
    (old / "runs" / "20261001-r1").mkdir(parents=True)
    (old / "run_log.md").write_text("# Журнал запусков\n", encoding="utf-8")
    (old / "update_ignore.txt").write_text("Templates/x.md\n.opencode/skills/jira-export-Test/SKILL.md\n",
                                           encoding="utf-8")
    (old / "kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    (old / "package.json").write_text('{"dependencies": {"@opencode-ai/plugin": "1"}}', encoding="utf-8")
    (old / "node_modules" / "x").mkdir(parents=True)
    (old / "node_modules" / "x" / "index.js").write_text("", encoding="utf-8")
    (root / "Prompts" / "мой.md").write_text("Контекст: `python3 .opencode/scripts/ctx_pack.py`\n",
                                             encoding="utf-8")
    return root


@test
def test_update_moves_the_engine_out_of_the_opencode_harness(tmp: Path):
    """Переезд: движок — в `.aurora/`, данные движка — туда же, журнал — в meta, навыки
    проекта — в `.claude/skills/`, свои скрипты — в `Scripts/`, копии кита убраны, файлы
    OpenCode остались, пути в файлах проекта поправлены."""
    U = _u()
    restore = set_home(tmp / "home")
    try:
        root = _legacy(tmp)
        rp = U.relayout_plan(root)
        moves = dict(rp["moves"])
        assert moves[".opencode/scripts/ph5_build.py"] == "Scripts/ph5_build.py"
        assert moves[".opencode/skills/jira-export-Test"] == ".claude/skills/jira-export-Test"
        assert moves[".opencode/run_log.md"] == "AuroraKnowledgeDB/meta/run_log.md"
        assert moves[".opencode/update_ignore.txt"] == "aurora.update_ignore.txt"
        assert moves[".opencode/state/last_route.json"] == ".aurora/state/last_route.json"
        assert ".opencode/scripts/kb_verify.py" in rp["drop"], "остаток кита принят за скрипт проекта"
        assert ".opencode/skills/aurora-vault" in rp["drop"], "навык кита переехал в навыки проекта"
        assert set(rp["keep"]) == {".opencode/package.json", ".opencode/node_modules"}, rp["keep"]
        assert U.run(root, apply=True) == 0
        assert (root / ".aurora/scripts/aurora_update.py").is_file(), "движок не встал в .aurora/"
        assert (root / ".aurora/state/last_route.json").is_file() and (root / ".aurora/runs/20261001-r1").is_dir()
        assert (root / "Scripts/ph5_build.py").is_file(), "свой скрипт проекта потерян"
        assert (root / "AuroraKnowledgeDB/meta/run_log.md").is_file()
        skill = (root / ".claude/skills/jira-export-Test/SKILL.md").read_text(encoding="utf-8")
        assert ".aurora/scripts/jira_export.py" in skill, "путь в навыке проекта не поправлен"
        ui = (root / "aurora.update_ignore.txt").read_text(encoding="utf-8")
        assert ".claude/skills/jira-export-Test/SKILL.md" in ui and ".opencode" not in ui
        assert ".aurora/scripts/ctx_pack.py" in (root / "Prompts/мой.md").read_text(encoding="utf-8")
        left = sorted(p.name for p in (root / ".opencode").iterdir())
        assert left == ["node_modules", "package.json"], f"в .opencode осталось чужое движку: {left}"
        gi = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".aurora/" in gi and ".opencode/" in gi and "!.claude/skills/" in gi
        # повторный прогон: переезжать нечего
        again = U.relayout_plan(root)
        assert not (again["moves"] or again["drop"] or again["clash"]), f"переезд не завершился: {again}"
    finally:
        restore()


@test
def test_the_move_waits_for_a_running_agent(tmp: Path):
    """Пишущий прогон агента держит замок — переезд не перекладывает его состояние."""
    U = _u()
    restore = set_home(tmp / "home")
    try:
        root = _legacy(tmp)
        (root / ".opencode/state/agent.lock").write_text(
            json.dumps({"pid": os.getpid(), "task": "build"}), encoding="utf-8")
        assert U.agent_running(root).startswith("build")
        assert U.run(root, apply=True) == 3
        assert (root / ".opencode/scripts").is_dir() and not (root / "Scripts/ph5_build.py").exists(), \
            "переезд начался при работающем агенте"
    finally:
        restore()


@test
def test_every_kit_folder_has_a_guide_and_notes_survive(tmp: Path):
    """У каждой папки схемы — описание; блок кита обновляется, заметки проекта ниже метки
    остаются, чужой README.md не теряется; правило вложенных папок — по схеме."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    FG = importlib.import_module("folder_guides")
    guides = FG.load(KIT)
    schema = [ln.strip() for ln in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    own = {"AuroraKnowledgeDB", "TemplatesCommon"}         # у них описание от кита уже есть
    tops = {p.split("/")[0] for p in schema} - own
    missing = [p for p in sorted(set(schema) | tops) if p not in guides and p not in own]
    assert not missing, f"папки схемы без описания: {missing}"
    for path, g in guides.items():
        for field in ("title", "what", "put", "not", "who"):
            assert g.get(field), f"{path}: нет поля {field}"
    assert "задаёт кит" in FG.render("Raw", guides["Raw"]), "у корня схемы нет правила второго уровня"
    assert "`_`" in FG.render("Raw/project", guides["Raw/project"])
    root = make_project(tmp)
    (root / "Workspaces/README.md").write_text("# Мой реестр задач\n", encoding="utf-8")
    FG.apply(root, FG.plan(root, KIT))
    ws = (root / "Workspaces/README.md").read_text(encoding="utf-8")
    assert ws.startswith(FG.BEGIN) and "# Мой реестр задач" in ws, "чужое описание потеряно"
    raw = root / "Raw/project/README.md"
    raw.write_text(raw.read_text(encoding="utf-8").replace("Человек.", "СТАРОЕ.") + "\nМои заметки.\n",
                   encoding="utf-8")
    changes = FG.plan(root, KIT)
    assert [c[0] for c in changes] == ["Raw/project/README.md"], changes
    FG.apply(root, changes)
    text = raw.read_text(encoding="utf-8")
    assert "СТАРОЕ" not in text and "Мои заметки." in text, "заметки проекта под меткой пропали"
    assert FG.plan(root, KIT) == [], "описания переписываются на каждом обновлении"


@test
def test_a_folder_guide_is_never_content(tmp: Path):
    """README.md папки — не источник, не шаблон, не артефакт и не карточка."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    BP = importlib.import_module("build_plan")
    root = make_project(tmp)
    for d in ("Raw/project", "Artifacts/acceptance", "AuroraKnowledgeDB/Concepts"):
        (root / d / "README.md").write_text("# описание\n", encoding="utf-8")
    assert not [p for p in AC.walk_md(str(root)) if p.endswith("README.md")], \
        "общий обход отдал описание папки как содержимое"
    assert "README.md" in BP.SKIP, "план разбора возьмёт описание папки документом"
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert ck.count("is_folder_guide(") >= 3, "панель покажет описание шаблоном или артефактом"


@test
def test_the_panel_finds_an_engine_that_has_not_moved_yet(tmp: Path):
    """Проект, ещё не обновлённый с 1.158.0, работает: движок находится в `.opencode/`,
    журнал пишется на прежнее место; навыки проекта ищутся в `.claude/skills/` первыми."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    RC = importlib.import_module("request_context")
    root = make_project(tmp)
    assert AC.engine_dir(root) == ".aurora"
    os.rename(root / ".aurora", root / ".opencode")
    assert AC.engine_dir(root) == ".opencode"
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    assert ck.runlog_path(str(root)).endswith(os.path.join(".opencode", "run_log.md"))
    dirs = RC.skill_dirs(str(root), str(KIT))
    assert dirs[0] == os.path.join(str(root), ".claude", "skills"), dirs
    assert os.path.join(str(root), ".agents", "skills") in dirs
