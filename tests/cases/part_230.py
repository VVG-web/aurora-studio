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
import shlex
import subprocess
import sys
from contextlib import contextmanager
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


@contextmanager
def _runtime_home(tmp: Path):
    """Блокировка runtime живёт в HOME; проверка не должна создавать её в настоящем."""
    old_home, old_lock = RT.HOME, RT.RUNTIME_LOCK
    RT.HOME, RT.RUNTIME_LOCK = tmp, tmp / "runtime.lock"
    try:
        yield tmp
    finally:
        RT.HOME, RT.RUNTIME_LOCK = old_home, old_lock


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
def test_runtime_keeps_packages_when_reusing_or_replacing_venv(tmp: Path):
    """Смена Python не должна стирать установленные пакеты.

    Раньше `create_venv` делал `rmtree` существующего каталога: настройка runtime
    удаляла установленный Pydantic AI. Теперь та же база переиспользуется, а другая
    или принудительная замена оставляет старое окружение рядом как venv.bak-<время>.
    """
    target = tmp / "keep-venv"
    first = RT.create_venv(sys.executable, target)
    assert first["ok"], first["error"]
    assert not first.get("reused"), first
    marker = target / "installed-package.txt"
    marker.write_text("pydantic-ai", encoding="utf-8")

    again = RT.create_venv(sys.executable, target)
    assert again["ok"] and again.get("reused"), again
    assert marker.read_text(encoding="utf-8") == "pydantic-ai", "пакеты пропали при повторном выборе"

    forced = RT.create_venv(sys.executable, target, reuse=False)
    assert forced["ok"], forced["error"]
    backup = Path(forced.get("backup") or "")
    assert backup.is_dir(), forced
    assert (backup / "installed-package.txt").read_text(encoding="utf-8") == "pydantic-ai", \
        "старое окружение не сохранено"
    assert (target / "pyvenv.cfg").is_file(), "новое окружение не создано"


@test
def test_runtime_uses_a_selected_venv_directly(tmp: Path):
    """Выбранный готовый venv — это окружение, а не база для ещё одного venv."""
    venv = tmp / "direct-venv"
    made = RT.create_venv(sys.executable, venv)
    assert made["ok"], made["error"]
    with _runtime_home(tmp / "direct-home"):
        got = RT.configure("", made["effective"], write=False)
    assert got["ok"], got
    assert got.get("reused") is True, got
    runtime = got["runtime"]
    # На macOS `/var` — ссылка на `/private/var`: интерпретатор называет канонический
    # путь, и сравнивать строки путей нельзя.
    assert os.path.realpath(runtime["effective_python"]) == os.path.realpath(made["effective"]), runtime
    assert os.path.realpath(runtime["venv_path"]) == os.path.realpath(str(venv)), runtime
    assert runtime["isolated"] is True, runtime
    assert runtime["source"] == "venv", runtime
    assert runtime["base_python"], runtime


@test
def test_runtime_restores_the_old_venv_when_the_new_one_fails(tmp: Path):
    """Сбой создания нового окружения не должен оставлять человека без старого."""
    if os.name != "posix":
        return
    target = tmp / "restore-venv"
    made = RT.create_venv(sys.executable, target)
    assert made["ok"], made["error"]
    marker = target / "keep.txt"
    marker.write_text("packages", encoding="utf-8")
    fake = tmp / "python-fail"
    probe = json.dumps({
        "executable": str(fake), "real": str(fake), "version": "3.13.0",
        "prefix": str(tmp / "fake-prefix"), "base_prefix": str(tmp / "fake-prefix"),
        "base_executable": str(fake), "venv": True, "writable": True,
        "purelib": str(tmp / "fake-purelib"), "user_site": "",
        "externally_managed": False,
    })
    body = (
        "#!/bin/sh\n"
        f'if [ "$2" = "-c" ]; then printf \'%s\\n\' {shlex.quote(probe)}; exit 0; fi\n'
        "echo 'new venv failed' >&2\n"
        "exit 1\n"
    )
    assert _exe(fake, body), "не удалось создать тестовый интерпретатор"
    got = RT.create_venv(str(fake), target)
    assert not got["ok"], got
    assert not got.get("backup"), got
    assert marker.read_text(encoding="utf-8") == "packages", "старое окружение не вернулось"
    assert (target / "pyvenv.cfg").is_file(), target


