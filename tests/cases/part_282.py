"""0003 2A: public Git writers never erase a staged version on refusal."""
from __future__ import annotations

import io
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import patch

from cases.part_281 import JOURNAL, _block, _commit, _git, _init, _nested
from harness import KIT, make_project, run, run_node, test

WRITERS = ("panel", "checkpoint", "result")
CARD = "AuroraKnowledgeDB/Concepts/a.md"


def _public(name, repo):
    import agent_runner as ar
    import git_sync as gs

    if name == "panel":
        return gs.commit(str(repo), "Keep human work")
    if name == "checkpoint":
        return ar.checkpoint(str(repo), "agent:distill", True)
    return ar.commit_result(str(repo), "agent:distill", "Agent work", True)


def _state(repo):
    """Index bytes, stage modes/OIDs, flags, cached changes, HEAD, refs, reflog, worktree."""
    index = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip())
    return (
        index.read_bytes() if index.exists() else None,
        _git(repo, "ls-files", "--stage", "--debug", "-z").stdout,
        _git(repo, "ls-files", "-v", "-z").stdout,
        _git(repo, "diff", "--cached", "--binary").stdout,
        _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
        _git(repo, "reflog", "show", "--all", "--format=%gD %H %gs").stdout,
        tuple((p, (repo / p).read_bytes() if (repo / p).is_file() else None)
              for p in (CARD, JOURNAL, ".gitignore", "human.md", "intent.md")),
    )


def _paths(repo):
    return _git(repo, "show", "--format=", "--name-only", "HEAD").stdout.splitlines()


def _project(repo: Path, *, rules: bool = True) -> Path:
    _init(repo)
    if rules:
        _block(repo)
    card = repo / CARD
    card.parent.mkdir(parents=True)
    card.write_text("old card\n", encoding="utf-8")
    for rel in ("human.md", "skip.md", "assume.md"):
        (repo / rel).write_text("original\n", encoding="utf-8")
    _commit(repo, "initial")
    return repo


def _flags(repo):
    _git(repo, "update-index", "--skip-worktree", "skip.md")
    _git(repo, "update-index", "--assume-unchanged", "assume.md")
    (repo / "intent.md").write_text("intent only\n", encoding="utf-8")
    _git(repo, "add", "-N", "intent.md")


def _barrier_call(fn, ready, resume):
    def at_barrier(*args, **kwargs):
        ready.set()
        assert resume.wait(20), "waited too long for competing git add"
        return fn(*args, **kwargs)

    return at_barrier


@test
def test_2a_partially_staged_blob_is_never_replaced_by_the_worktree(tmp: Path):
    for who in WRITERS:
        repo = _project(tmp / who, rules=False)
        _flags(repo)
        (repo / CARD).write_text("only staged version\n", encoding="utf-8")
        _git(repo, "add", "--", CARD)
        (repo / CARD).write_text("unrelated worktree version\n", encoding="utf-8")
        before = _state(repo)
        got = _public(who, repo)
        assert _state(repo) == before, (who, "mutated despite incompatible stage", got)
        assert got["ok"] is False, (who, got)
        assert _git(repo, "show", f":{CARD}").stdout == "only staged version\n"


@test
def test_2a_registry_exception_never_publishes_a_candidate_index(tmp: Path):
    import local_paths as lp

    for who in WRITERS:
        for kind, failure in (("timeout", subprocess.TimeoutExpired("git ls-files", 60)),
                              ("launch", OSError("git failed to start"))):
            repo = _project(tmp / f"{who}-{kind}")
            _flags(repo)
            (repo / CARD).write_text("new card\n", encoding="utf-8")
            before = _state(repo)
            with patch.object(lp, "enforce_index", side_effect=failure) as failed:
                got = _public(who, repo)
            assert failed.called, (who, "never tested the failure after add", got)
            assert got["ok"] is False, (who, got)
            assert _state(repo) == before, (who, kind, got)


