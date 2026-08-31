# Connectors Module

The Connectors module ships the built-in integration connectors for the Aurora framework —
the purpose is: read external systems into mirrored, git-stable folders under
`Sources/`. Two connectors ship with the kit and are always installed: Confluence Data Center
(`confluence-dc`, a `wiki`-kind mirror of a page tree) and Jira Data Center (`jira-dc`,
a `board`-kind mirror of issues). Any further system — Notion, SharePoint, YouTrack — is
added the same way, as a folder under `connectors/`.

Each connector is a self-contained folder with two files: `connector.json`, a JSON manifest
that tells the engine what the module is and how to run it, and `SKILL.md`, a template for
the project sync skill (filled with placeholders like `{{PROJECT_SLUG}}`). The engine
itself does not know Confluence or Jira at all: it only knows the two mirror *kinds*
(`wiki` and `board`) and serves any module that declares one. The export scripts live in the
kit's `scripts/` (`confluence_export.py`, `jira_export.py`) and user-facing commands in the
panel are `sync:confluence` and `sync:jira`.

## Documents

- [Confluence Connector](connectors/01-confluence-connector.md) - Confluence Data Center wiki mirror: manifest, settings, hard rules
- [Jira Connector](connectors/02-jira-connector.md) - Jira Data Center board mirror: manifest, settings, export + audit workflow
- [Connector Manifest & Skill Layout](connectors/03-connector-manifest-skill-layout.md) - connector.json schema, SKILL.md template, repo layout

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, connectors*