@test
def test_runtime_pip_repairs_missing_pip_and_ca_bundle(tmp: Path):
    """pip в venv может отсутствовать (uv) или потерять `cacert.pem` (гонка).
    Оба случая лечатся ensurepip и одним повтором, а на экран идёт одна строка."""
    if os.name != "posix":
        return
    assert RT._pip_error_line("Collecting x\nERROR: cacert.pem invalid\ncached").startswith("ERROR:")
    assert "Collecting" not in RT._pip_error_line("Collecting x\nERROR: cacert.pem invalid\ncached")
    assert RT._needs_reload(sys.executable) is False
    assert RT._needs_reload(str(tmp / "another-python")) is True
    # На POSIX `venv/bin/python` — ссылка на базу: realpath их не различает. Панель,
    # запущенная базой, обязана увидеть, что записан venv, и предложить перезагрузку.
    made = RT.create_venv(sys.executable, tmp / "reload-venv")
    assert made["ok"], made["error"]
    assert RT._needs_reload(made["effective"]) is True, made["effective"]

    for has_pip, ok_on in ((1, 1), (0, 2)):
        case = tmp / f"pip-{has_pip}-{ok_on}"
        case.mkdir()
        log = case / "calls.log"
        fake = case / "python-venv"
        probe = json.dumps({
            "executable": str(fake), "real": str(fake), "version": "3.13.0",
            "prefix": str(case / "prefix"), "base_prefix": str(case / "prefix"),
            "base_executable": str(fake), "venv": True, "writable": True,
            "purelib": str(case / "purelib"), "user_site": "",
            "externally_managed": False,
        })
        body = (
            "#!/bin/sh\n"
            f"log={shlex.quote(str(log))}\n"
            'case "$3" in\n'
            f'  "import pip") exit {has_pip};;\n'
            f'  *"import json"*) printf \'%s\\n\' {shlex.quote(probe)}; exit 0;;\n'
            "esac\n"
            'if [ "$4" = "-m" ] && [ "$5" = "ensurepip" ]; then\n'
            '  echo "ensurepip" >> "$log"; echo "Successfully installed pip"; exit 0\n'
            "fi\n"
            'if [ "$4" = "-m" ] && [ "$5" = "pip" ]; then\n'
            '  echo "pip-install" >> "$log"\n'
            '  n=$(grep -c \'^pip-install$\' "$log" 2>/dev/null || true)\n'
            f'  if [ "$n" -lt {ok_on} ]; then\n'
            "    echo 'ERROR: Could not install packages due to an OSError: "
            "Could not find a suitable TLS CA certificate bundle, invalid path: /x/cacert.pem' >&2\n"
            "    exit 1\n"
            "  fi\n"
            '  echo "Successfully installed pydantic-ai"; exit 0\n'
            "fi\n"
            "exit 0\n"
        )
        assert _exe(fake, body), "не удалось создать тестовый интерпретатор"
        got = RT.pip_install(str(fake), ["pydantic-ai"], timeout=30)
        assert got["ok"] and got.get("repaired"), got
        calls = log.read_text(encoding="utf-8").splitlines()
        assert "ensurepip" in calls, calls
        assert calls.count("pip-install") == ok_on, calls


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
def test_bootstrap_does_not_save_when_pydantic_fails(tmp: Path):
    """Сбой установки Pydantic AI не должен записывать конфигурацию: иначе панель
    подумает, что runtime готов, а агент останется без адаптера."""
    home = tmp / "bootstrap-home"
    venv = tmp / "bootstrap-venv"
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import aurora_runtime as RT\n"
        "RT.install_pydantic = lambda **kw: {'ok': False, 'error': 'pip упал', "
        "'pydantic': {'ok': False}}\n"
        "r = RT.bootstrap(install=True, path=sys.argv[2])\n"
        "print(json.dumps({'ok': r.get('ok'), 'error': r.get('error', ''),\n"
        "                  'config': RT.CONFIG.exists()}))\n"
    )
    p = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS), str(venv)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "AURORA_HOME": str(home)}, timeout=300)
    assert p.returncode == 0, (p.stderr or "")[-1500:]
    data = json.loads((p.stdout or "").strip().splitlines()[-1])
    assert not data["ok"] and "Pydantic" in data["error"], data
    assert data["config"] is False, data


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
        with _runtime_home(tmp / "pep668-home"):
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
def test_pip_install_explains_pep668(tmp: Path):
    """pip в управляемом системой Python отвечает externally-managed; runtime обязан
    сказать это словами и предложить venv, а не отдать сырой хвост pip."""
    if os.name != "posix":
        return
    fake = tmp / "python3.15"
    if not _exe(fake, "#!/bin/sh\n"
                      "if [ \"$2\" = \"-c\" ]; then\n"
                      "  printf '%s\\n' '{\"executable\": \"'" + str(fake) + "'\", "
                      "\"real\": \"'" + str(fake) + "'\", \"version\": \"3.15.2\", "
                      "\"venv\": false, \"writable\": false, \"externally_managed\": false, "
                      "\"prefix\": \"/x\", \"base_prefix\": \"/x\"}'\n"
                      "  exit 0\n"
                      "fi\n"
                      "echo 'error: externally-managed-environment' >&2\n"
                      "exit 1\n"):
        return
    got = RT.pip_install(str(fake), ["pydantic-ai"], timeout=30)
    assert not got["ok"], got
    assert got["externally_managed"], got
    assert "PEP 668" in got["error"] and "venv" in got["error"], got["error"]


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
    # На POSIX `bin/python` — симлинк на базу, на Windows — копия. Проверяем результат,
    # а не тип файла: панель обязана увидеть venv в обоих случаях.
    assert ck.needs_user_site(effective) is False, effective


