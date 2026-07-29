# Implementing Aurora in your project

Playbook for leads / SA / BA introducing the framework.

## Why Aurora

LLM agents hallucinate when all markdown looks equal. Aurora separates:

1. **Evidence** (`Raw/`, `Sources/`) — immutable or sync-owned
2. **Knowledge** (`AuroraKnowledgeDB/` cards with `status`) — trust filter for prompts
3. **Products** (`Artifacts/`, `Deliverables/`) — generated work, not fed back as “truth”

Invariants (never violate): see `.opencode/skills/aurora-vault/SKILL.md`.

## Rollout phases

### Phase 0 — Install (30–60 min)

Follow [INSTALL.md](INSTALL.md). Commit the scaffold. Do **not** invent domain cards yet.

### Phase 1 — Evidence first (1–3 days)

Put real materials only:

| Material | Path |
|---|---|
| Contract / SoW / ТЗ | `Raw/contract/` |
| Laws / regulations | `Raw/laws/` |
| Meeting transcripts / protocols | `Raw/meetings/` |
| Customer AS-IS, decks | `Raw/customer/` |
| Living project notes | `Raw/project/` |
| Confluence / Jira mirrors | via sync skills → `Sources/` |

### Phase 2 — First cards (bootstrap)

- Extract candidates via `/aurora-vault build` or `ingest-raw` / `ingest-meeting` / `ingest-tz`
- Default status: `imported` or `draft`
- Bootstrap mode: agents may use imported cautiously until verified ≥ ~20% (see `retrieval.md`)
- Add 5–10 **golden questions** once you have verified facts

### Phase 3 — Verify gate (ongoing)

Human owner sets:

```yaml
status: verified
owner: "@name"
review_by: YYYY-MM-DD
```

`verified` is the top status of the base. Never silent overwrite of verified.

### Phase 4 — Artifacts & SDD

- US → `Artifacts/us/`, AC → `Artifacts/ac/`
- Specs → `AuroraKnowledgeDB/Specs/` (`/aurora-vault spec`) then `spec-pack` for vendors
- Trace: ТЗ → REQ → SPEC → Jira → AC → test plan

### Phase 5 — Hygiene

Weekly: `/aurora-vault garden` + `python3 .opencode/scripts/kb_lint.py`  
Monthly: update `AuroraKnowledgeDB/meta/metrics.md`  
After big syncs: `/aurora-vault eval` against golden questions

## Agent configuration

1. Open the **project** as the IDE workspace root (not this kit).
2. Ensure `AGENTS.md` is loaded as project instructions.
3. Skills under `.opencode/skills/` (and/or copy `aurora-vault` into `.claude/skills/` / Cursor skills if your tool requires it).
4. Optional Atlassian MCP for sync skills.

### Cursor

- Project rules: `.cursor/rules/atlassian.mdc`
- Point agent at project root

### Claude Code / OpenCode

- Skills in `.opencode/skills/aurora-vault/`
- Invoke `/aurora-vault <command>`

## Migrating legacy bases

1. Install Aurora without `--force`.
2. Map old folders:

| Legacy | Aurora |
|---|---|
| `Laws/`, `docs/legal/` | `Raw/laws/` |
| `Transcripts/`, `meetings/` | `Raw/meetings/` |
| Root `JIRA/` | `Sources/JIRA/` |
| Flat `AuroraKnowledgeDB/ZK-*.md` | `AuroraKnowledgeDB/{Concepts,Processes,…}/` + frontmatter upgrade |
| Working BPMN / drafts | `Workspaces/<task>/` |

3. Status mapping suggestion:

| Old | New |
|---|---|
| fact / confirmed | `imported` (then human → `verified`) |
| todo / to_verify | `draft` |
| decision | `Decisions/DR-NNNN-…` with `status: accepted` |

4. Keep legacy IDs in `aliases:` so wiki-links keep working.
5. Rebuild `manifest.json` via `/aurora-vault build`.

## Success criteria

- [ ] `kb_lint` = 0 errors
- [ ] AGENTS.md matches real folder tree
- [ ] At least one verified card with owner + review_by
- [ ] Golden questions pass `/aurora-vault eval`
- [ ] Artifacts cite `based_on` cards when generated from context packs

## Anti-patterns

- Putting AI drafts straight into `AuroraKnowledgeDB/` as `verified`
- Editing `Deliverables/released/` or `Raw/contract/`
- Feeding `Artifacts/` back into prompts as ground truth
- Deleting cards instead of `supersede`
- Sync writing outside `Sources/`
