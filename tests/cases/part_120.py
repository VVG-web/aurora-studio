"""Проверки движка Aurora, часть 120. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 5 «Цитируемые документы», пакет CT-16: закон, государственный контракт и договор нарезаются без модели
на дословные единицы (`legal_slicer`, команда `kb:cite`), единицы — карточки типа `cited-document` в разделе `Documents`.
Здесь — разрез по всем режимам (закон, контракт, заголовки, целый документ, длинная статья), правила «чей это источник»,
запись единиц (шапка по шаблону, идемпотентность, пересборка одной статьи, архив исчезнувших, занятое имя, скан), обход
цитируемых источников обычным разбором, закрытые карточки, вид `document`, ремонт, оглавление и карты, порядок шагов
маршрутов. Фикстуры — tests/corpus/laws (придуманные закон и контракт). Модель и сеть не используются; проекты — только во
временных папках, настоящий HOME прогона подменён раннером.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cases import part_70 as P70
from harness import KIT, SCRIPTS, card, run, test

KB = "AuroraKnowledgeDB"
LAWS = KIT / "tests" / "corpus" / "laws"
LAW = "Raw/laws/Закон о примерах.md"
CONTRACT = "Raw/contract/Государственный контракт.md"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _fx(name: str) -> str:
    return (LAWS / name).read_text(encoding="utf-8")


def _flat(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _commit(root: Path) -> None:
    P70._commit(root)


def _project(tmp: Path, law: str = "law_chapters.md") -> Path:
    """Проект с шаблонами кита, законом в Raw/laws и чистым деревом."""
    root = P70._project(tmp)
    if law:
        _put(root, LAW, _fx(law))
    _commit(root)
    return root


def _cite(root: Path, *args, expect=None):
    return run("kb_cited.py", *args, cwd=root, expect_rc=expect)


def _docs(root: Path) -> list:
    folder = root / KB / "Documents"
    return sorted(p.name for p in folder.glob("*.md")) if folder.is_dir() else []


def _snapshot(root: Path, folder: str) -> dict:
    base = root / folder
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(base.rglob("*.md"))}


def _covered(res) -> str:
    """Склейка единиц и того, что в единицы не вошло, в порядке источника, без пробельных."""
    parts = sorted([(u.start, u.text) for u in res.units] + [(d.start, d.text) for d in res.dropped], key=lambda x: x[0])
    return _flat("".join(t for _s, t in parts))


def _py(root: Path, code: str) -> str:
    """Короткая программа в папке проекта (скрипты, читающие пути от текущей папки, не запускают в процессе прогона); сообщения об ошибках идут в её вывод."""
    cp = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); sys.stdout.reconfigure(encoding='utf-8'); sys.stderr = sys.stdout\n{code}"],
                        cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


# ------------------------------------------------------------------ разрез: режимы (AC-CT-16.1)

@test
def test_slicer_law_with_chapters_gives_one_unit_per_article(_t):
    """Закон с главами и статьями `###`: режим law, шесть единиц, путь `Глава N > ст. N`, главы и короткая преамбула вне единиц."""
    LS = _mod("legal_slicer")
    res = LS.slice_text(_fx("law_chapters.md"), "Закон о примерах.md")
    assert res.mode == "law" and res.doc == "Закон о примерах", (res.mode, res.doc)
    assert [u.unit for u in res.units] == [f"ст. {n}" for n in range(1, 7)]
    assert [u.path for u in res.units] == ["Глава 1 > ст. 1", "Глава 1 > ст. 2", "Глава 1 > ст. 3",
                                           "Глава 2 > ст. 4", "Глава 2 > ст. 5", "Глава 2 > ст. 6"]
    assert [u.order for u in res.units] == [1, 2, 3, 4, 5, 6] and {u.kind for u in res.units} == {"article"}
    assert res.units[2].heading == "Подтверждение заявки"
    assert res.doc_ref == "№ 1 от 01.02.2020" and res.edition == "ред. от 2023-03-15", (res.doc_ref, res.edition)
    assert sorted(d.kind for d in res.dropped) == ["chapter", "chapter", "preamble"]
    assert all(u.text.lstrip("# ").startswith("Статья") for u in res.units), "единица начинается со своего заголовка"


@test
def test_slicer_marker_without_hashes_and_bold_marker_give_the_same_units(_t):
    """Строки `Статья 1. …` без разметки и `**Статья 1. …**` дают те же шесть единиц."""
    LS = _mod("legal_slicer")
    base = LS.slice_text(_fx("law_chapters.md"), "x.md")
    for name in ("law_plain.md", "law_bold.md"):
        res = LS.slice_text(_fx(name), name)
        assert res.mode == "law" and [u.unit for u in res.units] == [u.unit for u in base.units], name
        assert [u.heading for u in res.units] == [u.heading for u in base.units], name
    assert LS.slice_text(_fx("law_plain.md")).units[0].text.startswith("Статья 1. Предмет закона")
    assert LS.slice_text(_fx("law_bold.md")).units[0].text.startswith("**Статья 1. Предмет закона**"), "текст единицы — как в источнике"


@test
def test_slicer_contract_cuts_clauses_and_splits_a_section_longer_than_the_limit(_t):
    """Контракт: режим contract, единицы `п. 1`…`п. 7`; раздел 2 длиннее предела режется на `п. 2.1`, `п. 2.2`; приложение отдельно."""
    LS = _mod("legal_slicer")
    res = LS.slice_text(_fx("contract.md"), "Государственный контракт.md")
    assert res.mode == "contract" and res.doc == "Государственный контракт на поставку товаров"
    labels = [u.unit for u in res.units]
    assert labels == ["преамбула", "п. 1", "п. 2.1", "п. 2.2", "п. 3", "п. 4", "п. 5", "п. 6", "п. 7", "прил. 1"], labels
    sub = [u for u in res.units if u.unit.startswith("п. 2.")]
    assert [u.path for u in sub] == ["п. 2 > п. 2.1", "п. 2 > п. 2.2"] and all(u.heading == "Цена и порядок расчётов" for u in sub)
    assert all(len(u.text) <= LS.UNIT_MAX for u in res.units)
    assert res.doc_ref == "№ 7 от 10.03.2021"
    assert [u.kind for u in res.units][0] == "preamble" and res.units[-1].kind == "appendix"


@test
def test_slicer_document_without_markers_is_one_unit(_t):
    """Три коротких абзаца без маркеров: режим whole, одна единица `документ`."""
    LS = _mod("legal_slicer")
    res = LS.slice_text(_fx("no_markers.md"), "Памятка.md")
    assert res.mode == "whole" and [u.unit for u in res.units] == ["документ"]
    assert res.units[0].slug == "документ" and "Срок подтверждения" in res.units[0].text


@test
def test_slicer_markdown_headings_make_sections(_t):
    """Документ без статей и пунктов, но с заголовками: режим headings, «раздел N» по порядку; короткая секция присоединяется к соседней."""
    LS = _mod("legal_slicer")
    para = "Описание порядка работы склада при приёмке товара от поставщика и оформлении накладной. " * 4
    text = f"# Регламент склада\n\n## Приёмка\n\n{para}\n\n## Хранение\n\n{para}\n\n## Короткая\n\nОдна строка.\n\n## Выдача\n\n{para}\n"
    res = LS.slice_text(text, "Регламент.md")
    assert res.mode == "headings" and [u.unit for u in res.units] == ["раздел 1", "раздел 2", "раздел 3"], [u.unit for u in res.units]
    assert [u.heading for u in res.units] == ["Приёмка", "Хранение", "Выдача"]
    assert "Одна строка." in res.units[1].text, "короткая секция осталась в предыдущей"
    assert LS.uncovered(res) == [] and _covered(res) == _flat("".join(res.lines))


@test
def test_slicer_long_article_splits_by_part_numbers(_t):
    """Статья на 14 000 знаков с пунктами `1.`…`9.`: подъединицы `ст. 5 ч. 1–3`, `ч. 4–6`, `ч. 7–9`, каждая не длиннее предела."""
    LS = _mod("legal_slicer")
    res = LS.slice_text(_fx("law_big_article.md"), "x.md")
    labels = [u.unit for u in res.units]
    assert labels == ["ст. 1", "ст. 2", "ст. 3", "ст. 4", "ст. 5 ч. 1–3", "ст. 5 ч. 4–6", "ст. 5 ч. 7–9", "ст. 6"], labels
    parts = [u for u in res.units if u.unit.startswith("ст. 5")]
    assert all(len(u.text) <= LS.UNIT_MAX for u in parts) and sum(len(u.text) for u in parts) > 12000
    assert [u.slug for u in parts] == ["ст-5-ч-1-3", "ст-5-ч-4-6", "ст-5-ч-7-9"]
    assert parts[0].text.startswith("### Статья 5. Очень длинная статья"), "заголовок статьи — в первой части"
    assert all(u.heading == "Очень длинная статья" for u in parts)


@test
def test_slicer_long_text_without_part_numbers_splits_by_paragraphs(_t):
    """Длинная единица без номеров частей делится по абзацам: `ст. 5 (часть 1 из N)`; абзац длиннее предела режется по пробелу."""
    LS = _mod("legal_slicer")
    para = "Условие исполнения заявки определяется сторонами по согласованию и фиксируется в накладной. "
    body = "\n\n".join(para * 8 for _ in range(14))
    text = "# Закон\n\n### Статья 1. Первая\n\nТекст.\n\n### Статья 2. Вторая\n\nТекст.\n\n" \
           f"### Статья 3. Длинная\n\n{body}\n\n### Статья 4. Четвёртая\n\nТекст.\n"
    res = LS.slice_text(text, "Закон.md")
    long = [u for u in res.units if u.unit.startswith("ст. 3")]
    n = len(long)
    assert n >= 2 and [u.unit for u in long] == [f"ст. 3 (часть {k} из {n})" for k in range(1, n + 1)], [u.unit for u in long]
    assert [u.slug for u in long] == [f"ст-3-ч-{k}-из-{n}" for k in range(1, n + 1)]
    assert all(len(u.text) <= LS.UNIT_MAX for u in long) and LS.uncovered(res) == []
    assert _flat("".join(u.text for u in long)) == _flat(f"### Статья 3. Длинная\n\n{body}")
    solid = LS.slice_text("# З\n\n### Статья 1. А\n\nx\n\n### Статья 2. Б\n\ny\n\n### Статья 3. В\n\n" + ("слово " * 3000), "з.md")
    assert all(len(u.text) <= LS.UNIT_MAX for u in solid.units), "абзац без пустых строк режется по пробелу"
    assert _flat("".join(u.text for u in solid.units if u.unit.startswith("ст. 3"))) == _flat("### Статья 3. В\n\n" + "слово " * 3000)


@test
def test_slicer_concatenation_of_units_equals_the_source(_t):
    """AC-CT-16.3: единицы и то, что в них не вошло (заголовки глав, короткая преамбула), вместе дают источник с точностью до пробелов."""
    LS = _mod("legal_slicer")
    for name in sorted(p.name for p in LAWS.glob("*.md")):
        res = LS.slice_text(_fx(name), name)
        assert res.units, name
        assert LS.uncovered(res) == [], (name, [res.lines[i][:40] for i in LS.uncovered(res)][:3])
        assert _covered(res) == _flat("".join(res.lines)), name
        assert all(len(u.text) <= LS.UNIT_MAX for u in res.units), name


@test
def test_slicer_is_deterministic_and_does_not_depend_on_line_endings(_t):
    """AC-CT-16.2: дважды — те же единицы; источник с `\\r\\n` режется так же, как с `\\n`; шапка и предупреждение конвертера в тело не входят."""
    LS = _mod("legal_slicer")
    text = _fx("law_chapters.md")
    a, b = LS.slice_text(text, "x.md"), LS.slice_text(text, "x.md")
    assert [(u.unit, u.path, u.text, u.slug, u.order) for u in a.units] == [(u.unit, u.path, u.text, u.slug, u.order) for u in b.units]
    crlf = LS.slice_text(text.replace("\n", "\r\n"), "x.md")
    assert [(u.unit, u.text) for u in crlf.units] == [(u.unit, u.text) for u in a.units]
    conv = ('---\ntitle: "Закон о примерах"\nconverter: pandoc\nconverted: 2026-01-01\n---\n\n'
            "> ⚙️ **Машинная конвертация.** Истина — оригинал `x.docx`; здесь возможны потери разметки.\n\n" + text)
    got = LS.slice_text(conv, "Закон о примерах.converted.md")
    assert [(u.unit, u.text) for u in got.units] == [(u.unit, u.text) for u in a.units], "предупреждение конвертера не попало в единицы"
    assert not any("Машинная конвертация" in d.text for d in got.dropped) and got.doc == "Закон о примерах"


@test
def test_slicer_does_not_take_references_in_the_text_for_markers(_t):
    """«Статья 5 настоящего закона…» в абзаце, «Приложение 1 является частью…», список внутри раздела договора — не маркеры."""
    LS = _mod("legal_slicer")
    text = ("# Закон\n\n### Статья 1. Первая\n\nСтатья 5 настоящего закона применяется к заявкам. Приложение 1 является неотъемлемой частью закона.\n\n"
            "Статья 2 изложить в следующей редакции.\n\n### Статья 2. Вторая\n\nТекст второй.\n\n### Статья 3. Третья\n\nТекст третий.\n")
    res = LS.slice_text(text, "з.md")
    assert [u.unit for u in res.units] == ["ст. 1", "ст. 2", "ст. 3"], [u.unit for u in res.units]
    assert "Статья 5 настоящего закона применяется" in res.units[0].text
    contract = ("# Договор\n\n1. Предмет договора\n\nПоставщик передаёт товар.\n\n2. Права сторон\n\n1. Первое право стороны\n2. Второе право стороны\n\n"
                "3. Ответственность\n\nСтороны отвечают.\n\n4. Заключительные положения\n\nДоговор подписан.\n")
    got = LS.slice_text(contract, "Договор.md")
    assert got.mode == "contract" and [u.unit for u in got.units if u.kind == "clause"] == ["п. 1", "п. 2", "п. 3", "п. 4"], \
        [u.unit for u in got.units]


@test
def test_slicer_numbered_lines_of_an_appendix_are_not_clauses(_t):
    """После «Приложение» нумерованные строки — таблица приложения, а не разделы договора; статьи в приложении закона — его текст."""
    LS = _mod("legal_slicer")
    contract = ("# Договор поставки\n\n1. Предмет договора\n\nТекст первого раздела договора.\n\n2. Цена договора\n\nТекст второго раздела.\n\n"
                "3. Сроки поставки\n\nТекст третьего раздела.\n\nПриложение № 1 к договору\n\n1. Спецификация\n\n2. График\n\n3. Порядок\n\nТекст.\n")
    got = LS.slice_text(contract, "Договор.md")
    assert [u.unit for u in got.units if u.kind == "clause"] == ["п. 1", "п. 2", "п. 3"]
    assert got.units[-1].unit == "прил. 1" and "3. Порядок" in got.units[-1].text
    law = ("# Закон\n\n### Статья 1. А\n\nа\n\n### Статья 2. Б\n\nб\n\n### Статья 3. В\n\nв\n\n### Приложение 1. Образец\n\n### Статья 1. Внутри\n\nг\n")
    res = LS.slice_text(law, "з.md")
    assert [u.unit for u in res.units] == ["ст. 1", "ст. 2", "ст. 3", "прил. 1"] and "Статья 1. Внутри" in res.units[-1].text


@test
def test_slicer_document_properties_come_from_the_header_the_heading_or_the_file_name(_t):
    """Короткое имя документа: `title` шапки, иначе первый H1, иначе имя файла без `.converted`; не длиннее 60 знаков."""
    LS = _mod("legal_slicer")
    body = "### Статья 1. А\n\nа\n\n### Статья 2. Б\n\nб\n\n### Статья 3. В\n\nв\n"
    assert LS.slice_text('---\ntitle: "Закон из шапки"\n---\n\n# Заголовок текста\n\n' + body, "f.md").doc == "Закон из шапки"
    assert LS.slice_text("# Заголовок текста\n\n" + body, "f.md").doc == "Заголовок текста"
    assert LS.slice_text(body, "Положение о складе.converted.md").doc == "Положение о складе"
    long = LS.slice_text("# " + "Очень длинное название документа о поставке товаров на склад " * 3 + "\n\n" + body, "f.md").doc
    assert len(long) <= LS.DOC_NAME_MAX, long
    res = LS.slice_text("# Закон о примерах\n\nЗакон № 12-ФЗ от 03.04.2019 (в ред. от 01.02.2021)\n\n" + body, "f.md")
    assert res.doc_ref == "№ 12-ФЗ от 03.04.2019" and res.edition == "ред. от 2021-02-01", (res.doc_ref, res.edition)
    u = res.units[1]
    assert LS.unit_title(res.doc, u) == "Закон о примерах — ст. 2. Б"
    assert LS.unit_cite(res.doc, u, res.doc_ref) == "ст. 2, Закон о примерах (№ 12-ФЗ от 03.04.2019)"
    assert LS.unit_cite(res.doc, u) == "ст. 2, Закон о примерах"


@test
def test_slicer_speed_on_a_big_law(_t):
    """Закон на 2000 статей режется быстро (время процессора, а не часы: на машине идёт чужая работа)."""
    LS = _mod("legal_slicer")
    text = "# Закон\n\n" + "\n".join(f"### Статья {n}. Тема {n}\n\n1. Поставщик обязан подтвердить заявку номер {n}.\n"
                                    "2. Заказчик обязан принять товар по накладной.\n" for n in range(1, 2001))
    t0 = time.process_time()
    res = LS.slice_text(text, "Закон.md")
    spent = time.process_time() - t0
    assert len(res.units) == 2000 and spent < 5, (len(res.units), spent)


# ------------------------------------------------------------------ чей это источник (R-0…R-5)

@test
def test_cited_source_routing(tmp: Path):
    """R-0…R-5: закон — цитируемый; `cited: true` включает; `cited: false` отключает окончательно; договор — по имени, ТЗ — нет; Sources — никогда; скан — отказ."""
    LS = _mod("legal_slicer")
    root = tmp / "p"

    def verdict(rel, head=""):
        text = (f"---\n{head}\n---\n\n" if head else "") + "Текст документа. " * 20
        return LS.source_verdict(rel, text, str(root))[0]

    assert verdict("Raw/laws/Закон.md") == "cited"
    assert verdict("Raw/laws/Закон.md", "cited: false") == "plain", "R-0: пометка сильнее папки"
    assert verdict("Raw/laws/Закон.md", "cited: true") == "cited"
    assert verdict("Raw/customer/Письмо.md") == "plain" and verdict("Raw/customer/Письмо.md", "cited: true") == "cited"
    assert verdict("Raw/project/Концепция.md", "cited: yes") == "cited" and verdict("Raw/project/Концепция.md", "cited: false") == "plain"
    for name in ("Государственный контракт.md", "Госконтракт №5.md", "Договор поставки.md", "Дополнительное соглашение.md",
                 "государственный_контракт.md", "ДОГОВОР.md"):
        assert verdict(f"Raw/contract/{name}") == "cited", name
    for name in ("Техническое задание.md", "ТЗ к контракту.md", "Пояснительная записка к договору.md", "ПМИ договор.md", "График.md"):
        assert verdict(f"Raw/contract/{name}") == "plain", name
    assert verdict("Raw/contract/Договор.md", "cited: false") == "plain"
    assert verdict("Raw/customer/Договор.md") == "plain", "имя решает только в Raw/contract"
    for rel in ("Sources/Confluence/Закон.md", "Sources/Web/Договор.md", "Raw/meetings/Договор.md", "Raw/laws/Закон.docx",
                "AuroraKnowledgeDB/Concepts/Закон.md"):
        assert verdict(rel, "cited: true") == "plain", rel
    v, why = LS.source_verdict("Raw/laws/Скан.md", "---\nconverter: ocr-model\n---\n\n" + "Текст. " * 40, str(root))
    assert v == "ocr" and "скан" in why and LS.is_cited_source("Raw/laws/Скан.md", "---\nconverter: ocr-model\n---\n\nx", str(root)), (v, why)
    assert LS.source_verdict("Raw/laws/Скан.md", "---\nconverter: ocr-model\ncited: false\n---\n\nx", str(root))[0] == "plain"
    assert not LS.is_cited_source("Raw/laws/Закон.md", "---\ncited: false\n---\n\nx", str(root))
    here = tmp / "q"
    (here / "Raw" / "laws").mkdir(parents=True)
    (here / "Raw" / "laws" / "Файл.md").write_text("---\ncited: false\n---\n\n" + "Текст. " * 40, encoding="utf-8")
    assert LS.source_verdict("Raw/laws/Файл.md", "", str(here))[0] == "plain", "шапка читается из файла, когда текста не дали"


# ------------------------------------------------------------------ kb:cite: запись единиц (AC-CT-16.1, 16.2, 16.4)

@test
def test_cite_dry_run_writes_nothing_and_apply_creates_cards_that_pass_the_template(tmp: Path):
    """Сухой прогон ничего не пишет (код 1: есть что делать); `--apply` создаёт шесть карточек по шаблону, строго без ошибок."""
    root = _project(tmp)
    cp = _cite(root, expect=1)
    assert "создать 6" in cp.stdout and "(dry-run)" in cp.stdout, cp.stdout
    assert _docs(root) == [] and not (root / KB / "meta" / "manifest.json").exists(), "сухой прогон писал"
    cp = _cite(root, "--apply", expect=0)
    names = _docs(root)
    assert names == [f"Закон-о-примерах-ст-{n}.md" for n in range(1, 7)], names
    C, AC = _mod("card_types"), _mod("aurora_common")
    reg = C.registry_for(str(root))
    for n in names:
        text = _read(root, f"{KB}/Documents/{n}")
        errors = [v for v in C.validate_card(text, path=f"{KB}/Documents/{n}", reg=reg, level="strict") if v.severity == "error"]
        assert not errors, (n, [(v.code, v.message) for v in errors])
    text = _read(root, f"{KB}/Documents/Закон-о-примерах-ст-4.md")
    fm = AC.frontmatter(text)
    assert fm["type"] == "cited-document" and fm["template"] == "akdb_cited-document_v1.0.md" and fm["kind"] == "document"
    assert fm["status"] == "knowledge" and fm["built"] == "machine" and fm["built_by"] == "script" and fm["trust"] == "raw"
    assert fm["doc"] == "Закон о примерах" and fm["unit"] == "ст. 4" and fm["unit_kind"] == "article" and fm["order"] == "4"
    assert fm["path"] == "Глава 2 > ст. 4" and fm["doc_ref"] == "№ 1 от 01.02.2020" and fm["edition"] == "ред. от 2023-03-15"
    assert fm["cite"] == "ст. 4, Закон о примерах (№ 1 от 01.02.2020)"
    assert fm["prev"] == "[[Закон-о-примерах-ст-3]]" and fm["next"] == "[[Закон-о-примерах-ст-5]]", (fm["prev"], fm["next"])
    assert AC.card_sources(text) == [LAW] and "prev" not in AC.frontmatter(_read(root, f"{KB}/Documents/Закон-о-примерах-ст-1.md"))
    assert "next" not in AC.frontmatter(_read(root, f"{KB}/Documents/Закон-о-примерах-ст-6.md"))
    LS = _mod("legal_slicer")
    res = LS.slice_text(_fx("law_chapters.md"), LAW)
    for u, n in zip(res.units, names):
        body = AC.body(_read(root, f"{KB}/Documents/{n}")).strip()
        assert body == f"# {LS.unit_title(res.doc, u)}\n\n{u.text}", n
    assert _cite(root, "--check", expect=0).returncode == 0
    assert run("card_types.py", "--check", "--strict", cwd=root).returncode == 0
    man = json.loads((root / KB / "meta" / "manifest.json").read_text(encoding="utf-8"))["sources"][LAW]
    assert man["by"] == "kb:cite" and man["cards"] == 6 and man["hash"], man


@test
def test_cite_second_run_writes_nothing_and_one_changed_article_rewrites_one_card(tmp: Path):
    """Повторный прогон — ноль записей; правка текста одной статьи — переписана одна карточка с записью в истории, остальные не тронуты."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    _commit(root)
    again = _cite(root, "--apply", expect=0)
    assert "Записано: 0" in again.stdout, again.stdout
    assert subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip() == ""
    before = _snapshot(root, f"{KB}/Documents")
    src = (root / LAW).read_text(encoding="utf-8")
    assert "в течение трёх рабочих дней" in src
    (root / LAW).write_bytes(src.replace("в течение трёх рабочих дней", "в течение пяти рабочих дней", 1).encode("utf-8"))
    cp = _cite(root, "--apply", expect=0)
    assert "Записано: 1" in cp.stdout, cp.stdout
    after = _snapshot(root, f"{KB}/Documents")
    changed = [k for k in after if after[k] != before[k]]
    assert changed == [f"{KB}/Documents/Закон-о-примерах-ст-3.md"], changed
    text = after[changed[0]].decode("utf-8")
    assert "в течение пяти рабочих дней" in text and "## История изменений" in text and "текст единицы обновлён из источника" in text
    _commit(root)
    again = _cite(root, "--apply", expect=0)
    assert "Записано: 0" in again.stdout
    assert _snapshot(root, f"{KB}/Documents") == after, "после пересборки повторный прогон меняет файлы"


