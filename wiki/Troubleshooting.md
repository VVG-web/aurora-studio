# Troubleshooting

[Wiki home](README.md) · related: [FAQ](FAQ.md) · [Windows and macOS](Windows-and-macOS.md) · start with `kit:doctor`

Engine scripts explain the reason instead of just failing. The common cases:

## Before a run

| Symptom | Cause and fix |
|---|---|
| **"git-guard: N uncommitted files"** | A mass write over a dirty tree makes rollback impossible. Commit your work (panel: *Commit*) — or, if you understand the risk, add `--allow-dirty`. |
| **The agent refuses to write: no git** | A writing run commits a checkpoint first. No repository → nothing to roll back to. `git init` and make a first commit of the whole base. |
| **«уже идёт пишущий прогон»** (a writing run is already going) | One writer per project. The message names the holder's pid and start. If that process is dead the lock is lifted by itself; if it is alive, wait or stop it. |
| **`kb_lint: нет папки AuroraKnowledgeDB/`** (no such folder) | You ran it from the kit, not from the project root. |
| **The agent does not see `AGENTS.md`** | Open the **project folder** as the workspace root, not the kit. |
| **`/aurora-vault` is not found in a conversation** | The skill is a copy in `~/.claude/skills`: `kit:skills --apply`. |

## Syncing

| Symptom | Cause and fix |
|---|---|
| **"no token — set CONFLUENCE_PERSONAL_TOKEN"** | Sync scripts go to Confluence and Jira directly; your editor's MCP does not help. Copy `aurora.env.local.example` to `.env.aurora.local` and fill it in. Check: `python3 .aurora/scripts/jira_export.py --limit 1`. |
| **A sync skill looks at the wrong space or JQL** | The values live in `aurora.config.yaml`, not in the skill: edit the config or run `aurora_setup.py` again. |
| **Timeouts, half the pages missing** | The pages the walk did not reach stay in the state; run the sync again. `--prune` refuses to delete after an export with errors. |
| **A page moved in Confluence** | `kit:remap-sources`, then `kb:repair --gone-sources`. |
| **A subtree is missing in the mirror, but the page is visible on the web** | Read the sync output: `ветка <title>: сервер отдал N из M` or `отказ 403` names the branch the server did not hand over (the token's rights, a moved page), and the root line shows the depth reached. `sync:confluence --diagnose-tree` prints the tree with the number of children per page and writes nothing. An incomplete engine copy gives the same picture: `kit:doctor` lists it. |
| **`sync:audit` reports `COLLISION`** | Two pages differ only in case; the mirror appends the page id. Run `kb:names`. |

## The agent

| Symptom | Cause and fix |
|---|---|
| **"did not answer" / an empty answer** | `agent:ping` checks the chain; `agent:probe` tells "no connection" from "wrong key" from "no such model" and lists the gateway's models. |
| **Three failures in a row, the run stops** | By design: it is the gateway, not the cards. Fix the gateway, run again — the rest is picked up. |
| **Very slow, one request at a time** | `agent:width` measures how many parallel requests a gateway holds; set `…_WIDTH`. |
| **A claim in a thesis is "no support"** | Momus found nothing in the source. It is listed in `ops:todo`; `agent:distill --recheck` checks again. |

## The base

| Symptom | Cause and fix |
|---|---|
| **"top-level folders outside the schema"** | Move the content to `Workspaces/<task>/`, or close the folder in `.gitignore` if it is service data, or declare it in `aurora.config.yaml` → `paths.extra_structure_dirs`. |
| **A commit fails on the ratchet** | The number of lint errors grew above the baseline. Fix (`kb:repair`) — or deliberately `AURORA_SKIP_RATCHET=1`. It lifts only the ratchet; nothing lifts the commit-message check. |
| **Hundreds of broken links after parsing** | Links to cards that do not exist yet: `kb:repair --links`, then `--stubs`. |
| **A source is marked parsed but has no cards** | `kb:build --reopen --apply`. |
| **One source, one card for 60 KB of text** | Parsing broke off early: `kb:build --thin`. |
| **One entity lives as five cards** | `kb:twins`, `agent:twins`. |
| **The share of `knowledge` is low** | Either issues are in progress or no links were found. Trace (`ops:trace-table`), and check `trust_statuses` in the config. |
| **"source gone" on a card** | The page moved or was deleted: `kb:repair --gone-sources`. |
| **A file name does not pass on Windows or macOS** | `kb:names` (preview, then `--apply`). |
| **Cyrillic folder under `.gitignore` flagged as outside the schema** | Fixed in 1.148.2; update the engine (`kit:update`). |

## The panel

| Symptom | Cause and fix |
|---|---|
| **"engine behind the kit"** | **Version** → preview → `--apply`. A route will not start on a project whose engine is older than the panel's kit. |
| **A fresh clone: "the project engine is not on this computer" / "incomplete"** | `.aurora/` is a local engine copy and does not travel in git. Open the project in the panel: it completes `.aurora/` by itself (`kit:update --engine-only`; project files and git are untouched). If it did not, the bar above the sections and **Version** have the **Prepare project** button. Everything else of the update (`AGENTS.md`, templates, `.gitignore` rules) stays behind **Version** → **Apply**. |
| **"Pydantic AI is selected, but calls bypass it" (red bar)** | Appears **only** when the Pydantic AI path itself is faulty, and the bar names the cause. `venv python: … not found` — Pydantic AI is not installed on this computer: **Install** → Pydantic AI. `adapter: .aurora/scripts/agents/… not found` — the project engine is incomplete (the page *Install* may be green): **Version** → **Prepare project**. `package … is not installed in the venv` / `does not import` / `failed the compatibility check` / `the adapter process crashed` — **Install** → Pydantic AI (reinstall or restore the version). `the adapter's HTTP client does not reach backend №N, while a direct request does` — the adapter process has its own network: check `HTTP_PROXY`/`NO_PROXY`, the gateway's root certificate (`SSL_CERT_FILE`), DNS. Every message and its cure — [INSTALL](../docs/en/INSTALL.md#what-each-message-means). |
| **"The model gateway is unreachable: …" (red bar) or a run summary line "connection drop"** | A network or gateway trouble, **not** Pydantic AI: the bar names the kind (DNS, connection refused, connection timed out, no route, TLS/certificate, proxy, reset, server error 5xx, key refused 401/403, 404, not JSON) and the backend. Check the network, VPN and gateway status; for a key or model name — the **Models** section. The bar appears after three failures in a row on one backend and goes away by itself with the first answer; a lone drop is only a note in the summary ("repeated over a direct request — this is not a Pydantic AI fault"). |
| **The page shows an old interface after an update** | Restart the panel (`aurora_cockpit.py --restart`); the first start of a new version rebuilds the command registry and can take a minute. |
| **The port is taken** | `--port 9000`, or `--restart` to replace the running panel. |
| **macOS blocks `start-aurora.command`** | Right-click → *Open*. |

If a symptom is not here, the lifecycle page lists
[where it breaks most often](../docs/en/lifecycle.md#8-where-it-breaks-most-often), and
[INSTALL](../docs/en/INSTALL.md#troubleshooting) has the setup-time table.
