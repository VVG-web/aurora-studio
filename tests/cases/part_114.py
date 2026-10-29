"""Проверки движка Aurora, часть 114. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 4 «Записи в Raw/», пакет CT-13: решения, вопросы и рукописные справочники лежат мастер-файлами в
`Raw/decisions|questions|reference`, а в базе остаётся их зеркало. Здесь — папки и справки, команда `kb:record`
(`--new`, `--list`, `--check`), права редактора панели, код V-17 (поля цикла записи), свой цикл статуса вопроса,
замена решения (`kb:supersede`) и принятие (`kb:decide-accept`) на мастер-файлах, шаблон исправления в новом файле
`Raw/corrections`. Модель и сеть не используются; проекты — только во временных папках, настоящий HOME прогона подменён раннером.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

from cases import part_70 as P70
from cases import part_42 as P42
from harness import KIT, SCRIPTS, card, run, test

KB = "AuroraKnowledgeDB"
HOMES = {"decision": "Raw/decisions", "question": "Raw/questions", "reference": "Raw/reference"}


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _project(tmp: Path, git: bool = True) -> Path:
    return P70._project(tmp, git=git)


def _kr(root: Path, *args, expect=None):
    return run("kb_record.py", *args, cwd=root, expect_rc=expect)


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


def _head(fields: dict) -> str:
    return P70._head(fields)


DR_BODY = """\
# DR-0001: Срок подтверждения заявки — три рабочих дня

## Контекст
В ТЗ срок подтверждения заявки — три рабочих дня, в описании процесса — пять. Нужно единое значение для [[Заявка на поставку]].

## Рассмотренные варианты

### 1. Три рабочих дня — выбран
- Плюсы: совпадает с ТЗ (документ заказчика).
- Минусы: поставщики просили больше времени.

### 2. Пять рабочих дней — отклонён
- Плюсы: удобнее поставщикам.
- Минусы: противоречит ТЗ.
- **Причина отказа:** ТЗ — подписанный документ; изменение требует доп. соглашения.

## Решение
Срок подтверждения — три рабочих дня. Решили аналитик и руководитель проекта.

## Следствия
- Описание процесса приводится к ТЗ.
- Затронутые карточки: [[Заявка на поставку]], [[Обработка заявки на поставку]]

