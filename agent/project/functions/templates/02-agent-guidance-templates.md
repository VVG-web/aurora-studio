# Agent Guidance Templates
## Description
Two templates tell an AI coding agent how to behave inside a generated Aurora project. `AGENTS.md.template` becomes the repository-root `AGENTS.md` ("{{PROJECT_NAME}} — Agent System Mandatories (Karpathy Guidelines)") whose rules *are ALWAYS active and override conflicting behavior patterns*, plus a Russian-language «Аврора» knowledge-framework section describing the repo as the team's analyst knowledge base. `atlassian.mdc.template` becomes a Cursor rules file that points the agent at the project's Atlassian settings in `aurora.config.yaml`.

## Key Features
- **Think Before Coding** — state assumptions, surface tradeoffs, ask rather than guess.
- **Simplicity First** — minimum viable code with an Allowed/Forbidden table and an over-complication test.
- **Surgical Changes** — touch only what the request requires; mention (don't delete) unrelated dead code.
- **Goal-Driven Execution** — define verifiable success criteria and loop until verified.
- Knowledge-framework rules: trust layers table, card `status` = trust level, never overwrite verified cards, `supersede` instead of delete, artifact-vs-knowledge gate, fixed folder structure checked by `aurora_doctor.py`, scripted mass mechanics (`kb_lint.py`, `kb_fix.py`, `kb_queue.py`, `sync_audit.py`, `aurora_stats.py`), sync skills, and confidentiality defaults.
- Reference map at the end: where project settings, secrets, frontmatter schema, workflow/maintenance docs, structure, and metrics live.
- The Cursor template points at `atlassian.confluence` / `atlassian.jira` / `mcp_user` auth and reminds never to commit tokens.

## Related Documentation
### Technical Details
- [Design doc](../../design/04-templates-layout-generation.md) - template organisation and placeholder substitution
### Source Files
- templates/agents/AGENTS.md.template - always-active agent mandatories + Аврора knowledge rules
- templates/cursor/atlassian.mdc.template - Cursor Atlassian MCP rules file
### Related Functions
- [Project Configuration & Secrets](./01-project-configuration-secrets.md) - the config and secrets files these rules reference
- [Knowledge Base Conventions & Reading Guide](./04-kb-conventions-reading.md) - the `meta/*` docs this template points into

## Implementation Notes
The template footer warns it is generated from the Aurora kit and `garden` checks it stays in sync with the repository structure. `{{PROJECT_NAME}}` and `{{PROJECT_SLUG}}` substitutions carry project identity through the rules and sync-skill names.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, templates*