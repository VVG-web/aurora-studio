#!/usr/bin/env python3
"""atlassian_ops.py — Jira и Confluence проекта для ботов и ассистентов («Аврора»).

Боту по расписанию и чужому ассистенту (через MCP Авроры) нужно то, чего синк не умеет:
спросить очередь задач одним запросом, найти страницу истории по её номеру, прочитать её
текстом, а в конце — положить отчёт вложением, оставить комментарий и сменить метки. Раньше
это делал отдельный скрипт на сервере с переменными окружения, локом и прямыми REST-запросами;
в Авроре ему не было места, и промпт бота ссылался на команды, которых нет.

Работает ключами проекта — теми же, что синк (`JIRA_PAT`, `CONFLUENCE_PAT` в
`.env.aurora.local` проекта); наружу ключи не выходят никогда.

Вход и выход — JSON, чтобы вызывающий (MCP Авроры, бот, тест) не разбирал текст:

    echo '{"op": "check"}' | python3 .aurora/scripts/atlassian_ops.py
    echo '{"op": "jira_search", "jql": "labels = for_ai_review", "limit": 50}' | python3 …
    echo '{"op": "jira_publish", "issue": "ABC-1", "filename": "ai_review_US-1.md",
           "content": "…", "comment": "…", "labels_add": ["done"], "labels_remove": ["todo"],
           "dry_run": true}' | python3 …
    echo '{"op": "confluence_find", "query": "US-3.6.21"}' | python3 …
    echo '{"op": "confluence_page", "page": "123456"}' | python3 …

Запись в Jira выключена, пока проект её не разрешил:

    mcp:
      jira_write: true      # aurora.config.yaml проекта

Пробный прогон (`dry_run`) показывает, что было бы сделано, и работает всегда.

Публикация атомарна на задачу: вложение первым; комментарий и метки — только если вложение
легло (или уже лежало с тем же именем — повтор не грузит второе). Статус задачи не меняется,
рецензируемая история не трогается — инструмент пишет только в названную задачу.

Панель: команды нет — это служебный исполнитель MCP Авроры (`aurora_mcp.py`).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sources_core import CONFIG, block, config_text, read_secret  # noqa: E402

OPS = ("check", "jira_search", "jira_issue", "jira_publish", "confluence_find",
       "confluence_page")
SEARCH_FIELDS = "summary,issuetype,status,parent,labels,attachment,created,project"
LIMIT_MAX = 200
TEXT_MAX = 120_000          # страница Confluence текстом: дальше — не контекст, а свалка


class OpError(Exception):
    """Отказ операции: уходит вызывающему текстом причины."""


def settings(root: str = "") -> dict:
    """Адреса и разрешение записи — из aurora.config.yaml проекта."""
    text = config_text(os.path.join(root, CONFIG)) if root else config_text()
    jira = block(text, "jira:", "auth:", "\nconfluence:")
    conf = block(text, "confluence:", "jira:")
    m = re.search(r"(?m)^mcp:\s*\n((?:[ \t]+.*\n?)*)", text)
    mcp = m.group(1) if m else ""
    w = re.search(r"(?m)^\s+jira_write:\s*(\S+)", mcp)
    from aurora_common import yaml_scalar
    return {"jira_url": yaml_scalar(jira, "base_url").rstrip("/"),
            "jira_key": yaml_scalar(jira, "project_key"),
            "confluence_url": yaml_scalar(conf, "base_url").rstrip("/"),
            "space": yaml_scalar(conf, "space"),
            "jira_write": bool(w and w.group(1).strip().strip("'\"").lower()
                               in ("true", "yes", "1", "on", "да"))}


def jira(root: str = ""):
    from jira_export import Api
    s = settings(root)
    if not s["jira_url"]:
        raise OpError("в aurora.config.yaml нет jira.base_url — Jira проекту не подключена")
    auth, _how = read_secret("JIRA", root)
    if not auth:
        raise OpError("нет ключа Jira: JIRA_PAT в .env.aurora.local проекта")
    return Api(s["jira_url"], auth)


def confluence(root: str = ""):
    from confluence_export import Api
    s = settings(root)
    if not s["confluence_url"]:
        raise OpError("в aurora.config.yaml нет confluence.base_url — Confluence проекту не "
                      "подключён")
    auth, _how = read_secret("CONFLUENCE", root)
    if not auth:
        raise OpError("нет ключа Confluence: CONFLUENCE_PAT в .env.aurora.local проекта")
    return Api(s["confluence_url"], auth)


def http_why(e: Exception) -> str:
    """Причина сбоя словами сервера: тело ответа Jira называет, что не так с запросом."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        try:
            data = json.loads(body)
            msgs = data.get("errorMessages") or []
            msgs += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
            if msgs:
                return f"HTTP {e.code}: " + "; ".join(map(str, msgs))[:300]
        except ValueError:
            pass
        return f"HTTP {e.code}: {body[:200]}".strip()
    return f"{type(e).__name__}: {e}"[:300]


