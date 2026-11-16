"""Проверки движка Aurora, часть 238: архив диагностики для ошибок панели.

Кнопка «Лог» у тревоги, в консоли и в шапке собирает zip: версии, окружение, состояние
Pydantic AI и venv, журнал адаптера, хвост прогона и выбранную ошибку. В проект ничего
не пишется, ключи и тела ответов модели вырезаются. Сеть и настоящий адаптер не нужны.
"""
from __future__ import annotations

import importlib
import io
import sys
import time
import zipfile
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test


def _mods():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("agent_core"), importlib.import_module("diagnostics")


def _zip_text(blob: bytes) -> tuple[zipfile.ZipFile, str]:
    z = zipfile.ZipFile(io.BytesIO(blob))
    text = "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
    return z, text


@test
def test_the_diagnostics_bundle_has_versions_logs_and_no_secrets(tmp: Path):
    """Архив содержит версии, окружение, состояние адаптера и хвост прогона. Ключи, токены,
    логин прокси, тело ответа и путь проекта не попадают; путь проекта заменён на `<project>`."""
    AG, D = _mods()
    restore = set_home(tmp / "home")
    rt_home = AG.RT.HOME
    AG.RT.HOME = Path.home() / ".aurora"
    project = tmp / "PRJ A"
    run = project / ".aurora" / "runs" / "run-1"
    run.mkdir(parents=True)
    (project / ".aurora" / "engine_version.txt").write_text("1.230.5\n", encoding="utf-8")
    (project / ".aurora" / "scripts" / "agents").mkdir(parents=True)
    (project / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py").write_text("", encoding="utf-8")
    (run / "console.log").write_text(
        "старт PRJ A\napi_key=SECRET-TOKEN\nобычная строка\n", encoding="utf-8")
    (run / "failures.jsonl").write_text(
        '{"at":"2026-11-16T10:00:00Z","task":"agent:build","stage":"адаптер",'
        '"why":"Pydantic AI не используется","answer":"ANSWER-SECRET",'
        '"reasoning_tail":"REASON-SECRET","call":{"role":"qa","adapter":"pydantic_ai",'
        '"via":"http","log":["№1: ImportError"]}}\n', encoding="utf-8")
    (run / "pulse-1.json").write_text(
        '{"pid":1,"inflight":[{"n":1,"model":"m"}],"answered":1}\n', encoding="utf-8")
    logs = Path.home() / ".aurora" / "logs"
    logs.mkdir(parents=True)
    (logs / "adapter.log").write_text(
        "stderr token=SECRET-TOKEN\nImportError: cannot import name 'Model'\n", encoding="utf-8")
    AG._write_alarm({"agent:build": {
        "at": "2026-11-16T10:00:00Z", "why": "Pydantic AI выбран, но вызов прошёл мимо",
        "project": str(project), "calls": 3, "kind": "client_net", "area": "adapter",
        "sub": "proxy", "args": {"n": 1}, "path": "agent:build"}})
    saved = {}
    try:
        for name, value in {
            "venv_status": lambda: (True, "2.67.0"),
            "venv_display_path": lambda: "~/.aurora/venv",
            "_runtime_status": lambda: {"ok": True, "runtime": {"isolated": True}},
            "pydantic_settings": lambda _cfg: {
                "active": True, "venv": {"ok": True, "version": "2.67.0"},
                "backends": [{"n": 1, "url": "http://gw.example/v1", "key_set": True}]},
            "config": lambda: {},
            "last_selfcheck": lambda: {"ok": True, "version": "2.67.0", "at": "2026-11-16T10:00:00Z"},
            "adapter_gap": lambda: "",
            "other_home_hint": lambda: "",
            "_adapter_argv": lambda _adapter=None: [sys.executable, "adapter.py"],
            "_adapter_file": lambda: project / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py",
            "_adapter_sha": lambda: "abc123",
            "_venv_python": lambda: Path(sys.executable),
            "venv_env": lambda *_a, **_k: {},
        }.items():
            saved[name] = getattr(AG, name)
            setattr(AG, name, value)
        import access_check as AC
        ac_saved = (AC.whoami, AC.python_env_set, AC.others)
        AC.whoami = lambda: {"user": "tester", "home": str(Path.home()), "elevated": False}
        AC.python_env_set = lambda: []
        AC.others = lambda _eid, _me: []
        name, blob = D.build(str(project), {
            "source": "adapter_alarm", "key": "agent:build", "area": "adapter",
            "kind": "client_net", "why": "Pydantic AI выбран, но вызов прошёл мимо",
            "message": "Pydantic AI не используется"}, run_id="run-1", deep=False)
        z, text = _zip_text(blob)
        assert name.startswith("aurora-diagnostics-") and name.endswith(".zip"), name
        assert {"README.md", "summary.md", "diagnostics.json", "adapter.log", "env.json",
                "venv.json", "run/console.log", "run/failures.jsonl", "run/pulses.json"} <= set(z.namelist())
        assert "Pydantic AI" in text and "2.67.0" in text and "ImportError" in text, text[:2000]
        assert "Pydantic AI не используется" in text and "agent:build" in text, text[:3000]
        assert "ANSWER-SECRET" not in text and "REASON-SECRET" not in text, "тело ответа утекло"
        assert "SECRET-TOKEN" not in text, "токен утек"
        assert "user:pass" not in text and "***" in text, text[:3000]
        assert str(project) not in text and "<project>" in text, text[:3000]
        assert "adapter.log" in z.namelist() and "stderr" in z.read("adapter.log").decode(), z.namelist()
        assert len(blob) > 1000, "архив подозрительно пуст"
    finally:
        for name, value in saved.items():
            setattr(AG, name, value)
        try:
            AC.whoami, AC.python_env_set, AC.others = ac_saved
        except UnboundLocalError:
            pass
        AG._write_alarm({})
        AG.RT.HOME = rt_home
        restore()


@test
def test_adapter_stderr_goes_to_a_log_outside_the_project(tmp: Path):
    """Stderr адаптера больше не уходит в DEVNULL: он ложится в домашний журнал, а не в проект."""
    AG, _D = _mods()
    restore = set_home(tmp / "home")
    rt_home = AG.RT.HOME
    AG.RT.HOME = Path.home() / ".aurora"
    marker = "ADAPTER-STDERR-MARKER"
    script = tmp / "bad_adapter.py"
    script.write_text(
        "import sys\nprint(%r, file=sys.stderr)\n" % marker, encoding="utf-8")
    try:
        AG._adapter_log("token=SECRET-TOKEN обычная строка")
        proc = AG._spawn_adapter([sys.executable, str(script)])
        try:
            proc.wait(timeout=15)
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001
                    pass
        tail = ""
        for _ in range(30):
            tail = AG.adapter_log_tail(100)
            if marker in tail:
                break
            time.sleep(0.1)
        assert marker in tail, tail
        assert "SECRET-TOKEN" not in tail, tail
        assert str(Path.home() / ".aurora" / "logs") in str(AG.adapter_log_path())
        assert not (Path.cwd() / "adapter.log").exists()
    finally:
        AG.RT.HOME = rt_home
        restore()


@test
def test_the_panel_exports_a_diagnostics_bundle_from_any_error(tmp: Path):
    """У тревог и ошибок есть кнопка «Лог», в шапке — постоянная; сервер отдаёт zip байтами."""
    src = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/diagnostics"' in src and "def send_bytes(" in src
    assert "D.build(project, context" in src
    html = (KIT / "cockpit" / "ui" / "index.html").read_text(encoding="utf-8")
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    for needle in ('id="diagBtn"', 'id="diagConsole"'):
        assert needle in html, needle
    for needle in ("function downloadDiagnostics(", '"/api/diagnostics"',
                   "diagButton({source:", "toast-diag"):
        assert needle in panel, needle
    for lang in ("ru", "en"):
        import json
        cat = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert cat.get("diag.btn") and cat.get("diag.hint") and cat.get("diag.failed"), lang
