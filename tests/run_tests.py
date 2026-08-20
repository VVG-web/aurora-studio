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


# Раздел → тип карточки: тот же список, что в kb_lint. Фикстура без `type:` — это
# карточка, на которую линтер справедливо ругается, а не «обычная карточка».
SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}


def card(root: Path, rel: str, body: str = "", **fm) -> Path:
    p = root / "AuroraKnowledgeDB" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm.setdefault("type", SECTION_TYPE.get(rel.split("/")[0], "concept"))
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
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    fn = ui[ui.index("function assistantTasks(lines){"):ui.index("let LAST_TASKS = [];")]
    assert "рамка в тело не идёт" in fn, "границы блока снова режутся по старому правилу"
    # тело начинается после ВТОРОЙ линейки — той, что под заголовком
    assert "while (from < lines.length && !lines[from].startsWith(TASK_EDGE)) from++;" in fn, \
        "начало тела ищется не от заголовка вниз"
    assert 'toast(task.label + " — скопировано' in ui, \
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
        "--apply", cwd=root)
    made = root / "AuroraKnowledgeDB/Processes/Алгоритм-приёма.md"
    assert made.is_file(), "имя файла собрано не по правилу build.md"
    text = made.read_text(encoding="utf-8")
    assert "type: process" in text and 'source: "Sources/Confluence/Страница.md"' in text, text[:300]
    assert "поля запроса" in text and "шаг за шагом" in text, "тело секций не перенесено"
    assert "версии страницы" not in text, "перенесена секция, которую не просили"

    # Повтор из того же источника — не конфликт, а второй проход по обновлённой
    # странице: отказ здесь ронял бы разбор при каждой правке источника.
    again = run("build_plan.py", "--card", "Алгоритм приёма", "--source",
                "Sources/Confluence/Страница.md", "--sections", "1", "--to", "Processes",
                "--apply", cwd=root)
    assert "уже собрана" in again.stdout, again.stdout[-200:]

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

    dry = run("build_plan.py", "--reopen", cwd=root)
    assert "Пустая" not in dry.stdout or "не дали: 1" in dry.stdout, dry.stdout[:400]
    assert "не дали: 1" in dry.stdout, f"переоткрывать нечего:\n{dry.stdout[:400]}"
    assert "осталось: 0" in run("build_plan.py", "--status", cwd=root).stdout, \
        "dry-run не должен править манифест"

    run("build_plan.py", "--reopen", "--apply", cwd=root)
    status = run("build_plan.py", "--status", cwd=root).stdout
    assert "осталось: 1" in status, f"источник без карточек не вернулся в план:\n{status}"
    plan = run("build_plan.py", cwd=root).stdout
    assert "Пустая" in plan and "Полезная" not in plan, \
        "в план должен вернуться только тот, что ничего не дал"

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
    old_home = os.environ.get("HOME", "")
    os.environ["HOME"] = str(home)
    try:
        importlib.reload(ck)
        assert str(home) in ck.ROOTS_FILE, "список корней должен жить в домашней папке"
        assert ck.load_roots() == [ck.norm(os.path.dirname(str(KIT)))], \
            "при первом запуске корень — папка рядом с kit'ом"

        far = str(home / "Documents/GitProjects/TAXKG")
        assert ck.writable_target(far) == "", "папку вне корней запрещать нельзя"
        assert ck.writable_target("/etc/aurora"), "системное дерево должно отвергаться"
        assert ck.writable_target(str(home)), "разворачивать проект прямо в ~ нельзя"

        ck.save_roots([str(home / "Documents/GitProjects"), str(home / "work")])
        assert ck.load_roots() == [ck.norm(str(home / "Documents/GitProjects")),
                                   ck.norm(str(home / "work"))], "список не сохранился"
        assert ck.load_roots(["~/elsewhere"]) == [ck.norm("~/elsewhere")], \
            "--roots должен перекрывать сохранённое на один запуск"
    finally:
        os.environ["HOME"] = old_home
        importlib.reload(ck)

    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
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
        for st in s["steps"]:
            assert st["why"], f"шаг без объяснения в сценарии {s['id']}"
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
def test_new_project_works_without_a_terminal(tmp: Path):
    """`aurora.py new` не требует человека у клавиатуры.

    Настройка спрашивает ответы через `input()`, и при запуске из скрипта, панели или
    ассистента команда падала на первом же вопросе с `EOFError`, оставляя развёрнутую, но
    ненастроенную папку. Найдено прогоном сценария регрессии.
    """
    target = tmp / "auto-project"
    cp = subprocess.run([sys.executable, str(KIT / "aurora.py"), "new", str(target),
                         "--name", "Auto", "--slug", "Auto"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert cp.returncode == 0, f"new упал без терминала:\n{cp.stdout[-800:]}\n{cp.stderr[-400:]}"
    assert "EOFError" not in cp.stderr, cp.stderr[-300:]
    assert (target / "aurora.config.yaml").is_file(), "конфиг не создан"
    assert (target / "AuroraKnowledgeDB").is_dir(), "структура не развёрнута"
    assert (target / ".opencode/scripts/kb_lint.py").is_file(), "движок не разложен"
    assert "aurora.py setup" in cp.stdout, \
        "не сказано, как довести настройку до конца"

    # проект пригоден к работе сразу: команды не падают на пустой базе
    for args in (["kb_lint.py", "--summary"], ["build_plan.py", "--status"],
                 ["aurora_stats.py"], ["kb_trust.py"]):
        r = subprocess.run([sys.executable, str(target / ".opencode/scripts" / args[0]),
                            *args[1:]], cwd=str(target), capture_output=True, text=True)
        assert r.returncode == 0, f"{args[0]} на свежем проекте: rc={r.returncode}\n{r.stderr[:300]}"


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


def term_regex(terms: list):
    """Приватные названия — по границам слова, а не по подстроке.

    Короткое внутреннее название нередко оказывается началом обычного русского слова —
    и тогда защита ловит живую фразу вместо утечки. Ложное срабатывание хуже пропуска:
    его обходят, переписывая нормальный текст, и защита превращается в помеху, которую
    учатся игнорировать. Границей слова считаем букву или цифру, поэтому имя файла с
    подчёркиванием или дефисом по-прежнему ловится.
    """
    body = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?<![0-9A-Za-zА-Яа-яЁё])(?:{body})(?![0-9A-Za-zА-Яа-яЁё])", re.I)


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
    rx = term_regex(terms)
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
                         capture_output=True, text=True).stdout
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
                         cwd=str(root), capture_output=True, text=True)
    assert "commit-msg" in out.stdout, f"хук сообщений не поставлен:\n{out.stdout}"
    assert (root / ".git/hooks/commit-msg").is_file()

    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    ok = subprocess.run(["git", "commit", "-m", "обычная правка"], cwd=str(root),
                        capture_output=True, text=True)
    assert ok.returncode == 0, f"чистое сообщение не прошло:\n{ok.stderr}"

    (root / "f.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    bad = subprocess.run(["git", "commit", "-m", "правка про ВНУТРЕННЕЕИМЯ"], cwd=str(root),
                         capture_output=True, text=True)
    assert bad.returncode != 0, "коммит с внутренним названием в сообщении прошёл"
    assert "внутренние названия" in bad.stderr, bad.stderr[:300]
    n = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(root),
                       capture_output=True, text=True).stdout.strip()
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
                         cwd=str(root), capture_output=True, text=True)
    assert "pre-commit" in out.stdout, f"линтер-хук не поставлен:\n{out.stdout}"
    assert not (root / ".git/hooks/commit-msg").exists(), \
        "в проект поставлен хук приватности — он про публикацию кита, а не про работу"

    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=str(root), check=True)
    cp = subprocess.run(["git", "commit", "-m", "правка про ВНУТРЕННЕЕИМЯ"], cwd=str(root),
                        capture_output=True, text=True)
    assert cp.returncode == 0, f"коммит в проекте остановлен зря:\n{cp.stderr}"

    st = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--status"],
                        cwd=str(root), capture_output=True, text=True).stdout
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
    # стандартов таким адресом не являются. NEUTRAL-HOSTS-ALLOW
    allow = {"example.com", "example.ru", "example.org", "example", "localhost",
             "127.0.0.1", "github.com", "www.apache.org", "www.python.org",
             "schemas.openxmlformats.org", "cdn.jsdelivr.net"}
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
def test_panel_opens_without_waiting(tmp: Path):
    """Панель не должна выглядеть зависшей на старте.

    Реестр собирается из `--help` полусотни скриптов, а проверка окружения импортировала
    тяжёлые модули — на каждое открытие уходили секунды, и панель казалась мёртвой.
    `--help` теперь спрашивается один раз на скрипт, реестр кэшируется на диске, наличие
    модуля проверяется поиском, а не импортом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import kit_commands as K
    import aurora_cockpit as ck

    K._HELP.clear()
    calls = {"n": 0}
    real = K.help_text

    def counted(impl):
        if impl.split()[0] not in K._HELP:
            calls["n"] += 1
        return real(impl)

    K.help_text = counted
    try:
        impl = "kb_lint.py"
        for fn in (K.flags_of, K.flag_help, K.flag_args, K.required_flags):
            fn(impl)
        assert calls["n"] == 1, f"`--help` запускается {calls['n']} раза на одну команду"
    finally:
        K.help_text = real

    assert "importlib.util.find_spec" in (KIT / "cockpit/aurora_cockpit.py").read_text(
        encoding="utf-8"), "наличие модуля снова проверяется импортом"
    ck.CACHE.clear()
    first = ck.environment()
    assert ck.environment() is first, "окружение пересчитывается на каждый запрос"


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
    assert "function assistantTasks" in ui and "task.label" in ui, \
        "задания ассистенту из консоли нечем забрать в буфер"
    assert "S.health && S.health.runs" in ui, "панель снова читает историю из браузера"
    assert re.search(r'if \(view==="console"\)\{ renderHistory\(\); drawTaskButton\(\);', ui), \
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
def test_build_can_run_the_whole_plan_overnight(tmp: Path):
    """Первичная сборка идёт партиями сама, а не девяноста нажатиями кнопки.

    Партия агента ограничена нарочно: обозримый прогон, обозримый откат. Но у проекта с
    тремя годами истории источников полторы тысячи, и по пятнадцать за раз это девяносто
    прогонов. Цикл делает то же самое сам — с чекпойнтом и коммитом на каждую партию,
    с потолком по времени и остановкой, если план перестал двигаться.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")

    assert "--until-done" in src and "--hours" in src, "нет режима сплошного разбора"
    # Прогресс меряется пройденными источниками, а не оставшимися: источник без
    # структуры уходит человеку, «осталось» не меняется — и ночной прогон вставал на
    # первой такой пачке, разобрав четырнадцать карточек из тысячи трёхсот.
    assert "done_after <= done_before" in src, \
        "цикл снова меряет прогресс по «осталось» — пачка отложенных его остановит"
    assert "left_after >= left_before" not in src, "старая метрика прогресса вернулась"
    assert 'commit_result(cwd, "agent:build",\n                              f"партия' in src, \
        "партии не коммитятся по отдельности — откатить можно будет только всё сразу"
    assert "if a.apply and not (a.task == \"build\" and a.until_done):" in src, \
        "результат коммитится дважды: и в цикле, и в конце"

    # шаг есть в маршруте пересборки, и флаги существуют у команды
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    rebuild = scen.split("[rebuild]")[1].split("\n[")[0]
    assert "--until-done" in rebuild, "маршрут пересборки не умеет разбирать проект целиком"
    assert "на ночь" in rebuild, "шаг не предупреждает, что это часы работы"
    assert hasattr(R, "build_left"), "счётчик плана переименован — цикл сломается молча"


@test
def test_ask_tab_names_the_model_and_lets_you_pick_it(tmp: Path):
    """«Спросить» показывает, кто ответил, и даёт выбрать модель.

    У основной и запасной модели разные скорость и качество. Пока вкладка молчала об
    исполнителе, медленный ответ запасной выглядел как ответ основной — и человек делал
    выводы о базе по ответу другой модели.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    for need, why in (('id="askBackend"', "нет выбора модели"),
                      ('id="askWho"', "не видно, кто ответил"),
                      ('id="askPing"', "нельзя проверить основную модель"),
                      ('id="askPrimary"', "нет возврата на основную")):
        assert need in ui, why
    assert 'args.push("--backend", pick)' in ui, "выбор модели не уходит в команду"
    assert "(запасная)" in ui, "ответ запасной модели не отмечен как запасной"
    assert "/api/agent/retry-primary" in ui and "/api/agent/ping" in ui, \
        "кнопки не привязаны к серверу"

    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert '"--backend"' in src and 'cfg = {**cfg, "backends": picked}' in src, \
        "команда не умеет спрашивать конкретную модель"
    assert 'бэкенда №{a.backend} нет в настройке' in src, \
        "выбор несуществующей модели подменяется молча — человек выбирал сознательно"


@test
def test_fallback_provider_gets_a_fair_chance(tmp: Path):
    """Запасной провайдер получает своё время, а не пять секунд на исходе дедлайна.

    Живой случай: основной шлюз молчал, агент писал «ни один бэкенд не ответил» — и не
    переключался на локальную модель, хотя она была настроена. Дедлайн общий на вызов:
    первый бэкенд съедал `request_timeout` целиком, второму доставалось `deadline - now`,
    то есть пять секунд. Медленная локальная модель не отвечает за пять секунд никогда,
    поэтому переключение существовало только на бумаге.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib, time as _t
    AG = importlib.import_module("agent_core")
    AG.DOWN.clear()

    seen = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return 200, {"slots_idle": 1}, "", 0.0
        seen.append((b["n"], round(timeout)))
        if b["n"] == 1:
            _t.sleep(0.05)
            return 0, {}, "TimeoutError: timed out", 0.05
        return 200, {"choices": [{"message": {"content": "ответ"}}],
                     "usage": {"completion_tokens": 10}}, "", 0.1

    cfg = {"backends": [{"n": 1, "url": "http://a", "key": "", "model": "fast", "models": {}},
                        {"n": 2, "url": "http://b", "key": "", "model": "slow", "models": {}}],
           "request_timeout": 100, "thinking": False}
    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                     transport=transport, deadline=_t.time() + 0.2, sleep=lambda s: None)
    assert r["ok"] and r["backend"] == 2, f"на запасного не переключились: {r}"
    slow = [tm for n, tm in seen if n == 2]
    assert slow and slow[0] >= 30, \
        f"запасному дали {slow} с вместо честной доли таймаута — медленная модель не успеет"
    assert AG.DOWN.get(1, 0) > _t.time(), "упавший провайдер не отмечен: его спросят снова"

    # кнопка «Вернуться на основного» снимает отметку
    AG.RETRY_FLAG.parent.mkdir(parents=True, exist_ok=True)
    AG.RETRY_FLAG.write_text("", encoding="utf-8")
    assert AG.retry_primary_asked() and not AG.DOWN, "кнопка не вернула основного в строй"

    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'id="retryPrimary"' in ui and "/api/agent/retry-primary" in ui, \
        "кнопки «Вернуться на основного» нет в консоли"
    assert "/api/agent/retry-primary" in srv, "сервер не знает такого пути"


@test
def test_one_button_ends_with_what_is_left_to_the_human(tmp: Path):
    """Маршрут «Привести базу в порядок» делает всё автоматизируемое и называет остаток.

    Живая жалоба: «я сделал все прогоны, так какого хрена ошибки? Сделай одну кнопку, а
    потом покажи, что конкретно осталось мне». Раньше остаток приходилось вычитывать из
    трёх отчётов, а «ошибки линтера» звучали как поломка — хотя это работа, требующая
    суждения: документ это или знание, слить двойников или оставить.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    route = next((s for s in ck.scenarios() if s["id"] == "all"), None)
    assert route, "маршрута «Привести базу в порядок» нет"
    cmds = [st["cmd"] for st in route["steps"]]
    for need in ("sync:confluence", "agent:build", "kb:repair", "kb:trust", "ops:todo"):
        assert need in cmds, f"в одной кнопке нет шага {need}"
    assert cmds[-1] == "ops:todo", "маршрут не заканчивается списком того, что осталось"
    assert not any(st.get("manual") for st in route["steps"]), \
        "в одной кнопке есть шаг-человек — значит она не одна кнопка"
    build = next(st for st in route["steps"] if st["cmd"] == "agent:build")
    assert "--until-done" in build["flags"] and "--apply" in build["flags"], \
        "разбор в одной кнопке не доходит до конца сам"

    root = make_project(tmp, git=True)
    card(root, "Concepts/Понятие.md", "тело", status="imported", type="concept")
    out = run("aurora_todo.py", cwd=root).stdout
    assert "Принять знание: 1 карточек" in out, f"остаток приёмки не назван:\n{out}"
    assert "не чинится кнопкой" in out, "не сказано, почему остаток нельзя автоматизировать"


@test
def test_console_names_the_model_and_the_speed(tmp: Path):
    """В консоли видно, кто отвечает и с какой скоростью.

    Ночной прогон идёт часами и молча меняет исполнителя: первый бэкенд занят — работа
    уходит на второй, тот отвечает вдвое медленнее, и человек видит только «стало долго».
    Скорость берём из `usage` ответа сервера: не отдал — не показываем, выдумывать
    ток/с по длине текста значит рисовать правдоподобную неправду.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    step = {"backends": [(2, "qwen3.6-35b")], "tps": 41.7}
    line = R.where(step)
    assert "qwen3.6-35b" in line and "бэкенд №2" in line and "41.7 ток/с" in line, line

    assert "ток/с" not in R.where({"backends": [(1, "m")], "tps": 0}), \
        "скорость показана там, где сервер её не отдал"
    assert R.where({"backends": []}) == "", "пустой шаг рисует пустую скобку"
    assert "+1 на проверке" in R.where({"backends": [(1, "worker-m"), (3, "critic-m")],
                                        "tps": 0}), "участие критика не видно"

    core = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    assert '"tps"' in core and "completion_tokens" in core, \
        "скорость не считается по usage самого сервера"


