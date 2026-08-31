# Analyst Report Pipeline Design
## Overview
The analyst report pipeline (module `reports/analyst`) builds a single self-contained HTML dashboard of
weekly Jira and Confluence analyst activity. It is stdlib-only Python with no project constants in the
scripts: configuration comes from `aurora.config.yaml`, paths and defaults are resolved by `paths.py`,
and the pipeline is split into a **data-fetch phase** (once per project) and a **compute phase**
(once per reporting year) so that switching report periods never re-queries the corporate systems.

## Architecture / Components
### Paths & configuration (`paths.py`)
**File:** reports/analyst/paths.py
**Purpose:** single source of settings, defaults, and per-year cache layout; plain-regex YAML
parsing and CSV/delimiter-aware roster reading.
**API / Interface:**
```python
ROSTER_PATH = setting("roster")
DATA_DIR    = setting("data_dir")
OUTPUT_PATH = setting("output")
def out(name: str, y: int | None = None) -> str: ...   # by-year/<year>/<name>
def years_built() -> list: ...
```
### Shared Jira API (`jira_api.py`)
**File:** reports/analyst/jira_api.py
**Purpose:** one copy of Jira auth, `api()` (retry/backoff) and paginated `search_all()` for the
fetch scripts.
**API / Interface:**
```python
BASE_URL = paths.jira()["base_url"] + "/rest/api/2"
FIELDS = ("key,issuetype,status,resolution,assignee,reporter,created,updated,labels,summary,parent")
def token() -> str: ...
def api(path: str, params: dict | None = None, max_tries: int = 4): ...
def search_all(jql: str, fields: str = FIELDS, progress=None) -> list: ...
```
### Fetch phase
**Files:** reports/analyst/fetch_issues.py, fetch_subtasks.py, fetch_full.py,
fetch_confluence_metadata.py
**Purpose:** export raw Jira issues + subtasks + changelog histories and Confluence page metadata into
the shared cache (`issues.json`, `full_status.json`, `confluence_raw_metadata.json`).
### Assignee resolution (`assignee_resolver.py`)
**File:** reports/analyst/assignee_resolver.py
**Purpose:** answer "who was the assignee at moment t" from changelog history, the «Ответственный»
field, analyst subtasks, the `sync:jira` mirror, in order of reliability.
**API / Interface:**
```python
class AssigneeResolver:
    def __init__(self, full, issues_list, roster=None, synced_assignees=None): ...
    def at(self, issue_key, at_ts): ...
def load_synced_assignees(sources_dir, key_prefix=""): ...
```
### Compute phase
**Files:** reports/analyst/process_confluence.py, make_analyst_metrics.py,
update_analyst_metrics.py, verify_weekly_by_person.py
**Purpose:** produce per-year JSON — `confluence_activity.json` and `analyst_metrics.json` — with
weekly `stories`/`others`/`ba_sa` buckets, stage-transition durations, and a validated
`weekly_by_person` reconciliation.
### Dashboard generation (`make_extended.py`)
**File:** reports/analyst/make_extended.py
**Purpose:** assemble every built year into one HTML report with an embedded `DATA` object and the
`chart.js` front end; canonicalise names via roster aliases and keep Confluence pages as one shared list.
### Dashboard server (`serve_dashboard.py`)
**File:** reports/analyst/serve_dashboard.py
**Purpose:** optional HTTP server backing the edit/reveal/rebuild buttons
(`/__open/`, `/__reveal/`, `/__rebuild`).

## Design Decisions
- **Stdlib only.** YAML is parsed with regex instead of `PyYAML`, statistics without `numpy`, and the
  server with `http.server`. There are no runtime dependencies to install.
- **No project constants in scripts.** Everything (year, roster/events paths, Jira/Confluence
  addresses, project name/slug) comes from `aurora.config.yaml` with defaults, so a fresh project
  assembles with empty templates.
- **Fetch once, compute per year.** Raw exports are year-agnostic; the reporting year is injected via
  `AURORA_REPORT_YEAR` and reads `paths.year()`, so `fetch_full.py` does not need `dt.year == Y`
  filters and switching periods never re-fetches.
- **One dashboard with a year picker.** Instead of building a separate HTML per year, all years are
  carried in one file and the picker re-points the active data references (`DATA.jira` etc.) — the
  front end knows nothing about switching. This keeps report size sane (Confluence pages shared once).
- **Trust the reconciliation.** `verify_weekly_by_person.py` only writes `weekly_by_person` when its
  sum matches the authoritative totals, guarding against double-counting bugs (e.g. the earlier
  BA-SA/others overlap).
- **Changelog fallback ladder.** Because Jira omits the creation-time assignee and some people use the
  «Ответственный» field, assignee-at-time resolution falls through a defined source ladder rather than
  guessing "Не назначен" at the first miss.
- **Trend over the working window.** The dashboard trend line is computed only from the first active week
  to the last (excluding the current unfinished week), so late project starts and not-yet-reached weeks
  do not distort the slope.

## Source Files
- reports/analyst/paths.py - config, defaults, per-year paths, roster/CSV helpers
- reports/analyst/jira_api.py - shared Jira API
- reports/analyst/fetch_issues.py - issue list export
- reports/analyst/fetch_subtasks.py - subtask export
- reports/analyst/fetch_full.py - status/assignee/responsible history export
- reports/analyst/fetch_confluence_metadata.py - Confluence metadata export
- reports/analyst/assignee_resolver.py - assignee-at-time resolution
- reports/analyst/process_confluence.py - Confluence weekly activity
- reports/analyst/make_analyst_metrics.py - Jira weekly/transitions metrics
- reports/analyst/update_analyst_metrics.py - BA-SA Task weekly metrics
- reports/analyst/verify_weekly_by_person.py - per-person reconciliation
- reports/analyst/make_extended.py - dashboard assembly
- reports/analyst/serve_dashboard.py - dashboard server
- reports/analyst/templates/roster.csv - roster CSV template header
- reports/analyst/templates/events.csv - events CSV template header

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*