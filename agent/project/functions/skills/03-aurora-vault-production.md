# Aurora Vault — Retrieval & Production Function

## Description

This cluster covers the retrieval policy and the analyst-artifact production workflow of the
aurora-vault skill. Retrieval decides how `AuroraKnowledgeDB` cards are fed into the LLM
context for any enriched task; production turns knowledge into analyst artifacts (US, AC, specs),
deliverable documents, and outward publication. The context pack is assembled by a script
(`ctx_pack.py`), never by the model "from memory", so the trust headers, status/release
filters and the usage log are applied mechanically on every run.

## Key Features

- **`ctx:context <тема>` (`context`)** — context pack: selection, status filter, trust
  headers, `usage.log` (`ctx_pack.py`); modes `generate|review|ask|evaluate`, plus
  `--budget`.
- **`ctx:ask <вопрос>` (`ask`)** — answer from the base with citations; "почему / почему
  не X" includes deprecated cards and rejected DRs.
- **`ctx:eval` (`eval`)** — regression run of golden questions against the golden answers.
- **`ctx:retro <событие>` (`retro`)** — learned lessons: what the base did not know when
  we were wrong.
- **`make:create <тип>` (`create`)** — generate an artifact into `Artifacts/<тип>/`; only
  standard types (closed list).
- **`make:kinds`** — project artifact registry: template, folder, prompt, "без технологий"
  rule, clean-copy boundary (read via MCP `artifact_spec`).
- **`make:review <US/AC>` (`review`)** — quality check of an artifact against the knowledge base.
- **`make:spec <тема>` (`spec`)** — build a feature spec from REQ and `knowledge` cards
  (SDD).
- **`make:spec-pack <SPEC-NNN>` (`spec-pack`)** — spec bundle for external development
  (`spec_pack.py`), built with the Definition-of-Ready gate checked mechanically.
- **`make:validate <SPEC> <объект>` (`validate`)** — reconcile contractor implementation/tests
  against the spec scenarios.
- **`make:assemble <документ>` (`assemble`)** — assemble a deliverable document
  (ОПЗ/ПМИ/РП) from the base per template.
- **`ship:publish <артефакт>`** — artifact → generated Confluence page (`publish_doc.py`);
  knowledge cards never go outward.
- **`ship:export <документ>`** — deliverable → docx/pdf (pandoc, branded template).
- **`ship:acceptance <объект>`** — record acceptance/trials and triage customer remarks.
- **`ship:release <документ>` (`release`)** — freeze the delivered version (snapshot + commit
  + date).
- **`ops:trace` / `ops:trace-table` / `ops:todo` / `ops:questions` / `ops:retrieval`
  / `ops:report`** — traceability, task list, question registry, ranking guard, analyst
  dashboard.
- **`agent:make`** — the primary artifact production path: enrichment → plan with questions →
  worker → critic → verifier ("Момус").

## Related Documentation

### Technical Details
- [Skills Support Files Design](../../design/02-skills-support-files.md) - skill layout and registration

### Source Files
- skills/aurora-vault/SKILL.md - main skill, command registry and invariants
- skills/aurora-vault/references/workflows.md - command procedures for ingest, trace, spec, review, decide
- skills/aurora-vault/references/retrieval.md - retrieval policy, bootstrap mode, card header format

### Related Functions
- [Extraction & Lifecycle](./01-aurora-vault-extraction.md) - produces the cards retrieval draws from
- [Sources & Maintenance](./02-aurora-vault-maintenance.md) - keeps the base healthy before production

## Implementation Notes

A key invariant is so-called "kitchen never goes outward": analyst-only sections («Уточнения»,
«Допущения», «Под вопросом») live in the artifact body under the exact line
`<!-- ниже — производство, в чистовик не идёт -->` and publication cuts the document at that
marker (matched by exact string, no indent). Requirement cards are replaced only with a delta:
`kb:supersede` refuses a `type: requirement` card without `--changed` and `--migration`,
because the moment of replacement is the only one when the person still remembers the answer.

---
*Last updated: 2026-08-28*
*Areas: skills, aurora-vault, production*