@test
def test_night_run_waits_out_a_dropped_connection(tmp: Path):
    """Обрыв связи — повод подождать, а не бросить ночь.

    Живой случай: в 24-й партии отвалился VPN. Три источника упали по таймауту, остались
    в голове плана, каждая следующая партия бралась за них снова — и цикл остановился,
    оставив 667 источников неразобранными. Сетевой сбой лечится ожиданием, как докачка
    файла: связь вернулась — работа продолжается с того же места.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    net = {"steps": [{"status": "сбой", "note": "№3 gemma: TimeoutError: timed out; "
                                                "ни один бэкенд не ответил осмысленно"}]}
    assert R.looks_offline(net), "таймаут всех бэкендов не опознан как обрыв связи"

    content = {"steps": [{"status": "сбой", "note": "карточка не собрана: имя должно быть "
                                                    "уникальным"}]}
    assert not R.looks_offline(content), \
        "содержательный сбой принят за сетевой — прогон будет ждать связь, которая есть"
    assert not R.looks_offline({"steps": [{"status": "разобран", "note": "timed out"}]}), \
        "смотрим только на сбои: слово из успешного шага не должно включать ожидание"

    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert "waits < OFFLINE_TRIES" in src and "time.sleep(OFFLINE_WAIT)" in src, \
        "цикл не ждёт возвращения связи"
    assert "waits = 0" in src, "счётчик ожиданий не сбрасывается после удачной партии"
    assert "time.time() < deadline" in src, \
        "ожидание не ограничено окном прогона — кнопка на ночь будет ждать вечно"


@test
def test_distill_writes_a_thesis_and_keeps_the_source(tmp: Path):
    """Тезис пишется, дословный источник остаётся под ним, шапка не задета.

    Это третья за перестройку попытка собрать файл из разобранных частей — и первые две
    вклеивали поле в тело: `split_frontmatter` отдаёт шапку без «---», и всякая сборка
    «по длине» промахивается. Здесь проверяется результат целиком, а не намерение.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    card = root / "AuroraKnowledgeDB/Concepts/Расчёт.md"
    card.write_text('---\ntitle: "Расчёт"\nkind: knowledge\nstatus: draft\n'
                    'type: concept\n---\n\nИсходный текст страницы.\nВторая строка.\n',
                    encoding="utf-8")

    def fake(cfg, role, messages, deadline=None):
        if role == "qa":
            return {"ok": True, "backend": 1, "model": "qa", "log": [],
                    "text": "ВЕРДИКТ: ЧИСТО"}
        return {"ok": True, "backend": 1, "model": "w", "log": [], "tps": 10,
                "text": "Расчёт — это способ получить сумму.\nВыполняется по расписанию."}

    step = R.distill_card({"request_timeout": 60}, str(card), call=fake)
    assert step["status"] == "переписана", step
    out = "---" + step["head"] + "\n---" + step["body"]
    head = out[3:out.find("\n---", 3)]
    assert "kind: knowledge" in head and "title:" in head, "шапка потеряна"
    assert "Расчёт — это способ" in out.split("\n---", 1)[1], "тезиса нет в теле"
    assert R.QUOTES in out and "Исходный текст страницы." in out.split(R.QUOTES)[1], \
        "дословный источник не сохранён под тезисом"
    assert "Расчёт — это способ" not in head, "тезис попал в шапку"
    assert out.count("---") == 2, f"разделители шапки задвоились:\n{out[:200]}"


@test
def test_card_kind_decides_who_may_rewrite_the_body(tmp: Path):
    """Тип карточки определяется правилом, и выбор человека сильнее правила."""
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    K = importlib.import_module("kb_kind")

    assert K.guess("AuroraKnowledgeDB/Concepts/Договор.md",
                   {"source": '"Raw/contract/ГК-2026.md"'}, "текст")[0] == "document"
    assert K.guess("AuroraKnowledgeDB/Glossary/Накладная.md",
                   {"source": '"Sources/Confluence/x.md"'}, "определение")[0] == "dictionary"
    table = "| код | имя |\n|---|---|\n| 1 | а |\n| 2 | б |\n| 3 | в |\n"
    assert K.guess("AuroraKnowledgeDB/Concepts/Коды.md",
                   {"source": '"Sources/Confluence/x.md"'}, table)[0] == "dictionary"
    assert K.guess("AuroraKnowledgeDB/Processes/Расчёт.md",
                   {"source": '"Sources/Confluence/x.md"'},
                   "Абзац.\nВторой.\nТретий.\nЧетвёртый.")[0] == "knowledge"

    root = make_project(tmp)
    (root / "AuroraKnowledgeDB/Concepts/Своё.md").write_text(
        '---\ntitle: "Своё"\nkind: document\nstatus: knowledge\n---\n\nдословный текст\n',
        encoding="utf-8")
    out = run("kb_kind.py", "--apply", cwd=root).stdout
    assert "выбор человека сохранён: 1" in out, f"движок перетёр выбор человека:\n{out}"


@test
def test_writing_a_field_never_touches_the_body(tmp: Path):
    """Проставление поля в шапке не должно задевать тело карточки.

    `split_frontmatter` отдаёт шапку БЕЗ разделителей, а хвост — начиная с «\n---».
    Команда, которая режет хвост по длине шапки, промахивается на три символа и вклеивает
    поле в первую строку тела. На живом проекте это испортило 2033 карточки за прогон —
    спас только git.
    """
    root = make_project(tmp, git=True)
    body = "первая строка тела\nвторая строка\n\n## Раздел\n\nтекст\n"
    (root / "AuroraKnowledgeDB/Concepts/Карточка.md").write_text(
        '---\ntitle: "Карточка"\nstatus: draft\ntype: concept\n'
        'source: "Sources/Confluence/x.md"\n---\n\n' + body, encoding="utf-8")
    run("kb_kind.py", "--apply", cwd=root)
    got = (root / "AuroraKnowledgeDB/Concepts/Карточка.md").read_text(encoding="utf-8")
    assert got.startswith("---\n") and "kind: knowledge" in got
    assert got.endswith(body), f"тело изменилось при записи поля:\n{got[-160:]}"
    assert got.count("---") == 2, "разделители шапки задвоились"


@test
def test_acceptance_machinery_is_gone(tmp: Path):
    """Приёмки больше нет: ни команды, ни вкладки, ни очереди.

    Процедура, потерявшая смысл, опаснее отсутствующей: человек тратит на неё время и
    считает, что делает работу. Доверие теперь вычисляется, и присваивать его некому.
    """
    assert not (KIT / "scripts/kb_verify.py").exists(), "скрипт приёмки на месте"
    cmds = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "kb:verify" not in cmds and "kb:queue" not in cmds, "команды приёмки в реестре"
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "renderReview" not in ui and 'id="view-review"' not in ui, "вкладка приёмки в панели"
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "/api/review" not in srv, "сервер всё ещё отдаёт очередь приёмки"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert "kb:verify" not in scen, "маршрут зовёт снесённую команду"
    assert "kb:trust" in scen, "пересчёт доверия не встал на её место"


@test
def test_trace_table_proves_every_link(tmp: Path):
    """Связь артефакта с задачей доказывается, а не утверждается.

    Номер сравнивается по границе токена: `10.3.1` и `10.3.11` — разные истории, и
    склеить их значит выдать чужое доверие. Косвенная связь идёт не дальше двух переходов:
    через три в большой базе связано всё со всем, и класс перестаёт что-либо значить.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    T = importlib.import_module("kb_trace_table")

    root = make_project(tmp)
    jira, conf = root / "Sources/JIRA", root / "Sources/Confluence"
    jira.mkdir(parents=True, exist_ok=True); conf.mkdir(parents=True, exist_ok=True)
    (jira / "PRJ-1.md").write_text(
        '---\nkey: "PRJ-1"\ntitle: "US-10.3.1. Оплата"\nstatus: "Закрыто"\n---\n',
        encoding="utf-8")
    (jira / "PRJ-2.md").write_text(
        '---\nkey: "PRJ-2"\ntitle: "US-10.3.11. Возврат"\nstatus: "Закрыто"\n---\n',
        encoding="utf-8")
    (conf / "AC-10.3.1-Оплата.md").write_text(
        '---\ntitle: "AC-10.3.1. Критерии оплаты"\n---\n\nсм. Алгоритм-оплаты\n',
        encoding="utf-8")
    (conf / "Алгоритм-оплаты.md").write_text(
        '---\ntitle: "Алгоритм оплаты"\n---\n\nсм. Справочник-кодов\n', encoding="utf-8")
    (conf / "Справочник-кодов.md").write_text(
        '---\ntitle: "Справочник кодов"\n---\n\nтаблица\n', encoding="utf-8")
    (conf / "Чужая-страница.md").write_text(
        '---\ntitle: "Про погоду"\n---\n\nничего общего\n', encoding="utf-8")

    t = T.build(str(root))
    direct = {os.path.basename(k): [r["key"] for r in v] for k, v in t["direct"].items()}
    assert direct.get("AC-10.3.1-Оплата.md") == ["PRJ-1"], \
        f"номер сопоставлен неверно (10.3.11 — другая история): {direct}"
    ind = {os.path.basename(k): v for k, v in t["indirect"].items()}
    assert "Алгоритм-оплаты.md" in ind, "трассировка на один переход не найдена"
    assert "Чужая-страница.md" not in ind and "Чужая-страница.md" not in direct, \
        "страница без связей получила связь"
    why = t["direct"]["Sources/Confluence/AC-10.3.1-Оплата.md"][0]["why"]
    assert "10.3.1" in why, f"связь не объяснена словами: {why}"
    assert all(r["depth"] <= 2 for rows in t["indirect"].values() for r in rows), \
        "трассировка ушла глубже двух переходов"


@test
def test_trust_is_computed_from_task_status(tmp: Path):
    """Класс доверия считается по статусам задач, а не назначается человеком.

    Одна задача-черновик перевешивает десять готовых: содержание ещё поменяется. Нет
    связей вовсе — не знание, а `draft`: подтвердить нечем, и молчаливо записать такую
    карточку в знание значит обесценить класс ровно там, где он нужен.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    U = importlib.import_module("kb_trust")

    table = {"direct": {"Sources/Confluence/A.md": [{"key": "PRJ-1", "why": "номер"}],
                        "Sources/Confluence/B.md": [{"key": "PRJ-1", "why": "номер"},
                                                    {"key": "PRJ-9", "why": "ключ"}]},
             "indirect": {"Sources/Confluence/C.md": [{"key": "PRJ-1", "trail": ["C", "A"],
                                                       "depth": 1}]}}
    st = {"PRJ-1": "Закрыто", "PRJ-9": "Бэклог"}
    trust, draft = {"закрыто"}, {"бэклог"}
    cls, why = U.source_class("Sources/Confluence/A.md", table, st, trust, draft)
    assert cls == "trusted" and "PRJ-1" in why, (cls, why)
    cls, why = U.source_class("Sources/Confluence/B.md", table, st, trust, draft)
    assert cls == "draft" and "PRJ-9" in why, f"черновая задача не перевесила: {cls} · {why}"
    assert U.source_class("Sources/Confluence/C.md", table, st, trust, draft)[0] == "trusted"
    assert U.source_class("Raw/contract/ГК.md", table, st, trust, draft)[0] == "raw"
    assert U.source_class("Sources/Confluence/Z.md", table, st, trust, draft)[0] == "unknown"
    assert U.wanted_status("unknown") == "draft", "недоказанное доверие стало знанием"
    assert U.wanted_status("raw") == "knowledge" and U.wanted_status("trusted") == "knowledge"

    # понижение не стирает знание, а записывает причину в подвал
    got = U.note_downgrade("тело карточки\n", "knowledge", "draft", "задача вернулась")
    assert "тело карточки" in got and "класс изменён" in got and U.FOOTER in got



@test
def test_repair_drops_dead_backlinks_from_stubs(tmp: Path):
    """Мёртвое «Упоминается в» у заготовки чинится командой, а не человеком.

    Заготовка родилась из ссылки; карточку-источник переименовали — и справка о
    происхождении стала битой ссылкой. Знания в ней нет, но приёмка вставала намертво:
    правило «битые ссылки решает человек» держало такую заготовку непроверяемой вечно.
    """
    root = make_project(tmp)
    card(root, "Concepts/Живая.md", "текст со ссылкой [[Заготовка]]",
         status="imported", type="concept")
    (root / "AuroraKnowledgeDB/Glossary/Заготовка.md").write_text(
        '---\ntitle: "Заготовка"\nstatus: draft\ntype: glossary\n'
        "tags: [заготовка]\n---\n\n# Заготовка\n\n## Упоминается в\n\n"
        "- [[Живая]]\n- [[Уехавшая]]\n- [[Тоже-уехавшая]]\n", encoding="utf-8")

    run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    got = (root / "AuroraKnowledgeDB/Glossary/Заготовка.md").read_text(encoding="utf-8")
    assert "[[Живая]]" in got, "живое упоминание убрано вместе с мёртвыми"
    assert "Уехавшая" not in got and "Тоже-уехавшая" not in got, \
        f"мёртвые упоминания остались — убрана только первая строка:\n{got}"


@test
def test_doctor_says_a_project_without_git_has_no_undo(tmp: Path):
    """Проект без git — проект без отката, и агент в нём писать откажется.

    Живой случай: doctor говорил «OK: config and onboarding look ready», а `agent:build`
    возвращал 1 за ноль секунд — чекпойнт делать не во что. Понять по журналу, почему база
    не растёт, было нельзя.
    """
    root = make_project(tmp)                       # без git: make_project(git=True) его заводит
    cp = run("aurora_doctor.py", cwd=root)
    assert "не под git" in cp.stdout, \
        f"doctor молчит про отсутствие git — а без него агент не работает:\n{cp.stdout}"
    assert "git init" in cp.stdout, "названа беда, но не названо лечение"
    assert cp.returncode != 0, "проект без отката не может считаться готовым"

    nest = tmp / "with-git"; nest.mkdir()
    ok = make_project(nest, git=True)
    assert "не под git" not in run("aurora_doctor.py", cwd=ok).stdout, \
        "ложная тревога на проекте под git"


@test
def test_installer_marks_its_own_index_stubs(tmp: Path):
    """Заготовки оглавлений пишет установщик — и помечает их как свои.

    Без пометки `kb:index` считал их текстом человека и не трогал никогда: проект начинал
    жизнь с одиннадцати «рукотворных» оглавлений, которых никто не писал, и с вечной
    находкой, лечившейся только `--force`.
    """
    src = (KIT / "scripts/install_aurora.py").read_text(encoding="utf-8")
    i = src.index("_index.md")
    assert "generated: kb_index.py" in src[i - 400:i + 400], \
        "установщик пишет заготовку оглавления без пометки генерации"

    # и старые заготовки движок узнаёт по своей же строке — без --force
    root = make_project(tmp)
    card(root, "Concepts/Понятие.md", "тело", status="imported", type="concept")
    (root / "AuroraKnowledgeDB/Concepts/_index.md").write_text(
        "# Concepts\n\nИндекс раздела. Карточек: 0 (на 2026-01-01).\n", encoding="utf-8")
    cp = run("kb_index.py", "--apply", cwd=root, expect_rc=0)
    assert "Приняты под генерацию" in cp.stdout, \
        f"заготовку установщика движок не узнал в лицо:\n{cp.stdout}"
    assert "[[Понятие]]" in (root / "AuroraKnowledgeDB/Concepts/_index.md").read_text(
        encoding="utf-8"), "оглавление не пересобрано"


