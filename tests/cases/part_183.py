#!/usr/bin/env python3
"""part_183 — индекс кода и шаблон AC: два независимых набора проверок.

Индекс кода: инвентарь, инкрементальность, исключения, секреты и продолжение (CR-08).
Проверки идут на синтетических репозиториях из `tests/code_fixtures.py`: настоящий git,
детерминированные коммиты, без сети. Панель и база знаний не нужны.

Шаблон AC: статус не спрашивается вручную, «Как» ограничено ролями из «Ролевой модели».
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402
import codeidx.ids as ids  # noqa: E402
import codeidx.model as model  # noqa: E402
import codeidx.scan as scan  # noqa: E402
import codeidx.store as store_mod  # noqa: E402


def _config(root: Path, repos: dict) -> None:
    path = root / "aurora.config.yaml"
    path.write_text(CR.write_block(path.read_text(encoding="utf-8"), repos), encoding="utf-8")


def _cli(root: Path, *args, expect_rc=0):
    return run("code_index.py", "--project", str(root), *args, cwd=root, expect_rc=expect_rc)


def _number(text: str, label: str) -> int:
    m = re.search(rf"{label} ([\d ]+)", text)
    assert m, f"в выводе нет «{label}»:\n{text}"
    return int(m.group(1).replace(" ", ""))


@test
def test_ac_template_has_no_manual_status_and_points_how_to_role_model(tmp: Path):
    """Шаблон AC не содержит вопроса про статус, а поле «Как» ограничено ролями из «Ролевой модели»."""
    text = (KIT / "scaffold/Templates/AC_template.md").read_text(encoding="utf-8")
    assert "Укажите статус User Story" not in text
    assert "{{Статус User Story" not in text
    assert "status: {{" not in text, "в шаблоне остался ручной status-плейсхолдер"
    assert "Ролевая модель" in text, "шаблон не отправляет к ролевой модели проекта"
    assert "Не придумывайте роли" in text, "нет запрета на выдуманные роли"
    assert "системн" in text.lower(), "не сказано, как поступать с системным поведением"


@test
def test_code_index_inventory_and_incremental(_t):
    """gen-mono 2000 файлов: первый прогон читает всё, второй — ни одного файла."""
    root = make_project(_t)
    repo = CF.gen_mono(root / "Code" / "gen-mono", n=2000)
    _config(root, {"gen-mono": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    first = _cli(root, "--index", "--apply")
    assert _number(first.stdout, "файлов") == 2000, first.stdout
    assert _number(first.stdout, "прочитано") == 2000, first.stdout
    second = _cli(root, "--index", "--apply")
    assert _number(second.stdout, "прочитано") == 0, second.stdout
    assert _number(second.stdout, "из кэша") == 2000, second.stdout
    status = _cli(root, "--status")
    assert "актуален" in status.stdout, status.stdout
    db = root / ".aurora" / "cache" / "code" / "gen-mono" / "index.sqlite"
    assert db.is_file(), db
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2000


@test
def test_code_index_exclusions_roles_and_secrets(_t):
    """Исключения работают, роли названы, значение секрета не попадает в индекс."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "mixed")
    token = "AKIA" + "1234567890ABCDEF"
    files = {
        "src/app.py": "print('ok')\n",
        "tests/test_app.py": "def test_x():\n    assert 1\n",
        "db/migration/001_init.sql": "CREATE TABLE orders (id INT);\n",
        "application.yml": "server:\n  port: 8080\n",
        "README.md": "# Demo\n",
        "node_modules/pkg/index.js": "module.exports = 1;\n",
        ".env": "TOKEN=" + token + "\n",
        "big.bin": b"\x00\x01\x02" * 10,
        "gen/model_pb2.py": "# generated\n",
        "package-lock.json": "{}\n",
        "app.min.js": "var a=1;" * 100 + "\n",
        "src/config.py": 'TOKEN = "' + token + '"\n',
    }
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files=files)
    _config(root, {"mixed": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    cp = _cli(root, "--index", "--apply")
    for reason in ("deps", "secret", "generated", "lock", "binary", "minified"):
        assert reason in cp.stdout, (reason, cp.stdout)
    db = root / ".aurora" / "cache" / "code" / "mixed" / "index.sqlite"
    assert token.encode() not in db.read_bytes(), "значение секрета попало в файл индекса"
    with sqlite3.connect(str(db)) as conn:
        rows = dict(conn.execute("SELECT path, skipped FROM files"))
        assert rows["src/app.py"] == "" and rows["tests/test_app.py"] == ""
        assert rows["db/migration/001_init.sql"] == ""
        assert rows["node_modules/pkg/index.js"] == "deps"
        assert rows["gen/model_pb2.py"] == "generated"
        assert rows["package-lock.json"] == "lock"
        assert rows[".env"] == "secret"
        assert rows["big.bin"] == "binary"
        assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] >= 1
    secrets = _cli(root, "--secrets")
    assert "src/config.py" in secrets.stdout, secrets.stdout
    assert token not in secrets.stdout, "значение секрета попало в вывод"


@test
def test_code_index_resume_after_interruption(_t):
    """Прерванный прогон оставляет готовые строки, следующий продолжает с них."""
    root = make_project(_t)
    repo = CF.init(root / "Code" / "many")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00",
              files={f"src/f{i}.py": f"print({i})\n" for i in range(20)})
    db = root / ".aurora" / "cache" / "code" / "many" / "index.sqlite"
    with store_mod.Store(db) as st:
        part = scan.scan_repo(repo, st, stop_after=5)
        assert part.interrupted, "остановка не отмечена"
        assert st.counts()["files"] == 5, st.counts()
    with store_mod.Store(db) as st:
        full = scan.scan_repo(repo, st)
        assert not full.interrupted
        assert full.files == 20 and full.indexed == 20, (full.files, full.indexed)
        assert full.reused >= 5, full.reused
        assert full.parsed == 15, full.parsed
        assert st.counts()["files"] == 20


@test
def test_code_index_encoding_and_crlf_hash(_t):
    """cp1251 с CRLF и UTF-8 с LF дают один нормализованный текст и один хэш."""
    text = "Привет\r\nмир\r\n"
    utf8, enc1 = model.decode_text(text.encode("utf-8"))
    cp, enc2 = model.decode_text(text.encode("cp1251"))
    assert enc1 == "utf-8" and enc2 == "cp1251", (enc1, enc2)
    assert model.normalize_newlines(utf8) == model.normalize_newlines(cp) == "Привет\nмир\n"
    assert model.text_hash(utf8) == model.text_hash(cp)
    assert model.line_count(utf8) == 2


@test
def test_code_index_stable_ids(_t):
    """ID не зависят от порядка обхода и различают одноимённые символы в разных строках."""
    assert ids.file_id("a/b.py") == ids.file_id("a/b.py")
    assert ids.file_id("a/b.py") != ids.file_id("a/c.py")
    assert ids.symbol_id("a.py", "run", 1) != ids.symbol_id("a.py", "run", 10)
    assert json.loads(ids.canonical_json({"b": 1, "a": 2})) == {"a": 2, "b": 1}
