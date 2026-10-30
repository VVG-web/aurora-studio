# Project code: the `Code/` folder and the "Code" section

Русская версия: [../code-room.md](../code-room.md).

Aurora can keep **clones of the project's code repositories** next to the knowledge base. Code is
neither knowledge nor an analyst's document: it is evidence of *how the system is built*. From it the
engine learns a repository's revision, languages and size; the next releases add a code index,
checking a story's acceptance criteria against the code and the "Clean room" — artifacts built from
the code to compare with the analysis.

This release lays the ground: the `Code/` folder, the repository registry, the "Code" panel section
and the `code:sync` and `code:status` commands. No model is called anywhere in it.

## Folders

| Folder | What it holds | Who writes | In the project's git |
|---|---|---|---|
| `Code/<name>/` | the clone of one code repository, read-only | `code:sync` | **no** — the `Code/*` rule in `.gitignore`; only `Code/README.md` and `Code/.gitkeep` are in git |
| `CleanRoom/` | artifacts built from the code, laid out like `Artifacts/` (document-kind folders arrive with the "Clean room") | the engine | yes |
| `CleanRoom/Backlog/` | a table of contents of the code artifacts in backlog form; the engine fills it once the "Clean room" exists | the engine | yes |
| `Artifacts/Backlog/` | the backlog a human keeps: one file | a human | yes |

Code is kept out of the project's git on purpose: it is large, it has its own history, secrets and
licence, and two sources of truth would drift apart. The registry gives reproducibility: the
address, the branch and the pinned revision are written in `aurora.config.yaml`, and every member
gets the same clones with their own `code:sync`. A clone that did get into the project's git index
(before the rule or through `git add -f`) is a `kit:doctor` error with the fix
`git rm -r --cached Code/<name>`. A folder in `Code/` with no registry entry and a registry entry
with no clone are `kit:doctor` warnings.

"Files" does not unfold clones (one clone is tens of thousands of files): the folder guide is shown,
the repositories themselves are in the "Code" section. Writing into `Code/` from the panel is closed:
the next sync would wipe the edit; code is edited in its own repository.

### The human backlog: `Artifacts/Backlog/`

One Markdown file; the template is `Templates/backlog_template.md`:

```markdown
# Структура

## Activity 1 Order intake

### Epic 1.1 Order registration

* US-1.1.1. Accept an order from a supplier [ALG-010, CON-001] ([PRJA-101](https://jira.example.com/browse/PRJA-101))
```

`## Activity <code> <title>` is an activity, `### Epic <n.m> <title>` an epic, a line
`* US-<n.m.k>. <title> [artifact codes, comma-separated] ([KEY-N](link to the issue))` a story;
artifact codes and the issue are optional. The engine will compare this file with the code table of
contents (`CleanRoom/Backlog/`): what the code lacks and what the code has beyond the backlog.
Parsing the file comes in the next releases; the form is fixed now so that the backlog can be
started.

## The repository registry: `code_repos:`

The block in `aurora.config.yaml` is edited **only by the form** of the "Code" section: the form
sends the snapshot of the registry it read, and if the block was changed elsewhere (another tab,
`git pull`, a hand edit) the write does not go through (409), and the form offers "Take from disk"
or "Write mine over it". Only this block is rewritten: the byte-order mark, line endings, comments
and neighbouring sections of the config stay byte for byte. The project settings form does not know
the block and carries it over whole.

```yaml
code_repos:
  order-intake:
    url: "https://git.example.com/team/order-intake.git"
    provider: "generic"
    branch: "main"
    pin: ""
    include: ""
    exclude: "**/generated/**, *.min.js"
    access: "vpn:VPN-1"
    models: false
    publish: false
```

| Field | Meaning |
|---|---|
| entry name | the clone folder `Code/<name>`: Latin letters, digits, `.`, `_`, `-`, starting with a letter or digit, up to 64 characters; Windows names (`con`, `aux`, `nul`…) are refused; unique regardless of case |
| `url` | the clone address **without login and password** (a password in the address is refused with advice; a login is dropped silently): `https://…`, `git@…:…`, `ssh://…`, a path or `file://` |
| `provider` | `github`, `gitlab`, `bitbucket`, `gitea` or `generic` — as in "Project Git"; guessed from the address |
| `branch` | which branch to follow; empty — the server's default branch |
| `pin` | the pinned revision: a SHA (7–40 characters) or a tag; empty — the clone follows the branch |
| `include`, `exclude` | path masks, comma-separated: what to count as code beyond the defaults and what not to (`**` — any folders, a mask without `/` matches the name at any depth) |
| `access` | how the repository is reached: empty (as always), `internet`, `local` or `vpn:<profile>` — for the connection check |
| `models` | whether this repository's code may be given to models; **no by default** |
| `publish` | whether artifacts about this code may be published; **no by default** |

