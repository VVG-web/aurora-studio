"""Проверки движка Aurora, часть 1 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from harness import (  # noqa: F401
    KIT,
    SCRIPTS,
    card,
    card_srcs,
    count_cards,
    make_project,
    panel_sources,
    run,
    set_home,
    term_regex,
    test,
    why,
)


@test
def test_repair_fixes_links_and_keeps_every_card(tmp: Path):
    root = make_project(tmp)
    card(root, "Glossary/Заявка.md", "см. [[Заявка Статус: Черновик]] и [[AИС-Налог-3]]")
    card(root, "Statuses/Заявка-Статус-Черновик.md")
    card(root, "Systems/АИС-Налог-3.md")
    before = count_cards(root)
    run("kb_fix.py", "--all", "--apply", cwd=root)
    assert count_cards(root) == before, f"карточек было {before}, стало {count_cards(root)}"
    text = (root / "AuroraKnowledgeDB/Glossary/Заявка.md").read_text(encoding="utf-8")
    assert "[[Заявка-Статус-Черновик]]" in text, "ссылка по заголовку не нормализована"
    assert "[[АИС-Налог-3]]" in text, "гомоглиф в ссылке не починен"
    lint = run("kb_lint.py", "--summary", cwd=root)
    assert "ошибок 0" in lint.stdout, f"после ремонта остались ошибки: {lint.stdout}"


@test
def test_repair_never_overwrites_on_name_collision(tmp: Path):
    """Регрессия: два файла метились в одно каноничное имя и один молча затирался."""
    root = make_project(tmp)
    card(root, "Processes/АLG-095-Удаление.md", "латинские L,G — имя чинится в ALG")
    card(root, "Processes/ALG-095-Удаление.md", "уже правильное имя")
    before = count_cards(root)
    cp = run("kb_fix.py", "--homoglyphs", "--apply", cwd=root)
    assert count_cards(root) == before, "карточка потеряна при коллизии имён"
    assert (root / "AuroraKnowledgeDB/Processes/ALG-095-Удаление.md").read_text(
        encoding="utf-8").count("уже правильное имя") == 1, "существующий файл перезаписан"
    assert "двойник" in cp.stdout, "коллизия не отражена в отчёте"


@test
def test_repair_is_idempotent(tmp: Path):
    root = make_project(tmp)
    card(root, "Glossary/ВAЛЮТA.md", "[[Несуществующая-карточка]]")
    run("kb_fix.py", "--all", "--apply", cwd=root)
    snapshot = {p.name: p.read_text(encoding="utf-8")
                for p in (root / "AuroraKnowledgeDB").rglob("*.md")}
    run("kb_fix.py", "--all", "--apply", cwd=root)
    again = {p.name: p.read_text(encoding="utf-8")
             for p in (root / "AuroraKnowledgeDB").rglob("*.md")}
    assert snapshot == again, "повторный прогон изменил файлы (не идемпотентен)"


@test
def test_repair_merge_archives_donor_and_rewrites_links(tmp: Path):
    root = make_project(tmp)
    card(root, "Concepts/Карта-А.md", "тело победителя", status="imported")
    card(root, "Concepts/Kарта-А.md", "тело донора", status="imported")
    card(root, "Concepts/Ссылающаяся.md", "[[Kарта-А]]")
    run("kb_fix.py", "--merge", "Карта-А", "Kарта-А", "--apply", cwd=root)
    assert (root / "AuroraKnowledgeDB/_archive/Kарта-А.md").exists(), "донор не уехал в _archive"
    donor = (root / "AuroraKnowledgeDB/_archive/Kарта-А.md").read_text(encoding="utf-8")
    assert "status: deprecated" in donor and "superseded_by" in donor, "донор без deprecated/superseded_by"
    keep = (root / "AuroraKnowledgeDB/Concepts/Карта-А.md").read_text(encoding="utf-8")
    assert "тело донора" in keep, "тело донора не перенесено"
    ref = (root / "AuroraKnowledgeDB/Concepts/Ссылающаяся.md").read_text(encoding="utf-8")
    assert "[[Карта-А]]" in ref, "входящая ссылка не переписана"


@test
def test_git_guard_blocks_dirty_tree(tmp: Path):
    root = make_project(tmp, git=True)
    card(root, "Glossary/Термин.md", "[[Битая]]")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "cards"], cwd=str(root), check=True)
    (root / "AuroraKnowledgeDB/Glossary/Термин.md").write_text("изменено вручную", encoding="utf-8")
    cp = run("kb_fix.py", "--all", "--apply", cwd=root, expect_rc=2)
    assert "git-guard" in cp.stderr, "нет объяснения, почему остановились"
    run("kb_fix.py", "--all", "--apply", "--allow-dirty", cwd=root)


@test
def test_queue_ranks_by_value_and_skips_useless(tmp: Path):
    root = make_project(tmp)
    card(root, "Glossary/Важный.md", status="imported")
    card(root, "Concepts/Ненужный.md", status="imported")
    for i in range(3):
        card(root, f"Concepts/Ссылка{i}.md", "[[Важный]]")
    cp = run("aurora_stats.py", "--queue", "--limit", "10", cwd=root)
    assert "Важный" in cp.stdout, "востребованная карточка не попала в очередь"
    body = cp.stdout.split("## Пакетами")[0]
    assert "Ненужный" not in body, "карточка с нулевой ценностью попала в очередь"


@test
def test_stats_counts_questions_and_acceptance(tmp: Path):
    root = make_project(tmp)
    card(root, "Requirements/REQ-001-Тест.md", type="requirement", req_id="REQ-001",
         req_status="implemented", status="imported")
    card(root, "Questions/Q-001-Вопрос.md", type="question", q_id="Q-001", q_status="asked",
         due="2000-01-01", blocks='["[[REQ-001]]"]', status="draft")
    cp = run("aurora_stats.py", cwd=root)
    assert "открытых (open/asked): **1**" in cp.stdout, "открытый вопрос не посчитан"
    assert "просроченных (`due` в прошлом): **1**" in cp.stdout, "просроченный вопрос не посчитан"
    assert "без отчёта приёмки" in cp.stdout, "не найден implemented без приёмки"
    (root / "Artifacts/acceptance/2026-01-01_acceptance_ПМИ.md").write_text(
        "---\ntype: acceptance\ncovers: [REQ-001]\nverdict: passed\nheld: 2026-01-01\n---\n",
        encoding="utf-8")
    cp2 = run("aurora_stats.py", cwd=root)
    assert "без отчёта приёмки" not in cp2.stdout, "приёмка не закрыла разрыв"


@test
def test_trust_share_counts_only_what_can_be_checked(tmp: Path):
    """Доля доверенного — среди карточек со знанием: без заготовок и служебных файлов.

    В знаменателе лежали пустышки и `README.md` базы. На живом проекте 632 пустышки из 1657
    карточек держали долю ниже 40 % при любом качестве знания, а потолок при полном доверии
    был 48 %: показатель, который не может дойти до ста, ничего не сообщает.
    """
    root = make_project(tmp)
    card(root, "Concepts/Знание.md", status="knowledge", kind="knowledge",
         body="Проверенное знание о предмете.")
    card(root, "Concepts/Черновик.md", status="draft", kind="knowledge",
         body="Непроверенное знание о предмете.")

    def share():
        cp = run("aurora_stats.py", "--json", cwd=root)
        s = json.loads(cp.stdout[cp.stdout.index("{"):cp.stdout.rindex("}") + 1])
        return s["pct_verified"], s["trust_total"], s["stubs"]

    pct, base, stubs = share()
    for i in range(3):
        card(root, f"Concepts/Пустышка-{i}.md", status="placeholder", tags="[заготовка]",
             body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._")
    (root / "AuroraKnowledgeDB/README.md").write_text("# База знаний\n\nОписание базы.\n",
                                                      encoding="utf-8")
    pct2, base2, stubs2 = share()
    assert stubs2 == stubs + 3, "заготовки перестали считаться — счётчик пустышек сломан"
    assert base2 == base, f"заготовки или служебный файл попали в знаменатель доли: {base} → {base2}"
    assert pct2 == pct, f"доля доверенного поехала от пустышек и README: {pct} → {pct2}"


@test
def test_lint_validates_question_cards(tmp: Path):
    root = make_project(tmp)
    card(root, "Questions/Q-002-Плохой.md", type="question", q_id="Q-002", q_status="answered")
    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "answer_source" in cp.stdout, "ответ без источника не пойман"


@test
def test_confluence_conversion_is_deterministic_and_clean(tmp: Path):
    """Конвертация storage-format: стабильна между прогонами и чистит макросы."""
    sys.path.insert(0, str(SCRIPTS))
    import confluence_export as ce

    storage = (
        '<p>Текст со <ac:structured-macro ac:name="status">'
        '<ac:parameter ac:name="title">Готово</ac:parameter></ac:structured-macro> внутри.</p>'
        '<ac:structured-macro ac:name="toc"/>'
        '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter>'
        '<ac:plain-text-body>print(1)</ac:plain-text-body></ac:structured-macro>'
        '<table><tr><th>А</th><th>Б</th></tr><tr><td>1</td><td>2</td></tr></table>'
        '<p><ac:link><ri:page ri:content-title="Другая страница"/>'
        '<ac:plain-text-link-body>ссылка</ac:plain-text-link-body></ac:link></p>'
        '<p style="color:red" class="x" data-id="7">Атрибуты выброшены</p>'
    )
    first = ce.to_markdown(storage, "https://confluence.example.com", "SP")
    second = ce.to_markdown(storage, "https://confluence.example.com", "SP")
    assert first == second, "конвертация недетерминирована"
    assert "[Статус: Готово]" in first, "макрос статуса не превращён в текст"
    assert "toc" not in first.lower(), "макрос оглавления не вырезан"
    assert "```python" in first, "код не стал fenced-блоком"
    assert "| А | Б |" in first and "|---|---|" in first, "таблица не стала markdown-таблицей"
    assert "[ссылка](https://confluence.example.com/display/SP/" in first, "ac:link не развёрнут"
    assert "style=" not in first and "class=" not in first, "служебные атрибуты просочились"

    assert ce.safe_name('RU.PRJ "Курс" / КБК') == "RU.PRJ_Курс_КБК"
    assert ce.safe_name("", "12345") == "page_12345"

    fm = ce.render_front_matter({"id": "1", "title": "Т", "space": "SP", "version": 2,
                                 "updated": "2026-01-01", "url": "u", "breadcrumbs": "a / б",
                                 "hash": "abc"})
    assert "export" not in fm and "converted:" not in fm, \
        "в шапке есть дата экспорта — она даст ложный дифф на каждом прогоне"


@test
def test_nested_sync_root_is_skipped(tmp: Path):
    """Корень внутри другого корня выгрузился бы вторым файлом в корень зеркала."""
    sys.path.insert(0, str(SCRIPTS))
    import confluence_export as ce

    class FakeApi:
        tree = {"100": [], "200": ["100"], "300": []}   # 200 — потомок 100

        def page(self, pid):
            return {"ancestors": [{"id": a} for a in self.tree[str(pid)]]}

    keep, dropped = ce.drop_nested_roots(FakeApi(), ["100", "200", "300"])
    assert keep == ["100", "300"], f"оставлены не те корни: {keep}"
    assert dropped == [("200", "100")], f"вложенный корень не отброшен: {dropped}"


@test
def test_remap_repoints_sources_after_mirror_move(tmp: Path):
    """Переезд зеркала: source: карточек должен пойти за страницей по page_id."""
    root = make_project(tmp)
    mirror = root / "Sources/Confluence"

    # старое зеркало в формате прежнего LLM-синка
    (mirror / "Старый_путь").mkdir(parents=True, exist_ok=True)
    (mirror / "Старый_путь/Страница.md").write_text(
        "# Страница\n\n- **ID:** 123456\n", encoding="utf-8")
    (mirror / "Ушедшая.md").write_text("# Ушедшая\n\n- **ID:** 999999\n", encoding="utf-8")
    card(root, "Concepts/Знание.md", "тело", source='"Sources/Confluence/Старый_путь/Страница.md"')
    card(root, "Concepts/Осиротевшее.md", "тело", source='"Sources/Confluence/Ушедшая.md"')

    run("kb_remap.py", "--snapshot", cwd=root)
    assert (root / "AuroraKnowledgeDB/meta/mirror_snapshot.json").is_file(), "снимок не сохранён"

    # зеркало пересобрано: путь другой, страница 999999 из дерева исчезла
    shutil.rmtree(mirror)
    (mirror / "Новый/Путь").mkdir(parents=True, exist_ok=True)
    (mirror / "Новый/Путь/Страница.md").write_text(
        "---\npage_id: 123456\n---\n\n# Страница\n", encoding="utf-8")
    (mirror / "sync_state.md").write_text(
        "**Sync Date:** 2026-07-27\n\n| # | Page ID | Title | Local Path | Status |\n"
        "|---|---|---|---|---|\n"
        "| 1 | 123456 | Страница | Новый/Путь/Страница.md | SYNCED |\n", encoding="utf-8")

    cp = run("kb_remap.py", "--apply", cwd=root)
    moved = (root / "AuroraKnowledgeDB/Concepts/Знание.md").read_text(encoding="utf-8")
    assert "Sources/Confluence/Новый/Путь/Страница.md" in card_srcs(moved), \
        f"источник не перенацелен:\n{moved}"
    orphan = (root / "AuroraKnowledgeDB/Concepts/Осиротевшее.md").read_text(encoding="utf-8")
    assert "Ушедшая.md" in orphan, "источник исчезнувшей страницы не должен подменяться наугад"
    assert "не сопоставлено: 1" in cp.stdout, f"пропавшая страница не попала в отчёт:\n{cp.stdout}"


@test
def test_lint_finds_artifacts_but_not_domain_codes(tmp: Path):
    """US/AC/Epic в знаниях — находка; ALG-095 и REQ — законные жители базы."""
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "T"\n  slug: "T"\natlassian:\n  jira:\n    project_key: "PROJ"\n',
        encoding="utf-8")
    card(root, "Concepts/US-3.1.11-Приём-корректировки.md", "", type="concept")
    card(root, "Concepts/AC-4.2.12-Панель-информация.md", "", type="concept")
    card(root, "Concepts/PROJ-1234-Задача.md", "", type="concept")
    card(root, "Processes/ALG-095-Удаление-спецификации.md", "", type="process")
    card(root, "Requirements/REQ-042-Обмен-с-смежная система.md", "", type="requirement")
    card(root, "Glossary/Заявка.md", "")                      # без type — механическая починка

    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    arts = [l for l in cp.stdout.splitlines() if "артефакт в знаниях" in l]
    assert not any("ALG-095" in l for l in arts), "код алгоритма ошибочно принят за артефакт"
    assert not any("REQ-042" in l for l in arts), "требование ошибочно принято за артефакт"
    for name in ("US-3.1.11", "AC-4.2.12", "PROJ-1234"):
        assert any(name in l for l in arts), f"артефакт {name} не найден"
    assert "артефакты, попавшие в базу знаний: 3" in cp.stdout, cp.stdout[:800]

    run("kb_fix.py", "--frontmatter", "--apply", cwd=root)
    glossary = (root / "AuroraKnowledgeDB/Glossary/Заявка.md").read_text(encoding="utf-8")
    assert "type: glossary" in glossary, "type не проставлен по разделу"


@test
def test_ctx_pack_filters_by_status_and_logs_usage(tmp: Path):
    """Пак: только доверенное в generate, шапки доверия, запись в usage.log."""
    root = make_project(tmp)
    card(root, "Glossary/Заявка.md", "Документ оплаты. См. [[Проверка-Заявка]]",
         status="verified", owner='"@vadim"', verified="2026-01-01", review_by="2030-01-01")
    card(root, "Processes/Проверка-Заявка.md", "Проверка на границе Заявка",
         status="verified", owner='"@vadim"', verified="2026-01-01", review_by="2030-01-01")
    card(root, "Concepts/Черновик-Заявка.md", "сырой набросок про Заявка", status="draft")
    for i in range(9):
        card(root, f"Glossary/Термин{i}.md", "текст", status="verified",
             owner='"@x"', verified="2026-01-01", review_by="2030-01-01")

    cp = run("ctx_pack.py", "Заявка", cwd=root)
    # Шапка теперь говорит о классе источника и основании, а не о том, кто и когда
    # поставил отметку: доверие больше не чьё-то решение.
    assert "[verified | доверенный источник" in cp.stdout, "нет шапки доверия"
    assert "Черновик-Заявка" not in cp.stdout, "draft попал в generate-пак"
    assert "Проверка-Заявка" in cp.stdout, "связанная карточка не подтянулась переходом"
    usage = (root / "AuroraKnowledgeDB/meta/usage.log").read_text(encoding="utf-8")
    assert "Заявка" in usage, "употребление не записано в usage.log"

    cp2 = run("ctx_pack.py", "Заявка", "--mode", "evaluate", "--no-log", cwd=root)
    assert "Черновик-Заявка" in cp2.stdout, "в evaluate черновик обязан быть"
    assert "НЕ ФАКТ" in cp2.stdout, "у черновика нет предупреждающей шапки"



@test
def test_supersede_moves_to_archive_and_relinks(tmp: Path):
    root = make_project(tmp)
    card(root, "Systems/Старая-шина.md", "описание", status="verified",
         owner='"@v"', verified="2026-01-01", review_by="2030-01-01")
    card(root, "Systems/Новая-шина.md", "описание", status="verified",
         owner='"@v"', verified="2026-01-01", review_by="2030-01-01")
    card(root, "Processes/Обмен.md", "идёт через [[Старая-шина]]", status="verified",
         owner='"@v"', verified="2026-01-01", review_by="2030-01-01")

    run("kb_supersede.py", "Старая-шина", "Новая-шина", "--reason", "заменена на Kafka",
        "--apply", cwd=root)
    old = (root / "AuroraKnowledgeDB/_archive/Старая-шина.md").read_text(encoding="utf-8")
    assert "status: deprecated" in old and "superseded_by" in old, "донор не помечен"
    assert "заменена на Kafka" in old, "причина не записана в историю"
    new = (root / "AuroraKnowledgeDB/Systems/Новая-шина.md").read_text(encoding="utf-8")
    assert "supersedes" in new, "у преемника нет supersedes"
    ref = (root / "AuroraKnowledgeDB/Processes/Обмен.md").read_text(encoding="utf-8")
    assert "[[Новая-шина]]" in ref, "входящая ссылка не переписана"

    rc = run("kb_supersede.py", "Новая-шина", "Нет-такой", cwd=root, expect_rc=1)
    assert "не найден преемник" in rc.stderr, "замена «в никуда» должна блокироваться"


@test
def test_impact_finds_released_documents(tmp: Path):
    root = make_project(tmp)
    card(root, "Systems/Шина.md", "описание", status="verified",
         owner='"@v"', verified="2026-01-01", review_by="2030-01-01")
    (root / "Deliverables/released").mkdir(parents=True, exist_ok=True)
    (root / "Deliverables/released/ОПЗ_v1_2026-01-01.md").write_text(
        '---\ntype: deliverable\nbased_on: ["[[Шина]]"]\n---\n\n# ОПЗ\n', encoding="utf-8")

    cp = run("kb_trace.py", "--impact", "Шина", cwd=root)
    assert "Сданные заказчику документы" in cp.stdout, "сданный документ не выделен"
    assert "ОПЗ_v1_2026-01-01" in cp.stdout, "документ не найден по based_on"

    cp2 = run("kb_trace.py", "--explain",
              "Deliverables/released/ОПЗ_v1_2026-01-01.md", cwd=root)
    assert "Шина" in cp2.stdout and "verified" in cp2.stdout, "основания документа не показаны"


@test
def test_jira_markup_converts_deterministically(tmp: Path):
    """Вики-разметка Jira → markdown: порядок правил и стабильность."""
    sys.path.insert(0, str(SCRIPTS))
    import jira_export as je

    src = ("h2. Заголовок\n # Первый\n # Второй\n* маркер\n"
           "Текст {{кода}}, _курсив_, [ссылка|https://example.ru]\n"
           "{code:sql}SELECT 1{code}\n||Поле||Значение||\n|Статус|Готово|\n"
           "{color:red}важно{color}")
    out = je.jira_to_md(src)
    assert out == je.jira_to_md(src), "конвертация недетерминирована"
    assert out.startswith("## Заголовок"), f"заголовок не преобразован:\n{out}"
    assert "1. Первый" in out and "- маркер" in out, "списки не преобразованы"
    assert "```sql\nSELECT 1\n```" in out, "код не преобразован"
    assert "| Поле | Значение |" in out and "| Статус | Готово |" in out, "таблица не преобразована"
    assert "[ссылка](https://example.ru)" in out and "`кода`" in out, "ссылка/моноширинный"
    assert "{color" not in out and "важно" in out, "макрос не убран"

    issue = {"key": "PROJ-1", "fields": {"summary": "Тест", "issuetype": {"name": "Задача"},
                                         "status": {"name": "Готово"}, "updated": "2026-01-01T10:00:00",
                                         "created": "2026-01-01T09:00:00", "description": "h1. Раз"}}
    md = je.render(issue, "https://jira.example.com", "", [])
    assert 'key: "PROJ-1"' in md and "# PROJ-1: Тест" in md, "рендер задачи сломан"
    assert "export" not in md.split("---")[1], "в шапке есть дата экспорта — будет ложный дифф"


@test
def test_sync_diff_finds_changed_sources(tmp: Path):
    """Дрейф: источник изменился после сверки — карточка попадает в отчёт."""
    root = make_project(tmp)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    src = root / "Raw/project/док.md"
    src.write_text("исходный текст", encoding="utf-8")
    card(root, "Concepts/Знание.md", "тело", status="verified", owner='"@v"',
         verified="2026-01-01", review_by="2030-01-01", source='"Raw/project/док.md"')

    cp = run("sync_audit.py", "--drift", "--stamp", "--apply", cwd=root)
    assert "Проставлено: 1" in cp.stdout, f"хеш источника не зафиксирован:\n{cp.stdout}"
    cp = run("sync_audit.py", "--drift", cwd=root, expect_rc=0)
    assert "**дрейф**" in cp.stdout and "дрейф» (источник" not in cp.stdout

    src.write_text("источник изменился", encoding="utf-8")
    cp = run("sync_audit.py", "--drift", cwd=root, expect_rc=1)
    assert "Дрейф — перепроверить" in cp.stdout, "изменение источника не поймано"
    assert "Знание" in cp.stdout, "карточка не названа"

    src.unlink()
    cp = run("sync_audit.py", "--drift", cwd=root)
    assert "Битые источники" in cp.stdout, "исчезнувший источник не пойман"


@test
def test_release_freezes_snapshot_once(tmp: Path):
    root = make_project(tmp)
    card(root, "Systems/Шина.md", "", status="verified", owner='"@v"',
         verified="2026-01-01", review_by="2030-01-01")
    card(root, "Systems/Черновик.md", "", status="draft")
    work = root / "Deliverables/work/ОПЗ_v2.1.md"
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_text('---\ndoc: ОПЗ\nversion: "2.1"\ntype: deliverable\n'
                    'based_on: ["[[Шина]]", "[[Черновик]]"]\n---\n\n# ОПЗ\n', encoding="utf-8")

    cp = run("ship_doc.py", "--release", "Deliverables/work/ОПЗ_v2.1.md", "--date", "2026-05-05",
             "--apply", cwd=root)
    snap = root / "Deliverables/released/ОПЗ_v2.1_2026-05-05.md"
    assert snap.is_file(), f"снапшот не создан:\n{cp.stdout}"
    assert "released: 2026-05-05" in snap.read_text(encoding="utf-8"), "нет даты передачи"
    assert "released: 2026-05-05" in work.read_text(encoding="utf-8"), "рабочая копия не помечена"
    assert "Ниже verified: 1" in cp.stdout, "риск непроверенного основания не назван"

    cp2 = run("ship_doc.py", "--release", "Deliverables/work/ОПЗ_v2.1.md", "--date", "2026-05-05",
              cwd=root, expect_rc=1)
    assert "уже существует" in cp2.stderr, "перезапись сданного должна блокироваться"


@test
def test_build_plan_partitions_and_resumes(tmp: Path):
    """План извлечения: порядок групп, партии по бюджету, возобновление по манифесту."""
    root = make_project(tmp)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/Reference/Glossary").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/Reference/abbreviations.md").write_text("x" * 900, encoding="utf-8")
    (root / "AuroraKnowledgeDB/Reference/Glossary/Заявка.md").write_text("x" * 900, encoding="utf-8")
    (root / "Raw/project/обзор.md").write_text("y" * 900, encoding="utf-8")
    (root / "Sources/Confluence/страница.md").write_text("z" * 900, encoding="utf-8")

    cp = run("build_plan.py", "--budget", "2000", cwd=root)
    assert "Источников: 3" in cp.stdout, f"извлечённая карточка Reference принята за источник:\n{cp.stdout}"
    plan = cp.stdout[cp.stdout.index("Партий:"):]
    assert plan.index("abbreviations.md") < plan.index("обзор.md") < plan.index("страница.md"), \
        "нарушен порядок обхода build.md (терминология → проект → Confluence)"
    assert "Партий: 2" in cp.stdout, f"бюджет партии не соблюдён:\n{cp.stdout}"  # 900+900 ≤ 2000 < 2700

    # отметка ставится по факту: карточки с этим source должны существовать
    for i in range(7):
        card(root, f"Concepts/Из-обзора-{i}.md", "тело", source='"Raw/project/обзор.md"')
    run("build_plan.py", "--done", "Raw/project/обзор.md", "--cards", "7", cwd=root)
    cp2 = run("build_plan.py", cwd=root)
    assert "обработано: 1 (7 карточек)" in cp2.stdout, "прогресс не учтён"
    assert "обзор.md" not in cp2.stdout[cp2.stdout.index("Партий:"):], "обработанный остался в плане"

    (root / "Raw/project/обзор.md").write_text("y" * 1200, encoding="utf-8")
    cp3 = run("build_plan.py", cwd=root)
    assert "обзор.md" in cp3.stdout, "изменившийся источник не вернулся в план"


@test
def test_lint_checks_release_registry(tmp: Path):
    """applies_to без реестра релизов — мёртвая разметка: фильтр контекста молча выключен."""
    root = make_project(tmp)
    # Связь у карточки должна быть: с 1.91 одиночка — отдельная ошибка линтера, и она
    # заслонила бы то, ради чего написан этот кейс.
    card(root, "Systems/Шина-R2.md", "см. [[Очередь]]", status="knowledge", applies_to="[R2]")
    card(root, "Systems/Очередь.md", "см. [[Шина-R2]]", status="knowledge")

    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "нет AuroraKnowledgeDB/meta/releases.md" in cp.stdout, \
        f"отсутствие реестра релизов не поймано:\n{cp.stdout}"

    (root / "AuroraKnowledgeDB/meta/releases.md").write_text(
        "| Релиз | Состояние |\n| R1 | current |\n", encoding="utf-8")
    cp2 = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "вне реестра" in cp2.stdout, "релиз R2 отсутствует в реестре — должно быть ошибкой"

    (root / "AuroraKnowledgeDB/meta/releases.md").write_text(
        "| Релиз | Состояние |\n| R1 | — |\n| R2 | current |\n", encoding="utf-8")
    cp3 = run("kb_lint.py", cwd=root, expect_rc=0)
    assert "ошибок 0" in cp3.stdout, f"корректная разметка не должна ругаться:\n{cp3.stdout}"


@test
def test_spec_pack_bundles_grounds_and_names_risks(tmp: Path):
    """Бандл: основания по ссылке-идентификатору, DoR, якоря вместо wiki-ссылок."""
    root = make_project(tmp)
    card(root, "Specs/SPEC-012-Обмен.md", "Опирается на [[Шина]] и [[Черновик]].",
         type="spec", spec_id="SPEC-012", status="draft", version='"1.2"',
         implements='["[[REQ-042]]"]', decisions='["[[DR-0007-Шина]]"]',
         based_on='["[[Шина]]"]')
    card(root, "Requirements/REQ-042-Обмен-с-смежная система.md", "", type="requirement",
         req_id="REQ-042", req_status="stated", status="imported")
    card(root, "Systems/Шина.md", "Kafka по VPN", status="knowledge",
         trust_basis='"задача PRJ-1 в статусе Готово"', review_by="2030-01-01")
    card(root, "Systems/Черновик.md", "набросок", status="draft")
    card(root, "Decisions/DR-0007-Шина.md", "Выбрали Kafka", type="decision", status="accepted")

    cp = run("spec_pack.py", "SPEC-012", "--apply", cwd=root)
    assert "REQ-042-Обмен-с-смежная система (stated)" in cp.stdout, \
        f"ссылка по идентификатору не разрезолвлена — DoR молчит:\n{cp.stdout}"
    assert "Черновик (draft)" in cp.stdout, "основание ниже verified не названо"

    pack = (root / "Deliverables/work/spec-packs/SPEC-012_v1.2.md").read_text(encoding="utf-8")
    assert "## Риски передачи" in pack, "риски не попали в бандл"
    assert "Kafka по VPN" in pack and "Выбрали Kafka" in pack, "тела оснований не приложены"
    assert "[knowledge | доверенный источник | задача PRJ-1 в статусе Готово]" in pack, \
        f"нет шапки доверия у основания или она говорит по старой модели:\n{pack[:600]}"
    assert "НЕ ПРОВЕРЕНО ЧЕЛОВЕКОМ" not in pack and "владелец" not in pack, \
        "в пакете осталась шапка прежней приёмки"
    assert "[[" not in pack, "wiki-ссылки не разрезолвлены в якоря — снаружи базы не кликаются"


@test
def test_index_regenerates_but_respects_handmade(tmp: Path):
    root = make_project(tmp)
    card(root, "Glossary/Заявка.md", "Документ о предстоящей поставке товаров.",
         status="verified", owner='"@v"', verified="2026-01-01", review_by="2030-01-01")
    card(root, "Glossary/ЕНС.md", "Единый налоговый счёт.", status="imported")
    (root / "AuroraKnowledgeDB/Systems/_index.md").write_text(
        "# Мой рукотворный индекс\n", encoding="utf-8")
    card(root, "Systems/Шина.md", "Kafka", status="imported")

    cp = run("kb_index.py", "--apply", cwd=root)
    idx = (root / "AuroraKnowledgeDB/Glossary/_index.md").read_text(encoding="utf-8")
    assert "[[Заявка]]" in idx and "[[ЕНС]]" in idx, "карточки не попали в индекс"
    assert idx.index("[[Заявка]]") < idx.index("[[ЕНС]]"), "verified должен идти раньше imported"
    assert "Документ о предстоящей поставке товаров" in idx, "нет описания карточки"

    hand = (root / "AuroraKnowledgeDB/Systems/_index.md").read_text(encoding="utf-8")
    assert hand == "# Мой рукотворный индекс\n", "рукотворный индекс затёрт без спроса"
    # Пропуск — это находка, а не тишина: иначе маршрут рапортует «шаг пройден», и
    # человек проходит все сценарии подряд, а оглавления не обновляются ни разу.
    assert cp.returncode == 1, \
        f"отставшее рукотворное оглавление не объявлено находкой: rc={cp.returncode}"
    assert "## оглавление отстало от базы: 1" in cp.stdout, \
        f"находка не в формате отчёта — маршрут её не покажет:\n{cp.stdout}"
    assert "Шина" in cp.stdout, "не названа карточка, которой нет в оглавлении"

    run("kb_index.py", "--apply", "--force", cwd=root)
    assert "[[Шина]]" in (root / "AuroraKnowledgeDB/Systems/_index.md").read_text(encoding="utf-8")


@test
def test_index_adopts_what_an_older_generator_left(tmp: Path):
    """Оглавление без пометки, но с составом оглавления, — машинное: его пересобираем.

    Пометку ставят не всегда: ранние версии команды её не писали, а до них оглавления
    собирала модель. Защита «чужой текст не затираем» держала такие файлы годами — в
    живом проекте раздел вырос с двух карточек до 218, а оглавление осталось прежним.
    """
    root = make_project(tmp)
    for i in range(4):
        card(root, f"Processes/ALG-00{i}-Процесс.md", f"Шаг {i}", status="imported")
    (root / "AuroraKnowledgeDB/Processes/_index.md").write_text(
        "# Processes — бизнес-процессы\n\n"
        "Описание бизнес-процессов и их активностей.\n\n"
        "| Карточка | Описание |\n|---|---|\n"
        "| [[ALG-000-Процесс]] | первый |\n"
        "| [[ALG-777-Уехавший]] | карточку переименовали, ссылка умерла |\n",
        encoding="utf-8")

    cp = run("kb_index.py", "--apply", cwd=root, expect_rc=0)
    assert "Приняты под генерацию" in cp.stdout, \
        f"старое машинное оглавление не опознано:\n{cp.stdout}"
    idx = (root / "AuroraKnowledgeDB/Processes/_index.md").read_text(encoding="utf-8")
    for i in range(4):
        assert f"[[ALG-00{i}-Процесс]]" in idx, f"карточка ALG-00{i} не попала в оглавление"
    assert "ALG-777" not in idx, "мёртвая запись пережила пересборку"
    # заголовок и введение писал человек — они переживают регенерацию
    assert "# Processes — бизнес-процессы" in idx, "потерян осмысленный заголовок раздела"
    assert "Описание бизнес-процессов и их активностей." in idx, "потеряно введение раздела"

    before = idx
    run("kb_index.py", "--apply", cwd=root, expect_rc=0)
    assert (root / "AuroraKnowledgeDB/Processes/_index.md").read_text(
        encoding="utf-8") == before, "повторный прогон меняет файл — введение накапливается"


@test
def test_index_leaves_a_section_overview_written_by_a_human(tmp: Path):
    """Раздел, где человек написал текст со ссылками, — не оглавление, а знание."""
    root = make_project(tmp)
    for i in range(3):
        card(root, f"Reference/Справочник-{i}.md", f"строки {i}", status="imported")
    hand = ("# Reference — как читать справочники\n\n"
            "Справочники ведутся руками, у каждого свой владелец и срок годности.\n\n"
            "Аббревиатуры подмешиваются в каждый пак автоматически, поэтому их\n"
            "не нужно перечислять в постановке: модель увидит их и так.\n\n"
            "Если справочник расходится с источником, правьте источник, а не карточку:\n"
            "иначе следующий синк вернёт расхождение обратно.\n\n"
            "- [[Справочник-0]] — пример\n- [[Справочник-1]] — пример\n")
    (root / "AuroraKnowledgeDB/Reference/_index.md").write_text(hand, encoding="utf-8")

    cp = run("kb_index.py", "--apply", cwd=root, expect_rc=1)
    assert (root / "AuroraKnowledgeDB/Reference/_index.md").read_text(
        encoding="utf-8") == hand, "текст человека затёрт: абзацы приняли за оглавление"
    assert "оглавление отстало от базы: 1" in cp.stdout, \
        f"отставание рукотворного оглавления не названо находкой:\n{cp.stdout}"


@test
def test_index_stays_quiet_when_handmade_is_current(tmp: Path):
    """Рукотворное оглавление, где все карточки на месте, не отстало — и молчит."""
    root = make_project(tmp)
    card(root, "Systems/Шина.md", "Kafka", status="imported")
    (root / "AuroraKnowledgeDB/Systems/_index.md").write_text(
        "# Системы\n\n- [[Шина|шина данных]] — очередь событий\n", encoding="utf-8")

    cp = run("kb_index.py", "--apply", cwd=root, expect_rc=0)
    assert "оглавление отстало" not in cp.stdout, \
        f"полное рукотворное оглавление объявлено отставшим — ложная находка:\n{cp.stdout}"
    assert (root / "AuroraKnowledgeDB/Systems/_index.md").read_text(
        encoding="utf-8").startswith("# Системы"), "рукотворный индекс затёрт"


@test
def test_trace_links_only_to_existing_registry(tmp: Path):
    """Ссылка на реестр договорных документов ставится, только если он есть в базе."""
    root = make_project(tmp)
    run("kb_trace.py", "--requirements", cwd=root)
    out = (root / "AuroraKnowledgeDB/MOC/Трассировка-требований.md").read_text(encoding="utf-8")
    assert "[[contract_documents]]" not in out, \
        "генератор ставит ссылку на карточку, которой в проекте нет — линтер ловит битую"

    card(root, "Reference/contract_documents.md", "реестр", status="imported")
    run("kb_trace.py", "--requirements", cwd=root)
    out = (root / "AuroraKnowledgeDB/MOC/Трассировка-требований.md").read_text(encoding="utf-8")
    assert "[[contract_documents]]" in out, "реестр есть, а ссылки на него нет"


@test
def test_audit_normalizes_unicode_paths(tmp: Path):
    """macOS хранит имена в NFD: без нормализации один файл — сразу MISSING и ORPHAN."""
    import unicodedata
    root = make_project(tmp)
    mirror = root / "Sources/Confluence"
    d = mirror / "Раздел"
    d.mkdir(parents=True, exist_ok=True)
    name = "Ссылка_(QR-кода).md"
    (d / name).write_text("page_id: 12345\n\nтекст\n", encoding="utf-8")
    nfd = unicodedata.normalize("NFD", f"Раздел/{name}")
    (mirror / "sync_state.md").write_text(
        "<!-- Confluence sync state -->\n**Sync Date:** 2026-07-27\n**Pages:** 1\n\n"
        "| # | Page ID | Title | Local Path | Status |\n|---|---|---|---|---|\n"
        f"| 1 | 12345 | Ссылка | {nfd} | SYNCED |\n", encoding="utf-8")

    cp = run("sync_audit.py", cwd=root)
    assert "MISSING: **0**" in cp.stdout and "ORPHAN: **0**" in cp.stdout, \
        f"NFD-путь в состоянии не сведён с NFC на диске:\n{cp.stdout}"


@test
def test_fix_drops_retired_schema_fields(tmp: Path):
    """Поля, выведенные из схемы, ремонт убирает: иначе модель копирует их дальше."""
    root = make_project(tmp, git=True)
    card(root, "Glossary/Термин.md", "тело", status="canonical", type="concept",
         audience="[SA, Dev]", confirmed_by='"@кто-то"')
    card(root, "Glossary/Живой.md", "тело", status="verified", type="concept")

    (root / "AuroraKnowledgeDB/Glossary/Без-типа.md").write_text(
        '---\ntitle: "Без-типа"\nstatus: imported\n---\n\nтело\n', encoding="utf-8")

    run("kb_fix.py", "--retire", "--apply", "--allow-dirty", cwd=root)
    got = (root / "AuroraKnowledgeDB/Glossary/Термин.md").read_text(encoding="utf-8")
    assert "audience" not in got and "confirmed_by" not in got, "поля вне схемы остались"
    assert "status: verified" in got, \
        "легаси-статус canonical должен стать verified, а не потерять доверие"
    assert "тело" in got, "тело карточки пострадало при чистке шапки"
    # чистка схемы не должна попутно достраивать шапки: это отдельное решение по базе
    bez = (root / "AuroraKnowledgeDB/Glossary/Без-типа.md").read_text(encoding="utf-8")
    assert "type:" not in bez, "--retire достроил type — режимы смешались, diff не разобрать"


@test
def test_jira_status_reports_candidates_not_verdicts(tmp: Path):
    """Обратный поток: закрытые задачи — кандидат человеку, а не автоматический implemented."""
    root = make_project(tmp, git=True)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)

    def issue(name, key, status, resolution="_empty_"):
        (mirror / f"{name}.md").write_text(
            f"# {name}: задача\n\n- **URL:** https://jira.example/browse/{key}\n"
            f"- **Type:** Task\n- **Status:** {status}\n- **Resolution:** {resolution}\n",
            encoding="utf-8")

    issue("t1", "PRJ-1", "Закрыто")
    issue("t2", "PRJ-2", "Done")
    issue("t3", "PRJ-3", "В работе")
    issue("t4", "PRJ-4", "Готово", "Canceled")
    issue("t5", "PRJ-5", "Согласование у заказчика")   # статус, которого движок не знает
    (mirror / "t6.md").write_text(
        "# t6: задача\n\n- **URL:** https://jira.example/browse/PRJ-6\n- **Type:** Task\n"
        "- **Status:** Done\n\nРеализует REQ-009 целиком.\n", encoding="utf-8")
    # Нынешний формат зеркала: поля в frontmatter. Пока фикстура знала только старый вид,
    # обратный поток на живом зеркале из 189 задач печатал «пустое зеркало» — и это
    # выглядело как «расхождений нет». Оба формата обязаны читаться.
    (mirror / "PRJ-7.md").write_text(
        '---\nkey: "PRJ-7"\ntitle: "US-9.9.9. Свежий формат"\ntype: "История"\n'
        'status: "Закрыто"\nepic_title: "Epic 9"\nupdated: "2026-06-10 15:06:39"\n'
        'url: "https://jira.example/browse/PRJ-7"\n---\n\n# PRJ-7: US-9.9.9\n',
        encoding="utf-8")
    card(root, "Requirements/REQ-007-Frontmatter.md", "", type="requirement",
         req_id="REQ-007", req_status="agreed", jira='["PRJ-7"]')

    card(root, "Requirements/REQ-001-Готово.md", "", type="requirement",
         req_id="REQ-001", req_status="agreed", jira='["PRJ-1", "PRJ-2"]')
    card(root, "Requirements/REQ-002-В-работе.md", "", type="requirement",
         req_id="REQ-002", req_status="agreed", jira='["PRJ-3"]')
    card(root, "Requirements/REQ-003-Отменено.md", "", type="requirement",
         req_id="REQ-003", req_status="agreed", jira='["PRJ-4"]')
    card(root, "Requirements/REQ-009-По-упоминанию.md", "", type="requirement",
         req_id="REQ-009", req_status="stated", jira="[]")

    cp = run("jira_status.py", cwd=root)
    out = cp.stdout
    assert "REQ-001" in out.split("## Кандидаты")[1].split("##")[0], \
        f"требование с закрытыми задачами не попало в кандидаты:\n{out}"
    assert "REQ-003" in out.split("## Требования под риском")[1].split("##")[0], \
        "отменённая задача не подняла риск"
    assert "REQ-002" not in out.split("## Кандидаты")[1].split("##")[0], \
        "требование с открытой задачей нельзя предлагать в implemented"
    assert "Согласование у заказчика" in out, "незнакомый статус не показан человеку"
    assert "В работе" not in out.split("не знает")[-1], \
        "обычная рабочая стадия названа незнакомым статусом"
    assert "REQ-009-По-упоминанию → PRJ-6" in out, "связь по упоминанию REQ-NNN не найдена"
    assert "Задач в зеркале: 7" in out, \
        f"зеркало в нынешнем формате не прочитано — задача из frontmatter потеряна:\n{out}"
    assert "REQ-007" in out.split("## Кандидаты")[1].split("##")[0], \
        "закрытая задача из frontmatter-зеркала не дала кандидата"

    before = (root / "AuroraKnowledgeDB/Requirements/REQ-001-Готово.md").read_text(encoding="utf-8")
    assert "implemented" not in before

    run("jira_status.py", "--apply", "--link", "--allow-dirty", cwd=root)
    got = (root / "AuroraKnowledgeDB/Requirements/REQ-001-Готово.md").read_text(encoding="utf-8")
    assert "jira_state:" in got and "jira_checked:" in got, "наблюдение не записано"
    assert "req_status: agreed" in got, \
        "req_status двигает приёмка, а не статус задачи в Jira"
    linked = (root / "AuroraKnowledgeDB/Requirements/REQ-009-По-упоминанию.md").read_text(encoding="utf-8")
    assert "PRJ-6" in linked, "--link не проставил найденную связь"


@test
def test_repair_converges_instead_of_repeating_itself(tmp: Path):
    """Повторный ремонт должен сходиться, а не показывать тот же список.

    Три источника вечного шума, каждый — не работа для человека:
    самоповтор синонима внутри одной карточки выглядел спором двух карточек;
    ссылки-образцы в шаблонах (`[[...]]`, `[[{{кто-то}}]]`) чинить нечем;
    служебные файлы (`_index.md`, `meta/`) генерируются или ссылаются на будущее знание.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    (kb / "Roles").mkdir(parents=True, exist_ok=True)
    (kb / "Roles/Заявитель.md").write_text(
        '---\ntitle: "Заявитель"\ntype: role\nstatus: imported\n'
        'aliases: ["Заявитель", "Заявитель", "Податель"]\n---\n\nроль\n', encoding="utf-8")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/образец.md").write_text(
        '---\ntitle: "Образец"\n---\n\nсм. [[...]] и [[{{протокол}}]]\n', encoding="utf-8")
    (kb / "meta").mkdir(exist_ok=True)
    (kb / "meta/golden_questions.md").write_text(
        "# Вопросы\n\n- [[Знание-которого-пока-нет]]\n", encoding="utf-8")

    first = run("kb_fix.py", "--all", "--apply", "--allow-dirty", cwd=root)
    assert "повторяющих имя своей же карточки" in first.stdout, \
        f"самоповтор синонима не снят:\n{first.stdout[:700]}"
    for junk in ("[[...]]", "{{протокол}}", "Знание-которого-пока-нет"):
        assert junk not in first.stdout, f"в отчёт попало нерешаемое: {junk}"

    text = (kb / "Roles/Заявитель.md").read_text(encoding="utf-8")
    assert text.count("Заявитель\"") <= 1 or "Податель" in text, "синонимы карточки испорчены"

    second = run("kb_fix.py", "--all", "--apply", "--allow-dirty", cwd=root)
    assert "Одинаковые alias: 0" in second.stdout or "Одинаковые alias" not in second.stdout, \
        f"второй прогон нашёл те же конфликты:\n{second.stdout[:700]}"
    assert "повторяющих имя своей же карточки" not in second.stdout, \
        "самоповторы возвращаются на каждом прогоне — ремонт не сходится"


