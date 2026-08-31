# Knowledge Document Templates Function
## Description
The `scaffold/Templates/` set defines the canonical shape of every knowledge-base document and
knowledge card in an Aurora project. Filling one in is a guided exercise: instructions live in
`<!-- ИНСТРУКЦИЯ: … -->` comments, `{{placeholders}}` mark where content goes, and
`<!-- ОПЦИОНАЛЬНО: … -->` sections are dropped when not applicable. The result is uniform,
verifiable documentation that the engine and the sync skills can find and interpret.

## Key Features
- **User Story** (`user_story_template.md`) — full template: metadata table (Код/TJ.NDS.US-X.X.X,
  links to JIRA `TJNDS-{номер}`, `Реализует` REQ links), «Как – Я хочу – Чтобы»
  description, precondition status groups (✅/⚠️/❗), main/alternative scenarios with steps, acceptance
  criteria, backward-compatibility, and a `How to demo` section.
- **Acceptance Criteria** (`AC_template.md`) — an optional-section acceptance-criteria document: user-story
  table, then condition/trigger, generated-document contents, delivery channels, storage/availability,
  per-status result handling, business logic, error handling, logging/audit, optional detailed algorithm
  and cross-US dependencies, plus a related-requirements section.
- **Feature Specification (SDD)** (`spec_template.md`) — frontmatter (`SPEC-{{NNN}}`, `type: spec`,
  `implements`, `decisions`, `based_on`), then «Зачем», «Границы», behavior scenarios in
  Дано/Когда/Тогда EARS style, «Данные», «Интеграции», «Ограничения и решения», «НФТ»,
  «Открытые вопросы» and «Критерии приёмки».
- **Decision Record** (`decision_record_template.md`) — frontmatter, context, all discussed variants
  (including rejected, with reasons), the chosen decision, consequences, and history.
- **Requirement card** (`requirement_card_template.md`) — `REQ-{{NNN}}` card with
  `req_status` (stated → agreed → implemented | rejected), wording kept as close to the customer as
  possible, context, clarifications, and traceability.
- **Question card** (`question_template.md`) — `Q-NNN` card with `q_status` lifecycle
  (open → asked → answered | closed-no-answer), owner/channel/deadline, blocking links, hypotheses,
  and post-answer actions.
- **Meeting summary** (`meeting_summary_template.md`) — transcript-derived résumé: participants, agenda,
  agreements with quotes, stated/changed requirements, disagreements, action items, and knowledge extracted.
- **Acceptance report** (`acceptance_report_template.md`) — `type: acceptance` record of
  acceptance runs: per-item verdicts (`пройдено` · `не пройдено` …), customer remarks parsed
  into defect / new requirement / question / discrepancy, and a checklist of what to update in the base.
- **GOST user manual** (`shablon_rp_template.md`) — a large Russian-GOST-style manual skeleton
  (approval block, revision sheet, glossary, introduction, role model, distribution, step-by-step
  «Начало работы» with screenshots, emergency procedures).

## Related Documentation
### Source Files
- scaffold/Templates/user_story_template.md - User Story full template
- scaffold/Templates/AC_template.md - Acceptance Criteria template
- scaffold/Templates/spec_template.md - Feature Specification (SDD) template
- scaffold/Templates/decision_record_template.md - Decision Record template
- scaffold/Templates/requirement_card_template.md - REQ requirement card template
- scaffold/Templates/question_template.md - Q-NNN question card template
- scaffold/Templates/meeting_summary_template.md - meeting résumé template
- scaffold/Templates/acceptance_report_template.md - acceptance report template
- scaffold/Templates/shablon_rp_template.md - GOST-style user manual skeleton

### Related Functions
- [Workflow Prompts](./03-workflow-prompts.md) - the prompts that fill these templates
- [Project Setup Templates](./01-project-setup-templates.md) - the repo conventions these documents follow

## Implementation Notes
Frontmatter drives engine behaviour: `type` selects the section, `status` is the trust/completion
level, and the traceability fields (`based_on`, `implements`, `covers`, `supersedes`, `blocks`)
link cards into the knowledge graph. The GOST manual is generated from `Raw/examples/shablon_rp.md` and
relies on heavy HTML table markup, unlike the markdown-first analyst templates.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, scaffolding*