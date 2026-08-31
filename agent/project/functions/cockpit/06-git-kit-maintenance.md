# Git & Kit Maintenance Function

## Description

Handles the project's version-control needs and the upkeep of the Aurora kit itself, all from the panel:
showing uncommitted/untracked state and remote divergence, creating commits (with a skip-only-ratchet
escape), pushing to a remote, updating the kit from its repository (`--ff-only`), and presenting an
«О проекте» about screen with release facts.

## Key Features

- **Project git state** (`git_state()`): no-git projects are reported as such; otherwise returns the
  branch, the list of dirty files, whether a remote exists, how many commits ahead (`rev-list --count @{u}..HEAD`),
  and the pre-commit hook's ratchet mode (`hook_mode`).
- **Per-file git state** (`git_file_state()`): `changed/new/committed/outside git` via
  `git status --porcelain --ignored=matching`, so `.gitignore`-ed files are never mislabeled
  as committed.
- **Commit** (`git_commit()`): refuses empty messages, `git add` (given files or `-A`), commits
  with `-m`, falling back to `--no-verify` only in the sense that `skip_ratchet` sets
  `AURORA_SKIP_RATCHET=1` (commit-msg hooks are never bypassed).
- **Push** (`git_push()`): selects the named remote or the first configured one, shows the full failure
  tail when push fails.
- **Kit maintenance** (`kit_git_status()`, `kit_pull()`): `fetch` then reports ahead/behind/dirty
  and incoming log lines; `kit_pull` refuses a dirty tree and updates only via fast-forward, clearing the
  registry cache afterwards. Triggered from the UI's `/api/kit/update` and `/api/kit/status`.
- **About** (`about()`): kit/UI versions, repo URL, branch, short commit, commit date, author,
  license (from a `LICENSE` file), command count and the latest release headings from `CHANGELOG.md`.
- **Kit version helpers**: `kit_version()` (from the kit's `VERSION` file), `ui_version()`
  (from `const UI_VERSION` in the HTML), `minor()` (major.minor comparison) and `version_gap()`
  which blocks routes across engine-versions.

## Related Documentation

### Technical Details
- [Cockpit Architecture Design](../../design/01-cockpit-architecture.md) - server ownership of privileged git/kit actions
### Source Files
- `cockpit/aurora_cockpit.py` - `git_out`, `git_state`, `git_file_state`, `git_commit`, `git_push`, `hook_mode`, `kit_git_status`, `kit_pull`, `about`, `kit_version`, `ui_version`, `version_gap`, `git_branch`, `git_dirty_count`
- `cockpit/ui/index.html` - `view-git` bar in files pane, `view-version`, `view-about`

### Related Functions
- [File Editor](./05-file-editor.md) - open files carry their git state pills and commit/push actions
- [Command Runner & Console](./04-command-runner-console.md) - commits/pushes surface in the run log

## Implementation Notes

Every git invocation goes through `git_out()` / `subprocess.run` with a list of args — never a
shell string. `--ignored` is mandatory in `git_file_state`; lacking it, files under `.gitignore`
were silently reported as committed. All kit operations run against `KIT` (`..` from `cockpit/`), the
recognised repo root, which is what lets the panel also be started from within a project to develop the
engine (`kit_is_source()`).

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, cockpit*