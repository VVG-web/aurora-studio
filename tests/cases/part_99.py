"""Проверки движка Aurora, часть 99: подсказка про длинные пути в начале прогона и в итоге.

Каркас и помощники — tests/harness.py. Маршрут с выгрузкой («Обновить базу» кнопкой, по расписанию
или из терминала) говорит в начале одной строкой, какие строки конфига не поместятся в путь на
Windows, macOS или Linux, и повторяет её в итоге; запуск при этом идёт — не всё будет загружено.
Строка одна на запуск, а не на шаг; в маршруте без выгрузки её нет; сбой проверки маршрут не роняет.
Процессов, сети и настоящего домашнего каталога проверки не касаются.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import sys

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

COCKPIT = KIT / "cockpit"
LONG_OUT = "/".join(["папка"] * 40)


def _mods():
    for p in (str(SCRIPTS), str(COCKPIT), str(KIT / "tests")):
        if p not in sys.path:
            sys.path.insert(0, p)
    rr = importlib.import_module("route_runner")
    pb = importlib.import_module("path_budget")
    fake = importlib.import_module("cases.part_18").FakePanel
    return rr, pb, fake


def _project(tmp: Path, long: bool = True) -> Path:
    root = make_project(tmp)
    if long:
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + f"artifacts:\n  ac:\n    out: {LONG_OUT}\n", encoding="utf-8")
    return root


def _route(cmds: list) -> dict:
    steps = [{"manual": False, "cmd": c, "why": "шаг", "flags": []} for c in cmds]
    return {"id": "update", "title": "Обновить базу", "when": "", "group": "база", "steps": steps}


class _System:
    """Пин системы «этой машины»: корень её — настоящий путь проекта, остальные — допущения. Без него тест зависит от ОС прогона."""

    def __init__(self, pb, name: str):
        self.pb, self.name = pb, name

    def __enter__(self):
        self.saved = self.pb.current_system
        self.pb.current_system = lambda: self.name
        return self

    def __exit__(self, *exc):
        self.pb.current_system = self.saved


def _panel(tmp: Path, cmds: list, fake, pb):
    class Panel(fake):
        PB = pb
    return Panel(tmp / "panel", {c: [(0, ["готово"])] for c in cmds}, [_route(cmds)])


@test
def test_a_route_with_a_sync_says_about_long_paths_once_and_still_runs(tmp: Path):
    """Выгрузка в маршруте: одна строка в начале и одна в итоге, шаги идут, маршрут пройден."""
    rr, pb, fake = _mods()
    root = _project(tmp)
    (tmp / "panel").mkdir()
    panel = _panel(tmp, ["sync:confluence", "sync:web", "sync:audit"], fake, pb)
    logs: list = []
    with _System(pb, "windows"):
        res = rr.run_route(panel, str(root), "update", True, log=logs.append)
    assert res["ok"] and res["reason"] == "passed", res
    assert [c[1] for c in panel.calls] == ["sync:confluence", "sync:web", "sync:audit"], panel.calls
    start = [m for m in logs if m.startswith("⚠ Длинные пути: не всё будет загружено")]
    final = [m for m in logs if m.startswith("Длинные пути: artifacts → ac → out — не поместившиеся файлы названы")]
    assert len(start) == 1 and len(final) == 1, logs
    assert "artifacts → ac → out" in start[0] and "kit:paths" in start[0], start[0]
    assert logs.index(start[0]) < min(i for i, m in enumerate(logs) if "sync:confluence" in m), "строка стоит до первого шага"
    assert logs.index(final[0]) > logs.index("■ Итог прогона"), "итоговая строка стоит после итога прогона"
    journal = (panel.runs / res["run_id"] / "console.log").read_text(encoding="utf-8")
    assert journal.count("Длинные пути: не всё будет загружено") == 1 and "не поместившиеся файлы названы" in journal, journal


@test
def test_a_route_without_a_sync_or_without_findings_stays_silent(tmp: Path):
    """Нет шагов выгрузки или нет находок — строки нет; проверка, которая упала, маршрут не роняет."""
    rr, pb, fake = _mods()
    (tmp / "a").mkdir()
    (tmp / "b").mkdir()
    (tmp / "c").mkdir()
    long_root, short_root = _project(tmp / "a"), _project(tmp / "b", long=False)
    for root, cmds in ((long_root, ["kb:repair", "kb:lint"]), (short_root, ["sync:confluence"])):
        panel = _panel(tmp / ("a" if root == long_root else "b"), cmds, fake, pb)
        logs: list = []
        with _System(pb, "windows"):
            res = rr.run_route(panel, str(root), "update", True, log=logs.append)
        assert res["ok"], res
        assert not [m for m in logs if "Длинные пути" in m], logs
    panel = _panel(tmp / "c", ["sync:confluence"], fake, pb)

    class Boom:
        @staticmethod
        def assess(project):
            raise RuntimeError("сбой проверки")
    panel.PB = Boom
    logs = []
    assert rr.run_route(panel, str(long_root), "update", True, log=logs.append)["ok"]
    assert not [m for m in logs if "Длинные пути" in m]


@test
def test_the_path_hint_is_worded_by_the_catalogue_of_each_language(tmp: Path):
    """Строка про длинные пути — из каталога языка; на другой системе (корень — допущение) она говорит иначе и без итога."""
    rr, pb, fake = _mods()
    root = _project(tmp, long=False)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + f"paths:\n  sources_confluence: {LONG_OUT}\n", encoding="utf-8")
    (tmp / "panel").mkdir()
    panel = _panel(tmp, ["sync:confluence"], fake, pb)
    words = {"ru": ("⚠ Длинные пути: не всё будет загружено — ", "Длинные пути: ", "ℹ Длинные пути на другой системе "),
             "en": ("⚠ Long paths: not everything will be loaded — ", "Long paths: ", "ℹ Long paths on another system ")}
    for lang, (start, final, other) in words.items():
        panel.strings = json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        with _System(pb, "windows"):
            got = rr.paths_lines(panel, str(root), rr.Texts(panel, lang))
        assert got[0].startswith(start) and got[1].startswith(final), got
        assert "paths → sources_confluence" in got[0] and "kit:paths" in got[0], got[0]
        assert "не всё было загружено" not in got[1] and "not everything was loaded" not in got[1], "итог не врёт о факте"
        with _System(pb, "linux"):       # на этой машине помещается, а у коллег на Windows — нет
            elsewhere = rr.paths_lines(panel, str(root), rr.Texts(panel, lang))
        assert elsewhere[0].startswith(other) and elsewhere[1] == "", elsewhere
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert "if parent or cmd not in RR.sync_commands(project):" in src and "job_path_hint(project, cmd, parent)" in src, "выгрузка одной кнопкой тоже говорит об этом в начале"
    route = (COCKPIT / "route_runner.py").read_text(encoding="utf-8")
    assert route.index('self._git("update", "before_route")') < route.index("self._paths_hint(steps)"), \
        "подсказка считается после обновления конфига с сервера"


@test
def test_kit_paths_and_the_doctor_report_the_same_lines_without_blocking(tmp: Path):
    """`kit:paths` печатает строки и код 1, `--json` — те же находки, чистый проект — код 0; доктор — одно предупреждение, код 0."""
    root = _project(tmp)
    cp = run("path_budget.py", cwd=root)
    assert cp.returncode == 1 and "✗ artifacts → ac → out (aurora.config.yaml, строка" in cp.stdout, cp.stdout + cp.stderr
    assert "Запуск возможен, но не всё будет загружено: artifacts → ac → out." in cp.stdout, cp.stdout
    assert os.name == "nt" or "допущение" in cp.stdout, "корень чужой системы назван допущением"
    data = json.loads(run("path_budget.py", "--json", cwd=root).stdout)
    assert data["lines"] == 1 and data["findings"][0]["key"] == "artifacts:ac" and data["findings"][0]["where"] == ["artifacts", "ac", "out"]
    doc = run("aurora_doctor.py", cwd=root)
    warns = [ln for ln in doc.stdout.splitlines() if ln.startswith("WARN: длинные пути")]
    assert len(warns) == 1 and "строк конфига 1" in warns[0] and "artifacts → ac → out" in warns[0] and "kit:paths" in warns[0], doc.stdout
    (tmp / "ok").mkdir()
    clean = _project(tmp / "ok", long=False)
    assert run("path_budget.py", cwd=clean).returncode == 0
    base = run("aurora_doctor.py", cwd=clean)
    assert not [ln for ln in base.stdout.splitlines() if "длинные пути" in ln]
    assert doc.returncode == base.returncode, "длинные пути — предупреждение: код доктора от них не меняется"
    assert not [ln for ln in doc.stdout.splitlines() if ln.startswith("ERROR:") and "длинные пути" in ln]


@test
def test_kit_paths_is_a_read_only_command_in_the_registry_and_the_docs(tmp: Path):
    """Команда `kit:paths` в реестре и справочниках; документация объясняет сообщения на двух языках; настройки только через панель."""
    registry = (KIT / "commands.txt").read_text(encoding="utf-8")
    row = next(ln for ln in registry.splitlines() if ln.startswith("kit | kit:paths |"))
    assert "| скрипт | path_budget.py |" in row and "только чтение" in row, row
    for rel in ("docs/commands.md", "docs/en/commands.md"):
        assert "`kit:paths`" in (KIT / rel).read_text(encoding="utf-8"), rel
    ru = (KIT / "docs/connectors.md").read_text(encoding="utf-8")
    en = (KIT / "docs/en/connectors.md").read_text(encoding="utf-8")
    assert "## Длина путей: проверка при сохранении настроек" in ru and "## Path length: the check on saving settings" in en
    for need in ("259", "247", "LongPathsEnabled", "core.longpaths", "C:\\Users\\username\\Documents\\Projects", "kit:paths", "kit:doctor"):
        assert need in ru and need in en, need
    for rel in ("wiki/Troubleshooting.md", "wiki/ru/Troubleshooting.md"):
        assert "kit:paths" in (KIT / rel).read_text(encoding="utf-8"), rel
    script = (SCRIPTS / "path_budget.py").read_text(encoding="utf-8")
    assert "write_config" not in script and "aurora_setup" not in script, "проверка ничего не пишет в настройки"
    import re
    assert not re.search(r"open\([^)]*,\s*[\"'][wax]b?\+?[\"']", script) and ".write(" not in script \
        and "os.remove" not in script and "makedirs" not in script and "os.rename" not in script, \
        "проверка только читает: файлы открываются на чтение (конфиг и текст выгрузчика), записей нет"
