#!/usr/bin/env python3
"""Дотянуть подзадачи уже выгруженных задач и дописать их в issues.json.

Запуск (нужен доступ к Jira проекта и токен):
    JIRA_PERSONAL_TOKEN=... python3 fetch_subtasks.py
"""
import os, json, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from jira_api import search_all

ISSUES_PATH = paths.data("issues.json")
CHUNK = 100  # ключей родителей в одном JQL


issues = json.load(open(ISSUES_PATH, encoding="utf-8"))
have = {i["key"] for i in issues}
# у подзадач родителей не спрашиваем — они сами не бывают родителями
parents = sorted(i["key"] for i in issues if not i["fields"].get("parent"))
print(f"в issues.json: {len(issues)} задач, из них родителей-кандидатов: {len(parents)}")

found = []
for n in range(0, len(parents), CHUNK):
    chunk = parents[n:n + CHUNK]
    jql = "parent in (%s) ORDER BY key ASC" % ", ".join(chunk)
    batch = search_all(jql)
    found.extend(batch)
    print(f"  родителей {n + len(chunk)}/{len(parents)} → подзадач найдено всего {len(found)}")

new = [i for i in found if i["key"] not in have]
print(f"\nподзадач в Jira: {len(found)}, из них новых: {len(new)}")
if new:
    by_type = {}
    for i in new:
        by_type[i["fields"]["issuetype"]["name"]] = by_type.get(i["fields"]["issuetype"]["name"], 0) + 1
    print("по типам:", by_type)
    issues.extend(new)
    with open(ISSUES_PATH, "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=1)
    print(f"дописано в issues.json, теперь задач: {len(issues)}")
    print("\nдальше пересобрать метрики и дашборд:")
    print("  python3 make_analyst_metrics.py")
    print("  python3 update_analyst_metrics.py")
    print("  python3 verify_weekly_by_person.py")
    print("  python3 make_extended.py")
else:
    print("новых подзадач нет — issues.json уже полный")