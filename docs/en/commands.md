# Command reference

A short English reference of every command in the registry (`commands.txt`). The complete
reference with modifiers, taken live from the scripts' `--help`, is the generated Russian
[../commands.md](../commands.md), or `python3 aurora.py list <project>` in a terminal.
Русская версия: [../commands.md](../commands.md).

Short names in parentheses are historical aliases and always work. **Executor** marks the
border: **script** — deterministic mechanics, the result is reproducible; **model** — work
with meaning, done by the assistant by a procedure from `skills/aurora-vault/references/`;
**script + model** — a script counts and prepares, a model or a human decides. A command
that writes shows a preview without `--apply`.

This file is generated: `python3 scripts/kit_commands.py --en-md docs/en/commands.md`.
The descriptions live in `cockpit/i18n/data/en.json` — the same ones the panel shows.

## `kit:` — The engine and the project

| Command | What it does | Executor | Since |
|---|---|---|---|
| `kit:doctor` (`doctor`) | Project readiness: config, skills, secrets in git (config, skills, bots, `mcp.json` — one scanner shared with the hooks), engine version and the state of its copy on this computer (missing files by name), folder structure, file-name portability. `--structure` — details per folder outside the schema. | script | 1.0.0 |
| `kit:paths` | Path length on Windows, macOS and Linux: which config lines (Confluence roots, website addresses, source and project folders) will not fit into a path and what will not be loaded because of it; read-only, never blocks a run; the Windows root, when it is not this machine, is an assumption named in the report. | script | 1.182.0 |
| `kit:hooks` | Git hooks: pre-commit with the linter, the ratchet (errors may not grow) and the secret check; in the kit also commit-msg and pre-push, so internal names, secrets and kitchen branches do not reach history. | script | 1.3.0 |
| `kit:remap-sources` (`remap`) | Retarget cards' `source:` after a mirror moved (Confluence — by page_id, Jira — by issue key). | script | 1.7.0 |
| `kit:update` | Update the engine in a project to the kit's version (preview, then `--apply`); `--structure-only` — only the schema folders; `--engine-only` — only the engine in this machine's `.aurora/` (a clone without `.aurora/` is completed; project files and git are not touched — the panel does this itself when you pick the project); `--rollback` — return the project's engine from the snapshot taken before the last update (without `--apply` it only previews); `--allow-downgrade` — update even when the project's engine or stamp is newer than the kit (a deliberate rollback). | script | 1.3.0 |
| `kit:i18n` | Panel interface languages: completeness of every catalogue against the markup; `--new <code> <name>` creates a language. | script | 1.98.0 |
| `kit:skills` | Put Aurora's skills into the agent's shared folder (`~/.claude/skills`) so `/aurora-vault` and `/aurora-dev` are found in any conversation. | script | 1.54.0 |
| `kit:mcp` (`mcp`) | The base as a tool for any assistant: server check and a ready connection line. The server only reads — writing through MCP is impossible. | script | 1.66.0 |
| `kit:list` | This reference: commands, modifiers, what executes them, since which version. | script | 1.9.8 |

## `sync:` — Mirrors of external systems

| Command | What it does | Executor | Since |
|---|---|---|---|
| `sync:sources` (`sources`) | Source modules: what is installed and which mirrors are connected to the project. | script | 1.28.0 |
| `sync:confluence` | Deterministic mirror Confluence → `Sources/Confluence/` (module `confluence-dc`); re-downloads only pages whose version changed. | script | 1.6.0 |
| `sync:web` | Pages by a list of URLs → `Sources/Web/` (module `web`); trust is a tick per link, written into the saved file's header. | script | 1.101.0 |
| `sync:jira` | Deterministic mirror Jira → `Sources/JIRA/` (module `jira-dc`). | script | 1.9.0 |
| `sync:audit` (`audit`) | Mirror integrity: missing / orphan / collision / stale state / foreign files, including schemas of pages that no longer exist; walks every connected module. | script | 1.3.0 |
| `sync:diff` (`diff`) | Drift: a source changed after the knowledge was built. | script | 1.9.1 |
| `sync:jira-status` | The reverse flow: issue statuses → candidates for `req_status`, issues without requirements, links by mention. | script | 1.9.9 |

