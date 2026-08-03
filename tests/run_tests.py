#!/usr/bin/env python3
"""run_tests.py — регрессионные тесты движка Aurora на синтетическом проекте.

Зачем: скрипты движка пишут разом в сотни файлов живой базы. Дефект «два файла
переименовались в одно имя, карточка молча затёрта» был найден только ручной сверкой
счётчиков — такие вещи должен ловить прогон, а не человек.

  python3 tests/run_tests.py           # прогнать всё
  python3 tests/run_tests.py -v        # с выводом команд

Каждый тест строит проект с нуля во временной папке (структура — из structure_dirs.txt),
кладёт заранее сломанные карточки и проверяет поведение скриптов. Живые проекты не
используются. Выход: 0 — все тесты прошли, 1 — есть падения.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
SCRIPTS = KIT / "scripts"
VERBOSE = "-v" in sys.argv
RESULTS: list = []


# ------------------------------------------------------------------ каркас

def run(script: str, *args, cwd: Path, expect_rc=None) -> subprocess.CompletedProcess:
    cp = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                        cwd=str(cwd), capture_output=True, text=True)
    if VERBOSE:
        print(f"    $ {script} {' '.join(args)} → rc={cp.returncode}")
        print("      " + "\n      ".join(cp.stdout.strip().splitlines()[:12]))
    if expect_rc is not None and cp.returncode != expect_rc:
        raise AssertionError(f"{script} {' '.join(args)}: rc={cp.returncode}, ждали {expect_rc}\n"
                             f"{cp.stdout}\n{cp.stderr}")
    return cp


def make_project(tmp: Path, git: bool = False) -> Path:
    """Пустой проект со стандартной структурой (из structure_dirs.txt) и движком."""
    root = tmp / "project"
    root.mkdir()
    for line in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            (root / line).mkdir(parents=True, exist_ok=True)
    (root / ".opencode" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(KIT / "structure_dirs.txt", root / ".opencode" / "structure_dirs.txt")
    for s in SCRIPTS.glob("*.py"):
        shutil.copy(s, root / ".opencode" / "scripts" / s.name)
    # модули источников: манифесты и папки их зеркал (в проекте это делает install/update)
    (root / ".opencode" / "connectors").mkdir(parents=True, exist_ok=True)
    for man in (KIT / "connectors").glob("*/connector.json"):
        m = json.loads(man.read_text(encoding="utf-8"))
        shutil.copy(man, root / ".opencode" / "connectors" / f"{m['id']}.json")
        (root / m["mirror"]["default_path"]).mkdir(parents=True, exist_ok=True)
    (root / ".opencode" / "skills" / "aurora-vault").mkdir(parents=True, exist_ok=True)
    (root / ".opencode" / "skills" / "aurora-vault" / "SKILL.md").write_text("stub", encoding="utf-8")
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Test"\n  slug: "Test"\natlassian:\n  confluence:\n'
        '    space: "T"\n  jira:\n    project_key: "T"\n', encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=str(root), check=True)
    return root


def card(root: Path, rel: str, body: str = "", **fm) -> Path:
    p = root / "AuroraKnowledgeDB" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(f"{k}: {v}\n" for k, v in fm.items())
    p.write_text(f"---\ntitle: \"{p.stem}\"\n{head}---\n\n# {p.stem}\n\n{body}\n", encoding="utf-8")
    return p


def count_cards(root: Path) -> int:
    return len(list((root / "AuroraKnowledgeDB").rglob("*.md")))


def test(fn):
    name = fn.__name__.replace("test_", "").replace("_", " ")
    with tempfile.TemporaryDirectory() as td:
        try:
            fn(Path(td))
            RESULTS.append((name, None))
            print(f"  ✅ {name}")
        except AssertionError as e:
            RESULTS.append((name, str(e)))
            first = (str(e).splitlines() or ["(без пояснения — добавьте текст в assert)"])[0]
            print(f"  ❌ {name}\n     {first}")
        except Exception as e:  # noqa: BLE001
            RESULTS.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ❌ {name} — {type(e).__name__}: {e}")
    return fn


# ------------------------------------------------------------------- тесты

def _write_minimal_docx(path: Path) -> None:
    """Минимальный настоящий .docx (zip с document.xml) — без внешних зависимостей."""
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>'
           f'<w:p><w:r><w:t>Тестовый абзац</w:t></w:r></w:p></w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)


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
    cp = run("kb_queue.py", "--limit", "10", cwd=root)
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
    assert 'source: "Sources/Confluence/Новый/Путь/Страница.md"' in moved, \
        f"source не перенацелен:\n{moved}"
    orphan = (root / "AuroraKnowledgeDB/Concepts/Осиротевшее.md").read_text(encoding="utf-8")
    assert "Ушедшая.md" in orphan, "источник исчезнувшей страницы не должен подменяться наугад"
    assert "не сопоставлено: 1" in cp.stdout, f"пропавшая страница не попала в отчёт:\n{cp.stdout}"


@test
def test_classify_finds_artifacts_but_not_domain_codes(tmp: Path):
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

    cp = run("kb_classify.py", "--limit", "20", cwd=root, expect_rc=1)
    assert "ALG-095" not in cp.stdout, "код алгоритма ошибочно принят за артефакт"
    assert "REQ-042" not in cp.stdout, "требование ошибочно принято за артефакт"
    for name in ("US-3.1.11", "AC-4.2.12", "PROJ-1234"):
        assert name in cp.stdout, f"артефакт {name} не найден"
    assert "артефактов в знаниях: **3**" in cp.stdout, cp.stdout.splitlines()[2]

    run("kb_classify.py", "--fix-type", "--apply", cwd=root)
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
    assert "[verified | проверено 2026-01-01" in cp.stdout, "нет шапки доверия"
    assert "Черновик-Заявка" not in cp.stdout, "draft попал в generate-пак"
    assert "Проверка-Заявка" in cp.stdout, "связанная карточка не подтянулась переходом"
    usage = (root / "AuroraKnowledgeDB/meta/usage.log").read_text(encoding="utf-8")
    assert "Заявка" in usage, "употребление не записано в usage.log"

    cp2 = run("ctx_pack.py", "Заявка", "--mode", "evaluate", "--no-log", cwd=root)
    assert "Черновик-Заявка" in cp2.stdout, "в evaluate черновик обязан быть"
    assert "НЕ ПРОВЕРЕНО ЧЕЛОВЕКОМ" in cp2.stdout, "у черновика нет предупреждающей шапки"


@test
def test_verify_batch_checks_before_promoting(tmp: Path):
    root = make_project(tmp)
    card(root, "Glossary/Годная.md", "текст", status="imported", source='"Raw/project/x.md"')
    card(root, "Glossary/Без-источника.md", "текст", status="imported")
    card(root, "Glossary/С-битой-ссылкой.md", "[[Нет-такой]]", status="imported",
         source='"Raw/project/x.md"')

    cp = run("kb_verify.py", "Glossary", "--owner", "@vadim", "--apply", cwd=root)
    good = (root / "AuroraKnowledgeDB/Glossary/Годная.md").read_text(encoding="utf-8")
    assert "status: verified" in good and 'owner: "@vadim"' in good, "карточка не верифицирована"
    assert "review_by:" in good, "не проставлен срок годности"
    for bad in ("Без-источника", "С-битой-ссылкой"):
        text = (root / f"AuroraKnowledgeDB/Glossary/{bad}.md").read_text(encoding="utf-8")
        assert "status: imported" in text, f"{bad} не должна была пройти гейт"
    assert "нет source" in cp.stdout and "битые ссылки" in cp.stdout, "причины пропуска не названы"

    # `canonical` из схемы убран (1.10.0): верхний статус базы — verified
    rc = run("kb_verify.py", "Glossary", "--owner", "@v", "--status", "canonical",
             cwd=root, expect_rc=2)
    assert "canonical" in rc.stderr and "invalid choice" in rc.stderr, \
        "ступень canonical должна быть недоступна"


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

    cp = run("kb_impact.py", "Шина", cwd=root)
    assert "Сданные заказчику документы" in cp.stdout, "сданный документ не выделен"
    assert "ОПЗ_v1_2026-01-01" in cp.stdout, "документ не найден по based_on"

    cp2 = run("kb_impact.py", "--explain",
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

    cp = run("sync_diff.py", "--stamp", "--apply", cwd=root)
    assert "Проставлено: 1" in cp.stdout, f"хеш источника не зафиксирован:\n{cp.stdout}"
    cp = run("sync_diff.py", cwd=root, expect_rc=0)
    assert "**дрейф**" in cp.stdout and "дрейф» (источник" not in cp.stdout

    src.write_text("источник изменился", encoding="utf-8")
    cp = run("sync_diff.py", cwd=root, expect_rc=1)
    assert "Дрейф — перепроверить" in cp.stdout, "изменение источника не поймано"
    assert "Знание" in cp.stdout, "карточка не названа"

    src.unlink()
    cp = run("sync_diff.py", cwd=root)
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

    cp = run("release_doc.py", "Deliverables/work/ОПЗ_v2.1.md", "--date", "2026-05-05",
             "--apply", cwd=root)
    snap = root / "Deliverables/released/ОПЗ_v2.1_2026-05-05.md"
    assert snap.is_file(), f"снапшот не создан:\n{cp.stdout}"
    assert "released: 2026-05-05" in snap.read_text(encoding="utf-8"), "нет даты передачи"
    assert "released: 2026-05-05" in work.read_text(encoding="utf-8"), "рабочая копия не помечена"
    assert "Ниже verified: 1" in cp.stdout, "риск непроверенного основания не назван"

    cp2 = run("release_doc.py", "Deliverables/work/ОПЗ_v2.1.md", "--date", "2026-05-05",
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
    card(root, "Systems/Шина-R2.md", "", status="imported", applies_to="[R2]")

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
    card(root, "Systems/Шина.md", "Kafka по VPN", status="verified", owner='"@sa"',
         verified="2026-01-01", review_by="2030-01-01")
    card(root, "Systems/Черновик.md", "набросок", status="draft")
    card(root, "Decisions/DR-0007-Шина.md", "Выбрали Kafka", type="decision", status="accepted")

    cp = run("spec_pack.py", "SPEC-012", "--apply", cwd=root)
    assert "REQ-042-Обмен-с-смежная система (stated)" in cp.stdout, \
        f"ссылка по идентификатору не разрезолвлена — DoR молчит:\n{cp.stdout}"
    assert "Черновик (draft)" in cp.stdout, "основание ниже verified не названо"

    pack = (root / "Deliverables/work/spec-packs/SPEC-012_v1.2.md").read_text(encoding="utf-8")
    assert "## Риски передачи" in pack, "риски не попали в бандл"
    assert "Kafka по VPN" in pack and "Выбрали Kafka" in pack, "тела оснований не приложены"
    assert "[verified | проверено 2026-01-01" in pack, "нет шапки доверия у основания"
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

    run("kb_index.py", "--apply", cwd=root)
    idx = (root / "AuroraKnowledgeDB/Glossary/_index.md").read_text(encoding="utf-8")
    assert "[[Заявка]]" in idx and "[[ЕНС]]" in idx, "карточки не попали в индекс"
    assert idx.index("[[Заявка]]") < idx.index("[[ЕНС]]"), "verified должен идти раньше imported"
    assert "Документ о предстоящей поставке товаров" in idx, "нет описания карточки"

    hand = (root / "AuroraKnowledgeDB/Systems/_index.md").read_text(encoding="utf-8")
    assert hand == "# Мой рукотворный индекс\n", "рукотворный индекс затёрт без спроса"
    run("kb_index.py", "--apply", "--force", cwd=root)
    assert "[[Шина]]" in (root / "AuroraKnowledgeDB/Systems/_index.md").read_text(encoding="utf-8")


@test
def test_trace_links_only_to_existing_registry(tmp: Path):
    """Ссылка на реестр договорных документов ставится, только если он есть в базе."""
    root = make_project(tmp)
    run("aurora_trace.py", cwd=root)
    out = (root / "AuroraKnowledgeDB/MOC/Трассировка-требований.md").read_text(encoding="utf-8")
    assert "[[contract_documents]]" not in out, \
        "генератор ставит ссылку на карточку, которой в проекте нет — линтер ловит битую"

    card(root, "Reference/contract_documents.md", "реестр", status="imported")
    run("aurora_trace.py", cwd=root)
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

    card(root, "Glossary/Без-типа.md", "тело", status="imported")

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
        for st in s["steps"]:
            assert st["why"], f"шаг без объяснения в сценарии {s['id']}"
            if not st.get("manual"):
                assert st["cmd"] in known, \
                    f"сценарий {s['id']} зовёт несуществующую команду {st['cmd']}"
            else:
                # шаг без кнопки обязан говорить, что сделать вместо неё
                assert st.get("skill", "").startswith("/aurora-vault"), \
                    f"шаг «{st['title']}» в сценарии {s['id']} не называет команду скилла"
    runnable = {r["cmd"] for r in ck.registry() if r["runnable"]}
    for s in ck.scenarios():
        for st in s["steps"]:
            if not st.get("manual") and st["cmd"] not in runnable:
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
                   capture_output=True, text=True)
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "pageId=https://" not in cfg, "в конфиг попал бессмысленный адрес"

    # форма читает корни любого вида, иначе неразрешённая строка исчезает с экрана
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert 'page_id:\\s*"?([^"\\n]+?)"?' in ui, \
        "панель читает только числовые page_id — нечисловая строка пропадёт из формы"


@test
def test_cockpit_warns_about_unsaved_settings(tmp: Path):
    """Панель обязана предупредить, что уход со страницы потеряет введённое."""
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
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
                        capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr[:400]
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert 'page_id: "111"' in cfg and 'page_id: "222"' in cfg, \
        "несколько корней не записались (второй был вставлен ссылкой)"
    assert 'space: "NEW"' in cfg and "scrub: off" in cfg, "поля формы не доехали до конфига"
    assert cfg.count("page_id:") == 2, "корни задвоились или потерялись"


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
def test_update_works_from_project_copy(tmp: Path):
    """Копия update внутри проекта должна находить kit, а не падать на манифесте.

    Панель запускала именно её — и получала трассировку вместо предпросмотра
    обновления: скрипт считал корнем kit'а сам проект.
    """
    root = make_project(tmp)
    (root / ".opencode/scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPTS / "aurora_update.py", root / ".opencode/scripts/aurora_update.py")
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "T"\n  slug: T\n', encoding="utf-8")

    # без подсказки — понятная ошибка, а не стек
    cp = subprocess.run([sys.executable, str(root / ".opencode/scripts/aurora_update.py"), "."],
                        cwd=str(root), capture_output=True, text=True)
    assert cp.returncode == 2, f"ожидался управляемый отказ, а не {cp.returncode}"
    assert "Traceback" not in cp.stderr, "человек получает трассировку вместо объяснения"
    assert "kit_path.txt" in cp.stderr, "не сказано, как починить"

    # с подсказкой — обычная работа
    (root / ".opencode/kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    cp = subprocess.run([sys.executable, str(root / ".opencode/scripts/aurora_update.py"), "."],
                        cwd=str(root), capture_output=True, text=True)
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
                                         capture_output=True, text=True).stdout.split("\0") if p]
    project_dirs = ("AuroraKnowledgeDB/", "Sources/", "Raw/", "Artifacts/",
                    "Deliverables/", "Workspaces/", ".opencode/")
    stray = [p for p in tracked
             if not p.startswith(("tests/corpus/", "scaffold/", "templates/", "examples/"))
             and any(d in p for d in project_dirs)]
    assert not stray, ("в поставку попали файлы проекта:\n    " + "\n    ".join(stray[:10])
                       + "\n  Кит — не проект: уберите из git (`git rm --cached`)")
    assert not (KIT / "aurora.config.yaml").exists(), \
        "в ките лежит конфиг проекта — команды будут считать кит проектом"


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
    tracked = subprocess.run(["git", "ls-files"], cwd=str(KIT),
                             capture_output=True, text=True).stdout.split()
    rx = re.compile("|".join(re.escape(t) for t in terms), re.I)
    hits = []
    for rel in tracked:
        path = KIT / rel
        if not path.is_file() or path.suffix not in (".md", ".py", ".txt", ".json",
                                                     ".yaml", ".yml", ".html"):
            continue
        if rel.startswith("tests/corpus/"):     # корпус синтетический, домена в нём нет
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            m = rx.search(line)
            if m and "test_structure_spots" not in line:
                hits.append(f"{rel}:{n} — «{m.group(0)}»")
    assert not hits, ("внутренние названия попали в отслеживаемые файлы:\n    "
                      + "\n    ".join(hits[:15])
                      + "\n  Обезличьте текст, а привязку к проекту держите в local/")


@test
def test_only_neutral_hosts_in_tracked_files(tmp: Path):
    """Адреса и почта в поставке — только заведомо ничейные.

    Приватный список ловит ровно то, что в него записали, и на живом примере это
    подвело: домен ведомства в примере отчёта туда никто не вносил — это же не название
    проекта. Домен — признак сам по себе: любой хост вне белого списка означает, что
    в текст просочился чей-то настоящий контур. Проверка работает без local/ —
    у стороннего разработчика она тоже сработает.
    """
    allow = {"example.com", "example.ru", "example.org", "example", "localhost",
             "127.0.0.1", "github.com", "www.apache.org", "www.python.org",
             "schemas.openxmlformats.org"}
    def ok(host: str) -> bool:
        h = host.lower().rstrip(".")
        if h in allow or any(h.endswith("." + a) for a in allow):
            return True
        return "." not in h                       # https://c и подобные фикстуры без домена
    rx = re.compile(r"https?://([A-Za-z0-9.-]+)|[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    tracked = subprocess.run(["git", "ls-files"], cwd=str(KIT),
                             capture_output=True, text=True).stdout.split()
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
    cl = run("kb_classify.py", cwd=root).stdout
    got["артефактов в знаниях"] = num(cl, r"артефактов в знаниях: \*\*(\d+)\*\*")
    got["без типа"] = num(cl, r"без типа: \*\*(\d+)\*\*")
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
    assert "schema_version: 4" in legacy, "версия схемы не проставлена"
    assert "trust:" not in legacy, "поле trust осталось после миграции"
    old = (root / "AuroraKnowledgeDB/Systems/Старая.md").read_text(encoding="utf-8")
    assert "audience" not in old and "status: verified" in old, "ступень v3 не отработала"
    assert "тело" in old, "тело карточки пострадало при миграции"

    cp = run("kb_schema.py", cwd=root)
    assert "Вся база на текущей версии схемы" in cp.stdout, "миграция не идемпотентна"


@test
def test_jira_matches_stories_by_title(tmp: Path):
    """История и задача связываются по названию: одинаковый номер — ещё не связь."""
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"; mirror.mkdir(parents=True, exist_ok=True)
    us = root / "Artifacts/us"; us.mkdir(parents=True, exist_ok=True)

    def issue(name, key, title):
        (mirror / f"{name}.md").write_text(
            f"# {key}: {title}\n\n- **URL:** https://jira.example/browse/{key}\n"
            "- **Type:** Task\n- **Status:** В работе\n", encoding="utf-8")

    def story(uid, title, jira=""):
        link = f"| Ссылка_на_JIRA | [{jira}](https://jira.example/browse/{jira}) |\n" if jira else ""
        (us / f"{uid}._{title.replace(' ', '_')}.md").write_text(
            f"# Задача на разработку истории\n\n| | |\n| --- | --- |\n"
            f"| Название | {uid}. {title} |\n{link}", encoding="utf-8")

    story("US-3.1.1", "Приём Заявка из ГП 3", "PRJ-1")
    issue("t1", "PRJ-1", "US-3.1.1. Приём Заявка из ГП 3")           # совпало
    story("US-3.2.2", "Проверка текстовых полей")
    issue("t2", "PRJ-2", "US-3.2.2. Логирование операций")          # номер тот, имя другое
    story("US-3.3.3", "История без задачи")
    issue("t4", "PRJ-4", "US-9.9.9. Задача без истории")

    cp = run("jira_status.py", cwd=root)
    out = cp.stdout
    assert "Истории и задачи: 1 совпали по названию" in out, f"матч по названию не сработал:\n{out}"
    renamed = out.split("Названия разошлись")[1].split("Истории без задачи")[0]
    assert "US-3.2.2" in renamed and "PRJ-2" in renamed, "расхождение названий не показано"
    assert "US-3.1.1" not in renamed, "совпавшая пара попала в расхождения"
    assert "US-3.3.3" in out.split("Истории без задачи")[1][:200], "история без задачи не найдена"
    assert "US-9.9.9" in out, "задача без истории не найдена"
    # «US-4.4.3» сам похож на ключ задачи: номер истории должен срезаться первым
    import importlib, sys as _s
    _s.path.insert(0, str(SCRIPTS)); js = importlib.import_module("jira_status")
    assert js.norm_title("US-4.4.3. Печатная форма") == js.norm_title("PRJ-470: US-4.4.3. Печатная форма"), \
        "номер истории похож на ключ задачи — он должен срезаться первым"


@test
def test_retire_cleans_templates_too(tmp: Path):
    """Шаблон — источник новых карточек: поле, оставшееся в нём, вернётся в базу."""
    root = make_project(tmp, git=True)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/spec_template.md").write_text(
        '---\ntitle: "Шаблон"\nstatus: draft\naudience: [SA, Dev]\n---\n\nтело\n', encoding="utf-8")
    card(root, "Glossary/Термин.md", "тело", status="imported")

    cp = run("aurora_doctor.py", cwd=root)
    assert "в шаблонах" in cp.stdout, "doctor молчит о выведенных полях в шаблонах"

    run("kb_fix.py", "--retire", "--apply", "--allow-dirty", cwd=root)
    assert "audience" not in (root / "Templates/spec_template.md").read_text(encoding="utf-8"), \
        "kb:retire не почистил шаблон"
    cp = run("aurora_doctor.py", cwd=root)
    assert "в шаблонах" not in cp.stdout, "предупреждение осталось после чистки"


@test
def test_cockpit_ui_version_tracks_kit(tmp: Path):
    """Панель не должна молча отстать от ядра: отставший интерфейс выглядит рабочим."""
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    m = re.search(r'const UI_VERSION = "([^"]+)"', ui)
    assert m, "в панели не объявлена версия UI_VERSION"
    kit = (KIT / "VERSION").read_text(encoding="utf-8").strip()
    ui_v, kit_v = m.group(1), kit
    assert ui_v.split(".")[:2] == kit_v.split(".")[:2], (
        f"панель собрана под {ui_v}, ядро {kit_v} — младшая версия разошлась. "
        "Либо обновите cockpit/ui/index.html под новые команды и метрики, "
        "либо поднимите UI_VERSION осознанно")


@test
def test_cockpit_apply_is_reachable(tmp: Path):
    """Пишущую команду нужно уметь применить: панель без «Применить» умеет только dry-run."""
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert 'id="consoleApply"' in ui, "в консоли нет места для кнопки «Применить»"
    assert "PENDING_APPLY" in ui, "панель не помнит предпросмотр — применять нечего"
    # признак жил в RUN, а RUN пересоздаётся при каждом открытии ящика: кнопка на нём
    # включалась только пока ящик закрыт
    assert "RUN.previewed" not in ui, \
        "«Применить» снова зависит от признака, который сбрасывается при открытии команды"


@test
def test_cockpit_runlog_lives_in_the_project(tmp: Path):
    """Журнал запусков — файл проекта, а не память вкладки.

    «Когда последний раз обновляли зеркала» спрашивает вся команда, а ответ, лежащий в
    localStorage одного браузера, отвечает только одному человеку. Поэтому: файл в
    `.opencode/`, версия ядра и автор в записи, и чтение его панелью обратно.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    root = make_project(tmp)
    ck.write_runlog(str(root), "sync:jira", 0, "sync:jira --force")
    ck.write_runlog(str(root), "kit:doctor", 1, "kit:doctor")
    log = (root / ".opencode/run_log.md")
    assert log.is_file(), "журнал не лёг в проект — команда его не увидит"
    runs = ck.read_runlog(str(root))
    assert set(runs) == {"sync:jira", "kit:doctor"}, runs
    assert runs["kit:doctor"]["rc"] == 1, "код возврата потерян"
    assert runs["sync:jira"]["kit"] == ck.kit_version(), \
        "в записи нет версии ядра — непонятно, на чём команда отработала"
    assert runs["sync:jira"]["who"], "непонятно, кто запускал"

    # повторный запуск заменяет строку, а не копит их: файл едет в git и не должен
    # превращаться в источник конфликтов при слиянии веток
    ck.write_runlog(str(root), "sync:jira", 2, "sync:jira")
    assert ck.read_runlog(str(root))["sync:jira"]["rc"] == 2
    rows = [l for l in log.read_text(encoding="utf-8").splitlines()
            if l.startswith("| sync:jira |")]
    assert len(rows) == 1, f"журнал копит дубли вместо строки на команду: {rows}"

    # панель отдаёт журнал вместе со здоровьем — иначе отметкам у команд неоткуда взяться
    assert "runs" in ck.health(str(root)), "журнал не попадает в /api/health"

    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "function assistantTasks" in ui and "Партия " in ui, \
        "задания ассистенту из консоли нечем забрать в буфер"
    assert "S.health && S.health.runs" in ui, "панель снова читает историю из браузера"
    assert 'if (view==="console"){ renderHistory(); drawTaskButton(); }' in ui, \
        "на входе в «Консоль» не восстанавливаются журнал и кнопка задания"
    assert ui.count("lastRun(") >= 3, \
        "отметка последнего запуска стоит не везде: команды, сценарии, журнал"