# ------------------------------------------------------------------ операции

def op_check(req: dict, root: str = "") -> dict:
    """Доступны ли Jira и Confluence проекта и под кем — до того, как что-то писать."""
    s = settings(root)
    out = {"jira_write": s["jira_write"]}
    try:
        api = jira(root)
        info = api.get("/rest/api/2/serverInfo")
        me = api.get("/rest/api/2/myself")
        out["jira"] = {"ok": True, "version": info.get("version", ""),
                       "user": me.get("displayName") or me.get("name") or "",
                       "project_key": s["jira_key"]}
    except (OpError, urllib.error.URLError, OSError, ValueError) as e:
        out["jira"] = {"ok": False, "why": str(e) if isinstance(e, OpError) else http_why(e)}
    try:
        api = confluence(root)
        me = api.get("/rest/api/user/current")
        out["confluence"] = {"ok": True, "user": me.get("displayName") or me.get("username") or "",
                             "space": s["space"]}
    except (OpError, urllib.error.URLError, OSError, ValueError) as e:
        out["confluence"] = {"ok": False,
                             "why": str(e) if isinstance(e, OpError) else http_why(e)}
    out["ok"] = bool(out["jira"].get("ok") or out["confluence"].get("ok"))
    return out


def compact_issue(it: dict) -> dict:
    """Задача Jira без мусора: то, по чему бот решает, что делать с карточкой."""
    f = it.get("fields") or {}
    parent = f.get("parent") or {}
    pf = parent.get("fields") or {}
    return {"key": it.get("key", ""),
            "summary": f.get("summary", ""),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key", ""),
            "project": (f.get("project") or {}).get("key", ""),
            "labels": f.get("labels") or [],
            "created": f.get("created", ""),
            "parent": {"key": parent.get("key", ""), "summary": pf.get("summary", ""),
                       "type": (pf.get("issuetype") or {}).get("name", "")} if parent else {},
            "attachments": [a.get("filename", "") for a in f.get("attachment") or []]}


def op_jira_search(req: dict, root: str = "") -> dict:
    jql = str(req.get("jql") or "").strip()
    if not jql:
        raise OpError("не задан jql")
    limit = max(1, min(int(req.get("limit") or 50), LIMIT_MAX))
    fields = str(req.get("fields") or SEARCH_FIELDS)
    try:
        issues = jira(root).search(jql, fields, limit)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OpError("Jira не ответила: " + http_why(e)) from None
    return {"total": len(issues), "issues": [compact_issue(i) for i in issues]}


def op_jira_issue(req: dict, root: str = "") -> dict:
    key = str(req.get("issue") or "").strip()
    if not re.match(r"^[A-Z][A-Z0-9_]+-\d+$", key):
        raise OpError(f"не похоже на ключ задачи Jira: «{key}»")
    try:
        it = jira(root).get(f"/rest/api/2/issue/{key}?fields={SEARCH_FIELDS},description")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OpError("Jira не ответила: " + http_why(e)) from None
    out = compact_issue(it)
    out["description"] = ((it.get("fields") or {}).get("description") or "")[:20000]
    return out


