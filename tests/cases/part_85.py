"""Проверки движка Aurora, часть 85. Каркас и помощники — tests/harness.py.

Реестр идентификаторов (BL-TEAM-07, BL-TEAM-02, BL-KBA-17): выдача номеров под замком, паспорт
артефакта агента, журнал решений, маска ключа и устойчивые идентификаторы сценариев. Синтетические
проекты, без модели и сети.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
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


# ------------------------------------------------------------------ BL-TEAM-02: журнал решений

GOOD_BODY = """\
# DR-{n}: Срок подтверждения заявки — три рабочих дня

## Контекст
В ТЗ срок три рабочих дня, в описании процесса — пять. Нужно единое значение для [[Заявка на поставку]].

## Рассмотренные варианты

### 1. Три рабочих дня — выбран
- Плюсы: совпадает с ТЗ.
- Минусы: поставщики просили больше.

### 2. Пять рабочих дней — отклонён
- Плюсы: удобнее поставщикам.
- Минусы: противоречит ТЗ.
- **Причина отказа:** ТЗ — подписанный документ, изменение требует доп. соглашения.

## Решение
Срок подтверждения — три рабочих дня.

## Следствия
- Описание процесса приводится к ТЗ.
- Затронутые карточки: [[Заявка на поставку]]

## История
- 2026-09-20 — создана (@analyst)
"""


def _dr(root: Path, n: int = 1, status: str = "proposed", body: str = GOOD_BODY, date: str = "2026-09-20",
        extra: str = "") -> Path:
    num = f"{n:04d}"
    head = (f'---\ntitle: "DR-{num}: Срок подтверждения заявки"\naliases: ["DR-{num}"]\ntags: [decision]\n'
            f"type: decision\ntemplate: akdb_decision_v1.0.md\nkind: document\nstatus: {status}\ndate: {date}\n"
            f'owner: "@analyst"\nsupersedes: []\nsuperseded_by:\napplies_to: []\ncreated: {date}\nupdated: {date}\n'
            f"{extra}---\n\n")
    return _touch(root, f"AuroraKnowledgeDB/Decisions/DR-{num}-срок.md", head + body.replace("{n}", num))


def _decision_project(tmp: Path) -> Path:
    from harness import card
    root = make_project(tmp)
    card(root, "Concepts/Заявка на поставку.md", "Заявка на поставку — документ.", status="knowledge", kind="knowledge")
    card(root, "Concepts/Заявка.md", "Заявка — документ.", status="knowledge", kind="knowledge")
    return root


@test
def test_decide_accept_records_who_when_and_a_body_fingerprint(tmp: Path):
    """Принятие: статус, дата, отпечаток тела в шапке, строка истории; принять второй раз нельзя."""
    root = _decision_project(tmp)
    path = _dr(root)
    cp = run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=0)
    assert "proposed → accepted" in cp.stdout, cp.stdout
    text = path.read_text(encoding="utf-8")
    fm = _mod("aurora_common").frontmatter(text)
    assert fm["status"] == "accepted" and len(fm["accepted_fp"]) == 64, text[:600]
    assert fm["date"] != "2026-09-20" and fm["owner"] == "@analyst", fm
    hist = text.split("## История", 1)[1]
    assert "принято (@analyst)" in hist and "создана (@analyst)" in hist, hist
    cp = run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "accepted" in cp.stderr and "proposed" in cp.stderr, cp.stderr
    # шаблон и линтер приняли результат
    lint = run("kb_lint.py", cwd=root)
    assert "тело принятого решения" not in lint.stdout, lint.stdout


@test
def test_decide_accept_refuses_a_decision_with_a_broken_form(tmp: Path):
    """Принять нельзя: один вариант, нет причины отказа, нет затронутых, чужая карточка, остатки «Под вопросом»."""
    root = _decision_project(tmp)
    one = GOOD_BODY.split("### 2.")[0] + "## Решение\nСрок — три дня.\n\n## Следствия\n- Приводим к ТЗ.\n\n## История\n- создана\n"
    path = _dr(root, 1, body=one)
    cp = run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=1)
    for code in ("DR-01", "DR-04"):
        assert code in cp.stderr, cp.stderr
    assert "status: proposed" in path.read_text(encoding="utf-8"), "отказанное решение изменено"
    no_reason = GOOD_BODY.replace("- **Причина отказа:** ТЗ — подписанный документ, изменение требует доп. соглашения.\n", "")
    _dr(root, 2, body=no_reason)
    cp = run("kb_decide.py", "DR-0002", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "DR-02" in cp.stderr and "Пять рабочих дней" in cp.stderr, cp.stderr
    _dr(root, 3, body=GOOD_BODY.replace("[[Заявка на поставку]]\n", "[[Нет такой карточки]]\n"))
    cp = run("kb_decide.py", "DR-0003", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "DR-05" in cp.stderr and "Нет такой карточки" in cp.stderr, cp.stderr
    _dr(root, 4, body=GOOD_BODY + "\n## Под вопросом\n- вариант без подтверждения\n")
    cp = run("kb_decide.py", "DR-0004", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "DR-08" in cp.stderr, cp.stderr
    cp = run("kb_decide.py", "DR-0099", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "нет в" in cp.stderr, cp.stderr


@test
def test_decide_accepts_only_a_person_and_a_rejection_needs_a_reason(tmp: Path):
    """Имя модели или программы не принимается; отклонение без причины — отказ; с причиной — в истории."""
    root = _decision_project(tmp)
    path = _dr(root)
    for who in ("Claude", "@gpt-4", "aurora-bot", "AI assistant"):
        cp = run("kb_decide.py", "DR-0001", "--by", who, cwd=root, expect_rc=1)
        assert "человек" in cp.stderr, (who, cp.stderr)
    assert "status: proposed" in path.read_text(encoding="utf-8")
    run("kb_decide.py", "DR-0001", "--reject", "--by", "@analyst", cwd=root, expect_rc=1)
    run("kb_decide.py", "DR-0001", "--reject", "--reason", "  ", "--by", "@analyst", cwd=root, expect_rc=1)
    run("kb_decide.py", "DR-1", "--reject", "--reason", "заказчик снял вопрос", "--by", "@analyst", cwd=root, expect_rc=0)
    text = path.read_text(encoding="utf-8")
    assert "status: rejected" in text and "отклонено (@analyst) — причина: заказчик снял вопрос" in text, text[:900]
    # отклонить принятое нельзя — его заменяют
    _dr(root, 2, status="accepted")
    cp = run("kb_decide.py", "2", "--reject", "--reason", "передумали", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "kb:supersede" in cp.stderr, cp.stderr


@test
def test_accepted_decision_body_is_immutable_but_history_and_renames_are_not(tmp: Path):
    """`kb:lint`: тело принятого решения изменено — ошибка; история, перенос строк и переименование цели ссылки — нет."""
    root = _decision_project(tmp)
    path = _dr(root)
    run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=0)
    base = run("kb_lint.py", cwd=root)
    assert "тело принятого решения" not in base.stdout, base.stdout
    accepted = path.read_text(encoding="utf-8")
    # история дописана, ссылка переименована (так делают kb:supersede и kb:repair), строки переложены — отпечаток тот же
    ok = (accepted.replace("## История\n", "## История\n\n- 2026-10-01: заменено в ссылках.\n")
          .replace("[[Заявка на поставку]]", "[[Заявка]]").replace("- Плюсы: совпадает с ТЗ.",
                                                                  "- Плюсы:   совпадает\nс ТЗ."))
    path.write_text(ok, encoding="utf-8")
    cp = run("kb_lint.py", cwd=root)
    assert "тело принятого решения" not in cp.stdout, cp.stdout
    # слово в причине отказа — правка по существу
    path.write_text(accepted.replace("подписанный документ", "устное соглашение"), encoding="utf-8")
    cp = run("kb_lint.py", cwd=root)
    assert cp.returncode == 1 and "тело принятого решения изменено" in cp.stdout and "DR-0001" in cp.stdout, cp.stdout
    gate = run("kb_lint.py", "--gate", cwd=root)
    assert "decision-body-changed" in gate.stdout, gate.stdout
    # решение, принятое до команды (нет отпечатка), не судится
    legacy = _dr(root, 2, status="accepted")
    legacy.write_text(legacy.read_text(encoding="utf-8").replace("подписанный документ", "что угодно"), encoding="utf-8")
    path.write_text(accepted, encoding="utf-8")
    cp = run("kb_lint.py", cwd=root)
    assert "тело принятого решения" not in cp.stdout, cp.stdout


@test
def test_decision_journal_is_stable_and_names_stale_and_changed(tmp: Path):
    """`ops:decisions`: таблица решений, просроченные предложенные, изменённые принятые; два запуска совпадают."""
    root = _decision_project(tmp)
    _dr(root, 1)
    run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=0)
    _dr(root, 2, date="2020-01-01")                                        # давно предложено и не принято
    bad = _dr(root, 3)
    run("kb_decide.py", "DR-0003", "--by", "@analyst", cwd=root, expect_rc=0)
    bad.write_text(bad.read_text(encoding="utf-8").replace("Срок подтверждения — три", "Срок — два"), encoding="utf-8")
    first = run("kb_decide.py", "--journal", cwd=root, expect_rc=0).stdout
    second = run("kb_decide.py", "--journal", cwd=root, expect_rc=0).stdout
    assert first == second, "журнал недетерминирован"
    assert "Журнал решений — 3" in first and "accepted 2" in first and "proposed 1" in first, first
    assert "больше 14 дней назад" in first and "DR-0002" in first.split("больше 14 дней назад")[1].split("—")[0], first
    assert "тело изменено после принятия" in first and "DR-0003" in first.split("Тело изменено после принятия:")[1], first
    j = json.loads(run("kb_decide.py", "--journal", "--json", cwd=root).stdout)
    assert j["stale"] == ["DR-0002"] and j["body_changed"] == ["DR-0003"], j
    assert [r["id"] for r in j["decisions"]] == ["DR-0001", "DR-0002", "DR-0003"]
    assert run("kb_decide.py", "--check", cwd=root).returncode == 1


@test
def test_kb_lint_reports_decision_form_as_warnings(tmp: Path):
    """Форма решений (один вариант и т. п.) — предупреждение: код возврата и счёт ошибок прежние."""
    root = _decision_project(tmp)
    clean = run("kb_lint.py", cwd=root)
    assert "решений с замечаниями" not in clean.stdout
    _dr(root, 1, body=GOOD_BODY.split("### 2.")[0] + "## Решение\nТри дня.\n\n## Следствия\n- Приводим.\n")
    cp = run("kb_lint.py", cwd=root)
    assert cp.returncode == clean.returncode, (cp.returncode, cp.stdout)
    assert "решений с замечаниями по форме 1" in cp.stdout and "DR-01" in cp.stdout, cp.stdout
    assert "решений с замечаниями" not in run("kb_lint.py", "--only", "AuroraKnowledgeDB/Concepts/Заявка.md", cwd=root).stdout


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
    assert "Критерий.md" not in cp.stdout, "ключ реестра RU.PRJ.REQ-010 принят за номер карточки REQ-010"
    card(root, "Concepts/AC-3.1-Критерий.md", "Критерий.", status="knowledge", kind="knowledge")
    card(root, "Concepts/AC-3-Другое.md", "Другое.", status="knowledge", kind="knowledge")
    cp = run("kb_trace.py", "--impact", "AC-3.1-Критерий", cwd=root, expect_rc=0)
    assert "Критерий.md (паспорт: covers)" in cp.stdout, cp.stdout
    cp = run("kb_trace.py", "--impact", "AC-3-Другое", cwd=root, expect_rc=0)
    assert "Критерий.md" not in cp.stdout, cp.stdout
    R = _mod("agent_runner")
    links, bad = R.parse_from(["req-042", "ac-3.1"])
    assert not bad and links == {"implements": ["REQ-042"], "covers": ["AC-3.1"]}, (links, bad)


# ------------------------------------------------------------------ BL-TEAM-02: черновик решения из источника

PROTOCOL = """\
# Протокол встречи 2026-10-01

