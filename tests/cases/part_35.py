"""Проверки движка Aurora, часть 35: правила кита — авторы коммитов и обезличивание (1.169.1).

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
    job = test_yml[test_yml.index("  authors:"):test_yml.index("  floor:")]
    assert "fetch-depth: 0" in job and ".github/scripts/check_authors.py" in job, job
    assert "github.event.pull_request.base.sha || github.event.before" in job, job
    contributing = (KIT / "docs" / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "check_authors.py" in contributing, "правило об авторах не описано для участников"
    import json
    settings = json.loads((KIT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings.get("attribution") == {"commit": "", "pr": "", "sessionUrl": False}, \
        "ассистент в репозитории кита снова допишет в коммит и PR свою подпись"


@test
def test_ci_matrix_covers_oses_and_python_floor_without_breaking_release(tmp: Path):
    """CI-матрица ОС x Python (BL-DEC-02): блокирующие `tests` (3.14), `floor` (3.11) и `windows` (3.12, три доли),
    неблокирующие `newer-python` (3.12 и 3.13), `legacy-310`, `windows-314` (3.14, три доли) и `macos` (3.14);
    блокирующий Windows нельзя ослабить: release.yml берёт только итог success, а continue-on-error на него не влияет;
    имя workflow не менялось (от него зависит release.yml),
    release-проверка и ruff на месте, действия не на снятом с раннеров Node 20 (checkout/setup-python v4/v5).
    Структура проверяется по тексту: pyyaml в тестовом окружении нет (штатный разбор — `ruby -ryaml`).
    Документы называют те же задания и версии; в тексте нет сросшихся скобок политики версий."""
    text = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    assert text.startswith("name: engine-tests\n"), "имя workflow изменено: release.yml перестанет ждать проверки"
    assert "workflows: [engine-tests]" in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "\t" not in text, "табуляция в YAML"
    lines = text.split("\n")
    start = lines.index("jobs:")
    jobs: dict = {}
    cur = None
    for ln in lines[start + 1:]:
        m = re.match(r"^  ([a-z0-9-]+):\s*$", ln)
        if m:
            cur = m.group(1)
            jobs[cur] = []
        elif cur is not None:
            jobs[cur].append(ln)
    assert list(jobs) == ["tests", "authors", "floor", "newer-python", "legacy-310", "windows", "windows-314", "macos"], list(jobs)
    body = {k: "\n".join(v) for k, v in jobs.items()}
    for name, py in (("tests", "3.14"), ("floor", "3.11"), ("legacy-310", "3.10"),
                     ("windows", "3.12"), ("windows-314", "3.14"), ("macos", "3.14")):
        assert re.search(rf'^\s+python-version: "{re.escape(py)}"$', body[name], re.M), (name, py)
    assert "runs-on: windows-latest" in body["windows"] and "runs-on: windows-latest" in body["windows-314"]
    assert "runs-on: macos-latest" in body["macos"]
    assert re.search(r'^        python: \["3\.12", "3\.13"\]$', body["newer-python"], re.M), "матрица newer-python"
    assert "runs-on: ubuntu-24.04" in body["newer-python"] and "fail-fast: false" in body["newer-python"]
    assert "tests/run_tests.py" in body["newer-python"] and "compileall" in body["newer-python"]
    for name in ("newer-python", "legacy-310", "windows-314", "macos"):
        assert re.search(r"^    continue-on-error: true$", body[name], re.M), f"{name}: должен быть неблокирующим"
    for name in ("tests", "authors", "floor", "windows"):
        assert not re.search(r"^\s*continue-on-error:", body[name], re.M), f"{name}: блокирует выпуск, его нельзя ослаблять"
    assert "tests/check_release.py" in body["tests"] and "tests/run_tests.py --smoke" in body["tests"]
    assert "ruff check --select F401,F841,B023" in body["tests"]
    assert "tests/run_tests.py" in body["floor"] and "compileall" in body["floor"]
    for name in ("windows", "windows-314"):
        assert "--shard=${{ matrix.shard }}/3" in body[name], name
        assert re.search(r"^        shard: \[1, 2, 3\]$", body[name], re.M), f"{name}: три доли"
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        src = wf.read_text(encoding="utf-8")
        assert not re.search(r"actions/(checkout@v[1-5]|setup-python@v[1-5])\b", src), \
            f"{wf.name}: действие на снятом Node 20"
    # Документация называет те же задания; ADR не обещает того, чего в CI нет.
    for rel in ("docs/CONTRIBUTING.md", "docs/en/CONTRIBUTING.md"):
        doc = (KIT / rel).read_text(encoding="utf-8")
        assert "`newer-python`" in doc and "3.12 " in doc and "3.13" in doc, f"{rel}: newer-python не описан"
        assert "( 3." not in doc, f"{rel}: сросшиеся скобки политики версий"
        assert "`windows-314`" in doc, f"{rel}: windows-314 не описан"
    for rel in ("docs/architecture.md", "docs/en/architecture.md"):
        doc = (KIT / rel).read_text(encoding="utf-8")
        assert ") (" not in doc[doc.index("test.yml"):doc.index("test.yml") + 700], f"{rel}: две скобочные вставки подряд"
    adr = (KIT / "docs" / "decisions" / "ADR-059-deployment-profiles.md").read_text(encoding="utf-8")
    assert "`oldest-python` заменено" in adr and "`newer-python`" in adr, "ADR-059 расходится с заданиями CI"
    assert "блокирующее задание на 3.11 и 3.12" not in adr
    assert "`windows-314`" in adr and "`windows` (3.12" in adr, "ADR-059: Windows 3.12 блокирует, 3.14 отдельно"


@test
def test_contributing_states_roles_and_anonymised_bug_notes_in_both_languages(tmp: Path):
    """Правила кита (ru и en): ядро не узнаёт источник по названию продукта, целевой словарь ролей
    помечен как не внедрённый и отделён от `role` манифеста, экспортёры коннекторов названы исключением;
    причины багов в коде, тестах, CHANGELOG и коммитах обезличены (PRJ-A/B/C)."""
    for rel in ("docs/CONTRIBUTING.md", "docs/en/CONTRIBUTING.md"):
        text = (KIT / rel).read_text(encoding="utf-8")
        for role in ("tasktracker", "projectwiki", "documents", "coderepo", "testmgmt", "webpages"):
            assert f"`{role}`" in text, f"{rel}: роль {role} не названа в правиле"
        for name in ("jira_export.py", "confluence_export.py", "atlassian_ops.py", "jira_status.py"):
            assert f"`{name}`" in text, f"{rel}: экспортёр {name} не назван исключением"
        assert "sources_registry.py" in text and "`role`" in text and "`tasks`" in text, \
            f"{rel}: не сказано, что сегодня источник берут по полю role манифеста"
        assert "PRJ-A" in text and "PRJ-C" in text and "CHANGELOG.md" in text, \
            f"{rel}: нет правила об обезличивании заметок о багах"
    ru = (KIT / "docs/CONTRIBUTING.md").read_text(encoding="utf-8")
    en = (KIT / "docs/en/CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "ещё не внедрён" in ru and "is not implemented yet" in en, \
        "словарь ролей снова подан как действующий"
