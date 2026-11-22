"""Проверки части 257: старые записи, уборка `timing.d` и документация (S6-LEGACY).

Здесь сходятся требования этапа 6: старая запись без снимков сети говорит
`простой: нет данных`, запись со снимками даёт оценку с `~`, уборка не удаляет
свежие записи, `timing.d` на 1000 запусков остаётся меньше 20 МБ, а документы
называют те же поля и пределы, что и код. Сеть и панель не поднимаются.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test

for _path in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _timing():
    return importlib.import_module("timing")


def _cockpit():
    return importlib.import_module("aurora_cockpit")


def _net_report():
    return importlib.import_module("net_report")


def _iso(days_ago: float = 0.0) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _put_meta(root: Path, rid: str, **meta) -> Path:
    base = root / ".aurora" / "runs" / rid
    base.mkdir(parents=True, exist_ok=True)
    (base / "console.log").write_text("вывод\n", encoding="utf-8")
    path = base / "meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return path


def _record_payload(root: Path, run: str, at: str, services: dict | None = None) -> dict:
    return {
        "schema": 1, "run": run, "record_id": "", "at": at,
        "project": root.name, "kind": "route", "task": "update",
        "timing": {
            "v": 1, "scope": "run", "queue_ms": 120000, "active_ms": 630000,
            "total_ms": 750000, "work_ms": 450000, "service_wait_ms": 180000,
            "other_wait_ms": 0, "unknown_ms": 0, "coverage": 1.0,
            "source": "spans", "confidence": "exact",
            "quality": {"wait_known": True, "approx": False, "legacy": False,
                        "queue_separated": True},
        },
        "services": services or {"llm": {"calls": 1}},
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _timing_record(root: Path, part: str, name: str, at: str,
                   services: dict | None = None) -> Path:
    path = root / "AuroraKnowledgeDB" / "meta" / "timing.d" / part / name
    _write_json(path, _record_payload(root, Path(name).stem, at, services))
    return path


def _no_dash(text: str) -> None:
    assert "\u2014" not in text, "длинное тире в новом тексте"
    assert "\u2013" not in text, "среднее тире в новом тексте"


@test
def test_s6_legacy_old_meta_without_net_is_unknown(tmp: Path):
    """Старый meta без снимков: работа и ожидание неизвестны, строка говорит словами."""
    T = _timing()
    CK = _cockpit()
    root = make_project(tmp)
    rid = "20261015-101500-ab12cd-route"
    path = _put_meta(
        root, rid, kind="route", id=rid, title="Обновить базу",
        started="2026-10-15T10:15:00Z", finished="2026-10-15T13:15:00Z",
        seconds=10800, status="passed", who="analyst", kit="1.231.14",
        trigger="button", write="project", rc=0)
    before = path.read_text(encoding="utf-8")

    row = next(item for item in CK.history(str(root)) if item["id"] == rid)
    assert row["seconds"] == 10800, row
    assert row["status"] == "passed" and row["rc"] == 0, row
    assert row["who"] == "analyst" and row["kit"] == "1.231.14", row
    assert row["trigger"] == "button" and row["write"] == "project", row
    timing = row["timing"]
    assert isinstance(timing, dict), row
    assert timing["work_ms"] is None and timing["service_wait_ms"] is None, timing
    assert timing["source"] == "legacy" and timing["confidence"] == "unknown", timing
    assert timing["quality"]["wait_known"] is False, timing
    assert T.format_work_wait(timing) == "3 ч (простой: нет данных)"
    assert "0 с (простой" not in T.format_work_wait(timing)

    got = CK.history_run(str(root), rid)
    assert got["meta"]["timing"]["source"] == "legacy", got
    assert got["meta"]["timing"]["quality"]["wait_known"] is False, got
    assert path.read_text(encoding="utf-8") == before, "history_run переписала meta.json"

    assert CK._timing_of({"seconds": 3600, "net": {"snapshots": 0, "blocked_s": 7200}})[
        "source"] == "legacy"
    assert CK._timing_of({"seconds": 60, "net": "queue"})["confidence"] == "unknown"
    assert CK._timing_of({"seconds": None, "net": {"snapshots": 1, "blocked_s": 30}})[
        "work_ms"] is None
    assert CK._timing_of({}) is None


@test
def test_s6_legacy_old_meta_with_net_is_approx(tmp: Path):
    """Старый meta со снимками: производная оценка с `~` и без выдуманной точности."""
    T = _timing()
    CK = _cockpit()
    root = make_project(tmp)
    rid = "20261016-090000Z-aaaaaa"
    path = _put_meta(
        root, rid, kind="command", id=rid, title="agent:distill",
        started="2026-10-16T09:00:00Z", finished="2026-10-16T14:00:00Z",
        seconds=18000, status="done", rc=0,
        net={"seconds": 18000, "blocked_s": 7200, "snapshots": 3})
    before = path.read_text(encoding="utf-8")

    row = next(item for item in CK.history(str(root)) if item["id"] == rid)
    timing = row["timing"]
    assert timing["work_ms"] == 10_800_000, timing
    assert timing["service_wait_ms"] == 7_200_000, timing
    assert timing["source"] == "net_snapshots", timing
    assert timing["confidence"] == "approx", timing
    assert timing["quality"]["wait_known"] is True, timing
    assert timing["quality"]["approx"] is True, timing
    assert T.format_work_wait(timing) == "3 ч (+ ~2 ч ожидания)"
    assert path.read_text(encoding="utf-8") == before, "history переписала meta.json"

    got = CK.history_run(str(root), rid)
    assert got["meta"]["timing"]["service_wait_ms"] == 7_200_000, got
    assert path.read_text(encoding="utf-8") == before, "history_run переписала meta.json"

    nested = CK._timing_of({"seconds": 600, "net": {"summary": {"snapshots": 1,
                                                               "blocked_s": 60}}})
    assert nested["service_wait_ms"] == 60_000 and nested["work_ms"] == 540_000, nested

    clamped = CK._timing_of({"seconds": 60, "net": {"snapshots": 1, "blocked_s": 120}})
    assert clamped["work_ms"] == 0 and clamped["service_wait_ms"] == 60_000, clamped
    assert clamped["quality"]["clamped"] is True, clamped
    assert not T.timing_problems(clamped)


@test
def test_s6_legacy_clamps_other_and_marks_truncated_service(tmp: Path):
    """Старая запись: other_ms не больше active, обрезанный service_ms не exact."""
    T = _timing()

    other_only = T.legacy_timing(60, None, other_ms=120_000)
    assert other_only["active_ms"] == 60_000, other_only
    assert other_only["other_wait_ms"] == 60_000, other_only
    assert other_only["work_ms"] is None and other_only["service_wait_ms"] is None, other_only
    assert other_only["confidence"] == "unknown", other_only
    assert other_only["quality"]["clamped"] is True, other_only
    assert not T.timing_problems(other_only)

    service_only = T.legacy_timing(60, None, service_ms=120_000)
    assert service_only["active_ms"] == 60_000, service_only
    assert service_only["service_wait_ms"] == 60_000, service_only
    assert service_only["work_ms"] == 0 and service_only["other_wait_ms"] == 0, service_only
    assert service_only["confidence"] == "approx", service_only
    assert service_only["quality"]["approx"] is True, service_only
    assert service_only["quality"]["clamped"] is True, service_only
    assert not T.timing_problems(service_only)

    both = T.legacy_timing(60, None, service_ms=30_000, other_ms=60_000)
    assert both["service_wait_ms"] == 30_000 and both["other_wait_ms"] == 30_000, both
    assert both["work_ms"] == 0, both
    assert both["confidence"] == "approx", both
    assert both["quality"]["clamped"] is True, both
    assert not T.timing_problems(both)


@test
def test_s6_legacy_net_report_shows_two_numbers_and_estimate(tmp: Path):
    """`net:report` различает отсутствие данных, оценку по снимкам и точную сводку."""
    NR = _net_report()
    with_net = {
        "id": "r1", "cmd": "agent:distill",
        "summary": {"seconds": 18000, "snapshots": 3, "blocked_s": 7200,
                    "network_required": True, "reason": "network"},
    }
    text = NR._show(with_net)
    assert "- длительность: 3 ч (+ ~2 ч ожидания)" in text, text
    assert "- ожидание: оценка по снимкам сети" in text, text

    without = NR._show({"id": "r2", "summary": {"seconds": 10800, "snapshots": 0,
                                                "blocked_s": 0}})
    assert "- длительность: 3 ч (простой: нет данных)" in without, without
    assert "0 с (простой" not in without, without

    empty = NR._show({"id": "empty"})
    assert "- длительность: нет данных" in empty, empty

    fallback = NR._timing_of({"seconds": 18000}, None,
                             {"snapshots": 3, "blocked_s": 3600})
    assert fallback["service_wait_ms"] == 3_600_000, fallback
    from_meta = NR._timing_of({"seconds": 3600,
                               "net": {"snapshots": 2, "blocked_s": 600}},
                              None, {})
    assert from_meta["service_wait_ms"] == 600_000, from_meta
    no_snaps = NR._timing_of({"seconds": 3600}, None,
                             {"snapshots": 0, "blocked_s": 7200})
    assert no_snaps["source"] == "legacy" and no_snaps["work_ms"] is None, no_snaps
    broken = NR._timing_of({"seconds": 60, "net": "queue"}, None, {})
    assert broken["confidence"] == "unknown", broken


@test
def test_s6_legacy_list_reports_keeps_200_limit_and_net_runs(tmp: Path):
    """`net-runs.d` только читается, а предел `list_reports` остаётся 200."""
    NR = _net_report()
    root = make_project(tmp)
    folder = root / "AuroraKnowledgeDB" / "meta" / "net-runs.d" / "alice-m1"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(205):
        at = (datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
              + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
        _write_json(folder / f"r{index:03d}.json", {
            "id": f"r{index:03d}", "cmd": "agent:distill",
            "snapshots": [{"at": at}],
            "summary": {"seconds": 10, "snapshots": 1, "blocked_s": 0,
                        "network_required": False, "reason": "ok"},
        })
    before = {path.name: path.read_bytes() for path in folder.glob("*.json")}
    assert inspect.signature(NR.list_reports).parameters["limit"].default == 200

    reports = NR.list_reports([str(root)])
    assert len(reports) == 200, len(reports)
    assert len(reports) == len(NR.list_reports([str(root)], limit=200))
    after = {path.name: path.read_bytes() for path in folder.glob("*.json")}
    assert after == before, "list_reports переписала net-runs.d"


@test
def test_s6_legacy_prune_keeps_fresh_and_deletes_old(tmp: Path):
    """Уборка `timing.d` держит 90 дней и не удаляет запись со свежим `at`."""
    T = _timing()
    root = make_project(tmp)
    folder = root / "AuroraKnowledgeDB" / "meta" / "timing.d" / "u-m"
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()

    def make(name: str, at: str | None, mtime_days: float) -> Path:
        data = _record_payload(root, name, at or "")
        if at is None:
            data.pop("at", None)
        path = folder / f"{name}.json"
        _write_json(path, data)
        stamp = now - mtime_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    kept_recent = make("kept-recent", _iso(1), 200)
    kept_boundary = make("kept-boundary", _iso(89), 200)
    deleted_old = make("deleted-old", _iso(91), 200)
    deleted_without_at = make("deleted-without-at", None, 200)
    kept_old_at_fresh_file = make("kept-old-at-fresh-file", _iso(100), 1)
    kept_fresh_file = make("kept-fresh-file", _iso(1), 1)
    assert T.KEEP_DAYS == 90

    gone = T.prune(str(root))
    assert gone == 2, gone
    assert kept_recent.exists() and kept_boundary.exists(), "уборка удалила свежие записи"
    assert kept_old_at_fresh_file.exists() and kept_fresh_file.exists()
    assert not deleted_old.exists() and not deleted_without_at.exists()


@test
def test_s6_legacy_timing_d_1000_records_stay_small(tmp: Path):
    """1000 нормализованных записей `timing.d` занимают меньше 20 МБ."""
    T = _timing()
    root = make_project(tmp)
    services = T.service_metrics([
        {"kind": "call", "service": "llm", "status": "ok", "dur_ms": 4200,
         "metrics": {"latency_ms": 4200, "ttft_ms": 500, "tokens_in": 1200,
                     "tokens_out": 800, "tps_basis": "stream"}},
        {"kind": "call", "service": "confluence", "status": "ok", "dur_ms": 900,
         "metrics": {"latency_ms": 900, "bytes": 5400, "bytes_basis": "response"}},
    ])
    folder = root / "AuroraKnowledgeDB" / "meta" / "timing.d" / "u-m"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        row = _record_payload(root, f"run-{index:04d}", _iso((index % 80) + 1), services)
        _write_json(folder / f"run-{index:04d}.json", row)
    paths = sorted(folder.glob("*.json"))
    total = sum(path.stat().st_size for path in paths)
    assert len(paths) == 1000, len(paths)
    assert total <= 20 * 1024 * 1024, total
    assert total / len(paths) <= 20 * 1024, total / len(paths)
    assert len(T.iter_records([str(root)], limit=200)) == 200


@test
def test_s6_legacy_duplicate_records_keep_newest_once(tmp: Path):
    """Повтор ключа `(проект, run, record_id)` показывается одной свежей записью."""
    T = _timing()
    root = make_project(tmp)
    older = _timing_record(root, "a-m", "run-1.json", "2026-11-21T10:00:00Z",
                           {"llm": {"calls": 1}})
    newer = _timing_record(root, "b-m", "run-1.json", "2026-11-21T10:00:01Z",
                           {"llm": {"calls": 7}})
    assert older.exists() and newer.exists()
    rows = [item for item in T.iter_records([str(root)]) if item["run"] == "run-1"]
    assert len(rows) == 1, rows
    assert rows[0]["services"]["llm"]["calls"] == 7, rows


@test
def test_s6_legacy_spans_limit_and_record_run_read_full_file(tmp: Path):
    """Предел памяти 5000 спанов не обрезает `record_run`; 50 запусков остаются."""
    T = _timing()
    CK = _cockpit()
    assert T.SPAN_LIMIT == 5000 and CK.RUNS_KEEP == 50
    root = make_project(tmp)
    run_dir = root / "runs" / "r-257"
    run_dir.mkdir(parents=True)
    lines = []
    for index in range(5001):
        lines.append(json.dumps({
            "v": 1, "type": "span", "run_id": "r-257", "span_id": f"s{index}",
            "kind": "wait", "service": "llm", "start": "2026-11-21T10:00:00.000Z",
            "end": "2026-11-21T10:00:01.000Z", "dur_ms": 1000.0,
            "status": "ok", "source": "live", "confidence": "exact",
        }))
    lines.append("битая строка")
    (run_dir / "timing.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert len(T.read_spans(str(run_dir))) == 5001
    assert (run_dir / "timing.jsonl").stat().st_size <= 2 * 1024 * 1024
    path = T.record_run(str(root), "r-257", str(run_dir), kind="job", task="sync",
                        actor_id="u", machine="m")
    assert path and Path(path).is_file(), path
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["run"] == "r-257" and data["kind"] == "job", data
    assert not data["timing"]["quality"].get("truncated"), data["timing"]["quality"]


@test
def test_s6_legacy_docs_match_contract_and_api(tmp: Path):
    """Документы называют те же поля, пределы и API, что и код."""
    T = _timing()
    CK = _cockpit()
    NR = _net_report()
    connectivity = (KIT / "docs/connectivity.md").read_text(encoding="utf-8")
    architecture = (KIT / "docs/architecture.md").read_text(encoding="utf-8")
    commands = (KIT / "docs/commands.md").read_text(encoding="utf-8")
    changelog = (KIT / "CHANGELOG.md").read_text(encoding="utf-8")

    conn = connectivity.split("### Работа и ожидание в отчётах", 1)[1].split(
        "## Приложение", 1)[0]
    for word in ("`work`", "`service_wait`", "`other_wait`", "`queue`", "`unknown`",
                 "+ 0 с", "параллель", "простой: нет данных", "timing.jsonl",
                 "timing.d", "90 дней"):
        assert word in conn, f"connectivity: нет «{word}»"
    _no_dash(conn)

    arch = architecture.split("### След замера", 1)[1].split("### Роли", 1)[0]
    for word in ("timing.jsonl", "timing.d", "5000", "90 дней", "50 последних",
                 "200", "window_metrics", "legacy_timing", "null", "net-runs.d",
                 "/api/history", "/api/metrics"):
        assert word in arch, f"architecture: нет «{word}»"
    _no_dash(arch)

    cmd = commands.split("## Время в выводе команд", 1)[1].split("## `kit:", 1)[0]
    for word in ("agent:*", "net:report", "Работа и ожидание", "timing.d",
                 "простой: нет данных", "~"):
        assert word in cmd, f"commands: нет «{word}»"
    _no_dash(cmd)

    assert "- **Шестой этап: старые записи говорят честно.**" in changelog
    block = changelog.split("- **Шестой этап: старые записи говорят честно.**", 1)[1].split(
        "- **Проверки:**", 1)[0]
    assert "оценка" in block and "~" in block and "нет данных" in block, block
    assert "точность" not in block and "точно" not in block, "CHANGELOG обещает точность"
    _no_dash(block)

    assert T.KEEP_DAYS == 90 and T.SPAN_LIMIT == 5000
    assert CK.RUNS_KEEP == 50
    assert inspect.signature(NR.list_reports).parameters["limit"].default == 200
    cockpit_src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for endpoint in ("/api/history", "/api/history/run", "/api/metrics"):
        assert f'"{endpoint}"' in cockpit_src, endpoint
        assert endpoint in connectivity or endpoint in architecture, endpoint
