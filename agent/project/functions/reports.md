# Reports Module

The **Reports** module builds the «Эффективность аналитиков» (analyst efficiency) dashboard — a
single self-contained HTML file with weekly activity from Jira and Confluence, issue transitions
between stages, and filters by issue type and person. It is a Python stdlib-only pipeline with no
external dependencies (no `PyYAML`, no `requests`, no `numpy`), driven entirely by the project's
`aurora.config.yaml` rather than by hard-coded project constants.

The pipeline runs in two phases. First, a **data-fetch phase** (run once per project) that exports
raw material from Jira and Confluence into a cache directory: the issue list, subtasks, full status /
assignee / "responsible" change history, and Confluence page metadata. Then a **compute phase** (run
once per reporting year) that turns that raw material into per-year JSON metrics and finally assembles
the HTML dashboard across all years. The `serve_dashboard.py` server is optional and only powers the
"open config in editor" and "rebuild" buttons that appear when the dashboard is served over HTTP.

The dashboard carries every year built so far — the year picker in the header simply re-points the
same blocks at another year's data, so switching a reporting period never re-queries Jira or Confluence.

## Documents

- [Reports Configuration & Paths](reports/01-reports-configuration-paths.md) - settings source, defaults, per-year paths, roster/CSV parsing
- [Data Fetching (Jira & Confluence)](reports/02-data-fetching.md) - export scripts, auth tokens, cache files
- [Assignee Resolution](reports/03-assignee-resolution.md) - who was the assignee at a given moment
- [Analyst Metric Computation](reports/04-analyst-metric-computation.md) - weekly transitions, stage durations, BA-SA Task, per-person reconciliation
- [Dashboard Generation](reports/05-dashboard-generation.md) - assembly of years into one HTML report
- [Dashboard Server](reports/06-dashboard-server.md) - HTTP server for open-file / rebuild buttons
- [Analyst Report Pipeline Architecture](../design/05-analyst-report-pipeline.md) - design overview

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*