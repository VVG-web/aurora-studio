"""Проверки части 239: снимки сети, VPN и LLM вокруг прогонов.

Каркас — tests/harness.py. Настоящая сеть и настоящий `~/.aurora` не используются:
состояние проверок и журнал событий подменяются временными путями, системный VPN — заглушкой.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from harness import SCRIPTS, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))


def _no():
    return importlib.import_module("net_observe")


def _project(tmp: Path) -> Path:
    root = tmp / "проект"
    (root / "AuroraKnowledgeDB").mkdir(parents=True)
    (root / ".aurora" / "runs" / "r1").mkdir(parents=True)
    return root


def _env(tmp: Path, **extra):
    values = {
        "AURORA_CONNECTIVITY_STATE": str(tmp / "state.json"),
        "AURORA_CONNECTIVITY_EVENTS": str(tmp / "events.jsonl"),
        "AURORA_NET_SYSTEM_VPN": "0",
        "AURORA_NET_OBSERVE": "1",
        "AURORA_USER": "Алиса",
        "AURORA_MACHINE": "m1",
    }
    values.update(extra)
    return values


def _with_env(values: dict):
    class _Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in values}
            os.environ.update(values)

        def __exit__(self, *exc):
            for key, value in self.old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return _Ctx()


@test
def test_vpn_names_and_snapshot_are_compact(tmp: Path):
    """Имена системных VPN сохраняются, а probe-адреса и секреты — нет."""
    NO = _no()
    text = """Available network connection services:
