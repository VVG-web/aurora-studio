#!/usr/bin/env python3
"""Fetch FULL status history (all transitions) for each issue — to count 'готово'-status entries."""
import os, sys, json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from paths import DATA_DIR

BASE = paths.jira()["base_url"] + "/rest/api/2"
TOK = os.environ.get("JIRA_PERSONAL_TOKEN")
if not TOK:
    raise RuntimeError("JIRA_PERSONAL_TOKEN environment variable is not set")

OUT_PATH = os.path.join(DATA_DIR, "full_status.json")
ISSUES_PATH = os.path.join(DATA_DIR, "issues.json")
issues = json.load(open(ISSUES_PATH, encoding="utf-8"))

# В части историй исполнителя пишут не в Assignee, а в кастомное поле
# «Ответственный». Id поля ищем по имени, чтобы не
# зашивать customfield_XXXXX: на другом стенде он будет другим.
RESPONSIBLE_FIELD_NAME = "Ответственный"

def resolve_field_id(name):
    req = urllib.request.Request(f"{BASE}/field", headers={"Authorization": f"Bearer {TOK}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        for fld in json.loads(r.read().decode()):
            if (fld.get("name") or "").strip().casefold() == name.casefold():
                return fld["id"]
    return None

RESPONSIBLE_ID = resolve_field_id(RESPONSIBLE_FIELD_NAME)
print(f"поле «{RESPONSIBLE_FIELD_NAME}»: {RESPONSIBLE_ID or 'НЕ НАЙДЕНО — выгрузка будет без него'}")

FIELDS = "status,updated,assignee,resolution,issuetype"
if RESPONSIBLE_ID:
    FIELDS += "," + RESPONSIBLE_ID


def user_name(value):
    """Имя пользователя из значения поля (объект, строка или список)."""
    if isinstance(value, dict):
        return value.get("displayName") or value.get("name")
    if isinstance(value, list):
        return next((user_name(v) for v in value if user_name(v)), None)
    return value or None


def get_changelog(key):
    url = f"{BASE}/issue/{key}?expand=changelog&fields={FIELDS}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOK}"})
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1)
                continue
            return {"key": key, "error": str(e)}
        except Exception as e:
            return {"key": key, "error": str(e)}
    return {"key": key, "error": "timed out"}

# Можно обновить только отдельные задачи, не перевыгружая весь проект:
#   python3 fetch_full.py PRJ-1300 PRJ-422
# Остальные записи в full_status.json при этом сохраняются как есть.
only = [a.strip() for a in sys.argv[1:] if a.strip()]
keys_needed = only or [it["key"] for it in issues]
if only:
    print(f"обновляем только: {', '.join(only)}")
results = [None]*len(keys_needed)
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(get_changelog, k): i for i, k in enumerate(keys_needed)}
    done = 0
    for fut in as_completed(futs):
        results[futs[fut]] = fut.result()
        done += 1
        if done % 100 == 0:
            print(f"processed {done}/{len(keys_needed)}")

out = {}
if only:
    # точечное обновление — доливаем в уже выгруженное
    try:
        out = json.load(open(OUT_PATH, encoding="utf-8"))
    except FileNotFoundError:
        pass
for res in results:
    if res is None or "error" in res:
        continue
    key = res["key"]
    f = res["fields"]
    # full status+assignee history
    status_history = []
    assignee_history = []
    responsible_history = []
    for h in res.get("changelog", {}).get("histories", []):
        author = (h.get("author") or {}).get("displayName", "")
        at = h.get("created", "")[:19]
        for item in h.get("items", []):
            fld = item.get("field")
            entry = {"at": at, "author": author, "from": (item.get("fromString") or "").strip(), "to": (item.get("toString") or "").strip()}
            if fld == "status":
                status_history.append(entry)
            elif fld == "assignee":
                assignee_history.append(entry)
            # у кастомных полей в changelog приходит имя поля, а не его id
            elif fld == RESPONSIBLE_FIELD_NAME or item.get("fieldId") == RESPONSIBLE_ID:
                responsible_history.append(entry)
    out[key] = {
        "key": key,
        "current_status": (f.get("status") or {}).get("name"),
        "assignee_now": (f.get("assignee") or {}).get("displayName"),
        "email": (f.get("assignee") or {}).get("emailAddress"),
        "issuetype": (f.get("issuetype") or {}).get("name"),
        "updated": f.get("updated", "")[:19],
        "status_history": status_history,
        "assignee_history": assignee_history,
        "responsible_now": user_name(f.get(RESPONSIBLE_ID)) if RESPONSIBLE_ID else None,
        "responsible_history": responsible_history,
    }

with open(OUT_PATH, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=0)

# summary: counts of transitions into the three ready statuses
import collections
READY = {"Аналитика - готово": 0, "Разработка - готово": 0, "Тестирование - готово": 0}
for v in out.values():
    for s in v["status_history"]:
        if s["to"] in READY:
            READY[s["to"]] += 1
print("\nTransitions into ready statuses (whole pool, all dates):", dict(READY))
print("Total parsed:", len(out))
print("Errors:", sum(1 for x in results if x and "error" in x))