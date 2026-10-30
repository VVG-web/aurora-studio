"""Проверки движка Aurora, часть 155. Каркас и помощники — tests/harness.py.

Синхронизация кода (CR-05): `code:sync` клонирует репозитории реестра в `Code/<имя>/` и ставит их на закреплённую
ревизию или вершину ветки без ветки (detached), забирает новое и переписанную историю, не трогает клон с правками или
чужой клон (`--reset` возвращает его к реестру), без `--apply` не пишет ни байта и в сеть не ходит, пропускает
репозиторий, чья связь недоступна; `code:status` называет ревизию, отставание, языки и размер. Сеть не используется:
«серверы» — голые репозитории в папке.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test

sys.path.insert(0, str(KIT / "tests"))
import code_fixtures as CF  # noqa: E402


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.reload(importlib.import_module(name))


def _sync(root: Path, *args) -> subprocess.CompletedProcess:
    env = {**os.environ, "AURORA_NET_GATE": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(root / ".aurora/scripts/code_sync.py"), *args], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                          stdin=subprocess.DEVNULL)


def _project(tmp: Path, repos: dict, creds: dict | None = None) -> Path:
    root = make_project(tmp)
    C = _mod("code_repos")
    r = C.save(root, repos, "", creds)
    assert r.get("ok"), r
    return root


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def _head(repo: Path) -> str:
    return CF.git(repo, "rev-parse", "HEAD")


@test
def test_code_sync_clones_to_the_pinned_revision_and_the_branch_tip_detached(tmp: Path):
    """Клон по пути и по `file://`: закреплённый — на свою ревизию, незакреплённый — на вершину ветки; оба без ветки,
    с `core.autocrlf=false`; состояние записано, языки и размер посчитаны."""
    srv = tmp / "srv"
    w1, r1 = CF.server("order-intake", srv)
    w2, r2 = CF.server("py-notify", srv)
    pin = CF.git(w2, "rev-parse", "HEAD~1")
    root = _project(tmp, {"order-intake": {"url": CF.url_of(r1), "branch": "main"},
                          "py-notify": {"url": str(r2), "pin": pin[:10]}})
    cp = _sync(root, "--sync", "--apply")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    a, b = root / "Code/order-intake", root / "Code/py-notify"
    assert _head(a) == _head(w1) and _head(b) == pin, "клоны встали не на ту ревизию"
    for repo in (a, b):
        assert CF.git(repo, "symbolic-ref", "-q", "HEAD", check=False) == "", f"{repo.name}: клон на ветке, а не detached"
        assert CF.git(repo, "config", "core.autocrlf") == "false"
        assert CF.git(repo, "status", "--porcelain") == "", f"{repo.name}: рабочее дерево клона не чистое"
    st = json.loads((root / ".aurora/state/code/sync.json").read_text(encoding="utf-8"))
    entry = st["repos"]["order-intake"]
    assert entry["ok"] and entry["action"] == "clone" and entry["rev"] == _head(w1)
    langs = {k: v["files"] for k, v in entry["stats"]["languages"].items()}
    assert langs == CF.expected("order-intake")["languages"], langs
    assert "склонирован" in cp.stdout and "ревизия" in cp.stdout, cp.stdout


@test
def test_code_sync_dry_run_writes_nothing_and_goes_nowhere(tmp: Path):
    """Без `--apply` — план: ни клона, ни состояния, ни правки в проекте; адрес, которого нет, не трогается."""
    root = _project(tmp, {"app": {"url": str(tmp / "nowhere.git"), "branch": "main"}})
    before = _tree(root)
    cp = _sync(root, "--sync")
    assert cp.returncode == 0, cp.stdout
    assert "склонирую" in cp.stdout and "Ничего не записано" in cp.stdout, cp.stdout
    assert _tree(root) == before, "сухой прогон изменил проект"
    assert not (root / "Code/app").exists()


@test
def test_code_sync_takes_new_commits_and_follows_a_rewritten_history(tmp: Path):
    """Новый коммит на сервере — клон его забирает; переписанная история — не ошибка, а новая цель."""
    work, remote = CF.server("ts-portal", tmp / "srv")
    root = _project(tmp, {"ts-portal": {"url": str(remote)}})
    assert _sync(root, "--sync", "--apply").returncode == 0
    clone = root / "Code/ts-portal"
    first = _head(clone)
    sha = CF.commit(work, "portal: filter by supplier", "2026-03-05T10:00:00+00:00",
                    files={"src/filter.ts": "export const bySupplier = true;\n"})
    CF.push(work)
    cp = _sync(root, "--sync", "--apply")
    assert cp.returncode == 0 and _head(clone) == sha and "обновлён" in cp.stdout, cp.stdout
    CF.git(work, "reset", "-q", "--hard", first)
    other = CF.commit(work, "portal: rewritten", "2026-03-06T10:00:00+00:00", files={"src/other.ts": "export {};\n"})
    CF.push(work, force=True)
    cp = _sync(root, "--sync", "--apply")
    assert cp.returncode == 0 and _head(clone) == other, cp.stdout
    assert not (clone / "src/filter.ts").exists(), "файл переписанной истории остался в клоне"
    cp = _sync(root, "--sync", "--apply")
    assert cp.returncode == 0 and "без изменений" in cp.stdout, cp.stdout


@test
def test_code_sync_leaves_a_dirty_or_foreign_clone_and_reset_returns_it(tmp: Path):
    """Клон с правками и клон другого адреса не трогаются (код 1, причина словами); «Сбросить клон» возвращает клон
    к реестру и ставит на ревизию; сброс без `--repo` — отказ."""
    _work, remote = CF.server("noise", tmp / "srv")
    _w2, other = CF.server("py-notify", tmp / "srv")
    root = _project(tmp, {"noise": {"url": str(remote)}})
    assert _sync(root, "--sync", "--apply").returncode == 0
    clone = root / "Code/noise"
    (clone / "src/main.go").write_text("package main // правка\n", encoding="utf-8")
    (clone / "scratch.txt").write_text("моё\n", encoding="utf-8")
    cp = _sync(root, "--sync", "--apply")
    assert cp.returncode == 1 and "В клоне есть правки" in cp.stdout and "scratch.txt" in cp.stdout, cp.stdout
    assert (clone / "scratch.txt").is_file(), "синхронизация стёрла правки без «Сбросить клон»"
    assert _sync(root, "--sync", "--reset", "--apply").returncode == 2, "сброс всех клонов разом не должен проходить"
    cp = _sync(root, "--sync", "--reset", "--repo", "noise", "--apply")
    assert cp.returncode == 0 and not (clone / "scratch.txt").exists(), cp.stdout
    assert CF.git(clone, "status", "--porcelain") == ""
    CF.git(clone, "remote", "set-url", "origin", str(other))
    cp = _sync(root, "--sync", "--apply")
    assert cp.returncode == 1 and "не от этого репозитория" in cp.stdout, cp.stdout
    cp = _sync(root, "--sync", "--reset", "--repo", "noise", "--apply")
    assert cp.returncode == 0 and CF.git(clone, "remote", "get-url", "origin") == str(remote), cp.stdout


@test
def test_code_sync_keeps_the_secret_out_of_git_config_output_and_state(tmp: Path):
    """Токен репозитория не попадает ни в `.git/config` клона, ни в вывод команды, ни в файл состояния, ни в конфиг."""
    restore = set_home(tmp / "home")
    try:
        _work, remote = CF.server("py-notify", tmp / "srv")
        token = "tok-" + "5" * 26
        root = _project(tmp, {"py-notify": {"url": str(remote)}},
                        {"py-notify": {"auth": "token", "user": "reader", "secret": token}})
        cp = _sync(root, "--sync", "--apply", "--json")
        assert cp.returncode == 0, cp.stdout
        places = {"вывод": cp.stdout + cp.stderr,
                  ".git/config": (root / "Code/py-notify/.git/config").read_text(encoding="utf-8"),
                  "состояние": (root / ".aurora/state/code/sync.json").read_text(encoding="utf-8"),
                  "конфиг": (root / "aurora.config.yaml").read_text(encoding="utf-8")}
        leaked = [k for k, v in places.items() if token in v]
        assert not leaked, f"токен попал в: {leaked}"
        st = _sync(root, "--status", "--json")
        assert token not in st.stdout and json.loads(st.stdout)["repos"][0]["state"] == "ok"
    finally:
        restore()


@test
def test_code_sync_without_repositories_says_so_in_one_line_and_exits_zero(tmp: Path):
    """Пустой реестр — не ошибка: шаг маршрута «Обновить базу» проходит одной строкой."""
    root = make_project(tmp)
    for args in (("--sync", "--apply"), ("--sync",), ("--status",)):
        cp = _sync(root, *args)
        assert cp.returncode == 0 and len(cp.stdout.strip().splitlines()) == 1, (args, cp.stdout)
    cp = _sync(root, "--sync", "--apply", "--repo", "nope")
    assert cp.returncode == 2 and "нет в реестре" in cp.stdout, cp.stdout


@test
def test_code_sync_skips_a_repository_whose_connection_is_down_and_syncs_the_rest(tmp: Path):
    """Два репозитория, два профиля связи: недоступный пропускается с причиной и строкой `AURORA-NET` (код 2 — маршрут
    подождёт связь), доступный синхронизируется. Предзапуск команды целиком берёт профили обоих."""
    srv = tmp / "srv"
    _w1, r1 = CF.server("ts-portal", srv)
    _w2, r2 = CF.server("py-notify", srv)
    root = _project(tmp, {"ts-portal": {"url": str(r1), "access": "vpn:VPN-A"},
                          "py-notify": {"url": str(r2), "access": "vpn:VPN-B"}})
    G = _mod("conn_gate")
    req = G.command_requirements("code:sync", str(root), deps={})
    assert req["profiles"] == ["VPN-A", "VPN-B"] and 1 in req["types"], req
    assert set(G.required_for(str(root))["profiles"]) >= {"VPN-A", "VPN-B"}
    CS = _mod("code_sync")
    saved = CS.net_verdicts
    CS.net_verdicts = lambda repos: {n: ({"ok": False, "blocked": [{"code": "profile_offline", "name": "VPN-A"}]}
                                         if n == "ts-portal" else {"ok": True, "blocked": []}) for n in repos}
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    cwd = os.getcwd()
    try:
        os.chdir(root)
        with redirect_stdout(out):
            rc = CS.run_sync(str(root), apply=True)
    finally:
        os.chdir(cwd)
        CS.net_verdicts = saved
    text = out.getvalue()
    assert rc == 2 and "AURORA-NET" in text and "VPN-A недоступен" in text, text
    assert (root / "Code/py-notify/.git").exists() and not (root / "Code/ts-portal").exists(), text
    marker = G.parse_marker(text.splitlines())
    assert marker.get("sources") == ["code:ts-portal"], marker


@test
def test_code_status_names_languages_and_size_by_the_fixture_markup(tmp: Path):
    """Языки и размер без внешних инструментов: у каждой фикстуры — ровно разметка `expected.json`; зависимости,
    сгенерированное, минифицированное, блокировки и двоичное — не в счёт, а названы причиной; маски реестра работают."""
    CS = _mod("code_sync")
    for name in CF.NAMES:
        repo = CF.build(name, tmp / name)
        s = CS.repo_stats(repo)
        exp = CF.expected(name)
        assert {k: v["files"] for k, v in s["languages"].items()} == exp["languages"], (name, s["languages"])
        assert s["skipped"] == exp.get("skipped", {}), (name, s["skipped"])
        if "other" in exp:
            assert s["other"] == exp["other"], (name, s["other"])
    repo = tmp / "order-intake"
    lines = sum(p.read_bytes().count(b"\n") for p in repo.rglob("*.java"))
    assert CS.repo_stats(repo)["languages"]["Java"]["lines"] == lines
    s = CS.repo_stats(repo, {"exclude": "refund/**, *.md", "include": ""})
    assert s["languages"]["Java"]["files"] == 7 and "Markdown" not in s["languages"] and s["skipped"]["excluded"] == 4, s
    s = CS.repo_stats(tmp / "noise", {"include": "vendor/**", "exclude": ""})
    assert s["languages"]["Go"]["files"] == 2, "маска include не вернула файл из vendor/"


@test
def test_code_commands_are_registered_everywhere_the_kit_looks(_t):
    """Реестр команд, зависимости от связи, шаг маршрута «Обновить базу», манифест движка, справочники и навык."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    for cmd, impl in (("code:sync", "code_sync.py --sync"), ("code:status", "code_sync.py --status")):
        assert f"| {cmd} | — | скрипт | {impl} | 1.195.0 |" in reg, cmd
        for doc in ("docs/commands.md", "docs/en/commands.md", "skills/aurora-vault/SKILL.md"):
            assert f"`{cmd}`" in (KIT / doc).read_text(encoding="utf-8"), (cmd, doc)
    deps = json.loads((KIT / "command_deps.json").read_text(encoding="utf-8"))["commands"]
    assert deps["code:sync"]["offline_capable"] is False and deps["code:status"]["offline_capable"] is True
    upd = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8").split("[update]")[1].split("\n[")[0]
    steps = [ln.split("|")[0].strip() for ln in upd.splitlines()[1:] if ln.strip()]
    assert steps.index("code:sync") == steps.index("sync:web") + 1, steps[:6]
    step = next(ln for ln in upd.splitlines() if ln.startswith("code:sync"))
    assert step.rstrip().endswith("| --apply"), step
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    for f in ("code_repos.py", "code_sync.py"):
        assert f"scripts/{f}" in man and f".aurora/scripts/{f}" in man, f
    cp = subprocess.run([sys.executable, str(SCRIPTS / "kit_commands.py"), "--check"], cwd=str(KIT),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
