# Scaffolding Architecture Design
## Overview
The Scaffolding module is a **pure asset layer**: it contains no executable code, only files that get
materialised into projects. Its two source roots play different roles. `templates/` (project scope)
holds the skeleton of a *new repository*. `scaffold/` (document scope) holds the analyst-facing
templates and prompts used *for the lifetime of a project* to produce knowledge documents.

## Two-tier structure
**Project bootstrapping (templates/).** The committed welcome-pack: `aurora.config.yaml.template`
plus the gitignored secrets example, the AGENTS.md mandatories, the Cursor Atlassian hint, the
per-OS launchers, and the `meta/` file that teaches a first reader how the base works. Placeholder
variables (`{{PROJECT_NAME}}`, `{{PROJECT_SLUG}}`, `{{KIT_PATH}}`, `{{CONFLUENCE_SPACE}}`,
`{{JIRA_KEY}}`, `{{YEAR}}`) are substituted when a repository is created.

**Document production (scaffold/).** A matrix of templates × prompts. Prompts in `scaffold/Prompts/`
reference the matching template in `scaffold/Templates/` through their `template:` frontmatter field and
state the output directory and equivalent skill:

```
---
title: "Промпт: написать критерии приёмки"
template: Templates/AC_template.md
output: Artifacts/ac/
skill: "/aurora-vault make:create ac"
---
```

This triple (template → output → skill) is the architectural contract: the manual prompt is intentionally
kept equivalent to the automated `/aurora-vault` skill so that a human can reproduce or understand any
skill-produced document. Templates in turn front-load their instructions as `<!-- ИНСТРУКЦИЯ: … -->`
comments and use `{{placeholders}}`, with optional sections removed when not applicable — keeping one source
of truth for a document shape while allowing per-project trimming.

## Placeholder & naming discipline
- Committed constants live in `aurora.config.yaml`; personal credentials only in the gitignored
  `.env.aurora.local` (see `aurora.env.local.example`). No token ever lands in this module or in
  skills.
- Card/artifact naming is governed by `templates/meta/conventions.md` (REQ/DR/SPEC/Q prefixes,
  `YYYY-MM-DD_<type>_<object>.md` artifacts, PascalCase folders, flat unique files) and the fixed
  folder structure checked by `aurora_doctor.py --structure`.

## Relationship to other modules
The Scaffolding assets define the conventions and document shapes that the Cockpit, Connectors and Skills
modules operate on: skills (`/aurora-vault …`) and sync skills mirror the prompts, and generated
documents follow these templates so the engine can parse and verify them.

## Implementation Notes
Being asset-only, the module has no runtime dependencies; correctness is enforced by the consuming engine
(`aurora.py` bootstrap, `garden` checks template↔repo correspondence, doctor structure checks) rather
than by this module itself.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, scaffolding*