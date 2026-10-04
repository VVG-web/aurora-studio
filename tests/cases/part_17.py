"""Проверки движка Aurora, часть 17: контроль PRJ-C после облачных выпусков 1.147–1.150.

Каркас и помощники — tests/harness.py. Здесь — то, что нашла сверка живой базы PRJ-C
4.10.2026 с выпусками, сделанными параллельно: шаг, который падает, и маршрут, который
этого не видит.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from harness import SCRIPTS, card, make_project, run, test  # noqa: F401


@test
def test_a_crashing_engine_script_exits_with_code_three(tmp: Path):
    """Необработанная ошибка скрипта движка — код 3, а не 1.

    Код 1 у движка — «есть находки» (линтер, ремонт), и маршрут идёт дальше. Трассировка
    Python тоже даёт 1: на PRJ-C 1.10.2026 `kb:repair --stub-text` трижды падал внутри
    «Починить базу», а маршрут каждый раз рапортовал «пройден». Код 3 маршрут и итог
    называют упавшим шагом.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    code = ("import sys; sys.path.insert(0, %r); import aurora_common; raise RuntimeError('x')"
            % str(SCRIPTS))
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert cp.returncode == 3, (cp.returncode, cp.stderr[-300:])
    assert "RuntimeError" in cp.stderr, "трассировка пропала — причину не прочесть"
    RS = importlib.import_module("run_summary")
    got = RS.route(str(tmp), "", 1.0, [{"cmd": "kb:repair", "rc": 3}])
    assert any("упал" in k for k in got["data"]["errors"]), got["data"]["errors"]


@test
def test_returning_a_stub_its_look_survives_the_whole_repair(tmp: Path):
    """`kb:repair --all --stub-text` проходит до конца на заготовке с историей.

    После того как `--stub-text` вернул заготовке её вид, заголовок дословного раздела
    оставался только в истории изменений («прежний тезис»), и `drop_false_redistill` в шаге
    `--frontmatter` падал с `IndexError`: ни одна из 121 заготовки PRJ-C не починилась.
    """
    root = make_project(tmp)
    card(root, "Concepts/Авторизация.md",
         "Авторизация — заготовка понятия, в которой ссылка уже есть.\n\n"
         "## Источник (перенесено дословно)\n\n# Авторизация\n\n"
         "_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n\n"
         "## История изменений\n\n- 2026-08-30: тезис пересобран — источник изменился\n"
         "  <details><summary>прежний тезис</summary>\n\n"
         "  ## Источник (перенесено дословно)\n\n  # Авторизация\n\n  </details>\n",
         status="draft", tags="[заготовка]", distilled="2026-08-30")
    cp = run("kb_fix.py", "--all", "--stub-text", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1), cp.stderr[-600:]
    assert "Traceback" not in cp.stderr, cp.stderr[-600:]
    text = (root / "AuroraKnowledgeDB/Concepts/Авторизация.md").read_text(encoding="utf-8")
    assert "status: placeholder" in text and "заготовка понятия, в которой" not in text, text


@test
def test_the_test_run_never_reads_the_developers_personal_kit_files(_t):
    """Прогон тестов не читает личные файлы настоящего кита — ни ключи шлюзов, ни MCP.

    Изоляция агента ловила это для вызова без кита, а панель передаёт кит явно: на машине
    разработчика три теста английского режима видели его MCP-сервер и кольцо эмбеддингов и
    краснели, а в CI были зелёными (4.10.2026). Тесту, которому нужна настройка машины,
    положено заводить свой временный кит — его файлы читаются как обычно.
    """
    import importlib
    import os
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    kit = os.path.dirname(str(SCRIPTS))
    assert os.environ.get("AURORA_TESTS_ISOLATED"), "прогон идёт без изоляции"
    assert A.personal_kit_file(os.path.join(kit, A.ENV_FILE))
    assert A.personal_kit_file(os.path.join(kit, "local", "mcp.json"))
    assert A.load_env(os.path.join(kit, A.ENV_FILE)) == {}
    assert not A.personal_kit_file(os.path.join(str(_t), A.ENV_FILE)), \
        "временный кит теста объявлен личным — тесты настроек машины ослепнут"


@test
def test_the_windows_launcher_keeps_crlf_in_the_projects_git(tmp: Path):
    """Пусковой .bat проекта попадает в git с CRLF, а не с LF.

    Кит пишет start-aurora.bat с CRLF, но у проектов не было .gitattributes: git с
    core.autocrlf=input (обычная настройка Mac) сохранял файл с одними LF. В PRJ-A он так и
    лежал в истории 4.10.2026 — на Windows из такого клона cmd.exe промахивается мимо меток
    goto. Правило кладут и установка, и обновление движка.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    U = importlib.import_module("aurora_update")
    bat = b"@echo off\r\ngoto :start\r\n:start\r\necho ok\r\n"

    def staged(repo: Path) -> bytes:
        repo.mkdir(exist_ok=True)
        (repo / "start-aurora.bat").write_bytes(bat)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=input", "add", "-A"],
                       check=True, capture_output=True)
        return subprocess.run(["git", "-C", str(repo), "cat-file", "blob", ":start-aurora.bat"],
                              check=True, capture_output=True).stdout

    assert b"\r\n" not in staged(tmp / "без правила"), \
        "без правила git должен был срезать CRLF — проверка ничего не ловит"

    repo = tmp / "проект"
    repo.mkdir()
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    added = U.refresh_gitattributes(repo)
    assert added == ["/start-aurora.bat -text"], added
    assert U.refresh_gitattributes(repo) == [], "повторное обновление дописало правило ещё раз"
    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in attrs, "строка человека потерялась"
    assert staged(repo) == bat, "пусковой файл ушёл в git проекта не байт в байт"

    upd = (SCRIPTS / "aurora_update.py").read_text(encoding="utf-8")
    inst = (SCRIPTS / "install_aurora.py").read_text(encoding="utf-8")
    assert "refresh_gitattributes(target)" in upd, "обновление движка не кладёт правило"
    assert '".gitattributes", GITATTRIBUTES_BLOCK' in inst, "установка не кладёт правило"
