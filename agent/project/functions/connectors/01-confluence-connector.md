# Confluence Connector Function

## Description
The Confluence Data Center connector mirrors a space's page tree into a git-stable folder
`Sources/Confluence/` (the `wiki` kind). Pages are fetched via the Rest API
(`scripts/confluence_export.py`) — nothing is installed on the server. The mirror is written
**only** by the script, never by a model or by hand: conversion is code, so the same page
always produces byte-for-byte the same file. The user-facing command (what the panel and the
registry call it) is `sync:confluence`. On top of the deterministic mirror, the **MCP**
(read-only) skill offers ad-hoc reads: look at a page or its version, find a page by
title/CQL, read customer comments.

The connector is declared by `connectors/confluence-dc/connector.json` (`id: "confluence-dc"`,
server/data-center variant since `1.28.0`) and its `connectors/confluence-dc/SKILL.md` is
a `confluence-sync-{{PROJECT_SLUG}}` skill template. It works against data-center REST paths
(`/rest/api/content/...`), so Cloud and Server are covered by the same code.

## Key Features
- **Deterministic wiki mirror** — page tree laid out as folders; a page with children becomes a
  folder plus `index.md`; files and headers carry no export date or version (no spurious git diffs).
- **`--verify` determinism gate** — export twice into temp folders and byte-compare; `--force`
  rebuilds the whole mirror; `--prune` removes mirrors of deleted pages.
- **Macro & link preprocessing** — Requirement Yogi (`RYk`/`RYl`/`RYo`/`RYr` marks),
  mermaid and draw.io diagrams (downloading source attachments), excerpts, Jira issue links,
  tables (markdown where expressible, clean HTML otherwise).
- **Sync roots** — `page_id` of roots to walk from, configured in `aurora.config.yaml`;
  nested roots are dropped so a page can't land twice in the mirror.
- **Read-only MCP access** — look up pages/versions/titles/CQL and read comments without
  touching the mirror.

## Related Documentation
### Technical Details
- [Connector Manifest & Skill Layout](./03-connector-manifest-skill-layout.md) - connector.json schema and the SKILL.md template
### Source Files
- connectors/confluence-dc/connector.json - manifest: kind, mirror, run, auth, settings
- connectors/confluence-dc/SKILL.md - sync-skill template (ad-hoc read + hard rules)
- scripts/confluence_export.py - the exporter: API client, conversion, tree walk, main
### Related Functions
- [Jira Connector](./02-jira-connector.md) - the `board`-kind sibling mirror

## Implementation Notes
Product-side lives in `scripts/confluence_export.py`; the shared half (state file, extra-file
search, `--prune`, determinism `--verify`) comes from `sources_core.py` (`RestApi`, `WikiMirror`).
Settings are read from the `confluence:` block of `aurora.config.yaml` (`base_url`, `space`,
`sync_roots`), secrets from `CONFLUENCE_PAT` / `CONFLUENCE_USER` + `CONFLUENCE_PASSWORD`
(env or `.env.aurora.local`). Runtime deps: `beautifulsoup4` and `markdownify`
(`pip install beautifulsoup4 markdownify`). The mirror's git-residue health is checked
afterwards with `scripts/sync_audit.py`.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, connectors*