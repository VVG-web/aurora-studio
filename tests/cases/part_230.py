#!/usr/bin/env python3
"""part_230 — единый Python подсистемы: проверка, venv, PEP 668 и конфигурация.

Проверки отвечают на вопросы, из-за которых ломался runtime:
- верить ли интерпретатору, который называет несуществующий `sys.executable`
  (так python.org 3.15.2 оставлял висячую `bin/python3.15`);
- создаётся ли venv только из проверенной базы и находится ли его живой Python;
- становится ли Homebrew/PEP 668 изолированным, даже если человек выбрал «глобально»;
- остаётся ли конфигурация единственной правдой и падает ли рабочий выбор при её поломке;
- не протекают ли `PYTHONHOME`/`PYTHONPATH` в дочерние процессы.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test

sys.path.insert(0, str(SCRIPTS))
import aurora_runtime as RT  # noqa: E402


def _exe(path: Path, body: str) -> bool:
    """Сделать исполняемую обёртку-«интерпретатор». False — платформа не умеет."""
    try:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    except (OSError, NotImplementedError):
        return False
    return True


@test
def test_runtime_accepts_the_running_interpreter(tmp: Path):
    got = RT.validate_python(sys.executable)
    assert got["valid"], got["reason"]
    assert got["version"] == "%d.%d.%d" % sys.version_info[:3], got["version"]
    assert os.path.isfile(got["executable"]), got["executable"]
    assert bool(got["venv"]) == (sys.prefix != sys.base_prefix), got["venv"]
    assert got["source"] in RT.SOURCE_ORDER, got["source"]


@test
def test_runtime_refuses_a_missing_or_broken_interpreter(tmp: Path):
    missing = tmp / "missing-python3.15"
    got = RT.validate_python(str(missing))
    assert not got["valid"] and got["reason"], got

    folder = tmp / "python-as-folder"
    folder.mkdir()
    got = RT.validate_python(str(folder))
    assert not got["valid"] and got["reason"], got

    # Висячая ссылка: файла, на который она указывает, нет. Так выглядит битый venv
    # после удаления базы, и это должно быть видно по имени причины, а не по «не запустился».
    broken = tmp / "broken-python"
    try:
        os.symlink(str(tmp / "gone-python"), str(broken))
    except (OSError, NotImplementedError):
        broken = None
    if broken is not None:
        got = RT.validate_python(str(broken))
        assert not got["valid"], got
        assert got["broken_link"], got
        assert "висяч" in got["reason"], got["reason"]

    # Не исполняемый файл. На Windows os.access(X_OK) не различает права, поэтому только POSIX.
    if os.name == "posix":
        plain = tmp / "python-not-executable"
        plain.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        plain.chmod(0o644)
        got = RT.validate_python(str(plain))
        assert not got["valid"], got
        assert "исполняем" in got["reason"], got["reason"]


@test
def test_runtime_does_not_trust_a_wrong_self_path(tmp: Path):
    """Интерпретатор, который называет несуществующий себя, неконсистентен."""
    if os.name != "posix":
        return
    fake = tmp / "python3.15"
    if not _exe(fake, "#!/bin/sh\n"
                      "printf '%s\\n' '{\"executable\": \"/nonexistent/python3.15\", "
                      "\"real\": \"/nonexistent/python3.15\", \"version\": \"3.15.2\", "
                      "\"venv\": false, \"writable\": false, \"externally_managed\": false, "
                      "\"prefix\": \"/x\", \"base_prefix\": \"/x\"}'\n"):
        return
    got = RT.validate_python(str(fake))
    assert not got["valid"], got
    assert "неконсистент" in got["reason"], got["reason"]
    assert "3.15.2" in got["reason"] or "/nonexistent/python3.15" in got["reason"], got["reason"]


@test
def test_runtime_venv_comes_from_a_validated_base(tmp: Path):
    target = tmp / "runtime-venv"
    made = RT.create_venv(sys.executable, target)
    assert made["ok"], made["error"]
    effective = Path(made["effective"])
    assert effective.is_file(), effective
    checked = RT.validate_python(str(effective))
    assert checked["valid"] and checked["venv"], checked
    assert RT.find_venv_python(target) is not None, "venv не находится перебором"

    # Невалидная база не создаёт venv и не оставляет каталог: полурабочее состояние хуже отказа.
    nowhere = tmp / "never-created"
    bad = RT.create_venv(str(tmp / "no-such-python"), nowhere)
    assert not bad["ok"], bad
    assert not nowhere.exists(), "каталог venv остался после отказа базы"


@test
def test_runtime_config_is_the_single_truth(tmp: Path):
    """Конфигурация пишется атомарно, перепроверяется при старте и падает явно при поломке."""
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import aurora_runtime as RT\n"
        "made = RT.configure('', sys.executable, isolated=False, write=True)\n"
        "first = RT.status()\n"
        "cfg = RT.read_config()\n"
        "cfg['runtime']['base_python'] = '/nonexistent/python3'\n"
        "cfg['runtime']['effective_python'] = '/nonexistent/python3'\n"
        "RT._write_config(cfg)\n"
        "RT._drop_cache()\n"
        "second = RT.status()\n"
        "try:\n"
        "    RT.effective_python()\n"
        "    raised = ''\n"
        "except Exception as e:\n"
        "    raised = type(e).__name__ + ': ' + str(e)\n"
        "print(json.dumps({'made': made.get('ok'), 'first': first, 'second': second,\n"
        "                  'raised': raised, 'self': RT.python_or_self()}))\n"
    )
    home = tmp / "aurora-home"
    home.mkdir()
    p = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "AURORA_HOME": str(home)}, timeout=120)
    out = (p.stdout or "").strip().splitlines()[-1:] or [""]
    assert p.returncode == 0, (p.stderr or "")[-2000:]
    data = json.loads(out[0])
    assert data["made"], data
    assert data["first"]["configured"] and data["first"]["ok"], data["first"]
    assert data["first"]["runtime"]["effective_python"], data["first"]["runtime"]
    # Сломанная конфигурация не превращается в тихий откат: рабочий выбор падает явно.
    assert data["second"]["configured"] and not data["second"]["ok"], data["second"]
    assert data["raised"], "effective_python вернул путь вместо отказа"
    assert data["self"], "python_or_self не вернул интерпретатор для панели"
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["runtime"]["base_python"] == "/nonexistent/python3", config


@test
def test_runtime_pep668_goes_to_venv(tmp: Path):
    """Homebrew и PEP 668 запрещают глобальную установку: выбор «глобально» не отменяет venv."""
    original = RT.best, RT.create_venv
    calls: list = []
    kind = ["homebrew"]

    def fake_best(source: str = "", path: str = "") -> dict:
        return {"valid": True, "executable": sys.executable, "source": kind[0],
                "version": "3.15.2", "externally_managed": kind[0] == "homebrew",
                "venv": False}

    def fake_create(base: str, path=None) -> dict:
        calls.append({"base": base, "path": str(path or "")})
        return {"ok": True, "effective": str(tmp / "venv-python"), "base": {}, "links": []}

    RT.best, RT.create_venv = fake_best, fake_create
    try:
        forced = RT.configure("", "", isolated=False, write=False)
        kind[0] = "custom"
        custom = RT.configure("", "", isolated=False, write=False)
    finally:
        RT.best, RT.create_venv = original
    assert forced["ok"], forced
    assert forced["runtime"]["isolated"] is True, forced["runtime"]
    assert calls and calls[0]["path"] == str(RT.venv_path()), calls
    # Обычный Python без пометки PEP 668 остаётся глобальным, если человек так выбрал.
    assert custom["runtime"]["isolated"] is False, custom["runtime"]


@test
def test_panel_does_not_pass_user_to_a_venv(tmp: Path):
    """`bin/python` в venv — симлинк на базовый Python. `realpath` их не различает,
    поэтому панель обязана спросить сам интерпретатор: иначе в venv уйдёт `--user`,
    а pip в venv его запрещает."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck  # noqa: E402
    made = RT.create_venv(sys.executable, tmp / "panel-venv")
    assert made["ok"], made["error"]
    effective = made["effective"]
    assert Path(effective).resolve() == Path(sys.executable).resolve(), "venv должен ссылаться на базу"
    assert ck.needs_user_site(effective) is False, effective


