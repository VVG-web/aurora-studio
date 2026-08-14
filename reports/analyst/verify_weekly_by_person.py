#!/usr/bin/env python3
"""
Rebuild weekly_by_person from full_status.json to match the authoritative weekly totals.

Logic mirrors make_analyst_metrics.py and update_analyst_metrics.py:
- For История and Инцидент (and any non-BA-SA type): event = transition to "Аналитика - готово"
- For BA-SA Task: event = FIRST transition to "Закрыто"
- Year must equal the reporting year from aurora.config.yaml (ISO year)
- Bucket: История→stories, Инцидент→others, BA-SA Task→ba_sa
"""
import json
import csv
from collections import defaultdict
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from paths import DATA_DIR, ROSTER_PATH

YEAR = paths.YEAR
FULL_STATUS_PATH = os.path.join(DATA_DIR, "full_status.json")
ISSUES_PATH = os.path.join(DATA_DIR, "issues.json")
METRICS_PATH = paths.out("analyst_metrics.json")

# Load source data
full = json.load(open(FULL_STATUS_PATH, encoding="utf-8"))
issues_list = json.load(open(ISSUES_PATH, encoding="utf-8"))
metrics = json.load(open(METRICS_PATH, encoding="utf-8"))

# Build issue_types mapping: key -> issuetype.name
issue_types = {}
for iss in issues_list:
    key = iss["key"]
    issue_types[key] = iss["fields"]["issuetype"]["name"]

# Load roster for role mapping
roster = paths.roster()

def iso_week(ts):
    """Return ISO week as string 'WW' (00-padded)."""
    d = None
    for c in [ts, ts[:10]]:
        try:
            d = datetime.date.fromisoformat(c[:10])
            break
        except Exception:
            pass
    if d is None:
        return None
    return f"{d.isocalendar()[1]:02d}"

def iso_year(ts):
    """Return ISO year."""
    d = None
    for c in [ts, ts[:10]]:
        try:
            d = datetime.date.fromisoformat(c[:10])
            break
        except Exception:
            pass
    if d is None:
        return None
    return d.isocalendar()[0]

from assignee_resolver import AssigneeResolver, load_synced_assignees

_resolver = AssigneeResolver(full, issues_list, roster,
                             load_synced_assignees(paths.sources_jira(),
                                                   paths.jira()["project_key"] + "-"))

def get_assignee_at(issue_key, at_ts):
    """Assignee at moment at_ts — общая логика, см. assignee_resolver."""
    return _resolver.at(issue_key, at_ts)


# Build weekly_by_person from scratch
# Structure: {person: {week: {"stories": 0, "others": 0, "ba_sa": 0}}}
weekly_by_person = defaultdict(lambda: defaultdict(lambda: {"stories": 0, "others": 0, "ba_sa": 0}))

for key, issue in full.items():
    typ = issue_types.get(key, "?")
    
    # Determine which status triggers the event based on issue type
    if typ == "BA-SA Task":
        # BA-SA Task: event = FIRST transition to "Закрыто"
        for tr in issue.get("status_history", []):
            if tr["to"] == "Закрыто":
                y = iso_year(tr["at"])
                w = iso_week(tr["at"])
                if y is None or w is None or y != YEAR:
                    continue
                
                assignee = get_assignee_at(key, tr["at"])
                if assignee is None:
                    assignee = "Не назначен"
                
                weekly_by_person[assignee][w]["ba_sa"] += 1
                break  # Only FIRST transition to "Закрыто"
    else:
        # История, Инцидент, and any other non-BA-SA type: event = transition to "Аналитика - готово"
        for tr in issue.get("status_history", []):
            if tr["to"] != "Аналитика - готово":
                continue
            y = iso_year(tr["at"])
            w = iso_week(tr["at"])
            if y is None or w is None or y != YEAR:
                continue
            
            assignee = get_assignee_at(key, tr["at"])
            if assignee is None:
                assignee = "Не назначен"
            
            # Bucket by issue type
            if typ == "История":
                weekly_by_person[assignee][w]["stories"] += 1
            else:
                # Инцидент and any other non-BA-SA type → others
                weekly_by_person[assignee][w]["others"] += 1

# Convert to regular dict for JSON serialization
weekly_by_person_dict = {}
for person, weeks_data in weekly_by_person.items():
    weekly_by_person_dict[person] = dict(weeks_data)

# Validate: sum all buckets and compare against metrics["weekly"]
aggregate = {"stories": 0, "others": 0, "ba_sa": 0}
for person, weeks_data in weekly_by_person_dict.items():
    for week, buckets in weeks_data.items():
        aggregate["stories"] += buckets.get("stories", 0)
        aggregate["others"] += buckets.get("others", 0)
        aggregate["ba_sa"] += buckets.get("ba_sa", 0)

# Get authoritative totals from metrics["weekly"]
weekly_totals = {"stories": 0, "others": 0, "ba_sa": 0}
for week, buckets in metrics["weekly"].items():
    weekly_totals["stories"] += buckets.get("stories", 0)
    weekly_totals["others"] += buckets.get("others", 0)
    weekly_totals["ba_sa"] += buckets.get("ba_sa", 0)

print(f"Rebuilt weekly_by_person sums: stories={aggregate['stories']}, others={aggregate['others']}, ba_sa={aggregate['ba_sa']}")
print(f"Authoritative weekly totals:   stories={weekly_totals['stories']}, others={weekly_totals['others']}, ba_sa={weekly_totals['ba_sa']}")

# Check if they match
if aggregate["stories"] == weekly_totals["stories"] and \
   aggregate["others"] == weekly_totals["others"] and \
   aggregate["ba_sa"] == weekly_totals["ba_sa"]:
    print("Sums match!")
    
    # Write the rebuilt weekly_by_person back to analyst_metrics.json
    metrics["weekly_by_person"] = weekly_by_person_dict
    
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    
    print("Updated analyst_metrics.json with corrected weekly_by_person")
    exit(0)
else:
    print("Sums do NOT match!")
    print(f"  stories: rebuilt={aggregate['stories']}, expected={weekly_totals['stories']}")
    print(f"  others: rebuilt={aggregate['others']}, expected={weekly_totals['others']}")
    print(f"  ba_sa: rebuilt={aggregate['ba_sa']}, expected={weekly_totals['ba_sa']}")
    exit(1)