@test
def test_dedupe_merges_a_batch_by_a_stated_rule(tmp: Path):
    """Двойники сливаются пачкой по объявленному правилу, спорное остаётся человеку.

    Пар в живой базе под сотню, и разбирать их по одной командой на пару — работа ради
    работы: в большинстве случаев ответ виден механически. Но не во всех: общий синоним
    у двух карточек не означает, что это один предмет.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"

    # 1. раздел по имени: алгоритм живёт в Processes
    card(root, "Concepts/ALG-052-Проверка.md", "короткая версия")
    card(root, "Processes/ALG-052-Проверка.md", "полная версия алгоритма " * 10)
    # 2. статус: принятое старше черновика
    card(root, "Concepts/Термин-А.md", "черновик", status="imported")
    card(root, "Glossary/Термин-А.md", "принято", status="verified",
         owner='"@v"', verified="2026-01-01", review_by="2030-01-01")
    # 3. общий синоним — не повод сливать
    (kb / "Processes/Этап-контроля.md").write_text(
        '---\ntitle: "Этап контроля"\ntype: process\nstatus: imported\n'
        'aliases: ["Контроль на границе"]\n---\n\nэтап процесса\n', encoding="utf-8")
    (kb / "Concepts/Контроль-на-границе.md").write_text(
        '---\ntitle: "Контроль на границе"\ntype: concept\nstatus: imported\n'
        'aliases: ["Контроль на границе"]\n---\n\nдругое понятие\n', encoding="utf-8")

    dry = run("kb_fix.py", "--merge-all", cwd=root)
    assert "Слияние двойников" in dry.stdout, dry.stdout[:600]
    assert "раздел по имени: Processes" in dry.stdout, "правило раздела не сработало"
    assert "общий синоним" in dry.stdout, "пара с общим синонимом слита автоматически"
    assert (kb / "Concepts/ALG-052-Проверка.md").is_file(), "dry-run уже что-то удалил"

    run("kb_fix.py", "--merge-all", "--apply", "--allow-dirty", cwd=root)
    winner = (kb / "Processes/ALG-052-Проверка.md").read_text(encoding="utf-8")
    assert "короткая версия" in winner, "тело донора не присоединено"
    loser = (kb / "_archive/ALG-052-Проверка.md")
    assert loser.is_file(), "донор не уехал в _archive"
    assert "status: deprecated" in loser.read_text(encoding="utf-8"), "донор не деприкейтнут"
    assert (kb / "Glossary/Термин-А.md").is_file() and \
        not (kb / "Concepts/Термин-А.md").exists(), "победил черновик, а не принятое"
    assert (kb / "Processes/Этап-контроля.md").is_file() and \
        (kb / "Concepts/Контроль-на-границе.md").is_file(), \
        "карточки с общим синонимом должны остаться обе"


@test
def test_scripts_name_their_cockpit_command(tmp: Path):
    """У каждого скрипта в шапке написано, как его работа называется в панели.

    Человек нажимает кнопку `kb:dedupe`, а не набирает путь к файлу. Модель, читающая
    код, обязана видеть это имя — иначе в отчёте появляется «запустите kb_dedupe.py»,
    то есть файл, которого нет. Строка `Панель:` сверяется с реестром: разойдись они —
    подсказка станет врать.
    """
    import collections
    reg = collections.defaultdict(list)
    for line in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines():
        if "|" not in line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")]
        if len(p) < 5 or ".py" not in p[4]:
            continue
        reg[p[4].split()[0]].append(p[1])

    missing, stale = [], []
    for script, cmds in sorted(reg.items()):
        path = KIT / "scripts" / script
        if not path.is_file():
            continue
        head = path.read_text(encoding="utf-8").split('"""')[1]
        line = next((l for l in head.splitlines() if l.startswith("Панель:")), "")
        if not line:
            missing.append(script)
            continue
        for cmd in cmds:
            if f"`{cmd}`" not in line:
                stale.append(f"{script}: в шапке нет {cmd}")
    assert not missing, "скрипты не называют свою команду панели: " + ", ".join(missing)
    assert not stale, "шапка разошлась с реестром:\n    " + "\n    ".join(stale)