@test
def test_2a_completed_foreign_stage_wins_before_git_index_lock(tmp: Path):
    """A real git add completes while each public writer is between snapshot and lock."""
    import local_paths as lp

    for who in WRITERS:
        for topology in ("regular", "unborn", "linked"):
            case = tmp / f"{who}-{topology}"
            case.mkdir()
            main = None
            if topology == "unborn":
                repo = _init(case / "repo")
                (repo / CARD).parent.mkdir(parents=True)
                assert not (repo / ".git/index").exists()
            elif topology == "linked":
                main = _project(case / "main", rules=False)
                repo = case / "repo"
                _git(main, "worktree", "add", "--detach", str(repo), "HEAD")
                assert (repo / ".git").is_file()
                main_before = _state(main)
            else:
                repo = _project(case / "repo", rules=False)
            (repo / CARD).write_text("new card\n", encoding="utf-8")
            (repo / "human.md").write_text("foreign staged bytes\n", encoding="utf-8")
            ready, resume = Event(), Event()
            at_lock = _barrier_call(lp._take_index_lock, ready, resume)

            with ThreadPoolExecutor(max_workers=1) as pool, patch.object(lp, "_take_index_lock", at_lock):
                operation = pool.submit(_public, who, repo)
                try:
                    assert ready.wait(20), "did not reach the snapshot/lock barrier"
                    _git(repo, "add", "--", "human.md")
                    (repo / "human.md").write_text("different worktree bytes\n", encoding="utf-8")
                    after_add = _state(repo)
                finally:
                    resume.set()
                got = operation.result(timeout=30)
            assert got["ok"] is False and "параллельно" in str(got), (who, topology, got)
            assert _state(repo) == after_add, (who, topology, "index, refs, reflog or worktree changed")
            assert _git(repo, "show", ":human.md").stdout == "foreign staged bytes\n"
            if main:
                assert _state(main) == main_before, (who, "linked writer touched main checkout")


@test
def test_2a_real_git_add_cannot_enter_while_private_stage_is_prepared(tmp: Path):
    """The normal path proceeds; only the Aurora lock owner can publish the index."""
    import local_paths as lp

    for who in WRITERS:
        repo = _project(tmp / who)
        (repo / CARD).write_text("normal card\n", encoding="utf-8")
        before = _state(repo)
        ready, resume = Event(), Event()
        after_add = _barrier_call(lp.enforce_index, ready, resume)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(lp, "enforce_index", after_add):
            operation = pool.submit(_public, who, repo)
            try:
                assert ready.wait(20), "did not reach the post-add barrier"
                other = subprocess.run(["git", "-C", str(repo), "add", "--", "human.md"],
                                       capture_output=True, timeout=10)
                assert other.returncode != 0 and b"index.lock" in other.stderr, other
                assert _state(repo) == before, (who, "private staging changed visible state")
            finally:
                resume.set()
            got = operation.result(timeout=30)
        assert got["ok"] is True and not got.get("index_warning"), (who, got)
        assert _paths(repo) == [CARD], (who, _paths(repo))
        assert _git(repo, "show", f"HEAD:{CARD}").stdout == "normal card\n"
        assert _git(repo, "diff", "--cached", "--name-only").stdout == ""
        assert not (repo / ".git/index.lock").exists()


@test
def test_2a_failed_flag_read_refuses_without_reconstructing_the_index(tmp: Path):
    import local_paths as lp

    real_git = lp._git

    def unreadable(root, *args, **kwargs):
        if args[:2] == ("ls-files", "-v"):
            return subprocess.CompletedProcess(args, 128, b"", b"flag read failed")
        return real_git(root, *args, **kwargs)

    for who in WRITERS:
        repo = _project(tmp / who, rules=False)
        _flags(repo)
        (repo / CARD).write_text("new card\n", encoding="utf-8")
        before = _state(repo)
        with patch.object(lp, "_git", unreadable):
            got = _public(who, repo)
        assert got["ok"] is False and "flag read failed" in str(got), (who, got)
        assert "flags_restored" not in got and "saved_tree" not in got, got
        assert _state(repo) == before, (who, "failed flag snapshot mutated state")


