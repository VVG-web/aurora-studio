"""Проверки движка Aurora, часть 41. Каркас и помощники — tests/harness.py.

Детерминированный линтер артефактов (BL-TEAM-05): `scripts/artifact_lint.py` решает кодом то,
что чек-лист ревью отдавал модели, — разделы шаблона, формулу истории, пару «Действие /
Реакция» у шагов, число шагов и групп критериев, остатки шаблона, формулировки спеки. Синтетические
артефакты, без модели и сети; в `kb:lint` замечания идут предупреждениями, код возврата не меняют.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

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
    assert ids == ["AC-02"], ids
    ids = _ids(AL.lint_text("# Критерии приёмки\n\n1. Система делает.\n", "ac"))
    assert ids == ["AC-01"], ids


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


# ------------------------------------------------------------------ доработка после ревью

def _filled(path: str, edits=()) -> str:
    """Шаблон вида → «заполненный» документ: комментарии сняты, `{{…}}` заменены значением.

    Так проверяется, что линтер и поставляемые шаблоны не разошлись: сменят заголовок в
    шаблоне — тест упадёт, а не заведёт молча ложные замечания на каждом новом артефакте.
    Плейсхолдер с тире («код — название — что меняется») получает значение с теми же тире.
    """
    text = (KIT / path).read_text(encoding="utf-8")
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    rx = re.compile(r"\{\{(?:[^{}]|\{[^{}]*\})*\}\}")
    while rx.search(text):
        text = rx.sub(lambda m: " — ".join(["Значение 5"] * (m.group(0).count("—") + 1)), text)
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    return text


@test
def test_artifact_lint_shipped_templates_give_no_structure_findings(_t):
    """Шаблон → заполненный документ → линтер без замечаний: правила привязаны к шаблонам."""
    AL = _lint()
    for path, profile, edits in (
        ("scaffold/Templates/user_story_template.md", "us", ()),
        ("scaffold/TemplatesCommon/us_v1.0.md", "us", ()),
        ("scaffold/Templates/AC_template.md", "ac", ()),
        ("scaffold/TemplatesCommon/ac_v1.0.md", "ac", ()),
        # Основания спеки и открытые вопросы автор заполняет и опустошает сам.
        ("scaffold/Templates/spec_template.md", "spec",
         (("based_on: []", 'based_on: ["[[Основание]]"]'),
          ("## Открытые вопросы\nЗначение 5", "## Открытые вопросы\n"))),
    ):
        found = AL.lint_text(_filled(path, edits), profile)
        assert found == [], (path, found)


@test
def test_artifact_lint_code_spans_are_values_not_emptiness(_t):
    """Значение в обратных кавычках — значение: шаблон сам велит писать так технические имена."""
    AL = _lint()
    coded = (GOOD_US.replace("| Реализует | RU.PRJ.REQ-010 |", "| Реализует | `RU.PRJ.REQ-010` |")
             .replace("Реакция системы: принимает документ", "Реакция системы: `принимает`")
             .replace("| **Как** | Оператор системы |", "| **Как** | `Оператор` |")
             .replace("Система отказывает в приёме повреждённого документа.",
                      "См. алгоритм `RU.PRJ.ALG-010`."))
    assert AL.lint_text(coded, "us") == [], AL.lint_text(coded, "us")
    # А `{{…}}` в обратных кавычках — пример, а не остаток шаблона.
    shown = GOOD_US.replace("Система принимает документ.", "Плейсхолдеры пишут как `{{имя}}`.")
    assert AL.lint_text(shown, "us") == []


@test
def test_artifact_lint_template_leftovers_and_ac_numbering(_t):
    """«УДАЛИТЬ ПЕРЕД ПУБЛИКАЦИЕЙ» и «уточнить у» — остатки; у критериев свой номер вопроса."""
    AL = _lint()
    only_delete = GOOD_US.replace("## Описание", "<!-- УДАЛИТЬ ПЕРЕД ПУБЛИКАЦИЕЙ: название -->\n\n## Описание")
    assert "US-23" in _ids(AL.lint_text(only_delete, "us"))
    note = GOOD_US.replace("Нет требований.\n\n## How", "Уточнить у аналитика.\n\n## How")
    assert "US-23" in _ids(AL.lint_text(note, "us")), AL.lint_text(note, "us")
    ac = "# Описание\n\nТекст.\n\n# Критерии приемки\n\n1. Система делает. {{срок}}\n"
    assert _ids(AL.lint_text(ac, "ac")) == ["AC-03"], AL.lint_text(ac, "ac")


@test
def test_artifact_lint_ac_history_is_optional(_t):
    """AC в форме `AC_template.md` (без «Истории изменений») чист: шаблон по умолчанию её не содержит."""
    AL = _lint()
    ok = ("# Описание\n\n|   |   |\n| --- | --- |\n| **Как** | Оператор |\n\n"
          "## Критерии приемки\n\n### Приём\n\n1. Система принимает документ.\n")
    assert AL.lint_text(ok, "ac") == []


@test
def test_artifact_lint_spec_accepts_ears_and_nested_then(_t):
    """EARS одной фразой и «Тогда:» с реакцией вложенным пунктом — не дефект формы."""
    AL = _lint()
    one_line = GOOD_SPEC.replace(
        "- **Дано:** документ в очереди\n- **Когда:** приходит запрос на приём\n"
        "- **Тогда:** система должна сохранить документ\n",
        "Когда приходит запрос на приём, система должна сохранить документ.\n")
    assert AL.lint_text(one_line, "spec") == [], AL.lint_text(one_line, "spec")
    nested = GOOD_SPEC.replace("- **Тогда:** система должна отказать в приёме",
                               "- **Тогда:**\n  - система должна отказать в приёме")
    assert AL.lint_text(nested, "spec") == [], AL.lint_text(nested, "spec")
    # Без «должна» и во вложенном пункте замечание остаётся.
    bad = GOOD_SPEC.replace("- **Тогда:** система должна отказать в приёме",
                            "- **Тогда:**\n  - отказ")
    assert _ids(AL.lint_text(bad, "spec")) == ["SPEC-04"], AL.lint_text(bad, "spec")
    # Нет ни ключей, ни EARS — прежние замечания.
    none = GOOD_SPEC.replace("- **Дано:** документ повреждён\n", "")
    assert _ids(AL.lint_text(none, "spec")) == ["SPEC-03"]


@test
def test_artifact_lint_template_hint_is_not_a_scenario(_t):
    """Подсказка шаблона спеки про EARS не заменяет «Когда / Тогда» и не служит значением."""
    AL = _lint()
    hint = ("_Формулировки — в EARS-стиле: «Когда <триггер>, система должна <реакция>» — "
            "однозначно и переводимо в тест._")
    only_given = GOOD_SPEC.replace(
        "- **Когда:** приходит запрос на приём\n- **Тогда:** система должна сохранить документ\n",
        "\n" + hint + "\n", 1)
    got = AL.lint_text(only_given, "spec")
    assert _ids(got) == ["SPEC-03"] and len(got) == 2, got            # нет «Когда» и «Тогда»
    then_hint = GOOD_SPEC.replace("- **Тогда:** система должна сохранить документ\n",
                                  "- **Тогда:**\n\n" + hint + "\n", 1)
    assert "SPEC-03" in _ids(AL.lint_text(then_hint, "spec")), AL.lint_text(then_hint, "spec")
    bad_then = GOOD_SPEC.replace("- **Тогда:** система должна сохранить документ\n",
                                 "- **Тогда:** отказ\n\n" + hint + "\n", 1)
    assert _ids(AL.lint_text(bad_then, "spec")) == ["SPEC-04"], AL.lint_text(bad_then, "spec")
    # Полный сценарий с подсказкой под ним по-прежнему чист.
    full = GOOD_SPEC.replace("- **Тогда:** система должна сохранить документ\n",
                             "- **Тогда:** система должна сохранить документ\n\n" + hint + "\n", 1)
    assert AL.lint_text(full, "spec") == [], AL.lint_text(full, "spec")


@test
def test_artifact_lint_todo_mark_in_code_is_residue(_t):
    """`TODO: {{…}}` — метка неизвестного из шаблона истории: в кавычках она тоже остаток."""
    AL = _lint()
    mark = "`TODO: {{чего не хватает}}`"
    us = GOOD_US.replace("Система принимает документ.", "Система принимает документ. " + mark)
    assert "US-23" in _ids(AL.lint_text(us, "us")), AL.lint_text(us, "us")
    ac = "# Описание\n\nТекст " + mark + ".\n\n# Критерии приемки\n\n1. Система делает.\n"
    assert _ids(AL.lint_text(ac, "ac")) == ["AC-03"], AL.lint_text(ac, "ac")
    spec = GOOD_SPEC.replace("- **Тогда:** система должна отказать в приёме",
                             "- **Тогда:** система должна отказать в приёме " + mark)
    assert "SPEC-01" in _ids(AL.lint_text(spec, "spec")), AL.lint_text(spec, "spec")
    # Пример плейсхолдера в кавычках остаётся примером.
    shown = GOOD_US.replace("Система принимает документ.", "Плейсхолдеры пишут как `{{имя}}`.")
    assert AL.lint_text(shown, "us") == []


@test
def test_artifact_lint_hint_filter_keeps_lines_with_one_placeholder(_t):
    """Подсказка шаблона — курсивная строка или два «<…>» подряд; одно «<имя>» — обычная фраза."""
    AL = _lint()
    then = GOOD_SPEC.replace("- **Тогда:** система должна сохранить документ",
                             "- **Тогда:** система должна вернуть сообщение «Файл <имя> не найден»")
    assert AL.lint_text(then, "spec") == [], AL.lint_text(then, "spec")
    when = GOOD_SPEC.replace("- **Когда:** приходит запрос на приём",
                             "- **Когда:** оператор нажимает кнопку <Сохранить>")
    assert AL.lint_text(when, "spec") == [], AL.lint_text(when, "spec")
    nested = GOOD_SPEC.replace("- **Тогда:** система должна отказать в приёме",
                               "- **Тогда:**\n  - система должна показать <причина отказа>")
    assert AL.lint_text(nested, "spec") == [], AL.lint_text(nested, "spec")
    # Два плейсхолдера в одной строке без курсива — по-прежнему подсказка, а не сценарий.
    two = ("\n«Когда <триггер>, система должна <реакция>»\n")
    only_given = GOOD_SPEC.replace(
        "- **Когда:** приходит запрос на приём\n- **Тогда:** система должна сохранить документ\n",
        two, 1)
    got = AL.lint_text(only_given, "spec")
    assert _ids(got) == ["SPEC-03"] and len(got) == 2, got
    # Строка, что начинается с «Тогда:» / «Когда:», — сценарий, даже с двумя «<…>» в значении.
    two_then = GOOD_SPEC.replace("- **Тогда:** система должна сохранить документ",
                                 "- **Тогда:** система должна показать <Имя> и <Фамилия>")
    assert AL.lint_text(two_then, "spec") == [], AL.lint_text(two_then, "spec")
    two_when = GOOD_SPEC.replace("- **Когда:** приходит запрос на приём",
                                 "Когда: оператор выбирает <Да> или <Нет>")
    assert AL.lint_text(two_when, "spec") == [], AL.lint_text(two_when, "spec")


@test
def test_artifact_lint_closing_hashes_and_formula_forms_still_read(_t):
    """Разбор заголовка и поля кодом, а не `\\s*#*\\s*$`, читает те же формы, что и раньше."""
    AL = _lint()
    closed = GOOD_US.replace("## Описание", "## Описание ##", 1)
    assert AL.lint_text(closed, "us") == [], AL.lint_text(closed, "us")
    colon = GOOD_US.replace("| **Как** | Оператор системы |\n| **Я хочу** | получить документ из смежной системы |\n"
                            "| **Чтобы** | документ был сохранён и доступен для обработки |",
                            "**Как**: Оператор системы\n**Я хочу**: получить документ\n**Чтобы**: он был сохранён")
    assert AL.lint_text(colon, "us") == [], AL.lint_text(colon, "us")
    one = GOOD_US.replace("| **Как** | Оператор системы |\n| **Я хочу** | получить документ из смежной системы |\n"
                          "| **Чтобы** | документ был сохранён и доступен для обработки |",
                          "Как оператор, я хочу получить документ, чтобы он был сохранён.")
    assert AL.lint_text(one, "us") == [], AL.lint_text(one, "us")
    empty = GOOD_US.replace("| **Я хочу** | получить документ из смежной системы |", "| **Я хочу** |  |")
    assert _ids(AL.lint_text(empty, "us")) == ["US-01"], AL.lint_text(empty, "us")
    # Ключи формулы в таблице читаются без учёта регистра: пустая строчная таблица — не «чисто».
    low = GOOD_US.replace("| **Как** | Оператор системы |\n| **Я хочу** | получить документ из смежной системы |\n"
                          "| **Чтобы** | документ был сохранён и доступен для обработки |",
                          "| как |  |\n| я хочу |  |\n| чтобы |  |")
    got = AL.lint_text(low, "us")
    assert _ids(got) == ["US-01"] and "«Как», «Я хочу», «Чтобы»" in got[0]["msg"], got


@test
def test_artifact_lint_long_whitespace_runs_are_linear(_t):
    """Строка из 200000 пробелов (или nbsp, табов) не вешает линтер: регулярные выражения без отката.

    Артефакт приходит от модели или из вики, а `kb:lint` идёт шагом маршрута без потолка по
    времени. Счёт — в дочернем процессе с пределом: на старых выражениях заголовок с 1600
    пробелами разбирался десять секунд, 200000 не кончились бы никогда.
    """
    import json
    from aurora_common import clean_copy
    cases = []

    def before_mark(extra: str) -> str:
        # Хвост после MADE_MARK линтер отбрасывает (`clean_copy`): проверяемое ставим выше него.
        return GOOD_US.replace("<!-- ниже", extra + "\n<!-- ниже", 1)

    for ch in (" ", "\u00a0", "\t"):
        sp = ch * 200000
        cases += [
            ("head", GOOD_US.replace("## Описание", "## Описание" + sp + "x", 1), "us"),
            ("head-closed", before_mark("## x" + sp + "##" + sp), "us"),
            ("head-empty", before_mark("##" + sp + "\nx"), "us"),
            ("row", GOOD_US.replace("| Реализует | RU.PRJ.REQ-010 |",
                                    "| Реализует |" + sp + "a" + sp + "b |", 1), "us"),
            ("formula-row", GOOD_US.replace("| **Как** | Оператор системы |",
                                            "|" + sp + "**Как**" + sp + "|" + sp, 1), "us"),
            ("step", GOOD_US.replace("Действие пользователя: не выполняет",
                                     "Действие пользователя" + sp + "x", 1), "us"),
            ("step-stars", GOOD_US.replace("Реакция системы: принимает документ",
                                           "Реакция системы" + sp + "*" + sp + "x", 1), "us"),
            ("blank", before_mark(sp + "\n" + sp + "- x" + sp), "us"),
            ("vague", before_mark("см." + sp + "алгоритм" + sp + "x"), "us"),
            ("ac", "# Описание" + sp + "\n\n# Критерии приемки" + sp + "\n\n1." + sp + "x\n", "ac"),
            ("spec-key", GOOD_SPEC.replace("- **Когда:** приходит запрос на приём",
                                           "-" + sp + "**Когда:**" + sp + "*" + sp, 1), "spec"),
            ("spec-then", GOOD_SPEC.replace("- **Тогда:** система должна сохранить документ",
                                            "- **Тогда:**" + sp + "\n" + sp, 1), "spec"),
            ("spec-ears", GOOD_SPEC + "\n### Сценарий 3\nесли ," + sp + "x\n", "spec"),
            ("based-on", "---\nbased_on: [a]" + sp + "x\n---\n" + GOOD_SPEC, "spec"),
        ]
    # И не только пробелы. Много «как» без «я хочу» (таблицу формулы заменяем на них, иначе
    # «я хочу» находится сразу и старое выражение не откатывается); незакрытые «<!--» и «```»,
    # «{{» без «}», «- - - …» в открытых вопросах — тот же откат у остальных выражений.
    table = ("| **Как** | Оператор системы |\n| **Я хочу** | получить документ из смежной системы |\n"
             "| **Чтобы** | документ был сохранён и доступен для обработки |")
    assert table in GOOD_US
    cases += [
        ("formula-words", GOOD_US.replace(table, "как " * 30000, 1), "us"),
        ("comment-open", before_mark("<!--" * 30000), "us"),
        ("fence-open", before_mark("```a\n" * 30000), "us"),
        ("braces", before_mark("{{" * 60000), "us"),
        ("open-questions", GOOD_SPEC.replace("## Открытые вопросы\n", "## Открытые вопросы\n" + "- " * 60000 + "x\n", 1), "spec"),
    ]
    # Случай, который после `clean_copy` короче проверяемого, ничего не проверяет.
    short = [n for n, text, _ in cases if len(clean_copy(text)) < 100000]
    assert not short, f"хвост отрезан MADE_MARK, случай пуст: {short}"
    child = ("import json, sys, time\nsys.path.insert(0, sys.argv[1])\nimport artifact_lint as AL\n"
             "out = []\nfor name, text, prof in json.load(sys.stdin):\n"
             "    t = time.time(); AL.lint_text(text, prof); out.append((name, time.time() - t))\n"
             "print(json.dumps(out))\n")
    try:
        cp = subprocess.run([sys.executable, "-I", "-c", child, str(SCRIPTS)], input=json.dumps(cases),
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    except subprocess.TimeoutExpired:
        raise AssertionError("линтер не уложился в 90 с на длинных сериях пробелов: откат в регулярном выражении")
    assert cp.returncode == 0, cp.stderr[-500:]
    slow = [(n, round(t, 2)) for n, t in json.loads(cp.stdout.strip().splitlines()[-1]) if t > 1.0]
    assert not slow, f"дольше секунды: {slow}"


@test
def test_artifact_lint_code_scanners_read_like_the_old_regexes(_t):
    """Разбор блоков кода, комментариев, `{{…}}` и «нет вопросов» кодом даёт то же, что давали выражения."""
    import random
    AL = _lint()
    fence = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$")
    comment = re.compile(r"(?s)<!--.*?-->")
    holder = re.compile(r"\{\{[^}\n]*\}\}?")
    none = re.compile(r"(?i)^\W*(нет|пусто|—|-)\W*$")
    toks = ["```", "~~~", "``", "\n", "\n", "\n", " ", "\t", "a", "x", "<!--", "-->", "<!---->", "<!-->",
            "{{", "}}", "}", "{", "\r\n", "- ", "—", "-", "нет", "пусто", "Нет"]
    rnd = random.Random(7)
    for _ in range(6000):
        text = "".join(rnd.choice(toks) for _ in range(rnd.randint(0, 25)))
        assert AL._strip_fences(text) == fence.sub("", text), repr(text)
        assert AL._strip_comments(text) == comment.sub("", text), repr(text)
        assert AL._placeholders(text) == holder.findall(text), repr(text)
        for line in text.split("\n"):
            core = line.strip()
            if core:
                assert AL._is_none_mark(core) == bool(none.match(core)), repr(core)


@test
def test_artifact_lint_project_follows_symlinks_without_crash(tmp: Path):
    """Файл-ссылка за пределами папок видов не роняет проверку, а путь в отчёте — проектный."""
    AL = _lint()
    root = make_project(tmp)
    us = root / "Artifacts/us"
    us.mkdir(parents=True, exist_ok=True)
    shared = root / "outside"
    shared.mkdir()
    (shared / "US-9.md").write_text("# История\n\nВольный текст.\n", encoding="utf-8")
    try:
        os.symlink(shared / "US-9.md", us / "US-9.md")
    except (OSError, NotImplementedError):
        return                                                      # ссылки недоступны (Windows без прав)
    checked, found = AL.lint_project(str(root))
    assert checked == 1 and [f[0] for f in found] == ["Artifacts/us/US-9.md"], (checked, found)
    # Папка вида — ссылка на общий каталог: путь остаётся «Artifacts/us/…», а не «../shared/…».
    (us / "US-9.md").unlink()
    (root / "Artifacts/us").rmdir()
    (root / "shared_us").mkdir()
    (root / "shared_us/US-1.md").write_text("# История\n\nВольный текст.\n", encoding="utf-8")
    os.symlink(root / "shared_us", root / "Artifacts/us")
    checked, found = AL.lint_project(str(root))
    assert checked == 1 and [f[0] for f in found] == ["Artifacts/us/US-1.md"], (checked, found)


@test
def test_artifact_lint_project_judges_a_file_by_one_profile(tmp: Path):
    """Виды `us` и `ac` с общей или вложенной папкой: файл судит один профиль и считается раз."""
    AL = _lint()
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "artifacts:\n  us:\n    template: T/us.md\n    out: Deliverables/work\n"
                   "  ac:\n    template: T/ac.md\n    out: Deliverables/work\n", encoding="utf-8")
    work = root / "Deliverables/work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "A.md").write_text(GOOD_US, encoding="utf-8")
    cwd = os.getcwd()
    os.chdir(root)
    try:
        checked, found = AL.lint_project(".")
        assert checked == 1 and found == [], (checked, found)        # один файл — один раз, как история
        # Вложенная папка отдаётся профилю с более глубокой папкой.
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "  ac:\n    template: T/ac.md\n    out: Deliverables/work\n",
            "  ac:\n    template: T/ac.md\n    out: Deliverables/work/ac\n"), encoding="utf-8")
        (work / "ac").mkdir(exist_ok=True)
        (work / "ac/B.md").write_text("# Описание\n\nТекст.\n\n# Критерии приемки\n\n1. Система делает.\n",
                                      encoding="utf-8")
        checked, found = AL.lint_project(".")
        assert checked == 2 and found == [], (checked, found)
    finally:
        os.chdir(cwd)


@test
def test_artifact_lint_warnings_are_not_counted_as_base_errors(tmp: Path):
    """Предупреждения о форме не попадают в виды ошибок `ops:todo` и «Здоровья» панели."""
    root = make_project(tmp)
    (root / "Artifacts/us").mkdir(parents=True, exist_ok=True)
    (root / "Artifacts/us/US-1.md").write_text("# История\n\nВольный текст.\n", encoding="utf-8")
    full = run("kb_lint.py", "--full", cwd=root).stdout
    assert "форма артефактов" in full and "замечаний по форме" in full, full
    errs = re.search(r"ошибок (\d+)", full)
    assert errs and errs.group(1) == "0", full
    # ops:todo разбирает вывод линтера по разделам «## вид: N»
    sys.path.insert(0, str(SCRIPTS))
    todo = importlib.import_module("aurora_todo")
    assert todo.lint_sections(full) == {}, todo.lint_sections(full)
    out = run("aurora_todo.py", cwd=root).stdout
    assert "ошибок базы" not in out and "после последней починки" not in out, out
    run("kb_lint.py", "--residue", cwd=root)
    out = run("aurora_todo.py", cwd=root).stdout
    assert "ошибок базы" not in out and "после последней починки" not in out, out
    # «Здоровье» панели берёт виды ошибок тем же образом
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    info = ck._health_lint(str(root), "ru")["lint"]
    assert info.get("errors") == 0 and info.get("kinds") == {}, info


# ----------------------------------------------------------------------------------------
# BL-SEC-16: один разбор шапки карточки — блочные списки и «---» внутри значений.

def _head_card(root: Path, rel: str, head: str, body: str = "Тело.\n") -> Path:
    p = root / "AuroraKnowledgeDB" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{head}---\n\n{body}", encoding="utf-8")
    return p


@test
def test_card_head_reads_block_lists_like_inline_ones(tmp: Path):
    """`aliases`, `tags`, `related` в блочной записи читаются так же, как в строчной.

    Движок сам пишет `related:` (`kb:links --cards`) и `aliases:` (переименование) блоком, а
    `frontmatter` видел в таком поле пустую строку: граф без связей, синонимы мимо поиска,
    теги мимо карт содержания. Старая строчная запись читается как раньше.
    """
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    block = ('title: "Заявление"\ntype: concept\naliases:\n  - "Заявление"\n  - \'Request\'\n'
             'tags:\n  - тема\n  - "платежи"\nrelated:\n  - "[ЭСФ](ЭСФ.md)"\n  - "[Декларация](Декларация.md)"\n'
             'status: draft\n')
    inline = ('title: "Заявление"\ntype: concept\naliases: ["Заявление", "Request"]\n'
              'tags: ["тема", "платежи"]\nrelated: ["[ЭСФ](ЭСФ.md)", "[Декларация](Декларация.md)"]\n'
              'status: draft\n')
    fb, fi = AC.frontmatter(f"---\n{block}---\n\nТело.\n"), AC.frontmatter(f"---\n{inline}---\n\nТело.\n")
    assert fb == fi, (fb, fi)
    assert re.findall(r'"([^"]+)"', fb["aliases"]) == ["Заявление", "Request"], fb["aliases"]
    assert fb["status"] == "draft" and fb["type"] == "concept", fb
    for head in (block, inline):
        text = f"---\n{head}---\n\nТело.\n"
        assert AC.aliases(text) == ["Заявление", "Request"], head
        assert AC.list_field(text, "tags") == ["тема", "платежи"], head
        assert AC.related_names(text) == ["ЭСФ", "Декларация"], (head, AC.related_names(text))
    # прежние записи: список без кавычек в строку, одиночное значение, пустое поле
    old = '---\ntitle: "Т"\ntags: [тема, платежи]\naliases:\nrelated: [А, Б]\nstatus: index\n---\n\nx\n'
    assert AC.frontmatter(old)["tags"] == "[тема, платежи]" and AC.frontmatter(old)["aliases"] == ""
    assert AC.related_names(old) == ["А", "Б"], AC.related_names(old)
    # знак конца строки внутри значения не рождает пункта списка: строки шапки — настоящие
    forged = '---\ntitle: "Блог   - подложный"\naliases:\n  - "Настоящий"\n---\n\nx\n'
    assert AC.aliases(forged) == ["Настоящий"], AC.aliases(forged)
    assert AC.frontmatter(forged)["aliases"] == '["Настоящий"]', AC.frontmatter(forged)
    # читалки доверия: повтор и подделка шапки видны по-прежнему
    dup = '---\nstatus: draft\nstatus:\n  - knowledge\n---\n\nx\n'
    assert AC.frontmatter(dup, unique=True).get("status") is None


@test
def test_cards_graph_sees_block_related_and_aliases(tmp: Path):
    """Граф базы видит `related:` блоком и синонимы блоком: связь есть и без ссылки в теле."""
    sys.path.insert(0, str(SCRIPTS))
    G = importlib.import_module("kb_graph")
    root = make_project(tmp)
    _head_card(root, "Concepts/ЭСФ.md", 'title: "ЭСФ"\ntype: concept\nstatus: knowledge\n')
    _head_card(root, "Concepts/Декларация.md", 'title: "Декларация"\ntype: concept\nstatus: knowledge\n')
    _head_card(root, "Concepts/Реестр.md",
               'title: "Реестр"\ntype: concept\nstatus: knowledge\nrelated:\n  - "[ЭСФ](ЭСФ.md)"\n')
    # ссылка по прежнему имени: оно записано блоком aliases и раньше терялось
    _head_card(root, "Concepts/Счёт.md",
               'title: "Счёт"\ntype: concept\nstatus: knowledge\naliases:\n  - "Фактура"\n')
    _head_card(root, "Concepts/Учёт.md", 'title: "Учёт"\ntype: concept\nstatus: knowledge\n',
               "Ведётся по [[Декларация|декларации]] и по [[Счёт|Фактура]].\n")
    cwd = os.getcwd()
    try:
        os.chdir(root)
        data = G.write_cards_graph("AuroraKnowledgeDB/meta/graph.json")
    finally:
        os.chdir(cwd)
    pairs = {(e["from"], e["to"]): e for e in data["edges"]}
    assert pairs.get(("Реестр", "ЭСФ"), {}).get("rel") == "связана", pairs
    assert pairs[("Счёт", "Учёт")]["conf"] == "EXTRACTED", pairs[("Счёт", "Учёт")]


@test
def test_card_body_ignores_dashes_inside_head_values(tmp: Path):
    """«---» внутри значения шапки не обрывает её: тело берётся после закрывающей черты.

    Срез `text.split("---", 2)[-1]` в шести местах отдавал телом хвост заголовка
    «Ретро --- итоги» вместе с остатком шапки.
    """
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    KSQ = importlib.import_module("kb_search_quality")
    IDX = importlib.import_module("kb_index")
    text = '---\ntitle: "Ретро --- итоги"\nstatus: draft\n---\n\nТезис про ретроспективу команды.\n'
    assert AC.body(text).strip() == "Тезис про ретроспективу команды.", repr(AC.body(text))
    assert KSQ.thesis(text) == "Тезис про ретроспективу команды.", KSQ.thesis(text)
    root = tmp / "kb"
    (root / "Concepts").mkdir(parents=True)
    (root / "Concepts" / "Ретро.md").write_text(text, encoding="utf-8")
    rows = IDX.collect(str(root / "Concepts"))
    assert rows and "Тезис про ретроспективу" in rows[0]["desc"], rows
    # навык: тело без шапки, в шапке — «---» в описании
    skill = tmp / "p" / ".claude" / "skills" / "ретро" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: ретро\ndescription: "Итоги --- и выводы"\n---\n\nДелай так.\n',
                     encoding="utf-8")
    RC = importlib.import_module("request_context")
    path, body = RC.find_skill("ретро", str(tmp / "p"), kit=str(tmp / "nokit"))
    assert path and body == "Делай так.", (path, body)
    # файл без закрывающей черты не падает (раньше web_export брал [2] и ловил IndexError)
    assert AC.body("---\ntitle: x\nбез закрытия") == "---\ntitle: x\nбез закрытия"


@test
def test_jira_mirror_head_longer_than_the_old_window_is_read(tmp: Path):
    """Шапка зеркала Jira длиннее 2000 знаков читается целиком: заголовок задачи не теряется."""
    sys.path.insert(0, str(SCRIPTS))
    G = importlib.import_module("kb_graph")
    jira = tmp / "JIRA"
    jira.mkdir()
    pad = "".join(f'  - "метка-{i:04d}"\n' for i in range(250))
    (jira / "PRJ-7.md").write_text(
        f'---\nlabels:\n{pad}title: "PRJ-7 US 1.2.3 Заявление"\nstatus: "Закрыто"\n---\n\nx\n',
        encoding="utf-8")
    g = G.Graph.__new__(G.Graph)
    g.issues = {}
    G.Graph.read_jira(g, str(jira))
    assert g.issues.get("PRJ-7", {}).get("summary", "").endswith("Заявление"), g.issues
    assert g.issues["PRJ-7"]["status"] == "Закрыто", g.issues


@test
def test_base_names_reads_block_aliases_and_long_heads(tmp: Path):
    """Имена базы для «Спросить» включают синонимы блоком и из шапки длиннее 600 знаков.

    `base_names` брал синонимы регуляркой по первым 600 знакам: блочная запись (так пишет
    переименование) и длинная шапка были невидимы, и `[[прежнее имя]]` из ответа
    попадало в «выдумано» вместо «есть в базе».
    """
    sys.path.insert(0, str(SCRIPTS))
    AR = importlib.import_module("agent_runner")
    root = make_project(tmp)
    _head_card(root, "Concepts/Счёт.md",
               'title: "Счёт"\ntype: concept\naliases:\n  - "Фактура"\n  - \'Invoice\'\nstatus: knowledge\n')
    _head_card(root, "Concepts/Платёж.md",
               'title: "Платёж"\ntype: concept\naliases: ["Оплата"]\nstatus: knowledge\n')
    pad = "".join(f'  - "метка-{i:04d}"\n' for i in range(120))
    _head_card(root, "Concepts/Длинная.md",
               f'title: "Длинная"\nlabels:\n{pad}aliases:\n  - "Далёкий синоним"\nstatus: knowledge\n')
    names = AR.base_names(str(root))
    assert {"Счёт", "Фактура", "Invoice", "Платёж", "Оплата", "Далёкий синоним"} <= names, names
    res = AR.classify_links("См. [[Фактура]] и [[Далёкий синоним]].", [], "", str(root))
    assert not res.get("invented"), res


@test
def test_card_head_blocks_tolerate_comments_and_blank_lines(tmp: Path):
    """Пустая строка и комментарий внутри блока его не закрывают; `frontmatter` и `list_field` согласны.

    Раньше `aliases:\\n  - A\\n\\n  - B` терял B, а для `aliases: # синонимы` с пунктами
    `list_field` отдавал пункты, `frontmatter` — строку «# синонимы».
    """
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    text = '---\ntitle: "Т"\naliases: # синонимы\n  - A\n\n  # пояснение\n  - "B"\nstatus: draft\n---\n\nx\n'
    assert AC.list_field(text, "aliases") == ["A", "B"], AC.list_field(text, "aliases")
    assert AC.frontmatter(text)["aliases"] == '["A", "B"]', AC.frontmatter(text)
    assert AC.frontmatter(text)["status"] == "draft"
    # поле только с комментарием и без пунктов остаётся как было
    lone = '---\naliases: # синонимы\nstatus: draft\n---\n\nx\n'
    assert AC.list_field(lone, "aliases") == [] and AC.frontmatter(lone)["status"] == "draft"
    # блок закрывает любая другая строка, а не пустая
    shut = '---\naliases:\n  - A\ntitle: "Т"\n  - не мой\n---\n\nx\n'
    assert AC.list_field(shut, "aliases") == ["A"], AC.list_field(shut, "aliases")


@test
def test_card_head_item_has_no_quadratic_backtracking(tmp: Path):
    """Пункт списка со множеством пробелов разбирается за линейное время.

    Регулярка пункта на `-`, 60000 пробелов, слово, 60000 пробелов и второе слово
    откатывалась квадратично (секунды на одной строке шапки).
    """
    import time
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    line = "-" + " " * 60000 + "a" + " " * 60000 + "b"
    t0 = time.time()
    assert AC.head_item(line) == "a" + " " * 60000 + "b", len(AC.head_item(line) or "")
    assert len(AC.head_blocks("aliases:\n" + line + "\n")["aliases"]) == 1
    assert time.time() - t0 < 1.0, time.time() - t0
    assert AC.head_item('- "x"') == "x" and AC.head_item("-y") == "y" and AC.head_item("-") is None
    assert AC.head_item("text") is None


@test
def test_card_frontmatter_has_no_quadratic_backtracking(tmp: Path):
    """`frontmatter()` на заголовке с огромной серией пробелов укладывается в секунду.

    `title` зеркала берётся из `<title>` страницы без ограничения длины, а проверка «значение
    пусто» шла регуляркой с квадратичным откатом: каждый обход базы (граф, индекс, доверие)
    вис на 60000 пробелах между словами на десятки секунд. То же — хвост-комментарий поля
    `status`.
    """
    import time
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    gap = "a" + " " * 60000 + "b"
    text = f'---\ntitle: "{gap}"\nurl: {gap}\nstatus: draft{" " * 60000}x\naliases:\n  - "A"\n---\n\nx\n'
    t0 = time.time()
    fm = AC.frontmatter(text)
    fu = AC.frontmatter(text, unique=True)
    assert time.time() - t0 < 1.0, time.time() - t0
    assert fm["title"] == gap and fm["aliases"] == '["A"]', fm["aliases"]
    assert fu["title"] == gap
    # смысл прежних шаблонов сохранён
    assert AC.head_blank("") and AC.head_blank("   ") and AC.head_blank("# c") and AC.head_blank("  # c")
    assert not AC.head_blank("x # c") and not AC.head_blank("#x".replace("#", "x"))
    assert AC.frontmatter("---\nstatus: draft # note\n---\n")["status"] == "draft"
    assert AC.frontmatter("---\nstatus:   knowledge   #x # y\n---\n")["status"] == "knowledge"
    assert AC.frontmatter("---\nstatus: dr#aft\n---\n")["status"] == "dr#aft"
    assert AC.frontmatter("---\ntitle: A #1\n---\n")["title"] == "A #1"


@test
def test_card_block_writers_drop_the_whole_block_like_the_reader(tmp: Path):
    """Писатели блочных списков снимают блок целиком: комментарий, пустая строка и пункт в нулевой колонке.

    Читатель (`head_blocks`) блок с такими вставками читает целиком, а писатель по прежнему
    шаблону останавливался на первой же вставке; остаток старого блока читался как пункты
    соседнего пустого поля `aliases:` — ложный синоним-путь источника.
    """
    import re
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    BP = importlib.import_module("build_plan")
    KF = importlib.import_module("kb_fix")
    KG = importlib.import_module("kb_graph")
    variants = {
        "comment": '  # imported\n  - "a.md"',
        "blank": '\n  - "a.md"',
        "column0": '- "a.md"',
        "mixed": '  - "z.md"\n\n  # c\n- "a.md"',
    }
    for name, tail in variants.items():
        head = f'title: "Т"\naliases:\nsources:\n{tail}\nstatus: draft\n'
        assert AC.head_blocks(head)["sources"][-1] == "a.md", (name, AC.head_blocks(head))
        # build_plan.with_sources: шапка без разделителей
        got = BP.with_sources(head, ["b.md"])
        assert AC.head_blocks(got)["sources"] == ["b.md"], (name, got)
        assert AC.head_blocks(got)["aliases"] == [], (name, got)
        # aurora_common.with_sources: карточка целиком
        out = AC.with_sources(f"---\n{head}---\n\nx\n", ["b.md"])
        assert AC.aliases(out) == [], (name, out)
        assert AC.card_sources(out)[-1] == "b.md" and "status: draft" in out, (name, out)
        # kb_graph: `related:` с такими вставками заменяется целиком
        rel_head = head.replace("sources:", "related:").replace("a.md", "[A](A.md)")
        m = KG.REL_BLOCK_RE.search(rel_head)
        assert m and rel_head[m.end():].lstrip("\n").startswith("status:"), (name, m and m.group(0))
        # kb_fix.plan_rename: прежние синонимы сохранены, а остаток блока не остаётся
        card = KF.Card("K/Concepts/Старое.md", f"---\ntitle: \"Старое\"\naliases:\n{tail}\nstatus: draft\n---\n\n# Старое\n")
        plan = KF.Plan()
        res, err = KF.plan_rename({"K/Concepts/Старое.md": card}, plan, "Старое", "Новое")
        assert res and not err, (name, res, err)
        new_text = plan.file_writes[next(iter(plan.file_writes))]
        assert "a.md" in AC.aliases(new_text) and new_text.count("a.md") == 1, (name, new_text)
        assert "status: draft" in new_text and AC.frontmatter(new_text)["status"] == "draft", new_text
    # пункты без вставок, как раньше: блок кончается там, где кончаются пункты
    pat = re.compile(r"^aliases:.*" + AC.HEAD_BLOCK_TAIL, re.M)
    plain = 'aliases:\n  - "x"\n  - "y"\nstatus: draft\n'
    assert plain[pat.search(plain).end():] == "\nstatus: draft\n"
    # хвост после последнего пункта (пустая строка, комментарий) блоку не принадлежит
    tailed = 'aliases:\n  - "x"\n\n# конец\nstatus: draft\n'
    assert tailed[pat.search(tailed).end():] == "\n\n# конец\nstatus: draft\n"
    # тысячи пустых строк без пункта не вешают шаблон
    import time
    t0 = time.time()
    assert pat.search("aliases:" + "\n" + (" " * 50 + "\n") * 2000 + "status: draft\n")
    assert time.time() - t0 < 1.0, time.time() - t0


@test
def test_card_head_block_and_inline_agree_in_every_reader(tmp: Path):
    """Одна карточка в блочной и в строчной записи одинаково читается картами, эмбеддингом и ретривом.

    Читатели держатся на `frontmatter()`: kb_moc (теги), kb_embed (синонимы в голове куска),
    ctx_pack (синонимы карточки, связи `related`). Тест ловит разъезд, если разбор шапки
    тихо изменится.
    """
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    MOC = importlib.import_module("kb_moc")
    EMB = importlib.import_module("kb_embed")
    CP = importlib.import_module("ctx_pack")
    block = ('title: "Счёт"\ntype: concept\nstatus: knowledge\naliases:\n  - "Фактура"\n'
             'tags:\n  - платежи\n  - учёт\nrelated:\n  - "[ЭСФ](ЭСФ.md)"\n')
    inline = ('title: "Счёт"\ntype: concept\nstatus: knowledge\naliases: ["Фактура"]\n'
              'tags: [платежи, учёт]\nrelated: ["[ЭСФ](ЭСФ.md)"]\n')
    got = {}
    cwd = os.getcwd()
    for label, head in (("block", block), ("inline", inline)):
        sub = tmp / label
        sub.mkdir()
        root = make_project(sub)
        card = _head_card(root, "Concepts/Счёт.md", head, "Ведётся по документам.\n")
        _head_card(root, "Concepts/ЭСФ.md", 'title: "ЭСФ"\ntype: concept\nstatus: knowledge\n')
        try:
            os.chdir(root)
            moc = MOC.read_cards()["Счёт"]
            texts = EMB.card_texts("AuroraKnowledgeDB")["Счёт"]
            c = CP.Card("AuroraKnowledgeDB/Concepts/Счёт.md", card.read_text(encoding="utf-8"))
            rel = AC.related_targets(card.read_text(encoding="utf-8"))
        finally:
            os.chdir(cwd)
        got[label] = (moc["tags"], "Фактура" in texts, c.aliases, rel)
    assert got["block"] == got["inline"], got
    assert got["block"] == (["платежи", "учёт"], True, ["Фактура"], ["ЭСФ"]), got["block"]


@test
def test_card_head_readers_take_lists_from_one_parser(tmp: Path):
    """Синонимы и теги читаются одним разбором: строчная запись без кавычек и пункт с запятой.

    kb_index и kb_moc рвали пункт `- "Заявление, форма 2"` по запятой, а ctx_pack, kb_graph и
    механическое связывание агента видели только значения в кавычках: `aliases: [Оплата]`
    для них было пустым, блочная запись — нет.
    """
    sys.path.insert(0, str(SCRIPTS))
    IDX = importlib.import_module("kb_index")
    MOC = importlib.import_module("kb_moc")
    CP = importlib.import_module("ctx_pack")
    KG = importlib.import_module("kb_graph")
    cwd = os.getcwd()
    heads = {
        "comma": ('title: "Заявление"\ntype: concept\nstatus: knowledge\naliases:\n'
                  '  - "Заявление, форма 2"\ntags:\n  - "налог, учёт"\n'),
        "bare": 'title: "Платёж"\ntype: concept\nstatus: knowledge\naliases: [Оплата]\ntags: [платежи, учёт]\n',
    }
    want = {"comma": ({"Заявление, форма 2"}, ["налог, учёт"]), "bare": ({"Оплата"}, ["платежи", "учёт"])}
    for label, head in heads.items():
        sub = tmp / label
        sub.mkdir()
        root = make_project(sub)
        stem = "Заявление" if label == "comma" else "Платёж"
        card = _head_card(root, f"Concepts/{stem}.md", head, "Текст.\n")
        try:
            os.chdir(root)
            names = IDX.collect("AuroraKnowledgeDB/Concepts")[0]["names"]
            moc = MOC.read_cards()[stem]
            c = CP.Card(f"AuroraKnowledgeDB/Concepts/{stem}.md", card.read_text(encoding="utf-8"))
            graph = KG.write_cards_graph(str(sub / "graph.json"))
        finally:
            os.chdir(cwd)
        aliases, tags = want[label]
        assert names == {stem} | aliases, (label, names)
        assert moc["tags"] == tags, (label, moc["tags"])
        assert set(c.aliases) == aliases, (label, c.aliases)
        assert graph is not None


@test
def test_inline_alias_list_keeps_quoted_commas(tmp: Path):
    """Строчный `aliases: ["Заявление, форма 2", "ЗФ2"]` читается по кавычкам, а не по каждой запятой.

    Прежние читатели (ctx_pack, граф, механическое связывание) брали пункты по кавычкам, и
    единый разбор обязан давать то же: ложные куски «Заявление» и «форма 2» шли в ранжирование
    и в формы имён. Писатели синонимов (`kb_fix`, `kb_translit`) режут пункты так же, иначе
    переименование разрывало такую запись.
    """
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    CP = importlib.import_module("ctx_pack")
    IDX = importlib.import_module("kb_index")
    KF = importlib.import_module("kb_fix")
    TR = importlib.import_module("kb_translit")
    want = ["Заявление, форма 2", "ЗФ2"]
    head = 'title: "Заявление"\ntype: concept\nstatus: knowledge\naliases: ["Заявление, форма 2", "ЗФ2"]\n'
    text = f"---\n{head}---\n\nТекст.\n"
    assert AC.aliases(text) == want, AC.aliases(text)
    assert CP.Card("AuroraKnowledgeDB/Concepts/Заявление.md", text).aliases == want
    # пункт с апострофом внутри слова и кавычка без пары — обычные знаки, не граница пункта
    assert AC.split_inline_items("l'avion, z") == ["l'avion", "z"]
    assert AC.split_inline_items("'a, b', c") == ["'a, b'", "c"]
    assert AC.split_inline_items('a, "b') == ["a", '"b']
    cwd = os.getcwd()
    root = make_project(tmp)
    _head_card(root, "Concepts/Заявление.md", head, "Текст.\n")
    try:
        os.chdir(root)
        names = IDX.collect("AuroraKnowledgeDB/Concepts")[0]["names"]
    finally:
        os.chdir(cwd)
    assert names == {"Заявление"} | set(want), names
    card = KF.Card("x.md", text)
    assert card.aliases == want
    assert 'aliases: ["Заявление, форма 2", "ЗФ2", "Новый"]' in KF.add_alias(card, "Новый")
    assert "aliases: [\"ЗФ2\"]" in KF.drop_alias(card, "Заявление, форма 2")
    out = TR.add_alias(text, "Latin")
    assert AC.aliases(out) == want + ["Latin"], (AC.aliases(out), out)


@test
def test_find_skill_does_not_grow_sys_path(tmp: Path):
    """`find_skill` не вставляет путь движка в `sys.path` заново на каждый вызов."""
    sys.path.insert(0, str(SCRIPTS))
    RC = importlib.import_module("request_context")
    RC.find_skill("нет-такого", str(tmp), kit=str(tmp / "nokit"))     # первый вызов может вставить путь
    before = list(sys.path)
    for _ in range(3):
        RC.find_skill("нет-такого", str(tmp), kit=str(tmp / "nokit"))
    assert sys.path.count(str(SCRIPTS)) == before.count(str(SCRIPTS)), sys.path[:6]
    assert len(sys.path) == len(before), (len(before), len(sys.path))


# ------------------------------------------------------------------ один сканер секретов (BL-SEC-18)
# Образцы собираем из кусков: в тексте этого файла настоящего на вид токена нет, поэтому сканер
# пуша и CI (он смотрит и тесты) не принимает сам тест за утечку.

def _gh() -> str:
    return "gh" + "p_" + "aB3dE5gH7k" * 4


def _samples() -> dict:
    """{правило: строка с секретом} — по одному образцу на каждое правило сканера."""
    return {
        "github-token": "token " + _gh(),
        "gitlab-token": "gl" + "pat-" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn",
        "slack-token": "xo" + "xb-" + "1234567890-abcdefghij",
        "aws-key": "AK" + "IA" + "QWERTY12345ASDFG",
        "api-key": "sk-" + "a1b2c3d4" * 5,
        "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "url-credentials": "git clone https://svc:s3cretpw@git.example.com/x.git",
        "token-name": "JIRA_PAT=" + "q9w8e7r6t5y4",
        "api-token": "api_token: " + "Zx9Cv8Bn7Mm6Lk5Jh4Gf3",
        "password": "password: hunter22",
    }


def _git_in(repo: Path, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                           *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})}, check=check)


@test
def test_one_secret_scanner_knows_provider_formats_aurora_names_and_placeholders(tmp: Path):
    """BL-SEC-18: общий сканер (`aurora_common.scan_secret_text`) узнаёт собственные имена токенов
    Авроры (`JIRA_PAT`, `CONFLUENCE_PERSONAL_TOKEN`) и форматы поставщиков, а заглушки пропускает.

    Раньше у доктора было три шаблона, ловивших 2 образца из 11, а у линтера — копия из двух.
    Десять образцов — каждое правило по разу; десять заглушек и обычных слов не срабатывают;
    значение секрета в находку не попадает (в журнал CI уходит только маска).
    """
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    for rule, text in _samples().items():
        got = C.scan_secret_text(text)
        assert rule in [g[0] for g in got], f"{rule}: образец не найден: {got}"
        assert all(len(shown) <= 5 for _r, _n, shown in got), f"{rule}: в маске больше первых знаков: {got}"
    assert C.scan_secret_text('{"env": {"JIRA_PAT": "q9w8e7r6t5y4"}}') and C.scan_secret_text('"password": "hunter22"'), \
        "значение в JSON (имя в кавычках) не найдено — так лежит mcp.json"
    for name in ("CONFLUENCE_PERSONAL_TOKEN", "WEB_PERSONAL_TOKEN", "CONFLUENCE_PAT", "ATLASSIAN_API_TOKEN"):
        assert C.scan_secret_text(f"{name}=" + "k8j7h6g5f4d3"), f"{name}: имя токена Авроры не узнано"
    safe = ["JIRA_PAT=<токен>", "JIRA_PERSONAL_TOKEN=${JIRA_PERSONAL_TOKEN}", "password: <пароль>",
            "ATLASSIAN_API_TOKEN=xxxxxxxx", "token: your-token-here", "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
            "https://user:password@host.example.com/x", "gh" + "p_" + "x" * 36, "AK" + "IAIOSFODNN7EXAMPLE",
            "JIRA_PAT=os.environ[\"JIRA_PAT\"]", "sk-assessment-and-mitigation-plan-for-the-quarter",
            "Пароль хранится в менеджере паролей.", "PAT = None", '{"env": {"JIRA_PAT": "<токен>"}}',
            '"github_token": ""', '"password": true']
    for text in safe:
        assert not C.scan_secret_text(text), f"заглушка принята за секрет: {text!r} → {C.scan_secret_text(text)}"
    only_formats = {r for text in _samples().values() for r, _n, _s in C.scan_secret_text(text, formats_only=True)}
    assert only_formats == {"github-token", "gitlab-token", "slack-token", "aws-key", "api-key"}, only_formats
    assert C.is_secret_place("bots/ревью/бот.md") and C.is_secret_place("mcp.json") \
        and C.is_secret_place(".claude/skills/x/SKILL.md") and C.is_secret_place("aurora.config.yaml")
    assert not C.is_secret_place("AuroraKnowledgeDB/Concepts/a.md") and not C.is_secret_place(".aurora/skills/x.md") \
        and not C.is_secret_place("bots/readme.png")


@test
def test_doctor_and_lint_scan_the_same_places_that_reach_git(tmp: Path):
    """BL-SEC-18: доктор и линтер базы ищут секреты в одних местах — конфиг, навыки проекта, боты, `mcp.json`
    — и только в том, что уедет в git: `.aurora/skills` (вне git), закрытый `.gitignore` `.mcp.json` и карточки
    базы (там «password: …» — пересказ чужой документации) не считаются."""
    root = make_project(tmp, git=True)
    (root / ".gitignore").write_text(".mcp.json\n", encoding="utf-8")
    plant = {
        ".claude/skills/jira/SKILL.md": _samples()["token-name"] + "\n",
        "bots/ревью.md": "Токен: " + _gh() + "\n",
        "mcp.json": '{"url": "https://svc:s3cretpw@git.example.com/x.git"}\n',
        ".mcp.json": '{"env": {"JIRA_PAT": "q9w8e7r6t5y4"}}\n',
        ".aurora/skills/aurora-vault/SKILL.md": _gh() + "\n",
        "AuroraKnowledgeDB/Concepts/Доступ.md": "password: hunter22\n",
        ".claude/skills/ok/SKILL.md": "JIRA_PAT=<токен>\npassword: <пароль>\n",
    }
    for rel, text in plant.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    want = {".claude/skills/jira/SKILL.md", "bots/ревью.md", "mcp.json"}
    code = ("import os, sys; sys.path.insert(0, '.aurora/scripts'); import aurora_common as C, aurora_doctor as D;"
            "print('\\n'.join(r for r, _, _ in C.scan_secret_files('.'))); print('==='); print('\\n'.join(D.scan_secrets()))")
    cp = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    scanned, _, doctor = cp.stdout.partition("===\n")
    assert set(scanned.split()) == want, f"сканируется не то, что уедет в git: {scanned!r}"
    assert all(f"{rel}: возможный секрет (" in doctor for rel in want), doctor
    assert _gh() not in cp.stdout and "hunter22" not in cp.stdout, "доктор напечатал значение секрета"
    lint = run("kb_lint.py", cwd=root)
    assert all(f"{rel}: возможный секрет в git (" in lint.stdout for rel in want), lint.stdout[-1500:]
    assert ".mcp.json:" not in lint.stdout and "Доступ.md: возможный секрет" not in lint.stdout, lint.stdout[-1500:]
    assert _gh() not in lint.stdout


@test
def test_pre_commit_stops_a_secret_even_when_the_ratchet_is_off(tmp: Path):
    """BL-SEC-18: pre-commit проекта останавливает коммит с секретом в добавленных строках — и при снятом
    храповике (он снимает проверку базы, а не защиту от утечки); заглушка, карточка базы и старый секрет
    в соседней строке коммит не останавливают; сломавшаяся проверка коммит не блокирует."""
    root = make_project(tmp, git=True)
    run("aurora_hooks.py", "--install", "--mode", "warn", cwd=root, expect_rc=0)
    commit = lambda msg, env=None: _git_in(root, "commit", "-qm", msg, env=env, check=False)  # noqa: E731

    (root / "mcp.json").write_text('{"env": {"JIRA_PAT": "q9w8e7r6t5y4"}}\n', encoding="utf-8")
    _git_in(root, "add", "-A")
    for env in (None, {"AURORA_SKIP_RATCHET": "1"}):
        cp = commit("секрет", env)
        assert cp.returncode != 0, f"коммит с секретом прошёл (env={env})"
        assert "mcp.json:1 → token-name" in cp.stderr, cp.stderr
        assert "q9w8e7r6t5y4" not in cp.stdout + cp.stderr, "хук напечатал значение секрета"

    (root / "mcp.json").write_text('{"env": {"JIRA_PAT": "<токен>"}}\n', encoding="utf-8")
    (root / "AuroraKnowledgeDB/Concepts").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/Concepts/Доступ.md").write_text("password: hunter22\n", encoding="utf-8")
    _git_in(root, "add", "-A")
    cp = commit("заглушка и карточка")
    assert cp.returncode == 0, cp.stdout + cp.stderr

    # старый секрет уже в истории: коммит соседней строки за него не отвечает (его найдёт doctor)
    (root / "bots").mkdir(exist_ok=True)
    (root / "bots/б.md").write_text("пароль\npassword: hunter22\n", encoding="utf-8")
    _git_in(root, "add", "-A", "-f")
    _git_in(root, "commit", "--no-verify", "-qm", "старое")
    (root / "bots/б.md").write_text("пароль\npassword: hunter22\nещё строка\n", encoding="utf-8")
    _git_in(root, "add", "-A")
    cp = commit("соседняя строка")
    assert cp.returncode == 0, cp.stdout + cp.stderr

    # проверка сама сломалась (код 2): коммит не блокируем, но говорим
    (root / ".aurora/scripts/aurora_hooks.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    (root / "bots/б.md").write_text("пароль\nещё строка\n", encoding="utf-8")
    _git_in(root, "add", "-A")
    cp = commit("сбой проверки")
    assert cp.returncode == 0 and "проверка секретов завершилась кодом 2" in cp.stderr, cp.stdout + cp.stderr


@test
def test_push_scan_finds_provider_tokens_without_a_names_list_and_never_prints_them(tmp: Path):
    """BL-SEC-18: pre-push кита ищет в добавленных строках форматы токенов поставщиков тем же сканером — и
    без списка внутренних названий; значение не печатает; образцы-заглушки, закрытый ключ-образец и адрес
    с `user:pass@` в тестах кита не мешают пушу."""
    repo = tmp / "kit"
    repo.mkdir()
    _git_in(repo, "init", "-q")
    (repo / "a.txt").write_text("начало\n", encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-qm", "начало")
    base = _git_in(repo, "rev-parse", "HEAD").stdout.strip()

    def push(msg: str, text: str):
        (repo / "b.txt").write_text(text, encoding="utf-8")
        _git_in(repo, "add", "-A")
        _git_in(repo, "commit", "-qm", msg)
        head = _git_in(repo, "rev-parse", "HEAD").stdout.strip()
        return subprocess.run([sys.executable, str(SCRIPTS / "aurora_hooks.py"), "--scan-push", "--hook", "v7"],
                              cwd=str(repo), input=f"refs/heads/main {head} refs/heads/main {base}\n",
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = push("образцы", "\n".join(["gh" + "p_" + "x" * 36, _samples()["private-key"], _samples()["url-credentials"],
                                    "password: hunter22", "AK" + "IAIOSFODNN7EXAMPLE"]) + "\n")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "НЕ проверено на внутренние названия" in ok.stderr, "молчание о том, что списка названий нет"
    bad = push("утечка", f"ключ {_gh()}\n")
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "b.txt → github-token (ghp_…)" in bad.stderr, bad.stderr
    assert _gh() not in bad.stdout + bad.stderr, "пуш напечатал значение токена"


@test
def test_ci_gate_takes_the_names_list_from_a_repository_secret(tmp: Path):
    """BL-SEC-18: CI кита гоняет тот же сканер, что pre-push (`.github/scripts/scan_pushed.py`): список названий
    берётся из секрета PRIVATE_TERMS и пишется в `local/private_terms.txt`, а без секрета в журнале предупреждение
    (раньше проверка названий в чистом клоне молча выходила); секреты проверяются в любом случае;
    существующий список не перезаписывается."""
    script = KIT / ".github" / "scripts" / "scan_pushed.py"
    repo = tmp / "ci"
    repo.mkdir()
    _git_in(repo, "init", "-q")
    (repo / "a.txt").write_text("начало\n", encoding="utf-8")
    (repo / ".gitignore").write_text("local/\n", encoding="utf-8")       # как в ките: список вне git
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-qm", "начало")
    base = _git_in(repo, "rev-parse", "HEAD").stdout.strip()
    term = "Синтетика-Икс"

    def ci(text: str, msg: str, **env):
        (repo / "b.txt").write_text(text, encoding="utf-8")
        _git_in(repo, "add", "-A")
        _git_in(repo, "commit", "-qm", msg)
        head = _git_in(repo, "rev-parse", "HEAD").stdout.strip()
        clean = {k: v for k, v in os.environ.items() if k != "PRIVATE_TERMS"}
        return subprocess.run([sys.executable, str(script), base, head], cwd=str(repo), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env={**clean, **env})
    cp = ci(f"в тексте {term}\n", "правка", PRIVATE_TERMS=term + "\n")
    assert cp.returncode == 1 and term in cp.stderr, cp.stdout + cp.stderr
    assert (repo / "local/private_terms.txt").read_text(encoding="utf-8").strip() == term
    (repo / "local/private_terms.txt").write_text("Другое\n", encoding="utf-8")     # уже есть — не затирается
    cp = ci(f"снова {term}\n", "ещё", PRIVATE_TERMS=term)
    assert cp.returncode == 0 and "уже есть" in cp.stdout, cp.stdout + cp.stderr
    assert (repo / "local/private_terms.txt").read_text(encoding="utf-8").strip() == "Другое"
    (repo / "local/private_terms.txt").unlink()
    cp = ci(f"{term} без секрета\n", "без секрета")
    assert cp.returncode == 0 and "::warning::" in cp.stdout and "PRIVATE_TERMS" in cp.stdout, cp.stdout + cp.stderr
    cp = ci(f"{_gh()}\n", "токен")
    assert cp.returncode == 1 and "github-token" in cp.stderr and _gh() not in cp.stdout + cp.stderr, \
        cp.stdout + cp.stderr
    wf = (KIT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    job = wf[wf.index("  secrets:"):wf.index("  floor:")]
    assert ".github/scripts/scan_pushed.py" in job and "PRIVATE_TERMS: ${{ secrets.PRIVATE_TERMS }}" in job \
        and "fetch-depth: 0" in job and "continue-on-error" not in job, job



@test
def test_secret_scanner_sees_headers_bare_keys_and_real_passwords_but_not_templates(tmp: Path):
    """BL-SEC-18 (проход 2): заголовок `Authorization: Bearer …`, голые `api_key:`/`token:` и пароль со скобками или
    началом вроде `Пароль2024` находятся, а шаблоны (`<…>`, `${…}`, `$ИМЯ`, `os.environ[...]`, `your-token`) — нет.
    Фильтр заглушек прежде считал заглушкой любое значение со скобкой или с началом `your`/`пароль`."""
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    key = "a1b2c3d4" * 3
    bad = ['"Authorization": "Bearer ' + key + '"', "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijk",
           "Authorization: Basic dXNlcjpzZWNyZXQxMjM0", '"apiKey": "' + key + '"', "api_key: " + key,
           "API_KEY=" + key, "SECRET_KEY=" + key, "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIk7", "token: " + key,
           "secret: " + key, "password: Zx9(Cv8w!", 'password: "Zx9[Cv8w!k"', "password: $ecretPw99",
           "password: %Secret99", "https://svc:p(ss1w0rd@git.example.com/x.git", "JIRA_PAT=yourself123456",
           "password: Пароль2024", "password: yourself123", "Authorization: Bearer Zx9Cv8Bn7Mm6"]
    for text in bad:
        assert C.scan_secret_text(text), f"секрет не найден: {text!r}"
    safe = ["Authorization: Bearer ${TOKEN}", "Authorization: Bearer <token>", '"Authorization": "Bearer YOUR_TOKEN"',
            "token: authentication", "secret: configuration-value", "JIRA_PAT=$JIRA_PAT", "JIRA_PAT=%JIRA_PAT%",
            'JIRA_PAT=os.environ.get("X", "y")', "password: getpass()", "password: your-password",
            "password: example_pw", "password: replace_me", "password: <ваш пароль>", "password: [REDACTED]",
            "token: ${{ secrets.X }}", "password: пароль",
            # проход 3: заглушки в camelCase (заглавная буква начинает следующее слово) и проза после `Authorization:`
            "password: yourPassword", "JIRA_PAT=YourTokenHere", "password: replaceWithPassword",
            "token: insertYourTokenHere1", "Authorization: Bearer authentication", "Authorization: Token required"]
    for text in safe:
        assert not C.scan_secret_text(text), f"шаблон принят за секрет: {text!r} → {C.scan_secret_text(text)}"
    # то, что кит отдаёт проектам (навыки, документация), красным не краснеет
    leaked = []
    for rel in subprocess.run(["git", "ls-files", "skills", "docs", "wiki"], cwd=str(KIT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout.split("\n"):
        if rel.endswith(C.SECRET_SCAN_SUFFIXES) and (KIT / rel).is_file():
            leaked += [(rel, r) for r in C.scan_secret_text((KIT / rel).read_text(encoding="utf-8", errors="ignore"))]
    assert not leaked, leaked[:5]


@test
def test_secret_scanner_is_linear_on_long_lines(tmp: Path):
    """BL-SEC-18 (проход 2): правило адреса с паролем было квадратичным на строках без пробелов (`"a-"*40000` — 5,7 с,
    мегабайт — часы), а pre-commit и kb:lint читают строки любой длины. Теперь схема и вид вперёд ограничены."""
    import time
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    for text in ("a-" * 40000, "ab.cd." * 14000, "sk-" * 40000, "token:" * 40000, "x://" * 30000, "a_" * 100000 + "TOKEN"):
        t0 = time.time()
        C.scan_secret_text(text)
        spent = time.time() - t0
        assert spent < 1.5, f"{text[:12]!r}…: {spent:.1f} с на {len(text)} знаков — рост не линейный"
    assert C.scan_secret_text("sk-" + "a1b2c3d4" * 5) and C.scan_secret_text("git clone https://svc:s3cretpw@git.example.com/x")


@test
def test_mcp_json_secret_in_env_or_headers_is_found_under_any_key_name(tmp: Path):
    """BL-SEC-18 (проход 2): продукт считает `env` и `headers` секретными картами (`MCP_SECRET_MAPS`), а сканер знал
    только суффиксы `_TOKEN`/`_PAT`. Теперь `mcp.json` разбирается как JSON: значение секретного по имени ключа
    (`MY_KEY`, `Cookie`) находится, `${…}` и обычные настройки — нет."""
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    bad = ('{"mcpServers": {"a": {"command": "x", "env": {"LOG": "debug", "MY_KEY": "abc12345xyz"},\n'
           ' "headers": {"Cookie": "sid=abc123def"}}, "b": {"url": "https://example.com/m", "auth": "tok4en99"}}}')
    hits = C.scan_mcp_json(bad)
    assert sorted(n for _r, n, _s in hits) == [1, 2, 2], hits
    assert all(len(shown) <= 2 for _r, _n, shown in hits), hits
    # проход 3: пути, UUID и хеши — настройки, а не секреты (даже под секретным именем и длинные)
    for k, v in (("DATA_DIR", "/home/analyst/projects/kb2/data"), ("CONFIG", "./config/server1/settings.json"),
                 ("PYTHONPATH", "/Users/me/aurora/scripts2/lib"), ("WORKSPACE_ID", "123e4567-e89b-12d3-a456-426614174000"),
                 ("BUILD", "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"), ("TOKEN_FILE", "/run/secrets/jira_token2"),
                 ("API_KEY_FILE", "~/.config/aurora/key1"), ("SECRET_PATH", "file:///etc/aurora/secret2"),
                 ("PASSWORD_FILE", "C:\\secrets\\pass1.txt")):
        doc = json.dumps({"mcpServers": {"a": {"command": "x", "env": {k: v}}}})
        assert not C.scan_mcp_json(doc), f"{k}={v!r} принято за секрет"
    assert C.scan_mcp_json(json.dumps({"mcpServers": {"a": {"env": {"JIRA_PAT": "q9w8e7r6t5y4"}}}})), \
        "значение под секретным именем перестали находить"
    ok = ('{"mcpServers": {"a": {"command": "x", "env": {"LOG": "debug", "MY_KEY": "${MY_KEY}", "T": "<токен>"},\n'
          ' "headers": {"Authorization": "Bearer ${JIRA_PAT}"}, "auth": "oauth"}}}')
    assert not C.scan_mcp_json(ok), C.scan_mcp_json(ok)
    assert C.scan_mcp_json("не json") == []
    root = make_project(tmp, git=True)
    (root / "mcp.json").write_text(bad, encoding="utf-8")
    got = {(r, rule) for r, rule, _n in C.scan_secret_files(str(root))}
    assert ("mcp.json", "mcp-secret") in got, got


@test
def test_env_files_of_aurora_are_a_scanned_place_when_git_would_take_them(tmp: Path):
    """BL-SEC-18 (проход 2): файл настроек Авроры с токеном (`.env.aurora` + `.local`), не закрытый `.gitignore`
    или добавленный через `add -f`, находят doctor, kb:lint и pre-commit (раньше его не было среди мест);
    закрытый — не краснеет, образец `.env.example` не смотрят."""
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    env_name = ".env.aurora" + ".local"
    assert C.is_secret_place(env_name) and C.is_secret_place(".env") and C.is_secret_place("aurora.env.local") \
        and not C.is_secret_place(".env.example") and not C.is_secret_place("sub/.env")
    root = make_project(tmp, git=True)
    run("aurora_hooks.py", "--install", "--mode", "warn", cwd=root, expect_rc=0)
    (root / env_name).write_text("JIRA_PAT=q9w8e7r6t5y4\n", encoding="utf-8")
    doctor = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, '.aurora/scripts'); import aurora_doctor as D;"
                             "print('\\n'.join(D.scan_secrets()))"], cwd=str(root), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert f"{env_name}: возможный секрет (" in doctor.stdout, doctor.stdout + doctor.stderr
    assert "q9w8e7r6t5y4" not in doctor.stdout
    lint = run("kb_lint.py", cwd=root)
    assert f"{env_name}: возможный секрет в git (" in lint.stdout, lint.stdout[-800:]
    _git_in(root, "add", "-A")
    cp = _git_in(root, "commit", "-qm", "токен", check=False)
    assert cp.returncode != 0 and f"{env_name}:1 → token-name" in cp.stderr, cp.stdout + cp.stderr
    assert "q9w8e7r6t5y4" not in cp.stdout + cp.stderr
    (root / ".gitignore").write_text(env_name + "\n", encoding="utf-8")       # закрыт — git его не отдаёт
    _git_in(root, "reset", "-q", env_name)
    assert env_name not in C.secret_scan_files(str(root))
    _git_in(root, "add", "-A")
    cp = _git_in(root, "commit", "-qm", "закрыт", check=False)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    # проход 3: `aurora.env.local` (образец без ведущей точки) — тоже место pre-commit, а не только доктора и линтера
    (root / "aurora.env.local").write_text("JIRA_PAT=q9w8e7r6t5y4\n", encoding="utf-8")
    _git_in(root, "add", "-f", "aurora.env.local")
    cp = _git_in(root, "commit", "-qm", "без точки", check=False)
    assert cp.returncode != 0 and "aurora.env.local:1 → token-name" in cp.stderr, cp.stdout + cp.stderr
    assert "q9w8e7r6t5y4" not in cp.stdout + cp.stderr


@test
def test_pre_commit_secret_check_reads_only_settings_places_and_sees_mcp_and_header_forms(tmp: Path):
    """BL-SEC-18 (проход 2): проверка секретов в pre-commit просит у git diff только мест настроек — большой файл
    базы в индексе не читается (раньше один файл в 200 МБ давал 7 с и 1,5 ГБ памяти на каждый коммит); при этом
    `Authorization: Bearer …` в навыке и `"MY_KEY": "…"` в mcp.json коммит останавливают, а `kb_lint --only`
    не обходит проект в поисках секретов (их ищет pre-commit)."""
    root = make_project(tmp, git=True)
    run("aurora_hooks.py", "--install", "--mode", "warn", cwd=root, expect_rc=0)
    big = root / "AuroraKnowledgeDB" / "assets" / "dump.md"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("строка вложения с password: hunter22\n" * 120000, encoding="utf-8")      # ≈ 4,5 МБ
    _git_in(root, "add", "-A")
    code = ("import sys; sys.path.insert(0, '.aurora/scripts'); import aurora_hooks as H\n"
            "seen = []\n"
            "orig = H._git_run\n"
            "def spy(args, binary=False):\n"
            "    rc, out, err = orig(args, binary)\n"
            "    seen.append((args, len(out)))\n"
            "    return rc, out, err\n"
            "H._git_run = spy\n"
            "rc = H.scan_secrets_staged()\n"
            "diff = [s for s in seen if 'diff' in s[0]][0]\n"
            "print(rc, diff[1], '--' in diff[0], any('AuroraKnowledgeDB' in a for a in diff[0]))\n")
    cp = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    rc, size, has_paths, names_kb = cp.stdout.split()
    assert (rc, has_paths, names_kb) == ("0", "True", "False"), cp.stdout
    assert int(size) < 20000, f"git отдал {size} байт: diff не ограничен местами настроек"
    _git_in(root, "commit", "-qm", "вложение")
    # формы, которые прежний сканер пропускал
    (root / ".claude/skills/x").mkdir(parents=True)
    (root / ".claude/skills/x/SKILL.md").write_text(
        'curl -H "Authorization: Bearer ' + "a1b2c3d4" * 3 + '" https://example.com\n', encoding="utf-8")
    _git_in(root, "add", "-A")
    cp = _git_in(root, "commit", "-qm", "заголовок", check=False)
    assert cp.returncode != 0 and ".claude/skills/x/SKILL.md:1 → auth-header" in cp.stderr, cp.stdout + cp.stderr
    assert "a1b2c3d4" not in cp.stdout + cp.stderr
    _git_in(root, "reset", "-q")
    (root / "mcp.json").write_text('{"mcpServers": {"a": {"command": "x",\n "env": {"MY_KEY": "abc12345xyz"}}}}\n',
                                   encoding="utf-8")
    (root / ".claude/skills/x/SKILL.md").unlink()
    _git_in(root, "add", "-A")
    cp = _git_in(root, "commit", "-qm", "ключ", check=False)
    assert cp.returncode != 0 and "mcp.json:2 → mcp-secret" in cp.stderr, cp.stdout + cp.stderr
    assert "abc12345xyz" not in cp.stdout + cp.stderr
    # старый секрет в mcp.json и правка соседней строки коммит не останавливают
    _git_in(root, "commit", "--no-verify", "-qm", "старое")
    (root / "mcp.json").write_text('{"mcpServers": {"a": {"command": "y",\n "env": {"MY_KEY": "abc12345xyz"}}}}\n',
                                   encoding="utf-8")
    _git_in(root, "add", "-A")
    cp = _git_in(root, "commit", "-qm", "соседняя", check=False)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    # kb_lint --only не ходит за секретами по всему проекту
    probe = ("import sys, runpy; sys.path.insert(0, '.aurora/scripts'); import aurora_common as C\n"
             "def boom(*a, **k): raise SystemExit(99)\n"
             "C.scan_secret_files = boom\n"
             "sys.argv = ['kb_lint.py', '--only', 'AuroraKnowledgeDB/assets/dump.md']\n"
             "runpy.run_path('.aurora/scripts/kb_lint.py', run_name='__main__')\n")
    cp = subprocess.run([sys.executable, "-c", probe], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.returncode != 99, "kb_lint --only снова обходит проект в поисках секретов"


@test
def test_ci_writes_the_private_names_list_before_the_full_suite_and_hook_texts_mention_secrets(tmp: Path):
    """BL-SEC-18 (проход 2): задание tests пишет `local/private_terms.txt` из секрета PRIVATE_TERMS до полного набора
    (иначе проверки части 01 тихо выходят) или предупреждает; описание хука в панели и страница wiki про хуки
    говорят про остановку коммита с секретом и про коды выхода проверки секретов."""
    wf = (KIT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    job = wf[wf.index("  tests:"):wf.index("  authors:")]
    assert job.index("PRIVATE_TERMS") < job.index("name: Full suite") and "private_terms.txt" in job \
        and "::warning::" in job, job
    import json
    for lang, word in (("ru", "секрет"), ("en", "secret")):
        cat = json.loads((KIT / "cockpit/modules/install/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert word in cat["install.ratchet_what"] and "mcp.json" in cat["install.ratchet_what"], cat["install.ratchet_what"]
    for rel in ("wiki/Windows-and-macOS.md", "wiki/ru/Windows-and-macOS.md"):
        assert "--scan-secrets" in (KIT / rel).read_text(encoding="utf-8"), rel


@test
def test_secret_scan_sees_utf16_env_and_bom_mcp_json_and_pre_push_names_its_limits(tmp: Path):
    """BL-SEC-18 (проход 3): `.env`, записанный PowerShell 5.1 в UTF-16 (`>`), и `mcp.json` с BOM раньше не видел никто
    (BOM ломал разбор JSON, UTF-16 — каждое правило); теперь их находят doctor/kb:lint и pre-commit. Закрытый ключ
    пуш кита не ищет — и docstring хука этого больше не обещает."""
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    mcp = '{"mcpServers": {"a": {"command": "x", "env": {"MY_KEY": "abc12345xyz"}}}}'
    assert C.scan_mcp_json("\ufeff" + mcp), "mcp.json с BOM выпал из проверки"
    assert C.decode_secret_bytes("JIRA_PAT=q9w8e7r6t5y4\n".encode("utf-16")) == "JIRA_PAT=q9w8e7r6t5y4\n"
    assert C.decode_secret_bytes(b"\xef\xbb\xbfa=1") == "a=1"
    root = make_project(tmp, git=True)
    run("aurora_hooks.py", "--install", "--mode", "warn", cwd=root, expect_rc=0)
    (root / ".env").write_bytes("JIRA_PAT=q9w8e7r6t5y4\r\n".encode("utf-16"))
    (root / "mcp.json").write_bytes(b"\xef\xbb\xbf" + mcp.encode("utf-8"))
    got = {(r, rule) for r, rule, _n in C.scan_secret_files(str(root))}
    assert (".env", "token-name") in got and ("mcp.json", "mcp-secret") in got, got
    _git_in(root, "add", "-f", ".env")
    cp = _git_in(root, "commit", "-qm", "utf16", check=False)
    assert cp.returncode != 0 and ".env:1 → token-name" in cp.stderr, cp.stdout + cp.stderr
    _git_in(root, "reset", "-q", ".env")
    _git_in(root, "add", "mcp.json")
    cp = _git_in(root, "commit", "-qm", "bom", check=False)
    assert cp.returncode != 0 and "mcp.json:1 → mcp-secret" in cp.stderr, cp.stdout + cp.stderr
    assert "q9w8e7r6t5y4" not in cp.stdout + cp.stderr and "abc12345xyz" not in cp.stdout + cp.stderr
    assert not C.scan_secret_text(_samples()["private-key"], formats_only=True)
    head = (SCRIPTS / "aurora_hooks.py").read_text(encoding="utf-8").split('"""')[1]
    assert "закрытый ключ" in head and "только форматы поставщиков" in head and "(`ghp_…`, `AKIA…`, закрытые" not in head