* (Disconnected) AAAA VPN (com.wireguard.macos) "Старый" [VPN:wireguard]
* (Connected)    BBBB PPP --> L2TP "Рабочий VPN" [PPP:L2TP]
* (Connected)    CCCC PPP --> L2TP "Резерв" [PPP:L2TP]
"""
    rows = NO.parse_scutil(text)
    assert [r["name"] for r in rows] == ["Рабочий VPN", "Резерв"], rows
    assert all(r["kind"] for r in rows), rows
    snap = {"state": "partial", "enabled": True,
            "profiles": [{"name": "VPN-1", "state": "offline", "required": True,
                          "probes": [{"target": "secret.internal:443"}]}],
            "providers": [{"id": "gw", "name": "Шлюз", "state": "offline",
                           "why": "profile_offline", "profile": "VPN-1", "required": True}],
            "internet": {"state": "online", "monitored": True}}
    req = {"profiles": ["VPN-1"], "providers": ["gw"], "internet": False, "llm": True}
    compact = NO._compact(snap, req)
    raw = json.dumps(compact, ensure_ascii=False)
    assert "secret.internal" not in raw and "probes" not in raw, raw
    assert compact["blocked"] and compact["blocked"][0]["code"] == "profile_offline", compact


@test
def test_observation_is_per_user_and_machine(tmp: Path):
    """Два участника и две машины не делят один общий файл."""
    NO = _no()
    project = _project(tmp)
    with _with_env(_env(tmp)):
        first = NO.shared_path(str(project), "r1")
        with _with_env(_env(tmp, AURORA_USER="Боб")):
            second = NO.shared_path(str(project), "r1")
        with _with_env(_env(tmp, AURORA_MACHINE="m2")):
            third = NO.shared_path(str(project), "r1")
    assert first and second and third
    assert len({str(first), str(second), str(third)}) == 3, (first, second, third)
    assert "Алиса" not in str(first) and "m1" not in str(first)


@test
def test_two_user_shards_merge_without_a_conflict(tmp: Path):
    """Разные участники добавляют разные файлы, git сливает ветки без конфликта."""
    repo = tmp / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=30)
    assert run("init", "-q", "-b", "master").returncode == 0
    assert run("config", "user.name", "Тест").returncode == 0
    assert run("config", "user.email", "test@example.com").returncode == 0
    (repo / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "alice-m1").mkdir(parents=True)
    (repo / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "bob-m1").mkdir(parents=True)
    (repo / "base").write_text("base\n", encoding="utf-8")
    assert run("add", ".").returncode == 0
    assert run("commit", "-qm", "base").returncode == 0
    assert run("checkout", "-qb", "alice").returncode == 0
    (repo / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "alice-m1" / "r1.json").write_text(
        '{"id":"r1"}\n', encoding="utf-8")
    assert run("add", ".").returncode == 0 and run("commit", "-qm", "alice").returncode == 0
    assert run("checkout", "-q", "master").returncode == 0
    assert run("checkout", "-qb", "bob").returncode == 0
    (repo / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "bob-m1" / "r2.json").write_text(
        '{"id":"r2"}\n', encoding="utf-8")
    assert run("add", ".").returncode == 0 and run("commit", "-qm", "bob").returncode == 0
    merged = run("merge", "alice", "--no-edit")
    assert merged.returncode == 0, merged.stdout + merged.stderr


@test
def test_long_observer_writes_at_twelve_minutes_and_then_each_twelve(tmp: Path):
    """11:59 — рано, 12:00 — снимок, 24:00 — второй; снимок не плодится каждые 30 секунд."""
    NO = _no()
    project = _project(tmp)
    run_dir = project / ".aurora" / "runs" / "r1"
    now = 1_800_000_000.0
    job = {"id": "j1", "run_id": "r1", "project": str(project), "run_dir": str(run_dir),
           "cmd": "kb:map", "parent": "", "started": now - 11 * 60 - 59, "done": False,
           "net_long": 0}
    with _with_env(_env(tmp)):
        obs = NO.Observer(lambda: [job], clock=lambda: now)
        assert obs.tick(now) == 0
        job["started"] = now - 12 * 60
        assert obs.tick(now) == 1
        assert obs.tick(now + 30) == 0, "снимок повторяется раньше 12 минут"
        job["started"] = now - 24 * 60
        assert obs.tick(now) == 1
        data = json.loads((run_dir / "net.json").read_text(encoding="utf-8"))
        assert [s["scope"] for s in data["snapshots"]].count("long") == 2, data["snapshots"]


@test
def test_run_report_reads_local_and_shared_snapshots(tmp: Path):
    """Отчёт читает снимки и показывает длительность и снимки, не трогая сеть."""
    NO = _no()
    project = _project(tmp)
    run_dir = project / ".aurora" / "runs" / "r1"
    with _with_env(_env(tmp)):
        NO.start("job", str(project), "r1", str(run_dir), "agent:distill", "")
        NO.long_snapshot("job", str(project), "r1", str(run_dir), "agent:distill", "", 720)
        NO.finish("job", str(project), "r1", str(run_dir), "agent:distill", "",
                  started=time.time() - 725)
        cp = subprocess.run([sys.executable, str(SCRIPTS / "net_report.py"),
                             "--project", str(project), "--run", "r1"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        assert cp.returncode == 0, cp.stderr
        assert "Сеть прогона r1" in cp.stdout and "снимков" in cp.stdout, cp.stdout
        assert "- длительность: 12 мин 05 с (+ 0 с ожидания)" in cp.stdout, cp.stdout
        # общий файл для верхнего уровня существует и читается тем же отчётом
        shared = NO.shared_path(str(project), "r1")
        assert shared and shared.is_file(), shared
        data = json.loads(shared.read_text(encoding="utf-8"))
        assert data["summary"]["snapshots"] >= 3, data


@test
def test_connectivity_event_journal_keeps_transitions(tmp: Path):
    """Переходы профиля сохраняются в отдельном JSONL, а не теряются при обрезке состояния."""
    CN = importlib.import_module("connectivity")
    path = tmp / "rec" / "events.jsonl"
    with _with_env({"AURORA_CONNECTIVITY_EVENTS": str(path)}):
        CN.append_events([{"at": "2026-11-17T00:00:00Z", "scope": "profile",
                           "name": "VPN-1", "from": "online", "to": "offline"}])
        CN.append_events([{"at": "2026-11-17T01:00:00Z", "scope": "global",
                           "name": "kit", "from": "no_vpn", "to": "online"}])
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and lines[0]["name"] == "VPN-1" and lines[1]["to"] == "online", lines
