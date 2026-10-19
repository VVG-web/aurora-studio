# Migrating an existing base to card templates

New and touched cards are written by a template on their own: every write goes through the single entry
(`card_types.write_card_text`), and the `template: akdb_<type>_v<version>.md` stamp in the head means "the card passes the
template of its type" (see [card templates](card-templates.md)). Cards nothing has touched stay "pre-template". This page
is about bringing the whole old base to the templates: the `kb:conform` and `agent:conform` commands, the steps of the "Fix
the base" route and the "migration complete" state.

## Stages

| Stage | Command | Model | What it does |
|---|---|---|---|
| 0. Scouting | `kb:conform` (without `--apply`) | no | A "type × outcome" table, the remainder by V-NN codes, anomalies. Writes nothing |
| 1. Mechanical part | `kb:conform --apply` | no | Head repairs by a fixed list of rules and the stamp where a card passes the template strictly; the report `meta/conform_report.md` |
| 2. Finishing | `agent:conform --apply --until-done` | yes, role `worker` | Lays out the own part of a machine knowledge card by the template sections with a verbatim transfer of lines |
| 3. Records | moving hand-written decisions, questions and references to `Raw/` | no | Not in this release: such records stay an exception |
| 4. Completion | `kb:conform --finalize --apply` | no | Writes `meta/conform_state.json`; after that a card without `template:` is a `kb:lint` error |

Stages 1 and 2 stand in the **"Fix the base"** route after the last trust recount and before the indexes — when texts and
heads are already in their final form; they are not in "Update the base" or "Rebuild the base". So the nightly chain
"Update" → "Fix" brings the remainder over by itself. Stage 4 is a separate decision: it is taken when the remainder is closed.

## What changes in a card (stage 1)

Only what is listed, deterministically; nothing else:

- **Type.** No `type` — taken from the section; a synonym a model invented (`entity`, `userstory`, `algorithm` …) is
  replaced by the schema type (the card goes to the "retyped" report); a type outside the registry — a remainder with V-03.
- **Head.** `built: mac` → `built: machine`; the key `pdated` → `updated` (or removed when `updated` exists); a single
  `source:` → a `sources:` block; no `kind` — assigned by the same rule as `kb:kind`; no `status` → `draft` (`index` for a
  map); no `created` — the earliest known date; no `updated` → `created`; an empty `template:` line is removed. A field
  that is absent and has nowhere to come from is **not invented**: a machine card without `source_synced` has no error,
  only a warning, and gets the stamp.
- **Own part** — only for `built: machine`, `kind: knowledge`, not a stub: consecutive identical service blocks ("Что
  относится к теме", "Не путать" …) collapse into one. Service sections stay where they are (V-24 is only a warning): they
  cannot go into the source area — a source update erases the block — nor to the end of the card, where history and repair
  notes are written.
- **Never changed:** the bodies of `kind: document` and `kind: dictionary`; "Исправления человеком", "История
  изменений", "Источник…"; cards of unknown origin (no `built: machine` and no live `sources`) — their head only gets
  the missing fields, the body is untouched. Unmanaged types (`spec`) and hand-written records (a decision, a question, a
  reference without `built: machine` and without a source) are skipped.

The command makes no commits and does not touch `Raw/`. It is idempotent: a second run changes nothing, the report
included. Without `--apply` — a dry run; with `--apply` a clean git tree or `--allow-dirty` is needed. Rollback — `git revert`
of the route commit. The write goes through the single entry: an edit that gives a card a new violation is not accepted,
the card stays as it was.

## Reading the report

`kb:conform` prints a table: by type — how many cards already carry the stamp, how many will get it, how many go to the
`agent:conform` queue, how many stay without a stamp and which cards violate the template with a stamp in place. Then the
remainder by codes and the anomalies (stub keys `ilt`/`lt`, `kind: template`, a machine card without `sources`, unknown
origin). The file `AuroraKnowledgeDB/meta/conform_report.md` repeats the state of the base after the run and changes only
when that state changes. Machine output — `--json`.

A typical remainder on a live base is about two percent: cards whose definition is a paragraph longer than 600
characters, and a `# …` heading unequal to `title` in cards of unknown origin. The first are not cured by moving lines,
the second the engine does not rename.

## `agent:conform`

It takes cards whose own part violates only sections and the definition (V-09, V-10, V-12), `built: machine`,
`kind: knowledge`, an own part of at most 6000 characters, with a line fit to be the definition (plain text up to 600
characters). The model gets the numbered lines and the closed list of the type's sections and answers with numbers:
`{"lead": N, "body": [N…], "sections": {"Section": [N…]}}`. It writes no text. The script places the headings and checks:
every number exactly once; the set of lines (without `## …` headings) equals the original; the definition is plain text
up to 600 characters; the result passes the template strictly. Any failure — the card is not changed and is set aside
until its file is edited (`.aurora/state/conform_parked.json`), the model is not asked about it again. Success — a write
through the single entry with the stamp and a line in "История изменений".

## Completing the migration

`kb:conform --finalize` shows whether the migration can be declared complete: no cards without a template are left except
unmanaged types and hand-written records. If so, `--finalize --apply` writes `meta/conform_state.json`
(`{"complete": true, "date": …, "versions": {type: version}, "exceptions": {…}}`). If not, the command names the
remainder by codes and writes nothing. After the write `kb:lint` counts a card without `template:` as an error (kind
`missing-template`), `kit:doctor` names the date of completion, and `ops:todo` shows the remainder as the engine's work
rather than a human decision. To withdraw the declaration — delete `meta/conform_state.json`.
