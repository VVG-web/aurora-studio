# Scaffolding Module

The **Scaffolding** module supplies the raw material for bootstrapping and growing an Aurora
knowledge-base project: ready-made **project files** (in `templates/`) that get dropped into a
new repository, **document and knowledge-card templates** (in `scaffold/Templates/`) that analysts
fill in by hand, and **workflow prompts** (in `scaffold/Prompts/`) that drive an assistant
through producing those documents.

Project files (`templates/`) cover the committed config `aurora.config.yaml` and the gitignored
secrets file `.env.aurora.local`, OS-native launcher scripts that open the doctor/stats/setup/
cockpit toolchain from the project folder, an `AGENTS.md` that lays down the Karpathy-style agent
mandatories plus the Aurora knowledge rules, a Cursor MCP cheat-sheet for Atlassian, and the two
`meta/` orientation files a fresh analyst needs (`READING.md` and `conventions.md`).

Document templates and prompts are paired: each `scaffold/Prompts/*.md` names the template it
fills (`template:` frontmatter field), the output folder, and the skill command it is the manual
analogue of (`/aurora-vault make:create us`, `/aurora-vault decide`, …). Together they cover user
stories, acceptance criteria, specs, decision records, requirement cards, questions, meeting summaries,
acceptance reports, and a GOST-style user manual.

## Documents

- [Project Setup Templates](scaffolding/01-project-setup-templates.md) - committed config, local secrets, AGENTS.md, cursor rule, launchers, meta conventions
- [Knowledge Document Templates](scaffolding/02-knowledge-document-templates.md) - US, AC, spec, DR, requirement, question, meeting, acceptance, manual
- [Workflow Prompts](scaffolding/03-workflow-prompts.md) - assistant prompts for creating and reviewing the documents

## Design

- [Scaffolding Architecture](../design/03-scaffolding-architecture.md) - templates, placeholders, prompt-template-skill wiring

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, scaffolding*