Обсуждали срок подтверждения заявки на поставку.

Аналитик: в ТЗ срок подтверждения заявки — три рабочих дня, в описании процесса пять.
Руководитель проекта: срок подтверждения — три рабочих дня, как в ТЗ, потому что ТЗ подписан заказчиком.
Пять рабочих дней отклонили: это противоречит подписанному ТЗ, изменение потребует доп. соглашения.
"""


def _decide_project(tmp: Path):
    from harness import card
    root = make_project(tmp)
    card(root, "Concepts/Заявка на поставку.md", "Заявка на поставку — документ.", status="knowledge", kind="knowledge")
    _touch(root, "Raw/meetings/2026-10-01_срок/protocol.md", PROTOCOL)
    return root


def _decide_answer(**over) -> dict:
    ans = {"title": "Срок подтверждения заявки — три рабочих дня",
           "context": "В ТЗ и в описании процесса разные сроки подтверждения заявки.",
           "options": [
               {"name": "Три рабочих дня", "chosen": True, "pros": "совпадает с ТЗ", "cons": "",
                "quote": "срок подтверждения — три рабочих дня, как в ТЗ"},
               {"name": "Пять рабочих дней", "chosen": False, "pros": "", "cons": "противоречит ТЗ",
                "why_not": "противоречит подписанному ТЗ, нужно доп. соглашение",
                "quote": "Пять рабочих дней отклонили: это противоречит подписанному ТЗ"}],
           "decision": "Срок подтверждения заявки — три рабочих дня.",
           "decision_quote": "срок подтверждения — три рабочих дня, как в ТЗ",
           "consequences": ["Описание процесса приводится к ТЗ."], "affects": ["Заявка на поставку"], "replaces": []}
    ans.update(over)
    return ans


def _decide_call(ans, seen: list):
    def fake(cfg_, role, messages, **kw):
        seen.append((role, messages))
        text = ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [], "text": text}
    return fake


def _decide_cfg():
    return _mod("agent_core").parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})


@test
def test_decide_drafts_a_proposed_decision_and_marks_what_the_source_does_not_say(tmp: Path):
    """`agent:decide`: цитаты проверяет код; ненайденное и несуществующие карточки — «Под вопросом»; принять нельзя, пока он не пуст."""
    R = _mod("agent_runner")
    root = _decide_project(tmp)
    ans = _decide_answer()
    ans["options"].append({"name": "Семь дней", "chosen": False, "why_not": "долго",
                           "quote": "все единогласно проголосовали за семь дней"})          # такого в протоколе нет
    ans["affects"].append("Нет такой карточки")
    ans["context"] = "Контекст со ссылкой [[Выдумка]] и заготовкой {{x}}."
    seen: list = []
    res = R.run_decide(_decide_cfg(), str(root), "Raw/meetings/2026-10-01_срок/protocol.md", "срок заявки", "@analyst", [],
                       True, call=_decide_call(ans, seen), today="2026-10-19")
    assert res["ok"] and res["written"] and res["id"] == "DR-0001", res
    path = root / res["path"]
    text = path.read_text(encoding="utf-8")
    fm = _mod("aurora_common").frontmatter(text)
    assert fm["status"] == "proposed" and fm["template"] == "akdb_decision_v1.0.md" and fm["owner"] == "@analyst", text[:500]
    assert "### 1. Три рабочих дня — выбран" in text and "### 2. Пять рабочих дней — отклонён" in text, text
    assert "**Причина отказа:** противоречит подписанному ТЗ" in text
    assert "Источник: «срок подтверждения — три рабочих дня, как в ТЗ»" in text
    under = text.split("## Под вопросом", 1)[1]
    assert "Вариант «Семь дней»" in under and "Нет такой карточки" in under, under
    assert "[[Выдумка]]" not in text and "{{" not in text, "ссылка и заготовка из ответа модели попали в карточку"
    assert "Затронутые карточки: [[Заявка на поставку]]" in text and "[[Нет такой карточки]]" not in text
    # запрос к модели: источник в ограждении недоверенных данных, роль — планировщик
    role, msgs = seen[0]
    assert role == "planner"
    assert "Протокол встречи 2026-10-01" in msgs[0]["content"]
    # пока «Под вопросом» не пуст, решение не принять; после разбора — принимается
    cp = run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=1)
    assert "DR-08" in cp.stderr, cp.stderr
    cleaned = text.split("## Под вопросом")[0].rstrip() + "\n"
    cleaned = cleaned.replace("### 3. Семь дней — отклонён\n- **Причина отказа:** долго\n- ⚠ Цитаты нет в источнике: вариант назван моделью, подтвердите или удалите\n", "")
    path.write_text(cleaned, encoding="utf-8")
    run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=0)
    assert "status: accepted" in path.read_text(encoding="utf-8")


@test
def test_decide_preview_writes_nothing_and_takes_no_number(tmp: Path):
    """Без `--apply` карточки нет и номер не занят; ответ «решения нет», сбой модели и мусор — отказ без записи."""
    R = _mod("agent_runner")
    IDR = _mod("id_registry")
    root = _decide_project(tmp)
    src = "Raw/meetings/2026-10-01_срок/protocol.md"
    res = R.run_decide(_decide_cfg(), str(root), src, "", "@analyst", [], False, call=_decide_call(_decide_answer(), []))
    assert res["ok"] and not res["written"] and "# DR-0001:" in res["text"], res
    assert not list((root / "AuroraKnowledgeDB/Decisions").glob("DR-*.md"))
    assert IDR.peek("DR", str(root)) == "DR-0001", "предпросмотр занял номер"
    assert "Предпросмотр" in R.report_decide(res, False) and "--apply" in R.report_decide(res, False)
    no = R.run_decide(_decide_cfg(), str(root), src, "", "@analyst", [], True, call=_decide_call({"none": "решения нет"}, []))
    assert not no["ok"] and "не нашлось" in no["why"], no
    junk = R.run_decide(_decide_cfg(), str(root), src, "", "@analyst", [], True, call=_decide_call("не json", []))
    assert not junk["ok"] and "разбираемый" in junk["why"], junk

    def down(cfg_, role, messages, **kw):
        return {"ok": False, "log": ["№1: шлюз не ответил"], "text": ""}

    bad = R.run_decide(_decide_cfg(), str(root), src, "", "@analyst", [], True, call=down)
    assert not bad["ok"] and "модель не ответила" in bad["why"], bad
    assert IDR.peek("DR", str(root)) == "DR-0001", "отказ занял номер"


@test
def test_decide_checks_who_and_where_before_calling_the_model(tmp: Path):
    """Имя модели, чужой файл, несуществующая карточка и вопрос без номера отсекаются до обращения к модели."""
    R = _mod("agent_runner")
    root = _decide_project(tmp)
    src = "Raw/meetings/2026-10-01_срок/protocol.md"
    seen: list = []
    call = _decide_call(_decide_answer(), seen)
    for kwargs, word in (({"owner": "Claude"}, "человек"), ({"source": "../secret.md"}, "нет в проекте"),
                         ({"source": "Q-9"}, "нет в"), ({"source": ""}, "нужен источник"),
                         ({"affects": ["Нет такой"]}, "нет карточки")):
        args = {"source": src, "owner": "@analyst", "affects": [], **kwargs}
        res = R.run_decide(_decide_cfg(), str(root), args["source"], "", args["owner"], args["affects"], True, call=call)
        assert not res["ok"] and word in res["why"], (kwargs, res)
    assert not seen, "модель вызвана до проверки входа"


@test
def test_decide_reads_a_question_with_its_answer_source(tmp: Path):
    """`--source Q-7`: карточка вопроса и файл `answer_source` идут моделью как источник; карточка вопроса — ссылка в контексте."""
    R = _mod("agent_runner")
    root = _decide_project(tmp)
    _touch(root, "AuroraKnowledgeDB/Questions/Q-007-срок-заявки.md",
           '---\ntitle: "Q-007 — срок заявки"\ntype: question\nstatus: draft\nq_status: answered\nanswered: 2026-10-01\n'
           'answer_source: "Raw/meetings/2026-10-01_срок/protocol.md"\nblocks: []\n---\n\n# Q-007\n\nКакой срок подтверждения заявки?\n')
    seen: list = []
    res = R.run_decide(_decide_cfg(), str(root), "Q-7", "", "@analyst", [], False, call=_decide_call(_decide_answer(), seen))
    assert res["ok"] and res["source"] == "Q-007-срок-заявки", res
    content = seen[0][1][0]["content"]
    assert "Какой срок подтверждения заявки?" in content and "Руководитель проекта: срок подтверждения" in content
    assert "[[Q-007-срок-заявки]]" in res["text"], res["text"][:900]


# ------------------------------------------------------------------ BL-TEAM-03: ревью файла и зеркала

STORY_FILE = """\
---
title: "US-1.1.1 Приём заявки"
type: us
artifact_id: ART-0001
status: draft
based_on: ["[[Заявка]]"]
---

# Задача на разработку истории

## Предусловия US

* Готова форма: [[Заявка]]

## Описание

| **Как** | Оператор системы |
| **Я хочу** | принять заявку из смежной системы |

<!-- ниже — производство, в чистовик не идёт -->

## Допущения

