"""Проверки движка Aurora, часть 116. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 4 «Записи в Raw/», пакет CT-15: перенос рукописных записей из `Decisions/`, `Questions/`, `Reference/` в
`Raw/` командой `kb:record --migrate`. Сухой прогон печатает план, `--apply` делает `git mv`, переписывает шапку по таблице
копируемых полей и строит зеркала; повтор ничего не делает; зеркала, карточки с живым `sources` и внутренний вид `template`
не переносятся; занятый целевой путь — отказ без потерь. Перенос закрывает исключения «записи, ждущие переноса в Raw/»
завершения миграции на шаблоны (`kb:conform --finalize`). Проекты и репозитории — только во временных папках.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from cases import part_114 as P114
from cases import part_70 as P70
from harness import run, test

KB = "AuroraKnowledgeDB"
_put, _read, _mod = P114._put, P114._read, P114._mod

LEGACY_DR = """\
---
title: "DR-0001: Срок подтверждения заявки"
aliases: ["DR-0001"]
tags: [decision]
type: decision
status: accepted
date: 2026-09-20
owner: "@analyst"
supersedes: []
superseded_by:
related: ["[Заявка на поставку](Заявка на поставку.md)"]
kind: knowledge
trust_checked: 2026-09-21
trust_basis: "прежний прогон"
distilled: 2026-09-21
schema_version: 3
created: 2026-09-20
updated: 2026-10-01
---

# DR-0001: Срок подтверждения заявки

## Контекст
Срок в ТЗ и в описании процесса разный. См. [[Заявка на поставку]].

## Решение
Три рабочих дня.
"""
LEGACY_REF = """\
---
title: "Сокращения проекта"
aliases: ["SPR-001"]
tags: [справочник]
type: reference
status: knowledge
built_by: "старый прогон"
created: 2026-08-01
updated: 2026-09-01
---

# Сокращения проекта