@test
def test_cite_inserted_article_moves_order_and_vanished_article_goes_to_the_archive(tmp: Path):
    """Новая статья вставлена, последняя убрана: порядок и соседи пересчитаны, новая создана, исчезнувшая — в `_archive/` со статусом `deprecated`."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    _commit(root)
    text = (root / LAW).read_text(encoding="utf-8")
    cut = text[:text.index("### Статья 6.")]
    at = cut.index("### Статья 4.")
    new = cut[:at] + "### Статья 3.1. Новая статья\n\n1. Новая статья о возврате товара вставлена между третьей и четвёртой.\n\n" + cut[at:]
    (root / LAW).write_bytes(new.encode("utf-8"))
    cp = _cite(root, "--apply", expect=0)
    assert "создать 1" in cp.stdout and "в архив 1" in cp.stdout, cp.stdout
    assert _docs(root) == ["Закон-о-примерах-ст-1.md", "Закон-о-примерах-ст-2.md", "Закон-о-примерах-ст-3-1.md",
                           "Закон-о-примерах-ст-3.md", "Закон-о-примерах-ст-4.md", "Закон-о-примерах-ст-5.md"]
    AC = _mod("aurora_common")
    s4 = AC.frontmatter(_read(root, f"{KB}/Documents/Закон-о-примерах-ст-4.md"))
    assert s4["order"] == "5" and s4["prev"] == "[[Закон-о-примерах-ст-3-1]]" and s4["next"] == "[[Закон-о-примерах-ст-5]]"
    assert AC.frontmatter(_read(root, f"{KB}/Documents/Закон-о-примерах-ст-3.md"))["next"] == "[[Закон-о-примерах-ст-3-1]]"
    assert "next" not in AC.frontmatter(_read(root, f"{KB}/Documents/Закон-о-примерах-ст-5.md"))
    gone = [p for p in (root / KB / "_archive").glob("*ст-6*.md")]
    assert len(gone) == 1
    old = gone[0].read_text(encoding="utf-8")
    assert AC.frontmatter(old)["status"] == "deprecated" and "единицы больше нет в источнике" in old
    _commit(root)
    again = _cite(root, "--apply", expect=0)
    assert "Записано: 0" in again.stdout and "в архив 0" in again.stdout, again.stdout


@test
def test_cite_refuses_a_source_recognized_from_a_scan(tmp: Path):
    """R-5: источник с `converter: ocr…` не режется, карточек нет, в отчёте причина; обычный разбор его тоже не берёт."""
    root = _project(tmp, law="")
    _put(root, "Raw/laws/Скан закона.md", _fx("scan_ocr.md"))
    _commit(root)
    cp = _cite(root, expect=1)
    assert "не режется" in cp.stdout and "распознан по скану" in cp.stdout and "цитировать нельзя" in cp.stdout, cp.stdout
    cp = _cite(root, "--apply", expect=1)
    assert _docs(root) == [] and "распознан по скану" in cp.stdout
    out = _py(root, "import build_plan as BP\nprint([p for _g, p, _s in BP.sources()])")
    assert "Скан" not in out, out
    found = _cite(root, "--check", expect=1)
    assert "V-CITE-4" in found.stdout, found.stdout


@test
def test_cite_name_taken_by_another_card_is_a_finding_not_an_overwrite(tmp: Path):
    """Имя единицы занято чужой карточкой: единица не создаётся (V-CITE-1), чужая карточка цела, остальные пишутся."""
    root = _project(tmp)
    card(root, "Concepts/Закон-о-примерах-ст-2.md", "Статья два — понятие проекта, не закон.", status="knowledge", kind="knowledge")
    _commit(root)
    mine = (root / KB / "Concepts" / "Закон-о-примерах-ст-2.md").read_bytes()
    cp = _cite(root, "--apply", expect=1)
    assert "V-CITE-1" in cp.stdout and "Закон-о-примерах-ст-2" in cp.stdout, cp.stdout
    assert _docs(root) == [f"Закон-о-примерах-ст-{n}.md" for n in (1, 3, 4, 5, 6)]
    assert (root / KB / "Concepts" / "Закон-о-примерах-ст-2.md").read_bytes() == mine
    assert "V-CITE-1" in _cite(root, "--check", expect=1).stdout


@test
def test_cite_repeated_article_number_gets_a_suffix_and_two_documents_with_one_name_get_two_slugs(tmp: Path):
    """Статья 5 дважды в одном документе — `…-ст-5` и `…-ст-5-2`; два документа с одним именем — разные имена карточек, и от порядка обхода они не зависят."""
    root = _project(tmp, law="")
    _put(root, "Raw/laws/Закон дубль.md", _fx("law_duplicate.md"))
    _commit(root)
    _cite(root, "--apply", expect=0)
    names = _docs(root)
    assert "Закон-о-примерах-ст-5.md" in names and "Закон-о-примерах-ст-5-2.md" in names, names
    AC = _mod("aurora_common")
    assert AC.frontmatter(_read(root, f"{KB}/Documents/Закон-о-примерах-ст-5-2.md"))["order"] == "7"
    _put(root, "Raw/laws/Закон второй.md", _fx("law_plain.md"))
    _commit(root)
    _cite(root, "--apply", expect=0)
    names = _docs(root)
    second = sorted(n for n in names if n.startswith("Закон-о-примерах-2-"))
    assert len(second) == 6, names
    assert len(names) == len(set(n.lower() for n in names))
    snap = _snapshot(root, f"{KB}/Documents")
    _commit(root)
    assert "Записано: 0" in _cite(root, "--apply", expect=0).stdout
    assert _snapshot(root, f"{KB}/Documents") == snap
    (root / "Raw/laws/Закон дубль.md").unlink()
    _commit(root)
    _cite(root, "--apply", "--allow-dirty", expect=0)
    assert sorted(n for n in _docs(root) if n.startswith("Закон-о-примерах-2-")) == second, "имена оставшегося документа сменились"


@test
def test_cite_follows_a_renamed_source_instead_of_refusing_taken_names(tmp: Path):
    """Закон переименован или перенесён в другую папку: те же единицы переходят к новому пути источника, отказов по занятым именам нет, новых карточек нет."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    _commit(root)
    before = {k: v for k, v in _snapshot(root, f"{KB}/Documents").items()}
    (root / "Raw" / "laws" / "Архив").mkdir()
    (root / LAW).rename(root / "Raw" / "laws" / "Архив" / "Закон о примерах (старая редакция).md")
    _commit(root)
    cp = _cite(root, "--apply", expect=0)
    assert "V-CITE-1" not in cp.stdout and "обновить 6" in cp.stdout and "создать 0" in cp.stdout, cp.stdout
    after = _snapshot(root, f"{KB}/Documents")
    assert sorted(after) == sorted(before), "появились лишние карточки или пропали прежние"
    AC = _mod("aurora_common")
    for rel, data in after.items():
        assert AC.card_sources(data.decode("utf-8")) == ["Raw/laws/Архив/Закон о примерах (старая редакция).md"], rel
    _commit(root)
    assert "Записано: 0" in _cite(root, "--apply", expect=0).stdout


