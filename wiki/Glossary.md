# Glossary

[Wiki home](README.md) · related: [The card](The-Card.md) · [Trust](Trust.md)

| Term | Meaning |
|---|---|
| **Aurora Studio / the kit** | This repository: the engine, skills, templates and the panel. Deployed into a project, which then carries its own copy |
| **Project** | A folder with `aurora.config.yaml` and the fixed layout. Holds its own engine copy in `.opencode/` and its own knowledge |
| **Card** | A Markdown file with the knowledge about one entity. [The card](The-Card.md) |
| **Kind** (`kind`) | `dictionary`, `document` or `knowledge` — decides who may edit the body |
| **Status** | `knowledge`, `draft`, `placeholder`, `index`, `deprecated` — the trust class |
| **Trust** | Computed from Jira statuses and where the source lives; never assigned. [Trust](Trust.md) |
| **`trust_basis`** | The reason for a card's status, in words ("PRJ-123 in progress") |
| **Thesis** | A short claim written by the model above the verbatim source of a `knowledge` card |
| **Momus** | The second model that checks every claim against the source |
| **Stub / placeholder** | A card created only because a link names the concept; excluded from search and context until a definition appears |
| **Mirror** | A deterministic copy of Confluence, Jira or a site in `Sources/` |
| **`Raw/`** | Immutable evidence: contract, laws, customer material, meetings, corrections |
| **Correction** | A human's note that a card is wrong, kept in `Raw/corrections/` and applied at every build (`kb:correct`) |
| **Artifact** | A produced document in `Artifacts/`; never knowledge |
| **`based_on`** | The cards an artifact stands on |
| **Deliverable** | An artifact assembled for handover; `released/` holds frozen snapshots |
| **DR (Decision Record)** | An append-only record of a decision with rejected options |
| **REQ, SPEC, Q** | Requirement, specification and question-to-the-customer cards (`REQ-NNN`, `SPEC-NNN`, `Q-NNN`) |
| **MOC** | Map of content — a generated index card that makes cards reachable |
| **Route** | An ordered set of steps run by one button: Update, Fix, Rebuild, Write, Deliver |
| **Lap** (loop) | One batch parsed, thesised, linked and committed — a run is cut into complete laps |
| **Ring** | The numbered list of LLM gateways a call goes through |
| **Gateway / backend** | An OpenAI-compatible LLM endpoint |
| **Ratchet** | The pre-commit rule that the number of lint errors may not grow |
| **git-guard** | The refusal to write over a dirty tree, so rollback stays possible |
| **Skill** | A procedure for assistants: `aurora-vault`, `aurora-grill`, `aurora-dev` |
| **MCP** | Model Context Protocol; Aurora's server gives an assistant the base as a read-only tool |
| **Cockpit / the panel** | The local web control panel |
| **Engine manifest** | `engine_manifest.txt`: the whitelist of files `update` may overwrite in a project |
| **Registry** | `commands.txt`: the single source of truth for which commands exist |
