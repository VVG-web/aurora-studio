"""Проверки части 246: единый учёт работы и ожидания (timing v1).

Каркас — tests/harness.py. Панель и сеть не поднимаются: проверяются чистый модуль
`scripts/timing.py`, производная сводка `history`/`history_run`, строка `net:report`
и исходник панели с доступной подсказкой.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))


def _timing():
    return importlib.import_module("timing")


def _cockpit():
    p = str(KIT / "cockpit")
    if p not in sys.path:
        sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _project(tmp: Path) -> Path:
    root = tmp / "проект"
    (root / ".aurora" / "runs").mkdir(parents=True)
    return root


def _put_meta(root: Path, rid: str, **meta) -> Path:
    base = root / ".aurora" / "runs" / rid
    base.mkdir(parents=True)
    (base / "console.log").write_text("вывод\n", encoding="utf-8")
    path = base / "meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return path


@test
def test_legacy_timing_without_net_is_unknown(tmp: Path):
    """Запись без net: работа и ожидание неизвестны, ноль не выдумывается."""
    T = _timing()
    timing = T.legacy_timing(10800, None)
    assert timing["active_ms"] == 10_800_000 and timing["total_ms"] == 10_800_000
    assert timing["work_ms"] is None and timing["service_wait_ms"] is None
    assert timing["unknown_ms"] is None and timing["source"] == "legacy"
    assert timing["confidence"] == "unknown"
    assert timing["quality"]["wait_known"] is False
    assert not T.timing_problems(timing)


@test
def test_legacy_timing_without_snapshots_is_unknown(tmp: Path):
    """Сводка без снимков — та же неизвестность, даже если blocked_s случайно заполнен."""
    T = _timing()
    timing = T.legacy_timing(3600, {"snapshots": 0, "blocked_s": 7200})
    assert timing["work_ms"] is None and timing["service_wait_ms"] is None
    assert timing["source"] == "legacy" and timing["coverage"] == 0.0
    assert timing["quality"]["wait_known"] is False


@test
def test_legacy_timing_without_seconds_is_unknown(tmp: Path):
    """Нет seconds: длительность и ожидание неизвестны, ноль не выдаётся за измерение."""
    T = _timing()
    timing = T.legacy_timing(None, {"snapshots": 1, "blocked_s": 30})
    assert timing["active_ms"] is None and timing["total_ms"] is None
    assert timing["work_ms"] is None and timing["service_wait_ms"] is None
    assert timing["unknown_ms"] is None and timing["coverage"] == 0.0
    assert timing["source"] == "legacy" and timing["confidence"] == "unknown"
    assert timing["quality"]["wait_known"] is False
    assert "clamped" not in timing["quality"]
    assert T.format_work_wait(timing) == "нет данных"
    assert T.timing_problems(timing) == []


@test
def test_legacy_timing_with_net_is_approx(tmp: Path):
    """Снимки дают две цифры: ожидание сети приблизительное и помечено approx."""
    T = _timing()
    timing = T.legacy_timing(18000, {"snapshots": 4, "blocked_s": 7200})
    assert timing["work_ms"] == 10_800_000
    assert timing["service_wait_ms"] == 7_200_000
    assert timing["wait_by_service"] == {"network": 7_200_000}
    assert timing["wait_by_cause"] == {"network": 7_200_000}
    assert timing["source"] == "net_snapshots" and timing["confidence"] == "approx"
    assert timing["quality"]["wait_known"] is True and timing["quality"]["approx"] is True
    assert not timing["quality"]["clamped"] and not T.timing_problems(timing)


@test
def test_legacy_timing_clamps_blocked(tmp: Path):
    """blocked_s > seconds не даёт отрицательной работы и помечает клампинг."""
    T = _timing()
    timing = T.legacy_timing(60, {"snapshots": 1, "blocked_s": 120})
    assert timing["work_ms"] == 0 and timing["service_wait_ms"] == 60_000
    assert timing["quality"]["clamped"] is True
    assert not T.timing_problems(timing)


@test
def test_union_and_subtract_intervals(tmp: Path):
    """Параллельное ожидание считается один раз, а работа вычитается из ожидания."""
    T = _timing()
    assert T.union_ms([(0, 8), (8, 10), (3, 6)]) == 10
    assert T.union_ms([(0, 5), (5, 10)]) == 10
    assert T.subtract_ms([(0, 10), (20, 30)], [(5, 25)]) == [(0, 5), (25, 30)]


@test
def test_timing_contract_invariants(tmp: Path):
    """Схема v1 проходит инварианты, а отрицательная работа и разрыв суммы — нет."""
    T = _timing()
    timing = T.legacy_timing(18000, {"snapshots": 2, "blocked_s": 7200})
    assert T.timing_problems(timing) == []
    bad = dict(timing, work_ms=-1)
    assert T.timing_problems(bad)
    bad = dict(timing, unknown_ms=2)
    assert any("work + wait" in p for p in T.timing_problems(bad))


@test
def test_history_row_has_timing(tmp: Path):
    """Строка истории получает timing, но seconds и meta.json на диске не меняются."""
    T = _timing()
    root = _project(tmp)
    rid = "20261121-101500-ab12cd-route"
    meta = {"kind": "route", "id": rid, "title": "Обновить базу", "started": "2026-11-21T07:15:00Z",
            "finished": "2026-11-21T09:20:00Z", "seconds": 18000, "status": "passed",
            "net": {"seconds": 18000, "blocked_s": 7200, "snapshots": 3}}
    path = _put_meta(root, rid, **meta)
    before = path.read_text(encoding="utf-8")
    rows = _cockpit().history(str(root))
    row = next(r for r in rows if r["id"] == rid)
    assert row["seconds"] == 18000
    assert T.format_work_wait(row["timing"]) == "3 ч (+ ~2 ч ожидания)"
    assert path.read_text(encoding="utf-8") == before, "history переписала meta.json"


@test
def test_history_run_derives_without_rewriting(tmp: Path):
    """history_run добавляет производный timing в ответ, файл на диске не меняется."""
    root = _project(tmp)
    rid = "20261121-101500-ab12cd-command"
    path = _put_meta(root, rid, kind="command", id=rid, title="Обновить", seconds=3600,
                     status="done", rc=0)
    before = path.read_text(encoding="utf-8")
    got = _cockpit().history_run(str(root), rid)
    assert got["meta"]["seconds"] == 3600
    assert got["meta"]["timing"]["work_ms"] is None
    assert got["meta"]["timing"]["source"] == "legacy"
    assert path.read_text(encoding="utf-8") == before, "history_run переписала meta.json"


@test
def test_net_report_two_digits(tmp: Path):
    """net:report показывает две цифры и не выдумывает простой без снимков."""
    import net_report
    data = {"id": "r1", "cmd": "agent:distill",
            "summary": {"seconds": 18000, "snapshots": 3, "blocked_s": 7200,
                        "network_required": True, "reason": "network"}}
    out = net_report._show(data)
    assert "- длительность: 3 ч (+ ~2 ч ожидания)" in out, out
    unknown = net_report._show({"id": "r2", "summary": {"seconds": 10800, "snapshots": 0,
                                                        "blocked_s": 0}})
    assert "- длительность: 3 ч (простой: нет данных)" in unknown, unknown
    empty = net_report._show({"id": "empty"})
    assert "- длительность: нет данных" in empty, empty
    assert "0 с (простой: нет данных)" not in empty, empty
    root = tmp / "net-project"
    shared = root / "AuroraKnowledgeDB/meta/net-runs.d/alice-m1"
    shared.mkdir(parents=True)
    (shared / "r1.json").write_text(json.dumps({
        "id": "r1", "cmd": "agent:distill", "snapshots": [{"at": "2026-11-21T09:20:00Z"}],
        "summary": {"seconds": 18000, "snapshots": 3, "blocked_s": 7200,
                    "network_required": True, "reason": "network"}}), encoding="utf-8")
    reports = net_report.list_reports([str(root)])
    assert reports and reports[0]["seconds"] == 18000
    assert reports[0]["timing"]["service_wait_ms"] == 7_200_000


@test
def test_format_work_wait_ru(tmp: Path):
    """Русские формы: часы, минуты, приблизительность и отсутствие данных."""
    T = _timing()
    known = T.legacy_timing(18000, {"snapshots": 1, "blocked_s": 7200})
    assert T.format_work_wait(known) == "3 ч (+ ~2 ч ожидания)"
    assert T.format_work_wait(known, style="full") == "3 часа (+ примерно 2 часа ожидания)"
    assert T.format_work_wait(T.legacy_timing(10800, None)) == "3 ч (простой: нет данных)"
    assert T.format_work_wait(T.legacy_timing(18000, {"snapshots": 1, "blocked_s": 0})) == \
        "5 ч (+ 0 с ожидания)"
    assert T.format_duration(400) == "0 с"
    assert T.format_duration(185000) == "3 мин 05 с"
    assert T.format_duration(7500000) == "2 ч 05 мин"
    assert T.format_duration(60000, style="full") == "1 минута"
    assert T.format_duration(7500000, style="full") == "2 часа 5 минут"


@test
def test_format_work_wait_en(tmp: Path):
    """Английские формы не склеивают число и существительное в неверном роде."""
    T = _timing()
    known = T.legacy_timing(18000, {"snapshots": 1, "blocked_s": 7200})
    assert T.format_work_wait(known, lang="en") == "3 h (+ ~2 h of waiting)"
    assert T.format_work_wait(known, style="full", lang="en") == "3 hours (+ approximately 2 hours of waiting)"
    assert T.format_work_wait(T.legacy_timing(10800, None), lang="en") == "3 h (downtime: no data)"
    assert T.format_duration(60000, style="full", lang="en") == "1 minute"
    assert T.format_duration(7500000, style="full", lang="en") == "2 hours 5 minutes"


@test
def test_format_duration_edges(tmp: Path):
    """Границы форматтера: нули не теряют старшую единицу, младшая не печатается зря."""
    T = _timing()
    # Вход форматтера в миллисекундах, поэтому границы заданы в секундах.
    seconds = (0, 1, 59, 60, 3599, 3600, 3661)
    assert [T.format_duration(n * 1000) for n in seconds] == [
        "0 с", "1 с", "59 с", "1 мин", "59 мин 59 с", "1 ч", "1 ч 01 мин",
    ]
    assert T.format_duration(-1000) == "0 с"
    assert T.format_duration(None) == "0 с"


@test
def test_panel_duration_omits_zero_units(tmp: Path):
    """Панель печатает 3 ч, 30 мин и 0 с без нулевых младших единиц, как Python."""
    node = shutil.which("node")
    if not node:
        return
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    start = js.index("function fmtTiming(ms){")
    end = js.index("\n// Две цифры", start)
    fn = js[start:end]
    values = [0, 1000, 59000, 60000, 1800000, 3599000, 3600000, 3660000, 3900000, 65000]
    expected = {
        "ru": ["0 с", "1 с", "59 с", "1 мин", "30 мин", "59 мин 59 с",
               "1 ч", "1 ч 01 мин", "1 ч 05 мин", "1 мин 05 с"],
        "en": ["0 s", "1 s", "59 s", "1 min", "30 min", "59 min 59 s",
               "1 h", "1 h 01 min", "1 h 05 min", "1 min 05 s"],
    }
    for lang in ("ru", "en"):
        strings = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        code = (
            "const S = " + json.dumps(strings, ensure_ascii=False) + ";\n"
            "const t = (key, vars={}) => { const s = S[key]; if (s == null) throw new Error(key);\n"
            "  return s.replace(/\\{(\\w+)\\}/g, (_, k) => String(vars[k] ?? '')); };\n"
            + fn +
            "\nconsole.log(JSON.stringify(" + json.dumps(values) + ".map(fmtTiming)));"
        )
        cp = subprocess.run([node, "-e", code], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        assert cp.returncode == 0, cp.stderr[:600]
        assert json.loads(cp.stdout) == expected[lang], (lang, cp.stdout)


@test
def test_timing_tooltip_is_focusable(tmp: Path):
    """Чип времени доступен с клавиатуры: aria, role, focus-visible, Escape и stopPropagation."""
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    css = (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")
    for token in ("function fmtWorkWait(", "function timingTip(", "function timingChip(",
                  "aria-describedby", "tabindex:\"0\"", "role:\"button\"",
                  "ev.key === \"Escape\"", "ev.key === \"Enter\"", "stopPropagation",
                  "fmt: {ago, kb, when: histWhen, esc, tick, md, mdLite, rtime, howLong,",
                  "workWait: fmtWorkWait, timingTip"):
        assert token in js, f"в panel.js нет {token}"
    assert ".timing-chip:focus-visible" in css and "--primary" in css, "у чипа нет focus-visible"
    node = shutil.which("node")
    if node:
        cp = subprocess.run([node, "--check", str(KIT / "cockpit/ui/panel.js")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cp.returncode == 0, cp.stderr[:600]


@test
def test_old_history_still_renders(tmp: Path):
    """Старая строка без timing не падает: API отдаёт timing, UI оставляет fmtDur fallback."""
    T = _timing()
    root = _project(tmp)
    rid = "20261121-101500-ab12cd-old"
    _put_meta(root, rid, kind="command", id=rid, title="Старый запуск", seconds=3600,
              status="done", rc=0)
    row = next(r for r in _cockpit().history(str(root)) if r["id"] == rid)
    assert row["timing"]["quality"]["wait_known"] is False
    assert T.format_work_wait(row["timing"]) == "1 ч (простой: нет данных)"
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    assert "if (!timing) return fallbackSec != null ? fmtDur(fallbackSec) : t(\"timing.unknown\");" in js
    assert "(r.seconds != null || r.timing) ? timingChip(r) : null" in js