def op_jira_publish(req: dict, root: str = "") -> dict:
    """Вложение → комментарий → метки. Следующий шаг — только если предыдущий прошёл."""
    key = str(req.get("issue") or "").strip()
    if not re.match(r"^[A-Z][A-Z0-9_]+-\d+$", key):
        raise OpError(f"не похоже на ключ задачи Jira: «{key}»")
    dry = bool(req.get("dry_run"))
    s = settings(root)
    if not dry and not s["jira_write"]:
        raise OpError("запись в Jira выключена для этого проекта: в aurora.config.yaml нужно "
                      "`mcp:` → `jira_write: true`. Пробный прогон (dry_run) работает и без неё")
    filename = os.path.basename(str(req.get("filename") or "").strip())
    content = req.get("content")
    path = str(req.get("path") or "").strip()
    if path and content is None:
        full = os.path.realpath(os.path.join(root or os.getcwd(), path))
        base = os.path.realpath(root or os.getcwd())
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            raise OpError(f"файла «{path}» в проекте нет")
        with open(full, "rb") as f:
            content = f.read()
        filename = filename or os.path.basename(full)
    if content is not None and not filename:
        raise OpError("у вложения нет имени: filename")
    comment = str(req.get("comment") or "")
    add = [str(x) for x in req.get("labels_add") or [] if str(x).strip()]
    remove = [str(x) for x in req.get("labels_remove") or [] if str(x).strip()]
    steps, api = [], None if dry else jira(root)

    def step(what: str, ok: bool, note: str = "") -> bool:
        steps.append({"step": what, "ok": ok, **({"note": note} if note else {})})
        return ok

    if content is not None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        if dry:
            # Пробный прогон тоже смотрит, что уже лежит: «положил бы» поверх готового
            # отчёта — неправда о том, что случится в боевом.
            try:
                have = jira(root).get(f"/rest/api/2/issue/{key}?fields=attachment")
                names = [a.get("filename") for a in
                         ((have.get("fields") or {}).get("attachment") or [])]
            except (OpError, urllib.error.URLError, OSError, ValueError):
                names = []
            step("вложение", True, f"«{filename}» уже лежит — повторно не грузил бы"
                 if filename in names else f"положил бы «{filename}» ({len(data)} байт)")
        else:
            try:
                have = jira(root).get(f"/rest/api/2/issue/{key}?fields=attachment")
                names = [a.get("filename") for a in
                         ((have.get("fields") or {}).get("attachment") or [])]
            except (urllib.error.URLError, OSError, ValueError) as e:
                step("вложение", False, "задача не прочитана: " + http_why(e))
                return {"ok": False, "issue": key, "dry_run": dry, "steps": steps}
            if filename in names:
                step("вложение", True, f"«{filename}» уже лежит — повторно не грузил")
            else:
                try:
                    api.send("POST", f"/rest/api/2/issue/{key}/attachments",
                             files={"file": (filename, data)}, timeout=120)
                    step("вложение", True, f"«{filename}» ({len(data)} байт)")
                except (urllib.error.URLError, OSError, ValueError) as e:
                    step("вложение", False, http_why(e))
                    # Метки не трогаем: карточка не сделана, пока отчёт не лёг.
                    return {"ok": False, "issue": key, "dry_run": dry, "steps": steps}
    if comment:
        if dry:
            step("комментарий", True, f"оставил бы комментарий ({len(comment)} знаков)")
        else:
            try:
                # Server: тело комментария — строка (wiki-разметка); ADF — только в Cloud.
                api.send("POST", f"/rest/api/2/issue/{key}/comment", {"body": comment})
                step("комментарий", True)
            except (urllib.error.URLError, OSError, ValueError) as e:
                step("комментарий", False, http_why(e))
                return {"ok": False, "issue": key, "dry_run": dry, "steps": steps}
    if add or remove:
        ops = [{"remove": x} for x in remove] + [{"add": x} for x in add]
        if dry:
            step("метки", True, "сменил бы: " + ", ".join(
                [f"−{x}" for x in remove] + [f"+{x}" for x in add]))
        else:
            try:
                # `/editMetadata` на Server бывает 404; метки — через PUT update.labels.
                api.send("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": ops}})
                step("метки", True, ", ".join([f"−{x}" for x in remove] + [f"+{x}" for x in add]))
            except (urllib.error.URLError, OSError, ValueError) as e:
                step("метки", False, http_why(e))
                return {"ok": False, "issue": key, "dry_run": dry, "steps": steps}
    if not steps:
        raise OpError("нечего публиковать: нет ни вложения, ни комментария, ни меток")
    return {"ok": True, "issue": key, "dry_run": dry, "steps": steps}