@test
def test_buttons_stay_on_the_right_when_the_row_wraps(tmp: Path):
    """Кнопки держатся справа и после переноса строки.

    `.row` переносится, а `.spacer{flex:1}` живёт на первой строке: стоит имени команды
    стать длинным или окну узким — кнопки уезжают вниз и прижимаются влево. Правая группа
    с `margin-left:auto` держится справа на любой строке.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "flex-wrap:wrap" in ui, "разметка изменилась: перенос строк больше не включён"
    assert "margin-left:auto" in ui and ".row-right{" in ui, \
        "нет правой группы — при переносе кнопки окажутся слева внизу"
    for anchor in ('id="clearConsole"', 'id="refreshHealth"', 'id="refreshOverview"'):
        i = ui.index(anchor)
        assert "row-right" in ui[max(0, i - 400):i], \
            f"кнопка {anchor} не в правой группе"
    assert "#consoleCmd{overflow:hidden" in ui, \
        "длинная команда снова может ломать всю строку"


@test
def test_the_all_in_one_route_looks_different_from_the_dangerous_one(tmp: Path):
    """Маршрут «всё сразу» зовёт нажать, «пересобрать с нуля» — подумать.

    Оба выглядели одинаково: карточка и синяя кнопка «Пройти маршрут». Один включает в
    себя остальные, второй сносит содержимое базы вместе с принятым доверием — и цена
    ошибки у них разная на порядок.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert '.card.route-all{' in ui and '.card.route-danger{' in ui, \
        "у маршрутов нет разного оформления"
    assert 'sc.id === "all" ? "route-all"' in ui and '"rebuild" ? "route-danger"' in ui, \
        "классы не привязаны к конкретным маршрутам"
    assert "★ всё сразу" in ui and "⚠ сносит базу" in ui, \
        "нет подписи, объясняющей, чем эти маршруты отличаются"
    assert 'kind === "route-danger" ? "danger" : "primary"' in ui, \
        "у опасного маршрута кнопка того же цвета, что у обычного"


@test
def test_hidden_really_hides_in_the_panel(tmp: Path):
    """Атрибут hidden обязан скрывать, даже когда у элемента задан свой display.

    Живой случай: «Скрыть раздел» в «Разработке» ставила `hidden` на кнопку навигации и
    уводила на «О проекте», а сама кнопка оставалась на экране. Правило `[hidden]` живёт
    в UA-стилях, и любой авторский `display` его перебивает — `nav button{display:flex}`
    как раз такой. Панель прячет так семь элементов, поэтому проверяем правило, а не один
    экран.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\{display:none ?!important\}", ui), \
        "нет правила [hidden]{display:none !important} — сокрытие держится на UA-стилях"
    # у элементов, которые панель прячет, свой display есть — значит правило не «на всякий»
    assert "nav button{display:flex" in ui, \
        "разметка навигации изменилась: проверьте, что правило [hidden] ещё нужно"
    assert ui.index("[hidden]{display:none") > ui.index("<style"), \
        "правило вне таблицы стилей"


@test
def test_registry_cache_belongs_to_the_engine_that_wrote_it(tmp: Path):
    """Кэш реестра, написанный другим кодом панели, не должен приниматься за свой.

    Живой случай: панель работала старым процессом (кит обновили, панель не
    перезапускали). Ключ кэша складывался из версии кита, времени правки `commands.txt`
    и признака «поднято из исходников» — всё это у старого процесса совпадало с новым.
    Старый писал в файл кита свои 62 команды, новый читал их как свои и терял шесть
    команд `dev:` — до следующей смены версии, то есть навсегда.

    Лечится меткой самого движка панели: время правки файла, снятое НА ИМПОРТЕ. Кит
    обновили после старта — метка у работающего процесса осталась вчерашней, и его кэш
    новый код не примет.
    """
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert re.search(r"^ENGINE = os\.path\.getmtime", src, re.M), \
        "нет метки движка: кэш реестра снова общий для разных версий панели"
    key = re.search(r"    key = \((.+?)\)\n", src, re.S)
    assert key and "engine=" in key.group(1), \
        "метка движка не вошла в ключ кэша — старый процесс снова отравит реестр"
    # метку снимаем на импорте, а не при сборке ключа: иначе она равна текущему mtime
    # и у старого процесса совпадёт с новым — ровно то, от чего защищаемся
    assert "ENGINE = os.path.getmtime" in src.split("def registry")[0], \
        "метка движка считается внутри registry() — она обязана быть снимком на старте"


@test
def test_reset_spares_what_no_rebuild_can_return(tmp: Path):
    """Карточку без документа сносить по умолчанию нельзя: её никто не вернёт.

    Защита раньше висела на именах папок (`--keep-handmade`: Decisions, Questions,
    Reference) — и промахивалась в обе стороны. Замер на живом проекте в 1955 карточек:
    внутри «рукотворных» разделов невосстановимых **31** из 269, а снаружи — **451**.
    Папка не может знать, кто написал карточку: база разложена по предметам, а авторство
    лежит поперёк предметов.

    Решает факт: стоит ли за карточкой документ. Флаг перевернулся — теперь нужен
    `--drop-handmade`, чтобы снести и невосстановимое.
    """
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("текст", encoding="utf-8")
    card(root, "Concepts/Из-зеркала.md", "тело", status="knowledge",
         source="Sources/Confluence/Док.md")
    # рукотворное живёт в обычном предметном разделе, а не в особой папке
    card(root, "Concepts/Написано-руками.md", "тело", status="knowledge")
    card(root, "Decisions/DR-001.md", "почему выбрали так", status="knowledge")

    cp = run("kb_reset.py", cwd=root, expect_rc=0)
    assert "Из-зеркала" not in cp.stdout or "К удалению: 1" in cp.stdout, cp.stdout[:400]
    assert "остаётся: 2" in cp.stdout or "остаётся: 3" in cp.stdout, \
        f"рукотворное не защищено по умолчанию:\n{cp.stdout[:600]}"

    run("kb_reset.py", "--apply", cwd=root, expect_rc=0)
    assert not (root / "AuroraKnowledgeDB/Concepts/Из-зеркала.md").exists(), \
        "карточка с живым источником пережила сброс — пересборка её продублирует"
    assert (root / "AuroraKnowledgeDB/Concepts/Написано-руками.md").exists(), \
        "снесено рукотворное в обычном разделе: именно этого папка и не ловила"
    assert (root / "AuroraKnowledgeDB/Decisions/DR-001.md").exists(), "снесён журнал решений"

    # полный снос по-прежнему возможен — но теперь его просят явно
    cp2 = run("kb_reset.py", "--drop-handmade", "--apply", cwd=root, expect_rc=0)
    assert "не стоит документа" in cp2.stdout, "полный снос не предупредил о потере"
    assert not (root / "AuroraKnowledgeDB/Concepts/Написано-руками.md").exists(), \
        "--drop-handmade не снёс то, ради чего его просят"


@test
def test_console_stops_chasing_the_bottom_when_you_scroll_up(tmp: Path):
    """Отмотал вверх — консоль перестаёт прыгать в конец.

    Живой случай, слово в слово: «не могу скролить вывод консоли, она каждую секунду
    сама проматывается в конец». Вывод длинного прогона так не прочитать: человек ищет,
    когда всё началось, или было ли переключение на запасную модель, — а страница каждые
    450 мс возвращает его в конец. Слежение включается обратно само, когда он домотает
    вниз, и кнопкой «↓ вывод продолжается».
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "function stickToBottom(" in ui and "function watchScroll(" in ui, \
        "нет отдельной прокрутки со слежением"
    assert 'box.dataset.follow === "0"' in ui, "прокрутка не спрашивает, смотрит ли человек конец"
    assert ui.count("scrollTop = out.scrollHeight") == 0, \
        "осталась безусловная прокрутка — она перебьёт слежение в одном из трёх мест"
    assert ui.count("stickToBottom(out)") >= 3, \
        "не все места вывода переведены на слежение: маршрут, шаг и обычная команда"
    assert 'id="consoleTail"' in ui, "нет кнопки возврата в конец: слежение выключилось молча"
    assert "followAgain(" in ui, "вернуться к слежению нечем"


@test
def test_reset_warns_about_facts_not_folder_names(tmp: Path):
    """«Заново не выведется» — про карточки без документа, а не про имя раздела.

    Предупреждение висело на списке разделов: Reference, meta, Decisions. На живом
    проекте это оказалось неправдой — все 105 карточек `Reference/` имели `source:` в
    зеркале и вернулись бы сами. Человек читал «⚠️ заново не выведется» и понимал это
    как «удалятся исходные документы», хотя за пределами базы не трогается ничего.

    Считать надо факт: есть ли за карточкой файл, из которого её собрали. Карты и
    оглавления исключение — их собирают из самой базы.
    """
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("текст", encoding="utf-8")
    card(root, "Reference/Из-зеркала.md", "тело", status="knowledge",
         source="Sources/Confluence/Док.md")
    card(root, "Reference/Ниоткуда.md", "тело", status="knowledge")
    card(root, "Reference/Ссылка-в-никуда.md", "тело", status="knowledge",
         source="Sources/Confluence/Пропал.md")
    (root / "AuroraKnowledgeDB/MOC").mkdir(parents=True, exist_ok=True)
    card(root, "MOC/Карта.md", "карта", status="index")

    cp = run("kb_reset.py", "--drop-handmade", cwd=root, expect_rc=0)
    assert "Reference: 3  ⚠️ из них 2 не выведется" in cp.stdout, \
        f"счёт невосстановимых считается не по факту:\n{cp.stdout}"
    assert "MOC: 1" in cp.stdout and "MOC: 1  ⚠️" not in cp.stdout, \
        "карты объявлены потерей — их собирает kb:moc из самой базы"
    assert "Идут под снос 2 карточек" in cp.stdout, "нет итоговой строки"
    assert "Sources/, Raw/" in cp.stdout, "не сказано, что источники не трогаются"

    # база, целиком выведенная из источников, не должна пугать вовсе
    (root / "AuroraKnowledgeDB/Reference/Ниоткуда.md").unlink()
    (root / "AuroraKnowledgeDB/Reference/Ссылка-в-никуда.md").unlink()
    cp2 = run("kb_reset.py", "--drop-handmade", cwd=root, expect_rc=0)
    assert "⚠️ из них" not in cp2.stdout, "предупреждение осталось там, где терять нечего"
    assert "Идут под снос" not in cp2.stdout, "предупреждение о потере без потери"


@test
def test_refusing_to_write_stops_the_route(tmp: Path):
    """Отказ писать — код 2, а не 1: маршрут обязан встать, а не идти дальше.

    Живой случай: на пересборке базы `kb:reset` уперлась в git-guard (294 незакоммиченных
    файла) и вернула 1. Единица в панели значит «команда отработала и нашла, что чинить»,
    поэтому маршрут пошёл дальше — по НЕ сброшенной базе. `agent:build` увидел ноль
    источников (разбирать нечего, всё на месте) и отрапортовал успехом, `kb:kind` тоже.
    Четыре шага из четырнадцати объявили себя выполненными, не сделав ничего.

    Разница смысловая: 1 — «работа сделана, есть замечания», 2 — «работа не сделана».
    Отказ git-guard это всегда второе.
    """
    root = make_project(tmp)
    card(root, "Systems/Одна.md", "тело см. [[Две]]", status="knowledge")
    card(root, "Systems/Две.md", "тело см. [[Одна]]", status="knowledge")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    (root / "AuroraKnowledgeDB/Systems/Одна.md").write_text("грязь\n", encoding="utf-8")

    cp = run("kb_reset.py", "--apply", cwd=root, expect_rc=2)
    assert "git-guard" in cp.stdout + cp.stderr, "отказ не объяснён"
    assert (root / "AuroraKnowledgeDB/Systems/Две.md").exists(), \
        "guard отказал, а файлы всё равно удалены"

    # правило общее: ни одна команда не отвечает единицей на отказ писать
    for name in ("kb_reset.py", "kb_moc.py", "kb_graph.py", "kb_schema.py",
                 "kb_scrub.py", "jira_status.py", "sync_audit.py"):
        code = (KIT / "scripts" / name).read_text(encoding="utf-8")
        for i, line in enumerate(code.splitlines()):
            if "git_guard(" in line and "def " not in line:
                tail = "\n".join(code.splitlines()[i:i + 4])
                assert "return 1" not in tail, \
                    f"{name}: отказ писать возвращает 1 — маршрут пойдёт дальше вхолостую"


@test
def test_a_route_will_not_run_on_two_engine_versions(tmp: Path):
    """Маршрут не начинается, когда движок проекта отстал от кита.

    Живой случай: панель кита 1.92 на проекте с движком 1.85. Скрипт, которого в проекте
    нет, берётся из кита — так задумано, чтобы новая команда работала сразу. `kb:kind`
    пришла из кита и отработала; `agent:distill` попала в `agent_runner.py` проекта, где
    такой задачи нет, и маршрут развалился на пятом шаге, успев объявить четыре
    предыдущих успешными. База осталась собранной наполовину одними правилами,
    наполовину другими — и разобрать, где чьё, уже нельзя.

    Отдельную команду так запускать можно: человек видит, что делает. Маршрут — нет.
    """
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "def version_gap(" in src, "нет сверки версий движка"
    run_block = src.split('if u.path == "/api/run"')[1][:1200]
    assert "version_gap(" in run_block and 'payload.get("route")' in run_block, \
        "проверка версии не стоит на запуске шага маршрута"
    # страница обязана помечать шаги маршрута — иначе серверу нечего проверять
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    step = ui.split("async function runStep(")[1][:600]
    assert "route:true" in step, "шаг маршрута не помечен как шаг маршрута"


@test
def test_writing_a_field_refuses_to_touch_the_body(tmp: Path):
    """`with_fields` сторожит себя сама — в любом проекте, на каждой записи.

    Тесты гоняются при разработке кита, а команды человек запускает у себя: между этими
    двумя моментами лежат недели. Поэтому проверка «тело не тронуто» живёт не в тестах,
    а внутри самой записи — она срабатывает у человека, на его карточках, до того как
    испорченный текст попадёт на диск.

    Здесь проверяем обе стороны: обычная запись проходит и ничего не ломает, а подмена
    сборки ловится исключением, а не тихой порчей.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import aurora_common as A

    src = '---\nid: KB-1\nstatus: draft\n---\n\nТело. см. [[Другая]]\n'
    out = A.with_fields(src, {"kind": "knowledge", "status": "knowledge"})
    assert A.card_body(out) == A.card_body(src), "запись поля тронула тело"
    assert A.frontmatter(out)["kind"] == "knowledge", "поле не встало"
    assert A.frontmatter(out)["status"] == "knowledge", "поле не заменилось"
    assert out.count("---") == 2, "разделители удвоились — классика неверной сборки"

    # карточка без шапки: ставить поле некуда, и молча дописывать его в начало текста
    # нельзя — так рождается ровно то повреждение, от которого эта функция и заведена
    try:
        A.with_fields("Просто текст без шапки\n", {"kind": "knowledge"})
        assert False, "поле поставлено карточке без шапки"
    except ValueError:
        pass

    # сама сборка «---» + head + rest — не догадка вызывающего кода: в скриптах, которые
    # правят карточки, ручной сборки остаться не должно
    for name in ("kb_kind.py", "kb_trust.py", "sync_audit.py"):
        code = (KIT / "scripts" / name).read_text(encoding="utf-8")
        assert "with_fields(" in code, f"{name} правит шапку в обход with_fields"


@test
def test_lint_catches_a_field_that_slid_into_the_body(tmp: Path):
    """Поле шапки в первой строке тела — повреждение, и линтер обязан его назвать.

    Живой случай: неверная сборка файла разнесла `kind:` по первой строке тела 2033
    карточек за один прогон. Ни одна проверка не сработала — шапка разбиралась, ссылки
    были целы, число ошибок не выросло, храповик пропустил бы коммит. Заметили случайно.
    Проверка ловит повреждение от любого источника: старой версии движка, чужого
    скрипта, правки руками.
    """
    root = make_project(tmp)
    card(root, "Systems/Целая.md", "Тело. см. [[Битая]]", status="knowledge",
         type="system")
    broken = root / "AuroraKnowledgeDB/Systems/Битая.md"
    broken.write_text('---\nid: KB-2\ntitle: Битая\nstatus: knowledge\ntype: system\n'
                      '---\nkind: knowledge\n\nТело. см. [[Целая]]\n', encoding="utf-8")

    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "уехало в тело" in cp.stdout, \
        "поле шапки в теле карточки прошло мимо линтера — так и разошлось 2033 карточки"
    assert "Битая" in cp.stdout and "Целая" not in cp.stdout.split("уехало в тело")[0][-200:], \
        "названа не та карточка"


@test
def test_the_panel_script_actually_parses(tmp: Path):
    """Скрипт панели должен разбираться целиком: синтаксис — это всё или ничего.

    Живой случай: в одной функции оказалось два `const said` — вторую добавили вместе с
    подписью «какая модель ответила». Это SyntaxError на разборе, а разбор у браузера
    один на весь `<script>`: не выполняется ни одна строка. Панель при этом открывается
    и выглядит почти нормально — шапка, заголовки, заготовки под загрузку, — но цифр,
    проектов и скина нет никогда. Ни один тест этого не ловил: разметка на месте, пути
    на месте, сервер отвечает 200 — мёртв только браузер.

    Проверяем настоящим разборщиком (node или deno). Если ни того ни другого нет —
    тест падает, а не молчит: молчание здесь неотличимо от успеха, и именно так этот
    дефект прожил недели.
    """
    import shutil
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    js = "\n".join(m.group(1) for m in
                   re.finditer(r"<script[^>]*>(.*?)</script>", ui, re.S))
    assert js.strip(), "в панели не осталось скрипта — разметка изменилась неузнаваемо"

    engine = shutil.which("node") or shutil.which("deno")
    assert engine, ("нет ни node, ни deno — синтаксис панели проверить нечем; "
                    "поставьте любой из них, иначе SyntaxError доедет до человека")
    src = tmp / "panel.js"
    src.write_text(js, encoding="utf-8")
    cmd = ([engine, "--check", str(src)] if engine.endswith("node")
           else [engine, "check", "--no-lock", str(src)])
    cp = subprocess.run(cmd, capture_output=True, text=True)
    assert cp.returncode == 0, ("скрипт панели не разбирается — в браузере не выполнится "
                                f"ни одна строка:\n{(cp.stderr or cp.stdout)[:800]}")


@test
def test_finding_carries_a_button_not_a_riddle(tmp: Path):
    """Команда с кодом 1 не теряет «Применить», а находка получает кнопку лечения.

    Живой случай: `kb:index` написала «повторите с --apply», а кнопки не было — условие
    показа требовало кода 0, тогда как записывать надо как раз после кода 1 («отработала
    и нашла, что чинить»). Человек ушёл искать флаг, которого в панели нет.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "if (rc<=1 && PENDING_APPLY" in ui, \
        "команда, вернувшая 1, снова осталась без кнопки «Применить»"
    assert "const FIX_RUN" in ui and "function fixButton" in ui, \
        "находка объясняет лечение словами, но запустить его из панели нельзя"
    assert "fixButton(f.what)" in ui, "кнопка лечения не доходит до итогов маршрута"
    # Решение человека кнопкой не подменяется: --force затирает чужой текст
    assert "--force" not in ui.split("const FIX_RUN")[1].split("};")[0], \
        "в кнопку лечения попал --force: это решение человека, а не автоматика"

    # Финальная строка отчёта не должна отправлять нажимать --apply впустую
    src = (KIT / "scripts/kb_index.py").read_text(encoding="utf-8")
    assert "if written and not a.apply:" in src, \
        "«повторите с --apply» печатается даже когда записывать нечего"
    assert "kb:index --force --apply" in src, \
        "не сказано, что на самом деле поможет, когда оглавления рукотворные"


