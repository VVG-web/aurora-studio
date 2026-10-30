"""Проверки движка Aurora, часть 173. Каркас — tests/harness.py, подставной Confluence — part_148.py.

Операции вики (роль `wiki`) у коннектора Confluence Data Center — ровно то, что публикация делает с вики, чтобы
следующим шагом `ship:publish` перешёл на них без потери поведения: найти страницу по заголовку, прочитать её (версия,
предки, тело, пространство), дети постранично с отчётом о неполном списке, создать, обновить с проверкой версии
(чужая правка — `conflict`), перенести под другого родителя, пространство страницы. Запись — одной попыткой.
"""
from __future__ import annotations

from pathlib import Path

from cases import part_148 as W
from cases import part_170 as T
from harness import test

ENV = {"CONFLUENCE_PAT": T.TOKEN, "CONFLUENCE_PERSONAL_TOKEN": "", "CONFLUENCE_USER": "", "CONFLUENCE_PASSWORD": ""}


def _wiki(tmp: Path):
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url)
    rid = wiki.add("Корень", None, "DOC")
    return wiki, root, rid, T._mod("connector_ops")


def _call(CO, root, op, **params):
    with T.env(**ENV):
        return CO.call(str(root), "wiki", op, params)


@test
def test_confluence_connector_wiki_ops_find_and_read_a_page_and_its_space(tmp: Path):
    """`find_page` — страница пространства с точным заголовком (или `null`) с версией, автором правки, предками, телом и
    адресом; `get_page` — то же по номеру (`body: false` — без тела в запросе); `page_space` — пространство страницы.
    Нет страницы — `not_found`; не номер — отказ до сервера. Набор операций манифеста — ровно контракт роли."""
    wiki, root, rid, CO = _wiki(tmp)
    try:
        pid = wiki.add("Платёж за пять дней", rid, "DOC", "<p>тело страницы</p>")
        m, _fail, _src = CO.pick(str(root), "wiki")
        assert m["operations"]["ops"] == list(CO.ROLES["wiki"]), "манифест вики разошёлся с контрактом роли"
        found = _call(CO, root, "find_page", space="DOC", title="Платёж за пять дней")
        assert found["ok"], found
        page = found["result"]["page"]
        assert page["id"] == pid and page["title"] == "Платёж за пять дней" and page["space"] == "DOC", page
        assert page["version"] == 1 and page["by"] == "Коллега" and page["when"] == "2026-10-01", page
        assert page["ancestors"] == [{"id": rid, "title": "Корень"}] and "тело страницы" in page["body"], page
        assert page["url"] == f"{wiki.url}/pages/viewpage.action?pageId={pid}", page
        assert any("expand=version,space,ancestors,body.storage" in p for m_, p, _b in wiki.log if "title=" in p)
        none = _call(CO, root, "find_page", space="DOC", title="Нет такой")
        assert none["ok"] and none["result"]["page"] is None, none
        got = _call(CO, root, "get_page", page_id=pid)
        assert got["ok"] and {k: got["result"][k] for k in ("id", "version", "space")} == \
            {"id": pid, "version": 1, "space": "DOC"} and got["result"]["ancestors"][-1]["id"] == rid, got
        _call(CO, root, "get_page", page_id=pid, body=False)
        assert wiki.log[-1][1].endswith(f"/rest/api/content/{pid}?expand=version,space,ancestors"), wiki.log[-1]
        space = _call(CO, root, "page_space", page_id=pid)
        assert space["ok"] and space["result"] == {"id": pid, "title": "Платёж за пять дней", "space": "DOC"}, space
        missing = _call(CO, root, "get_page", page_id="999999")
        assert not missing["ok"] and missing["kind"] == "not_found", missing
        bad = _call(CO, root, "get_page", page_id="../etc")
        assert not bad["ok"] and bad["kind"] == "bad_request", bad
    finally:
        wiki.close()


