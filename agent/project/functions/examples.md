# Examples Module

The **examples** module ("Examples") ships example usage scenarios and sample
configurations for the Aurora knowledge framework. It holds ready-made sample content
that mirrors Aurora's expected conventions, so integrators and agents can copy a
starting point instead of building a card or config from scratch.

Currently the module contains a single example: `examples/sample_card.md`, a "Example
process card" — a knowledge card with a complete frontmatter header (aliases, tags,
type, trust, ownership, provenance, audience) and a short body placeholder. It documents
the card format and the lifecycle expectations an Aurora card carries: until `status:
verified`, the content must be treated as a hypothesis, not a fact.

## Documents

- [Sample Card Example](./examples/01-sample-card.md) - the example process card and the frontmatter/card-layout convention it demonstrates

---
*Last updated: 2026-08-28*
*Areas: examples, aurora-vault*