@test
def test_cite_mixed_sources_contract_by_name_marked_customer_file_and_tz_left_to_the_parser(tmp: Path):
    """Контракт в `Raw/contract` по имени и файл с `cited: true` режутся; ТЗ рядом и обычный документ — нет; закон с `cited: false` не режется."""
    root = _project(tmp, law="")
    _put(root, CONTRACT, _fx("contract.md"))
    _put(root, "Raw/contract/Техническое задание.md", "# ТЗ\n\n1. Цель\n\n" + "Описание цели. " * 30 + "\n")
    _put(root, "Raw/customer/Регламент заказчика.md", "---\ncited: true\n---\n\n# Регламент\n\n## Приёмка\n\n" + "Правило приёмки. " * 30
         + "\n\n## Выдача\n\n" + "Правило выдачи. " * 30 + "\n")
    _put(root, "Raw/customer/Письмо.md", "# Письмо\n\n" + "Просим уточнить сроки. " * 30 + "\n")
    _put(root, "Raw/laws/Закон без разреза.md", "---\ncited: false\n---\n\n" + _fx("law_plain.md"))
    _commit(root)
    cp = _cite(root, expect=1)
    assert "Государственный контракт.md" in cp.stdout and "Регламент заказчика.md" in cp.stdout, cp.stdout
    assert "Техническое задание" not in cp.stdout and "Письмо" not in cp.stdout and "без разреза" not in cp.stdout, cp.stdout
    _cite(root, "--apply", expect=0)
    names = _docs(root)
    assert "Государственный-контракт-на-поставку-товаров-п-2-1.md" in names and "Государственный-контракт-на-поставку-товаров-прил-1.md" in names
    assert any(n.startswith("Регламент-") and "раздел-1" in n for n in names), names
    out = json.loads(_py(root, "import build_plan as BP, json\nprint(json.dumps([p for _g, p, _s in BP.sources()], ensure_ascii=False))"))
    assert sorted(out) == sorted(["Raw/customer/Письмо.md", "Raw/laws/Закон без разреза.md", "Raw/contract/Техническое задание.md"]), out