@test
def test_copy_button_takes_the_task_without_its_frame(tmp: Path):
    """В буфер уходит тело задания, а не оформление вокруг него.

    Задание обрамлено линейками и заголовком «…— скопируйте блок целиком в чат». Эта
    строка адресована человеку у панели; попав в чат, она даёт ассистенту указание,
    которое к нему не относится, а линейки просто съедают контекст.
    """
    ui = panel_sources()
    fn = ui[ui.index("function assistantTasks(lines){"):ui.index("let LAST_TASKS = [];")]
    assert "рамка в тело не идёт" in fn, "границы блока снова режутся по старому правилу"
    # тело начинается после ВТОРОЙ линейки — той, что под заголовком
    assert "while (from < lines.length && !lines[from].startsWith(TASK_EDGE)) from++;" in fn, \
        "начало тела ищется не от заголовка вниз"
    assert 't("task.copied", {label: task.label})' in ui, \
        "подпись в уведомлении врёт про партию там, где партий нет"


@test
def test_build_skill_does_not_ask_model_to_scan_the_base(tmp: Path):
    """Инструкция не должна поручать модели работу, которая стоит обхода всей базы.

    До 1.48.1 в build.md было четыре таких места: «Verify file exists BEFORE writing the
    link», «search all existing notes by aliases», Pre-Write Validation на каждую карточку
    и обязательные шесть-семь синонимов. Каждое — перебор тысячи файлов на карточку;
    отсюда сутки на партию и полсотни конфликтующих синонимов в живой базе.
    """
    text = (KIT / "skills/aurora-vault/references/build.md").read_text(encoding="utf-8")
    forbidden = [
        ("Verify file exists", "проверка существования цели ссылки — это kb:lint"),
        ("search all existing notes", "поиск по всей базе — это резолвер kb:repair --links"),
        ("MANDATORY — Every Note", "обязательные синонимы на каждую карточку рождают конфликты"),
        ("Pre-Write Validation", "чек-лист на каждую карточку — это kb:lint после партии"),
    ]
    hits = [f"«{needle}» — {why}" for needle, why in forbidden if needle in text]
    assert not hits, "инструкция снова просит модель обходить базу:\n    " + "\n    ".join(hits)

    for must in ("kb:repair --links", "kb:repair --stubs", "kb:links --cards", "kb:index"):
        assert must in text, f"не сказано, что {must} делает эту работу за модель"


