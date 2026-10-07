# Model calls

Where Aurora calls models: when, which roles by step, which prompt, which tools the model has, and whether the call goes through Pydantic AI. The page is built from the `scripts/llm_calls.json` catalogue (`python3 scripts/llm_calls.py --write`); the tests match the catalogue against the code, so a new call site without a description here fails the check.

**A role is a chain of models.** Which models stand behind a role is set in the panel's "Models" section (the kit's `local/models.json`, one setup for all projects). The first model of the chain answers, the next ones are spares: if a gateway did not answer or is overloaded, the call goes on. The role's reasoning is there too (the role's "reasoning" box on the LLM tab). Live chains for every call — "Models → Where they work".

- [Breaking sources into cards](#build)
- [Card theses](#distill)
- [Extracting foreign definitions](#extract)
- [Links in theses](#relink)
- [Synonym conflicts](#aliases)
- [Twins](#twins)
- [Contradictions](#clashes)
- [Cards from Jira issues](#tasks)
- [Latin names](#translit)
- [Golden questions: lost targets](#golden)
- [Ask the base](#ask)
- [Artifact (Productivity)](#make)
- [Review of a story or an algorithm](#review)
- [Project bot](#bot)
- [Bot prompt review](#coach)
- [Model chain check](#ping)
- [Gateway width probe](#width)
- [Live connection check](#probe)
- [Scan recognition](#ocr)
- [Meaning index (embeddings)](#embeddings)

<a id="build"></a>

## Breaking sources into cards

- **When.** The agent:build command — in batches until the plan is done; Cron schedules.
- **Commands:** `agent:build`.
- **Routes:** «Update the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Planner (planner) | A large source without structure: a plan of sections to cut it by. | `PROMPT_PLAN_SOURCE` (agent_runner.py) |
| Planner (planner) | A meeting transcript from Raw/meetings: a plan part by part. | `PROMPT_MEETING` (agent_runner.py) |
| Writer (worker) | Storyboarding a source: topic boundaries, card names, kind of knowledge. The engine, not the model, moves the text into cards. | `PROMPT_BUILD` (agent_runner.py) |
| Writer (worker) | A source without structure: break it down, skip it or treat it as service text. | `PROMPT_NO_SECTIONS` (agent_runner.py) |
| Critic (critic) | Checking the storyboard before writing into the base. | `PROMPT_BUILD_CRITIC` (agent_runner.py) |
| Critic (critic) | Checking the decision on a source without structure. | `PROMPT_NO_SECTIONS` (agent_runner.py) |

- **Tools:** none: the engine puts everything needed into the task.
- **Path:** Pydantic AI.
- **Then:** The engine moves the text, marks the source as processed, the linter checks the cards.

<a id="distill"></a>

## Card theses

- **When.** The agent:distill command; `--recheck` re-checks theses without support.
- **Commands:** `agent:distill`.
- **Routes:** «Update the base», «Repair the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) | A card thesis from the moved source text. Changed source — PROMPT_REDISTILL; long text — by parts (PROMPT_DISTILL_PART) and joining (PROMPT_DISTILL_JOIN). | `PROMPT_DISTILL` (agent_runner.py) |
| Planner (planner) | An overgrown card: a plan to split it into several. | `PROMPT_PLAN_SPLIT` (agent_runner.py) |
| Writer (worker) | Paragraphs repeated on dozens of pages: a template or knowledge. | `PROMPT_TEMPLATE_KINDS` (agent_runner.py) |
| Momus (qa) | Momus: every claim of the thesis rests on the source. Without support — a mark and a re-check. | `PROMPT_MOMUS` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.
- **Then:** A thesis without support does not become knowledge; trust is computed by the engine.

<a id="extract"></a>

## Extracting foreign definitions

- **When.** The agent:extract command.
- **Commands:** `agent:extract`.
- **Routes:** «Update the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Planner (planner) | Finds definitions of other entities in a card and moves them into their cards: a card is an entity, not a document. | `PROMPT_EXTRACT` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="relink"></a>

## Links in theses

- **When.** The agent:relink command.
- **Commands:** `agent:relink`.
- **Routes:** «Update the base», «Repair the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) | Places [[…]] links in a finished thesis; the thesis text does not change. | `PROMPT_RELINK` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="aliases"></a>

## Synonym conflicts

- **When.** The agent:aliases command.
- **Commands:** `agent:aliases`.
- **Routes:** «Update the base», «Repair the base».

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) | A decision on a synonym conflict: whose synonym it is. | `PROMPT_WORKER` (agent_runner.py) |
| Critic (critic) | Checking the decision before writing. | `PROMPT_CRITIC` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="twins"></a>

## Twins

- **When.** The agent:twins command.
- **Commands:** `agent:twins`.
- **Routes:** «Update the base», «Repair the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Critic (critic) | The fate of cards about the same thing: merge, separate or keep. | `PROMPT_TWINS` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="clashes"></a>

## Contradictions

- **When.** The agent:clashes command.
- **Commands:** `agent:clashes`.

| Role | What it does | Prompt |
|---|---|---|
| Momus (qa) | Looks for contradictions between cards about one subject. | `PROMPT_CLASH` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="tasks"></a>

## Cards from Jira issues

- **When.** The agent:tasks command.
- **Commands:** `agent:tasks`.
- **Routes:** «Update the base», «Repair the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Critic (critic) | The fate of a card made from an issue: return the knowledge to its subject, keep it or remove it. | `PROMPT_TASKS` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="translit"></a>

## Latin names

- **When.** The agent:translit command.
- **Commands:** `agent:translit`.
- **Routes:** «Repair the base».

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) | A Russian name for a card named in Latin letters. | `PROMPT_TRANSLIT` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="golden"></a>

## Golden questions: lost targets

- **When.** The agent:golden command.
- **Commands:** `agent:golden`.
- **Routes:** «Repair the base».

| Role | What it does | Prompt |
|---|---|---|
| Critic (critic) | Matches a golden question against answer candidates and picks the new target. | `PROMPT_GOLDEN` (agent_runner.py) |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="ask"></a>

## Ask the base

- **When.** The "Ask" section, the agent:ask command. The model is chosen in the section: a role ("Writer" by default) or exactly a provider model.
- **Commands:** `agent:ask`.
- **Routes:** «Write an artifact».

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) — chosen | An answer only from the card pack, with a link for every claim; a follow-up question continues the conversation. | `PROMPT_ASK` (agent_runner.py) |
| Momus (qa) | Momus checks that the answer rests on the pack's cards. | `PROMPT_MOMUS` (agent_runner.py) |

- **Tools:** none: the engine builds the knowledge pack with hybrid search (words and meaning).
- **Path:** Pydantic AI.
- **Then:** The engine resolves the answer's links against the base; the conversation is written to meta/ask/.

<a id="make"></a>

## Artifact (Productivity)

- **When.** The "Productivity" section, the agent:make command.
- **Commands:** `agent:make`.

| Role | What it does | Prompt |
|---|---|---|
| Planner (planner) — with tools | Questions and the document plan. The only step with tools: it can search the base and the MCP chosen in the request. | `PROMPT_MAKE_PLAN` (agent_runner.py) |
| Writer (worker) | The document by the plan and the kind's form. | `PROMPT_MAKE_WRITE` (agent_runner.py) |
| Critic (critic) | Checking against the form and the plan. | `PROMPT_MAKE_CRITIC` (agent_runner.py) |
| Momus (qa) | Momus: the document's claims rest on the pack. | `PROMPT_MOMUS` (agent_runner.py) |

- **Tools:** the planner has read_file, list_dir, kb_search, kb_context, artifact_spec and the request's MCP; the outgoing guard lets out only words of the task and the pack.
- **Path:** Pydantic AI.

<a id="review"></a>

## Review of a story or an algorithm

- **When.** The make:review-auto command, the aurora server's review_page tool (bots and assistants).
- **Commands:** `make:review-auto`.

| Role | What it does | Prompt |
|---|---|---|
| Momus (qa) | Answers to the closed checklist from the review_v2.0.md template — several runs (3 by default) with a vote on every question. | `build_prompt` (review_run.py) |
| Momus (qa) | Free recommendations on what to fix. | `free_recs` (review_run.py) |

- **Tools:** none: the engine reads the page and the linked ones.
- **Path:** Pydantic AI.
- **Then:** The score and the verdict are computed by code (weights 5/3/1, threshold 9.0).

<a id="bot"></a>

## Project bot

- **When.** The "Bots" section → "Run", the bot's schedule, the "bot" step of a Cron chain, the bot:run command.
- **Commands:** `bot:run`.

| Role | What it does | Prompt |
|---|---|---|
| Planner (planner) — without reasoning | Before work: concepts, terms and codes from the task and the links — for the knowledge pack (project context). Without reasoning. | `PROMPT` (bot_context.py) |
| Writer (worker) — chosen, with tools | The bot's prompt with the knowledge pack. A role or exactly a model — in the bot's "Model" card. | `bots/*.md` |

- **Tools:** read_file, list_dir, kb_search, kb_context, artifact_spec, save_output and the bot's MCP (the aurora server — review, Jira, Confluence, memory, lock, time); at most 40 calls.
- **Path:** Pydantic AI.
- **Then:** A run without tool calls while it has its MCP is a failure with the reason.

<a id="coach"></a>

## Bot prompt review

- **When.** The "Bots" section → "Improve with AI".

| Role | What it does | Prompt |
|---|---|---|
| Critic (critic) — chosen | A critical review of the prompt by the engine's facts and edits on the person's request. | `SYSTEM` (bot_coach.py) |

- **Tools:** none: the engine gathers the facts (the bot's tools with parameters, signs of scripts and environment).
- **Path:** Pydantic AI.

<a id="ping"></a>

## Model chain check

- **When.** "Models" → "Check", the agent:ping command.
- **Commands:** `agent:ping`.

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) — without reasoning | Each gateway in turn — "Repeat one word: ready", 60 tokens. | — |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="width"></a>

## Gateway width probe

- **When.** The agent:width command.
- **Commands:** `agent:width`.

| Role | What it does | Prompt |
|---|---|---|
| Writer (worker) — without reasoning | Short identical requests in parallel: how many simultaneous ones the gateway holds. | — |

- **Tools:** none.
- **Path:** Pydantic AI.

<a id="probe"></a>

## Live connection check

- **When.** The agent:probe command.
- **Commands:** `agent:probe`.

| Role | What it does | Prompt |
|---|---|---|
| — | Polling each gateway directly: the model list and a short request, the reason of a refusal. | — |

- **Tools:** none.
- **Path:** direct HTTP, bypassing Pydantic AI.
- **Why direct:** checks the gateway itself, bypassing the adapter: this shows who refused — the gateway or the adapter.

<a id="ocr"></a>

## Scan recognition

- **When.** The kb:ingest-office command: PDFs without a text layer from Raw/.
- **Commands:** `kb:ingest-office`.
- **Routes:** «Update the base».

| Role | What it does | Prompt |
|---|---|---|
| Document scans (OCR) | An image of each page → text (OCR_PROMPT), up to the page cap from "Models → OCR". | — |

- **Tools:** none.
- **Path:** direct HTTP, bypassing Pydantic AI.
- **Why direct:** the request carries a page image; the adapter path for images is not built yet.

<a id="embeddings"></a>

## Meaning index (embeddings)

- **When.** The kb:embed command; a query to the index — at every hybrid search: "Ask", "Productivity", the bot context, kb_search, the Aurora MCP.
- **Commands:** `kb:embed`.
- **Routes:** «Update the base», «Repair the base», «Rebuild the base from scratch».

| Role | What it does | Prompt |
|---|---|---|
| Knowledge base index (embeddings) | Vectors of card pieces and of the query; this is not a chat model. | — |

- **Tools:** none.
- **Path:** direct HTTP, bypassing Pydantic AI.
- **Why direct:** embeddings are not a conversation with a model; Pydantic AI does not handle them.
