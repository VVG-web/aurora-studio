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
- `access.mode` — how the source is reached by default: `internet` («no VPN, needs the internet», the web module), `vpn`,
  `local` or `mixed`. No field — the source counts as available, as always. A project overrides the mode with a
  `source_access:` block (in the panel: «Project settings → Source access mode»); see the
  [connection check](connectivity.md).

The manifest is JSON, not YAML: it is read by the engine, not by a human, and has to be parsed without external dependencies.

## Manifest envelope and API version

A single shared loader reads manifests: `load_manifest(path, kind)` in `scripts/aurora_common.py`
(BL-ARC-44). Each kind the loader reads (`connector`, `module`, `profile`) has its own mini-schema; what they
share is the "envelope": `id`, `api_version`, a title (a string or per language) and `requires`.
Skins and Git providers are not read by the loader. A skin header is parsed by `cockpit.skins()`,
and it shares one rule with the manifests, the version check (`ext_api_problem`: the header field
`api` is checked exactly like `api_version`). `gitproviders/<id>/provider.json` is read by `git_sync`,
and its fields are checked by a test (`tests/cases/part_44.py`), not by the loader.

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
  `api_version`, or an `id`, `run.script` or `run.skill` containing "/", "\\" or "..", or a `mirror.default_path` that is not a plain
  relative path (only "/"-separated segments of letters, digits, "_", "-", dots and spaces are allowed; a segment
  starts with a letter or digit and does not end with a dot or space, one trailing "/" is fine; anything else — a
  leading "/" or "\\", a drive letter, ".." or ".", an empty segment, a line break or other control characters,
  "#", ":", quotes, brackets — is refused) — produces a
  "Модуль источников пропущен: …" line in the `update` report (in dry-run and with `--apply`) or a
  `skip (манифест не принят)` line in the `install` report. The run goes on with the other modules and the
  exit code does not change. The `.aurora/connectors/<id>.json` file name comes only from an accepted
  manifest, so an `id` cannot move the file out of the folder; for the same reason `mirror.default_path`
  is checked before the installer's `ensure_dir`, and the panel does not write into `aurora.config.yaml`
  a mirror path that failed the same check (`aurora_common.ext_safe_relpath`). A manifest whose launch script is missing
  is installed without it (`skip (скрипт не найден)`). Doctor shows skipped manifests as one warning.
- Transition period: the panel's sections (`cockpit/modules/*/module.json`) are already read by the shared
  loader in `lenient` mode (an optional field of the wrong type gets its default and a `warn` instead of
  a refusal); the other kinds of extensions move over one by one, and until then both readings accept the
  format without `api_version`.
- `mirror.default_path` is required for a mirror but not for reading the registry: a module without it
  and without a path in `aurora.config.yaml` has no mirror, while the other modules keep working.
- **A mirror path from a manifest is checked in the registry itself, not by every consumer.** A manifest that already lies in
  `.aurora/connectors/` (copied earlier or put there by hand) and carries a `mirror.default_path` that is not a plain
  relative path (`../outside_dir`, a line break, ":" and the rest of the list above) is dropped by
  `sources_registry.load()`: an empty result and a line in the registry report (`registry_report()`, doctor, stderr).
  So `update` (including `--structure-only`) does not create a folder outside the project from a manifest, and the
  settings form does not write into the config a line that would inject foreign keys. The check applies only to paths
  from manifests. A path from the project's own `aurora.config.yaml` (the `sources:` section, the `paths:` keys) is
  written by the owner, so the registry, `update` and the settings form pass it through as is: `Sources/Wiki (old)`,
  `Sources/JIRA (PRJ-A)`, `Sources/Wiki+Docs` and `../Shared/Confluence` stay connected, and saving the settings form
  does not erase such an entry. The owner's value is not rewritten, but `update` does not create a folder outside the
  project for it: a path with ".." or an absolute one is skipped with the report line "Путь зеркала вне проекта — папка
  не создана: <path>" (the mirror path is outside the project, the folder was not created); folders inside the project,
  `Sources/Wiki (old)` among them, are created as before. When connecting a module the panel checks the manifest's path itself (the registry in
  a project may be a copy of an older release) and also refuses a manifest with no path at all. The registry prints
  JSON to stdout and report notes to stderr; the panel takes the first JSON object of the output, so a "}" in a note's
  text does not empty the "Sources" section.