@test
def test_git_section_names_a_secret_stop_and_the_checkpoint_does_not_skip_the_secret_check(tmp: Path):
    """BL-SEC-18 (проход 3): раздел «Git» узнаёт отказ pre-commit из-за секрета (раньше — «Git сообщил об ошибке»
    с бессмысленным «повторить»), а чекпойнт перед агентом больше не коммитит с `--no-verify`: храповик он снимает
    своей переменной, проверку секретов — нет; чистое дерево по-прежнему фиксируется."""
    sys.path.insert(0, str(SCRIPTS))
    GS = importlib.import_module("git_sync")
    R = importlib.import_module("agent_runner")
    tail = "aurora: в коммите похоже на секрет — коммит остановлен:\n  mcp.json:1 → token-name (q…)"
    assert GS.commit_code(tail) == "secret_hook"
    assert GS.PROBLEMS["secret_hook"][2] == [] and GS.commit_code("aurora: храповик\nAURORA_SKIP_RATCHET=1") == "ratchet"
    root = make_project(tmp, git=True)
    run("aurora_hooks.py", "--install", "--mode", "block", cwd=root, expect_rc=0)
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-qm", "база", env={"AURORA_SKIP_RATCHET": "1"}, check=False)
    before = _git_in(root, "rev-parse", "HEAD").stdout.strip()
    (root / "mcp.json").write_text('{"env": {"JIRA_PAT": "q9w8e7r6t5y4"}}\n', encoding="utf-8")
    got = R.checkpoint(str(root), "agent:distill", True)
    assert got["ok"] is False and "секрет" in got["why"] and "q9w8e7r6t5y4" not in got["why"], got
    assert _git_in(root, "rev-parse", "HEAD").stdout.strip() == before, "чекпойнт закоммитил секрет"
    (root / "mcp.json").write_text('{"env": {"JIRA_PAT": "<токен>"}}\n', encoding="utf-8")
    got = R.checkpoint(str(root), "agent:distill", True)
    assert got["ok"] is True and got["committed"] >= 1, got
    assert _git_in(root, "rev-parse", "HEAD").stdout.strip() != before


