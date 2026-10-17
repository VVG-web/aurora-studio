"""Проверки движка Aurora, часть 45: движок проекта на этой машине.

Каркас и помощники — tests/harness.py. Папка `.aurora/` (копия движка) не едет через git: клон
проекта получает её только от кита. Здесь проверяется, что неполный или отсутствующий движок
называется по имени, готовится сам и не прячется за молчаливым откатом на скрипты кита, а причина
«мимо Pydantic AI» говорит, какого именно файла нет. Сети, модели и настоящего домашнего каталога
проверки не касаются.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import shutil
import subprocess
import sys
import time

from harness import KIT, SCRIPTS, isolated_registry, set_home, test  # noqa: F401


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _python_rel() -> str:
    return "Scripts/python.exe" if os.name == "nt" else "bin/python"


# ------------------------------------------------------------------ Pydantic AI: причина обхода

@test
def test_adapter_gap_names_the_missing_file_and_the_cure(tmp: Path):
    """Нет venv и нет файла адаптера — разные причины с путём и лекарством; пути только под
    `~/.aurora` и `<проект>/.aurora`, без имени пользователя и папок вне них."""
    AC = _mod("agent_core")
    restore = set_home(tmp / "user-home")
    saved = (AC.VENV, AC._adapter_file, dict(AC._HUB))
    venv = tmp / "user-home" / ".aurora" / "venv"
    adapter = tmp / "customer-folder" / "project" / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py"
    try:
        AC.VENV = venv
        AC._adapter_file = lambda: adapter
        # 1. нет ничего: называются оба файла
        both = AC.adapter_gap()
        assert f"venv python: ~/.aurora/venv/{_python_rel()} не найден" in both, both
        assert "«Установка» → Pydantic AI" in both, both
        assert "адаптер: .aurora/scripts/agents/pydantic_ai_adapter.py не найден" in both, both
        # 2. venv есть, адаптера нет: причина про адаптер и про «Версия», про venv — ни слова
        py = venv / _python_rel()
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
        only_adapter = AC.adapter_gap()
        assert only_adapter.startswith("адаптер: .aurora/scripts/agents/"), only_adapter
        assert "«Версия» → «Подготовить проект»" in only_adapter and "venv" not in only_adapter, only_adapter
        assert AC.gap_kind(only_adapter) == "adapter"
        # 3. адаптер есть, venv нет: причина про venv и про «Установка»
        py.unlink()
        adapter.parent.mkdir(parents=True)
        adapter.write_text("", encoding="utf-8")
        only_venv = AC.adapter_gap()
        assert only_venv.startswith("venv python: ~/.aurora/venv/"), only_venv
        assert "«Установка» → Pydantic AI" in only_venv and "адаптер" not in only_venv, only_venv
        assert AC.gap_kind(only_venv) == "venv"
        # 4. на месте всё: причины нет
        py.write_text("", encoding="utf-8")
        assert AC.adapter_gap() == ""
        # чужого в сообщениях нет: ни временной папки, ни имени пользователя, ни папки заказчика
        for text in (both, only_adapter, only_venv):
            for stranger in (str(tmp), "customer-folder", "user-home"):
                assert stranger not in text, (stranger, text)
    finally:
        AC.VENV, AC._adapter_file = saved[0], saved[1]
        AC._HUB.update(saved[2])
        restore()


@test
def test_adapter_hub_and_transport_carry_the_exact_gap(tmp: Path):
    """`adapter_hub` и транспорт Pydantic AI отдают причину с путём, а не одно «venv не установлен»;
    самопроверка говорит то же. Два случая дают разный текст."""
    AC = _mod("agent_core")
    restore = set_home(tmp / "home")
    saved = (AC.VENV, AC._adapter_file, dict(AC._HUB))
    adapter = tmp / "project" / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py"
    venv = tmp / "home" / ".aurora" / "venv"
    reasons = []
    try:
        AC.VENV = venv
        AC._adapter_file = lambda: adapter
        for case in ("no-venv", "no-adapter"):
            AC._HUB.update(proc=None, broken="", deaths=0)
            for p in (venv / _python_rel(), adapter):
                p.unlink(missing_ok=True)
            if case == "no-venv":
                adapter.parent.mkdir(parents=True, exist_ok=True)
                adapter.write_text("", encoding="utf-8")
            else:
                (venv / _python_rel()).parent.mkdir(parents=True, exist_ok=True)
                (venv / _python_rel()).write_text("", encoding="utf-8")
            proc, why = AC.adapter_hub()
            assert proc is None and why == AC.adapter_gap(), (case, why)
            st, body, err, _dt = AC.pydantic_transport(
                {"url": "http://127.0.0.1:9/v1", "key": ""}, {"model": "m", "messages": []}, 1)
            assert st is None and body is None and err == AC.ADAPTER_FAIL + why, (case, err)
            chk = AC.adapter_selfcheck("", force=True)
            assert chk["ok"] is False and chk["problems"] == [why], (case, chk)
            reasons.append(why)
        assert reasons[0] != reasons[1], reasons
        assert "venv python" in reasons[0] and "адаптер:" not in reasons[0], reasons[0]
        assert "адаптер:" in reasons[1] and "venv python" not in reasons[1], reasons[1]
    finally:
        AC.VENV, AC._adapter_file = saved[0], saved[1]
        AC._HUB.clear()
        AC._HUB.update(saved[2])
        restore()


@test
def test_run_summary_and_alarm_keep_the_whole_adapter_gap(tmp: Path):
    """Строка итога не режет длинную причину (путь и лекарство нужны целиком), а тревога панели знает,
    чем лечить: файл адаптера — «Версия», venv — «Установка»."""
    AC = _mod("agent_core")
    RS = _mod("run_summary")
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    restore = set_home(tmp / "home")
    saved = (AC._adapter_file,)
    try:
        AC._adapter_file = lambda: tmp / "p" / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py"
        gap = AC._adapter_gap()
        assert len(gap) > 120, "заготовка не проверяет усечение"
        line = RS.from_agent([], {"bypass": 2, "bypass_why": gap}, 1.0, None)["errors"]
        shown = next(k for k in line if k.startswith("⛔ мимо Pydantic AI"))
        assert "…" not in shown and shown.endswith("«Подготовить проект»"), shown
        AC._write_alarm({"agent:a": {"at": "2026-10-17T10:00:00Z", "why": gap, "project": "P", "calls": 1},
                         "agent:b": {"at": "2026-10-17T10:00:00Z", "why": AC._venv_gap(), "project": "P", "calls": 1},
                         "agent:c": {"at": "2026-10-17T10:00:00Z", "why": "процесс падал 3 раза", "project": "P", "calls": 1}})
        fixes = {r["path"]: r["fix"] for r in ck.adapter_alarm()}
        assert fixes == {"agent:a": "adapter", "agent:b": "venv", "agent:c": ""}, fixes
    finally:
        AC._adapter_file = saved[0]
        AC._write_alarm({})
        restore()


# ------------------------------------------------------------------ движок проекта: состояние и подготовка

def _clone(tmp: Path, name: str = "clone", stamp: str = "1.0.0") -> Path:
    """Проект «как после клонирования»: база знаний и конфиг есть, `.aurora/` нет."""
    root = tmp / name
    (root / "AuroraKnowledgeDB" / "meta").mkdir(parents=True)
    (root / "AuroraKnowledgeDB" / "meta" / "aurora_version.txt").write_text(stamp + "\n", encoding="utf-8")
    (root / "aurora.config.yaml").write_text('project:\n  name: "PRJ-A"\n  slug: "prj-a"\n', encoding="utf-8")
    (root / "AGENTS.md").write_text("# Свои правила проекта\n", encoding="utf-8")
    return root


def _outside_engine(root: Path) -> dict:
    """Всё, что лежит вне `.aurora/`: путь → содержимое. Подготовка движка не вправе ничего здесь менять."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".aurora" not in p.relative_to(root).parts and ".git" not in p.relative_to(root).parts}


