#!/usr/bin/env python3
"""confluence_ops.py — операции вики (роль `wiki`) коннектора `confluence-dc`: Confluence Server / Data Center, REST.

Ядро (`connector_ops.py`) запускает скрипт процессом: на stdin `{"op", "params", "project"}`, в stdout — одна строка
`{"ok": true, "result": …}` либо `{"ok": false, "kind": …, "error": …}` (`sources_core.serve_ops`). Вход и адрес — те
же, что у синка (`confluence_export.connect`): `atlassian.confluence.base_url` конфига и ключ машины `CONFLUENCE_PAT`
(или `CONFLUENCE_USER` + `CONFLUENCE_PASSWORD`).

Набор — ровно то, что публикация (`ship:publish`) делает с Confluence: найти страницу по заголовку, прочитать её
(версия, предки, тело, пространство), дети постранично с отчётом о полноте списка, создать, обновить с проверкой
версии (чужая правка — отказ `conflict`), перенести под другого родителя, пространство страницы.

  echo '{"op": "find_page", "params": {"space": "DOC", "title": "Итог"}}' | python3 .aurora/scripts/confluence_ops.py

Страница в ответах — `{id, title, space, version, by, when, ancestors: [{id, title}], body, url}`; `body` —
storage-разметка (нет, если её не просили). Контракт роли `wiki` — `docs/connectors.md`.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "..", "scripts")):
    if os.path.isfile(os.path.join(_p, "sources_core.py")) and os.path.abspath(_p) not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import aurora_common  # noqa: E402,F401 — защита версии Python
from confluence_export import children_verdict, connect  # noqa: E402
from sources_core import OpFailure, serve_ops  # noqa: E402

FULL = "version,space,ancestors,body.storage"
LIGHT = "version,space,ancestors"
PAGE_ID = re.compile(r"^\d{1,20}$")
SPACE_KEY = re.compile(r"^[~\w.@-]{1,255}$")


def page_url(base: str, pid) -> str:
    return f"{base}/pages/viewpage.action?pageId={pid}"


def view(page: dict, base: str) -> dict:
    """Страница ответа Confluence → страница контракта. Предков и тела нет в ответе — нет и в выдаче."""
    ver = page.get("version") or {}
    out = {"id": str(page.get("id") or ""), "title": str(page.get("title") or ""),
           "space": str((page.get("space") or {}).get("key") or ""), "version": int(ver.get("number") or 0),
           "by": str((ver.get("by") or {}).get("displayName") or ""), "when": str(ver.get("when") or "")[:10],
           "url": page_url(base, page.get("id"))}
    if page.get("ancestors") is not None:
        out["ancestors"] = [{"id": str(a.get("id") or ""), "title": str(a.get("title") or "")}
                            for a in page.get("ancestors") or [] if isinstance(a, dict)]
    storage = (page.get("body") or {}).get("storage")
    if isinstance(storage, dict):
        out["body"] = str(storage.get("value") or "")
    return out


def _pid(value, name: str = "page_id") -> str:
    value = str(value or "").strip()
    if not PAGE_ID.match(value):
        raise OpFailure("bad_request", f"{name}: нужен номер страницы, а не «{value[:40]}»")
    return value


def _space(value) -> str:
    value = str(value or "").strip()
    if not SPACE_KEY.match(value):
        raise OpFailure("bad_request", f"не похоже на ключ пространства Confluence: «{value[:40]}»")
    return value


def _title(value) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise OpFailure("bad_request", "не задан заголовок страницы")
    return title


def _version(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise OpFailure("bad_request", "version — номер версии страницы, которую видел вызывающий") from None
    if n < 1:
        raise OpFailure("bad_request", "version — номер версии страницы, которую видел вызывающий")
    return n


def _read(api, pid: str, body: bool = True) -> dict:
    return api.get(f"/rest/api/content/{urllib.parse.quote(pid, safe='')}?expand={FULL if body else LIGHT}")


def op_find_page(params: dict, root: str) -> dict:
    space, title = _space(params.get("space")), _title(params.get("title"))
    api, cfg, _how = connect(root)
    hit = api.by_title(space, title, FULL if params.get("body", True) is not False else LIGHT)
    return {"page": view(hit, cfg["base_url"]) if hit else None}


def op_get_page(params: dict, root: str) -> dict:
    pid = _pid(params.get("page_id"))
    api, cfg, _how = connect(root)
    return view(_read(api, pid, params.get("body", True) is not False), cfg["base_url"])


def op_children(params: dict, root: str) -> dict:
    pid = _pid(params.get("page_id"))
    api, cfg, _how = connect(root)
    kids, rep = api.children_report(pid)
    gap = children_verdict(rep)
    return {"pages": [view(k, cfg["base_url"]) for k in kids if isinstance(k, dict)], "complete": not gap,
            "gap": gap, "got": rep.get("got", 0)}


def op_create_page(params: dict, root: str) -> dict:
    space, title = _space(params.get("space")), _title(params.get("title"))
    parent = _pid(params.get("parent_id"), "parent_id") if params.get("parent_id") else ""
    payload = {"type": "page", "title": title, "space": {"key": space},
               "body": {"storage": {"value": str(params.get("body") or ""), "representation": "storage"}}}
    if parent:
        payload["ancestors"] = [{"id": parent}]
    api, cfg, _how = connect(root)
    # Одна попытка: повтор POST после обрыва создал бы вторую страницу; повтор находит первую по заголовку.
    return view(api.send("POST", "/rest/api/content", payload, timeout=60, tries=1) or {}, cfg["base_url"])


def _put(api, cfg: dict, pid: str, title: str, body: str, version: int, space: str = "", parent: str = "",
         message: str = "") -> dict:
    payload = {"id": pid, "type": "page", "title": title,
               "body": {"storage": {"value": body, "representation": "storage"}},
               "version": {"number": version + 1, **({"message": message} if message else {})}}
    if space:
        payload["space"] = {"key": space}
    if parent:
        payload["ancestors"] = [{"id": parent}]
    got = api.send("PUT", f"/rest/api/content/{urllib.parse.quote(pid, safe='')}", payload, timeout=60, tries=1)
    return view(got or {}, cfg["base_url"])


def op_update_page(params: dict, root: str) -> dict:
    """Новые заголовок и тело поверх версии `version`, которую видел вызывающий; `parent_id` — перенос вместе с правкой.
    Страницу правили после этой версии — сервер отказывает (409), ответ — `conflict`."""
    pid, title, version = _pid(params.get("page_id")), _title(params.get("title")), _version(params.get("version"))
    parent = _pid(params.get("parent_id"), "parent_id") if params.get("parent_id") else ""
    space = _space(params.get("space")) if params.get("space") else ""
    api, cfg, _how = connect(root)
    return _put(api, cfg, pid, title, str(params.get("body") or ""), version, space, parent,
                _one_line(params.get("message")))


def op_move_page(params: dict, root: str) -> dict:
    """Перенос под `parent_id` без правки текста: заголовок и тело — как на сервере. `version` (необязательно) —
    версия, которую видел вызывающий: на сервере другая — отказ `conflict` без записи."""
    pid, parent = _pid(params.get("page_id")), _pid(params.get("parent_id"), "parent_id")
    api, cfg, _how = connect(root)
    page = view(_read(api, pid), cfg["base_url"])
    if params.get("version") is not None and _version(params.get("version")) != page["version"]:
        raise OpFailure("conflict", f"страница {pid} на сервере уже версии {page['version']}")
    return _put(api, cfg, pid, page["title"], page.get("body", ""), page["version"], page["space"], parent,
                _one_line(params.get("message")))


def op_page_space(params: dict, root: str) -> dict:
    pid = _pid(params.get("page_id"))
    api, _cfg, _how = connect(root)
    page = api.get(f"/rest/api/content/{urllib.parse.quote(pid, safe='')}?expand=space")
    return {"id": pid, "title": str(page.get("title") or ""), "space": str((page.get("space") or {}).get("key") or "")}


def _one_line(value) -> str:
    return " ".join(str(value or "").split())[:255]


OPS = {"find_page": op_find_page, "get_page": op_get_page, "children": op_children, "create_page": op_create_page,
       "update_page": op_update_page, "move_page": op_move_page, "page_space": op_page_space}


if __name__ == "__main__":
    sys.exit(serve_ops(OPS))