## История
- 2026-09-20 — создана (@analyst)
"""


def decision(num: int = 1, name: str = "Срок-подтверждения-заявки", status: str = "accepted", **over) -> "tuple[str, str]":
    """(путь мастера, текст): решение, которое проходит все проверки."""
    fm = {"title": f'"DR-{num:04d}: Срок подтверждения заявки — три рабочих дня"', "aliases": f'["DR-{num:04d}"]',
          "tags": "[decision]", "type": "decision", "template": "akdb_decision_v1.0.md", "status": status,
          "date": "2026-09-20", "owner": '"@analyst"', "supersedes": "[]", "superseded_by": None, "applies_to": "[]",
          "created": "2026-09-20", "updated": "2026-10-01"}
    body = over.pop("body", None) or DR_BODY.replace("DR-0001", f"DR-{num:04d}")
    fm.update(over)
    return f"Raw/decisions/DR-{num:04d}-{name}.md", _head(fm) + "\n" + body


def question(num: int = 1, name: str = "Максимум-вложений", status: str = "open", **over) -> "tuple[str, str]":
    fm = {"title": f'"Q-{num:03d}: Максимум вложений в заявке"', "aliases": f'["Q-{num:03d}"]', "tags": "[question]",
          "type": "question", "template": "akdb_question_v1.0.md", "q_id": f"Q-{num:03d}", "q_status": status,
          "owner": '"@analyst"', "asked_to": '"заказчик, отдел снабжения"', "channel": '"письмо"',
          "blocks": '["[[Заявка на поставку]]"]', "created": "2026-09-24", "updated": "2026-10-01"}
    if status in ("asked", "answered", "closed-no-answer"):
        fm["asked"] = "2026-09-25"
    if status == "answered":
        fm.update(answered="2026-10-01", answer_source='"Raw/customer/Письмо.md"')
    body = over.pop("body", None) or (
        f"# Q-{num:03d}: Максимум вложений в заявке\n\n## Вопрос\nСколько файлов можно прикладывать к одной заявке: пять или десять?\n\n"
        "## Почему это важно\nБез ответа не закрыть критерии приёмки по [[Заявка на поставку]].\n\n## Ответ\n—\n\n"
        "## История\n- 2026-09-24: заведён (@analyst).\n")
    fm.update(over)
    return f"Raw/questions/Q-{num:03d}-{name}.md", _head(fm) + "\n" + body


def reference(name: str = "Коды-причин-отказа", **over) -> "tuple[str, str]":
    fm = {"title": '"Коды причин отказа"', "aliases": '["SPR-004"]', "tags": None, "type": "reference",
          "template": "akdb_reference_v1.0.md", "kind": "dictionary", "owner": '"@analyst"',
          "summary": '"Коды причин, по которым заявка не принимается."', "created": "2026-08-09", "updated": "2026-10-01"}
    body = over.pop("body", None) or ("# Коды причин отказа\n\n| Код | Причина |\n|---|---|\n| 01 | Не заполнены позиции |\n"
                                      "| 02 | Срок поставки в прошлом |\n")
    fm.update(over)
    return f"Raw/reference/{name}.md", _head(fm) + "\n" + body


def _base(tmp: Path) -> Path:
    """Проект под git с карточкой, на которую ссылаются записи."""
    root = _project(tmp)
    card(root, "Concepts/Заявка на поставку.md", "Заявка на поставку — документ, которым заказчик поручает поставщику передать товар.",
         status="knowledge", kind="knowledge")
    card(root, "Processes/Обработка заявки на поставку.md", "Обработка заявки на поставку — процесс, которым склад принимает заявку.",
         status="knowledge", kind="knowledge")
    # таблица трассировки нужна `kb:trust`: без неё он доверие не считает
    _put(root, f"{KB}/meta/trace/trace.json", '{"direct": {}, "indirect": {}}')
    P70._commit(root)
    return root


# ------------------------------------------------------------------ папки и справки (AC-CT-13.1, AC-CT-13.6)

@test
def test_raw_record_folders_exist_in_the_schema_and_in_a_fresh_project(tmp: Path):
    """AC-CT-13.1: схема называет три папки записей, новый проект получает их с `README.md` из справки."""
    schema = [ln.strip() for ln in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    for home in HOMES.values():
        assert home in schema, f"{home} нет в structure_dirs.txt"
    FG = _mod("folder_guides")
    guides = FG.load(KIT)
    root = P42._new_kit_project(tmp / "fresh")
    for home in HOMES.values():
        assert (root / home).is_dir(), f"в свежем проекте нет {home}"
        text = (root / home / "README.md").read_text(encoding="utf-8")
        assert guides[home]["title"] in text and guides[home]["what"].split(".")[0] in text, f"{home}: справка не из кита"
        assert FG.render(home, guides[home]).strip() in text.replace("\r\n", "\n"), f"{home}: справка отличается от блока кита"


@test
def test_folder_guides_for_records_say_where_the_master_lives(_t):
    """Справки трёх папок записей и трёх разделов базы называют мастер-файл и зеркало словами плана (AC-CT-13.6)."""
    guides = _mod("folder_guides").load(KIT)
    for home, cmd in (("Raw/decisions", "kb:record --new decision"), ("Raw/questions", "Q-NNN-<суть>.md"),
                      ("Raw/reference", "TemplatesCommon/akdb_reference_v1.0.md")):
        g = guides[home]
        assert all(g.get(f) for f in ("title", "what", "put", "not", "who")), home
        assert cmd in g["put"], (home, g["put"])
        assert "зеркало" in g["what"].lower() and KB in g["what"], home
    assert guides[f"{KB}/Decisions"]["put"] == "Ничего руками: зеркало строит `kb:record --mirror`; решения пишут в `Raw/decisions/`."
    assert guides[f"{KB}/Questions"]["not"] == "Правки зеркала — их сотрёт следующая сборка. Новый вопрос — `kb:record --new question`."
    assert guides[f"{KB}/Reference"]["who"] == "Движок."
    assert "Raw/reference/" in guides[f"{KB}/Reference"]["put"]
    # прежнее утверждение «вопросы заводит движок» и «решения собирает движок из протоколов» снято
    for sec in ("Questions", "Decisions"):
        assert "заводит движок" not in guides[f"{KB}/{sec}"]["put"] and "из протоколов" not in guides[f"{KB}/{sec}"]["put"], sec


# ------------------------------------------------------------------ kb:record --new (AC-CT-13.2)

@test
def test_record_new_numbers_follow_masters_mirrors_and_the_archive(tmp: Path):
    """AC-CT-13.2: DR-0001, DR-0002; зеркало DR-0005 или файл в архиве двигают нумерацию дальше."""
    root = _project(tmp, git=False)
    cp = _kr(root, "--new", "decision", "Тест", "--owner", "analyst", expect=0)
    assert "Raw/decisions/DR-0001-Тест.md" in cp.stdout
    text = _read(root, "Raw/decisions/DR-0001-Тест.md")
    A = _mod("aurora_common")
    fm = A.frontmatter(text)
    assert (fm["type"], fm["template"], fm["status"], fm["owner"], fm["title"]) == (
        "decision", "akdb_decision_v1.0.md", "proposed", "@analyst", "DR-0001: Тест"), fm
    assert "# DR-0001: Тест\n" in text and "{{Какая задача" in text, "заготовка по телу шаблона: номер подставлен, остальное — заполнить"
    _kr(root, "--new", "decision", "Второе решение", "--owner", "@analyst", "--status", "accepted", expect=0)
    assert (root / "Raw/decisions/DR-0002-Второе-решение.md").is_file()
    assert A.frontmatter(_read(root, "Raw/decisions/DR-0002-Второе-решение.md"))["status"] == "accepted"
    _put(root, f"{KB}/Decisions/DR-0005-зеркало.md", "---\ntitle: x\n---\n\nx\n")
    _kr(root, "--new", "decision", "Третье", "--owner", "@analyst", expect=0)
    assert (root / "Raw/decisions/DR-0006-Третье.md").is_file(), "зеркало DR-0005 не занимает номер"
    _put(root, f"{KB}/_archive/DR-0009-старое.md", "---\ntitle: x\n---\n\nx\n")
    _kr(root, "--new", "decision", "Четвёртое", "--owner", "@analyst", expect=0)
    assert (root / "Raw/decisions/DR-0010-Четвёртое.md").is_file(), "архив не занимает номер"
    cp = _kr(root, "--new", "question", "Максимум вложений", "--blocks", "[[Заявка на поставку]]", "--owner", "@analyst", expect=0)
    q = _read(root, "Raw/questions/Q-001-Максимум-вложений.md")
    qfm = A.frontmatter(q)
    assert (qfm["q_id"], qfm["q_status"], qfm["type"]) == ("Q-001", "open", "question"), qfm
    assert 'blocks: ["[[Заявка на поставку]]"]' in q and "# Q-001: Максимум вложений\n" in q
    _kr(root, "--new", "reference", "Коды причин отказа", "--owner", "@analyst", expect=0)
    ref = _read(root, "Raw/reference/Коды-причин-отказа.md")
    rfm = A.frontmatter(ref)
    assert (rfm["type"], rfm["kind"]) == ("reference", "dictionary") and "# Коды причин отказа\n" in ref
    assert "<!--" not in ref and "Примечания" not in ref, "необязательный раздел и комментарий шаблона в заготовку не идут"


@test
def test_record_new_refuses_what_it_cannot_make_right(tmp: Path):
    """Пустая суть, имя модели вместо владельца, занятое имя, чужой тип и несуществующий тип — отказ с причиной и без файлов."""
    root = _project(tmp, git=False)
    cp = _kr(root, "--new", "decision", "", "--owner", "@analyst", expect=1)
    assert "суть" in cp.stderr, cp.stderr
    cp = _kr(root, "--new", "decision", "Тест", "--owner", "Claude", expect=1)
    assert "человек" in cp.stderr and "--owner" in cp.stderr, cp.stderr
    assert not any((root / "Raw/decisions").glob("*.md")), "файл создан несмотря на отказ"
    cp = _kr(root, "--new", "decision", "Тест", "--owner", "@a", "--status", "rejected", expect=1)
    assert "proposed" in cp.stderr, cp.stderr
    _kr(root, "--new", "reference", "Список", "--owner", "@a", expect=0)
    cp = _kr(root, "--new", "reference", "Список", "--owner", "@a", expect=1)
    assert "уже есть" in cp.stderr, cp.stderr
    _put(root, f"{KB}/Reference/Занято.md", "---\ntitle: Занято\ntype: reference\nstatus: knowledge\nbuilt: machine\n---\n\nx\n")
    cp = _kr(root, "--new", "reference", "Занято", "--owner", "@a", expect=1)
    assert "V-REC-1" in cp.stderr, cp.stderr
    cp = run("kb_record.py", "--new", "concept", "x", cwd=root)
    assert cp.returncode == 2, "тип вне записей argparse отвергает кодом 2"
    cp = run("kb_record.py", "--list", "--check", cwd=root)
    assert cp.returncode == 2, "два режима разом — ошибка вызова"
    # {{…}} в сути не превращаются в заготовки
    _kr(root, "--new", "decision", "С {{заготовкой}} внутри", "--owner", "@a", expect=0)
    made = next((root / "Raw/decisions").glob("DR-*.md"))
    assert "{{заготовкой}}" not in made.read_text(encoding="utf-8").split("## Контекст")[0]


@test
def test_record_list_counts_by_type_and_state(tmp: Path):
    root = _base(tmp)
    for rel, text in (decision(1), decision(2, "второе", status="proposed"), question(1, status="asked"), reference()):
        _put(root, rel, text)
    out = json.loads(_kr(root, "--list", "--json", expect=0).stdout)
    assert out["decision"]["masters"] == 2 and out["decision"]["by_status"] == {"accepted": 1, "proposed": 1}, out
    assert out["question"]["by_status"] == {"asked": 1} and out["reference"]["by_status"] == {"dictionary": 1}, out
    assert out["decision"]["mirrors"] == 0 and out["correction"]["masters"] == 0
    text = _kr(root, "--list", expect=0).stdout
    assert "Решения (`Raw/decisions`): 2" in text and "зеркал в базе: 0" in text, text


# ------------------------------------------------------------------ kb:record --check (AC-CT-13.3)

@test
def test_record_check_passes_clean_records_and_names_the_rest(tmp: Path):
    """AC-CT-13.3: запись в чужой папке, дубль номера, вопрос без `q_status`, заготовки `{{…}}` — ошибки кодом 1."""
    root = _base(tmp)
    for rel, text in (decision(1), question(1, status="answered"), reference()):
        _put(root, rel, text)
    cp = _kr(root, "--check", expect=0)
    assert "ошибок 0" in cp.stdout, cp.stdout
    # запись типа question в папке решений
    rel, text = question(7, "чужой")
    _put(root, "Raw/decisions/Q-007-чужой.md", text)
    cp = _kr(root, "--check", expect=1)
    assert "V-17" in cp.stdout and "лежит в Raw/decisions/" in cp.stdout, cp.stdout
    (root / "Raw/decisions/Q-007-чужой.md").unlink()
    # дубль номера: тот же DR-0001 под другим именем
    _put(root, "Raw/decisions/DR-0001-другое.md", decision(1, "другое")[1])
    cp = _kr(root, "--check", expect=1)
    assert "номер DR-0001 занят дважды" in cp.stdout, cp.stdout
    (root / "Raw/decisions/DR-0001-другое.md").unlink()
    # вопрос без q_status
    _put(root, *question(2, "без-хода", q_status=None))
    cp = _kr(root, "--check", expect=1)
    assert "V-17" in cp.stdout and "q_status" in cp.stdout, cp.stdout
    (root / "Raw/questions/Q-002-без-хода.md").unlink()
    # ответ без источника, открытый без blocks, заданный без даты
    _put(root, *question(3, "ответ", status="answered", answer_source=None))
    _put(root, *question(4, "открытый", blocks="[]"))
    _put(root, *question(5, "заданный", status="asked", asked=None))
    cp = _kr(root, "--check", expect=1)
    for word in ("answer_source", "blocks", "asked"):
        assert word in cp.stdout, (word, cp.stdout)
    for n in ("003-ответ", "004-открытый", "005-заданный"):
        (root / f"Raw/questions/Q-{n}.md").unlink()
    # заготовки после --new
    _kr(root, "--new", "decision", "Заготовка", "--owner", "@a", expect=0)
    cp = _kr(root, "--check", expect=1)
    assert "V-13" in cp.stdout and "{{…}}" in cp.stdout, cp.stdout
    # имя не по образцу
    for p in (root / "Raw/decisions").glob("DR-0002-*.md"):
        p.unlink()
    _put(root, "Raw/decisions/решение.md", decision(9)[1])
    cp = _kr(root, "--check", expect=1)
    assert "DR-<номер>" in cp.stdout, cp.stdout
    # json: тот же итог машинно
    data = json.loads(_kr(root, "--check", "--json", expect=1).stdout)
    assert data["errors"] >= 1 and all(f["level"] in ("error", "warn", "info") for f in data["findings"]), data


@test
def test_record_check_reads_corrections_old_without_template_are_information(tmp: Path):
    """AC-CT-13.7: исправление без `template:` — сведение, а не ошибка; с чужим шаблоном или без `corrects` — ошибка."""
    root = _base(tmp)
    _put(root, "Raw/corrections/2026-10-01-аналитик.md",
         '---\ncorrects: "[[Заявка на поставку]]"\ncreated: 2026-10-01\nstatus: active\n---\n\n# Исправление: Заявка на поставку\n\nТри дня.\n')
    cp = _kr(root, "--check", "--json", expect=0)
    data = json.loads(cp.stdout)
    rows = [f for f in data["findings"] if f["path"].startswith("Raw/corrections/")]
    assert rows and all(f["level"] == "info" for f in rows) and data["errors"] == 0, data
    _put(root, "Raw/corrections/2026-10-02-плохое.md", "---\ncreated: 2026-10-02\nstatus: active\n---\n\nбез corrects\n")
    cp = _kr(root, "--check", expect=1)
    assert "2026-10-02-плохое.md" in cp.stdout and "corrects" in cp.stdout, cp.stdout


@test
def test_record_check_names_the_names_that_belong_to_records(tmp: Path):
    """V-REC-1: карточка другого типа с именем `DR-…`/`Q-…` — ошибка и у `kb:record --check`, и у `kb:lint`; даты и дробные номера — нет."""
    root = _base(tmp)
    card(root, "Concepts/DR-0001-понятие.md", "DR-0001 — не решение, а понятие.", status="knowledge", kind="knowledge")
    card(root, "Concepts/Q-3.1-код.md", "Q-3.1 — код раздела.", status="knowledge", kind="knowledge")
    card(root, "Concepts/DR-2026-10-19-заметка.md", "Заметка с датой.", status="knowledge", kind="knowledge")
    cp = _kr(root, "--check", expect=1)
    assert "V-REC-1" in cp.stdout and "DR-0001-понятие" in cp.stdout, cp.stdout
    assert "Q-3.1" not in cp.stdout and "DR-2026-10-19" not in cp.stdout, cp.stdout
    lint = run("kb_lint.py", cwd=root)
    assert "(V-REC-1)" in lint.stdout and "DR-0001-понятие" in lint.stdout, lint.stdout[:800]
    assert "Q-3.1-код" not in "\n".join(l for l in lint.stdout.splitlines() if "V-REC-1" in l)


# ------------------------------------------------------------------ код V-17 и свой цикл вопроса

def _mirror_text(tid: str, **over) -> str:
    """Зеркало записи в базе, которое проходит проверку; `over` меняет поля (None — убрать)."""
    if tid == "decision":
        fm = {"title": '"DR-0001: Срок"', "aliases": '["DR-0001"]', "tags": "[decision]", "type": "decision",
              "template": "akdb_decision_v1.0.md", "kind": "document", "status": "accepted", "date": "2026-09-20",
              "owner": '"@analyst"', "sources": '\n  - "Raw/decisions/DR-0001-срок.md"', "source_synced": "2026-10-01",
              "created": "2026-09-20", "updated": "2026-10-01", "built": "machine"}
        body = "# DR-0001: Срок\n\n## Решение\nТри дня. См. [[Заявка на поставку]].\n"
    else:
        fm = {"title": '"Q-001: Максимум"', "aliases": '["Q-001"]', "tags": "[question]", "type": "question",
              "template": "akdb_question_v1.0.md", "kind": "document", "status": "draft", "q_id": "Q-001", "q_status": "open",
              "blocks": '["[[Заявка на поставку]]"]', "q_effective": "open",
              "sources": '\n  - "Raw/questions/Q-001-максимум.md"', "source_synced": "2026-10-01", "created": "2026-09-24",
              "updated": "2026-10-01", "built": "machine"}
        body = "# Q-001: Максимум\n\n## Вопрос\nСколько файлов? См. [[Заявка на поставку]].\n"
    fm.update(over)
    return _head(fm) + "\n" + body


@test
def test_v17_record_cycle_fields_and_mirror_source(tmp: Path):
    """V-17: у решения нужна `date`, у вопроса — `q_id`, `q_status` и условные поля; зеркало указывает на мастер в `Raw/…`."""
    C = _mod("card_types")
    reg = C.registry(str(tmp / "no-project"), kit_root=str(KIT))

    def v17(text: str, rel: str) -> list:
        return [v for v in C.validate_card(text, path=rel, reg=reg, level="strict") if v.code == "V-17"]

    assert C.SEVERITY["V-17"] == "error" and "V-17" in C.TITLES
    assert not v17(_mirror_text("decision"), f"{KB}/Decisions/DR-0001-срок.md")
    assert not v17(_mirror_text("question"), f"{KB}/Questions/Q-001-максимум.md")
    bad = v17(_mirror_text("decision", date=None), f"{KB}/Decisions/DR-0001-срок.md")
    assert bad and "date" in bad[0].message
    bad = v17(_mirror_text("decision", sources='\n  - "Sources/Confluence/Решение.md"'), f"{KB}/Decisions/DR-0001-срок.md")
    assert bad and "мастер-файл в Raw/decisions" in bad[0].message, bad
    for over, word in (({"q_status": None}, "q_status"), ({"q_id": None}, "q_id"), ({"q_status": "wait"}, "вне хода"),
                       ({"q_status": "answered"}, "answered"), ({"blocks": "[]"}, "blocks"), ({"q_status": "asked"}, "asked")):
        found = v17(_mirror_text("question", **over), f"{KB}/Questions/Q-001-максимум.md")
        assert found and any(word in v.message for v in found), (over, [v.message for v in found])
    ok = v17(_mirror_text("question", q_status="answered", answered="2026-10-01", answer_source='"Raw/customer/Письмо.md"'),
             f"{KB}/Questions/Q-001-максимум.md")
    assert not ok, [v.message for v in ok]
    # рукописная запись без built: происхождение — V-20 с подсказкой про перенос, а не V-17 про зеркало
    hand = _mirror_text("decision", built=None, sources=None, source_synced=None)
    codes = {v.code: v for v in C.validate_card(hand, path=f"{KB}/Decisions/DR-0001-срок.md", reg=reg, level="strict")}
    assert "V-20" in codes and "kb:record --migrate" in codes["V-20"].hint and "Raw/decisions/" in codes["V-20"].hint, codes
    # у карточки знаний прежнее сообщение
    plain = C.validate_card(_head({"title": '"Заявка"', "type": "concept", "template": "akdb_concept_v1.0.md", "kind": "knowledge",
                                   "status": "draft", "created": "2026-01-01", "updated": "2026-01-01"}) + "\nЗаявка — документ.\n",
                            path=f"{KB}/Concepts/Заявка.md", reg=reg, level="strict")
    assert any(v.code == "V-20" and "kb:record" not in v.hint for v in plain)


@test
def test_question_has_its_own_status_cycle(tmp: Path):
    """Вопрос — запись: его зеркало всегда `draft`, `kb:trust` статус не пересчитывает, `kb:lint` его принимает."""
    A = _mod("aurora_common")
    assert A.OWN_LIFECYCLE["question"] == ("draft",) and A.OWN_LIFECYCLE["decision"] == ("proposed", "accepted", "rejected", "superseded")
    assert not A.is_decision_card({"type": "question", "status": "draft"}), "вопрос не решение: идёт в пак обычным путём"
    root = _base(tmp)
    mirror = _mirror_text("question", sources='\n  - "Raw/questions/Q-001-максимум.md"')
    _put(root, "Raw/questions/Q-001-максимум.md", question(1, "максимум")[1])
    _put(root, f"{KB}/Questions/Q-001-максимум.md", mirror)
    P70._commit(root)
    run("kb_trust.py", "--apply", cwd=root)
    after = _read(root, f"{KB}/Questions/Q-001-максимум.md")
    assert after == mirror, "kb:trust тронул запись-вопрос (источник в Raw/ сделал бы его knowledge)"
    lint = run("kb_lint.py", cwd=root)
    assert "Q-001-максимум" not in lint.stdout or "вне шкалы" not in lint.stdout, lint.stdout[:600]
    # статус вне шкалы по-прежнему ошибка, а совет не отсылает к kb:trust
    _put(root, f"{KB}/Questions/Q-002-чужой.md", mirror.replace("Q-001", "Q-002").replace("status: draft", "status: wip"))
    bad = run("kb_lint.py", cwd=root)
    row = next(l for l in bad.stdout.splitlines() if "Q-002-чужой" in l and "вне шкалы" in l)
    assert "пересчитайте" not in row and "всегда draft" in row, row
    # статистика: вопросы вне доли доверенного, как и решения
    S = _mod("aurora_stats")
    assert S.OWN_LIFECYCLE is A.OWN_LIFECYCLE


# ------------------------------------------------------------------ панель (AC-CT-13.4)

def _cockpit():
    sys.path.insert(0, str(KIT / "cockpit"))
    sys.path.insert(0, str(SCRIPTS))
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    return ck


@test
def test_panel_records_permissions(tmp: Path):
    """AC-CT-13.4: создание в `Raw/decisions|questions|reference` разрешено; правка зеркала (`built: machine`) отклонена с причиной."""
    ck = _cockpit()
    assert ck.KB_WRITABLE == (f"{KB}/meta",)
    for home in HOMES.values():
        assert home in ck.MAKEABLE, home
    assert f"{KB}/Decisions" not in ck.MAKEABLE and f"{KB}/Questions" not in ck.MAKEABLE
    root = _base(tmp)
    made = ck.file_create(str(root), "Raw/decisions/DR-0042-новое.md", "# DR-0042\n")
    assert made.get("ok"), made
    for rel in ("Raw/questions/Q-042-новое.md", "Raw/reference/Список.md"):
        assert ck.file_create(str(root), rel, "x").get("ok"), rel
    assert ck.file_create(str(root), f"{KB}/Decisions/DR-0043-новое.md", "x").get("error"), "зеркало создают не руками"
    # зеркало: причина называет мастер-файл
    mirror = _mirror_text("decision")
    why = ck.why_readonly(f"{KB}/Decisions/DR-0001-срок.md", mirror)
    assert "зеркало записи" in why and "Raw/decisions/DR-0001-срок.md" in why and "сотрёт" in why, why
    why_q = ck.why_readonly(f"{KB}/Questions/Q-001-максимум.md", _mirror_text("question"))
    assert "Raw/questions/Q-001-максимум.md" in why_q, why_q
    # рукописная запись до переноса правится на месте; без текста — решения и вопросы правимы, справочник закрыт
    assert ck.why_readonly(f"{KB}/Decisions/DR-0001-срок.md", _mirror_text("decision", built=None, sources=None)) == ""
    assert ck.why_readonly(f"{KB}/Decisions/DR-1.md") == "" and ck.why_readonly(f"{KB}/Questions/Q-1.md") == ""
    assert "выведена из источников" in ck.why_readonly(f"{KB}/Reference/Список.md")
    assert ck.why_readonly(f"{KB}/Reference/Список.md", "---\ntitle: x\ntype: reference\n---\n\nрукописный\n") == ""
    derived = "---\ntitle: x\ntype: reference\nbuilt: machine\nsources:\n  - \"Sources/Confluence/Список.md\"\n---\n\nx\n"
    assert "выведена из источников" in ck.why_readonly(f"{KB}/Reference/Список.md", derived), "выведенный из Confluence справочник — не зеркало записи"
    assert ck.why_readonly(f"{KB}/meta/releases.md") == "" and ck.why_readonly("Raw/decisions/DR-0001-срок.md") == ""
    # настоящая запись: файл зеркала отклонён, мастер принят
    _put(root, f"{KB}/Decisions/DR-0001-срок.md", mirror)
    out = ck.file_write(str(root), f"{KB}/Decisions/DR-0001-срок.md", "подмена")
    assert "зеркало записи" in out.get("error", "") and _read(root, f"{KB}/Decisions/DR-0001-срок.md") == mirror, out
    for fn, args in ((ck.file_delete, (str(root), f"{KB}/Decisions/DR-0001-срок.md")),
                     (ck.file_rename, (str(root), f"{KB}/Decisions/DR-0001-срок.md", "другое.md"))):
        assert fn(*args).get("error"), fn.__name__
    _put(root, "Raw/decisions/DR-0001-срок.md", "---\ntitle: x\n---\n\nмастер\n")
    assert ck.file_write(str(root), "Raw/decisions/DR-0001-срок.md", "правка мастера").get("ok")
    # дерево файлов показывает зеркало закрытым, а мастер открытым
    tree = {r["path"]: r["readonly"] for r in ck.file_tree(str(root))["files"]}
    assert "зеркало записи" in tree[f"{KB}/Decisions/DR-0001-срок.md"] and tree["Raw/decisions/DR-0001-срок.md"] == "", tree
    assert f"Raw/decisions" in ck.file_tree(str(root))["create_dirs"]


# ------------------------------------------------------------------ замена и принятие решения на мастерах (AC-CT-13.5)

def _mirrored(root: Path, *pairs) -> None:
    for rel, text in pairs:
        _put(root, rel, text)
    run("kb_record.py", "--mirror", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    P70._commit(root)


@test
def test_supersede_decision_changes_masters_and_keeps_the_old_one_in_place(tmp: Path):
    """AC-CT-13.5: у решения `kb:supersede` правит мастера (`superseded`, `supersedes`), не ставит `deprecated`, в архив не уносит."""
    root = _base(tmp)
    old, new = decision(1, "старое"), decision(2, "новое", status="accepted")
    _mirrored(root, old, new)
    link_card = card(root, "Concepts/Ссылка.md", "Ссылка на решение — [[DR-0001-старое]].", status="knowledge", kind="knowledge")
    P70._commit(root)
    before_link = link_card.read_text(encoding="utf-8")
    cp = run("kb_supersede.py", "DR-0001-старое", "DR-0002-новое", "--reason", "срок пересмотрен", cwd=root, expect_rc=0)
    assert "(dry-run)" in cp.stdout and "superseded" in cp.stdout and _read(root, old[0]) == old[1], cp.stdout
    cp = run("kb_supersede.py", "DR-0001-старое", "DR-0002-новое", "--reason", "срок пересмотрен", "--apply", cwd=root, expect_rc=0)
    A = _mod("aurora_common")
    old_fm = A.frontmatter(_read(root, old[0]))
    assert old_fm["status"] == "superseded" and old_fm["superseded_by"] == "[[DR-0002-новое]]", old_fm
    assert "deprecated" not in _read(root, old[0])
    new_fm = A.frontmatter(_read(root, new[0]))
    assert "DR-0001-старое" in new_fm["supersedes"], new_fm
    assert "срок пересмотрен" in _read(root, old[0]).split("## История")[1]
    # зеркало старого — на месте, со статусом superseded; в архив ничего не ушло
    assert A.frontmatter(_read(root, f"{KB}/Decisions/DR-0001-старое.md"))["status"] == "superseded"
    assert A.frontmatter(_read(root, f"{KB}/Decisions/DR-0002-новое.md"))["supersedes"].count("DR-0001") == 1
    assert not list((root / KB / "_archive").glob("*.md")), "заменённое решение уехало в архив"
    assert link_card.read_text(encoding="utf-8") == before_link, "ссылки на заменённое решение переписаны"
    assert run("kb_record.py", "--check", cwd=root).returncode == 0
    # повтор не ломает: зеркала в порядке, мастера не растут
    run("kb_record.py", "--mirror", "--allow-dirty", cwd=root, expect_rc=0)


@test
def test_supersede_other_cards_still_goes_the_old_way(tmp: Path):
    """Не решение — как раньше: `deprecated`, архив, переписанные ссылки."""
    root = _base(tmp)
    card(root, "Concepts/Старое понятие.md", "Старое понятие — устаревшее определение.", status="knowledge", kind="knowledge")
    card(root, "Concepts/Новое понятие.md", "Новое понятие — актуальное определение.", status="knowledge", kind="knowledge")
    card(root, "Concepts/Ссылка.md", "См. [[Старое понятие]].", status="knowledge", kind="knowledge")
    P70._commit(root)
    run("kb_supersede.py", "Старое-понятие", "Новое-понятие", "--apply", cwd=root, expect_rc=1)     # имена — как файлы, с дефисами
    run("kb_supersede.py", "Старое понятие", "Новое понятие", "--apply", cwd=root, expect_rc=0)
    assert (root / KB / "_archive" / "Старое понятие.md").is_file()
    assert "status: deprecated" in _read(root, f"{KB}/_archive/Старое понятие.md")
    assert "[[Новое понятие]]" in _read(root, f"{KB}/Concepts/Ссылка.md")


@test
def test_supersede_refuses_a_master_that_its_mirror_would_reject(tmp: Path):
    """Преемник с заготовками `{{…}}` — замена не выполняется целиком: ни пометки в старом, ни записи в новом."""
    root = _base(tmp)
    old = decision(1, "старое")
    _mirrored(root, old)
    rel, text = decision(2, "новое")
    _put(root, rel, text.replace("Три рабочих дня — выбран", "{{Вариант}} — выбран"))
    cp = run("kb_supersede.py", "DR-0001-старое", "DR-0002-новое", "--apply", cwd=root, expect_rc=1)
    assert "V-13" in cp.stderr and "ничего не записано" in cp.stderr, cp.stderr
    assert _read(root, old[0]) == old[1], "старое решение изменено при отказе"


@test
def test_decide_accept_writes_the_master_and_refreshes_the_mirror(tmp: Path):
    """`kb:decide-accept` принимает мастер в `Raw/decisions/`, зеркало в базе перестраивается; рукописное решение в базе — как раньше."""
    root = _base(tmp)
    rel, text = decision(1, "срок", status="proposed", accepted_fp=None)
    _mirrored(root, (rel, text))
    cp = run("kb_decide.py", "DR-0001", "--by", "@analyst", cwd=root, expect_rc=0)
    assert "Зеркало в базе обновлено" in cp.stdout, cp.stdout
    A = _mod("aurora_common")
    master = A.frontmatter(_read(root, rel))
    assert master["status"] == "accepted" and master["accepted_fp"], master
    mirror = A.frontmatter(_read(root, f"{KB}/Decisions/DR-0001-срок.md"))
    assert mirror["status"] == "accepted" and mirror["accepted_fp"] == master["accepted_fp"], mirror
    assert _read(root, f"{KB}/Decisions/DR-0001-срок.md").count("принято (@analyst)") == 1, "строка истории зеркала"
    journal = json.loads(run("kb_decide.py", "--journal", "--json", cwd=root, expect_rc=0).stdout)
    assert [d["name"] for d in journal["decisions"]] == ["DR-0001-срок"], "зеркало и мастер — одно решение, а не два"
    # правка тела зеркала видна линтеру как правка принятого решения
    (root / KB / "Decisions" / "DR-0001-срок.md").write_text(
        _read(root, f"{KB}/Decisions/DR-0001-срок.md").replace("Решили аналитик", "Решил аналитик"), encoding="utf-8")
    assert "тело принятого решения" in run("kb_lint.py", cwd=root).stdout


@test
def test_agent_decide_drafts_into_raw_decisions_and_mirrors_it(tmp: Path):
    """`agent:decide --apply` кладёт черновик новым мастером в `Raw/decisions/`, строит зеркало; номер общий с `kb:record --new`."""
    from cases import part_85 as P85
    R = _mod("agent_runner")
    root = P85._decide_project(tmp)
    res = R.run_decide(P85._decide_cfg(), str(root), "Raw/meetings/2026-10-01_срок/protocol.md", "срок", "@analyst", [], True,
                       call=P85._decide_call(P85._decide_answer(), []), today="2026-10-19")
    assert res["ok"] and res["written"] and res["path"].startswith("Raw/decisions/DR-0001-"), res
    assert (root / res["path"]).is_file()
    mirror = root / KB / "Decisions" / Path(res["path"]).name
    assert mirror.is_file() and "built: machine" in mirror.read_text(encoding="utf-8"), "зеркала черновика нет"
    assert not [p for p in (root / KB / "Decisions").glob("*.md") if "built: machine" not in p.read_text(encoding="utf-8")], \
        "в базе осталась рукописная карточка"
    assert run("kb_record.py", "--check", cwd=root).returncode == 0
    nxt = _kr(root, "--new", "decision", "Следующее", "--owner", "@analyst", expect=0)
    assert "DR-0002-Следующее" in nxt.stdout, nxt.stdout


# ------------------------------------------------------------------ исправление (AC-CT-13.7)

@test
def test_new_correction_carries_its_template_and_old_ones_still_apply(tmp: Path):
    """AC-CT-13.7: `kb:correct --new` ставит `template: akdb_correction_v1.0.md`; прежнее исправление без поля применяется как раньше."""
    root = _base(tmp)
    run("kb_corrections.py", "--new", "Заявка на поставку", "--text", "Срок — три рабочих дня.", cwd=root, expect_rc=0)
    files = sorted((root / "Raw/corrections").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    A = _mod("aurora_common")
    fm = A.frontmatter(text)
    assert fm["template"] == "akdb_correction_v1.0.md" and fm["corrects"] == "[[Заявка на поставку]]" and fm["status"] == "active", fm
    assert text.index("corrects:") < text.index("template:") < text.index("created:"), "порядок полей шаблона"
    legacy = _put(root, "Raw/corrections/2026-09-01-прежнее.md",
                  '---\ncorrects: "[[Заявка на поставку]]"\ncreated: 2026-09-01\nstatus: active\n---\n\n# Исправление: Заявка на поставку\n\nСтарое слово.\n')
    out = run("kb_corrections.py", "--list", cwd=root, expect_rc=0).stdout
    assert files[0].stem in out and legacy.stem in out, out
    cp = run("kb_record.py", "--check", "--json", cwd=root, expect_rc=0)
    data = json.loads(cp.stdout)
    assert data["errors"] == 0
    infos = [f for f in data["findings"] if f["path"].endswith(legacy.name)]
    assert infos and all(f["level"] == "info" for f in infos), infos
    assert not [f for f in data["findings"] if f["path"].endswith(files[0].name)], "новое исправление проходит шаблон без замечаний"
    run("kb_corrections.py", "--apply", cwd=root, expect_rc=0)
    assert "Исправления человеком" in _read(root, f"{KB}/Concepts/Заявка на поставку.md")


# ------------------------------------------------------------------ ассистенты и справки

@test
def test_assistant_docs_send_records_to_raw(_t):
    """Навыки, промпт, правила для ассистентов и соглашения называют `Raw/…` местом записей и `kb:record` — командой."""
    wf = (KIT / "skills/aurora-vault/references/workflows.md").read_text(encoding="utf-8")
    assert "kb:record --new question" in wf and "kb:record --new decision" in wf and "Raw/questions/Q-NNN" in wf
    assert "AuroraKnowledgeDB/Questions/Q-NNN-<кратко>.md" not in wf and "AuroraKnowledgeDB/Decisions/DR-NNNN-<суть>.md" not in wf
    fm = (KIT / "skills/aurora-vault/references/frontmatter.md").read_text(encoding="utf-8")
    assert "living cards" not in fm and "immutable evidence" not in fm, "фраза о неизменяемом Raw устарела"
    assert "Raw/reference/" in fm and "Raw/questions/" in fm and "Raw/decisions/" in fm
    dr = (KIT / "scaffold/Prompts/DR_create.md").read_text(encoding="utf-8")
    assert "output: Raw/decisions/" in dr and "template: TemplatesCommon/akdb_decision_v1.0.md" in dr and "kb:record --new decision" in dr
    assert "AuroraKnowledgeDB/Decisions/" not in dr.split("---", 2)[1], "в шапке промпта остался прежний выход"
    for name, tpl in (("decision_record", "akdb_decision_v1.0.md"), ("question", "akdb_question_v1.0.md"),
                      ("requirement_card", "akdb_requirement_v1.0.md")):
        text = (KIT / f"scaffold/Templates/{name}_template.md").read_text(encoding="utf-8")
        after = text.split("\n---\n", 1)[1].lstrip("\n")
        assert after.startswith(f"<!-- Заменён `TemplatesCommon/{tpl}`; файл оставлен для прежних проектов. -->"), name
    agents = (KIT / "templates/agents/AGENTS.md.template").read_text(encoding="utf-8")
    assert "Raw/decisions|questions|corrections|reference" in agents
    conv = (KIT / "templates/meta/conventions.md").read_text(encoding="utf-8")
    assert "Raw/reference/" in conv and "Living domain lists" not in conv and "Not Raw" not in conv
    skill = (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    assert any(line.startswith("| `kb:record` |") for line in skill.splitlines())


@test
def test_record_command_is_registered_everywhere(_t):
    """Новая команда: реестр, манифест, зависимости связи, английские описания; панель вызывает её тем же скриптом."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    row = next(l for l in reg.splitlines() if l.startswith("kb | kb:record |"))
    assert "| скрипт | kb_record.py |" in row
    assert re.search(r"(?m)^scripts/kb_record\.py\s*=>\s*\.aurora/scripts/kb_record\.py$", (KIT / "engine_manifest.txt").read_text(encoding="utf-8"))
    deps = json.loads((KIT / "command_deps.json").read_text(encoding="utf-8"))["commands"]
    assert deps["kb:record"]["requires_llm"] is False and deps["kb:record"]["offline_capable"] is True
    en = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    assert "kb:record" in en["commands"] and "Raw/decisions" in en["commands"]["kb:record"]
    src = (SCRIPTS / "kb_record.py").read_text(encoding="utf-8")
    assert "Панель: `kb:record`" in src
