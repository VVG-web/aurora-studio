# Knowledge Base Conventions & Reading Guide
## Description
Two templates seed the `meta/` folder of a generated project's knowledge DB and teach a reader how that DB works. `conventions.md` (Knowledge DB Conventions) is the per-project contract: naming, tags, repository-wide naming rules, `Sources/` handling, the working-spaces policy, and the closed artifact-type taxonomy. `READING.md` ("Как читать эту папку") is the onboarding guide for an assistant or harness that opens the folder for the first time, explaining the file kinds, YAML frontmatter, card anatomy, search paths, and what must never be edited by hand.

## Key Features
- **Naming** — hyphenated card filenames and artifacts `YYYY-MM-DD_<type>_<object>.md`; infrastructure folders Latin PascalCase, content folders lowercase; forbidden chars and case-only collision rules; `_` prefix reserved for service paths.
- **Tag taxonomy** — `{domain}.{subdomain}.{concept}` (e.g. `process.workflow`, `system.integration`, `role.actor`, `req.trace`).
- **Hard repository rules** — docs closed by `.gitignore` may be off-schema; everything committed must follow the fixed structure or live under `Workspaces/<task>/`.
- **Closed artifact taxonomy** — the authoritative list: `us/`, `ac/`, `algorithms/`, `dictionaries/`, `screens/`, `contracts/`, `mappings/`, `role-model/`, `diagrams/`, `acceptance/` — with the "artifact = document result" vs "knowledge = distilled truth" split and the «diagram as code» invariant.
- **Reading guide** — three file kinds (knowledge card / map `MOC` / section `_index.md`), YAML frontmatter semantics (`title`, `type`, `status`, `kind`, `source`, `trust`, `trust_basis`, plus lifecycle fields), card body anatomy (thesis, «Под вопросом», verbatim «Источник», «История изменений»), and trust tiers from `status: knowledge` down to `deprecated`.
- **What is forbidden** — editing `MOC/`/`_index.md`, hand-setting `status`/`trust`/`trust_basis`, overwriting `kind: dictionary`/`document` cards, deleting cards (use `supersede`), and writing via the read-only MCP server.

## Related Documentation
### Technical Details
- [Design doc](../../design/04-templates-layout-generation.md) - template organisation and placeholder substitution
### Source Files
- templates/meta/conventions.md - per-project knowledge DB conventions
- templates/meta/READING.md - how-to-read-this-folder onboarding guide
### Related Functions
- [Agent Guidance Templates](./02-agent-guidance-templates.md) - AGENTS.md mandates agents to obey these conventions

## Implementation Notes
Both files are engine-generated and regenerated with the kit; the reading guide's footer warns that per-project agreements must be written in `conventions.md`, not in the guide itself. The conventions file is the source of truth for the closed artifact-type list and cross-references `aurora_doctor.py --structure`.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, templates*