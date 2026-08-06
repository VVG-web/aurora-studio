# Deploy Aurora Studio into a project

## Prerequisites

- Python 3.9+
- Write access to the target project directory
- Aurora Studio cloned locally (`git clone https://github.com/<org>/aurora-studio.git`)

## 1. Deploy (new or existing repo) — recommended

From the Aurora Studio root:

```bash
python3 aurora.py new /absolute/path/to/your-project
```

This scaffolds the trust-layer structure, copies the engine, then launches the
**interactive setup**, which asks for everything project-specific:

- project **name / slug** (slug names the sync skills),
- Confluence **base URL, space**, and **root pages to sync** (page IDs — from the page URL `...pageId=NNN`),
- Jira **base URL, project key**, and **default JQL**,
- bootstrap threshold.

### Re-run setup any time — from the project itself

Setup is copied into the project, so anyone can change or complete settings later
(current values are pre-filled; Enter keeps them):

```bash
cd /path/to/your-project
python3 .opencode/scripts/aurora_setup.py
```

### Scriptable alternative (CI / no prompts)

The scaffolder can also run flag-driven, without the interactive step:

```bash
python3 scripts/install_aurora.py \
  --target /absolute/path/to/your-project \
  --name "Project Display Name" --jira-key PROJ --confluence-space SPACE
```

| Flag | Meaning |
|---|---|
| `--target` | Project root (created if missing) |
| `--name` | Human name → AGENTS.md, reports, sync-skill slug |
| `--jira-key` | Jira project key for the sync skill |
| `--confluence-space` | Confluence space key |
| `--dry-run` | Print actions only |
| `--force` | Overwrite existing files (dangerous) |

Default behavior: **never overwrite** existing files. Re-running is safe for filling gaps.

## 2. What appears in the target

```
AGENTS.md
aurora.config.yaml           # project settings (space, JQL, sync_roots, skills)
aurora.env.local.example     # copy → .env.aurora.local (gitignored)
.gitignore
.cursor/rules/atlassian.mdc  # thin pointer to aurora.config.yaml
.opencode/
  skills/aurora-vault/
  skills/confluence-sync-<Slug>/
  skills/jira-export-<Slug>/
  scripts/aurora_setup.py     # re-runnable interactive setup
  scripts/kb_lint.py          # find mechanical errors
  scripts/kb_fix.py           # repair: links, homoglyph names, frontmatter, merge dupes
  scripts/sync_audit.py       # Sources/ mirror integrity
  scripts/aurora_stats.py     # health dashboard + monthly metrics
  scripts/aurora_hooks.py     # pre-commit ratchet
  scripts/kb_trace.py        # impact, provenance and the traceability table
  scripts/aurora_doctor.py
  structure_dirs.txt          # fixed folder schema (enforced by doctor --structure)
  update_ignore.txt           # optional: paths the project keeps its own (globs)
Sources/{Confluence,JIRA}/
Raw/{laws,contract,customer,project,meetings,examples}/
AuroraKnowledgeDB/
Artifacts/{us,ac,algorithms,dictionaries,screens,contracts,mappings,role-model,diagrams,reviews,reports,drafts,meetings}/
Deliverables/{work,released}/
Workspaces/
Templates/  Prompts/
```

Folder list is fixed by the engine (`structure_dirs.txt`) and identical in every Aurora
project — projects don't add their own artifact types; non-standard material lives in
`Workspaces/<task>/`.

Install report: `Artifacts/reports/YYYY-MM-DD_report_aurora-install.md`.

## 3. Verify / doctor

```bash
cd /path/to/your-project
python3 .opencode/scripts/aurora_doctor.py --structure
python3 .opencode/scripts/kb_lint.py --summary
python3 .opencode/scripts/aurora_hooks.py --install   # pre-commit ratchet
```

Expect: doctor OK (or warnings only); `kb_lint` ошибок 0 (empty base is fine); the hook
records the current error count as a baseline that may only go down.

Deploying into a repo that already has documents? Run the maintenance chain once:
`sync_audit.py` → `kb_fix.py --all` (dry-run, разобрать нерешаемое) → `kb_fix.py --all
--apply` → `kb_fix.py --dupes` + `--merge` → `aurora_stats.py --queue`. Procedure:
`.opencode/skills/aurora-vault/references/maintenance.md`.

## 4. First week checklist

1. [ ] Re-run `aurora_setup.py` if any project setting was skipped
2. [ ] Read `AGENTS.md` and `.opencode/skills/aurora-vault/SKILL.md`
3. [ ] Configure **your** mcp-atlassian in Cursor (repo skills already present)
4. [ ] Optional: `cp aurora.env.local.example .env.aurora.local` (personal tokens for CLI only)
5. [ ] Drop evidence into `Raw/` (contract, ТЗ, meeting transcripts)
6. [ ] Open HTML guides under `Artifacts/drafts/`
7. [ ] Run `/aurora-vault build` or `ingest-raw` via your AI agent
8. [ ] Commit the scaffold (never commit `.env.aurora.local`)

## 5. Existing projects with an accumulated pile of docs

Full procedure: `skills/aurora-vault/references/migration.md`. In short:

1. Deploy Aurora **without** `--force` (keeps your files)
2. Lay evidence into `Raw/`, mirrors into `Sources/`, task material into `Workspaces/`
3. Extract cards from the sources (`build` / `ingest-*`) — they land as `imported`
4. Verify lazily to promote `imported → verified`
5. See [IMPLEMENTATION.md](IMPLEMENTATION.md) § Migrating legacy bases

## 6. Greenfield (empty repo)

```bash
python3 /path/to/aurora-studio/aurora.py new /path/to/new-project
cd /path/to/new-project && git init && git add -A \
  && git commit -m "Bootstrap Aurora Studio"
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `kb_lint: нет папки AuroraKnowledgeDB/` | Run lint from project root, not from Aurora Studio |
| AGENTS.md not picked up by agent | Open the **project** folder as workspace root |
| Sync skill wrong space / JQL | Re-run `aurora_setup.py` or edit `aurora.config.yaml` (not skill body) |
| Sync skill folder name ≠ slug | Re-run `aurora_setup.py` — it reconciles folder names to the slug |
| Secrets in git / doctor SECRET | Remove tokens; use `.env.aurora.local` or Cursor MCP only |
| Case rename `raw`/`Raw` on macOS | Two-step rename: `mv raw tmp && mv tmp Raw` |
