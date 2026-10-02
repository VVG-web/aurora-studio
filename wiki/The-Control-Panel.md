# The control panel

[Wiki home](README.md) · related: [Getting started](Getting-Started.md) · [Daily work](Daily-Work.md) · full text:
[control panel](../docs/en/control-panel.md)

A local web page that runs on your machine only (`127.0.0.1`) and drives the engine: it finds the projects, runs commands
from the registry and streams their output. It is not a server for a team — every analyst runs their own.

```bash
python3 aurora.py cockpit                              # or double-click start-aurora.command / .bat
python3 cockpit/aurora_cockpit.py --port 9000          # another port
python3 cockpit/aurora_cockpit.py --add-root ~/work    # add a folder to look for projects in
```

`⌘K` / `Ctrl+K` opens the palette: commands, sections, projects. The language (Russian or English), the theme and the skin
are switched in the header.

## The sections

| Group | Section | For |
|---|---|---|
| **The machine** | The Bridge | tiles of every project: engine version, share of `knowledge`, blockers, lint errors, branch, uncommitted changes. The aura colour is the severity: teal — in order, amber — worth a look, red — blockers |
| | The kit's setup | where the panel looks for projects; connecting a new project; the whole config as text |
| | Install | what is missing on the machine and which commands depend on it; engine add-ons |
| | Help | the documentation with a table of contents |
| | About | version, repository, releases and **updating the kit with a button** |
| **The project** | The project's settings | access (tokens: "filled / empty"), config, artifact kinds, the agent's gateways and roles, MCP servers |
| | Health | composition of the base, trust, links, errors, mirror freshness, what is left for a human; every tile leads to the command that fixes it |
| | Routines | the base's routes: **Update**, **Fix**, **Rebuild** — with a "Preview" that writes nothing |
| | Production | **Write an artifact** and **Hand over documents** |
| | Files | the project tree, a Markdown editor (tables, mermaid, formulas), "Commit" and "Push" |
| | Ask | questions to the base; conversations are kept for everyone |
| | Commands | the whole registry: executor, modifiers, version, last run |
| | Mirrors | state of Confluence, Jira and sites; the sync → audit → drift chain |
| | Version | the project's engine against the kit; update preview |
| **The console** | Console | the streaming output of runs, the return code, history; a route's run unfolds by steps |

## Principles worth knowing

- **Nothing writes without a preview.** The panel does not add `--apply` by itself: first a preview, then your confirmation.
- **A run survives a closed tab.** Jobs live in the panel's process; reloading the page does not hide a working job. Starting
  the same command in the same project twice returns the running job instead of a second one.
- **A stopped route resumes** where it stood, and a card on the Bridge says what is running and what stopped.
- **Secrets never come back.** A token you enter is written to `.env.aurora.local`; the panel only ever shows
  "filled / empty".
- **Nothing leaves the loopback.** The page and its token are served to `127.0.0.1` only, and the panel refuses a foreign
  `Host`.
- **The interface is translated** (Russian, English). What the engine prints into the console — reports, linter output —
  stays Russian; the panel's own texts, command and flag descriptions, routes and error messages follow the language.

## Look and feel

The panel has skins (`cockpit/skins/*.css`, three layers of tokens): Zine 2.0 (the default), Contrast, Gauges, Editorial,
Unicorn. A skin changes the look, not the meaning — the density and the trust scale are the same. See
[skins](../cockpit/skins/README.md).