@test
def test_2a_agent_commits_only_the_db_and_preserves_foreign_partial_stage(tmp: Path):
    repo = _project(tmp / "repo")
    _flags(repo)
    human = repo / "human.md"
    human.write_text("human stage\n", encoding="utf-8")
    _git(repo, "add", "--", "human.md")
    human.write_text("human worktree\n", encoding="utf-8")
    outside = ("--", "human.md", "skip.md", "assume.md", "intent.md")
    before = _git(repo, "ls-files", "-v", "--stage", "--debug", "-z", *outside).stdout
    (repo / CARD).write_text("agent card\n", encoding="utf-8")
    got = _public("result", repo)
    assert got["ok"] is True and not got.get("index_warning"), got
    assert _paths(repo) == [CARD]
    assert _git(repo, "show", ":human.md").stdout == "human stage\n"
    assert human.read_text(encoding="utf-8") == "human worktree\n"
    assert _git(repo, "ls-files", "-v", "--stage", "--debug", "-z", *outside).stdout == before


@test
def test_2a_initial_commit_without_an_index_and_without_a_competitor(tmp: Path):
    for who in WRITERS:
        repo = _init(tmp / who)
        _block(repo)
        card = repo / CARD
        card.parent.mkdir(parents=True)
        card.write_text("first card\n", encoding="utf-8")
        (repo / "human.md").write_text("first note\n", encoding="utf-8")
        assert not (repo / ".git/index").exists()
        assert _git(repo, "for-each-ref").stdout == ""
        got = _public(who, repo)
        assert got["ok"] is True and not got.get("index_warning"), (who, got)
        assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
        expected = {CARD} if who == "result" else {".gitignore", CARD, "human.md"}
        assert set(_paths(repo)) == expected, (who, _paths(repo))
        assert _git(repo, "diff", "--cached", "--name-only").stdout == ""
        assert not (repo / ".git/index.lock").exists()


@test
def test_2a_staged_deletion_with_a_recreated_file_refuses_before_rule_updates(tmp: Path):
    for who in WRITERS:
        repo = _project(tmp / who, rules=False)
        _git(repo, "rm", "--cached", "--", CARD)
        (repo / CARD).write_text("new worktree bytes over staged deletion\n", encoding="utf-8")
        before = _state(repo)
        got = _public(who, repo)
        assert got["ok"] is False and CARD in str(got), (who, got)
        assert _state(repo) == before, (who, "changed staged deletion, ref or worktree")


@test
def test_2a_unusable_rules_stop_every_writer_before_add(tmp: Path):
    import local_paths as lp

    outside = tmp / "outside-ignore"
    outside.write_text(lp.gitignore_block(), encoding="utf-8")
    failures = []
    for who in WRITERS:
        for variant in ("symlink", "write-error", "staged-incomplete"):
            repo = _project(tmp / f"{who}-{variant}", rules=False)
            rules = repo / ".gitignore"
            if variant == "symlink":
                rules.symlink_to(outside)
            if variant == "staged-incomplete":
                rules.write_text(lp.gitignore_block().replace(".aurora/\n", ""), encoding="utf-8")
                _git(repo, "add", "--", ".gitignore")
            (repo / CARD).write_text("a new public note\n", encoding="utf-8")
            private = repo / "AuroraKnowledgeDB/.aurora/fixture.txt"
            private.parent.mkdir()
            private.write_text("synthetic local state\n", encoding="utf-8")
            before = _state(repo)
            write_error = OSError("fixture: rules are not writable") if variant == "write-error" else None
            with (patch.object(lp, "_write_rules", wraps=lp._write_rules, side_effect=write_error),
                  patch.object(lp, "run_index_step", wraps=lp.run_index_step) as step):
                got = _public(who, repo)
            added = any(call.args[1][0] == "add" for call in step.call_args_list)
            if got["ok"] or added or _state(repo) != before or ".gitignore" not in str(got):
                failures.append((who, variant, added, got))
    assert outside.read_text(encoding="utf-8") == lp.gitignore_block()
    assert not failures, failures