### Sign-in

As in the "Git" section: the `git clone` line from the repository page is pasted whole, the engine
reads the provider, address and branch; the sign-in is as git is set up on the machine, a token, a
login and password or an SSH key. The secret is kept **outside the project**, in
`~/.aurora/git/credentials.json` (mode 600) under the full path of the clone `Code/<name>`; only a
mask goes out. The token is written neither into the config, nor into the address, nor onto the git
command line: git takes it from the environment through a credential helper. If the server in the
address changes, the saved token does not move to the new server — it is entered again. A
repository removed from the registry has its sign-in erased.

**bitbucket.org:** the login is the user name (not the e-mail; it is in the clone line), and an API
token allowed to read repositories goes instead of the password.

### Code and models

The owner decided: code fragments go to models **only with the permission of each repository**
(the "Code may be given to models" switch, off by default). Without it, the clone's files are seen
neither by the assistants' reading tools ("Ask", bots, production) nor by task attachments
(`@Code/…`), and the `Code/` folder never shows up in the request field's suggestions. The folder
guide `Code/README.md` is not a clone.

## Commands

### `code:sync`

Clones new repositories of the registry and updates the cloned ones: takes what is new from the
server (`fetch`) and sets the working tree to the pinned revision or the server branch tip **without
a branch** (`checkout --detach`). Nothing is merged or pushed. Clones get `core.autocrlf=false`
(bytes as in the repository) and `core.longpaths=true` (Windows); large LFS files are not pulled.

- **Without `--apply` — a plan**: what will be cloned and where it will stand; the command goes to
  no network and writes nothing.
- A history rewritten on the server is not an error: the clone simply moves to the new target.
- **A clone with edits** (changed or new files) and **a clone of another address** are left alone,
  with the reason named. "Reset the clone" (`code:sync --reset --repo <name> --apply`) returns the
  clone to the registry: the address from the registry, edits and extra files of the clone gone. For
  one repository only.
- **Connection check**: a repository whose VPN profile (or the internet) is down now is skipped with
  the exact reason and the machine line `AURORA-NET`, the others are synced; exit code 2 tells the
  route and Cron that the step is not broken but waits for the connection. Before a launch from the
  panel the command's needs add up over all repositories (the computed source "code").
- State is kept in `.aurora/state/code/sync.json` (outside git): when, what was done, the revision,
  languages and size.
- An empty registry is one line and exit code 0: the step in the "Update the base" route (after
  `sync:web`) does nothing in a project without code.

Exit codes: 0 — everything synced, 1 — some repositories were not synced (each with its reason; the
route goes on), 2 — an execution failure or a refusal of the connection check.

### `code:status`

No network: for each repository — the clone's state, the revision and its date, the lag behind the
server branch as of the last update (for a pinned one — how far the branch has moved ahead), the
last sync, the permissions and **languages and size**. The repositories' stack is not known in
advance — the engine finds it out itself: a language by extension and file name, lines by line
breaks, no external tools. Not counted as code and named separately: foreign dependencies
(`node_modules/`, `vendor/`, `build/`, `dist/`, `target/`…), generated code (`_pb2.py`, `.pb.go`, a
"Code generated … DO NOT EDIT" header), minified files, lock files, binaries, files with secret
names (`.env*`, `*.pem`…) and files above 512 KB. The clone's longest path that would not fit on
Windows without long paths is flagged.

## The "Code" section

In the project group, next to "Git". The "Connect a repository" card takes a `git clone` line, the
folder name, the sign-in and two switches ("To models", "Publish"); "Check and connect" first asks
the server (`ls-remote` with the same sign-in), then writes the registry. Each repository shows the
clone's state, the revision, the lag, the last sync, languages and size; "Sync", "Reset the clone"
(with edits or a foreign address), "Settings" (address, provider, branch, revision, masks,
connection, permissions, sign-in, "Check the connection") and "Remove from the list" — the clone
stays on disk: the engine does not take it upon itself to delete data. The sync runs as a panel job
and is seen in the Console. At night — the step of the "Update the base" route or the
`code:sync --apply` command in the "Cron" section. Masks need not be typed: when the clone exists, "Choose…" next to
the field opens the clone's folders, and the ticked folders are appended as `folder/**` masks; an SSH key is chosen in a
window from `~/.ssh`.

## What comes next

The code index and search (symbols, REST routes, tables, topics, configuration, commits), a story's
requirements pack, checking acceptance criteria against the code and the "Clean room" are the next
releases.
