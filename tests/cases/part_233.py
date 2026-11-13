"""Проверки движка: источники с пробелом и скобкой в имени (часть 233).

Живой случай на большом проекте: `kb:repair --links` снимал существующий источник
`Raw/Extracted/.../Форма (1.4).docx.md` как «не путь к файлу» — проверка
`re.search(r"\\s\\(", x)` срабатывала раньше `os.path.exists`. Карточка оставалась
без источника в шапке, `kb:build --reopen` возвращал файл в план, и каждый прогон
«Обновить базу» снова звал модель по тем же двадцати файлам.
"""
from pathlib import Path

from harness import card, card_srcs, make_project, run, test


@test
def test_repair_keeps_an_existing_source_with_a_space_and_parenthesis(tmp: Path):
    """Существующий путь `... (1).md` — источник, а не фраза: снимать его нельзя."""
    root = make_project(tmp)
    src = root / "Raw/Extracted/Форма (1.4).docx.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("---\nconverted_from: \"Форма (1.4).docx\"\n"
                   "source_hash: abc\n---\n\nтекст\n", encoding="utf-8")
    p = card(root, "Concepts/Форма-документа.md", "текст",
             status="knowledge",
             sources='["Raw/Extracted/Форма (1.4).docx.md"]')

    run("kb_fix.py", "--gone-sources", "--apply", "--allow-dirty", cwd=root)
    text = p.read_text(encoding="utf-8")
    assert "Raw/Extracted/Форма (1.4).docx.md" in card_srcs(text), \
        f"существующий источник снят как «не путь к файлу»:\n{text[:800]}"


@test
def test_repair_still_drops_a_missing_path_with_a_parenthesis(tmp: Path):
    """Несуществующий путь с ` (` по-прежнему не источник: его снимаем."""
    root = make_project(tmp)
    p = card(root, "Concepts/Заметка.md", "текст",
             status="knowledge",
             sources='["Raw/нет такого (1).md"]')

    run("kb_fix.py", "--gone-sources", "--apply", "--allow-dirty", cwd=root)
    text = p.read_text(encoding="utf-8")
    assert "Raw/нет такого (1).md" not in card_srcs(text), \
        f"несуществующий путь остался в источниках:\n{text[:800]}"
