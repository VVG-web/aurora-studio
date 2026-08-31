# Jira Connector Function

## Description
The Jira Data Center connector mirrors issues into a git-stable folder `Sources/JIRA/` (the
`board` kind): one markdown file per issue, filename = issue key (`PRJ-1182.md`). It is
declared by `connectors/jira-dc/connector.json` (`id: "jira-dc"`, since `1.28.0`) and
its `connectors/jira-dc/SKILL.md` is a `jira-export-{{PROJECT_SLUG}}` skill template.
The user-facing command is `sync:jira`, backed by `scripts/jira_export.py`.

Unlike Confluence, the Jira mirror is still produced **by the model** running the skill — a
deterministic script is planned but not yet present. The skill therefore emphasises running
`scripts/sync_audit.py` after export and not rewriting files when only formatting changed (to
avoid the "diff without change" drift). The `connector.json` already declares the future
`scripts/jira_export.py` as its `run.script`.

## Key Features
- **`board`-kind mirror** — flat list of issues, one file per issue key, stable across runs.
- **Source-of-truth in `aurora.config.yaml`** — reads `atlassian.jira.base_url`,
  `atlassian.jira.project_key`, `atlassian.jira.default_jql`.
- **State keeps accumulating** — state lives in `Sources/JIRA/update_log.md`; a narrow query
  does not throw out the rest of the mirror from the previous run.
- **Auth** — Cursor MCP using your account, or `JIRA_PAT` / `JIRA_USER` + `JIRA_PASSWORD`
  from `.env.aurora.local`; tokens are never committed to git.
- **Hard data rules** — only real Jira data, write only under `Sources/JIRA/`, never invent issues.

## Related Documentation
### Technical Details
- [Connector Manifest & Skill Layout](./03-connector-manifest-skill-layout.md) - connector.json schema and the SKILL.md template
### Source Files
- connectors/jira-dc/connector.json - manifest: kind, mirror, run, auth, settings
- connectors/jira-dc/SKILL.md - export-skill template (workflow + hard rules)
- scripts/jira_export.py - the (declared) exporter: REST client, JQL, board mirror
### Related Functions
- [Confluence Connector](./01-confluence-connector.md) - the `wiki`-kind sibling mirror

## Implementation Notes
Product-side in `scripts/jira_export.py`; the shared half comes from `sources_core.py`
(`BoardMirror`, `RestApi`, `cited_by_cards`, `verify`). Config uses the `jira:` block and a
legacy `sources_jira` path key (`mirror.legacy_path_key`), mirror default path
`Sources/JIRA`, state file `update_log.md`. Dedicated CLI flags seen in the exporter include
`--jql`, `--force`, `--comments` and `--verify`. The planned deterministic script will remove
the model-written-mirror caveat.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, connectors*