# --- Уверенность выдачи, честный отказ и прозрачный фильтр доверия (BL-KBA-14) ---------------

def _answerability_base(tmp: Path):
    """База из доверенных карточек про заявки и поставки + черновик про совсем другое."""
    root = make_project(tmp)
    for i, body in enumerate((
            "Заявка на поставку оформляется заявителем; статус заявки виден в реестре.",
            "Заявка на возврат платежа проходит проверку оператором.",
            "Поставка товара подтверждается накладной и актом приёмки.",
            "Реестр договоров ведёт юрист, карточка договора содержит стороны и сроки.",
            "Накладная формируется после отгрузки и хранится три года.",
            "Тариф на перевозку считается по весу и расстоянию.")):
        stem = ("Заявка-на-поставку", "Заявка-на-возврат", "Поставка-товара",
                "Реестр-договоров", "Накладная", "Тариф-перевозки")[i]
        from harness import card
        card(root, f"Concepts/{stem}.md", body, status="verified", type="concept")
    from harness import card
    card(root, "Concepts/Квантовый-насос.md",
         "Квантовый насос Хаксли работает на криогенном сверхпроводнике.",
         status="draft", type="concept")
    return root


@test
def test_pack_refuses_when_question_is_not_in_base(tmp: Path):
    """Вопрос вне базы с одним общим словом не получает пак «как точный» (KB2-04, KB3-21).

    `fuse` нормирует вес к лучшему в запросе, и «рецепт борща курс биткоина заявка» набирал
    1,0 на карточках «Заявка…» из-за одного слова. Теперь доля слов вопроса, найденных
    пятью лучшими карточками, считается без нормировки: ниже порога движок отказывает сам
    (код 1, без модели) и называет, чего не хватает; на полпути — пак с пометкой «Слабая
    опора»; точный вопрос идёт как раньше. Порог выключается (`abstain=0`).
    """
    root = _answerability_base(tmp)

    far = run("ctx_pack.py", "рецепт борща курс биткоина заявка", "--no-log", cwd=root)
    assert far.returncode == 1, f"вопрос вне базы получил пак:\n{far.stdout[:300]}"
    assert "в базе этого нет" in far.stderr and "борщ" in far.stderr, far.stderr
    assert "Context pack" not in far.stdout, "при отказе пак всё равно напечатан"

    # отказ можно выключить: старое поведение — пак из любых совпавших карточек
    off = run("ctx_pack.py", "рецепт борща курс биткоина заявка", "--no-log",
              "--retrieval", "abstain=0", cwd=root)
    assert off.returncode == 0 and "Context pack" in off.stdout, off.stderr

    # половина вопроса найдена, половины нет в базе: пак есть, но с названным пробелом
    half = run("ctx_pack.py", "какой статус у заявки на отпуск", "--no-log", cwd=root,
               expect_rc=0)
    assert "Слабая опора" in half.stdout and "отпуск" in half.stdout.split("Слабая опора")[1][:200], \
        half.stdout[:600]

    # точный вопрос — без пометок
    exact = run("ctx_pack.py", "заявка на поставку", "--no-log", cwd=root, expect_rc=0)
    assert "Слабая опора" not in exact.stdout, "точный вопрос помечен слабым"
    assert "Заявка-на-поставку" in exact.stdout

    # постановка задачи (длиннее вопроса) по доле слов не судится
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("ctx_pack")
    long_topic = " ".join(f"слово{n}" for n in range(20))
    assert C.confidence([object()], long_topic) is None, "длинную постановку судят как вопрос"


