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
| **A sync did not start: «VPN-1 is unavailable» / exit code 2 / a line `AURORA-NET {…}`** | The connection check is on and the source's profile does not answer, so the script refused before the first request instead of timing out. Bring the VPN up and run again, or queue the run in the panel dialog. `net:status` shows the state, `net:probe` checks now. |
| **"no token — set CONFLUENCE_PERSONAL_TOKEN"** | Sync scripts go to Confluence and Jira directly; your editor's MCP does not help. Copy `aurora.env.local.example` to `.env.aurora.local` and fill it in. Check: `python3 .aurora/scripts/jira_export.py --limit 1`. |
| **A sync skill looks at the wrong space or JQL** | The values live in `aurora.config.yaml`, not in the skill: edit the config or run `aurora_setup.py` again. |
| **Timeouts, half the pages missing** | The pages the walk did not reach stay in the state; run the sync again. `--prune` refuses to delete after an export with errors. |
| **A page moved in Confluence** | `kit:remap-sources`, then `kb:repair --gone-sources`. |
| **A subtree is missing in the mirror, but the page is visible on the web** | Read the sync output: `ветка <title>: сервер отдал N из M` or `отказ 403` names the branch the server did not hand over (the token's rights, a moved page), and the root line shows the depth reached. `sync:confluence --diagnose-tree` prints the tree with the number of children per page and writes nothing. An incomplete engine copy gives the same picture: `kit:doctor` lists it. |
| **"long paths: not everything will be loaded" at the start of a run, or a red line under a root or an address in "Project settings"** | A config line gives a path that does not fit Windows (259 characters, a folder 247, a name 255), macOS (a name 255 UTF-16 characters) or Linux (a name 255 bytes) for the project root named in the message. The run is not blocked; the files that do not fit are skipped and counted ("Long paths: N mirror files not written"). `kit:paths` lists the lines. Shorten the project folder name or move the project closer to the disk root, or enable Windows long paths and run `git config --global core.longpaths true`. Details: [path length](../docs/en/connectors.md#path-length-the-check-on-saving-settings). |
| **`sync:audit` reports `COLLISION`** | Two pages differ only in case; the mirror appends the page id. Run `kb:names`. |

## The agent

| Symptom | Cause and fix |
|---|---|
| **«all providers of the role are unavailable» / a step is «waiting for the connection»** | The connection check knows the providers' profile or the internet is down; the call did not go out, nothing is quarantined, and a route step waits by itself (a one-off call refuses with the reason). It resumes when the link returns; «Check now» wakes it. If the VPN is up, check the provider's handle check in *Models → the provider* (path and key). |
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

## Attachments and diagram transcripts

What the lines of `kb:ingest-office` (and the same lines in the run summary, the plan and `kb:doctor`) mean.

| Symptom | Cause and fix |
|---|---|
| **«Без транскрипта (текста слишком мало — логотип, иконка, пустая схема)»** (no transcript: too little text) | The file has fewer than 200 characters of text (80 for a diagram or image): a logo, an icon, an empty scheme. It is not an error. The command remembers the file's hash in `Raw/Extracted/.skipped.json`, so a repeat run and the doctor leave it alone; a changed file is tried again, `--force` tries all. |
| **«Не разобраны: нет транскрипта — N»** in the parsing plan, or a `kb:doctor` warning "без транскрипта" | A document, attachment or diagram has no current transcript: the command was not run after the sync, the cap of files per run cut it off, or no converter fits. Run `kb:ingest-office`; the plan names the files, so nothing is skipped silently. |
| **«Документов низкого качества: N»** (documents of low quality) | The text came out empty or unreadable (a scan without recognition, a broken encoding, repeated characters). Such a transcript has `quality: low` and is not parsed. Fix the original, or configure the recognition ring (*Models* → OCR) and run `kb:ingest-office --force`. |
| **«Отложено до следующего запуска (потолок файлов за запуск — N)»** (postponed to the next run) | The per-run cap of attachments and diagrams was reached; written transcripts are counted. Run the command again, or raise "Files per run cap" in "Project settings" → "Extraction from attachments". |
| **«Дубликаты по содержимому»** (duplicates by content) | The same file was attached to several pages; one transcript serves them all. Nothing to do. |
| **«Длинные пути: имя транскрипта сокращено до хеша»** (long paths: the transcript name is cut to a hash) | The transcript path did not fit 200 characters, so its name became a hash; the header still has the full `attachment` name. To keep readable names, shorten the page titles or the project path ([path length](../docs/en/connectors.md#path-length-the-check-on-saving-settings)). |
| **«Изображения без текста: распознавание не настроено»** (images without text: recognition is not configured) | The switch "Recognise images with a model" is on, but the recognition ring has no model. Set the model in *Models* → OCR, or turn the switch off. It is a warning; the step stays green. |
| **«пропущено (больше предела размера вложения)»** (skipped: above the attachment size limit) | The file is larger than `attachment_max_mb`, the same limit as the sync's. Raise it in the project form if the document is needed. |
| **«нет подходящего конвертера»** (no suitable converter) | Neither the built-in reader nor `pandoc` / `markitdown` handles the format (for example a pptx without either installed). The line names the file; install a converter ([INSTALL](../docs/en/INSTALL.md)) or export the file to docx / pdf. |
| **A refusal naming DOCTYPE or ENTITY, or "XML nesting is above N"** | The file is not safe to read: a document or diagram that declares external entities or is built as a bomb. It is refused on purpose and not converted; take the content from the original editor. |

## The panel

| Symptom | Cause and fix |
|---|---|
| **"engine behind the kit"** | **Version** → preview → `--apply`. A route will not start on a project whose engine is older than the panel's kit. |
| **A fresh clone: "the project engine is not on this computer" / "incomplete"** | `.aurora/` is a local engine copy and does not travel in git. Open the project in the panel: it completes `.aurora/` by itself (`kit:update --engine-only`; project files and git are untouched). If it did not, the bar above the sections and **Version** have the **Prepare project** button. Everything else of the update (`AGENTS.md`, templates, `.gitignore` rules) stays behind **Version** → **Apply**. |
| **"Pydantic AI is selected, but calls bypass it" (red bar)** | Appears **only** when the Pydantic AI path itself is faulty, and the bar names the cause. `venv python: … not found` — Pydantic AI is not installed on this computer: **Install** → Pydantic AI. `adapter: .aurora/scripts/agents/… not found` — the project engine is incomplete (the page *Install* may be green): **Version** → **Prepare project**. `package … is not installed in the venv` / `does not import` / `failed the compatibility check` / `the adapter process crashed` — **Install** → Pydantic AI (reinstall or restore the version). `the adapter's HTTP client does not reach backend №N, while the engine's own check does` — the adapter process has its own network: check `HTTP_PROXY`/`NO_PROXY`, the gateway's root certificate (`SSL_CERT_FILE`), DNS. Every message and its cure — [INSTALL](../docs/en/INSTALL.md#what-each-message-means). |
| **"The model gateway is unreachable: …" (red bar) or a run summary line "connection drop"** | A network or gateway trouble, **not** Pydantic AI: the bar names the kind (DNS, connection refused, connection timed out, no route, TLS/certificate, proxy, reset, server error 5xx, key refused 401/403, 404, not JSON) and the backend. Check the network, VPN and gateway status; for a key or model name — the **Models** section. The bar appears after three failures in a row on one backend and goes away by itself with the first answer; a lone drop is only a note in the summary ("repeated — this is not a Pydantic AI fault"); direct HTTP is not used for it. |
| **The page shows an old interface after an update** | Restart the panel (`aurora_cockpit.py --restart`); the first start of a new version rebuilds the command registry and can take a minute. |
| **The port is taken** | `--port 9000`, or `--restart` to replace the running panel. |
| **macOS blocks `start-aurora.command`** | Right-click → *Open*. |

If a symptom is not here, the lifecycle page lists
[where it breaks most often](../docs/en/lifecycle.md#8-where-it-breaks-most-often), and
[INSTALL](../docs/en/INSTALL.md#troubleshooting) has the setup-time table.
