"""Проверки движка Aurora, часть 100. Каркас и помощники — tests/harness.py.

«О проекте» без git: кит, скачанный zip-архивом с GitHub, не знает своей истории — адрес репозитория, ветку,
автора, коммит и дату страница берёт из самого кита (`.git_archival.txt`, который `git archive` заполняет при
сборке архива, и констант). Распакованный внутрь чужого репозитория кит не должен показывать чужие сведения.
Сети нет; кит и папки — временные.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401

NODE = "0123456789abcdef0123456789abcdef01234567"


def _ck():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


class _Kit:
    """Подмена папки кита и того, что `about()` берёт у настоящего кита; возврат в `finally`."""

    def __init__(self, ck, root: Path):
        self.ck, self.root = ck, root
        self.names = ("KIT", "kit_version", "ui_version", "registry")

    def __enter__(self):
        self.saved = {n: getattr(self.ck, n) for n in self.names}
        self.ck.KIT = str(self.root)
        self.ck.kit_version = lambda: "1.2.3"
        self.ck.ui_version = lambda: "1.2.3"
        self.ck.registry = lambda: {}
        return self

    def __exit__(self, *a):
        for n, v in self.saved.items():
            setattr(self.ck, n, v)


@test
def test_the_about_page_of_a_zip_kit_reads_its_facts_from_the_archive_stamp(tmp: Path):
    """Архив GitHub: `.git_archival.txt` с подставленными хешем, датой и ссылками — страница называет
    коммит, дату и ветку, а адрес репозитория и автора берёт из кита, хотя `.git` нет."""
    ck = _ck()
    root = tmp / "aurora-studio-1.2.3"
    root.mkdir()
    (root / "CHANGELOG.md").write_text("## 1.2.3 — тест\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache", encoding="utf-8")
    (root / ".git_archival.txt").write_text(
        f"node: {NODE}\nnode-date: 2026-10-17T21:03:04+03:00\nref-names: HEAD -> master, tag: v1.2.3\n", encoding="utf-8")
    with _Kit(ck, root):
        a = ck.about()
    assert a["commit"] == NODE[:7] and a["commit_date"] == "2026-10-17", a
    assert a["branch"] == "master", a
    assert a["repo"] == ck.KIT_REPO and a["author"] == ck.KIT_AUTHOR, a
    # архив тега: «HEAD ->» нет — вместо ветки называется тег релиза
    (root / ".git_archival.txt").write_text(
        f"node: {NODE}\nnode-date: 2026-10-17T21:03:04+03:00\nref-names: tag: v1.2.3\n", encoding="utf-8")
    with _Kit(ck, root):
        assert ck.about()["branch"] == "v1.2.3"


@test
def test_an_unstamped_archive_file_and_a_missing_one_give_no_commit_but_still_the_repo_and_author(tmp: Path):
    """В клоне `.git_archival.txt` несёт нераскрытый «$Format:…$», у старого архива его нет вовсе: коммита и даты
    нет (чужих значений не выдумываем), адрес репозитория, ветка обновления и автор — всё равно названы."""
    ck = _ck()
    root = tmp / "kit"
    root.mkdir()
    (root / "CHANGELOG.md").write_text("## 1.2.3 — тест\n", encoding="utf-8")
    (root / ".git_archival.txt").write_text("node: $Format:%H$\nnode-date: $Format:%cI$\nref-names: $Format:%D$\n",
                                            encoding="utf-8")
    for _ in range(2):
        with _Kit(ck, root):
            a = ck.about()
        assert a["commit"] == "" and a["commit_date"] == "", a
        assert a["repo"] == ck.KIT_REPO and a["author"] == ck.KIT_AUTHOR and a["branch"] == ck.KIT_BRANCH, a
        (root / ".git_archival.txt").unlink(missing_ok=True)


@test
def test_a_zip_kit_unpacked_inside_another_repository_does_not_show_that_repository(tmp: Path):
    """Архив, распакованный внутрь чужого git-репозитория, не даёт странице адрес, ветку и автора этого
    репозитория: git спрашивается только у самого кита (рядом с ним есть `.git`)."""
    ck = _ck()
    outer = tmp / "outer"
    outer.mkdir()
    env = dict(__import__("os").environ, GIT_AUTHOR_NAME="Someone Else", GIT_AUTHOR_EMAIL="x@example.com",
               GIT_COMMITTER_NAME="Someone Else", GIT_COMMITTER_EMAIL="x@example.com")
    for cmd in (["git", "init", "-q", "-b", "other-branch"], ["git", "remote", "add", "origin", "https://example.org/other/repo.git"],
                ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(cmd, cwd=outer, check=True, capture_output=True, env=env)
    root = outer / "aurora-studio-1.2.3"
    root.mkdir()
    (root / "CHANGELOG.md").write_text("## 1.2.3 — тест\n", encoding="utf-8")
    with _Kit(ck, root):
        a = ck.about()
    assert a["repo"] == ck.KIT_REPO, a
    assert a["author"] == ck.KIT_AUTHOR and "Someone" not in str(a), a
    assert a["branch"] != "other-branch" and a["commit"] == "", a