@test
def test_build_slices_source_and_assembles_card(tmp: Path):
    """Текст карточки переносит скрипт: модель решает границы тем, а не перепечатывает.

    Раньше задание требовало от ассистента создать карточки, то есть заново набрать текст
    источника своими токенами. На живой базе это 5,6 МБ вывода — несколько суток работы
    там, где решений на пару часов. Теперь модель называет тему и указывает номера секций.
    """
    root = make_project(tmp)
    src = root / "Sources/Confluence/Страница.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        '---\ntitle: "Страница"\npage_id: 1\n---\n\n'
        "## Входящие данные\n\n" + "поля запроса и их смысл. " * 20 + "\n\n"
        "## Алгоритм\n\n" + "шаг за шагом, что делает система. " * 20 + "\n\n"
        "## История изменений\n\n" + "версии страницы и кто правил. " * 20 + "\n\n"
        "## Подпись\n\nкоротко\n", encoding="utf-8")

    sl = run("build_plan.py", "--slice", "Sources/Confluence/Страница.md", cwd=root)
    assert "секций: 3" in sl.stdout, f"нарезка не по заголовкам:\n{sl.stdout[:600]}"
    assert "Подпись" not in sl.stdout, "секция короче порога попала в раскадровку"
    assert "--card" in sl.stdout, "в задании нет команды сборки карточки"

    dry = run("build_plan.py", "--card", "Алгоритм приёма", "--source",
              "Sources/Confluence/Страница.md", "--sections", "1,2", "--to", "Processes",
              cwd=root)
    assert "(dry-run)" in dry.stdout, "без --apply карточка не должна записываться"
    assert not list((root / "AuroraKnowledgeDB/Processes").glob("Алгоритм*.md"))

    run("build_plan.py", "--card", "Алгоритм приёма", "--source",
        "Sources/Confluence/Страница.md", "--sections", "1,2", "--to", "Processes",
        "--by", "qwen3.8-flash", "--apply", cwd=root)
    made = root / "AuroraKnowledgeDB/Processes/Алгоритм-приёма.md"
    assert made.is_file(), "имя файла собрано не по правилу build.md"
    text = made.read_text(encoding="utf-8")
    assert "type: process" in text, text[:300]
    # Модель-автор — свойство карточки, а не журнала прогона: журналы удаляются по сроку
    # хранения, и сравнить разбор на двух моделях потом оказывается не на чем.
    assert 'built_by: "qwen3.8-flash"' in text, f"модель-автор не записана:\n{text[:400]}"
    assert card_srcs(text) == ["Sources/Confluence/Страница.md"], text[:300]
    assert "поля запроса" in text and "шаг за шагом" in text, "тело секций не перенесено"
    plain = run("build_plan.py", "--card", "Приём без автора", "--source",
                "Sources/Confluence/Страница.md", "--sections", "1", "--to", "Processes",
                "--apply", cwd=root)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    bare = (root / "AuroraKnowledgeDB/Processes/Приём-без-автора.md").read_text(encoding="utf-8")
    assert "built_by" not in bare, "без --by в шапке появилось пустое поле"
    assert "версии страницы" not in text, "перенесена секция, которую не просили"

    # Повтор из того же источника — не конфликт, а второй проход по обновлённой
    # странице: отказ здесь ронял бы разбор при каждой правке источника.
    again = run("build_plan.py", "--card", "Алгоритм приёма", "--source",
                "Sources/Confluence/Страница.md", "--sections", "1", "--to", "Processes",
                "--apply", cwd=root)
    assert again.returncode == 0 and ("обновлён источник" in again.stdout
                                      or "без изменений" in again.stdout), again.stdout[-200:]
    redo = made.read_text(encoding="utf-8")
    assert redo.count("поля запроса") == 20, \
        f"повтор из того же источника задвоил текст страницы: {redo.count('поля запроса')} вместо 20"

    other = root / "Sources" / "Confluence" / "Другая.md"
    other.write_text("# Другая\n\n" + "текст. " * 60, encoding="utf-8")
    clash = run("build_plan.py", "--card", "Алгоритм приёма", "--source",
                "Sources/Confluence/Другая.md", "--sections", "1", "--to", "Processes",
                "--apply", cwd=root, expect_rc=1)
    assert "уже есть" in clash.stderr, "двойник имени из чужого источника должен отвергаться"


