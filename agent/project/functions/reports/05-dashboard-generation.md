# Dashboard Generation Function
## Description
Assembles the final «Эффективность аналитиков» dashboard — one self-contained HTML file carrying every
built year at once. It reads the per-year `analyst_metrics.json` and `confluence_activity.json`
files, rolls the Confluence pages out once for all years, and embeds a `DATA` object plus a full
inline `chart.js` front end into the HTML template. Opening the file works with no server.

```python
YEARS = paths.years_built() or [YEAR]
DATA = {
    "jira": YEAR_DATA[str(YEAR)]["jira"],
    "confluence": YEAR_DATA[str(YEAR)]["confluence"],
    "pages": shared_pages(),
    "years": YEAR_DATA,
    "year": YEAR,
    "years_available": YEARS,
    "partial": partial,
    "roster": roster,
    "events": events,
    ...
}
```

## Key Features
- **All years in one file.** `years_built()` determines what to carry; the year picker in the header
  just re-points `DATA.jira` / `DATA.confluence` / `DATA.week_labels` at another year — no rebuild
  or re-fetch is needed to switch a period.
- **Name canonicalisation.** `prepare()` and `shared_pages()` map every name to its current ФИО via the
  roster's `Прежние ФИО` aliases (`canon()`), merging one analyst who appears under an old surname
  back into a single row; empty names become «Не назначен».
- **Shared Confluence pages.** `shared_pages()` returns the page list once (not once per year), so a
  multi-year report does not repeat the same page metadata seven times; week-marking is done in the
  browser per active year.
- **Events banding.** The `events.csv` rows are loaded (`weeks`, `caption`, `severity`) and aligned
  to the two-digit week numbers the data uses; red/yellow bands plus captions are drawn on the weekly
  chart.
- **Perspective and trend control.** Renders weekly stacked bars (Истории / Прочие / BA-SA), the
  trend line computed only over the working window (from the first active week to the last, excluding the
  current unfinished week), stage-duration tables per percentile (95% default), per-employee duration
  tables, and per-week Confluence page-created / page-updated charts.
- **Filters.** Client-side filtering by issue type, role, person and week (with search), a weight for
  Прочие/BA-SA, and a total vs. per-capita scale mode.
- **Config & rebuild buttons.** `rebuild_cmd` embeds the ready rebuild command; `config_files` records
  roster / events / metrics paths so buttons can open them in an editor (via the server) or copy them.

## Related Documentation
### Technical Details
- [Analyst Report Pipeline Architecture](../../design/05-analyst-report-pipeline.md) - design overview
### Source Files
- reports/analyst/make_extended.py - assembly and dashboard front end
### Related Functions
- [Analyst Metric Computation](./04-analyst-metric-computation.md) - provides the per-year JSON
- [Reports Configuration & Paths](./01-reports-configuration-paths.md) - output path, roster/events
- [Dashboard Server](./06-dashboard-server.md) - powers the edit/rebuild buttons over HTTP

## Implementation Notes
Module constants `DATA` and `YEARS` are computed at import; `YEAR_DATA = {str(y): prepare(y) for y in
YEARS}`. Week labels use `datetime.date.fromisocalendar(y, w, 1)`. `shared_pages()` returns a single
copy of pages with `_week_created` / `_week_updated` fields removed (laid out in the browser).

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*