**What is kept when the panel or the settings form writes.** "Save choice" ("Mirrors" → "Source modules") does not rebuild
`sources:` from manifests: entries of already connected modules stay verbatim (the owner's id and path, a second instance of the
same module, extra keys of an entry, nested lists, comments between entries), and with the ticks unchanged the config is not rewritten at all. The panel reads the section with the same `sources_registry.split_section` as the registry, so an entry without `module:` (the registry does not connect it) and the comments above the first entry are not lost when the choice is saved. The settings form reads the section with the same parser and takes the entries already declared in the config as **raw text**: extra keys (`exclude:`, `note:`), nested lists, comments and entries without `module:` survive saving the form byte for byte; the form generates an `id`/`module`/`path` line only for a connected module that has no entry in the config (first setup, historical mirrors from `paths:`). The `mirror.default_path`
and the `ext_safe_relpath` check apply only to a newly ticked module; an unticked module removes all of its instances (an entry of
a module that is not installed stays: there is nothing in the panel to tick or untick it with). While the `sources:` section does
not exist, the historical mirrors are carried into it with the paths from `paths:`. The settings form writes
`paths.sources_confluence` and `paths.sources_jira` from the previous config (the defaults `Sources/Confluence` and
`Sources/JIRA` only when the key is absent): `confluence_export.py` and `jira_export.py` read those keys while the registry and
doctor read `sources:`, so the owner's path has to be the same in both sections. Who checks a path: one from a manifest is
checked against the whitelist (`ext_safe_relpath`), one from the project's own config is not checked by anyone: the owner writes it.

**One parser for the `sources:` section (BL-NEW-02).** A single registry function reads the section, `sources_registry.declared_chunks(text)`, and returns `[(module, id, path, raw text of the entry)]`. A new entry starts with "- " at the indent of the section's first entry or to the left of it; everything deeper (a nested `exclude:` list, extra keys), comments and blank lines inside an entry and after it stay as its raw text, so `id`/`module`/`path` are taken from the entry's top level and not from the items of a nested list: an entry with `exclude:` before `module:` is read correctly. A trailing comment on a value line (`id: Confluence  # folder name`) is not part of the value. An entry without `module:` comes back with `module=None` (the registry does not connect it, but does not lose it either); comments above the first entry are the section's "head" (`split_section`); the head plus the raw entries joined give the section body back byte for byte. `has_section(text)` tells an empty `sources:` header from a missing section. The registry's `declared()` is built on this parser; the panel ("Save choice") and the settings form write the section with it too.

**An empty `sources:` header and the legacy fallback (BL-NEW-02).** If the `sources:` section is present in the config, the registry takes only what it declares (`declared()`); it goes back to `paths:` only when there is no section at all. A header with no module entries means "nothing connected": the historical mirrors from `paths:` do not come back to life in such a project, `instances()`, `mirror_paths()` and `roles()` are empty, and doctor shows no legacy mirrors. Unticking the last module in the panel writes an explicit empty `sources:` (with a comment above it), saving the same choice again leaves the config untouched, and the settings form keeps an empty header empty (it used to drop the section, and the mirrors came back from `paths:`). To get the historical mirrors back, delete the `sources:` header entirely or tick the modules in the panel.

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

## Confluence page attachments

The files a page body refers to are saved next to the page, in `<page>_assets/`, and the text keeps a link to the local
file:

- attachment viewer macros `view-file`, `viewpdf`, `viewdoc`, `viewxls`, `viewppt` (they used to vanish without a trace) →
  `[name](<page>_assets/name)`;
- an attached image `<ac:image>` → `![name](<page>_assets/name)`; an image by external address (`ri:url`) stays a link to
  that address and is not downloaded;
- an attachment link `<ac:link>` → `[label or name](<page>_assets/name)`;
- an attachment of another page (`ri:page` inside `ri:attachment`) is not downloaded: the text carries the note
  `[вложение другой страницы: name]` ("attachment of another page").

The target of an `<ac:link>` (page, attachment, person) is taken from the link's direct children only: a link to a page
with an icon image in its body stays a page link, and the icon is not downloaded.

