#!/usr/bin/env python3
"""part_184 — извлечение T0: Java/Spring, Python, TypeScript, SQL, конфигурация и метки.

Пакет CR-09. Проверки идут на тех же синтетических репозиториях, что и CR-08, плюс
отдельные файлы для мусора и меток требований. Сеть и модель не нужны.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402
import codeidx.store as store_mod  # noqa: E402
import codeidx.scan as scan  # noqa: E402


def _path():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _commit(repo: Path, message: str):
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", message)


def _config(root: Path, repos: dict, extra: str = "") -> None:
    path = root / "aurora.config.yaml"
    text = path.read_text(encoding="utf-8") + extra
    path.write_text(CR.write_block(text, repos), encoding="utf-8")


def _cli(root: Path, *args, expect_rc=0):
    return run("code_index.py", "--project", str(root), *args, cwd=root, expect_rc=expect_rc)


def _db(root: Path, name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(root / ".aurora" / "cache" / "code" / name / "index.sqlite"))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


@test
def test_code_index_java_contract(_t):
    """order-intake: маршруты, DTO, сущность, таблицы, топик, конфигурация и тесты."""
    root = make_project(_t)
    repo = CF.build("order-intake", root / "Code" / "order-intake")
    _config(root, {"order-intake": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    _cli(root, "--index", "--apply")
    with _db(root, "order-intake") as conn:
        routes = {(r["method"], r["path"]) for r in _rows(conn, "SELECT method, path FROM routes")}
        assert ("POST", "/api/orders") in routes, routes
        assert ("DELETE", "/api/refunds/{id}") in routes, routes
        order_route = _rows(conn, "SELECT * FROM routes WHERE method='POST' AND path='/api/orders'")[0]
        assert order_route["request"] == "OrderRequest", order_route
        assert "400" in order_route["status_codes"] and "200" in order_route["status_codes"], order_route
        tables = {(r["table_name"], r["source"]) for r in _rows(conn, "SELECT table_name, source FROM db_tables")}
        assert ("orders", "entity") in tables and ("orders", "migration") in tables, tables
        cols = _rows(conn, "SELECT name, pk, is_unique FROM db_columns WHERE table_name='orders'")
        assert any(r["name"] == "external_id" and r["is_unique"] for r in cols), cols
        assert any(r["name"] == "id" and r["pk"] for r in cols), cols
        assert any(r["name"] == "supplier" for r in cols), cols
        topics = {(r["name"], r["role"]) for r in _rows(conn, "SELECT name, role FROM topics")}
        assert ("orders.accepted", "producer") in topics, topics
        keys = {r["key"] for r in _rows(conn, "SELECT key FROM configs")}
        assert "orders.visibility-seconds" in keys, keys
        assert "spring.kafka.bootstrap-servers" in keys, keys
        tests = {r["target"] for r in _rows(conn, "SELECT target FROM tests")}
        assert {"OrderController", "RefundService"} <= tests, tests
        dto_fields = {r["name"] for r in _rows(
            conn, "SELECT name FROM symbols WHERE kind='field' AND qname LIKE '%OrderRequest.%'")}
        assert {"externalId", "supplier", "warehouse", "quantity"} <= dto_fields, dto_fields


@test
def test_code_index_python_ast_ranges(_t):
    """Python: точные диапазоны `create` и поля моделей из `ast`."""
    root = make_project(_t)
    repo = CF.build("py-notify", root / "Code" / "py-notify")
    _config(root, {"py-notify": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    _cli(root, "--index", "--apply")
    with _db(root, "py-notify") as conn:
        create = _rows(conn, "SELECT * FROM symbols WHERE name='create' AND kind='function'")[0]
        assert (create["line_start"], create["line_end"]) == (15, 17), create
        route = _rows(conn, "SELECT * FROM routes")[0]
        assert (route["method"], route["path"]) == ("POST", "/api/notifications"), route
        assert route["request"] == "NotificationIn", route
        table = _rows(conn, "SELECT table_name FROM db_tables")[0]
        assert table["table_name"] == "notifications", table
        targets = {r["target"] for r in _rows(conn, "SELECT target FROM tests")}
        assert "create" in targets, targets


@test
def test_code_index_typescript_routes_and_classes(_t):
    """TypeScript: комментарий-маршрут, класс контроллера и интерфейс строки."""
    root = make_project(_t)
    repo = CF.build("ts-portal", root / "Code" / "ts-portal")
    _config(root, {"ts-portal": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    _cli(root, "--index", "--apply")
    with _db(root, "ts-portal") as conn:
        route = _rows(conn, "SELECT * FROM routes")[0]
        assert (route["method"], route["path"]) == ("GET", "/portal/orders"), route
        assert route["handler"], route
        names = {r["name"] for r in _rows(conn, "SELECT name FROM symbols WHERE kind IN ('class','interface')")}
        assert {"OrdersController", "OrderRow"} <= names, names
        fields = {r["name"] for r in _rows(
            conn, "SELECT name FROM symbols WHERE kind='field' AND parent LIKE '%OrderRow'")}
        assert {"externalId", "supplier", "status"} <= fields, fields


@test
def test_code_index_parse_error_does_not_stop(_t):
    """Сломанный файл помечается `parse_error`, остальные разбираются."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "broken")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files={"src/good.py": "def ok():\n    return 1\n", "src/broken.py": "def broken(:\n"})
    _config(root, {"broken": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    _cli(root, "--index", "--apply")
    with _db(root, "broken") as conn:
        rows = {r["path"]: r["skipped"] for r in _rows(conn, "SELECT path, skipped FROM files")}
        assert rows["src/broken.py"] == "parse_error", rows
        assert rows["src/good.py"] == "", rows
        assert _rows(conn, "SELECT name FROM symbols WHERE name='ok'")


@test
def test_code_index_reqtags(_t):
    """Метки `REQ:`, коды артефактов, номера историй и известные ключи проекта."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "tagged")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files={"src/a.py": "# REQ: BENCH-1\n# ALG-12 и US-1.1\n\ndef run():\n    return 1\n"})
    _config(root, {"tagged": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}},
            extra="jira_keys: [BENCH]\n")
    _cli(root, "--index", "--apply")
    with _db(root, "tagged") as conn:
        keys = {r["key"] for r in _rows(conn, "SELECT key FROM reqtags")}
        assert {"BENCH-1", "ALG-12", "US-1.1"} <= keys, keys


@test
def test_code_index_40_files_under_two_seconds(_t):
    """Разбор 40 файлов укладывается в две секунды (T0, без модели)."""
    root = make_project(_t)
    repo = CF.gen_mono(root / "Code" / "small", n=40)
    _config(root, {"small": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    with store_mod.Store(root / ".aurora" / "cache" / "code" / "small" / "index.sqlite") as st:
        stats = scan.scan_repo(repo, st)
        assert stats.files == 40, stats.files
        assert stats.duration_s < 2.0, stats.duration_s


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
def test_update_brings_new_gitignore_rules_without_overwriting_project_choices(tmp: Path):
    """Проект, который заведён давно, при kit:update получает новые LOCAL правила,
    не теряя собственные правила и содержимое личных файлов; обновление идемпотентно."""
    _path()
    import aurora_update as AU
    import install_aurora as IA

    repo = tmp / "repo"
    (repo / "AuroraKnowledgeDB/meta").mkdir(parents=True)
    (repo / "start-aurora.bat").write_text("@echo off\n", encoding="utf-8")
    (repo / "AuroraKnowledgeDB/meta/recent-files.json").write_text(
        '["Concepts/Home.md"]', encoding="utf-8")
    gitignore = repo / ".gitignore"
    gitignore.write_text("# мои правила проекта\nprivate-output/\n*.bak\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Owner")
    _git(repo, "config", "user.email", "owner@example.com")
    _commit(repo, "initial project before new ignore rules")

    added = AU.refresh_gitignore(repo)
    assert "start-aurora.bat" in added and "AuroraKnowledgeDB/meta/recent-files.json" in added, added
    text = gitignore.read_text(encoding="utf-8")
    assert "private-output/" in text and "*.bak" in text, "правки проекта потеряны"
    assert text.startswith("# мои правила проекта"), "пользовательский комментарий перезаписан"
    for rel in ("AuroraKnowledgeDB/meta/recent-files.json", "start-aurora.bat",
                "AuroraKnowledgeDB/meta/aurora_version.txt"):
        assert rel in IA.GITIGNORE_BLOCK.splitlines(), f"не игнорируется китом: {rel}"

    # Повторное обновление не должно добавлять дубликатов и сохраняет содержимое.
    before = gitignore.read_bytes()
    assert AU.refresh_gitignore(repo) == [], "повторное update снова дописывает gitignore"
    assert gitignore.read_bytes() == before
    assert (repo / "AuroraKnowledgeDB/meta/recent-files.json").read_text(
        encoding="utf-8") == '["Concepts/Home.md"]'

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