@test
def test_mermaid_blocks_render_on_github(tmp: Path):
    """Диаграммы в документации обязаны рендериться у GitHub, а не только у нас.

    GitHub рендерит mermaid версией 10, где двоеточие в подписи перехода
    `stateDiagram-v2` роняет парсер: `a --> b: kb:build` — ошибка «Unable to render rich
    display». А имена команд Авроры сплошь с двоеточиями. Пишем их как `kb#58;build`:
    и читается, и лексер не падает.
    """
    import re as _re
    bad = []
    for md in sorted(KIT.rglob("*.md")):
        if any(part in (".git", "node_modules", "examples") for part in md.parts):
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        for block in _re.findall(r"```mermaid\n(.*?)```", text, _re.S):
            first = next((l.strip() for l in block.splitlines() if l.strip()), "")
            if not first.startswith("stateDiagram"):
                continue
            for line in block.splitlines():
                m = _re.match(r"^\s*\S+\s*-->\s*\S+:\s*(.*)$", line)
                if m and ":" in m.group(1):
                    bad.append(f"{md.relative_to(KIT)}: {line.strip()[:70]}")
    assert not bad, ("двоеточие в подписи перехода stateDiagram — GitHub не отрисует "
                     "диаграмму:\n    " + "\n    ".join(bad[:5]) +
                     "\n  Пишите `kb#58;build` вместо `kb:build`")


@test
def test_done_is_a_fact_not_a_claim(tmp: Path):
    """Отметка «разобрано» ставится по базе, а не по слову ассистента.

    Раньше `--done` записывался на слово вместе с числом карточек, которое ассистент
    называл сам: на живой базе так набралось 356 отметок с нулём карточек — источники
    выпали из плана, не дав знания. Законный ноль объявляется явно и с причиной.
    """
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Стр.md").write_text(
        '---\ntitle: "Стр"\npage_id: 1\n---\n\n' + "текст " * 200, encoding="utf-8")

    refused = run("build_plan.py", "--done", "Sources/Confluence/Стр.md", "--cards", "5",
                  cwd=root, expect_rc=1)
    assert "отметка не поставлена" in refused.stderr, refused.stderr[:300]
    assert "--empty" in refused.stderr, "не подсказано, как отметить законно пустой источник"

    card(root, "Concepts/Понятие.md", "тело", source='"Sources/Confluence/Стр.md"')
    ok = run("build_plan.py", "--done", "Sources/Confluence/Стр.md", "--cards", "5",
             cwd=root, expect_rc=0)
    assert "карточек 1" in ok.stdout and "называли 5" in ok.stdout, \
        f"число карточек должно браться из базы, а не из флага:\n{ok.stdout}"

    (root / "Sources/Confluence/Оглавление.md").write_text(
        '---\ntitle: "Оглавление"\npage_id: 2\n---\n\n' + "ссылки " * 100, encoding="utf-8")
    run("build_plan.py", "--done", "Sources/Confluence/Оглавление.md",
        "--empty", "страница-оглавление", cwd=root)
    man = json.loads((root / "AuroraKnowledgeDB/meta/manifest.json").read_text(encoding="utf-8"))
    rec = man["sources"]["Sources/Confluence/Оглавление.md"]
    assert rec["cards"] == 0 and rec["empty_reason"] == "страница-оглавление", \
        f"причина пустоты не записана: {rec}"


@test
def test_build_plan_reopens_sources_that_gave_nothing(tmp: Path):
    """Отметку «обработан» ставит ассистент, и она врёт в обе стороны.

    Файл, отмеченный сделанным, но не давший ни одной карточки, выпадает из плана
    навсегда: прогресс растёт, знание — нет. Проверяем по базе, а не по счётчику в
    манифесте: ноль карточек у задачи Jira или готового справочника бывает законным.
    """
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    for name in ("Пустая", "Полезная"):
        (root / f"Sources/Confluence/{name}.md").write_text(
            f'---\ntitle: "{name}"\npage_id: 1\n---\n\n' + "текст " * 60, encoding="utf-8")
    card(root, "Concepts/Из-полезной.md", "знание",
         source='"Sources/Confluence/Полезная.md"')

    run("build_plan.py", "--done", "Sources/Confluence/Полезная.md", "--cards", "3", cwd=root)
    # отметка «сделано» без единой карточки больше не проходит на слово
    refused = run("build_plan.py", "--done", "Sources/Confluence/Пустая.md",
                  cwd=root, expect_rc=1)
    assert "отметка не поставлена" in refused.stderr, refused.stderr[:300]
    run("build_plan.py", "--done", "Sources/Confluence/Пустая.md",
        "--empty", "проверка правила", cwd=root)
    assert "осталось: 0" in run("build_plan.py", "--status", cwd=root).stdout, \
        "оба источника должны считаться обработанными"

    # Источник с ВЫНЕСЕННЫМ вердиктом «пусто» — разобранный, а не пропущенный: он не
    # даст карточек никогда, и возвращать его в план значит гонять по нему модель вечно.
    # Ровно из-за этого маршрут «Обновить базу» не заканчивался работой в ноль (1.100.41).
    dry = run("build_plan.py", "--reopen", cwd=root)
    assert "Вернуть в план: 0" in dry.stdout, \
        f"источник с вердиктом «пусто» снова переоткрывается:\n{dry.stdout[:400]}"

    # А вот знание, ПРОПАВШЕЕ из базы, обязано вернуть источник в план: отметка
    # «обработан» без карточек — это молчаливая потеря, ради которой `--reopen` и есть.
    (root / "AuroraKnowledgeDB/Concepts/Из-полезной.md").unlink()
    dry2 = run("build_plan.py", "--reopen", cwd=root)
    assert "Вернуть в план: 1" in dry2.stdout, \
        f"источник, чьи карточки исчезли, не вернулся:\n{dry2.stdout[:400]}"
    assert "осталось: 0" in run("build_plan.py", "--status", cwd=root).stdout, \
        "dry-run не должен править манифест"

    run("build_plan.py", "--reopen", "--apply", cwd=root)
    status = run("build_plan.py", "--status", cwd=root).stdout
    assert "осталось: 1" in status, f"источник без карточек не вернулся в план:\n{status}"
    plan = run("build_plan.py", cwd=root).stdout
    assert "Полезная" in plan and "Пустая" not in plan, \
        "в план должен вернуться тот, чьё знание пропало, а не признанный пустым"

    # вторая сторона той же беды: карточка есть, но разбор оборвался на середине
    big = root / "Sources/Confluence/Толстая.md"
    big.write_text('---\ntitle: "Толстая"\npage_id: 9\n---\n\n' +
                   "".join(f"## Раздел {i}\n\n" + "текст " * 400 + "\n\n" for i in range(12)),
                   encoding="utf-8")
    card(root, "Concepts/Одна-из-толстой.md", "знание", source='"Sources/Confluence/Толстая.md"')
    run("build_plan.py", "--done", "Sources/Confluence/Толстая.md", "--cards", "1", cwd=root)
    thin = run("build_plan.py", "--thin", cwd=root)
    assert "Толстая.md" in thin.stdout, f"тонкий разбор не найден:\n{thin.stdout[:500]}"
    assert "Полезная" not in thin.stdout, "нормально разобранный источник попал в подозрения"
    assert "осталось: 1" in run("build_plan.py", "--status", cwd=root).stdout, \
        "--thin без --reopen не должен править манифест"
    run("build_plan.py", "--thin", "--reopen", "--apply", cwd=root)
    assert "осталось: 2" in run("build_plan.py", "--status", cwd=root).stdout, \
        "источник с неполным разбором не вернулся в план"


