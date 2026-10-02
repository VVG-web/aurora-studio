# FAQ

[Wiki home](README.md) · more: [Troubleshooting](Troubleshooting.md) · [Glossary](Glossary.md)

### Do I need an LLM to use Aurora?
No. Syncing, linting, repairing, links, maps, trust, tracing, export — all deterministic scripts. A gateway is needed for
`agent:*` (parsing into cards with theses, asking, producing artifacts), semantic search (`kb:embed`) and scan recognition.
→ [The agent](The-Agent.md)

### Is my data sent anywhere?
Only to the gateways *you* configure, and only for the model-driven commands. Mirrors and cards stay on your machine and in
your git. If your perimeter forbids sending materials out, leave semantics and scans off and use a local gateway. The panel
listens on `127.0.0.1` only.

### Who approves a card?
Nobody. Trust is computed from the statuses of the Jira issues behind a card's source (or from a signed document in
`Raw/`). An issue goes back to work — the card becomes `draft` at the next `kb:trust`. → [Trust](Trust.md)

### Why is the share of `knowledge` low?
Issues still in progress, or no links to them were found. The second is cured by tracing (`ops:trace-table`), not by
changing statuses. Also check `trust_statuses` in the config.

### Can I edit a card by hand?
Yes for `knowledge` cards where you add what the sources lack — but prefer a **correction** (`kb:correct`): it lives in
`Raw/corrections/` and is applied at every rebuild, so it survives. Dictionary and document bodies are never rewritten,
and the history footer is never overwritten. → [The card](The-Card.md)

### What does Momus do?
A second model that checks every claim of an agent answer against the source: either a supporting quote, or "no support".
Claims without support are not carried into documents. → [The agent](The-Agent.md)

### Why a fixed folder layout?
So skills, prompts, templates and `update` work in every project. Anything non-standard goes to `Workspaces/<task>/`; a folder
needed only by one project is declared in `paths.extra_structure_dirs`.

### Can I stop "Update the base" halfway?
Yes, at any moment. The run is cut into complete laps and every lap is committed: what was parsed stays valid.

### Where are my tokens stored?
In `.env.aurora.local`, which is git-ignored. The panel shows only "filled / empty" and never gives a value back.

### How do I connect Claude Code or Cursor?
`kit:mcp` prints a ready connection block; the server only reads. → [Assistants, MCP and skills](Assistants-MCP-and-Skills.md)

### Can the model rewrite my documents?
It cannot touch `Raw/` (immutable), `Sources/` (written only by sync) or `Deliverables/released/` (frozen). It writes only
through whitelisted engine commands, always after a checkpoint commit.

### How is a mirror different from a copy?
It is deterministic: the same source gives the same file byte for byte, so a repeated sync makes no noise in git and
`sync:audit` can verify it. → [Sources and mirrors](Sources-and-Mirrors.md)

### What happens to old knowledge?
It rots by itself in the right direction: when a linked issue is reopened the card drops to `draft`. A replaced card is
moved to `_archive/` with `kb:supersede` and incoming links are rewritten; Decision Records are append-only and keep the
rejected options.

### Does it work on Windows?
Yes — the test suite runs on `windows-latest` in CI. → [Windows and macOS](Windows-and-macOS.md)

### Which languages?
The engine's output and the cards are as you write them (Russian is the project's own language); the panel and the
documentation are available in Russian and English. A new interface language is a new file:
[Contributing](Contributing.md).

### Where is the history of changes?
[CHANGELOG.md](../CHANGELOG.md) (in Russian) and the GitHub Releases page; every release is created from it.
