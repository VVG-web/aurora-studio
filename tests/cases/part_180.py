"""Проверки движка Aurora, часть 180. Каркас — tests/harness.py.

Панель для заменяемых коннекторов (1.200.0): выбор трекера и вики в «Настройках проекта» (выпадающий список на роль,
слепок и 409, подсказки), рамка «Трекер задач» в форме видов вместо свёрнутого блока «задача…», блок «Задача» у документа
в «Файлах» и «Продуктивности» (списки трекера, «Сохранить», «Создать задачу», ссылка на заведённую), окно создания
артефакта отдаёт свойства задачи движку. Без окон подтверждения; все надписи — в каталогах ru и en.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from harness import KIT, panel_sources, test

CYR = re.compile("[а-яА-ЯёЁ]")


def _fn(ui: str, head: str) -> str:
    start = ui.index(head)
    return ui[start:ui.index("\n}\n", start)]


def _cats():
    return (json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8")),
            json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8")))


@test
def test_project_settings_choose_the_task_tracker_and_the_wiki(_t):
    """Группа «Трекер задач и вики»: по выпадающему списку на роль из `/api/connectors/roles`, пункт «по умолчанию» —
    единственный с ролью; «Сохранить» шлёт слепок, расхождение — строка и «Перечитать», без `confirm()`; подсказка «?» у
    каждого поля; после записи списки трекера в памяти страницы сбрасываются."""
    ui = panel_sources()
    group = _fn(ui, "async function renderConnectorRoles(")
    assert "renderConnectorRoles(box)" in _fn(ui, "async function renderProject(")
    assert '"/api/connectors/roles?project="' in group and '"/api/connectors/roles/save"' in group
    assert "expect: digest" in group and "r.conflict" in group and 't("connrole.reload")' in group
    assert 'el("select"' in group and "kindTip(tip)" in group and "title: tip" in group
    assert '["tasks", t("connrole.l_tasks"), t("connrole.tip_tasks")]' in group
    assert '["wiki", t("connrole.l_wiki"), t("connrole.tip_wiki")]' in group
    assert "TASK_OPTS.clear()" in group and "confirm(" not in group


@test
def test_kinds_form_has_a_task_tracker_frame_instead_of_the_folded_task_block(_t):
    """Третья рамка «Трекер задач»: ключ проекта, тип из списка трекера с «Обновить список», исполнитель, метки,
    компоненты — `lab` с подсказкой; умолчания (ключ из настроек трекера, исполнитель по почте git) — подсказкой в пустом
    поле. Эпика у вида нет, прежний свёрнутый блок «задача…» убран, а `rec.task` уходит в запись целиком (прежний `epic`
    не теряется)."""
    ui = panel_sources()
    form = _fn(ui, "async function renderKinds(")
    frame = form[form.index('const tracker = el("fieldset"'):form.index('const create = el("fieldset"')]
    assert 'data-frame":"tracker"' in frame and 't("kinds.frame_tracker")' in frame and 't("kinds.frame_tracker_about")' in frame
    for key in ("task_project", "task_type", "task_assignee", "task_labels", "task_components"):
        assert f'lab("{key}"' in frame, key
    assert 'tfield("labels"' in form and 'tfield("assignee"' in form and 'tfield("epic"' not in form
    assert "taskOptions(" in form and "taskFill(true)" in form and 't("tracker.refresh")' in form
    assert 't("tracker.default", {v: base.project_key})' in form and "who.display || who.name" in form
    assert "el(\"div\",{class:\"kframes\"}, create, publish, tracker)" in form
    for gone in ("kinds.task_open", "kinds.task_close", "kinds.task_about", "taskBox", "delete rec.task.epic"):
        assert gone not in form, gone
    ru, en = _cats()
    for key in ("kinds.task_open", "kinds.task_about", "kinds.f_epic"):
        assert key not in ru and key not in en, f"осталась надпись убранного блока: {key}"


@test
def test_document_task_block_is_in_files_and_production_without_confirmations(_t):
    """Блок «Задача» — один на панель (`taskBlock`): в «Файлах» у документа вида (`#fileTask`, пока в редакторе
    несохранённое — не пишет), в «Продуктивности» — у выбранного документа окна публикации и в окне создания (значения
    уходят `--task-field`). Списки — `/api/task/options` (эпик — выпадающим списком), «Сохранить» — `/api/task/doc` со
    слепком, «Создать задачу» — `/api/task/create`; заведённая задача — ссылкой «Открыть КЛЮЧ»."""
    ui = panel_sources()
    block = _fn(ui, "async function taskBlock(")
    assert '"/api/task/doc?"' in block and '"/api/task/doc", {method:"POST"' in block and "expect: digest" in block
    assert '"/api/task/create"' in block and "taskEpics(epic, o.epics" in block and "taskTypes(type, o.types" in block
    assert 'pageLink(d.task_url, t("tracker.open", {key: d.task_key}))' in block and "d.task_key" in block
    assert 't("tracker.create", {tracker: tr.title' in block and "r.conflict" in block and 't("tracker.reload")' in block
    assert "draft" in block and "box.values = values" in block and "confirm(" not in block
    options = _fn(ui, "function taskOptions(")
    assert '"/api/task/options?"' in options and 'q.set("fresh", "1")' in options
    files = _fn(ui, "async function drawFileTask(")
    assert "taskBlock({path: rel" in files and "F.dirty" in files and 't("tracker.save_editor_first")' in files
    assert "drawFileTask(d.path)" in _fn(ui, "async function openFile(")
    html = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert 'id="fileTask"' in html
    assert "taskBlock}" in ui, "блок не отдан разделам через ctx.ui"
    work = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    assert "ctx.ui.taskBlock({kind, draft: true})" in work and "ctx.ui.taskBlock({path: rel})" in work
    assert '["--task-field", k + "=" + v]' in work and "confirm(" not in work
    assert 'id="makeTask"' in (KIT / "cockpit/modules/work/view.html").read_text(encoding="utf-8")


@test
def test_tracker_and_role_texts_are_in_both_catalogs(_t):
    """Надписи блока «Задача», рамки и группы ролей — в обоих каталогах ядра; английские — без русских букв (кроме
    кавычек-ёлочек), подстановки в ru и en одинаковые; каждая надпись, которую спрашивает панель, есть в каталоге."""
    ru, en = _cats()
    ui = panel_sources()
    # ключ спрошен строкой — в `t("…")` или выбором `t(условие ? "…" : "…")`
    asked = set(re.findall(r'"((?:tracker|connrole)\.[\w.]+|kinds\.(?:l_task|tip_task|frame_tracker)[\w.]*)"', ui))
    prefixes = {k for k in asked if k.endswith("_")}            # `t("tracker.l_" + key)` — ключи собираются в коде
    used = (asked - prefixes) | {k for k in ru if any(k.startswith(p) for p in prefixes)}
    assert len(used) >= 40 and {"tracker.l_epic", "tracker.tip_epic"} <= used, sorted(used)
    for key in sorted(used | {"dirty.connectors"}):
        assert ru.get(key, "").strip() and en.get(key, "").strip(), key
        assert not CYR.search(re.sub(r"«[^»]*»", "", en[key])), (key, en[key])
        holes = lambda s: sorted(re.findall(r"\{(\w+)\}", s))  # noqa: E731
        assert holes(ru[key]) == holes(en[key]), key
    for key in [k for k in ru if k.startswith(("tracker.", "connrole."))]:
        assert key in used, f"надпись в каталоге, а панель её не спрашивает: {key}"
