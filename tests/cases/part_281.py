#!/usr/bin/env python3
"""part_281 — регрессии третьего ревью 0003: граница проекта и независимые M/minor.

Все заготовки — одноразовые `git init` во временной папке прогона. Пользовательские
проекты и их индексы не читаются и не меняются.

Что проверяем:
  (a) B2: коммит панели, чекпойнт и commit_result вложенного проекта отказывают ДО
      мутации: HEAD, индекс и staged-сосед родителя не меняются, журнал demo не коммитится;
  (b) B2+: запуск из подпапки своего проекта (не вложенного) коммитит нормально;
  (c) M3: «git не ответил» — не «нечего снимать»; обновление возвращает 7, а не 0;
  (d) M4: успех .gitignore выводится из перечитывания, а не из пустого списка;
  (e) M5: doctor печатает путь один раз со всеми доказанными причинами;
  (f) minor: корень проекта находится из подпапки, загрузчик реестра не обещает fallback.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from harness import make_project, run, test

JOURNAL = "AuroraKnowledgeDB/meta/run_log.md"


def _lp():
    from harness import SCRIPTS
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import local_paths
    return local_paths


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=env)
    assert cp.returncode == 0, f"git {' '.join(args)}: rc={cp.returncode}\n{cp.stdout}\n{cp.stderr}"
    return cp


def _init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _index_state(repo: Path) -> tuple:
    """Логическое состояние индекса: записи, флаги и staged-содержимое.

    Байты сравнивать нельзя: `git status` обновляет stat-поля, и это не мутация stage.
    """
    return (_git(repo, "ls-files", "-s", "-z").stdout,
            _git(repo, "ls-files", "-v", "-z").stdout,
            _git(repo, "diff", "--cached", "--name-status", "-z").stdout)


def _names(repo: Path, rev: str = "HEAD") -> list:
    return _git(repo, "show", "--name-only", "--format=", rev).stdout.split()


def _block(repo: Path) -> None:
    (repo / ".gitignore").write_text(_lp().gitignore_block(), encoding="utf-8")


def _nested(tmp: Path, name: str, *, card: bool = False,
            parent_card: bool = False) -> tuple[Path, Path]:
    """Родитель с Code/product и журналом; вложенный demo со своим журналом и карточкой."""
    parent = _init(tmp / name)
    _block(parent)
    (parent / "Code/product").mkdir(parents=True)
    (parent / "Code/product/impl.py").write_text("print(1)\n", encoding="utf-8")
    parent_log = parent / JOURNAL
    parent_log.parent.mkdir(parents=True, exist_ok=True)
    parent_log.write_text("parent-local\n", encoding="utf-8")
    if parent_card:
        (parent / "AuroraKnowledgeDB/Concepts").mkdir(parents=True)
        (parent / "AuroraKnowledgeDB/Concepts/p.md").write_text("parent card\n", encoding="utf-8")
    demo = parent / "projects/demo"
    demo.mkdir(parents=True)
    _block(demo)
    (demo / JOURNAL).parent.mkdir(parents=True)
    (demo / JOURNAL).write_text("demo-local\n", encoding="utf-8")
    (demo / "Cards").mkdir()
    (demo / "Cards/a.md").write_text("card\n", encoding="utf-8")
    if card:
        (demo / "AuroraKnowledgeDB/Concepts").mkdir(parents=True, exist_ok=True)
        (demo / "AuroraKnowledgeDB/Concepts/d.md").write_text(
            "---\ntype: concept\n---\n# D\n", encoding="utf-8")
    sibling = parent / "projects/sibling/notes.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("old\n", encoding="utf-8")
    _commit(parent, "init")
    sibling.write_text("staged sibling\n", encoding="utf-8")
    _git(parent, "add", "--", "projects/sibling/notes.md")
    (demo / JOURNAL).write_text("demo dirty\n", encoding="utf-8")
    (demo / "Cards/a.md").write_text("card dirty\n", encoding="utf-8")
    (parent / "Code/product/impl.py").write_text("print(2)\n", encoding="utf-8")
    if parent_card:
        (parent / "AuroraKnowledgeDB/Concepts/p.md").write_text("parent dirty\n", encoding="utf-8")
    return parent, demo


def _assert_parent_untouched(parent: Path, before_head: str, before_index, before_staged: list):
    assert _git(parent, "rev-parse", "HEAD").stdout.strip() == before_head, "HEAD родителя сдвинут"
    assert _index_state(parent) == before_index, "содержимое/флаги индекса родителя изменены"
    staged = _git(parent, "diff", "--cached", "--name-only").stdout.split()
    assert staged == before_staged, f"staged-сосед родителя изменён: {staged}"
    assert _git(parent, "ls-files", "--", "Code/product/impl.py").stdout.strip(), \
        "Code/product снят с индекса родителя"
    assert _git(parent, "ls-files", "--", JOURNAL).stdout.strip(), \
        "журнал родителя снят с индекса"


@test
def test_git_publish_rejects_a_nested_project_before_lock_or_transport(tmp: Path):
    import git_sync as gs

    parent, demo = _nested(tmp, "network")
    _git(parent, "branch", "-M", "main")
    _git(parent, "remote", "add", "origin", str(tmp / "unused.git"))
    base = _git(parent, "rev-parse", "HEAD").stdout.strip()
    head = _commit(parent, "parent has an unpublished commit")
    _git(parent, "update-ref", "refs/remotes/origin/main", base)
    assert not gs.has_changes(demo) and gs.count(demo, "origin/main..HEAD") == 1
    before_index = (parent / ".git/index").read_bytes()
    attempts = (
        ("push", lambda: gs.push(demo, trigger="after_commit")),
        ("push without commit", lambda: gs.push(demo, do_commit=False)),
        ("push update-first", lambda: gs.push(demo, update_first=True)),
        ("update", lambda: gs.update(demo)),
        ("set-upstream", lambda: gs.fix(demo, "set-upstream")),
    )
    success = {"remote": "origin", "ok": True, "pushed": 1, "summary": "mock transport"}
    with (patch.object(gs, "OpLock", wraps=gs.OpLock) as lock,
          patch.object(gs, "_fetch", return_value=(0, "")) as fetch,
          patch.object(gs, "_push_one", return_value=success) as push,
          patch.object(gs, "_ensure_upstream") as upstream):
        outcomes = {name: (call().get("problem") or {}).get("code") for name, call in attempts}
        assert set(outcomes.values()) == {"local_paths_scope"}, (
            outcomes, lock.call_count, fetch.call_count, push.call_count)
        assert not (lock.called or fetch.called or push.called or upstream.called)
        assert not (parent / ".git/aurora").exists(), "refusal wrote settings, lock or log to the parent"
        assert (parent / ".git/index").read_bytes() == before_index
        assert _git(parent, "rev-parse", "HEAD", "origin/main").stdout.splitlines() == [head, base]

        # An ordinary directory inside the same project uses its own Git root.
        subdir = parent / "projects/sibling"
        assert gs.push(subdir, do_commit=False, update_first=True)["ok"]
        assert fetch.call_args.args[0] == push.call_args.args[0] == str(parent)


@test
def test_git_automation_never_writes_the_parent_repo_of_a_nested_project(tmp: Path):
    import git_auto
    import git_sync as gs

    parent, demo = _nested(tmp, "automation")
    settings, _ = gs.normalize({
        "auto_update": {"enabled": True, "on": ["open", "interval"]},
        "auto_push": {"enabled": True, "on": ["after_commit"]},
    })
    settings_path = gs.settings_file(parent)
    gs._write_json(settings_path, settings)
    before = {p.name: p.read_bytes() for p in settings_path.parent.iterdir()}
    cockpit = Mock(JOBS_LOCK=None, JOBS={})
    cockpit.project_activity.side_effect = [{"running": True}, {"running": False}]
    cockpit.start_job.return_value = "fake-job"
    automation = git_auto.GitAuto(cockpit, lambda: [str(demo)])
    with patch.object(gs, "record", wraps=gs.record) as record:
        opened = automation.event(str(demo), "open")
        fired = automation.tick()
        after = {p.name: p.read_bytes() for p in settings_path.parent.iterdir()}
    assert (not opened["fired"] and not fired and not record.called
            and not cockpit.start_job.called and after == before), (opened, fired, sorted(after), record.call_count)

    out = io.StringIO()
    with patch.object(gs, "_push_one") as push, redirect_stdout(out):
        rc = gs.main(["--push", "--auto=after_commit", "--project", str(demo), "--json"])
    assert rc == 1 and not push.called
    assert json.loads(out.getvalue())["problem"]["code"] == "local_paths_scope"
    assert {p.name: p.read_bytes() for p in settings_path.parent.iterdir()} == before


@test
def test_nested_project_writers_refuse_before_mutation(tmp: Path):
    """(a) B2: три писателя вложенного проекта отказывают, не трогая родителя."""
    import agent_runner as AR
    import git_sync as GS

    parent, demo = _nested(tmp, "panel")
    head, before_index = _git(parent, "rev-parse", "HEAD").stdout.strip(), _index_state(parent)
    staged = _git(parent, "diff", "--cached", "--name-only").stdout.split()
    res = GS.commit(str(demo))
    assert res["ok"] is False, f"панель закоммитила вложенный проект: {res}"
    assert "вложен" in str(res.get("problem") or {}), res
    _assert_parent_untouched(parent, head, before_index, staged)
    assert _git(parent, "show", f"HEAD:projects/demo/{JOURNAL}").stdout == "demo-local\n", \
        "содержимое журнала demo в HEAD изменилось"

    parent, demo = _nested(tmp, "checkpoint")
    head, before_index = _git(parent, "rev-parse", "HEAD").stdout.strip(), _index_state(parent)
    staged = _git(parent, "diff", "--cached", "--name-only").stdout.split()
    cp = AR.checkpoint(str(demo), "задача", True)
    assert cp["ok"] is False and "вложен" in cp["why"], cp
    _assert_parent_untouched(parent, head, before_index, staged)
    assert _git(parent, "show", f"HEAD:projects/demo/{JOURNAL}").stdout == "demo-local\n", \
        "чекпойнт записал журнал demo"

    parent, demo = _nested(tmp, "result", card=True, parent_card=True)
    head, before_index = _git(parent, "rev-parse", "HEAD").stdout.strip(), _index_state(parent)
    staged = _git(parent, "diff", "--cached", "--name-only").stdout.split()
    done = AR.commit_result(str(demo), "agent:distill", "заход", True)
    assert done["ok"] is False and "вложен" in done["why"], done
    _assert_parent_untouched(parent, head, before_index, staged)
    assert _git(parent, "show", "HEAD:AuroraKnowledgeDB/Concepts/p.md").stdout == "parent card\n", \
        "commit_result закоммитил базу РОДИТЕЛЯ"
    assert _git(parent, "show", "HEAD:projects/demo/AuroraKnowledgeDB/Concepts/d.md").stdout == \
        "---\ntype: concept\n---\n# D\n", "commit_result закоммитил карточку demo"


@test
def test_writer_from_subdirectory_of_own_project_still_commits(tmp: Path):
    """(b) B2+: подпапка своего проекта — не вложенный проект; коммит работает."""
    import agent_runner as AR
    import git_sync as GS

    repo = _init(tmp / "own")
    _block(repo)
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
    _commit(repo, "init")
    # Локальный журнал появился после коммита: он закрыт .gitignore и в HEAD его нет.
    (repo / JOURNAL).parent.mkdir(parents=True, exist_ok=True)
    (repo / JOURNAL).write_text("log\n", encoding="utf-8")
    (repo / "Cards/a.md").write_text("new card\n", encoding="utf-8")
    res = GS.commit(str(repo / "Cards"))
    assert res["ok"], res
    names = _names(repo)
    assert "Cards/a.md" in names and JOURNAL not in names, names

    (repo / "Cards/a.md").write_text("checkpoint card\n", encoding="utf-8")
    cp = AR.checkpoint(str(repo / "AuroraKnowledgeDB"), "задача", True)
    assert cp["ok"] and cp["committed"] == 1, cp
    assert "Cards/a.md" in _names(repo) and JOURNAL not in _names(repo), _names(repo)

    (repo / "AuroraKnowledgeDB/Concepts").mkdir(parents=True, exist_ok=True)
    (repo / "AuroraKnowledgeDB/Concepts/c.md").write_text(
        "---\ntype: concept\n---\n# C\n", encoding="utf-8")
    (repo / "Cards/a.md").write_text("human while agent works\n", encoding="utf-8")
    done = AR.commit_result(str(repo / "AuroraKnowledgeDB"), "agent:distill", "заход", True)
    assert done["ok"], done
    names = _names(repo)
    assert "AuroraKnowledgeDB/Concepts/c.md" in names, names
    assert "Cards/a.md" not in names, f"commit_result присвоил работу человека: {names}"
    assert JOURNAL not in names, names


@test
def test_git_failure_is_not_an_empty_index(tmp: Path):
    """(c) M3: отказ определения git — не «снимать нечего»; обновление говорит кодом 7."""
    import contextlib
    import io
    from unittest.mock import patch

    import aurora_update as AU

    root = make_project(tmp)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _commit(root, "installed")

    with patch.object(AU, "_git_repo_state",
                      lambda _t: {"state": "unknown", "detail": "forced git failure"}):
        report = AU.clean_local_state(root)
        assert report["checked"] is False and "forced" in report["detail"], report
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            assert AU.run(root, True) == 7, "непроверенный индекс выдан за успех"
        text = out.getvalue()
        assert "не проверены" in text, text[-1500:]

    # Настоящего репозитория нет — это не отказ: снимать нечего.
    plain = tmp / "plain"
    plain.mkdir()
    assert AU.clean_local_state(plain)["checked"] is True
    assert AU._git_repo_state(plain)["state"] == "none"


@test
def test_gitignore_success_requires_a_verification_read(tmp: Path):
    """(d) M4: запись, которой нет при перечитывании, — отказ установки, не ok=True."""
    import install_aurora as IA
    import local_paths as lp

    from harness import SCRIPTS
    sys.path.insert(0, str(SCRIPTS))
    import aurora_fs

    target = _init(tmp / "install")
    real_read = lp._read_gitignore
    calls = {"n": 0}

    def read_once(_path):
        calls["n"] += 1
        # Первое чтение удалось, перечитывание после записи — нет.
        return "" if calls["n"] == 1 else None

    lp._read_gitignore = read_once
    try:
        report = lp.ensure_gitignore_report(target / ".gitignore")
    finally:
        lp._read_gitignore = real_read
    assert report["ok"] is False and report["detail"], report

    # Запись «прошла», но перечитывание не содержит правил: успех не выводится из вызова.
    target2 = _init(tmp / "install2")
    real_write = lp._write_rules
    lp._write_rules = lambda path, _text: path.write_text("# чужие правила\n", encoding="utf-8")
    try:
        report = lp.ensure_gitignore_report(target2 / ".gitignore")
    finally:
        lp._write_rules = real_write
    assert report["ok"] is False and "не записались" in report["detail"], report

    # Установщик обязан остановиться до служебных файлов.
    target3 = _init(tmp / "install3")
    real_write = lp._write_rules
    lp._write_rules = lambda path, _text: path.write_text("# чужие правила\n", encoding="utf-8")
    try:
        inst = IA.Installer(target=target3, name="Demo", slug=None, jira=None, confluence=None,
                            force=False, dry_run=False)
        rc = inst.run()
    finally:
        lp._write_rules = real_write
    assert rc != 0, "установка продолжилась без записанных правил"
    assert not (target3 / "AuroraKnowledgeDB").exists(), "служебные папки созданы без правил"
    assert not (target3 / ".aurora").exists(), "движок создан без правил"


@test
def test_doctor_names_one_path_once_with_all_causes(tmp: Path):
    """(e) M5: tracked+overridden — одна строка, в ней обе причины и оба действия."""
    import aurora_doctor as AD

    lp = _lp()
    repo = _init(tmp / "doctor")
    _block(repo)
    log = repo / JOURNAL
    log.parent.mkdir(parents=True)
    log.write_text("log\n", encoding="utf-8")
    with (repo / ".gitignore").open("a", encoding="utf-8") as f:
        f.write(f"!{JOURNAL}\n")
    _commit(repo, "journal tracked and negated")
    found = lp.findings(repo)
    assert found["tracked"] == [JOURNAL] and JOURNAL in found["overridden"], found

    real_root = AD.ROOT
    AD.ROOT = repo
    try:
        errors, warns = AD.check_local_paths()
    finally:
        AD.ROOT = real_root
    named = [ln for ln in errors + warns if JOURNAL in ln]
    assert len(named) == 1, f"путь назван не один раз: {named}"
    line = named[0]
    assert "лежат в индексе" in line and "перекрыт" in line, line
    assert "git rm -r --cached" in line and "уберите отрицание" in line, line

    # Отказ check-ignore не отменяет уже вычисленные missing_rules.
    repo = _init(tmp / "doctor-probe")
    _block(repo)
    (repo / JOURNAL).parent.mkdir(parents=True)
    (repo / JOURNAL).write_text("log\n", encoding="utf-8")
    _commit(repo, "journal")
    text = (repo / ".gitignore").read_text(encoding="utf-8")
    (repo / ".gitignore").write_text(
        "\n".join(ln for ln in text.splitlines()
                  if ln.strip() != "AuroraKnowledgeDB/meta/run_log.md") + "\n",
        encoding="utf-8")
    real_git = lp._git

    def failing_probe(root, *args, **kw):
        if args[:1] == ("check-ignore",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: check-ignore failed")
        return real_git(root, *args, **kw)

    AD.ROOT, lp._git = repo, failing_probe
    try:
        errors, warns = AD.check_local_paths()
    finally:
        AD.ROOT, lp._git = real_root, real_git
    assert any(JOURNAL in e and "правила нет" in e for e in errors), errors
    assert any("не проверены" in w for w in warns), warns


@test
def test_project_root_and_loader_wording(_tmp: Path):
    """(f) minor: корень проекта из подпапки; загрузчик не обещает встроенный список."""
    lp = _lp()
    parent = _init(Path(_tmp) / "parent")
    (parent / "AuroraKnowledgeDB").mkdir()
    (parent / "projects/demo/AuroraKnowledgeDB").mkdir(parents=True)
    assert lp.project_root(parent / "AuroraKnowledgeDB") == parent.resolve()
    assert lp.project_root(parent / "projects/demo") == (parent / "projects/demo").resolve()
    assert lp.boundary(parent)["nested"] is False
    nested = lp.boundary(parent / "projects/demo")
    assert nested["ok"] is True and nested["nested"] is True and nested["prefix"] == "projects/demo"

    src = (Path(__file__).resolve().parents[2] / "scripts" / "aurora_common.py").read_text(
        encoding="utf-8")
    head = src[src.index("def load_local_paths("):]
    head = head[:head.index('"""', head.index('"""') + 3)]
    assert "встроенному списку" not in head, "загрузчик всё ещё обещает работу по списку"
