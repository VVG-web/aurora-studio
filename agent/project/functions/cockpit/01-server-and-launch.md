# Server & Launch Function

## Description

Launches and runs the local Aurora Cockpit web server. The panel is started from the kit's CLI
(`aurora.py cockpit`) or directly, serving the single-page UI on `127.0.0.1:8787` by
default. It binds only to the loopback interface, issues a fresh per-session token, prints a
one-time URL, opens a browser, and coordinates restarts so an already-open tab keeps working.

## Key Features

- **CLI entry point** in `aurora_cockpit.py`'s `main()` with flags:
  ```python
  ap.add_argument("--port", type=int, default=8787)
  ap.add_argument("--roots", nargs="*", default=None, ...)
  ap.add_argument("--add-root", metavar="PATH", action="append", ...)
  ap.add_argument("--no-browser", action="store_true")
  ap.add_argument("--restart", action="store_true", ...)
  ap.add_argument("--force", action="store_true", ...)
  ```
- Serves `/` and `/index.html` by reading `cockpit/ui/index.html`, injecting the session token and
  the Russian string catalogue, `Cache-Control: no-store`.
- Authenticates every non-vendor API request via `guarded()`: loopback `Host` check plus token
  comparison with `secrets.compare_digest`.
- Serves vendored libraries under `/vendor/` without a token through `send_static()`, with a fixed
  allowlist of content types (`STATIC_TYPES`) and `Cache-Control: max-age=86400`.
- Detects another running instance via `SESSION` (`.session.json` with port/url/pid) and the
  `alive()` ping (`/api/ping` returns `{"app": "aurora-cockpit", ...}`), opening the
  existing panel instead of failing on a busy port.
- Cooperative restart: `restart_self()`/`--restart` stop the old process (SIGTERM), refuse
  while jobs are running unless `--force` (checked against `running_now()`), and pass
  `AURORA_COCKPIT_TOKEN` to the successor so open tabs survive.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - server components, token/security model
### Source Files
- `cockpit/aurora_cockpit.py` - `main()`, `Handler` (GET/POST, `guarded`, `send_static`), `restart_self`, `stop_job`, session helpers
- `cockpit/ui/index.html` - the single-page UI and `restartPanel()` reload flow

### Related Functions
- [Project Discovery](./02-project-discovery.md) - the panel's project list and search roots

## Implementation Notes

The token is generated once per process (`secrets.token_urlsafe(24)`) or inherited via the
`AURORA_COCKPIT_TOKEN` env var on a cooperative restart. `STARTED` (import time) and `ENGINE`
(the file's mtime at import) let the UI detect a stale process — fresh markup served by old code —
via `stale_process` in `/api/state`. `write_session`/`read_session` persist the one-time address
so a second launch can simply reopen the running panel.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*