- ПРОИЗВОДСТВО-СЕКРЕТ: срок принят по умолчанию
"""


def _review_project(tmp: Path):
    from harness import card
    root = make_project(tmp)
    card(root, "Concepts/Заявка.md", "Заявка — документ. Форма: поле ИНН обязательно.", status="knowledge",
         kind="knowledge")
    _touch(root, "Artifacts/us/US-1.1.1-приём.md", STORY_FILE)
    (root / "TemplatesCommon").mkdir(exist_ok=True)               # в проекте шаблон ревью лежит рядом с остальными общими
    shutil.copy(KIT / "scaffold/TemplatesCommon/review_v2.1.md", root / "TemplatesCommon/review_v2.1.md")
    return root


def _review_stub(cl, override=None, seen=None):
    from harness import _answer, stub_messages

    def fake(cfg_, role, messages, **kw):
        if seen is not None:
            seen.append(stub_messages(messages, kw))
        return {"ok": True, "text": _answer(cl, "us", override), "model": "m", "seen": 1, "cut": 0}
    return fake


@test
def test_review_file_reads_the_artifact_and_linked_cards_without_confluence(tmp: Path):
    """`--file`: чистовик и связанные карточки базы идут в промпт; производство — нет; сети и Confluence нет."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    seen: list = []
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-1.1.1-приём.md", root=str(root), runs=3,
                         call=_review_stub(cl, seen=seen))
    assert rec["status"] == "ok" and rec["profile"] == "us" and rec["verdict"] == "готова к передаче", rec
    assert rec["subject"] == "file" and rec["code"] == "US-1.1.1-приём" and len(rec["subject_sha"]) == 16, rec
    prompt = "\n".join(m["content"] for m in seen[0])
    assert "принять заявку из смежной системы" in prompt, "чистовик не попал в промпт"
    assert "поле ИНН обязательно" in prompt, "связанная карточка базы не прочитана"
    assert "ПРОИЗВОДСТВО-СЕКРЕТ" not in prompt, "раздел производства ушёл модели"
    assert [ln["title"] for ln in rec["linked"]] == ["Заявка"] and rec["linked"][0]["status"] == "ok", rec["linked"]
    assert not rec["incomplete"] and len(seen) == 3
    report = rr.render(rec, cl)
    assert "object_link: \"Artifacts/us/US-1.1.1-приём.md\"" in report, report[:400]


@test
def test_review_file_profile_and_zero_network(tmp: Path):
    """Профиль — по `type:`, заголовку или папке; не определён — понятный пропуск; зеркало читается как предмет."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    assert rr.file_profile("Artifacts/us/x.md", "---\ntype: algorithm\n---\n") == "alg"
    assert rr.file_profile("Artifacts/x/y.md", "# ALG-5 Расчёт\n") == "alg"
    assert rr.file_profile("Artifacts/us/y.md", "просто текст") == "us"
    assert rr.file_profile("Artifacts/x/y.md", "просто текст") == ""
    assert rr.file_profile("Artifacts/x/y.md", "просто текст", "alg") == "alg"
    _touch(root, "Artifacts/reports/итог.md", "# Итог\n\nТекст.\n")
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/reports/итог.md", root=str(root), runs=1,
                         call=_review_stub(cl))
    assert rec["verdict"] == "не оценено" and "задайте --profile" in " ".join(rec["reasons"]), rec
    # страница-зеркало: предмет ревью, а не только источник связей; шапку зеркала модель не видит
    _touch(root, "Sources/Confluence/US-4.2.1 Страница.md",
           '---\ntitle: "US-4.2.1 Страница"\nurl: https://example.com/x\nry_defines: [RU.PRJ.US-4.2.1]\n---\n\n'
           "Как оператор я хочу принять документ.\n")
    seen: list = []
    rec = rr.review_file({"request_timeout": 5}, cl, "Sources/Confluence/US-4.2.1 Страница.md", root=str(root),
                         runs=1, call=_review_stub(cl, seen=seen))
    assert rec["profile"] == "us" and rec["status"] == "ok", rec
    assert "принять документ" in "\n".join(m["content"] for m in seen[0]) and "ry_defines" not in "\n".join(m["content"] for m in seen[0])
    # ключ Requirement Yogi в тексте артефакта ведёт на страницу зеркала, где он объявлен
    _touch(root, "Artifacts/us/US-9.md", "---\ntype: us\n---\n\n# Ист\n\n## Предусловия\n\nРеализует RU.PRJ.US-4.2.1.\n")
    seen.clear()
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-9.md", root=str(root), runs=1,
                         call=_review_stub(cl, seen=seen))
    assert [ln["title"] for ln in rec["linked"]] == ["US-4.2.1 Страница"], rec["linked"]
    assert "принять документ" in "\n".join(m["content"] for m in seen[0])
    # CLI: нет файла и несовместимые флаги — отказ до обращения к модели
    cp = run("review_run.py", "--file", "нет/такого.md", cwd=root)
    assert cp.returncode == 1 and "нет файла" in (cp.stderr + cp.stdout), (cp.returncode, cp.stderr)
    cp = run("review_run.py", "--file", "Artifacts/us/US-9.md", "--page", "10", cwd=root)
    assert cp.returncode == 2 and "несовместим" in cp.stderr, cp.stderr
    cp = run("review_run.py", "--file", "нет/такого.md", "--gate", cwd=root)
    assert cp.returncode == 2, "в воротах отказ входа — код 2"


@test
def test_review_file_drops_a_no_whose_quote_is_not_in_the_text(tmp: Path):
    """Ответ «нет» принимается с цитатой, найденной в тексте; выдуманная или отсутствующая — «не определено»."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    good = {"v": "no", "evidence": "в строке «Оператор системы» не названа система-источник", "fix": "назвать"}
    fake_q = {"v": "no", "evidence": "в тексте сказано «Заявка принимается через шину Kafka»", "fix": "убрать"}
    bare = {"v": "no", "evidence": "формулировка неясная", "fix": "уточнить"}
    stub = _review_stub(cl, {"US-04": good, "US-05": fake_q, "US-09": bare})
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-1.1.1-приём.md", root=str(root), runs=3, call=stub)
    ans = rec["answers"]
    assert ans["US-04"]["v"] == "no", ans["US-04"]
    assert ans["US-05"]["v"] == "unknown" and "цитаты нет в тексте" in ans["US-05"]["evidence"], ans["US-05"]
    assert ans["US-09"]["v"] == "unknown", ans["US-09"]
    assert rec["quotes_dropped"] == 6 and rec["verify_quotes"], rec["quotes_dropped"]
    assert "Ответов «нет» без цитаты в тексте (сняты) | 6" in rr.render(rec, cl)
    # выключено — принято как есть
    off = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-1.1.1-приём.md", root=str(root), runs=3,
                         call=stub, verify=False)
    assert off["answers"]["US-05"]["v"] == "no" and not off["verify_quotes"]
    # цитата ассистента (review_score) сверяется, когда передан текст
    req = {"profile": "us", "text": "Как оператор системы я хочу принять заявку",
           "answers": {"US-05": {"v": "no", "evidence": "«выдуманная цитата»"},
                       "US-04": {"v": "no", "evidence": "«принять заявку»"}}}
    got = rr.score_answers(cl, req)
    assert got["quotes_dropped"] == 1 and got["source"] == "assistant", got


@test
def test_review_stamp_and_publish_gate_follow_the_clean_text(tmp: Path):
    """Итог ревью в шапке артефакта; ворота видят устаревшее (текст изменён), провал и отсутствие ревью."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    os.chdir(root)
    path = "Artifacts/us/US-1.1.1-приём.md"
    assert [i for i, _w in rr.publish_gate((root / path).read_text(encoding="utf-8"))] == ["review-missing"]
    rec = rr.review_file({"request_timeout": 5}, cl, path, root=str(root), runs=3, call=_review_stub(cl))
    assert rr.stamp_review(path, rec, cl) is True
    text = (root / path).read_text(encoding="utf-8")
    fm = _mod("aurora_common").frontmatter(text)
    assert fm["review"] == "готова к передаче" and fm["review_sha"] == rec["subject_sha"], text[:500]
    assert fm["review_template"] == "review_v2.1.md" and fm["review_failed"] == "[]" and fm["artifact_id"] == "ART-0001"
    assert rr.publish_gate(text) == [], rr.publish_gate(text)
    # производство и шапка отпечаток не меняют, чистовик — меняет
    assert rr.publish_gate(text.replace("срок принят по умолчанию", "срок принят иначе")) == []
    stale = rr.publish_gate(text.replace("принять заявку", "принять запрос"))
    assert [i for i, _w in stale] == ["review-stale"], stale
    # провал: вердикт «доработать» и номера вопросов
    bad = _review_stub(cl, {"US-04": {"v": "no", "evidence": "«Оператор системы»", "fix": "уточнить"}})
    rec2 = rr.review_file({"request_timeout": 5}, cl, path, root=str(root), runs=3, call=bad)
    assert rr.stamp_review(path, rec2, cl)
    gate = rr.publish_gate((root / path).read_text(encoding="utf-8"))
    assert [i for i, _w in gate] == ["review-verdict"] and "US-04" in gate[0][1], gate
    # зеркало и сырьё шапкой не метятся, не оценённое — тоже
    _touch(root, "Sources/Confluence/US-1.md", "---\ntitle: US-1\n---\n\nТекст.\n")
    assert rr.stamp_review("Sources/Confluence/US-1.md", rec, cl) is False
    assert (root / "Sources/Confluence/US-1.md").read_text(encoding="utf-8").startswith("---\ntitle: US-1\n---")
    assert rr.stamp_review(path, {"status": "ok", "verdict": "не оценено", "subject_sha": "x"}, cl) is False
    # файл без шапки получает её, текст не тронут
    _touch(root, "Artifacts/us/голый.md", "# Голая история\n\nКак оператор я хочу.\n")
    rec3 = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/голый.md", root=str(root), runs=1,
                          call=_review_stub(cl))
    assert rr.stamp_review("Artifacts/us/голый.md", rec3, cl)
    bare = (root / "Artifacts/us/голый.md").read_text(encoding="utf-8")
    assert bare.startswith("---\n") and bare.endswith("# Голая история\n\nКак оператор я хочу.\n"), bare


@test
def test_publish_gate_blocks_a_kind_with_review_until_the_review_is_fresh(tmp: Path):
    """`ship:publish`: у вида с `review: us` нет ревью / текст изменён / вердикт ниже — код 2 без обращения к Confluence."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip() + '\nartifacts:\n  us:\n    template: T/us.md\n'
                   '    out: Artifacts/us\n    review: us\n', encoding="utf-8")
    path = "Artifacts/us/US-1.1.1-приём.md"
    cp = run("publish_doc.py", path, cwd=root, expect_rc=2)
    assert "review-missing" in cp.stderr and "--allow review" in cp.stderr, cp.stderr
    os.chdir(root)
    rec = rr.review_file({"request_timeout": 5}, cl, path, root=str(root), runs=3, call=_review_stub(cl))
    assert rr.stamp_review(path, rec, cl)
    # свежее ревью «готова к передаче» ворота проходит: дальше отказ уже от конфигурации Confluence, а не ревью
    cp = run("publish_doc.py", path, cwd=root, expect_rc=1)
    assert "нет confluence.base_url" in cp.stderr and "ворота ревью" not in cp.stderr, cp.stderr
    # текст изменён после ревью
    p = root / path
    p.write_text(p.read_text(encoding="utf-8").replace("принять заявку", "принять запрос"), encoding="utf-8")
    cp = run("publish_doc.py", path, cwd=root, expect_rc=2)
    assert "review-stale" in cp.stderr, cp.stderr
    # обход: ворота пройдены, причины названы, публикация идёт дальше (здесь — до отказа конфигурации)
    cp = run("publish_doc.py", path, "--allow", "review", cwd=root, expect_rc=1)
    assert "Ворота ревью обойдены" in cp.stdout and "нет confluence.base_url" in cp.stderr, (cp.stdout, cp.stderr)
    # вид без объявленного ревью — поведение прежнее (только предупреждение об отметке)
    _touch(root, "Artifacts/reports/итог.md", "---\ntitle: итог\ntype: report\n---\n\n# Итог\n")
    cp = run("publish_doc.py", "Artifacts/reports/итог.md", cwd=root, expect_rc=1)
    assert "ворота ревью" not in cp.stderr, cp.stderr
    # объявленное ревью проверяется реестром видов
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("review: us", "review: zzz"), encoding="utf-8")
    cp = run("make_kinds.py", cwd=root)
    assert "zzz" in cp.stdout and cp.returncode == 1, cp.stdout
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("review: zzz", "review: alg"), encoding="utf-8")
    cp = run("make_kinds.py", "--kind", "us", cwd=root)
    assert "Ревью перед публикацией: профиль alg" in cp.stdout, cp.stdout


