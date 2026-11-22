"""Проверки части 256: две цифры и единый формат на оставшихся поверхностях (S5).

Здесь сходятся журнал запусков, итог прогона, сводка диагностики, чип времени и
производная работа шага расписания. Сеть и браузер не поднимаются: Python-часть
проверяется на временном проекте, JavaScript-часть под node с подставным DOM.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run_node, test


def _module(path, name):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import importlib
    return importlib.import_module(name)


def _cockpit():
    return _module(KIT / "cockpit", "aurora_cockpit")


def _summary():
    return _module(SCRIPTS, "run_summary")


def _diagnostics():
    return _module(SCRIPTS, "diagnostics")


def _cells(line: str) -> list:
    return [x.strip() for x in line.strip().strip("|").split("|")]


def _log(project: Path) -> list:
    return (project / "AuroraKnowledgeDB" / "meta" / "run_log.md").read_text(
        encoding="utf-8").splitlines()


def _timing(work: int = 600_000, wait: int = 120_000, approx: bool = False) -> dict:
    return {
        "v": 1, "work_ms": work, "service_wait_ms": wait,
        "queue_ms": 0, "active_ms": work + wait, "total_ms": work + wait,
        "source": "spans", "confidence": "approx" if approx else "exact",
        "quality": {"wait_known": wait is not None, "approx": approx,
                    "legacy": False, "queue_separated": True},
    }


@test
def test_s5_surfaces_runlog_writes_nine_columns_and_keeps_seven(tmp: Path):
    """Новый прогон пишет работу и простой, прогон без timing остаётся семиколоночным."""
    ck = _cockpit()
    root = make_project(tmp)
    ck.write_runlog(str(root), "sync:jira", 0, "sync:jira --force", 754,
                    timing=_timing())
    rows = _log(root)
    line = next(x for x in rows if x.startswith("| sync:jira |"))
    cells = _cells(line)
    assert len(cells) == 9, cells
    assert cells[6] == "754" and cells[7] == "600000" and cells[8] == "120000", cells

    got = ck.read_runlog(str(root))["sync:jira"]
    assert got["secs"] == 754, got
    assert got["timing"]["work_ms"] == 600_000, got
    assert got["timing"]["service_wait_ms"] == 120_000, got
    assert got["timing"]["quality"]["wait_known"] is True, got

    ck.write_runlog(str(root), "kit:doctor", 1, "kit:doctor", 3)
    rows = _log(root)
    doctor = next(x for x in rows if x.startswith("| kit:doctor |"))
    assert len(_cells(doctor)) == 7, doctor
    old = next(x for x in rows if x.startswith("| sync:jira |"))
    assert len(_cells(old)) == 9, "повторная запись потеряла работу и простой"


@test
def test_s5_surfaces_runlog_reads_old_six_seven_and_nine_columns(tmp: Path):
    """Старые шесть и семь колонок читаются, пустые новые ячейки не становятся нулём."""
    ck = _cockpit()
    root = make_project(tmp)
    path = root / "AuroraKnowledgeDB" / "meta" / "run_log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "| Команда | Когда (UTC) | Код | Ядро | Кто | Строка запуска | Секунд | Работа (мс) | Простой (мс) |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        "| old6 | 2026-11-21T10:00:00Z | 0 | 1.0 | u | old6 |\n"
        "| old7 | 2026-11-21T10:01:00Z | 1 | 1.0 | u | old7 | 754 |\n"
        "| new9 | 2026-11-21T10:02:00Z | 0 | 1.0 | u | new9 | 30 | 5000 | ~3000 |\n"
        "| unknown9 | 2026-11-21T10:03:00Z | 0 | 1.0 | u | unknown9 | 30 |  |  |\n",
        encoding="utf-8")
    runs = ck.read_runlog(str(root))
    assert runs["old6"]["secs"] == 0 and "timing" not in runs["old6"], runs["old6"]
    assert runs["old7"]["secs"] == 754 and "timing" not in runs["old7"], runs["old7"]
    assert runs["new9"]["timing"]["work_ms"] == 5000, runs["new9"]
    assert runs["new9"]["timing"]["service_wait_ms"] == 3000, runs["new9"]
    assert runs["new9"]["timing"]["quality"]["approx"] is True, runs["new9"]
    assert runs["unknown9"]["timing"]["work_ms"] is None, runs["unknown9"]
    assert runs["unknown9"]["timing"]["service_wait_ms"] is None, runs["unknown9"]
    assert runs["unknown9"]["timing"]["quality"]["wait_known"] is False, runs["unknown9"]
    assert runs["unknown9"]["timing"]["confidence"] == "unknown", runs["unknown9"]


@test
def test_s5_surfaces_summary_delegates_and_keeps_old_wording(tmp: Path):
    """Старая запись печатает прежнее «Время», новая делегирует общему форматтеру."""
    rs = _summary()
    assert rs.human_time(3600) == "1 ч 0 мин"
    assert rs.human_time(3600, timing={"total_ms": 3_600_000}) == "1 ч"

    old = rs.from_agent([], {}, 720.0, None)
    old_text = "\n".join(rs.render(old))
    assert "Время: 12 мин 0 с" in old_text, old_text
    assert "Работа и ожидание" not in old_text, old_text

    new = rs.from_agent([], {}, 720.0, None, timing=_timing())
    text = "\n".join(rs.render(new))
    assert "Время: 12 мин" in text, text
    assert "Работа и ожидание: 10 мин (+ 2 мин ожидания)" in text, text


@test
def test_s5_surfaces_diagnostics_prints_two_numbers_without_secrets(tmp: Path):
    """Сводка диагностики получает строку работы и простоя и не печатает лишних данных."""
    diag = _diagnostics()
    timing = dict(_timing(), prompt="SECRET-PROMPT", url="https://example.com/secret")
    text = diag._summary({"generated": "2026-11-21T10:00:00Z",
                          "run": {"run_id": "run-1", "timing": timing}})
    assert "Работа и ожидание: 10 мин (+ 2 мин ожидания)" in text, text
    assert "SECRET-PROMPT" not in text and "example.com" not in text, text
    assert text.index("Работа и ожидание") > text.index("Журнал адаптера:"), text
    assert text.index("Работа и ожидание") < text.index("Тревога адаптера:"), text


@test
def test_s5_surfaces_panel_chip_is_focusable_and_ids_are_unique(tmp: Path):
    """fmtDur делегирует fmtTiming, а два чипа с одним run.id не делят aria-describedby."""
    node = shutil.which("node")
    if not node:
        return
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    start = js.index("function fmtDur(sec){")
    end = js.index("// «Когда» архивного прогона", start)
    code = js[start:end]
    strings = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    harness = (
        "const S = " + json.dumps(strings, ensure_ascii=False) + ";\n"
        "const t = (key, vars={}) => { const s = S[key]; if (s == null) throw new Error(key);\n"
        "  return s.replace(/\\{(\\w+)\\}/g, (_, k) => String(vars[k] ?? '')); };\n"
        "function el(tag, attrs, ...kids){ return {tag, attrs: attrs || {}, kids, style: {},"
        " hidden: false, addEventListener(){},"
        " getBoundingClientRect(){ return {bottom:0, top:0, left:0, width:0, height:0}; },"
        " append(...x){ this.kids.push(...x); }}; }\n"
        "globalThis.innerHeight = 800; globalThis.innerWidth = 1200;\n"
        "globalThis.document = {createTextNode: (x) => ({text: x})};\n"
        "const f = new Function('t','el','document','setTimeout','clearTimeout',"
        "'innerHeight','innerWidth', " + json.dumps(code) + " + "
        "'\\nreturn {fmtDur, fmtTiming, timingChip};');\n"
        "const api = f(t, el, document, setTimeout, clearTimeout, 800, 1200);\n"
        "const a = api.timingChip({id:'same', seconds:90, timing:"
        "{work_ms:60000, service_wait_ms:30000, quality:{wait_known:true}}});\n"
        "const b = api.timingChip({id:'same', seconds:90});\n"
        "const ca = a.kids[0], cb = b.kids[0];\n"
        "console.log(JSON.stringify({dur: api.fmtDur(3600),"
        " durMs: api.fmtTiming(3600000), a: ca.attrs['aria-describedby'],"
        " b: cb.attrs['aria-describedby'], role: ca.attrs.role,"
        " tabindex: ca.attrs.tabindex, label: ca.attrs['aria-label'],"
        " title: ca.attrs.title, text: ca.kids[0], legacy: cb.kids[0]}));"
    )
    cp = run_node(tmp, harness, name="panel_chip.mjs")
    assert cp is not None, "node not found"
    assert cp.returncode == 0, cp.stderr[:800]
    got = json.loads(cp.stdout)
    assert got["dur"] == "1 ч" and got["durMs"] == "1 ч", got
    assert got["a"] != got["b"], got
    assert got["role"] == "button" and got["tabindex"] == "0", got
    assert "Работа: 1 мин" in got["label"] and "Простой" in got["label"], got
    assert "\n" in got["title"] and "Работа" in got["title"], got
    assert got["text"] == "1 мин (+ 30 с ожидания)", got
    assert got["legacy"] == "1 мин 30 с (простой: нет данных)", got

    css = (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")
    assert ".timing-chip:focus-visible" in css, "у чипа нет focus-visible"
    assert "prefers-reduced-motion" in css, "движение не отключается настройкой системы"


@test
def test_s5_surfaces_cron_item_derives_work_and_marks_it_approximate(tmp: Path):
    """Шаг цепочки с одними ожиданиями получает производную работу с пометкой оценки."""
    node = shutil.which("node")
    if not node:
        return
    view = KIT / "cockpit/modules/cron/view.js"
    harness = (
        "const m = await import(" + json.dumps(view.as_uri()) + ");\n"
        "const derived = m.itemTiming({started:'2026-11-21T10:00:00',"
        " finished:'2026-11-21T10:10:00',"
        " timing:{v:1, queue_ms:0, other_wait_ms:120000}});\n"
        "const exact = m.itemTiming({started:'2026-11-21T10:00:00',"
        " finished:'2026-11-21T10:10:00',"
        " timing:{v:1, work_ms:300000, service_wait_ms:60000,"
        " quality:{wait_known:true}}});\n"
        "const unknown = m.itemTiming({timing:{v:1, other_wait_ms:5000}});\n"
        "console.log(JSON.stringify({derived, exact, unknown}));\n"
    )
    path = tmp / "cron_timing.mjs"
    path.write_text(harness, encoding="utf-8")
    cp = subprocess.run([node, str(path)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[:800]
    got = json.loads(cp.stdout)
    derived = got["derived"]
    assert derived["work_ms"] == 480_000, derived
    assert derived["source"] == "derived" and derived["confidence"] == "approx", derived
    assert derived["quality"]["approx"] is True, derived
    assert got["exact"]["work_ms"] == 300_000 and "source" not in got["exact"], got
    assert got["unknown"].get("work_ms") is None, got["unknown"]


@test
def test_s5_surfaces_modules_share_one_chip_and_one_formatter(tmp: Path):
    """Модули добавляют общий чип, не копируя aria и не склеивая его с текстом."""
    modules = {
        "ask": "cockpit/modules/ask/view.js",
        "cron": "cockpit/modules/cron/view.js",
        "bots": "cockpit/modules/bots/view.js",
        "commands": "cockpit/modules/commands/view.js",
    }
    for name, rel in modules.items():
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "ctx.ui.timingChip" in text, f"{name}: не общий чип ядра"
        assert "aria-describedby" not in text, f"{name}: копия aria-разметки"
        assert "tabindex" not in text, f"{name}: копия tabindex"
    ask = (KIT / modules["ask"]).read_text(encoding="utf-8")
    assert "ctx.fmt.workWait" not in ask, "ask не должен дублировать форматтер"
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    assert "timingChip,\n" in panel or "timingChip," in panel, "ctx.ui без timingChip"
    assert "workWait: fmtWorkWait, timingTip" in panel, "ctx.fmt без прежних имён"
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    start = ck.index("def start_job(")
    seg = ck[start:ck.index("def launch(", start)]
    assert seg.index('meta["timing"]') < seg.index("write_runlog(project"), \
        "write_runlog вызывается до расчёта timing"
    assert 'timing=meta.get("timing") if meta else None' in seg, \
        "write_runlog не получает timing"
    for lang in ("ru", "en"):
        strings = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        for key in ("dur.work_wait", "dur.work_wait_zero", "dur.work_wait_approx",
                    "dur.work_wait_unknown", "dur.wait_unknown", "timing.unknown"):
            value = strings[key]
            assert "\u2014" not in value and "\u2013" not in value, (lang, key, value)
