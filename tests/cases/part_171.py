"""Проверки движка Aurora, часть 171. Каркас — tests/harness.py, подставная Jira — part_170.py.

Операции трекера задач (роль `tasks`) у коннектора Jira Data Center: типы задач без подзадач, эпики постранично и не
закрытые первыми, кэш списков на час (часы подменяются, «сейчас» не зашито), поле Epic Link по схеме (запасной путь —
имя, нет поля — родитель), поиск человека по почте, создание задачи одной попыткой, remote link без дублей, отказы с
видом (`auth`, `config`, `not_found`, `bad_request`, `net`). Вход — тот же, что у синка (`jira_export.connect`).
"""
from __future__ import annotations

import os
from pathlib import Path

from cases import part_170 as T
from harness import make_project, test


def _ops(tmp: Path, mode: str = "schema"):
    tracker = T.Tracker(mode)
    root = T.jira_project(tmp, tracker.url, sub=mode)
    return tracker, root, T._mod("connector_ops")


def _gets(tracker, part: str) -> int:
    return len([1 for m, p, _b, _a in tracker.log if m == "GET" and part in p])


@test
def test_jira_connector_issue_types_without_subtasks_and_epics_open_first_across_pages(tmp: Path):
    """`issue_types` — типы проекта без подзадач; `epics` — эпики проекта (тип найден среди типов проекта), все
    страницы поиска, не закрытые первыми, внутри групп — свежие первыми. Ключ уходит заголовком Bearer."""
    tracker, root, CO = _ops(tmp)
    try:
        with T.env(**T.jira_env(tmp)):
            types = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"})
            epics = CO.call(str(root), "tasks", "epics", {"project_key": "PRJA"})
            missing = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJZ"})
            bad = CO.call(str(root), "tasks", "epics", {"project_key": "prj a"})
        assert types["ok"] and [t["name"] for t in types["result"]["types"]] == ["Story", "Epic", "Task"], types
        assert types["result"]["types"][0] == {"id": "10001", "name": "Story", "description": "история"}, types
        assert epics["ok"], epics
        got = epics["result"]["epics"]
        assert [e["key"] for e in got] == ["PRJA-2", "PRJA-3", "PRJA-1"], got
        assert [e["done"] for e in got] == [False, False, True] and got[0]["title"] == "Открытый эпик", got
        assert got[0]["status"] == "In Progress", got
        searches = [p for m, p, _b, _a in tracker.log if "/rest/api/2/search" in p]
        assert len(searches) == 2 and all("issuetype%20%3D%2010000" in p for p in searches), searches
        assert all(a == f"Bearer {T.TOKEN}" for _m, _p, _b, a in tracker.log), "ключ не ушёл заголовком"
        assert not missing["ok"] and missing["kind"] == "not_found" and "404" in missing["detail"], missing
        assert not bad["ok"] and bad["kind"] == "bad_request", bad
    finally:
        tracker.close()


@test
def test_jira_connector_lists_are_cached_for_an_hour_by_the_clock(tmp: Path):
    """Типы задач и эпики берутся из кэша проекта, пока ему меньше часа по часам ядра (`connector_ops.clock`
    подменён — время не ждём и не зашиваем); `fresh` — заново; другой ключ проекта — другая запись. В файлах кэша —
    адрес сервера и ответ, но не ключ доступа."""
    tracker, root, CO = _ops(tmp)
    clock, now = CO.clock, [1_000_000.0]
    CO.clock = lambda: now[0]
    try:
        with T.env(**T.jira_env(tmp)):
            first = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"})
            assert first["ok"] and not first["cached"] and _gets(tracker, "/project/PRJA") == 1, first
            now[0] += 3599
            hit = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"})
            assert hit["ok"] and hit["cached"] and hit["result"] == first["result"], hit
            assert _gets(tracker, "/project/PRJA") == 1, "кэш моложе часа, а сервер спросили снова"
            fresh = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"}, fresh=True)
            assert fresh["ok"] and not fresh["cached"] and _gets(tracker, "/project/PRJA") == 2, fresh
            now[0] += 3601
            stale = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"})
            assert stale["ok"] and not stale["cached"] and _gets(tracker, "/project/PRJA") == 3, stale
            other = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJZ"})
            assert not other["ok"] and other["kind"] == "not_found", "другой ключ проекта взят из чужой записи кэша"
            # отказ в кэш не попадает: следующий вызов снова идёт на сервер
            CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJZ"})
            assert _gets(tracker, "/project/PRJZ") == 2
            # find_user и прочие операции не кэшируются
            CO.call(str(root), "tasks", "find_user", {"email": "analyst@example.com"})
            CO.call(str(root), "tasks", "find_user", {"email": "analyst@example.com"})
            assert _gets(tracker, "/user/search") == 2
        files = list((root / ".aurora/state/cache/connectors").glob("*.json"))
        assert len(files) == 1 and files[0].name.startswith("tasks-issue_types-"), files
        body = files[0].read_text(encoding="utf-8")
        assert tracker.url in body and T.TOKEN not in body and T.TOKEN not in files[0].name, body
    finally:
        CO.clock = clock
        tracker.close()


