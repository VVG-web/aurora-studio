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
- `service_role` (optional) — what kind of service stands behind the mirror: `tasktracker`, `projectwiki`, `documents`,
  `coderepo`, `testmgmt` or `webpages`. The core finds the mirror by it (`aurora_common.service_dirs`), so replacing a
  tracker or a wiki means another connector with the same role, not edits to core files. The list is closed: an unknown
  role does not disable the module, it only leaves it without a role (a line in `registry_report()`). Missing from a
  project's copy of the manifest, it is taken from the manifest of the same module in the kit;
- `aliases` (optional) — old product names that configs and scenarios know it by (`["jira"]`):
  `sources_registry.canonical_role("Jira")` returns the role. A name that two modules share between different roles
  resolves to no role at all;
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
- `operations` (optional) — the module can do more than a mirror: the operations of a role, a task tracker (`tasks`)
  or a wiki (`wiki`). Which role it plays, which operations it can do and which script runs them — see
  ["Operations: task tracker and wiki"](#operations-task-tracker-and-wiki);
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

## Text of attachments and diagrams: `Raw/Extracted/`

Page attachments stay in the mirror as they are; `kb:ingest-office` turns the knowledge in them into text:

| What | Where the transcript goes |
|---|---|
| documents and diagrams in `Raw/` | next to the original (`X.md`; the name taken by foreign markdown — `X.converted.md`; `X.docx` and `X.txt` share a name — the second gets `X.txt.md`) |
| attachments in `Sources/**/<page>_assets/` | `Raw/Extracted/<source>/<page path>/<full attachment name>.md` |
| diagrams in `Workspaces/` | `Raw/Extracted/Workspaces/<path>/<full name>.md` (diagrams only; these drafts do not go into knowledge parsing) |

**Why not in the mirror.** The sync owns the mirror: its `--prune` and `sync:audit` ("strays") would remove foreign files. The
mirror and the originals are never changed by the command.

**What is converted.**

- documents: docx (the built-in converter, the same on any machine: headings by style, `basedOn` and `outlineLvl`, nested lists,
  tables — and with merged cells row by row as "column: value", so that nothing is lost; links, footnotes at the end of the
  section, accepted tracked changes, reviewer comments in a separate "Комментарии" block; headers, footers, page numbers and the
  table of contents are left out), xlsx, pdf (the text layer with page anchors `<!-- стр. N -->`, without running headers and
  page numbers; a scan is read by a vision model if one is configured), pptx, odt, rtf, csv, txt;
- diagrams: draw.io (pages, nodes, containers and lanes, links with labels), BPMN 2.0 models (a summary, participants, lanes, the
  process flow from the start events, subprocesses, calls, gateways with conditions, message flows), DMN decision tables, mermaid
  diagrams — **without a model**; foreign XML is read safely: DOCTYPE and ENTITY are refused, size, nesting depth and element
  count are capped, and `scriptTask.script` stays text;
- images (optional, off by default): jpg, png, webp, gif are read by the vision model of the recognition ring, svg is read as
  text without a model. Icons under 8 KB are not sent to the model.

**The transcript header.** `converted_from`, `converter`, `converted`, `source_hash`, `status: draft`; an attachment also gets
`page` (the owning page) and `attachment`; `quality: low` and `quality_note` when the text is unfit; a split document has
`part`, `parts`, `part_of`, `prev`, `next`. A transcript is a machine copy; the original is the truth.

**A repeat run** compares the original's hash: unchanged — nothing is written; changed — the transcript is re-read; the original
is gone (the mirror is in place) — the transcript goes with its empty folders; the mirror itself is missing — nothing is removed.
Attachments with identical content get one transcript. An attachment with too little text (a logo, an icon, an empty diagram)
gets none, and the command remembers its hash in `Raw/Extracted/.skipped.json` so as not to try again, and the doctor and the
plan do not call it "not converted". Foreign markdown in the transcript folder is left alone.

**Limits.** The attachment size limit is the same `attachment_max_mb` as the sync's; attachments and diagrams per run —
`ingest.max_files` (200 by default; written transcripts are counted, the rest comes with the next run; documents from `Raw/` are
not capped); the text of an attachment transcript — 200,000 characters; a long document is split into `<name>-часть-NN.md`
parts by top-level headings (a pdf without headings — by 40 pages). A transcript path that does not fit 200 characters gets its
name cut to a hash, and the summary says how many.

**Low quality.** Empty or unreadable text (a scan without recognition, a broken encoding, repeated characters, not a single
sentence) is marked `quality: low`: such a transcript is not parsed and is named in the parsing plan, in the route summary and in
`kit:doctor`. Fix the original or turn recognition on and run `kb:ingest-office --force`.

**What is not read.** Text inside docx text boxes is not extracted; pictures inside documents stay in the transcript as a
placeholder such as `![image1.png]`; pptx, odt and rtf are read only by `pandoc` or `markitdown` (there is no built-in reader
for them; without either the file is named "no suitable converter"); web connector attachments (`Sources/Web/…/_files/`) are
converted by `sync:web` itself in the old way, without splitting into parts and without the quality mark. If a page moved, its
transcript is made again at the new path, and cards built on the old one keep pointing to the previous path until the page is
parsed again.

**What parsing sees.** The plan takes transcripts as a separate "Вложения" group after the pages; drafts from `Workspaces/` and
low-quality transcripts are not in it. A card built from a transcript lists the owning page in `sources` too, and the
transcript's trust is the page's trust. A file without a current transcript is named by the plan ("no transcript"), not skipped
silently. A page whose only knowledge is a link to a diagram that has a transcript no longer gets a "no text in the source"
stub. Web connector attachments (`Sources/Web/…/_files/`) are not parsed by this command: `sync:web` converts them itself.

**Where to edit.** "Project settings" → "Extraction from attachments": the switches "Extract text from attachments" (on),
"Extract draw.io diagrams and BPMN models" (on), "Recognise images with a model" (off) and the files-per-run cap. They are
written to the `ingest:` section of `aurora.config.yaml`:

```yaml
ingest:
  attachments: true
  diagrams: true
  images: false
  max_files: 200
```

## Path length: the check on saving settings

A project is a git repository opened on Windows, macOS and Linux, and the root differs from person to person: a colleague on Windows
usually has `C:\Users\<name>\Documents\…`, longer than yours on a Mac. A path that is created on one system is not created at all on another.
The limits the engine counts by (`scripts/path_budget.py`):

| System | File path | Folder path | Name |
|---|---|---|---|
| Windows (long paths not enabled) | 259 characters (MAX_PATH 260 with the terminating null) | 247 characters (creating a folder is stricter) | 255 UTF-16 characters |
| macOS | 1023 bytes (PATH_MAX 1024 with the null) | 1023 bytes | 255 UTF-16 characters (as on Windows) |
| Linux | 4095 bytes | 4095 bytes | 255 **bytes** of UTF-8: Cyrillic takes two bytes a letter, about 127 letters |

**Where it shows.** "Project settings" → the project form. Saving the form (and saving the config text below it) goes through as before
and returns findings: a red line under the Confluence root row or the website address, and a box under the "Save" button -
"Long paths: N lines — you can run, but not everything will be loaded". Nothing is blocked: a path you wrote into your own config is not
limited or "fixed", it is only reported. The same findings come from:

- `kit:paths` — a report in the terminal and the panel (`--json` for machines); read-only, exit code 1 when there are findings;
- `kit:doctor` — one warning with the number of lines (not an error, not a blocker);
- routes with sync steps (for example "Update the base"; button, Cron, terminal) and a single-button sync — one line "Long paths: not everything
  will be loaded — <lines>" at the start of the run (not at every step) and a reminder in the summary (the files that did not fit are named by the
  sync summary); in the console and in the run's `console.log`. When it does not fit only on another system (the project root there is an
  assumption), the line says so separately: "This run is not affected, colleagues will not get everything loaded".