@test
def test_cockpit_skins_declare_supported_core(tmp: Path):
    """Скин красит то, чего в панели могло ещё не быть — значит, обязан назвать версию."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    for s in ck.skins():
        head = ck.skin_css(s["id"])[:400]
        assert "for:" in head, f"скин {s['id']} не объявляет версию ядра"
        assert s["for"], f"скин {s['id']}: версия не разобралась"
        assert not s["behind"], \
            f"скин {s['id']} собран под {s['for']}, а ядро {ck.kit_version()}"


@test
def test_cockpit_marks_command_outcome(tmp: Path):
    """Код 1 — это «нашла, что чинить», а не «сломалась»: красить их одинаково — врать.

    doctor с ошибками и аудит с расхождениями отрабатывают штатно и возвращают 1;
    не пустивший git-гейт возвращает 2. Отметка у команды должна их различать.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    m = re.search(r"function rcMark\(rc\)\{(.*?)\n\}", ui, re.S)
    assert m, "нет единой трактовки кода возврата — цвета разъедутся по экранам"
    body = m.group(1)
    assert '"ok"' in body and '"warn"' in body and '"bad"' in body, \
        "исходов по-прежнему два: «успех» и «всё плохо»"
    assert "rc === 1" in body, "код 1 не отделён от настоящего сбоя"
    assert "lastRun(r.cmd)" in ui, "в списке команд не видно итога последнего запуска"