| Сокращение | Расшифровка |
|---|---|
| ТЗ | Техническое задание |
"""


def _legacy(tmp: Path) -> Path:
    """Проект под git: два рукописных решения (одно со статусом вне цикла), справочник без владельца, вопрос с живым источником,
    зеркало, выведенный справочник, карточка `kind: template`."""
    root = P114._base(tmp)
    _put(root, f"{KB}/Decisions/DR-0001-срок.md", LEGACY_DR)
    _put(root, f"{KB}/Decisions/DR-0002-странное.md",
         LEGACY_DR.replace("DR-0001", "DR-0002").replace("status: accepted", "status: wip"))
    _put(root, f"{KB}/Reference/Сокращения.md", LEGACY_REF)
    # не переносятся: выведенный справочник, зеркало, карточка с живым источником, внутренний вид
    _put(root, f"{KB}/Reference/Выведенный.md", '---\ntitle: Выведенный\ntype: reference\nkind: dictionary\nstatus: knowledge\n'
         'built: machine\nsources:\n  - "Sources/Confluence/Нет.md"\n---\n\n# Выведенный\n\n| а | б |\n|---|---|\n')
    _put(root, "Raw/decisions/DR-0003-зеркало.md", P114.decision(3, "зеркало")[1])
    _put(root, "Raw/customer/Письмо.md", "Письмо заказчика.\n")
    _put(root, f"{KB}/Questions/Q-001-с-источником.md",
         '---\ntitle: "Q-001: С источником"\ntype: question\nstatus: draft\nq_id: Q-001\nq_status: answered\nanswered: 2026-10-01\n'
         'answer_source: "Raw/customer/Письмо.md"\nsources:\n  - "Raw/customer/Письмо.md"\n---\n\n# Q-001: С источником\n\nОтвет в письме.\n')
    _put(root, f"{KB}/Decisions/DR-0004-форма.md", "---\ntitle: Форма\ntype: decision\nkind: template\nstatus: proposed\n---\n\n# Форма\n")
    run("kb_record.py", "--mirror", "--apply", "--allow-dirty", cwd=root)         # зеркало DR-0003
    P70._commit(root)
    return root


def _migrate(root: Path, *args, expect=None):
    return run("kb_record.py", "--migrate", *args, cwd=root, expect_rc=expect)


def _git(root: Path, *args) -> str:
    return P70._git(root, "-c", "core.quotepath=false", *args)


# ------------------------------------------------------------------ AC-CT-15.1

@test
def test_migrate_dry_run_prints_the_plan_and_writes_nothing(tmp: Path):
    """AC-CT-15.1: сухой прогон называет, что и куда переедет, чего не тронет; дерево и git не меняются."""
    root = _legacy(tmp)
    before = _git(root, "status", "--porcelain")
    cp = _migrate(root, expect=1)
    out = cp.stdout
    assert "(dry-run)" in out and "переносится: 3" in out, out
    for src, dst in ((f"{KB}/Decisions/DR-0001-срок.md", "Raw/decisions/DR-0001-срок.md"),
                     (f"{KB}/Decisions/DR-0002-странное.md", "Raw/decisions/DR-0002-странное.md"),
                     (f"{KB}/Reference/Сокращения.md", "Raw/reference/Сокращения.md")):
        assert f"{src} → {dst}" in out, out
    assert "вне цикла решения" in out and "владелец справочника неизвестен" in out, out
    moves = [ln for ln in out.splitlines() if " → " in ln]
    assert len(moves) == 3, moves
    for kept in ("Выведенный", "DR-0003", "Q-001-с-источником", "DR-0004"):
        assert not any(kept in ln for ln in moves), (kept, moves)
    assert _git(root, "status", "--porcelain") == before and not (root / "Raw/decisions/DR-0001-срок.md").exists()
    assert (root / KB / "Decisions" / "DR-0001-срок.md").read_text(encoding="utf-8") == LEGACY_DR.replace("\r\n", "\n")


@test
def test_migrate_apply_moves_with_git_converts_the_head_and_builds_mirrors(tmp: Path):
    """AC-CT-15.1: `--apply` — `git mv` в `Raw/…`, шапка мастера по таблице полей, тело побайтно, зеркала построены."""
    root = _legacy(tmp)
    old_body = LEGACY_DR.split("\n---\n", 1)[1]
    cp = _migrate(root, "--apply", expect=1)                    # 1: статус вне цикла и пропуски называются
    assert "Записано:" in cp.stdout, cp.stdout
    master = _read(root, "Raw/decisions/DR-0001-срок.md")
    A = _mod("aurora_common")
    fm = A.frontmatter(master)
    assert fm["template"] == "akdb_decision_v1.0.md" and fm["type"] == "decision" and fm["status"] == "accepted", fm
    assert fm["owner"] == "@analyst" and fm["date"] == "2026-09-20" and fm["created"] == "2026-09-20", fm
    for dropped in ("related", "kind", "trust_checked", "trust_basis", "distilled", "schema_version", "built"):
        assert dropped not in fm, f"поле движка «{dropped}» попало в мастер: {fm}"
    assert master.split("\n---\n", 1)[1] == old_body, "тело переехало не побайтно"
    # git видит перенос (история сохраняется), прежнего файла нет
    porcelain = _git(root, "status", "--porcelain")
    assert re.search(r"(?m)^R.? +AuroraKnowledgeDB/Decisions/DR-0001-срок\.md -> Raw/decisions/DR-0001-срок\.md", porcelain), porcelain
    assert not (root / KB / "Decisions" / "DR-0001-срок.md").is_file() or "built: machine" in _read(root, f"{KB}/Decisions/DR-0001-срок.md")
    # справочник: вид dictionary, владелец пуст, поля движка сняты
    ref = A.frontmatter(_read(root, "Raw/reference/Сокращения.md"))
    assert ref["kind"] == "dictionary" and "owner" not in ref and "built_by" not in ref, ref
    # зеркала на прежних местах
    mirror = _read(root, f"{KB}/Decisions/DR-0001-срок.md")
    mfm = A.frontmatter(mirror)
    assert mfm["built"] == "machine" and A.card_sources(mirror) == ["Raw/decisions/DR-0001-срок.md"] and mfm["kind"] == "document", mfm
    assert mirror.split("\n---\n", 1)[1] == old_body
    assert A.frontmatter(_read(root, f"{KB}/Reference/Сокращения.md"))["kind"] == "dictionary"
    # статус вне цикла остался как есть; зеркала такого решения нет (шаблон не принял), в отчёте названо
    assert A.frontmatter(_read(root, "Raw/decisions/DR-0002-странное.md"))["status"] == "wip"
    assert "DR-0002-странное" in cp.stdout and "V-06" in cp.stdout, cp.stdout
    assert not (root / KB / "Decisions" / "DR-0002-странное.md").exists()
    # `kb:record --check` называет то, что осталось поправить, а рукописного в базе больше нет
    chk = run("kb_record.py", "--check", cwd=root)
    assert "DR-0002-странное" in chk.stdout and "V-06" in chk.stdout and "рукописная карточка в базе" not in chk.stdout, chk.stdout


@test
def test_migrate_leaves_alone_mirrors_derived_cards_live_sources_and_forms(tmp: Path):
    """AC-CT-15.3: `built: machine`, живой `sources` и `kind: template` не переносятся; выведенное и зеркала на месте, байт в байт."""
    root = _legacy(tmp)
    keep = {rel: (root / rel).read_bytes() for rel in (f"{KB}/Reference/Выведенный.md", f"{KB}/Decisions/DR-0003-зеркало.md",
                                                       f"{KB}/Questions/Q-001-с-источником.md", f"{KB}/Decisions/DR-0004-форма.md")}
    _migrate(root, "--apply")
    for rel, data in keep.items():
        assert (root / rel).read_bytes() == data, f"{rel}: тронуто"
    assert not (root / "Raw/questions/Q-001-с-источником.md").exists() and not (root / "Raw/decisions/DR-0004-форма.md").exists()
    assert sorted(p.name for p in (root / "Raw/decisions").glob("*.md")) == ["DR-0001-срок.md", "DR-0002-странное.md", "DR-0003-зеркало.md"]


@test
def test_migrate_is_idempotent(tmp: Path):
    """AC-CT-15.2: повторный запуск ничего не делает: ни переноса, ни записи, код 0."""
    root = _legacy(tmp)
    _migrate(root, "--apply")
    P70._commit(root)
    snapshot = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md") if ".git" not in p.parts}
    cp = _migrate(root, "--apply", expect=0)
    assert "переносится: 0" in cp.stdout and "делать нечего" in cp.stdout, cp.stdout
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md") if ".git" not in p.parts} == snapshot
    assert _migrate(root, expect=0).returncode == 0


@test
def test_migrate_refuses_a_busy_target_and_loses_nothing(tmp: Path):
    """AC-CT-15.4: одноимённый мастер уже есть — перенос этой карточки отказан, обе стороны на месте; остальные идут дальше."""
    root = _legacy(tmp)
    _put(root, "Raw/decisions/DR-0001-срок.md", P114.decision(1, "срок", status="proposed")[1])
    mine = _read(root, "Raw/decisions/DR-0001-срок.md")
    cp = _migrate(root, "--apply", expect=1)
    assert "уже есть — перенос отказан, ничего не потеряно" in cp.stdout and "DR-0001-срок" in cp.stdout, cp.stdout
    assert _read(root, "Raw/decisions/DR-0001-срок.md") == mine, "чужой мастер затёрт"
    assert (root / KB / "Decisions" / "DR-0001-срок.md").read_text(encoding="utf-8") == LEGACY_DR, "рукописная карточка потеряна"
    assert (root / "Raw/reference/Сокращения.md").is_file(), "остальные карточки не перенеслись"


@test
def test_migrate_needs_git_and_a_clean_tree(tmp: Path):
    """Без git перенос не выполняется; по грязному дереву — отказ, с `--allow-dirty` идёт; сухой прогон работает везде."""
    (tmp / "g").mkdir()
    root = P114._project(tmp / "g", git=True)
    _put(root, f"{KB}/Decisions/DR-0001-срок.md", LEGACY_DR)
    P70._commit(root)
    _put(root, f"{KB}/Decisions/DR-0001-срок.md", LEGACY_DR + "\nправка\n")
    cp = _migrate(root, "--apply", expect=2)
    assert "git-guard" in cp.stderr and not (root / "Raw/decisions/DR-0001-срок.md").exists()
    _migrate(root, "--apply", "--allow-dirty", expect=0)
    assert "правка" in _read(root, "Raw/decisions/DR-0001-срок.md"), "незакоммиченная правка потеряна"
    (tmp / "n").mkdir()
    nogit = P114._project(tmp / "n", git=False)
    _put(nogit, f"{KB}/Decisions/DR-0001-срок.md", LEGACY_DR)
    assert _migrate(nogit, expect=1).returncode == 1, "сухой прогон без git работает"
    cp = _migrate(nogit, "--apply", "--allow-dirty", expect=2)
    assert "не под git" in cp.stderr and (nogit / KB / "Decisions" / "DR-0001-срок.md").is_file()


@test
def test_migrate_closes_the_exceptions_of_the_conform_finalize(tmp: Path):
    """`kb:conform --finalize` называет рукописные записи исключением; после переноса записей, ждущих переноса, нет, миграцию можно завершить."""
    root = P114._project(tmp)
    _put(root, f"{KB}/Decisions/DR-0001-срок.md", LEGACY_DR)
    P70._commit(root)
    cp = run("kb_conform.py", "--finalize", cwd=root, expect_rc=0)
    assert "записи, ждущие переноса в Raw/: 1" in cp.stdout, cp.stdout
    run("kb_record.py", "--migrate", "--apply", cwd=root)
    P70._commit(root)
    run("kb_conform.py", "--apply", cwd=root)
    P70._commit(root)
    cp = run("kb_conform.py", "--finalize", cwd=root, expect_rc=0)
    assert "записи, ждущие переноса в Raw/: 0" in cp.stdout, cp.stdout
    run("kb_conform.py", "--finalize", "--apply", cwd=root, expect_rc=0)
    state = json.loads((root / KB / "meta" / "conform_state.json").read_text(encoding="utf-8"))
    assert state["complete"] is True and state["exceptions"]["records"] == 0, state
    lint = run("kb_lint.py", cwd=root).stdout
    assert "рукописных записей в базе" not in lint and "без template:" not in lint, lint[-600:]


@test
def test_lint_and_list_count_the_records_that_wait_for_the_move(tmp: Path):
    root = _legacy(tmp)
    data = json.loads(run("kb_record.py", "--list", "--json", cwd=root, expect_rc=0).stdout)
    assert data["decision"]["pending"] == 2 and data["reference"]["pending"] == 1 and data["question"]["pending"] == 0, data
    assert "рукописных записей в базе: 3" in run("kb_lint.py", cwd=root).stdout
    _migrate(root, "--apply")
    data = json.loads(run("kb_record.py", "--list", "--json", cwd=root, expect_rc=0).stdout)
    assert data["decision"]["pending"] == 0 and data["reference"]["pending"] == 0, data


@test
def test_migrate_finds_hand_written_records_in_subfolders_of_a_section(tmp: Path):
    """Рукописный справочник в подпапке раздела (так лежали справочники прежних баз) переносится в плоскую `Raw/reference/`."""
    root = P114._base(tmp)
    _put(root, f"{KB}/Reference/Подпапка/Список систем.md", LEGACY_REF.replace("Сокращения проекта", "Список систем"))
    P70._commit(root)
    cp = _migrate(root, "--apply", expect=0)
    assert "Список систем.md" in cp.stdout and (root / "Raw/reference/Список систем.md").is_file(), cp.stdout
    assert not (root / KB / "Reference" / "Подпапка" / "Список систем.md").exists()
    assert (root / KB / "Reference" / "Список систем.md").is_file(), "зеркало ложится в корень раздела"