**What is checked** — every config line that names a place on disk, and the message names it as written:

| Line | What is counted |
|---|---|
| `sources: <id> → path`, `paths → sources_confluence`, `paths → sources_jira` | the mirror folder: its path, the files and folders already in the mirror (an `os.scandir` walk, a ceiling of 60,000 files — "checked partially"), for an empty Confluence or Jira mirror the worst case by the exporter's rules |
| `atlassian → confluence → sync_roots → line N` | the files of this root's subtree (by the sync state `sync_state.md`) |
| `web → pages → line N` | the page file name by the `web_export.slug` rule (up to 60 characters of the title plus the address tail) and the attachment names in `_files/` |
| `paths → knowledge_db`, `paths → extra_structure_dirs → line N`, `artifacts → <kind> → out`, `reports → <name> → output` | the folder or file path and what is already in the folder |

**The project root.** For the system you work on it is the real path, for the others an assumption, named in every message: Windows
`C:\Users\username\Documents\Projects\<project folder name>`, macOS `/Users/username/Documents/Projects/<name>`, Linux
`/home/username/Projects/<name>`. These are code constants (`WIN_ASSUMED_ROOT` and its neighbours), not a file setting. The check is
conservative: it counts the worst case and says "if Windows long paths are not enabled".

**What the messages mean.**

