"""Проверки движка Aurora, часть 41. Каркас и помощники — tests/harness.py.

Детерминированный линтер артефактов (BL-TEAM-05): `scripts/artifact_lint.py` решает кодом то,
что чек-лист ревью отдавал модели, — разделы шаблона, формулу истории, пару «Действие /
Реакция» у шагов, число шагов и групп критериев, остатки шаблона, формулировки спеки. Синтетические
артефакты, без модели и сети; в `kb:lint` замечания идут предупреждениями, код возврата не меняют.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test  # noqa: F401

GOOD_US = """\
# Задача на разработку истории

## История изменений

|  |  |
| --- | --- |
| Код | RU.PRJ.US-1.1.1 |
| Реализует | RU.PRJ.REQ-010 |
| Зависимости |  |

## Описание

|  |  |
| --- | --- |
| **Как** | Оператор системы |
| **Я хочу** | получить документ из смежной системы |
| **Чтобы** | документ был сохранён и доступен для обработки |

## Предусловия US

#### ✅ Готово

* RU.PRJ.ALG-010 — приём документа

#### ⚠️ Изменить

* RU.PRJ.SPR-020 — справочник каналов — добавляется канал «резервный»

## Пользовательские сценарии

### Основной сценарий. Документ принят

#### Предусловия сценария

* Документ отправлен

#### Шаги сценария

1. **Документ получен**
   1. Действие пользователя: не выполняет
   2. Реакция системы: принимает документ
2. **Документ сохранён**
   1. Действие пользователя: ожидает
   2. Реакция системы: сохраняет документ и фиксирует результат

## Acceptance criteria

### Приём

1. Система принимает документ.

### Ошибки

1. Система отказывает в приёме повреждённого документа.

## Требования к обратной совместимости

Нет требований.

## How to demo

Нет требований.

<!-- ниже — производство, в чистовик не идёт -->

## Допущения

{{тут черновик производства, чистовик его не отдаёт}} ??? TODO
"""

GOOD_SPEC = """\
---
title: "SPEC-001: Приём документа"
type: spec
spec_id: SPEC-001
status: draft             # класс доверия считает kb:trust
based_on: ["[[Приём-документа]]"]
---

# SPEC-001: Приём документа

## Поведение

### Сценарий 1: успешный приём
- **Дано:** документ в очереди
- **Когда:** приходит запрос на приём
- **Тогда:** система должна сохранить документ

### Сценарий 2: отказ
- **Дано:** документ повреждён
- **Когда:** приходит запрос на приём
- **Тогда:** система должна отказать в приёме

## НФТ
- Время приёма не более 5 секунд для документа до 10 МБ

## Открытые вопросы