@test
def test_jira_connector_finds_the_epic_link_field_by_schema_and_falls_back_to_parent(tmp: Path):
    """Эпик ставится полем Epic Link: его номер находится в `/rest/api/2/field` по схеме
    `com.pyxis.greenhopper.jira:gh-epic-link` (приманка с именем «Epic Link» и чужой схемой не берётся), без схемы — по
    имени; поля нет совсем — эпик становится родителем (`parent`, как в Jira Cloud). Синк ищет поле тем же правилом."""
    JE = T._mod("jira_export")
    assert JE.epic_field_id([{"id": "customfield_1", "name": "Epic Link", "schema": {"custom": "x"}},
                             {"id": "customfield_2", "name": "Эпик", "schema": {"custom": JE.EPIC_LINK_SCHEMA}}]) \
        == "customfield_2"
    assert JE.epic_field_id([{"id": "customfield_3", "name": "Ссылка на эпик"}]) == "customfield_3"
    assert JE.epic_field_id([{"id": "summary", "name": "Summary"}, "мусор"]) == ""
    for mode, field in (("schema", "customfield_10500"), ("name", "customfield_10600"), ("none", "parent")):
        tracker, root, CO = _ops(tmp, mode)
        try:
            with T.env(**T.jira_env(tmp)):
                res = CO.call(str(root), "tasks", "create_issue",
                              {"project_key": "PRJA", "type": "Story", "summary": "С эпиком", "epic": "PRJA-2"})
            assert res["ok"] and res["result"]["epic_via"] == field, (mode, res)
            sent = tracker.created()[-1]["fields"]
            assert sent[field] == ({"key": "PRJA-2"} if field == "parent" else "PRJA-2"), (mode, sent)
            assert "customfield_10999" not in sent, "взята приманка с именем Epic Link и чужой схемой"
        finally:
            tracker.close()


@test
def test_jira_connector_creates_an_issue_with_epic_and_assignee_by_email(tmp: Path):
    """`find_user` находит логин по почте; `create_issue` заводит задачу с типом, исполнителем, метками, компонентами и
    эпиком и отвечает ключом и адресом; `get_issue` читает её. Отказ сервера (нет такого исполнителя) — `bad_request` с
    его словами; обрыв (503) — `net`, и POST ушёл ровно один раз: повтор завёл бы вторую задачу."""
    tracker, root, CO = _ops(tmp)
    try:
        with T.env(**T.jira_env(tmp)):
            who = CO.call(str(root), "tasks", "find_user", {"email": "Analyst@Example.com"})
            nobody = CO.call(str(root), "tasks", "find_user", {"email": "nobody@example.com"})
            res = CO.call(str(root), "tasks", "create_issue",
                          {"project_key": "PRJA", "type": "Story", "summary": "  Платёж\nза пять дней ",
                           "description": "Документ проекта: Artifacts/us/US-1.md", "assignee": who["result"]["name"],
                           "labels": ["analytics", "release-2"], "components": "Core, API", "epic": "PRJA-2"})
            issue = CO.call(str(root), "tasks", "get_issue", {"key": res["result"]["key"]})
            ghost = CO.call(str(root), "tasks", "create_issue",
                            {"project_key": "PRJA", "type": "Story", "summary": "x", "assignee": "ghost"})
            tracker.fail.append(("POST", "/rest/api/2/issue", 503))
            before = len(tracker.created())
            down = CO.call(str(root), "tasks", "create_issue", {"project_key": "PRJA", "type": "Task", "summary": "y"})
            no_type = CO.call(str(root), "tasks", "create_issue", {"project_key": "PRJA", "summary": "y"})
        assert who["ok"] and who["result"] == {"name": "analyst", "key": "JIRAUSER10", "display": "Analyst A",
                                               "email": "analyst@example.com"}, who
        assert not nobody["ok"] and nobody["kind"] == "not_found" and "nobody@example.com" in nobody["detail"], nobody
        assert res["ok"] and res["result"]["key"] == "PRJA-5" and res["result"]["url"] == f"{tracker.url}/browse/PRJA-5", res
        sent = tracker.created()[0]["fields"]
        assert sent["project"] == {"key": "PRJA"} and sent["issuetype"] == {"name": "Story"}, sent
        assert sent["summary"] == "Платёж за пять дней" and sent["assignee"] == {"name": "analyst"}, sent
        assert sent["labels"] == ["analytics", "release-2"] and sent["components"] == [{"name": "Core"}, {"name": "API"}]
        assert sent["description"].startswith("Документ проекта") and sent["customfield_10500"] == "PRJA-2", sent
        assert issue["ok"] and issue["result"]["status"] == "To Do" and issue["result"]["type"] == "Story", issue
        assert not ghost["ok"] and ghost["kind"] == "bad_request" and "ghost" in ghost["detail"], ghost
        assert not down["ok"] and down["kind"] == "net" and len(tracker.created()) == before + 1, \
            ("повтор POST после 503", down, len(tracker.created()) - before)
        assert not no_type["ok"] and no_type["kind"] == "bad_request", no_type
    finally:
        tracker.close()