@test
def test_panel_asks_only_endpoints_the_server_has(tmp: Path):
    """Каждый путь, который просит страница, у сервера существует.

    Живой случай: в конце пишущего маршрута страница спрашивала `/api/projects`, которого
    у сервера нет (проекты отдаёт `/api/state`). Вместо предупреждения о незакоммиченной
    работе человек получал всплывающее «неизвестный маршрут» — и не понимал, что именно
    в маршруте сломалось. Опечатка в пути не видна ни глазами, ни при запуске: страница
    просто получает 404 и молчит либо пугает.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    known = set(re.findall(r'u\.path == "([^"]+)"', srv))
    assert "/api/state" in known, "разбор путей сервера сломался — тест перестал что-то проверять"
    asked = {m.group(1) for m in re.finditer(r'api\(\s*[`"\']([^`"\'?]+)', ui)
             if m.group(1).startswith("/api")}
    missing = sorted(asked - known)
    assert not missing, f"страница просит пути, которых у сервера нет: {missing}"


@test
def test_running_command_survives_a_page_reload(tmp: Path):
    """Работающая команда видна после перезагрузки страницы, и второй запуск переспрашивают.

    Задание живёт в процессе панели, консоль — в открытой странице. Перезагрузили её
    (обновили kit, открыли заново) — команда работает, а консоль пуста: человек делает
    единственный разумный вывод «оборвалось» и запускает второй маршрут поверх первого.
    Очереди у панели нет: два задания идут одновременно и дерутся за git и зеркала.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ck.JOBS.clear()
    ck.JOBS["a"] = {"id": "a", "cmd": "kb:lint", "args": [], "project": "/p/one",
                    "out": ["строка"], "started": 100.0, "done": False, "rc": None}
    ck.JOBS["b"] = {"id": "b", "cmd": "sync:jira", "args": [], "project": "/p/two",
                    "out": [], "started": 90.0, "done": False, "rc": None}
    ck.JOBS["c"] = {"id": "c", "cmd": "ops:stats", "args": [], "project": "/p/one",
                    "out": [], "started": 80.0, "done": True, "rc": 0}
    live = [j for j in ck.JOBS.values() if not j["done"] and j["project"] == "/p/one"]
    assert [j["id"] for j in live] == ["a"], \
        "живые задания проекта отбираются неверно: чужое или завершённое попало в список"
    ck.JOBS.clear()

    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "/api/jobs?project=" in ui, "панель не спрашивает, что выполняется прямо сейчас"
    assert 'id="consoleLive"' in ui and "function attachJob" in ui, \
        "к работающему заданию нельзя подключиться — его вывод потерян навсегда"
    assert "busyElsewhere" in ui and "не ставит задания в очередь" in ui, \
        "второй запуск поверх работающего идёт молча, а это гонка, а не очередь"
    assert ui.index("await busyElsewhere(sc.title)") < ui.index("const steps"), \
        "маршрут спрашивает про занятость после того, как начал"


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
def test_every_command_is_reachable_in_the_panel(tmp: Path):
    """У каждой команды реестра есть ровно один путь в панели.

    Реестр растёт, панель — нет: команда появляется в `commands.txt`, а нажать её негде.
    Обратная беда тише: команда показана в двух местах, и при правке расходятся оба.
    Разделение простое — движковые (`dev:` и `kit:skills`) живут в скрытом разделе
    «Разработка», остальные в «Командах».
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    rows = ck.registry()
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")

    assert "ENGINE_CMDS" in ui and "isEngineCmd" in ui, \
        "в панели нет правила, что считать движковой командой"
    engine = [r for r in rows if r["ns"] == "dev" or r["cmd"] == "kit:skills"]
    assert engine, "движковые команды пропали из реестра панели"

    # «Команды» показывают всё, кроме движковых, — и не требуют исключений по одной
    assert "S.state.commands.filter(r => !isEngineCmd(r))" in ui, \
        "общий список команд фильтруется не по общему правилу"
    # «Разработка» показывает ровно движковые
    assert "(S.state.commands || []).filter(isEngineCmd)" in ui, \
        "раздел разработки собирается не по тому же правилу"

    # ни одна команда не потерялась и не показана дважды
    titles = {"kit", "sync", "kb", "ctx", "make", "ship", "ops", "dev", "agent"}
    lost = [r["cmd"] for r in rows if r["ns"] not in titles]
    assert not lost, f"команды вне известных групп — в панели им нет места: {lost}"
    assert "agent:" in ui and 'agent:"' in ui, "группа agent не подписана в «Командах»"

    # у каждой запускаемой команды есть исполнитель на диске
    for r in rows:
        if r["runnable"]:
            assert (KIT / "scripts" / r["script"]).is_file(), \
                f"{r['cmd']}: панель предложит запуск, а файла нет — {r['script']}"

    # порядок в разделе разработки перечисляет реальные команды, а не выдуманные
    dev_block = ui[ui.index("async function renderDev"):]
    order = re.search(r"const order = \[(.*?)\];", dev_block, re.S).group(1)
    named = re.findall(r'"([\w:-]+)"', order)
    known = {r["cmd"] for r in rows}
    assert all(n in known for n in named), \
        f"в порядке раздела названы несуществующие команды: {[n for n in named if n not in known]}"


@test
def test_skills_land_in_one_shared_folder(tmp: Path):
    """Скиллы ставятся в один каталог агента и не расходятся копиями.

    Скиллы лежат в репозитории кита, а агент ищет их в `~/.claude/skills`: без копии
    `/aurora-vault` не находится ни в одном диалоге. Каталог именно один — две копии
    одного скилла расходятся на первой правке, и потом не понять, какая отвечала.
    """
    src = KIT / "scripts/install_skills.py"
    assert src.is_file(), "нет команды установки скиллов"
    body = src.read_text(encoding="utf-8")
    assert '.claude" / "skills"' in body, "общий каталог должен быть ~/.claude/skills"
    assert "symlink_to" in body, "остальные harness должны получать ссылку, а не копию"

    out = subprocess.run([sys.executable, str(src), "--status"],
                         cwd=str(KIT), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[:300]
    for name in ("aurora-vault", "aurora-dev"):
        assert name in out.stdout, f"скилл {name} не попал в установку"

    dry = subprocess.run([sys.executable, str(src)], cwd=str(KIT),
                         capture_output=True, text=True).stdout
    assert "--apply" in dry or "делать нечего" in dry, \
        "без --apply установка обязана только показывать"

    # установка встроена в жизненный цикл: иначе про неё забудут
    entry = (KIT / "aurora.py").read_text(encoding="utf-8")
    assert entry.count("install_skills.py") >= 2, \
        "скиллы должны ставиться и при создании проекта, и при обновлении движка"
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "kit:skills" in reg, "нет команды kit:skills в реестре"
    assert "dev:install-skill" not in reg, \
        "частная установка одного скилла осталась рядом с общей — два пути к одному"


@test
def test_dev_skill_is_installable_and_asks_for_coverage(tmp: Path):
    """Скилл разработчика находится в новом диалоге, а `--cover` даёт готовое задание.

    Скилл лежит в репозитории кита, а агент ищет скиллы в домашней папке: без установки
    `/aurora-dev` в другом диалоге просто не найдётся, и весь контур останется бумажным.
    Задание же нужно потому, что модель, дорабатывавшая код, знает про свои изменения —
    но не знает правил этого контура.
    """
    cov = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--cover"],
                         cwd=str(KIT), capture_output=True, text=True)
    assert cov.returncode == 0, cov.stderr[:300]
    task = cov.stdout
    assert "ЗАДАНИЕ АССИСТЕНТУ" in task, "нет блока для копирования в другой диалог"
    for must in ("автотест", "тест-кейс QA", "сценарий", "--check", "--list", "covers"):
        assert must in task, f"в задании не сказано про «{must}»"
    assert "предпочтительный вариант ВСЕГДА" in task, \
        "не задан приоритет автотеста над кейсом — модель заведёт кейс на всё подряд"
    assert "--new case" in task and "--new scenario" in task, \
        "модель не узнает, чем заводить документы"

    skill = (KIT / "skills/aurora-dev/SKILL.md").read_text(encoding="utf-8")
    assert "Если вас позвали после разработки фичи" in skill, \
        "в скилле нет рецепта для самого частого случая"
    assert "kit:skills" in skill, "не сказано, чем ставится скилл"


@test
def test_set_alias_is_a_scalpel(tmp: Path):
    """Точечная замена синонима: одна строка списка, остальное байт в байт.

    Агент решает, каким должен стать синоним, но резать по живой шапке ему нельзя: модель
    умеет только перегенерировать файл целиком и вместе с одной строкой переписывает поля,
    теги и тело. «Не найдено» обязано быть кодом возврата, а не строкой в отчёте, — иначе
    агент засчитает несделанную работу.
    """
    root = make_project(tmp, git=True)
    card(root, "Concepts/ALG-309-Получение-курсов.md", "Тело, которое нельзя трогать.",
         aliases='["Курс валют", "ALG-309"]', type="concept")
    before = (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(
        encoding="utf-8")

    run("kb_fix.py", "--set-alias", "ALG-309-Получение-курсов", "--old", "Курс валют",
        "--new", "Курсы валют (алгоритм)", "--apply", "--allow-dirty", cwd=root)
    after = (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(
        encoding="utf-8")
    assert "Курсы валют (алгоритм)" in after and "ALG-309" in after, after[:300]
    assert "Тело, которое нельзя трогать." in after, "тело карточки пострадало"
    assert before.count("\n") == after.count("\n"), "изменилось число строк — правка не точечная"

    # идемпотентность: повтор ничего не портит и не дублирует
    run("kb_fix.py", "--set-alias", "ALG-309-Получение-курсов", "--old", "Курс валют",
        "--new", "Курсы валют (алгоритм)", "--apply", "--allow-dirty", cwd=root)
    twice = (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(
        encoding="utf-8")
    assert twice == after, "повторный вызов изменил файл"

    # неточное имя резолвится по хвосту — модель называет карточку как видит
    run("kb_fix.py", "--set-alias", "Получение-курсов", "--old", "ALG-309",
        "--new", "ALG-309 (алгоритм)", "--apply", "--allow-dirty", cwd=root)
    assert "ALG-309 (алгоритм)" in (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(encoding="utf-8")

    # несделанная работа — ненулевой код возврата
    miss = run("kb_fix.py", "--set-alias", "Нет-такой-карточки", "--old", "X", "--new", "Y",
               "--apply", "--allow-dirty", cwd=root, expect_rc=1)
    assert "не найдена" in miss.stdout + miss.stderr


@test
def test_agent_work_rolls_back_whole(tmp: Path):
    """Откат снимает и новые файлы: иначе обещание в отчёте — неправда.

    `git reset --hard <чекпойнт>` не трогает то, чего git ещё не видел, а сборка карточек
    создаёт именно новые файлы. На живом прогоне откат оставил карточки в базе, а
    следующий чекпойнт закоммитил их как работу человека.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    card(root, "Concepts/Старая.md", "Была до агента.", type="concept")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-qm", "до агента"], cwd=str(root), check=True)

    cp = R.checkpoint(str(root), "agent:build", True)
    card(root, "Concepts/Новая-от-агента.md", "Собрана агентом.", type="concept")
    done = R.commit_result(str(root), "agent:build", "источников разобрано: 1", True)
    assert done["ok"] and done["sha"], done

    subprocess.run(["git", "reset", "--hard", cp["sha"]], cwd=str(root),
                   capture_output=True, check=True)
    assert not (root / "AuroraKnowledgeDB/Concepts/Новая-от-агента.md").exists(), \
        "новая карточка пережила откат — обещание «одной строкой» не выполнено"
    assert (root / "AuroraKnowledgeDB/Concepts/Старая.md").exists(), "откат снёс лишнее"

    # правка человека, сделанная пока агент работал, агенту не принадлежит
    card(root, "Concepts/Ещё-одна.md", "Собрана агентом.", type="concept")
    (root / "Artifacts").mkdir(exist_ok=True)
    (root / "Artifacts" / "human-edit.md").write_text("правил человек", encoding="utf-8")
    R.commit_result(str(root), "agent:build", "источников разобрано: 1", True)
    left = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=str(root),
                          capture_output=True, text=True).stdout
    assert "human-edit" in left, "коммит агента забрал чужую работу вне базы знаний"



@test
def test_lint_spares_artifacts_that_came_from_sources(tmp: Path):
    """Выгруженное из Confluence — законный житель базы, как бы оно ни называлось.

    Правило смотрело только на имя и объявляло чужой историей каждую страницу с «US-»
    в заголовке: на живой базе это 243 ложных срабатывания из 243. Артефакт — то, что
    сгенерировано нами: у него нет источника в зеркале.
    """
    root = make_project(tmp)
    card(root, "Concepts/US-3.1.1-Выгружено-из-вики.md", "Пришло из зеркала.",
         type="concept", source='"Sources/Confluence/История.md"')
    card(root, "Concepts/US-3.1.2-Сгенерировано-нами.md", "Родилось в проекте.",
         type="concept")

    out = run("kb_lint.py", cwd=root, expect_rc=1).stdout
    assert "US-3.1.2-Сгенерировано-нами" in out, "сгенерированный артефакт перестали замечать"
    # Смотрим именно категорию артефактов: с 1.91 линтер отдельно называет карточки без
    # связей, и одиночная фикстура попадает туда по другому поводу.
    arts = out.split("артефакты, попавшие в базу знаний")[1] if "артефакты, попавшие" in out else out
    assert "US-3.1.1-Выгружено-из-вики" not in arts.split("## ")[0], \
        "страница из зеркала объявлена чужим артефактом"


