# Knowledge card templates

A knowledge-base card is described by a **template** — a file `TemplatesCommon/akdb_<type>_v<version>.md`
(for example `akdb_concept_v1.0.md`). The template records everything about the type: its name, the base
section it lives in, the allowed kinds and statuses, the sections the body is made of, what the head looks
like, an example of a finished card and a self-check list. The template is read by a human, by an assistant in
the IDE and by **the engine itself**: the type registry takes the passport from it, and the card check takes
its rules.

This page describes what works in the kit today. What is not there yet is named in one list at the end
([what the next releases bring](#10-what-the-next-releases-bring)) and is not described as ready below.

## 1. What works now

- Thirteen templates live in the kit's `scaffold/TemplatesCommon/`. A new project gets them on creation, an
  existing one on `aurora.py update <project> --apply` (the **seed** rule: new files appear under their own
  names; a copy the project has changed stays in place, and the kit's version lands next to it as `<file>.new`).
- The type registry (`card_types.registry()`) reads the template passports. The "section → type" table is one:
  parsing, repair, the linter and the schema take it from the registry; with no templates it is the old one.
- The **`kb:cards`** command shows the registry and checks cards against their types' templates (section 7).
- Project rules (section 3): the instruction **"How to determine the card type"** (`Prompts/KB_classify.md`; it travels
  by the same **seed** rule, `kb:cards --guide` prints its text), the **`cards:`** block of the config — a type's title,
  description and switch, the **type catalogue** `AuroraKnowledgeDB/meta/card_types.md` and the "Card types" group of
  "Project settings".
- After its summary line `kb:lint` prints a "card templates" section; `kit:doctor` names the delivered templates,
  the registry problems and the state of the type catalogue (section 8).
- The template is **mandatory when writing**: a new card is checked strictly, an edit by difference; a card that passes gets
  the `template:` stamp (a violation in a stamped card is a `kb:lint` error, in a "pre-template" one a note); see
  "Writing a card" in the [architecture](architecture.md). `kb:cards` shows errors as warnings (strict mode is `--strict`).

## 2. How a template is built

The file is assembled from zones, top to bottom:

| Zone | What is in it | Read by |
|---|---|---|
| Passport | a flat frontmatter: name, description, class, section, kinds, statuses, body sections, minimum of links | human, assistant, code |
| Header comment and rules for the AI | "HOW TO USE", genre, what lives and what does not live in each part, borders with neighbouring types, style | human, assistant |
| Common block `AKDB:COMMON` | twelve common rules — the same text in all thirteen templates | assistant, an equality check |
| `## Шапка карточки`, `## Тело карточки` (head, body) | skeletons of the head (a `# …` hint at each field) and the body (`<!-- ИНСТРУКЦИЯ: … -->` comments) | code, assistant |
| `## Пример` (example) | a finished card on neutral data; it passes the check with no violations | assistant, test |
| `## Чек-лист самопроверки` (self-check list) | a `[ ]` list | assistant |
| Extract `AKDB:PROMPT` | a short text of the type (up to `prompt_chars` characters) for model requests | code |

The section titles inside the template files are in Russian, as is all engine output. The passport is a flat
frontmatter (no nested YAML). The six required fields of the common templates
(`artifact`, `template`, `version`, `file`, `author`, `created`) are joined by the fields of the type:

| Field | Value |
|---|---|
| `card_type` | the type identifier: `concept`, `status-model`, `cited-document` … |
| `card_class` | `entity`, `source`, `record` or `map` (section 3) |
| `name`, `name_en`, `description`, `description_en` | the type's name and description in two languages |
| `section`, `home`, `id_prefix` | the base section (`Concepts`, `Processes` …; empty for a correction); for records — the folder of the master file in `Raw/` and the number prefix (`DR-`, `Q-`) |
| `kinds`, `statuses` | allowed kinds (the first is the default) and statuses |
| `sections` | the body sections the model writes, in order |
| `fields_extra` | head fields beyond the common dictionary |
| `min_links`, `prompt_chars` | the minimum of outgoing links (section 6); the limit for the model extract |
| `migration` | `none`, `auto` or `llm`: how cards of the previous version are brought up |

A card's class is **not stored** in the card: it is read from the template passport by `type`.

Templates are searched in this order: the project's `Templates/` (own types and editions), the project's
`TemplatesCommon/` (the kit's templates delivered by an update), and, if the type is in neither, the kit's own
`scaffold/TemplatesCommon/`. A project file wins over a kit file at any version. An incomplete passport does not break
the check: the file is skipped, and `kb:cards --list` and `kit:doctor` name the problem.

## 3. Thirteen types

| Type (`type`) | Name | Template | Class | Base section | Kinds (`kind`; the first is the default) |
|---|---|---|---|---|---|
| `concept` | Concept | `akdb_concept_v1.0.md` | entity | `Concepts` | knowledge, dictionary, document |
| `glossary` | Term | `akdb_glossary_v1.0.md` | entity | `Glossary` | dictionary, knowledge, document |
| `process` | Process | `akdb_process_v1.0.md` | entity | `Processes` | knowledge, document |
| `system` | System | `akdb_system_v1.0.md` | entity | `Systems` | knowledge, dictionary, document |
| `role` | Role | `akdb_role_v1.0.md` | entity | `Roles` | knowledge, dictionary |
| `status-model` | Status model | `akdb_status-model_v1.0.md` | entity | `Statuses` | dictionary, knowledge |
| `requirement` | Requirement | `akdb_requirement_v1.0.md` | entity | `Requirements` | knowledge, document |
| `reference` | Reference list | `akdb_reference_v1.0.md` | source | `Reference` | dictionary, document |
| `cited-document` | Cited document | `akdb_cited-document_v1.0.md` | source | `Documents` | document |
| `decision` | Decision | `akdb_decision_v1.0.md` | record | `Decisions` | document |
| `question` | Question | `akdb_question_v1.0.md` | record | `Questions` | document |
| `correction` | Correction | `akdb_correction_v1.0.md` | record | — | — |
| `moc` | Map | `akdb_moc_v1.0.md` | map | `MOC` | document, dictionary |

Classes: `entity` — knowledge about a subject, accumulates from many sources; `source` — a verbatim carrier, a document or
a table whose value is the exact text; `record` — written by a human, not derived from sources; `map` — navigation
with no claims of its own.

Worth knowing about the table:

- Unsure about the type — take `concept`; the borders of neighbouring types are worked out in the "Rules for the AI"
  block of every template and in the type-choosing instruction (below).
- The `Documents` section of cited documents is not yet in the project's folder scheme and the engine creates no
  cards of this type: the template describes the form in advance (section 10).
- A decision, a question and a correction are **records**: a human writes them. The templates also describe the
  master file in `Raw/`, but for now records live where they did: `Decisions/`, `Questions/`, `Raw/corrections/`.
  A correction is never a card in the base: `kb:correct` writes it into the target card.
- `spec` (the `Specs` section) stays a valid `type` value **with no template**: such cards are written by hand
  from `Templates/spec_template.md`, and the check does not judge them.
- Not in the set: a rule, an interface and a data entity as separate cards (they are the "Rules" section of an entity,
  the "Interfaces" of a system, a concept with "Properties"), specifications, deliveries, acceptance; person,
  organisation, place and project — personal data, such cards do not occur in the bases.

### How to pick a type: `Prompts/KB_classify.md`

Reading thirteen templates to pick one takes long. For this there is a short instruction, "How to determine the card
type" (up to 9000 characters, in Russian like the templates): pick the type by its steps, then open that one template.

| Step | What it is about |
|---|---|
| 0 | this is knowledge about a subject, not a task, not our own artifact, not a work status, not an opinion and not a table of contents |
| 1 | the knowledge is extracted from a source or belongs to a human (then it is a record, step 4) |
| 2 | the table "if the source is… → type, template, section": the **first match from the top** is taken |
| 3 | borders of neighbouring pairs: `concept` and `glossary`, `requirement` and `process`, `reference` and `concept` … |
| 4 | records — a decision, a question, a correction, a reference: a human writes them, the file lies in `Raw/` |
| 5 | maps are created by the engine only |
| 6 | one entity — one card: find the existing one and extend it first, do not start a second |
| 7 | the answer format |

The answer is one line: `type: <type>; template: <template file>; section: <section>; why: <reason>`; if no type fits —
`type: none; why: <reason>`. Inventing a type or picking a "similar" one is forbidden. `card_types.classify_answer`
checks such an answer: the type, the template and the section are compared with the registry, and an answer that does
not fit is rejected, not "guessed".

The file belongs to the project: edit it in any editor, keeping steps 0–7 and the answer line of step 7. A kit update does
not overwrite your file: the new version lands next to it as `KB_classify.md.new`. The kit does not add a project's own
type to the tables of steps 2 and 3 — the project adds the row. The built-in agent puts steps 2 and 3 into its parsing
requests (section 8).

### The `cards:` block in `aurora.config.yaml`

A project overrides a type's title and description and switches a type off with the `cards:` block — two-level, like
`artifacts:`: the key is the type identifier, under it lines `field: "value"`. There are three fields:

| Field | Value | Default |
|---|---|---|
| `title` | the type's label in the panel and the catalogue, one line up to 80 characters | `name` of the template passport |
| `description` | the type's description, one line up to 220 characters | `description` of the passport |
| `enabled` | `"true"` or `"false"` — as a string | `"true"` |

The section, class, template, kinds and statuses belong to the template: a line with such a field and a line about a
type outside the registry are skipped, and `kit:doctor` names them with warnings. There is no block by default — an
example is commented out at the end of the config:

```yaml
# cards:
#   glossary:
#     title: "Термин (словарь заказчика)"
#     description: "Определение термина из словаря заказчика; без собственных свойств."
#   status-model:
#     enabled: "false"
```

The block is edited in the panel or by hand; the settings form (`aurora.py setup`) keeps it, like any section it does
not know. What `enabled: "false"` does **now**: the check names the cards of that type (V-03 "the type is switched
off"), `classify_answer` rejects such a type, the catalogue shows "no", and `kit:doctor` reminds you if cards of a
switched-off type remain in the base. The cards themselves stay and are checked as before; parsing does not offer such
a type and creates no new cards of it.

### The type catalogue: `AuroraKnowledgeDB/meta/card_types.md`

A short list of types for assistants and requests: a table (type, name, description, template, section, class, kinds,
"Enabled"), a paragraph on where records are written, and a line "Собрано: date · types · enabled". The catalogue is
**generated** — it is not a card (`meta/` is a service folder), manual edits are lost. It is built from the effective
registry — the templates and the `cards:` block — on project creation (`aurora.py new` and the panel button); on
`aurora.py update <project> --apply` if the file is missing or behind the templates and the block (the dry run prints
"the type catalogue will be rebuilt"); on saving the type settings in the panel; and by `kb:cards --write` (without
`--apply` — a plan, with `--apply` — a write).

The file is not rewritten when only the build date changed, so there are no daily edits in git; above 6000 characters
the descriptions are cut to 120. A project that keeps the catalogue for itself (`aurora.update_ignore.txt`) is not
rebuilt by the update: `kit:doctor` says so, and `kb:cards --write --apply` builds it.

### Panel settings: "Project settings → Card types"

The panel stores nothing itself: it writes the project files that `kb:cards` reads.

- **The types table.** The kit's thirteen types and the project's own: title and description (overridable; a value
  equal to the template default is marked "default", the arrow restores it), template and version, section, class,
  source, the "Enabled" switch. Saving writes the `cards:` block and rebuilds the catalogue. The block is written whole
  from what is typed: a default and an enabled type drop the entry, entries for types outside the registry are left
  alone, comments inside the block are lost, the rest of the config is untouched. A type the registry itself switched
  off (`V-REG-1`) cannot be switched on here.
- **How to determine the card type.** The text of `Prompts/KB_classify.md` (no project file — the kit's text is shown),
  a character counter and a check of length, steps 0–7 and the answer format. "Restore the kit's text" (with
  confirmation) replaces the project file with the kit's copy. Only this file is written, atomically, in its encoding
  and line endings; the check does not forbid saving — the file belongs to the project.
- **State of the base by templates** (read-only), from `kb:cards --stats`; "Check" runs `kb:cards --check --summary`.

Writing is guarded against a lost update, like the "Files" tab: the page returns the digest of what it read (the
`cards:` block by meaning, without comments; the instruction by bytes), and if the disk holds something else already
(an editor, `git pull`, a kit update), the 409 answer writes nothing and the panel offers "keep mine" or "take from
disk". The table and the instruction are saved apart and never reset each other's unsaved edits. A catalogue the
project keeps for itself (`aurora.update_ignore.txt`) is not rebuilt by saving. The switch works as described above:
parsing and the built-in agent's requests take the sections of enabled types only.

## 4. Type, kind and status — three different axes

| Axis | Field | Answers the question | Values |
|---|---|---|---|
| Type | `type` | **what** the card is about and **by which template** it is made | registry identifiers (section 3) |
| Kind | `kind` | **who may edit the body** | `knowledge` — the model writes a thesis; `dictionary` — carried over whole; `document` — verbatim, never changed |
| Status | `status` | **the level of trust** (for a decision — the cycle `proposed → accepted \| rejected → superseded`) | `knowledge`, `draft`, `placeholder`, `deprecated`; `index` for maps |

The template sets which combinations are allowed: the passport fields `kinds` and `statuses`. The type is
written by the section folder: a card in `Processes/` is `type: process`.

## 5. The card contract

**The head.** The required fields depend on the class. For `entity` and `source`: `title`, `type`, `template`,
`status`, `created`, `updated`; `kind` if the type has kinds; for a machine card (`built: machine`) that is not
a stub also `sources` and `source_synced`. For `map`: `title`, `type`, `template`, `status`, `updated`. The other
fields come from the common dictionary (`aliases`, `tags`, `sources`, `built`, `related`, `trust`, `distilled`,
`summary` and others) or from the type's `fields_extra`. The `template` field names the template file
(`template: akdb_concept_v1.0.md`); older cards do not have it — that is not an error but a "pre-template" card.
Dates are `YYYY-MM-DD`. The templates have no `schema_version` field: the form version of a card is set by
`template`; older cards may keep the field and it is not touched. The head is read by the engine's single
parser; nested YAML is not allowed.

**The body** is split into an own part and a service tail (the service headings are the engine's constants and stay in
Russian). Only the own part is checked:

```
[head]
[the leading definition paragraph]           ← the first line, one or two sentences
[## the type's sections — a closed list, in order, no repeats]
[## Расхождения (discrepancies)]             ← written by the engine
[> 🎙 Из встречи …]                          ← a mark, if the sources include Raw/meetings
[## Под вопросом]
[## Источник (перенесено дословно)]          ← verbatim text from here on, not checked
[## Исправления человеком]
[## История изменений]
[## Слияние]
```

Rules:

- The leading paragraph is a definition: not a heading, list or table, up to 600 characters. For `kind: dictionary`
  and `document` the own part may be a pure carry-over, for a stub (`status: placeholder`) — a stub text.
- A `##` heading in the own part must be named in the type's list, in its order; there are no empty sections.
- Template leftovers (`{{…}}`, `<!-- ИНСТРУКЦИЯ`, `[ПРИМЕР]`, a `TODO:` line) are not allowed in the own part.
- A `# Name` heading is allowed; if it is in the model-written part (not a verbatim carry-over) it equals `title`.
- The tail is service, in the order shown above; the engine appends `Не путать`, `Что относится к теме`,
  `Упоминается в` and `Названо в карточках` below the own part itself.
- If `sources` include `Raw/meetings/…`, the own part must carry the mark `> 🎙 Из встречи …`.

The own-part sections the model writes (optional, in this order):

| Type | Sections |
|---|---|
| `concept` | `Свойства`, `Правила` |
| `glossary` | `Синонимы` |
| `process` | `Участники`, `Шаги`, `Исключения`, `Результат` |
| `system` | `Интерфейсы`, `Данные` |
| `role` | `Ответственность`, `Права и ограничения` |
| `status-model` | `Состояния`, `Переходы` |
| `requirement` | `Условия` |
| `reference`, `cited-document`, `decision`, `question`, `moc` | no own sections: the body is a carry-over or generated |

## 6. Links

A card without links is a lint error; in the templates this is a number, the `min_links` field — how many different
cards it must name (the `[[…]]` links of the own part plus the typed head fields; a link to itself and repeats do not
count). For stubs, `kind: dictionary` and `document` it is not checked: the model did not write the body.

| Type | `min_links` | What is expected |
|---|---|---|
| `concept`, `system`, `role`, `status-model`, `requirement` | 1 | at least one neighbouring entity: part of what, what it uses, the object whose statuses are described, the source of the requirement |
| `process` | 2 | a participant (a role or a system) and the data, the result or a status model |
| `decision`, `question`, `correction` | 1 | the affected card (for a question — what it blocks, for a correction — what it corrects) |
| `glossary`, `reference`, `cited-document`, `moc` | 0 | a term may have no links; an entry from a map is mandatory |

A link is named with a verb ("is part of", "uses", "forms"), not "see also". The `related:` field is kept by the
engine (`kb:links --cards`); nobody fills it by hand.

Typed head fields are a closed list; the value is a `[[Name]]` link (one or a list), except `corrected_by`
(correction file names, as `kb:correct` writes them) and `answer_source` (a path in `Raw/`); `corrects` also
accepts a name without brackets:
`supersedes`, `superseded_by` — on entities and decisions; `same_subject` — on entities (reserved, the engine does not
set it yet); `blocks`, `answer_source` — on a question; `corrects` — on a correction; `corrected_by` — on entities and
verbatim carriers (set by `kb:correct`); `decisions` — on a requirement; `prev`, `next` — on a cited document;
`part_of` — on the parts of a split card.

The base graph builds edges from these fields: `part_of` — "part of a document", `prev` and `next` — "next".

## 7. Violation codes (V-NN) and the `kb:cards` command

Severity: `error` rejects a write (on creation any, on an edit only a new one) and in a stamped card is a `kb:lint`
error; `warn` goes into the report, `info` into the statistics. `kb:cards --check` without `--strict` shows errors as warnings.

| Code | Severity | Rule | Release |
|---|---|---|---|
| V-01 | error | the head is not found or cannot be parsed | works |
| V-02 | error | a required head field is missing (a missing `template:` alone is an `info` note: the card is pre-template) | works |
| V-03 | error | `type` is not in the registry, the type is switched off or differs from the template's type | works |
| V-04 | error | `template:` does not name an existing template file of its type | works |
| V-05 | error | `kind` is outside those allowed for the type | works |
| V-06 | error | `status` is outside those allowed for the type | works |
| V-07 | warn | a head field outside the common dictionary and `fields_extra` | works |
| V-08 | error | a date not in `YYYY-MM-DD` | works |
| V-09 | error | a `##` heading of the own part is not in the type's list and is not a service one | works |
| V-10 | error | sections are out of order or repeated; service headings are out of order | works |
| V-11 | — | reserved, not used | — |
| V-12 | error | the first line of the own part is not a definition, or the own part is empty | works |
| V-13 | error | `{{…}}`, `<!-- ИНСТРУКЦИЯ`, `[ПРИМЕР]` or `TODO:` is left in the own part | works |
| V-14 | error | a `## Расхождения` line does not follow the grammar | later: with the discrepancies section |
| V-15 | warn | fewer outgoing links than `min_links` | works |
| V-16 | error | a typed field is out of place or its value is not a link | works |
| V-17 | error | a record: no cycle fields, the master file is not in `Raw/`, the mirror does not point to the master | later: with records in `Raw/` |
| V-18 | info | `template:` is older than the type's highest version | works |
| V-19 | error | `sources` include a meeting transcript but the meeting mark is missing | works |
| V-20 | warn | no `built: machine` and no `sources`: the origin is unknown | works |
| V-21 | error | a cited document: no `doc`, `unit` or `cite`, or the kind is not `document` | later: with cited documents |
| V-22 | error | a map has no `<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ` mark | works |
| V-23 | error | the `# Heading` of the own part is not equal to `title` | works |
| V-24 | warn | a service section (`Не путать`, `Что относится к теме`) stands in the own part | works |
| V-25 | error | `status: index` on a card that is neither a map nor "split" | works |

The check is a pure function: no network, no model, deterministic; 3000 cards take seconds.

### The `kb:cards` command

Run in the project root: the `kb:cards` command in the panel, `python3 .aurora/scripts/card_types.py` in a terminal:

| Call | What it does |
|---|---|
| `kb:cards` or `--list` | a table of the effective registry: type, name, class, section, template file, where it came from |
| `--show TYPE` | one type's passport, the template versions and the model extract |
| `--check [PATH…]` | check the base's cards (or only the named files and folders) against the templates of their types |
| `--check --strict` | the same, but errors stay errors and give exit code 1 |
| `--stats` | cards in total, by template versions, without a template, with violations by code |
| `--write` | the type catalogue `AuroraKnowledgeDB/meta/card_types.md`: without `--apply` — the plan only, with `--apply` — the write (if the catalogue file was edited by hand and not committed — with `--allow-dirty`) |
| `--guide` | the effective text of the instruction "How to determine the card type": the project's `Prompts/KB_classify.md`, or the kit's file if there is none or it is empty |
| `--all`, `--summary`, `--json`, `--root PATH` | with `--check` and `--stats` — include the archive (`_archive/`); with `--check` — the summary line only; machine output; the project root |

Exit code: **0** — no errors (in the ordinary mode always, if the check took place), **1** — errors in strict
mode, **2** — a failure: no base folder, no templates (the registry is empty — update the project with
`kit:update`), no such file or type, no instruction. The command writes no cards; the only thing it writes is the type catalogue, by `--write --apply`.

How to read the `--check` output:

```
kb:cards: проверено 13 · с шаблоном 12 · до-шаблонных 1 · ошибок 0 · предупреждений 4 · сведений 1 (советы: …)

V-06 · warn · карточек 1: статус вне допустимых для типа
   - AuroraKnowledgeDB/Concepts/Накладная.md:4: статус «imported» не из допустимых для типа «concept»: …
   как исправить: kb:trust пересчитывает статус по классу источника
```

(The output is in Russian, like all engine output.) The first line is the summary: how many were checked, how many have
`template:`, how many are "pre-template". Then groups by code: code · severity · how many cards, up to eight examples
"path:line: message" and a hint on how to fix it. Registry problems (an incomplete passport, two types in one section)
are printed as "проблема реестра" lines.

## 8. Relation to `kb:lint` and `kit:doctor`

`kb:lint`: a template violation in a card with the `template:` stamp is an **error** (group "нарушения шаблона у
карточек со штампом", exit code 1, the pre-commit hook sees the rise); in a card without the stamp it is a note. The
summary line "карточек N, ошибок M" gets "· до-шаблонных: K" after the errors once the base has stamped cards (no
stamps — unchanged). Then the section **"### шаблоны карточек (предупреждения, не ошибки)"**: V-NN codes, full run only.

`kit:doctor` adds notes: how many of the thirteen kit templates are delivered (not all — `kit:update`), registry
problems, how many cards have no `template:`, whether the type catalogue is current, whether cards of a switched-off
type remain. They do not affect the verdict; `cards:` block lines the registry did not accept are warnings.

## 9. Template versions

The version is part of the file name (`_v1.0`). Old versions are not deleted: a card made by `v1.0` is checked
by `v1.0` until it is moved to a new one. Versions are compared as numbers (`1.10` is newer than `1.9`), and
`kb:cards --stats` counts how many cards lag behind the highest version of the type (code V-18).

**A released `akdb_*_v1.0.md` file is not edited.** The seed rule delivers an edit to old projects only as
`.new`, so a fix or an extension is a new version (`v1.1`, `v2.0`) with the passport field
`migration: none|auto|llm` (`none` — old cards stay valid for good).

### Your own type or your own edition of a type

Put a file `akdb_<type>_v<version>.md` with a passport, like the kit's templates, into the project's `Templates/`.

- **Your own edition of a kit type:** the same `card_type`, `section` and `card_class`, any version. The project
  file wins over the kit file. You must not change `section`, `card_class` or `card_type`: the registry rejects
  such a template (the `V-REG-2` problem).
- **A new type:** a new identifier (2–31 characters: `a-z`, `0-9`, `-`), a new section — a `PascalCase` folder
  inside `AuroraKnowledgeDB/`, declared in `paths.extra_structure_dirs` of the config; class `entity`, `source`
  or `record` (only the kit creates maps). One section — one type: two templates with one section switch both
  types off until fixed (the `V-REG-1` problem).

A project adds the new type's row to the table of step 2 of `Prompts/KB_classify.md` — otherwise the classifier will not
offer it; the catalogue gets the type by itself (`kb:cards --write --apply`). Today such a type enters the registry and
the check; the section of such a type enters the list of sections of the parsing request, the engine creates cards by
its template, and the single write entry checks the head and the body.

## 10. What the next releases bring

Not here yet and not described as ready: records in `Raw/decisions`, `Raw/questions`, `Raw/reference` and `kb:record`;
cited documents (the `Documents` section, `kb:cite`); a mass conversion of the base to templates (`kb:conform`); the
`## Расхождения` section and the ladder "trust → question → correction". "A card only by a template" already holds
for new and edited cards, but only a touch (an edit, a thesis, repair) sets the stamp: most of an old base stays
"pre-template", and its violations are notes; they become errors after `kb:conform`.