@test
def test_engine_targets_match_what_the_update_plan_lays_under_dot_aurora(tmp: Path):
    """Список путей движка (его читает состояние) совпадает с тем, что кладёт полное обновление под `.aurora/`:
    разбор манифеста не расходится в двух местах."""
    AU = _mod("aurora_update")
    root = _clone(tmp)
    planned = {c.path for c in AU.plan(root, AU.project_fields(root))
               if c.kind == "write" and c.path.startswith(".aurora/")}
    targets = AU.engine_targets()
    assert planned == set(targets) and len(targets) == len(set(targets)), planned ^ set(targets)
    for must in (".aurora/scripts/agents/pydantic_ai_adapter.py", ".aurora/scripts/aurora_common.py",
                 ".aurora/skills/aurora-vault/SKILL.md", ".aurora/commands.txt"):
        assert must in targets, must


@test
def test_engine_state_names_missing_incomplete_stale_and_ok(tmp: Path):
    """Состояния движка проекта: нет вовсе, неполный (с точным перечнем), старше кита, в порядке, прежняя
    папка; пути из `aurora.update_ignore.txt` в «недостающее» не попадают."""
    AU = _mod("aurora_update")
    root = _clone(tmp)
    kit = AU.kit_version()
    st = AU.engine_state(root)
    assert st["state"] == "missing" and st["kit"] == kit and st["copy"] == "1.0.0", st
    assert st["total"] == len(st["missing"]) and st["total"] > 50, st["total"]
    assert ".aurora/scripts/agents/pydantic_ai_adapter.py" in st["missing"]
    # достроили целиком — в порядке (метки копии нет, а штамп проекта равен киту)
    (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text(kit + "\n", encoding="utf-8")
    assert AU.engine_prepare(root)["after"] == "ok"
    st = AU.engine_state(root)
    assert st["state"] == "ok" and st["missing"] == [], st
    # не хватает двух файлов — «неполный», и они названы
    gone = [".aurora/docs/card-templates.md", ".aurora/scripts/agents/pydantic_ai_adapter.py"]     # порядок манифеста
    for rel in gone:
        (root / rel).unlink()
    st = AU.engine_state(root)
    assert st["state"] == "incomplete" and st["missing"] == gone, st
    # сломанный указатель на кит — тоже недостающее: копии скриптов берут от него манифест
    AU.engine_prepare(root)
    (root / ".aurora/kit_path.txt").write_text(str(tmp / "no-such-kit") + "\n", encoding="utf-8")
    assert AU.engine_state(root)["missing"] == [".aurora/kit_path.txt"]
    AU.engine_prepare(root)
    # метка копии ниже кита — старше (а не «в порядке»)
    (root / ".aurora/engine_version.txt").write_text("0.9.0\n", encoding="utf-8")
    st = AU.engine_state(root)
    assert st["state"] == "stale" and st["copy"] == "0.9.0" and not st["missing"], st
    assert AU.version_key("1.174.0") < AU.version_key("1.200.1") and AU.version_key("1.9.0") < AU.version_key("1.10.0")
    # путь, который проект ведёт сам, ни недостающим, ни целью подготовки не считается
    (root / ".aurora/scripts/agents/graphify_adapter.py").unlink()
    (root / "aurora.update_ignore.txt").write_text("# свой\n.aurora/scripts/agents/*\n", encoding="utf-8")
    assert AU.engine_state(root)["missing"] == []
    assert ".aurora/scripts/agents/graphify_adapter.py" not in AU.engine_prepare(root)["created"]
    assert not (root / ".aurora/scripts/agents/graphify_adapter.py").exists()
    # движок ещё в `.opencode/` — «старше», переезд полным обновлением, не подготовкой
    old = _clone(tmp, "old-layout")
    (old / ".opencode/scripts").mkdir(parents=True)
    st = AU.engine_state(old)
    assert st["state"] == "stale" and st["legacy"] is True, st
    res = AU.engine_prepare(old)
    assert res["ok"] is False and res["reason"] == "legacy" and not (old / ".aurora").exists(), res


@test
def test_engine_prepare_touches_only_dot_aurora_and_is_idempotent(tmp: Path):
    """Подготовка движка пишет только под `.aurora/`: ни один файл проекта и ни одна запись git не меняются;
    повтор ничего не пишет; правка проекта в файле движка заменяется, чужой файл рядом — нет; движок новее
    кита только достраивается; предпросмотр не пишет вовсе."""
    AU = _mod("aurora_update")
    root = _clone(tmp)
    (root / ".gitignore").write_text(".aurora/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=u@example.com", "-c", "user.name=User", "commit", "-qm", "base"],
                   cwd=str(root), check=True)
    before = _outside_engine(root)
    # предпросмотр: список есть, записи нет
    dry = AU.engine_prepare(root, apply=False)
    assert dry["ok"] and dry["applied"] is False and len(dry["created"]) > 50 and not (root / ".aurora").exists()
    # подготовка
    res = AU.engine_prepare(root)
    assert res["ok"] and res["before"] == "missing" and res["after"] == "ok" and res["failed"] == [], res
    assert res["created"] == dry["created"] and res["refreshed"] == [] and res["ignored"] is True, res
    adapter = root / ".aurora/scripts/agents/pydantic_ai_adapter.py"
    assert adapter.read_bytes() == (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(
        encoding="utf-8").encode("utf-8")
    assert (root / ".aurora/kit_path.txt").read_text(encoding="utf-8").strip() == str(AU.KIT)
    assert (root / ".aurora/engine_version.txt").read_text(encoding="utf-8").strip() == AU.kit_version()
    assert _outside_engine(root) == before, "подготовка движка изменила файлы проекта"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True,
                            encoding="utf-8", errors="replace").stdout
    assert status.strip() == "", f"git видит изменения после подготовки: {status}"
    # повтор: нечего делать
    again = AU.engine_prepare(root)
    assert again["created"] == [] and again["refreshed"] == [] and again["removed"] == [], again
    # своя правка в файле движка заменяется; свой файл рядом остаётся
    own = root / ".aurora/scripts/my_own_tool.py"
    own.write_text("# файл проекта, не движка\n", encoding="utf-8")
    common = root / ".aurora/scripts/aurora_common.py"
    common.write_text("# правка\n", encoding="utf-8")
    fixed = AU.engine_prepare(root)
    assert fixed["refreshed"] == [".aurora/scripts/aurora_common.py"] and fixed["created"] == [], fixed
    assert common.read_text(encoding="utf-8") != "# правка\n"
    assert own.read_text(encoding="utf-8") == "# файл проекта, не движка\n"
    # движок новее кита: недостающее достраивается, существующее не откатывается
    (root / ".aurora/engine_version.txt").write_text("99.0.0\n", encoding="utf-8")
    common.write_text("# от более нового кита\n", encoding="utf-8")
    adapter.unlink()
    ahead = AU.engine_prepare(root)
    assert ahead["ahead"] is True and ahead["created"] == [".aurora/scripts/agents/pydantic_ai_adapter.py"], ahead
    assert common.read_text(encoding="utf-8") == "# от более нового кита\n"
    assert (root / ".aurora/engine_version.txt").read_text(encoding="utf-8").strip() == "99.0.0"
    # правило .gitignore проекта не добавляется само, но об его отсутствии сказано
    bare = _clone(tmp, "no-ignore-rule")
    subprocess.run(["git", "init", "-q"], cwd=str(bare), check=True)
    assert AU.engine_prepare(bare)["ignored"] is False
    assert not (bare / ".gitignore").exists(), "подготовка движка завела правило в файле проекта"


