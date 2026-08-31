# Reports Configuration & Paths Function
## Description
Resolves where the analyst report takes its settings and data from and where it puts results. The
report has no config of its own: everything comes from the project's `aurora.config.yaml`, read
without `PyYAML` (plain regex-based YAML parsing), with safe defaults for a project that wrote
nothing under `reports:`. Every value returned is an absolute path from the project root, since the
package scripts always run with the project as the working directory.

```python
ROSTER_PATH = setting("roster")
EVENTS_PATH = setting("events")
DATA_DIR    = setting("data_dir")
OUTPUT_PATH = setting("output")
```

## Key Features
- **YAML without PyYAML.** `scalar()` and `section()` (indent-aware, keyed by leading spaces,
  never strung out to the next section marker) pull a key or a nested block out of
  `aurora.config.yaml`. `_analyst()` extracts the nested `reports:` → `analyst:` section.
- **Defaults for everything.** `DEFAULTS` supplies `roster`, `events`, `data_dir`, `output` when
  the section is absent, so a project with no config still assembles.
- **Per-year reporting.** The reporting year comes from the `AURORA_REPORT_YEAR` environment
  variable first, else the `year:` config key, else today's year — the orchestrator runs the
  compute chain once per year by injecting the year. `configured_years()` reads the optional
  `years: [2024, 2025]` restriction.
- **Per-year cache layout.** Raw exports are shared across years (`data()`); computed per-year JSON
  goes into `by-year/<year>/` (`out()`), and the directory is created here so the four compute
  steps each need no `makedirs` of their own. `years_built()` lists years whose
  `analyst_metrics.json` already exists.
- **CSV parsing for Excel.** `read_rows()` auto-detects `;` vs `,` (Russian Excel saves with `;`)
  and strips the UTF-8 BOM. `roster()` parses ФИО → role once for any consumer.
- **Jira/Confluence addresses.** `jira()` and `confluence()` read `base_url` / `project_key` /
  `space` from the `atlassian:` section. `sources_jira()` returns the `sync:jira` mirror path
  used to freshen assignees.
- **Compatibility view.** `get_config()` / `load_config()` expose a dict-shaped view for scripts
  that migrated from the original package.
- **Name canonicalisation.** `project_slug()` is a machine-safe short project name (spaces →
  `_`, used in the output filename); `project_name()` is the human name shown in the dashboard
  header.

## Related Documentation
### Technical Details
- [Analyst Report Pipeline Architecture](../../design/05-analyst-report-pipeline.md) - design overview
### Source Files
- reports/analyst/paths.py - configuration, defaults, per-year paths, CSV/roster helpers
- reports/analyst/templates/roster.csv - template CSV header (`ФИО;Email;Роль;Прежние ФИО`)
- reports/analyst/templates/events.csv - template CSV header (`weeks;name;caption;severity`)
### Related Functions
- [Data Fetching (Jira & Confluence)](./02-data-fetching.md) - uses these paths and tokens
- [Dashboard Generation](./05-dashboard-generation.md) - reads output path and config files

## Implementation Notes
All module-level constants (`ROSTER_PATH`, `OUTPUT_PATH`, `YEAR`, …) are resolved at import time.
`ensure_dirs()` creates the cache, per-year and output directories up front. The CSV delimiter
heuristic counts separators in the first line only.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*