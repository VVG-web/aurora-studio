"""Проверки движка Aurora, часть 181. Каркас — tests/harness.py, подставная вики — part_148.py, сервер — part_107.py.

Выбор места публикации в окне самой публикации: операция `children` вики с признаком `has_children` (папка или лист —
дешёвый вопрос «limit=1», публикация тот же список без признака), кэш списка на час и `fresh=1`; API панели
`/api/publish/tree` (спуск по дереву от корня вида/шапки/конфига, уже лежащая страница — выбора нет) и
`/api/publish/place` (поле `publish_parent` шапки со слепком и 409, не-номер — 400); строка каталога в обоих языках.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

from cases import part_107 as S
from cases import part_148 as W
from cases import part_170 as T
from harness import make_project, set_home, test

ENV = {"CONFLUENCE_PAT": T.TOKEN, "CONFLUENCE_PERSONAL_TOKEN": "", "CONFLUENCE_USER": "", "CONFLUENCE_PASSWORD": ""}

KIND_US = ('artifacts:\n'
           '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
           '    publish_url: "{root}"\n'
           '    publish_path: "Релиз {{release}}"\n'
           '    publish_title: "[US] {{title}}"\n')


def _q(root: Path, **kw) -> str:
    return "?" + urllib.parse.urlencode({"project": str(root), **kw})


@test
def test_wiki_children_report_has_children_by_a_cheap_probe_and_cache(tmp: Path):
    """`children` с `deep` — каждой странице `has_children` (папка/лист): по одному дешёвому запросу «limit=1», не
    списку целиком; публикация зовёт список без признака (`deep: false`). Список кэшируется на час, `fresh=True`
    обходит кэш; ключ кэша различает родителей."""
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url)
    rid = wiki.add("Корень", None, "DOC")
    folder = wiki.add("Релиз 2.1", rid, "DOC")
    wiki.add("Раздел", folder, "DOC")
    wiki.add("Лист", rid, "DOC")
    CO = T._mod("connector_ops")
    try:
        with T.env(**ENV):
            got = CO.call(str(root), "wiki", "children", {"page_id": rid, "deep": True})
            assert got["ok"], got
            pages = {p["title"]: p for p in got["result"]["pages"]}
            assert pages["Релиз 2.1"]["has_children"] is True, pages
            assert pages["Лист"]["has_children"] is False, pages
            # дешёвый вопрос — limit=1, не полный список: в журнале по одному на страницу
            probes = [p for m, p, _b in wiki.log if m == "GET" and "child/page" in p and "limit=1" in p]
            assert len(probes) == 2 and any(f"/{folder}/child/page" in p for p in probes), probes
            # публикация — тот же список без признака: ни одного дешёвого вопроса
            wiki.log.clear()
            plain = CO.call(str(root), "wiki", "children", {"page_id": rid, "deep": False})
            assert plain["ok"] and all("has_children" not in p for p in plain["result"]["pages"]), plain
            assert not [p for m, p, _b in wiki.log if "limit=1" in p], wiki.log
            # кэш на час: второй раз без сети, fresh — снова к серверу
            wiki.srv.shutdown()
            again = CO.call(str(root), "wiki", "children", {"page_id": rid, "deep": True})
            assert again["ok"] and again["cached"], again
            other = CO.call(str(root), "wiki", "children", {"page_id": folder, "deep": True})
            assert not other.get("cached"), other
    finally:
        wiki.close()


@test
def test_publish_tree_panel_api_walks_from_the_kind_root_and_refuses_without_one(tmp: Path):
    """`GET /api/publish/tree` — дети корня публикации (порядок `ship:publish`: вид → шапка → конфиг) с признаком
    `has_children`; `parent` — спуск внутрь выбранной папки; корня нет — причина и куда идти; страница уже лежит
    (`confluence_page_id`) — `fixed`, выбора нет; файл менялся на диске — слепок тот же сервис читает."""
    restore = set_home(tmp / "home")
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url, KIND_US)
    rid = wiki.add("Корень", None, "DOC")
    folder = wiki.add("Релиз 2.1", rid, "DOC")
    wiki.add("Раздел", folder, "DOC")
    leaf = wiki.add("Лист", rid, "DOC")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("{root}", rid), encoding="utf-8")
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: us\n---\n\n# Итог\n", encoding="utf-8")
    srv, call = S._server(tmp)
    try:
        with T.env(**ENV):
            st, d = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and d["root"] == rid, d
            titles = {p["title"]: p["has_children"] for p in d["pages"]}
            assert titles == {"Релиз 2.1": True, "Лист": False}, d
            st, deep = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md", parent=folder))
            assert st == 200 and deep["root"] == folder and [x["title"] for x in deep["pages"]] == ["Раздел"], deep
            st, cached = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert cached.get("cached"), cached
            st, fresh = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md", fresh="1"))
            assert not fresh.get("cached"), fresh
            # корень и шапки нет: вид без publish_url, конфиг без publish_parent — причина и «настройки проекта»
            (tmp / "elsewhere").mkdir(exist_ok=True)
            other = make_project(tmp / "elsewhere")
            (other / "Artifacts" / "us").mkdir(parents=True, exist_ok=True)
            (other / "Artifacts" / "us" / "US-1.md").write_text("---\ntype: us\n---\n\n# Итог\n", encoding="utf-8")
            st, nope = call("GET", "/api/publish/tree" + _q(other, path="Artifacts/us/US-1.md"))
            assert st == 200 and nope["pages"] == [] and nope["why"], nope
            # страница уже лежит: confluence_page_id в шапке — выбора нет
            doc.write_text(f"---\ntype: us\nconfluence_page_id: {leaf}\n---\n\n# Итог\n", encoding="utf-8")
            st, fixed = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and fixed["fixed"] and fixed["pages"] == [], fixed
    finally:
        srv.shutdown()
        wiki.close()
        restore()


@test
def test_publish_place_panel_api_writes_the_header_with_a_snapshot_and_409(tmp: Path):
    """`POST /api/publish/place` — поле `publish_parent` шапки той же записью, что редактор: слепок обязателен,
    файл менялся — 409 и файл не тронут; не-номер — 400; повтор тем же номером — `unchanged`, файл не тронут;
    BOM и CRLF сохраняются (шапку пишет `set_field`, файл — `file_write`)."""
    restore = set_home(tmp / "home")
    root = make_project(tmp)
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("﻿---\ntype: us\r\n---\r\n\r\n# Итог\r\n", encoding="utf-8-sig")
    srv, call = S._server(tmp)
    try:
        st, first = call("GET", "/api/files/read" + _q(root, path="Artifacts/us/US-1.md"))
        digest = first.get("digest") or ""
        assert digest, first
        body = {"project": str(root), "path": "Artifacts/us/US-1.md", "parent": "506912207", "expect": digest}
        st, res = call("POST", "/api/publish/place", body)
        assert st == 200 and res["ok"] and res["parent"] == "506912207", res
        raw = doc.read_bytes()
        text = raw.decode("utf-8-sig")
        assert b"publish_parent: 506912207" in raw, raw[:80]
        assert raw.startswith(b"\xef\xbb\xbf"), "BOM потерян"
        assert "\r\n" in text, "CRLF потерян"
        assert "\r\n\r\n# Итог\r\n" in text, repr(text[-40:])
        st, again = call("POST", "/api/publish/place", {**body, "expect": res.get("digest") or digest})
        assert st == 200 and again["ok"] and again["unchanged"], again
        st, stale = call("POST", "/api/publish/place", {**body, "expect": "not-the-digest"})
        assert st == 409 and stale["conflict"], stale
        st, bad = call("POST", "/api/publish/place", {**body, "parent": "../etc", "expect": res.get("digest") or digest})
        assert st == 400 and bad["error"], bad
    finally:
        srv.shutdown()
        restore()


@test
def test_publish_place_texts_and_button_are_in_both_catalogs_and_the_script(tmp: Path):
    """Строки окна выбора места — в русском и английском каталогах без остатков `{…}`; кнопка «Выбрать место…» стоит в
    карточке публикации и сохраняет выбор полем `publish_parent` шапки; окно спрашивает `/api/publish/tree`, различает
    лист (выбор сразу) и папку («Положить в корень»)."""
    import json
    kit = Path(__file__).resolve().parent.parent.parent
    base = kit / "cockpit" / "i18n"
    ru = json.loads((base / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    keys = [k for k in ru if k.startswith("pub.place")]
    assert len(keys) >= 12, keys
    assert all(k in en for k in keys), [k for k in keys if k not in en]
    assert ru["pub.place_root"] == "Положить в корень «{title}»" and "{title}" in en["pub.place_root"]
    src = (kit / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert "function pickPublishPlace" in src, "окна выбора места нет в panel.js"
    assert "/api/publish/tree" in src and "/api/publish/place" in src, "окно не спрашивает API дерева/места"
    assert "publish_parent" not in src.split("pickPublishPlace")[1].split("\n}")[0], "окно само пишет поле шапки"
    card = src.split("function drawPublishCard")[1].split("\n}", 1)[0]
    assert 't("pub.place")' in card, "кнопки «Выбрать место…» нет в карточке публикации"
    assert "pickPublishPlace({" in card and "parent: place.id" in card, card[-400:]
    assert "has_children" in src, "лист/папка не различаются"