- *"N files will not fit: the longest path is M characters against the limit of 259 on Windows"* — the files already lie in the mirror
  (made on a machine with a short root) and will not be created on a system with such a root: `git clone` says "Filename too long" and the
  sync cannot update them. The config line is named; for a Confluence root it is the line under whose subtree the files lie.
- *"in the worst case the path reaches … against the limit of …"* — the mirror is still empty (or this is the worst case by the naming rules):
  the engine keeps a page path from the project root within 200 characters (`PATH_CHARS`), and a root longer than 58 characters eats this
  reserve; website file names and Jira issues are bounded by the rules of their exporters. For Confluence the alarm exists only when the
  settings have sync roots.
- *"the folder path is … against the limit of 247"* — Windows will not create such a folder even if the files in it are short.
- *"the folder path takes … and the shortest file needs … more"* — nothing fits into this folder.
- *"the name "…" is N characters/bytes against the limit of 255"* — a long name: Windows and macOS count UTF-16 characters (it fits nowhere),
  Linux counts bytes (127 Cyrillic letters pass, 128 are not created on Linux only).

**What to do.** Shorten the project folder name or move the project closer to the disk root (`C:\Projects\…`); or enable Windows long paths
(`LongPathsEnabled` = 1 in the registry or a group policy) and run `git config --global core.longpaths true`; for a long path you wrote into the
config yourself, shorten it in the config; for a name, shorten the name.

**What the exporters do on overflow.** Confluence keeps a page path from the project root within 200 characters (`place()`): a page deeper
than the limit goes into the folder of its nearest ancestor. If the path still does not fit the system (a long project root on Windows),
the page is not written: the sync goes on, the log has "! page N: the path does not fit this system (length M, path limit L, name limit 255) — not written",
the summary has "Long paths: not written mirror files: N", the page stays out of the cache (the next sync tries again), and if a page itself is
lost `--prune` removes nothing in this run (lost attachments do not stop the cleanup). An attachment with no room is not downloaded in vain.
The website export behaves the same way (when a page is lost `--prune` removes nothing either), in Jira a lost issue stays out of the state; if the mirror folder itself cannot be created, there is one line with the reason and exit code 1, no traceback.

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

## Operations: task tracker and wiki

A mirror reads a source into git. There is also the way back: create a task in the tracker for a document, link the
published page to it, publish a document to the wiki. To keep the tracker and the wiki replaceable, the core does not
know them by name: it knows a **role** and **operation names**, and the connector knows how to run an operation. If
tomorrow another tracker replaces Jira, a connector that can do the same operations is installed and chosen in the
project settings; Aurora's code does not change.

| Role | What it is | Built-in connector |
|---|---|---|
| `tasks` | task tracker | `jira-dc` (Jira Server / Data Center, REST API v2) |
| `wiki` | wiki | `confluence-dc` (Confluence Server / Data Center) |

### Manifest: the `operations` field

```json
"operations": {
  "role": "tasks",
  "script": "jira_ops.py",
  "ops": ["issue_types", "epics", "find_user", "create_issue", "link_page", "get_issue"],
  "server": "base_url",
  "defaults": {"project_key": "project_key"}
}
```

- `role` — `tasks` or `wiki`; the role of the operations, not the trust role (the top-level `role`);
- `script` — the operations script: a plain file name. It lies next to the manifest (`connectors/<id>/`) or in the kit's
  `scripts/`; `update` and `install` put it into the project's `.aurora/scripts/` next to the export script
  (`aurora_common.connector_scripts`), and `kit:doctor` lists it among the engine files;
