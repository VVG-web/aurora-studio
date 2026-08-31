# Connector Manifest & Skill Layout Function

## Description
Every connector is a self-contained folder `connectors/<id>/` with two files, plus the
export script (for the built-in ones it lives in the kit's `scripts/`):

```
connectors/<id>/
├── connector.json     # JSON manifest: what the module is and how to run it
├── SKILL.md           # template for the project sync skill (placeholders {{PROJECT_SLUG}} and friends)
└── <script>.py       # export script (built-ins hold theirs in scripts/ of the kit)
```

The engine does not parse product markup — it only knows the two mirror *kinds* (`wiki` and
`board`) and serves any module that declares one. The manifest is **JSON, not YAML**, because
the engine reads it without an external parser. A new connector (Notion, SharePoint, YouTrack)
is added simply by dropping a folder into `connectors/`.

## Key Features
- **`kind`** — `wiki` or `board`; the engine picks the layout and the audit rules by it.
- **`mirror.default_path`** — the mirror folder; it becomes legitimate under `Sources/` so
  `kit:doctor --structure` stops treating it as ownerless.
- **`mirror.legacy_path_key`** — present only on the two built-ins (`sources_confluence`,
  `sources_jira`); enables them in projects that don't yet have a `sources:` section.
- **`run.command`** — the panel/registry command name (`sync:confluence`, `sync:jira`),
  which humans press rather than typing a script path.
- **`auth.env_prefix`** — the engine derives env var names from it (`CONFLUENCE_PAT`,
  `JIRA_PERSONAL_TOKEN`, `USER` + `PASSWORD` forms).
- **`settings_block`** — where in `aurora.config.yaml` the module finds its own settings
  (each product's settings are private; the registry does not interpret them).

## Related Documentation
### Technical Details
- [Confluence Connector](./01-confluence-connector.md) - the `wiki`-kind connector built on this layout
- [Jira Connector](./02-jira-connector.md) - the `board`-kind connector built on this layout
### Source Files
- connectors/confluence-dc/connector.json - manifest example (wiki kind)
- connectors/jira-dc/connector.json - manifest example (board kind)
- connectors/confluence-dc/SKILL.md - sync-skill template example
- connectors/jira-dc/SKILL.md - export-skill template example
- docs/connectors.md - engine-module split, wiring into a project, minimal export script

## Implementation Notes
Distribution (`aurora.py update <project> --apply`) copies the manifest into the project's
`.opencode/connectors/<id>.json`, the script into `.opencode/scripts/`, and the skill body
into existing skill folders (kit version lands beside as `.new` to avoid overwriting). A module
is enabled per project via the `sources:` list in `aurora.config.yaml` (`id`, `module`,
`path`), which is also what the panel's «Модули источников» writes. Verify the result with
`scripts/sources_registry.py`. Disabling a module removes it from `sources:` but never deletes
the mirror folder — data is never deleted by the engine.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, connectors*