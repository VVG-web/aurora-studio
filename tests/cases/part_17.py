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
