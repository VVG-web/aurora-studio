"""Проверки движка Aurora, часть 72. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 6 «Миграция», пакет CT-20 — маршруты, `ops:todo`, `kit:doctor`, состояние миграции и строгость
линтера: шаги `kb:conform` и `agent:conform` стоят в «Починить базу» (после пересчёта доверия, до оглавлений), в
«Обновить» и «Пересобрать» их нет; остаток называет `ops:todo` как работу маршрута или как пробел движка, а не как
решение человека; доктор говорит, завершена ли миграция; после `kb:conform --finalize --apply` карточка без `template:`
— ошибка `kb:lint`. Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, run, test  # noqa: F401

from cases import part_70 as P70  # noqa: E402

KB = "AuroraKnowledgeDB"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _routes() -> dict:
    """{id маршрута: [(команда, флаги)]} из `cockpit/scenarios.txt`."""
    out, cur = {}, None
    for line in (KIT / "cockpit" / "scenarios.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(\w+)\]", line)
        if m:
            cur = m.group(1)
            out[cur] = []
            continue
        if cur and line.strip() and not line.startswith(("#", "-", "цикл:", "конец цикла")):
            parts = [p.strip() for p in line.split("|")]
            out[cur].append((parts[0], parts[2] if len(parts) > 2 else ""))
    return out


@test
def test_conform_steps_stand_in_the_fix_route_only(tmp: Path):
    """AC-CT-20.1: в «Починить базу» `kb:conform` и следом `agent:conform` — после последнего пересчёта доверия, когда
    тексты и шапки в окончательном виде, и до оглавлений; в «Обновить» и «Пересобрать» их нет."""
    routes = _routes()
    fix = [c for c, _f in routes["fix"]]
    i = fix.index("kb:conform")
    assert fix[i + 1] == "agent:conform", fix[i - 1:i + 3]
    assert i > max(k for k, c in enumerate(fix) if c == "kb:trust"), "kb:conform раньше последнего kb:trust"
    assert i < fix.index("kb:index"), "kb:conform позже kb:index"
    flags = dict(routes["fix"])
    assert flags["kb:conform"].split() == ["--apply", "--allow-dirty"], flags["kb:conform"]
    assert flags["agent:conform"].split() == ["--apply", "--until-done"], flags["agent:conform"]
    for name, steps in routes.items():
        if name != "fix":
            assert not [c for c, _f in steps if c in ("kb:conform", "agent:conform")], f"шаг миграции в маршруте {name}"
    # команды есть в реестре, у скрипта есть заявленные флаги
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "| kb:conform |" in reg and "| agent:conform |" in reg
    assert "--until-done" in (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert "--allow-dirty" in (SCRIPTS / "kb_conform.py").read_text(encoding="utf-8")


@test
def test_todo_names_cards_without_a_template_as_route_work_then_as_an_engine_gap(tmp: Path):
    """AC-CT-20.2: `ops:todo` перечисляет «карточек без шаблона: N» — до первого `kb:conform` как работу маршрута, после
    него как пробел движка («ремонт не взял»), и ни разу — как «решаете вы»."""
    root = P70._project(tmp)
    P70._card(root, "Concepts/Заявка.md")                                                        # штамп поставит kb:conform
    P70._card(root, "Concepts/Список.md", title='"Список"', own="- пункт\n- ещё пункт\n")          # остаток: нет определения
    P70._commit(root)
    out = run("aurora_todo.py", cwd=root).stdout
    head, _, tail = out.partition("## Сделают маршруты и команды — нажать кнопку")
    assert "карточек без шаблона: 2" in tail and "Починить базу" in tail, out
    assert "Решаете вы" not in out and "Ремонт не взял" not in out, out
    run("kb_conform.py", "--apply", cwd=root)
    out = run("aurora_todo.py", cwd=root).stdout
    assert "## Ремонт не взял — пробел движка, не ваше решение" in out, out
    engine = out.split("## Ремонт не взял — пробел движка, не ваше решение", 1)[1].split("\n## ", 1)[0]
    assert "карточек без шаблона: 1" in engine and "V-12 — 1" in engine and "пробел движка" in engine, engine
    assert "Решаете вы" not in out
    # всё со штампом (после правки остатка): строки о шаблонах нет
    P70._card(root, "Concepts/Список.md", title='"Список"')
    P70._commit(root)
    run("kb_conform.py", "--apply", cwd=root)
    assert "карточек без шаблона" not in run("aurora_todo.py", cwd=root).stdout


@test
def test_doctor_reports_the_migration_state(tmp: Path):
    """AC-CT-20.3: `kit:doctor` показывает состояние миграции — не завершена с остатком, все со штампом, завершена."""
    D = _mod("aurora_doctor")
    root = P70._project(tmp)
    P70._card(root, "Concepts/Заявка.md")
    P70._card(root, "Concepts/Список.md", title='"Список"', own="- пункт\n- ещё пункт\n")
    P70._commit(root)

    def lines() -> list:
        return [l for l in D.check_card_templates(root) if l.startswith("миграция на шаблоны")]

    assert lines() == ["миграция на шаблоны: не завершена, карточек без шаблона 2 — ведут `kb:conform` и `agent:conform` "
                       "в маршруте «Починить базу»"], lines()
    run("kb_conform.py", "--apply", cwd=root)
    assert "карточек без шаблона 1" in lines()[0]
    P70._card(root, "Concepts/Список.md", title='"Список"')
    run("kb_conform.py", "--apply", "--allow-dirty", cwd=root)
    assert lines() == ["миграция на шаблоны: все карточки со штампом — объявите конец командой "
                       "`kb:conform --finalize --apply`"], lines()
    run("kb_conform.py", "--finalize", "--apply", cwd=root, expect_rc=0)
    done = lines()
    assert len(done) == 1 and re.fullmatch(r"миграция на шаблоны: завершена \d{4}-\d{2}-\d{2}", done[0]), done
    # новая карточка без штампа после завершения: доктор называет остаток
    P70._put(root, "Concepts/Новая.md", (root / KB / "Concepts" / "Заявка.md").read_text(encoding="utf-8")
             .replace("template: akdb_concept_v1.0.md\n", "").replace("Заявка", "Новая"))
    assert "но карточек без шаблона 1" in lines()[0], lines()
    # доктор не добавляет WARN/ERROR: это сведения
    assert not [l for l in D.check_card_templates(root) if l.startswith(("WARN:", "ERROR:"))]


@test
def test_lint_missing_template_after_finalize(tmp: Path):
    """AC-CT-20.4: после `kb:conform --finalize --apply` карточка без `template:` — ошибка линтера (код 1, вид
    `missing-template` в воротах); неуправляемые типы и ждущие переноса записи ошибкой не становятся; до завершения
    миграции линтер ведёт себя как прежде."""
    root = P70._project(tmp)
    P70._card(root, "Concepts/Заявка.md")
    P70._put(root, "Specs/Спека.md", "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация.\n")
    P70._put(root, "Decisions/DR-0001.md",
             "---\ntitle: DR-0001\ntype: decision\nstatus: accepted\ndate: 2026-09-01\n---\n\nРешение.\n")
    P70._commit(root)
    def errors(*args) -> int:
        cp = run("kb_lint.py", "--summary", *args, cwd=root)
        found = re.search(r"ошибок (\d+)", cp.stdout)
        assert found, cp.stdout
        return int(found.group(1))

    base = errors()                      # прочие ошибки фикстуры (спека и решение без нужных полей) от миграции не зависят
    run("kb_conform.py", "--apply", cwd=root)
    run("kb_conform.py", "--finalize", "--apply", cwd=root, expect_rc=0)
    assert errors() == base, "завершённая миграция сделала ошибкой неуправляемый тип или рукописную запись"
    # человек правит карточку в обход входа и теряет штамп — теперь это ошибка
    p = root / KB / "Concepts" / "Заявка.md"
    p.write_bytes(P70._strip_stamp(p.read_bytes().decode("utf-8")).encode("utf-8"))
    assert errors() == base + 1
    full = run("kb_lint.py", cwd=root)
    assert full.returncode == 1 and "## карточки без шаблона после завершения миграции: 1" in full.stdout, full.stdout
    assert "миграция на шаблоны завершена" in full.stdout
    assert "AuroraKnowledgeDB/Specs/Спека.md" not in "".join(l for l in full.stdout.splitlines() if "без template:" in l)
    gate = run("kb_lint.py", "--gate", cwd=root)
    assert gate.returncode == 1 and "missing-template" in gate.stdout, gate.stdout
    # снять объявление — линтер прежний
    (root / KB / "meta" / "conform_state.json").unlink()
    assert errors() == base


@test
def test_lint_with_an_old_card_types_module_does_not_judge_the_migration(tmp: Path):
    """Обновление оборвалось: новый линтер рядом со старым `card_types.py` без состояния миграции — линтер работает как
    раньше, а не падает ImportError."""
    import shutil
    import subprocess
    old = tmp / "old-scripts"
    shutil.copytree(SCRIPTS, old, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    ct = old / "card_types.py"
    text = ct.read_text(encoding="utf-8")
    assert "def migration_complete(" in text and "def missing_template_error(" in text
    ct.write_text(text.replace("def migration_complete(", "def _gone_migration_complete(")
                  .replace("def missing_template_error(", "def _gone_missing_template_error("), encoding="utf-8")
    root = P70._project(tmp, git=False)
    P70._card(root, "Concepts/Заявка.md")
    (root / KB / "meta").mkdir(exist_ok=True)
    (root / KB / "meta" / "conform_state.json").write_text('{"complete": true}\n', encoding="utf-8")
    cp = subprocess.run([sys.executable, str(old / "kb_lint.py"), "--summary"], cwd=str(root), capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0 and "ошибок 0" in cp.stdout and "Traceback" not in cp.stderr, (cp.stdout, cp.stderr[-600:])


@test
def test_todo_separates_the_agent_queue_from_the_engine_gap(tmp: Path):
    """Карточку, которую ещё возьмёт `agent:conform`, `ops:todo` называет работой маршрута; пробел движка — только то, что
    не возьмёт никто (в очереди она ждёт следующего «Починить», а не научить ремонт)."""
    root = P70._project(tmp)
    P70._card(root, "Processes/Приём.md", title='"Приём"', type="process",
              own="- Проверить заявку.\n" + P70.LEAD + "\n- Подтвердить заявку.\n")          # очередь агента
    P70._card(root, "Concepts/Список.md", title='"Список"', own="- пункт\n- ещё пункт\n")      # пробел движка
    P70._commit(root)
    run("kb_conform.py", "--apply", cwd=root)
    out = run("aurora_todo.py", cwd=root).stdout
    route = out.split("## Сделают маршруты и команды — нажать кнопку", 1)[1].split("\n## ", 1)[0]
    engine = out.split("## Ремонт не взял — пробел движка, не ваше решение", 1)[1].split("\n## ", 1)[0]
    assert "карточек без шаблона в очереди agent:conform: 1" in route, out
    assert "карточек без шаблона: 1" in engine and "V-12 — 1" in engine, out


@test
def test_doctor_counts_the_migration_without_exceptions(tmp: Path):
    """Неуправляемый тип и рукописная запись остатком миграции не считаются: доктор не просит завершить миграцию, пока они
    «не со штампом», и не тревожится после завершения."""
    D = _mod("aurora_doctor")
    root = P70._project(tmp)
    P70._card(root, "Concepts/Заявка.md")
    P70._put(root, "Specs/Спека.md", "---\ntitle: Спека\ntype: spec\nstatus: draft\n---\n\nСпецификация.\n")
    P70._put(root, "Decisions/DR-0001.md",
             "---\ntitle: DR-0001\ntype: decision\nstatus: accepted\ndate: 2026-09-01\n---\n\nРешение.\n")
    P70._commit(root)
    run("kb_conform.py", "--apply", cwd=root)
    lines = [l for l in D.check_card_templates(root) if l.startswith("миграция на шаблоны")]
    assert lines == ["миграция на шаблоны: все карточки со штампом — объявите конец командой "
                     "`kb:conform --finalize --apply`"], lines
    run("kb_conform.py", "--finalize", "--apply", cwd=root, expect_rc=0)
    lines = [l for l in D.check_card_templates(root) if l.startswith("миграция на шаблоны")]
    assert len(lines) == 1 and "но карточек" not in lines[0] and "завершена" in lines[0], lines


@test
def test_lint_only_judges_a_card_without_template_after_finalize(tmp: Path):
    """Путь хука pre-commit (`kb:lint --only <файл>`): после завершения миграции карточка без штампа в коммите — ошибка."""
    root = P70._project(tmp)
    P70._card(root, "Concepts/Заявка.md")
    P70._card(root, "Concepts/Вторая.md", title='"Вторая"')
    P70._commit(root)
    run("kb_conform.py", "--apply", cwd=root)
    run("kb_conform.py", "--finalize", "--apply", cwd=root, expect_rc=0)
    p = root / KB / "Concepts" / "Заявка.md"
    p.write_bytes(P70._strip_stamp(p.read_bytes().decode("utf-8")).encode("utf-8"))
    mine = run("kb_lint.py", "--only", f"{KB}/Concepts/Заявка.md", cwd=root)
    other = run("kb_lint.py", "--only", f"{KB}/Concepts/Вторая.md", cwd=root)
    assert mine.returncode == 1 and "карточка без template: миграция на шаблоны завершена" in mine.stdout, mine.stdout
    assert "карточка без template:" not in other.stdout, other.stdout       # штампованная карточка этой ошибки не получает
