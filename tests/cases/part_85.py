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


# ------------------------------------------------------------------ BL-TEAM-07: паспорт артефакта

def _make_project_with_kind(tmp: Path, kind: str = "us"):
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/T.md").write_text("# История\n\n## Описание\n\n## Сценарии\n", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip()
                   + f'\nartifacts:\n  {kind}:\n    title: "История"\n    template: "Templates/T.md"\n'
                     f'    out: "Artifacts/{kind}"\n', encoding="utf-8")
    (root / "Artifacts" / kind).mkdir(parents=True, exist_ok=True)
    return root


def _fake_model(draft: str, calls: list):
    def fake(cfg_, role, messages, **kw):
        calls.append(role)
        ok = {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": []}
        if role == "planner":
            return {**ok, "text": json.dumps({"questions": [], "plan": "1. Описание"}, ensure_ascii=False)}
        if role == "critic":
            return {**ok, "text": json.dumps({"ok": True, "issues": []}, ensure_ascii=False)}
        if role == "qa":
            return {**ok, "text": "1. ОПОРА «срок»\n\nВЕРДИКТ: ЧИСТО"}
        return {**ok, "text": draft}
    return fake


@test
def test_make_writes_the_artifact_passport_and_stage_times(tmp: Path):
    """`agent:make`: номер, связи, время этапов и число обращений к модели попадают в шапку артефакта."""
    A = _mod("agent_core")
    R = _mod("agent_runner")
    root = _make_project_with_kind(tmp)
    from harness import card
    for i in range(5):
        card(root, f"Concepts/Заявка-{i}.md", f"Заявка проходит статусы. Срок десять дней. [[Заявка-{(i + 1) % 5}]]",
             status="knowledge", kind="knowledge")
    card(root, "Requirements/REQ-042-Срок-заявки.md", "Срок заявки — десять дней. [[Заявка-1]]",
         status="knowledge", kind="knowledge", type="requirement")
    calls: list = []
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    fake = _fake_model("# История\n\n## Описание\n\nСрок заявки — десять дней, см. [[REQ-042-Срок-заявки]] "
                       "и [[REQ-999-Выдумка]].\n", calls)
    links, bad = R.parse_from(["REQ-007", "SPEC-012,AC-3.1"])
    assert not bad and links == {"implements": ["REQ-007"], "parent": ["SPEC-012"], "covers": ["AC-3.1"]}, (links, bad)
    r = R.run_make(cfg, str(root), "us", "срок заявки десять дней", "", "", True, call=fake, links=links)
    assert r["ok"] and r["stage"] == "done", r
    st = R.load_session(str(root), r["sid"])
    doc = (root / st["path"]).read_text(encoding="utf-8")
    head = doc.split("---", 2)[1]
    fm = _mod("aurora_common").frontmatter(doc)
    assert fm["artifact_id"] == "ART-0001", head
    # связи: объявленные + карточка REQ из пака, на которую документ сослался; выдуманную REQ-999 в паспорт не берём
    assert "implements: [REQ-007, REQ-042]" in head, head
    assert "parent: SPEC-012" in head and "covers: [AC-3.1]" in head, head
    assert "REQ-999" not in head
    # время этапов — UTC с секундами, а не дата
    import re as _re
    for stage in R.MAKE_STAGES:
        assert _re.search(rf"^  {stage}: \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", head, _re.M), (stage, head)
    assert _re.search(r"^started: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", head, _re.M), head
    assert _re.search(r"^finished: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", head, _re.M), head
    assert f"model_calls: {len(calls)}" in head and len(calls) >= 4, (calls, head)
    # сессия хранит тот же номер: продолжение не выдаёт второй
    assert st["artifact_id"] == "ART-0001"
    r2 = R.run_make(cfg, str(root), "", "", r["sid"], "", False, call=fake)
    assert r2["ok"], r2
    assert R.load_session(str(root), r["sid"])["artifact_id"] == "ART-0001"
    assert "Номер: `ART-0001`" in R.report_make(r2)


@test
def test_two_artifacts_get_two_numbers_and_the_header_survives_hostile_values(tmp: Path):
    """Каждый артефакт получает свой номер; значения паспорта не несут переводов строки и пробелов."""
    R = _mod("agent_runner")
    root = _make_project_with_kind(tmp)

    def doc(sid: str, **extra) -> str:
        st = {"sid": sid, "kind": "us", "idea": "Идея " + sid,
              "spec": {"out": "Artifacts/us", "template": "t.md", "title": "US"},
              "stages": {"checked": "2026-10-19T01:02:03Z"}, "issues": [],
              "momus": {"ok": True, "unsupported": 0, "clean": True},
              "draft": "Текст.", "pack": "# Context pack\n", "path": None, **extra}
        return (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")

    one = doc("s1")
    two = doc("s2")
    assert "artifact_id: ART-0001" in one and "artifact_id: ART-0002" in two, (one[:200], two[:200])
    evil = doc("s3", links={"implements": ["REQ-1\nstatus: ready", "REQ-2"], "parent": ["SPEC 3"]},
               started="2026-10-19T00:00:00Z\nstatus: ready")
    head = evil.split("---", 2)[1]
    assert [l for l in head.splitlines() if l.startswith("status:")] == ["status: ready"], head   # одна строка status
    assert "implements: [REQ-2]" in head and "parent:" not in head, head
    assert "started:" not in head, "значение с переводом строки попало в шапку"
    # неузнанный идентификатор — ошибка вызова, а не молчаливая потеря связи
    _l, bad = R.parse_from(["REQ-1", "ФИЧА-7"])
    assert "ФИЧА-7" in bad, bad
    _l, bad = R.parse_from(["SPEC-1", "US-2"])
    assert "родитель" in bad, bad
    links, bad = R.parse_from(["RU.PRJ.REQ-010", "RU.PRJ.AC-1.1.1"])
    assert not bad and links == {"implements": ["RU.PRJ.REQ-010"], "covers": ["RU.PRJ.AC-1.1.1"]}, (links, bad)


@test
def test_make_without_the_id_module_still_writes_the_document(tmp: Path):
    """Движок проекта, обновлённый не целиком (нет id_registry), документ пишет — без номера, а не отказом."""
    import builtins
    R = _mod("agent_runner")
    root = _make_project_with_kind(tmp)
    saved = sys.modules.pop("id_registry", None)
    real_import = builtins.__import__

    def deny(name, *a, **kw):
        if name == "id_registry":
            raise ImportError("нет модуля")
        return real_import(name, *a, **kw)

    builtins.__import__ = deny
    try:
        st = {"sid": "s9", "kind": "us", "idea": "Идея", "spec": {"out": "Artifacts/us", "template": "t.md",
              "title": "US"}, "stages": {}, "issues": [], "draft": "Текст.", "pack": "", "path": None}
        text = (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["id_registry"] = saved
    assert "artifact_id" not in text and "session: s9" in text, text[:300]


@test
def test_impact_follows_the_passport_links_of_artifacts(tmp: Path):
    """`ops:impact`: артефакт, чей паспорт называет цель (`implements`, `parent`, `covers`), попадает в зависимых."""
    from harness import card
    root = make_project(tmp)
    card(root, "Requirements/REQ-042-Срок-заявки.md", "Срок — десять дней.", status="knowledge",
         kind="knowledge", type="requirement", req_id="REQ-042")
    _touch(root, "Artifacts/us/История.md",
           "---\ntitle: История\nartifact_id: ART-0003\nimplements: [REQ-042]\nparent: SPEC-012\n---\n\nТекст.\n")
    _touch(root, "Artifacts/ac/Критерии.md",
           "---\ntitle: Критерии\nartifact_id: ART-0004\nparent: ART-0003\ncovers: [AC-3.1]\n---\n\nТекст.\n")
    _touch(root, "Artifacts/us/Другая.md", "---\ntitle: Другая\nimplements: [REQ-421]\n---\n\nТекст.\n")
    cp = run("kb_trace.py", "--impact", "REQ-042-Срок-заявки", cwd=root, expect_rc=0)
    assert "Artifacts/us/История.md (паспорт: implements)" in cp.stdout, cp.stdout
    assert "Другая.md" not in cp.stdout, "REQ-421 принят за REQ-042"        # совпадение целого номера, не префикса
    # цель можно назвать номером артефакта: зависимые — те, у кого он родитель
    cp = run("kb_trace.py", "--impact", "ART-0003", cwd=root, expect_rc=0)
    assert "Artifacts/ac/Критерии.md (паспорт: parent)" in cp.stdout, cp.stdout
    assert "История.md" not in cp.stdout.split("Зависит от неё")[1], cp.stdout


@test
def test_id_registry_edge_cases_found_in_review(tmp: Path):
    """Снимок сданного не даёт повтор; большая и BOM-шапка читаются; даты и дроби не номера; замок живого процесса."""
    ID = _mod("id_registry")
    root = make_project(tmp)
    # снимок сданного документа несёт копию шапки: номер занят, повтора нет
    _touch(root, "Artifacts/us/а.md", "---\ntitle: а\nartifact_id: ART-0001\n---\n\nТекст.\n")
    _touch(root, "Deliverables/released/а_v1.md", "---\ntitle: а\nartifact_id: ART-0001\n---\n\nТекст.\n")
    assert ID.duplicates(str(root)) == {}, ID.duplicates(str(root))
    assert ID.peek("ART", str(root)) == "ART-0002"
    # шапка длиннее окна чтения (сотни ссылок) и шапка с BOM
    big = ("---\ntitle: б\nartifact_id: ART-0005\nbased_on: ["
           + ", ".join(f'"[[Карточка-{i}]]"' for i in range(400)) + "]\n---\n\nТекст.\n")
    assert len(big) > ID.HEAD_CHARS
    _touch(root, "Artifacts/us/б.md", big)
    _touch(root, "Artifacts/us/в.md", "﻿---\ntitle: в\nartifact_id: ART-0007\n---\n\nТекст.\n")
    assert ID.peek("ART", str(root)) == "ART-0008", ID.peek("ART", str(root))
    # дата в имени и дробный номер — не номера реестра
    _touch(root, "AuroraKnowledgeDB/Decisions/DR-2026-10-19-заметка.md")
    _touch(root, "AuroraKnowledgeDB/Questions/Q-1.1-подвопрос.md")
    _touch(root, "AuroraKnowledgeDB/Questions/Q-001-вопрос.md")
    assert ID.peek("DR", str(root)) == "DR-0001" and ID.peek("Q", str(root)) == "Q-002", (ID.peek("DR", str(root)), ID.peek("Q", str(root)))
    assert ID.duplicates(str(root)) == {}
    # огромное число в шапке не роняет выдачу
    _touch(root, "Artifacts/us/г.md", "---\ntitle: г\nartifact_id: ART-" + "9" * 5000 + "\n---\n")
    assert ID.peek("ART", str(root)) == "ART-0008"
    # пробел вокруг имени пространства не обходит отметку
    assert ID.allocate("ART", str(root)) == "ART-0008"
    assert ID.peek(" art ", str(root)) == "ART-0009"
    # замок живого процесса, который не отпускают, — понятный отказ, а не зависание
    lock = root / ".aurora/state/ids/DR.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "at": __import__("time").time()}), encoding="utf-8")
    was = ID.LOCK_WAIT_S
    ID.LOCK_WAIT_S = 0.3
    try:
        try:
            ID.allocate("DR", str(root))
            raise AssertionError("выдача прошла мимо чужого замка")
        except ID.IdBusy:
            pass
        cp = run("id_registry.py", "DR", cwd=root)         # другой процесс, замок держит живой pid
        assert cp.returncode == 2 and "занята" in cp.stderr, (cp.returncode, cp.stderr)
        # замок мёртвого процесса снимается
        lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "at": __import__("time").time() - 600}), encoding="utf-8")
        assert ID.allocate("DR", str(root)) == "DR-0001"
    finally:
        ID.LOCK_WAIT_S = was


