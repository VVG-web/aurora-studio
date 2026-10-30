#!/usr/bin/env python3
"""jira_ops.py — операции трекера задач (роль `tasks`) коннектора `jira-dc`: Jira Server / Data Center, REST API v2.

Ядро (`connector_ops.py`) запускает скрипт процессом: на stdin `{"op", "params", "project"}`, в stdout — одна строка
`{"ok": true, "result": …}` либо `{"ok": false, "kind": …, "error": …}` (`sources_core.serve_ops`). Вход и адрес — те
же, что у синка (`jira_export.connect`): `atlassian.jira.base_url` конфига и ключ машины `JIRA_PAT` (или `JIRA_USER` +
`JIRA_PASSWORD`) в `.env.aurora.local`.

  echo '{"op": "issue_types", "params": {"project_key": "PRJA"}}' | python3 .aurora/scripts/jira_ops.py

Операции и их ответы — контракт роли `tasks` в `docs/connectors.md`:

  issue_types(project_key)                → {types: [{id, name, description}]}         без подзадач
  epics(project_key, limit?)              → {epics: [{key, title, status, done}]}      не закрытые первыми
  find_user(email)                        → {name, key, display, email}                 name — логин для assignee
  create_issue(project_key, type, summary, description?, assignee?, labels?, components?, epic?) → {key, id, url, epic_via}
  link_page(issue_key, url, title?)       → {id, created}                               повтор не плодит вторую ссылку
  get_issue(key)                          → {key, summary, status, status_category, type, url}
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import urllib.parse

# Скрипт лежит в копии движка проекта рядом с `jira_export.py`; в ките — в папке коннектора, а общий код в `scripts/`.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "..", "scripts")):
    if os.path.isfile(os.path.join(_p, "sources_core.py")) and os.path.abspath(_p) not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import aurora_common  # noqa: E402,F401 — защита версии Python
from jira_export import connect, epic_field_id  # noqa: E402
from sources_core import OpFailure, serve_ops  # noqa: E402

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
EPICS_LIMIT = 200           # эпиков в выпадающем списке; дальше — шум, а не выбор
EPICS_CEILING = 1000
EPIC_NAMES = ("epic", "эпик")


def _project_key(params: dict, name: str = "project_key") -> str:
    value = str(params.get(name) or "").strip()
    if not PROJECT_KEY.match(value):
        raise OpFailure("bad_request", f"не похоже на ключ проекта Jira: «{value[:40]}»")
    return value


def _issue_key(value) -> str:
    value = str(value or "").strip()
    if not ISSUE_KEY.match(value):
        raise OpFailure("bad_request", f"не похоже на ключ задачи Jira: «{value[:40]}»")
    return value


def _one_line(value, limit: int = 255) -> str:
    return " ".join(str(value or "").split())[:limit]


def _words(value) -> list:
    """Метки и компоненты: список или строка через запятую → список непустых строк."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def op_issue_types(params: dict, root: str) -> dict:
    api, _cfg, _how = connect(root)
    key = _project_key(params)
    data = api.get(f"/rest/api/2/project/{urllib.parse.quote(key)}")
    return {"types": [{"id": str(t.get("id") or ""), "name": str(t.get("name") or ""),
                       "description": str(t.get("description") or "")}
                      for t in data.get("issueTypes") or [] if isinstance(t, dict) and not t.get("subtask")]}


def _epic_type(types: list) -> str:
    """Тип «эпик» проекта для JQL: по уровню иерархии (Cloud), иначе по имени; не нашёлся — имя `Epic`."""
    hit = next((t for t in types if isinstance(t, dict) and t.get("hierarchyLevel") == 1), None) \
        or next((t for t in types if isinstance(t, dict) and str(t.get("name") or "").casefold() in EPIC_NAMES), None)
    return str(hit["id"]) if hit and hit.get("id") else "Epic"


def op_epics(params: dict, root: str) -> dict:
    api, _cfg, _how = connect(root)
    key = _project_key(params)
    try:
        limit = max(1, min(int(params.get("limit") or EPICS_LIMIT), EPICS_CEILING))
    except (TypeError, ValueError):
        raise OpFailure("bad_request", "limit — целое число") from None
    project = api.get(f"/rest/api/2/project/{urllib.parse.quote(key)}")
    jql = f'project = "{key}" AND issuetype = {_epic_type(project.get("issueTypes") or [])} ORDER BY updated DESC'
    rows = []
    for it in api.search(jql, "summary,status", limit):
        f = it.get("fields") or {}
        status = f.get("status") or {}
        rows.append({"key": str(it.get("key") or ""), "title": str(f.get("summary") or ""),
                     "status": str(status.get("name") or ""),
                     "done": (status.get("statusCategory") or {}).get("key") == "done"})
    rows.sort(key=lambda r: r["done"])          # устойчивая сортировка: внутри групп остаётся «свежие первыми»
    return {"epics": rows}