@test
def test_2a_local_blob_without_a_worktree_copy_survives_writers_and_update(tmp: Path):
    import aurora_update as au

    failures = []
    for who in (*WRITERS, "update"):
        repo = _project(tmp / who)
        journal = repo / JOURNAL
        journal.parent.mkdir()
        journal.write_text("old HEAD\n", encoding="utf-8")
        _commit(repo, "track journal")
        journal.write_text("unique staged bytes\n", encoding="utf-8")
        _git(repo, "add", "-f", "--", JOURNAL)
        journal.unlink()
        (repo / CARD).write_text("new public note\n", encoding="utf-8")
        before = _state(repo)
        got = au.clean_local_state(repo) if who == "update" else _public(who, repo)
        accepted = got["checked"] if who == "update" else got["ok"]
        preserved = _state(repo) == before and _git(repo, "show", f":{JOURNAL}").stdout == "unique staged bytes\n"
        if accepted or not preserved or JOURNAL not in str(got):
            failures.append((who, preserved, got))
    assert not failures, failures


@test
def test_2a_git_commit_is_not_reported_as_refused_if_index_publication_fails(tmp: Path):
    import git_sync as gs
    import local_paths as lp

    for who in WRITERS:
        repo = _project(tmp / who)
        (repo / CARD).write_text("commit succeeds\n", encoding="utf-8")
        original = (repo / ".git/index").read_bytes()
        with patch.object(lp.IndexTransaction, "_publish", side_effect=OSError("index filesystem failure")) as failure:
            got = _public(who, repo)
        assert failure.call_count == 1, (who, got)
        assert got["ok"] is True and "index filesystem failure" in got["index_warning"], (who, got)
        if who == "panel":
            last = gs.read_last(repo)["commit"]
            assert last["index_warning"] == got["index_warning"] and last["commit"] == got["commit"], last
        assert (repo / ".git/index").read_bytes() == original
        assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2"
        assert _paths(repo) == [CARD], (who, _paths(repo))
        assert not (repo / ".git/index.lock").exists()


@test
def test_2a_git_cli_push_and_update_fail_when_only_the_local_commit_succeeded(tmp: Path):
    import git_sync as gs
    import local_paths as lp

    outcomes = []
    for action, flags in (("push", []), ("update", ["--commit-first"])):
        repo = _project(tmp / action)
        _git(repo, "remote", "add", "origin", str(tmp / "unused.git"))
        (repo / CARD).write_text("new card\n", encoding="utf-8")
        original = (repo / ".git/index").read_bytes()
        saved = {f"auto_{action}": {"enabled": True, "on": ["interval"]}}
        out = io.StringIO()
        with (patch.object(lp.IndexTransaction, "_publish", side_effect=OSError("index filesystem failure")),
              patch.object(gs, "load_saved", return_value=saved),
              patch.object(gs, "_fetch", return_value=(1, "unexpected fixture transport")) as fetch,
              patch.object(gs, "_push_one", return_value={"ok": False}) as push,
              redirect_stdout(out)):
            rc = gs.main([f"--{action}", *flags, "--project", str(repo), "--auto", "interval", "--json"])
        output = out.getvalue().splitlines()
        got = json.loads(output[-1])
        last = gs.read_last(repo)[action]
        state = gs.read_state(repo)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert _paths(repo) == [CARD] and (repo / ".git/index").read_bytes() == original
        assert not (fetch.called or push.called), (action, "external operation started")
        if not (rc == 1 and len(output) == 1 and got["ok"] is False and last["ok"] is False
                and got.get("commit_sha") == last.get("commit_sha") == head
                and got.get("outcome") == last.get("outcome") == "inspect_index"
                and "index filesystem failure" in got["index_warning"]
                and state.get(f"auto_{action}_fail_at", 0) > 0):
            outcomes.append((action, rc, got, last, state))
    assert not outcomes, outcomes


