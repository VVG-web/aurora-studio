"""Проверки движка Aurora, часть 85. Каркас и помощники — tests/harness.py.

Реестр идентификаторов (BL-TEAM-07, BL-TEAM-02, BL-KBA-17): выдача номеров под замком, паспорт
артефакта агента, журнал решений, маска ключа и устойчивые идентификаторы сценариев. Синтетические
проекты, без модели и сети.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _touch(root: Path, rel: str, text: str = "---\ntitle: x\n---\n\nТекст.\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------------ BL-TEAM-07: выдача номеров

@test
def test_next_id_gives_distinct_numbers_to_parallel_callers(tmp: Path):
    """Параллельные вызовы `kb:next-id` получают разные номера; архив и отметка не отдают занятое."""
    ID = _mod("id_registry")
    root = make_project(tmp)
    _touch(root, "AuroraKnowledgeDB/Decisions/DR-0007-выбор-шины.md")
    _touch(root, "AuroraKnowledgeDB/_archive/DR-0012-старое.md")           # архив тоже занимает номер
    assert ID.peek("DR", str(root)) == "DR-0013", ID.peek("DR", str(root))
    assert ID.peek("dr", str(root)) == "DR-0013"                           # регистр пространства не важен

    with ThreadPoolExecutor(max_workers=8) as ex:
        got = list(ex.map(lambda _i: ID.allocate("DR", str(root)), range(40)))
    assert len(set(got)) == 40, sorted(got)
    assert min(got) == "DR-0013" and max(got) == "DR-0052", (min(got), max(got))
    # peek ничего не занимает, а следующий вызов продолжает счёт
    assert ID.peek("DR", str(root)) == "DR-0053"
    assert ID.peek("DR", str(root)) == "DR-0053"
    assert ID.allocate("DR", str(root)) == "DR-0053"
    assert not (root / ".aurora/state/ids/DR.lock").exists(), "замок выдачи не отпущен"


@test
def test_next_id_from_separate_processes_never_repeats(tmp: Path):
    """Три процесса, по пять выдач: пятнадцать разных номеров (то, что не умеет «следующий по _index.md»)."""
    root = make_project(tmp)
    seen = []

    def burst(_i: int) -> list:
        out = []
        for _ in range(5):
            cp = run("id_registry.py", "Q", "--root", str(root), cwd=root)
            assert cp.returncode == 0, cp.stderr
            out.append(cp.stdout.strip())
        return out

    with ThreadPoolExecutor(max_workers=3) as ex:
        for part in ex.map(burst, range(3)):
            seen += part
    assert len(set(seen)) == 15, sorted(seen)
    assert all(s.startswith("Q-") and len(s) == 5 for s in seen), seen        # три цифры


@test
def test_next_id_does_not_reuse_a_deleted_number_and_survives_a_clean_clone(tmp: Path):
    """Номер удалённой карточки не возвращается (отметка), а чистый клон без отметки считает по файлам."""
    ID = _mod("id_registry")
    root = make_project(tmp)
    assert ID.allocate("REQ", str(root)) == "REQ-001"
    f = _touch(root, "AuroraKnowledgeDB/Requirements/REQ-001-первое.md")
    assert ID.allocate("REQ", str(root)) == "REQ-002"
    f.unlink()                                                             # карточку удалили
    assert ID.allocate("REQ", str(root)) == "REQ-003", "номер удалённой карточки вернулся"
    # чистый клон: отметки (.aurora/state) нет — номер считается по файлам проекта
    _touch(root, "AuroraKnowledgeDB/Requirements/REQ-009-девятое.md")
    for p in (root / ".aurora/state/ids").glob("*.json"):
        p.unlink()
    assert ID.peek("REQ", str(root)) == "REQ-010", ID.peek("REQ", str(root))
    # неизвестное пространство — понятная ошибка, а не трассировка
    cp = run("id_registry.py", "NOPE", cwd=root)
    assert cp.returncode == 2 and "пространства" in cp.stderr and "DR" in cp.stderr, cp.stderr


@test
def test_next_id_check_names_files_that_carry_one_identifier(tmp: Path):
    """`--check`: два разных файла с одним номером — повтор; та же карточка в архиве — нет."""
    ID = _mod("id_registry")
    root = make_project(tmp)
    run("id_registry.py", "--check", cwd=root, expect_rc=0)
    _touch(root, "AuroraKnowledgeDB/Decisions/DR-0007-а.md")
    _touch(root, "AuroraKnowledgeDB/_archive/DR-0007-а.md")                # тот же файл в архиве: не повтор
    run("id_registry.py", "--check", cwd=root, expect_rc=0)
    _touch(root, "AuroraKnowledgeDB/Decisions/DR-0007-б.md")               # второй файл с тем же номером
    cp = run("id_registry.py", "--check", cwd=root, expect_rc=1)
    assert "DR-0007" in cp.stdout and "DR-0007-б" in cp.stdout, cp.stdout
    dups = ID.duplicates(str(root))
    assert list(dups) == ["DR-0007"] and {os.path.basename(w) for w in dups["DR-0007"]} == {"DR-0007-а", "DR-0007-б"}, dups
    # кириллическая «Q» в имени не принимается за латинскую, а номер вопроса с разной шириной — один номер
    _touch(root, "AuroraKnowledgeDB/Questions/Q-7-короткий.md")
    _touch(root, "AuroraKnowledgeDB/Questions/Q-007-длинный.md")
    assert "Q-007" in ID.duplicates(str(root))


@test
def test_artifact_ids_are_read_from_headers_and_sessions(tmp: Path):
    """Номер артефакта занимают шапка файла и сессия, у которой файла ещё нет; повтор в двух файлах виден."""
    ID = _mod("id_registry")
    root = make_project(tmp)
    _touch(root, "Artifacts/us/Один.md", "---\ntitle: Один\nartifact_id: ART-0004\n---\n\nТекст.\n")
    sess = root / "Workspaces/s1"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "session.json").write_text(json.dumps({"sid": "s1", "artifact_id": "ART-0009"}), encoding="utf-8")
    assert ID.peek("ART", str(root)) == "ART-0010", ID.peek("ART", str(root))
    assert ID.duplicates(str(root)) == {}
    _touch(root, "Artifacts/ac/Два.md", "---\ntitle: Два\nartifact_id: ART-0004\n---\n\nТекст.\n")
    dups = ID.duplicates(str(root))
    assert list(dups) == ["ART-0004"] and len(dups["ART-0004"]) == 2, dups


@test
def test_kb_lint_shows_repeated_identifiers_as_a_warning_only(tmp: Path):
    """`kb:lint` называет повторы идентификаторов, но код возврата и счёт ошибок не меняет."""
    root = make_project(tmp)
    clean = run("kb_lint.py", cwd=root)
    assert "повторов идентификаторов" not in clean.stdout, clean.stdout
    _touch(root, "AuroraKnowledgeDB/Decisions/DR-0007-а.md",
           '---\ntitle: "DR-0007-а"\ntype: decision\nstatus: proposed\n---\n\n# а\n')
    _touch(root, "AuroraKnowledgeDB/Decisions/DR-0007-б.md",
           '---\ntitle: "DR-0007-б"\ntype: decision\nstatus: proposed\n---\n\n# б\n')
    cp = run("kb_lint.py", cwd=root)
    assert "повторов идентификаторов 1" in cp.stdout and "DR-0007" in cp.stdout, cp.stdout
    only = run("kb_lint.py", "--only", "AuroraKnowledgeDB/Decisions/DR-0007-а.md", cwd=root)
    assert "повторов идентификаторов" not in only.stdout, "хук коммита судит только переданные карточки"
    errs = [l for l in cp.stdout.splitlines() if l.startswith("kb_lint: карточек")]
    assert errs and "ошибок" in errs[0] and "повторов" not in errs[0], errs
