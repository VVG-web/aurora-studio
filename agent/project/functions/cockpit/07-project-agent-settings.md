# Project & Agent Settings Function

## Description

The «Настройки проекта», «Настройка», «Спросить базу», «Артефакты» and «Разработка»
screens let the analyst configure projects and the machine without a terminal: editing the
`aurora.config.yaml`, pointing at sources and tokens, defining artifact kinds, managing the built-in
agent's env/backends, creating new projects, and querying the base in natural language.

## Key Features

- **Config editing** (`/api/config`, `_write_config`): the config is edited as text with a
  `.bak` backup and a sanity check for a `project:` block; separate helper `kinds_write` rewrites
  only the `artifacts:` section, driven by `make_kinds.FIELDS`.
- **Sources & tokens** (`_write_sources`, `_write_tokens`): rewrites the `sources:` section
  (module ids must be among `sources_registry.py` output) and writes sync tokens into
  `.env.aurora.local` (never read back, file chmod `0o600`).
- **Setup form** (`_run_setup`): passes answers to `aurora_setup.py --target <project> --json -`
  so one place builds the config.
- **New project** (`_create_project`): validates the target against `allowed_bases()`/`writable_target`,
  auto-adds the parent to search roots, then runs `install_aurora.py --target --name` (with
  optional `--slug`, `--jira-key`, `--confluence-space`).
- **Agent configuration** (`agent_state`, `agent_write_env`, `agent_ping`, `agent_venv_install`,
  `backend_models`): reads layers (kit < project) via `agent_core.parse_config`, shows keys masked and
  what the project *owns* vs. inherits; writes only `AURORA_AGENT_*`/`AURORA_EMBED_*`
  vars; returns to the primary provider via the `retry-primary` flag file; installs Pydantic AI into
  the venv on demand.
- **Ask-the-base** (`ask_threads`, `ask_thread`, `card_text`, `agent_ping` UI): natural-language
  questions against the base, threads persisted under the project's `meta/ask/` folder and committed to
  git, answer attribution via `agent_runner`.
- **Artifacts** (`kinds_read`, `artifact_files`, `unfinished`, `corrections_state`): list artifact
  kinds and their files with publication status, flag unfinished pipeline documents, and count active/asked
  human corrections.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - agent imports from `scripts/`, layers kit < project
### Source Files
- `cockpit/aurora_cockpit.py` - `agent_state`, `agent_write_env`, `agent_ping`, `agent_venv_install`, `backend_models`, `kinds_read`, `kinds_write`, `artifact_files`, `unfinished`, `corrections_state`, `ask_threads`, `ask_thread`, `card_text`, `_run_setup`, `_create_project`, `_write_tokens`, `_write_sources`, `_write_config`, `handler` POST routes
- `cockpit/ui/index.html` - `view-project`, `view-setup`, `view-ask`, `view-work`, `view-dev`

### Related Functions
- [Command Runner & Console](./04-command-runner-console.md) - agent settings surface in `/api/agent` payload used by the run flow
- [Git & Kit Maintenance](./06-git-kit-maintenance.md) - kit-level agent/token targets

## Implementation Notes

The agent env is *layered*: `agent_state` merges kit env then project env and reports which keys the
project itself sets (`own`) — otherwise a form would show an inherited value the user mistakes for their own.
`agent_write_env` guards `scope`: a project-scoped write without a project path, or a kit-scoped write
when a project is given, is refused so a "project edit" can never silently become a global one. Secrets
are only ever reported as present/absent.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*