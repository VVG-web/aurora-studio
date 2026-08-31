# Aurora Vault — Sources & Maintenance Function

## Description

This cluster covers the mirroring and mechanical-maintenance commands of the aurora-vault
skill: deterministic exports from external systems into `Sources/`, integrity checks, repair,
and the various "garden" procedures that keep the knowledge base mechanically healthy. The
governing rule of the whole set is **"script first, then judgement"**: every mass operation
has a deterministic script in `.opencode/scripts/`, and the model runs the script and
interprets its report rather than walking the database itself. All scripts are dry-run by
default; writes require an explicit `--apply`.

## Key Features

- **`sync:sources`** — which source modules are installed and which mirrors are connected
  (`sources_registry.py`).
- **`sync:confluence`** — deterministic Confluence mirror → `Sources/Confluence/` (module
  `confluence-dc`, view `wiki`). Pure REST client; no plugin on the server.
- **`sync:jira`** — deterministic Jira mirror → `Sources/JIRA/` (module `jira-dc`, view
  `board`); `--jql`, `--comments`, `--verify`.
- **`sync:jira-status`** — reverse flow: task statuses → candidates for `req_status`
  (`jira_status.py`).
- **`sync:audit`** — mirror integrity: MISSING / ORPHAN / MOVED / COLLISION / STALE.
- **`sync:diff` (`diff`)** — drift: source changed after verification (hash comparison).
- **`kb:repair`** — fix broken links, homoglyphs, legacy frontmatter, out-of-schema
  fields, stubs for link targets (`kb_fix.py`); separate modes via flags (`--retire`,
  `--sections`, `--names`, `--links`, `--stubs`, `--aliases`, `--frontmatter`).
- **`kb:lint`** — mechanical database errors: links, frontmatter, card types, artifacts in
  knowledge, secrets, cards without links.
- **`kb:index`** — regenerate `_index.md` section indices (hand-written ones untouched).
- **`kb:scrub`** — personal data: find and mask with markers (`kb_scrub.py`); mode from
  `privacy.scrub` in config (off / report / mask).
- **`kb:schema`** — card schema version and migration between versions (`kb_schema.py`).
- **`kb:supersede` (`supersede`)** — replace knowledge preserving history: deprecated →
  `_archive`, links rewritten (`kb_supersede.py`); requirements need a delta (`--changed`,
  `--migration`).
- **`kb:reset` (`reset`)** — wipe and rebuild the base (`kb_reset.py`).
- **`kit:hooks`** — two git hooks: pre-commit ratchet linter and commit-msg privacy check.
- **`ops:` reporting** — `ops:stats`/`status` (database health dashboard) and `ops:impact`
  (`--explain <документ>`).

## Related Documentation

### Technical Details
- [Skills Support Files Design](../../design/02-skills-support-files.md) - skill layout and registration

### Source Files
- skills/aurora-vault/SKILL.md - main skill, command registry and invariants
- skills/aurora-vault/references/maintenance.md - mechanical procedures (repair, sync, audit, scrub)
- skills/aurora-vault/references/migration.md - new-project bootstrap and mirror migration

### Related Functions
- [Extraction & Lifecycle](./01-aurora-vault-extraction.md) - builds the cards these commands maintain

## Implementation Notes

Determinism is the point of the mirroring scripts: when an LLM writes markdown, the same page
is exported slightly differently each run, so git shows a change where there is none and syncs
stop being run. `confluence_export.py` converts by code for byte-identical output; headers
deliberately carry no export date, so a run does not touch unchanged files. Snapshot invariants:
delivered documents are set once (`ship:release`) and are immutable; the scrubber never edits
evidence (`Raw/`, `Sources/`, `Deliverables/released/`).

---
*Last updated: 2026-08-28*
*Areas: skills, aurora-vault, maintenance*