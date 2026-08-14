#!/usr/bin/env python3
"""fetch_issues.py — список задач проекта из Jira → issues.json.

Первый шаг цепочки. В пакете, из которого приехал отчёт, его не было вовсе:
`fetch_full.py` открывал `issues.json` и падал, потому что создавать этот файл было
некому — его приносили руками из прошлой выгрузки. На чистом проекте цепочка
не запускалась ни разу.

JQL берётся из `atlassian.jira.default_jql`, а если его нет — собирается по ключу
проекта. Выгрузка не фильтруется по году: год применяется позже, при счёте, и
переключить отчётный год без повторного похода в Jira должно быть можно.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from jira_api import FIELDS, search_all

OUT = paths.data("issues.json")


def jql() -> str:
    from paths import scalar, section, _text
    j = section(section(_text(), "atlassian"), "jira", indent=2)
    q = scalar(j, "default_jql", "")
    if q:
        return q
    key = paths.jira()["project_key"]
    if not key:
        raise SystemExit("ops:report: в конфиге нет ни default_jql, ни project_key")
    return f"project = {key} ORDER BY key ASC"


query = jql()
print(f"Выгружаю задачи: {query}")
issues = search_all(query, FIELDS,
                    progress=lambda n, total: print(f"  {n}/{total}"))

by_type = {}
for i in issues:
    name = (i.get("fields", {}).get("issuetype") or {}).get("name", "—")
    by_type[name] = by_type.get(name, 0) + 1

os.makedirs(paths.DATA_DIR, exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(issues, f, ensure_ascii=False, indent=1)

print(f"Записано задач: {len(issues)} → {os.path.relpath(OUT, paths.PROJECT_ROOT)}")
print("по типам:", by_type)
