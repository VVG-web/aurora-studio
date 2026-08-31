# Cockpit Module

The Aurora Cockpit is a **local, single-user control panel** for the Aurora framework: it
discovers every Aurora project on the machine, shows the health of their knowledge bases, launches
engine commands from a registry, edits project files, manages git commits, updates the engine kit,
and provides quick-start routine scenarios — all in one browser window on `127.0.0.1`.

Everything is stdlib-only and self-contained. The server lives in `cockpit/aurora_cockpit.py`
(a Python 3 `http.server` app that also runs engine commands as subprocesses), and the whole UI —
structure, inline JS, no CDN, no build step — lives in a single file `cockpit/ui/index.html`.
Theming is driven by `cockpit/skins/*.css` files, strings by `cockpit/i18n/*.json`, and
quick-start routines by the plain-text `cockpit/scenarios.txt`; adding a skin or a language
requires editing neither the server nor the panel.

Security model: the server listens only on the loopback interface and every API request (except the
vendored `/vendor/` static files) requires a per-session token delivered together with the HTML page.

## Documents

- [Server & Launch](cockpit/01-server-and-launch.md) - entry point, CLI flags, token/security, restart, session files
- [Project Discovery](cockpit/02-project-discovery.md) - searching roots, project cards, the «Мостик» bridge overview
- [Health Dashboard](cockpit/03-health-dashboard.md) - base-health metrics aggregated from engine commands and caches
- [Command Runner & Console](cockpit/04-command-runner-console.md) - command registry, job execution, console, run log, quick-start routes, and console resumability (honest stall stop, offline wait, single-run retry, persisted `/api/route/state`)
- [File Editor](cockpit/05-file-editor.md) - project file tree, read/edit/create/rename/delete, preview, lint-on-save
- [Git & Kit Maintenance](cockpit/06-git-kit-maintenance.md) - git state/commit/push, engine-kit self-update, about
- [Project & Agent Settings](cockpit/07-project-agent-settings.md) - agent config/env, artifact kinds, tokens, sources, setup, project creation
- [Skins & Localization](cockpit/08-skins-localization.md) - theming via CSS files and i18n string catalogues

---
*Last updated: 2026-08-30*
*Areas: aurora-studio, cockpit*