@test
def test_2a_git_cli_prints_an_unconfirmed_commit_without_a_problem_dict(tmp: Path):
    import git_sync as gs
    import local_paths as lp

    repo = _project(tmp / "repo")
    _git(repo, "remote", "add", "origin", str(tmp / "unused.git"))
    (repo / CARD).write_text("new card\n", encoding="utf-8")
    before = lp._head_position(str(repo))
    real_position = lp._head_position

    def unconfirmed(root):
        current = real_position(root)
        if current != before:
            raise OSError("fixture: cannot read new HEAD")
        return current

    out = io.StringIO()
    with (patch.object(lp, "_head_position", side_effect=unconfirmed),
          patch.object(gs, "_push_one") as push, redirect_stdout(out)):
        rc = gs.main(["--push", "--project", str(repo)])
    assert rc == 1 and not push.called
    assert "cannot read new HEAD" in out.getvalue() and "отправка не выполнена" in out.getvalue(), out.getvalue()
    assert gs.read_last(repo)["push"]["ok"] is False


@test
def test_2a_panel_formats_an_index_warning_instead_of_success(tmp: Path):
    module = KIT / "cockpit/modules/gitsync"
    view = (module / "view.js").read_text(encoding="utf-8")
    i18n = {lang: json.loads((module / f"i18n/{lang}.json").read_text(encoding="utf-8")) for lang in ("en", "ru")}
    for catalog in i18n.values():
        assert "{commit}" in catalog["gitsync.warn.index_not_published"]
        assert "HEAD" in catalog["gitsync.warn.index_outcome_unknown"]
    assert 'e.index_warning) ctx.toast(entryText(ctx, e), "warn")' in view
    cp = run_node(tmp, 'import assert from "node:assert/strict";\n' + view + "\n" + '''
const ctx = {t: (key, values = {}) => [key, values]};
assert.deepEqual(entryText(ctx, {action: "commit", ok: true, index_warning: "disk", commit: "abc"}),
  ["gitsync.warn.index_not_published", {commit: "abc"}]);
assert.deepEqual(entryText(ctx, {action: "commit", ok: false, index_warning: "unknown"}),
  ["gitsync.warn.index_outcome_unknown", {}]);
''')
    if cp is not None:
        assert cp.returncode == 0, cp.stderr


@test
def test_rc2_git_fix_refuses_the_parent_before_lock_or_journal(tmp: Path):
    import git_sync as gs

    parent, demo = _nested(tmp, "parent", parent_card=True)
    _git(parent, "branch", "wanted")
    on_current = parent / "current-branch.txt"
    on_current.write_text("dummy branch state\n", encoding="utf-8")
    _git(parent, "add", "--", on_current.name)
    _git(parent, "commit", "-qm", "current branch only", "--", on_current.name)
    settings = gs.settings_file(parent)
    gs._write_json(settings, {"branch": "wanted"})
    assert "other_branch" in {p["code"] for p in gs.status(demo)["problems"]}

    def fix_cli(path, what):
        cp = run("git_sync.py", "--fix", f"--what={what}", "--json", cwd=path)
        return cp.returncode, json.loads(cp.stdout)

    def snapshot():
        return (_state(parent), (parent / ".git/HEAD").read_bytes(), on_current.exists(),
                {p.name: p.read_bytes() for p in settings.parent.iterdir() if p.is_file()})

    before = snapshot()
    refused = {}
    for what in ("checkout", "init", *(w for w in gs.FIXES if w not in ("checkout", "init"))):
        rc, got = fix_cli(demo, what)
        refused[what] = (rc, (got.get("problem") or {}).get("code"))
    assert refused == dict.fromkeys(gs.FIXES, (1, "local_paths_scope")) and snapshot() == before, (
        refused, {"parent_worktree_changed": not on_current.exists(),
                  "parent_journal_written": (settings.parent / "log.jsonl").exists()})

    subdir = parent / "projects/sibling"
    assert fix_cli(subdir, "checkout")[0] == 0
    assert _git(parent, "branch", "--show-current").stdout.strip() == "wanted"
    assert _git(parent, "show", ":projects/sibling/notes.md").stdout == "staged sibling\n"
    assert fix_cli(subdir, "init")[1]["nothing"] and not (subdir / ".git").exists()
    fresh = tmp / "fresh"
    fresh.mkdir()
    assert fix_cli(fresh, "init")[0] == 0 and (fresh / ".git").is_dir()
    broken = tmp / "broken"
    broken.mkdir()
    marker = b"gitdir: missing-gitdir\n"
    (broken / ".git").write_bytes(marker)
    rc, got = fix_cli(broken, "init")
    assert (rc, got["problem"]["code"]) == (1, "no_repo") and (broken / ".git").read_bytes() == marker


