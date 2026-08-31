# Skills Module

The `skills` module packages the agent skills that ship with Aurora Studio. A skill is a
self-contained folder with a `SKILL.md` (the operative instructions), a `skill.json`
(registration metadata — name, description, entrypoint) and, where needed, `references/`
documents with the detailed procedures loaded on demand (progressive disclosure). Each skill
is registered into the agent's shared catalog (`~/.claude/skills`) via the `kit:skills`
command so it can be invoked from any dialog under its `/aurora-*` name.

The module contains three skills:

- **aurora-vault** — operates the Aurora knowledge framework: a Zettelkasten knowledge base
  (`AuroraKnowledgeDB`) built from project sources, a built-in LLM agent that parses sources
  into cards and distils them, trust computed from Jira task statuses, Decision Records, and
  the production of analyst artifacts (US, AC, specs).
- **aurora-dev** — the development kitchen for the engine itself: run test scenarios with a
  recorded journal, cover changes with autotests or QA cases. Kit-only; not for projects
  built on Aurora.
- **aurora-grill** — decomposes an intent along a decision tree: rounds of numbered questions
  with recommended answers until no silent assumptions remain. Used by the planner before
  producing an artifact.

## Documents

- [Aurora Vault — Extraction & Lifecycle Function](./skills/01-aurora-vault-extraction.md) - building cards from sources, the built-in agent, and the card trust/lifecycle model
- [Aurora Vault — Sources & Maintenance Function](./skills/02-aurora-vault-maintenance.md) - mirroring Confluence/Jira, repair, dedupe, lint, and mechanical maintenance commands
- [Aurora Vault — Retrieval & Production Function](./skills/03-aurora-vault-production.md) - context packs, knowledge use, and analyst artifact production commands
- [Aurora Dev QA Function](./skills/04-aurora-dev-qa.md) - engine test scenarios, QA cases, and release gates
- [Aurora Grill Function](./skills/05-aurora-grill.md) - intent decomposition by decision-tree rounds
- [Skills Support Files Design](../design/02-skills-support-files.md) - skill.json, SKILL.md, references layout, and the kit-only boundary

---
*Last updated: 2026-08-28*
*Areas: skills, aurora-vault, aurora-dev, aurora-grill*