@test
def test_pack_names_cards_hidden_by_trust_filter(tmp: Path):
    """Фильтр доверия не отбрасывает молча: пак называет скрытое и его статус (KB2-07).

    Ответ лежал только в черновике, а agent:ask в режиме generate говорил «в базе этого
    нет». Теперь пак печатает «Скрыто по доверию: N (статусы), лучший скрытый — `имя`»
    (имя и статус, без тела и без [[ссылки]]), а когда доверенного нет совсем — отказ
    подсказывает режим evaluate. kb:eval раскладывает промах на статус и релиз.
    """
    root = _answerability_base(tmp)

    only = run("ctx_pack.py", "квантовый насос Хаксли", "--no-log", cwd=root)
    assert only.returncode == 1, "в generate достали черновик"
    assert "Квантовый-насос" in only.stderr and "draft" in only.stderr \
        and "evaluate" in only.stderr, f"отказ не назвал скрытое: {only.stderr}"
    assert "криогенном" not in only.stderr + only.stdout, "тело скрытой карточки раскрыто"

    both = run("ctx_pack.py", "квантовый насос заявка на поставку", "--no-log", cwd=root,
               expect_rc=0)
    note = [ln for ln in both.stdout.splitlines() if "Скрыто по доверию" in ln]
    assert note and "`Квантовый-насос`" in note[0] and "draft: 1" in note[0], both.stdout[:700]
    assert "[[" not in note[0], "скрытая карточка названа ссылкой — выглядит основанием"
    assert "криогенном" not in both.stdout, "тело скрытой карточки попало в пак"

    # режим evaluate пропускает черновик — и сообщать о скрытом нечего
    ev = run("ctx_pack.py", "квантовый насос Хаксли", "--mode", "evaluate", "--no-log",
             cwd=root, expect_rc=0)
    assert "Скрыто по доверию" not in ev.stdout and "Квантовый-насос" in ev.stdout

    # разложение промаха в замере: цель есть, но отсечена статусом, — это не «не найдено»
    sys.path.insert(0, str(SCRIPTS))
    KQ = importlib.import_module("kb_search_quality")
    cwd0 = os.getcwd()
    try:
        os.chdir(root)
        KQ._CARDS = None
        res = KQ.pack_measure([("Квантовый-насос", "квантовый насос Хаксли"),
                               ("Заявка-на-поставку", "заявка на поставку")],
                              {"квантовый насос Хаксли": {}, "заявка на поставку": {}})
    finally:
        KQ._CARDS = None
        os.chdir(cwd0)
    assert res["находка"] == 0.5 and res["промах: статус"] == 0.5 and res["промах: релиз"] == 0.0, res