@test
def test_cite_ignores_a_machine_copy_when_the_person_kept_a_copy_and_skips_templates_and_stubs(tmp: Path):
    """Расшифровка `X.converted.md` рядом с копией человека не источник (как в обычном разборе); заглушка и шаблон — тоже."""
    root = _project(tmp, law="")
    text = _fx("law_plain.md")
    _put(root, "Raw/laws/Закон.md", text)
    _put(root, "Raw/laws/Закон.converted.md",
         '---\ntitle: "Закон"\nconverter: pandoc\nconverted_from: "Закон.docx"\n---\n\n> ⚙️ **Машинная конвертация.** Истина — оригинал.\n\n' + text)
    _put(root, "Raw/laws/Короткий.md", "Статья 1. Мало.\n")
    _put(root, "Raw/laws/Шаблон закона.md", "# Шаблон\n\n" + "Текст шаблона. " * 30 + "\n")
    _commit(root)
    cp = _cite(root, expect=1)
    assert "Raw/laws/Закон.md" in cp.stdout and "converted" not in cp.stdout and "Короткий" not in cp.stdout and "Шаблон" not in cp.stdout, cp.stdout


# ------------------------------------------------------------------ код V-21 и слово человека

@test
def test_validate_v21_flags_a_unit_without_passport_fields_or_with_a_human_correction(tmp: Path):
    """V-21: у единицы нет `doc`, `unit` или `cite` либо есть `corrected_by` — ошибка; исправная единица и чужие типы кода не получают."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    C = _mod("card_types")
    reg = C.registry_for(str(root))
    rel = f"{KB}/Documents/Закон-о-примерах-ст-2.md"
    good = _read(root, rel)

    def codes(text: str, path: str = rel) -> list:
        return [v.code for v in C.validate_card(text, path=path, reg=reg, level="strict") if v.severity == "error"]

    assert codes(good) == [], "исправная единица"
    for field in ("doc", "unit", "cite"):
        broken = re.sub(rf"(?m)^{field}: .*\n", "", good, count=1)
        assert "V-21" in codes(broken), field
    assert "V-21" in codes(good.replace("\nbuilt: machine", "\ncorrected_by: x\nbuilt: machine", 1))
    concept = ("---\ntitle: \"Заявка\"\ntype: concept\ntemplate: akdb_concept_v1.0.md\nkind: knowledge\nstatus: knowledge\n"
               "corrected_by: x\ncreated: 2026-01-01\nupdated: 2026-01-01\nbuilt: machine\nsources:\n  - \"Sources/Confluence/Заявка.md\"\n"
               "source_synced: 2026-01-01\n---\n\nЗаявка — документ.\n")
    assert "V-21" not in codes(concept, f"{KB}/Concepts/Заявка.md"), "поле `corrected_by` законно у других типов"
    assert C.SEVERITY["V-21"] == "error" and "V-21" in C.TITLES


@test
def test_kb_correct_leaves_a_cited_unit_alone_and_names_the_reason(tmp: Path):
    """Исправление человека к единице закона не записывается (V-21): закон не правят; причина названа, остальные карточки не страдают."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    card(root, "Concepts/Заявка.md", "Заявка — документ, которым заказчик поручает поставку.", status="knowledge", kind="knowledge")
    _put(root, "Raw/corrections/2026-10-29-аналитик.md",
         '---\ncorrects: "[[Закон-о-примерах-ст-2]]"\ncreated: 2026-10-29\nstatus: active\n---\n\n# Исправление\n\nЧитать иначе.\n')
    _commit(root)
    before = _read(root, f"{KB}/Documents/Закон-о-примерах-ст-2.md")
    cp = run("kb_corrections.py", "--apply", cwd=root)
    assert "V-21" in cp.stdout and "закон и контракт не правят" in cp.stdout and "Записано в карточки: 0" in cp.stdout, cp.stdout
    assert _read(root, f"{KB}/Documents/Закон-о-примерах-ст-2.md") == before, "единицу исправили"