@test
def test_cockpit_can_recount_metrics(tmp: Path):
    """Базу правят не только команды панели — числа нужно уметь пересчитать и вручную."""
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    for btn, page in (("refreshHealth", "«Здоровье»"), ("refreshOverview", "«Мостик»")):
        assert f'id="{btn}"' in ui, f"на странице {page} нет кнопки пересчёта"
        assert f'$("#{btn}").onclick' in ui, f"кнопка пересчёта на {page} ничего не делает"
    assert 'stamp("#healthStamp")' in ui and 'stamp("#overviewStamp")' in ui, \
        "нет отметки времени: по числам не понять, до работы они посчитаны или после"


@test
def test_cockpit_warns_when_project_engine_lags(tmp: Path):
    """Флаги панель берёт из kit'а, а запускает движок проекта — расхождение нужно назвать."""
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "S.project.behind && !r.from_kit" in ui, \
        "ящик команды не предупреждает, что флаги из kit'а, а движок проекта другой"
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"from_kit": script in KIT_SIDE' in srv, \
        "панель не различает скрипты, которые и так запускаются из kit'а"


@test
def test_commands_registry_matches_engine(tmp: Path):
    """Справочник команд не должен расходиться ни с движком, ни с флагами скриптов."""
    root = make_project(tmp)
    run("kit_commands.py", "--check", cwd=root, expect_rc=0)

    cp = run("kit_commands.py", "kb", cwd=root)
    assert "kb:repair" in cp.stdout and "kb:scrub" in cp.stdout
    # блок ровно одной команды: следом в реестре идёт kb:retire — у неё свой набор флагов
    block = cp.stdout.split("kb:repair", 1)[1].split("kb:retire")[0]
    mods = [l.strip() for l in block.splitlines() if l.strip().startswith("--")]
    assert any(l.startswith("--merge") for l in mods), \
        "модификаторы берутся не из --help — иначе список разойдётся с кодом"
    assert not any(l.split()[0] == "--all" for l in mods), \
        "флаг, зашитый в саму команду (kb_fix.py --all), не должен предлагаться повторно"
    # у флага должно быть пояснение: список голых имён ничего не объясняет человеку
    merge = next(l for l in mods if l.startswith("--merge"))
    assert len(merge.split()) > 1, f"флаг без пояснения из --help: «{merge}»"

    out = root / "справка.md"
    run("kit_commands.py", "--md", str(out), cwd=root)
    md = out.read_text(encoding="utf-8")
    assert md.count("| `") > 40, "в справочнике потерялись команды"
    assert "1.9.6" in md and "модель" in md, "нет версии появления или типа исполнителя"


