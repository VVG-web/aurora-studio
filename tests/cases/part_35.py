"""Проверки движка Aurora, часть 35: авторы коммитов — только люди (1.169.1).

Каркас и помощники — tests/harness.py. Правило кита: автор и committer коммита — человек, без
имени модели, бота и трейлера Co-Authored-By с ними. Ассистент облачной сессии подписывает
коммиты своим именем, workflow подписывали свои коммиты ботом — так в master в октябре 2026
попали три коммита ассистента и коммиты бота. Проверка `authors` в engine-tests ловит такое
до слияния и не даёт собраться выпуску; workflow подписывают коммиты тем, кто их запустил.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess
import sys

from harness import KIT, test  # noqa: F401

CHECK = KIT / ".github" / "scripts" / "check_authors.py"
WORKFLOWS = KIT / ".github" / "workflows"


def _repo(tmp: Path) -> tuple:
    """Репозиторий с коммитами человека, ассистента, бота, трейлера и слияния кнопкой GitHub."""
    repo = tmp / "repo"
    repo.mkdir()

    def git(*a, env=None):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                              encoding="utf-8", env={**os.environ, **(env or {})}, check=True).stdout

    def commit(name, email, msg, cname=None, cemail=None):
        (repo / "f.txt").write_text(msg, encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", msg, env={
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": cname or name, "GIT_COMMITTER_EMAIL": cemail or email})
        return git("rev-parse", "HEAD").strip()

    git("init", "-q")
    base = commit("Аналитик", "analyst@example.com", "начало")
    sha = {
        "human": commit("Аналитик", "analyst@example.com", "правка человека"),
        "model": commit("Claude", "assistant@example.com", "правка ассистента"),
        "bot": commit("github-actions[bot]",
                      "41898282+github-actions[bot]@users.noreply.github.com", "правка бота"),
        "trailer": commit("Аналитик", "analyst@example.com",
                          "правка вдвоём\n\nCo-Authored-By: GPT-5 <assistant@example.com>"),
        "session": commit("Аналитик", "analyst@example.com",
                          "правка в облаке\n\nClaude-Session: https://example.com/s/1"),
        "people": commit("Аналитик", "analyst@example.com",
                         "правка вдвоём с коллегой\n\nCo-Authored-By: Коллега <colleague@example.com>"),
        "web": commit("Аналитик", "analyst@example.com", "слияние кнопкой на сайте",
                      cname="GitHub", cemail="noreply@github.com"),
    }
    return repo, base, sha


def _run(repo: Path, *args, **env):
    clean = {k: v for k, v in os.environ.items()
             if k not in ("SUGGEST_LOGIN", "SUGGEST_ID", "OWNER", "OWNER_ID")}
    return subprocess.run([sys.executable, str(CHECK), *args], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", env={**clean, **env})


@test
def test_commits_by_a_model_or_a_bot_are_refused(tmp: Path):
    """Проверка называет коммит ассистента, бота и трейлер с моделью; человека, соавтора-
    человека и слияние кнопкой GitHub (committer — сам GitHub) пропускает."""
    repo, base, sha = _repo(tmp)
    cp = _run(repo, base, sha["web"], SUGGEST_LOGIN="analyst", SUGGEST_ID="42")
    assert cp.returncode == 1, cp.stdout + cp.stderr
    flagged = {m.group(1) for m in re.finditer(r"::error::([0-9a-f]{7})", cp.stdout)}
    want = {sha[k][:7] for k in ("model", "bot", "trailer", "session")}
    assert flagged == want, f"названы {flagged}, ждали {want}:\n{cp.stdout}"
    assert "Co-Authored-By: GPT-5" in cp.stdout, "не сказано, какая строка мешает"
    assert 'user.email="42+analyst@users.noreply.github.com"' in cp.stdout, \
        "нет готовой команды, которая подставит человека в автора"
    assert _run(repo, base, sha["human"]).returncode == 0
    assert _run(repo, sha["session"], sha["web"]).returncode == 0, \
        "соавтор-человек или слияние кнопкой GitHub сочтены нарушением"


@test
def test_without_a_known_base_only_the_head_is_checked(tmp: Path):
    """Новая ветка (base из нулей) или push с --force (base нет в клоне): проверяется вершина,
    а не вся история; вместо бота в подсказке — владелец репозитория."""
    repo, _base, sha = _repo(tmp)
    assert _run(repo, "0" * 40, sha["web"]).returncode == 0, "проверена вся история ветки"
    cp = _run(repo, "f" * 40, sha["model"], SUGGEST_LOGIN="dependabot[bot]", SUGGEST_ID="1",
              OWNER="owner", OWNER_ID="7")
    assert cp.returncode == 1 and sha["model"][:7] in cp.stdout, cp.stdout
    assert 'user.name="owner"' in cp.stdout and "7+owner@users.noreply.github.com" in cp.stdout, \
        "в автора предложен бот, а не владелец репозитория"


@test
def test_workflows_sign_commits_with_the_person_who_ran_them(tmp: Path):
    """Workflow, которые коммитят, подписывают коммит тем, кто запустил прогон (вместо бота —
    владелец), на весь шаг — и rebase повторной попытки тоже; engine-tests проверяет авторов
    с полной историей."""
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        assert "github-actions[bot]" not in text, f"{wf.name}: коммит подписан ботом"
        if re.search(r"^\s+git (?:-c \S+ )*commit\b", text, re.M):
            assert "ACTOR: ${{ github.actor }}" in text and 'GIT_COMMITTER_NAME="$ACTOR"' in text, \
                f"{wf.name}: коммитит, но автор — не запустивший человек"
            assert '*"[bot]") ACTOR="$OWNER"' in text, f"{wf.name}: бот остаётся автором"
    test_yml = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    job = test_yml[test_yml.index("  authors:"):test_yml.index("  oldest-python:")]
    assert "fetch-depth: 0" in job and ".github/scripts/check_authors.py" in job, job
    assert "github.event.pull_request.base.sha || github.event.before" in job, job
    contributing = (KIT / "docs" / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "check_authors.py" in contributing, "правило об авторах не описано для участников"
    import json
    settings = json.loads((KIT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings.get("attribution") == {"commit": "", "pr": "", "sessionUrl": False}, \
        "ассистент в репозитории кита снова допишет в коммит и PR свою подпись"