@test
def test_engine_prepare_does_not_write_through_links_or_outside_the_project(tmp: Path):
    """Ссылка на общий каталог движка не заменяется и не пишется насквозь: файл остаётся как был,
    подготовка называет его «вне проекта» и достраивает остальное."""
    AU = _mod("aurora_update")
    root = _clone(tmp)
    shared = tmp / "shared-engine"
    shared.mkdir()
    (root / ".aurora/scripts").mkdir(parents=True)
    victim = shared / "aurora_common.py"
    victim.write_text("# общий файл\n", encoding="utf-8")
    try:
        os.symlink(victim, root / ".aurora/scripts/aurora_common.py")
        os.symlink(shared, root / ".aurora/docs", target_is_directory=True)
    except (OSError, NotImplementedError):
        return              # Windows без права на ссылки: проверять нечем
    res = AU.engine_prepare(root)
    assert victim.read_text(encoding="utf-8") == "# общий файл\n", "запись ушла за ссылку"
    assert sorted(p.name for p in shared.iterdir()) == ["aurora_common.py"], "в общий каталог легли чужие файлы"
    assert ".aurora/scripts/aurora_common.py" in res["shared"], res["shared"]
    assert any(p.startswith(".aurora/docs/") for p in res["shared"]), res["shared"]
    assert (root / ".aurora/scripts/agents/pydantic_ai_adapter.py").is_file(), "остальное не достроено"


