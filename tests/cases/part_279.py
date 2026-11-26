#!/usr/bin/env python3
"""part_279 — локальные файлы машины не попадают в git (0003).

Проверки идут на настоящем `git init` во временной папке: подставного git нет, потому
что весь смысл задачи — в поведении самого git (`.gitignore` задним числом не действует,
`git rm --cached` оставляет файл на диске, `add -A` возвращает снятое в индекс).

Что проверяем:
  (a) блок `.gitignore` заморожен и совпадает с установщиком; новый проект получает
      правила до первой записи, в том числе при обрыве установки (AC-2);
  (b) явные пути и клоны `Code/*` снимаются с индекса, файлы на диске целы (AC-3);
  (c) коммит панели и чекпойнт агента фиксируют обычный файл, локального нет ни в
      коммите, ни в индексе (AC-4);
  (d) грязно только локальным — это no-op, а не остановка агента (AC-4);
  (e) общие данные команды остаются в git после очистки (AC-6);
  (f) doctor называет локальный путь в индексе и печатает безопасную команду (AC-5);
  (g) без `local_paths.py` импорт и update работают, пишущие коммиты отказывают (B-3);
  (h) `.gitignore` не в UTF-8 не теряет правила человека при дописывании блока кита (B-1);
  (i) нечитаемый `.gitignore` не переписывается вовсе (B-1);
  (j) настоящий клон — gitlink `Code/<имя>` — снимается, doctor даёт работающую команду (M-1);
  (k) локальный файл со staged-правкой не попадает в коммит (M-2);
  (l) встроенные литералы отката не разошлись с реестром (m-1);
  (m) полный `main` доктора без реестра работает и говорит «правила не проверены» (m-6).
  (n) checkpoint → commit_result не возвращает снятый журнал в историю (B-1);
  (o) commit_result из подпапки не записывает удаления вне базы (B-1);
  (p) отрицание `!` после блока не обходит правила, doctor называет перекрытие (B-2);
  (q) doctor различает «чисто», «правило перекрыто» и «проверка не выполнена» (B-2);
  (r) `.gitignore` хранит байты и права, не пишется через симлинк и при сбое (M-5);
  (s) обычный файл `Code/<файл>` не клон, имя с пробелом экранируется (M-4);
  (t) отказ `add`/`diff` — не успех, индекс человека возвращён (M-2).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, run, test

# Замороженная копия блока правил: она меняется только вместе с ТР и `local_paths.py`.
# Иначе набор локальных путей молча разъедется с тем, что пишет установка.
FROZEN_BLOCK = """
# Aurora / local
.DS_Store
.env
.env.*
!.env.aurora.local.example
aurora.env.local
.env.aurora.local
.mcp.json
__pycache__/
*.pyc
.ruff_cache/
~$*
*.log
# Временный файл атомарной записи: остаётся только после убитого процесса
*.aurora-tmp
# Повреждённое состояние движка, отложенное в сторону (aurora_fs.read_state), — для разбора, не для git
*.corrupt
*.corrupt-[0-9]*

