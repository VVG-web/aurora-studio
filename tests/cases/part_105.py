"""Проверки движка Aurora, часть 105. Каркас и помощники — tests/harness.py.

Транскрипты вложений и схем в плане разбора и в базе: группа «Вложения» после страниц, пропуск черновиков
из Workspaces/ и транскриптов `quality: low` с названием в плане, предупреждение «нет транскрипта»,
страница-владелец в `sources` карточки, доверие по странице, страница-указатель на схему. Нейтральные
данные (PRJ-A, «Страница верхнего уровня»); сети и модели нет.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from docfix import docx, para
from harness import KIT, SCRIPTS, card_srcs, make_project, run, test  # noqa: F401

PROSE = ("Подсистема-1 принимает заявки PRJ-A от заказчика и проверяет их по правилам, описанным в разделе 3. "
         "Если проверка прошла, заявка получает статус «принята», иначе возвращается автору с причиной отказа. ")
PAGE_REL = "Sources/Confluence/Страница верхнего уровня.md"
ASSETS = "Sources/Confluence/Страница верхнего уровня_assets"
OUT = "Raw/Extracted/Confluence/Страница верхнего уровня"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _bpmn() -> str:
    return ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">'
            '<bpmn:process id="P" name="Процесс 1"><bpmn:startEvent id="S" name="Заявка получена"/>'
            '<bpmn:userTask id="T" name="Проверить заявку PRJ-A"/><bpmn:endEvent id="E" name="Готово"/>'
            '<bpmn:sequenceFlow id="F1" sourceRef="S" targetRef="T"/><bpmn:sequenceFlow id="F2" sourceRef="T" '
            'targetRef="E"/></bpmn:process></bpmn:definitions>')


def _drawio() -> str:
    cells = ('<mxCell id="a" value="Принять заявку PRJ-A" vertex="1" parent="1"><mxGeometry x="0" y="0" as="geometry"/>'
             '</mxCell><mxCell id="b" value="Проверить" vertex="1" parent="1"><mxGeometry x="0" y="80" as="geometry"/>'
             '</mxCell><mxCell id="e" value="далее" edge="1" source="a" target="b" parent="1"><mxGeometry as="geometry"/>'
             '</mxCell>')
    return ('<mxfile><diagram name="Страница-1"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + cells + '</root></mxGraphModel></diagram></mxfile>')


def _page(root: Path, body: str = None) -> Path:
    page = root / PAGE_REL
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: Страница верхнего уровня\n---\n\n" + (body if body is not None else
                    "Описание страницы PRJ-A, достаточно длинное, чтобы быть источником разбора. " * 6) + "\n",
                    encoding="utf-8")
    return page


@test
def test_plan_puts_attachment_transcripts_after_pages_and_names_what_it_left_out(tmp: Path):
    BP = _mod("build_plan")
    names = [g for g, _r in BP.GROUPS]
    assert names.index("Confluence") < names.index("Web") < names.index("Вложения") < names.index("Встречи"), names
    root = make_project(tmp)
    _page(root)
    docx(root / ASSETS / "спецификация.docx", para(PROSE * 2))
    (root / ASSETS).mkdir(parents=True, exist_ok=True)
    (root / ASSETS / "процесс.bpmn").write_text(_bpmn(), encoding="utf-8")
    (root / "Workspaces/Задача-1").mkdir(parents=True, exist_ok=True)
    (root / "Workspaces/Задача-1/черновик.bpmn").write_text(_bpmn(), encoding="utf-8")
    docx(root / "Raw/customer/Мусор.docx", para("l1|i!ii ,,.. ;;; ### ~~~ ^^^ ¦¦¦ ░░░ ▒▒▒ ▓▓▓ ///"))
    run("office_ingest.py", cwd=root, expect_rc=0)
    assert (root / "Raw/Extracted/Workspaces/Задача-1/черновик.bpmn.md").exists()
    plan = run("build_plan.py", cwd=root, expect_rc=0).stdout
    lines = plan.splitlines()
    row = {l.split("|")[1].strip(): l for l in lines if l.startswith("| ") and l.count("|") >= 5}
    assert "| Вложения | 2 |" in row["Вложения"], plan[:900]
    assert lines.index(row["Confluence"]) < lines.index(row["Вложения"]), "страницы должны идти раньше вложений"
    assert "[Вложения] Raw/Extracted/Confluence/Страница верхнего уровня/спецификация.docx.md" in plan, plan
    assert "Workspaces" not in plan.split("## Партия")[1], "черновик из Workspaces/ попал в план разбора"
    assert "Низкого качества — в план не включены: 1" in plan and "Мусор.md" in plan, plan
    assert "[Raw/customer] Raw/customer/Мусор.md" not in plan and "Raw/customer/Мусор.md (" not in plan, \
        "транскрипт низкого качества попал в партию"
    # исправили оригинал и перевели заново — транскрипт возвращается в план
    docx(root / "Raw/customer/Мусор.docx", para(PROSE * 2))
    run("office_ingest.py", cwd=root, expect_rc=0)
    plan = run("build_plan.py", cwd=root, expect_rc=0).stdout
    assert "Низкого качества" not in plan and "Raw/customer/Мусор.md" in plan, plan


@test
def test_plan_names_a_document_without_a_transcript_instead_of_skipping_it_silently(tmp: Path):
    root = make_project(tmp)
    docx(root / "Raw/customer/Регламент.docx", para(PROSE * 2))
    plan = run("build_plan.py", cwd=root, expect_rc=0).stdout
    assert "Не разобраны: нет транскрипта — 1" in plan and "Raw/customer/Регламент.docx" in plan, plan
    assert "kb:ingest-office" in plan
    run("office_ingest.py", cwd=root, expect_rc=0)
    assert "Не разобраны" not in run("build_plan.py", cwd=root, expect_rc=0).stdout
    # документ изменился после перевода — транскрипт устарел, и это снова названо
    docx(root / "Raw/customer/Регламент.docx", para("Новая редакция регламента PRJ-A. " + PROSE))
    assert "Не разобраны: нет транскрипта — 1" in run("build_plan.py", cwd=root, expect_rc=0).stdout


@test
def test_a_card_from_a_transcript_lists_its_page_and_inherits_the_pages_trust(tmp: Path):
    root = make_project(tmp)
    _page(root)
    body = (para("Алгоритм проверки", style="2") + "".join(para("Шаг %d проверки заявки PRJ-A описан подробно." % i)
                                                       for i in range(1, 8)))
    docx(root / ASSETS / "спецификация.docx", body)
    run("office_ingest.py", cwd=root, expect_rc=0)
    transcript = f"{OUT}/спецификация.docx.md"
    assert (root / transcript).is_file()
    run("build_plan.py", "--card", "Алгоритм проверки", "--source", transcript, "--sections", "1", "--to", "Processes",
        "--apply", cwd=root, expect_rc=0)
    made = (root / "AuroraKnowledgeDB/Processes/Алгоритм-проверки.md").read_text(encoding="utf-8")
    assert card_srcs(made) == [transcript, PAGE_REL], card_srcs(made)
    # вторая карточка дописывается в ту же сущность: страница в источниках одна, не двойник
    run("build_plan.py", "--append", "Алгоритм проверки", "--source", transcript, "--sections", "1",
        "--apply", cwd=root)
    assert card_srcs((root / "AuroraKnowledgeDB/Processes/Алгоритм-проверки.md").read_text(encoding="utf-8")) \
        .count(PAGE_REL) == 1
    # доверие транскрипта — доверие страницы, а не «подписанный документ» из-за пути Raw/
    os.chdir(root)
    U = _mod("kb_trust")
    table, st, trust, draft = {"direct": {}, "indirect": {}}, {}, {"закрыто"}, {"бэклог"}
    page_cls = U.source_class(PAGE_REL, table, st, trust, draft)[0]
    cls, why = U.source_class(transcript, table, st, trust, draft)
    assert cls == page_cls and cls != "raw" and PAGE_REL in why, (cls, why, page_cls)
    cls, why = U.source_class("Raw/Extracted/Confluence/Без страницы/файл.docx.md", table, st, trust, draft)
    assert cls == "draft" and "без страницы" in why, (cls, why)
    assert U.source_class("Raw/contract/ГК.md", table, st, trust, draft)[0] == "raw", "договор перестал быть первоисточником"


@test
def test_a_page_that_only_points_to_a_diagram_is_not_a_stub_once_the_diagram_has_a_transcript(tmp: Path):
    BP = _mod("build_plan")
    pointer = "Схема процесса: [схема.drawio](Страница%20верхнего%20уровня_assets/схема.drawio)\n"
    root = make_project(tmp)
    page = _page(root, pointer)
    (root / ASSETS).mkdir(parents=True, exist_ok=True)
    (root / ASSETS / "схема.drawio").write_text(_drawio(), encoding="utf-8")
    text = page.read_text(encoding="utf-8")
    os.chdir(root)
    assert BP.only_artifact_link(text), "страница-указатель не узнана"
    assert BP.only_artifact_link(text, PAGE_REL), "без транскрипта страница — указатель"
    assert BP.page_transcripts(PAGE_REL) == []
    cp = run("kb_fix.py", "--unparsed", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    stub = root / "AuroraKnowledgeDB/Concepts/Страница-верхнего-уровня.md"
    assert stub.is_file() and "Описано во внешнем артефакте" in stub.read_text(encoding="utf-8"), \
        "заготовка под страницу-указатель без транскрипта не заведена"
    stub.unlink()
    run("office_ingest.py", cwd=root, expect_rc=0)
    assert BP.page_transcripts(PAGE_REL) == [f"{OUT}/схема.drawio.md"], BP.page_transcripts(PAGE_REL)
    assert BP.page_transcripts("Sources/Confluence/Другая.md") == []
    assert BP.only_artifact_link(text, PAGE_REL) == "", "страница с транскриптом схемы всё ещё «указатель»"
    transcript = (root / f"{OUT}/схема.drawio.md").read_text(encoding="utf-8")
    assert BP.only_artifact_link(transcript) == "", "транскрипт, называющий оригинал, принят за ссылку на него"
    run("kb_fix.py", "--unparsed", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    assert not stub.exists(), "заготовка «текста в источнике нет» заведена под страницу с транскриптом схемы"


@test
def test_an_empty_verdict_for_a_page_with_transcripts_says_where_the_knowledge_is(tmp: Path):
    root = make_project(tmp)
    page = _page(root, "Схема процесса: [схема.drawio](Страница%20верхнего%20уровня_assets/схема.drawio)\n")
    (root / ASSETS).mkdir(parents=True, exist_ok=True)
    (root / ASSETS / "схема.drawio").write_text(_drawio(), encoding="utf-8")
    run("office_ingest.py", cwd=root, expect_rc=0)
    os.chdir(root)
    AR = _mod("agent_runner")
    for source, expect in ((PAGE_REL, True), ("Sources/Confluence/Другая.md", False)):
        step = {"backends": [], "degraded": False}
        got = AR.accept_empty({}, str(root), source, page.read_text(encoding="utf-8"), "только ссылка", step,
                              False, False, lambda *a, **k: {"ok": False}, None)
        note = got["note"]
        assert ("их транскрипты разбираются отдельно" in note) == expect, (source, note)
        assert note.endswith("только ссылка"), note
