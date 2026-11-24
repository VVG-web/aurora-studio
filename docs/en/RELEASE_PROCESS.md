# Fast release loop

This document records the process that grew out of release 1.231.19: a full CI
matrix on every push, lint after the full suite, and platform rules kept in
people's heads cost several extra hours. The rules below are checked
mechanically.

## 1. Cheap checks first

`engine-tests` has a `preflight` job:

- `tests/check_release.py`;
- `scripts/quality_eval.py --gate`;
- `compileall`;
- `ruff`;
- `scripts/check_portability.py --changed <base>`.

Heavy jobs (`tests`, `floor`, `newer-python`, `windows`, `windows-314`,
`macos`, `adapter-live`, `legacy-310`) wait for `preflight`. A red preflight
does not spend forty minutes on the full suite.

## 2. One full run per release

The Ubuntu `tests` job is split into three shards, like `windows`; `floor`,
`newer-python`, and `macos` are also split into three shards. Each shard runs on
its own machine; invariants stay in shard one.

A new push to the same branch cancels the previous run (`concurrency`). The old
run is no longer about the current commit.

A green `engine-tests` on a `release/*` branch fast-forwards `master` to the
verified commit and creates the tag. The second full run on `master` is no
longer needed. If `master` is not an ancestor of the verified commit, the
release stops.

## 3. Release freeze

- Features land in `integration` before `release/*` is opened.
- Only fixes found by the gate go into `release/*`.
- A new feature after the branch is opened waits for the next release.
- A bigger release does not replace a short loop: batch features before the
  freeze, do not collect everything and test once.

## 4. Parallel lanes

- 4-5 independent lanes per release, each owning its own files.
- An integrator merges the lanes and runs the full matrix once.
- A dedicated platform agent owns Windows and Ubuntu failures so feature
  authors do not wait in its queue.
- A lane is not done until its tests pass on its branch.

## 5. Platform rules

`scripts/check_portability.py` checks changed lines for:

- `os.path.relpath(path, start)`: `start` must be `os.path.realpath(...)`,
  `os.path.abspath(...)`, or `.resolve()`. Otherwise Windows turns a relative
  `start` and an absolute `path` into `../../runneradmin/...`.
- `write_text_atomic(plan..., ...)` must pass `newline=""`: text mode turns LF
  into CRLF while the plan hash is computed from LF.
- Tests must not use `abs(..._ms...) <= N` with `N <= 1000`: wall-clock on a
  slow runner adds a second. Inject the clock and assert exact values.
- Paths in reports and JSON always use `/`, regardless of the OS.
- Compare paths through `Path` or `os.path.normpath`, not strings with
  `os.sep`.
- A test that invokes pandoc must also pass on a distro pandoc built without
  `embed_data_files`: sandbox mode cannot see its own `data/docx`.

## 6. When CI is red

1. Look at the first failure, do not rerun the matrix.
2. Rerun only the failed job when the cause is external (network, runner).
3. Make a new commit only after reproducing locally.
4. If the failure is platform-specific, add a checker rule or a test that fails
   without the fix.
5. Do not change a test just to get green: change the code, or add a platform
   rule with an explanation.

## Checklist before `release/*`

- [ ] `preflight` is green.
- [ ] The full matrix is green on the integration branch.
- [ ] `VERSION`, `CHANGELOG.md`, `UI_VERSION`, and `module.json` agree.
- [ ] `check_portability.py --changed origin/master` is clean.
- [ ] New platform tests fail without the fix.
- [ ] `release/*` contains fixes only, no new features.
