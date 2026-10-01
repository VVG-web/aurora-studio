# Aurora knowledge-base rules on one page

The full version — [knowledge-rules.md](knowledge-rules.md). Here is what to remember every day. Русская версия (ships
to projects): [../knowledge-rules-tldr.md](../knowledge-rules-tldr.md).

---

## Trust is computed, not assigned

A card's class is derived from the statuses of the Jira issues linked to its source. A human does not set it or remove
it. The issue went back to work — the card became a draft by itself. A wiki page is trusted in one of three ways: its
section is ticked "trust" in the import settings (logical model, data formats), the page is a reference list, or its
links are trusted: an algorithm — by the story it implements, a contract — through the algorithm, a form — by its
stories; the weakest link decides. In detail — [knowledge-rules.md](knowledge-rules.md), sections 1–2.

`knowledge` — a fact · `draft` — not yet confirmed · `placeholder` — a stub: the name is taken by a link, no knowledge ·
`index` — service (maps and tables of contents) · `deprecated` — replaced, for history only.

**A card is an entity, not a retelling of a document.** Different documents speak about one object — the knowledge
about it is **accumulated** in one card, not spread across five names. If another entity was explained inside it — it
moves to its own card and a link takes its place (`agent:extract`). So the base grows in depth: there are fewer new
names than new facts. What drifted apart is brought together by `agent:twins` — the model decides, not a human.

**A stub does not answer.** A link to a concept appears before the knowledge — that is how a card index works. Such
cards are excluded from search, context and measurements; all of them are visible in `MOC/Пустышки` (the stubs map).
A definition appeared — `kb:repair` removes the mark itself.

**A low share of trusted is not an emergency.** Either issues are still in progress or no links were found. The second
is cured by tracing, not by statuses.

## Three kinds of cards: the kind decides who edits the body

- **`dictionary`** — a reference list, carried over whole. The model does not touch it.
- **`document`** — verbatim text. Never rewritten by anyone.
- **`knowledge`** — the model writes a thesis and puts the verbatim source under it.

The kind is stronger than the status: a trusted dictionary still must not be retold.

## Links are mandatory

A card without links is found neither by a human nor by retrieval: it is on disk and not in the base. It is a lint
error, not a quirk. Cured by `kb:moc --apply`.

## The engine does not rewrite what it did not write

The body of a dictionary and a document is not touched. The "Change history" footer is not overwritten. The previous
thesis is kept on a rebuild — there is nowhere else to restore it from.

## What happens when something changes

| What changed | What the engine does |
|---|---|
| a source's text | returns it to the plan, replaces **only** the carried-over text, rebuilds the thesis, moves the previous one into history |
| an issue's status | rewrites nothing — changes the trust class at the nearest `kb:trust` |
| a card outgrew the model's window | the planner proposes boundaries, the engine cuts, the parts are linked, the original becomes a document map |

## The mark is set by whoever knows

The machine marks its work `built: machine`. **The absence of the mark does not mean "a human wrote it"** — it means
"origin unknown". The engine draws no conclusions from the absence of a feature: what it does not know it calls unknown.

## What a human does

Almost nothing of what they did before. Does not approve cards, does not set trust, does not fix links by hand. What
remains:

- **write what the sources lack** — decisions (DR), questions to the customer, own reference lists. Write into the
  subject section, mark nothing;
- **resolve the disputed** — duplicates the machine did not dare to merge; cards without links to issues; claims
  Momus marked "no support";
- **press two buttons** — "Update the base" (take the new from sources) and "Fix the base" (put in order what is there).

## Two buttons and what lies between them

**Update** looks outward: sync, parsing in batches, theses, links, trust. **Fix** looks inward: links, duplicates,
maps, tables of contents, the schema, a quality review. It does not go to the sources.

A run is cut into complete laps: every lap adds finished knowledge, not stubs. Switched off in the middle of the night
— what was parsed stays valid.

## File names — stricter than all three systems

The project is opened on Windows, macOS and Linux, and a name must pass everywhere: without `< > : " / \ | ? *`, not
CON/NUL/COM1, no longer than 255 bytes (Cyrillic is two bytes per letter), a path from the project root up to 200
characters, names in one folder differ not only by case, letters in NFC. The check — `aurora_doctor.py`.

## What to believe in the base's answer

Every card enters context with a trust header and a **basis** — not "draft" but "PRJ-123 in progress". The answer is
checked by Momus: every claim has either support with a quote or "no support". A claim without support is not carried
into a document even if it looks reasonable — that is exactly how a guess becomes a requirement.