@test
def test_make_not_ready_on_unverified_basis(tmp: Path):
    """Документ, сославшийся на непроверенную карточку (bootstrap), не бывает `ready` (KB3-13).

    Режим bootstrap пускает draft в пак generate, а `ready` определялся лишь критиком и
    Момусом — документ на черновиках был неотличим от построенного на знании. Теперь
    основание называется в шапке (`basis_unverified`) и в «Допущениях», а `ready` закрыт;
    ссылка только на доверенное оставляет `ready` как было.
    """
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    C = importlib.import_module("ctx_pack")
    root = make_project(tmp)
    (root / "Artifacts" / "us").mkdir(parents=True, exist_ok=True)

    pack = ("# Context pack\n\n## Заявка-1 — Заявка\n\n## Черновик-2 — Черновик\n"
            f"\n---\n\n> {C.UNVERIFIED_MARK}Черновик-2\n")
    assert C.unverified_names(pack) == ["Черновик-2"], C.unverified_names(pack)

    def doc(draft: str, sid: str) -> str:
        st = {"sid": sid, "kind": "us", "idea": "Идея " + sid,
              "spec": {"out": "Artifacts/us", "template": "t.md", "title": "US"},
              "stages": {"checked": "2026-10-12"}, "issues": [], "momus": {"ok": True,
              "unsupported": 0, "clean": True}, "draft": draft, "pack": pack, "path": None}
        return (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")

    trusted = doc("Опирается на [[Заявка-1]].", "s1")
    assert "status: ready" in trusted and "basis_unverified" not in trusted, trusted[:300]

    lean = doc("Опирается на [[Заявка-1]] и [[Черновик-2]].", "s2")
    assert "status: draft" in lean and "status: ready" not in lean, lean[:300]
    assert 'basis_unverified: ["[[Черновик-2]]"]' in lean, lean[:400]
    assert "опора на непроверенное" in lean.split("## Допущения")[1], "нет строки в «Допущениях»"


# --- Доработка BL-KBA-14 (проход 2) ------------------------------------------------------------

def _ctx(root: Path, topic: str, *extra: str):
    return run("ctx_pack.py", topic, "--no-log", *extra, cwd=root)


@test
def test_make_idea_is_not_refused_by_abstain_but_question_is(tmp: Path):
    """Идея для `agent:make` — постановка, а не вопрос: отказ «в базе этого нет» её не судит.

    Новая функциональность, опирающаяся на известную сущность («оповещение курьера о новой
    накладной»), находит в базе 10–15 % слов — для вопроса это отказ, для постановки —
    нормальный пак с пометкой «Слабая опора». `run_make` зовёт поиск с `abstain=0`, прямой
    вопрос без него по-прежнему получает отказ.
    """
    root = _answerability_base(tmp)
    idea = "Сделать US: оповещение курьера о новой накладной через мобильное приложение"
    assert _ctx(root, idea).returncode == 1, "вопрос с покрытием ниже порога не отказан"
    off = _ctx(root, idea, "--retrieval", "abstain=0")
    assert off.returncode == 0 and "Накладная" in off.stdout, off.stderr
    assert "Слабая опора" in off.stdout, "пометка при abstain=0 пропала"

    # настоящий путь производства: пак собирается движком проекта, отказа нет
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    A = importlib.import_module("agent_core")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/US.md").write_text("# US\n\n## Описание\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  us:\n    title: "US"\n'
                          '    template: "Templates/US.md"\n    out: "Artifacts/us"\n',
                        encoding="utf-8")

    def fake(cfg_, role, messages, **kw):
        text = {"planner": json.dumps({"questions": [], "assumptions": [], "plan": "1. Описание"},
                                      ensure_ascii=False),
                "critic": json.dumps({"ok": True, "issues": [], "coverage": {}}),
                "qa": "ВЕРДИКТ: чисто"}.get(role, "# US\n\nтекст")
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [], "text": text}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    r1 = R.run_make(cfg, str(root), "us", idea, "", "", False, call=fake)
    assert r1.get("ok"), f"идея отказана как вопрос: {r1.get('why')}"
    assert "Слабая опора" in R.load_session(str(root), r1["sid"])["pack"]


