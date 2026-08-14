#!/usr/bin/env python3
"""process_confluence.py — сырьё Confluence → недельная активность по людям.

Вход: `confluence_raw_metadata.json` (его пишет fetch_confluence_metadata.py).
Выход: `confluence_activity.json` — его читает make_extended.py.
"""
import json
import os
import sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

YEAR = paths.YEAR

roster = paths.roster()
print(f"Roster loaded: {len(roster)} people")

# Load confluence metadata
with open(paths.data('confluence_raw_metadata.json'), 'r', encoding='utf-8') as f:
    data = json.load(f)

pages = data['pages']
print(f"Total pages: {len(pages)}")

# Parse dates and extract ISO week
def parse_date(date_str):
    if not date_str:
        return None
    try:
        # Format: 2024-11-26T16:11:12.174+03:00
        return datetime.strptime(date_str[:19], '%Y-%m-%dT%H:%M:%S')
    except:
        return None

def iso_week(date_obj):
    if not date_obj:
        return None
    # ISO week number
    return date_obj.isocalendar()[1]

def year(date_obj):
    if not date_obj:
        return None
    return date_obj.year

# Process pages
processed_pages = []
weeks_in_year = set()
created_in_year = 0
authors_not_in_roster = set()
role_counts = defaultdict(int)

for page in pages:
    title = page.get('title', '')
    page_id = page.get('page_id', '')
    author_created = page.get('author_created', '')
    author_updated = page.get('author_updated', '')
    
    # Parse created date
    created_str = page.get('created', '')
    created = parse_date(created_str)
    
    # Parse updated date
    updated_str = page.get('updated', '')
    updated = parse_date(updated_str)
    
    # Get role based on author_created (creator)
    role = roster.get(author_created, None)
    if role:
        role_counts[role] += 1
    else:
        if author_created:
            authors_not_in_roster.add(author_created)
    
    # Track weeks of the reporting year for created
    if created and year(created) == YEAR:
        week = iso_week(created)
        if week:
            weeks_in_year.add(week)
        created_in_year += 1
    
    # Also track weeks of the reporting year for updated
    if updated and year(updated) == YEAR:
        week = iso_week(updated)
        if week:
            weeks_in_year.add(week)
    
    processed_pages.append({
        'title': title,
        'page_id': page_id,
        'created': created_str,
        'updated': updated_str,
        'author_created': author_created,
        'author_updated': author_updated,
        'role': role
    })

# Remove duplicates by page_id (keep first occurrence)
seen_ids = set()
unique_pages = []
for page in processed_pages:
    pid = page['page_id']
    if pid not in seen_ids:
        seen_ids.add(pid)
        unique_pages.append(page)

processed_pages = unique_pages
# Build role_of mapping
role_of = {fio: role for fio, role in roster.items()}

# Build weeks list (sorted)
weeks_list = sorted([str(w).zfill(2) for w in weeks_in_year])

# Build output
output = {
    'weeks': weeks_list,
    'pages': processed_pages,
    'role_of': role_of
}

# Write output
os.makedirs(os.path.dirname(paths.out('confluence_activity.json')), exist_ok=True)
with open(paths.out('confluence_activity.json'), 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"Written confluence_activity.json")

# Statistics
print(f"\n=== Statistics ===")
print(f"Total pages (raw): {len(pages)}")
print(f"Total pages (unique): {len(processed_pages)}")
print(f"Pages with non-empty created: {sum(1 for p in processed_pages if p['created'])}")
print(f"Pages created in {YEAR}: {created_in_year}")
print(f"ISO weeks in {YEAR}: {len(weeks_list)} -> {weeks_list[:10]}...")
print(f"Unique authors (created): {len(set(p['author_created'] for p in processed_pages if p['author_created']))}")
print(f"Authors NOT in roster: {len(authors_not_in_roster)}")
print(f"Role distribution: {dict(role_counts)}")

# Top 5 weeks by creation
week_counts = defaultdict(int)
for page in processed_pages:
    created = parse_date(page.get('created', ''))
    if created and year(created) == YEAR:
        week = iso_week(created)
        if week:
            week_counts[week] += 1

top_weeks = sorted(week_counts.items(), key=lambda x: -x[1])[:5]
print(f"Top 5 weeks (by creation): {top_weeks}")
