---
name: jira-export-{{PROJECT_SLUG}}
description: >
  Export Jira issues for {{PROJECT_NAME}} into Sources/JIRA/.
  Reads project_key and default_jql from aurora.config.yaml.
  Use whenever the user asks to sync/export/pull Jira for {{PROJECT_NAME}}.
version: "1.1.0"
entrypoint: SKILL.md
---

# JIRA Export Skill — {{PROJECT_NAME}}

Sync Jira → `Sources/JIRA/`.

> ⚠️ В отличие от Confluence, зеркало Jira пока делает модель — детерминированного
> скрипта для Jira ещё нет (запланирован). Поэтому здесь возможен тот же дрейф
> «дифф без изменений»: после экспорта запускайте `sync_audit.py` и не переписывайте
> файлы, если изменилось только форматирование.

## Project settings (source of truth)

Read **`aurora.config.yaml`**:

- `atlassian.jira.base_url`
- `atlassian.jira.project_key`
- `atlassian.jira.default_jql`

Auth: Cursor MCP — **your** account only. Never commit API tokens.

## Prerequisites

- mcp-atlassian configured
- `aurora.config.yaml` present

## Default JQL

Take from `atlassian.jira.default_jql` in `aurora.config.yaml` (do not hardcode another project's key).

## Workflow

1. Diagnostics: verify project via Jira API/MCP
2. Load `Sources/JIRA/update_log.md` (create if missing)
3. For each issue: fetch full data → render markdown → write `Sources/JIRA/{filename}.md`
4. Update log after each successful write
5. Summary: updated / skipped / failed

## Hard rules

- Only real Jira data
- Write only under `Sources/JIRA/`
- Never invent issues