@test
def test_kinds_form_carries_the_review_field(tmp: Path):
    """Поле `review` вида артефакта: есть в форме панели и в обоих каталогах строк, записывается панелью и читается движком."""
    from harness import panel_sources
    src = panel_sources()
    for key in ("kinds.review", "kinds.review_hint", "kinds.review_off"):
        assert src.count(f'"{key}"') >= 3, (key, src.count(f'"{key}"'))              # разметка, ru, en
    for key in ("kinds.review_us", "kinds.review_alg"):                              # в разметке собираются из «kinds.review_» + вид
        assert src.count(f'"{key}"') >= 2, (key, src.count(f'"{key}"'))
    assert re.search(r'review:"",[^}]*task:\{\}', src), "новый вид создаётся без поля review"
    MK = _mod("make_kinds")
    assert "review" in MK.FIELDS
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    root = tmp / "проект"
    (root / "Templates").mkdir(parents=True)
    (root / "Templates/US.md").write_text("шаблон", encoding="utf-8")
    (root / "aurora.config.yaml").write_text('project:\n  name: Т\n', encoding="utf-8")
    res = ck.kinds_write(str(root), {"us": {"title": "История", "template": "Templates/US.md", "out": "Artifacts/us",
                                            "review": "us", "task": {}}})
    assert res.get("ok"), res
    assert MK.read_kinds(str(root))["us"]["review"] == "us", "ревью не дожило до чтения"


@test
def test_mcp_tools_review_file_and_story_lint_are_bounded(tmp: Path):
    """MCP: `story_lint` читает файл проекта; путь от модели не выходит за проект; `review_file` отказывает не-тексту."""
    M = _mod("aurora_mcp")
    root = _review_project(tmp)
    names = {t["name"] for t in M.TOOLS}
    assert {"review_file", "story_lint"} <= names, names
    assert all("project" in t["inputSchema"]["required"] for t in M.TOOLS_ALL if t["name"] in ("review_file", "story_lint"))

    def call(name: str, args: dict):
        try:
            return M.call_tool(str(root), name, args), False
        except M.ToolError as e:
            return str(e), True

    out, err = call("story_lint", {"path": "Artifacts/us/US-1.1.1-приём.md", "profile": "us"})
    assert not err and "US-" in out, out                              # замечания по форме, JSON от движка
    out, err = call("story_lint", {})
    assert not err, out
    for bad in ("../чужой.md", "/etc/passwd", "Artifacts/us/нет.md"):
        out, err = call("story_lint", {"path": bad, "profile": "us"})
        assert err and "внутри проекта" in out, (bad, out)
        out, err = call("review_file", {"path": bad})
        assert err and "внутри проекта" in out, (bad, out)
    out, err = call("story_lint", {"path": "Artifacts/us/US-1.1.1-приём.md"})
    assert err and "profile" in out, out
    # не текст проекта: конфиг и служебная папка модели не отдаются
    out, err = call("review_file", {"path": "aurora.config.yaml"})
    assert "ревью читает текст внутри проекта" in out, out
    _touch(root, ".aurora/state/секрет.md", "# секрет\n")
    out, err = call("review_file", {"path": ".aurora/state/секрет.md"})
    assert "за границей чтения" in out, out
    out, err = call("review_file", {"path": "Artifacts/us/US-1.1.1-приём.md", "profile": "zzz"})
    assert err and "profile" in out, out


# ------------------------------------------------------------------ BL-TEAM-02: доработка по независимому просмотру

@test
def test_decide_source_stays_inside_the_project_text_boundary(tmp: Path):
    """Источник решения — текст проекта по общей границе чтения: секреты, служебные папки, чужие файлы и `answer_source` закрыты."""
    R = _mod("agent_runner")
    root = _decide_project(tmp)
    (root / ".env.aurora.local").write_text("TOKEN=СЕКРЕТНЫЙ-ТОКЕН-12345\n", encoding="utf-8")
    outside = tmp / "чужой.md"
    outside.write_text("# Чужой файл\n\nЧУЖОЙ-ТЕКСТ-ВНЕ-ПРОЕКТА\n", encoding="utf-8")
    seen: list = []
    call = _decide_call(_decide_answer(), seen)
    for src in (".env.aurora.local", "aurora.config.yaml", "../чужой.md", str(outside), ".aurora/state/x.md"):
        res = R.run_decide(_decide_cfg(), str(root), src, "", "@analyst", [], False, call=call)
        assert not res["ok"] and "нет в проекте" in res["why"], (src, res)
    # вопрос с вредным answer_source: карточка читается, ответ-секрет и чужой файл — нет
    for bad in (".env.aurora.local", "../чужой.md", str(outside)):
        _touch(root, "AuroraKnowledgeDB/Questions/Q-001-вред.md",
               '---\ntitle: "Q-001"\ntype: question\nstatus: draft\nq_status: answered\n'
               f'answer_source: "{bad}"\nblocks: []\n---\n\n# Q-001\n\nКакой срок?\n')
        seen.clear()
        res = R.run_decide(_decide_cfg(), str(root), "Q-1", "", "@analyst", [], False, call=call)
        sent = seen[0][1][0]["content"]
        assert res["ok"] and "Какой срок?" in sent, res
        assert "СЕКРЕТНЫЙ-ТОКЕН" not in sent and "ЧУЖОЙ-ТЕКСТ" not in sent, f"файл вне границы ушёл модели ({bad})"


@test
def test_decide_human_name_rejects_models_and_header_forgery(tmp: Path):
    """`--by` и `--owner`: имена моделей в любом написании, управляющие знаки и подделка полей шапки — отказ; обычные имена проходят."""
    KD = _mod("kb_decide")
    for bad in ("claude_code", "Claude3", "ClaudeCode", "gpt4", "GPT_4", "Сlaude", "Клод", "Ассистент", "Codex",
                "Opus", "my-bot", "aurora-studio", "@x\rstatus: accepted\raccepted_fp: dead", "x y", "a\tb", 'a"b', "a\\b"):
        who, err = KD.human_name(bad)
        assert err and not who, (bad, who, err)
    for good in ("Aurora", "Ольга Клодт", "Botvinnik", "Ботвинник", "Abbott", "@analyst", "Иван Петров"):
        who, err = KD.human_name(good)
        assert who == "@" + good.lstrip("@") and not err, (good, who, err)


@test
def test_decision_fingerprint_sees_inner_headings_and_link_labels(tmp: Path):
    """Отпечаток: первый заголовок и переименование цели ссылки он не видит, внутренний заголовок и подпись ссылки — видит."""
    KD = _mod("kb_decide")
    base = "---\ntitle: t\n---\n\n" + GOOD_BODY.replace("{n}", "0001")
    fp = KD.body_fingerprint(base)
    assert KD.body_fingerprint(base.replace("# DR-0001: Срок", "# DR-0001: Другое имя", 1)) == fp
    assert KD.body_fingerprint(base.replace("[[Заявка на поставку]]", "[[Заявка]]")) == fp
    assert KD.body_fingerprint(base.replace("Срок подтверждения — три рабочих дня.", "# Теперь пять дней\nСрок.")) != fp
    assert KD.body_fingerprint(base.replace("[[Заявка на поставку]]", "[[Заявка|заявку]]")) != fp
    assert KD.body_fingerprint(base.replace("Затронутые карточки: [[Заявка на поставку]]", "Затронутые карточки: нет")) != fp


@test
def test_decision_form_reads_real_headings_and_english_decisions(tmp: Path):
    """Форма: «Обработка отклонений — выбран» выбран, «Причины отказа» читается, список затронутых строками ниже; английское решение."""
    KD = _mod("kb_decide")
    body = ("# DR-0001: X\n\n## Контекст\nТекст [[Заявка]].\n\n## Рассмотренные варианты\n\n"
            "### 1. Обработка отклонений — выбран\n- Плюсы: а.\n\n### 2. Принятие всего подряд — отклонён\n"
            "- **Причины отказа:** слишком шумно.\n\n## Решение\nОтклонения обрабатываем.\n\n## Следствия\n"
            "- Затронутые карточки:\n  - [[Заявка]]\n  - [[Заявка на поставку]]\n")
    text = "---\ntitle: t\nstatus: proposed\n---\n\n" + body
    assert KD.lint_decision(text, {"Заявка", "Заявка на поставку"}) == [], KD.lint_decision(text, {"Заявка", "Заявка на поставку"})
    assert [v["chosen"] for v in KD.variants(body)] == [True, False]
    assert KD.affected(body) == ["Заявка", "Заявка на поставку"]
    en = ("# DR-0002: Pick a bus\n\n## Context\nText [[Bus]].\n\n## Considered options\n\n### 1. Kafka — chosen\n- Pros: fast.\n\n"
          "### 2. Rabbit — rejected\n- Reason for rejection: ops cost.\n\n## Decision\nKafka.\n\n## Consequences\n- Affected cards: [[Bus]]\n")
    assert KD.lint_decision("---\ntitle: t\nstatus: proposed\n---\n\n" + en, {"Bus"}) == []


@test
def test_decide_survives_hostile_model_answers(tmp: Path):
    """Ответ модели с кавычками в заголовке, строками вместо списков, «chosen: "false"», чужими и слабыми цитатами."""
    R = _mod("agent_runner")
    root = _decide_project(tmp)
    src = "Raw/meetings/2026-10-01_срок/protocol.md"
    ans = _decide_answer(title='Срок: "три" дня', consequences="Описание процесса приводится к ТЗ.", affects="Заявка на поставку")
    ans["options"][0]["chosen"] = "false"                                   # строка «false» истиной не считается
    ans["options"].append({"name": "Просто слабая цитата", "chosen": False, "why_not": "-", "quote": "----------"})
    ans["options"].append({"name": "Чужая цитата", "chosen": False, "why_not": "-",
                           "quote": ans["options"][1]["quote"]})            # цитата другого варианта
    ans["options"].append({"name": "Общая фраза", "chosen": False, "why_not": "-", "quote": "Обсуждали срок"})
    res = R.run_decide(_decide_cfg(), str(root), src, "", "@analyst", [], True, call=_decide_call(ans, []), today="2026-10-19")
    assert res["ok"] and res["written"], res                                # кавычки в заголовке шаблон принял
    text = (root / res["path"]).read_text(encoding="utf-8")
    assert '"' not in text.splitlines()[1].split(":", 1)[1].strip('" ') and "# DR-0001: Срок: три дня" in text, text[:400]
    assert text.count("\n- Описание процесса приводится к ТЗ.") == 1 and "\n- О\n" not in text
    assert "### 1. Три рабочих дня — отклонён" in text, "строка «false» стала выбором"
    under = text.split("## Под вопросом", 1)[1]
    for name in ("Просто слабая цитата", "Чужая цитата", "Общая фраза"):
        assert f"Вариант «{name}»" in under, (name, under)
    assert "Вариант «Пять рабочих дней»" not in under, "своя цитата принята за чужую"
    assert "Затронутые карточки: [[Заявка на поставку]]" in text and "нет в базе" not in under