## `kb:` — Extraction and the life of knowledge

| Command | What it does | Executor | Since |
|---|---|---|---|
| `kb:build` (`build`) | Card extraction: plan, batches and ledger by script, the extraction itself by the model; `--slice`, `--card`, `--append`, `--reopen`, `--thin`. | script + model | 1.0.0 |
| `kb:ingest-office` | docx/pdf/xlsx/pptx from `Raw/` → Markdown transcripts next to the original; scans are read by a vision model if configured. | script | 1.5.0 |
| `kb:ingest` (`ingest`, `ingest-raw`, `ingest-meeting`, `ingest-tz`) | A document from `Raw/` → cards linked to the primary source; the branch is chosen by the document: a spec → REQ with `tz_ref`, a meeting transcript → summary, DR, REQ and facts. | model | 1.0.0 |
| `kb:repair` (`fix`) | Repair: broken links, homoglyphs, legacy frontmatter, fields outside the schema, stubs for links; modes by flag (`--titles`, `--terms`, `--gone-sources`, `--template`, `--copies`, `--aliases`, `--sections`, …). Renaming and moving steps of one run combine on a single card (name from the rename, folder from the move); a taken or contradicting destination is left in the report. Preview without `--apply`. | script | 1.3.0 |
| `kb:names` (`fix`) | File and folder names by the strictest rules of Windows, macOS and Linux: long Confluence paths, card names with links, case-only differences. | script | 1.140.0 |
| `kb:translit` (`translit`) | The Latin↔Cyrillic name dictionary: finds cards with a transliterated name and Russian content, keeps one translation per concept in `meta/translit.md`, renames by it (`--rename`). | script | 1.100.31 |
| `kb:dedupe` | Duplicates: search and batch merge by rule (`--merge-all`), including one name in different spellings; `--merge-audit` is read-only: merged cards in `_archive` whose name, synonyms or sources did not reach the winner (a one-off check of bases after the multi-donor merge defect; a source that no longer exists on disk is not counted as lost but is reported on a separate not-checked line: the winner may have had it remapped or removed by `--gone-sources`; a machine transcription that has a human copy and was dropped by `--copies` is not counted either; a template-magnet source dropped by `--template` may show up as a finding). | script | 1.3.0 |
| `kb:twins` (`twins`) | Cards carrying the same knowledge under different names: compares texts, not names. Merges nothing — names the group. | script | 1.100.31 |
| `kb:split` (`split`) | Cut a bloated card by its headings: the parts become atomic cards, the card becomes a document map linking to them. | script | 1.62.0 |
| `kb:embed` (`embed`) | The base's semantic index: card vectors for search by meaning; the index lies outside git and is rebuilt; texts go to the same gateway as the agent. | script + model | 1.65.0 |
| `kb:moc` (`moc`) | Maps of content by groupings (terms, concepts, roles, data…), per source document (`--by-source`), per artifact code (`--by-code`) and a list of abandoned cards. | script | 1.32.0 |
| `kb:index` (`index`) | Regenerate sections' `_index.md`; hand-written ones are not touched but stale ones are named as a finding (exit code 1). | script | 1.9.4 |
| `kb:scrub` (`scrub`) | Personal data: find and cover with markers; the mode is `privacy.scrub`. | script | 1.9.6 |
| `kb:schema` | The card schema version (`schema_version`) and migration of the base between versions along a declared chain. | script | 1.12.0 |
| `kb:correct` (`correct`) | Corrective artifacts: a human writes in their own words what is wrong in a card and how it really is. Lives in `Raw/corrections/` and is applied at EVERY build; `--check`, `--retire`. | script | 1.99.0 |
| `kb:decide-accept` (`decide-accept`) | Accept or reject a proposed decision (DR): `--by @name` — who accepted (a person; a model's name is refused), `--reject --reason "…"` — reject with a reason. Accepting checks the form (at least two options, a refusal reason for the rejected ones, the affected cards exist in the base, no `{{…}}` and no unresolved "Under question") and puts a body fingerprint into the head: an accepted decision is immutable, `kb:lint` reports a body changed after acceptance as an error; `--check` — the form of every decision. | script | 1.185.0 |
| `kb:supersede` (`supersede`) | Replace knowledge with history: deprecated → `_archive`, links rewritten. A requirement is not replaced without `--changed` and `--migration`. | script | 1.8.0 |
| `kb:links` (`links`, `graph`) | The link graph: Requirement Yogi keys and story numbers; `--cards` carries links into cards' `related:`; `--cards-json` exports the base's own graph for the panel. | script | 1.19.0 |
| `kb:map` (`map`) | What the graph says: communities that have grown to their own map, bridges between topics and islands unreachable by links. | script | 1.66.0 |
| `kb:graph-export` | The base's graph outward: `meta/graphify/graph.json` in graphify format with link type and confidence, themes by modularity, notes in `MOC/Сообщества/`, an offline graph page. | script | 1.138.0 |
| `kb:code-graph` | The project's code and SQL in the graph without a model: tables from SQL on pages and in `*.sql`, table pairs of one query, cards naming a table; code from `graphify: code_dirs`. | script | 1.138.0 |
| `kb:reset` (`reset`) | Wipe the base and rebuild: removes what will be rebuilt (a card with a live `source:` or `built: machine`), touches nothing outside `AuroraKnowledgeDB/`; cards of unknown origin stay (`--list-unknown`, `--drop-unknown`); rollback from git. | script | 1.24.0 |
| `kb:lint` (`lint`) | Mechanical errors of the base: links, frontmatter, card kinds, artifacts in knowledge, secrets, cards without links. | script | 1.0.0 |
| `kb:next-id` (`next-id`) | Issue the next number of REQ, SPEC, Q, DR, an agent artifact (ART) or a Gherkin scenario (SC) under a lock: two parallel calls get different numbers, the number of a deleted card does not come back; `--peek` shows the number without taking it; `--check` finds identifiers carried by two different files (`kb:lint` shows them as a warning); `--list` — the namespaces. | script | 1.185.0 |
| `kb:cards` | Card types of the base: the registry built from the `akdb_*` templates and the `cards:` block of the config (`--list`, `--show`), a check of cards against the template of their type by V-NN codes (`--check`: errors are shown as warnings, `--strict` for strict; a violation in a card with the `template:` stamp is a `kb:lint` error), a summary by template versions and pre-template cards (`--stats`), the text of the instruction "How to determine the card type" (`--guide`), the type catalogue `meta/card_types.md` (`--write`: the plan, with `--apply` — the write); writes nothing else. | script | 1.173.0 |
| `kb:conform` | Migration of the base cards to templates without a model: header repairs by a fixed list of rules (`built: mac`, `pdated`, a single `source:`, missing `kind`, `status`, dates), the `template:` stamp where a card passes the template of its type strictly, and the report `meta/conform_report.md`; without `--apply` it only scouts and writes nothing. `--finalize --apply` declares the migration complete (`meta/conform_state.json`): after that `kb:lint` treats a card without `template:` as an error. The bodies of `document` and `dictionary`, human corrections, the history and the source are never changed; violations in the structure of the own part of a machine card are laid out by `agent:conform`. | script | 1.181.0 |
| `kb:question` | Open a question to the customer (Q-NNN): to whom, what it blocks, the deadline. | model | 1.4.0 |
| `kb:answer` | Record an answer: close the question and carry the knowledge into a REQ / spec / DR. | model | 1.4.0 |
| `kb:decide` (`decide`) | Draw up a Decision Record (+ supersede an old DR). | model | 1.0.0 |
| `kb:garden` (`garden`) | Weekly hygiene: a checklist of four scripts, sorting out by a human. | script + model | 1.0.0 |
| `kb:trust` (`trust`) | Recompute cards' trust class: a Confluence section with the "trust" tick, a reference branch, then links — the weakest decides (result: status knowledge/draft and the basis in words; a human assigns no trust). | script | 1.89.0 |
| `kb:kind` (`kind`) | A card's kind: dictionary, document or knowledge — it decides whether a model may rewrite the body (result: the `kind` field). | script | 1.90.0 |

## `ctx:` — Using knowledge

| Command | What it does | Executor | Since |
|---|---|---|---|
| `ctx:context` (`context`) | A context pack: selection, status filter, trust headers, a record in `usage.log`; `--mode generate|review|ask|evaluate`, `--budget`, `--save`. | script | 1.8.0 |
| `ctx:ask` (`ask`) | An answer from the base with quotes; "why not X" — including rejected DRs. | model | 1.0.0 |
| `ctx:eval` (`eval`) | A regression run of golden questions after syncs and migrations. | model | 1.0.0 |
| `ctx:retro` (`retro`) | Lessons learned: what the base did not know when we were wrong. | model | 1.0.0 |

## `make:` — Producing artifacts

| Command | What it does | Executor | Since |
|---|---|---|---|
| `make:kinds` (`kinds`) | The project's artifact registry: template, result folder, project prompt, the "no technologies" rule per kind, the final-text boundary, a review before publishing (`review`) and a Requirement Yogi key mask (`ref_mask`). | script | 1.69.0 |
| `make:create` (`create`) | An artifact in `Artifacts/<type>/` — only a standard type from conventions.md. The manual path; the main one is `agent:make`. | model | 1.1.0 |
| `make:review` (`review`) | Check an artifact's quality against the base. | model | 1.0.0 |
| `make:story-lint` (`story-lint`) | Artifact form by code, without a model or a network: template sections, the "As / I want / So that" formula, "Implements", precondition statuses, the Action / Reaction pair, at most 10 steps and 5 criteria groups, leftover `{{…}}`; profiles us, ac, spec; `--gate` — a verdict for a route (`--gate=warn` — the rollout mode). | script | 1.185.0 |
| `make:review-auto` (`review-auto`) | Review of a story or algorithm without a human (a Confluence page — `--page`; a local artifact or a mirror page — `--file`, offline: related material is read from the base and the mirror; a "no" is accepted only with a quote found in the text; `--apply` writes the result into the artifact head, and the `ship:publish` gate sees how fresh the review is): a closed checklist from `review_v2.1.md`, three model runs vote on each question, a script computes the score and verdict; `--cql`, `--as-of`. | script + model | 1.119.0 |
| `make:spec` (`spec`) | A feature specification from REQ and `knowledge` cards (SDD). | model | 1.0.0 |
| `make:spec-pack` (`spec-pack`) | A spec bundle: grounds, DRs, abbreviations, DoR risks — one self-contained file. | script | 1.9.4 |
| `make:validate` (`validate`) | Compare a contractor's implementation and tests with the spec's scenarios. | model | 1.0.0 |
| `make:assemble` (`assemble`) | Assemble a deliverable (OPZ/PMI/user guide) from the base by a template. | model | 1.0.0 |

## `ship:` — Outward

| Command | What it does | Executor | Since |
|---|---|---|---|
| `ship:publish` (`publish`) | An artifact → a generated Confluence page; knowledge cards do not go out. The body is cut at the marker line: "Clarifications", "Assumptions", "In question" stay in the analyst's draft. For an artifact kind with a declared review (`review: us or alg` in `artifacts:`) only an artifact with a fresh review "ready to hand over" goes out (`make:review-auto --file … --apply`): no review, a text changed after it, or a lower verdict — code 2 and a list of reasons; the bypass is `--allow review` (recorded in the head as `review_bypass`). | script | 1.9.6 |
| `ship:export` (`export`) | A deliverable → docx/pdf (pandoc, a corporate template). | script | 1.5.0 |
| `ship:release` (`release`) | Freeze the delivered version: a snapshot, a base commit, a date. | script | 1.9.1 |
| `ship:acceptance` | Acceptance results and sorting out the customer's remarks. | model | 1.4.0 |

## `ops:` — Management and reporting

| Command | What it does | Executor | Since |
|---|---|---|---|
| `ops:stats` (`status`, `stats`) | The base's health dashboard: statuses, risks, metrics; `--append-metrics`. | script | 1.3.0 |
| `ops:retrieval` | Which cards come first for analysts' real queries and what changed since last time: a ranking watchdog. | script | 1.96.0 |
| `ops:search-quality` (`search-quality`, `поиск`) | Search quality as a number: self-search — a card's thesis as a question, the right answer known by construction. R@1/R@5/MRR. | script | 1.100.28 |
| `ops:gaps` (`gaps`) | Meaning gaps: a concept named but with no card, a link named but not placed, lonely cards, a thesis behind its source, a vanished source. | script | 1.100.35 |
| `ops:todo` (`todo`) | What is left: what routes and commands will finish, what repair did not take, what depends on project settings and where a human decides. | script | 1.84.0 |
| `ops:decisions` (`decisions`) | The decision journal: the state of every DR, owner, date, how many cards it affects, proposals older than 14 days that are not accepted, accepted ones with a changed body, form remarks; `--json` — machine form; two runs on one day print the same. | script | 1.185.0 |
| `ops:scenarios` (`scenarios`) | Gherkin scenarios (`Artifacts/tests/**/*.feature`): every scenario has a stable identifier in the `@id:` tag and a link to a criterion `@covers:<key>`, and a Requirement Yogi key of the story sits above Feature. The report: a scenario without `@id`, one `@id` on two scenarios, no `@covers`, `@covers` on a key that is neither in the artifacts nor in the mirror; `--assign --apply` puts `@id:SC-NNNN` in (the number is issued by `kb:next-id`), without `--apply` — a preview. | script | 1.185.0 |
| `ops:coverage` (`coverage`) | Requirement coverage by the Requirement Yogi keys of the mirror (`ry_defines`, `ry_links`): a story without a link to a requirement, a requirement without a story, a requirement without a criteria page (AC), a link to a key that is not in the mirror; `--requirement KEY` — the wave of edits when the customer's requirement changes: the stories, criteria and project artifacts that name it. Items of the terms of reference without a key are not counted — they need the text checked against the registry. | script | 1.185.0 |
| `ops:impact` (`impact`) | What depends on a card; `--explain` — what a document is built on. | script | 1.8.0 |
| `ops:trace` (`trace`) | Tracing: spec item → REQ → SPEC → Jira → AC → PMI → acceptance. | script | 1.0.0 |
| `ops:trace-table` | The "artifact ↔ issue" table: direct links, links by code and tracing by names two hops deep; trust is computed on it. | script | 1.89.0 |
| `ops:questions` | The question registry: open, overdue, what they block. | script + model | 1.4.0 |
| `ops:report` (`report`) | The analyst-efficiency dashboard: weekly activity in Jira and Confluence, issue transitions; settings — in the `reports:` section of the config. | script | 1.78.0 |

## `agent:` — The built-in agent

| Command | What it does | Executor | Since |
|---|---|---|---|
| `agent:aliases` | The agent resolves synonym conflicts: clarifies where cards differ and defers real duplicates to a human; edits only through engine commands. | script + model | 1.57.0 |
| `agent:build` | The agent parses a batch of sources into cards (result: new cards, trust is not assigned): storyboard, topic boundaries, names, the parse mark; `--until-done`, `--critic`. | script + model | 1.58.0 |
| `agent:distill` | Write theses for `knowledge` cards: the model reads the carried-over text and writes a definition, the verbatim source goes under it; Momus checks each; `--recheck`. | script + model | 1.90.0 |
| `agent:extract` (`extract`) | Move others' definitions out of cards: knowledge about another entity moves to its own card and a link takes its place; the move is verbatim. | script + model | 1.100.32 |
| `agent:twins` | Decide the fate of cards with matching text: one entity or different is decided by the model from the text. A merge is reversible. | script + model | 1.100.32 |
| `agent:golden` | Golden questions with lost targets: a target that changed its name (merge, synonym) is moved mechanically; for the rest the model looks among candidates for the card whose text holds the reference answer — none has it, the row stays and lint shows the lost knowledge. | script + model | 1.152.0 |
| `agent:tasks` (`tasks`) | Return to a subject the knowledge of Jira issues that settled as separate cards: an issue is work, not an entity; its code and link are appended to the subject's card. | script + model | 1.100.38 |
| `agent:clashes` (`clashes`) | Contradictions between cards about one subject: both sides are quoted verbatim, who is right is decided by a human. | script + model | 1.100.35 |
| `agent:relink` (`relink`) | Place links inside finished theses without rewriting the text: the model inserts only markup, the engine strips it and checks the original character by character. | script + model | 1.100.36 |
| `agent:conform` | Lay out the own part of a machine card by the sections of its template with a verbatim transfer of lines: the model names only the numbers of the lines already there (the definition, the rest of the paragraph, the lines per type section) and writes no text; the script places the headings, checks that the set of lines is unchanged and the result passes the template strictly, and only then writes it with the `template:` stamp; a card that fails the check is left as it was and set aside until its file is edited. | script + model | 1.181.0 |
| `agent:translit` | Fill the Latin↔Cyrillic name dictionary for cards named by transliteration: a translation is work with meaning, done by the model. | script + model | 1.101.0 |
| `agent:make` | Produce an artifact of a registry type: enrichment from the base, a plan with questions to the analyst, worker, critic, Momus. The stages are marked in the header — after an interruption it resumes from the same point. Understands `@path`, `/skill`, `@server`. | script + model | 1.95.0 |
| `agent:decide` (`decide-from`) | A draft decision (Decision Record) from a source: a meeting protocol, an answer to a question (`--source Q-007`) or a project file. Every option and the decision itself carry a verbatim quote of the source; the code checks the quotes; what is not confirmed stays in the card under the marker in the "Under question" section, and only existing cards are taken as affected. Without `--apply` — a preview, with it — the card `DR-NNNN` with status proposed (the number is issued by `kb:next-id`); a person accepts the decision (`kb:decide-accept`). | script + model | 1.185.0 |
| `agent:ask` | Ask the base in your own words: the engine assembles context, the model answers only from the cards and puts a link on each claim; Momus checks the answer; the conversation goes to `meta/ask/`. | script + model | 1.63.0 |
| `agent:width` | Measure how many simultaneous requests each gateway holds: raise the load while throughput grows. | script | 1.99.2 |
| `agent:pydantic` | Pydantic AI settings: what goes to the gateway per role — model, reasoning on or off, chat-template fields and the timeout, whether the installed version passed the compatibility check. | script | 1.139.0 |
| `agent:ping` | Check the model chain by a live request: every backend, roles, speed; an empty answer counts as a refusal. | script | 1.56.0 |
| `agent:probe` (`probe`) | A live connectivity check: asks every gateway now; tells "no connection", "wrong key" and "no such model" apart and shows the model list; `--why` checks the request layer by layer. | script | 1.100.27 |

## `git:` — Project Git: server, update, push

| Command | What it does | Executor | Since |
|---|---|---|---|
| `git:status` | Project Git: branch, link to the server, how much is not sent and how much is new on the server, changes by group, conflicts, the last commit; `--fetch` asks the server first. Problems come with the cause and the fix. | script | 1.154.0 |
| `git:update` | Update the project from the server with the strategy from the setup (fast-forward only, merge, or put your changes on top); uncommitted work is not lost. A conflict gives the file list and a version choice in the «Git» section; `--auto` — a run by automation, only if it is switched on. | script | 1.154.0 |
| `git:push` | Send the project to the server: uncommitted work is committed first by the message template, a branch with no link to the server gets linked; the server moved ahead — `--update-first`. Sign-in comes from the project setup; the token gets into neither the URL nor the command line. | script | 1.154.0 |
| `git:commit` | Commit the project's changes with a message from the setup template or your own (`--message`); Aurora's hooks run, the ratchet is lifted only by an explicit `--skip-ratchet`. | script | 1.154.0 |
| `git:fix` | One-click fixes: abort or finish an update, take your or the server's version of a file, link the branch to the server, create the repository, switch to the project branch, remove a password from the server URL. | script | 1.154.0 |
| `git:check` | Check the connection: the URL, sign-in and rights through the provider API (if its module is installed), git access to the repository and the branch on the server. | script | 1.154.0 |

## `bot:` — Project bots: prompt, MCP, skills, schedule

| Command | What it does | Executor | Since |
|---|---|---|---|
| `bot:run` | Run a project bot (`--bot bots/<name>.md`): its prompt, MCP servers, skills and attachments go to the model with tools; the report and result files — in `Workspaces/bots/<bot>/<run>/`, the outcome — in the bot list and the «Cron» section. A bot with a schedule runs by itself. | script + model | 1.156.0 |
| `bot:check` | Check a bot without running it: the schedule (cron), MCP servers, skills and attachment paths — with the next runs. | script | 1.156.0 |
| `bot:list` (`bots`) | The project's bots: schedule and next run, the last run's outcome, problems. | script | 1.156.0 |

## `net:` — Connectivity: VPN profiles, probes, the kit's mode of work

| Command | What it does | Executor | Since |
|---|---|---|---|
| `net:status` | Availability of connections: profiles (VPN), probes, the internet and the model providers, the kit's mode of work and the reasons; read-only, changes nothing — profiles and probes are set up in the panel, the «Connection check» section. | script | 1.178.0 |
| `net:probe` | Check the connection now: all probes (or `--only` — one profile or probe); the state is updated with anti-flapping; exit code 1 when the profiles in need are down. With the feature off it does nothing. | script | 1.178.0 |

## `dev:` — QA of the engine

| Command | What it does | Executor | Since |
|---|---|---|---|
| `dev:qa-list` | Engine QA: which cases and scenarios exist, what is covered by an automatic test, which cases were never run. | script | 1.51.0 |
| `dev:qa-retrieval` | Output on the kit's reference corpus: a ranking watchdog in git. It fails itself when the card order changed — and shows how. | script | 1.96.0 |
| `dev:qa-check` | Integrity of the QA registry: duplicate numbers, links to non-existent cases, stale versions. | script | 1.51.0 |
| `dev:qa-gap` | What changed in code and what covers it; the decision "automatic test or case" is taken by the model. | script + model | 1.51.0 |
| `dev:qa-cover` | Cover what was done: a coverage table and a ready task for the assistant to add tests, cases and scenarios. | script + model | 1.53.0 |
| `dev:qa-run` | Run a scenario with a journal: automatic tests, a step checklist, a report in `Development/QA/runs/`. | script + model | 1.51.0 |
| `dev:qa-new` | Create a test case or scenario from a template with the next free number. | script | 1.51.0 |

## Exit codes and quality gates

A command reports its result by exit code, the same for every script and route step:

| Code | Meaning |
|---|---|
| `0` | ok: no findings |
| `1` | findings: the command worked and found things to fix; on a gate step it means "the gate is not passed" |
| `2` | execution failure: the command could not do its job (bad arguments, no data, a refusal) |
| `3` | unhandled script error (traceback in the output); treated as a failure |
| any other code `2` or above, below zero | failure; negative means the process was interrupted |

An ordinary route step that returns `1` does not stop the route: this is the normal state of a live base, and the summary lists such steps as "found things to fix". Code `2` and above stops it.

**Gates.** A step with `--gate` among its flags in `cockpit/scenarios.txt` is a quality gate: code `1` on it stops the route (reason `gated`, `failed=<command>`, the journal says "the route is stopped at the gate"). The step is not recorded as passed, so "Continue the route" repeats the gate after the fix. The run summary does not count this stop as an execution failure. The `--gate` flag is passed to the script itself, so it is set only on steps whose scripts support it. A gate's output must not contain network-failure signs ("timeout", "ssl", "connection error" and the like): code `1` with them is taken by the route for a network failure and waited out, not for a verdict (unless the gate named its verdict with a machine line, see below). In `aurora.py route` a stop at a gate exits `1` (stopped), not `2` (execution failure).

**Verdict line.** Besides the exit code, a gate script may print a machine line `AURORA-GATE {"gate": "<name>", "verdict": "pass|warn|fail", "items": [{"id": "...", "detail": "..."}], "waivable": false}` (`run_summary.emit_gate`). The console does not show it; the route lists the violated items (`id` and `detail`) in the journal and stores the verdict in the step record. Rules for a gate step: code `1` means not passed; code `0` with verdict `fail` is also not passed (a mismatch is read fail-closed, an unknown verdict counts as `fail`); `warn` with code `0` lets the route go on with a warning in the journal (adoption mode); code `0` without a line is fine: scripts of earlier releases and scripts without the adapter keep working, the line is a detail, not the verdict. A step that named its verdict with the line is not taken for a network failure. For a step without `--gate` the verdict is only recorded and shown.

**Scripts with a gate mode.** `spec_pack.py` (`make:spec-pack`): with `--gate` the handover risks of the Definition of Ready (requirements not `agreed`, open questions, grounds below `verified`, grounds missing from the base) give code `1` and an `AURORA-GATE` line (gate `spec_pack`, each risk with an `id` and a `detail`); no risks give code `0` and `pass`; an input error (no base, no spec) is code `2` in this mode. Without `--gate` the script behaves as before: risks are printed, code `0`. The other scripts with the flag: `kb_trace.py --requirements --gate` (`ops:trace`) — chain gaps requirement item → requirement → epic/story → acceptance (the "Gaps" section of the table, each with an `id` and a `detail`) give code `1`, an intact chain gives `0` and `pass`; `kb_lint.py --gate` (`kb:lint`) and `artifact_lint.py --gate` — the errors and findings the scripts already found become verdict items, and their codes stay (`0` clean, `1` found); a `kb_lint` item is named by its error kind (`broken-link`, `orphan-card`…), and the verdict line carries the first 50 items, the full count is in its `total` field. A missing base is code `2` in gate mode for all four scripts (for `artifact_lint` it means no artifact folder at all: a project with `Artifacts/us` is checked even without `AuroraKnowledgeDB/`); `2` also for `kb_lint` when the base is a link outside the project (the cards were not checked, which is not "clean") and for `artifact_lint` with files without `--profile`. **Adoption mode** (all four scripts): `--gate=warn` (or the environment variable `AURORA_GATE_MODE=warn`) prints the verdict `warn` and returns `0` — for a project route with historical red checks; a step with `--gate=warn` is not a gate and does not stop the route, the warning goes to the log. Without `--gate` there is no verdict line and the codes are as before. **Review** `review_run.py --page … --gate` (`make:review-auto`, one page): the score threshold (9.0) becomes the exit code — "ready for handover" with no findings gives `pass` and `0`, with reservations (minor questions failed or undetermined, unstable answers) — `warn` and `0`, a score below the threshold or critical and major findings — `fail` and `1`. The first verdict item `score` carries the points, then come the failed and undetermined checklist questions (question `id` and text). A failure of the run itself (no access to Confluence, page not read, no usable model run, too little coverage, a broken request) is code `2` without a verdict, formerly code `1`. A batch with `--gate` is a call error. Without `--gate` the codes are as before: `0` in text mode, by status with `--json`.
