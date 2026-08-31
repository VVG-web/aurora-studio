# Command Runner & Console Function

## Description

Lets the panel run Aurora engine commands from a registry and watch their output in a built-in console,
and drives multi-step quick-start routines («Быстрый старт»). Commands come from the kit's
`commands.txt` via `kit_commands`; the UI never types an arbitrary shell string — arguments are
validated against each command's declared flags and passed as a list (no shell).

## Key Features

- **Registry** (`registry()`): reads `commands.txt` through `kit_commands` into a disk cache
  (`.registry-cache.json`) keyed by kit version, VERSION mtime, commands.txt mtime, source-mode
  flag and `ENGINE`; warms `--help` text in a thread pool (8 workers). `dev:` commands are
  hidden unless the panel is started from a kit source tree (`kit_is_source()`).
- **Validation** (`start_job()`): only registered, runnable commands start; only declared flags plus
  `--apply`, `--allow-dirty`, `--force`, `--json` are accepted. Route runs are refused when
  `version_gap()` finds the project engine and kit on different minor versions.
- **Job execution**: each run becomes a `JOBS` entry streamed line-by-line into a bounded buffer
  (`job["out"]`), polled by `/api/job`; `stop_job()` terminates then kills; `mark_running`/
  `write_runlog` persist liveness and the per-command last run.
- **Console UI** (`view-console`): live output with sever color-coded lines, retry/back-to-primary
  controls, aggressive-tail detection, and the run-log history list fed from `.opencode/run_log.md`.
- **Quick-start routes** (`scenarios()` parsing `cockpit/scenarios.txt`): named routines with ordered
  steps (command lines, human steps, `цикл:`/`конец цикла` batching) launched via `/api/run`
  with `route=1`; a «Посмотреть» button drops `--apply` to show the same route without writing.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - job model, registry caching, script_path
### Source Files
- `cockpit/aurora_cockpit.py` - `registry`, `command_by_name`, `scenarios`, `start_job`, `stop_job`, `run_capture`, `write_runlog`, `read_runlog`, `who`, `version_gap`, `kinds` route handling; route-state persistence `route_state_path`, `read_route_state`, `write_route_state`, `clear_route_state` and the `GET`/`POST /api/route/state` endpoints
- `cockpit/scenarios.txt` - the quick-start route definitions
- `cockpit/ui/index.html` - `view-commands`, `view-console`, `view-quickstart`, `view-work`, palette `⌘K`; console resumability `showLastRoute`, `resumeLastRoute`, `showOfflineResume`, `waitNetworkCycle`, `retryStep` and constants `ROUTE_OFFLINE_RETRY_MS`, `ROUTE_OFFLINE_TRIES`, `OFFLINE_SIGNS`

### Related Functions
- [Project & Agent Settings](./07-project-agent-settings.md) - agent env/ping routes used by the run flow
- [Health Dashboard](./03-health-dashboard.md) - metric tiles jump to a command

## Implementation Notes

Arguments go through `subprocess` **without a shell** (`cwd=project`, inherited env from
`aurora_common.child_env` with `PYTHONUNBUFFERED="1"`). Return code semantics drive route
behavior: `2` and above stop a route, `1` ("worked and found something to fix") does not. The run
log records only panel-launched runs (one row per command, last run) so it stays a small
merge-friendly file in git.

## Console Resumability (1.100.6)

Routes and single runs can now be stopped honestly and resumed instead of silently passing or
restarting from scratch:

- **Honest stall stop**: a route that completes a lap with no work kind decreasing is stopped (not
  marked "passed") with the exact label `остановлен: застой — работа не убывает` and a
  «Продолжить маршрут» button.
- **Persisted route state**: the last stopped route is written to the project's common dir
  `AuroraKnowledgeDB/meta/last_route.json` (`{scId, runId, title, write, reason, at, step, attempts?,
  nextRetryAt?}`) via `POST /api/route/state` (cleared by a `null` body or `{"clear": true}`).
  `GET /api/route/state?project=` returns it, so «Продолжить маршрут» survives a tab or panel
  restart. `reason` is one of `stall | failed | offline | stopped`.
- **Resume skips only success**: continuation re-runs the route skipping only steps that already
  returned `0`; steps that returned `1` ("found something to fix") are re-run.
- **Offline wait**: a step failing with `1` plus an offline signature (the panel's `OFFLINE_SIGNS`
  is a literal copy of the engine's list) puts the route on a wait — retry every 15 minutes
  (`ROUTE_OFFLINE_RETRY_MS`), up to 8 attempts (~2 h, `ROUTE_OFFLINE_TRIES`), with «Попробовать
  сейчас» / «не ждать» buttons. The timer lives in the tab but survives a reload via `nextRetryAt`
  (missed tick = resume now; future tick = re-arm; capped = manual only). «не ждать» records
  `reason:"stopped"` (a user stop, not a green pass).
- **Single-run retry**: a standalone command ending with `rc >= 2` gets a «Попробовать снова»
  button that re-sends the same command line as a new job, incrementing the attempt number
  («попытка N»). No button on `rc 0/1` or on a signal-killed (negative-rc) run.

---
*Last updated: 2026-08-30*
*Areas: aurora-studio, cockpit*