#!/usr/bin/env python3
"""part_207 — проверка реализованности требований из «Продуктивности».

MVP без модели: разбор разделов требований, арифметика процента и устойчивость к
проекту без индексов Code. Отдельно проверяем, что форма зовёт API и имеет подписи.
"""
from __future__ import annotations

import json
import sys

from harness import KIT, SCRIPTS, make_project, run, test

sys.path.insert(0, str(KIT / "scripts"))
import requirements_check as RQ  # noqa: E402
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402

SPEC = """# Спецификация

## Контекст
- Это не требование.

## Требования
- [ ] Панель показывает процент
- [x] Кнопка продолжает маршрут
1. API отвечает JSON
| # | Требование |
|---|---|
| 1 | Сохранять документы |

## Риски
- Пожар
"""


@test
def test_requirements_parser_skips_context_and_reads_items(_t):
    rows = RQ.parse_requirements(SPEC, "spec.md")
    texts = [r["text"] for r in rows]
    assert texts == ["Панель показывает процент", "Кнопка продолжает маршрут",
                     "API отвечает JSON", "1 — Сохранять документы"], texts
    assert [r["id"] for r in rows] == ["R-1", "R-2", "R-3", "R-4"]
    assert rows[0]["source"] == "spec.md" and rows[0]["line"] == 7, rows[0]
    assert RQ.parse_requirements("## Требования\n- {{placeholder}}\n", "x.md") == []
    fallback = RQ.parse_requirements("- [ ] Работает без заголовка\n", "x.md")
    assert [r["text"] for r in fallback] == ["Работает без заголовка"], fallback
    code_name = RQ.parse_requirements("## Требования\n- `order_log` и DUPLICATE_ORDER\n", "x.md")
    assert code_name[0]["text"] == "order_log и DUPLICATE_ORDER", code_name


@test
def test_requirements_percent_and_missing_index_are_safe(_t):
    s = RQ.summarize([{"verdict": "found"}, {"verdict": "partial"},
                      {"verdict": "not_found"}])
    assert s == {"total": 3, "found": 1, "partial": 1, "not_found": 1,
                 "percent": 50.0}, s
    assert RQ.summarize([])["percent"] == 0.0
    root = make_project(_t)
    (root / "spec.md").write_text(SPEC, encoding="utf-8")
    (root / "outside.md").write_text("## Требования\n- Секрет\n", encoding="utf-8")
    got = RQ.check(str(root), ["spec.md", "../outside.md"])
    assert got["summary"]["total"] == 4, got["summary"]
    assert got["summary"]["percent"] == 0.0, got["summary"]
    assert [d["path"] for d in got["documents"]] == ["spec.md"], got["documents"]
    assert all(r["verdict"] == "not_found" for r in got["documents"][0]["requirements"])


@test
def test_requirements_find_evidence_in_an_indexed_repository(_t):
    root = make_project(_t)
    (root / "spec.md").write_text(
        "## Требования\n"
        "1. POST /api/orders пишет в order_log.\n"
        "2. Дубль отклоняется с кодом `DUPLICATE_ORDER`.\n"
        "3. Событие уходит в топик orders.accepted.\n", encoding="utf-8")
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files={
        "src/OrderController.java": (
            "package demo;\n"
            "@RestController\n"
            "@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  private static final String DUPLICATE = \"DUPLICATE_ORDER\";\n"
            "  @PostMapping(\"/orders\")\n"
            "  public String create() { return DUPLICATE; }\n"
            "}\n"),
        "src/OrderListener.java": (
            "package demo;\n"
            "public class OrderListener {\n"
            "  @KafkaListener(topics = \"orders.accepted\")\n"
            "  public void on(String event) {}\n"
            "}\n"),
        "db/schema.sql": "CREATE TABLE order_log (id INT);\n",
    })
    config = root / "aurora.config.yaml"
    config.write_text(CR.write_block(config.read_text(encoding="utf-8"), {
        "orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}}),
        encoding="utf-8")
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    got = RQ.check(str(root), ["spec.md"])
    rows = got["documents"][0]["requirements"]
    assert got["summary"]["total"] == 3, got["summary"]
    assert any(r["verdict"] == "found" for r in rows), rows
    assert got["summary"]["percent"] > 0, got["summary"]
    assert any(e["kind"] == "route" for r in rows for e in r["evidence"]), rows


@test
def test_requirements_export_writes_a_report_inside_the_project(_t):
    root = make_project(_t)
    (root / "spec.md").write_text(SPEC, encoding="utf-8")
    got = RQ.write_report(str(root), ["spec.md"])
    assert got["ok"] and got["path"].startswith("ArtifactsCode/requirements/"), got
    report = root / got["path"]
    assert report.is_file(), got
    text = report.read_text(encoding="utf-8")
    assert "Проверка требований по коду" in text and "Требований: 4" in text, text
    assert "Источник: `spec.md`" in text, text
    for bad in ("../outside.md", "Templates/x.md", "/tmp/x.md"):
        try:
            RQ.write_report(str(root), ["spec.md"], bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"путь {bad} должен быть отвергнут")


@test
def test_requirements_folder_expands_only_markdown_documents(_t):
    root = make_project(_t)
    docs = root / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("## Требования\n- Первое\n", encoding="utf-8")
    (docs / "two.md").write_text("## Требования\n- Второе\n", encoding="utf-8")
    (docs / "ignore.txt").write_text("## Требования\n- Не документ\n", encoding="utf-8")
    got = RQ.check(str(root), ["docs", "docs/one.md"])
    assert [d["path"] for d in got["documents"]] == ["docs/one.md", "docs/two.md"], got["documents"]
    assert got["summary"]["total"] == 2, got["summary"]


@test
def test_requirements_ui_uses_the_check_endpoint(_t):
    js = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    html = (KIT / "cockpit/modules/work/view.html").read_text(encoding="utf-8")
    server = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "/api/requirements/check" in js and "runReqCheck" in js, js[:200]
    assert '"/api/requirements/check"' in server and '"/api/requirements/export"' in server, server[:200]
    assert 'id="reqGo"' in html and 'id="reqResult"' in html and 'id="reqExport"' in html, html[-500:]
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/work/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        for key in ("work.req.title", "work.req.go", "work.req.summary",
                    "work.req.found", "work.req.partial", "work.req.not_found",
                    "work.req.export", "work.req.col_comment", "work.req.index_error"):
            assert key in data, (lang, key)
