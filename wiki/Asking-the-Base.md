# Asking the base

[Wiki home](README.md) · related: [Trust](Trust.md) · [The agent](The-Agent.md) · [Assistants, MCP and skills](Assistants-MCP-and-Skills.md)

There are three ways to get an answer out of the base, from most to least automatic.

| Way | Use when | Result |
|---|---|---|
| **Ask** section / `agent:ask` | you want an answer in your own words | an answer from the cards **only**, a link on every claim, checked by Momus |
| `ctx:context <topic>` | you will paste verified context into any prompt or tool | a selection of cards with trust headers and bases |
| the [MCP server](Assistants-MCP-and-Skills.md) | an assistant should search the base itself while it works | search, read a card, assemble a pack — read-only |

## Ask

In the panel's **Ask** section type a question as you would to a colleague. The engine assembles context from cards; the
model answers from them only and puts a link on each claim; **Momus** then checks the answer: a claim is either
supported by a quote or marked "no support". The conversation is kept in `AuroraKnowledgeDB/meta/ask/` as plain
Markdown and goes to git with the base — what the team asked and what the base answered is shared knowledge. A
conversation that exposed a gap in the base is a reason to write a card.

- **Follow up without losing context:** `agent:ask --thread <id>` (the panel continues the open conversation).
- **Choose the model:** `--backend N` asks one gateway from the ring (1 is the primary).
- **Choose what counts:** modes — trusted only; everything including drafts; meetings only; trusted plus meetings. A
  statement made at a meeting does not pass into "trusted only": a meeting is not a signed document.
- **Do not journal:** `--no-journal` skips the conversation file (this is what the MCP server uses: it only reads).
- **"Why did we choose X? Why not Y?"** — `ctx:ask <question>` answers with quotes, including rejected and replaced
  Decision Records.

## Context packs

`ctx:context <topic>` selects cards and prints them with a **trust header and a basis** each — not "draft" but "PRJ-123
in progress". Useful flags: `--mode generate|review|ask|evaluate|meetings|trusted_meetings`, `--max-cards`, `--budget`,
`--save` (to `Artifacts/drafts/`). In `generate` mode only trusted knowledge goes in — build requirements on that.

The rule for any manual path: **assemble the context first**, do not feed an assistant Confluence pages as truth. The
difference between "a card with status `knowledge` and a basis" and "a page from the wiki" is the difference between a
fact and a rumour.

## How the search works

Search combines **words** with **meaning**: the semantic index (`kb:embed`) holds a vector per card and lives outside
git (rebuilt on demand). Stubs (`placeholder`) and the archive are excluded. Related and backlinked neighbours can join
a hop; a collapsed neighbour carries its gist, not its body.

Two watchdogs keep the ranking honest:

| Command | Measures |
|---|---|
| `ops:retrieval` | which cards come first for the analysts' real questions, and what changed since the last run |
| `ops:search-quality` | does the base find itself: a card's thesis as a question, the right answer known by construction — R@1, R@5, MRR |

## What to believe in an answer

An answer is only as good as its basis: read the **header** (`knowledge` or `draft`, and why), prefer claims with a
quote, and treat "no support" as a to-do for a human, not as a fact. → [Trust](Trust.md)
