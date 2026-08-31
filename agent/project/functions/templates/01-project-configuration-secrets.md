# Project Configuration & Secrets Templates
## Description
These two templates seed a new Aurora project's configuration. `aurora.config.yaml.template` is the **committed** configuration file a user copies to `aurora.config.yaml` — schema version 1 — holding the project's name/slug, the skills it requires and recommends, connected source modules, Atlassian sync settings, paths, verify/trust, privacy, analyst reports and bootstrap thresholds. `aurora.env.local.example` is the **gitignored** secrets file a user copies to `.env.aurora.local`, holding per-machine personal tokens for Confluence and Jira plus the optional embedded LLM agent backend chain.

## Key Features
- Committed project config with `{{PLACEHOLDER}}` substitutions (`{{PROJECT_NAME}}`, `{{PROJECT_SLUG}}`, `{{CONFLUENCE_SPACE}}`, `{{JIRA_KEY}}`, `{{YEAR}}`) that the user fills in.
- `skills.required` / `skills.recommended` lists; sources section mapping module IDs (Confluence, JIRA) to data-center modules and `Sources/` paths.
- Atlassian section: `confluence` (`base_url`, `space`, `sync_roots`), `jira` (`project_key`, `default_jql`, `done_statuses`, `cancelled_statuses`, `trust_statuses`, `assumption_statuses`), and `auth.mode: mcp_user`.
- Privacy `kb:scrub` modes `off` / `report` / `mask`, with `mask_contacts` and `include_raw` flags.
- Analyst report config under `reports.analyst` (`year`, `roster`, `events`, `data_dir`, `output`).
- Secrets template documents the exact variable list for Confluence and Jira PATs (`CONFLUENCE_PERSONAL_TOKEN` / `CONFLUENCE_PAT` / `CONFLUENCE_USER`+`CONFLUENCE_PASSWORD`, and the matching Jira trio) plus the agent backend ring (`AURORA_AGENT_*`) and embedding overrides (`AURORA_EMBED_MODEL` / `AURORA_EMBED_URL` / `AURORA_EMBED_KEY`).
- The secrets file is closed by `.gitignore`; base URLs / spaces / JQL live only in the committed config.

## Related Documentation
### Technical Details
- [Design doc](../../design/04-templates-layout-generation.md) - template organisation and placeholder substitution
### Source Files
- templates/aurora.config.yaml.template - committed project configuration template
- templates/aurora.env.local.example - gitignored per-machine secrets example
### Related Functions
- [Agent Guidance Templates](./02-agent-guidance-templates.md) - agent rules reference these config and secret files
- [Project Launchers](./03-project-launchers.md) - setup menu reconfigures Confluence/Jira/privacy

## Implementation Notes
`{{...}}` tokens in this cluster also appear in the launchers (`{{KIT_PATH}}`) and agents (`{{PROJECT_NAME}}`, `{{PROJECT_SLUG}}`) templates, so generation must substitute them consistently across the whole set. The secrets template stresses that tokens never live in the committed config or in skills — only in `.env.aurora.local` and Cursor MCP.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, templates*