@test
def test_runtime_keeps_its_own_environment(tmp: Path):
    env = RT.child_env({"PYTHONHOME": "/wrong", "PYTHONPATH": "/wrong", "PATH": "/bin"})
    assert "PYTHONHOME" not in env and "PYTHONPATH" not in env, env
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8", env
    assert env["PATH"] == "/bin", "остальное окружение должно сохраниться"


@test
def test_status_force_bypasses_the_cache(tmp: Path):
    """Кнопка «Проверить снова» должна перепроверить конфигурацию, а не отдать
    закэшированное состояние: force доходит до current()."""
    original = RT.current
    calls = []

    def fake_current(force: bool = False) -> dict:
        calls.append(force)
        return {"configured": False, "ok": False, "reason": "", "runtime": {},
                "base": {}, "effective_python": "", "pydantic": {}}

    RT.current = fake_current
    try:
        RT.status()
        RT.status(force=True)
    finally:
        RT.current = original
    assert calls == [False, True], calls


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
def test_doctor_reports_runtime_state(tmp: Path):
    """Доктор не должен печатать OK по процессу, если записанная конфигурация runtime
    сломана; без конфигурации он обязан предупредить, а не молчать."""
    broken = tmp / "doctor-broken"
    broken.mkdir()
    (broken / "config.json").write_text(json.dumps({
        "version": 1,
        "runtime": {"source": "custom", "base_python": "/nonexistent/python3",
                    "effective_python": "/nonexistent/python3", "venv_path": "",
                    "python_version": "3.15.2", "isolated": False,
                    "externally_managed": False,
                    "validated_at": "2026-11-10T00:00:00Z", "pydantic_ai_version": ""},
    }, ensure_ascii=False), encoding="utf-8")
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import aurora_doctor as DR\n"
        "errors, warns = [], []\n"
        "DR.check_runtime(errors, warns)\n"
        "print(json.dumps({'errors': errors, 'warns': warns}))\n"
    )
    p = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "AURORA_HOME": str(broken)}, timeout=120)
    assert p.returncode == 0, (p.stderr or "")[-1500:]
    data = json.loads((p.stdout or "").strip().splitlines()[-1])
    assert data["errors"] and "Python подсистемы" in data["errors"][0], data

    empty = tmp / "doctor-empty"
    empty.mkdir()
    p = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "AURORA_HOME": str(empty)}, timeout=120)
    assert p.returncode == 0, (p.stderr or "")[-1500:]
    data = json.loads((p.stdout or "").strip().splitlines()[-1])
    assert not data["errors"] and any("не выбран" in w for w in data["warns"]), data


