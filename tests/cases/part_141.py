"""Проверки движка Aurora, часть 141. Каркас и помощники — tests/harness.py.

Название сущности и карты, когда у страницы Confluence нет текста, а есть только схема (draw.io): номер страницы
именем не бывает. Зеркало кладёт страницу глубокой ветки под её номером (`100003.md`), а расшифровка вложения не
повторяет название страницы, — модель видела в пути одни номера и называла по ним, а карта документа получала имя
«Документ · 100003». Страницы, сервер и модель подставные (PRJ-A, example.com); сети нет, живые проекты не читаются.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

PRJ_ROOT = "PRJ-A Root"
LONG = "Длинное название раздела верхнего уровня пространства PRJ-A для проверки пути "


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _drawio(*labels: str) -> bytes:
    cells = "".join(f'<mxCell id="n{i}" value="{t}" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>'
                    for i, t in enumerate(labels))
    edge = ('<mxCell id="e" value="далее" edge="1" source="n0" target="n1" parent="1"><mxGeometry as="geometry"/>'
            "</mxCell>" if len(labels) > 1 else "")
    return ('<mxfile><diagram name="Схема"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + cells + edge + "</root></mxGraphModel></diagram></mxfile>").encode("utf-8")


def _macro(name: str) -> str:
    return (f'<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">{name}</ac:parameter>'
            "</ac:structured-macro>")


def _wiki(CE, pages: dict, tree: dict):
    """Подставной сервер на настоящем клиенте: `pages` — {номер: (название, storage, {вложение: байты})}."""
    class FakeApi(CE.Api):
        def get(self, path):
            u = urllib.parse.urlparse(path)
            parts = u.path.split("/")
            if u.path.endswith("/child/page"):
                kids = tree.get(parts[4], [])
                return {"results": [{"id": c, "title": pages[c][0], "version": {"number": 1}} for c in kids],
                        "size": len(kids), "limit": 50, "_links": {}}
            if u.path.endswith("/child/attachment"):
                return {"results": [{"title": n, "_links": {"download": f"/dl/{parts[4]}/{n}"},
                                     "extensions": {"fileSize": len(b)}} for n, b in pages[parts[4]][2].items()],
                        "_links": {}}
            if u.path.startswith("/rest/api/user"):
                return {}
            pid = parts[-1]
            return {"title": pages[pid][0], "id": pid, "version": {"number": 1, "when": "2026-10-01T00:00:00.000Z"},
                    "body": {"storage": {"value": pages[pid][1]}}, "space": {"key": "S"},
                    "_links": {"webui": f"/p/{pid}"}, "ancestors": []}

        def fetch(self, url, max_bytes=None):
            _, _, pid, name = url.split("/", 3)
            return pages[pid][2][name]

    return FakeApi("https://wiki.example.com", "")


def _export(CE, root: Path, pages: dict, tree: dict, top: str, limit: int | None = None) -> None:
    """Выгрузить дерево в `Sources/Confluence` проекта; `limit` — предел длины пути (глубокая ветка)."""
    here, was = os.getcwd(), CE.PATH_CHARS
    os.chdir(root)
    try:
        if limit:
            CE.PATH_CHARS = limit
        exp = CE.Exporter(_wiki(CE, pages, tree), str(root / "Sources/Confluence"), "https://wiki.example.com",
                          "S", True)
        with contextlib.redirect_stdout(io.StringIO()):
            exp.walk(top, [])
    finally:
        CE.PATH_CHARS = was
        os.chdir(here)


def _rel(root: Path, *parts: str) -> list:
    return sorted(str(p.relative_to(root)).replace(os.sep, "/") for p in (root / Path(*parts)).rglob("*.md"))


def _text(prefix: str, word: str = "PRJ-A") -> str:
    return "".join(f"<p>{prefix} {word}: описание {i}, достаточно длинное, чтобы быть источником разбора.</p>"
                   for i in range(6))


@test
def test_a_page_without_text_keeps_its_own_title_in_the_mirror_the_plan_and_the_stub(tmp: Path):
    """Контроль гипотезы «вместо названия страницы в сущность уходит номер»: у обычного дерева страница без текста
    называется заголовком везде — в шапке, в имени файла, в имени источника и заготовки-указателя; номера нет."""
    CE = _mod("confluence_export")
    BP = _mod("build_plan")
    empty_diagram = b"<mxfile/>"                                                            # не схема: расшифровки нет
    pages = {
        "100001": (PRJ_ROOT, _text("Корень"), {}),
        "100002": ("Page One", _macro("Empty-1"), {"Empty-1.drawio": empty_diagram}),         # схема, которую не прочесть
        "100003": ("Page Two", "", {}),                                                       # (а) пусто совсем
        "100004": ("Page Three", '<p><ac:link><ri:attachment ri:filename="spec.pdf"/></ac:link></p>',
                   {"spec.pdf": b"%PDF-1.4"}),                                                # (б) только ссылка
        "100005": ("Page Four", _macro("Flow-1"),
                   {"Flow-1.drawio": _drawio("Принять заявку PRJ-A", "Проверить заявку PRJ-A")}),   # (в) текст в схеме
        "100006": ("Page Five", _text("Страница") + _macro("Flow-2"),
                   {"Flow-2.drawio": _drawio("Принять письмо PRJ-A", "Проверить письмо PRJ-A")}),   # (г) контроль
    }
    root = make_project(tmp)
    _export(CE, root, pages, {"100001": ["100002", "100003", "100004", "100005", "100006"]}, "100001")
    mirror = _rel(root, "Sources/Confluence")
    for title in ("Page One", "Page Two", "Page Three", "Page Four", "Page Five"):
        stem = title.replace(" ", "_")
        assert f"Sources/Confluence/{PRJ_ROOT.replace(' ', '_')}/{stem}.md" in mirror, (title, mirror)
    assert not [p for p in mirror if re.search(r"\d{5,}", p)], f"номер страницы попал в имя файла: {mirror}"
    page_one = root / f"Sources/Confluence/{PRJ_ROOT.replace(' ', '_')}/Page_One.md"
    head = page_one.read_text(encoding="utf-8")
    assert 'title: "Page One"' in head and "page_id: 100002" in head and "# Page One" in head, head
    os.chdir(root)
    assert BP.source_title(str(page_one.relative_to(root))) == "Page One"
    empty = root / f"Sources/Confluence/{PRJ_ROOT.replace(' ', '_')}/Page_Two.md"
    assert BP.source_title(str(empty.relative_to(root))) == "Page Two", "пустая страница названа не заголовком"

    # расшифровки схем: страница-владелец названа в подсказке для модели, номера в ней нет
    run("office_ingest.py", cwd=root)           # нечитаемые схема и pdf — ошибка команды, остальное расшифровано
    transcripts = _rel(root, "Raw/Extracted")
    assert len(transcripts) == 2, transcripts
    flow = next(t for t in transcripts if "Flow-1" in t)
    note = BP.page_note(flow)
    assert "«Page Four»" in note and "номер" not in note, note
    assert BP.page_note(str(page_one.relative_to(root))) == "", "у страницы с обычным именем файла подсказки нет"

    # заготовки-указатели: по заголовку страницы; пустая страница и страница с расшифровкой заготовки не получают
    cp = run("kb_fix.py", "--unparsed", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    cards = sorted(p.name for p in (root / "AuroraKnowledgeDB/Concepts").glob("*.md"))
    assert "Page-One.md" in cards and "Page-Three.md" in cards, (cards, cp.stdout)
    assert not [c for c in cards if re.search(r"\d{4,}", c) or "Page-Two" in c or "Page-Four" in c], cards
    assert "Без названия страницы" not in cp.stdout, cp.stdout


@test
def test_a_deep_page_with_only_a_diagram_reaches_the_model_under_its_title_not_its_number(tmp: Path):
    """Глубокая ветка: страница лежит под своим номером (`100003.md`), расшифровка схемы — в папке с этим номером.
    Подсказка называет страницу заголовком из шапки; карточка, названная номером, отклоняется со вторым заходом."""
    CE = _mod("confluence_export")
    BP = _mod("build_plan")
    AG = _mod("agent_core")
    AR = _mod("agent_runner")
    pages = {
        "100001": (LONG + "1", _text("Корень"), {}),
        "100002": (LONG + "2", _text("Раздел"), {}),
        "100003": ("Приём заявок PRJ-A", _macro("Схема без названия-1779012345"),
                   {"Схема без названия-1779012345.drawio": _drawio("Принять заявку PRJ-A", "Проверить заявку PRJ-A")}),
    }
    root = make_project(tmp)
    _export(CE, root, pages, {"100001": ["100002"], "100002": ["100003"]}, "100001", limit=80)
    page = "Sources/Confluence/100001/100002/100003.md"
    assert page in _rel(root, "Sources/Confluence"), _rel(root, "Sources/Confluence")
    run("office_ingest.py", cwd=root, expect_rc=0)
    transcript = next(t for t in _rel(root, "Raw/Extracted") if "/100003/" in t)
    assert "Приём" not in transcript and "100003" in transcript, f"имя расшифровки должно быть без названия: {transcript}"

    root_s = str(root)
    assert BP.owner_page(transcript, root_s) == page and BP.owner_page(page, root_s) == page
    assert BP.page_title_of(page, root_s) == "Приём заявок PRJ-A"
    # зеркало, записанное на Windows (`\r\n`), читается так же
    crlf = root / "Sources/Confluence/100001/100002/100003.md"
    keep = crlf.read_bytes()
    crlf.write_bytes(keep.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert BP.page_title_of(page, root_s) == "Приём заявок PRJ-A" and BP.numbered_page(page, root_s)
    crlf.write_bytes(keep)
    note = BP.page_note(transcript, root_s)
    assert "«Приём заявок PRJ-A»" in note and "номер" in note, note
    assert "«Приём заявок PRJ-A»" in BP.page_note(page, root_s) and "номер" in BP.page_note(page, root_s)
    # номер страницы — не имя: ни он сам, ни «page_<номер>»; слова с цифрами и коды остаются именами
    for bad in ("100003", "page_100003", "Page_100003", "page 100003", "page-100003"):
        assert BP.named_by_page_id(bad, transcript, root_s) and BP.named_by_page_id(bad, page, root_s), bad
    for good in ("Приём заявок PRJ-A", "Заявка 100003", "404", "ALG-100003", "Page 2"):
        assert not BP.named_by_page_id(good, transcript, root_s), good
    assert not BP.named_by_page_id("", transcript, root_s)

    # агент: первый ответ называет карточку номером — отклонён с названием страницы; второй проходит
    answers = iter([json.dumps({"cards": [{"title": "100003", "sections": "1", "to": "Concepts", "summary": "схема"}]},
                               ensure_ascii=False),
                    json.dumps({"cards": [{"title": "Приём заявок PRJ-A", "sections": "1", "to": "Concepts",
                                           "summary": "Заявку принимают и проверяют"}]}, ensure_ascii=False)])
    prompts: list = []

    def fake(cfg, role, messages, **kw):
        prompts.append(messages[0]["content"])
        return {"ok": True, "text": next(answers), "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    os.chdir(root)          # провенанс карточки (`source_refs`) читает страницу-владельца от папки процесса
    step = AR.solve_source(cfg, root_s, "Вложения", transcript, True, False, fake, 0)
    assert step["status"] == "разобран", step
    assert len(prompts) == 2, f"ждали отказ и второй заход: {len(prompts)}"
    assert "Название страницы Confluence, к которой относится вложение: «Приём заявок PRJ-A»" in prompts[0], prompts[0][:600]
    assert "ПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА" in prompts[1] and "названа номером страницы" in prompts[1] \
        and "«Приём заявок PRJ-A»" in prompts[1].split("ПРЕДЫДУЩАЯ ПОПЫТКА")[1], prompts[1][-500:]
    names = sorted(p.name for p in (root / "AuroraKnowledgeDB/Concepts").glob("*.md"))
    assert names == ["Приём-заявок-PRJ-A.md"], names
    card = (root / "AuroraKnowledgeDB/Concepts/Приём-заявок-PRJ-A.md").read_text(encoding="utf-8")
    assert 'title: "Приём заявок PRJ-A"' in card and page in card and transcript in card, card[:700]

    # ручной ввод тем же правилом: `--card <номер>` не заводит карточку
    for bad in ("100003", "page_100003"):
        cp = run("build_plan.py", "--card", bad, "--source", transcript, "--sections", "1", "--to", "Concepts",
                 "--apply", cwd=root)
        assert cp.returncode == 1 and "номер страницы Confluence" in cp.stderr and "Приём заявок PRJ-A" in cp.stderr, \
            (bad, cp.stdout, cp.stderr)
    assert sorted(p.name for p in (root / "AuroraKnowledgeDB/Concepts").glob("*.md")) == names, "карточка-номер заведена"


@test
def test_the_by_source_map_of_a_numbered_page_carries_its_title_and_ordinary_maps_keep_their_names(tmp: Path):
    """Карта документа страницы под номером называется её заголовком («Документ · Приём заявок PRJ-A»), а не
    «Документ · 100003»; обычная страница и расшифровка вложения называются по-прежнему — именем файла."""
    CE = _mod("confluence_export")
    pages = {
        "100001": (LONG + "1", _text("Корень"), {}),
        "100002": (LONG + "2", _text("Раздел"), {}),
        "100003": ("Приём заявок PRJ-A", _text("Приём") + _macro("Схема-1"),
                   {"Схема-1.drawio": _drawio("Принять заявку PRJ-A", "Проверить заявку PRJ-A")}),
    }
    root = make_project(tmp)
    _export(CE, root, pages, {"100001": ["100002"], "100002": ["100003"]}, "100001", limit=80)
    deep = "Sources/Confluence/100001/100002/100003.md"
    assert deep in _rel(root, "Sources/Confluence")
    run("office_ingest.py", cwd=root, expect_rc=0)
    plain = root / "Sources/Confluence/Обычная.md"
    plain.write_text('---\npage_id: 200001\ntitle: "Обычная страница"\n---\n\n# Обычная страница\n\n'
                     + "Текст обычной страницы PRJ-A, достаточно длинный. " * 8 + "\n", encoding="utf-8")
    transcript = next(t for t in _rel(root, "Raw/Extracted") if "/100003/" in t)
    names = iter(["Альфа сущность", "Бета сущность", "Гамма сущность", "Дельта сущность", "Эпсилон сущность",
                  "Дзета сущность"])
    for src in (deep, deep, "Sources/Confluence/Обычная.md", "Sources/Confluence/Обычная.md", transcript, transcript):
        name = next(names)
        run("build_plan.py", "--card", name, "--source", src, "--sections", "1", "--to", "Concepts", "--apply",
            cwd=root, expect_rc=0)
    cp = run("kb_moc.py", "--by-source", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    maps = sorted(p.name for p in (root / "AuroraKnowledgeDB/MOC").glob("Документ--*.md"))
    assert "Документ--Приём-заявок-PRJ-A.md" in maps and "Документ--Обычная.md" in maps, (maps, cp.stdout)
    assert not [m for m in maps if "100003" in m], f"номер страницы в имени карты: {maps}"
    assert any(m.startswith("Документ--Схема-1") for m in maps), maps          # расшифровка — по имени вложения
    text = (root / "AuroraKnowledgeDB/MOC/Документ--Приём-заявок-PRJ-A.md").read_text(encoding="utf-8")
    assert 'title: "Документ · Приём заявок PRJ-A"' in text and f"`{deep}`" in text, text[:500]
    # повторный проход — те же карты, без переименований и без записи
    again = run("kb_moc.py", "--by-source", "--apply", "--allow-dirty", cwd=root, expect_rc=0).stdout
    assert "без изменений: 3" in again and sorted(p.name for p in (root / "AuroraKnowledgeDB/MOC").glob("Документ--*.md")) == maps


@test
def test_a_source_whose_title_is_lost_and_whose_file_name_is_a_page_number_gets_no_stub_and_is_reported(tmp: Path):
    """Названия нет ни в шапке, ни заголовком, а имя файла — номер страницы: заготовку номером не называем, а источник
    называем в отчёте. С названием в шапке заготовка заводится как прежде."""
    BP = _mod("build_plan")
    root = make_project(tmp)
    link = "Схема процесса: [схема.drawio](100003_assets/схема.drawio)\n"
    nameless = root / "Sources/Confluence/100003.md"
    nameless.write_text("---\npage_id: 100003\nspace: S\nversion: 1\nupdated: 2026-10-01\nurl: https://wiki.example.com/p/100003\n"
                        "breadcrumbs: \"\"\n---\n\n" + link, encoding="utf-8")
    titled = root / "Sources/Confluence/100004.md"
    titled.write_text('---\npage_id: 100004\ntitle: "Схема приёма PRJ-A"\nspace: S\nversion: 1\nupdated: 2026-10-01\n'
                      'url: https://wiki.example.com/p/100004\nbreadcrumbs: ""\n---\n\n# Схема приёма PRJ-A\n\n' + link,
                      encoding="utf-8")
    os.chdir(root)
    assert BP.source_title("Sources/Confluence/100003.md") == "", "номер принят за название"
    assert BP.source_title("Sources/Confluence/100004.md") == "Схема приёма PRJ-A"
    assert BP.source_title("Raw/project/Регламент.md", "Текст без шапки\n") == "Регламент", "обычное имя файла — название"
    cp = run("kb_fix.py", "--unparsed", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    cards = sorted(p.name for p in (root / "AuroraKnowledgeDB/Concepts").glob("*.md"))
    assert cards == ["Схема-приёма-PRJ-A.md"], (cards, cp.stdout)
    assert "Без названия страницы" in cp.stdout and "Sources/Confluence/100003.md" in cp.stdout, cp.stdout
