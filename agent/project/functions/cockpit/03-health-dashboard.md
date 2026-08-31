# Health Dashboard Function

## Description

The «Здоровье» (health) dashboard and the «Мостик» global summary aggregate the state of each
project's knowledge base. The panel itself computes nothing — it runs the engine's own commands
(`stats --json`, `lint`, `doctor`, `audit`, etc.) and assembles their output plus on-disk
caches into one JSON payload (`/api/health`) that the UI renders as clickable metric tiles, each
leading to the command that works on that number.

## Key Features

- **`health(project)`** aggregates: stats (`aurora_stats.py --json`), full lint breakdown
  (`kb_lint.py` incl. kinds and `lint_baseline.txt`), doctor (`aurora_doctor.py`) errors/warns,
  mirror audit (`sync_audit.py --json` with a legacy fallback), build progress
  (`build_plan.py --status`), the last embedded-agent run, sources registry, run log, trace summary,
  todo count, source health, semantic-index health, ping state, unfinished artifacts, corrections, and
  retrieval freshness.
- **Corrections state** (`corrections_state`) reports how many human corrections are active and how many
  are "under question" (their source updated after the correction was written) via `kb_corrections.py --check`.
- **Semantic index health** (`index_health`) distinguishes "not indexed" from "stale" cards by
  comparing bodies against the index's digest (`kb_embed.card_texts`/`digest`), read from the
  project's `embeddings.json` snapshot under `AuroraKnowledgeDB/meta`.
- **Source health** (`source_health`) counts, per `Sources/`/`Raw/` mirror, total vs. parsed
  vs. archived documents from the project's `manifest.json` under `AuroraKnowledgeDB/meta`.
- **Build progress** (`build_progress`) parses the «Источников / обработано / осталось» line from
  `build_plan.py --status` into a percentage.
- **Metrics UI** (`.metric` tiles in `cockpit/ui/index.html`) — every number is clickable and jumps
  to the engine command that addresses it.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - how health reads scripts vs. disk caches
### Source Files
- `cockpit/aurora_cockpit.py` - `health`, `corrections_state`, `index_health`, `source_health`, `build_progress`, `last_agent_run`, `todo_count`, `ping_state`, `unfinished`, `retrieval_state`, `graph_state`, `run_capture`, `script_path`
- `cockpit/ui/index.html` - `view-health` and `view-overview` markup

### Related Functions
- [Project Discovery](./02-project-discovery.md) - tile severity colors feed from doctor blockers
- [Command Runner & Console](./04-command-runner-console.md) - metric tiles launch the relevant command

## Implementation Notes

`script_path(project, script)` decides where a script comes from: `KIT_SIDE` scripts
(`aurora_update.py`, `install_aurora.py`, `kit_commands.py`, `aurora_setup.py`) always from the
kit; everything else from the project engine with a kit fallback, so a new command works in an old
project. Output is captured with a timeout (`run_capture`), and several numbers are read from disk
caches (trace-summary, graph.json, embeddings, manifest) rather than re-run — the dashboard should be
fast to open and honest about how fresh it is.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*