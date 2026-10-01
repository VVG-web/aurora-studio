# Knowledge-base rules

How a card is built in Aurora, where trust comes from and who may edit what. These are rules, not wishes:
commands work by them and are checked against them. Русская версия (the canonical text that ships to projects
with the engine): [../knowledge-rules.md](../knowledge-rules.md).

**On one page** — [knowledge-rules-tldr.md](knowledge-rules-tldr.md). Below is the full version.

## 1. Trust is a property of the source

A human does not assign trust to cards or confirm them. The engine computes the **source class** and moves the
card to a status. It is recomputed on every run: an issue's status in the tracker changes, and the base must
change with it.

| Class | When | Card status |
|---|---|---|
| `raw` | trusted by nature: `Raw/`, a Confluence section ticked "trust", a web link with the tick, a wiki reference branch | `knowledge` |
| `trusted` | all links — issues and stories — are in trusted statuses | `knowledge` |
| `draft` | at least one link is in a draft status | `draft` |
| `unknown` | no links | `draft` |

Order of checking — the first match decides:

1. **A meeting** (`Raw/meetings/`) neither gives nor takes away trust: a card from meetings alone is a draft and
   is counted on its own line, outside the trust share.
2. **`Raw/`** — a signed customer document.
3. **A Confluence section with the "trust" tick** in the import settings (`sync_roots[].trusted`) — its whole
   subtree is trusted at once, stronger than issues. It is set on reference sections: the logical model, the
   description of data formats.
4. **A web link** — by its own `trusted` tick (ticked — trusted, not — draft).
5. **A wiki reference branch** by name (`verify.trusted_branches`: NSI, reference lists, glossary, classifiers).
6. **A page's links**: issues and stories, and through code — algorithms (section 2). The **weakest** decides:
   one issue in progress outweighs ten finished ones.
7. **Tracing by neighbours' names** — only if there are no explicit links.

Other priorities:

- **an epic's status does not decide trust** — stories and issues do; a link only to an epic equals no links;
- **a subtask that was forgotten and left open is judged by its story** if the story is already in a trusted status;
- wiki folders in `verify.trusted_sources` give no trust: a wiki is trusted by a section tick, a reference branch
  or links.

A class without links is not "almost trusted" but **unproven**: such a card does not become knowledge. Otherwise the
class stops meaning anything exactly where it is needed.

## 2. Where links come from

Mirrors declare a role in the connector's description: `tasks` (a tracker), `artifacts` (documents), `raw` (primary
sources). `ops:trace-table` matches one with the other.

**A direct link** is one of three:

- a number matched: `AC-10.3.1` in an artifact's title and `US-10.3.1` in an issue's summary. Compared on the
  token boundary: `10.3.11` is already another story;
- a reference is visible: an issue key in an artifact's text, a page's `page_id` in an issue. One side is enough —
  the relation is symmetric;
- a story is named in the text: "Implements: US-4.1.1" on an algorithm or form page. The story's own page does not
  take other stories as support — its trust comes from its issue.

**A link by code** — a page names another's code: a contract — `ALG-072`, a form — `US-4.1.1`. The code is the
leading token of the title (`ALG-072 Registration…`); a space prefix (`SERVICE.UI-003`) is part of the code. Trust
goes from the story: a **story** relies only on its own issue, an **algorithm** (`ALG-…`) — on the stories it
implements, **other pages** — on stories and algorithms. Trust does not flow back: a story that mentioned a form does
not depend on the form's status. A reference to a reference list or a ticked section is neutral — a form's readiness
is decided by its stories.

**An indirect link** — tracing by neighbours' names, **up to two hops** deep, both ways. Three hops in a large base
connect everything with everything, and the class turns into noise. It does not pass through hubs (pages linked to
more than every twentieth artifact) and between an ancestor and a descendant in the wiki tree: a parent lists its
children, and through it an algorithm used to get the issues of all neighbouring algorithms of the folder.

Every link is recorded together with its proof. A month later the question "why is this card trusted" must be closed
by a line from the table, not by a memory.

## 3. Three kinds of cards

The kind decides **who may edit the body**. It is the most expensive decision in the base.

| Kind | What it is | Body |
|---|---|---|
| `dictionary` | dictionaries, reference lists, enumerations | carried over whole, the model does not touch it |
| `document` | contract, spec, regulation, printed form | verbatim, changing is forbidden |
| `knowledge` | everything else | the model writes a thesis and rethinks it |