## Критерии приёмки
| Сценарий | Как проверяем |
|---|---|
| Сценарий 1 | документ сохранён |
| Сценарий 2 | отказ с причиной |
"""


def _lint():
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("artifact_lint")


def _ids(items):
    return sorted({i["id"] for i in items})


@test
def test_artifact_lint_clean_story_has_no_findings(_t):
    """Полная история по шаблону — без замечаний, а то, что ниже границы производства, не судится.

    Под границей лежат `{{…}}`, `???` и TODO черновика: заказчику они не уходят, и ругать их
    значило бы шуметь на каждом артефакте агента.
    """
    AL = _lint()
    found = AL.lint_text(GOOD_US, "us")
    assert found == [], found


@test
def test_artifact_lint_story_defects_are_named_by_checklist_id(_t):
    """Каждый дефект истории находится кодом и называется номером вопроса чек-листа ревью."""
    AL = _lint()
    bad = (GOOD_US
           .replace("| Реализует | RU.PRJ.REQ-010 |\n", "| Реализует |  |\n")
           .replace("| **Чтобы** | документ был сохранён и доступен для обработки |\n", "")
           .replace("* RU.PRJ.ALG-010 — приём документа\n",
                    "* RU.PRJ.ALG-010 — приём документа\n\n#### ❗ Сделать\n\n"
                    "* RU.PRJ.ALG-011 — новый\n")
           .replace("## Требования к обратной совместимости\n\nНет требований.\n\n", "")
           .replace("   2. Реакция системы: принимает документ\n", "")
           .replace("### Ошибки\n", "### Ошибки\n\nСм. алгоритм приёма.\n")
           .replace("* RU.PRJ.SPR-020 — справочник каналов — добавляется канал «резервный»",
                    "* RU.PRJ.SPR-020 — справочник каналов")
           .replace("Нет требований.\n\n<!--", "{{заполнить}} [ПРИМЕР] ???\n\n<!--"))
    ids = _ids(AL.lint_text(bad, "us"))
    for want in ("US-01", "US-03", "US-04", "US-06", "US-07", "US-22", "US-23"):
        assert want in ids, (want, ids)
    # Без границы производства то, что ниже, тоже чистовик.
    no_mark = GOOD_US.replace("<!-- ниже — производство, в чистовик не идёт -->", "")
    assert "US-23" in _ids(AL.lint_text(no_mark, "us"))


@test
def test_artifact_lint_story_size_limits(_t):
    """Больше десяти шагов основного сценария и больше пяти групп критериев — US-18."""
    AL = _lint()
    steps = "".join(f"{n}. **Шаг {n}**\n   1. Действие пользователя: делает\n"
                    f"   2. Реакция системы: отвечает\n" for n in range(1, 12))
    big = GOOD_US.replace(
        GOOD_US[GOOD_US.index("1. **Документ получен**"):GOOD_US.index("## Acceptance criteria")],
        steps + "\n")
    groups = "".join(f"### Группа {n}\n\n1. Система делает {n}.\n\n" for n in range(1, 7))
    big = big.replace(big[big.index("### Приём"):big.index("## Требования")], groups)
    msgs = [i["msg"] for i in AL.lint_text(big, "us") if i["id"] == "US-18"]
    assert any("11 шагов" in m for m in msgs) and any("6 групп" in m for m in msgs), msgs
    # Ровно на пределе — замечаний нет.
    assert not [i for i in AL.lint_text(GOOD_US, "us") if i["id"] == "US-18"]


@test
def test_artifact_lint_spec_and_ac_rules(_t):
    """Спека: сценарий ↔ строка критериев, EARS, число в НФТ, пустые вопросы, основания; AC: разделы."""
    AL = _lint()
    assert AL.lint_text(GOOD_SPEC, "spec") == [], AL.lint_text(GOOD_SPEC, "spec")
    bad = (GOOD_SPEC.replace("| Сценарий 2 | отказ с причиной |\n", "")
           .replace("система должна отказать в приёме", "отказ")
           .replace("- Время приёма не более 5 секунд для документа до 10 МБ", "- Приём должен быть быстрым")
           .replace("## Открытые вопросы\n", "## Открытые вопросы\n- Какой канал резервный?\n")
           .replace('based_on: ["[[Приём-документа]]"]', "based_on: []             # основания"))
    ids = _ids(AL.lint_text(bad, "spec"))
    assert ids == ["SPEC-02", "SPEC-04", "SPEC-05", "SPEC-06", "SPEC-07"], ids
    ac_ok = ("# История изменений\n\n| Код | AC-1 |\n\n# Описание\n\nТекст.\n\n"
             "# Критерии приемки\n\n1. Система делает.\n")
    assert AL.lint_text(ac_ok, "ac") == []
    ids = _ids(AL.lint_text("# Описание\n\nТекст.\n\n# Критерии приёмки\n\nпусто\n", "ac"))
    assert ids == ["AC-01", "AC-02"], ids


@test
def test_artifact_lint_in_kb_lint_is_a_warning_only(tmp: Path):
    """`kb:lint`: замечания по форме артефактов печатаются, но код возврата и счёт ошибок не меняют."""
    root = make_project(tmp)
    (root / "AuroraKnowledgeDB/Concepts").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/Concepts/Понятие.md").write_text(
        '---\ntitle: "Понятие"\ntype: concept\nstatus: draft\nrelated: ["[[Понятие]]"]\n---\n\n'
        "# Понятие\n\nТекст.\n", encoding="utf-8")
    clean = run("kb_lint.py", cwd=root)
    assert "замечаний по форме" not in clean.stdout, clean.stdout      # артефактов нет — строки нет
    (root / "Artifacts/us").mkdir(parents=True, exist_ok=True)
    (root / "Artifacts/us/US-1.md").write_text("# История\n\n{{Роль}} ???\n", encoding="utf-8")
    (root / "Artifacts/us/README.md").write_text("# Папка\n", encoding="utf-8")
    (root / "Artifacts/ac/AC-1.md").write_text(
        "# История изменений\n\n# Описание\n\nТекст.\n\n# Критерии приемки\n\n1. Система делает.\n",
        encoding="utf-8")
    cp = run("kb_lint.py", cwd=root)
    assert cp.returncode == clean.returncode, (cp.returncode, cp.stdout)
    head = [l for l in cp.stdout.splitlines() if l.startswith("kb_lint: карточек")]
    assert head == [l for l in clean.stdout.splitlines() if l.startswith("kb_lint: карточек")], cp.stdout
    assert "артефактов 2, замечаний по форме" in cp.stdout and "Artifacts/us/US-1.md" in cp.stdout, cp.stdout
    assert "AC-1.md" not in cp.stdout, "чистая история критериев названа в замечаниях"
    assert "README.md" not in cp.stdout
    # Хук коммита судит только переданные карточки: артефакты он не читает.
    only = run("kb_lint.py", "--only", "AuroraKnowledgeDB/Concepts/Понятие.md", cwd=root)
    assert "замечаний по форме" not in only.stdout, only.stdout
    # Свой каталог вида `us` из реестра `artifacts:` проекта.
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "artifacts:\n  us:\n    template: T/us.md\n"
                   "    out: Deliverables/work\n", encoding="utf-8")
    (root / "Deliverables/work").mkdir(parents=True, exist_ok=True)
    (root / "Deliverables/work/Моя.md").write_text("# История\n\n{{Роль}}\n", encoding="utf-8")
    cp = run("kb_lint.py", "--summary", cwd=root)
    assert "Deliverables/work" not in cp.stdout and "артефактов 2," in cp.stdout, cp.stdout
    cli = run("artifact_lint.py", cwd=root, expect_rc=1)
    assert "Deliverables/work/Моя.md" in cli.stdout and "US-23" in cli.stdout, cli.stdout