@test
def test_runtime_keeps_its_own_environment(tmp: Path):
    env = RT.child_env({"PYTHONHOME": "/wrong", "PYTHONPATH": "/wrong", "PATH": "/bin"})
    assert "PYTHONHOME" not in env and "PYTHONPATH" not in env, env
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8", env
    assert env["PATH"] == "/bin", "остальное окружение должно сохраниться"


@test
def test_aurora_home_moves_the_whole_machine_state(tmp: Path):
    """`AURORA_HOME` двигает не только config.json: venv, кэши и состояние агента
    должны идти за ним, иначе проверка runtime пишет в настоящую домашнюю папку."""
    home = tmp / "machine-home"
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import agent_core as AG, aurora_extras as EX, git_sync as GS\n"
        "print(json.dumps([str(AG.VENV), str(EX.HOME), str(GS.aurora_home())]))\n"
    )
    p = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "AURORA_HOME": str(home)}, timeout=120)
    assert p.returncode == 0, (p.stderr or "")[-1500:]
    paths = json.loads((p.stdout or "").strip().splitlines()[-1])
    assert paths == [str(home / "venv"), str(home), str(home)], paths


@test
def test_runtime_detects_the_install_source(tmp: Path):
    cases = {
        "/opt/homebrew/bin/python3.15": "homebrew",
        "/usr/local/Cellar/python@3.13/bin/python3": "homebrew",
        "/Library/Frameworks/Python.framework/Versions/3.15/bin/python3": "python.org",
        "C:\\Program Files\\Python\\Python315\\python.exe": "python.org",
        "C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python315\\python.exe": "python.org",
        "C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\python3.exe": "store",
        "/usr/bin/python3": "system",
        str(Path.home() / ".aurora" / "venv" / "bin" / "python"): "venv",
    }
    for path, want in cases.items():
        got = RT.detect_source(path)
        assert got == want, f"{path}: {got} вместо {want}"