@test
def test_cockpit_buttons_have_explanatory_tooltips(tmp: Path):
    """Кнопки cockpit объясняют, что произойдёт, а не повторяют подпись.

    Новый пользователь видит «Настроить автоматически», «Поставить Pydantic AI»,
    «Перезагрузить панель» и не знает, чем они отличаются. Подсказка должна назвать
    действие и его последствия; ключи обязаны быть в обоих каталогах.
    """
    index = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    for key in ("badge.hint", "nav.overview_hint", "nav.setup_hint", "nav.project_hint",
                "nav.files_hint", "nav.console_hint", "palette.hint", "project.reload_hint",
                "console.stop_hint", "console.clear_hint"):
        assert f'data-i18n-title="{key}"' in index, key

    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    for key in ("runtime.gate_close_hint", "runtime.open_install_hint",
                "runtime.gate_reload_hint", "runtime.check_again_hint"):
        assert f't("{key}")' in js, key
    assert "function moduleAbout" in js and "title: moduleAbout(m) || moduleTitle(m)" in js, \
        "пункты меню модулей остались без пояснения"
    assert "b.title = moduleAbout(rec.manifest) || moduleTitle(rec.manifest)" in js, \
        "подсказка меню не переводится при смене языка"

    view = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    for key in ("install.rt_check_hint", "install.rt_bootstrap_hint",
                "install.rt_install_pydantic_hint", "install.rt_reload_hint",
                "install.rt_dismiss_hint", "install.rt_details_hint",
                "install.rt_candidate_hint", "install.rt_candidate_venv_hint",
                "install.fix_hint", "install.do_install_hint"):
        assert f't("{key}")' in view, key

    for lang in ("ru", "en"):
        panel = json.loads((KIT / "cockpit/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("palette.hint", "project.reload_hint", "console.stop_hint",
                    "console.clear_hint", "runtime.gate_close_hint",
                    "runtime.open_install_hint", "runtime.gate_reload_hint",
                    "runtime.check_again_hint"):
            assert key in panel, (lang, key)
        install = json.loads((KIT / "cockpit/modules/install/i18n" / f"{lang}.json")
                             .read_text(encoding="utf-8"))
        for key in ("install.rt_check_hint", "install.rt_bootstrap_hint",
                    "install.rt_install_pydantic_hint", "install.rt_reload_hint",
                    "install.rt_dismiss_hint", "install.rt_details_hint",
                    "install.rt_candidate_hint", "install.rt_candidate_venv_hint",
                    "install.fix_hint", "install.do_install_hint"):
            assert key in install, (lang, key)


@test
def test_runtime_bar_names_one_venv(tmp: Path):
    """В шапке не должно быть «venv · venv»: версия и режим, источник — в «Установке»."""
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    block = js.split("function runtimeText", 1)[1].split("let GATE_DISMISSED", 1)[0]
    assert "const src" not in block, block
    assert 'r.isolated ? t("runtime.mode_venv")' in block, block
    assert '[v ? "Python " + v : "", mode]' in block, block

    view = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    assert 'if (rt.source !== "venv") line(t("install.rt_mode"), mode);' in view, \
        "карточка установки снова показывает режим venv дважды"


@test
def test_runtime_gate_can_be_closed_and_the_panel_restarted(tmp: Path):
    """Экран ошибки не должен требовать перезагрузки браузера.

    У блокирующего экрана были только «Открыть установку» и «Проверить снова»:
    длинная причина растягивала карточку, кнопки уходили за край, крестика не было.
    Теперь есть закрытие, перезапуск панели и прокручиваемые детали; карточка Python
    предлагает перезагрузку, когда процесс панели остался на прежнем интерпретаторе.
    """
    js = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    for needle in ("runtime.gate_close", "runtime.gate_reload", "GATE_DISMISSED",
                   "restartPanel({patient:true})"):
        assert needle in js, needle
    # Раньше локальная переменная `t` в toast() затеняла перевод: `t("toast.close")`
    # падал, и sticky-ошибка оставалась без крестика. Проверяем, что имя не вернулось.
    toast_src = js.split("function toast(", 1)[1].split("// Исключение", 1)[0]
    assert "const to = el" in toast_src and "t(\"toast.close\")" in toast_src, toast_src[:400]
    css = (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")
    gate = css.split(".runtime-gate{", 1)[1].split("/* документация */", 1)[0]
    assert "max-height" in gate and "overflow:auto" in gate, gate
    assert ".toast.sticky .toast-text" in css and "max-height" in css, "тост не ограничен по высоте"

    view = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    for needle in ("install.rt_reload", "install.rt_reload_about", "ctx.restartPanel",
                   "install.rt_details", "lastBackup", "install.rt_backup"):
        assert needle in view, needle

    for lang in ("ru", "en"):
        install = json.loads((KIT / "cockpit/modules/install/i18n" / f"{lang}.json")
                             .read_text(encoding="utf-8"))
        for key in ("install.rt_reload", "install.rt_reload_about", "install.rt_reused",
                    "install.rt_backup", "install.rt_direct_venv",
                    "install.rt_candidate_venv", "install.rt_details",
                    "install.rt_dismiss"):
            assert key in install, (lang, key)
        panel = json.loads((KIT / "cockpit/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("runtime.gate_close", "runtime.gate_reload", "runtime.gate_details"):
            assert key in panel, (lang, key)


@test
def test_merge_driver_uses_the_runtime_python(tmp: Path):
    """git merge driver не должен звать `python3` из PATH: он идёт тем же
    интерпретатором, что и подсистема."""
    sys.path.insert(0, str(SCRIPTS))
    import install_aurora as IA  # noqa: E402
    repo = tmp / "merge-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert IA.configure_merge_driver(repo) is True
    value = subprocess.run(["git", "-C", str(repo), "config", "--get",
                            "merge.aurora-manifest.driver"],
                           capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert not value.startswith("python3 "), value
    assert value.startswith(sys.executable) or value.startswith(f'"{sys.executable}"'), value


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