@test
def test_glossary_only_pack_is_refused_and_weak_does_not_switch_abstain_off(tmp: Path):
    """Справочник аббревиатур — не выдача; `weak=0` не выключает отказ (проход 2, BL-KBA-14).

    Глоссарий всегда входит в пак, поэтому вопрос без единого совпадения на базе с ним
    получал пак из одной карточки «Аббревиатуры» и уходил модели. Отказ и пометка — два
    ключа, и каждый выключается сам: `abstain=0` — без отказа, `weak=0` — без пометки.
    """
    root = _answerability_base(tmp)
    from harness import card
    card(root, "Reference/Аббревиатуры.md", "ЭСФ — электронная счёт-фактура.",
         status="verified", type="reference")

    far = _ctx(root, "рецепт борща")
    assert far.returncode == 1 and "Context pack" not in far.stdout, \
        f"пак из одного глоссария ушёл как выдача:\n{far.stdout[:300]}"
    # с abstain=0 (производство, бот) поведение прежнее
    assert _ctx(root, "рецепт борща", "--retrieval", "abstain=0").returncode == 0

    topic = "рецепт борща курс биткоина заявка"
    assert _ctx(root, topic).returncode == 1
    assert _ctx(root, topic, "--retrieval", "weak=0").returncode == 1, \
        "weak=0 молча выключил и отказ"
    half = _ctx(root, "какой статус у заявки на отпуск", "--retrieval", "weak=0")
    assert half.returncode == 0 and "Слабая опора" not in half.stdout, "weak=0 не снял пометку"


