# Skills Support Files Design

## Context

The `skills/` module is the shipping unit of agent instructions for Aurora Studio. It is not a
library of code — it is a set of **skills**, each a folder containing a `SKILL.md`, a
`skill.json` registration, and (where the command surface is large) a `references/` directory.
Understanding the shape of these files, and the rules that keep them honest, is the design view
of the module.

## Skill layout

Every skill has the same skeleton:

- **`skill.json`** — registration metadata consumed by the harness: `name`, `description`,
  `entrypoint`. Example for the knowledge skill (`skills/aurora-vault/skill.json`):
  ```json
  { "name": "aurora-vault",
    "description": "Operate the Aurora (Аврора) knowledge framework: ...",
    "entrypoint": "SKILL.md" }
  ```
  The description doubles as the trigger/usage matcher: aurora-vault's enumerates the Russian
  and English phrasings that should route to it («/aurora-vault», «база знаний»,
  «Zettelkasten», «decision records», ...).
- **`SKILL.md`** — the operative instruction the model follows: a frontmatter block with
  `name` and `description`, then the body. aurora-*vault*'s is the largest: folder
  semantics (trust layers), the command registry organized into namespaces (`kit:`, `sync:`,
  `kb:`, `agent:`, `ctx:`, `make:`, `ship:`, `ops:`), the script-vs-model rule, and
  the invariants. aurora-*dev*'s and aurora-*grill*'s are procedure-sized.
- **`references/`** — detailed procedures loaded **on demand** (progressive disclosure: read
  only the file the requested command needs, keeping context small). aurora-vault has one file
  per concern: build, frontmatter, maintenance, migration, retrieval, workflows. aurora-dev
  carries the test-case and scenario templates here because they are part of the delivery.

## Progressive disclosure

The operative rule of the vault skill's SKILL.md: "One skill, many commands. Detailed
procedures live in `references/` — read ONLY the file needed for the requested command." Each
command-row in the registry carries a Reference pointer to exactly one reference file (for example
`kb:trust` → `references/maintenance.md`, `kb:question` → `references/workflows.md`), so the
model can jump to the right procedure without loading the rest.

## Skill vs engine boundary

The skills describe commands; the commands' mechanics live in engine scripts
(`.opencode/scripts/*.py` in a project, `scripts/*.py` in the kit) that the SKILL.md
invokes with exact paths. The skills therefore reference **script names and flags verbatim** in
their reference files (for example `kb_fix.py --all --apply`, `ctx_pack.py "…" --mode
ask`), and the invariant "mass mechanics run by script, not by the model" is stated directly
in the vault SKILL.md ("script vs model" table) and in aurora-dev's strict level order.

aurora-*dev* is deliberately kit-only: it checks for `engine_manifest.txt` in the root and the
absence of `aurora.config.yaml`, and `scripts/dev_qa.py` refuses to run inside a project built
on Aurora. Its `Development/` QA tree lives in a private `development` branch pushed only to a
private repo, so the internal verification never leaks into the public engine repository.

## Skill installation

Skills live in the kit repository but the agent looks for them in its own catalog
(`~/.claude/skills/`), so a new dialog cannot find `/aurora-dev`/`/aurora-vault` until the
skills are installed. All Aurora skills are installed together by `kit:skills` (script
`scripts/install_skills.py --status` / `--apply`); other harnesses receive a symlink to that
single catalog directory. One copy — two copies of the same skill diverge on the first edit and
then nobody knows which one answered the dialog. Installation is built into the lifecycle:
`aurora.py new` installs skills immediately and `aurora.py update --apply` refreshes them with
the engine; explicit install is only needed after editing the skills themselves, because the
catalog entry is a copy, not a link back to the repository.

## Source Files

- skills/aurora-vault/SKILL.md - knowledge-framework skill, install via `kit:skills`
- skills/aurora-vault/skill.json - registration metadata
- skills/aurora-dev/SKILL.md - engine-development kitchen skill
- skills/aurora-grill/SKILL.md - decision-tree intent decomposition skill

---
*Last updated: 2026-08-28*
*Areas: skills*