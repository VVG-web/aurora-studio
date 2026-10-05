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
| `kit:doctor` (`doctor`) | Project readiness: config, skills, secrets in git, engine version, folder structure, file-name portability. `--structure` — details per folder outside the schema. | script | 1.0.0 |
| `kit:hooks` | Git hooks: pre-commit with the linter and the ratchet (errors may not grow); in the kit also commit-msg, so internal names do not reach history. | script | 1.3.0 |
| `kit:remap-sources` (`remap`) | Retarget cards' `source:` after a mirror moved (Confluence — by page_id, Jira — by issue key). | script | 1.7.0 |
| `kit:update` | Update the engine in a project to the kit's version (preview, then `--apply`); `--structure-only` — only the schema folders. | script | 1.3.0 |
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
| `kb:repair` (`fix`) | Repair: broken links, homoglyphs, legacy frontmatter, fields outside the schema, stubs for links; modes by flag (`--titles`, `--terms`, `--gone-sources`, `--template`, `--copies`, `--aliases`, `--sections`, …). Preview without `--apply`. | script | 1.3.0 |
| `kb:names` (`fix`) | File and folder names by the strictest rules of Windows, macOS and Linux: long Confluence paths, card names with links, case-only differences. | script | 1.140.0 |
| `kb:translit` (`translit`) | The Latin↔Cyrillic name dictionary: finds cards with a transliterated name and Russian content, keeps one translation per concept in `meta/translit.md`, renames by it (`--rename`). | script | 1.100.31 |
| `kb:dedupe` | Duplicates: search and batch merge by rule (`--merge-all`), including one name in different spellings. | script | 1.3.0 |
| `kb:twins` (`twins`) | Cards carrying the same knowledge under different names: compares texts, not names. Merges nothing — names the group. | script | 1.100.31 |
| `kb:split` (`split`) | Cut a bloated card by its headings: the parts become atomic cards, the card becomes a document map linking to them. | script | 1.62.0 |
| `kb:embed` (`embed`) | The base's semantic index: card vectors for search by meaning; the index lies outside git and is rebuilt; texts go to the same gateway as the agent. | script + model | 1.65.0 |
| `kb:moc` (`moc`) | Maps of content by groupings (terms, concepts, roles, data…), per source document (`--by-source`), per artifact code (`--by-code`) and a list of abandoned cards. | script | 1.32.0 |
| `kb:index` (`index`) | Regenerate sections' `_index.md`; hand-written ones are not touched but stale ones are named as a finding (exit code 1). | script | 1.9.4 |
| `kb:scrub` (`scrub`) | Personal data: find and cover with markers; the mode is `privacy.scrub`. | script | 1.9.6 |
| `kb:schema` | The card schema version (`schema_version`) and migration of the base between versions along a declared chain. | script | 1.12.0 |
| `kb:correct` (`correct`) | Corrective artifacts: a human writes in their own words what is wrong in a card and how it really is. Lives in `Raw/corrections/` and is applied at EVERY build; `--check`, `--retire`. | script | 1.99.0 |
| `kb:supersede` (`supersede`) | Replace knowledge with history: deprecated → `_archive`, links rewritten. A requirement is not replaced without `--changed` and `--migration`. | script | 1.8.0 |
| `kb:links` (`links`, `graph`) | The link graph: Requirement Yogi keys and story numbers; `--cards` carries links into cards' `related:`; `--cards-json` exports the base's own graph for the panel. | script | 1.19.0 |
| `kb:map` (`map`) | What the graph says: communities that have grown to their own map, bridges between topics and islands unreachable by links. | script | 1.66.0 |
| `kb:graph-export` | The base's graph outward: `meta/graphify/graph.json` in graphify format with link type and confidence, themes by modularity, notes in `MOC/Сообщества/`, an offline graph page. | script | 1.138.0 |
| `kb:code-graph` | The project's code and SQL in the graph without a model: tables from SQL on pages and in `*.sql`, table pairs of one query, cards naming a table; code from `graphify: code_dirs`. | script | 1.138.0 |
| `kb:reset` (`reset`) | Wipe the base and rebuild: removes what will be rebuilt (a card with a live `source:` or `built: machine`), touches nothing outside `AuroraKnowledgeDB/`; cards of unknown origin stay (`--list-unknown`, `--drop-unknown`); rollback from git. | script | 1.24.0 |
| `kb:lint` (`lint`) | Mechanical errors of the base: links, frontmatter, card kinds, artifacts in knowledge, secrets, cards without links. | script | 1.0.0 |
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
| `make:kinds` (`kinds`) | The project's artifact registry: template, result folder, project prompt, the "no technologies" rule per kind and the final-text boundary. | script | 1.69.0 |
| `make:create` (`create`) | An artifact in `Artifacts/<type>/` — only a standard type from conventions.md. The manual path; the main one is `agent:make`. | model | 1.1.0 |
| `make:review` (`review`) | Check an artifact's quality against the base. | model | 1.0.0 |
| `make:review-auto` (`review-auto`) | Review of a story or algorithm without a human: a closed checklist from `review_v2.0.md`, three model runs vote on each question, a script computes the score and verdict; `--page`, `--cql`, `--as-of`. | script + model | 1.119.0 |
| `make:spec` (`spec`) | A feature specification from REQ and `knowledge` cards (SDD). | model | 1.0.0 |
| `make:spec-pack` (`spec-pack`) | A spec bundle: grounds, DRs, abbreviations, DoR risks — one self-contained file. | script | 1.9.4 |
| `make:validate` (`validate`) | Compare a contractor's implementation and tests with the spec's scenarios. | model | 1.0.0 |
| `make:assemble` (`assemble`) | Assemble a deliverable (OPZ/PMI/user guide) from the base by a template. | model | 1.0.0 |

## `ship:` — Outward

| Command | What it does | Executor | Since |
|---|---|---|---|
| `ship:publish` (`publish`) | An artifact → a generated Confluence page; knowledge cards do not go out. The body is cut at the marker line: "Clarifications", "Assumptions", "In question" stay in the analyst's draft. | script | 1.9.6 |
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
| `agent:translit` | Fill the Latin↔Cyrillic name dictionary for cards named by transliteration: a translation is work with meaning, done by the model. | script + model | 1.101.0 |
| `agent:make` | Produce an artifact of a registry type: enrichment from the base, a plan with questions to the analyst, worker, critic, Momus. The stages are marked in the header — after an interruption it resumes from the same point. Understands `@path`, `/skill`, `@server`. | script + model | 1.95.0 |
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