@test
def test_weak_mark_is_not_put_on_answerable_questions(tmp: Path):
    """Шаблоны «что такое X» и другая словоформа не делают отвечаемый вопрос слабым.

    «такое» и «значит» не входят в запрос как слова, а правило «слова нет в базе — выше
    слабой опоры не пускает» не касается другой формы слова («оформить» против «оформляется»:
    общее начало из четырёх знаков). Имя и новое слово без опоры в базе по-прежнему слабо.
    """
    root = _answerability_base(tmp)
    for q in ("Что такое накладная?", "Что значит заявка на поставку?",
              "Как оформить заявку на поставку?", "Как подтвердить поставку товара?",
              "Как оформить заявку на поставку товара?"):
        got = _ctx(root, q)
        assert got.returncode == 0 and "Слабая опора" not in got.stdout, \
            f"отвечаемый вопрос «{q}» помечен слабым:\n{got.stdout[:400]}"
    # другая форма слова засчитывается вполовину: ответ в карточке есть, и пометка не нужна
    form = _ctx(root, "Как формировать накладные?")
    assert "Слабая опора" not in form.stdout, form.stdout[:400]
    assert form.returncode == 0, form.stderr
    named = _ctx(root, "Как оформить заявку на поставку Зарнитц?")
    assert "Слабая опора" in named.stdout and "Зарнитц" in named.stdout.split("Слабая опора")[1][:200], \
        "имя без опоры в базе не названо"

    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("ctx_pack")
    cwd0 = os.getcwd()
    try:
        os.chdir(root)
        C.measure_rarity(C.load_cards())
        assert C._shares_prefix(C.norm("оформить")), "словоформа «оформляется» не узнана"
        assert not C._shares_prefix(C.norm("Зарнитц")), "новое слово принято за словоформу"
        assert not C._shares_prefix("us-4.7.9"), "номер принят за словоформу"
    finally:
        os.chdir(cwd0)
    assert C._surface("Старый тариф по объёму Груза", C.norm("Груза")) == "Груза", \
        "регистр написания потерян"
    assert C._surface("тариф по объёму", C.norm("объёму")) == "объёму", "ё потеряна"