- `ops` — the operations the connector can do. For anything not in the list the core answers "cannot do" and does not
  call the script;
- `server` — which connector setting (in its `settings_block`) names the server: it is part of the cache key;
- `defaults` — default operation parameters taken from the connector settings: `{"parameter": "setting key"}`. For
  `jira-dc` the tracker project key comes from `atlassian.jira.project_key`.

A manifest without `operations` (a project copy older than the field) still gets the role — by the service role
(`tasktracker` for `tasks`, `projectwiki` for `wiki`) — and answers an operation with "cannot do — update the project
engine".

### Which connector is active

The `connectors:` block in `aurora.config.yaml`:

```yaml
connectors:
  tasks: jira-dc
  wiki: confluence-dc
```

Without the block or the role line, the single installed connector that declares the role is used. If several declare
it, the operation does not run until a human chooses ("several connectors declare the role"). A chosen connector that is
not installed or does not declare the role is a refusal with the reason, not a silent substitute.

The panel writes the block: `GET /api/connectors/roles?project=…` returns, per role, the active connector, where the
choice comes from (`config` — the block, `single` — the only one), whom to choose from (with the operations the
connector cannot do) and the block snapshot `digest`; `POST /api/connectors/roles/save` with
`{project, roles: {role: id}, expect: <digest>}` writes the block. An empty id returns the role to the default choice.
A stale (or missing) snapshot is a 409 answer and nothing is written. The write changes only the `connectors:` block:
the rest of the config, comments and line endings stay byte for byte. In the panel this is the group **Project settings →
Task tracker and wiki**: a drop-down per role (it lists the connectors that declare the role; the "default" item is the only
one with the role), a hint at each field, "Save" with the snapshot; a mismatch is a line with the reason and "Reload", no
confirmation windows.

Core: `scripts/connector_ops.py` — `active(project, role)` (the id of the active connector), `supports(project, role,
op)`, `call(project, role, op, params, fresh=False)`, `state(project)`, `save(project, roles, expect)`. From a console:
`python3 .aurora/scripts/connector_ops.py` (who is active) or
`python3 .aurora/scripts/connector_ops.py --role tasks --op issue_types --params '{"project_key": "PRJA"}'`.

### The call contract

An operation is a separate process. The core runs `python3 <script>` in the project root and writes to stdin:

```json
{"op": "create_issue", "params": {"project_key": "PRJA", "type": "Story", "summary": "…"}, "project": "/path/to/project"}
```

The script prints one JSON line to stdout (the last line with an `ok` field is the answer):

```json
{"ok": true, "result": {"key": "PRJA-12", "url": "https://tracker.example.com/browse/PRJA-12"}}
{"ok": false, "kind": "auth", "error": "the reason text from the server"}
```

The exit code is 0 on a refusal too: a refusal is an answer. The limit is 300 seconds per call, then a `net` refusal.
A ready frame is `sources_core.serve_ops({name: function(params, project)})`: the function returns `result` and reports
a refusal with `OpFailure(kind, text)` or a request failure — `sources_core.failure_of` derives the kind (401 → `auth`,
403 → `forbidden`, 404 → `not_found`, 400 → `bad_request`, 409 → `conflict`, 5xx and a broken connection → `net`).
When the server refused, `serve_ops` also puts the response code (`status`) into the answer: by it the caller tells "the
server refused" from "the connection broke" (publishing tells the `http` and `network` outcomes apart this way).

**Refusal kinds** (`kind`). The core names the reason to a human from the `connector_ops.CAUSES` table in Russian and
English, and passes the connector's text alongside as a detail (`detail`):