@test
def test_rc2_git_result_repairs_a_noncanonical_ignore_rule_before_add(tmp: Path):
    repo = _project(tmp / "repo")
    rules = repo / ".gitignore"
    before = rules.read_bytes().replace(b"\n.env\n", b"\n .env\n")
    rules.write_bytes(before)
    tracked = repo / "AuroraKnowledgeDB/.env.tracked-demo"
    tracked.write_text("dummy tracked data\n", encoding="utf-8")
    _commit(repo, "project with an edited rule and an already tracked file")
    (repo / "human.md").write_text("dummy staged note\n", encoding="utf-8")
    _git(repo, "add", "--", "human.md")
    dummy = repo / "AuroraKnowledgeDB/.env"
    dummy.write_text("DUMMY=not-a-secret\n", encoding="utf-8")
    example = dummy.with_name(".env.aurora.local.example")
    example.write_text("DUMMY=example\n", encoding="utf-8")

    got = _public("result", repo)
    tree = _git(repo, "ls-files", "--", "AuroraKnowledgeDB").stdout.splitlines()
    assert got["ok"] and "AuroraKnowledgeDB/.env" not in tree, (got, tree)
    assert "AuroraKnowledgeDB/.env.aurora.local.example" in tree and tracked.relative_to(repo).as_posix() in tree
    assert rules.read_bytes().startswith(before) and rules.read_bytes().splitlines().count(b".env") == 1
    assert _git(repo, "check-ignore", "--", "AuroraKnowledgeDB/.env").stdout.strip() == "AuroraKnowledgeDB/.env"
    assert _git(repo, "show", ":human.md").stdout == "dummy staged note\n"
    assert _git(repo, "diff", "--cached", "--name-only").stdout == "human.md\n"


@test
def test_rc2_git_writers_refuse_overridden_rules_for_new_candidates(tmp: Path):
    import local_paths as lp

    failures = []
    for who in WRITERS:
        repo = _project(tmp / who)
        kb = repo / "AuroraKnowledgeDB"
        (kb / ".gitignore").write_text("!.env\n!dummy log.log\n", encoding="utf-8")
        for name in (".env", "dummy log.log"):
            (kb / name).write_text("DUMMY=not-a-secret\n", encoding="utf-8")
        (repo / CARD).write_text("dummy new card\n", encoding="utf-8")
        before = _state(repo)
        with patch.object(lp, "run_index_step", wraps=lp.run_index_step) as step:
            got = _public(who, repo)
        added = any(c.args[1][0] == "add" for c in step.call_args_list)
        if got["ok"] or added or "AuroraKnowledgeDB/.env" not in str(got) or _state(repo) != before:
            failures.append((who, added, got, _paths(repo)))
    assert not failures, failures