# ------------------------------------------------------------------ обычный разбор, закрытые карточки, вид, ремонт (AC-CT-16.5…16.7)

@test
def test_agent_build_skips_cited_sources_and_keeps_the_rest(tmp: Path):
    """AC-CT-16.5: `sources()` не отдаёт цитируемые источники (контракт по имени, помеченный файл); ТЗ и обычный документ остаются; закон с `cited: false` теперь разбирается."""
    root = _project(tmp, law="")
    _put(root, CONTRACT, _fx("contract.md"))
    _put(root, "Raw/contract/Техническое задание.md", "# ТЗ\n\n## Цель\n\n" + "Описание цели. " * 30 + "\n\n## Объём\n\n" + "Описание объёма. " * 30 + "\n")
    _put(root, "Raw/project/Помеченный.md", "---\ncited: true\n---\n\n# Док\n\n" + "Текст. " * 60 + "\n")
    _put(root, "Raw/project/Обычный.md", "# Док\n\n" + "Текст. " * 60 + "\n")
    _put(root, "Raw/laws/Закон.md", _fx("law_plain.md"))
    _put(root, "Raw/laws/Свободный закон.md", "---\ncited: false\n---\n\n" + _fx("law_plain.md"))
    _commit(root)
    got = json.loads(_py(root, "import build_plan as BP, json\nprint(json.dumps(sorted(p for _g, p, _s in BP.sources()), ensure_ascii=False))"))
    assert got == sorted(["Raw/contract/Техническое задание.md", "Raw/project/Обычный.md", "Raw/laws/Свободный закон.md"]), got
    groups = _py(root, "import build_plan as BP\nprint([g for g, _ in BP.GROUPS])")
    assert "Raw/laws" in groups, groups


