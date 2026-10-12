"""Проверки движка Aurora, часть 41. Каркас и помощники — tests/harness.py.

Детерминированный линтер артефактов (BL-TEAM-05): `scripts/artifact_lint.py` решает кодом то,
что чек-лист ревью отдавал модели, — разделы шаблона, формулу истории, пару «Действие /
Реакция» у шагов, число шагов и групп критериев, остатки шаблона, формулировки спеки. Синтетические
артефакты, без модели и сети; в `kb:lint` замечания идут предупреждениями, код возврата не меняют.
"""
from __future__ import annotations

import importlib
import os
import re
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

    Срез `text.split("---", 2)[-1]` в восьми местах отдавал телом хвост заголовка
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