@test
def test_scrub_respects_project_privacy_mode(tmp: Path):
    """Режим — свойство контура: в закрытом репозитории маскировать нечего."""
    root = make_project(tmp, git=True)
    (root / "Artifacts/reports").mkdir(parents=True, exist_ok=True)
    (root / "Artifacts/reports/о.md").write_text("Тел. +7 (999) 123-45-67\n", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nprivacy:\n  scrub: off\n", encoding="utf-8")

    cp = run("kb_scrub.py", cwd=root, expect_rc=0)
    assert "выключен в проекте" in cp.stdout, "режим off не учтён"
    assert "123-45-67" not in cp.stdout
    cp = run("kb_scrub.py", "--force", cwd=root)
    assert "телефон" in cp.stdout, "--force не пробивает выключенный режим"

    cfg.write_text(cfg.read_text(encoding="utf-8").replace("scrub: off", "scrub: mask"),
                   encoding="utf-8")
    cp = run("kb_scrub.py", cwd=root, expect_rc=1)
    assert "телефон" in cp.stdout, "в режиме mask находки должны быть ошибкой"
    cp = run("aurora_doctor.py", cwd=root)
    assert "privacy.scrub = mask" in cp.stdout, "doctor молчит о режиме приватности"


@test
def test_scrub_finds_pii_and_spares_evidence(tmp: Path):
    """ПДн закрываются маркерами, но не в доказательствах и не в деловых реквизитах."""
    root = make_project(tmp, git=True)
    (root / "Artifacts/reports").mkdir(parents=True, exist_ok=True)
    (root / "Artifacts/reports/встреча.md").write_text(
        "Звонил Иванову +7 (999) 123-45-67, почта i.ivanov@example.ru.\n"
        "ИНН организации 7707083893 — деловая ссылка.\n"
        "Случайные 12 цифр 123456789012 не ПДн, а 500100732259 — ИНН физлица.\n",
        encoding="utf-8")
    (root / "Raw/meetings").mkdir(parents=True, exist_ok=True)
    raw = root / "Raw/meetings/транскрипт.md"
    raw.write_text("Петров, 8-999-123-45-67\n", encoding="utf-8")

    cp = run("kb_scrub.py", cwd=root)
    assert "телефон" in cp.stdout and "почта" in cp.stdout, "телефон/почта не найдены"
    assert "ИНН физлица" in cp.stdout, "ИНН физлица с верной контрольной суммой пропущен"
    assert "7707083893" not in cp.stdout, "ИНН организации принят за ПДн"
    assert "123456789012" not in cp.stdout, "случайные 12 цифр приняты за ИНН"
    assert "123-45-67" not in cp.stdout, "отчёт печатает ПДн целиком"

    run("kb_scrub.py", "--apply", "--allow-dirty", cwd=root)
    got = (root / "Artifacts/reports/встреча.md").read_text(encoding="utf-8")
    assert "[ПДн: телефон]" in got and "[ПДн: почта]" in got, "маркеры не проставлены"
    assert "7707083893" in got, "деловой ИНН затёрт"
    assert "123456789012" in got, "затёрто число, не прошедшее контрольную сумму"
    assert raw.read_text(encoding="utf-8") == "Петров, 8-999-123-45-67\n", \
        "Raw/ — неизменяемое доказательство, правка без --include-raw запрещена"


@test
def test_publish_converts_and_guards_layers(tmp: Path):
    root = make_project(tmp)
    card(root, "Systems/Шина.md", "Kafka", status="verified")
    cp = run("publish_doc.py", "AuroraKnowledgeDB/Systems/Шина.md", cwd=root, expect_rc=1)
    assert "не публикуется" in cp.stdout + cp.stderr, "карточка знаний ушла бы наружу"

    import xml.etree.ElementTree as ET
    sys.path.insert(0, str(SCRIPTS))
    import publish_doc as P
    md = ("# Отчёт\n\nТекст **жирный** и `код`, ссылка [[Карточка-А]] и [[Нет-такой]].\n\n"
          "- один\n- два\n  - вложенный\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
          "```python\nx = 1\n```\n")
    st = P.to_storage(md, {"Карточка-А": "https://c/pages/1"})
    # storage обязан быть валидным XHTML; префикс ac: объявляет сама страница
    ET.fromstring('<root xmlns:ac="urn:ac" xmlns:ri="urn:ri">' + st + "</root>")
    assert "<li>два<ul>" in st, "вложенный список вынесен из пункта — Confluence отвергнет body"
    assert 'href="https://c/pages/1"' in st and "«Нет-такой»" in st, \
        "wiki-ссылки не разрешены: опубликованные — ссылкой, остальные — текстом"
    assert 'ac:name="code"' in st and "CDATA[x = 1]" in st, "код не обёрнут в макрос"
    assert st == P.to_storage(md, {"Карточка-А": "https://c/pages/1"}), \
        "конвертация недетерминирована — каждая публикация поднимет версию страницы"

    stamped = P.stamp("---\ntype: report\n---\n\nтело\n", "12345", "abc1234")
    assert "confluence_page_id: 12345" in stamped and "published_commit: abc1234" in stamped
    assert "тело" in stamped, "тело документа потеряно при простановке полей"


@test
def test_structure_spots_ignored_but_tracked(tmp: Path):
    """Правило в .gitignore появилось позже коммита — git отвечает «не игнорируется»,
    и папка выглядит нарушением схемы. Диагноз должен быть про индекс, а не про схему."""
    root = make_project(tmp, git=True)
    (root / "__pycache__").mkdir()
    (root / "__pycache__/x.pyc").write_bytes(b"\x00")
    subprocess.run(["git", "add", "-A", "-f"], cwd=str(root), capture_output=True)
    subprocess.run(["git", "commit", "-m", "мусор"], cwd=str(root), capture_output=True)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")

    cp = run("aurora_doctor.py", "--structure", cwd=root, expect_rc=1)
    assert "лежат в индексе" in cp.stdout, f"не назван настоящий диагноз:\n{cp.stdout}"
    assert "git rm -r --cached __pycache__" in cp.stdout, "нет готовой команды починки"
    bad = [l for l in cp.stdout.splitlines() if "вне схемы движка" in l]
    assert not bad, f"отслеживаемый мусор обвинён в нарушении схемы: {bad}"


@test
def test_doctor_enforces_fixed_structure(tmp: Path):
    root = make_project(tmp)
    (root / "Artifacts" / "мои-схемы").mkdir()
    (root / "СвояПапка").mkdir()
    cp = run("aurora_doctor.py", "--structure", cwd=root)
    assert "СвояПапка" in cp.stdout and "мои-схемы" in cp.stdout, "самодеятельные папки не пойманы"
    assert cp.returncode == 1, "нарушение схемы должно быть ошибкой"


@test
def test_structure_allows_gitignored_folders(tmp: Path):
    """Что закрыто .gitignore — вне схемы допустимо; остальное вне схемы — ошибка."""
    root = make_project(tmp, git=True)
    (root / ".sisyphus").mkdir()
    (root / ".sisyphus/state.json").write_text("{}", encoding="utf-8")
    (root / "СвояПапка").mkdir()
    (root / "СвояПапка/файл.md").write_text("текст", encoding="utf-8")
    (root / ".gitignore").write_text(".sisyphus/\n", encoding="utf-8")

    cp = run("aurora_doctor.py", "--structure", cwd=root, expect_rc=1)
    assert "СвояПапка" in cp.stdout, "папка вне схемы и вне .gitignore не помечена"
    err = [l for l in cp.stdout.splitlines() if l.startswith("ERROR") and "вне схемы" in l]
    assert err and ".sisyphus" not in err[0], f".gitignore-папка попала в ошибки: {err}"
    assert "закрыты .gitignore" in cp.stdout, "нет строки про допущенные gitignore-папки"


@test
def test_doctor_catches_git_case_drift(tmp: Path):
    """macOS прячет расхождение регистра между диском и индексом git — doctor не должен."""
    root = make_project(tmp, git=True)
    card(root, "Glossary/Термин.md")
    (root / "Raw" / "project" / "док.md").write_text("текст", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "content"], cwd=str(root), check=True)
    # имитируем дрейф: в индексе папка становится raw/, на диске остаётся Raw/
    subprocess.run(["git", "mv", "Raw", "raw_tmp"], cwd=str(root), check=True)
    subprocess.run(["git", "mv", "raw_tmp", "raw"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "drift"], cwd=str(root), check=True)
    os.rename(root / "raw", root / "Raw_x")
    os.rename(root / "Raw_x", root / "Raw")
    cp = run("aurora_doctor.py", "--structure", cwd=root)
    assert "регистр папок" in cp.stdout, f"дрейф регистра не пойман:\n{cp.stdout}"


@test
def test_sync_audit_finds_orphans_and_missing(tmp: Path):
    root = make_project(tmp)
    conf = root / "Sources/Confluence"
    (conf / "Раздел").mkdir(parents=True, exist_ok=True)
    (conf / "Раздел/Есть.md").write_text("- **ID:** 111111\n\nтекст", encoding="utf-8")
    (conf / "Раздел/Сирота.md").write_text("- **ID:** 333333\n\nтекст", encoding="utf-8")
    (conf / "sync_state.md").write_text(
        "**Sync Date:** 2026-07-25\n\n| # | Page ID | Title | Local Path | Status |\n"
        "|---|---|---|---|---|\n"
        "| 1 | 111111 | Есть | Раздел/Есть.md | SYNCED |\n"
        "| 2 | 222222 | Пропала | Раздел/Пропала.md | SYNCED |\n", encoding="utf-8")
    cp = run("sync_audit.py", cwd=root, expect_rc=1)
    assert "MISSING: **1**" in cp.stdout, f"не найдено MISSING: {cp.stdout[:400]}"
    assert "ORPHAN: **1**" in cp.stdout, "не найден ORPHAN"


@test
def test_kb_reset_empties_the_base_and_nothing_else(tmp: Path):
    """Сброс обнуляет `AuroraKnowledgeDB/` целиком и не выходит за её пределы.

    «Обнулить» значит обнулить: журнал решений и справочники уходят вместе с карточками,
    откат — из git. Но два файла внутри базы знанием не являются: настройки Obsidian и
    отметка версии движка, по которой панель и `doctor` понимают, что установлено.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Concepts", "Decisions", "Questions", "Reference", "MOC",
                    "_archive", "meta", ".obsidian"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    (kb / "Concepts/Карточка.md").write_text(
        '---\ntitle: "К"\nsource: "Sources/Confluence/a.md"\nstatus: verified\n---\nтекст\n',
        encoding="utf-8")
    (kb / "MOC/Связи.md").write_text("сгенерировано\n", encoding="utf-8")
    (kb / "_archive/Старая.md").write_text("вытесненная карточка\n", encoding="utf-8")
    (kb / "meta/manifest.json").write_text('{"sources": {}}', encoding="utf-8")
    (kb / "meta/golden_questions.md").write_text("# эталоны\n", encoding="utf-8")
    (kb / "meta/aurora_version.txt").write_text("1.0.0\n", encoding="utf-8")
    (kb / ".obsidian/workspace.json").write_text('{"main": {}}', encoding="utf-8")
    (kb / "Decisions/DR-001.md").write_text("почему выбрали так\n", encoding="utf-8")
    (kb / "Questions/Q-001.md").write_text("вопрос заказчику\n", encoding="utf-8")
    (kb / "Reference/abbr.md").write_text("аббревиатуры\n", encoding="utf-8")
    for outside in ("Sources/Confluence/a.md", "Raw/project/тз.md", "Artifacts/us/US-1.md",
                    "Deliverables/work/док.md", "Workspaces/задача/черновик.md",
                    "Templates/us_template.md", "Prompts/p.md"):
        (root / outside).parent.mkdir(parents=True, exist_ok=True)
        (root / outside).write_text("содержимое\n", encoding="utf-8")

    dry = run("kb_reset.py", cwd=root)
    assert "verified: 1" in dry.stdout and "работа человека" in dry.stdout, \
        f"не предупреждает, что удаляет проверенное человеком:\n{dry.stdout[:600]}"
    assert "заново из источников не выведется" in dry.stdout, \
        "не назвал разделы, которых нет ни в одном источнике"
    assert (kb / "Concepts/Карточка.md").exists(), "dry-run удалил файлы"

    run("kb_reset.py", "--apply", cwd=root)
    for gone in ("Concepts/Карточка.md", "MOC/Связи.md", "_archive/Старая.md",
                 "meta/manifest.json", "meta/golden_questions.md", "Decisions/DR-001.md",
                 "Questions/Q-001.md", "Reference/abbr.md"):
        assert not (kb / gone).exists(), f"база не обнулена: остался {gone}"
    assert (kb / "meta/aurora_version.txt").exists(), \
        "снесена отметка версии движка — панель и doctor перестанут её видеть"
    assert (kb / ".obsidian/workspace.json").exists(), \
        "снесены настройки хранилища Obsidian — это не знание и из источников не вернётся"
    assert (kb / "Concepts").is_dir(), "исчезла папка раздела: структура принадлежит движку"
    for outside in ("Sources/Confluence/a.md", "Raw/project/тз.md", "Artifacts/us/US-1.md",
                    "Deliverables/work/док.md", "Workspaces/задача/черновик.md",
                    "Templates/us_template.md", "Prompts/p.md"):
        assert (root / outside).exists(), f"сброс вышел за пределы базы: {outside}"


@test
def test_kb_reset_keep_handmade_spares_what_has_no_source(tmp: Path):
    """`--keep-handmade` оставляет то, чего нет ни в одном источнике.

    Смена способа извлечения — не повод стирать память проекта: журнал решений, вопросы,
    рукотворные справочники и правила базы `kb:build` не вернёт. Учёт извлечения уходит
    и здесь, иначе план сборки выйдет пустым.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Concepts", "Decisions", "Questions", "Reference", "meta"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    (kb / "Concepts/Карточка.md").write_text(
        '---\ntitle: "К"\nstatus: imported\n---\nтекст\n', encoding="utf-8")
    (kb / "Decisions/DR-001.md").write_text("почему выбрали так\n", encoding="utf-8")
    (kb / "Questions/Q-001.md").write_text("вопрос заказчику\n", encoding="utf-8")
    (kb / "Reference/abbr.md").write_text("аббревиатуры\n", encoding="utf-8")
    (kb / "meta/conventions.md").write_text("# правила\n", encoding="utf-8")
    (kb / "meta/golden_questions.md").write_text("# эталоны\n", encoding="utf-8")
    (kb / "meta/manifest.json").write_text('{"sources": {}}', encoding="utf-8")
    (kb / "meta/links.json").write_text("{}", encoding="utf-8")

    run("kb_reset.py", "--keep-handmade", "--apply", cwd=root)
    for keep in ("Decisions/DR-001.md", "Questions/Q-001.md", "Reference/abbr.md",
                 "meta/conventions.md", "meta/golden_questions.md"):
        assert (kb / keep).exists(), f"--keep-handmade удалил рукотворное: {keep}"
    assert not (kb / "Concepts/Карточка.md").exists(), "карточки должны уйти в обоих режимах"
    assert not (kb / "meta/manifest.json").exists(), \
        "учёт извлечения остался — kb:build сочтёт источники разобранными и план выйдет пустым"
    assert not (kb / "meta/links.json").exists(), "сгенерированный граф связей не удалён"

    # --all сносит и рукотворное, но только по явному ключу
    run("kb_reset.py", "--all", "--apply", "--allow-dirty", cwd=root)
    assert not (kb / "Decisions/DR-001.md").exists(), "--all не снёс журнал решений"