@test
def test_closed_cards_take_no_new_knowledge_and_are_not_offered_as_targets(tmp: Path):
    """AC-CT-16.5: в единицу документа, решение и вопрос ничего не дописывается (`append_card`), и как `into` их разбору не предлагают."""
    AC, CP, R = _mod("aurora_common"), _mod("ctx_pack"), _mod("agent_runner")
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    unit_rel = f"{KB}/Documents/Закон-о-примерах-ст-1.md"
    unit_text = _read(root, unit_rel)
    assert AC.is_closed_card(AC.frontmatter(unit_text), unit_text)
    assert AC.is_closed_card({"type": "decision"}) and AC.is_closed_card({"type": "question"}) and AC.is_closed_card({}, "", "") is False
    assert AC.is_closed_card({}, "", "Decisions") and not AC.is_closed_card({"type": "concept"}), "у DR прежних схем тип даёт раздел"
    out = _py(root, "import build_plan as BP\n"
              f"rel = {unit_rel!r}\n"
              "old = open(rel, encoding='utf-8').read()\n"
              "rc = BP.append_card(rel, old, 'Новое знание о статье.', 'Sources/Confluence/Страница.md', True)\n"
              "print(rc, open(rel, encoding='utf-8').read() == old)")
    assert "карточка закрыта: добавлять в неё нечего" in out and out.split()[-2:] == ["1", "True"], out
    concept = "---\ntitle: \"Заявка\"\ntype: concept\nkind: knowledge\nstatus: knowledge\n---\n\nЗаявка — документ.\n"
    ranked = [(1.0, CP.Card(unit_rel, unit_text)), (0.9, CP.Card(f"{KB}/Concepts/Заявка.md", concept)),
              (0.8, CP.Card(f"{KB}/Decisions/DR-0001-x.md", "---\ntitle: \"DR\"\ntype: decision\nstatus: accepted\n---\n\nРешили.\n"))]
    into = [n for n, _s, _b in R._pick(ranked, 10, "", AC, appendable=True)]
    refs = [n for n, _s, _b in R._pick(ranked, 10, "", AC, appendable=False)]
    assert into == ["Заявка"] and len(refs) == 3, (into, refs)


