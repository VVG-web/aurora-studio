# Contributing

[Wiki home](README.md) · full guide: [CONTRIBUTING](../docs/en/CONTRIBUTING.md) · architecture:
[architecture](../docs/en/architecture.md)

## The loop

```bash
python3 tests/run_tests.py                 # the whole suite (a few minutes)
python3 tests/run_tests.py --smoke         # only the fast source-scan invariants
python3 tests/run_tests.py --only <text>   # tests whose name contains <text>
python3 tests/run_tests.py --shard=1/3     # a third of the suite (CI runs Windows this way)
ruff check --select F401,F841,B023 scripts cockpit reports aurora.py
```

Every defect found gets **a test that is red without the fix**, the fix, and its own release. The suite is the executable
description of the engine: read a test's docstring to learn why a rule exists.

## Adding things

| To add | Do |
|---|---|
| a **command** | a script in `scripts/` (with a `Панель: \`ns:cmd\`` line in its docstring), a row in `commands.txt`, a line in `engine_manifest.txt`, a row in the `aurora-vault` skill, regenerate the references, test, CHANGELOG |
| a **source module** | a folder `connectors/<id>/` with `connector.json`, `SKILL.md` and a script — [source modules](../docs/en/connectors.md) |
| a **panel section** | a folder `cockpit/modules/<id>/` — manifest, markup, script, strings with the section's prefix |
| a **skin** | a file `cockpit/skins/<name>.css` — tokens only, both theme states |
| an **interface language** | `python3 scripts/kit_i18n.py --new <code> "<name>"`, then fill `cockpit/i18n/data/<code>.json` and run `--check` |
| a **wiki page** | a file in `wiki/` and its twin in `wiki/ru/`, linked from `README.md` — the tests check links and parity |

## A release

1. Bump `VERSION` (and `UI_VERSION` in `cockpit/ui/panel.js` — a test compares them) and add a
   `## X.Y.Z — summary` entry to `CHANGELOG.md` (Russian: what changed **for a human**, defects found after
   implementation named separately; a breaking schema change is marked **BREAKING** with the migration step).
2. Start the commit message with the version: `vX.Y.Z: summary`. `python3 tests/check_release.py`, the suite and
   `kit_i18n.py --check` must be green.
3. Merge into `master` once CI is green. **The release is created by CI:** the
   [`release.yml`](../.github/workflows/release.yml) workflow waits for the tests, reads `VERSION`, and — if no Release of
   that version exists — tags the commit that raised it and publishes the GitHub Release with the text from `CHANGELOG.md`.
   No entry in the changelog, no release (the workflow fails with a clear error).
4. Releases come one at a time: wait for a release to appear before merging the next one.

Authorship in commits, pull requests and releases is a person's — no tool is named as an author or co-author.

## Principles to respect

- **Script first, model second.** A mass operation is a deterministic script with a dry-run; the model only judges.
- **Never rewrite what the engine did not write.** Bodies of dictionaries and documents, history footers, previous theses.
- **A mirror is deterministic.** The same source → the same bytes.
- **The folder layout is fixed** and the same everywhere; the engine manifest is a whitelist.
- **Read-only means read-only:** the MCP server, the panel's preview and observation commands do not write.
