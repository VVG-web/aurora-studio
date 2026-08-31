# Templates Layout & Generation Design
## Overview
The `templates/` directory is the Aurora kit's project scaffolding: eight files grouped in five subfolders (`launchers/`, `meta/`, `agents/`, `cursor/`) plus two root config files. Each serves as a fill-in-the-blanks starting point that becomes a fixed part of a generated Aurora project, with placeholder tokens (`{{PLACEHOLDER}}`) substituted at generation time and personal secrets kept out of git.

## Architecture / Components
### Project config template
**File:** templates/aurora.config.yaml.template
**Purpose:** committed per-project configuration, schema version 1.
**API / Interface:**
```yaml
aurora:
  version: 1
project:
  name: "{{PROJECT_NAME}}"
  slug: "{{PROJECT_SLUG}}"
```
Holds `skills`, `sources`, `atlassian`, `paths`, `verify`, `privacy`, `reports.analyst`, and `bootstrap`.

### Secrets example
**File:** templates/aurora.env.local.example
**Purpose:** template for the gitignored `.env.aurora.local`; per-machine personal tokens and agent backend ring.
Tracks Confluence/Jira PAT variable pairs (e.g. `CONFLUENCE_PERSONAL_TOKEN` / `CONFLUENCE_PAT`), the user/password fallback, the `AURORA_AGENT_*` backend chain, and `AURORA_EMBED_*` embeddings overrides.

### Agent guidance
**Files:** templates/agents/AGENTS.md.template, templates/cursor/atlassian.mdc.template
**Purpose:** always-active coding-agent rules and a Cursor Atlassian pointer, both referencing `aurora.config.yaml`.

### Launchers
**Files:** templates/launchers/start-aurora.bat, templates/launchers/start-aurora.command
**Purpose:** interactive six-item double-click menus that locate Python and the kit and dispatch to `aurora_doctor.py`, `aurora_stats.py`, `aurora_setup.py`, `aurora.py cockpit [--restart]`, and `kit_commands.py`.

### Meta docs
**Files:** templates/meta/READING.md, templates/meta/conventions.md
**Purpose:** onboard readers of the generated knowledge DB and pin the per-project naming/trust/artifact contracts.

## Design Decisions
- **Placeholders over per-project editing:** every variable value (`{{PROJECT_NAME}}`, `{{PROJECT_SLUG}}`, `{{CONFLUENCE_SPACE}}`, `{{JIRA_KEY}}`, `{{YEAR}}`, `{{KIT_PATH}}`) is a substitution token so the same kit can scaffold any project uniformly. Each token is scoped: config/agent placeholders carry project identity, `{{KIT_PATH}}` only locates the kit.
- **Secrets strictly separated from committed config:** base URLs, spaces and JQL live in the committed `aurora.config.yaml`; tokens live only in gitignored `.env.aurora.local`. The `.env` template even notes MCP cannot hand credentials out, so sync scripts need their own direct REST access.
- **Closed, engine-fixed schema:** card statuses, folder structure and the artifact-type taxonomy are computed/fixed by the engine (`structure_dirs.txt`, `aurora.py update`); hand edits are lost, and agents must write agreements into `conventions.md` rather than generated files.
- **Cross-platform parity:** Windows batch and macOS/Linux shell launchers offer identical menus and core script dispatch with per-platform interpreter/kit discovery.

## Source Files
- templates/aurora.config.yaml.template - committed project configuration template
- templates/aurora.env.local.example - gitignored secrets example
- templates/agents/AGENTS.md.template - always-active agent mandatories + knowledge rules
- templates/cursor/atlassian.mdc.template - Cursor Atlassian rule
- templates/launchers/start-aurora.bat - Windows launcher
- templates/launchers/start-aurora.command - macOS/Linux launcher
- templates/meta/READING.md - knowledge DB reading guide
- templates/meta/conventions.md - knowledge DB conventions

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, templates*