@test
def test_closed_cards_are_left_out_of_thesis_queue_and_twins(tmp: Path):
    """Единица документа с `kind: knowledge` в шапке не попадает ни в очередь тезисов, ни в двойники: решает тип, а не вид."""
    root = _project(tmp, law="")
    _put(root, "Raw/laws/Закон дубль.md", _fx("law_duplicate.md"))
    _commit(root)
    _cite(root, "--apply", expect=0)
    for n in ("Закон-о-примерах-ст-1", "Закон-о-примерах-ст-5-2"):
        p = root / KB / "Documents" / f"{n}.md"
        p.write_bytes(p.read_bytes().replace(b"kind: document", b"kind: knowledge", 1))
    same = " ".join(f"термин{n} определяет порядок приёмки партии номер{n}" for n in range(1, 16))
    card(root, "Concepts/Приёмка заявки.md", same, status="draft", kind="knowledge")
    card(root, "Concepts/Приёмка заявки склада.md", same, status="draft", kind="knowledge")
    queue = json.loads(_py(root, "import agent_runner as R, agent_core as A, json, os\n"
                           "cfg = A.parse_config({'AURORA_AGENT_BACKEND_1_URL': 'u', 'AURORA_AGENT_BACKEND_1_MODEL': 'm'})\n"
                           "print(json.dumps([os.path.basename(x) for x in R.distill_queue(cfg, '.')], ensure_ascii=False))"))
    assert "Приёмка заявки.md" in queue and not any(n.startswith("Закон-о-примерах") for n in queue), queue
    twins = run("kb_twins.py", "--min", "0.2", cwd=root, expect_rc=0).stdout
    assert "Приёмка заявки склада" in twins, "контроль: двойники карточек знания находятся"
    assert "Закон-о-примерах" not in twins and "Сравнивались 2 карточек из 2" in twins, twins