def page_link(base: str, page: dict) -> str:
    webui = ((page.get("_links") or {}).get("webui") or "")
    return base + webui if webui else f"{base}/pages/viewpage.action?pageId={page.get('id', '')}"


def op_confluence_find(req: dict, root: str = "") -> dict:
    """Страницы по номеру или словам в заголовке: номер истории ищется началом заголовка."""
    query = str(req.get("query") or "").strip()
    if not query:
        raise OpError("не задан query")
    s = settings(root)
    space = str(req.get("space") or s["space"] or "").strip()
    limit = max(1, min(int(req.get("limit") or 10), 50))
    q = query.replace('"', '\\"')
    cql = f'type = page AND title ~ "{q}"' + (f' AND space = "{space}"' if space else "")
    api = confluence(root)
    try:
        data = api.get("/rest/api/content/search?cql=" + urllib.parse.quote(cql)
                       + f"&limit={limit}&expand=version,space")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OpError("Confluence не ответил: " + http_why(e)) from None
    rows = []
    for p in data.get("results") or []:
        rows.append({"id": str(p.get("id", "")), "title": p.get("title", ""),
                     "space": (p.get("space") or {}).get("key", ""),
                     "version": (p.get("version") or {}).get("number", 0),
                     "url": page_link(s["confluence_url"], p),
                     # номер в начале заголовка — признак «та самая страница»
                     "starts_with_query": p.get("title", "").strip().startswith(query)})
    rows.sort(key=lambda r: not r["starts_with_query"])
    return {"query": query, "space": space, "pages": rows}


def op_confluence_page(req: dict, root: str = "") -> dict:
    """Страница текстом и её ссылки на другие страницы — без сырого html."""
    ref = str(req.get("page") or "").strip()
    if not ref:
        raise OpError("не задана page: номер или адрес страницы")
    from confluence_export import resolve_ref
    from review_run import html_to_text
    s = settings(root)
    api = confluence(root)
    pid, _title, err = resolve_ref(api, ref, s["space"])
    if err:
        raise OpError(err)
    try:
        page = api.get(f"/rest/api/content/{pid}?expand=body.view,version,space")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OpError("Confluence не ответил: " + http_why(e)) from None
    view = ((page.get("body") or {}).get("view") or {}).get("value", "")
    links = sorted(set(re.findall(r"pageId=(\d+)", view)) - {pid})
    text = html_to_text(view)
    return {"id": pid, "title": page.get("title", ""),
            "version": (page.get("version") or {}).get("number", 0),
            "space": (page.get("space") or {}).get("key", ""),
            "url": page_link(s["confluence_url"], page),
            "linked_page_ids": links[:100],
            "text": text[:TEXT_MAX], "truncated": len(text) > TEXT_MAX}


def run(req: dict, root: str = "") -> dict:
    op = str(req.get("op") or "")
    fn = {"check": op_check, "jira_search": op_jira_search, "jira_issue": op_jira_issue,
          "jira_publish": op_jira_publish, "confluence_find": op_confluence_find,
          "confluence_page": op_confluence_page}.get(op)
    if not fn:
        raise OpError(f"операции «{op}» нет; есть: {', '.join(OPS)}")
    return fn(req, root)


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"вход — не JSON: {e}"}, ensure_ascii=False))
        return 2
    try:
        out = run(req if isinstance(req, dict) else {})
    except OpError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
