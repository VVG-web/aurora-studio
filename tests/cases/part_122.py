"""Проверки движка Aurora, часть 122. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 5 «Цитируемые документы», документация (CT-24, часть 5): правила базы, правила на одной странице,
руководство по шаблонам, таблицы файлов проекта, схема цикла, вики, глоссарий, проводник по базе, навык и справочник команд
называют раздел `Documents`, команду `kb:cite` и цитаты в ответах — на двух языках одним смыслом. Документ, отставший от
движка, хуже отсутствующего: слова, на которые опираются читатели, проверяются здесь.
"""
from __future__ import annotations

from harness import KIT, test


def _read(rel: str) -> str:
    return (KIT / rel).read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


@test
def test_knowledge_rules_describe_cited_documents_in_both_languages(_t):
    """В правилах базы (§4) есть раздел о цитируемых документах: что считается цитируемым, как режется, что в карточке, как цитирует «Спросить», пределы."""
    ru, en = _read("docs/knowledge-rules.md"), _read("docs/en/knowledge-rules.md")
    for text, head, tail, words in (
        (ru, "### Цитируемые документы: закон режется по статьям", "## 5. Устройство карточки",
         ("kb:cite", "Raw/laws/", "cited: true", "cited: false", "Documents/", "`cite`", "«ёлочках»", "Цитаты: проверено N, не найдено M",
          "converter: ocr", "_archive/", "6000", "200 знаков", "`cited-document`", "без модели")),
        (en, "### Cited documents: a law is cut by articles", "## 5. The card's structure",
         ("kb:cite", "Raw/laws/", "cited: true", "cited: false", "Documents/", "`cite`", "guillemets", "Quotes: checked N, not found M",
          "converter: ocr", "_archive/", "6000", "200", "`cited-document`", "without a model"))):
        sec = _section(text, head, tail)
        for word in words:
            assert word in sec, f"{head}: нет «{word}»"
        assert text.index(head) > text.index("### " + ("Карточка — сущность" if text is ru else "A card is an entity")), "раздел стоит в §4 после правила о сущности"


@test
def test_one_page_summary_names_cited_documents(_t):
    for rel, head in (("docs/knowledge-rules-tldr.md", "## Закон и контракт режутся по статьям"),
                      ("docs/en/knowledge-rules-tldr.md", "## A law and a contract are cut by articles")):
        text = _read(rel)
        assert head in text and "kb:cite" in text and "Documents/" in text and "cited-document" in text and "cited: true" in text, rel


@test
def test_card_templates_guide_marks_cited_documents_as_working(_t):
    """Руководство по шаблонам: раздел `Documents` есть, V-21 работает, «пока нет» про цитируемые документы снято; длина в пределах 400 строк."""
    for rel, works, now_not in (("docs/card-templates.md", "| работает |", "цитируемые документы (раздел `Documents`, `kb:cite`)"),
                                ("docs/en/card-templates.md", "| works |", "cited documents (the `Documents` section, `kb:cite`)")):
        text = _read(rel)
        row = next(ln for ln in text.splitlines() if ln.startswith("| V-21 |"))
        assert row.rstrip().endswith(works) and "corrected_by" in row, rel
        assert "kb:cite" in text and "`cited-document`" in text and now_not not in text, rel
        assert "ещё не заведён" not in text and "is not yet in the project's folder scheme" not in text, rel
        assert text.count("\n") <= 400, f"{rel}: длиннее 400 строк"


@test
def test_project_file_tables_and_install_list_the_documents_folder(_t):
    for rel in ("docs/INSTALL.md", "docs/en/INSTALL.md"):
        assert "Questions, Decisions, Documents, MOC," in _read(rel), rel
    for rel in ("docs/architecture.md", "docs/en/architecture.md"):
        row = next(ln for ln in _read(rel).splitlines() if ln.startswith("| `AuroraKnowledgeDB/Documents/`"))
        assert "kb:cite" in row and "Raw/laws/" in row and "cited: true" in row, rel


@test
def test_lifecycle_and_cycle_wiki_show_the_cite_step(_t):
    """Схема «Обновить базу» называет `kb:cite` в подготовке; кнопки «Обновить» и «Починить» в вики говорят о единицах законов и контрактов."""
    for rel in ("docs/lifecycle.md", "docs/en/lifecycle.md"):
        assert "sync:audit · kb:record --mirror · kb:ingest-office · kb:cite" in _read(rel), rel
    en, ru = _read("wiki/The-Cycle.md"), _read("wiki/ru/The-Cycle.md")
    assert en.count("`kb:cite`") >= 1 and "units of laws and contracts" in en
    assert ru.count("`kb:cite`") >= 1 and "единицы законов и контрактов" in ru


@test
def test_wiki_card_page_and_glossary_name_cited_documents(_t):
    for rel, head, after in (("wiki/The-Card.md", "## Cited documents: a law cut into articles", "## Five statuses"),
                             ("wiki/ru/The-Card.md", "## Цитируемые документы: закон, нарезанный по статьям", "## Пять статусов")):
        text = _read(rel)
        assert head in text and text.index(head) < text.index(after), rel
        sec = _section(text, head, after)
        for word in ("kb:cite", "Documents/", "cited: true", "cited: false", "cite", "converter: ocr"):
            assert word in sec, f"{rel}: нет «{word}»"
    assert "| **Cited document** |" in _read("wiki/Glossary.md") and "| **Цитируемый документ** |" in _read("wiki/ru/Glossary.md")


@test
def test_reading_guide_conventions_and_skill_know_the_documents_section(_t):
    """Проводник, соглашения проекта и навык называют раздел `Documents` и команду `kb:cite`; справки по сборке и шапке карточки — тоже."""
    assert "`Documents` (единицы законов и контрактов" in _read("templates/meta/READING.md")
    assert "`<документ>-ст-N`" in _read("templates/meta/conventions.md")
    skill = _read("skills/aurora-vault/SKILL.md")
    row = next(ln for ln in skill.splitlines() if ln.startswith("| `AuroraKnowledgeDB/Documents/` |"))
    assert "kb:cite" in row and "Raw/laws/" in row
    cmd = next(ln for ln in skill.splitlines() if ln.startswith("| `kb:cite` |"))
    assert cmd.rstrip().endswith("`references/build.md` |") and "converter: ocr" in cmd
    assert "### Цитируемые документы (не разбираются моделью)" in _read("skills/aurora-vault/references/build.md")
    fm = _read("skills/aurora-vault/references/frontmatter.md")
    assert "## Cited-document extras (type: cited-document)" in fm and "unit_kind" in fm and "cite:" in fm


@test
def test_wiki_sources_and_ask_pages_name_cited_documents(_t):
    """Страницы об источниках и о «Спросить» говорят про разрез законов и про проверку цитат на двух языках."""
    for rel in ("wiki/Sources-and-Mirrors.md", "wiki/ru/Sources-and-Mirrors.md"):
        text = _read(rel)
        assert "kb:cite" in text and "cited: true" in text and "Raw/laws/" in text, rel
    en, ru = _read("wiki/Asking-the-Base.md"), _read("wiki/ru/Asking-the-Base.md")
    assert "Quotes: checked N, not found M" in en and "verbatim text of" in en and "`cited-document`" in en
    assert "Цитаты: проверено N, не найдено M" in ru and "дословный текст документа" in ru and "`cited-document`" in ru
    maint = _read("skills/aurora-vault/references/maintenance.md")
    assert "`kb:cite`" in maint and "Documents/" in maint, "в описании сброса базы нет возврата единиц документов"