@test
def test_confluence_connector_children_come_page_by_page_and_report_a_gap(tmp: Path):
    """`children` дочитывает список по `_links.next` (у подставного сервера — по две страницы в ответе) и говорит, полон
    ли он: сбой на продолжении — `complete: false` и причина (`gap`), полученное не теряется; отказ на первом ответе —
    отказ операции с видом (403 — `forbidden`)."""
    wiki, root, rid, CO = _wiki(tmp)
    try:
        kids = [wiki.add(f"Раздел {i}", rid, "DOC") for i in range(5)]
        res = _call(CO, root, "children", page_id=rid)
        assert res["ok"] and res["result"]["complete"] and res["result"]["gap"] == "", res
        assert [p["id"] for p in res["result"]["pages"]] == kids and res["result"]["got"] == 5, res
        assert len([1 for m, p, _b in wiki.log if "/child/page" in p]) == 3, "список детей прочитан не постранично"
        wiki.fail.append(("GET", "start=2", 503))
        part = _call(CO, root, "children", page_id=rid)
        assert part["ok"] and not part["result"]["complete"] and "503" in part["result"]["gap"], part
        assert [p["id"] for p in part["result"]["pages"]] == kids[:2], part
        wiki.fail.append(("GET", "/child/page", 403))
        refused = _call(CO, root, "children", page_id=rid)
        assert not refused["ok"] and refused["kind"] == "forbidden", refused
    finally:
        wiki.close()


@test
def test_confluence_connector_creates_updates_moves_and_refuses_a_stale_version(tmp: Path):
    """`create_page` — новая страница под родителем; заголовок занят — `bad_request`. `update_page` — поверх версии,
    которую видел вызывающий; правка после неё (в том числе «человек сохранил, пока шла запись») — `conflict`, ничего
    не записано. `update_page` с `parent_id` переносит вместе с правкой; `move_page` переносит без правки текста, а с
    устаревшей версией отказывает без записи. Каждая запись — одна попытка."""
    wiki, root, rid, CO = _wiki(tmp)
    try:
        other = wiki.add("Другой раздел", rid, "DOC")
        made = _call(CO, root, "create_page", space="DOC", parent_id=rid, title="Новая", body="<p>раз</p>")
        assert made["ok"] and made["result"]["version"] == 1 and made["result"]["space"] == "DOC", made
        pid = made["result"]["id"]
        assert "Новая" in wiki.children(rid)
        dup = _call(CO, root, "create_page", space="DOC", parent_id=rid, title="Новая", body="")
        assert not dup["ok"] and dup["kind"] == "bad_request" and "already exists" in dup["detail"], dup
        upd = _call(CO, root, "update_page", page_id=pid, title="Новая", body="<p>два</p>", version=1, space="DOC",
                    message="aurora publish")
        assert upd["ok"] and upd["result"]["version"] == 2 and "два" in wiki.pages[pid]["body"], upd
        writes = len(wiki.writes())
        stale = _call(CO, root, "update_page", page_id=pid, title="Новая", body="<p>три</p>", version=1)
        assert not stale["ok"] and stale["kind"] == "conflict" and "два" in wiki.pages[pid]["body"], stale
        assert len(wiki.writes()) == writes + 1, "конфликт повторяли"
        wiki.race.add(pid)
        raced = _call(CO, root, "update_page", page_id=pid, title="Новая", body="<p>три</p>", version=2)
        assert not raced["ok"] and raced["kind"] == "conflict" and "три" not in wiki.pages[pid]["body"], raced
        now = wiki.pages[pid]["version"]
        moved = _call(CO, root, "move_page", page_id=pid, parent_id=other)
        assert moved["ok"] and wiki.pages[pid]["parent"] == other and moved["result"]["version"] == now + 1, moved
        assert wiki.pages[pid]["title"] == "Новая" and "правка руками" in wiki.pages[pid]["body"], "перенос тронул текст"
        writes = len(wiki.writes())
        late = _call(CO, root, "move_page", page_id=pid, parent_id=rid, version=now)
        assert not late["ok"] and late["kind"] == "conflict" and len(wiki.writes()) == writes, late
        both = _call(CO, root, "update_page", page_id=pid, title="Новая", body="<p>четыре</p>",
                     version=wiki.pages[pid]["version"], parent_id=rid)
        assert both["ok"] and wiki.pages[pid]["parent"] == rid and "четыре" in wiki.pages[pid]["body"], both
        bad = _call(CO, root, "update_page", page_id=pid, title="Новая", body="", version="v2")
        assert not bad["ok"] and bad["kind"] == "bad_request", bad
    finally:
        wiki.close()