@test
def test_build_plan_prints_ready_task_for_assistant(tmp: Path):
    """`--partition N` отдаёт готовое задание: список файлов и правила, а не намёк на них."""
    root = make_project(tmp)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (root / f"Raw/project/Док-{i}.md").write_text("текст " * 200, encoding="utf-8")
    # отложенное в `_outdated`/`_archive` в план не берём: устаревшая копия договора
    # даёт карточки, противоречащие карточкам из действующей редакции
    (root / "Raw/project/_outdated").mkdir(parents=True, exist_ok=True)
    (root / "Raw/project/_outdated/Старый.md").write_text("текст " * 200, encoding="utf-8")
    plan = run("build_plan.py", cwd=root).stdout
    assert "_outdated" not in plan, f"в план попала отложенная копия:\n{plan[:600]}"

    # задание печатается и без --partition: за планом идут ровно за ним
    plain = run("build_plan.py", cwd=root).stdout
    assert "ЗАДАНИЕ АССИСТЕНТУ" in plain, f"план без задания:\n{plain[-600:]}"
    assert "ПАРТИЯ 1" in plain, "не сказано, на какую партию задание"
    assert "🆕 — движок его ещё не разбирал" in plain, "значки в плане не подписаны"

    # заданий печатается несколько — по одному на ближайшие партии
    assert plain.count("ЗАДАНИЕ АССИСТЕНТУ") >= 1, "заданий нет вовсе"

    out = run("build_plan.py", "--partition", "1", cwd=root).stdout
    assert "ЗАДАНИЕ АССИСТЕНТУ" in out, out[:400]
    assert out.count("ЗАДАНИЕ АССИСТЕНТУ") == 1, "с --partition печатается лишнее"
    assert "Раздели" not in out and "Разбери партию 1" in out, "нет самой формулировки задачи"
    assert "Док-0.md" in out, "в задании нет списка файлов партии"
    assert "status: imported" in out and "build_plan.py --done" in out, \
        "в задании нет правил жизненного цикла или шага завершения"
    assert "aurora-vault" in out, "не сказано, по какому скиллу работать"


