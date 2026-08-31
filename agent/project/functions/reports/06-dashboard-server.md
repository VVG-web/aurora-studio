# Dashboard Server Function
## Description
An optional `http.server`-based server (`serve_dashboard.py`) that serves the analyst dashboard over HTTP
and backs the dashboard's "open config in editor" and "rebuild" buttons. It serves the project root
(`paths.PROJECT_ROOT`), so both the report under `Artifacts/` and the settings files under
`Settings/` are reachable. Without it the dashboard still works as a plain file; the buttons then fall
back to copying the relevant command/path.

CLI: `--port` (default `8000`) and `--html` (open the dashboard in the browser after starting).

## Key Features
- **Static serving.** `DashboardHandler(SimpleHTTPRequestHandler)` with `directory=BASE_DIR` serves the
  whole project root; requests starting with `/__open/`, `/__reveal/` or `/__rebuild` are handled
  specially instead.
- **Open config in editor.** `handle_open_file()` maps a known config name (from `CONFIG_FILES`:
  `roster`, `events`, `analyst_metrics`, `confluence_activity`) to an absolute path — lookups by
  name prevent path injection — and opens it with a text editor. Editor preference order (macOS) is
  `AURORA_EDITOR_APP`, Cursor, VS Code, Sublime Text, TextEdit, then the default app; a `.csv` is
  opened in a text editor rather than Numbers so edits reach the dashboard.
- **Reveal in folder.** `/__reveal/<name>` shows a settings file in the OS file manager
  (Finder / Explorer / `xdg-open`).
- **Rebuild.** `/__rebuild` runs the compute chain in order —
  `process_confluence.py`, `make_analyst_metrics.py`, `update_analyst_metrics.py`,
  `verify_weekly_by_person.py`, `make_extended.py` — with `cwd=BASE_DIR`, returning an error with
  the failing step on a non-zero exit. The server is multi-threaded
  (`ThreadingHTTPServer`) because a rebuild takes seconds and a single-threaded server would hang the
  page meanwhile.
- **Dashboard URL discovery.** On startup it prints the report URL computed from `paths.OUTPUT_PATH`
  relative to the served project root.

## Related Documentation
### Technical Details
- [Analyst Report Pipeline Architecture](../../design/05-analyst-report-pipeline.md) - design overview
### Source Files
- reports/analyst/serve_dashboard.py - server, handler, endpoints
### Related Functions
- [Dashboard Generation](./05-dashboard-generation.md) - the HTML it serves and whose buttons call it
- [Reports Configuration & Paths](./01-reports-configuration-paths.md) - paths it serves

## Implementation Notes
`REBUILD_CHAIN` is built from `__file__`'s directory, and `BASE_DIR` is fixed at
`paths.PROJECT_ROOT`. The server binds to `localhost` only.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*