The kind is set by `kb:kind` by a rule: the source folder, the base section, the shape of the text (a table of codes
without prose is a dictionary). **A human's choice is stronger than the rule**: a human states it with a `kind:` field
in a correction's header (`Raw/corrections/`); a `kind` written into a card is written by the engine, not by a human,
and is recomputed on every run. A "contract" is a document only in the legal sense ("contract No.", "state
contract"): "Contract mapping CURRENCY" is a specification, not a paper. A table of an algorithm's steps and a table of
a screen form's requirements are knowledge, not a dictionary.

## 4. How a source becomes a card

- **`dictionary`** — a reference list is carried over whole, one card per list. The model gives a name and `summary`.
- **`document`** — the model picks the sections, a script carries the text verbatim. The value here is the text itself.
- **`knowledge`** — `agent:distill`: the model reads the carried-over sections and **writes a thesis** — a definition
  usable without opening the source. The verbatim text goes under the thesis in the section "Source (carried over
  verbatim)": it can always be checked.

Every thesis is checked by **Momus** — a second model, claim by claim, with quotes. A card with no support found is
marked `unsupported:` and lands in `ops:todo`. This is the only work the scheme leaves to a human: not to assign trust
but to sort out what was made up.

### A card is an entity, not a retelling of a document

This is the main rule of the Zettelkasten and the basis of the whole scheme. **The unit of the base is an entity, not
a source.** If five analytical artifacts speak about an analytical balance, the base has one card "Analytical
balance", and it **accumulates** what it is and how it works — from all five. Not five cards, not "Analytical balance
(from the spec)" and "Analytical balance in the payment process".

The reverse side of the same rule is **extraction**. As soon as an independent entity is explained inside a card, it is
moved into its own card and a link takes its place. Was:

> The analytical balance receives information from the FCOD subsystem, which is part of the MinFin payment-processing
> project.

Became — in the card "Analytical balance":

> The analytical balance receives information from the subsystem [[FCOD]].

and next to it the card "FCOD":

> A subsystem that is part of the MinFin payment-processing project.

Knowledge is neither lost nor doubled: it **moved to where it belongs** and is linked. This is how the base grows in
depth, not width: there are fewer new names than new facts.

Why this is not style. A card per document breaks everything else: links scatter across copies and the graph shows a
connectivity that does not exist; an edit lands on one copy and the others keep saying the old thing — and both sides
look equally credible; search returns a random one of the copies; context is spent on repetition. To check the state —
`kb:twins`.

How the engine does it (since 1.100.32):

- **parsing sees the base.** Before slicing a source the model receives a list of existing cards on nearby topics. The
  same entity — the knowledge is added to it (`--append`), not created under a new name;
- **origin is a list.** `sources:` holds every document the card grew from. Trust is computed by the **weakest** of
  them;
- **extraction — `agent:extract`.** Another's definition moves into its own card verbatim, a link takes its place. If
  not found verbatim — the move is cancelled: taking something similar means rewriting knowledge under the guise of
  moving;
- **duplicates are judged by the model** (`agent:twins`), not by a human: "one entity or different ones" is decided
  from the text. A merge is reversible.

Knowledge that drifted apart earlier is brought together by `kb:twins` and `agent:twins`. Requirements and what is not
done yet — [knowledge-accumulation.md](knowledge-accumulation.md).

## 5. The card's structure

```
---
title, kind, status, trust, trust_basis, trust_checked, source, related
---

Thesis: the entity's definition and essential conditions. Links to other cards.

## In question
Data from sources whose class is not determined. A separate block — not mixed with the confirmed.

## Source (carried over verbatim)
The fragments the thesis was assembled from.

## Change history
A change of meaning, a card split, absorbing a duplicate, a class downgrade.
```

The footer gets **only a change of meaning and structure**, not every edit: ordinary enrichment is visible in `git
log`, and duplicating history in the text is pointless. "Meaning" is a claim that artifacts referred to through
`based_on`: if it changed, everyone who relied on the card must see it.

## 6. Twins: a trusted one and a draft one

Two cards may exist about one concept: one assembled from trusted sources and one from untrusted ones. They are linked
by `same_subject:` and **never enter one pack**: `generate` gets the trusted one, `evaluate` — both, and the draft one
carries the header "the same concept by an untrusted source".

When an untrusted source gains trust, the pair merges: no draft card remains.

## 7. Links are mandatory

A card without links does not exist. A link is a reference in the body, an entry in `related:` or membership in a map
of content. An orphan is picked up by `kb:moc`: a section map and a source-document map — the second matters more, it
answers "where did this come from". A card no map took is a lint error, not a quiet orphan.

## 8. What a human no longer does

Does not confirm cards, does not keep a verification queue, does not set the owner and the trust's expiry date. The
commands `kb:verify` and `kb:queue` and the "Acceptance" tab are deleted: a procedure that lost its meaning is more
dangerous than a missing one — time is spent on it and people think they are doing work.

