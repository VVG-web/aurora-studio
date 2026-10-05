# Assistants, MCP and skills

[Wiki home](README.md) · related: [Asking the base](Asking-the-Base.md) · [Trust](Trust.md) · design:
[architecture §5](../docs/en/architecture.md)

Aurora meets assistants (Claude Code, Cursor, OpenCode, any MCP client) in two ways: **MCP** gives them the base as a
tool, and **skills** teach them the procedures.

## The MCP server

`scripts/aurora_mcp.py` is a small stdio server (JSON-RPC 2.0, standard library only — no SDK, no Node). It lets an
assistant search the base, read cards and ask questions while it works on your task, instead of you carrying context
into the chat by hand.

| Tool | Does |
|---|---|
| `kb_search` | find cards by meaning and words — name, trust status, gist |
| `kb_card` | read a card whole |
| `kb_context` | assemble a context pack by topic (`generate` mode = trusted knowledge only) |
| `kb_index` | the table of contents of the base: a line per card, grouped by section |
| `artifact_spec` | how to produce an artifact of this project: template, folder, prompt, the final-text boundary |
| `kb_ask` | ask the base: the project's model answers from the cards with links (slow — tens of seconds) |

**It only reads.** The assistant does not pass git-guard and does not take part in trust — the engine computes trust
from tasks, not the assistant. `kb_ask` does not write a conversation journal; it never changes files.

**One server — one base.** Customers must not mix: a server is started per project (`--project <root>`) and its name
carries the project's slug (`aurora-<slug>`), so `aurora-alpha.kb_search` is never confused with
`aurora-beta.kb_search`. A failed call returns `isError: true` with a plain-language reason (an unknown tool, a card that
does not exist, a mode outside the list), not a block of usage text.

### Connecting

`kit:mcp` (or `python3 aurora.py mcp`) checks the server and prints a ready connection block for every project on the
machine. The format is the same for all clients:

```json
{"mcpServers": {
  "aurora-alpha": {"command": "python3",
                   "args": ["<project>/.aurora/scripts/aurora_mcp.py", "--project", "<project>"]}}}
```

Servers of the machine (shared by all projects) live in the kit's `local/mcp.json`; a project's own in the project's
`mcp.json`. The panel's **Project settings** edits both; secrets never go into a project's `mcp.json` (it goes to git).
Selftest without an assistant: `python3 .aurora/scripts/aurora_mcp.py --selftest`.

## Skills

Three skills ship with the kit (`skills/`):

| Skill | For |
|---|---|
| `aurora-vault` | operating the framework: building and maintaining the base, trust, Decision Records, reviewing and generating artifacts; the procedure references behind the "procedure" commands |
| `aurora-grill` | taking an idea apart along a decision tree: rounds of questions with recommendations until no silent assumptions are left. The planner runs it before an artifact is produced; it is available in chat as well |
| `aurora-dev` | developing the engine itself: running tests and scenarios, covering changes. Not for projects that merely use Aurora |

Skills are **copies**, not links into the repository: install them with `kit:skills --apply` into `~/.claude/skills/`
(other harnesses get a link to that folder). `aurora.py new` does it for you and `aurora.py update --apply` refreshes
them. Installing a skill never leaves the agent without it: the new copy is made next to the old one and swapped in.

In a conversation, `/aurora-vault <command>` runs a procedure command (`kb:ingest`, `kb:decide`, `make:create`, …) —
the work that needs meaning rather than a script. Mentions in a task: `@path` (a project file or folder), `/skill`
(its method goes into the task), `@server` (an MCP server is connected at once).