| `kind` | What happened |
|---|---|
| `config` | the connector is not set up: no server address or another required setting |
| `auth` | no sign-in (no key in the machine's env file) or the server rejected it |
| `forbidden` | the key has no rights for the operation |
| `not_found` | the server did not find what was asked for (an issue, a page, a person) |
| `bad_request` | the server rejected the request; the parameters do not fit |
| `conflict` | the server has another version (the page was edited after it was read) |
| `net` | the server did not answer: network, VPN, timeout, 5xx |
| `unsafe` | the key is not sent to this address (the address changed, plain http) |
| `unsupported` | the connector has no such operation |
| `failed` | another failure |

The core adds its own: `bad_role`, `no_connector`, `ambiguous`, `unknown_connector`, `no_script`, `bad_response` (the
script did not answer by the contract).

The core's answer (`connector_ops.call`): `{"ok": true, "result": …, "connector": "<id>", "cached": false}` or
`{"ok": false, "kind": …, "error": "<the reason in words>", "detail": "<the connector's text>", "connector": "<id>"}`.

### Operations of the `tasks` role

| Operation | Parameters | Answer (`result`) |
|---|---|---|
| `issue_types` | `project_key` | `{types: [{id, name, description}]}` — without sub-tasks |
| `epics` | `project_key`, `limit` (default 200, up to 1000) | `{epics: [{key, title, status, done}]}` — not closed first |
| `find_user` | `email` | `{name, key, display, email}`; `name` is what is set as the assignee |
| `create_issue` | `project_key`, `type`, `summary`, `description`?, `assignee`?, `labels`?, `components`?, `epic`? | `{key, id, url, epic_via}` |
| `link_page` | `issue_key`, `url`, `title`? | `{id, created}`; a repeat makes no second link (`created: false`) |
| `get_issue` | `key` | `{key, summary, status, status_category, type, url}` |

`labels` and `components` are lists of strings. `assignee` is the login from `find_user`. In `jira-dc` the epic is set
through the Epic Link field: its id is found in `/rest/api/2/field` by the schema `com.pyxis.greenhopper.jira:gh-epic-link`
(the fallback is the field name); without such a field the epic becomes the parent (`parent`, as in Jira Cloud), and the
answer says so in `epic_via`. Creating an issue and a link is a single attempt: a retry after a broken connection would
create a second one.

**Cache.** `issue_types` and `epics` are cached for an hour in the project's `.aurora/state/cache/connectors/` (outside
git, each machine has its own). The key is the connector, the server address (the `server` setting), the operation and
the parameters; there are no access keys in the file name or inside. `call(..., fresh=True)` and
`GET /api/task/options?fresh=1` fetch anew.

### Operations of the `wiki` role

Exactly what publishing (`ship:publish`) does with the wiki: since 1.200.0 publishing goes **only** through these
operations of the active `wiki` connector (`publish_doc.Wiki` over `connector_ops`); there is no Confluence REST client in
it. A page in the answers is `{id, title, space, version, by, when, ancestors: [{id, title}], body, url}`; `body` is the
wiki markup (storage in Confluence), absent if not asked for.

| Operation | Parameters | Answer (`result`) |
|---|---|---|
| `site` | — | `{server, space, parent, auth}` — the project's wiki settings without the network: the address, the default space and parent of new pages, the sign-in method (empty — no key); publishing says "no address" and "no access" by them before the first request |
| `find_page` | `space`, `title`, `body`? (default yes) | `{page: a page or null}` |
| `get_page` | `page_id`, `body`? | the page with version, ancestors, space |
| `children` | `page_id` | `{pages: [{id, title, version, …}], complete, gap, got}` — all pages of the list (`_links.next` continuations are read); `complete: false` and `gap` — the server did not give everything |
| `create_page` | `space`, `title`, `body`, `parent_id`? | the created page |
| `update_page` | `page_id`, `title`, `body`, `version`, `space`?, `parent_id`?, `message`? | the updated page; `version` is the version the caller saw; edited later — `conflict` |
| `move_page` | `page_id`, `parent_id`, `version`?, `message`? | the page under the new parent; text and title as on the server |
| `page_space` | `page_id` | `{id, title, space}` |

### A task for a document: `ship:task`

`ship:task <file>` is a plan: which tracker, project key, type, epic, title, assignee, labels, components, which page
will be linked. `ship:task <file> --apply` creates the task through the active connector of the `tasks` role.

Properties, in order: the command flags (`--project-key`, `--type`, `--assignee`, `--epic`, `--labels`, `--components`,
`--summary`) → the `task:` block of the document header → the task properties of the kind (`artifacts.<kind>.task`) →
the connector settings (`operations.defaults`):