@test
def test_full_update_keeps_the_engine_copy_mark_in_step_with_the_stamp(tmp: Path):
    """Полное обновление ставит и штамп проекта, и метку копии движка: иначе копия, подготовленная раньше,
    выглядела бы отставшей от кита после обновления до следующей версии."""
    AU = _mod("aurora_update")
    restore = set_home(tmp / "home")
    try:
        root = _clone(tmp)
        AU.engine_prepare(root)
        (root / ".aurora/engine_version.txt").write_text("0.5.0\n", encoding="utf-8")
        assert AU.engine_state(root)["state"] == "stale"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert AU.run(root, True) == 0
        kit = AU.kit_version()
        assert (root / ".aurora/engine_version.txt").read_text(encoding="utf-8").strip() == kit
        assert (root / "AuroraKnowledgeDB/meta/aurora_version.txt").read_text(encoding="utf-8").strip() == kit
        assert AU.engine_state(root)["state"] == "ok"
    finally:
        restore()


@test
def test_engine_only_flag_is_a_command_mode_with_preview_and_apply(tmp: Path):
    """`kit:update --engine-only`: без `--apply` показывает и не пишет, с ним строит движок и говорит, что создано;
    с `--structure-only` вместе не принимается; переезд из `.opencode/` отказом называет полное обновление."""
    root = _clone(tmp)

    def run(target, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "aurora_update.py"), str(target), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    dry = run(root, "--engine-only")
    assert dry.returncode == 0 and "Создать:" in dry.stdout and "(dry-run)" in dry.stdout, dry.stdout + dry.stderr
    assert not (root / ".aurora").exists()
    done = run(root, "--engine-only", "--apply")
    assert done.returncode == 0 and "Движок проекта подготовлен на этом компьютере: создано" in done.stdout, done.stdout
    assert "Файлы проекта и git не тронуты" in done.stdout and (root / ".aurora/scripts/agent_core.py").is_file()
    assert "уже актуален" in run(root, "--engine-only").stdout
    assert run(root, "--engine-only", "--structure-only").returncode == 2
    legacy = _clone(tmp, "legacy")
    (legacy / ".opencode/scripts").mkdir(parents=True)
    cp = run(legacy, "--engine-only", "--apply")
    assert cp.returncode == 1 and "Предпросмотр обновления" in cp.stderr, cp.stderr
    assert not (legacy / ".aurora").exists()
    assert "--engine-only" in subprocess.run([sys.executable, str(SCRIPTS / "aurora_update.py"), "--help"],
                                             capture_output=True, text=True, encoding="utf-8",
                                             errors="replace").stdout