@test
def test_impact_reads_numbers_with_a_dot_and_yogi_keys(tmp: Path):
    """`ops:impact`: `AC-3.1` и ключ `RU.PRJ.REQ-010` в паспорте читаются целиком; `AC-3` не принимается за `AC-3.1`."""
    from harness import card
    root = make_project(tmp)
    card(root, "Requirements/REQ-010-Срок.md", "Срок.", status="knowledge", kind="knowledge", type="requirement")
    _touch(root, "Artifacts/ac/Критерий.md", "---\ntitle: К\nartifact_id: ART-0001\ncovers: [AC-3.1]\nimplements: [RU.PRJ.REQ-010]\n---\n\nТекст.\n")
    cp = run("kb_trace.py", "--impact", "REQ-010-Срок", cwd=root, expect_rc=0)
    assert "Artifacts/ac/Критерий.md (паспорт: implements)" in cp.stdout, cp.stdout
    card(root, "Concepts/AC-3.1-Критерий.md", "Критерий.", status="knowledge", kind="knowledge")
    card(root, "Concepts/AC-3-Другое.md", "Другое.", status="knowledge", kind="knowledge")
    cp = run("kb_trace.py", "--impact", "AC-3.1-Критерий", cwd=root, expect_rc=0)
    assert "Критерий.md (паспорт: covers)" in cp.stdout, cp.stdout
    cp = run("kb_trace.py", "--impact", "AC-3-Другое", cwd=root, expect_rc=0)
    assert "Критерий.md" not in cp.stdout, cp.stdout
    R = _mod("agent_runner")
    links, bad = R.parse_from(["req-042", "ac-3.1"])
    assert not bad and links == {"implements": ["REQ-042"], "covers": ["AC-3.1"]}, (links, bad)
