"""Проверки движка Aurora, часть 37: прогон не трогает настоящий `~/.aurora` владельца.

Каркас и помощники — tests/harness.py. Движок пишет и стирает под `~/.aurora` (venv надстройки,
кэш, расписание Cron); проверка, дошедшая до `install()` или `rmtree`, стёрла бы настоящее
(10.10.2026: `~/.aurora/venv` с Pydantic AI). Зонды venv идут изолированно (`-I`).
"""
from __future__ import annotations

from pathlib import Path
import importlib
import os
import re
import sys


from harness import KIT, SCRIPTS, live_adapter_python, test  # noqa: F401

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


def _workflow_jobs(text: str) -> dict:
    """Задания workflow по тексту: имя → тело. Без PyYAML (в CI его нет)."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*$", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


@test
def test_live_adapter_checks_cannot_be_skipped_silently_in_ci(tmp: Path):
    """Живые проверки адаптера берут venv из `AURORA_ADAPTER_VENV` (CI) или из настоящего
    `~/.aurora/venv`, путь зависит от ОС, а при `AURORA_REQUIRE_VENV=1` отсутствие venv — падение.

    Раньше путь был зашит как `Path.home()/.aurora/venv/bin/python`: на Windows
    (`Scripts/python.exe`) пропуск был гарантирован, а после подмены HOME на время прогона —
    и на любой машине; «пропускаем» считалось зелёным, и дрейф Pydantic AI виден не был."""
    for win, rel in ((False, "bin/python"), (True, "Scripts/python.exe")):
        venv = tmp / f"v{int(win)}"
        (venv / rel).parent.mkdir(parents=True)
        (venv / rel).write_text("", encoding="utf-8")
        assert live_adapter_python({"AURORA_ADAPTER_VENV": str(venv)}, windows=win) == venv / rel
    # venv нет: обычный прогон пропускает, CI с AURORA_REQUIRE_VENV=1 падает
    env = {"AURORA_ADAPTER_VENV": str(tmp / "no-venv")}
    assert live_adapter_python(env, windows=False) is None
    try:
        live_adapter_python({**env, "AURORA_REQUIRE_VENV": "1"}, windows=False)
    except AssertionError as e:
        assert "AURORA_REQUIRE_VENV" in str(e), e
    else:
        raise AssertionError("AURORA_REQUIRE_VENV=1 не превратил пропуск в падение")
    # без переменной смотрим на настоящий HOME владельца (read-only), а не на временный прогона
    own, real = os.environ.get("AURORA_TESTS_HOME"), os.environ.get("AURORA_TESTS_REAL_HOME")
    assert own and real and own != real
    fake_home = tmp / "home"
    (fake_home / ".aurora" / "venv" / "bin").mkdir(parents=True)
    (fake_home / ".aurora" / "venv" / "bin" / "python").write_text("", encoding="utf-8")
    assert live_adapter_python({"AURORA_TESTS_REAL_HOME": str(fake_home)}, windows=False) \
        == fake_home / ".aurora" / "venv" / "bin" / "python"
    # три живые проверки идут через помощника, а не через зашитый путь
    for rel in ("tests/cases/part_05.py", "tests/cases/part_13.py"):
        src = (KIT / rel).read_text(encoding="utf-8")
        assert '"venv" / "bin" / "python"' not in src and "live_adapter_python()" in src, rel


@test
def test_ci_matrix_pins_versions_and_runs_the_live_venv(tmp: Path):
    """CI (BL-LCY-06): ОС ubuntu/macos/windows, Python 3.10 (с предупреждением) и 3.13+, ниже 3.10 нет;
    действия закреплены версией (не веткой), тестовые зависимости и ruff — в requirements-ci.txt;
    node объявлен; задание adapter-live ставит venv надстройки и требует его; canary по расписанию
    не входит в engine-tests (иначе release.yml выпускал бы по расписанию)."""
    wf_dir = KIT / ".github" / "workflows"
    text = (wf_dir / "test.yml").read_text(encoding="utf-8")
    jobs = _workflow_jobs(text)
    assert {"tests", "floor", "newer-python", "legacy-310", "windows", "macos", "adapter-live"} <= set(jobs), set(jobs)
    # ОС и версии Python
    assert re.search(r"runs-on:\s*macos-", jobs["macos"]) and re.search(r"runs-on:\s*windows-", jobs["windows"])
    assert re.search(r"runs-on:\s*ubuntu-", jobs["tests"])
    versions = set(re.findall(r'python-version:\s*"?(\d+\.\d+)"?', text)) \
        | set(re.findall(r'"(3\.\d+)"', jobs["newer-python"]))
    nums = {tuple(int(x) for x in v.split(".")) for v in versions}
    assert (3, 10) in nums and any(v >= (3, 13) for v in nums), f"в матрице нет 3.10 или 3.13+: {sorted(nums)}"
    assert all(v >= (3, 10) for v in nums), f"CI проверяет Python ниже 3.10, который не поддерживается: {sorted(nums)}"
    # у каждого задания есть timeout, у каждого действия — версия или sha, но не ветка
    for name, body in jobs.items():
        assert "timeout-minutes:" in body, f"у задания {name} нет timeout-minutes"
    for f in sorted(wf_dir.glob("*.yml")):
        for ref in re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", f.read_text(encoding="utf-8")):
            action, _, ver = ref.partition("@")
            assert re.fullmatch(r"v\d+(\.\d+){0,2}|[0-9a-f]{40}", ver), f"{f.name}: {ref} — ссылка не закреплена версией или sha"
    # node с версией в заданиях, которые гоняют полный набор
    for name in ("tests", "floor", "newer-python", "windows", "macos"):
        assert "actions/setup-node@" in jobs[name] and re.search(r'node-version:\s*"\d+"', jobs[name]), name
    # тестовые зависимости закреплены, CI ставит их только через ограничения
    pins = (KIT / ".github" / "requirements-ci.txt").read_text(encoding="utf-8")
    for pkg in ("beautifulsoup4", "markdownify", "ruff"):
        assert re.search(rf"(?m)^{pkg}==\d+(\.\d+)+$", pins), f"{pkg} не закреплён в requirements-ci.txt"
    installs = re.findall(r"(?m)^.*pip install.*$", text)
    assert installs and all("-c .github/requirements-ci.txt" in ln or "$SPEC" in ln or "python-version" in ln
                            or "--upgrade pip" in ln for ln in installs), \
        [ln for ln in installs if "-c .github/requirements-ci.txt" not in ln]
    dep = (KIT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: github-actions" in dep and "package-ecosystem: pip" in dep
    # venv надстройки: Linux и Windows, ставится как панель, пропуск = падение, три живые проверки по именам
    ad = jobs["adapter-live"]
    assert "ubuntu-" in ad and "windows-latest" in ad and 'AURORA_REQUIRE_VENV: "1"' in ad and "AURORA_ADAPTER_VENV" in ad
    assert "--selfcheck" in ad and "aurora_extras" in ad
    known = set()
    for rel in ("tests/cases/part_05.py", "tests/cases/part_13.py"):
        known |= set(re.findall(r"(?m)^def (test_\w+)", (KIT / rel).read_text(encoding="utf-8")))
    called = re.findall(r'"([a-z][a-z ]+)"', ad)
    assert len(called) == 3, called
    for name in called:
        assert "test_" + name.replace(" ", "_") in known, f"adapter-live зовёт несуществующую проверку «{name}»"
    # canary: расписание и ручной запуск, свежие версии без ограничений, issue при красном; отдельный workflow
    canary = (wf_dir / "canary.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*-\s*cron:", canary) and "workflow_dispatch" in canary and "issues: write" in canary
    assert "-c .github" not in canary, "canary закреплён, а он для свежих"
    assert re.search(r"(?m)^name:\s*canary\s*$", canary) and "workflow_run" not in canary
    assert re.search(r"(?m)^name:\s*engine-tests\s*$", text) and "schedule:" not in text