@test
def test_doctor_lists_the_exact_missing_engine_files(tmp: Path):
    """`kit:doctor` называет состояние движка проекта и перечисляет недостающие файлы поимённо."""
    AU = _mod("aurora_update")
    root = _clone(tmp)

    def doctor() -> str:
        return subprocess.run([sys.executable, str(SCRIPTS / "aurora_doctor.py")], cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8", errors="replace"
                              ).stdout.replace("\r\n", "\n")

    out = doctor()
    assert "WARN: движка проекта нет на этом компьютере" in out and "«Подготовить проект»" in out, out
    AU.engine_prepare(root)
    (root / ".aurora/scripts/agents/pydantic_ai_adapter.py").unlink()
    (root / ".aurora/docs/card-templates.md").unlink()
    out = doctor()
    assert "WARN: движок проекта неполный: нет 2 из " in out, out
    assert ".aurora/docs/card-templates.md, .aurora/scripts/agents/pydantic_ai_adapter.py" in out, out
    assert ("недостающие файлы движка (2):\n  .aurora/docs/card-templates.md\n"
            "  .aurora/scripts/agents/pydantic_ai_adapter.py\n") in out, out
    AU.engine_prepare(root)
    (root / ".aurora/engine_version.txt").write_text("0.9.0\n", encoding="utf-8")
    assert f"WARN: движок проекта 0.9.0 старше кита {AU.kit_version()}" in doctor()
    AU.engine_prepare(root)
    out = doctor()
    assert "движка проекта нет" not in out and "движок проекта неполный" not in out, out
    assert "недостающие файлы движка" not in out and "старше кита" not in out, out