@test
def test_deprecated_is_not_reported_as_hidden_by_trust_and_hint_is_doable(tmp: Path):
    """Замещённая карточка — история, а не «непроверенное»: совета про evaluate для неё нет.

    `evaluate` deprecated не пропускает (они видны только в `ask`), поэтому «повторите в
    режиме evaluate» невыполним, а в самом `evaluate` такой совет вообще бессмыслен.
    """
    root = _answerability_base(tmp)
    from harness import card
    card(root, "Concepts/Старый-тариф.md", "Старый тариф считался по объёму груза.",
         status="deprecated", type="concept")
    card(root, "Concepts/Тариф-архив.md", "Архивный тариф по весу перевозки груза.",
         status="rejected", type="concept")

    for mode in ("generate", "evaluate"):
        got = _ctx(root, "старый тариф по объёму груза и тариф перевозки", "--mode", mode)
        note = [ln for ln in got.stdout.splitlines() if "Скрыто по доверию" in ln]
        assert not any("deprecated" in ln for ln in note), \
            f"замещённая карточка названа скрытой по доверию ({mode}): {note}"
        # подсказка про evaluate — только там и про то, что evaluate способен показать
        assert not any("evaluate покажет" in ln or "повторите в режиме evaluate" in ln
                       for ln in note), f"невыполнимый совет ({mode}): {note}"
    ask = _ctx(root, "старый тариф по объёму груза", "--mode", "ask")
    assert "Старый-тариф" in ask.stdout, "в ask замещённая карточка должна быть видна"
    only = _ctx(root, "старый тариф по объёму груза")
    assert only.returncode == 1 and "evaluate" not in only.stderr, only.stderr


@test
def test_ask_report_shows_weak_basis_and_anchor_is_the_mark_line(tmp: Path):
    """`weak_basis` доходит до человека, а ставится по строке-пометке пака, не по телу карточки."""
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    C = importlib.import_module("ctx_pack")
    root = make_project(tmp)

    def ask(pack: str) -> dict:
        def fake_pack(cwd, script, args):
            return {"ok": True, "out": pack}

        def fake_call(cfg, role, messages, deadline=None, history=None, **kw):
            return {"ok": True, "text": "Ответ [[Заявка]].", "backend": 1, "model": "m", "log": []}

        real = R.run_command
        R.run_command = fake_pack
        try:
            return R.run_ask({"request_timeout": 60}, str(root), "вопрос", "generate", 40,
                             call=fake_call, momus=False)
        finally:
            R.run_command = real

    marked = ask(f"# Пак (карточек 1)\n\n> ⚠️ {C.WEAK_MARK} из слов вопроса найдено 40%; "
                 "в базе нет: отпуск. Отвечайте.\n\n## Заявка\nтекст")
    assert marked["ok"] and marked.get("weak_basis") is True, marked
    assert "Слабая опора" in R.report_ask(marked, "вопрос", {"request_timeout": 60}), \
        "пометка есть в результате, но человек её в отчёте не видит"

    body = ask("# Пак (карточек 1)\n\n## Заявка\nВ описании встречается фраза: Слабая опора.")
    assert body["ok"] and not body.get("weak_basis"), "флаг поставлен по телу карточки"
    assert "Слабая опора" not in R.report_ask(body, "вопрос", {"request_timeout": 60})


@test
def test_refusal_text_keeps_its_head_and_unverified_names_keep_commas(tmp: Path):
    """Отказ движка читается с начала («в базе этого нет»), а имя с запятой не рвётся."""
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    C = importlib.import_module("ctx_pack")
    long = "ctx_pack: по теме «x» в базе этого нет: найдено 25%. " + "Скрыто по доверию " * 30
    assert R.pack_refusal(long).startswith("ctx_pack: по теме «x» в базе этого нет"), \
        "начало отказа срезано"
    trace = "Traceback (most recent call last):\n" + "  File x\n" * 100 + "ValueError: итог"
    assert R.pack_refusal(trace).endswith("ValueError: итог"), "у трассировки важен хвост"
    assert R.pack_refusal("") == "пак не собран"

    pack = ("# Context pack\n\n## Заявка, версия 2 — Заявка\n\n## Черновик — Ч\n\n"
            f"---\n\n> {C.UNVERIFIED_MARK}Заявка, версия 2, Черновик\n")
    assert C.unverified_names(pack) == ["Заявка, версия 2", "Черновик"], C.unverified_names(pack)


@test
def test_confidence_with_semantic_closeness_does_not_refuse(tmp: Path):
    """Поведение отказа при индексе смысла закреплено (KB2-04): близость выше SEM_FLOOR отказ снимает.

    Порог SEM_FLOOR=0.35 кластер сам называет слабым, поэтому на живой базе с индексом отказ
    может не срабатывать — это условие приёмки калибровки, а не случайность. Тест фиксирует
    текущее поведение подставленным `close`: близости 0.4 у лишних карточек дают «слабо», 0.2 — отказ.
    """
    root = _answerability_base(tmp)
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("ctx_pack")
    cwd0 = os.getcwd()
    try:
        os.chdir(root)
        cards = C.load_cards()
        C.measure_rarity(cards)
        seeds = [cards["Заявка-на-поставку"], cards["Накладная"]]
        topic = "рецепт борща курс биткоина заявка"
        far = C.confidence(seeds, topic, {"Накладная": 0.2})
        near = C.confidence(seeds, topic, {"Накладная": 0.4})
    finally:
        os.chdir(cwd0)
    assert far["level"] == "none", far
    assert near["level"] == "weak", near