A human is left with exactly three things: sorting out claims without support, deciding a disputed `kind` and fixing
what the engine honestly named in `ops:todo`.

## 9. Where hand-written things live

Nowhere special — where everything else does, in the section by subject.

The temptation to create a separate folder for hand-written material is understandable: then it is visible what must
not be touched. But a folder cannot know who wrote a card. The base is laid out **by subject**, and maps, links and
search rest on that: a card about discrepancies lies next to other cards about discrepancies, whoever wrote them.
Authorship cuts across subjects.

A measurement on a live project of 1955 cards where such a folder existed (`Decisions/`, `Questions/`, `Reference/`):
inside it **31** of 269 were irrecoverable, outside — **451**. The folder was wrong both ways.

### The mark is set by the machine, not by a human

The second temptation is to consider hand-written everything that has no `source:`. That is also wrong, and just as
easy to check: of 425 cards without a source **137** were created by `kb:repair --stubs` as stubs, **64** arrived in a
mass commit, and hand-written ones are a handful. Protecting stubs from deletion is doubly harmful: a rebuild will not
bring them back, and keeping blanks in the base is pointless — the linter catches them anyway.

The correct mark is **positive**: every machine entry sets `built: machine` itself. Slicing, stubs, the agent. On a
rebuilt base that is 965 of 965.

Hence three outcomes on reset, not two:

| What kind of card | The mark | What `kb:reset` does |
|---|---|---|
| will be rebuilt | a live `source:` | wipes: otherwise a twin appears after the rebuild |
| made by the machine | `built: machine` | wipes: the rebuild will make the same |
| origin unknown | neither | **keeps and names it** |

The third line is not "your work" but an honest "the engine does not know". See them by name: `kb:reset
--list-unknown`. If none is yours — `--drop-unknown`, and the base gets cleaner.

From this a rule for a human: **writing by hand — write into the subject section**. Nothing needs marking, but do not
count on the engine recognising your work by the absence of a mark: it recognises only machine work. And if a card
grew out of a document — put the document in `Raw/` and parse it by an ordinary run: then it becomes recoverable,
which is more reliable than guarding a single copy.

## 10. What the engine does when a source changes

Knowledge lives not at the moment of parsing but all the time: a page is edited, an issue is moved, a document comes
out in a new edition. Hence three separate mechanisms, and confusing them is costly.

**A source changed.** The file's fingerprint diverged from the one recorded at parsing — the source returns to the plan
(`kb:build --reopen`), parsing replaces in the card **only** the carried-over text and removes the thesis mark. The
thesis, history, links and header stay: they were written over the text, not by it. Then `agent:distill` writes a new
thesis and moves the previous one to the card's footer with the date, the document and a "what changed" line.

**An issue moved.** Nothing is rewritten at all — the trust class changes at the nearest `kb:trust`. The issue went
back to work, so the knowledge is a draft again, and no human acceptance can undo that.

**A card outgrew the model's window.** A thesis is not written for such a volume: the retelling degenerates into an
annotation. The planner proposes boundaries, the engine cuts, the parts get links to each other, and the original
becomes a document map.

A common rule for all three: **the engine does not rewrite what it did not write.** The body of a dictionary and a
document is never touched; the history footer is not overwritten; the previous thesis is kept, because there is
nowhere else to restore it from.

## 11. File and folder names

The base is a git repository, and it is checked out on Windows, macOS and Linux. So the name of every file and folder
passes the **strictest rules of the three systems at once**: a name valid on one breaks the checkout of the whole
repository on another, not one file. It happened: a 400-byte name is created on macOS while Linux accepts up to 255 —
and the kit's GitHub check failed on such a stub without reaching the check itself.

- without `< > : " / \ | ? *` and control characters; a name does not end with a dot or a space (Windows);
- not `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9` — neither with an extension nor without (Windows);
- a name no longer than **255 bytes** of UTF-8 (Linux): Cyrillic is two bytes per letter, i.e. about 120 letters; the
  engine itself keeps card names within 240 bytes and 150 characters;
- a path from the project root no longer than **200 characters** (Windows accepts 260 including the folder the project
  lies in);
- names in one folder differ not only by case: "Core_Balance" and "core_balance" are one file on macOS and Windows;
- letters in composed form NFC: macOS can store "й" and "ё" decomposed, and Linux reads such a name as a different one.

The names the engine composes (cards from titles, mirror pages, artifacts, conversations, corrections) already follow
these rules. The rule is for everyone who writes by hand — a human and any model.

The check — `python3 .opencode/scripts/aurora_doctor.py` (the "names:" lines); the fix — `python3
.opencode/scripts/kb_names.py --apply` (the `kb:names` command): Confluence mirror paths, card names with links, git
entries distinguishable only by case.