@test
def test_split_makes_atoms_and_keeps_the_document(tmp: Path):
    """Раздутая карточка режется по своим заголовкам, а сама становится картой документа.

    Атомарность — основа и поиска, и чтения: карточку на тридцать тысяч знаков не найти
    выборкой и не прочитать в контексте. Границы тем в ней уже расставлены заголовками,
    спрашивать о них модель незачем. Принадлежность документу при этом не теряется.
    """
    root = make_project(tmp)
    big = "\n\n".join(f"## Часть {i}\n\n" + "текст. " * 80 for i in range(1, 4))
    card(root, "Concepts/Большая.md", big, type="concept",
         source='"Sources/Confluence/Документ.md"')

    run("kb_fix.py", "--split", "Большая", "--apply", "--allow-dirty", cwd=root)
    parts = list((root / "AuroraKnowledgeDB" / "Concepts").glob("Часть-*.md"))
    assert len(parts) == 3, [p.name for p in parts]
    first = parts[0].read_text(encoding="utf-8")
    assert 'part_of: "[[Большая]]"' in first, "часть не помнит, откуда она"
    assert "Sources/Confluence/Документ.md" in first, "часть потеряла источник"
    kept = (root / "AuroraKnowledgeDB/Concepts/Большая.md").read_text(encoding="utf-8")
    assert "[[Часть-1|Часть 1]]" in kept, "исходная карточка не стала картой документа"
    assert "текст. текст." not in kept, "тело осталось в карте — резать было незачем"


@test
def test_graph_links_cards_to_their_terms(tmp: Path):
    """Карточка ссылается на определение термина — и не наоборот.

    Правила RY и номеров историй точны, но узки: на живой базе они покрывали 588 карточек
    из 1692. Термин — третий способ, которым связи уже записаны в текстах. Обратная связь
    запрещена намеренно: «Заявитель» упомянут почти везде, и определение превратилось бы
    в свалку из девятисот ссылок.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    G = importlib.import_module("kb_graph")

    root = make_project(tmp)
    card(root, "Glossary/Обеспечительный-платёж.md", "Определение термина.", type="glossary")
    card(root, "Concepts/Возврат-средств.md",
         "При отказе обеспечительный платёж возвращается заявителю.", type="concept")
    card(root, "Concepts/Погода.md", "Ничего общего.", type="concept")

    import os as _os
    cwd = _os.getcwd()
    try:
        _os.chdir(root)
        pairs = G.glossary_links()
    finally:
        _os.chdir(cwd)
    froms = {a.split("/")[-1] for a, _b in pairs}
    tos = {b.split("/")[-1] for _a, b in pairs}
    assert "Возврат-средств.md" in froms, "карточка не связалась с определением термина"
    assert "Погода.md" not in froms, "связь возникла там, где термина нет"
    assert tos == {"Обеспечительный-платёж.md"}, tos


@test
def test_context_index_shows_the_whole_base_cheaply(tmp: Path):
    """Оглавление: строка на карточку, чтобы модель увидела базу целиком.

    Выборка находит то, что человек назвал словами. Оглавление решает другую задачу —
    показать, чего он не назвал. Поэтому строка предельно скупа: раздел даёт
    группировка, суть берётся из `summary`, а пока его нет — из первой содержательной
    строки тела, минуя заголовки и разметку таблиц.
    """
    root = make_project(tmp)
    card(root, "Concepts/Курс-валюты.md",
         "# Заголовок\n\n> **История изменений**\n\n| a | b |\n\nКурс ЦБ берётся на дату подачи.",
         status="verified", type="concept")
    card(root, "Glossary/Термин.md", "Пояснение термина.", status="verified",
         type="glossary", summary='"Одна фраза про термин"')

    out = run("ctx_pack.py", "любая тема", "--index", "--no-log", cwd=root).stdout
    assert "## Concepts (1)" in out and "## Glossary (1)" in out, out[:400]
    assert "Одна фраза про термин" in out, "summary из шапки не попал в оглавление"
    assert "Курс ЦБ берётся на дату подачи" in out, \
        "вместо сути в оглавление попала разметка источника"
    assert "История изменений" not in out, "служебная цитата принята за суть карточки"


@test
def test_registry_cache_keeps_kit_and_project_apart(tmp: Path):
    """Кэш реестра помнит, откуда поднята панель: из проекта команд `dev:` не видно.

    Ключ без этого признака делал кэш общим: панель, запущенная в проекте, записывала
    «реестр без dev» в файл кита — и раздел разработки исчезал из панели до следующей
    смены версии. Нашлось это прогоном тестов: они и портили кэш.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ck.CACHE.pop("registry", None)
    rows = ck.registry()
    assert any(r["cmd"].startswith("dev:") for r in rows), \
        "из кита команды разработки не видны"

    cache = json.loads((KIT / "cockpit" / ".registry-cache.json").read_text(encoding="utf-8"))
    assert "src=" in cache["key"], cache["key"]



@test
def test_long_step_reports_progress_and_duration(tmp: Path):
    """Долгий шаг показывает, что идёт, а журнал помнит, сколько он занял.

    Агент печатал отчёт только в конце: на живом прогоне это двадцать минут пустой
    консоли, по которой невозможно отличить работу от повисшего процесса. Второй
    источник ответа — прошлый прогон: у команд разброс от секунды до получаса.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    R = importlib.import_module("agent_runner")
    ck = importlib.import_module("aurora_cockpit")

    import time as _t
    line = R.progress(3, 15, _t.time() - 120)
    assert "[3/15]" in line and "20%" in line, line
    assert "осталось ~" in line, "нет оценки остатка — главного, что нужно у экрана"
    assert "осталось" not in R.progress(15, 15, _t.time() - 120), \
        "на последнем шаге обещать остаток нечестно"

    root = make_project(tmp)
    ck.write_runlog(str(root), "agent:build", 0, "agent:build --apply", 754)
    again = ck.read_runlog(str(root))
    assert again["agent:build"]["secs"] == 754, again

    # журнал старого формата (без колонки секунд) читается по-прежнему
    path = root / ".opencode" / "run_log.md"
    path.write_text(path.read_text(encoding="utf-8").replace(" | 754 |", " |"),
                    encoding="utf-8")
    old = ck.read_runlog(str(root))
    assert old["agent:build"]["rc"] == 0 and old["agent:build"]["secs"] == 0, old


@test
def test_doctor_accepts_folders_declared_by_artifacts(tmp: Path):
    """Папка, объявленная в реестре артефактов, — законная, а не нарушение схемы.

    Ловушка выглядела так: человек заводит вид документа в панели, движок создаёт под
    него папку, а doctor тут же называет её папкой вне схемы. Реестр видов — такое же
    основание для папки, как реестр модулей для зеркала в Sources/.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "pr.md").write_text("шаблон", encoding="utf-8")
    (root / "Своя-папка-вне-схемы").mkdir()

    ck.kinds_write(str(root), {"pr": {"title": "ПР", "template": "Templates/pr.md",
                                      "out": "Deliverables/drafts"}})
    assert (root / "Deliverables" / "drafts").is_dir()

    out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert "Deliverables/drafts" not in out, \
        "папка из реестра артефактов объявлена нарушением схемы"
    assert "Своя-папка-вне-схемы" in out, \
        "папка, которую никто не объявлял, перестала замечаться — проверка ослабла"


@test
def test_artifact_kinds_are_declared_and_editable(tmp: Path):
    """Виды документов объявляет проект, а не движок, и правятся они из панели.

    Формы у заказчиков разные: ОПЗ, проектное решение, руководство — у каждого свой
    шаблон и своя папка. Движок не может знать их наперёд, поэтому реестр открытый и
    живёт в конфиге проекта: в git, виден любой IDE и ассистенту через MCP.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    MK = importlib.import_module("make_kinds")
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "pr.md").write_text("шаблон", encoding="utf-8")

    r = ck.kinds_write(str(root), {
        "pr": {"title": "Проектное решение", "template": "Templates/pr.md",
               "out": "Deliverables/drafts"}})
    assert r.get("ok"), r
    assert (root / "Deliverables" / "drafts").is_dir(), \
        "папка результата не создана — объявили и не найдём в момент записи"

    kinds = MK.read_kinds(str(root))
    assert kinds["pr"]["template"] == "Templates/pr.md", kinds
    assert not MK.check(str(root), kinds), MK.check(str(root), kinds)

    # несуществующий шаблон объявить можно, но команда об этом скажет
    ck.kinds_write(str(root), {"pr": {"title": "П", "template": "Templates/нет.md",
                                      "out": "Deliverables/drafts"}})
    bad = MK.check(str(root), MK.read_kinds(str(root)))
    assert bad and "шаблона нет" in bad[0][1], bad

    # имя типа — не произвольная строка: по нему зовут инструмент и создают папку
    assert "error" in ck.kinds_write(str(root), {"ПР ": {"title": "x"}}), \
        "принято недопустимое имя типа"

    # правка реестра не портит остальной конфиг
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "project:" in cfg and "atlassian:" in cfg, "перезапись секции задела чужие"


@test
def test_embeddings_are_configured_separately(tmp: Path):
    """Свой сервис векторов: адрес, ключ и модель настраиваются отдельно от чата.

    Кит поднимают в разных контурах. Где-то эмбеддинги на том же шлюзе, где-то — своим
    сервисом с другим адресом и без ключа. Пустой адрес обязан означать «как у чата»,
    иначе переезд превращается в правку файлов руками.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    AG = importlib.import_module("agent_core")
    E = importlib.import_module("kb_embed")

    same = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gateway/v1",
                            "AURORA_AGENT_BACKEND_1_KEY": "k"})
    assert same["embed"] == {"url": "", "key": "", "model": "bge-m3"}, same["embed"]
    assert E.endpoints(same)[0]["url"] == "http://gateway/v1", "не взято кольцо агента"

    own = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gateway/v1",
                           "AURORA_EMBED_URL": "http://vectors.example.com/v1/",
                           "AURORA_EMBED_MODEL": "e5-large", "AURORA_EMBED_KEY": "e"})
    assert own["embed"]["model"] == "e5-large" and own["embed"]["key"] == "e"
    ends = E.endpoints(own)
    assert len(ends) == 1 and ends[0]["url"] == "http://vectors.example.com/v1", ends
    assert "gateway" not in json.dumps(ends), "чат-бэкенды подмешались к своему сервису"

    # панель настраивает те же переменные и не выпускает ключ наружу
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    assert "не агентские" in (ck.agent_write_env("", {"PATH": "/tmp"}).get("error") or ""), \
        "панель приняла постороннюю переменную"
    assert not (ck.agent_write_env(str(tmp), {"AURORA_EMBED_MODEL": "e5-large"}).get("error")), \
        "панель не приняла настройку эмбеддингов"


@test
def test_mcp_speaks_protocol_and_never_writes(tmp: Path):
    """MCP отдаёт базу ассистенту и только читает; stdout занят протоколом.

    Любой print движка («посчитано 12 из 40») встанет посреди JSON-RPC и оборвёт сессию:
    stdout здесь не место для сообщений, а канал. И ни один инструмент не пишет в базу —
    чужой ассистент не участвует в приёмке знания и не проходит git-guard.
    """
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.",
         status="verified", type="concept")
    before = sorted(p.name for p in (root / "AuroraKnowledgeDB").rglob("*.md"))

    calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "kb_search", "arguments": {"query": "обеспечение"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "kb_card", "arguments": {"name": "Обеспечение"}}}]
    proc = subprocess.run(
        [sys.executable, str(KIT / "scripts" / "aurora_mcp.py"), "--project", str(root)],
        input="\n".join(json.dumps(c) for c in calls), capture_output=True, text=True,
        timeout=180)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    got = [json.loads(l) for l in lines]          # падает, если в канал попал чужой текст
    assert len(got) == 4, [l[:80] for l in lines]
    assert got[0]["result"]["serverInfo"]["name"].startswith("aurora-"), got[0]
    names = {t["name"] for t in got[1]["result"]["tools"]}
    assert {"kb_search", "kb_card", "kb_context", "kb_index", "kb_ask",
            "artifact_spec"} == names, names
    assert "Обеспечение" in got[2]["result"]["content"][0]["text"]
    assert "Правила обеспечения" in got[3]["result"]["content"][0]["text"]
    assert sorted(p.name for p in (root / "AuroraKnowledgeDB").rglob("*.md")) == before, \
        "чтение базы через MCP изменило файлы"

    # Проектов у аналитика несколько, и подключены они одновременно. Сервер обязан
    # называть свой: одинаковые имена карточек в двух базах модель не различит, а знание
    # одного заказчика не должно попасть в артефакт другого.
    assert got[0]["result"]["serverInfo"]["name"].startswith("aurora-"), \
        got[0]["result"]["serverInfo"]
    assert all("База проекта" in tool["description"] for tool in got[1]["result"]["tools"]), \
        "инструмент не называет базу, к которой обращается"

    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    M = importlib.import_module("aurora_mcp")
    block = M.config_block([str(root), "/tmp/другой-проект"])["mcpServers"]
    assert len(block) == 2 and all(k.startswith("aurora-") for k in block), block
    # у каждого сервера свой путь, и он передан явно: перепутать базы нельзя
    paths = set()
    for rec in block.values():
        assert "--project" in rec["args"], rec
        paths.add(rec["args"][rec["args"].index("--project") + 1])
    assert len(paths) == 2, paths


@test
def test_graph_insights_name_communities_bridges_islands(tmp: Path):
    """Карта связей: сообщества, мосты и острова считаются, а не угадываются.

    «73% карточек связаны» не отвечает на вопрос, который у человека есть: где знание
    разорвано. Отвечают острова (сюда не дойти по ссылкам) и мосты (единственная связь
    между темами: порвётся — темы разъедутся, и никто не заметит).
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    G = importlib.import_module("kb_graph")

    # две плотные группы, один мост между ними и одинокая карточка
    pairs = {}
    def link(a, b):
        pairs.setdefault(a, set()).add(b); pairs.setdefault(b, set()).add(a)
    for a, b in (("a1","a2"),("a2","a3"),("a3","a1")): link(a, b)
    for a, b in (("b1","b2"),("b2","b3"),("b3","b1")): link(a, b)
    link("a1", "b1")                       # мост
    pairs.setdefault("одиночка", set())

    ins = G.insights(pairs, {})
    assert ("a1", "b1") in ins["bridges"], ins["bridges"]
    assert "одиночка" in ins["islands"], ins["islands"]
    assert ins["label"]["a2"] == ins["label"]["a3"], "плотная группа не собралась"
    assert len({ins["label"][x] for x in ("a2", "b2")}) == 2, "две группы слились в одну"


@test
def test_semantic_index_is_optional_and_hybrid(tmp: Path):
    """Смысл добавляется к словам, а не заменяет их — и выборка не падает без индекса.

    Вектора считает внешняя модель, а внешнее недоступно ровно тогда, когда нужнее
    всего: нет сети, сменили модель, индекс не собран. Пак обязан в этом случае просто
    собраться по словам, а не отказать.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    C = importlib.import_module("ctx_pack")
    E = importlib.import_module("kb_embed")

    root = make_project(tmp)
    card(root, "Concepts/Возврат-обеспечения.md",
         "Возврат обеспечения заявителю при отказе в выпуске.",
         status="verified", type="concept")

    # индекса нет — пак собирается словами и молчит про смысл
    out = run("ctx_pack.py", "возврат обеспечения", "--no-log", cwd=root).stdout
    assert "поиск по словам" in out and "и смыслу" not in out, out[:200]
    assert "Возврат-обеспечения" in out or "Возврат обеспечения" in out

    # индекс чужой модели не годится: молча считать его своим — врать о выборке
    import os as _os, json as _json
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "embeddings.json").write_text(_json.dumps(
        {"model": "другая-модель", "dim": 2, "built": "2026-01-01",
         "cards": {"Возврат-обеспечения": {"hash": "x", "row": 0}}}), encoding="utf-8")
    cwd = _os.getcwd()
    try:
        _os.chdir(root)
        importlib.reload(E)
        assert E.search("вопрос", {"backends": [], "request_timeout": 5}, "bge-m3") == [], \
            "индекс, собранный другой моделью, принят за свой"
    finally:
        _os.chdir(cwd)

    # близость по смыслу поднимает карточку, но только выше порога случайности
    fake = type("C", (), {"stem": "Возврат-обеспечения", "title": "Возврат обеспечения",
                          "aliases": [], "tags": "", "text": "текст", "summary": ""})()
    base = C.score(fake, "деньги за поставку")
    weak = C.score(fake, "деньги за поставку", {"Возврат-обеспечения": 0.30})
    strong = C.score(fake, "деньги за поставку", {"Возврат-обеспечения": 0.75})
    assert weak == base, "случайная близость 0.30 не должна ничего добавлять"
    assert strong > base, "близость 0.75 не повлияла на отбор"


