# Cockpit Architecture Design

## Overview

Cockpit is a **two-part, stdlib-only local application**: a Python 3 HTTP server
(`cockpit/aurora_cockpit.py`, ~2,900 lines) that owns all privileged work, and a single
self-contained HTML file (`cockpit/ui/index.html`, ~4,900 lines) that renders every screen.
The server is the only place that reads the filesystem, shells out to engine scripts or writes
project state; the UI is pure presentation that talks to JSON API routes carrying a session token.

Executed engine commands run in the **kit** (`cockpit/` sits inside the Aurora kit, `KIT` is
its parent), so the panel can list the command registry from `commands.txt`, update the kit itself,
and show `docs` — a set called `DOC_ROOTS`. It never accepts arbitrary paths: every project path
must come from the list of discovered projects (`self._known`), every file path is resolved through
`inside()` (realpath containment), and every document shown must be under `DOC_ROOTS`.

## Architecture / Components

### The server — `cockpit/aurora_cockpit.py`
**Purpose:** A `ThreadingHTTPServer` on `127.0.0.1` (default port `8787`) exposing a
JSON-RPC style API, and a stdlib-only command executor.

**Key globals (copied verbatim):**
```python
TOKEN = os.environ.pop("AURORA_COCKPIT_TOKEN", "") or secrets.token_urlsafe(24)
DOC_ROOTS = ("docs", "skills/aurora-vault", "CHANGELOG.md", "commands.txt", "README.md")
ROOTS_FILE = os.path.join(os.path.expanduser("~"), ".aurora", "cockpit-roots.txt")
KIT_SIDE = ("aurora_update.py", "install_aurora.py", "kit_commands.py", "aurora_setup.py")
```

**Job model:** Long-running engine commands become `JOBS` entries guarded by `JOBS_LOCK`, run by a
daemon worker thread on each entry, streaming stdout lines into a bounded buffer (`job["out"]` capped
at 4000 lines) while `mark_running` tracks them on disk (`.running.json`) and `write_runlog`
records the last run per command in `.opencode/run_log.md`.

### The UI — `cockpit/ui/index.html`
**Purpose:** A single-page app with a left nav of runtime-rendered sections (`Мостик`, Здоровье,
Команды, Консоль, Файлы, Граф, Спросить, Отчёты, Настройки…) and a persistent bottom
console. Lazy-loads the vendored graph/editor libs (`/vendor/cytoscape/...`,
`/vendor/vditor/...`) on first open. Default interface language is Russian, loaded into the page
server-side; `UI_VERSION` must track the kit's minor version.

## Design Decisions

- **Stdlib only, closed contour.** The server imports nothing outside the Python standard library plus
  internal `scripts/` modules (`aurora_common`, `kit_commands`, `make_kinds`, `agent_core`,
  `agent_runner`, `confluence_export`). No packages are installed into the kit.
- **Token per session, loopback only.** `guarded()` rejects any non-loopback `Host` and any request
  without the correct token (`secrets.compare_digest`); `/vendor/` static files are served without a
  token because the browser fetches them itself and they contain no project data.
- **Everything validated by containment, not denylists.** Project paths come only from `find_projects`;
  file access resolves symbolic links first (`inside()`) so `..` and out-of-root symlinks are cut;
  report/version paths are looked up only among the actually-saved versions.
- **Skins and languages are data, not code.** New `cockpit/skins/*.css` (name/about/for parsed
  from the file header comment) and `cockpit/i18n/*.json` appear without touching server or UI.
- **Registry, graph, index, run log are cached on disk.** A table-stamp keyed cache
  (`.registry-cache.json`, `CACHE`); the per-project base graph is read from the project's own
  `meta` folder; the semantic index is read back from the project's `embeddings` snapshot; and
  the run log is kept in the project tree next to the engine.
- **Panel never applies irreversible actions itself.** `--apply` comes from the UI after human
  confirmation; git commits skip only the ratchet (never verify hooks); token secrets are only ever
  reported as «заполнено / пусто».
- **Restart is cooperative.** `restart_self`/`--restart` pass `AURORA_COCKPIT_TOKEN` to the
  new process so an already-open tab keeps working, and refuse to restart while jobs are running unless
  `--force`.

## Source Files

### Server
- `cockpit/aurora_cockpit.py` - the whole backend: discovery, health, commands, files, git, kit, agent, HTTP handler, `main()`
### UI
- `cockpit/ui/index.html` - the whole single-file frontend (structure + inline JS + fallback CSS)
### Data-driver files
- `cockpit/skins/*.css` - theming token files, auto-listed by `skins()`
- `cockpit/i18n/*.json` - string catalogues, auto-listed by `languages()`
- `cockpit/scenarios.txt` - quick-start routine scenarios, parsed by `scenarios()`
- `cockpit/vendor/` - vendored libraries (cytoscape, vditor) served at `/vendor/`

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*