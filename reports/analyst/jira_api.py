#!/usr/bin/env python3
"""jira_api.py — обращения к Jira, общие для шагов выгрузки.

Своя копия `api()` и постраничного поиска лежала в каждом fetch-скрипте: одинаковые
повторы, одинаковые таймауты, и расходились они молча. Здесь одна.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

BASE_URL = paths.jira()["base_url"] + "/rest/api/2"

# Поля, на которых стоит весь дальнейший счёт: тип задачи, статус, исполнитель, даты.
FIELDS = ("key,issuetype,status,resolution,assignee,reporter,created,updated,"
          "labels,summary,parent")


def token() -> str:
    tok = os.environ.get("JIRA_PERSONAL_TOKEN")
    if not tok:
        raise RuntimeError("нет JIRA_PERSONAL_TOKEN — положите его в .env.aurora.local")
    return tok


def api(path: str, params: dict | None = None, max_tries: int = 4):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    for i in range(max_tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == max_tries - 1:
                raise
            time.sleep(2 ** i)
    return None


def search_all(jql: str, fields: str = FIELDS, progress=None) -> list:
    """Все страницы выдачи по JQL."""
    out, start, total = [], 0, None
    while total is None or start < total:
        data = api("/search", {"jql": jql, "startAt": start,
                               "maxResults": 100, "fields": fields})
        if total is None:
            total = data.get("total", 0)
        issues = data.get("issues", [])
        if not issues:
            break
        out.extend(issues)
        start += len(issues)
        if progress:
            progress(start, total)
    return out
