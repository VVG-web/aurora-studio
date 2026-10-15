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

The web mirror declares trust **per record**: every link has its own tick, because a law and somebody's
blog come from the same internet but must not be trusted equally. The declaration is written into the saved file's header as
`trusted: true|false`, and `kb:trust` reads it from there. It is believed only from mirrors whose
`source_kind` is in `WEB_SOURCE_KINDS` (`web`, `web-document`): another document module that wants to
declare trust the same way must add its own value to that list (later it moves into the connector manifest). So trust remains a property of the source and is not derived
from a path match on disk, and no second list of trusted folders is needed in the config.

Foreign values (a page title, an author name, an address) are collapsed to a single line when the header is written: a
line break inside a title would otherwise create a new header field, such as a second `trusted`. The field is read by
parsing the header, not by a substring match: a header line is only a real line of the file (U+2028/2029 and U+0085 do
not break it), and trust is believed only from a file that names itself a web mirror (exactly one `source_kind: web` or
`web-document`) — Confluence and Jira mirrors have no `trusted` field, so there it is foreign text. These count as a forgery, that
is "do not trust": `trusted` written twice, `source_kind` written twice with one web value, a web-mirror
mark next to `page_id` or `key` (what Confluence and Jira write), a header with no closing line within the
read ceiling (262144 characters) a header cut short by a forged line inside a title, and a title with no closing quote (mirror writers always
close it, while a line break in the title of an older file tore it off — whatever stands behind the forged line, such a file is
not read). The header is read
whole, not through a window of a few lines, so padding cannot hide the real field; the sync root's `page_id` and a Jira task's key, status, parent and type are read the
same way: a repeated field is a forgery and its value is not believed. Mirrors already written are not rewritten, but
`sync:audit` and `kit:doctor` name such files ("header with a repeated field or a forgery": a repeat, a line-break
character in a value, `trusted` outside a web mirror, an unclosed or cut-short header, an unclosed title quote; an ordinary document that merely starts with `---` and has no mirror fields is not named) — rewrite them with `--force` of the mirror's sync command.

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

## Manifest envelope and API version

A single shared loader reads manifests: `load_manifest(path, kind)` in `scripts/aurora_common.py`
(BL-ARC-44). Each extension kind (`connector`, `module`, `skin`) has its own mini-schema; what they
share is the "envelope": `id`, `api_version`, a title (a string or per language) and `requires`.

- `api_version` is the extension API version a manifest was written for. When the field is missing the
  manifest is in the pre-envelope format and is read as `1`; manifests already in projects need no
  rewriting. A version newer than the engine knows (currently `EXT_API_VERSION = 1`) makes the
  extension skipped with a reason ("update the kit") instead of being read by guesswork.
- The loader checks that the root is an object, that required fields (`id`, `kind`, `mirror`, `run`)
  exist, and that fields and nested objects (`mirror.default_path`, `run.command`, `auth.env_prefix`)
  have the right type. Unknown fields do not get in its way.
- **A bad manifest does not bring the engine down.** Broken JSON, a list as the root, a missing `id`,
  a wrong type, a future `api_version`, an unknown `role` or `kind` — the module is skipped, the reason
  goes into the report (`sources_registry.registry_report()`) and stderr as a line, and the other
  modules are read as usual. The name in a message comes from the file or folder until the `id` has
  been read and checked.
- A connector's `kind` stays the storage kind (`wiki` or `board`); the extension kind is passed to
  the loader by the caller.
- **Laying out into a project goes through the loader too.** `update` and `install` read
  `connectors/*/connector.json` via `load_connector_manifest` (the loader plus a name check). A bad
  manifest — no `id`, an empty file, not JSON, a root that is not an object, `run` as a string, a future
  `api_version`, or an `id`, `run.script` or `run.skill` containing "/", "\\" or ".." — produces a
  "Модуль источников пропущен: …" line in the `update` report (in dry-run and with `--apply`) or a
  `skip (манифест не принят)` line in the `install` report. The run goes on with the other modules and the
  exit code does not change. The `.aurora/connectors/<id>.json` file name comes only from an accepted
  manifest, so an `id` cannot move the file out of the folder. A manifest whose launch script is missing
  is installed without it (`skip (скрипт не найден)`). Doctor shows skipped manifests as one warning.
- Transition period: the panel's sections (`cockpit/modules/*/module.json`) are already read by the shared
  loader in `lenient` mode (an optional field of the wrong type gets its default and a `warn` instead of
  a refusal); the other kinds of extensions move over one by one, and until then both readings accept the
  format without `api_version`.
- `mirror.default_path` is required for a mirror but not for reading the registry: a module without it
  and without a path in `aurora.config.yaml` has no mirror, while the other modules keep working.

## How a module gets into a project

1. The folder is put into the kit's `connectors/`.
2. `aurora.py update <project> --apply` spreads the manifest into `.aurora/connectors/<id>.json`, the script into
   `.aurora/scripts/`, and the sync skill's body into the project's existing skill folders (no overwriting: the kit
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

Check the result: `python3 .aurora/scripts/sources_registry.py`.

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