@test
def test_cockpit_roots_are_not_fixed_to_kit_neighbours(tmp: Path):
    """Проект можно развернуть где угодно, а не только в папке рядом с kit'ом.

    Корни поиска — пользовательская настройка, а не свойство движка: kit кладут куда
    угодно и переносят, проекты держат где удобно. Список живёт в домашней папке, папка
    нового проекта попадает в него сама — иначе проект пропал бы из панели сразу после
    создания. Ограничение осталось одно: системные деревья.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    home = tmp / "home"
    (home / "Documents/GitProjects").mkdir(parents=True)
    restore_home = set_home(home)
    try:
        importlib.reload(ck)
        assert str(home) in ck.ROOTS_FILE, "список корней должен жить в домашней папке"
        assert ck.load_roots() == [ck.norm(os.path.dirname(str(KIT)))], \
            "при первом запуске корень — папка рядом с kit'ом"

        far = str(home / "Documents/GitProjects/PRJ-A")
        assert ck.writable_target(far) == "", "папку вне корней запрещать нельзя"
        assert ck.writable_target("/etc/aurora"), "системное дерево должно отвергаться"
        assert ck.writable_target(str(home)), "разворачивать проект прямо в ~ нельзя"

        ck.save_roots([str(home / "Documents/GitProjects"), str(home / "work")])
        assert ck.load_roots() == [ck.norm(str(home / "Documents/GitProjects")),
                                   ck.norm(str(home / "work"))], "список не сохранился"
        assert ck.load_roots(["~/elsewhere"]) == [ck.norm("~/elsewhere")], \
            "--roots должен перекрывать сохранённое на один запуск"
    finally:
        restore_home()
        importlib.reload(ck)

    ui = panel_sources()
    assert "Где панель ищет проекты" in ui and "/api/roots" in ui, \
        "корни должны правиться из панели, а не только флагом при запуске"


@test
def test_cockpit_scenarios_skins_and_about(tmp: Path):
    """Быстрый старт, скины и «О проекте»: данные лежат в файлах, а не в коде панели."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    # --- сценарии: каждый шаг-команда обязан существовать в реестре, иначе кнопка
    # «Запустить» ведёт в никуда, а человек идёт по сценарию вслепую
    known = {r["cmd"] for r in ck.registry()}
    scen = ck.scenarios()
    assert len(scen) >= 4, "сценариев подозрительно мало"
    for s in scen:
        assert s["title"] and s["steps"], f"пустой сценарий {s['id']}"
        seen_cycle = 0
        for st in s["steps"]:
            assert st["why"], f"шаг без объяснения в сценарии {s['id']}"
            if st.get("cycle"):
                # Разметка блока, а не шаг: у неё нет команды и быть не должно.
                seen_cycle += 1 if st["cycle"] == "цикл:" else -1
                assert seen_cycle in (0, 1), \
                    f"сценарий {s['id']}: цикл открыт или закрыт не по парам"
                continue
            if not st.get("manual"):
                assert st["cmd"] in known, \
                    f"сценарий {s['id']} зовёт несуществующую команду {st['cmd']}"
                # Маршрут проходится одной кнопкой: панель подставляет флаги из файла,
                # не спрашивая человека. Флаг, которого у команды нет, — это остановка
                # маршрута на середине с «unrecognized arguments», причём в проекте,
                # где предыдущие шаги уже записали половину работы.
                row = {r["cmd"]: r for r in ck.registry()}[st["cmd"]]
                flags = st.get("flags", [])
                for i, flag in enumerate(flags):
                    if not flag.startswith("--"):
                        continue
                    assert flag in row.get("flags", []), (
                        f"сценарий {s['id']}: у {st['cmd']} нет флага {flag}")
                    # Флагу нужно значение — значит следующим в строке идёт оно, а не
                    # другой флаг и не конец: голый «--source-older-than» останавливал
                    # маршрут на пятом шаге сообщением argparse.
                    if flag in row.get("flags_value", []):
                        nxt = flags[i + 1] if i + 1 < len(flags) else ""
                        assert nxt and not nxt.startswith("--"), (
                            f"сценарий {s['id']}: флагу {flag} нужно значение "
                            f"({st['cmd']}), а его нет")
            else:
                # шаг без кнопки обязан говорить, что сделать вместо неё
                assert st.get("skill", "").startswith("/aurora-vault"), \
                    f"шаг «{st['title']}» в сценарии {s['id']} не называет команду скилла"
        assert seen_cycle == 0, f"сценарий {s['id']}: цикл открыт и не закрыт"
    runnable = {r["cmd"] for r in ck.registry() if r["runnable"]}
    for s in ck.scenarios():
        for st in s["steps"]:
            if not st.get("manual") and not st.get("cycle") and st["cmd"] not in runnable:
                continue   # модельную команду панель показывает как строку для ассистента

    # --- скины: имя и описание из шапки файла, путь наружу не принимается
    sk = ck.skins()
    assert any(s["id"] == "zine" for s in sk), "нет скина по умолчанию"
    for s in sk:
        assert s["name"] and s["about"], f"скин {s['id']} без имени или описания"
        assert "--primary" in ck.skin_css(s["id"]), f"скин {s['id']} не задаёт токены"
    assert ck.skin_css("../../VERSION") == "", "скин читается по пути вне папки скинов"

    # --- флаги со значением: панель обязана знать, что --jql требует аргумента
    reg = {r["cmd"]: r for r in ck.registry()}
    jira = reg.get("sync:jira")
    assert jira and jira["flag_args"].get("--jql") == "JQL", \
        "панель не знает, что --jql принимает значение — отправит его голым"
    assert jira["flag_args"].get("--force") == "", "--force значения не принимает"
    for row in reg.values():
        for f in row["flags"]:
            assert f in row["flag_args"], f"{row['cmd']}: у флага {f} неизвестно, нужен ли аргумент"

    # --- вторая панель на занятом порту: адрес уже работающей берётся из отпечатка
    import json as _json
    ck.SESSION = str(tmp / "session.json")
    ck.write_session(8787, "http://127.0.0.1:8787/?t=xyz")
    saved = _json.loads(Path(ck.SESSION).read_text(encoding="utf-8"))
    assert saved["url"].endswith("t=xyz") and saved["pid"] == os.getpid(), saved
    assert ck.read_session()["port"] == 8787, "отпечаток панели не читается"
    assert ck.alive("http://127.0.0.1:9/?t=xyz") is False, \
        "молчащий порт не должен считаться работающей панелью"

    # --- реестр перечитывается, когда kit обновился под работающей панелью
    ck.CACHE["registry"], ck.CACHE["registry_stamp"] = [{"cmd": "устарело"}], -1
    assert any(r["cmd"] == "sync:jira" for r in ck.registry()), \
        "панель отдаёт вчерашний реестр после обновления kit"

    # --- о проекте
    a = ck.about()
    assert a["kit"] == (KIT / "VERSION").read_text(encoding="utf-8").strip()
    assert a["commands"] == len(known) and a["license"] == "Apache-2.0"