@test
def test_jira_connector_links_a_page_once(tmp: Path):
    """`link_page` вешает на задачу remote link на страницу; повтор с тем же адресом ссылку не удваивает
    (`created: false`), другой адрес — вторая ссылка; не http(s) — отказ до сервера."""
    tracker, root, CO = _ops(tmp)
    page = "https://wiki.example.org/pages/viewpage.action?pageId=42"
    try:
        with T.env(**T.jira_env(tmp)):
            one = CO.call(str(root), "tasks", "link_page", {"issue_key": "PRJA-2", "url": page, "title": "US-1"})
            two = CO.call(str(root), "tasks", "link_page", {"issue_key": "PRJA-2", "url": page, "title": "US-1"})
            other = CO.call(str(root), "tasks", "link_page", {"issue_key": "PRJA-2", "url": page + "1"})
            bad = CO.call(str(root), "tasks", "link_page", {"issue_key": "PRJA-2", "url": "javascript:alert(1)"})
            gone = CO.call(str(root), "tasks", "link_page", {"issue_key": "PRJA-99", "url": page})
        assert one["ok"] and one["result"]["created"] is True, one
        assert two["ok"] and two["result"] == {"id": one["result"]["id"], "created": False}, two
        assert other["ok"] and other["result"]["created"] is True, other
        assert [r["object"]["url"] for r in tracker.links["PRJA-2"]] == [page, page + "1"], tracker.links
        assert tracker.links["PRJA-2"][0]["object"]["title"] == "US-1"
        assert tracker.links["PRJA-2"][0]["globalId"].startswith("aurora-page:")
        assert not bad["ok"] and bad["kind"] == "bad_request", bad
        assert not gone["ok"] and gone["kind"] == "not_found", gone
    finally:
        tracker.close()


@test
def test_jira_connector_refusals_name_the_kind_and_keep_the_connector_text(tmp: Path):
    """Отказ — вид и причина словами ядра (`error`), текст коннектора — рядом (`detail`): 401 — `auth`; нет адреса
    сервера — `config`; нет ключа — `auth` с подсказкой, где его взять. Сообщения синка о тех же бедах прежние."""
    tracker, root, CO = _ops(tmp)
    try:
        tracker.fail.append(("GET", "/project/PRJA", 401))
        with T.env(**T.jira_env(tmp)):
            res = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"})
        assert not res["ok"] and res["kind"] == "auth" and "HTTP 401" in res["detail"], res
        assert "«jira-dc»" in res["error"] and ".env.aurora.local" in res["error"], res
    finally:
        tracker.close()
    (tmp / "bare").mkdir()
    bare = make_project(tmp / "bare")
    with T.env(**T.jira_env(tmp)):
        res = CO.call(str(bare), "tasks", "issue_types", {"project_key": "PRJA"})
    assert not res["ok"] and res["kind"] == "config" and "base_url" in res["detail"], res
    keyless = {**T.jira_env(tmp), "JIRA_PAT": ""}
    with T.env(**keyless):
        res = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJA"})
    assert not res["ok"] and res["kind"] == "auth" and "JIRA_PAT" in res["detail"], res
    JE = T._mod("jira_export")
    try:
        JE.connect(str(bare))
        raise AssertionError("вход без адреса прошёл")
    except JE.OpFailure as e:
        assert e.kind == "config" and str(e) == "jira_export: нет atlassian.jira.base_url в aurora.config.yaml"
    assert os.environ.get("JIRA_PAT") != T.TOKEN, "ключ подставной Jira остался в окружении процесса проверок"