@test
def test_kind_keeps_document_for_cited_units_even_after_a_human_correction(tmp: Path):
    """AC-CT-16.6: `kb:kind` оставляет `document`; единица с чужим видом возвращается к `document`; слово человека о виде эту карточку не меняет."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    _commit(root)
    assert run("kb_kind.py", "--apply", cwd=root, expect_rc=0).stdout.count("Проставить или пересчитать: 0") == 1
    p = root / KB / "Documents" / "Закон-о-примерах-ст-3.md"
    p.write_bytes(p.read_bytes().replace(b"kind: document", b"kind: dictionary", 1))
    _put(root, "Raw/corrections/2026-10-01-аналитик.md",
         '---\ncorrects: "[[Закон-о-примерах-ст-3]]"\nkind: knowledge\ncreated: 2026-10-01\nstatus: active\n---\n\n# Исправление\n\nТекст.\n')
    run("kb_kind.py", "--apply", cwd=root, expect_rc=0)
    assert "kind: document" in p.read_text(encoding="utf-8")
    dry = run("kb_kind.py", cwd=root, expect_rc=0).stdout
    assert "Проставить или пересчитать: 0" in dry, dry


@test
def test_repair_keeps_cited_names_and_bodies(tmp: Path):
    """AC-CT-16.7: полный ремонт (`kb_fix --all`) не меняет ни имён, ни тел единиц — даже с кодом в названии и латинскими буквами в русских словах."""
    root = _project(tmp, law="")
    text = _fx("law_chapters.md").replace("# Закон о примерах", "# US-3.4.2 Закон о примерах", 1)
    lat_a, lat_o, lat_c = "\u0061", "\u006f", "\u0063"       # латинские буквы, похожие на русские: гомоглифы
    text = text.replace("Заявка — документ", f"Заявк{lat_a} — документ", 1).replace("Поставщик — юридическое", f"П{lat_o}{lat_c}тавщик — юридическое", 1)
    assert f"Заявк{lat_a}" in text and f"П{lat_o}{lat_c}тавщик" in text, "подмена не сработала"
    _put(root, LAW, text)
    _commit(root)
    _cite(root, "--apply", expect=0)
    _commit(root)
    before = _snapshot(root, f"{KB}/Documents")
    assert len(before) == 6 and any("US-3-4-2" in k for k in before), list(before)
    run("kb_fix.py", "--all", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    assert _snapshot(root, f"{KB}/Documents") == before, "ремонт изменил единицы"
    run("kb_fix.py", "--homoglyphs", "--names", "--titles", "--links", "--tautologies", "--stub-text", "--frontmatter", "--sections",
        "--retire", "--merge-all", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    assert _snapshot(root, f"{KB}/Documents") == before
    second = next(v for k, v in before.items() if k.endswith("ст-2.md")).decode("utf-8")
    assert f"Заявк{lat_a} — документ" in second and f"П{lat_o}{lat_c}тавщик" in second, "латинская буква в русском слове — часть текста закона"


@test
def test_index_moc_and_folder_guide_know_the_documents_section(tmp: Path):
    """Раздел `Documents` есть в схеме папок, в справке, в оглавлении раздела, в группах карт и в заготовках установщика."""
    schema = [ln.strip() for ln in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()]
    assert "AuroraKnowledgeDB/Documents" in schema
    guides = (KIT / "templates" / "folder_guides.md").read_text(encoding="utf-8")
    g = guides.split("## AuroraKnowledgeDB/Documents", 1)[1].split("\n## ", 1)[0]
    assert "kb:cite" in g and "Движок" in g and "закон не правят" in g, g
    law_g = guides.split("## Raw/laws", 1)[1].split("\n## ", 1)[0]
    contract_g = guides.split("## Raw/contract", 1)[1].split("\n## ", 1)[0]
    assert "kb:cite" in law_g and "распознанное моделью цитировать нельзя" in law_g and "kb:cite" in contract_g
    inst = (KIT / "scripts" / "install_aurora.py").read_text(encoding="utf-8")
    assert "Documents/_index" in inst and "Questions/_index" in inst
    assert re.search(r'^Цитируемые документы \| type:cited-document; section:Documents \|', (KIT / "moc_groups.txt").read_text(encoding="utf-8"), re.M)
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    shutil.copy(KIT / "moc_groups.txt", root / ".aurora" / "moc_groups.txt")
    _commit(root)
    run("kb_index.py", "--apply", cwd=root, expect_rc=0)
    index = _read(root, f"{KB}/Documents/_index.md")
    assert "[[Закон-о-примерах-ст-2]]" in index and "ст. 2. Основные понятия" in index, index
    run("kb_moc.py", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    moc = _read(root, f"{KB}/MOC/Цитируемые-документы.md")
    assert moc.count("[[Закон-о-примерах-ст-") == 6 and "type: moc" in moc, moc[:400]
    run("kb_moc.py", "--by-source", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    assert (root / KB / "MOC" / "Документ--Закон-о-примерах.md").is_file()
    lint = run("kb_lint.py", cwd=root)
    assert "ошибок 0" in lint.stdout, lint.stdout[-600:]


@test
def test_reset_then_cite_restores_the_units(tmp: Path):
    """`kb:reset` сносит единицы вместе с базой (они машинные), а `kb:cite` возвращает те же имена и тексты: источник в `Raw/` цел."""
    root = _project(tmp)
    _cite(root, "--apply", expect=0)
    _commit(root)
    before = {k: re.sub(rb"(?m)^(source_synced|created|updated|trust_checked): .*$", b"", v) for k, v in _snapshot(root, f"{KB}/Documents").items()}
    run("kb_reset.py", "--apply", cwd=root, expect_rc=0)
    assert _docs(root) == [] and (root / LAW).is_file()
    _cite(root, "--apply", "--allow-dirty", expect=0)
    after = {k: re.sub(rb"(?m)^(source_synced|created|updated|trust_checked): .*$", b"", v) for k, v in _snapshot(root, f"{KB}/Documents").items()}
    assert after == before and len(after) == 6


@test
def test_cite_refuses_a_dirty_tree_without_allow_dirty_and_a_project_without_git(tmp: Path):
    """Как у соседних команд: по незакоммиченному дереву — только с `--allow-dirty`; проект без git — отказ (код 2)."""
    root = _project(tmp)
    card(root, "Concepts/Грязь.md", "Карточка, которую правят прямо сейчас.", status="knowledge", kind="knowledge")
    _commit(root)
    mine = root / KB / "Concepts" / "Грязь.md"
    mine.write_bytes(mine.read_bytes() + "\nПравка человека, ещё не в git.\n".encode("utf-8"))
    bad = _cite(root, "--apply")
    assert bad.returncode == 2 and "git-guard" in bad.stderr and _docs(root) == [], (bad.returncode, bad.stderr)
    _cite(root, "--apply", "--allow-dirty", expect=0)
    (tmp / "nogit").mkdir()
    plain = P70._project(tmp / "nogit", git=False)
    _put(plain, LAW, _fx("law_chapters.md"))
    no = _cite(plain, "--apply")
    assert no.returncode == 2 and "не под git" in no.stderr, (no.returncode, no.stderr)
    assert _cite(plain, "--apply", "--allow-dirty", expect=0).returncode == 0


@test
def test_cite_does_nothing_without_sources_and_when_the_type_is_switched_off(tmp: Path):
    """Нет цитируемых источников — код 0 и сообщение; тип выключен в настройках проекта — единицы не создаются, код 0 (ночной маршрут не встаёт)."""
    root = _project(tmp, law="")
    cp = _cite(root, "--apply", expect=0)
    assert "Цитируемых источников нет" in cp.stdout, cp.stdout
    _put(root, LAW, _fx("law_chapters.md"))
    _commit(root)
    C = _mod("card_types")
    C.save_overrides(str(root), {"cited-document": {"enabled": "false"}})
    _commit(root)
    off = _cite(root, "--apply", expect=0)
    assert "выключен в настройках проекта" in off.stderr and _docs(root) == [], (off.stdout, off.stderr)


@test
def test_cite_json_output_lists_sources_and_actions(tmp: Path):
    """`--json` отдаёт план машинно: источники, режим, число единиц, действия."""
    root = _project(tmp)
    data = json.loads(_cite(root, "--json", expect=1).stdout)
    s = data["sources"][0]
    assert s["path"] == LAW and s["mode"] == "law" and s["units"] == 6 and data["total"]["create"] == 6, data
    assert {i["action"] for i in s["items"]} == {"create"}


# ------------------------------------------------------------------ маршруты и регистрации

def _routes() -> dict:
    out, cur = {}, None
    for line in (KIT / "cockpit" / "scenarios.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(\w+)\]", line)
        if m:
            cur = out.setdefault(m.group(1), [])
        elif cur is not None and line.strip() and not line.startswith(("#", "-")):
            parts = [p.strip() for p in line.split("|")]
            if parts[0].startswith(("цикл:", "конец цикла")):
                cur.append((parts[0].split(":")[0], "", []))
                continue
            cur.append((parts[0], parts[1] if len(parts) > 1 else "", (parts[2] if len(parts) > 2 else "").split()))
    return out


@test
def test_routes_cut_cited_documents_before_the_build(_t):
    """Шаг `kb:cite` стоит в «Обновить», «Починить» и «Пересобрать» после зеркал записей и до первого `agent:build`; в «Обновить» — после перевода документов в текст, в «Пересобрать» — после сброса; шаги переведены."""
    routes = _routes()
    flags = ["--apply", "--allow-dirty"]
    for rid in ("update", "fix", "rebuild"):
        steps = routes[rid]
        at = [i for i, (c, _t2, f) in enumerate(steps) if c == "kb:cite"]
        assert len(at) == 1 and steps[at[0]][2] == flags, (rid, at)
        i = at[0]
        assert i > [c for c, _x, _f in steps].index("kb:record"), f"{rid}: разрез раньше зеркал записей"
        assert i > [c for c, _x, _f in steps].index("kb:ingest-office"), f"{rid}: разрез раньше перевода документов в текст"
        assert all(i < j for j, (c, _x, _f) in enumerate(steps) if c == "agent:build"), f"{rid}: разрез после agent:build"
    rb = routes["rebuild"]
    assert [c for c, _x, _f in rb].index("kb:cite") > [c for c, _x, _f in rb].index("kb:reset")
    en = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))["scenarios"]
    texts = {t for rid in routes for c, t, _f in routes[rid] if c == "kb:cite"}
    assert len(texts) == 3 and all(en.get(t) for t in texts), [t for t in texts if t not in en]


@test
def test_cite_command_is_registered_everywhere(_t):
    """Команда `kb:cite`: реестр, манифест движка, зависимости, навык, справочники команд на двух языках, английские описания, строка «Панель» в скрипте."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    m = re.search(r"^kb \| kb:cite \| — \| скрипт \| kb_cited\.py \| 1\.192\.0 \| .+$", reg, re.M)
    assert m, "нет строки kb:cite в commands.txt"
    manifest = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    for script in ("kb_cited.py", "legal_slicer.py"):
        assert re.search(rf"^scripts/{script}\s+=> \.aurora/scripts/{script}$", manifest, re.M), script
    assert '"kb:cite":' in (KIT / "command_deps.json").read_text(encoding="utf-8")
    assert "`kb:cite`" in (KIT / "skills" / "aurora-vault" / "SKILL.md").read_text(encoding="utf-8")
    for doc in ("docs/commands.md", "docs/en/commands.md"):
        assert "| `kb:cite` |" in (KIT / doc).read_text(encoding="utf-8"), doc
    en = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    assert en["commands"].get("kb:cite") and "Cited documents" in en["commands"]["kb:cite"]
    head = (KIT / "scripts" / "kb_cited.py").read_text(encoding="utf-8")
    assert "Панель: `kb:cite`" in head and "import legal_slicer as LS" in head


@test
def test_panel_editor_names_the_source_of_a_cited_unit_in_both_languages(tmp: Path):
    """Единица закона в редакторе панели закрыта, причина называет источник (а не «корректирующий артефакт», который к закону не применяется) и переведена."""
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    rel = f"{KB}/Documents/Закон-о-примерах-ст-2.md"
    text = ('---\ntitle: "Закон о примерах — ст. 2"\ntype: cited-document\nbuilt: machine\nsources:\n  - "Raw/laws/Закон о примерах.md"\n---\n\nТекст.\n')
    why = ck.why_readonly(rel, text)
    assert "Raw/laws/Закон о примерах.md" in why and "kb:cite" in why and "корректирующим артефактом" not in why, why
    bare = ck.why_readonly(rel)
    assert "kb:cite" in bare and "корректирующим артефактом" not in bare, bare
    for ru in (why, bare):
        en = ck.translate_message(ru, "en")
        assert en != ru and "a unit of a cited document" in en and "kb:cite" in en and not re.search("[а-я]", en.replace("Закон о примерах", "")), en
    assert not ck.why_readonly(f"{KB}/meta/releases.md"), "правила базы в meta по-прежнему правятся"
