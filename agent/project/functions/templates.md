# Templates Module

The **templates** module ("Templates") ships the reusable, placeholder-filled scaffolding that the Aurora Studio kit generates into each new Aurora project. Every template uses `{{PLACEHOLDER}}` substitutions that the keeper fills in when a project is created (or the user edits after generation), and together the eight files establish the project's configuration, secrets plumbing, agent guidance, double-click launchers, and knowledge-base conventions.

Two files configure the project: `templates/aurora.config.yaml.template` (the committed project config — schema version 1, holds Atlassian space/JQL/sync roots, trust, privacy, reports and bootstrap settings) and `templates/aurora.env.local.example` (the gitignored `.env.aurora.local` secret file — Confluence/Jira personal tokens and the built-in LLM agent backend chain). Two files guide agents: `templates/agents/AGENTS.md.template` (the always-active "Agent System Mandatories" plus the «Аврора» knowledge-framework rules) and `templates/cursor/atlassian.mdc.template` (a Cursor rules file pointing agents at the Atlassian config). Two files launch the project by double-click on Windows and macOS/Linux (`templates/launchers/`), offering an interactive menu for doctor, stats, setup, cockpit and command-reference scripts. Finally, two files document the generated knowledge DB — `templates/meta/READING.md` (how to read the folder) and `templates/meta/conventions.md` (naming, tags, artifact taxonomy).

## Documents

- [Project Configuration & Secrets](templates/01-project-configuration-secrets.md) - `aurora.config.yaml` and `.env.aurora.local` templates
- [Agent Guidance Templates](templates/02-agent-guidance-templates.md) - AGENTS.md system mandatories and Cursor Atlassian rule
- [Project Launchers](templates/03-project-launchers.md) - Windows and macOS/Linux double-click launch menus
- [Knowledge Base Conventions & Reading Guide](templates/04-kb-conventions-reading.md) - `meta/READING.md` and `meta/conventions.md`
- [Templates Layout & Generation Design](../design/04-templates-layout-generation.md) - template organisation and placeholder substitution

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, templates*