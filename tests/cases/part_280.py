#!/usr/bin/env python3
"""part_280 — регрессии второго ревью 0003: git-контекст, границы и отказы.

Все заготовки — одноразовые `git init` во временной папке прогона. Пользовательские
проекты и их индексы не читаются и не меняются.

Что проверяем:
  (a) панель и чекпойнт не смотрят чужой GIT_INDEX_FILE/GIT_DIR: история проекта и
      посторонний индекс остаются прежними (B1);
  (b) `kit:update` вложенного проекта не снимает данные родителя и не трогает
      соседний staged-файл (B2);
  (c) отказ add/diff/commit после первой мутации возвращает индекс байт в байт,
      вместе со staged-содержимым и флагами (M3);
  (d) неудачный откат чекпойнта не выглядит успехом (M4);
  (e) доказанный путь в индексе виден и при нечитаемых правилах, путь не дублируется (M5);
  (f) чужое вложенное правило `Code/*` не рождает совет, пути литеральны для git (M6);
  (g) повторный `--apply` при актуальной версии всё равно чистит индекс и честно
      говорит об отказе (M7);
  (h) установка останавливается до служебных файлов, если правила записать нельзя (M8);
  (i) загрузчик реестра не обещает работу по встроенному списку (minor).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from harness import KIT, SCRIPTS, make_project, run, test


def _lp():
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
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_block(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".gitignore").write_text(_lp().gitignore_block(), encoding="utf-8")


def _names(repo: Path, rev: str = "HEAD") -> list:
    return _git(repo, "show", "--name-only", "--format=", rev).stdout.split()


def _index_bytes(repo: Path) -> bytes:
    return (repo / ".git" / "index").read_bytes()


def _index_state(repo: Path) -> tuple:
    """Индекс человека: байты, записи с режимами, флаги, staged-содержимое."""
    return (_index_bytes(repo),
            _git(repo, "ls-files", "-s", "-z").stdout,
            _git(repo, "ls-files", "-v", "-z").stdout,
            _git(repo, "diff", "--cached", "--name-status", "-z").stdout)


def _local_repo(repo: Path, mark: str = "local") -> None:
    """Блок правил, журнал и карточка; журнал насильно отслеживается, как до 0003."""
    _write_block(repo)
    (repo / "Cards").mkdir(exist_ok=True)
    (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
    log = repo / "AuroraKnowledgeDB/meta/run_log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{mark}\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")


def _alt_index(repo: Path, alt: Path, *drop: str) -> None:
    """Построить посторонний индекс из HEAD и убрать из него пути `drop`."""
    env = {**os.environ, "GIT_INDEX_FILE": str(alt)}
    _git(repo, "read-tree", "HEAD", env=env)
    for rel in drop:
        _git(repo, "rm", "-q", "--cached", "-f", "--", f":(literal){rel}", env=env)


@test
def test_explicit_git_context_keeps_foreign_index_and_history_clean(tmp: Path):
    """(a) B1: панель и чекпойнт работают с индексом проекта, а не с GIT_INDEX_FILE.

    Посторонний индекс без журнала раньше видел «чисто», пока `add -A` обновлял настоящий
    индекс: изменённый журнал уезжал в историю проекта. GIT_DIR/GIT_WORK_TREE уводили
    очистку в чужой репозиторий. Проверяем оба случая и оба писателя.
    """
    import agent_runner as AR
    import git_sync as GS

    journal = "AuroraKnowledgeDB/meta/run_log.md"

    # Панель: посторонний индекс без журнала.
    repo = _init(tmp / "panel-alt")
    _local_repo(repo)
    _commit(repo, "init")
    (repo / journal).write_text("new-local-dirty\n", encoding="utf-8")
    (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
    alt = tmp / "alt.index"
    _alt_index(repo, alt, journal)
    alt_before = alt.read_bytes()
    real_before = _index_bytes(repo)

    with patch.dict(os.environ, {"GIT_INDEX_FILE": str(alt)}):
        res = GS.commit(str(repo))
    assert res["ok"], res
    assert _names(repo) == ["Cards/a.md"], f"в коммит панели попал журнал: {_names(repo)}"
    assert "new-local-dirty" not in _git(repo, "log", "-p", "--all", "--format=").stdout, \
        "содержимое журнала попало в историю проекта"
    assert alt.read_bytes() == alt_before, "панель изменила посторонний индекс"
    assert not _git(repo, "ls-files", "--", journal).stdout.strip(), \
        "журнал остался в настоящем индексе после коммита"
    assert real_before != _index_bytes(repo), "подготовка: настоящий индекс не менялся"

    # Панель: GIT_DIR/GIT_WORK_TREE уводят очистку в чужой репозиторий.
    repo = _init(tmp / "panel-dir")
    _local_repo(repo)
    _commit(repo, "init")
    (repo / journal).write_text("new-local-dirty\n", encoding="utf-8")
    (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
    foreign = _init(tmp / "foreign")
    _local_repo(foreign, mark="foreign-local")
    foreign_head = _commit(foreign, "foreign init")
    with patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git"),
                                 "GIT_WORK_TREE": str(foreign)}):
        res = GS.commit(str(repo))
    assert res["ok"], res
    assert _names(repo) == ["Cards/a.md"], f"в коммит панели попал журнал: {_names(repo)}"
    assert "new-local-dirty" not in _git(repo, "log", "-p", "--all", "--format=").stdout, \
        "содержимое журнала попало в историю проекта"
    assert _git(foreign, "rev-parse", "HEAD").stdout.strip() == foreign_head, \
        "панель сдвинула HEAD чужого репозитория"
    assert _git(foreign, "ls-files", "--", journal).stdout.strip(), \
        "панель сняла журнал чужого репозитория с индекса"

    # Чекпойнт: посторонний индекс без журнала не должен быть тем индексом, который коммитят.
    repo = _init(tmp / "checkpoint-alt")
    _local_repo(repo)
    _commit(repo, "init")
    (repo / journal).write_text("new-local-dirty\n", encoding="utf-8")
    (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
    alt = tmp / "checkpoint-alt.index"
    _alt_index(repo, alt, journal)
    alt_before = alt.read_bytes()
    with patch.dict(os.environ, {"GIT_INDEX_FILE": str(alt)}):
        cp = AR.checkpoint(str(repo), "задача", True)
    assert cp["ok"] and cp["committed"] == 1, cp
    assert _names(repo) == ["Cards/a.md"], f"чекпойнт записал журнал: {_names(repo)}"
    assert alt.read_bytes() == alt_before, "чекпойнт изменил посторонний индекс"


@test
def test_nested_project_policy_does_not_touch_the_parent(tmp: Path):
    """(b) B2: очистка вложенного проекта не снимает родительский Code и чужой staged.

    `repo/projects/demo` без своего `.git`: правила пишутся в `demo/.gitignore`, а индекс
    принадлежит родителю. Политика обязана применяться к `projects/demo/...`, а не к
    `repo/Code/*`, `repo/AuroraKnowledgeDB`. Соседний staged-файл и карточка demo целы.
    """
    lp = _lp()
    parent = _init(tmp / "repo")
    _write_block(parent)
    (parent / "Code/product").mkdir(parents=True)
    (parent / "Code/product/impl.py").write_text("print(1)\n", encoding="utf-8")
    parent_log = parent / "AuroraKnowledgeDB/meta/run_log.md"
    parent_log.parent.mkdir(parents=True, exist_ok=True)
    parent_log.write_text("parent-local\n", encoding="utf-8")

    demo = parent / "projects/demo"
    demo.mkdir(parents=True)
    _write_block(demo)
    journal = "AuroraKnowledgeDB/meta/run_log.md"
    demo_log = demo / journal
    demo_log.parent.mkdir(parents=True)
    demo_log.write_text("demo-local\n", encoding="utf-8")
    (demo / "Cards").mkdir()
    (demo / "Cards/a.md").write_text("card\n", encoding="utf-8")
    inner = _init(demo / "Code/acme")
    (inner / "a.py").write_text("print(1)\n", encoding="utf-8")
    _commit(inner, "clone")

    sibling = parent / "projects/sibling/notes.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("old\n", encoding="utf-8")
    _git(parent, "add", "-A", "-f")
    _commit(parent, "init")
    sibling.write_text("staged sibling\n", encoding="utf-8")
    _git(parent, "add", "--", "projects/sibling/notes.md")

    found = lp.findings(demo)
    assert found["checked"] is True, found
    assert set(found["tracked"]) == {journal, "Code/acme"}, \
        f"доктор вложенного проекта видит чужие пути: {found}"
    assert found["missing_rules"] == [], found
    assert lp.missing_rules(demo) == [], "правила demo/.gitignore не найдены"
    assert lp.code_clones(demo) == ["Code/acme"], lp.code_clones(demo)

    removed = lp.untrack_local(demo)
    assert removed == [journal, "Code/acme"], removed
    assert _git(parent, "ls-files", "--", "Code/product/impl.py").stdout.strip(), \
        "родительский Code/product снят с индекса"
    assert _git(parent, "ls-files", "--", "AuroraKnowledgeDB/meta/run_log.md").stdout.strip(), \
        "родительский журнал снят с индекса"
    assert not _git(parent, "ls-files", "--", "projects/demo/AuroraKnowledgeDB/meta/run_log.md").stdout.strip(), \
        "локальный журнал demo остался в индексе"
    assert not _git(parent, "ls-files", "--", "projects/demo/Code/acme").stdout.strip(), \
        "клон demo остался в индексе"
    staged = _git(parent, "diff", "--cached", "--name-only").stdout.split()
    assert "projects/sibling/notes.md" in staged, f"соседний staged снят: {staged}"
    assert "Code/product/impl.py" not in staged, f"очистка demo подготовила удаление родителя: {staged}"
    assert parent_log.read_text(encoding="utf-8") == "parent-local\n"
    assert _git(parent, "ls-files", "--", "projects/demo/Cards/a.md").stdout.strip(), \
        "карточка demo ушла из индекса"


@test
def test_unresolved_project_boundary_fails_closed(tmp: Path):
    """(b2) B2: если граница проекта и git не определяется — отказ, а не «нарушений нет».

    Проверка, которая молча возвращает пустой список при чужом/неразрешимом корне,
    выглядит как «индекс чист» и пропускает служебный файл в коммит.
    """
    lp = _lp()
    journal = "AuroraKnowledgeDB/meta/run_log.md"
    repo = _init(tmp / "repo")
    _local_repo(repo)
    _commit(repo, "init")

    real_toplevel = lp._toplevel
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    lp._toplevel = lambda _root: elsewhere
    try:
        got = lp.enforce_index(repo)
        assert got["checked"] is False, got
        assert got["detail"] == lp.REASONS["scope_unresolved"], got
        assert lp.untrack_local(repo) == [], "неразрешённая граница сняла пути"
        found = lp.findings(repo)
        assert found["checked"] is False and found["detail"] == lp.REASONS["scope_unresolved"], found
    finally:
        lp._toplevel = real_toplevel
    assert _git(repo, "ls-files", "--", journal).stdout.strip(), \
        "неразрешённая граница изменила индекс проекта"


@test
def test_refusals_restore_the_index_bytes_and_flags(tmp: Path):
    """(c) M3: отказ после первой мутации возвращает индекс человека прежним.

    Панель снимала журнал до `add`, а `diff`/хук отказывали уже после `add -A`; временный
    индекс `commit_result` тоже менял настоящий. Сравниваем байты индекса, записи с
    режимами, флаги и staged-содержимое до и после каждого отказа.
    """
    from unittest.mock import patch

    import agent_runner as AR
    import git_sync as GS

    journal = "AuroraKnowledgeDB/meta/run_log.md"

    def prepare(name: str) -> tuple:
        repo = _init(tmp / name)
        _local_repo(repo)
        (repo / "Flags").mkdir()
        for rel in ("skip.md", "assume.md", "staged.md"):
            (repo / "Flags" / rel).write_text(f"{rel}\n", encoding="utf-8")
        _commit(repo, "init")
        _git(repo, "update-index", "--skip-worktree", "--", "Flags/skip.md")
        _git(repo, "update-index", "--assume-unchanged", "--", "Flags/assume.md")
        # Настоящий intent-to-add — на новый файл: уже закоммиченный `add -N` им не был.
        (repo / "Flags/ita.md").write_text("ita\n", encoding="utf-8")
        _git(repo, "add", "-N", "--", "Flags/ita.md")
        (repo / "Flags/staged.md").write_text("staged edit\n", encoding="utf-8")
        _git(repo, "add", "--", "Flags/staged.md")
        (repo / journal).write_text("dirty-local\n", encoding="utf-8")
        (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
        (repo / "Flags/skip.md").write_text("skip edit\n", encoding="utf-8")
        return repo

    def gs_fail(project, args, *a, **kw):
        return 1, "", "error: forced"

    repo = prepare("panel")
    before = _index_state(repo)
    real_run_git = GS.run_git

    def gs_add_fail(project, args, *a, **kw):
        if args[:1] == ["add"]:
            return gs_fail(project, args)
        return real_run_git(project, args, *a, **kw)

    with patch.object(GS, "run_git", gs_add_fail):
        res = GS.commit(str(repo))
    assert res["ok"] is False, res
    assert _index_state(repo) == before, "отказ add панели оставил индекс изменённым"

    def gs_diff_fail(project, args, *a, **kw):
        if args[:2] == ["diff", "--cached"]:
            return gs_fail(project, args)
        return real_run_git(project, args, *a, **kw)

    with patch.object(GS, "run_git", gs_diff_fail):
        res = GS.commit(str(repo))
    assert res["ok"] is False, res
    assert _index_state(repo) == before, "отказ diff панели оставил индекс изменённым"

    def gs_commit_fail(project, args, *a, **kw):
        if "commit" in args:
            return gs_fail(project, args)
        return real_run_git(project, args, *a, **kw)

    with patch.object(GS, "run_git", gs_commit_fail):
        res = GS.commit(str(repo))
    assert res["ok"] is False, res
    assert _index_state(repo) == before, "отказ commit панели оставил индекс изменённым"

    repo = prepare("result")
    before = _index_state(repo)
    card = repo / "AuroraKnowledgeDB/Concepts/x.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntype: concept\n---\n# X\n", encoding="utf-8")
    before = _index_state(repo)
    real_retry = AR.git_retry

    def ar_add_fail(*args, **kw):
        if args[:1] == ("add",):
            assert "GIT_INDEX_FILE" in kw.get("env", {}), "add не использует частный индекс"
            return 1, "", "error: forced"
        return real_retry(*args, **kw)

    with patch.object(AR, "git_retry", ar_add_fail):
        res = AR.commit_result(str(repo), "agent:distill", "заход", True)
    assert res["ok"] is False, res
    assert _index_state(repo) == before, "отказ add commit_result оставил индекс изменённым"

    real_git = AR.git

    def ar_diff_fail(*args, **kw):
        if args[:2] == ("diff", "--cached") and "--quiet" in args:
            # 1 — законные различия; отказ git — 128.
            return 128, "", "error: forced"
        return real_git(*args, **kw)

    with patch.object(AR, "git", ar_diff_fail):
        res = AR.commit_result(str(repo), "agent:distill", "заход", True)
    assert res["ok"] is False, res
    assert _index_state(repo) == before, "отказ diff commit_result оставил индекс изменённым"

    with patch.object(AR, "_commit_db", lambda *a, **kw: (1, "forced commit failure")):
        res = AR.commit_result(str(repo), "agent:distill", "заход", True)
    assert res["ok"] is False, res
    assert _index_state(repo) == before, "отказ коммита результата оставил индекс изменённым"


@test
def test_checkpoint_noop_reports_incomplete_rollback(tmp: Path):
    """(d) M2: незавершённая подготовка no-op даёт отказ; откат индекса не нужен."""
    import agent_runner as AR
    import local_paths as lp

    def prepare(name: str) -> Path:
        repo = _init(tmp / name)
        _write_block(repo)
        (repo / "Cards").mkdir()
        (repo / "Cards/a.md").write_text("card\n", encoding="utf-8")
        for rel in ("AuroraKnowledgeDB/meta/run_log.md",
                    "AuroraKnowledgeDB/meta/recent-files.json"):
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("old\n", encoding="utf-8")
        (repo / "Flags").mkdir()
        (repo / "Flags/skip.md").write_text("flag\n", encoding="utf-8")
        _git(repo, "add", "-A", "-f")
        _commit(repo, "init")
        _git(repo, "update-index", "--skip-worktree", "--", "Flags/skip.md")
        (repo / "AuroraKnowledgeDB/meta/run_log.md").write_text("dirty\n", encoding="utf-8")
        (repo / "AuroraKnowledgeDB/meta/recent-files.json").write_text("dirty\n", encoding="utf-8")
        return repo

    repo = prepare("diff")
    state = _index_state(repo), _git(repo, "rev-parse", "HEAD").stdout
    real_git = AR.git

    def fail_diff(*args, **kw):
        if args[:3] == ("diff", "--cached", "--name-only"):
            return 1, "", "error: diff refused"
        return real_git(*args, **kw)

    with patch.object(AR, "git", fail_diff):
        got = AR.checkpoint(str(repo), "задача", True)
    assert got["ok"] is False and "diff refused" in got["why"], got
    assert "saved_tree" not in got and "flags_restored" not in got, got
    assert (_index_state(repo), _git(repo, "rev-parse", "HEAD").stdout) == state

    repo = prepare("flags")
    state = _index_state(repo), _git(repo, "rev-parse", "HEAD").stdout
    git_registry = lp._git

    def fail_flags(root, *args, **kw):
        if args[:2] == ("ls-files", "-v"):
            return subprocess.CompletedProcess(args, 1, b"", b"error: flags unreadable")
        return git_registry(root, *args, **kw)

    with patch.object(lp, "_git", fail_flags):
        got = AR.checkpoint(str(repo), "задача", True)
    assert got["ok"] is False and "flags unreadable" in got["why"], got
    assert "flags_restored" not in got and "flags_lost" not in got, got
    assert (_index_state(repo), _git(repo, "rev-parse", "HEAD").stdout) == state

    real_retry = AR.git_retry

    def no_rollback(*args, **kw):
        assert args[0] not in ("read-tree", "update-index", "commit"), args
        return real_retry(*args, **kw)

    with patch.object(AR, "git_retry", no_rollback):
        got = AR.checkpoint(str(repo), "задача", True)
    assert got["ok"] is True and got["committed"] == 0, got
    assert (_index_state(repo), _git(repo, "rev-parse", "HEAD").stdout) == state


@test
def test_findings_keep_proven_path_when_rules_are_unreadable(tmp: Path):
    """(e) M5: доказанный путь в индексе виден и без проверки правил, без двух строк.

    `findings` находил журнал, затем спотыкался о `.gitignore` и doctor показывал только
    «проверка не выполнена». При успешной проверке тот же путь попадал и в ошибку, и в
    «правило перекрыто». Путь должен называться один раз, а причины — сходиться в `problems`.
    """
    lp = _lp()
    journal = "AuroraKnowledgeDB/meta/run_log.md"

    repo = _init(tmp / "unreadable")
    _write_block(repo)
    log = repo / journal
    log.parent.mkdir(parents=True)
    log.write_text("log\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "journal tracked")
    (repo / ".gitignore").unlink()
    (repo / ".gitignore").mkdir()
    (repo / ".gitignore/mine.txt").write_text("mine\n", encoding="utf-8")

    found = lp.findings(repo)
    assert found["checked"] is False and found["detail"] == lp.REASONS["gitignore_unreadable"], found
    assert journal in found["tracked"], f"доказанный путь потерян при отказе правил: {found}"
    cp = run("aurora_doctor.py", cwd=repo)
    text = cp.stdout + cp.stderr
    named = [ln for ln in text.splitlines() if journal in ln]
    assert len(named) == 1, f"doctor не назвал путь ровно один раз: {named}"
    assert "локальные пути машины лежат в индексе" in named[0], text[-2000:]
    assert "не проверены" in text, text[-2000:]

    repo = _init(tmp / "overridden")
    _write_block(repo)
    log = repo / journal
    log.parent.mkdir(parents=True)
    log.write_text("log\n", encoding="utf-8")
    with (repo / ".gitignore").open("a", encoding="utf-8") as f:
        f.write(f"!{journal}\n")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "journal tracked and negated")
    found = lp.findings(repo)
    assert found["checked"] is True, found
    assert journal in found["tracked"] and journal in found["overridden"], found
    assert set(found["problems"][journal]) == {"tracked", "overridden"}, found["problems"]
    cp = run("aurora_doctor.py", cwd=repo)
    text = cp.stdout + cp.stderr
    named = [ln for ln in text.splitlines() if journal in ln]
    assert len(named) == 1, f"путь назван дважды: {named}"
    assert "локальные пути машины лежат в индексе" in named[0], text[-2000:]

    # Отказ `check-ignore` не отменяет уже доказанную запись в индексе.
    repo = _init(tmp / "probe")
    _write_block(repo)
    log = repo / journal
    log.parent.mkdir(parents=True)
    log.write_text("log\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "journal tracked")
    import aurora_doctor as AD

    real_root, real_git = AD.ROOT, lp._git

    def failing_probe(root, *args, **kw):
        if args[:1] == ("check-ignore",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: check-ignore failed")
        return real_git(root, *args, **kw)

    AD.ROOT = repo
    lp._git = failing_probe
    try:
        errors, warns = AD.check_local_paths()
    finally:
        AD.ROOT = real_root
        lp._git = real_git
    assert any(journal in e for e in errors), f"доказанный путь потерян при отказе: {errors}"
    assert any("не проверены" in w for w in warns), warns


@test
def test_foreign_rule_source_does_not_authorize_untrack_advice(tmp: Path):
    """(f1) M6: `Code/*` из вложенного `.gitignore` не делает чужой файл локальным."""
    import aurora_update as AU

    lp = _lp()
    repo = _init(tmp / "repo")
    _write_block(repo)
    (repo / "Artifacts/foo/Code").mkdir(parents=True)
    (repo / "Artifacts/foo/Code/real.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "Artifacts/foo/.gitignore").write_text("Code/*\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "foreign rule")
    assert _git(repo, "ls-files", "--", "Artifacts/foo/Code/real.py").stdout.strip(), \
        "подготовка: файл не в индексе"
    assert lp.tracked_local(repo) == [], \
        f"чужое правило .gitignore выдало файл за локальный: {lp.tracked_local(repo)}"
    assert AU.tracked_but_ignored(repo) == [], \
        f"обновление советует снять чужой файл: {AU.tracked_but_ignored(repo)}"


@test
def test_reset_and_doctor_advice_use_literal_pathspecs(tmp: Path):
    """(f2) M6: имя со `*` не читается git как шаблон в reset и в подсказке doctor."""
    import agent_runner as AR
    import git_sync as GS

    lp = _lp()

    def prepare(name: str) -> Path:
        repo = _init(tmp / name)
        _write_block(repo)
        inner = _init(repo / "Code/a*b")
        (inner / "a.py").write_text("print(1)\n", encoding="utf-8")
        _commit(inner, "clone")
        (repo / "Code/axb").write_text("human neighbour\n", encoding="utf-8")
        (repo / "Code/axb.txt").write_text("human\n", encoding="utf-8")
        (repo / "Cards").mkdir()
        (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
        _git(repo, "add", "-A", "-f")
        _commit(repo, "init")
        (repo / "Code/axb.txt").write_text("staged human\n", encoding="utf-8")
        _git(repo, "add", "--", "Code/axb.txt")
        (repo / "Code/axb").write_text("staged neighbour\n", encoding="utf-8")
        _git(repo, "add", "--", "Code/axb")
        (repo / "Cards/a.md").write_text("new card\n", encoding="utf-8")
        return repo

    repo = prepare("panel")
    assert lp.code_clones(repo) == ["Code/a*b"], lp.code_clones(repo)
    cp = run("aurora_doctor.py", cwd=repo)
    text = cp.stdout + cp.stderr
    assert "':(literal)Code/a*b'" in text, f"doctor дал путь, который git прочтёт шаблоном: {text[-2000:]}"
    res = GS.commit(str(repo))
    assert res["ok"], res
    committed = _names(repo)
    assert "Code/axb.txt" in committed and "Code/axb" in committed and "Cards/a.md" in committed, \
        f"reset по литеральному пути не удержал соседний файл: {committed}"
    assert not _git(repo, "ls-files", "--", ":(literal)Code/a*b").stdout.strip(), \
        "клон остался в индексе"

    repo = prepare("checkpoint")
    cp = AR.checkpoint(str(repo), "задача", True)
    assert cp["ok"] and cp["committed"] == 3, cp
    committed = _names(repo)
    assert "Code/axb.txt" in committed and "Code/axb" in committed and "Cards/a.md" in committed, \
        f"чекпойнт снял соседний файл шаблоном: {committed}"


@test
def test_repeated_update_cleans_the_index_and_reports_refusal(tmp: Path):
    """(g) M7: актуальная версия не отменяет очистку индекса; отказ не выглядит успехом."""
    import contextlib
    import io

    import aurora_update as AU

    lp = _lp()
    journal = "AuroraKnowledgeDB/meta/run_log.md"
    root = make_project(tmp)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert AU.run(root, True) == 0
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _commit(root, "installed")
    assert AU.project_version(root) == AU.kit_version(), "подготовка: версия проекта не актуальна"
    log = root / journal
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("tracked-local\n", encoding="utf-8")
    _git(root, "add", "-f", "--", journal)
    _commit(root, "journal used to be tracked")

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        assert AU.run(root, True) == 0
    text = out.getvalue()
    assert not _git(root, "ls-files", "--", journal).stdout.strip(), \
        f"актуальная версия пропустила очистку индекса:\n{text[-2000:]}"
    assert "сняты с учёта git" in text, text[-2000:]

    _git(root, "add", "-f", "--", journal)
    real_enforce = lp.enforce_index

    def remaining(_root):
        return {"removed": [], "remaining": [journal], "checked": True, "detail": ""}

    lp.enforce_index = remaining
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            assert AU.run(root, True) == 7, "остаток в индексе не отличим от успеха"
    finally:
        lp.enforce_index = real_enforce
    text = out.getvalue()
    assert _git(root, "ls-files", "--", journal).stdout.strip(), \
        f"остаток в индексе скрыт: {text[-2000:]}"
    assert "остались в индексе" in text and "сняты с учёта git" not in text, text[-2000:]

    def unchecked(_root):
        return {"removed": [], "remaining": [], "checked": False,
                "detail": lp.REASONS["index_unreadable"]}

    lp.enforce_index = unchecked
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            assert AU.run(root, True) == 7, "непроверенный индекс не отличим от успеха"
    finally:
        lp.enforce_index = real_enforce
    text = out.getvalue()
    assert _git(root, "ls-files", "--", journal).stdout.strip(), \
        f"отказ проверки индекса скрыт: {text[-2000:]}"
    assert lp.REASONS["index_unreadable"] in text and "сняты с учёта git" not in text, text[-2000:]


@test
def test_install_stops_before_service_files_when_rules_cannot_be_written(tmp: Path):
    """(h) M8: symlink или нечитаемый `.gitignore` — отказ до `ensure_dirs`, файл не тронут."""
    import install_aurora as IA

    def install(target: Path) -> int:
        inst = IA.Installer(target=target, name="Demo", slug=None, jira=None, confluence=None,
                            force=False, dry_run=False)
        return inst.run()

    link_repo = _init(tmp / "link")
    outside = tmp / "outside.gitignore"
    outside.write_text("# снаружи\n", encoding="utf-8")
    (link_repo / ".gitignore").symlink_to(outside)
    rc = install(link_repo)
    assert rc != 0, "установка продолжилась без правил .gitignore"
    assert outside.read_text(encoding="utf-8") == "# снаружи\n", "внешний файл изменён"
    assert (link_repo / ".gitignore").is_symlink(), "симлинк заменён файлом"
    assert not (link_repo / "AuroraKnowledgeDB").exists(), "служебные папки созданы без правил"
    assert not (link_repo / "aurora.config.yaml").exists(), "конфиг создан без правил"
    assert not (link_repo / ".aurora").exists(), "копия движка создана без правил"

    dir_repo = _init(tmp / "dir")
    (dir_repo / ".gitignore").mkdir()
    (dir_repo / ".gitignore/mine.txt").write_text("mine\n", encoding="utf-8")
    rc = install(dir_repo)
    assert rc != 0, "установка продолжилась с нечитаемым .gitignore"
    assert (dir_repo / ".gitignore/mine.txt").read_text(encoding="utf-8") == "mine\n", \
        "содержимое нечитаемого .gitignore потеряно"
    assert not (dir_repo / "AuroraKnowledgeDB").exists(), "служебные папки созданы без правил"
    assert not (dir_repo / "aurora.config.yaml").exists(), "конфиг создан без правил"


@test
def test_registry_loader_does_not_promise_a_fallback(_tmp: Path):
    """(i) minor: без реестра загрузчик говорит нейтрально, а не «работаю по списку»."""
    code = """
import sys
sys.path.insert(0, @@SCRIPTS@@)
import importlib.abc

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "local_paths":
            raise ImportError("forced")
        return None

sys.meta_path.insert(0, Block())
import aurora_common
assert aurora_common.load_local_paths() is None
print("LOADER-OK")
"""
    cp = subprocess.run([sys.executable, "-c", code.replace("@@SCRIPTS@@", repr(str(SCRIPTS)))],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0 and "LOADER-OK" in cp.stdout, out
    assert "работаю по встроенному списку" not in out, f"загрузчик обещает работу по списку: {out}"
    assert "реестр локальных путей" in out and "недоступен" in out, out