@test
def test_kb_moc_gives_every_card_a_way_in(tmp: Path):
    """Карта содержания существует ради того, чтобы вход был у каждой карточки.

    Карточка, на которую ниоткуда нет ссылки, знанием не работает: её не найдут ни по
    связям, ни глазами. Поэтому: группы из объявленных правил, «Разное» для не попавших
    никуда и отдельная карта брошенных.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Glossary", "Concepts", "Roles", "MOC"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    def card(section, name, **fm):
        head = "".join(f"{k}: {v}\n" for k, v in fm.items())
        (kb / section / f"{name}.md").write_text(
            f'---\ntitle: "{name}"\n{head}status: imported\n---\n\nтекст\n', encoding="utf-8")
    card("Glossary", "ДОП", type="glossary")
    card("Concepts", "Приём", type="concept")
    card("Roles", "Аналитик", type="role")
    (kb / "Ничьё.md").write_text(          # ни типа, ни раздела с правилом
        '---\ntitle: "Ничьё"\nstatus: imported\n---\n\nтекст\n', encoding="utf-8")
    (kb / "Concepts/Приём.md").write_text(
        '---\ntitle: "Приём"\ntype: concept\nstatus: imported\n---\n\nсм. [[ДОП]]\n',
        encoding="utf-8")

    dry = run("kb_moc.py", cwd=root)
    assert "Термины и определения | 1" in dry.stdout, dry.stdout[:600]
    assert "Роли | 1" in dry.stdout, "группа ролей не собралась"
    assert "Разное | 1" in dry.stdout, "карточка без типа и раздела не попала в «Разное»"
    assert not list((kb / "MOC").glob("*.md")), "dry-run записал карты"

    run("kb_moc.py", "--apply", cwd=root)
    terms = (kb / "MOC/Термины-и-определения.md").read_text(encoding="utf-8")
    assert "[[ДОП|ДОП]]" in terms and "ФАЙЛ ГЕНЕРИРУЕТСЯ" in terms, terms[:400]
    lost = (kb / "MOC/Брошенные.md").read_text(encoding="utf-8")
    assert "Ничьё" in lost and "Аналитик" in lost, "брошенные не собраны"
    assert "[[ДОП|ДОП]]" not in lost, "на ДОП ссылается «Приём» — она не брошенная"

    # поиск кандидатов: скопление по метке и узел, на который все ссылаются
    for i in range(9):
        (kb / "Concepts" / f"Норма-{i}.md").write_text(
            f'---\ntitle: "Норма-{i}"\ntype: concept\ntags: [regulation]\n'
            "status: imported\n---\n\nсм. [[ДОП]]\n", encoding="utf-8")
    ideas = run("kb_moc.py", "--suggest", cwd=root).stdout
    assert "метка: regulation (9)" in ideas, f"скопление по метке не найдено:\n{ideas}"
    assert "tag:regulation" in ideas, "нет готовой строки для moc_groups.txt"

    # рукотворную карту генератор не трогает: у неё нет шапки «ФАЙЛ ГЕНЕРИРУЕТСЯ»
    (kb / "MOC/Роли.md").write_text("# Роли\n\nсобрано руками\n", encoding="utf-8")
    out = run("kb_moc.py", "--apply", "--allow-dirty", cwd=root).stdout
    assert "написан руками" in out, out[-400:]
    assert "собрано руками" in (kb / "MOC/Роли.md").read_text(encoding="utf-8")


@test
def test_kb_graph_writes_links_into_cards(tmp: Path):
    """Связь живёт в зеркале, а работать должна в базе: `related:` выводится из графа."""
    root = make_project(tmp, git=True)
    conf = root / "Sources/Confluence/Раздел"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "ALG.md").write_text(
        '---\ntitle: "ALG-1"\npage_id: 1\nry_defines: [RU.P.ALG-1]\n---\n\nтекст\n',
        encoding="utf-8")
    (conf / "US.md").write_text(
        '---\ntitle: "US-1.1"\npage_id: 2\nry_links: [RU.P.ALG-1]\n---\n\nтекст\n',
        encoding="utf-8")
    cards = root / "AuroraKnowledgeDB/Concepts"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "Алгоритм.md").write_text(
        '---\ntitle: "Алгоритм"\nsource: "Sources/Confluence/Раздел/ALG.md"\n'
        "status: imported\nrelated: []\n---\n\n# Алгоритм\n", encoding="utf-8")
    (cards / "История.md").write_text(
        '---\ntitle: "История"\nsource: "Sources/Confluence/Раздел/US.md"\n'
        "status: imported\n---\n\n# История\n", encoding="utf-8")

    dry = run("kb_graph.py", "--cards", cwd=root)
    assert "связей добавлено: 2" in dry.stdout, dry.stdout[:400]
    assert "related: []" in (cards / "Алгоритм.md").read_text(encoding="utf-8"), \
        "dry-run не должен писать в карточки"

    run("kb_graph.py", "--cards", "--apply", cwd=root)
    a = (cards / "Алгоритм.md").read_text(encoding="utf-8")
    b = (cards / "История.md").read_text(encoding="utf-8")
    assert '- "[История](История.md)"' in a and '- "[Алгоритм](Алгоритм.md)"' in b, a + b
    assert a.count("related:") == 1, f"поле related задвоилось:\n{a}"
    assert "# Алгоритм" in a, "тело карточки пострадало"

    second = run("kb_graph.py", "--cards", "--apply", cwd=root)
    assert "связей добавлено: 0" in second.stdout, "повторный прогон дублирует связи"


@test
def test_verify_by_jira_status(tmp: Path):
    """Статус задачи как основание доверия — и только при одной задаче на историю.

    История, дошедшая до разработки, прошла разбор аналитика и приёмку постановки: это
    основание. Две задачи на одну страницу — не «две причины верить», а неопределённость.
    """
    root = make_project(tmp, git=True)
    (root / "aurora.config.yaml").write_text(
        "project:\n  name: T\natlassian:\n  jira:\n"
        '    trust_statuses: [Закрыто, "Тестирование - готово"]\n'
        "    assumption_statuses: [Бэклог, Аналитика]\n", encoding="utf-8")
    conf, jira = root / "Sources/Confluence", root / "Sources/JIRA"
    conf.mkdir(parents=True, exist_ok=True); jira.mkdir(parents=True, exist_ok=True)
    for num in ("1.1", "1.2", "1.3"):
        (conf / f"US-{num}.md").write_text(
            f'---\ntitle: "US-{num}. История"\npage_id: {num.replace(".", "")}\n---\n\nтекст\n',
            encoding="utf-8")
    def issue(key, story, status):
        (jira / f"{key}.md").write_text(
            f'---\nkey: "{key}"\ntitle: "US-{story}. История"\nstatus: "{status}"\n---\n\nтекст\n',
            encoding="utf-8")
    issue("PRJ-1", "1.1", "Закрыто")          # доверяем
    issue("PRJ-2", "1.2", "Бэклог")           # ещё предположение
    issue("PRJ-3", "1.3", "Закрыто")          # спор: закрыто против бэклога — не судим
    issue("PRJ-4", "1.3", "Аналитика")
    issue("PRJ-5", "1.1", "Тестирование - готово")   # согласны с PRJ-1 — решение остаётся

    cards = root / "AuroraKnowledgeDB/Concepts"
    cards.mkdir(parents=True, exist_ok=True)
    for name, num in (("Готовое", "1.1"), ("Гипотеза", "1.2"), ("Спорное", "1.3")):
        (cards / f"{name}.md").write_text(
            f'---\ntitle: "{name}"\nsource: "Sources/Confluence/US-{num}.md"\n'
            "status: imported\ntrust: medium\n---\n\nтекст\n", encoding="utf-8")

    cp = run("kb_verify.py", "Concepts", "--owner", "@vadim", "--by-jira", "--apply", cwd=root)
    good = (cards / "Готовое.md").read_text(encoding="utf-8")
    guess = (cards / "Гипотеза.md").read_text(encoding="utf-8")
    argued = (cards / "Спорное.md").read_text(encoding="utf-8")
    assert "status: verified" in good and "PRJ-1" in good, f"не принято по статусу:\n{cp.stdout[:500]}"
    assert "PRJ-5" in good, "вторая задача с тем же решением не попала в основание"
    assert "status: draft" in guess, f"предположение не понижено:\n{guess}"
    assert "предположение" in guess, "в карточке нет основания решения"
    assert "status: imported" in argued, "карточка со спорящими задачами тронута"

    # задача со статусом вне обоих списков голоса не имеет и решению не мешает
    issue("PRJ-6", "1.2", "Согласование у заказчика")
    (cards / "Гипотеза.md").write_text(
        '---\ntitle: "Гипотеза"\nsource: "Sources/Confluence/US-1.2.md"\n'
        "status: imported\ntrust: medium\n---\n\nтекст\n", encoding="utf-8")
    run("kb_verify.py", "Concepts", "--owner", "@vadim", "--by-jira", "--apply",
        "--allow-dirty", cwd=root)
    guess2 = (cards / "Гипотеза.md").read_text(encoding="utf-8")
    assert "status: draft" in guess2, "молчащая задача сорвала решение"
    assert "без голоса: PRJ-6" in guess2, "не сказано, что у задачи статус вне списков"


@test
def test_verify_by_source_age_records_its_basis(tmp: Path):
    """Пакетная приёмка по возрасту источника пишет в карточку, на чём основано доверие."""
    root = make_project(tmp, git=True)
    conf = root / "Sources/Confluence"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "Старая.md").write_text(
        '---\ntitle: "Старая"\npage_id: 1\nupdated: 2019-03-01\n---\n\nтекст\n',
        encoding="utf-8")
    (conf / "Свежая.md").write_text(
        f'---\ntitle: "Свежая"\npage_id: 2\nupdated: {__import__("datetime").date.today().isoformat()}\n---\n\nтекст\n',
        encoding="utf-8")
    cards = root / "AuroraKnowledgeDB/Concepts"
    cards.mkdir(parents=True, exist_ok=True)
    for name, src in (("Устоявшееся", "Старая"), ("Свежее", "Свежая")):
        (cards / f"{name}.md").write_text(
            f'---\ntitle: "{name}"\nsource: "Sources/Confluence/{src}.md"\n'
            "status: imported\ntrust: medium\n---\n\nтекст\n", encoding="utf-8")

    cp = run("kb_verify.py", "Concepts", "--owner", "@vadim",
             "--source-older-than", "24", "--apply", cwd=root)
    old = (cards / "Устоявшееся.md").read_text(encoding="utf-8")
    fresh = (cards / "Свежее.md").read_text(encoding="utf-8")
    assert "status: verified" in old, f"старое не принято:\n{cp.stdout[:600]}"
    assert "verified_basis:" in old and "2019-03-01" in old, \
        f"основание доверия не записано:\n{old}"
    assert "status: imported" in fresh, "свежая страница принята вслепую"


@test
def test_remap_jira_moves_sources_to_issue_keys(tmp: Path):
    """Ссылки карточек переезжают со старых имён файлов Jira на ключи задач.

    Пока карточка ссылается на копию под старым именем, `--prune` не имеет права её
    удалить — это оборвало бы провенанс. Значит, сначала переезд ссылок, потом чистка.
    """
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "update_log.md").write_text("| Issue Key | Updated | Status | Local Path |\n",
                                          encoding="utf-8")
    (mirror / "PRJ-327.md").write_text(
        "# PRJ-327: US-3.1.1. Приём\n\n| **Key** | PRJ-327 |\n", encoding="utf-8")
    (mirror / "US-3.1.1.md").write_text(
        "# PRJ-327: US-3.1.1. Приём\n\n| **Key** | PRJ-327 |\n", encoding="utf-8")
    card = root / "AuroraKnowledgeDB/Concepts/Приём.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nsource: Sources/JIRA/US-3.1.1.md\n---\n\nтекст\n", encoding="utf-8")
    gone = root / "AuroraKnowledgeDB/Concepts/Потеряшка.md"
    gone.write_text("---\nsource: Sources/JIRA/3-1-9.md\n---\n\nтекст\n", encoding="utf-8")

    dry = run("kb_remap.py", "--mirror", "Sources/JIRA", cwd=root)
    assert "`US-3.1.1.md` → `PRJ-327.md`" in dry.stdout, dry.stdout[:600]
    assert "Ссылки в никуда (1)" in dry.stdout, "ссылка на несуществующий файл не отмечена"
    assert "source: Sources/JIRA/US-3.1.1.md" in card.read_text(encoding="utf-8"), \
        "dry-run не должен писать в карточки"

    run("kb_remap.py", "--mirror", "Sources/JIRA", "--apply", cwd=root)
    assert "source: Sources/JIRA/PRJ-327.md" in card.read_text(encoding="utf-8"), \
        "ссылка не перенацелена на ключ задачи"

    sys.path.insert(0, str(KIT / "scripts"))
    import jira_export as je
    assert je.cited(str(mirror), ["US-3.1.1.md"]) == set(), \
        "после переезда копия свободна и prune может её убрать"


@test
def test_kb_graph_builds_links_by_ry_and_story_number(tmp: Path):
    """Связи собираются по объявленным правилам, а не по догадкам.

    Ключ RY объявлен один раз — он и есть адрес; номер истории связывает критерии,
    задачу и саму историю. Всё, на что история ссылается по ключам, — её дети.
    """
    root = make_project(tmp)
    conf = root / "Sources/Confluence"
    jira = root / "Sources/JIRA"
    (conf / "Истории").mkdir(parents=True, exist_ok=True)
    (jira).mkdir(parents=True, exist_ok=True)

    def page(rel, title, defines=(), links=()):
        f = conf / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        head = [f'title: "{title}"', "page_id: 1", "space: SP"]
        if defines:
            head.append("ry_defines: [" + ", ".join(defines) + "]")
        if links:
            head.append("ry_links: [" + ", ".join(links) + "]")
        f.write_text("---\n" + "\n".join(head) + "\n---\n\nтекст\n", encoding="utf-8")

    page("Алгоритмы/ALG-026.md", "ALG-026 Сохранение данных", defines=["RU.PRJ.ALG-026"])
    page("Справочники/SPR-032.md", "SPR-032 Типы корректировок", defines=["RU.PRJ.SPR-032"])
    page("Истории/US-4.4.2.md", "US-4.4.2. Приём корректировки",
         links=["RU.PRJ.ALG-026", "RU.PRJ.SPR-032", "RU.PRJ.NO-001"])
    page("Критерии/AC-4.4.2.md", "AC-4.4.2. Приём корректировки")
    (jira / "PRJ-1895.md").write_text(
        "# PRJ-1895: US-4.4.2. Приём корректировки\n\n"
        "| Field | Value |\n| --- | --- |\n| **Key** | PRJ-1895 |\n"
        "| **Summary** | US-4.4.2. Приём корректировки |\n", encoding="utf-8")

    cp = run("kb_graph.py", cwd=root)
    assert "ключей RY объявлено: **2**" in cp.stdout, cp.stdout[:500]
    assert "связей по ключам: **2**" in cp.stdout, "ребро строится только на объявленный ключ"
    assert "висячих ключей (ссылка есть, объявления нет): **1**" in cp.stdout, cp.stdout[:600]

    one = run("kb_graph.py", "--story", "4.4.2", cwd=root).stdout
    assert "AC-4.4.2" in one and "PRJ-1895" in one, f"предки истории не собраны:\n{one}"
    assert "ALG · `RU.PRJ.ALG-026`" in one and "SPR · `RU.PRJ.SPR-032`" in one, \
        f"дети истории не собраны или тип не распознан:\n{one}"

    # MOC генерируется целиком: ручных правок в нём не держат
    run("kb_graph.py", "--write", cwd=root)
    moc = (root / "AuroraKnowledgeDB/MOC/Связи.md").read_text(encoding="utf-8")
    assert "ФАЙЛ ГЕНЕРИРУЕТСЯ" in moc and "type: moc" in moc, moc[:300]


@test
def test_confluence_export_keeps_macro_content(tmp: Path):
    """Данные из макросов доезжают до зеркала: дата, автор, задача, врезка.

    Все четыре лежат не в тексте, а в атрибутах и параметрах, и общая ветка выбрасывала
    их вместе с макросом. На живой странице это выглядело как «поля пустые»: дата
    изменения, автор, ссылка на задачу и целый раздел Acceptance criteria.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    storage = (
        '<table><tbody>'
        '<tr><th><p>Дата</p></th><td><p><time datetime="2026-01-15"/></p></td></tr>'
        '<tr><th><p>Автор</p></th><td><p>'
        '<ac:link><ri:user ri:username="v.petrov"/></ac:link></p></td></tr>'
        '<tr><th><p>Задача</p></th><td><p>'
        '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PRJ-1895</ac:parameter>'
        '</ac:structured-macro></p></td></tr></tbody></table>'
        '<h1>Acceptance criteria</h1>'
        '<ac:structured-macro ac:name="excerpt-include"><ac:parameter ac:name="">'
        '<ac:link><ri:page ri:content-title="AC-1.1 Приём"/></ac:link>'
        '</ac:parameter></ac:structured-macro>'
        '<p><ac:structured-macro ac:name="status-handy">'
        '<ac:parameter ac:name="Status">Утверждена</ac:parameter></ac:structured-macro></p>')
    md = ce.to_markdown(storage, "https://c.example.com", "SP", "https://jira.example.com")
    assert "2026-01-15" in md, f"дата из макроса потеряна:\n{md}"
    assert "@v.petrov" in md, f"упоминание пользователя потеряно:\n{md}"
    assert "PRJ-1895" in md, f"ссылка на задачу потеряна:\n{md}"
    assert "AC-1.1 Приём" in md and "Включено со страницы" in md, \
        f"врезка чужой страницы потеряна — раздел остаётся пустым:\n{md}"
    assert "[Статус: Утверждена]" in md, f"статус потерян:\n{md}"
    assert md == ce.to_markdown(storage, "https://c.example.com", "SP",
                                "https://jira.example.com"), "конвертация недетерминирована"
    # пробел в заголовке страницы кодируется плюсом, а не %2B: иначе ссылка ведёт в никуда
    assert "%2B" not in md, f"ссылка на страницу перекодирована:\n{md}"