@test
def test_decide_accept_and_reject_cannot_both_win(tmp: Path):
    """Одновременные принять и отклонить: выигрывает один, второй видит новое состояние."""
    KD = _mod("kb_decide")
    root = _decision_project(tmp)
    _dr(root)
    os.chdir(root)
    results = []

    def go(reject: bool):
        results.append(KD.decide(".", "DR-0001", "@analyst", reject, "причина" if reject else "")[0])

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(go, (False, True)))
    assert sorted(results) == [0, 1], results
    final = (root / "AuroraKnowledgeDB/Decisions/DR-0001-срок.md").read_text(encoding="utf-8")
    assert ("status: accepted" in final) != ("status: rejected" in final), final[:300]
    assert KD.find_dr(".", "²")[1], "надстрочная цифра принята за номер"


@test
def test_scrub_keeps_an_accepted_decision_sealed(tmp: Path):
    """`kb:scrub --apply` закрывает ПДн маркером в принятом решении и пересчитывает отпечаток; изменённое руками не отмывает."""
    KD = _mod("kb_decide")
    root = _decision_project(tmp)
    path = _dr(root, body=GOOD_BODY.replace("Срок подтверждения — три рабочих дня.",
                                            "Срок подтверждения — три рабочих дня. Контакт: ivan.petrov@example.com"))
    run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=0)
    assert not KD.body_changed(path.read_text(encoding="utf-8"))
    cp = run("kb_scrub.py", "--apply", "--allow-dirty", "--force", cwd=root)
    text = path.read_text(encoding="utf-8")
    assert "ivan.petrov@example.com" not in text and "[ПДн" in text, (cp.stdout, text[-500:])
    assert not KD.body_changed(text), "обезличивание сломало отпечаток принятого решения"
    assert "обезличено (kb:scrub)" in text.split("## История", 1)[1]
    lint = run("kb_lint.py", cwd=root)
    assert "тело принятого решения" not in lint.stdout, lint.stdout
    # изменённое руками тело не «отмывается» пересчётом
    tampered = text.replace("подписанный документ", "что угодно")
    assert KD.body_changed(tampered)
    laundered = KD.reseal(tampered, tampered + "\nещё слово", "обезличено")
    assert KD.body_changed(laundered), "reseal отмыл правку тела"


# ------------------------------------------------------------------ BL-KBA-17: маска ключа Requirement Yogi

MASK = "<СТРАНА>.<ПРОЕКТ>.US-x.x.x"


@test
def test_ref_mask_accepts_whole_keys_of_the_kind_only(tmp: Path):
    """Маска вида: ключ целиком по маске — да; короче, длиннее, чужого вида и часть длинного — нет; негодные маски называются."""
    ID = _mod("id_registry")
    rx = ID.mask_regex(MASK)
    assert rx.fullmatch("RU.PRJ.US-1.1.1") and rx.fullmatch("KZ.AB_1.US-10.2.33")
    for bad in ("RU.PRJ.US-1.1", "RU.PRJ.US-1.1.1.2", "RU.PRJ.AC-1.1.1", "PRJ.US-1.1.1", "RU.PRJ.US-a.1.1"):
        assert not rx.fullmatch(bad), bad
    text = "Реализует RU.PRJ.US-1.1.1. Также RU.PRJ.US-1.1.12, RU.PRJ.AC-1.1.1 и RU.PRJ.US-1.1.1.2."
    assert ID.find_keys(text, rx) == ["RU.PRJ.US-1.1.1", "RU.PRJ.US-1.1.12"], ID.find_keys(text, rx)
    like = ID.similar_regex(MASK)
    assert ID.find_keys("RU.PRJ.US-1.1, US-5 и RU.PRJ.AC-2.1", like) == ["RU.PRJ.US-1.1"]      # US-5 — ссылка по номеру
    assert ID.mask_problem("") == "" and ID.mask_problem(MASK) == ""
    for bad in ("US-1", "x.x", "a b-x", 'US-"x"', "<a b>-x", "<A><B><C><D>.US-x"):
        assert ID.mask_problem(bad), bad
    assert ID.mask_regex("US-1") is None and ID.mask_regex("") is None
    # собственный ключ документа: строка «Код» важнее порядка
    doc = "| Реализует | RU.PRJ.US-9.9.9 |\n| Код | RU.PRJ.US-1.1.1 |\n"
    assert ID.own_key(doc, MASK) == "RU.PRJ.US-1.1.1"
    assert ID.own_key("Ссылка RU.PRJ.US-2.2.2 и RU.PRJ.US-3.3.3", MASK) == "RU.PRJ.US-2.2.2"
    assert ID.own_key("ключа нет", MASK) == "" and ID.own_key("RU.PRJ.US-1.1.1", "") == ""
    # строка «Код» есть, а ключа на ней нет: чужой ключ ниже собственным не становится
    assert ID.own_key("| Код | ключ присвоит реестр |\n\nЗависит от RU.PRJ.US-1.1.0.", MASK) == ""


@test
def test_make_puts_the_found_key_into_the_passport_and_never_asks_the_model_to_invent_it(tmp: Path):
    """Вид с маской: ключ из текста — в паспорт (`ref_key`, `ref_scheme`), нет ключа — нет поля; модели велено не выдумывать номер."""
    A = _mod("agent_core")
    R = _mod("agent_runner")
    root = _make_project_with_kind(tmp)
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip() + f'\n    ref_mask: "{MASK}"\n', encoding="utf-8")
    from harness import card
    for i in range(5):
        card(root, f"Concepts/Заявка-{i}.md", f"Заявка проходит статусы. Срок десять дней. [[Заявка-{(i + 1) % 5}]]",
             status="knowledge", kind="knowledge")
    seen: list = []

    def run_with(draft: str) -> str:
        def fake(cfg_, role, messages, **kw):
            seen.append((role, messages, kw))
            ok = {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": []}
            if role == "planner":
                return {**ok, "text": json.dumps({"questions": [], "plan": "1. Описание"}, ensure_ascii=False)}
            if role == "critic":
                return {**ok, "text": json.dumps({"ok": True, "issues": []}, ensure_ascii=False)}
            if role == "qa":
                return {**ok, "text": "ВЕРДИКТ: ЧИСТО"}
            return {**ok, "text": draft}
        cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
        r = R.run_make(cfg, str(root), "us", "срок заявки десять дней " + draft[:5], "", "", True, call=fake)
        assert r["ok"], r
        return (root / R.load_session(str(root), r["sid"])["path"]).read_text(encoding="utf-8")

    with_key = run_with("| Код | RU.PRJ.US-1.1.1 |\n| Реализует | RU.PRJ.US-9.9.9 |\n\nТекст заявки.")
    head = with_key.split("---", 2)[1]
    assert "ref_key: RU.PRJ.US-1.1.1" in head and "ref_scheme: ry" in head, head
    no_key = run_with("| Код | ключ присвоит реестр |\n\nТекст заявки.")
    assert "ref_key" not in no_key.split("---", 2)[1]
    worker = next(m for role, m, _kw in seen if role == "worker")
    assert "заводит реестр" in worker[0]["content"] and MASK in worker[0]["content"], "модели не запрещено придумывать ключ"


@test
def test_kb_lint_names_keys_that_look_like_the_kind_but_miss_the_mask(tmp: Path):
    """`kb:lint`: «US-1.1» вместо «US-1.1.1» в артефакте и на странице зеркала — предупреждение; верные ключи и чужие виды — нет."""
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip() + f'\nartifacts:\n  us:\n    template: T/us.md\n'
                   f'    out: Artifacts/us\n    ref_mask: "{MASK}"\n', encoding="utf-8")
    clean = run("kb_lint.py", cwd=root)
    assert "не по маске" not in clean.stdout, clean.stdout
    _touch(root, "Artifacts/us/Верная.md", "---\ntitle: в\n---\n\n| Код | RU.PRJ.US-1.1.1 |\nСм. RU.PRJ.AC-2.2.2.\n")
    _touch(root, "Artifacts/us/Кривая.md", "---\ntitle: к\n---\n\n| Код | RU.PRJ.US-1.1 |\nРеализует RU.PRJ.US-2.2.2.\n")
    _touch(root, "Sources/Confluence/US-3.md", '---\ntitle: "US-3"\nry_defines: [RU.PRJ.US-3.3]\n---\n\nТекст.\n')
    _touch(root, "Sources/Confluence/US-4.md", '---\ntitle: "US-4"\nry_defines: [RU.PRJ.US-4.4.4, RU.PRJ.AC-4.4.4]\n---\n\nТекст.\n')
    cp = run("kb_lint.py", cwd=root)
    assert "ключей не по маске вида в 2 файлах" in cp.stdout, cp.stdout
    assert "Artifacts/us/Кривая.md" in cp.stdout and "REF-02" in cp.stdout and "RU.PRJ.US-1.1" in cp.stdout, cp.stdout
    assert "Sources/Confluence/US-3.md" in cp.stdout and "REF-03" in cp.stdout, cp.stdout
    block = cp.stdout.split("### ключи Requirement Yogi")[1]
    assert "Верная.md" not in block and "US-4.md" not in block, block
    assert cp.returncode == clean.returncode, "предупреждение изменило код возврата"
    assert "не по маске" not in run("kb_lint.py", "--only", "AuroraKnowledgeDB/x.md", cwd=root).stdout
    # make:kinds называет негодную маску и показывает годную ассистенту
    cp = run("make_kinds.py", "--kind", "us", cwd=root)
    assert MASK in cp.stdout and "не выдумывайте номер" in cp.stdout, cp.stdout
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(MASK, "US-1"), encoding="utf-8")
    cp = run("make_kinds.py", cwd=root)
    assert cp.returncode == 1 and "маска ключа" in cp.stdout, cp.stdout


