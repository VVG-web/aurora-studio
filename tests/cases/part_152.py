"""Проверки движка Aurora, часть 152. Каркас и помощники — tests/harness.py.

Фикстуры кода (CR-02): построитель синтетических репозиториев `tests/code_fixtures.py` собирает git-репозитории из
деревьев `tests/corpus/code/<имя>/tree/` по сценарию `commits.json` — детерминированно, без сети, с «сервером» —
голым клоном рядом. Модель и сеть не используются; репозитории — только во временных папках.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from harness import KIT, SCRIPTS, test

sys.path.insert(0, str(KIT / "tests"))
import code_fixtures as CF  # noqa: E402


@test
def test_code_fixtures_build_every_repository_the_same_way_twice(tmp: Path):
    """Каждая фикстура собирается быстро, с числом коммитов по сценарию, а повторная сборка даёт те же SHA."""
    t0 = time.time()
    first = CF.build_all(tmp / "a")
    took = time.time() - t0
    second = CF.build_all(tmp / "b")
    assert set(first) == set(CF.NAMES), first
    for name in CF.NAMES:
        exp = CF.expected(name)
        assert CF.head(first[name]) == CF.head(second[name]), f"{name}: повторная сборка дала другой SHA"
        count = int(CF.git(first[name], "rev-list", "--count", "HEAD"))
        assert count == exp["commits"], f"{name}: коммитов {count}, по сценарию {exp['commits']}"
        assert CF.git(first[name], "status", "--porcelain") == "", f"{name}: рабочее дерево не чистое"
        assert CF.git(first[name], "symbolic-ref", "--short", "HEAD") == "main", name
    # 20 с, а не 10 из приёмки: на Windows каждый запуск git стоит в разы дороже, чем на macOS и Linux.
    assert took < 20, f"фикстуры собирались {took:.1f} с"
    log = CF.git(first["order-intake"], "log", "--format=%ad|%s", "--date=iso-strict")
    assert "2026-01-12T09:00:00Z|BENCH-1 US-1.1: order intake skeleton" in log, log


@test
def test_code_fixtures_keep_crlf_cp1251_and_binary_bytes_as_written(tmp: Path):
    """CRLF, cp1251 и двоичное приходят в репозиторий байт в байт: в ките их нет, их пишет сценарий."""
    repo = CF.build("noise", tmp / "noise")
    assert (repo / "docs/notes.txt").read_bytes().count(b"\r\n") == 2, "концы строк CRLF потерялись"
    assert (repo / "docs/legacy.txt").read_bytes() == "Склад закрывается в шесть.\n".encode("cp1251")
    assert (repo / "assets/logo.bin").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "двоичный файл испорчен"
    blob = CF.git(repo, "cat-file", "-p", "HEAD:docs/notes.txt")
    assert "Stock is checked" in blob, "в объекте git не тот текст"


@test
def test_code_fixtures_server_takes_new_commits_and_a_rewritten_history(tmp: Path):
    """«Сервер» — голый клон: новый коммит уходит обычным push, переписанная история — принудительным."""
    work, remote = CF.server("py-notify", tmp)
    assert remote.name == "py-notify.git" and (remote / "HEAD").is_file()
    assert CF.url_of(remote).startswith("file://"), CF.url_of(remote)
    before = CF.head(remote, "refs/heads/main")
    sha = CF.commit(work, "notifications: retry", "2026-02-10T09:00:00+00:00",
                    files={"app/retry.py": "ATTEMPTS = 3\n"})
    CF.push(work)
    assert CF.head(remote, "refs/heads/main") == sha != before
    CF.git(work, "reset", "-q", "--hard", before)
    other = CF.commit(work, "notifications: retry five times", "2026-02-11T09:00:00+00:00",
                      files={"app/retry.py": "ATTEMPTS = 5\n"})
    CF.push(work, force=True)
    assert CF.head(remote, "refs/heads/main") == other, "переписанная история не ушла на сервер"
    r = subprocess.run(["git", "-C", str(remote), "merge-base", "--is-ancestor", sha, other], capture_output=True)
    assert r.returncode == 1, "прежний коммит остался предком нового — история не переписана"


@test
def test_code_fixtures_trip_no_secret_scanner_and_name_no_real_domains(_t):
    """Файлы фикстур не будят сканер секретов кита (форматы поставщиков), адреса в них — только example.com."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    hits, hosts = [], set()
    for p in sorted(CF.CORPUS.rglob("*")):
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        hits += [(p.relative_to(CF.CORPUS).as_posix(), name) for name, _line, _shown in
                 AC.scan_secret_text(text, formats_only=True)]
        hosts |= set(re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|ru|io)\b", text))
    assert not hits, f"сканер секретов сработал на фикстурах: {hits[:3]}"
    assert hosts <= {"example.com", "kafka.example.com"}, f"в фикстурах адреса вне example.com: {sorted(hosts)}"


@test
def test_code_fixture_tree_files_all_belong_to_the_scenario(tmp: Path):
    """Файл дерева, который не взял ни один коммит, — ошибка сценария, а не молча другой репозиторий."""
    import shutil
    fake = tmp / "corpus"
    shutil.copytree(CF.CORPUS / "ts-portal", fake / "ts-portal")
    (fake / "ts-portal/tree/src/forgotten.ts").write_text("export const x = 1;\n", encoding="utf-8")
    saved = CF.CORPUS
    CF.CORPUS = fake
    try:
        CF.build("ts-portal", tmp / "repo")
    except RuntimeError as e:
        assert "forgotten.ts" in str(e), e
    else:
        raise AssertionError("забытый в сценарии файл дерева не замечен")
    finally:
        CF.CORPUS = saved


@test
def test_code_fixtures_generate_a_large_monorepo(tmp: Path):
    """gen-mono: N файлов пяти языков одним коммитом; содержимое зависит только от N и зерна."""
    repo = CF.gen_mono(tmp / "mono", n=150)
    files = CF.git(repo, "ls-files").splitlines()
    assert len(files) == 150, len(files)
    assert {f.rsplit(".", 1)[1] for f in files} == {"java", "py", "ts", "go", "sql"}
    assert CF.head(repo) == CF.head(CF.gen_mono(tmp / "mono2", n=150)), "gen-mono не детерминирован"