The page ends with a `## Вложения страницы` ("Page attachments") section (draw.io and mermaid diagrams stay in
`## Схемы страницы`). A downloaded file is a row `- [name](<page>_assets/name)`; a file that was not downloaded is a row
`- не скачан (reason): [name](Confluence address)` ("not downloaded"), and the link in the text leads to the original. The
reasons: executable type, no attachment of that name on the page, markdown inside the mirror (`.md`, `.markdown`: they would be taken for pages), service name, download error. **Executable, script and web-page (html and kin) files are never
downloaded** (`exe`, `bat`, `cmd`, `com`, `msi`, `dll`, `jar`, `js`, `vbs`, `ps1`, `sh`, `scr`, `py`, `pl`, `rb`, `php`, `command`, `lnk`, `hta`, `wsf`, `vbe`, `jse`, `reg`, `msc`, `cpl`, `pif`, `bash`, `zsh`, `psm1`, `pkg`, `dmg`, `apk`, `app`, `html`, `htm`, `xhtml`, `shtml`, `mht`, `mhtml`).
A name with a leading dot (`.gitignore`, `.gitattributes`) is not downloaded either ("service name"): such a file in `_assets/` would change git's behaviour for the folder. A download failure of one file does
not fail the page and does not delete its previous copy. But when the attachment list was obtained and the name is not in it (the attachment was deleted in Confluence), or the policy (mode, size, type) no longer admits the file, the copy leaves `_assets/`
(an empty folder too) and the link leads to the original; a page with no attachments in its body does not keep the folder. When the list is unavailable (a network failure) nothing is deleted. Different attachments whose names coincide after being made safe
(a forbidden character) or differ only in case get separate files: the second one gets a fingerprint of its full name (`Law~1a2b3c.pdf`). The type is checked both by the original name and by the name the file will get (`setup.exe.` does not bypass the ban). A page whose file failed to download is not counted as fresh: the next sync re-reads it without waiting for the weekly full pass; while the attachment list is unavailable the folder is not cleaned. The file name is made safe (no path, no characters forbidden on Windows, length-limited; an extension such as `.xlsm` is kept on a long name). A page name with "#", "%", "?" and round brackets is encoded in links. `kb:names` does not move names that carry a path (`..%2F`). The same page text gives the
same markdown. The converter version is 2: the first sync after the update re-reads pages and fetches the missing
attachments. The `kb:names` repair recognises both sections and rewrites the file links embedded in the text together with
a rename.

**Which attachments to download.** In the `atlassian.confluence` block of `aurora.config.yaml`:

```yaml
atlassian:
  confluence:
    # all (default) | images | none
    attachments: all
    # limit for one file, MB (default 100; 0 = no limit)
    attachment_max_mb: 100
```

`images` downloads only images (by extension or an `image/*` type), `none` downloads nothing: links in the text lead to the
original in Confluence. An unknown value is read as `all`; a non-numeric or negative `attachment_max_mb` is read as 100; in both cases the sync prints a notice to stderr. A trailing comment on the value line is tolerated. The
size comes from the attachment properties (`fileSize` in the `child/attachment` response) **before** downloading; when it is
missing, the read itself stops at the limit. Check order: executable type (always) → mode → size. What was skipped stays a row
in `## Вложения страницы`: `- не скачан (режим images): [name](original)` or `- не скачан (больше 100 МБ): [name](original)`
("not downloaded (mode images / larger than 100 MB)"). Diagram sources (draw.io, mermaid) ignore the mode; the size limit applies
to them too (a row in `## Схемы страницы`: `не скачана (больше N МБ)`). 
**Where to edit.** In the panel: "Project settings" → the project form, right below the Confluence address and space: a "Confluence attachments" choice ("all that the page links to" = `all`, the default; "images only" = `images`; "do not download" = `none`) and a "Maximum file size, MB" number (a whole number, 0 or more; empty = 100, `0` = no limit). The form reads the values from the config when it opens and writes them with the same "Save" as the other fields. What is downloaded goes into the mirror's git: for sources with large documents (laws, regulations) set a lower limit or "do not download"; executable and script files are never downloaded. Checked before writing: the mode must be one of the three words and the limit a whole number from 0, otherwise the save is refused with a reason and the config is not changed. A value equal to the default does not add the key to a config without it; an empty limit removes the key (100 applies); "all" stays explicit when the key is already written. A fractional limit written into the config by hand passes while the field is untouched. Saving other form fields, the console wizard `aurora_setup.py` and writes of other settings groups do not erase the keys. The `confluence:` block is read by one rule everywhere (sync, wizard, panel): a comment after the colon and a different block indent are fine. The form sends the policy only when it was changed: saving the name, the roots or Jira leaves the policy alone.