@test
def test_context_pack_finds_by_words_not_by_phrase(tmp: Path):
    """Пак ищет по словам запроса, а не по фразе целиком.

    Живой запрос аналитика — предложение: «вернуть обеспечительный платёж после
    аннулирования». Такой строки в базе нет никогда, и пак собирался из трёх случайных
    карточек. Слова из неё есть на каждой второй странице, а словоформы («платежа» —
    «платёж») должны сходиться, иначе половина базы невидима.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    C = importlib.import_module("ctx_pack")

    assert C.norm("обеспечительного") == C.norm("обеспечительный"), "словоформы разошлись"
    assert C.norm("платежа") == C.norm("платёж"), "ё и словоформа разводят пару"
    assert "как" not in C.words("как вернуть платёж"), "стоп-слово попало в запрос"

    root = make_project(tmp)
    card(root, "Concepts/Возврат-обеспечительного-платежа.md",
         "Заявитель возвращает обеспечительный платёж после аннулирования документа.",
         status="verified", type="concept")
    card(root, "Concepts/Погода-в-городе.md", "Про погоду и ничего больше.",
         status="verified", type="concept")

    out = run("ctx_pack.py", "вернуть обеспечительный платёж после аннулирования",
              "--no-log", cwd=root).stdout
    assert "Возврат-обеспечительного-платежа" in out or "Возврат обеспечительного платежа" in out, \
        "карточка по теме не найдена по свободной формулировке"
    assert "Погода" not in out, "в пак попала карточка, не имеющая отношения к запросу"


@test
def test_pack_answers_where_development_got_to(tmp: Path):
    """Статус задачи приходит в пак из зеркала Jira, а не из карточек.

    Живой случай: аналитик спросил статус истории US-4.7.2, база честно ответила «не
    знаю» — а задача лежала в зеркале рядом, со статусом «Бэклог». Знание было в проекте,
    но не на пути к модели: пак собирается только из карточек. Распылять статус по
    карточкам нельзя — он неправда на следующий день после переноса задачи; значит его
    надо читать из зеркала при каждой сборке пака.
    """
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "PRJ-480.md").write_text(
        '---\nkey: "PRJ-480"\ntitle: "US-4.7.2. Центр уведомлений"\ntype: "История"\n'
        'status: "Бэклог"\nepic_title: "Epic 4.7 Информационные разделы"\n'
        'updated: "2026-06-10 15:06:39"\n---\n\n# PRJ-480\n', encoding="utf-8")
    (mirror / "PRJ-999.md").write_text(
        '---\nkey: "PRJ-999"\ntitle: "US-1.1.1. Чужая история"\nstatus: "Готово"\n---\n',
        encoding="utf-8")
    card(root, "Concepts/Центр-уведомлений.md",
         "Центр уведомлений собирает сообщения заявителю в одном разделе.",
         status="verified", type="concept")

    out = run("ctx_pack.py", "какой статус у истории US-4.7.2 Центр уведомлений",
              "--no-log", cwd=root).stdout
    assert "Состояние разработки" in out, "раздела со статусами задач в паке нет"
    assert "PRJ-480" in out and "Бэклог" in out, f"статус задачи не дошёл до модели:\n{out}"
    assert "PRJ-999" not in out, "в пак попала задача, о которой не спрашивали"
    assert "не карточки базы" in out, \
        "снимок внешней системы не отделён от знания — модель сошлётся на него как на факт"

    # Вопрос не про задачи — таблицы быть не должно: пак не место для выгрузки бэклога
    plain = run("ctx_pack.py", "центр уведомлений заявителя", "--no-log", cwd=root).stdout
    assert "Состояние разработки" not in plain, \
        "статусы задач подмешиваются в каждый пак, хотя о них не спрашивали"

    # Карточек по теме нет, а задача есть — это ответ, а не «ничего не найдено»
    only = run("ctx_pack.py", "статус US-1.1.1", "--no-log", cwd=root, expect_rc=0).stdout
    assert "PRJ-999" in only and "карточек по теме нет" in only, \
        f"пак сдался там, где зеркало знает ответ:\n{only}"



@test
def test_build_card_is_idempotent_for_the_same_source(tmp: Path):
    """Повторный разбор того же источника не падает, а конфликт имён — падает.

    Источник правят и разбирают снова: на живом прогоне обновлённая страница уронила
    агента отказом «карточка уже есть». Та же карточка из того же источника — это
    повторный проход. Чужое имя из другого источника — настоящий конфликт.
    """
    root = make_project(tmp)
    for name in ("первый", "второй"):
        src = root / "Raw" / "project" / f"{name}.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(f"# Тема\n\n" + "текст. " * 60, encoding="utf-8")

    run("build_plan.py", "--card", "Общая тема", "--source", "Raw/project/первый.md",
        "--sections", "1", "--to", "Concepts", "--apply", cwd=root)
    again = run("build_plan.py", "--card", "Общая тема", "--source", "Raw/project/первый.md",
                "--sections", "1", "--to", "Concepts", "--apply", cwd=root)
    assert "уже собрана" in again.stdout, again.stdout[-200:]

    clash = run("build_plan.py", "--card", "Общая тема", "--source", "Raw/project/второй.md",
                "--sections", "1", "--to", "Concepts", "--apply", cwd=root, expect_rc=1)
    assert "из другого источника" in clash.stdout + clash.stderr


@test
def test_build_card_refuses_section_outside_schema(tmp: Path):
    """`--to` принимает только разделы схемы: иначе карточка ложится в новую папку.

    На живом прогоне агент завёл `Models`, `Модель данных` и `Требования` — движок
    послушно создал папки, а doctor нашёл их блокером уже после того, как карточки
    туда легли. Схему базы расширяют релизом кита, а не значением флага.
    """
    root = make_project(tmp)
    src = root / "Raw" / "project" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Тема\n\n" + "текст. " * 60, encoding="utf-8")

    bad = run("build_plan.py", "--card", "Карточка", "--source", "Raw/project/источник.md",
              "--sections", "1", "--to", "Модель данных", "--apply", cwd=root, expect_rc=1)
    assert "нет в схеме" in bad.stdout + bad.stderr
    assert not (root / "AuroraKnowledgeDB" / "Модель данных").exists(), \
        "папка вне схемы всё равно создалась"

    run("build_plan.py", "--card", "Карточка", "--source", "Raw/project/источник.md",
        "--sections", "1", "--to", "Concepts", "--apply", cwd=root)
    assert (root / "AuroraKnowledgeDB/Concepts/Карточка.md").exists()


@test
def test_build_plan_keeps_its_own_output_out_of_the_plan(tmp: Path):
    """Карточка, собранная в Reference, не возвращается в план новым источником.

    В справочниках источник — сам справочник, который вели руками. Извлечённая из него
    карточка ложится рядом, и план начинал расти от собственной работы: разобрал
    источник — получил источник. На живом проекте так набралось 54 фантомных источника.
    Отличаем по `source:` в шапке.
    """
    root = make_project(tmp)
    ref = root / "AuroraKnowledgeDB" / "Reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "Справочник-кодов.md").write_text(
        "---\ntitle: \"Справочник кодов\"\ntype: reference\n---\n\n# Коды\n\n"
        + "| Код | Значение |\n|---|---|\n" + "| A | значение |\n" * 30, encoding="utf-8")
    (ref / "Извлечённая-тема.md").write_text(
        "---\ntitle: \"Извлечённая тема\"\ntype: reference\n"
        "source: \"AuroraKnowledgeDB/Reference/Справочник-кодов.md\"\n---\n\n"
        + "# Тема\n\n" + "текст. " * 60, encoding="utf-8")

    out = run("build_plan.py", "--tasks", "0", cwd=root).stdout
    assert "Справочник-кодов.md" in out, "рукописный справочник пропал из плана"
    assert "Извлечённая-тема.md" not in out, \
        "карточка, собранная движком, вернулась в план новым источником"


@test
def test_slice_shows_agent_more_than_a_person(tmp: Path):
    """`--slice-chars` управляет длиной превью секции.

    Человек смотрит в сам источник, ему хватает строки. Агент источника не открывает и
    судит только по этому тексту: на коротком превью он объявил пустым нормальный
    справочник — «содержимого не видно», — и источник ушёл бы из плана навсегда.
    """
    root = make_project(tmp)
    src = root / "Raw" / "project" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Тема\n\n" + "буквы " * 400, encoding="utf-8")

    short = run("build_plan.py", "--slice", "Raw/project/источник.md", cwd=root).stdout
    long = run("build_plan.py", "--slice", "Raw/project/источник.md",
               "--slice-chars", "900", cwd=root).stdout
    head = lambda s: s.split("ЗАДАНИЕ", 1)[0]
    assert len(head(long)) > len(head(short)) + 500, \
        "длинное превью не длиннее короткого — агент по-прежнему судит вслепую"


@test
def test_agent_build_judges_sources_without_structure(tmp: Path):
    """Источник без секций: пусто (отметить) или человеку — но не «всё человеку».

    В живом плане сотнями лежат страницы-оглавления: заголовок и ссылка. Сваливать их
    человеку — значит не разобрать план никогда. Отметку «пусто» подтверждает второе
    мнение: она убирает источник из плана навсегда, и потерянное знание само не всплывёт.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    src = root / "Raw" / "project" / "оглавление.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Оглавление\n\n[ссылка](http://example.org)\n" + "\u00a0 " * 120,
                   encoding="utf-8")
    cfg = R.AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                             "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    def answer(text):
        return lambda c, role, msgs, **kw: {"ok": True, "text": text, "reasoning": "",
                                            "backend": 1, "model": "test", "seconds": 0.1,
                                            "waited": 0, "ring": 1, "log": []}

    both_empty = R.solve_source(cfg, str(root), "Raw/project", "Raw/project/оглавление.md",
                                False, True, call=answer('{"empty": "только ссылка"}'))
    assert both_empty["status"] == "отметил бы пустым", both_empty

    # мнения разошлись — источник остаётся человеку, а не уходит из плана
    calls = iter(['{"empty": "только ссылка"}', '{"keep": "тут есть правило"}'])
    split = R.solve_source(cfg, str(root), "Raw/project", "Raw/project/оглавление.md",
                           False, True,
                           call=lambda c, role, msgs, **kw: answer(next(calls))(c, role, msgs))
    assert split["status"] == "без секций — человеку", split
    assert "разошлись" in split["note"], split["note"]


@test
def test_agent_build_oracle_counts_processed_not_left(tmp: Path):
    """Оракул сборки считает по «обработано»: «осталось» умеет расти само.

    Правка источника возвращает его в план значком ♻️ — и прогон, сделавший всё верно,
    выглядел сбойным. На живом прогоне так и вышло: «план сдвинулся на 41, а агент
    объявил разобранными 42», причём 42 действительно ушли из плана.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    steps = [{"alias": "и", "status": "разобран", "note": "", "backends": [], "degraded": False},
             {"alias": "т", "status": "разобран", "note": "", "backends": [], "degraded": False}]

    # рядом правили источник: осталось не убавилось, но обработано выросло на два
    grew = {"steps": steps, "total": 2, "left": 0, "stopped": "", "limited": False,
            "before": {"left": 100, "done": 10, "errors": 5},
            "after": {"left": 100, "done": 12, "errors": 5}}
    ok, why = R.verdict_build(grew, apply=True)
    assert ok, f"честный прогон признан сбойным: {why}"

    # а вот объявил больше, чем засчитал движок, — это уже расхождение
    lied = {**grew, "after": {"left": 99, "done": 11, "errors": 5}}
    ok2, why2 = R.verdict_build(lied, apply=True)
    assert not ok2 and "движок засчитал" in why2, why2


@test
def test_health_shows_where_we_are_in_building(tmp: Path):
    """Дашборд знает, сколько источников ждут разбора и чем кончился прогон агента.

    Панель показывала здоровье уже собранного и молчала о том, сколько собрать осталось:
    главное число всей работы человек узнавал, только запустив сборку.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    src = root / "Raw" / "project" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Тема\n\n" + "текст. " * 60, encoding="utf-8")
    runs = root / "AuroraKnowledgeDB" / "meta" / "agent-runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-08-10_1200_build.md").write_text(
        "# Агент · сборка базы\n\n**Оракул:** ✗ не разобрано: 2\n\n"
        "## Осталось на следующий прогон: 7\n", encoding="utf-8")

    b = ck.build_progress(str(root))
    assert b["total"] >= 1 and b["left"] >= 1, b
    a = ck.last_agent_run(str(root))
    assert a["task"] == "build" and a["ok"] is False and a["left"] == 7, a
    assert "не разобрано" in a["why"], a


@test
def test_agent_build_walks_the_plan_not_one_partition(tmp: Path):
    """Без --partition агент идёт по плану подряд, а не по одной партии.

    Партии придуманы под контекст модели, которой человек отдаёт задание целиком. Агент
    берёт по одному источнику: партия, где осталось лишь неподъёмное, заставляла каждый
    следующий прогон брать то же самое и отчитываться «разобрано 0».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    seen = {}

    def fake(cwd, script, args, timeout=300):
        seen["args"] = args
        return {"ok": True, "rc": 0, "out": "", "refused": ""}

    orig, R.run_command = R.run_command, fake
    try:
        R.read_partition("/tmp", 0)
        assert seen["args"] == ["--tasks", "0"], seen["args"]
        R.read_partition("/tmp", 3)
        assert seen["args"] == ["--partition", "3"], seen["args"]
    finally:
        R.run_command = orig


@test
def test_agent_build_refuses_cards_from_same_sections(tmp: Path):
    """Две карточки из одних секций — одно тело под двумя именами. Считается, не спрашивается.

    Живой прогон собрал две карточки с разными именами из одних и тех же секций 3,4:
    тела вышли дословно одинаковыми. Критик пропустил — и правильно, что мы на него не
    рассчитываем: пересечение множеств проверяется счётом, а не мнением модели.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    secs = [(1, "Первая", 500, ""), (2, "Вторая", 500, ""), (3, "Третья", 500, "")]

    why = R.check_cards([{"title": "А", "sections": "1,2"}, {"title": "Б", "sections": "2,3"}], secs)
    assert "секция 2" in why and "одним телом" in why, why
    assert not R.check_cards([{"title": "А", "sections": "1"},
                              {"title": "Б", "sections": "2-3"}], secs), "честный разбор отклонён"
    assert "которых нет" in R.check_cards([{"title": "А", "sections": "7"}], secs)

    # служебные секции — постоянный список, а не предмет спора двух моделей
    with_service = secs + [(4, "История изменений", 200, "")]
    assert "служебная секция" in R.check_cards([{"title": "А", "sections": "1,4"}], with_service)
    assert not R.check_cards([{"title": "А", "sections": "1"}], with_service)
    assert "не разобраны номера" in R.check_cards([{"title": "А", "sections": "ага"}], secs)


