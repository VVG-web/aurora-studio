# Project Setup Templates Function
## Description
Ready-made files that bootstrap the *shape* of a new Aurora project repository. They are the
skeleton a fresh team copies/materials so that every Aurora project — across many teams — has the same
committed config, the same secrets convention, the same agent rules and the same way to launch the
toolchain, regardless of operating system.

## Key Features
- **Committed project config.** `templates/aurora.config.yaml.template` is the schema-v1 project
  constants file: `project.name` / `project.slug`, `skills.required` / `skills.recommended`,
  `sources` (Confluence and JIRA modules), the `atlassian.confluence` / `atlassian.jira` /
  `atlassian.auth` blocks, `paths` (including `knowledge_db: AuroraKnowledgeDB`), `verify`
  trusted sources/sections, `privacy.scrub`, `reports.analyst`, and `bootstrap.verified_threshold_pct`.
- **Local secrets, kept out of git.** `templates/aurora.env.local.example` documents the env variables
  scripts need (`CONFLUENCE_PERSONAL_TOKEN`, `JIRA_PERSONAL_TOKEN`, …), copied to a gitignored
  `.env.aurora.local`. It also documents the agent/LLM and embedding chain configuration
  (`AURORA_AGENT_ADAPTER`, `AURORA_AGENT_BACKEND_1_URL`, `AURORA_EMBED_MODEL`, …).
- **Agent system mandatories.** `templates/agents/AGENTS.md.template` ships the Karpathy-style rules
  (think before coding, simplicity first, surgical changes, goal-driven execution) plus the Aurora
  knowledge-framework invariants: trust layers table, the `aurora-vault` skill as the single
  management tool, no-overwrite/no-delete rules, and the fixed repo structure check via
  `aurora_doctor.py --structure`.
- **Cursor Atlassian cheat-sheet.** `templates/cursor/atlassian.mdc.template` points the agent at
  `aurora.config.yaml` `atlassian.*` and reminds it to use the MCP user login, never committed tokens.
- **Native launchers.** `templates/launchers/start-aurora.command` (macOS/Linux shell) and
  `templates/launchers/start-aurora.bat` (Windows) present a menu to run doctor, stats, setup,
  cockpit, restart, and the command reference — locating the kit via a `{{KIT_PATH}}` hint then
  `$HOME/aurora-studio` then the sibling folder.
- **Meta orientation files.** `templates/meta/READING.md` explains how to read the knowledge base
  (card anatomy, YAML frontmatter fields, trust/status semantics), and `templates/meta/conventions.md`
  sets repo-wide naming, folder and artifact-taxonomy rules.

## Related Documentation
### Source Files
- templates/aurora.config.yaml.template - committed project config, schema v1
- templates/aurora.env.local.example - gitignored secrets and agent/embedding env
- templates/agents/AGENTS.md.template - agent mandatories + Aurora knowledge rules
- templates/cursor/atlassian.mdc.template - Cursor MCP Atlassian reminder
- templates/launchers/start-aurora.command - macOS/Linux launcher menu
- templates/launchers/start-aurora.bat - Windows launcher menu
- templates/meta/READING.md - how to read the knowledge base
- templates/meta/conventions.md - naming, folder and artifact rules

### Related Functions
- [Knowledge Document Templates](./02-knowledge-document-templates.md) - the documents those projects produce
- [Workflow Prompts](./03-workflow-prompts.md) - prompts for producing those documents

## Implementation Notes
All files use `{{PLACEHOLDER}}` substitution (e.g. `{{PROJECT_NAME}}`, `{{PROJECT_SLUG}}`,
`{{KIT_PATH}}`, `{{YEAR}}`) for templating at materialization time. `aurora.env.local.example` is
the only file where auth is expected; addresses/spaces deliberately stay in `aurora.config.yaml` so it
stays a single team-wide source of truth. Launchers fall back across candidate kit paths to stay
portable between machines.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, scaffolding*