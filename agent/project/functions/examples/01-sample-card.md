# Sample Card Example

## Description

`examples/sample_card.md` is a template knowledge card that demonstrates the card format
Aurora uses. It is a single "process" card whose body is a placeholder for real knowledge,
shipping with a complete YAML frontmatter block that models the fields an integrated card
is expected to carry. Users copy this file and replace the placeholder with real content.

## Key Features

- **Frontmatter header** — a YAML block between `---` fences defining card metadata:
  `title` ("Example process card"), an `aliases:` list (`["example-process"]`), a `type`
  of `process`, a `status` of `imported`, a `trust` of `medium`, `created`/`updated`
  provenance dates, `source: "Raw/examples/readme"`, `source_synced`, and an `audience`
  of `[SA, BA]`.
- **Placeholder body** — the markdown body instructs readers to replace it with real
  knowledge and, until `status: verified`, to treat the content as a hypothesis rather
  than a fact.
- **Provenance & lifecycle fields** — ownership, verification (`verified`, `review_by`)
  and `related:` linkage fields are left blank/defaulted, showing the fields an
  integrated card fills in during its lifecycle.

## Related Documentation

### Technical Details

- [Aurora Vault — Extraction & Lifecycle](../../design/02-skills-support-files.md) - card frontmatter and lifecycle conventions in the vault skill

### Source Files

- examples/sample_card.md - the example process card with frontmatter and placeholder body

### Related Functions

- [Aurora Vault — Extraction & Lifecycle Function](../skills/01-aurora-vault-extraction.md) - how real cards are extracted and given trust/lifecycle status

## Implementation Notes

The example is intentionally inert: it carries no executable logic, only a frontmatter
contract and a placeholder body. It is the reference shape the aurora-vault extraction
procedures (`references/frontmatter.md` schema) are expected to produce, and is used as a
copy source for hand-written or demo knowledge cards.

---
*Last updated: 2026-08-28*
*Areas: examples, aurora-vault*