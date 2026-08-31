# Workflow Prompts Function
## Description
The `scaffold/Prompts/` set are copy-paste prompts for an assistant (or LLM harness) that turn the
templates into finished documents. Each prompt is the **manual analogue of a skill command** — the
frontmatter names the skill (`/aurora-vault make:create ac`, `/aurora-vault decide`,
`/aurora-vault spec`, `/aurora-vault ingest-meeting`, `/aurora-vault review`, …), the template to
fill (`template:`), and the output folder. Analysts paste in their {{…}} parameters plus a context pack
from the knowledge base so the model reasons from verified cards, not guesses.

## Key Features
- **Create User Story** (`US_create.md`) — full history prompt to `Artifacts/us/`, naming the
  release-steps (`ctx_pack.py`), `verified`-only grounding, glossary terms, observable «когда X,
  система должна Y» wording, and separation of criteria into a dedicated AC step.
- **Create & Update Acceptance Criteria** (`AC_new.md`, `AC_update.md`) — `AC_new` writes verifiable
  criteria («дано… когда… тогда…») to `Artifacts/ac/`; `AC_update` produces a delta
  (было/стало/причина) plus the full refreshed text, and surfaces contradictions to resolve in the base
  before touching criteria.
- **Decision Record** (`DR_create.md`) — builds an ADR (`Artifacts`… output
  `AuroraKnowledgeDB/Decisions/`), enumerating ALL discussed variants with rejection reasons, linking a
  superseded DR, and taking the next free number from `Decisions/_index.md`.
- **Feature Specification** (`SPEC_create.md`) — assembles an SDD into `AuroraKnowledgeDB/Specs/`
  with EARS/Дано-Когда-Тогда scenarios, `based_on` only from the attached context pack, open
  questions as blockers, and a Definition-of-Ready self-check.
- **Review US/AC against the base** (`US_review_with_KB.md`) — a leading-analyst verdict
  (готова / доработать / переписать) checking terminology, referential integrity, contradictions,
  template completeness and unambiguous wording, output to `Artifacts/reviews/`.
- **Meeting transcript ingest** (`meeting_ingest.md`) — summarises a customer meeting transcript into
  `Artifacts/meetings/`, requires quote-backed agreements, and extracts DR / REQ / fact *candidates*
  separately from open (non-agreed) questions.

## Related Documentation
### Source Files
- scaffold/Prompts/US_create.md - create a user story
- scaffold/Prompts/AC_new.md - create acceptance criteria
- scaffold/Prompts/AC_update.md - update acceptance criteria
- scaffold/Prompts/DR_create.md - formalise a decision record
- scaffold/Prompts/SPEC_create.md - assemble a feature spec (SDD)
- scaffold/Prompts/US_review_with_KB.md - review a US/AC against the base
- scaffold/Prompts/meeting_ingest.md - ingest a meeting transcript

### Related Functions
- [Knowledge Document Templates](./02-knowledge-document-templates.md) - the templates these prompts fill
- [Project Setup Templates](./01-project-setup-templates.md) - the conventions prompts enforce

## Implementation Notes
Prompts carry their wiring in YAML frontmatter (`template`, `output`, `skill`) which matches the
pairing used by the `aurora-vault` skill, so a prompt stays in sync with its automated twin. All
prompts insist on a context pack and forbid inventing facts: unknown material becomes an explicit открытый
вопрос rather than an assumed truth.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, scaffolding*