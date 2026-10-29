"""Проверки движка Aurora, часть 117. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 4 «Записи в Raw/», документация (CT-24, часть 4): правила базы, правила на одной странице, таблицы
файлов проекта, вики, глоссарий, проводник по базе и справочник команд называют записи в `Raw/` и команду `kb:record` — на двух
языках одним смыслом. Документ, отставший от движка, хуже отсутствующего: слова, на которые опираются читатели, проверяются здесь.
"""
from __future__ import annotations

import re

from harness import KIT, test


def _read(rel: str) -> str:
    return (KIT / rel).read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


@test
def test_knowledge_rules_describe_records_and_kinds_in_both_languages(_t):
    """§3 говорит о видах (`kind`), а не о типах; §9 — о записях в `Raw/`, зеркалах и переносе; прежний вывод «нигде особенном» снят."""
    ru, en = _read("docs/knowledge-rules.md"), _read("docs/en/knowledge-rules.md")
    assert "## 3. Три вида карточек" in ru and "## 3. Three kinds of cards" in en and "Три типа карточек" not in ru
    assert "Выбор человека сильнее правила" not in ru and "Слово человека сильнее правила" in ru, "утверждение о `kind` устарело"
    for text, nine, ten in ((ru, "## 9. Где живёт написанное руками", "## 10. "), (en, "## 9. Where hand-written things live", "## 10. ")):
        sec = _section(text, nine, ten)
        for word in ("Raw/decisions/", "Raw/questions/", "Raw/reference/", "Raw/corrections/", "kb:record --mirror", "kb:record --migrate",
                     "kb:record --new", "built: machine", "`sources`", "31", "451", "kb:reset"):
            assert word in sec, f"{nine}: нет «{word}»"
        assert "Нигде особенном" not in sec and "Nowhere special" not in sec
        assert re.search(r"\| *(решение|decision) *\|", sec) and re.search(r"\| *(вопрос|question) *\|", sec), "таблица записей"
        assert "accepted" in sec and "draft" in sec and ("НЕ ФАКТ" in sec or "NOT A FACT" in sec)


@test
def test_one_page_summary_names_records(_t):
    for rel, head in (("docs/knowledge-rules-tldr.md", "## Записи человека живут в Raw"), ("docs/en/knowledge-rules-tldr.md", "## A human's records live in Raw")):
        text = _read(rel)
        assert head in text and "kb:record --mirror" in text and "Raw/decisions/" in text, rel
    assert "`Raw/decisions|questions|reference`" in _read("docs/knowledge-rules-tldr.md")


@test
def test_project_file_tables_list_the_record_folders(_t):
    """Таблицы «где что хранится» и установки называют три папки записей; схема папок и справки их знают."""
    for rel in ("docs/INSTALL.md", "docs/en/INSTALL.md"):
        assert "Raw/{laws,contract,customer,project,meetings,examples,corrections,decisions,questions,reference}/" in _read(rel), rel
    for rel in ("docs/architecture.md", "docs/en/architecture.md"):
        text = _read(rel)
        row = next(ln for ln in text.splitlines() if ln.startswith("| `Raw/decisions/`"))
        assert "kb:record" in row and "Raw/questions/" in row and "Raw/reference/" in row, rel
    structure = _read("structure_dirs.txt").splitlines()
    for folder in ("Raw/decisions", "Raw/questions", "Raw/reference"):
        assert folder in structure and f"## {folder}" in _read("templates/folder_guides.md"), folder


@test
def test_wiki_and_glossary_name_records_and_mirrors(_t):
    """Вики: карточка (раздел о записях), цикл (шаг зеркал в «Обновить» и «Починить»), глоссарий (запись, зеркало записи); пары ru/en."""
    for rel, head in (("wiki/The-Card.md", "## Records: a human's own cards live in `Raw/`"),
                      ("wiki/ru/The-Card.md", "## Записи: собственные карточки человека живут в `Raw/`")):
        text = _read(rel)
        assert head in text and "kb:record --mirror" in text and "kb:record --migrate" in text and "Raw/reference/" in text, rel
        assert text.index(head) < text.index("## Five statuses" if "ru/" not in rel else "## Пять статусов"), "раздел о записях стоит перед статусами"
    for rel in ("wiki/The-Cycle.md", "wiki/ru/The-Cycle.md"):
        text = _read(rel)
        assert text.count("`Raw/`") >= 1 and ("mirrors of the records" in text or "зеркала записей" in text), rel
    en, ru = _read("wiki/Glossary.md"), _read("wiki/ru/Glossary.md")
    for term in ("| **Record** |", "| **Mirror of a record** |"):
        assert term in en, term
    for term in ("| **Запись** |", "| **Зеркало записи** |"):
        assert term in ru, term
    assert "Immutable evidence" not in en and "Неизменяемые свидетельства" not in ru, "Raw/ — слой человека и заказчика, а не только доказательства"


@test
def test_reading_guide_and_command_reference_know_records(_t):
    reading = _read("templates/meta/READING.md")
    assert "`Decisions` (зеркала решений из `Raw/decisions/`)" in reading and "`kb:record`" in reading
    assert "журнал решений" not in reading.split("## 2.")[0], "в проводнике Decisions снова назван журналом, а не зеркалом"
    for rel, cell in (("docs/commands.md", "записи человека в `Raw/`"), ("docs/en/commands.md", "Human records in `Raw/`")):
        row = next(ln for ln in _read(rel).splitlines() if ln.startswith("| `kb:record`"))
        assert cell in row and ("`kb_record.py`" in row or rel.startswith("docs/en/")), rel
    for rel in ("docs/card-templates.md", "docs/en/card-templates.md"):
        text = _read(rel)
        assert "kb:record" in text and "Raw/decisions|questions|reference" in text, rel
        assert text.count("\n") <= 400, f"{rel}: длиннее 400 строк"


@test
def test_lifecycle_shows_the_mirror_step_and_the_raw_agreement(_t):
    for rel in ("docs/lifecycle.md", "docs/en/lifecycle.md"):
        text = _read(rel)
        assert "sync:audit · kb:record --mirror · kb:ingest-office" in text, f"{rel}: на схеме «Обновить» нет шага зеркал"
        assert "Raw/decisions\\|questions\\|reference" in text and "kb:record" in text, f"{rel}: про записи в Raw ничего нет"
        assert "immutable by agreement" not in text and "неизменяем по договорённости" not in text, f"{rel}: прежнее «Raw неизменяем»"