@test
def test_force_rereads_but_writes_only_changes(tmp: Path):
    """`--force` перечитывает источник, но переписывает только изменившееся.

    Иначе счётчик «записано» означает объём выгрузки, а не объём изменений: 877 записей
    там, где не поменялось ничего, — и понять по нему, что произошло, нельзя. У зеркала
    задач сверка стояла всегда, у зеркала страниц её отменял `--force`.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    src = ("scripts/confluence_export.py", "scripts/jira_export.py")
    for rel in src:
        code = (KIT / rel).read_text(encoding="utf-8")
        assert "if exists else None" in code or "if os.path.isfile(full) else None" in code, \
            f"{rel}: содержимое на диске не читается для сверки"
        assert "not self.force else None" not in code, \
            f"{rel}: --force снова отменяет сверку с диском"
    # обе ветки счётчиков живы: записанное и пропущенное считаются раздельно
    code = (KIT / "scripts/confluence_export.py").read_text(encoding="utf-8")
    assert "self.written += 1" in code and "self.skipped += 1" in code


@test
def test_tables_stay_markdown_unless_impossible(tmp: Path):
    """HTML в зеркале — только там, где markdown не выражает содержимое.

    Прежнее правило отправляло в HTML таблицу с любым списком или вторым абзацем в
    ячейке — на живой базе это 388 таблиц из 807, то есть почти половина знания приезжала
    разметкой, которую ни прочитать глазами, ни разобрать поиском.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    ok = ('<table><tbody>'
          '<tr><th>Поле</th><th>Значение</th></tr>'
          '<tr><td><ul><li>раз</li><li>два</li></ul></td>'
          '<td><p>абзац</p><p>ещё абзац</p></td></tr>'
          '<tr><td><a href="https://jira.example.com/browse/PRJ-1">PRJ-1</a></td>'
          '<td><strong>жирным</strong></td></tr></tbody></table>')
    md = ce.to_markdown(ok, "https://c.example.com", "SP")
    assert "<table" not in md, f"таблица со списком ушла в HTML:\n{md}"
    assert "- раз<br>- два" in md, f"список в ячейке потерян:\n{md}"
    assert "абзац<br>ещё абзац" in md, f"второй абзац потерян:\n{md}"
    assert "[PRJ-1](https://jira.example.com/browse/PRJ-1)" in md, \
        f"ссылка в ячейке потеряна — раньше ячейка бралась голым текстом:\n{md}"
    assert "**жирным**" in md, "выделение в ячейке потеряно"
    assert md == ce.to_markdown(ok, "https://c.example.com", "SP"), "конвертация недетерминирована"

    # объединённые ячейки markdown не выражает — остаётся HTML, и признак не теряется
    merged = ('<table class="wrapped"><tbody><tr><td colspan="2" class="x">на две</td></tr>'
              "<tr><td>а</td><td>б</td></tr></tbody></table>")
    out = ce.to_markdown(merged, "https://c.example.com", "SP")
    assert "<table" in out and 'colspan="2"' in out, f"объединение потеряно:\n{out}"
    assert 'class=' not in out, "в HTML остались шумовые атрибуты"

    # вложенная таблица и многострочный код — тоже не выражаются
    nested = "<table><tbody><tr><td><table><tbody><tr><td>вложено</td></tr></tbody></table></td></tr></tbody></table>"
    assert "<table" in ce.to_markdown(nested, "https://c.example.com", "SP")
    code = "<table><tbody><tr><td><pre>строка1\nстрока2</pre></td></tr></tbody></table>"
    assert "<table" in ce.to_markdown(code, "https://c.example.com", "SP")


@test
def test_confluence_export_keeps_plugin_diagrams(tmp: Path):
    """Диаграммы плагинов доезжают: mermaid — текстом, draw.io — исходником во вложении."""
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    st = ('<ac:structured-macro ac:name="mermaid"><ac:plain-text-body>'
          'graph TD; A--&gt;B;</ac:plain-text-body></ac:structured-macro>'
          '<ac:structured-macro ac:name="drawio">'
          '<ac:parameter ac:name="diagramName">Схема-входа</ac:parameter>'
          '</ac:structured-macro>'
          '<ac:structured-macro ac:name="excerpt"><ac:rich-text-body>'
          '<p>Печатная форма</p></ac:rich-text-body></ac:structured-macro>')
    assets = []
    md = ce.to_markdown(st, "https://c.example.com", "SP", "", assets)
    assert "```mermaid" in md and "graph TD" in md, f"диаграмма mermaid потеряна:\n{md}"
    assert md.count("```") == 2, f"ограда кода задвоилась:\n{md}"
    assert assets == ["Схема-входа"], f"исходник draw.io не запрошен: {assets}"
    assert "Врезка (excerpt)" in md and "Печатная форма" in md, \
        f"врезка потеряна или не помечена:\n{md}"

    # врезка таблицы, внешний макет и история правок — три макроса из живых страниц
    more = ('<ac:structured-macro ac:name="table-excerpt-include">'
            '<ac:parameter ac:name="name">RU_ALL</ac:parameter>'
            '<ac:parameter ac:name="page"><ac:link>'
            '<ri:page ri:content-title="MAP-037 Маппинг"/></ac:link></ac:parameter>'
            '</ac:structured-macro>'
            '<ac:structured-macro ac:name="widget"><ac:parameter ac:name="url">'
            '<ri:url ri:value="https://figma.example.com/design/x"/></ac:parameter>'
            '</ac:structured-macro>'
            '<ac:structured-macro ac:name="change-history"/>')
    md2 = ce.to_markdown(more, "https://c.example.com", "SP")
    assert "MAP-037 Маппинг" in md2 and "RU\\_ALL" in md2, f"врезка таблицы потеряна:\n{md2}"
    assert "figma.example.com" in md2, f"ссылка на внешний макет потеряна:\n{md2}"

    # ограда внутри цитаты: expand/info становятся blockquote, и «> » ломает диаграмму
    quoted = ('<ac:structured-macro ac:name="expand"><ac:rich-text-body>'
              '<ac:structured-macro ac:name="mermaid"><ac:plain-text-body>'
              'flowchart TD\n  A --&gt; B</ac:plain-text-body></ac:structured-macro>'
              '</ac:rich-text-body></ac:structured-macro>')
    md3 = ce.to_markdown(quoted, "https://c.example.com", "SP")
    body = md3.split("```")[1]
    assert ">" not in body.replace("-->", ""), f"строки диаграммы в цитате:\n{md3}"

    # блок кода не должен обрастать второй оградой — это ломало подсветку в зеркале
    code = ('<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sql'
            '</ac:parameter><ac:plain-text-body>SELECT 1;</ac:plain-text-body>'
            '</ac:structured-macro>')
    assert ce.to_markdown(code, "https://c.example.com", "SP").strip() == \
        "```sql\nSELECT 1;\n```"


