# Glossary

[Wiki home](README.md) · related: [The card](The-Card.md) · [Trust](Trust.md)

| Term | Meaning |
|---|---|
| **Aurora Studio / the kit** | This repository: the engine, skills, templates and the panel. Deployed into a project, which then carries its own copy |
| **Project** | A folder with `aurora.config.yaml` and the fixed layout. Holds its own engine copy in `.aurora/` and its own knowledge |
| **Card** | A Markdown file with the knowledge about one entity. [The card](The-Card.md) |
| **Type** (`type`) | What a card is about and by which template it is made: `concept`, `process`, `glossary` … (thirteen types); written by the section folder. [Card templates](../docs/en/card-templates.md) |
| **Card template** | A file `TemplatesCommon/akdb_<type>_v<version>.md`: the type's passport, a head and body skeleton, an example, a self-check list. Its name is written in the card's `template:` field; `kb:cards` checks cards against it, and every write of a card is checked by it |
| **Type-choosing instruction** | `Prompts/KB_classify.md`: steps 0–7 for picking one card type without reading every template; the file belongs to the project (`kb:cards --guide`) |
| **`cards:` block** | A section of `aurora.config.yaml`: a card type's title, description and switch; edited in "Project settings → Card types" |
| **Type catalogue** | The generated `AuroraKnowledgeDB/meta/card_types.md`: the list of types with descriptions; manual edits are lost |
| **Kind** (`kind`) | `dictionary`, `document` or `knowledge` — decides who may edit the body |
| **Status** | `knowledge`, `draft`, `placeholder`, `index`, `deprecated` — the trust class |
| **Trust** | Computed from Jira statuses and where the source lives; never assigned. [Trust](Trust.md) |
| **`trust_basis`** | The reason for a card's status, in words ("PRJ-123 in progress") |
| **Thesis** | A short claim written by the model above the verbatim source of a `knowledge` card |
| **Momus** | The second model that checks every claim against the source |
| **Stub / placeholder** | A card created only because a link names the concept; excluded from search and context until a definition appears |
| **Mirror** | A deterministic copy of Confluence, Jira or a site in `Sources/` |
| **`Raw/`** | The layer of the human and the customer: evidence (contract, laws, customer material, meetings — immutable) and records (decisions, questions, reference lists, corrections) |
| **Record** | What a human writes and no source holds: a decision, a question, a hand-kept reference list, a correction. The master file lies in `Raw/decisions|questions|reference|corrections`; it survives a rebuild. [The card](The-Card.md) |
| **Mirror of a record** | The base's verbatim copy of a record master (`built: machine`, `sources` → the master), built by `kb:record --mirror`; it is read-only, the master is edited |
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
