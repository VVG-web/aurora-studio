# aurora-studio — Project Documentation

> aurora-studio — unknown project. Auto-generated navigation hub for the
> incremental documentation map maintained by the vibedocing pipeline.

- **Source root:** `aurora-studio`
- **Default branch:** `master`

## Repository / Module Map

<!-- The pipeline fills this in as it walks commits. Seed the top-level modules here if you
     know them; otherwise let the agent populate it. -->

- `cockpit/` — control panel: server (`aurora_cockpit.py`), single-file UI (`ui/index.html`), `skins/*.css`, `scenarios.txt`; console resumability added in 1.100.6
- `scripts/` — engine scripts (kb:*, sync:*, ship:*, ops:*, ctx:*, make:*, kit:*), stdlib-only
- `skills/` — agent skills: `aurora-vault` (knowledge framework), `aurora-dev` (engine dev/QA)
- `templates/` — config / AGENTS / US / AC / DR / spec / meeting recipes
- `scaffold/` — project scaffolding layout
- `docs/` — human documentation (readme/, lifecycle, commands, roadmap)
- `tests/` — engine test suite (`run_tests.py`)
- `aurora.py` — single entry point: `new`, `setup`, `update`, maintenance commands

## Function Documentation

<!-- Module indexes (functions/<module>.md) and flat docs (functions/<number>-<name>.md)
     are linked here as functions are documented. -->
- [Cockpit Module](functions/cockpit.md)
- [Connectors Module](functions/connectors.md)
- [Skills Module](functions/skills.md)
- [Scaffolding Module](functions/scaffolding.md)
- [Templates Module](functions/templates.md)
- [Examples Module](functions/examples.md)
- [Reports Module](functions/reports.md)


## Technical Design Documents

<!-- Module indexes (design/<module>.md) and flat docs (design/<number>-<name>.md) are
     linked here as designs are documented. -->
- [Cockpit Architecture Design](design/01-cockpit-architecture.md)
- [Skills Support Files Design](design/02-skills-support-files.md)
- [Scaffolding Architecture Design](design/03-scaffolding-architecture.md)
- [Templates Layout & Generation Design](design/04-templates-layout-generation.md)
- [Analyst Report Pipeline Design](design/05-analyst-report-pipeline.md)


---

## Sync Status

> The single source of truth for incremental re-runs. The pipeline advances `baseline` to
> the last fully-processed project commit and **fills the fields below automatically**
> after every run — do not edit them by hand. After you sync new upstream changes into
> `aurora-studio`, re-running the pipeline documents only `baseline..HEAD`.

- **Project source:** `aurora-studio`
- **Branch:** `master`
- **Baseline commit:** `1acd75fcfe31` — test(embed): предфильтр на масштабе 1000–1500×768
- **Last synced:** 2026-08-28 (through `1acd75fcfe31`)

*Last updated: 2026-08-30*