@test
def test_kinds_form_carries_the_ref_mask_field_and_publish_warns_without_a_key(tmp: Path):
    """Поле `ref_mask` в форме «Артефакты» и каталогах строк, переживает запись панели; публикация без ключа предупреждает."""
    from harness import panel_sources
    src = panel_sources()
    for key in ("kinds.f_ref_mask", "kinds.ref_mask_hint"):
        assert src.count(f'"{key}"') >= 3, (key, src.count(f'"{key}"'))
    assert re.search(r'ref_mask:"",[^}]*task:\{\}', src)
    MK = _mod("make_kinds")
    assert "ref_mask" in MK.FIELDS
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    root = tmp / "проект"
    (root / "Templates").mkdir(parents=True)
    (root / "Templates/US.md").write_text("шаблон", encoding="utf-8")
    (root / "aurora.config.yaml").write_text('project:\n  name: Т\natlassian:\n  confluence:\n'
                                             '    base_url: "https://wiki.example.com"\n    space: "T"\n', encoding="utf-8")
    res = ck.kinds_write(str(root), {"us": {"title": "История", "template": "Templates/US.md", "out": "Artifacts/us",
                                            "ref_mask": MASK, "task": {}}})
    assert res.get("ok"), res
    assert ck.kinds_read(str(root))["review_profiles"] == ["us", "alg"], "панель не знает профилей ревью"
    assert MK.read_kinds(str(root))["us"]["ref_mask"] == MASK, "маска не дожила до чтения"
    _touch(root, "Artifacts/us/Без-ключа.md", "---\ntitle: к\ntype: us\n---\n\n# История\n")
    _touch(root, "Artifacts/us/С-ключом.md", "---\ntitle: к\ntype: us\nref_key: RU.PRJ.US-1.1.1\n---\n\n# История\n")
    env = {**os.environ, "CONFLUENCE_PAT": "", "CONFLUENCE_TOKEN": ""}              # сети в проверке нет

    def publish(name: str) -> str:
        cp = subprocess.run([sys.executable, str(SCRIPTS / "publish_doc.py"), name], cwd=str(root), env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return cp.stdout + cp.stderr

    assert "В документе нет ключа по маске вида" in publish("Artifacts/us/Без-ключа.md")
    assert "ключа по маске" not in publish("Artifacts/us/С-ключом.md")
    _touch(root, "Artifacts/us/Ключ-в-тексте.md", "---\ntitle: к\ntype: us\n---\n\n| Код | RU.PRJ.US-1.1.1 |\n")
    assert "ключа по маске" not in publish("Artifacts/us/Ключ-в-тексте.md"), "ключ есть в документе, шапка не пересобрана"


# ------------------------------------------------------------------ BL-TEAM-03: доработка по независимому просмотру

@test
def test_review_file_and_stamp_stay_inside_the_project_and_its_own_artifacts(tmp: Path):
    """`--file` не выходит за проект ни путём, ни `..`, ни ссылкой; шапку получают только свои артефакты (по реальному пути)."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    outside = tmp / "чужой.md"
    outside.write_text("# Чужая заметка\n\nЧУЖОЙ-ТЕКСТ\n", encoding="utf-8")
    seen: list = []
    for bad in ("../чужой.md", str(outside), "Artifacts/../../чужой.md"):
        cp = run("review_run.py", "--file", bad, cwd=root)
        assert cp.returncode == 1 and "нет файла" in (cp.stderr + cp.stdout), (bad, cp.returncode, cp.stderr)
        rec = rr.review_file({"request_timeout": 5}, cl, bad, root=str(root), runs=1, call=_review_stub(cl, seen=seen))
        assert rec["verdict"] == "не оценено" and rec["status"] == "missing", (bad, rec)
    assert not seen, "модель вызвана для файла вне проекта"
    os.chdir(root)
    _touch(root, "Sources/Confluence/US-1.md", "---\ntitle: US-1\n---\n\nТекст.\n")
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-1.1.1-приём.md", root=str(root), runs=1, call=_review_stub(cl))
    before = (root / "Sources/Confluence/US-1.md").read_text(encoding="utf-8")
    for sneaky in ("Artifacts/../Sources/Confluence/US-1.md", "Artifacts/../AuroraKnowledgeDB/Concepts/Заявка.md"):
        assert rr.stamp_refusal(sneaky, rec), sneaky
        assert rr.stamp_review(sneaky, rec, cl) is False, sneaky
    assert (root / "Sources/Confluence/US-1.md").read_text(encoding="utf-8") == before
    link = root / "Artifacts/us/ссылка.md"
    try:
        link.symlink_to(root / "AuroraKnowledgeDB/Concepts/Заявка.md")
    except (OSError, NotImplementedError):
        link = None                                                     # Windows без прав на ссылки
    if link is not None:
        assert rr.stamp_review("Artifacts/us/ссылка.md", rec, cl) is False, "шапка записана сквозь ссылку"
        assert "review_sha" not in (root / "AuroraKnowledgeDB/Concepts/Заявка.md").read_text(encoding="utf-8")
    # нормальный путь с лишним «./» и «..» внутри своей папки приводится к реальному и штампуется
    assert rr.stamp_review("./Artifacts/us/../us/US-1.1.1-приём.md", rec, cl) is True


@test
def test_review_file_reads_dotted_names_long_mirror_heads_and_markup_quotes(tmp: Path):
    """`based_on` с кодами через точку, зеркало с сотнями ключей в шапке, цитаты из таблиц и с многоточием, длинная evidence."""
    import time
    from harness import _review_checklist, _review_module, card
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    card(root, "Concepts/US-1.1.1 Приём заявки.md", "Приём заявки — точный контекст.", status="knowledge", kind="knowledge")
    card(root, "Concepts/US-1.1.md", "ЛОЖНЫЙ-КОНТЕКСТ другой истории.", status="knowledge", kind="knowledge")
    keys = ", ".join(f"RU.PRJ.REQ-{i:03d}" for i in range(1, 301))
    _touch(root, "Sources/Confluence/Требования.md", f'---\ntitle: "Требования"\nry_defines: [{keys}]\n---\n\nТРЕБОВАНИЕ-300 текст.\n')
    _touch(root, "Artifacts/us/US-5.md",
           '---\ntype: us\nbased_on: ["[[US-1.1.1 Приём заявки]]"]\n---\n\n# История\n\n## Предусловия\n\nРеализует RU.PRJ.REQ-300.\n')
    seen: list = []
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-5.md", root=str(root), runs=1, call=_review_stub(cl, seen=seen))
    titles = sorted(ln["title"] for ln in rec["linked"])
    assert titles == ["US-1.1.1 Приём заявки", "Требования"], titles
    prompt = "\n".join(m["content"] for m in seen[0])
    assert "точный контекст" in prompt and "ЛОЖНЫЙ-КОНТЕКСТ" not in prompt and "ТРЕБОВАНИЕ-300" in prompt
    # разметка таблицы и многоточие в цитате не делают её выдуманной
    hay = rr.quote_key("| **Как** | Оператор системы |\n| **Я хочу** | принять заявку из смежной системы |")
    ok = {"A": {"v": "no", "evidence": "«Как Оператор системы»"},
          "B": {"v": "no", "evidence": "«Оператор системы … принять заявку из смежной»"},
          "C": {"v": "no", "evidence": "«Оператор системы … выдуманный хвост которого нет»"}}
    assert rr.drop_unquoted(ok, hay) == 1 and ok["A"]["v"] == "no" and ok["B"]["v"] == "no" and ok["C"]["v"] == "unknown", ok
    # длинная evidence из кавычек не вешает разбор
    big = {"v": "no", "evidence": "« " * 20000}
    started = time.process_time()
    rr.drop_unquoted({"A": big}, hay)
    assert time.process_time() - started < 5, "разбор длинной evidence слишком долгий"


@test
def test_stamp_keeps_crlf_and_bom_and_does_not_touch_non_utf8(tmp: Path):
    """Шапка ревью: CRLF и BOM файла сохраняются; файл не в UTF-8 не меняется и не роняет прогон."""
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    os.chdir(root)
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/US-1.1.1-приём.md", root=str(root), runs=1, call=_review_stub(cl))
    crlf = root / "Artifacts/us/crlf.md"
    crlf.write_bytes(("﻿---\r\ntitle: к\r\ntype: us\r\n---\r\n\r\n# История\r\n\r\nКак оператор.\r\n").encode("utf-8"))
    assert rr.stamp_review("Artifacts/us/crlf.md", rec, cl) is True
    raw = crlf.read_bytes()
    assert raw.startswith("﻿---".encode("utf-8")) and raw.count(b"\r\n") == raw.count(b"\n"), raw[:200]
    assert b"review_sha: " in raw and "# История\r\n\r\nКак оператор.\r\n".encode("utf-8") in raw
    old = root / "Artifacts/us/cp1251.md"
    old.write_bytes("# История\n\nКак оператор.\n".encode("cp1251"))
    assert rr.stamp_review("Artifacts/us/cp1251.md", rec, cl) is False
    assert old.read_bytes() == "# История\n\nКак оператор.\n".encode("cp1251")


@test
def test_publish_gate_matches_the_kind_in_any_case_and_old_review_engine(tmp: Path):
    """Ворота включаются и при `type: US`; старый review_run без publish_gate — причина словами, а не трассировка."""
    root = _review_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip() + '\nartifacts:\n  us:\n    template: T/us.md\n'
                   '    out: Artifacts/us\n    review: us\n', encoding="utf-8")
    _touch(root, "Artifacts/us/Верхний.md", "---\ntitle: в\ntype: US\n---\n\n# История\n")
    cp = run("publish_doc.py", "Artifacts/us/Верхний.md", cwd=root, expect_rc=2)
    assert "review-missing" in cp.stderr, cp.stderr
    (root / ".aurora/scripts/review_run.py").write_text("# старый движок: publish_gate ещё нет\n", encoding="utf-8")
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/publish_doc.py"), "Artifacts/us/Верхний.md"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 2 and "review-engine" in cp.stderr and "Traceback" not in cp.stderr, (cp.returncode, cp.stderr)


# ------------------------------------------------------------------ BL-KBA-17: сценарии Gherkin и их идентификаторы

FEATURE = """\
# language: ru
@RU.PRJ.US-1.1.1
Функционал: Приём заявки
  Заявка принимается из смежной системы.

  Предыстория:
    Дано система доступна

  @id:SC-0001 @covers:RU.PRJ.AC-1.1.1 @positive @auto
  Сценарий: Заявка принята
    Когда оператор отправляет заявку
    Тогда заявка принята

  # без идентификатора и без критерия
  @negative
  Сценарий: Пустая заявка отклонена
    Когда оператор отправляет пустую заявку
    Тогда система отвечает отказом

  Структура сценария: Разные каналы
    Когда оператор отправляет заявку по каналу "<канал>"
    Тогда заявка принята

    Примеры:
      | канал |
      | почта |
"""


@test
def test_feature_ids_parse_tags_and_name_what_is_missing(tmp: Path):
    """`ops:scenarios`: теги над ключевыми словами, русские ключевые слова, замечания SC-01…SC-07."""
    FI = _mod("feature_ids")
    parsed = FI.parse(FEATURE)
    assert [f["name"] for f in parsed["features"]] == ["Приём заявки"] and parsed["features"][0]["tags"] == ["@RU.PRJ.US-1.1.1"]
    names = [(s["name"], s["tags"], s["outline"]) for s in parsed["scenarios"]]
    assert names[0] == ("Заявка принята", ["@id:SC-0001", "@covers:RU.PRJ.AC-1.1.1", "@positive", "@auto"], False), names
    assert names[1][1] == ["@negative"], names[1]                         # комментарий между тегами и словом тегов не сбрасывает
    assert names[2] == ("Разные каналы", [], True), names[2]              # «Примеры:» — не сценарий
    root = make_project(tmp)
    _touch(root, "Artifacts/tests/features/приём/US-1.1.1.feature", FEATURE)
    _touch(root, "Artifacts/ac/Крит.md", "---\ntitle: к\nref_key: RU.PRJ.AC-1.1.1\n---\n\nТекст.\n")
    res = FI.analyse(str(root))
    codes = sorted((f["id"], f["msg"][:30]) for f in res["findings"])
    assert [c for c, _m in codes].count("SC-01") == 2 and [c for c, _m in codes].count("SC-05") == 2, codes
    assert "SC-06" not in [c for c, _m in codes], "ключ критерия известен по ref_key артефакта"
    assert res["ids"] == {"SC-0001": ["Artifacts/tests/features/приём/US-1.1.1.feature:10"]}, res["ids"]
    # неизвестный критерий и feature без ключа истории
    _touch(root, "Artifacts/tests/features/второй.feature",
           "Feature: Без ключа\n\n  @id:SC-0002 @covers:RU.PRJ.AC-9.9.9\n  Scenario: Один\n    Given x\n")
    res = FI.analyse(str(root))
    found = {(f["id"], f["path"].rsplit("/", 1)[-1]) for f in res["findings"]}
    assert ("SC-06", "второй.feature") in found and ("SC-07", "второй.feature") in found, found
    cp = run("feature_ids.py", cwd=root)
    assert cp.returncode == 1 and "SC-01" in cp.stdout, cp.stdout


@test
def test_feature_ids_assign_is_a_preview_until_apply_and_keeps_line_endings(tmp: Path):
    """`--assign`: без `--apply` номера не выдаются и файл не меняется; с ним — тег в строку тегов или отдельной строкой."""
    FI = _mod("feature_ids")
    IDR = _mod("id_registry")
    root = make_project(tmp)
    path = _touch(root, "Artifacts/tests/features/приём.feature", FEATURE)
    crlf = _touch(root, "Artifacts/tests/features/crlf.feature", "")
    crlf.write_bytes(FEATURE.replace("SC-0001", "SC-0007").replace("\n", "\r\n").encode("utf-8"))
    before = path.read_text(encoding="utf-8")
    cp = run("feature_ids.py", "--assign", cwd=root, expect_rc=0)
    assert "предпросмотр" in cp.stdout and "SC-????" in cp.stdout, cp.stdout
    assert path.read_text(encoding="utf-8") == before and IDR.peek("SC", str(root)) == "SC-0008", "предпросмотр занял номер"
    run("feature_ids.py", "--assign", "--apply", cwd=root, expect_rc=0)
    text = path.read_text(encoding="utf-8")
    assert "@negative @id:SC-0010" in text, text                          # дописан в строку тегов над сценарием
    assert "  @id:SC-0011\n  Структура сценария: Разные каналы" in text, text   # своей строкой, с тем же отступом
    raw = crlf.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "перевод строк CRLF потерян"
    assert FI.analyse(str(root))["ids"].keys() >= {"SC-0001", "SC-0007", "SC-0008", "SC-0009", "SC-0010", "SC-0011"}
    assert not [f for f in FI.analyse(str(root))["findings"] if f["id"] == "SC-01"], "после --apply остались сценарии без @id"
    again = run("feature_ids.py", "--assign", "--apply", cwd=root, expect_rc=0)
    assert "Проставлено: 0" in again.stdout, again.stdout


@test
def test_scenario_ids_are_unique_across_files_and_visible_to_next_id(tmp: Path):
    """Один `@id` в двух файлах — SC-03, `kb:next-id --check` и `kb:lint`; номер выдаётся после занятых; копию в той же строке не считает дважды."""
    FI = _mod("feature_ids")
    IDR = _mod("id_registry")
    root = make_project(tmp)
    _touch(root, "Artifacts/tests/a.feature", "@RU.PRJ.US-1.1.1\nFeature: А\n\n  @id:SC-0005 @covers:RU.PRJ.AC-1.1.1\n  Scenario: Один\n    Given x\n")
    assert IDR.peek("SC", str(root)) == "SC-0006"
    _touch(root, "Artifacts/tests/b.feature", "@RU.PRJ.US-1.1.1\nFeature: Б\n\n  @id:SC-0005 @covers:RU.PRJ.AC-1.1.1\n  Scenario: Копия\n    Given x\n")
    res = FI.analyse(str(root))
    assert [f["id"] for f in res["findings"] if f["id"] == "SC-03"] == ["SC-03"], res["findings"]
    assert list(IDR.duplicates(str(root))) == ["SC-0005"], IDR.duplicates(str(root))
    cp = run("id_registry.py", "--check", cwd=root, expect_rc=1)
    assert "SC-0005" in cp.stdout, cp.stdout
    assert "SC-0005" in run("kb_lint.py", cwd=root).stdout
    assert run("id_registry.py", "SC", cwd=root).stdout.strip() == "SC-0006"
    _touch(root, "Artifacts/tests/c.feature", "@RU.PRJ.US-2.2.2\nFeature: В\n\n  @id:SC-0001 @id:SC-0002\n  Scenario: Два тега\n    Given x\n")
    assert "SC-02" in [f["id"] for f in FI.analyse(str(root))["findings"]]


# ------------------------------------------------------------------ BL-KBA-17: покрытие требований по ключам Requirement Yogi

def _mirror_page(root: Path, name: str, title: str, defines=(), links=()) -> None:
    _touch(root, f"Sources/Confluence/{name}.md",
           f'---\ntitle: "{title}"\nry_defines: [{", ".join(defines)}]\nry_links: [{", ".join(links)}]\n---\n\nТекст страницы.\n')


def _coverage_project(tmp: Path) -> Path:
    root = make_project(tmp)
    _mirror_page(root, "REQ-010", "REQ-010 Срок", defines=["RU.PRJ.REQ-010"])
    _mirror_page(root, "REQ-011", "REQ-011 Канал", defines=["RU.PRJ.REQ-011"])
    _mirror_page(root, "REQ-012", "REQ-012 Нигде", defines=["RU.PRJ.REQ-012"])
    _mirror_page(root, "US-1.1.1", "US-1.1.1 Приём", defines=["RU.PRJ.US-1.1.1"], links=["RU.PRJ.REQ-010"])
    _mirror_page(root, "AC-1.1.1", "AC-1.1.1 Критерии приёма")
    _mirror_page(root, "US-2.2.2", "US-2.2.2 Канал", defines=["RU.PRJ.US-2.2.2"], links=["RU.PRJ.REQ-011"])
    _mirror_page(root, "US-3.3.3", "US-3.3.3 Без требования", defines=["RU.PRJ.US-3.3.3"])
    _mirror_page(root, "US-4.4.4", "US-4.4.4 Висячая", defines=["RU.PRJ.US-4.4.4"], links=["RU.PRJ.REQ-099"])
    return root


@test
def test_coverage_names_the_gaps_between_requirement_story_and_criteria(tmp: Path):
    """`ops:coverage`: история без требования, требование без истории и без AC, висячий ключ; полные цепочки не в дырах."""
    CV = _mod("kb_coverage")
    root = _coverage_project(tmp)
    os.chdir(root)
    cov = CV.coverage(".")
    gaps = cov["gaps"]
    assert cov["requirements"] == 3 and cov["stories"] == 4, cov
    assert [g["title"] for g in gaps["story_without_requirement"]] == ["US-3.3.3 Без требования"], gaps
    assert [g["key"] for g in gaps["requirement_without_story"]] == ["RU.PRJ.REQ-012"], gaps
    assert [g["key"] for g in gaps["requirement_without_ac"]] == ["RU.PRJ.REQ-011"], gaps       # US-2.2.2 без страницы AC
    assert [g["key"] for g in gaps["dangling_requirement"]] == ["RU.PRJ.REQ-099"], gaps
    assert cov["with_story"] == 2 and cov["with_ac"] == 1, cov
    cp = run("kb_coverage.py", cwd=root, expect_rc=1)
    for need in ("Истории без ссылки на требование: 1", "Требования без истории: 1", "Требования без критериев приёмки: 1",
                 "RU.PRJ.REQ-099", "Пункты ТЗ или контракта без ключа"):
        assert need in cp.stdout, (need, cp.stdout)
    assert "REQ-010" not in cp.stdout.split("## Требования без истории")[1].split("##")[0], "полная цепочка попала в дыры"
    j = json.loads(run("kb_coverage.py", "--json", cwd=root).stdout)
    assert j["requirements"] == 3
    # типы ключей настраиваются: если требования у проекта называются иначе
    assert CV.coverage(".", ("NOSUCH",))["requirements"] == 0


@test
def test_coverage_requirement_wave_lists_stories_criteria_and_artifacts(tmp: Path):
    """`--requirement`: истории, их AC и артефакты (по паспорту и по тексту), которые называют требование; чужое не попадает."""
    CV = _mod("kb_coverage")
    root = _coverage_project(tmp)
    _touch(root, "Artifacts/us/Паспорт.md", "---\ntitle: п\nimplements: [REQ-010]\n---\n\nТекст.\n")
    _touch(root, "Artifacts/us/Ключ.md", "---\ntitle: к\nimplements: [RU.PRJ.REQ-010]\n---\n\nТекст.\n")
    _touch(root, "Artifacts/us/Текст.md", "---\ntitle: т\n---\n\nСм. требование RU.PRJ.REQ-010 и RU.PRJ.REQ-0101.\n")
    _touch(root, "Artifacts/us/Чужое.md", "---\ntitle: ч\nimplements: [REQ-011]\n---\n\nСм. RU.PRJ.REQ-0101.\n")
    os.chdir(root)
    w = CV.wave(".", "RU.PRJ.REQ-010")
    assert w["defined_on"].endswith("REQ-010.md") and [s.rsplit("/", 1)[-1] for s in w["stories"]] == ["US-1.1.1.md"], w
    assert [c.rsplit("/", 1)[-1] for c in w["criteria"]] == ["AC-1.1.1.md"], w
    got = {a["path"].rsplit("/", 1)[-1]: a["how"] for a in w["artifacts"]}
    assert got == {"Ключ.md": "паспорт", "Текст.md": "текст"}, got           # номер карточки REQ-010 — другое пространство
    cp = run("kb_coverage.py", "--requirement", "RU.PRJ.REQ-010", cwd=root, expect_rc=0)
    assert "Истории, которые на него ссылаются: 1" in cp.stdout and "Это список для пересмотра" in cp.stdout, cp.stdout
    cp = run("kb_coverage.py", "--requirement", "RU.PRJ.REQ-777", cwd=root, expect_rc=1)
    assert "нигде не объявлен" in cp.stdout, cp.stdout
    empty = tmp / "пустой"
    empty.mkdir()
    cp = subprocess.run([sys.executable, str(SCRIPTS / "kb_coverage.py")], cwd=str(empty), capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 2 and "нет Sources/Confluence" in cp.stderr, (cp.returncode, cp.stderr)


# ------------------------------------------------------------------ доработка по итоговым просмотрам партии

@test
def test_gate_names_numbers_and_remote_markup_in_decisions(tmp: Path):
    """Имена с невидимыми знаками и полной шириной, датированные DR, огромные числа, картинки и адреса в тексте модели."""
    KD = _mod("kb_decide")
    R = _mod("agent_runner")
    for bad in ("Cl\u200baude", "Cl\u00adaude", "An\u200dthropic", "\uff23\uff4c\uff41\uff55\uff44\uff45", "C l a u d e", "my llm", "gpt-4o"):
        who, err = KD.human_name(bad)
        assert err and not who, bad
    for good in ("Stallman", "Hillman", "Hellmann", "Willmott", "Ольга Клодт", "G. P. Thompson", "Opuscule"):
        who, err = KD.human_name(good)
        assert who and not err, good
    assert KD.dr_number("DR-2026-10-19-заметка.md") == -1 and KD.dr_number("DR-0007-x.md") == 7
    assert KD.dr_number("DR-" + "9" * 5000 + "-x.md") == -1 and KD.dr_number("DR-4.2-x.md") == -1
    root = _decision_project(tmp)
    assert KD.find_dr(str(root), "9" * 5000)[1], "огромное число принято за номер"
    dirty = R.decide_clean("![x](https://a.example/?d=SECRET) <img src=//a.example/x> текст www.a.example/p и https://b.example/q")
    assert dirty == "x текст и", dirty
    # источник решения: огромный номер вопроса и answer_source из тела карточки вопроса
    res = R.run_decide(_decide_cfg(), str(root), "Q-" + "9" * 5000, "", "@analyst", [], False, call=_decide_call(_decide_answer(), []))
    assert not res["ok"] and "нет в проекте" in res["why"], res
    _touch(root, "AuroraKnowledgeDB/Questions/Q-002-тело.md",
           '---\ntitle: "Q-002"\ntype: question\nstatus: draft\nq_status: answered\nblocks: []\n---\n\n# Q-002\n\n'
           "answer_source: Raw/meetings/2026-10-01_срок/protocol.md\n")
    seen: list = []
    res = R.run_decide(_decide_cfg(), str(root), "Q-2", "", "@analyst", [], False, call=_decide_call(_decide_answer(), seen))
    assert res["ok"] and "Руководитель проекта" not in seen[0][1][0]["content"], "строка из тела выбрала файл"
    # REQ с дробным номером не становится требованием REQ-4
    links = R.passport_lines({"links": {}}, ["REQ-4.2 Название", "REQ-042-Срок"])
    assert links == ["implements: [REQ-042]"], links
    # --from с огромным токеном отвергается быстро
    import time
    started = time.process_time()
    _l, bad = R.parse_from([".a-" * 40000 + "x"])
    assert bad and time.process_time() - started < 2, "разбор огромного токена слишком долгий"


@test
def test_gate_decision_fingerprint_ignores_the_meeting_mark_and_known_cards_include_the_archive(tmp: Path):
    """Пометка «из встречи» ставится движком и отпечаток не меняет; затронутая карточка из архива существует."""
    KD = _mod("kb_decide")
    root = _decision_project(tmp)
    base = "---\ntitle: t\n---\n\n" + GOOD_BODY.replace("{n}", "0001")
    marked = base.replace("# DR-0001: Срок подтверждения заявки — три рабочих дня\n",
                          "# DR-0001: Срок подтверждения заявки — три рабочих дня\n\n> 🎙 Из встречи 01.10.2026\n", 1)
    assert KD.body_fingerprint(marked) == KD.body_fingerprint(base)
    _touch(root, "AuroraKnowledgeDB/_archive/Старая-карточка.md", "---\ntitle: с\n---\n\nТекст.\n")
    assert "Старая-карточка" in KD.known_names(str(root))


@test
def test_gate_registry_masks_ids_and_batches(tmp: Path):
    """Соседние переменные части маски отвергаются; виды с папкой вне проекта не обходятся; номера одной пачкой."""
    ID = _mod("id_registry")
    assert ID.mask_problem("<A><B>.US-x") and not ID.mask_problem("<A>.<B>.US-x.x")
    import time
    started = time.process_time()
    rx = ID.mask_regex("<A>.<B>.<C>.US-x")
    ID.find_keys("a" * 3000 + " ", rx)
    assert time.process_time() - started < 2
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip() + f'\nartifacts:\n  us:\n    template: T/us.md\n'
                   f'    out: ../outside\n    ref_mask: "{MASK}"\n', encoding="utf-8")
    assert ID.kinds_with_masks(str(root)) == {}, "папка результата вне проекта обходится линтером"
    got = ID.allocate_many("SC", 5, str(root))
    assert got == ["SC-0001", "SC-0002", "SC-0003", "SC-0004", "SC-0005"], got
    assert ID.allocate("SC", str(root)) == "SC-0006" and ID.allocate_many("SC", 0, str(root)) == []
    with ThreadPoolExecutor(max_workers=4) as ex:
        parts = list(ex.map(lambda _i: ID.allocate_many("SC", 5, str(root)), range(4)))
    flat = [x for p in parts for x in p]
    assert len(set(flat)) == 20, flat


@test
def test_gate_feature_ids_paths_line_breaks_and_comments(tmp: Path):
    """`ops:scenarios`: пути вне проекта, U+2028 и BOM в файле, комментарий после тегов, `Example:`, смешанные окончания."""
    FI = _mod("feature_ids")
    root = make_project(tmp)
    outside = tmp / "чужой.feature"
    outside.write_text("Feature: чужой\n\n  Scenario: Один\n    Given x\n", encoding="utf-8")
    for given in ("../чужой.feature", str(outside)):
        assert FI.feature_files(str(root), [given]) == [], given
    os.chdir(root)
    cp = run("feature_ids.py", "--assign", "--apply", "../чужой.feature", cwd=root)
    assert outside.read_text(encoding="utf-8").count("@id") == 0, "файл вне проекта изменён"
    odd = root / "Artifacts/tests/странный.feature"
    odd.parent.mkdir(parents=True, exist_ok=True)
    odd.write_bytes("\ufeff@RU.PRJ.US-1.1.1\nFeature: A\u2028продолжение описания\n\n  @negative # заметка\n  Scenario: Первый\n"
                    "    Given x\n\n  Example: Второй\n    Given y\n".encode("utf-8"))
    parsed = FI.parse(odd.read_text(encoding="utf-8"))
    assert [s["name"] for s in parsed["scenarios"]] == ["Первый", "Второй"], parsed
    assert parsed["features"][0]["tags"] == ["@RU.PRJ.US-1.1.1"], "BOM спрятал тег над Feature"
    run("feature_ids.py", "--assign", "--apply", cwd=root, expect_rc=0)
    text = odd.read_text(encoding="utf-8")
    assert "@negative @id:SC-0001 # заметка" in text, text                # тег — до комментария
    assert "  @id:SC-0002\n  Example: Второй" in text and text.startswith("\ufeff@RU"), text
    again = run("feature_ids.py", "--assign", "--apply", cwd=root, expect_rc=0)
    assert "Проставлено: 0" in again.stdout, again.stdout
    mixed = root / "Artifacts/tests/смешанный.feature"
    mixed.write_bytes(b"@RU.PRJ.US-2.2.2\r\nFeature: B\r\n\n  Scenario: One\r\n    Given x\r\n")
    cp = run("feature_ids.py", "--assign", "--apply", cwd=root, expect_rc=0)
    assert "смешанные окончания" in cp.stdout and b"@id" not in mixed.read_bytes(), cp.stdout


@test
def test_gate_review_file_bom_long_lines_atomic_stamp_and_remote_markup(tmp: Path):
    """BOM у зеркала, длинная строка с «ключами», шапка без остатков `.tmp`, картинки в отчёте ревью."""
    import time
    from harness import _review_checklist, _review_module
    rr = _review_module()
    cl = _review_checklist(rr)
    root = _review_project(tmp)
    os.chdir(root)
    (root / "Artifacts/us/bom.md").write_bytes("\ufeff---\ntitle: US-9 Страница\n---\n\nКак оператор я хочу принять документ.\n".encode("utf-8"))
    rec = rr.review_file({"request_timeout": 5}, cl, "Artifacts/us/bom.md", root=str(root), runs=1, call=_review_stub(cl))
    assert rec["status"] == "ok" and rec["profile"] == "us", rec
    started = time.process_time()
    reader = rr.FileReader(str(root))
    reader.links_of("AB.a." * 8000, {})
    assert time.process_time() - started < 3, "строка из «ключей» разбирается слишком долго"
    assert rr.stamp_review("Artifacts/us/bom.md", rec, cl, str(root)) is True
    assert not list((root / "Artifacts/us").glob("*.tmp")), "после записи шапки остался временный файл"
    cell = rr._cell("см. ![x](https://a.example/?d=SECRET) и <img src=//a.example/x> и [ссылку](http://b.example/q)")
    assert "a.example" not in cell and "b.example" not in cell and "SECRET" not in cell, cell


@test
def test_gate_publish_finds_the_kind_by_folder_in_any_case_and_with_bom(tmp: Path):
    """Ворота: вид по папке результата, `review: US # заметка`, файл с BOM."""
    root = _review_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip() + '\nartifacts:\n  us:\n    template: T/us.md\n'
                   '    out: Artifacts/us\n    review: US # ревью обязательно\n', encoding="utf-8")
    _touch(root, "Artifacts/us/Без-вида.md", "---\ntitle: в\n---\n\n# История\n")
    cp = run("publish_doc.py", "Artifacts/us/Без-вида.md", cwd=root, expect_rc=2)
    assert "review-missing" in cp.stderr, cp.stderr
    (root / "Artifacts/us/Бом.md").write_bytes("\ufeff---\ntitle: б\ntype: us\n---\n\n# История\n".encode("utf-8"))
    cp = run("publish_doc.py", "Artifacts/us/Бом.md", cwd=root, expect_rc=2)
    assert "review-missing" in cp.stderr, cp.stderr
    assert "ревью «" not in run("make_kinds.py", cwd=root).stdout, "комментарий и регистр в review сделали вид негодным"


@test
def test_gate_mcp_story_lint_stops_at_the_read_boundary(tmp: Path):
    """MCP `story_lint`/`review_file`: служебная папка и секреты внутри проекта не читаются."""
    M = _mod("aurora_mcp")
    root = _review_project(tmp)
    _touch(root, ".aurora/state/leak.md", "# служебное\n\n{{sk-live-0123}}\n")
    (root / ".env.aurora.local").write_text("TOKEN=СЕКРЕТНЫЙ-ТОКЕН-12345\n", encoding="utf-8")
    for tool in ("story_lint", "review_file"):
        for bad in (".aurora/state/leak.md", ".env.aurora.local"):
            try:
                out = M.call_tool(str(root), tool, {"path": bad, "profile": "spec"} if tool == "story_lint" else {"path": bad})
                err = False
            except M.ToolError as e:
                out, err = str(e), True
            assert err and "СЕКРЕТНЫЙ" not in out and "sk-live" not in out, (tool, bad, out)
