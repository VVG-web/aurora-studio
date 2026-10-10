"""Проверки движка Aurora, часть 37: прогон не трогает настоящий `~/.aurora` владельца.

Каркас и помощники — tests/harness.py. Движок пишет и стирает под `~/.aurora` (venv надстройки,
кэш, расписание Cron); проверка, дошедшая до `install()` или `rmtree`, стёрла бы настоящее
(10.10.2026: `~/.aurora/venv` с Pydantic AI). Зонды venv идут изолированно (`-I`).
"""
from __future__ import annotations

from pathlib import Path
import importlib
import os
import sys

from harness import KIT, SCRIPTS, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_the_venv_interpreter_probes_run_isolated(tmp: Path):
    """`python -c` берёт первым в sys.path текущую папку, а панель зовёт venv из папки проекта:
    `zipfile.py` или `email/` в корне клона исполнились бы без согласия владельца (ADR-026).
    Три зонда venv — версия пакета, версия Python, проверка Pydantic AI — идут с `-I`.
    Всё, что умеет стирать venv (`install` при старом Python), смотрит на каталог во `tmp`:
    настоящий `~/.aurora/venv` владельца проверка не трогает."""
    _path()
    import subprocess
    AG = importlib.import_module("agent_core")
    EX = importlib.import_module("aurora_extras")
    extra = "pydantic-ai"
    fake = tmp / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    elsewhere = tmp / "never-created"          # сюда смотрит rmtree в install(); файла fake он не заденет
    seen = []

    def spy(cmd, *a, **kw):
        seen.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    real_venv = EX.EXTRAS[extra]["venv"]
    assert not str(elsewhere).startswith(str(real_venv)), "подмена каталога venv указывает на настоящий"
    saved = (AG.VENV, AG.subprocess.run, EX.venv_python, EX.EXTRAS[extra]["venv"])
    AG.VENV = fake.parent.parent
    AG.subprocess.run = spy
    EX.venv_python = lambda extra_id: fake
    EX.EXTRAS[extra]["venv"] = elsewhere
    try:
        AG.venv_status()
        EX.installed_version(extra)
        EX.install(extra)
    finally:
        AG.VENV, AG.subprocess.run, EX.venv_python, EX.EXTRAS[extra]["venv"] = saved
    assert EX.EXTRAS[extra]["venv"] == real_venv
    probes = [c for c in seen if "-c" in c]
    assert len(probes) >= 3, f"не все зонды venv замечены: {probes}"
    for c in probes:
        assert "-I" in c and c.index("-I") < c.index("-c"), f"зонд venv без изоляции: {c}"


@test
def test_the_test_run_never_sees_the_real_home(tmp: Path):
    """Прогон живёт в своей домашней папке (tests/harness.py): `~/.aurora` владельца — venv
    надстройки, согласия, расписание Cron — ему не виден. Без этого проверка, дошедшая до
    `install()` или `rmtree`, стирала настоящее (10.10.2026: `~/.aurora/venv`)."""
    _path()
    real = os.environ.get("AURORA_TESTS_REAL_HOME")
    own = os.environ.get("AURORA_TESTS_HOME")
    assert real and own and real != own, "прогон не запомнил настоящий HOME и не подменил его"
    # Пути надстроек считаются при импорте движка: проверка смотрит на них, а не на текущую
    # `HOME` (соседняя проверка вправе подменить её на время своей работы).
    EX = importlib.import_module("aurora_extras")
    AG = importlib.import_module("agent_core")
    for what, path in (("venv надстройки", EX.EXTRAS["pydantic-ai"]["venv"]), ("venv адаптера", AG.VENV),
                       ("кэш самопроверки", AG.SELFCHECK)):
        assert str(path).startswith(own), f"{what} указывает не во временный HOME: {path}"
        assert not str(path).startswith(real), f"{what} указывает в настоящий HOME: {path}"
