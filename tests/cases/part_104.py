"""Проверки движка Aurora, часть 104. Каркас и помощники — tests/harness.py.

Вложения страниц Confluence и схемы → транскрипты (`kb:ingest-office`): куда ложатся, что в шапке, что
с зеркалом, повторным запуском, удалением, дубликатами, пределами, настройками и длинными путями.
Всё нейтральное (PRJ-A, «Страница верхнего уровня»); сети и модели нет; xlsx и pdf — только если на машине
есть их разборщики (в CI их может не быть), остальное строится на docx и схемах.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import sys
from pathlib import Path

from docfix import docx, para, pdf
from harness import SCRIPTS, make_project, run as run_script, test  # noqa: F401

PROSE = ("Подсистема-1 принимает заявки PRJ-A от заказчика и проверяет их по правилам, описанным в разделе 3. "
         "Если проверка прошла, заявка получает статус «принята», иначе возвращается автору с причиной отказа. ")
LONG = PROSE * 3                                          # заведомо больше порога «пустого» вложения


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _tree(folder: Path) -> dict:
    """{путь: хеш} всех файлов под папкой — снимок, чтобы доказать, что зеркало не тронуто."""
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            out[p.relative_to(folder).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def _bpmn_text() -> str:
    return ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D">'
            '<bpmn:process id="P" name="Процесс 1"><bpmn:startEvent id="S" name="Заявка получена"/>'
            '<bpmn:userTask id="T" name="Проверить заявку PRJ-A"/><bpmn:endEvent id="E" name="Готово"/>'
            '<bpmn:sequenceFlow id="F1" sourceRef="S" targetRef="T"/><bpmn:sequenceFlow id="F2" sourceRef="T" '
            'targetRef="E"/></bpmn:process></bpmn:definitions>')


def _drawio_text() -> str:
    cells = ('<mxCell id="a" value="Принять заявку PRJ-A" vertex="1" parent="1"><mxGeometry x="0" y="0" as="geometry"/>'
             '</mxCell><mxCell id="b" value="Проверить" vertex="1" parent="1"><mxGeometry x="0" y="80" as="geometry"/>'
             '</mxCell><mxCell id="e" value="далее" edge="1" source="a" target="b" parent="1"><mxGeometry as="geometry"/>'
             '</mxCell>')
    return ('<mxfile><diagram name="Страница-1"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + cells + '</root></mxGraphModel></diagram></mxfile>')


def _project(tmp: Path) -> tuple:
    """Проект со страницей Confluence и её вложениями: docx, схема, модель BPMN, «логотип», пустой docx."""
    root = make_project(tmp)
    mirror = root / "Sources/Confluence"
    mirror.mkdir(parents=True, exist_ok=True)
    page = mirror / "Раздел" / "Страница верхнего уровня.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: Страница верхнего уровня\n---\n\nТекст страницы.\n", encoding="utf-8")
    assets = page.parent / "Страница верхнего уровня_assets"
    docx(assets / "спецификация.docx", para(LONG))
    (assets / "схема.drawio").write_text(_drawio_text(), encoding="utf-8")
    (assets / "процесс.bpmn").write_text(_bpmn_text(), encoding="utf-8")
    (assets / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    docx(assets / "иконка.docx", para("Лого"))
    return root, page, assets


EXTRACTED = "Raw/Extracted/Confluence/Раздел/Страница верхнего уровня"


@test
def test_confluence_attachments_become_transcripts_outside_the_mirror(tmp: Path):
    """Транскрипты лежат в Raw/Extracted/<источник>/<путь страницы без _assets>/<полное имя>.md, зеркало и
    оригиналы не тронуты; в шапке — страница, имя вложения, хеш и способ; логотип и пустой документ — без текста."""
    root, page, assets = _project(tmp)
    before = _tree(root / "Sources")
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    got = sorted(p.name for p in (root / EXTRACTED).glob("*.md"))
    assert got == ["процесс.bpmn.md", "спецификация.docx.md", "схема.drawio.md"], (got, cp.stdout)
    assert _tree(root / "Sources") == before, "зеркало источников изменилось"
    spec = (root / EXTRACTED / "спецификация.docx.md").read_text(encoding="utf-8")
    head = spec.split("---")[1]
    assert 'converted_from: "Sources/Confluence/Раздел/Страница верхнего уровня_assets/спецификация.docx"' in head, head
    assert 'page: "Sources/Confluence/Раздел/Страница верхнего уровня.md"' in head, head
    assert "attachment: спецификация.docx" in head and "converter: builtin-docx" in head, head
    assert "source_hash:" in head and "status: draft" in head and "quality:" not in head, head
    assert head.index("source_hash") < head.index("page:"), "хеш должен стоять в первых строках шапки"
    assert "Подсистема-1 принимает заявки PRJ-A" in spec and "Машинная конвертация" in spec
    bpmn = (root / EXTRACTED / "процесс.bpmn.md").read_text(encoding="utf-8")
    assert "converter: bpmn" in bpmn and "Проверить заявку PRJ-A" in bpmn and "Машинное описание схемы" in bpmn, bpmn
    assert "converter: drawio" in (root / EXTRACTED / "схема.drawio.md").read_text(encoding="utf-8")
    assert "Без транскрипта (текста слишком мало" in cp.stdout and "Вложений: 4 · транскриптов: 3" in cp.stdout, cp.stdout
    assert not (root / "Raw/Extracted/Confluence/Раздел/Страница верхнего уровня/logo.png.md").exists()
    assert (assets / "спецификация.docx").exists(), "оригинал пропал (он же доказательство)"


@test
def test_attachment_transcripts_are_idempotent_follow_changes_and_vanish_with_the_attachment(tmp: Path):
    root, page, assets = _project(tmp)
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    folder = root / EXTRACTED
    stamps = {p.name: p.stat().st_mtime_ns for p in folder.glob("*.md")}
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert "собрано: 0 · пропущено (не изменились): 3" in cp.stdout, cp.stdout
    assert stamps == {p.name: p.stat().st_mtime_ns for p in folder.glob("*.md")}, "повторный запуск переписал файлы"
    # вложение заменили — транскрипт перечитан
    docx(assets / "спецификация.docx", para("Новая редакция PRJ-A. " + LONG))
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert "Новая редакция PRJ-A" in (folder / "спецификация.docx.md").read_text(encoding="utf-8")
    # вложение удалили — транскрипт и пустая папка уходят; чужой markdown в той же папке остаётся
    (folder / "заметка.md").write_text("# Моя заметка\n\nНе транскрипт: шапки нет.\n", encoding="utf-8")
    (assets / "схема.drawio").unlink()
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert not (folder / "схема.drawio.md").exists() and (folder / "процесс.bpmn.md").exists(), cp.stdout
    assert "Убрано транскриптов, у которых нет оригинала: 1" in cp.stdout, cp.stdout
    assert (folder / "заметка.md").exists(), "чужой markdown в папке транскриптов удалён"
    # страницу убрали целиком: папка транскриптов пустеет и исчезает (вместе с заметкой её не удалить — она чужая)
    (folder / "заметка.md").unlink()
    for p in sorted(assets.glob("*"), reverse=True):
        p.unlink()
    assets.rmdir()
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert not (root / "Raw/Extracted/Confluence").exists(), "пустые папки транскриптов остались"


@test
def test_an_identical_attachment_gets_one_transcript_and_the_next_one_takes_over(tmp: Path):
    root, page, assets = _project(tmp)
    data = (assets / "спецификация.docx").read_bytes()
    (assets / "копия спецификации.docx").write_bytes(data)
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    folder = root / EXTRACTED
    names = sorted(p.name for p in folder.glob("*.docx.md"))
    assert len(names) == 1, names
    assert "Дубликаты по содержимому (один транскрипт на содержимое): 1" in cp.stdout, cp.stdout
    kept = names[0]
    original = "спецификация.docx" if kept.startswith("спец") else "копия спецификации.docx"
    other = "копия спецификации.docx" if original == "спецификация.docx" else "спецификация.docx"
    (assets / original).unlink()                      # остался двойник — он и получает транскрипт
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert sorted(p.name for p in folder.glob("*.docx.md")) == [other + ".md"], list(folder.glob("*"))


@test
def test_attachment_policy_size_blocked_types_and_the_file_cap(tmp: Path):
    CE = _mod("confluence_export")
    OI = _mod("office_ingest")
    # разбираются только известные типы — исполняемое и веб-страницы в их число не входят
    clash = (set(OI.SUPPORTED) | set(OI.DIAGRAM_EXTS)) & set(CE.BLOCKED_EXTS)
    assert not clash, f"разбираемый тип входит в запрещённые вложения: {clash}"
    root, page, assets = _project(tmp)
    # предел размера — тот же, что у синка (attachment_max_mb), в МБ
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('    space: "T"\n', '    space: "T"\n    attachment_max_mb: 1\n'),
                   encoding="utf-8")
    big = assets / "большой.docx"
    docx(big, para(LONG), parts={"word/media/blob.bin": os.urandom(1_300_000)})
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert "пропущено (больше предела размера вложения): Sources/Confluence/Раздел/Страница верхнего уровня_assets/большой.docx" \
        in cp.stdout, cp.stdout
    assert not (root / EXTRACTED / "большой.docx.md").exists()
    # потолок файлов за запуск: следующий запуск переводит остальное, переведённое не пересчитывается
    for p in (root / "Raw/Extracted").rglob("*"):
        if p.is_file():
            p.unlink()
    cp = run_script("office_ingest.py", "--max-files", "1", cwd=root, expect_rc=0)
    assert "Отложено до следующего запуска (потолок файлов за запуск — 1): 2" in cp.stdout, cp.stdout
    assert len(list((root / EXTRACTED).glob("*.md"))) == 1
    run_script("office_ingest.py", "--max-files", "1", cwd=root, expect_rc=0)
    run_script("office_ingest.py", "--max-files", "1", cwd=root, expect_rc=0)
    assert len(list((root / EXTRACTED).glob("*.md"))) == 3, "отложенное не добрано за следующие запуски"


@test
def test_project_settings_switch_attachments_and_diagrams_independently(tmp: Path):
    root, page, assets = _project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    folder = root / EXTRACTED

    def clear():
        for p in sorted((root / "Raw/Extracted").rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()

    cfg.write_text(base + "\ningest:\n  attachments: false\n  diagrams: true\n", encoding="utf-8")
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert sorted(p.name for p in folder.glob("*.md")) == ["процесс.bpmn.md", "схема.drawio.md"]
    clear()
    cfg.write_text(base + "\ningest:\n  attachments: true\n  diagrams: false\n", encoding="utf-8")
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert sorted(p.name for p in folder.glob("*.md")) == ["спецификация.docx.md"]
    clear()
    cfg.write_text(base, encoding="utf-8")
    run_script("office_ingest.py", "--no-attachments", "--no-diagrams", cwd=root, expect_rc=0)
    assert not (root / "Raw/Extracted").exists() or not list((root / "Raw/Extracted").rglob("*.md"))
    # выключили извлечение — прежние транскрипты не удаляются (они ещё читаются разбором), новые не появляются
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    cfg.write_text(base + "\ningest:\n  attachments: false\n  diagrams: false\n", encoding="utf-8")
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert len(list(folder.glob("*.md"))) == 3, "выключение настройки удалило готовые транскрипты"
    # явный путь выполняется всегда: человек назвал файл сам
    clear()
    run_script("office_ingest.py", str(assets / "спецификация.docx"), cwd=root, expect_rc=0)
    assert [p.name for p in folder.glob("*.md")] == ["спецификация.docx.md"]


@test
def test_diagrams_in_raw_and_workspaces_and_web_attachments_are_left_to_their_owners(tmp: Path):
    root = make_project(tmp)
    (root / "Raw/project/схема.bpmn").write_text(_bpmn_text(), encoding="utf-8")
    (root / "Workspaces/Задача-1").mkdir(parents=True, exist_ok=True)
    (root / "Workspaces/Задача-1/черновик.bpmn").write_text(_bpmn_text(), encoding="utf-8")
    (root / "Workspaces/Задача-1/заметки.docx").write_bytes(b"not a real docx")
    web = root / "Sources/Web/сайт/_files"
    docx(web / "приказ.docx", para(LONG))
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert (root / "Raw/project/схема.bpmn.md").exists() or (root / "Raw/project/схема.md").exists(), \
        "схема в Raw/ без транскрипта рядом с оригиналом"
    assert (root / "Raw/Extracted/Workspaces/Задача-1/черновик.bpmn.md").exists(), "схема из Workspaces/ не разобрана"
    assert not list((root / "Raw/Extracted").rglob("заметки*")), "документ из Workspaces/ разобран: там только схемы"
    assert not list(web.glob("*.md")) and not (root / "Raw/Extracted/Web").exists(), \
        "вложения веб-коннектора разобраны дважды: ими занимается сам коннектор"


@test
def test_a_missing_mirror_does_not_wipe_transcripts_and_dry_run_changes_nothing(tmp: Path):
    root, page, assets = _project(tmp)
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    held = _tree(root / "Raw/Extracted")
    # зеркала нет вовсе (синк не успел, папку убрали): это не «вложения удалили», транскрипты не трогаем
    import shutil
    shutil.move(str(root / "Sources/Confluence"), str(tmp / "зеркало-убрано"))
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert _tree(root / "Raw/Extracted") == held, "без зеркала транскрипты удалены"
    shutil.move(str(tmp / "зеркало-убрано"), str(root / "Sources/Confluence"))
    (assets / "схема.drawio").unlink()
    docx(assets / "новое.docx", para("Новое вложение PRJ-A. " + LONG))
    cp = run_script("office_ingest.py", "--dry-run", cwd=root, expect_rc=0)
    assert "(dry-run) Ничего не записано." in cp.stdout and "Будет убрано транскриптов" in cp.stdout, cp.stdout
    assert _tree(root / "Raw/Extracted") == held, "dry-run изменил транскрипты"


@test
def test_a_transcript_name_that_does_not_fit_the_path_limit_is_cut_to_a_hash_and_named(tmp: Path):
    OI = _mod("office_ingest")
    AC = _mod("aurora_common")
    rel = "Sources/Confluence/" + "а" * 40 + "/" + "б" * 50 + "_assets/" + "в" * 90 + ".docx"
    short, cut = OI.extracted_path(rel)
    assert cut and len(short) <= AC.PATH_CHARS and short.endswith(".md"), (len(short), short)
    assert "~" in short.rsplit("/", 1)[1] and short.startswith("Raw/Extracted/Confluence/"), short
    assert OI.extracted_path(rel) == (short, True), "сокращение недетерминировано"
    other = OI.extracted_path(rel.replace("в" * 90, "в" * 89 + "г"))[0]
    assert other != short, "разные имена свелись к одному"
    fits, cut2 = OI.extracted_path("Sources/Confluence/Раздел/Страница_assets/файл.docx")
    assert not cut2 and fits == "Raw/Extracted/Confluence/Раздел/Страница/файл.docx.md", fits
    # через команду: путь короче предела и названо в итоге
    root = make_project(tmp)
    asset = root / ("Sources/Confluence/" + "a" * 40 + "/" + "b" * 50 + "_assets/" + "c" * 95 + ".docx")
    docx(asset, para(LONG))
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    made = [p.relative_to(root).as_posix() for p in (root / "Raw/Extracted").rglob("*.md")]
    assert len(made) == 1 and len(made[0]) <= AC.PATH_CHARS, made
    assert "Длинные пути: имя транскрипта сокращено до хеша — 1" in cp.stdout, cp.stdout


@test
def test_xlsx_and_pdf_attachments_get_transcripts_when_their_readers_are_present(tmp: Path):
    OI = _mod("office_ingest")
    root, page, assets = _project(tmp)
    made = []
    try:
        import openpyxl  # type: ignore
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Лист1"
        ws.append(["Поле", "Описание"])
        for i in range(8):
            ws.append(["Поле %d" % i, "Описание поля %d подсистемы PRJ-A и правил его заполнения" % i])
        wb.save(assets / "таблица.xlsx")
        made.append("таблица.xlsx.md")
    except ImportError:
        pass
    alpha = ["Alpha requirement is described here in detail for the first page of the PRJ-A file",
             "and it continues with more words so that the page is not mistaken for an empty logo"]
    bravo = ["Bravo requirement follows on the second page of the same document for the readers",
             "and it also continues with more words so that the whole text passes the empty limit"]
    pdf(assets / "документ.pdf", [alpha, bravo])
    if OI.conv_pdf_builtin(str(assets / "документ.pdf")):
        made.append("документ.pdf.md")
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    names = {p.name for p in (root / EXTRACTED).glob("*.md")}
    for name in made:
        assert name in names, (name, sorted(names))
    if "документ.pdf.md" in made:
        body = (root / EXTRACTED / "документ.pdf.md").read_text(encoding="utf-8")
        assert "<!-- стр. 1 -->" in body and "<!-- стр. 2 -->" in body, body
    if "таблица.xlsx.md" in made:
        assert "## Лист: Лист1" in (root / EXTRACTED / "таблица.xlsx.md").read_text(encoding="utf-8")