@test
def test_confluence_ref_parsing(tmp: Path):
    """Ссылка вида …/display/ПРОСТРАНСТВО/Заголовок номера не содержит.

    Раньше она молча сохранялась целиком в поле page_id, конфиг получал бессмысленный
    `…?pageId=https://…`, а форма такую строку не показывала: «сохранено», но пусто.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    C = importlib.import_module("confluence_export")

    assert C.parse_ref("640781363") == ("640781363", "", "")
    assert C.parse_ref("https://c.example.com/pages/viewpage.action?pageId=123")[0] == "123"
    assert C.parse_ref("https://c.example.com/display/SPACE/GUI") == ("", "SPACE", "GUI")
    assert C.parse_ref("https://c.example.com/display/SP/Мой+раздел")[2] == "Мой раздел"
    assert C.parse_ref("просто текст") == ("", "", "")

    class FakeApi:
        def by_title(self, space, title):
            return {"id": "999", "title": title} if title == "GUI" else {}
    pid, title, err = C.resolve_ref(FakeApi(), "https://c.example.com/display/SP/GUI")
    assert (pid, title, err) == ("999", "GUI", ""), (pid, title, err)
    pid, _t, err = C.resolve_ref(FakeApi(), "https://c.example.com/display/SP/Нет")
    assert not pid and "нет страницы" in err, "молчаливый отказ вместо объяснения"

    # конфиг не должен получать url, собранный вокруг не-номера
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text('project:\n  name: "T"\n  slug: T\n', encoding="utf-8")
    subprocess.run([sys.executable, str(SCRIPTS / "aurora_setup.py"), "--target", str(root),
                    "--json", "-"],
                   input=json.dumps({"conf_url": "https://c.example.com",
                                     "sync_roots": [{"page_id": "https://c.example.com/display/SP/GUI",
                                                     "title": "GUI"}]}),
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "pageId=https://" not in cfg, "в конфиг попал бессмысленный адрес"

    # форма читает корни любого вида, иначе неразрешённая строка исчезает с экрана
    ui = panel_sources()
    got = re.search(r"sync_roots: \(\(\) => \{([\s\S]*?)\n    \}\)\(\),", ui)
    assert got, "разбор корней синка в форме не найден"
    node = shutil.which("node")
    if node:
        probe = tmp / "roots.js"
        probe.write_text("const cfgRaw = {text: " + json.dumps(
            'atlassian:\n  confluence:\n    sync_roots:\n'
            '      - page_id: "https://c.example.com/display/SP/GUI"\n        title: "GUI"\n'
            '      - page_id: "111"\n        title: "Раздел А"\n        trusted: true\n'
            '  jira:\n    project_key: "P"\n') + "};\nconsole.log(JSON.stringify((() => {"
            + got.group(1) + "\n})()));\n", encoding="utf-8")
        cp = subprocess.run([node, str(probe)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cp.returncode == 0, cp.stderr
        rows = json.loads(cp.stdout)
        assert [r["page_id"] for r in rows] == ["https://c.example.com/display/SP/GUI", "111"], \
            f"панель читает только числовые page_id — нечисловая строка пропадёт из формы: {rows}"
        assert [r["trusted"] for r in rows] == [False, True], rows


@test
def test_cockpit_warns_about_unsaved_settings(tmp: Path):
    """Панель обязана предупредить, что уход со страницы потеряет введённое."""
    ui = panel_sources()
    # блоки настройки помечают правки и подсвечивают свою кнопку
    for key, label in (("form", "«Настройки проекта»"), ("tokens", "«Доступы»"),
                       ("yaml", "«aurora.config.yaml»"), ("new", "«Подключить новый проект»")):
        assert f'saveButton("{key}"' in ui, f"у блока {label} нет кнопки с учётом правок"
        assert label in ui, f"блок {label} не назван — предупреждение будет безымянным"
    assert ".btn.unsaved{" in ui, "нет отдельного стиля кнопки с несохранённым"
    assert "beforeunload" in ui, "закрытие вкладки не перехватывается"
    # уход из раздела спрашивает подтверждение и перечисляет блоки
    assert 'S.view === "setup" && view !== "setup" && !confirmLeave()' in ui, \
        "переход между разделами не проверяет несохранённое"
    assert "[...DIRTY.values()].join" in ui, "в вопросе не перечисляются названия блоков"


@test
def test_setup_accepts_answers_as_form(tmp: Path):
    """Настройка формой пишет тот же конфиг, что и диалог: один способ, не два."""
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Old"\n  slug: Old\n', encoding="utf-8")
    answers = {
        "name": "Новый", "slug": "New",
        "conf_url": "https://c.example.com", "conf_space": "NEW",
        "sync_roots": [{"page_id": "111", "title": "Раздел А"},
                       {"page_id": "https://c.example.com/pages/viewpage.action?pageId=222",
                        "title": "Раздел Б"}],
        "jira_key": "NEW", "scrub": "off",
    }
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_setup.py"),
                         "--target", str(root), "--json", "-"],
                        input=json.dumps(answers, ensure_ascii=False),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[:400]
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert 'page_id: "111"' in cfg and 'page_id: "222"' in cfg, \
        "несколько корней не записались (второй был вставлен ссылкой)"
    assert 'space: "NEW"' in cfg and "scrub: off" in cfg, "поля формы не доехали до конфига"
    assert cfg.count("page_id:") == 2, "корни задвоились или потерялись"


@test
def test_setup_form_saves_jql_with_quotes(tmp: Path):
    """JQL с датой (created >= "2026-11-01") раньше ломал конфиг: писался в двойных
    кавычках без экранирования, и ни один читатель такую строку не разбирал — поле
    «JQL по умолчанию» выглядело пустым после сохранения, а sync:jira молча уходил
    на запрос по умолчанию. Пишем такое значение в YAML-одинарных кавычках."""
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "T"\n  slug: T\n', encoding="utf-8")
    jql = ("project = PRJ AND type in (Story,История,'BA-SA Task','Инцидент') "
           'AND (created >= "2026-11-01")')
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_setup.py"),
                         "--target", str(root), "--json", "-"],
                        input=json.dumps({"jira_key": "PRJ", "jira_jql": jql},
                                         ensure_ascii=False),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[:400]
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    line = next(l for l in cfg.splitlines() if l.strip().startswith("default_jql:"))
    assert line.strip().startswith("default_jql: '"), \
        f"JQL с кавычками записан без защиты — читатели его не разберут: {line.strip()}"
    assert "''BA-SA Task''" in line, "одинарные кавычки внутри значения не удвоены"
    # round-trip всеми читателями: setup (pre-fill формы) и движковый scalar (sync:jira)
    sys.path.insert(0, str(SCRIPTS))
    import aurora_setup, sources_core
    got = aurora_setup.read_config(root / "aurora.config.yaml")["jira_jql"]
    assert got == jql, f"setup читает другое значение: {got!r}"
    jblock = sources_core.block(cfg, "jira:", "auth:")
    assert sources_core.scalar(jblock, "default_jql") == jql, \
        "sources_core.py не читает JQL — sync:jira уйдёт на запрос по умолчанию"
    # простые значения пишутся и читаются по-прежнему (обратная совместимость)
    assert 'project_key: "PRJ"' in cfg, "обычное значение потеряло двойные кавычки"
    assert aurora_setup.yaml_scalar('    x: "обычное"', "x") == "обычное"
    assert aurora_setup.yaml_scalar("    x: голое", "x") == "голое"
    # строка, испорченная записью до 1.113.2, читается снятием крайних кавычек:
    # иначе введённый раньше JQL пришлось бы набирать заново
    legacy = f'    default_jql: "{jql}"'
    assert aurora_setup.yaml_scalar(legacy, "default_jql") == jql, \
        "старый испорченный конфиг не воскрес"
    assert sources_core.scalar(legacy, "default_jql") == jql, \
        "sources_core.py не читает старый испорченный конфиг"
    # панель: сервер разбирает конфиг тем же правилом и отдаёт форме готовые значения
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    assert ck.config_value(cfg, "default_jql") == jql, \
        "сервер панели читает JQL иначе — в форме снова будет пусто"
    vals = ck.config_values(cfg)
    assert vals["default_jql"] == jql and vals["project_key"] == "PRJ", \
        f"форма получает не те значения: {vals}"
    ui = panel_sources()
    assert "cfgRaw.values" in ui, "панель снова разбирает конфиг своей копией правила"
    assert "(?:'((?:[^'" not in ui, "в панели снова своя копия правила разбора конфига"
    # reports/analyst/paths.py зовёт то же правило — проверяется так же
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("analyst_paths", KIT / "reports/analyst/paths.py")
    apaths = _ilu.module_from_spec(spec)
    spec.loader.exec_module(apaths)
    assert apaths.scalar(legacy, "default_jql") == jql, \
        "отчёт не читает старый испорченный конфиг"
    new_line = "    default_jql: " + aurora_setup.yaml_str(jql)
    assert apaths.scalar(new_line, "default_jql") == jql, \
        "отчёт не читает JQL в одинарных кавычках"
    # панель — такой же читатель: обе формы записи плюс наследие
    assert ck.config_value(legacy, "default_jql") == jql, \
        "панель не читает старый испорченный конфиг"
    assert ck.config_value(new_line, "default_jql") == jql, \
        "панель не читает JQL в одинарных кавычках"


@test
def test_cockpit_reads_token_state_and_writes_config(tmp: Path):
    """Панель: «токен заполнен» не должно быть ложью, а правка конфига — с резервной копией."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "T"\n  slug: T\n', encoding="utf-8")
    # пустое значение + непустая строка ниже: типичная форма файла-образца
    (root / ".env.aurora.local").write_text(
        "CONFLUENCE_PERSONAL_TOKEN=abc123\nJIRA_PERSONAL_TOKEN=\n\n# Синоним:\n# JIRA_PAT=\n",
        encoding="utf-8")
    card_ = ck.project_card(str(root))
    assert card_["confluence_token"] is True, "заполненный токен не распознан"
    assert card_["jira_token"] is False, \
        "пустой токен показан как заполненный — панель успокаивает вместо предупреждения"