@test
def test_panel_reports_engine_state_prepares_on_request_and_notes_the_kit_fallback(tmp: Path):
    """Панель: карточка проекта несёт состояние движка; `engine_prepare_action` достраивает его и оставляет
    строку в истории «Консоли»; во время запуска отказывает; скрипт, взятый из кита вместо движка проекта,
    называется в консоли и в журнале прогона — а что исполняется, не меняется."""
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    restore_home = set_home(tmp / "home")
    root = _clone(tmp)
    project = str(root)
    try:
        card = ck.project_card(project)["engine_state"]
        assert card["state"] == "missing" and card["missing"] == card["total"] > 50 and card["legacy"] is False, card
        assert card["files"] and len(card["files"]) <= 8
        # записка про откат на кит: движка нет — скрипт из кита, строка с состоянием и подсказкой
        kit_path = os.path.join(ck.KIT, "scripts", "kb_lint.py")
        note = ck.engine_fallback_note(project, "kb_lint.py", kit_path)
        assert "missing" in note and "kb_lint.py" in note and "«Версия» → «Подготовить проект»" in note, note
        assert ck.engine_fallback_note(project, "aurora_update.py", kit_path) == "", "скрипты кита по замыслу — без записки"
        assert ck.engine_fallback_note(project, "kb_lint.py", str(root / ".aurora/scripts/kb_lint.py")) == ""
        # отказ во время запуска в проекте
        ck.JOBS["fake-run"] = {"id": "fake-run", "cmd": "sync:confluence", "project": project, "done": False}
        try:
            busy = ck.engine_prepare_action(project)
        finally:
            ck.JOBS.pop("fake-run", None)
        assert "sync:confluence" in busy.get("error", "") and not (root / ".aurora/scripts").exists(), busy
        # подготовка
        res = ck.engine_prepare_action(project)
        assert res["ok"] and res["before"] == "missing" and res["after"] == "ok", res
        assert res["created"] > 50 and res["state"]["state"] == "ok" and res["state"]["missing"] == 0, res
        assert res["files"] and all(f.startswith(".aurora/") for f in res["files"]), res["files"]
        runs = [p for p in Path(ck.runs_dir(project)).iterdir() if p.is_dir()]
        assert len(runs) == 1, runs
        meta = json.loads((runs[0] / "meta.json").read_text(encoding="utf-8"))
        assert meta["title"] == "kit:update --engine-only --apply" and meta["status"] == "done" and meta["rc"] == 0, meta
        log = (runs[0] / "console.log").read_text(encoding="utf-8")
        assert "Движок проекта подготовлен на этом компьютере" in log, log
        assert ck.engine_fallback_note(project, "kb_lint.py", kit_path) == "", "после подготовки записка не нужна"
        # запуск команды: что исполняется — как раньше, записка — только пока движка нет
        shutil.rmtree(root / ".aurora/scripts")
        (root / ".aurora/engine_version.txt").unlink()
        restore_registry = isolated_registry(ck, tmp)
        real_popen, real_running = ck.subprocess.Popen, ck.RUNNING
        launched = []

        class FakeProc:
            def __init__(self, argv, **kw):
                if argv[0] == sys.executable:         # запуск команды, а не вызов git из истории
                    launched.append(argv[1])
                self.stdout = iter(["вывод команды\n"])
                self.returncode = 0

            def wait(self):
                return 0

        ck.subprocess.Popen = FakeProc
        ck.RUNNING = str(tmp / "running.json")
        try:
            jid = ck.start_job(project, "kb:lint", [])
            job = ck.JOBS[jid]
            for _ in range(200):
                if job["done"]:
                    break
                time.sleep(0.05)
        finally:
            ck.subprocess.Popen, ck.RUNNING = real_popen, real_running
            restore_registry()
        assert job["done"] and job["rc"] == 0, job
        assert launched == [os.path.join(ck.KIT, "scripts", "kb_lint.py")], f"исполняется не то: {launched}"
        assert job["out"][0].startswith("⚠️ движок проекта: incomplete") and job["out"][-1] == "вывод команды", job["out"]
        saved = (Path(ck.runs_dir(project)) / job["run_id"] / "console.log").read_text(encoding="utf-8")
        assert "«Версия» → «Подготовить проект»" in saved and "вывод команды" in saved, saved
    finally:
        restore_home()


