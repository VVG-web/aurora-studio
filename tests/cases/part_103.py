"""Проверки движка Aurora, часть 103. Каркас и помощники — tests/harness.py.

Предобработка docx и pdf → markdown (`kb:ingest-office`): качество текста, якоря страниц pdf, пометка
`quality: low`, разрез длинного документа на части. Документы собираются программно и нейтральны
(PRJ-A, «Подсистема-1», example.com); сеть и модель не нужны, pandoc не нужен — встроенный конвертер
одинаков на любой машине.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

from docfix import (cell, comment_range, dele, docx, field, footnote_ref, hyperlink, image, ins, para, pdf,
                    row, run, table)
from harness import KIT, SCRIPTS, make_project, run as run_script, test  # noqa: F401


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


GOLDEN = """# Спецификация Подсистемы-1

# Общие положения

Документ описывает PRJ-A.[^1]

## Требования

- Первое требование
  - Уточнение первого
- Второе требование

1. Шаг один
   1. Подшаг
2. Шаг два

## Подробности

### Узкий раздел

См. [ссылка на спецификацию](https://example.com/spec).

Срок: 30 дней

Фраза спорная формулировка далее.

Стр.

![image1.png]

| Поле | Значение |
|---|---|
| Код | PRJ-A |

[^1]: Пояснение PRJ-A к тексту.

# Приложение

Таблица: Сроки по этапам

- Строка 1
  - Этап: Этап 1
  - Срок: 10 дней
  - Ответственный: Подсистема-1
- Строка 2
  - Этап: Этап 2
  - Срок: 20 дней
  - Ответственный: Подсистема-1
- Строка 3
  - Этап / Срок: Итого: 30 дней
  - Ответственный: Все

## Комментарии

- [Рецензент-1] к «спорная формулировка»: Уточнить формулировку у владельца."""


def _rich_body() -> str:
    return "".join([
        para("Спецификация Подсистемы-1", style="Title"),
        para("Общие положения", style="Heading1"),
        para("Документ описывает PRJ-A.", footnote_ref(1)),
        para("Требования", style="2"),                     # стиль с id «2»: по имени «heading 2», не по id
        para("Первое требование", num=(1, 0)),
        para("Уточнение первого", num=(1, 1)),
        para("Второе требование", num=(1, 0)),
        para("Шаг один", num=(2, 0)),
        para("Подшаг", num=(2, 1)),
        para("Шаг два", num=(2, 0)),
        para("Подробности", style="Custom"),               # наследник заголовка 2 по basedOn
        para("Узкий раздел", style="Outlined"),            # уровень задан только outlineLvl стиля
        para("См. ", hyperlink("rId1", "ссылка на спецификацию"), "."),
        para("Срок: ", ins("30 дней"), dele("10 дней")),
        para("Фраза ", comment_range(0, "спорная формулировка"), " далее."),
        para("Стр. ", field("PAGE", "7")),
        para(field("TOC", "Оглавление 1 ..... 3")),
        para(image("rId2")),
        table(row("Поле", "Значение"), row("Код", "PRJ-A")),
        para("Приложение", style="Heading1"),
        table(
            row(cell("Сроки по этапам", span=3)),
            row("Этап", "Срок", "Ответственный"),
            row("Этап 1", "10 дней", cell("Подсистема-1", vmerge="restart")),
            row("Этап 2", "20 дней", cell("", vmerge="continue")),
            row(cell("Итого: 30 дней", span=2), "Все"),
            row("Удалённый", "ряд", "тут", deleted=True),
        ),
    ])


@test
def test_docx_gives_the_golden_markdown_for_every_construct(tmp: Path):
    """Заголовки (по имени стиля, по basedOn, по outlineLvl), вложенные списки, таблицы (в том числе с
    объединением — построчно), ссылка, картинка-заглушка, сноска в конец раздела, принятые исправления,
    блок комментариев; колонтитулы, номер страницы и оглавление в текст не попадают."""
    DT = _mod("docx_text")
    text = DT.convert(docx(tmp / "spec.docx", _rich_body()))
    assert text is not None
    got, want = text.split("\n"), GOLDEN.split("\n")
    for n, (g, w) in enumerate(zip(got, want), 1):
        assert g == w, "строка %d: получено %r, ждали %r" % (n, g, w)
    assert len(got) == len(want), "длина: получено %d строк, ждали %d" % (len(got), len(want))
    assert "КОЛОНТИТУЛ" not in text and "10 дней" in text and "Удалённый" not in text


@test
def test_docx_reader_takes_moved_text_and_skips_the_moved_from_copy(tmp: Path):
    DT = _mod("docx_text")
    moved = ('<w:moveFrom w:id="3" w:author="Автор-1"><w:r><w:t>Перенесено-откуда</w:t></w:r></w:moveFrom>'
             '<w:moveTo w:id="4" w:author="Автор-1"><w:r><w:t>Перенесено-куда</w:t></w:r></w:moveTo>')
    text = DT.convert(docx(tmp / "moved.docx", para(run("Начало. "), moved)))
    assert "Перенесено-куда" in text and "Перенесено-откуда" not in text, text


@test
def test_a_docx_without_optional_parts_still_converts_and_a_hostile_one_is_refused(tmp: Path):
    """Только document.xml (без стилей, нумерации, сносок) — читается; пределы размера части, вложенности
    и числа элементов отказывают, а не роняют разбор."""
    DT = _mod("docx_text")
    plain = docx(tmp / "plain.docx", para("Обычный абзац PRJ-A."), standard=False)
    assert DT.convert(plain) == "Обычный абзац PRJ-A."
    assert DT.convert(str(tmp / "нет.docx")) is None and DT.convert(str(tmp / "a.txt")) is None
    saved = (DT.MAX_PART_BYTES, DT.MAX_PART_DEPTH, DT.MAX_PART_ELEMENTS)
    try:
        DT.MAX_PART_BYTES = 200
        assert DT.convert(plain) is None, "часть больше предела прочитана"
        DT.MAX_PART_BYTES = saved[0]
        deep = docx(tmp / "deep.docx", "<w:sdt><w:sdtContent>" * 30 + para("Глубоко") + "</w:sdtContent></w:sdt>" * 30,
                    standard=False)
        DT.MAX_PART_DEPTH = 20
        assert DT.convert(deep) is None, "слишком глубокая вложенность прочитана"
        DT.MAX_PART_DEPTH = saved[1]
        DT.MAX_PART_ELEMENTS = 3
        assert DT.convert(plain) is None, "слишком много элементов прочитано"
    finally:
        DT.MAX_PART_BYTES, DT.MAX_PART_DEPTH, DT.MAX_PART_ELEMENTS = saved
    assert DT.convert(plain) == "Обычный абзац PRJ-A."


@test
def test_builtin_docx_converter_goes_first_so_the_result_does_not_depend_on_pandoc(tmp: Path):
    OI = _mod("office_ingest")
    assert OI.CONVERTERS[0][0] == "builtin-docx", "встроенный docx стоит не первым — результат зависит от машины"
    saved = OI.conv_pandoc, OI.CONVERTERS[:]
    OI.conv_pandoc = lambda src: "ТЕКСТ-ОТ-PANDOC"
    OI.CONVERTERS[:] = [(n, OI.conv_pandoc if n == "pandoc" else f) for n, f in OI.CONVERTERS]
    try:
        text, conv = OI.convert(docx(tmp / "x.docx", para("Абзац PRJ-A")), "auto", use_ocr=False)
    finally:
        OI.conv_pandoc = saved[0]
        OI.CONVERTERS[:] = saved[1]
    assert conv == "builtin-docx" and "ТЕКСТ-ОТ-PANDOC" not in text and "Абзац PRJ-A" in text, (conv, text)
    names = [n for n, _f in OI.CONVERTERS]
    assert names.index("builtin-pdf") < names.index("markitdown"), "markitdown стоит раньше разбора pdf с якорями"


# --------------------------------------------------------------------------- pdf

BODY = ["Alpha requirement is described here in detail", "Bravo requirement follows on the next page",
        "Charlie requirement closes the first part", "Delta requirement ends the whole document"]


def _four_pages() -> list:
    return [["ACME PRJ-A Header", "", BODY[i - 1], "", "Page %d of 4" % i] for i in range(1, 5)]


@test
def test_pdf_cleaning_drops_running_headers_and_page_numbers_but_not_body_text(tmp: Path):
    TT = _mod("transcript_text")
    pages = ["\n".join(p) for p in _four_pages()]
    out = TT.clean_pdf_pages(pages)
    joined = "\n".join(out)
    assert "ACME PRJ-A Header" not in joined and "Page 2 of 4" not in joined, joined
    for i in range(1, 5):
        assert BODY[i - 1] in joined, "тело страницы %d потеряно" % i
    # две страницы: колонтитул не отличить от текста — остаётся, номера страниц уходят
    two = TT.clean_pdf_pages(["\n".join(p) for p in _four_pages()[:2]])
    assert all("ACME PRJ-A Header" in p and "of 4" not in p for p in two), two
    # номер в середине страницы не трогается, число в начале предложения тоже
    mid = TT.clean_pdf_pages(["Заголовок\nввод один\nввод два\n12\nТекст 2026 года\nстр. 3",
                              "Заголовок\nещё один\nещё два\nтекст три\nтекст четыре\n4",
                              "Заголовок\nа\nб\nв\nг\n5"])
    assert "\n12\n" in "\n" + mid[0] + "\n" and "Текст 2026 года" in mid[0] and "стр. 3" not in mid[0], mid


@test
def test_pdf_pages_get_anchors_and_lose_the_running_header(tmp: Path):
    OI = _mod("office_ingest")
    path = pdf(tmp / "doc.pdf", _four_pages())
    text = OI.conv_pdf_builtin(path)
    if text is None:
        return                       # ни одного разборщика pdf на машине (pypdf, fitz, pdftotext) — проверять нечем
    for n in range(1, 5):
        assert "<!-- стр. %d -->" % n in text, (n, text)
    assert "ACME PRJ-A Header" not in text and "Page 3 of 4" not in text, text
    assert "Charlie requirement closes" in text and text.index("стр. 2") < text.index("Charlie requirement"), text


# --------------------------------------------------------------------------- качество

PROSE = ("Подсистема-1 принимает заявки PRJ-A от заказчика и проверяет их по правилам, описанным в разделе 3. "
         "Если проверка прошла, заявка получает статус «принята», иначе возвращается автору с причиной отказа.")


@test
def test_quality_marks_empty_garbage_and_sentenceless_text_and_spares_tables_and_lists(tmp: Path):
    TT = _mod("transcript_text")
    assert TT.quality(PROSE) == (True, "")
    assert TT.quality("") == (False, "текста почти нет")
    assert TT.quality("<!-- стр. 1 -->\n\n<!-- стр. 2 -->")[0] is False
    ok, why = TT.quality("l1|i!ii ,,.. ;;; ### ~~~ ^^^ ¦¦¦ ░░░ ▒▒▒ ▓▓▓ ///")
    assert not ok and "читаемых слов" in why, (ok, why)
    ok, why = TT.quality("�� ���� ���� �� ��� ����� ��")
    assert not ok, (ok, why)
    ok, why = TT.quality(("а" * 80 + " ") * 3)
    assert not ok, "строка из одного повторяющегося знака принята: %s" % why
    ok, why = TT.quality("Да 1 Нет 2 Ок 3 Да 4 Нет 5 Ок 6 Да 7 Нет 8")
    assert not ok and "предложения" in why, (ok, why)
    assert TT.quality("12 34 56 78 90 12 34 56 78 90")[0] is False
    assert TT.quality("12 34 56 78 90 12 34 56 78 90", tabular=True)[0] is True, "таблица из чисел названа мусором"
    assert TT.quality("| Поле | Значение |\n|---|---|\n| Код | PRJ-A |\n| Срок | 30 дней |", tabular=True)[0]
    assert TT.quality("- Первое требование к системе\n- Второе требование к системе\n- Третье требование")[0]
    assert TT.quality("ИНН — идентификационный номер налогоплательщика, КПП — код причины постановки на учёт")[0]


@test
def test_a_garbage_or_empty_document_is_written_but_marked_low_and_named_in_the_summary(tmp: Path):
    root = make_project(tmp)
    docx(root / "Raw/customer/Мусор.docx", para("l1|i!ii ,,.. ;;; ### ~~~ ^^^ ¦¦¦ ░░░ ▒▒▒ ▓▓▓ ///"))
    docx(root / "Raw/customer/Хороший.docx", para(PROSE))
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    bad = (root / "Raw/customer/Мусор.md").read_text(encoding="utf-8")
    good = (root / "Raw/customer/Хороший.md").read_text(encoding="utf-8")
    assert "quality: low" in bad and "quality_note:" in bad, bad
    assert "quality:" not in good, good
    assert "Документов низкого качества: 1" in cp.stdout and "Мусор.md" in cp.stdout, cp.stdout
    assert '"low_quality": 1' in cp.stdout, "счётчик не попал в итог маршрута: " + cp.stdout[-300:]


# --------------------------------------------------------------------------- разрез

def _sections(count: int, size: int, level: str = "#") -> str:
    return "\n\n".join("%s Раздел %d\n\n%s" % (level, i, ("Текст раздела %d. " % i) * (size // 16)) for i in range(1, count + 1))


@test
def test_a_long_text_is_split_by_top_level_headings_then_lower_ones_then_pages_then_paragraphs(tmp: Path):
    TT = _mod("transcript_text")
    short = "# Раздел\n\nкоротко"
    assert TT.split_parts(short) == [short]
    parts = TT.split_parts(_sections(4, 25_000), limit=60_000)
    assert len(parts) == 2 and all(len(p) <= 60_000 for p in parts), [len(p) for p in parts]
    assert all(p.lstrip().startswith("# Раздел") for p in parts), "часть начинается не с заголовка"
    assert "Раздел 1" in parts[0] and "Раздел 3" in parts[1]
    # раздел верхнего уровня больше предела — делится по заголовкам следующего уровня
    big = "# Один\n\n" + _sections(5, 20_000, "##")
    parts = TT.split_parts(big, limit=60_000)
    assert len(parts) >= 2 and all(len(p) <= 60_000 for p in parts), [len(p) for p in parts]
    assert parts[0].startswith("# Один") and parts[1].lstrip().startswith("## Раздел"), [p[:20] for p in parts]
    # без заголовков (pdf): по страницам, по сорок
    pages = "".join("\n\n<!-- стр. %d -->\n\n%s" % (n, "слово " * 150) for n in range(1, 101))
    parts = TT.split_parts(pages, limit=60_000, per_part=40)
    assert len(parts) == 3, [len(p) for p in parts]
    assert "<!-- стр. 1 -->" in parts[0] and "<!-- стр. 41 -->" in parts[1] and "<!-- стр. 81 -->" in parts[2]
    assert not any("<!-- стр. 41 -->" in p for p in (parts[0], parts[2]))
    # ни заголовков, ни страниц: по абзацам, ничего не теряется
    flat = "\n\n".join("Абзац %d. " % i + "слово " * 200 for i in range(300))
    parts = TT.split_parts(flat, limit=60_000)
    assert len(parts) >= 2 and all(len(p) <= 60_000 for p in parts)
    assert "".join(parts).count("Абзац") == 300, "при разрезе по абзацам потерян текст"


@test
def test_a_long_document_is_written_as_linked_parts_and_rewritten_cleanly(tmp: Path):
    root = make_project(tmp)
    body = "".join([para("Часть %d" % n, style="Heading1")
                    + "".join(para("Абзац %d.%d документа PRJ-A описывает требования к подсистеме и правила её работы." % (n, k))
                              for k in range(330)) for n in range(1, 4)])
    docx(root / "Raw/customer/Большой.docx", body)
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    names = sorted(p.name for p in (root / "Raw/customer").glob("Большой*.md"))
    assert names == ["Большой-часть-01.md", "Большой-часть-02.md"] or len(names) >= 2, (names, cp.stdout)
    assert "Большой.md" not in names, "рядом с частями лежит ещё и целый транскрипт"
    first = (root / "Raw/customer" / names[0]).read_text(encoding="utf-8")
    last = (root / "Raw/customer" / names[-1]).read_text(encoding="utf-8")
    n = len(names)
    assert re.search(r"^part: 1$", first, re.M) and re.search(r"^parts: %d$" % n, first, re.M), first[:400]
    assert "part_of: Большой" in first and "prev:" not in first and "next: %s" % names[1] in first, first[:500]
    assert "prev: %s" % names[-2] in last and "next:" not in last, last[:500]
    assert "Абзац 1.0 документа" in first and "Абзац 3.329 документа" in last
    written = {p.name: p.stat().st_mtime_ns for p in (root / "Raw/customer").glob("Большой*.md")}
    cp2 = run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert "пропущено (не изменились): 1" in cp2.stdout, cp2.stdout
    assert written == {p.name: p.stat().st_mtime_ns for p in (root / "Raw/customer").glob("Большой*.md")}, \
        "повторный запуск переписал части"
    # документ сократился — части уходят, остаётся один транскрипт
    docx(root / "Raw/customer/Большой.docx", para("Теперь документ короткий: " + PROSE))
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert sorted(p.name for p in (root / "Raw/customer").glob("Большой*.md")) == ["Большой.md"], \
        "прежние части остались рядом с новым транскриптом"


@test
def test_documents_with_one_name_and_different_extensions_keep_separate_transcripts(tmp: Path):
    """`X.docx` и `X.txt` рядом: общий `X.md` каждый запуск переписывался бы по очереди одним из них."""
    root = make_project(tmp)
    docx(root / "Raw/customer/Заявка.docx", para("Из документа Word: " + PROSE))
    (root / "Raw/customer/Заявка.txt").write_text("Из текстового файла: " + PROSE + "\n", encoding="utf-8")
    run_script("office_ingest.py", cwd=root, expect_rc=0)
    files = sorted(p.name for p in (root / "Raw/customer").glob("Заявка*.md"))
    assert len(files) == 2, files
    texts = {p.name: p.read_text(encoding="utf-8") for p in (root / "Raw/customer").glob("Заявка*.md")}
    assert sum("Из документа Word" in t for t in texts.values()) == 1 and \
        sum("Из текстового файла" in t for t in texts.values()) == 1, texts
    cp = run_script("office_ingest.py", cwd=root, expect_rc=0)
    assert "пропущено (не изменились): 2" in cp.stdout and "собрано: 0" in cp.stdout, cp.stdout