@test
def test_mirror_cleanup_sees_foreign_files(tmp: Path):
    """Чистка зеркала видит не только `.md`.

    Файлы вроде `Имя.md_COLLISION` от прежних синк-скиллов пережили и `--force`, и
    `--prune`, потому что чистка смотрела на расширение. Папка с ними читалась человеком
    как дубль каталога, а аудит молчал: зеркало «чистое».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import sources_core as sc
    import sync_audit as sa
    root = tmp / "mirror"
    (root / "Раздел").mkdir(parents=True)
    (root / "Раздел/Стр.md").write_text("---\npage_id: 1\n---\nтекст", encoding="utf-8")
    (root / "Раздел/Стр.md_COLLISION").write_text("мусор", encoding="utf-8")
    (root / "Раздел/.DS_Store").write_text("", encoding="utf-8")
    (root / "sync_state.md").write_text("состояние", encoding="utf-8")

    # схемы страницы лежат рядом с ней и принадлежат зеркалу, хотя в состоянии их нет
    (root / "Раздел/Стр_assets").mkdir()
    (root / "Раздел/Стр_assets/Схема.drawio").write_text("<mxfile/>", encoding="utf-8")

    m = sc.WikiMirror(str(root))
    extra = m.extra_files(["Раздел/Стр.md"])
    assert extra == ["Раздел/Стр.md_COLLISION"], \
        f"чистка сносит схемы страницы или не видит мусор: {extra}"
    assert sa.foreign_files(str(root)) == ["Раздел/Стр.md_COLLISION"], "аудит его не показывает"

    # после чистки опустевший каталог уходит: пустая папка читается как дубль
    (root / "Пусто/Вложено").mkdir(parents=True)
    (root / "Пусто/Вложено/.DS_Store").write_text("", encoding="utf-8")
    assert sc.drop_empty_dirs(str(root)) == 2, "опустевшие каталоги остались"
    assert not (root / "Пусто").exists() and (root / "Раздел").exists()


@test
def test_confluence_export_keeps_requirement_yogi_keys(tmp: Path):
    """Ключи Requirement Yogi и ссылки на них остаются в зеркале.

    Макрос RY не имеет тела, и общая ветка выбрасывала его вместе с ключом: в проекте,
    где трассировка между документами идёт через RY, зеркало теряло все связи разом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    storage = (
        '<table><tbody><tr><th>ID</th><td><p>'
        '<ac:structured-macro ac:name="requirement" ac:schema-version="1" ac:macro-id="a">'
        '<ac:parameter ac:name="type">DEFINITION</ac:parameter>'
        '<ac:parameter ac:name="key">KB.ENS.URZ-251</ac:parameter>'
        '<ac:parameter ac:name="" /></ac:structured-macro></p></td></tr>'
        '<tr><th>Связано</th><td><p>'
        '<ac:structured-macro ac:name="requirement" ac:schema-version="1" ac:macro-id="b">'
        '<ac:parameter ac:name="freetext">Link</ac:parameter>'
        '<ac:parameter ac:name="type">LINK</ac:parameter>'
        '<ac:parameter ac:name="key">RU.PRJ.UI-012</ac:parameter></ac:structured-macro>'
        '</p></td></tr></tbody></table>')
    md = ce.to_markdown(storage, "https://c.example.com", "SP")
    assert "**RYk:KB.ENS.URZ-251**" in md, f"объявление ключа потеряно:\n{md}"
    assert "RYl:RU.PRJ.UI-012" in md, f"ссылка на ключ потеряна:\n{md}"
    assert "RYk:RU.PRJ.UI-012" not in md, "ссылка помечена как объявление ключа"
    assert md == ce.to_markdown(storage, "https://c.example.com", "SP"), "конвертация недетерминирована"

    # свойство требования и отчёт: свои метки, чтобы вид связи читался прямо в тексте
    extra = ('<p><ac:structured-macro ac:name="requirement-property">'
             '<ac:parameter ac:name="title">true</ac:parameter></ac:structured-macro>'
             '<ac:structured-macro ac:name="requirement-report">'
             '<ac:parameter ac:name="query">x</ac:parameter></ac:structured-macro></p>')
    md2 = ce.to_markdown(extra, "https://c.example.com", "SP")
    assert "RYo:title" in md2 and "RYr" in md2, f"свойство или отчёт потеряны:\n{md2}"

    defines, links = ce.ry_keys(storage)
    assert defines == ["KB.ENS.URZ-251"] and links == ["RU.PRJ.UI-012"], (defines, links)
    head = ce.render_front_matter({"id": "1", "title": "Стр", "space": "SP", "version": 1,
                                   "updated": "2026-07-30", "url": "https://c.example.com/x",
                                   "breadcrumbs": "Стр", "hash": "0" * 16,
                                   "ry_defines": defines, "ry_links": links})
    assert "ry_defines: [KB.ENS.URZ-251]" in head and "ry_links: [RU.PRJ.UI-012]" in head, head
    # ключ, объявленный на странице, не дублируется в списке ссылок
    both = storage + storage.replace("DEFINITION", "LINK")
    assert ce.ry_keys(both)[1] == ["RU.PRJ.UI-012"], "объявленный ключ попал в ссылки"


@test
def test_sync_audit_case_only_paths_are_not_a_loss(tmp: Path):
    """Регистр папки разошёлся с состоянием — это переименование, а не потеря страницы.

    Файловая система macOS и Windows к регистру нечувствительна: страницу переименовали в
    источнике, папка осталась под старым именем. Считать это одновременно MISSING и ORPHAN
    значит поднимать тревогу там, где ничего не пропало.
    """
    root = make_project(tmp)
    conf = root / "Sources/Confluence"
    (conf / "Раздел/Основной_баланс").mkdir(parents=True, exist_ok=True)
    (conf / "Раздел/Основной_баланс/index.md").write_text(
        "---\npage_id: 111111\n---\n\nтекст", encoding="utf-8")
    (conf / "sync_state.md").write_text(
        "**Sync Date:** 2026-07-25\n\n| # | Page ID | Title | Local Path | Status |\n"
        "|---|---|---|---|---|\n"
        "| 1 | 111111 | Баланс | Раздел/Основной_Баланс/index.md | SYNCED |\n", encoding="utf-8")
    cp = run("sync_audit.py", cwd=root, expect_rc=1)
    assert "CASE: **1**" in cp.stdout, f"расхождение регистра не выделено: {cp.stdout[:500]}"
    assert "MISSING: **0**" in cp.stdout, "страница объявлена потерянной, хотя она на месте"
    assert "ORPHAN: **0**" in cp.stdout, "тот же файл посчитан лишним"


@test
def test_registry_drives_mirrors_and_audit(tmp: Path):
    """Зеркала объявляют модули: движок не должен знать про Confluence и Jira по именам.

    Проверяем всю цепочку на выдуманном модуле: реестр видит его, аудит выбирает правила
    по объявленному виду хранилища, а `--source` сужает проверку до одного зеркала.
    """
    root = make_project(tmp)
    (root / ".opencode/connectors/demo-board.json").write_text(json.dumps({
        "id": "demo-board", "title": "Демо-доска", "kind": "board",
        "what": "выдуманный источник для теста",
        "mirror": {"default_path": "Sources/Demo", "state": "update_log.md"},
        "run": {"script": "demo_export.py", "command": "sync:demo", "skill": "demo-sync"},
        "auth": {"env_prefix": "DEMO"},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\nsources:\n  - id: Demo\n    module: demo-board\n    path: Sources/Demo\n",
                   encoding="utf-8")

    out = run("sources_registry.py", cwd=root).stdout
    assert "demo-board" in out and "Sources/Demo" in out, f"модуль не в реестре:\n{out}"

    # зеркала нет на диске — doctor зовёт завести папку, но схему это не ломает
    doc = run("aurora_doctor.py", "--structure", cwd=root)
    assert "Sources/Demo" in doc.stdout, "doctor молчит про заявленное зеркало"
    assert doc.returncode == 0, "заявленное зеркало не должно быть ошибкой схемы"

    mirror = root / "Sources/Demo"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "DEMO-1.md").write_text("задача", encoding="utf-8")
    (mirror / "update_log.md").write_text(
        "**Sync Date:** 2026-07-30\n\n| Issue Key | Updated | Status | Local Path |\n"
        "|---|---|---|---|\n| DEMO-2 | 2026-07-30 10:00 | Готово | DEMO-2.md |\n",
        encoding="utf-8")
    cp = run("sync_audit.py", cwd=root, expect_rc=1)
    assert "## Demo (Sources/Demo)" in cp.stdout, f"зеркало модуля не проверено:\n{cp.stdout}"
    assert "MISSING: **1**" in cp.stdout and "ORPHAN: **1**" in cp.stdout, \
        f"правила board-зеркала не применились:\n{cp.stdout}"
    assert "Confluence" not in cp.stdout, \
        "проверено зеркало, которого нет в sources: — список берётся не из реестра"

    one = run("sync_audit.py", "--source", "Demo", cwd=root, expect_rc=1).stdout
    assert "## Demo" in one, "--source отсеял то, что просили"

    js = json.loads(run("sync_audit.py", "--json", cwd=root, expect_rc=1).stdout)
    assert js["mirrors"]["Demo"]["kind"] == "board", f"машинный итог без вида зеркала: {js}"
    assert js["mirrors"]["Demo"]["missing"] == 1, f"числа не сошлись: {js}"

    # зеркало без состояния сверять не с чем — но молчать о нём нельзя: панель покажет
    # «пусто» там, где на диске лежат данные, которые никто не проверяет
    (mirror / "update_log.md").unlink()
    js = json.loads(run("sync_audit.py", "--json", cwd=root, expect_rc=1).stdout)
    assert js["mirrors"]["Demo"]["no_state"] and js["mirrors"]["Demo"]["files"] == 1, \
        f"зеркало без состояния пропало из машинного итога: {js}"

    # папка без модуля — замечание, а не блокер: данные удалять нельзя
    (root / "Sources/Ничья").mkdir(parents=True, exist_ok=True)
    doc = run("aurora_doctor.py", "--structure", cwd=root)
    assert "зеркала без модуля" in doc.stdout and "Sources/Ничья" in doc.stdout, \
        f"ничья папка в Sources/ не названа:\n{doc.stdout}"
    assert doc.returncode == 0, "ничья папка не должна валить проверку структуры"


@test
def test_jira_prune_removes_only_unknown_files(tmp: Path):
    """`--prune` убирает следы прежних выгрузок, но не служебные файлы и не свежие задачи."""
    sys.path.insert(0, str(KIT / "scripts"))
    import jira_export as je
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)
    for name in ("PRJ-1.md", "US-3.1.1.md", "JIRA_prompt.md", "JIRA_issue_template.md"):
        (mirror / name).write_text("текст", encoding="utf-8")
    state = je.write_state(str(mirror), [("PRJ-1", "2026-07-30 10:00", "Готово", "PRJ-1.md")])
    (mirror / "sync_report_2026-01-01.md").write_text("отчёт", encoding="utf-8")
    extra = je.stale(str(mirror), state)
    assert extra == ["US-3.1.1.md"], f"лишним должен быть только след старой выгрузки: {extra}"

    # упоминание номера в тексте — не ссылка на файл: иначе защита не даст удалить ничего
    (root / "AuroraKnowledgeDB/Concepts/Упоминание.md").write_text(
        "---\nsource: Sources/JIRA/PRJ-1.md\n---\n\nСделано в US-3.1.1\n", encoding="utf-8")
    assert je.cited(str(mirror), extra) == set(), \
        "совпадение по подстроке принято за ссылку на файл"

    # файл, на который ссылается карточка, удалять нельзя: это обрыв провенанса
    card = root / "AuroraKnowledgeDB/Concepts/Приём.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nsource: Sources/JIRA/US-3.1.1.md\n---\n\nтекст", encoding="utf-8")
    assert je.cited(str(mirror), extra) == {"US-3.1.1.md"}, \
        "файл, на который ссылается карточка, не защищён от удаления"


@test
def test_office_ingest_converts_and_is_idempotent(tmp: Path):
    root = make_project(tmp)
    docx = root / "Raw/customer/Документ.docx"
    _write_minimal_docx(docx)
    cp = run("office_ingest.py", cwd=root)
    md = root / "Raw/customer/Документ.md"
    assert md.exists(), f"транскрипт не создан: {cp.stdout}"
    text = md.read_text(encoding="utf-8")
    assert "converted_from:" in text and "Машинная конвертация" in text, "нет шапки провенанса"
    assert "Тестовый абзац" in text, "текст документа не извлечён"
    assert docx.exists(), "оригинал пропал (он же доказательство)"
    cp2 = run("office_ingest.py", cwd=root)
    assert "пропущено (не изменились): 1" in cp2.stdout, "повторный запуск переделывает работу"


# -------------------------------------------------------------------- main

def main() -> int:
    print(f"Aurora engine tests — kit {(KIT / 'VERSION').read_text().strip()}\n")
    failed = [(n, e) for n, e in RESULTS if e]
    print(f"\nПройдено: {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    if failed:
        print("\nПадения:")
        for n, e in failed:
            print(f"\n— {n}:\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