@test
def test_rc2_git_panel_reports_the_current_push_not_the_parent_journal(tmp: Path):
    import git_sync as gs

    parent, demo = _nested(tmp, "parent")
    gs.record(parent, gs.result("push", True, "old dummy push", pushed=8))
    last = gs.read_last(parent)
    job = run("git_sync.py", "--push", cwd=demo, expect_rc=1)
    reason = gs.PROBLEMS["local_paths_scope"][0]
    assert f"✗ {reason}" in job.stdout and gs.read_last(parent) == last
    fixture = {"settings": gs.load(demo), "status": gs.status(demo), "saved": True,
               "last": last, "options": {"update_on": [], "push_on": []}}
    view = (KIT / "cockpit/modules/gitsync/view.js").as_uri()
    script = f"import {{mount, refresh}} from {json.dumps(view)};\n" + r"""
import assert from 'node:assert/strict';
class Node {
  constructor(tag, props = {}, children = []) { Object.assign(this, props, {tag, children, dataset: {}}); }
  set innerHTML(_) { this.children = []; }
  append(...children) { this.children.push(...children); }
}
const fixture = FIXTURE, nodes = {}, toasts = [];
let job = JOB;
const $ = key => nodes[key] ??= new Node('div');
const ctx = {root: $('root'), project: {path: 'dummy'}, $, t: key => key,
  el: (tag, props, ...children) => new Node(tag, props, children),
  fmt: {when: x => x}, api: async () => fixture, toast: (...args) => toasts.push(args),
  reloadHealth: () => {}, run: async (cmd, args) => {
    assert.deepEqual([cmd, args], ['git:push', []]);
    return job;
  }};
function pushButton(node) {
  if (node?.tag === 'button' && node.children.includes('gitsync.push')) return node;
  return node?.children?.map(pushButton).find(Boolean);
}
mount(ctx);
await refresh(ctx);
await pushButton($('#gitsyncBody')).onclick();
job = {rc: 0, lines: ['✓ dummy current push']};
await pushButton($('#gitsyncBody')).onclick();
job = {rc: 1, lines: ['! dummy local commit only (abc123)']};
await pushButton($('#gitsyncBody')).onclick();
console.log(JSON.stringify(toasts));
""".replace("FIXTURE", json.dumps(fixture, ensure_ascii=False)).replace(
        "JOB", json.dumps({"rc": job.returncode, "lines": job.stdout.splitlines()}, ensure_ascii=False))
    cp = run_node(tmp, script, "gitsync-ui.mjs")
    if cp is not None:
        assert cp.returncode == 0, cp.stderr
        assert json.loads(cp.stdout) == [[reason, "err"], ["dummy current push", "ok"],
                                          ["dummy local commit only (abc123)", "warn"]], cp.stdout


@test
def test_rc2_git_kit_update_noop_does_not_need_to_write_a_split_index(tmp: Path):
    repo = make_project(tmp)
    run("aurora_update.py", "--apply", cwd=repo, expect_rc=0)
    _init(repo)
    human = repo / "human.md"
    human.write_text("dummy initial\n", encoding="utf-8")
    _git(repo, "add", "--", human.name)
    _git(repo, "commit", "-qm", "current project")
    _git(repo, "config", "core.splitIndex", "true")
    _git(repo, "update-index", "--split-index")
    assert _git(repo, "rev-parse", "--shared-index-path").stdout.strip()
    human.write_text("dummy staged\n", encoding="utf-8")
    _git(repo, "add", "--", human.name)
    human.write_text("dummy worktree\n", encoding="utf-8")

    def split_state():
        git = repo / ".git"
        return _state(repo), {p.name: p.read_bytes() for p in git.glob("sharedindex.*")}

    before = split_state()
    cp = run("aurora_update.py", "--apply", cwd=repo)
    assert "Движок и структура уже актуальны" in cp.stdout, cp.stdout + cp.stderr
    assert cp.returncode == 0 and split_state() == before, (cp.returncode, cp.stdout, cp.stderr)

    lock = repo / ".git/index.lock"
    lock.write_bytes(b"dummy concurrent lock")
    run("aurora_update.py", "--apply", cwd=repo, expect_rc=0)
    assert lock.read_bytes() == b"dummy concurrent lock" and split_state() == before
    lock.unlink()

    journal = repo / JOURNAL
    journal.write_text("dummy staged journal\n", encoding="utf-8")
    _git(repo, "add", "-f", "--", JOURNAL)
    journal.write_text("dummy unstaged journal\n", encoding="utf-8")
    before = split_state()
    cp = run("aurora_update.py", "--apply", cwd=repo, expect_rc=7)
    assert "split index" in cp.stdout and split_state() == before and not lock.exists(), cp.stdout