# Движок Авроры — копия кита: `aurora.py update` ставит её из кита, версия записана в
# AuroraKnowledgeDB/meta/aurora_version.txt. Внутри — и состояние, прогоны, вложения, кэш.
.aurora/
# Папки харнессов — агентов и редакторов — у каждого свои: настройки, планы, кэш. Общие
# у проекта только навыки: `.claude/skills/` остаётся в git.
.cursor/
.opencode/
.codex/
.gemini/
.windsurf/
.claude/*
!.claude/skills/

# Служебное состояние инструментов. Правило Авроры: что закрыто .gitignore,
# то допустимо вне схемы папок — doctor такие пути не считает нарушением.
.sisyphus/
node_modules/
.venv/
# Браузерные MCP-инструменты сбрасывают логи консоли и слепки страниц в текущую
# рабочую папку. Открыли дашборд, стоя в корне проекта, — получили блокер doctor и
# слепок чужой страницы в истории git.
.playwright-mcp/
.puppeteer/
# Семантический индекс — производная от карточек: бинарь на мегабайты, меняется
# целиком при смене модели и пересобирается за минуты (`kb:embed --apply`).
AuroraKnowledgeDB/meta/embeddings.bin
AuroraKnowledgeDB/meta/embeddings.json
# Граф базы наружу (`kb:graph-export`, `kb:code-graph`) — производная: graph.json для
# MCP-сервера графа, страница и скрипт Neo4j от graphify на мегабайты, пересобираются
# каждым прогоном. Заметки тем в `MOC/Сообщества/` — навигация базы, они в git.
AuroraKnowledgeDB/meta/graphify/
# Журнал запусков панели — по строке на команду, меняется каждым запуском: в git он давал
# каждый восьмой коммит и вечно грязное дерево. Дата обновления базы для команды — в
# AuroraKnowledgeDB/meta/kb_updated.d/, она в git по частям участников.
AuroraKnowledgeDB/meta/run_log.md
# Снимки сети за прогон (`net:report`) — локальная телеметрия этой машины, а не знание
# проекта: панель читает их с диска. В git они не едут, иначе каждый push или pull
# оставлял бы незакоммиченный файл, а история росла бы на файл за прогон.
AuroraKnowledgeDB/meta/net-runs.d/
# Последние открытые в панели файлы: личная история на этой машине, не общее состояние проекта.
AuroraKnowledgeDB/meta/recent-files.json
# Штамп версии движка, записанный этой машиной при update: у каждого участника свой
# шаг обновления, а панель берёт штамп из копии движка в .aurora/ (не в git).
AuroraKnowledgeDB/meta/aurora_version.txt
# Пусковые файлы этой машины: движок ставит их при установке, они не исходник проекта.
start-aurora.bat
start-aurora.command
# Локальная точка сравнения выдачи (`kb:retrieval --save`), не общий эталон проекта.
AuroraKnowledgeDB/meta/retrieval-last.json
# Производный граф карточек: пересобирается из базы командой kb:graph.
AuroraKnowledgeDB/meta/graph.json
# Персональное состояние Obsidian. Состав плагинов, их код и темы остаются общими.
AuroraKnowledgeDB/.obsidian/workspace.json
AuroraKnowledgeDB/.obsidian/workspace-mobile.json
AuroraKnowledgeDB/.obsidian/app.json
AuroraKnowledgeDB/.obsidian/appearance.json
AuroraKnowledgeDB/.obsidian/graph.json
AuroraKnowledgeDB/.obsidian/hotkeys.json
AuroraKnowledgeDB/.obsidian/bookmarks.json
AuroraKnowledgeDB/.obsidian/starred.json
AuroraKnowledgeDB/.obsidian/plugins/recent-files-obsidian/data.json
# Клоны репозиториев кода (`code:sync`): чужой код в историю проекта не едет — у каждого свой клон, состав
# записан в aurora.config.yaml (`code_repos:`). В git остаются только описание папки и её пометка.
Code/*
!Code/.gitkeep
!Code/README.md
"""

KEEP_IN_GIT = (
    "Artifacts/Backlog/plan.md",
    "Deliverables/Draft/report.md",
    "AuroraKnowledgeDB/meta/timing.d/a.json",
    "AuroraKnowledgeDB/meta/usage.d/a.json",
    "AuroraKnowledgeDB/meta/search-quality.d/a.json",
    "AuroraKnowledgeDB/meta/agent-runs/a.json",
    "AuroraKnowledgeDB/meta/ask/q.json",
    "AuroraKnowledgeDB/meta/kb_updated.d/2026-11-25.json",
    "AuroraKnowledgeDB/meta/lint_baseline.txt",
    "AuroraKnowledgeDB/meta/build_failures.json",
    "AuroraKnowledgeDB/.obsidian/plugins/calendar/main.js",
)


def _lp():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import local_paths
    return local_paths


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
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


def _write_block(repo: Path, text: str | None = None) -> None:
    (repo / ".gitignore").write_text(text if text is not None else _lp().gitignore_block(),
                                     encoding="utf-8")


def _create_local(repo: Path, mark: str) -> None:
    """Создать каждый явный локальный путь буквально: файл или каталог со своим файлом."""
    for rel in _lp().LOCAL_STATE_RELS:
        target = repo / rel
        if rel in _lp().LOCAL_STATE_DIRS:
            target.mkdir(parents=True, exist_ok=True)
            (target / "record.json").write_text(f"local-{mark}\n", encoding="utf-8")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"local-{mark}\n", encoding="utf-8")


def _names(repo: Path, rev: str = "HEAD") -> list:
    return _git(repo, "show", "--name-only", "--format=", rev).stdout.split()


@test
def test_new_project_gets_the_rules_before_the_first_write(tmp: Path):
    """(a) AC-1/AC-2: блок заморожен, правила пишутся до служебных файлов, новый проект чист."""
    lp = _lp()
    assert lp.gitignore_block().splitlines() == FROZEN_BLOCK.splitlines(), \
        "блок .gitignore кита разошёлся с замороженной копией: правьте их вместе"
    import install_aurora as IA
    assert IA.GITIGNORE_BLOCK == lp.gitignore_block(), \
        "установщик и реестр разошлись: новый проект получит правила, по которым никто не снимет индекс"
    assert lp.validate() == [], f"у реестра есть снимаемые пути без правила в блоке: {lp.validate()}"
    kinds = {"local_path", "local_dir", "clone_glob", "ignore_only"}
    for rule in lp.RULES:
        assert rule.kind in kinds, rule
        if rule.untrack:
            assert rule.why, f"у снимаемого пути нет причины, почему он локальный: {rule}"

    repo = _init(tmp / "new")
    inst = IA.Installer(target=repo, name="Demo", slug=None, jira=None, confluence=None,
                        force=False, dry_run=False)
    assert inst.run() == 0
    assert "AuroraKnowledgeDB/meta/run_log.md" in (repo / ".gitignore").read_text(encoding="utf-8"), \
        "настоящая установка не дописала правила в .gitignore"
    assert IA.merge_gitignore(repo / ".gitignore") == [], "повторный merge дописывает те же правила"
    (repo / "Карточка.md").write_text("x\n", encoding="utf-8")
    _commit(repo, "install")

    _create_local(repo, "1")
    status = _git(repo, "status", "--porcelain").stdout.strip()
    assert status == "", f"локальные файлы нового проекта видны git: {status}"

    # Обрыв установки после первого шага: правила уже лежат, служебных файлов ещё нет.
    cut = _init(tmp / "cut")

    class Cut(IA.Installer):
        def ensure_dirs(self):
            raise RuntimeError("обрыв установки")

    broken = Cut(target=cut, name="Cut", slug=None, jira=None, confluence=None,
                 force=False, dry_run=False)
    try:
        broken.run()
    except RuntimeError as exc:
        assert "обрыв" in str(exc)
    else:
        raise AssertionError("обрыв установки не дошёл до теста")
    assert "AuroraKnowledgeDB/meta/run_log.md" in (cut / ".gitignore").read_text(encoding="utf-8"), \
        "прерванная установка оставила проект без правил"
    assert not (cut / "AuroraKnowledgeDB").exists(), "обрыв случился после первой служебной папки"
    assert not (cut / "aurora.config.yaml").exists(), "обрыв случился после первой служебной записи"


@test
def test_declared_local_paths_leave_the_index_and_stay_on_disk(tmp: Path):
    """(b) AC-3: явные пути и клон снимаются с индекса, файлы на диске целы, повтор пуст."""
    lp = _lp()
    repo = _init(tmp / "repo")
    _write_block(repo)
    _create_local(repo, "1")
    (repo / "Code/acme").mkdir(parents=True)
    (repo / "Code/acme/a.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "Code/README.md").write_text("клоны\n", encoding="utf-8")
    (repo / "Code/.gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "local state used to be tracked")
    assert _git(repo, "ls-files", "--", *lp.LOCAL_STATE_RELS).stdout.strip(), "подготовка: путь не в индексе"

    removed = lp.untrack_local(repo)
    assert removed == list(lp.LOCAL_STATE_RELS) + ["Code/acme"], removed
    for rel in lp.LOCAL_STATE_RELS:
        assert not _git(repo, "ls-files", "--", rel).stdout.strip(), f"{rel} остался в индексе"
        target = repo / rel
        if rel in lp.LOCAL_STATE_DIRS:
            assert (target / "record.json").read_text(encoding="utf-8") == "local-1\n", rel
        else:
            assert target.read_text(encoding="utf-8") == "local-1\n", rel
    assert (repo / "Code/acme/a.py").is_file(), "файл клона удалён с диска"
    assert _git(repo, "ls-files", "--", "Code").stdout.split() == ["Code/.gitkeep", "Code/README.md"], \
        "описание папки и пометка ушли из git вместе с клоном"
    assert lp.untrack_local(repo) == [], "повторное снятие снова меняет индекс"


@test
def test_commit_and_checkpoint_never_stage_local_paths(tmp: Path):
    """(c) AC-4: в коммите только обычный файл, локального нет ни в коммите, ни в индексе."""
    import agent_runner as AR
    import git_sync as GS

    local_rel = "AuroraKnowledgeDB/meta/run_log.md"
    for kind in ("checkpoint", "git_sync"):
        repo = _init(tmp / kind)
        _write_block(repo)
        (repo / "Cards").mkdir()
        (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
        (repo / local_rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / local_rel).write_text("old\n", encoding="utf-8")
        _git(repo, "add", "-A", "-f")
        _commit(repo, "init")
        (repo / local_rel).write_text("new-local\n", encoding="utf-8")
        (repo / "Cards/a.md").write_text("new card\n", encoding="utf-8")

        if kind == "checkpoint":
            res = AR.checkpoint(str(repo), "задача", True)
            assert res["ok"], res
            assert res["committed"] == 1, res
        else:
            res = GS.commit(str(repo))
            assert res["ok"], res
            assert res["files"] == 1, res
        assert _names(repo) == ["Cards/a.md"], (kind, "коммит назвал лишнее", _names(repo))
        assert not _git(repo, "ls-files", "--", local_rel).stdout.strip(), \
            (kind, "локальный путь вернулся в индекс")
        assert (repo / local_rel).read_text(encoding="utf-8") == "new-local\n", \
            (kind, "файл на диске тронут")


@test
def test_dirty_only_with_local_paths_is_a_noop(tmp: Path):
    """(d) AC-4: только локальное — чекпойнт и коммит ничего не делают и не падают."""
    import agent_runner as AR
    import git_sync as GS

    repo = _init(tmp / "repo")
    _write_block(repo)
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("x\n", encoding="utf-8")
    head = _commit(repo, "init")

    # новый снимок сети закрыт правилом: git его не видит, значит и коммитить нечего
    shard = repo / "AuroraKnowledgeDB/meta/net-runs.d"
    shard.mkdir(parents=True)
    (shard / "run-1.json").write_text("{}\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain").stdout.strip() == "", "снимок сети виден git"

    cp = AR.checkpoint(str(repo), "задача", True)
    assert cp["ok"] and cp["committed"] == 0, cp
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "чекпойнт сдвинул HEAD"
    res = GS.commit(str(repo))
    assert res["ok"] and res.get("nothing") is True, res

    # модифицированный отслеживаемый журнал — тоже только локальное: ни чекпойнт, ни
    # коммит панели не сдвигают HEAD и не фиксируют удаление из индекса.
    log = repo / "AuroraKnowledgeDB/meta/run_log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-f", "--", "AuroraKnowledgeDB/meta/run_log.md")
    head = _commit(repo, "journal used to be tracked")
    log.write_text("changed\n", encoding="utf-8")
    cp = AR.checkpoint(str(repo), "задача", True)
    assert cp["ok"] and cp["committed"] == 0, cp
    res = GS.commit(str(repo))
    assert res["ok"] and res.get("nothing") is True, res
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "no-op сдвинул HEAD"

    # тот же журнал: commit_result коммитит только снятие из индекса
    res = AR.commit_result(str(repo), "задача", "заголовок", True)
    assert res["ok"], res
    assert not _git(repo, "ls-files", "--", "AuroraKnowledgeDB/meta/run_log.md").stdout.strip(), res
    assert log.read_text(encoding="utf-8") == "changed\n", "файл журнала тронут на диске"
    assert set(_names(repo)) <= {"AuroraKnowledgeDB/meta/run_log.md"}, _names(repo)


@test
def test_checkpoint_then_commit_result_does_not_return_the_journal(tmp: Path):
    """(n) B-1: журнал, снятый чекпойнтом, не возвращается в коммит агента изменённым.

    Чекпойнт фиксирует работу человека и снимает журнал с настоящего индекса, но в HEAD
    журнал остаётся. `commit_result` собирал временный индекс из HEAD и делал `add` по базе:
    изменённое содержимое журнала уезжало в историю как работа агента. Чистка временного
    индекса по реестру оставляет только удаление (D), а не правку (M).
    """
    import agent_runner as AR

    log_rel = "AuroraKnowledgeDB/meta/run_log.md"
    repo = _init(tmp / "repo")
    _write_block(repo)
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
    log = repo / log_rel
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "init")

    log.write_text("new-local-dirty\n", encoding="utf-8")
    (repo / "Cards/a.md").write_text("human work\n", encoding="utf-8")
    cp = AR.checkpoint(str(repo), "задача", True)
    assert cp["ok"] and cp["committed"] == 1, cp
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split() == ["Cards/a.md"], \
        "подготовка: чекпойнт закоммитил лишнее"
    assert log.read_text(encoding="utf-8") == "new-local-dirty\n", "чекпойнт тронул журнал на диске"

    card_rel = "AuroraKnowledgeDB/Concepts/x.md"
    card = repo / card_rel
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\ntype: concept\n---\n# X\n", encoding="utf-8")
    res = AR.commit_result(str(repo), "agent:distill", "заход", True)
    assert res["ok"], res

    changes = [ln.split("\t") for ln in
               _git(repo, "show", "--name-status", "--format=", "HEAD").stdout.splitlines()]
    assert ["A", card_rel] in changes, changes
    assert ["D", log_rel] in changes, f"журнал должен уйти только удалением: {changes}"
    assert not any(kind == "M" and path == log_rel for kind, path in changes), \
        f"изменённый журнал вернулся в коммит: {changes}"
    assert "new-local-dirty" not in _git(repo, "log", "-p", "--all", "--format=").stdout, \
        "содержимое журнала попало в историю"
    assert not _git(repo, "ls-files", "--", log_rel).stdout.strip(), "журнал вернулся в индекс"


@test
def test_commit_result_from_subdir_keeps_clones_and_launchers_out(tmp: Path):
    """(o) B-1: коммит агента из подпапки не записывает удаления вне базы.

    `_commit_db` снимал с временного индекса весь `local`, включая gitlink-клон и пусковой
    файл: их удаление входило в коммит агента, хотя это не его работа. И первый `status` шёл
    из подпапки, не находя базу, — ложное «агент ничего не изменил».
    """
    import agent_runner as AR

    repo = _init(tmp / "repo")
    _write_block(repo)
    inner = _init(repo / "Code/acme")
    (inner / "a.py").write_text("print(1)\n", encoding="utf-8")
    _commit(inner, "clone")
    (repo / "Code/README.md").write_text("клоны\n", encoding="utf-8")
    (repo / "Code/.gitkeep").write_text("", encoding="utf-8")
    (repo / "start-aurora.command").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "init")

    card_rel = "AuroraKnowledgeDB/Concepts/y.md"
    card = repo / card_rel
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\ntype: concept\n---\n# Y\n", encoding="utf-8")
    res = AR.commit_result(str(repo / "Cards"), "agent:distill", "заход", True)
    assert res["ok"], res

    changes = [ln.split("\t") for ln in
               _git(repo, "show", "--name-status", "--format=", "HEAD").stdout.splitlines()]
    assert changes == [["A", card_rel]], f"в коммит агента попало чужое: {changes}"
    assert "160000 commit" in _git(repo, "ls-tree", "HEAD", "--", "Code/acme").stdout, \
        "gitlink-клон исчез из HEAD коммитом агента"
    assert _git(repo, "ls-tree", "HEAD", "--", "start-aurora.command").stdout.strip(), \
        "пусковой файл исчез из HEAD коммитом агента"


@test
def test_index_errors_are_failures_and_restore_the_human_index(tmp: Path):
    """(t) M-2: отказ `add`/`diff` не выглядит успехом, а индекс человека возвращается.

    Раньше отказ `diff --cached` превращался в пустой список, и чекпойнт отвечал
    «нечего фиксировать»; отказ `add` оставлял индекс после `untrack_local` изменённым.
    """
    from unittest.mock import patch
    import agent_runner as AR
    import git_sync as GS

    log_rel = "AuroraKnowledgeDB/meta/run_log.md"
    repo = _init(tmp / "repo")
    _write_block(repo)
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
    log = repo / log_rel
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    head = _commit(repo, "init")
    (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
    log.write_text("changed\n", encoding="utf-8")

    real_retry = AR.git_retry

    def fail_add(*args, **kw):
        if args[:1] == ("add",):
            return 1, "", "error: add refused"
        return real_retry(*args, **kw)

    with patch.object(AR, "git_retry", fail_add):
        got = AR.checkpoint(str(repo), "задача", True)
    assert got["ok"] is False and got["kind"] == "refused", got
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "отказ add сдвинул HEAD"
    assert _git(repo, "diff", "--cached", "--name-only").stdout.strip() == "", \
        "отказ add оставил индекс изменённым"
    assert _git(repo, "ls-files", "--", log_rel).stdout.strip(), "журнал снят с индекса без коммита"

    real_git = AR.git

    def fail_diff(*args, **kw):
        if args[:2] == ("diff", "--cached"):
            return 1, "", "error: diff failed"
        return real_git(*args, **kw)

    with patch.object(AR, "git", fail_diff):
        got = AR.checkpoint(str(repo), "задача", True)
    assert got["ok"] is False and "diff" in got["why"], got
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "отказ diff сдвинул HEAD"
    assert _git(repo, "diff", "--cached", "--name-only").stdout.strip() == "", \
        "отказ diff оставил индекс изменённым"

    real_run_git = GS.run_git

    def gs_fail_add(project, args, *a, **kw):
        if args[:1] == ["add"]:
            return 1, "", "error: add refused"
        return real_run_git(project, args, *a, **kw)

    with patch.object(GS, "run_git", gs_fail_add):
        res = GS.commit(str(repo))
    assert res["ok"] is False and res["problem"], res
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "отказ add в панели сдвинул HEAD"

    def gs_fail_diff(project, args, *a, **kw):
        if args[:2] == ["diff", "--cached"]:
            return 1, "", "error: diff failed"
        return real_run_git(project, args, *a, **kw)

    with patch.object(GS, "run_git", gs_fail_diff):
        res = GS.commit(str(repo))
    assert res["ok"] is False and res["problem"], res
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "отказ diff в панели сдвинул HEAD"

    def fail_status(*args, **kw):
        if args[:1] == ("status",):
            return 128, "", "fatal: status failed"
        return real_git(*args, **kw)

    with patch.object(AR, "git", fail_status):
        got = AR.checkpoint(str(repo), "задача", True)
    assert got["ok"] is False and "status" in got["why"], got

    def gs_fail_status(project, args, *a, **kw):
        if args[:1] == ["status"]:
            return 128, "", "fatal: status failed"
        return real_run_git(project, args, *a, **kw)

    with patch.object(GS, "run_git", gs_fail_status):
        res = GS.commit(str(repo))
    assert res["ok"] is False and res["problem"], res

    # commit_result: ошибка `diff --cached` не должна выглядеть как «изменены только локальные».
    card = repo / "AuroraKnowledgeDB/Concepts/z.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\ntype: concept\n---\n# Z\n", encoding="utf-8")

    def fail_quiet(*args, **kw):
        if args[:2] == ("diff", "--cached") and "--quiet" in args:
            return 128, "", "fatal: diff failed"
        return real_git(*args, **kw)

    with patch.object(AR, "git", fail_quiet):
        res = AR.commit_result(str(repo), "agent:distill", "заход", True)
    assert res["ok"] is False and "diff" in res["why"], res
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, "отказ diff сдвинул HEAD"


@test
def test_gitignore_that_is_not_utf8_keeps_the_user_rules(tmp: Path):
    """(h) B-1: cp1251 `.gitignore` не затирается блоком кита и не теряет байты.

    Раньше `UnicodeDecodeError` принимался за пустой файл: `ensure_gitignore` писал поверх
    весь блок кита, и правила человека исчезали (проба ревью: 5424 байта вместо трёх строк).
    Файл читается и пишется байтами (`surrogateescape`): строки человека остаются как были,
    правила кита дописываются — без них локальные пути уехали бы в первый же коммит.
    """
    lp = _lp()
    repo = _init(tmp / "repo")
    ignore = repo / ".gitignore"
    mine = "# мои правила\n*.tmp\n/секрет/\n".encode("cp1251")
    ignore.write_bytes(mine)

    added = lp.ensure_gitignore(ignore)
    data = ignore.read_bytes()
    assert data.startswith(mine.rstrip()), "чужие строки .gitignore не сохранены байт в байт"
    text = data.decode("cp1251")
    assert "*.tmp" in text and "/секрет/" in text, "правила человека потеряны"
    assert b"\xef\xbf\xbd" not in data, "байты cp1251 заменены на U+FFFD"
    assert "AuroraKnowledgeDB/meta/run_log.md" in added, added
    assert "AuroraKnowledgeDB/meta/run_log.md" in text, "правила кита не дописаны"
    assert lp.missing_rules(repo) == [], "доктор считает готовый .gitignore пустым"
    assert lp.ensure_gitignore(ignore) == [], "повторный прогон дописывает правила заново"
    assert ignore.read_bytes() == data, "повторный прогон тронул байты"

    import git_sync as GS

    _commit(repo, "rules")
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("card\n", encoding="utf-8")
    log = repo / "AuroraKnowledgeDB/meta/run_log.md"
    log.parent.mkdir(parents=True)
    log.write_text("local\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    res = GS.commit(str(repo))
    assert res["ok"], res
    assert _names(repo) == ["Cards/a.md"], ("в коммит попал локальный путь", _names(repo))
    assert not _git(repo, "ls-files", "--", "AuroraKnowledgeDB/meta/run_log.md").stdout.strip(), \
        "локальный путь остался в индексе"
    assert log.is_file(), "файл на диске тронут"
    assert ignore.read_bytes() == data, "коммит переписал cp1251 .gitignore"


@test
def test_user_negation_does_not_return_local_paths_to_the_commit(tmp: Path):
    """(p) B-2: `!AuroraKnowledgeDB/meta/run_log.md` после блока не обходит правила.

    `ensure_gitignore` видел текст правила и считал его установленным, а `untrack_local` не
    видел новый файл в индексе; `add` добавлял журнал благодаря отрицанию. После `add`
    проверяется фактический индекс по политике реестра, а doctor называет правило перекрытым,
    а не «правил нет».
    """
    import agent_runner as AR
    import git_sync as GS

    lp = _lp()
    log_rel = "AuroraKnowledgeDB/meta/run_log.md"
    for kind in ("checkpoint", "git_sync", "commit_result"):
        repo = _init(tmp / kind)
        _write_block(repo)
        with (repo / ".gitignore").open("a", encoding="utf-8") as f:
            f.write(f"!{log_rel}\n")
        (repo / "Cards").mkdir()
        (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
        _commit(repo, "init")
        log = repo / log_rel
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("local-new\n", encoding="utf-8")

        if kind == "checkpoint":
            (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
            res = AR.checkpoint(str(repo), "задача", True)
            assert res["ok"], (kind, res)
        elif kind == "git_sync":
            (repo / "Cards/a.md").write_text("human\n", encoding="utf-8")
            res = GS.commit(str(repo))
            assert res["ok"], (kind, res)
        else:
            card = repo / "AuroraKnowledgeDB/Concepts/x.md"
            card.parent.mkdir(parents=True, exist_ok=True)
            card.write_text("---\ntype: concept\n---\n# X\n", encoding="utf-8")
            res = AR.commit_result(str(repo), "agent:distill", "заход", True)
            assert res["ok"], (kind, res)

        committed = _names(repo)
        assert log_rel not in committed, (kind, "журнал уехал в коммит", committed)
        assert not _git(repo, "ls-files", "--", log_rel).stdout.strip(), \
            (kind, "журнал остался в индексе")
        assert log.read_text(encoding="utf-8") == "local-new\n", (kind, "файл на диске тронут")

        found = lp.findings(repo)
        assert found["checked"] is True, (kind, found)
        assert log_rel in found["overridden"], (kind, "doctor не увидел перекрытое правило", found)
        assert log_rel not in found["missing_rules"], (kind, "перекрытое правило названо отсутствующим", found)

        cp = run("aurora_doctor.py", cwd=repo)
        text = cp.stdout + cp.stderr
        assert "правила локальных путей перекрыты" in text, (kind, text[-2000:])


@test
def test_doctor_tells_unchecked_from_clean_and_overridden(_tmp: Path):
    """(q) B-2: doctor различает три состояния и не подменяет отказ проверки успехом."""
    import importlib
    import inspect

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    AD = importlib.import_module("aurora_doctor")
    src = inspect.getsource(AD.check_local_paths)
    assert "except Exception" not in src, "doctor снова глотает любую ошибку проверки как «реестра нет»"

    class Stub:
        @staticmethod
        def findings(_root):
            return {"tracked": [], "missing_rules": [], "overridden": [],
                    "checked": False, "detail": "индекс git не читается"}

    original = AD._lp
    AD._lp = lambda: Stub
    try:
        errors, warns = AD.check_local_paths()
    finally:
        AD._lp = original
    assert errors == [], errors
    assert len(warns) == 1 and "проверка не выполнена" in warns[0], warns
    assert "недоступен доктору" not in warns[0], warns


@test
def test_findings_keeps_clean_and_names_every_unchecked_reason(tmp: Path):
    """(q) B-2: «чисто» и «проверка не выполнена» не смешиваются, у отказа своя причина.

    Четыре ветки отказа: индекс не читается (проект без git), `.gitignore` не читается
    (на его месте каталог), `git check-ignore` не ответил и индекс не перечитан после
    очистки. Во всех `checked=False`, пустые списки — не «чисто», а причина — известный
    текст `REASONS`. Чистая проверка наоборот говорит `checked=True` и причины не имеет.
    """
    lp = _lp()
    nogit = tmp / "no-git"
    nogit.mkdir()
    got = lp.findings(nogit)
    assert got["checked"] is False and got["detail"] == lp.REASONS["index_unreadable"], got
    assert got["tracked"] == got["missing_rules"] == got["overridden"] == [], got

    clean = _init(tmp / "clean")
    _write_block(clean)
    got = lp.findings(clean)
    assert got["checked"] is True and got["detail"] == "", got
    assert got["tracked"] == got["missing_rules"] == got["overridden"] == [], got

    no_ignore = _init(tmp / "no-ignore")
    (no_ignore / ".gitignore").mkdir()
    got = lp.findings(no_ignore)
    assert got["checked"] is False and got["detail"] == lp.REASONS["gitignore_unreadable"], got
    assert got["tracked"] == got["overridden"] == [], got

    no_probe = _init(tmp / "no-probe")
    _write_block(no_probe)
    real_git, real_entries = lp._git, lp._index_entries

    def failing_probe(root, *args, **kw):
        if args[:1] == ("check-ignore",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: check-ignore failed")
        return real_git(root, *args, **kw)

    lp._git = failing_probe
    try:
        got = lp.findings(no_probe)
    finally:
        lp._git = real_git
    assert got["checked"] is False and got["detail"] == lp.REASONS["check_ignore_failed"], got

    # `enforce_index` читает индекс до очистки; отказ второго чтения — тот же отказ проверки.
    calls = {"n": 0}

    def reread_once(base, *args, **kw):
        calls["n"] += 1
        return real_entries(base) if calls["n"] == 1 else None

    lp._index_entries = reread_once
    try:
        got = lp.enforce_index(no_probe)
    finally:
        lp._index_entries = real_entries
    assert got["checked"] is False and got["detail"] == lp.REASONS["index_not_reread"], got


@test
def test_local_path_reasons_are_translated_at_the_output_boundary(_tmp: Path):
    """Причины непроверенных локальных путей переводятся по каталогу, а не остаются русскими.

    Доктор подставляет русскую причину в шаблон предупреждения; английская панель
    переводит вложенную причину на границе вывода (`translate_message`). Каждая причина
    есть в `cockpit/i18n/data/en.json` → `messages` и в образцах `kit_i18n`, иначе
    `kit:i18n --check` назвал бы перевод лишним; русский вывод не меняется.
    """
    import importlib
    import json
    import re

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    if str(KIT / "cockpit") not in sys.path:
        sys.path.insert(0, str(KIT / "cockpit"))
    lp = _lp()
    KI = importlib.import_module("kit_i18n")
    AD = importlib.import_module("aurora_doctor")
    ck = importlib.import_module("aurora_cockpit")
    catalog = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))["messages"]
    skeletons = KI.message_skeletons()

    class Stub:
        detail = ""

        @classmethod
        def findings(cls, _root):
            return {"tracked": [], "missing_rules": [], "overridden": [],
                    "checked": False, "detail": cls.detail}

    original = AD._lp
    ck._MATCHERS.clear()
    try:
        # Последняя пара — реестр не назвал причину: доктор говорит запасным текстом.
        for detail, reason in [*((r, r) for r in lp.REASONS.values()), ("", AD.UNKNOWN_REASON)]:
            assert reason in catalog, f"у причины нет перевода: {reason!r}"
            assert reason in skeletons, f"причина не собирается из кода: {reason!r}"
            assert not re.search("[а-яА-ЯёЁ]", catalog[reason]), (reason, catalog[reason])
            Stub.detail = detail
            AD._lp = lambda: Stub
            errors, warns = AD.check_local_paths()
            assert errors == [] and len(warns) == 1, (reason, errors, warns)
            en = ck.translate_message(warns[0], "en")
            assert catalog[reason] in en, (reason, en)
            assert not re.search("[а-яА-ЯёЁ]", en), f"в английском остался русский текст: {en!r}"
            assert ck.translate_message(warns[0], "ru") == warns[0], "русский вывод изменился"
    finally:
        AD._lp = original
        ck._MATCHERS.clear()


@test
def test_unreadable_gitignore_is_left_alone(tmp: Path):
    """(i) B-1: `.gitignore` не прочитать — не пишем в него, чужие правила целы."""
    lp = _lp()
    target = tmp / ".gitignore"
    target.mkdir()                      # на месте файла каталог: read_bytes → OSError
    (target / "mine.txt").write_text("моё\n", encoding="utf-8")
    assert lp.ensure_gitignore(target) == [], "в нечитаемый путь записали блок кита"
    assert (target / "mine.txt").read_text(encoding="utf-8") == "моё\n", "содержимое потеряно"


@test
def test_gitignore_keeps_bytes_mode_and_never_writes_through_a_symlink(tmp: Path):
    """(r) M-5: чужой `.gitignore` сохраняется байт в байт, симлинк и сбой записи не трогают цель."""
    lp = _lp()
    repo = _init(tmp / "bytes")
    ignore = repo / ".gitignore"
    # Последнее правило — экранированный пробел: `rstrip()` съедал его и менял смысл строки.
    mine = b"# \xd0\xbc\xd0\xbe\xd0\xb8 \xd0\xbf\xd1\x80\xd0\xb0\xd0\xb2\xd0\xb8\xd0\xbb\xd0\xb0\nsecret\\ \n"
    ignore.write_bytes(mine)
    os.chmod(ignore, 0o640)
    lp.ensure_gitignore(ignore)
    data = ignore.read_bytes()
    assert data.startswith(mine), f"конец чужого файла изменён: {data[:60]!r}"
    assert b"AuroraKnowledgeDB/meta/run_log.md" in data, "правила кита не дописаны"
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o640, "права чужого .gitignore изменены"

    # Симлинк: правила не дописываются в файл за пределами проекта.
    ext = tmp / "outside.gitignore"
    ext.write_text("# снаружи\n", encoding="utf-8")
    outward = _init(tmp / "link")
    link = outward / ".gitignore"
    link.symlink_to(ext)
    assert lp.ensure_gitignore(link) == [], "правила записаны через симлинк"
    assert ext.read_text(encoding="utf-8") == "# снаружи\n", "внешний файл изменён"
    assert link.is_symlink(), "симлинк заменён обычным файлом"

    # Сбой атомарной записи не переходит к прямой перезаписи.
    target = repo / "fresh.gitignore"
    target.write_text("# мои правила\n", encoding="utf-8")
    import aurora_fs
    real = aurora_fs.write_atomic
    calls: list = []

    def broken(path, payload, **kw):
        calls.append(kw)
        raise OSError(28, "на диске нет места")

    aurora_fs.write_atomic = broken
    try:
        try:
            lp.ensure_gitignore(target)
        except OSError:
            pass
        else:
            raise AssertionError("отказ атомарной записи не дошёл до вызывающего")
    finally:
        aurora_fs.write_atomic = real
    assert target.read_text(encoding="utf-8") == "# мои правила\n", \
        "при сбое атомарной записи файл переписан напрямую"
    assert calls and calls[0].get("follow_links") is False, calls


@test
def test_plain_file_under_code_is_not_a_clone_and_names_are_quoted(tmp: Path):
    """(s) M-4: `Code/<файл>` без режима gitlink — работа человека; имя с пробелом экранируется."""
    lp = _lp()
    repo = _init(tmp / "plain")
    _write_block(repo)
    (repo / "Code").mkdir()
    (repo / "Code/notes.md").write_text("заметки человека\n", encoding="utf-8")
    (repo / "Code/README.md").write_text("клоны\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "plain file under Code")
    assert lp.code_clones(repo) == [], "обычный файл Code/notes.md назван клоном"
    assert lp.untrack_local(repo) == [], "обычный файл Code/notes.md снят с индекса"
    assert _git(repo, "ls-files", "--", "Code/notes.md").stdout.strip(), "файл человека ушёл из индекса"
    cp = run("aurora_doctor.py", cwd=repo)
    text = cp.stdout + cp.stderr
    assert "клоны кода лежат в индексе" not in text, text[-1500:]

    clone_repo = _init(tmp / "clone")
    _write_block(clone_repo)
    inner = _init(clone_repo / "Code/my clone")
    (inner / "a.py").write_text("print(1)\n", encoding="utf-8")
    _commit(inner, "clone")
    (clone_repo / "Code/README.md").write_text("клоны\n", encoding="utf-8")
    (clone_repo / "Code/.gitkeep").write_text("", encoding="utf-8")
    _git(clone_repo, "add", "-A", "-f")
    _commit(clone_repo, "clone tracked")

    assert lp.code_clones(clone_repo) == ["Code/my clone"], "gitlink с пробелом не распознан"
    cp = run("aurora_doctor.py", cwd=clone_repo)
    text = cp.stdout + cp.stderr
    assert "клоны кода лежат в индексе git проекта: Code/my clone" in text, text[-1500:]
    assert "git rm -r --cached -f ':(literal)Code/my clone'" in text, f"команда разбивает имя с пробелом: {text[-1500:]}"
    assert lp.untrack_local(clone_repo) == ["Code/my clone"], "клон с пробелом не снят"
    assert (inner / "a.py").read_text(encoding="utf-8") == "print(1)\n" and (inner / ".git").is_dir(), \
        "клон тронут на диске"


@test
def test_real_clone_gitlink_leaves_the_index(tmp: Path):
    """(j) M-1: настоящий клон (`git clone` → gitlink `Code/<имя>`) снимается, doctor даёт `-f`.

    `git ls-files` показывает gitlink одной записью `Code/acme` без второго слэша; прежнее
    распознавание требовало слэш и не видело самый обычный клон `code:sync`. `git rm --cached`
    без `-f` на gitlink отказывает, поэтому doctor советовал невыполнимую команду.
    """
    lp = _lp()
    repo = _init(tmp / "repo")
    _write_block(repo)
    inner = _init(repo / "Code/acme")   # настоящий вложенный репозиторий, не каталог с файлами
    (inner / "a.py").write_text("print(1)\n", encoding="utf-8")
    _commit(inner, "clone content")
    (repo / "Code/README.md").write_text("клоны\n", encoding="utf-8")
    (repo / "Code/.gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "clone got into the index")
    assert _git(repo, "ls-files", "--", "Code").stdout.split() == \
        ["Code/.gitkeep", "Code/README.md", "Code/acme"], "клон не gitlink"
    listed = _git(repo, "ls-files", "-s", "--", "Code").stdout.splitlines()
    assert [ln.split("\t")[-1] for ln in listed if ln.startswith("160000 ")] == ["Code/acme"], \
        f"клон лежит не ссылкой на вложенный репозиторий: {listed}"
    # Клон ушёл вперёд после того, как попал в индекс: без `-f` git такую ссылку не снимает.
    (inner / "a.py").write_text("print(2)\n", encoding="utf-8")
    _commit(inner, "clone moved on")

    cp = run("aurora_doctor.py", cwd=repo)
    text = cp.stdout + cp.stderr
    assert "клоны кода лежат в индексе git проекта: Code/acme" in text, text[-1500:]
    assert "git rm -r --cached -f ':(literal)Code/acme'" in text, f"doctor дал невыполнимую команду:\n{text[-1500:]}"

    assert lp.code_clones(repo) == ["Code/acme"], "gitlink-клон не распознан"
    removed = lp.untrack_local(repo)
    assert removed == ["Code/acme"], removed
    assert _git(repo, "ls-files", "--", "Code").stdout.split() == ["Code/.gitkeep", "Code/README.md"], \
        "описание папки и её пометка ушли из индекса вместе с клоном"
    assert (inner / "a.py").read_text(encoding="utf-8") == "print(2)\n" \
        and (inner / ".git").is_dir(), "клон тронут на диске"
    assert lp.untrack_local(repo) == [], "повторное снятие снова меняет индекс"


@test
def test_staged_local_file_never_enters_the_commit(tmp: Path):
    """(k) M-2: уникальный stage сохраняется, а безопасная очистка не коммитит локальное."""
    import git_sync as GS

    local_rel = "AuroraKnowledgeDB/meta/run_log.md"
    repo = _init(tmp / "repo")
    _write_block(repo)
    (repo / "Cards").mkdir()
    (repo / "Cards/a.md").write_text("old\n", encoding="utf-8")
    log = repo / local_rel
    log.parent.mkdir(parents=True)
    log.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "init")
    log.write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "-f", "--", local_rel)      # индекс расходится с HEAD
    log.write_text("staged and edited\n", encoding="utf-8")   # и с файлом на диске
    (repo / "Cards/a.md").write_text("new card\n", encoding="utf-8")

    head = _git(repo, "rev-parse", "HEAD").stdout
    index = (repo / ".git/index").read_bytes()
    res = GS.commit(str(repo))
    assert res["ok"] is False and local_rel in str(res), res
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert (repo / ".git/index").read_bytes() == index
    assert _git(repo, "show", f":{local_rel}").stdout == "staged\n"
    assert log.read_text(encoding="utf-8") == "staged and edited\n", "файл на диске тронут"

    # Человек вернул выбранную версию и на диск: единственная копия уже не в stage.
    log.write_text("staged\n", encoding="utf-8")
    res = GS.commit(str(repo))
    assert res["ok"], res
    assert res["files"] == 1, res
    assert _names(repo) == ["Cards/a.md"], ("коммит назвал локальный путь", _names(repo))
    assert not _git(repo, "ls-files", "--", local_rel).stdout.strip(), "локальный путь остался в индексе"
    assert _git(repo, "show", f"HEAD:{local_rel}").stdout == "old\n"
    assert log.read_text(encoding="utf-8") == "staged\n", "файл на диске тронут"


@test
def test_fallback_literals_match_the_registry(_tmp: Path):
    """(l) m-1: встроенные литералы отката не разошлись с реестром.

    Они работают, когда `local_paths.py` рядом нет (оборванное обновление, откат). Дрейф
    прошёл бы молча: тест (a) сравнивает атрибут установщика, который уже резолвится в
    реестр, а не сам литерал.
    """
    lp = _lp()
    import aurora_update as U
    import install_aurora as IA

    assert IA._LITERAL_GITIGNORE_BLOCK == lp.gitignore_block(), \
        "встроенный блок установщика разошёлся с реестром: правьте их вместе"
    assert list(U._LOCAL_STATE_RELS_LITERAL) == list(lp.LOCAL_STATE_RELS), \
        f"встроенный список обновления разошёлся с реестром: {U._LOCAL_STATE_RELS_LITERAL} против {lp.LOCAL_STATE_RELS}"
    assert list(U._LOCAL_STATE_DIRS_LITERAL) == list(lp.LOCAL_STATE_DIRS), \
        "встроенные каталоги обновления разошлись с реестром"


@test
def test_team_data_stays_in_git_after_the_cleanup(tmp: Path):
    """(e) AC-6: телеметрия команды и общие папки не закрываются и не снимаются."""
    lp = _lp()
    repo = _init(tmp / "repo")
    _write_block(repo)
    for rel in KEEP_IN_GIT:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "team data")
    _create_local(repo, "2")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "local state")

    lp.ensure_gitignore(repo / ".gitignore")
    lp.untrack_local(repo)
    _git(repo, "add", "-A")
    tracked = set(_git(repo, "ls-files").stdout.split())
    for rel in KEEP_IN_GIT:
        assert rel in tracked, f"общие данные ушли из git: {rel}"

    checked = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off",
                              "check-ignore", "--no-index", "--stdin"],
                             input="\n".join(KEEP_IN_GIT) + "\n", capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    assert checked.stdout.strip() == "", f"общие данные закрыты правилом кита: {checked.stdout}"


@test
def test_doctor_names_local_paths_in_the_index(tmp: Path):
    """(f) AC-5: doctor показывает путь, безопасную команду и не дублирует его."""
    lp = _lp()
    repo = _init(tmp / "repo")
    _write_block(repo)
    log = repo / "AuroraKnowledgeDB/meta/run_log.md"
    log.parent.mkdir(parents=True)
    log.write_text("log\n", encoding="utf-8")
    _git(repo, "add", "-A", "-f")
    _commit(repo, "journal tracked")

    found = lp.findings(repo)
    assert "AuroraKnowledgeDB/meta/run_log.md" in found["tracked"], found
    assert found["missing_rules"] == [], found
    again = lp.findings(repo)
    assert again["tracked"] == found["tracked"], "повторный вызов меняет вывод"
    assert len(found["tracked"]) == len(set(found["tracked"])), "путь назван дважды"

    src = (SCRIPTS / "aurora_doctor.py").read_text(encoding="utf-8")
    assert "findings(" in src, "doctor не спрашивает реестр локальных путей"
    assert src.count("check-ignore") == 1, \
        "у doctor появился второй источник правды о .gitignore: решать должен реестр"

    cp = run("aurora_doctor.py", cwd=repo)
    text = cp.stdout + cp.stderr
    # Один путь — одна строка находки: повторный прогон не должен плодить дубли.
    named = [ln for ln in text.splitlines() if "AuroraKnowledgeDB/meta/run_log.md" in ln]
    assert len(named) == 1, named
    assert "git rm -r --cached -f -q" in text, "doctor не дал безопасной команды исправления"


@test
def test_modules_work_without_the_registry(tmp: Path):
    """(g) AC-1/B-3: без local_paths.py импорт и update работают, пишущие коммиты отказывают.

    Пишущий потребитель без реестра не знает, что снимать с индекса, — `add` мог бы вернуть
    в коммит изменённый журнал. Отказ обязан случиться до изменения индекса, а не после.
    """
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
import git_sync, agent_runner, aurora_doctor, aurora_update, install_aurora
assert aurora_update.LOCAL_STATE_RELS, "встроенный список локальных путей потерян"
assert aurora_update.LOCAL_STATE_DIRS, "встроенные каталоги потеряны"
assert ".aurora/" in install_aurora.GITIGNORE_BLOCK.splitlines(), "встроенный блок потерян"
err, warn = aurora_doctor.check_local_paths()
assert err == [] and len(warn) == 1 and "не проверены" in warn[0], (err, warn)

import pathlib, subprocess, tempfile
base = pathlib.Path(tempfile.mkdtemp())
added = install_aurora.merge_gitignore(base / ".gitignore")
assert "AuroraKnowledgeDB/meta/run_log.md" in added, added
repo = base / "repo"
repo.mkdir()
for args in (["init", "-q"], ["config", "user.name", "T"], ["config", "user.email", "t@example.com"]):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
log = repo / "AuroraKnowledgeDB/meta/run_log.md"
log.parent.mkdir(parents=True)
log.write_text("x")
subprocess.run(["git", "-C", str(repo), "add", "-A", "-f"], check=True, capture_output=True)

# B-3: пишущие потребители без реестра отказывают ДО изменения индекса.
card = repo / "Cards/b.md"
card.parent.mkdir(parents=True)
card.write_text("human")
log.write_text("changed")

def index_state():
    return subprocess.run(["git", "-C", str(repo), "ls-files", "-s", "-z"],
                          check=True, capture_output=True).stdout

before = index_state()
cp = agent_runner.checkpoint(str(repo), "задача", True)
assert cp["ok"] is False and "реестр локальных путей" in cp["why"], cp
assert index_state() == before, "чекпойнт изменил индекс без реестра"
res = agent_runner.commit_result(str(repo), "задача", "заголовок", True)
assert res["ok"] is False and "реестр локальных путей" in res["why"], res
assert index_state() == before, "commit_result изменил индекс без реестра"
res = git_sync.commit(str(repo))
assert res["ok"] is False and res["problem"]["code"] == "no_local_paths", res
assert index_state() == before, "commit панели изменил индекс без реестра"

clean = aurora_update.clean_local_state(repo)
assert not clean["checked"] and not clean["removed"], clean
assert index_state() == before, "fallback обновления отменил уникальную staged-версию"
assert log.read_text() == "changed", "fallback затронул рабочую версию"
print("FALLBACK-OK")
"""
    cp = subprocess.run([sys.executable, "-c", code.replace("@@SCRIPTS@@", repr(str(SCRIPTS)))],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "FALLBACK-OK" in cp.stdout, cp.stdout

    registry = (KIT / "tests" / "run_tests.py").read_text(encoding="utf-8")
    assert "'part_278', 'part_279'" in registry, "part_279 не подключён после part_278"


@test
def test_doctor_main_without_the_registry_warns_instead_of_crashing(tmp: Path):
    """(m) m-6: полный `main` доктора без `local_paths.py` не падает и называет причину.

    Тест (g) зовёт только `check_local_paths`; остальной `main` (схема, git, конфиг)
    мог импортировать реестр по-своему и упасть на старом проекте. Проверяем целиком.
    """
    repo = _init(tmp / "repo")
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
import aurora_doctor
sys.argv = ["aurora_doctor.py"]
rc = aurora_doctor.main()
assert rc in (0, 1), rc
print("DOCTOR-FALLBACK-OK")
"""
    cp = subprocess.run([sys.executable, "-c", code.replace("@@SCRIPTS@@", repr(str(SCRIPTS)))],
                        cwd=str(repo), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, out
    assert "DOCTOR-FALLBACK-OK" in cp.stdout, out
    assert "правила не проверены" in out, out
    assert out.count("недоступен доктору") == 1, f"о недоступном реестре сказано не один раз: {out}"
    assert "работаю по встроенному списку" not in out, \
        f"загрузчик реестра дублирует сообщение доктора: {out}"