@test
def test_new_project_works_without_a_terminal(tmp: Path):
    """`aurora.py new` не требует человека у клавиатуры.

    Настройка спрашивает ответы через `input()`, и при запуске из скрипта, панели или
    ассистента команда падала на первом же вопросе с `EOFError`, оставляя развёрнутую, но
    ненастроенную папку. Найдено прогоном сценария регрессии.
    """
    target = tmp / "auto-project"
    cp = subprocess.run([sys.executable, str(KIT / "aurora.py"), "new", str(target),
                         "--name", "Auto", "--slug", "Auto"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    assert cp.returncode == 0, f"new упал без терминала:\n{cp.stdout[-800:]}\n{cp.stderr[-400:]}"
    assert "EOFError" not in cp.stderr, cp.stderr[-300:]
    assert (target / "aurora.config.yaml").is_file(), "конфиг не создан"
    assert (target / "AuroraKnowledgeDB").is_dir(), "структура не развёрнута"
    assert (target / ".aurora/scripts/kb_lint.py").is_file(), "движок не разложен"
    assert "aurora.py setup" in cp.stdout, \
        "не сказано, как довести настройку до конца"

    # проект пригоден к работе сразу: команды не падают на пустой базе
    for args in (["kb_lint.py", "--summary"], ["build_plan.py", "--status"],
                 ["aurora_stats.py"], ["kb_trust.py"]):
        r = subprocess.run([sys.executable, str(target / ".aurora/scripts" / args[0]),
                            *args[1:]], cwd=str(target), capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, f"{args[0]} на свежем проекте: rc={r.returncode}\n{r.stderr[:300]}"


@test
def test_update_works_from_project_copy(tmp: Path):
    """Копия update внутри проекта должна находить kit, а не падать на манифесте.

    Панель запускала именно её — и получала трассировку вместо предпросмотра
    обновления: скрипт считал корнем kit'а сам проект.
    """
    root = make_project(tmp)
    (root / ".aurora/scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPTS / "aurora_update.py", root / ".aurora/scripts/aurora_update.py")
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "T"\n  slug: T\n', encoding="utf-8")

    # без подсказки — понятная ошибка, а не стек
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_update.py"), "."],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 2, f"ожидался управляемый отказ, а не {cp.returncode}"
    assert "Traceback" not in cp.stderr, "человек получает трассировку вместо объяснения"
    assert "kit_path.txt" in cp.stderr, "не сказано, как починить"

    # с подсказкой — обычная работа
    (root / ".aurora/kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_update.py"), "."],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, f"с подсказкой обновление должно работать:\n{cp.stderr[:400]}"
    assert "kit " in cp.stdout, "не показана версия kit'а"


@test
def test_kit_ships_no_project_data(tmp: Path):
    """В поставку не уезжают папки проекта.

    Команды движка рассчитаны на проект и заводят его папки там, откуда их запустили —
    в том числе внутри самого кита. Так в git попали пустая трассировка и целый
    `scripts/AuroraKnowledgeDB/`. Кит — не проект: кроме синтетического корпуса тестов,
    ни базы знаний, ни зеркал, ни артефактов в нём быть не должно.
    """
    # -z: пути через NUL и без экранирования — иначе кириллица приезжает в кавычках
    # и проверка «начинается с tests/corpus/» промахивается на каждом втором файле
    tracked = [p for p in subprocess.run(["git", "ls-files", "-z"], cwd=str(KIT),
                                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split("\0") if p]
    project_dirs = ("AuroraKnowledgeDB/", "Sources/", "Raw/", "Artifacts/",
                    "Deliverables/", "Workspaces/", ".aurora/")
    stray = [p for p in tracked
             if not p.startswith(("tests/corpus/", "scaffold/", "templates/", "examples/"))
             and any(d in p for d in project_dirs)]
    assert not stray, ("в поставку попали файлы проекта:\n    " + "\n    ".join(stray[:10])
                       + "\n  Кит — не проект: уберите из git (`git rm --cached`)")
    assert not (KIT / "aurora.config.yaml").exists(), \
        "в ките лежит конфиг проекта — команды будут считать кит проектом"


@test
def test_a_private_name_is_caught_in_every_form_it_is_written(tmp: Path):
    """Основа в списке ловит склонения, целое слово — только себя.

    Русское название заказчика склоняется, и в списке оно стоит основой. Проверка же
    требовала границу слова справа — и основа не ловила ни одного склонения. Название
    заказчика прошло прогон зелёным и было снято глазами перед самой отправкой; тот же
    пробел был и в push-хуке, то есть последней сетки под ногами не было вовсе.

    Граница справа осталась там, где она и нужна: короткое название вроде трёхбуквенного
    без неё ловило бы половину живого текста. Отличает одно от другого не догадка
    проверки, а звёздочка — её ставит тот, кто ведёт список.
    """
    rx = term_regex(["Примор*", "ЛТК"])
    for probe, why_not in (("работа в Приморье", "склонение основы не поймано"),
                           ("приморский узел", "производное от основы не поймано"),
                           ("ПРИМОР", "сама основа не поймана"),
                           ("проект ЛТК идёт", "целое название не поймано")):
        assert rx.search(probe), f"{why_not}: {probe!r}"
    for probe, why_not in (("живой проект", "поймана обычная фраза"),
                           ("лткань", "целое название поймано внутри другого слова")):
        assert not rx.search(probe), f"{why_not}: {probe!r}"

    # хук перед отправкой обязан судить так же: иначе зелёный прогон и красный push
    hook = (SCRIPTS / "aurora_hooks.py").read_text(encoding="utf-8")
    assert 't.rstrip("*")' in hook and 'if t.endswith("*")' in hook, \
        "push-хук не понимает основу — проверка и последняя сетка разойдутся"


@test
def test_no_private_terms_in_tracked_files(tmp: Path):
    """Наружу уходит только обезличенное.

    Список внутренних названий лежит в `local/private_terms.txt` — вне git, потому что
    перечень внутренних имён сам является внутренним именем. Нет файла — нет и проверки:
    у стороннего разработчика приватного списка быть не должно.
    """
    terms_file = KIT / "local/private_terms.txt"
    if not terms_file.is_file():
        return
    terms = [l.strip() for l in terms_file.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    if not terms:
        return
    # Отслеживаемые ПЛЮС ещё не добавленные, но и не игнорируемые. Новый файл до первого
    # `git add` проверкой не виден — и уезжает в коммит с внутренними названиями внутри.
    # Так и вышло: `kb_translit.py` прошёл прогон зелёным, а хук отклонил push.
    tracked = subprocess.run(["git", "ls-files"], cwd=str(KIT),
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
    fresh = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                           cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
    tracked = list(dict.fromkeys(tracked + fresh))
    rx = term_regex(terms)
    hits = []
    for rel in tracked:
        path = KIT / rel
        if not path.is_file() or path.suffix not in (".md", ".py", ".txt", ".json",
                                                     ".yaml", ".yml", ".html"):
            continue
        if rel.startswith(("tests/corpus/",   # корпус синтетический, домена в нём нет
                           "cockpit/vendor/")):  # чужой код: мы его не пишем и не правим
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            m = rx.search(line)
            if m and "test_structure_spots" not in line:
                hits.append(f"{rel}:{n} — «{m.group(0)}»")
    assert not hits, ("внутренние названия попали в отслеживаемые файлы:\n    "
                      + "\n    ".join(hits[:15])
                      + "\n  Обезличьте текст, а привязку к проекту держите в local/")


@test
def test_no_private_terms_in_commit_messages(tmp: Path):
    """Сообщение коммита — тоже поставка, и оно не чинится линтером.

    Файлы можно переписать и закоммитить заново, а текст коммита уходит в историю
    навсегда: чтобы его убрать, нужно переписывать ветку и делать force-push. На живой
    работе внутренние названия попали в сообщение ровно при правке файла с этими
    названиями — проверка файлов такое не ловит по определению.
    """
    terms_file = KIT / "local/private_terms.txt"
    if not terms_file.is_file():
        return
    terms = [l.strip() for l in terms_file.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    if not terms:
        return
    log = subprocess.run(["git", "log", "--format=%H%x00%B%x01"], cwd=str(KIT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    rx = term_regex(terms)
    hits = []
    for entry in log.split("\x01"):
        if "\x00" not in entry:
            continue
        sha, body = entry.split("\x00", 1)
        m = rx.search(body)
        if m:
            hits.append(f"{sha.strip()[:8]} — «{m.group(0)}»")
    assert not hits, ("внутренние названия в сообщениях коммитов:\n    "
                      + "\n    ".join(hits[:10])
                      + "\n  Историю придётся переписывать: git rebase -i / filter-branch."
                      + "\n  Чтобы это не повторялось: kit:hooks --install (хук commit-msg)")


@test
def test_hooks_guard_commit_messages(tmp: Path):
    """`kit:hooks` ставит и проверку сообщений, а не только линтер файлов."""
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(root), check=True)
    (root / "local/private_terms.txt").write_text("ВНУТРЕННЕЕИМЯ\n", encoding="utf-8")
    # признак кита: манифест движка в корне и никакого конфига проекта
    (root / "engine_manifest.txt").write_text("# manifest\n", encoding="utf-8")

    out = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--install"],
                         cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "commit-msg" in out.stdout, f"хук сообщений не поставлен:\n{out.stdout}"
    assert (root / ".git/hooks/commit-msg").is_file()

    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    ok = subprocess.run(["git", "commit", "-m", "обычная правка"], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert ok.returncode == 0, f"чистое сообщение не прошло:\n{ok.stderr}"

    (root / "f.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    # Под локалью C: так на Linux и в проверке GitHub. Прежний хук искал grep'ом с
    # кириллицей в шаблоне — там он молча ничего не находил, и коммит проходил.
    bad = subprocess.run(["git", "commit", "-m", "правка про ВНУТРЕННЕЕИМЯ"], cwd=str(root),
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    assert bad.returncode != 0, "коммит с внутренним названием в сообщении прошёл"
    assert "внутренние названия" in bad.stderr, bad.stderr[:300]
    n = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    assert n == "1", f"коммит всё-таки создан (их {n})"


@test
def test_privacy_hook_is_kit_only(tmp: Path):
    """Проверка приватности защищает публикацию движка и не лезет в проекты.

    Кит уезжает в открытый git — там внутреннее название утечка. В проекте те же слова
    и есть предметная область, ради которой он заведён: останавливать такой коммит не за
    что. Признак кита однозначен — манифест движка в корне и никакого конфига проекта.
    """
    root = tmp / "project"
    (root / "local").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(root), check=True)
    (root / "aurora.config.yaml").write_text('project:\n  name: "T"\n', encoding="utf-8")
    # список внутренних названий есть даже здесь — и всё равно не должен ничего блокировать
    (root / "local/private_terms.txt").write_text("ВНУТРЕННЕЕИМЯ\n", encoding="utf-8")

    out = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--install"],
                         cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "pre-commit" in out.stdout, f"линтер-хук не поставлен:\n{out.stdout}"
    assert not (root / ".git/hooks/commit-msg").exists(), \
        "в проект поставлен хук приватности — он про публикацию кита, а не про работу"

    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    cp = subprocess.run(["git", "commit", "-m", "правка про ВНУТРЕННЕЕИМЯ"], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, f"коммит в проекте остановлен зря:\n{cp.stderr}"

    st = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--status"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "это проект, а не кит" in st, f"статус не объясняет, почему хука нет:\n{st}"


@test
def test_only_neutral_hosts_in_tracked_files(tmp: Path):
    """Адреса и почта в поставке — только заведомо ничейные.

    Приватный список ловит ровно то, что в него записали, и на живом примере это
    подвело: домен ведомства в примере отчёта туда никто не вносил — это же не название
    проекта. Домен — признак сам по себе: любой хост вне белого списка означает, что
    в текст просочился чей-то настоящий контур. Проверка работает без local/ —
    у стороннего разработчика она тоже сработает.
    """
    # Публичная инфраструктура — не «чужой контур». Правило написано против адресов
    # заказчика, утёкших в открытую поставку; CDN с библиотекой графиков и сайты
    # стандартов таким адресом не являются. Сайт лицензии шрифтов (1.121.0,
    # cockpit/vendor/fonts/OFL.txt) — тоже: текст лицензии правке не подлежит. NEUTRAL-HOSTS-ALLOW
    allow = {"example.com", "example.ru", "example.org", "example", "localhost",
             "127.0.0.1", "github.com", "www.apache.org", "www.python.org",
             "schemas.openxmlformats.org", "cdn.jsdelivr.net", "openfontlicense.org",
             "raw.githubusercontent.com",   # обновление кита читает VERSION с GitHub (1.124.0)
             "pypi.org",   # надстройки движка сверяют версию, которую поставит pip (1.138.0)
             "bitbucket.org"}   # облачный Bitbucket — сервер модуля провайдера Git (1.154.0)
    def ok(host: str) -> bool:
        h = host.lower().rstrip(".")
        if h in allow or any(h.endswith("." + a) for a in allow):
            return True
        return "." not in h                       # https://c и подобные фикстуры без домена
    rx = re.compile(r"https?://([A-Za-z0-9.-]+)|[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    tracked = subprocess.run(["git", "ls-files"], cwd=str(KIT),
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
    hits = []
    for rel in tracked:
        path = KIT / rel
        if not path.is_file() or path.suffix not in (".md", ".py", ".txt", ".json",
                                                     ".yaml", ".yml", ".html"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if "NEUTRAL-HOSTS-ALLOW" in line:     # строки самой проверки
                continue
            for m in rx.finditer(line):
                host = m.group(1) or m.group(2)
                if not ok(host):
                    hits.append(f"{rel}:{n} — {host}")
    assert not hits, ("в поставку попали чужие адреса:\n    " + "\n    ".join(hits[:15])
                      + "\n  Замените на example.com/example.ru — примеры не должны "
                        "указывать на настоящий контур")


@test
def test_golden_corpus_numbers_hold(tmp: Path):
    """Золотой корпус: срез базы с патологиями, пойманными на живых проектах.

    Здесь проверяется не «скрипт делает, что задумано» (это остальные тесты), а
    «движок видит в реальных формах ровно то же, что вчера». Числа записаны в
    EXPECTED.json; разошлось — значит поведение изменилось, и это надо объяснить.
    """
    corpus = KIT / "tests/corpus/project"
    assert corpus.is_dir(), "корпуса нет — соберите: python3 tests/make_corpus.py"
    root = tmp / "corpus"
    shutil.copytree(corpus, root)
    expected = json.loads((KIT / "tests/corpus/EXPECTED.json").read_text(encoding="utf-8"))

    def num(text, pattern):
        m = re.search(pattern, text)
        return int(m.group(1)) if m else None

    got = {}
    lint = run("kb_lint.py", "--summary", cwd=root).stdout
    got["карточек"] = num(lint, r"карточек (\d+)")
    got["ошибок линтера"] = num(lint, r"ошибок (\d+)")
    got["групп двойников"] = num(run("kb_fix.py", "--dupes", cwd=root).stdout,
                                 r"Двойники: групп (\d+)")
    got["гомоглифов к починке"] = num(run("kb_fix.py", "--homoglyphs", cwd=root).stdout,
                                      r"смешанным скриптом: (\d+)")
    sc = run("kb_schema.py", cwd=root).stdout
    got["схема: к переводу"] = num(sc, r"К переводу: (\d+)")
    got["схема: v1"] = num(sc, r"v1: (\d+)")
    got["схема: v2"] = num(sc, r"v2: (\d+)")
    scr = run("kb_scrub.py", cwd=root).stdout
    got["ПДн: находок"] = num(scr, r"находок (\d+)")
    got["ПДн: рабочих контактов"] = num(scr, r"рабочий контакт: (\d+)")
    cl = run("kb_lint.py", cwd=root, expect_rc=None).stdout
    got["артефактов в знаниях"] = num(cl, r"артефакты, попавшие в базу знаний: (\d+)")
    got["без типа"] = num(cl, r"карточки без типа: (\d+)")
    au = run("sync_audit.py", cwd=root).stdout
    got["зеркало: missing"] = num(au, r"MISSING: \*\*(\d+)\*\*")
    got["зеркало: orphan"] = num(au, r"ORPHAN: \*\*(\d+)\*\*")
    js = run("jira_status.py", cwd=root).stdout
    got["историй совпало"] = num(js, r"задачи: (\d+) совпали")
    got["историй без задач"] = num(js, r"Истории без задачи в Jira: (\d+)")
    got["задач без историй"] = num(js, r"которой нет в `Artifacts/us/`: \*?\*?(\d+)")
    raw = run("aurora_stats.py", "--json", cwd=root).stdout
    st = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    for label, key in (("сирот", "orphans_count"), ("протухших", "expired_count"),
                       ("без владельца", "no_owner_count"),
                       ("битых источников", "missing_source_count"),
                       ("проверенных", "trusted")):
        got[label] = st[key]

    drift = [f"{k}: было {expected[k]} → стало {got[k]}"
             for k in sorted(expected) if expected.get(k) != got.get(k)]
    assert not drift, ("движок стал видеть корпус иначе:\n    " + "\n    ".join(drift) +
                       "\n  Если изменение осознанное — обновите tests/corpus/EXPECTED.json")


@test
def test_schema_version_migrates_by_chain(tmp: Path):
    """Миграция схемы проверяема: видно, что было, что стало и что осталось."""
    root = make_project(tmp, git=True)
    (root / "AuroraKnowledgeDB/Glossary/Легаси.md").write_text(
        '---\ntitle: "Легаси"\n---\n\nтело\n', encoding="utf-8")
    card(root, "Systems/Старая.md", "тело", status="canonical", type="system",
         audience="[SA]")
    (root / "AuroraKnowledgeDB/Systems/Свежая.md").write_text(
        '---\ntitle: "Свежая"\nstatus: verified\ntype: system\nschema_version: 3\n---\n\nтело\n',
        encoding="utf-8")

    cp = run("kb_schema.py", cwd=root)
    assert "v1: 1" in cp.stdout and "К переводу: 3" in cp.stdout, \
        f"версии карточек посчитаны неверно:\n{cp.stdout}"
    assert "v3: убраны audience" in cp.stdout, "не сказано, что именно сделает ступень"
    assert "v4: убрано trust" in cp.stdout, "ступень с выводом trust не объявлена"

    run("kb_schema.py", "--apply", "--allow-dirty", cwd=root)
    legacy = (root / "AuroraKnowledgeDB/Глоссарий" if False else
              root / "AuroraKnowledgeDB/Glossary/Легаси.md").read_text(encoding="utf-8")
    assert "status: imported" in legacy and "type: glossary" in legacy, "ступень v2 не отработала"
    assert "schema_version: 6" in legacy, "версия схемы не проставлена"
    assert "trust:" not in legacy, "поле trust осталось после миграции"
    old = (root / "AuroraKnowledgeDB/Systems/Старая.md").read_text(encoding="utf-8")
    assert "audience" not in old and "status: verified" in old, "ступень v3 не отработала"
    assert "тело" in old, "тело карточки пострадало при миграции"

    cp = run("kb_schema.py", cwd=root)
    assert "Вся база на текущей версии схемы" in cp.stdout, "миграция не идемпотентна"