def op_find_user(params: dict, root: str) -> dict:
    email = str(params.get("email") or "").strip()
    if "@" not in email or any(c.isspace() for c in email):
        raise OpFailure("bad_request", f"не похоже на почту: «{email[:60]}»")
    api, _cfg, _how = connect(root)
    users = api.get(f"/rest/api/2/user/search?username={urllib.parse.quote(email)}&maxResults=50") or []
    users = [u for u in users if isinstance(u, dict)]
    hit = next((u for u in users if str(u.get("emailAddress") or "").casefold() == email.casefold()), None)
    if hit is None and len(users) == 1 and not users[0].get("emailAddress"):
        hit = users[0]                          # почта скрыта настройкой приватности, но человек один
    if hit is None:
        raise OpFailure("not_found", f"в Jira нет пользователя с почтой {email}"
                        + (f" (похожих: {len(users)})" if users else ""))
    return {"name": str(hit.get("name") or hit.get("key") or hit.get("accountId") or ""),
            "key": str(hit.get("key") or ""), "display": str(hit.get("displayName") or ""),
            "email": str(hit.get("emailAddress") or "")}


def op_create_issue(params: dict, root: str) -> dict:
    key = _project_key(params)
    kind = _one_line(params.get("type"), 100)
    summary = _one_line(params.get("summary"))
    if not kind:
        raise OpFailure("bad_request", "не задан тип задачи")
    if not summary:
        raise OpFailure("bad_request", "не задан заголовок задачи")
    epic = _issue_key(params["epic"]) if params.get("epic") else ""
    api, cfg, _how = connect(root)
    fields = {"project": {"key": key}, "summary": summary,
              "issuetype": {"id": kind} if kind.isdigit() else {"name": kind}}
    if params.get("description"):
        fields["description"] = str(params["description"])
    if params.get("assignee"):
        fields["assignee"] = {"name": _one_line(params["assignee"], 255)}
    labels = _words(params.get("labels"))
    if labels:
        fields["labels"] = labels
    components = _words(params.get("components"))
    if components:
        fields["components"] = [{"name": c} for c in components]
    via = ""
    if epic:
        # Epic Link — custom field с плавающим номером: id ищется по схеме поля. Нет такого поля (Jira Cloud,
        # новые иерархии) — эпик ставится родителем.
        via = epic_field_id(api.get("/rest/api/2/field")) or "parent"
        fields[via] = {"key": epic} if via == "parent" else epic
    # Одна попытка: повтор POST после обрыва завёл бы вторую задачу.
    got = api.send("POST", "/rest/api/2/issue", {"fields": fields}, timeout=60, tries=1) or {}
    new = str(got.get("key") or "")
    if not new:
        raise OpFailure("failed", "Jira не вернула ключ созданной задачи")
    return {"key": new, "id": str(got.get("id") or ""), "url": f"{cfg['base_url']}/browse/{new}", "epic_via": via}


def op_link_page(params: dict, root: str) -> dict:
    key = _issue_key(params.get("issue_key"))
    url = str(params.get("url") or "").strip()
    if not re.match(r"^https?://\S+$", url):
        raise OpFailure("bad_request", f"адрес страницы — http(s)://…, а не «{url[:60]}»")
    title = _one_line(params.get("title") or url)
    api, _cfg, _how = connect(root)
    path = f"/rest/api/2/issue/{urllib.parse.quote(key)}/remotelink"
    for link in api.get(path) or []:
        if isinstance(link, dict) and (link.get("object") or {}).get("url") == url:
            return {"id": str(link.get("id") or ""), "created": False}
    # `globalId` по адресу: Jira сама обновит ссылку с тем же идентификатором вместо второй.
    gid = "aurora-page:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    got = api.send("POST", path, {"globalId": gid, "object": {"url": url, "title": title}}, timeout=60, tries=1) or {}
    return {"id": str(got.get("id") or ""), "created": True}


def op_get_issue(params: dict, root: str) -> dict:
    key = _issue_key(params.get("key"))
    api, cfg, _how = connect(root)
    data = api.get(f"/rest/api/2/issue/{urllib.parse.quote(key)}?fields=summary,status,issuetype")
    f = data.get("fields") or {}
    status = f.get("status") or {}
    return {"key": str(data.get("key") or key), "summary": str(f.get("summary") or ""),
            "status": str(status.get("name") or ""),
            "status_category": str((status.get("statusCategory") or {}).get("key") or ""),
            "type": str((f.get("issuetype") or {}).get("name") or ""), "url": f"{cfg['base_url']}/browse/{key}"}


OPS = {"issue_types": op_issue_types, "epics": op_epics, "find_user": op_find_user,
       "create_issue": op_create_issue, "link_page": op_link_page, "get_issue": op_get_issue}


if __name__ == "__main__":
    sys.exit(serve_ops(OPS))
