# Source modules

The mirrors in `Sources/` are filled by pluggable modules. The engine knows nothing about Confluence and Jira: it knows
two **kinds** of storage and can serve any module that declares its kind. Русская версия:
[../connectors.md](../connectors.md).

| Kind | About | Layout | State file |
|---|---|---|---|
| `wiki` | a tree of pages with stable numbers | folders repeat the hierarchy; a page with children → a folder and `index.md` | `sync_state.md` |
| `board` | a flat list of records with stable keys | a file per record, the name is the key | `update_log.md` |

`kind` is about the **file layout**. The origin of knowledge is the manifest's second field, `role`: sources split into
tasks and documents, and trust is computed for them differently.

| `role` | What it is | Where trust comes from |
|---|---|---|
| `tasks` | work: tasks, tickets, work orders | the task's status; the list of trusted statuses is set by the project |
| `artifacts` | documents: wiki pages, site pages, exports | by links to tasks, or declared at connection |
| `raw` | a primary source: a law, a state contract, a spec | truth by definition, nothing to prove |

Bundled: `jira-dc` (board / tasks), `confluence-dc` (wiki / artifacts), `web` (board / artifacts).

A document module may declare trust **per record** — `web` does: every link has its own tick, because a law and somebody's
blog come from the same internet but must not be trusted equally. The declaration is written into the saved file's header as
`trusted: true|false`, and `kb:trust` reads it from there. So trust remains a property of the source and is not derived
from a path match on disk, and no second list of trusted folders is needed in the config.

Confluence Data Center (`confluence-dc`), Jira Data Center (`jira-dc`) and `web` ship with the kit and are always
installed. Everything else — Notion, SharePoint, Confluence Cloud, YouTrack, GitLab Issues — is added as a folder in
`connectors/`.

## What the engine does and what a module does

The shared part is `scripts/sources_core.py`:

- a REST client with a token (`RestApi`), reading secrets from `.env.aurora.local`;
- the mirror's state file: writing, parsing, merging with the previous run;
- finding extra files (`extra_files`) with case and Unicode normalisation in mind;
- protection from deleting what cards refer to (`cited_by_cards`);
- the determinism gate `--verify`: two exports and a byte-by-byte comparison.

A module implements only the product-specific part: how to call the API, how to turn the product's markup into Markdown,
which fields to put into the file header. Determinism is mandatory: the same page must give the same file byte for byte,
otherwise git shows edits where there are none and `sync:audit` cannot check the state.

## A module's structure

```
connectors/<id>/
├── connector.json     # the manifest: what the module is and how to launch it
├── SKILL.md           # a template of the project's sync skill (placeholders {{PROJECT_SLUG}} etc.)
└── <script>.py        # the export script (built-in ones lie in the kit's scripts/)
```

`connector.json`:

```json
{
  "id": "notion",
  "title": "Notion",
  "kind": "wiki",
  "role": "artifacts",
  "since": "1.29.0",
  "what": "Pages of a Notion database as a tree.",
  "mirror": {"default_path": "Sources/Notion", "state": "sync_state.md"},
  "run": {"script": "notion_export.py", "command": "sync:notion", "skill": "notion-sync"},
  "auth": {"env_prefix": "NOTION", "what": "an integration token"},
  "settings_block": "notion",
  "settings": [{"key": "database_id", "what": "which database is exported", "required": true}],
  "requires": {"python": ["beautifulsoup4"]}
}
```

- `kind` — `wiki` or `board`; the engine chooses both the layout and the audit rules by it;
- `role` — `tasks`, `artifacts` or `raw`: where a source's trust comes from (the table above);
- `requires.python` — Python packages the module needs; the engine runs on the standard library, so they are installed
  separately ([INSTALL](INSTALL.md#requirements));
- `mirror.default_path` — the mirror folder; it also becomes legitimate in `Sources/` (`kit:doctor --structure` stops
  calling it nobody's);
- `auth.env_prefix` — variable names are derived from it: `NOTION_PAT`, `NOTION_PERSONAL_TOKEN`, `NOTION_USER` +
  `NOTION_PASSWORD`;
- `settings_block` — where in `aurora.config.yaml` the module looks for its settings. The registry does not parse them:
  each product has its own (for Confluence, e.g., the list of sync roots is a list of dictionaries).
- `mirror.legacy_path_key` exists only in the two oldest built-in modules: it enables them in projects where the
  `sources:` section does not exist yet.

The manifest is JSON, not YAML: it is read by the engine, not by a human, and has to be parsed without external dependencies.

## How a module gets into a project

1. The folder is put into the kit's `connectors/`.
2. `aurora.py update <project> --apply` spreads the manifest into `.opencode/connectors/<id>.json`, the script into
   `.opencode/scripts/`, and the sync skill's body into the project's existing skill folders (no overwriting: the kit
   version lands next to it as `.new`).
3. The module is connected to the project in `aurora.config.yaml`:

```yaml
sources:
  - id: Notion            # also the folder name in Sources/
    module: notion
    path: Sources/Notion
```

   The panel does the same: "Mirrors" → "Source modules" → tick and save.
4. The mirror folder is created by `update` (or `--structure-only`).

Check the result: `python3 .opencode/scripts/sources_registry.py`.

## Disconnecting

Unticking removes the module from `sources:` but leaves the mirror folder: an export is data, and the engine does not
delete data. `kit:doctor` will then call the folder nobody's — a remark, not an error. The decision (delete, move to
`Raw/`, reconnect) is a human's.

## A minimal export script

```python
from sources_core import BoardMirror, RestApi, read_secret, verify

class Api(RestApi):
    agent = "aurora-notion-export/1.0"

class Mirror(BoardMirror):
    banner = "Notion sync state — generated by notion_export.py, do not edit by hand"
```

Then — a loop over the source's records: fill `mirror.rows`, call `mirror.write_state()`, show `mirror.extra_files(...)`
and delete them on `--prune`. Ready samples are `scripts/jira_export.py` (board) and `scripts/confluence_export.py` (wiki).

Required for accepting a module:

- `--verify` passes (determinism);
- there is no export date or version in file headers or names — otherwise every run gives a diff on the whole folder;
- the state is written with full paths from the mirror root — `sync:audit` works by them.