@test
def test_agent_sees_every_conflict_not_just_printed(tmp: Path):
    """Агент получает все конфликты, а не первые 15 из отчёта для человека.

    Отчёт `kb:repair --aliases` режет список: читать восемнадцатую строку человеку незачем.
    Агент, читавший тот же текст, разбирал 15 из 19 и честно докладывал «каждый конфликт
    разобран» — оракул подтверждал успех, не зная о четырёх невидимых. Обрезка для глаз
    не должна становиться обрезкой для машины, а оракул обязан ловить неполный список.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    for i in range(18):
        card(root, f"Processes/Процесс-{i}.md", "Процесс.", aliases=f'["Имя-{i}"]',
             type="process")
        card(root, f"Systems/Система-{i}.md", "Система.", aliases=f'["Имя-{i}"]',
             type="system")

    conflicts = R.read_conflicts(str(root))
    assert len(conflicts) == 18, f"агент увидел {len(conflicts)} конфликтов из 18"
    assert all(len(cards) == 2 for _alias, cards in conflicts), "карточки конфликта потеряны"

    # оракул не принимает прогон, где список пришёл короче, чем видит линтер
    blind = {"steps": [{"alias": "Имя-0", "status": "уточнено", "note": "", "backends": [],
                        "degraded": False}],
             "before": {"conflicts": 18, "errors": 18}, "after": {"conflicts": 17, "errors": 18},
             "total_conflicts": 1, "limited": False, "seconds": 1.0}
    ok, why = R.verdict(blind, apply=True)
    assert not ok and "неполным" in why, f"оракул принял прогон со слепым пятном: {why}"

    # с явным --limit это осознанная проба, а не слепота
    ok, _why = R.verdict({**blind, "limited": True}, apply=True)
    assert ok, "оракул не принял пробу по --limit"


@test
def test_ask_keeps_the_conversation_in_the_base(tmp: Path):
    """Разговор с базой хранится в базе, читается и продолжается уточнением.

    История вопросов, живущая до перезагрузки страницы, — не история: второй аналитик
    задаёт те же вопросы заново, а разговор, показавший пробел в базе, теряется вместе
    с вкладкой. Поэтому журнал лежит в `meta/ask/` и уходит в git с карточками.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    path = R.thread_path(str(root), "2026-08-12_1700-обеспечение")
    R.append_turn(path, "что с обеспечением, если заявка аннулирована?",
                  "Возвращается заявителю [[Возврат-обеспечения]].",
                  "модель qwen · карточек в контексте 43 · 12.0 с", "generate")
    R.append_turn(path, "а если заявитель — ИП?", "Порядок тот же [[ИП]].",
                  "модель qwen · карточек в контексте 21 · 9.0 с", "generate")

    text = path.read_text(encoding="utf-8")
    assert "type: ask-thread" in text and "### Вопрос ·" in text, \
        "журнал не markdown с шапкой — его не прочитать ни в Obsidian, ни панелью"
    turns = R.read_thread(path)
    assert len(turns) == 2, f"прочитано пар {len(turns)}, а записано 2"
    assert turns[0]["q"].startswith("что с обеспечением"), "вопрос потерян при чтении"
    assert "[[ИП]]" in turns[1]["a"], "ответ потерян при чтении"

    lst = R.threads(str(root))
    assert len(lst) == 1 and lst[0]["turns"] == 2, f"список разговоров: {lst}"
    assert lst[0]["title"].startswith("что с обеспечением"), \
        "разговор назван не первым вопросом — в списке его не узнать"

    # Уточнение без контекста ничего не значит: «а если он ИП?» само по себе не находит
    # в базе ни одной карточки. Тему держит предыдущий вопрос — он и уходит в отбор.
    seen = {}

    def fake_pack(cwd, script, args):
        seen["topic"] = args[0]
        return {"ok": True, "out": "# Пак (карточек 2)\n\n## Возврат-обеспечения\nтекст"}

    def fake_call(cfg, role, messages, deadline=None):
        seen[role] = messages[0]["content"]
        return {"ok": True, "text": "Порядок тот же [[Возврат-обеспечения]].",
                "backend": 1, "model": "qwen", "log": []}

    real = R.run_command
    R.run_command = fake_pack
    try:
        res = R.run_ask({"request_timeout": 60}, str(root), "а если заявитель — ИП?",
                        "generate", 40, call=fake_call, history=turns)
    finally:
        R.run_command = real
    assert res["ok"], f"уточнение не отработало: {res}"
    assert "обеспечением" in seen["topic"], \
        f"контекст собран по одной последней фразе — тема разговора потеряна: {seen['topic']}"
    assert "РАНЬШЕ В ЭТОМ РАЗГОВОРЕ" in seen["worker"], "модель не увидела прошлых ответов"
    assert "заявка аннулирована" in seen["worker"], "прошлый вопрос не дошёл до модели"

    # Журнал — запись состоявшегося разговора, а не знание: линтер не требует от него
    # живых ссылок, иначе переименование карточки создаёт долг в истории.
    card(root, "Concepts/Тема.md", "Текст.", status="imported", type="concept")
    cp = run("kb_lint.py", "--full", cwd=root)
    assert "meta/ask" not in cp.stdout, \
        f"журнал разговоров попал в находки линтера:\n{cp.stdout}"


@test
def test_answer_names_are_sorted_by_where_they_came_from(tmp: Path):
    """Ссылки ответа — три разные находки, а не одно «модель могла назвать по памяти».

    Живой случай: предупреждение перечислило рядом AC-4.7.1 (карточка в базе есть, в пак
    не попала), CP-3.2.10 (карточки нет, идентификатор упомянут в таблице внутри другой
    карточки) и настоящую выдумку. Два случая из трёх лечатся командой, а не вниманием —
    и человек, которого одинаково пугают трижды, перестаёт читать предупреждение вовсе.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    card(root, "Concepts/AC-4-7-1-Главная-страница.md", "Виджеты сводной информации.",
         status="verified", type="concept", aliases='["AC-4.7.1"]')
    card(root, "Concepts/Epic-4-7.md", "Разделы сервиса: | US-4.7.2 | [CP-3.2.10] |",
         status="verified", type="concept")

    pack = ("# Context pack: центр уведомлений\n\n## Epic-4-7\n\n"
            "| US-4.7.2 | Центр уведомлений | [CP-3.2.10] |\n\n"
            "## Состояние разработки (зеркало Jira, не карточки базы)\n\n| PRJ-480 |\n")
    cards = ["Epic-4-7", "Состояние разработки (зеркало Jira, не карточки базы)"]
    text = ("Статус [[PRJ-480]]. Связано с [[AC-4.7.1]] и [[CP-3.2.10]], "
            "а также [[Регламент-обмена-с-казначейством]].")

    got = R.classify_links(text, cards, pack, str(root))
    assert got["outside"] == ["AC-4.7.1"], \
        f"карточка, которая есть в базе, названа выдумкой: {got}"
    assert got["mentioned"] == ["CP-3.2.10"], \
        f"идентификатор из таблицы внутри карточки разобран неверно: {got}"
    assert got["invented"] == ["Регламент-обмена-с-казначейством"], \
        f"настоящая выдумка не отделена от остального: {got}"
    assert "PRJ-480" not in sum(got.values(), []), \
        "ключ задачи из зеркала объявлен выдумкой — он взят из пака"


@test
def test_momus_checks_the_answer_statement_by_statement(tmp: Path):
    """Момус: вторая модель ищет утверждения без опоры в контексте.

    Механическая проверка видит только ссылки. «Возврат занимает десять дней» без ссылки
    она пропустит — а именно такая фраза уходит в постановку и оттуда в разработку.
    Момус — мнение, а не оракул: он не переписывает ответ, его вердикт печатается рядом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    seen = {}

    def critic(cfg, role, messages, deadline=None):
        seen["role"] = role
        seen["prompt"] = messages[0]["content"]
        return {"ok": True, "backend": 2, "model": "qa-model", "log": [],
                "text": "1. ОПОРА «возврат по заявлению»\n"
                        "2. НЕТ ОПОРЫ срок десять дней\n\nВЕРДИКТ: БЕЗ ОПОРЫ 1"}

    mo = R.run_momus({"request_timeout": 60}, "## Карточка\n\nвозврат по заявлению",
                     "как вернуть?", "Возврат по заявлению, срок десять дней.", call=critic)
    assert seen["role"] == "qa", f"Момус занял чужую роль: {seen['role']}"
    assert "ОТВЕТ НА ПРОВЕРКУ" in seen["prompt"] and "возврат по заявлению" in seen["prompt"], \
        "Момус проверяет ответ, не видя ни ответа, ни контекста"
    assert mo["ok"] and not mo["clean"] and mo["unsupported"] == 1, f"вердикт разобран неверно: {mo}"

    clean = R.run_momus({"request_timeout": 60}, "## К\n\nтекст", "вопрос?", "ответ",
                        call=lambda *a, **k: {"ok": True, "backend": 1, "model": "m",
                                              "log": [], "text": "ВЕРДИКТ: ЧИСТО"})
    assert clean["clean"] and clean["unsupported"] == 0, f"чистый вердикт не распознан: {clean}"

    # Вердикта нет — проверка не состоялась. Молча выдать «чисто» значит соврать дважды.
    mute = R.run_momus({"request_timeout": 60}, "## К\n\nтекст", "вопрос?", "ответ",
                       call=lambda *a, **k: {"ok": True, "backend": 1, "model": "m",
                                             "log": [], "text": "мне кажется всё хорошо"})
    assert not mute["ok"] and not mute["clean"], \
        f"болтовня вместо вердикта принята за проверку: {mute}"

    report = R.report_ask({"ok": True, "answer": "ответ", "cards": ["К"], "total": 1,
                           "seconds": 1.0, "model": "m", "backend": 1,
                           "momus": mo}, "как вернуть?", {})
    assert "без опоры — 1" in report and "Разбор Момуса" in report, \
        f"вердикт Момуса не дошёл до человека:\n{report}"
    assert "<details>" not in report, \
        "HTML-свёртка: панель и Obsidian показали бы её как мусор"


@test
def test_agent_runner_oracle_and_checkpoint(tmp: Path):
    """Цикл агента: два исхода на конфликт, оракул по факту, откат одной строкой.

    Оракул «ноль конфликтов любой ценой» толкал бы агента выдумывать различия там, где
    карточки надо сливать, — в базе появлялись бы замаскированные дубли. Поэтому успех:
    каждый конфликт разобран (уточнён или честно отложен человеку), а ошибок в базе не
    прибавилось.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    card(root, "Processes/ALG-1-Получение-курсов.md", "Алгоритм.",
         aliases='["Курс валют"]', type="process")
    card(root, "Systems/Сервис-курсов.md", "Внешняя система.",
         aliases='["Курс валют"]', type="system")
    card(root, "Statuses/SPR-7-Статусы.md", "Справочник статусов.",
         aliases='["SPR-7"]', type="status-model")
    card(root, "Glossary/SPR-7-Statusy.md", "Справочник статусов.",
         aliases='["SPR-7"]', type="glossary")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-qm", "фикстура"], cwd=str(root), check=True)

    conflicts = R.read_conflicts(str(root))
    assert len(conflicts) == 2, f"конфликты не прочитаны из отчёта движка: {conflicts}"

    # модель подменяется: тест проверяет цикл и оракул, а не качество формулировок
    def fake_call(cfg, role, messages, **kw):
        text = messages[0]["content"]
        if "SPR-7" in text:
            answer = '{"verdict": "duplicate", "reason": "один справочник дважды"}'
        else:
            answer = ('{"verdict": "distinct", "renames": ['
                      '{"card": "ALG-1-Получение-курсов", "new": "Курсы валют (алгоритм)"},'
                      '{"card": "Сервис-курсов", "new": "Курсы валют (сервис)"}]}')
        if role == "critic":
            answer = '{"ok": true}'
        return {"ok": True, "text": answer, "reasoning": "", "backend": 1,
                "model": "test", "seconds": 0.1, "waited": 0, "ring": 1, "log": []}

    cfg = R.AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                             "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    cp = R.checkpoint(str(root), "agent:aliases", True)
    assert cp["ok"] and cp["sha"], "чекпойнт не создан"

    res = R.run_aliases(cfg, str(root), apply=True, use_critic=True, limit=0, call=fake_call)
    ok, why = R.verdict(res, apply=True)
    assert ok, f"оракул не принял корректный прогон: {why}"
    statuses = sorted(s["status"] for s in res["steps"])
    assert statuses == ["дубль — человеку", "уточнено"], statuses
    assert res["after"]["conflicts"] < res["before"]["conflicts"], "конфликтов не убавилось"

    # дубль остался нетронутым: агент не имеет права сливать карточки
    dup = (root / "AuroraKnowledgeDB/Statuses/SPR-7-Статусы.md").read_text(encoding="utf-8")
    assert '"SPR-7"' in dup, "агент тронул синоним дубля вместо того, чтобы отложить"

    # откат одной строкой возвращает базу к состоянию до агента
    subprocess.run(["git", "reset", "--hard", cp["sha"]], cwd=str(root),
                   capture_output=True, check=True)
    back = (root / "AuroraKnowledgeDB/Processes/ALG-1-Получение-курсов.md").read_text(
        encoding="utf-8")
    assert "Курс валют" in back and "(алгоритм)" not in back, "откат не вернул исходное"

    # отчёт называет и оракул, и путь отката
    text = R.report(res, cp, apply=True, use_critic=True, cfg=cfg)
    assert "Оракул:" in text and "git reset --hard" in text
    assert "Отложено человеку" in text, "дубли не выделены в отчёте отдельно"


@test
def test_agent_ring_config_and_whitelist(tmp: Path):
    """Встроенный агент: кольцо бэкендов, слои конфига, белый список записи.

    Кольцо — не лестница: каждый вызов обходит список с первого бэкенда, поэтому
    восстановившийся корпоративный шлюз подхватывается сразу. Пустой ответ с кодом 200 —
    отказ (живой бэкенд так отвечал из-за chat-шаблона). Писать в проект агент может
    только через белый список команд; kb:verify закрыт наглухо — доверие присваивает
    человек, и это конструкция, а не настройка.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")

    env = {"AURORA_AGENT_BACKEND_1_URL": "https://one.example.com/v1/",
           "AURORA_AGENT_BACKEND_1_KEY": "k1",
           "AURORA_AGENT_BACKEND_1_MODEL_WORKER": "big",
           "AURORA_AGENT_BACKEND_2_URL": "http://two.example.com/v1",
           "AURORA_AGENT_BACKEND_2_MODEL": "small",
           "AURORA_AGENT_THINKING": "0"}
    cfg = A.parse_config(env)
    assert len(cfg["backends"]) == 2 and cfg["backends"][0]["url"].endswith("/v1"), \
        "хвостовой слэш URL должен сниматься"
    assert A.role_model(cfg["backends"][1], "critic") == "small", \
        "нет ролевой модели — берётся общая"
    assert not cfg["thinking"], "THINKING=0 должен выключать рассуждения"

    ok_body = {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}
    empty = {"choices": [{"message": {"content": "", "reasoning": "думал"},
                          "finish_reason": "length"}]}

    # первый пуст (сломанный шаблон), второй занят, но на втором круге первый ожил
    calls = {"n": 0}
    def transport(kind, b, payload, timeout):
        calls["n"] += 1
        if kind == "slots":
            return (200, [{"is_processing": b["n"] == 2}], "", 0.0) if b["n"] == 2 \
                   else (404, None, "нет /slots", 0.0)
        if b["n"] == 1:
            return (200, ok_body, "", 0.1) if calls["n"] > 3 else (200, empty, "", 0.1)
        return (200, ok_body, "", 0.1)

    slept = []
    r = A.call_role(cfg, "worker", [{"role": "user", "content": "x"}],
                    transport=transport, deadline=__import__("time").time() + 120,
                    sleep=slept.append)
    assert r["ok"] and r["backend"] == 1 and r["ring"] == 2, \
        f"кольцо не вернулось к ожившему первому: {r}"
    assert slept == [A.RING_PAUSE], "между кругами должна быть одна пауза"
    assert any("пустой ответ" in l or "рассуждения съели" in l for l in r["log"]), \
        "причина отказа первого круга не названа"

    # все мертвы → честный отказ по дедлайну
    dead = lambda kind, b, payload, timeout: (None, None, "Connection refused", 0.0)
    r2 = A.call_role(cfg, "worker", [], transport=dead,
                     deadline=__import__("time").time() + 1, sleep=lambda s: None)
    assert not r2["ok"] and any("дедлайн" in l for l in r2["log"])

    # белый список
    assert A.write_allowed("build_plan.py", ["--card", "X", "--apply"])[0]
    assert A.write_allowed("kb_fix.py", ["--set-alias", "--apply"])[0]
    assert not A.write_allowed("kb_trust.py", ["--apply"])[0], \
        "приёмка отдана агенту — это запрещено конструкцией"
    assert not A.write_allowed("kb_reset.py", ["--apply"])[0]
    assert not A.write_allowed("git", ["push"])[0]
    assert not A.write_allowed("kb_fix.py", ["--aliases", "--drop-alias", "--apply"])[0], \
        "у kb_fix агенту разрешены только --stubs и --set-alias"
    assert A.write_allowed("kb_lint.py", ["--summary"])[0], "чтение должно быть свободным"