**Sync summary.** When there were attachments, the summary gets a line next to the diagrams one: `Вложения: скачано N ·
пропущено по типу M · по размеру K · ошибок E` ("Attachments: downloaded N · skipped by type M · by size K · errors E"; a
`· нет на странице P` part is added for attachments the page does not have). A failed attachment is not a failed page: the
sync exit code is unchanged, the error count shows in the line, and the page is re-read on the next sync.
The counters are shared by files and diagram sources: a saved diagram counts as downloaded, a diagram over the limit counts
under "by size".
**Changing the policy.** Pages with attachments or diagrams remember the policy (`mode:limit`) in the sync cache: change
`attachments` or `attachment_max_mb` and those pages are re-read on the next sync even when their Confluence version is
the same (the version cache would otherwise skip them and files skipped earlier would never be fetched). Pages without
attachments are not affected by the policy. A re-read page downloads all of its attachments again, including those already
in `_assets/`: raising the limit on a large mirror means re-downloading the attachments of those pages.
Sync root pages have no version from the child list and are re-read on every sync; their attachments are not downloaded again while the page version, the policy and the converter are the same, the cache entry is fresh (not older than a week), and the file is in place with a size equal to the attachment's property. Once a week and with `--force` it is a full download.
**Cleanup with `--prune`.** The `<page>_assets/` files of a vanished or moved page go together with it and count neither as "extra" in the list nor in the cleanup threshold (which counts everything else that is extra, stray files included).
**Attachment folder and neighbouring pages.** The `<page>_assets/` folder belongs to the page and is cleaned of attachment files only: the sync never touches `.md` files or subfolders in it. A page whose title ends in "assets" ("Design assets") gets its id in the name (`Design_assets_<id>`); otherwise it would share a folder with the attachments of the page "Design". If `<page>_assets` in the mirror is a symbolic link (or sits inside one) pointing outside the mirror, the sync writes nothing into it and deletes nothing from it: the page's attachments stay links to the original, a line about the linked folder goes to stderr, and the summary counts an attachment error.

## The Confluence page tree: what the sync shows

The sync walks down the tree with the "children of a page" request (`/rest/api/content/{id}/child/page`, 50 per
response, then `_links.next`), and a subtree the server did not hand over used to vanish silently: the page is
visible in the web interface and absent from the mirror. Now the sync prints (and the panel's Console shows):

- per root: `Корень <id> (<title>): страниц N · максимальная глубина D · страниц с детьми K` (pages N, maximum depth
  D, pages with children K) — a root of three levels with "depth 2" is visible at once;
- per branch whose child list is incomplete: `ветка <title>: сервер отдал N из M` (the server gave N of M — checked
  against the response's `size` field and against `_links.next` not followed to the end) or
  `ветка <title>: отказ 403 (нет доступа под этим токеном)` / `отказ 404 …` (refusal) — a refusal on a child list
  does not break the sync: the page is written, the other branches are exported;
- in the summary: `ветки с отказом сервера: N` (branches the server refused) and the list of those branches. A
  refusal (403, 404, a cut-off list) is an export error, exit code 1; "fewer than announced" leaves the code as it
  was. Both close `--prune`: what was not reached would look deleted. A page that had children and whose list did not
  arrive stays a folder (`index.md`) instead of turning into a leaf.

The child list of an unchanged page is re-read on every sync (the page version does not matter), so a shortened
answer shows even on a sync where nothing changed.

`sync:confluence --diagnose-tree` (in the panel — Mirrors → the Confluence card → "Page tree") prints the tree of
each root — page, id, number of children, a mark of a refusal or of a mismatch with `size` — and a total per root; it
writes no files and does not need the page parser (`beautifulsoup4`). If a page is missing in the tree but visible on the web, the cause is one of three: the token's
rights on that branch, a moved page, or a stale engine copy (`kit:doctor` names an incomplete engine file by file,
Version → Prepare project).

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