```yaml
---
type: us
task:
  project: PRJA
  type: Story
  epic: PRJA-7
  labels: analytics, release-2
---
```

The default assignee is the person with the project's `git config user.email` (`find_user`); if not found, the task is
created unassigned, and this is said. An e-mail written in `assignee` is looked up too; not found — a refusal. The task
title is the document title.

After creation `task_key` and `task_url` go into the document header — with the same write as the publish stamp: whole
or not at all, the file's BOM and line endings are kept. If the header has `published_url` (the document is published),
the page is linked to the task (`link_page`). If `task_key` is already there, no second task is created: the command
names the first one and only links the page.

The result is a machine line `AURORA-TASK {...}`: `outcome` (`plan`, `ok`, `exists`, `blocked`), `rc`, `connector`,
`task` (the properties), `key`, `url`, `page_url`, `linked`, `stamped`, `code` and `error` (the reason), `detail`,
`notes`. The panel server: `GET /api/task/options?project=…&project_key=…` — task types and epics (from the cache;
`fresh=1` — anew) and the default assignee (`assignee`: the git e-mail and the name found), `GET /api/task/doc?project=…&path=…`
(or `&kind=…` for a future document) — the tracker, the kind's and the settings' defaults, the header's `task:` block,
`task_key`, `task_url` and the file snapshot, `POST /api/task/doc` with `{project, path, task, expect}` — write the `task:`
block with the same write as the "Files" editor (the snapshot is required, a mismatch is 409), `POST /api/task/create` with
`{project, path, task: {…}}` — the same as `ship:task --apply`.

**In the panel.** The **Task tracker** frame of the kinds form holds the kind's task defaults: project key, type (a list
from the tracker, cached for an hour, "Refresh list"), assignee, labels, components; an empty field shows the default as a
hint. A kind has no epic — it is chosen at the document; an old `epic` of the kind stays in the config (the write keeps the
unknown) and has no effect. The **Task** block of a document — in "Files" (a document of a kind) and in "Production" (the
document of the publish dialog and the create window): the same fields plus the epic from the list of the project's epics,
"Save" (the `task:` block into the header), "Create a task in <tracker>" or the link "Open PRJA-123" if the task exists. In
the create window the values go to the engine (`agent:make --task-field FIELD=VALUE`) and into the new document's header.

**After publishing.** A document with `task_key` in the header gets a link to its page in the task after a successful
publish — by the active tracker, if it can `link_page` (a repeat makes no second link). No tracker or it cannot — one line
in the output, the publish is not affected; the result is the `task_link` field of the `AURORA-PUBLISH` line.

### How to write your own connector

1. A folder `connectors/<id>/`: `connector.json` with the `operations` field (role, script, list of operations) and the
   operations script. The connector does not have to be able to mirror, but the manifest is shared with source
   modules: `kind`, `mirror`, `run` are in it.
2. The script is anything `python3` runs; the simplest is Python with the engine's frame:

```python
import sys
from sources_core import OpFailure, serve_ops

def issue_types(params, project):
    key = params.get("project_key") or ""
    if not key:
        raise OpFailure("bad_request", "project_key is not set")
    return {"types": [{"id": "1", "name": "Task", "description": ""}]}

if __name__ == "__main__":
    sys.exit(serve_ops({"issue_types": issue_types}))
```

   The core puts the folder of its scripts into `PYTHONPATH`, so `sources_core` and `aurora_common` import both from
   `.aurora/scripts/` and from the connector folder in the kit. The script reads access keys itself
   (`sources_core.read_secret(prefix, project)`) and gives them out neither in the answer nor into the cache.
3. `aurora.py update <project> --apply` lays out the manifest and the script; the role connector is chosen in "Project
   settings" (or in the `connectors:` block). A check without the panel —
   `python3 .aurora/scripts/connector_ops.py --role tasks --op issue_types --params '{"project_key": "PRJA"}'`.

A stand-in "other tracker" with the same contract is checked in `tests/cases/part_170.py`: a test connector in a
temporary project creates a task through `ship:task`, and the core is not edited. A stand-in "other wiki" is in
`tests/cases/part_178.py`: its own `wiki` connector publishes a document through `ship:publish` (create, a repeat without
changes, an edit, someone else's edit — a conflict).