# ------------------------------------------------------------------ панель: полоса «движок проекта»

ENGINE_BAR_HARNESS = r"""
const calls = [], toasts = [], shown = [], refreshed = [];
let cur = null, seq = 0;
const t = (k, v) => k + (v ? JSON.stringify(v) : "");
const toast = (text, kind) => toasts.push({text, kind});
const show = (v) => shown.push(v);
const refreshModules = async () => { refreshed.push(1); };
const el = (tag, attrs, ...kids) => {
  const n = {tag, attrs: attrs || {}, kids: kids.flat().filter(k => k !== null && k !== false && k !== undefined), id: ++seq};
  n.remove = () => { if (cur === n) cur = null; };
  return n;
};
const main = {prepend(n){ cur = n; }};
const $ = (sel) => sel === "#enginebar" ? cur : sel === "main" ? main : null;
const S = {project: null};
let REPLY = {};
const api = async (path, opts) => { calls.push({path, opts}); return REPLY[path] || {}; };
@BAR@
const text = (n) => !n ? "" : typeof n === "string" ? n : (n.kids || []).map(text).join(" ");
const buttons = (n) => !n || typeof n === "string" ? [] : [].concat(n.tag === "button" ? [n] : [], ...(n.kids || []).map(buttons));
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
const card = (state, extra) => Object.assign({path: "/p/" + state, kit: "1.174.0", engine_state: Object.assign(
  {state, missing: 3, total: 103, files: [".aurora/a", ".aurora/b"], copy: "1.100.0", legacy: false}, extra || {})});
(async () => {
  // нет движка: сервер просят достроить сам, до здоровья; результат — строка и всплывающее сообщение
  let p = card("missing");
  S.project = p;
  REPLY["/api/engine/prepare"] = {ok: true, created: 103, refreshed: 0, files: [".aurora/x"], ignored: true,
                                  state: {state: "ok", missing: 0, total: 103, files: [], legacy: false}};
  await ensureEngine(p);
  check(calls.length === 1 && calls[0].opts.method === "POST" && JSON.parse(calls[0].opts.body).project === p.path,
        "нет движка — один запрос на подготовку именно этого проекта");
  check(p.engine_state.state === "ok", "состояние проекта обновлено ответом");
  check(toasts.length === 1 && toasts[0].text.startsWith("engine.prepared"), "сказано, что подготовлено");
  check(cur && text(cur).includes("engine.prepared") && text(cur).includes(".aurora/x"), "в полосе — что создано");
  check(!text(cur).includes("engine.not_ignored"), "правило git на месте — предупреждения нет");
  check(refreshed.length >= 1, "разделы перерисованы");
  // повторный выбор того же проекта, пока движок снова не в порядке, не зовёт сервер второй раз
  p.engine_state = {state: "incomplete", missing: 3, total: 103, files: [".aurora/a"], legacy: false};
  await ensureEngine(p);
  check(calls.length === 1, "автоподготовка — один раз за вкладку на проект");
  check(buttons(cur).some(b => text(b) === "engine.prepare"), "дальше — кнопка «Подготовить проект»");
  // неполный: тоже сам
  p = card("incomplete"); S.project = p; calls.length = 0;
  await ensureEngine(p);
  check(calls.length === 1, "неполный движок достраивается сам");
  // в порядке: ни запроса, ни полосы
  p = card("ok", {missing: 0}); S.project = p; calls.length = 0; cur = el("div");
  await ensureEngine(p);
  check(calls.length === 0 && cur === null, "движок в порядке — тихо, прежняя полоса снята");
  // старше кита: сам не обновляется — кнопка «Обновить» одним нажатием
  p = card("stale"); S.project = p; calls.length = 0;
  await ensureEngine(p);
  check(calls.length === 0, "отставший движок без нажатия не трогается");
  const upd = buttons(cur).find(b => text(b) === "engine.update");
  check(!!upd && text(cur).includes("engine.bar_stale"), "полоса и кнопка «Обновить»");
  REPLY["/api/engine/prepare"] = {ok: true, created: 0, refreshed: 90, files: [], ignored: false,
                                  state: {state: "ok", missing: 0, total: 103, files: [], legacy: false}};
  await upd.attrs.onclick();
  check(calls.length === 1 && p.engine_state.state === "ok", "кнопка зовёт то же действие");
  check(text(cur).includes("engine.not_ignored"), "нет правила .gitignore — сказано");
  // прежняя папка движка: переезд — полным обновлением, подготовка не зовётся
  p = card("stale", {legacy: true}); S.project = p; calls.length = 0;
  await ensureEngine(p);
  check(calls.length === 0 && buttons(cur).some(b => text(b) === "engine.open_version"), "прежняя папка — путь в «Версию»");
  buttons(cur)[0].attrs.onclick();
  check(shown.includes("version"), "кнопка ведёт в «Версию»");
  p = card("missing", {legacy: true}); S.project = p; calls.length = 0; ENGINE_TRIED.clear();
  await ensureEngine(p);
  check(calls.length === 0, "прежняя папка движка не достраивается, даже если состояние назвали иначе");
  // отказ сервера: причина в полосе, кнопка повторить
  p = card("missing"); S.project = p; calls.length = 0; ENGINE_TRIED.clear();
  REPLY["/api/engine/prepare"] = {error: "в проекте идёт запуск (kb:lint)", state: p.engine_state};
  await ensureEngine(p);
  check(text(cur).includes("engine.failed") && text(cur).includes("kb:lint"), "причина отказа видна");
  check(buttons(cur).some(b => text(b) === "engine.prepare") && !p._engineBusy, "кнопка повторить, занятость снята");
  // чужой проект на экране: полоса не рисуется
  const other = card("missing"); S.project = card("ok");
  cur = null; drawEngineBar(other);
  check(cur === null, "полоса только у выбранного проекта");
  console.log(JSON.stringify({fails}));
})().catch(e => { console.log(JSON.stringify({fails: ["исключение: " + (e && e.stack || e)]})); });
"""