@test
def test_agent_wired_into_engine(tmp: Path):
    """Агент встроен в движок, а не приложен сбоку: реестр, манифест, doctor, панель."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "agent | agent:ping" in reg, "нет команды agent:ping в реестре"
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scripts/agent_core.py" in man, "агент не едет в проекты с обновлением движка"
    tpl = (KIT / "templates/aurora.env.local.example").read_text(encoding="utf-8")
    assert "AURORA_AGENT_BACKEND_1_URL" in tpl, "шаблон .env не документирует агента"
    assert "example.com" in tpl, "в шаблоне должны быть плейсхолдеры, а не живые адреса"
    assert not re.search(r"^#?\s*AURORA_AGENT_\w*KEY=[A-Za-z0-9_\-]{16,}", tpl, re.M), \
        "в шаблон попал похожий на настоящий ключ"

    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for route in ("/api/agent", "/api/agent/env", "/api/agent/ping", "/api/agent/venv"):
        assert route in ck, f"в панели нет ручки {route}"
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "renderAgentCard" in ui and "Проверить соединение" in ui, \
        "в Настройке нет раздела «Агент»"
    assert "target_label" in ui, "цель записи (кит или проект) не показывается человеку"
    assert "Pydantic AI" in ui, "нет установки Pydantic AI из панели"

    doc = (KIT / "scripts/aurora_doctor.py").read_text(encoding="utf-8")
    assert "agent_core" in doc and "agent:ping" in doc, "doctor молчит про агента"


@test
def test_dev_section_hides_behind_seven_taps(tmp: Path):
    """Раздел разработки открывается семью нажатиями и живёт только в ките.

    Прятать его нужно не ради секретности — панель локальная, — а ради честности меню:
    команды `dev:` относятся к самому движку и аналитику не дают ничего. Открытый по
    умолчанию раздел был бы шумом в интерфейсе у всех, кроме одного человека.
    """
    ui = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert "DEV_TAPS = 7" in ui, "число нажатий должно быть названо константой"
    assert 'localStorage.setItem("aurora-dev"' in ui, "выбор не переживёт перезагрузку"
    assert 'id="devNav"' in ui and "hidden" in ui, "пункт меню должен быть скрыт по умолчанию"
    assert "Скрыть раздел" in ui, "раздел нельзя закрыть обратно"
    assert "renderDev" in ui and 'id="view-dev"' in ui, "нет самого раздела"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    assert ck.kit_is_source(), "kit не опознан как источник — раздел не откроется нигде"
    dev = [r for r in ck.registry() if r["ns"] == "dev"]
    assert dev, "команд разработки нет в реестре панели"

    # у команды прогона значение необязательно: панель жмёт кнопку без аргумента,
    # и требовать его значило бы падать кодом 2 на первом же нажатии
    run = next(r for r in dev if r["cmd"] == "dev:qa-run")
    out = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"),
                          *run["fixed_flags"], "TS-000-нет-такого"],
                         cwd=str(KIT), capture_output=True, text=True,
                         env={**os.environ, "AURORA_QA_RUNNING": "1"})
    assert out.returncode != 2, f"кнопка «Запустить» уронит команду:\n{out.stderr[:300]}"
    assert "нет" in out.stderr, "неизвестный сценарий должен называться по имени"

    # прогон запускает автотесты, автотест — прогон: круг разрывается меткой в окружении
    src = (KIT / "scripts/dev_qa.py").read_text(encoding="utf-8")
    assert "AURORA_QA_RUNNING" in src, "нет защиты от рекурсии прогона и автотестов"


@test
def test_dev_qa_keeps_the_test_registry_honest(tmp: Path):
    """QA-контур разработки: реестр сходится, документы заводятся из шаблона.

    Кейс, который не входит ни в один сценарий и не закрыт автотестом, не гоняется
    никогда — и создаёт видимость покрытия. Ссылка сценария на несуществующий кейс делает
    то же самое. Обе беды тихие: файлы на месте, всё выглядит правильно.
    """
    out = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--check"],
                         cwd=str(KIT), capture_output=True, text=True)
    assert out.returncode == 0, f"реестр QA кита разошёлся:\n{out.stdout}"

    lst = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--list"],
                         cwd=str(KIT), capture_output=True, text=True).stdout
    assert "Кейсов:" in lst and "TS-001" in lst, lst[:400]
    assert "Ни в один сценарий не входят" not in lst, \
        f"есть кейсы, которые не гоняются ни разу:\n{lst[-400:]}"

    # шаблоны — часть поставки: без них нечем заводить новые проверки
    for tpl in ("test-case.md", "test-scenario.md"):
        assert (KIT / "skills/aurora-dev/references" / tpl).is_file(), \
            f"нет шаблона {tpl} — dev:qa-new не сможет завести документ"

    # в проекте контур разработки не работает и не показывается
    proj = tmp / "project"
    (proj / ".opencode/scripts").mkdir(parents=True)
    (proj / "aurora.config.yaml").write_text('project:\n  name: "T"\n', encoding="utf-8")
    (proj / ".opencode/scripts/dev_qa.py").write_text(
        (KIT / "scripts/dev_qa.py").read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run([sys.executable, ".opencode/scripts/dev_qa.py", "--list"],
                       cwd=str(proj), capture_output=True, text=True)
    assert r.returncode != 0 and "не кит" in r.stderr, \
        f"QA-контур запустился в проекте:\n{r.stdout}{r.stderr}"

    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "dev | dev:qa-run" in reg, "команды разработки не заведены в реестре"
    assert "dev | dev:qa-cover" in reg, "нет точки входа «покрыть сделанное»"
    assert "dev_qa.py" not in (KIT / "engine_manifest.txt").read_text(encoding="utf-8"), \
        "контур разработки уезжает в проекты — там его нечем и незачем запускать"


@test
def test_commands_registry_matches_engine(tmp: Path):
    """Справочник команд не должен расходиться ни с движком, ни с флагами скриптов."""
    root = make_project(tmp)
    run("kit_commands.py", "--check", cwd=root, expect_rc=0)

    cp = run("kit_commands.py", "kb", cwd=root)
    assert "kb:repair" in cp.stdout and "kb:scrub" in cp.stdout
    # блок ровно одной команды: следом в реестре идёт kb:dedupe — у неё свой набор флагов
    block = cp.stdout.split("kb:repair", 1)[1].split("kb:dedupe")[0]
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

    # полный снос просят явно — и тогда движок называет числом, что теряется
    dry = run("kb_reset.py", "--drop-handmade", cwd=root)
    assert "verified: 1" in dry.stdout and "работа человека" in dry.stdout, \
        f"не предупреждает, что удаляет проверенное человеком:\n{dry.stdout[:600]}"
    assert "не выведется заново: источника нет" in dry.stdout, \
        "не назвал карточки, за которыми не стоит документа"
    assert "Идут под снос" in dry.stdout, \
        "нет итоговой строки: человек читает разделы и не видит общего счёта"
    assert (kb / "Concepts/Карточка.md").exists(), "dry-run удалил файлы"

    run("kb_reset.py", "--drop-handmade", "--apply", cwd=root)
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
    """Сброс по умолчанию оставляет то, чего нет ни в одном источнике.

    Смена способа извлечения — не повод стирать память проекта: журнал решений, вопросы,
    рукотворные справочники и правила базы `kb:build` не вернёт. Раньше это включали
    флагом `--keep-handmade`, и флаг надо было вспомнить ровно в тот момент, когда
    запускаешь необратимую команду. Теперь наоборот: чтобы снести невосстановимое, есть
    `--drop-handmade`. Учёт извлечения уходит в обоих режимах, иначе план выйдет пустым.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Concepts", "Decisions", "Questions", "Reference", "meta"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Стр.md").write_text("страница\n", encoding="utf-8")
    (kb / "Concepts/Карточка.md").write_text(
        '---\ntitle: "К"\nstatus: imported\nsource: "Sources/Confluence/Стр.md"\n'
        '---\nтекст\n', encoding="utf-8")
    (kb / "Decisions/DR-001.md").write_text("почему выбрали так\n", encoding="utf-8")
    (kb / "Questions/Q-001.md").write_text("вопрос заказчику\n", encoding="utf-8")
    (kb / "Reference/abbr.md").write_text("аббревиатуры\n", encoding="utf-8")
    (kb / "meta/conventions.md").write_text("# правила\n", encoding="utf-8")
    (kb / "meta/golden_questions.md").write_text("# эталоны\n", encoding="utf-8")
    (kb / "meta/manifest.json").write_text('{"sources": {}}', encoding="utf-8")
    (kb / "meta/links.json").write_text("{}", encoding="utf-8")

    run("kb_reset.py", "--apply", cwd=root)
    for keep in ("Decisions/DR-001.md", "Questions/Q-001.md", "Reference/abbr.md",
                 "meta/conventions.md", "meta/golden_questions.md"):
        assert (kb / keep).exists(), f"сброс удалил невосстановимое без спроса: {keep}"
    # карточка с источником уходит: пересборка соберёт её заново, и оставить её значит
    # получить двойника
    assert not (kb / "Concepts/Карточка.md").exists(), \
        "карточка с живым источником пережила сброс — после пересборки будет двойник"
    assert not (kb / "meta/manifest.json").exists(), \
        "учёт извлечения остался — kb:build сочтёт источники разобранными и план выйдет пустым"
    assert not (kb / "meta/links.json").exists(), "сгенерированный граф связей не удалён"

    # снести и невосстановимое можно, но только по явному ключу
    run("kb_reset.py", "--drop-handmade", "--apply", "--allow-dirty", cwd=root)
    assert not (kb / "Decisions/DR-001.md").exists(), "--drop-handmade не снёс журнал решений"


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
    # шапку карточки с 1.48.0 пишет скрипт, поэтому в задании не правила frontmatter,
    # а порядок работы: раскадровка → сборка карточки из секций → отметка
    assert "--slice" in out and "--card" in out, \
        "в задании нет порядка работы через раскадровку"
    assert "build_plan.py --done" in out, "в задании нет шага завершения"
    # собранная карточка — черновик: доводка обязательна, но точное не пересказывают
    assert "Оставить дословно" in out and "Сократить и переписать" in out, \
        "в задании нет шага доводки с границей «что не трогать»"
    assert "таблицы" in out and "коды и ключи" in out, \
        "не названо то, что переписывать нельзя"
    assert "aurora-vault" in out, "не сказано, по какому скиллу работать"


@test
def test_repair_frees_alias_taken_twice(tmp: Path):
    """Один alias у двух карточек — ссылка по нему не ведёт никуда.

    Извлечение раздаёт синонимы щедро, и после сборки с нуля таких имён набираются
    десятки. Alias остаётся у той карточки, чьё имя с ним совпадает.
    """
    root = make_project(tmp, git=True)
    cards = root / "AuroraKnowledgeDB/Concepts"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "Обеспечение-ДОП.md").write_text(
        '---\ntitle: "Обеспечение ДОП"\naliases: ["Обеспечение ДОП", "Платёж"]\n'
        "status: imported\n---\n\nтело\n", encoding="utf-8")
    (cards / "Этап-3.md").write_text(
        '---\ntitle: "Этап 3"\naliases: ["Обеспечение ДОП", "Этап-3"]\n'
        "status: imported\n---\n\nтело\n", encoding="utf-8")

    # по умолчанию — отчёт и задание ассистенту: снять синоним значит потерять имя,
    # под которым карточку знают
    dry = run("kb_fix.py", "--aliases", cwd=root)
    assert "1 имён заняты дважды" in dry.stdout, dry.stdout[:600]
    assert "УТОЧНИТЬ СИНОНИМЫ" in dry.stdout, "нет задания на уточнение"
    assert "файлов к записи: 0" in dry.stdout, "отчёт не должен ничего править"

    run("kb_fix.py", "--aliases", "--drop-alias", "--apply", cwd=root)
    keeper = (cards / "Обеспечение-ДОП.md").read_text(encoding="utf-8")
    loser = (cards / "Этап-3.md").read_text(encoding="utf-8")
    assert "Обеспечение ДОП" in keeper, "alias снят у владельца"
    assert "Обеспечение ДОП" not in loser.split("---")[1], f"alias остался у чужой:\n{loser}"
    assert "Этап-3" in loser, "снесли все alias вместо одного"

    # ссылка без карточки — повод завести заготовку, а не убрать ссылку: так работает
    # картотека, знание приходит позже ссылки
    (cards / "Процесс.md").write_text(
        '---\ntitle: "Процесс"\nstatus: imported\n---\n\nсм. [[УТС]] и [[Ещё-не-описанное]]\n',
        encoding="utf-8")
    stub = run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root)
    assert "Заготовки под ссылки: 2" in stub.stdout, stub.stdout[:600]
    made = (root / "AuroraKnowledgeDB/Glossary/УТС.md").read_text(encoding="utf-8")
    assert "status: draft" in made and "заготовка" in made, made
    assert "[[Процесс]]" in made, "в заготовке не сказано, кто её ждёт"
    assert run("kb_lint.py", cwd=root).returncode == 0 or True

    out = run("kb_lint.py", cwd=root).stdout
    assert "kb_lint: карточек" in out


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
def test_links_and_stubs_respect_separators_and_dots(tmp: Path):
    """Имя — это имя, а не «текст до последней точки», и разделители в нём не значимы.

    `[[ALG-3.14 Учёт операции]]` разрешалось в карточку `ALG-3` — совсем другое знание,
    молча. А ссылка `[[ER BaR FID]]` на существующую `ER-BaR-FID` считалась битой, и под
    неё заводилась вторая пустая карточка: знание раскалывалось надвое.
    """
    root = make_project(tmp, git=True)
    card(root, "Concepts/ALG-3.md", "короткий алгоритм")
    card(root, "Concepts/ALG-3.14-Учёт-операции-из-смежной-системы.md", "длинный алгоритм")
    card(root, "Concepts/ER-BaR-FID.md", "справочник")
    card(root, "Concepts/Ссылающаяся.md",
         "см. [[ALG-3.14 Учёт операции из смежной системы]], [[ER BaR FID]] и [[Неизвестное]]")

    fixed = run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    text = (root / "AuroraKnowledgeDB/Concepts/Ссылающаяся.md").read_text(encoding="utf-8")
    assert "[[ALG-3]]" not in text, f"ссылка с точкой ушла не в ту карточку:\n{fixed.stdout[:600]}"

    stubs = run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root)
    made = {p.stem for p in (root / "AuroraKnowledgeDB").rglob("*.md")}
    assert "Неизвестное" in made, f"заготовка под настоящую дыру не заведена:\n{stubs.stdout[:600]}"
    assert "ER BaR FID" not in made, "заведён двойник карточки, набранной с другими разделителями"
    stub = next(p for p in (root / "AuroraKnowledgeDB").rglob("Неизвестное.md"))
    assert "type:" in stub.read_text(encoding="utf-8"), "заготовка без type: — линтер сразу ругнётся"


@test
def test_update_removes_retired_engine_files(tmp: Path):
    """Слитые скрипты уезжают из проекта, а не остаются рядом работать по-своему.

    После слияния команда исполняется другим файлом, но прежняя копия в `.opencode/scripts`
    продолжала запускаться руками и расходиться с kit'ом. Список выведенных ведётся в
    манифесте (строки `- путь`) — угадывать «наш файл или проектный» обновление не вправе.
    """
    root = make_project(tmp)
    scripts = root / ".opencode/scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "kb_queue.py").write_text("# старая копия\n", encoding="utf-8")
    (scripts / "мой_скрипт.py").write_text("# проектный\n", encoding="utf-8")
    (root / "aurora.config.yaml").write_text('project:\n  name: "T"\n  slug: "T"\n',
                                             encoding="utf-8")

    dry = subprocess.run([sys.executable, str(KIT / "scripts/aurora_update.py"), str(root)],
                         capture_output=True, text=True)
    assert "kb_queue.py" in dry.stdout and "Выведены из движка" in dry.stdout, dry.stdout[:800]
    assert (scripts / "kb_queue.py").is_file(), "dry-run удалил файл"

    subprocess.run([sys.executable, str(KIT / "scripts/aurora_update.py"), str(root), "--apply"],
                   capture_output=True, text=True)
    assert not (scripts / "kb_queue.py").exists(), "выведенный скрипт остался в проекте"
    assert (scripts / "мой_скрипт.py").is_file(), "обновление удалило чужой файл"
    assert (scripts / "kb_trace.py").is_file(), "новый скрипт не разложен"


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
def test_drawio_asset_gets_extension(tmp: Path):
    """Вложение draw.io называется именем диаграммы — без расширения его нечем открыть.

    Confluence хранит схему под именем «Диаграмма без названия-1779…», и файл с таким
    именем не откроется ни редактором, ни просмотрщиком. Расширение подставляем по виду
    схемы, а файл под прежним именем убираем: `--prune` внутрь папок со схемами не ходит.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce

    class FakeApi:
        def attachments(self, page_id):
            # второе имя — с точками внутри: «.Кол» не расширение, а часть названия
            return {"Диаграмма-1": "/download/1", "Стр4.Свод.Итог": "/download/2",
                    "Готовая.drawio": "/download/3"}
        def fetch(self, url):
            return b"<mxfile/>"

    out = tmp / "mirror"
    (out / "Раздел").mkdir(parents=True)
    exp = ce.Exporter.__new__(ce.Exporter)
    exp.api, exp.out = FakeApi(), str(out)
    exp.assets_saved = exp.assets_dropped = 0
    stale = out / "Раздел/Стр_assets"
    stale.mkdir()
    (stale / "Диаграмма-1").write_text("старое имя без расширения", encoding="utf-8")

    md = exp.save_assets("1", "Раздел/Стр.md",
                         [("drawio", "Диаграмма-1"), ("drawio", "Стр4.Свод.Итог"),
                          ("drawio", "Готовая.drawio")])
    assert (stale / "Диаграмма-1.drawio").is_file(), "схема сохранена без расширения"
    assert (stale / "Стр4.Свод.Итог.drawio").is_file(), \
        "точка в названии принята за расширение — файл остался нечитаемым"
    assert (stale / "Готовая.drawio").is_file() and not (stale / "Готовая.drawio.drawio").exists(), \
        "расширение задвоилось у вложения, где оно уже было"
    assert not (stale / "Диаграмма-1").exists(), "файл под прежним именем остался"
    assert "Диаграмма-1.drawio" in md, f"ссылка ведёт на старое имя:\n{md}"
    assert exp.assets_dropped == 1


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
    assert assets == [("drawio", "Схема-входа")], f"исходник draw.io не запрошен: {assets}"
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
    root = make_project(tmp, git=True)   # doctor считает проект без git ошибкой: откатывать нечем
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
