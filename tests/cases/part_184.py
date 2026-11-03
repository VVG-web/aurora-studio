"""Проверки совместной работы: локальное состояние вне git, общие части без конфликтов."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from harness import SCRIPTS, test


def _path():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _commit(repo: Path, message: str):
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", message)


@test
def test_every_local_state_rule_untracks_but_preserves_the_file(tmp: Path):
    """Канонический ignore и update работают одним списком; повтор и содержимое безопасны."""
    _path()
    import aurora_update as AU
    import install_aurora as IA

    repo = tmp / "repo"
    repo.mkdir()
    for num, rel in enumerate(AU.LOCAL_STATE_RELS):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"local-{num}\n", encoding="utf-8")
        assert rel in IA.GITIGNORE_BLOCK.splitlines(), f"нет канонического ignore: {rel}"
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _commit(repo, "local state used to be tracked")

    removed = AU.untrack_local_state(repo)
    assert removed == list(AU.LOCAL_STATE_RELS), removed
    for num, rel in enumerate(AU.LOCAL_STATE_RELS):
        target = repo / rel
        assert target.read_text(encoding="utf-8") == f"local-{num}\n", rel
        assert not _git(repo, "ls-files", "--", rel).stdout.strip(), rel
    assert AU.untrack_local_state(repo) == [], "повторный update снова меняет index"
    # Machine-files остаются на диске и читаются командами после untrack (панель, doctor).
    assert (repo / "AuroraKnowledgeDB/meta/aurora_version.txt").read_text(
        encoding="utf-8").startswith("local-"), "штамп версии потерялся с диска"


@test
def test_two_git_users_merge_their_update_marks_without_a_conflict(tmp: Path):
    """Два клона расходятся коммитами, pull сливает разные части, legacy и записи сохраняются."""
    _path()
    import kb_updated as KU

    bare = tmp / "shared.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True,
                   capture_output=True)
    seed = tmp / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True,
                   capture_output=True)
    legacy = seed / KU.REL
    legacy.parent.mkdir(parents=True)
    old = {"updated": "2026-10-01T09:00:00Z",
           "update": {"at": "2026-10-01T08:00:00Z", "who": "Legacy"},
           "fix": {"at": "2026-10-01T09:00:00Z", "who": "Legacy"}}
    legacy.write_text(json.dumps(old), encoding="utf-8")
    _git(seed, "config", "user.name", "Legacy")
    _git(seed, "config", "user.email", "legacy@example.com")
    _commit(seed, "legacy update mark")
    _git(seed, "push", "-q", "origin", "HEAD:master")

    alice = tmp / "alice"
    bob = tmp / "bob"
    for repo, name in ((alice, "Alice"), (bob, "Bob")):
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True,
                       capture_output=True)
        _git(repo, "config", "user.name", name)
        _git(repo, "config", "user.email", f"{name.lower()}@example.com")

    was_user = os.environ.get("AURORA_USER")
    try:
        os.environ["AURORA_USER"] = "Alice"
        KU.mark(alice, "update", who="Alice", at="2026-11-02T10:00:00Z")
        KU.mark(alice, "fix", who="Alice", at="2026-11-02T10:30:00Z")
        _commit(alice, "Alice updates the base")
        _git(alice, "push", "-q", "origin", "HEAD:master")

        os.environ["AURORA_USER"] = "Bob"
        KU.mark(bob, "update", who="Bob", at="2026-11-02T11:00:00Z")
        KU.mark(bob, "fix", who="Bob", at="2026-11-02T11:30:00Z")
        _commit(bob, "Bob updates the base")
        _git(bob, "-c", "commit.gpgsign=false", "pull", "--no-rebase", "-q", "origin", "master")
        merged = KU.read(bob)
    finally:
        if was_user is None:
            os.environ.pop("AURORA_USER", None)
        else:
            os.environ["AURORA_USER"] = was_user

    assert merged["updated"] == "2026-11-02T11:30:00Z", merged
    assert merged["update"]["who"] == "Bob" and merged["fix"]["who"] == "Bob", merged
    shards = list((bob / KU.SHARD_REL).glob("*.json"))
    assert len(shards) == 2, f"после merge потеряна часть участника: {shards}"
    assert json.loads((bob / KU.REL).read_text(encoding="utf-8")) == old, "legacy переписан"


@test
def test_service_scripts_answer_help_without_input_or_files(tmp: Path):
    """Обязательный обход scripts/*.py --help не падает на служебных CLI."""
    for name in ("atlassian_ops.py", "legal_slicer.py"):
        cp = subprocess.run([sys.executable, str(SCRIPTS / name), "--help"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cp.returncode == 0, f"{name}: {cp.returncode}, {cp.stderr}"
        assert "использование:" in cp.stdout, f"{name}: {cp.stdout}"


@test
def test_atlassian_api_helper_preserves_connection_and_errors(tmp: Path):
    """Общий конструктор не меняет URL, учётные данные и понятные отказы Jira/Confluence."""
    _path()
    from unittest.mock import patch
    import atlassian_ops as AO

    class FakeApi:
        def __init__(self, url, auth, root):
            self.url, self.auth, self.root = url, auth, root

    with patch.object(AO, "read_secret", return_value=("secret", "test")):
        api = AO._api(FakeApi, "JIRA", "https://jira.example", "project")
    assert (api.url, api.auth, api.root) == ("https://jira.example", "secret", "project")
    for service, fragment in (("JIRA", "Jira проекту не подключена"),
                              ("CONFLUENCE", "Confluence проекту не подключён")):
        try:
            AO._api(FakeApi, service, "", "project")
        except AO.OpError as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(f"{service}: пустой URL принят")
    with patch.object(AO, "read_secret", return_value=("", "")):
        try:
            AO._api(FakeApi, "CONFLUENCE", "https://conf.example", "project")
        except AO.OpError as exc:
            assert "CONFLUENCE_PAT" in str(exc), str(exc)
        else:
            raise AssertionError("Confluence: пустой токен принят")