@test
def test_panel_prepares_a_project_engine_on_pick_and_offers_a_button(tmp: Path):
    """Панель: при выборе проекта без движка или с неполным движок достраивается сам (один раз за вкладку),
    отставший обновляется кнопкой, прежняя папка ведёт в «Версию», отказ виден с причиной и повтором.
    Под node; без него — только текст ядра и обе строки каталога."""
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    pick = panel[panel.index("async function pick(p, go=true){"):panel.index("// Отметка у пункта меню.")]
    assert "await ensureEngine(p);" in pick and pick.index("ensureEngine") < pick.index("loadHealth(p, HEALTH_ALL)"), \
        "движок готовится до здоровья проекта"
    assert "prepareEngine: () => prepareEngine(S.project)" in panel, "кнопка раздела «Версия» зовёт то же действие"
    for lang in ("ru", "en"):
        core = json.loads((KIT / "cockpit/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        ver = json.loads((KIT / "cockpit/modules/version/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("busy", "bar_missing", "bar_incomplete", "bar_stale", "bar_legacy", "prepare", "update",
                    "open_version", "dismiss", "prepared", "prepared_files", "not_ignored", "failed"):
            assert core.get("engine." + key), (lang, key)
        for key in ("title", "about", "ok", "missing", "incomplete", "stale", "legacy", "prepare", "update", "done"):
            assert ver.get("version.engine_" + key), (lang, key)
    if not shutil.which("node"):
        return
    a = panel.index("const ENGINE_TRIED = new Set();")
    bar = panel[a:panel.index("const ALARM_POLL_MS", a)]
    res = _run_node(tmp, "engine_bar.mjs", ENGINE_BAR_HARNESS.replace("@BAR@", bar))
    assert res["fails"] == [], "\n".join(res["fails"])


def _run_node(tmp: Path, name: str, code: str, *argv: str) -> dict:
    src = tmp / name
    src.write_text(code, encoding="utf-8")
    cp = subprocess.run([shutil.which("node"), str(src), *argv], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    return json.loads(cp.stdout.strip().splitlines()[-1])
