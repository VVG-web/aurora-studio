"""Проверки движка Aurora, часть 169. Каркас и помощники — tests/harness.py, подставной Confluence — part_148.py.

Форма видов объясняет себя. Под путём и заголовком — справка (корень, раздел, страница; `>`; встроенные подстановки с
примером; `{поле}` и `{поле?}`; пример «как в папках»), поля шапки, которые есть у документов вида (их отдаёт
`/api/kinds/example`, объединение по последним документам, без значений), и предупреждение о поле шаблона, которого нет
ни у одного документа. У подписи каждого поля создания и публикации — «?» с подсказкой (что это, на что влияет, пример,
обязательно ли), обязательные поля — со звёздочкой, корень — «нужно для публикации»; сервер не сохраняет вид без шаблона
или папки и называет поле. Всё — на двух языках. Проекты — во временных папках, домашняя папка подменена.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from cases import part_148 as W
from cases import part_150 as P150
from cases import part_151 as P151
from harness import KIT, SCRIPTS, _js_function, make_project, panel_sources, test

CYR = re.compile("[а-яА-ЯёЁ]")


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _cats() -> tuple:
    return tuple(json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8")) for lang in ("ru", "en"))


# Поля формы видов → ключ подсказки. Свойства задачи — отдельный блок, в эту форму подсказок он не входит.
TIPS = {"title": "kinds.tip_title", "template": "kinds.tip_template", "prompt": "kinds.tip_prompt", "out": "kinds.tip_out",
        "mcp": "kinds.tip_mcp", "ref_mask": "kinds.ref_mask_hint", "publish_url": "kinds.tip_publish_url",
        "publish_path": "kinds.tip_publish_path", "publish_title": "kinds.tip_publish_title",
        "publish_prompt": "kinds.tip_publish_prompt",
        # рамка «Трекер задач» (1.200.0): те же правила подсказки — пример и обязательно ли, в обоих языках
        "task_project": "kinds.tip_task_project", "task_type": "kinds.tip_task_type",
        "task_assignee": "kinds.tip_task_assignee", "task_labels": "kinds.tip_task_labels",
        "task_components": "kinds.tip_task_components"}
OTHER_TIPS = {"new": "kinds.tip_new", "tech_agnostic": "kinds.tech_hint", "review": "kinds.review_hint"}


# ------------------------------------------------------------------ поля шапки вида на сервере

@test
def test_kinds_example_lists_the_header_fields_and_the_unknown_ones(tmp: Path):
    """`GET /api/kinds/example` ищет документы и в подпапках папки вида (кроме папки другого вида), отдаёт поля шапки,
    которые есть у документов вида (объединение, без значений, без списков и штампа публикации), и поля шаблона, которых
    нет ни у одного документа (`unknown`). Пример пути считается по последнему документу, `{folders}` — по его подпапкам.
    `kindFields` печатает список и предупреждение на настоящих каталогах обоих языков."""
    panel = P150._Panel(tmp)
    try:
        kinds = ('artifacts:\n  ac:\n    title: "Критерии"\n    template: "Templates/US.md"\n    out: "Artifacts/ac"\n'
                 '    publish_url: "77"\n    publish_path: "{folders}"\n    publish_title: "{title}"\n'
                 '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/ac/us"\n')
        root = W.project(panel.roots, "https://wiki.example.com", kinds)
        new = W.put(root, "Artifacts/ac/Activity_A1_Core/Epic_1.2_Intake/AC-1.md",
                    W.doc('type: ac\nstatus: draft\nactivity: "A1 секрет"\ntags: [x]\nconfluence_page_id: "5"\n'))
        old = W.put(root, "Artifacts/ac/Activity_A1_Core/AC-2.md", W.doc("type: ac\nepic: E1\n"))
        other = W.put(root, "Artifacts/ac/us/US-1.md", W.doc("type: us\nrelease: R9\n"))
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(other, (2_000_000_000, 2_000_000_000))       # самый свежий, но чужого вида — в пример не идёт
        os.utime(new, (1_500_000_000, 1_500_000_000))
        q = urllib.parse.urlencode({"project": str(root), "kind": "ac"})
        st, ex = panel.call("GET", "/api/kinds/example?" + q)
        assert st == 200 and ex["doc"] == "Artifacts/ac/Activity_A1_Core/Epic_1.2_Intake/AC-1.md", ex
        assert ex["levels"] == ["Activity A1 Core", "Epic 1.2 Intake"] and ex["title"] == "Итог", ex
        assert ex["fields"] == ["type", "status", "activity", "epic"] and ex["unknown"] == [], ex
        assert "секрет" not in json.dumps(ex, ensure_ascii=False), "значения полей шапки ушли в ответ"
        q = urllib.parse.urlencode({"project": str(root), "kind": "ac", "publish_path": "{Activity} > {epic?} > {sprint}",
                                    "publish_title": "{name}"})
        st, ex = panel.call("GET", "/api/kinds/example?" + q)
        assert ex["unknown"] == ["Activity", "sprint"] and ex["missing"] == ["Activity", "sprint"], ex
        assert ex["title"] == "AC-1", ex
        ui = panel_sources()
        ru, en = ((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8") for lang in ("ru", "en"))
        out = P151._node(f"const RU = {ru}, EN = {en};\nconst S = {{lang: 'ru'}};\nlet I18N = RU;\n"
                         + P151._fn(ui, "function t(") + P151._fn(ui, "function kindFields(") + f"""
const ex = {json.dumps(ex, ensure_ascii=False)};
const out = {{ru: kindFields(ex), none: kindFields({{doc: ""}})}};
I18N = EN; S.lang = "en"; out.en = kindFields(ex);
console.log(JSON.stringify(out));
""", tmp)
        assert out["ru"]["have"] == "В шапке документов этого вида есть поля: type, status, activity, epic.", out["ru"]
        assert "{Activity}, {sprint}" in out["ru"]["warn"] and "{Activity}, {sprint}" in out["en"]["warn"], out
        assert out["en"]["have"].startswith("Documents of this kind have these header fields:") and not CYR.search(
            out["en"]["have"] + out["en"]["warn"]), out["en"]
        assert out["none"] == {"have": "", "warn": ""}, out["none"]
    finally:
        panel.close()


# ------------------------------------------------------------------ подсказки и справка формы

@test
def test_kinds_form_explains_every_field_in_both_languages(tmp: Path):
    """У каждого поля создания и публикации — подсказка в обоих каталогах: подпись с «?» и тот же текст в `title` поля;
    в тексте — пример и обязательно ли. Под путём и заголовком — короткие подсказки и справка: корень, раздел, страница,
    `>`, встроенные подстановки (по одной строке с примером на каждую из `make_kinds.BUILTIN_SLOTS`), `{поле}`, `{поле?}`,
    пусто — прямо под корнем, пример «как в папках»; поля шапки и предупреждение; без окон подтверждения."""
    MK = _mod("make_kinds")
    ru, en = _cats()
    ui = panel_sources()
    form = _js_function(ui, "async function renderKinds(")
    # MCP — плашки, а не поле ввода (`mcpBox`, часть 166): подсказка стоит у подписи блока плашек, не через `lab`.
    labs = re.findall(r'\blab\("(\w+)"', form) + re.findall(r'kindTip\(t\("kinds\.tip_(mcp)"\)\)', form)
    assert set(labs) == set(TIPS), f"поля формы и подсказки разошлись: {sorted(set(labs) ^ set(TIPS))}"
    assert "inp.title = text" in form and "kindTip(text)" in form, "подсказка поля и значок «?» говорят разное"
    assert 'null, t("kinds.ref_mask_hint"))' in form
    for key in OTHER_TIPS.values():
        assert f'kindTip(t("{key}"))' in form, f"нет значка «?» с {key}"
    for field, key in {**TIPS, **OTHER_TIPS}.items():
        for lang, cat, example, need in (("ru", ru, "Пример", ("Обязательно", "Необязательно", "Нужно для публикации")),
                                          ("en", en, "Example", ("Required", "Optional", "Needed for publishing"))):
            text = cat.get(key, "")
            assert text.strip() and example in text and any(n in text for n in need), (lang, field, text)
            if lang == "en":
                assert not CYR.search(text.replace("«", "").replace("»", "")), (field, text)
        assert f"kinds.l_{field}" in ru or field in OTHER_TIPS, field
    # справка к пути и заголовку
    help_keys = ("kinds.path_help", "kinds.path_help_tree", "kinds.path_help_fields", "kinds.path_help_slots",
                 "kinds.path_help_mirror", "kinds.publish_path_hint", "kinds.publish_title_hint", "kinds.fields_have",
                 "kinds.fields_unknown") + tuple("kinds.slot_" + s for s in MK.BUILTIN_SLOTS)
    for key in help_keys:
        assert ru.get(key, "").strip() and en.get(key, "").strip() and not CYR.search(en[key]), key
    for s in MK.BUILTIN_SLOTS:
        assert ":" in ru["kinds.slot_" + s] and ":" in en["kinds.slot_" + s], f"у {{{s}}} нет примера"
    assert all(w in ru["kinds.path_help_tree"] for w in ("Корень", "Раздел", "Страница", "«>»", "прямо под корень"))
    assert all(w in en["kinds.path_help_tree"] for w in ("root", "section", "page", "“>”", "right under the root"))
    assert "{поле?}" in ru["kinds.path_help_fields"] and "{field?}" in en["kinds.path_help_fields"]
    for cat in (ru, en):
        assert "{folders}" in cat["kinds.path_help_mirror"] and "{title}" in cat["kinds.path_help_mirror"]
        assert "{folders}" in cat["kinds.publish_path_hint"] and "{fields}" in cat["kinds.fields_have"]
    assert 'el("details",{class:"kf-help"}' in form and '(d.slots || []).map(' in form and 't("kinds.slot_" + s)' in form
    assert "fieldsHave" in form and "fieldsWarn" in form and "kindFields(r)" in form
    assert P150._ck().kinds_read(str(make_project(tmp)))["slots"] == list(MK.BUILTIN_SLOTS)
    assert "confirm(" not in form and "alert(" not in form, "в форме видов окно подтверждения"


# ------------------------------------------------------------------ обязательные поля

@test
def test_kinds_required_fields_are_starred_and_the_server_refuses_them_empty(tmp: Path):
    """Звёздочки — ровно у обязательных полей: имя вида, шаблон, папка результата (`KIND_REQUIRED` = то, что сервер не даёт
    сохранить пустым; любое другое поле вида пустым сохраняется). Корень помечен «нужно для публикации», а не звёздочкой.
    Вверху формы — легенда; пустое обязательное поле подсвечено; отказ сервера называет вид и поле, файл не тронут,
    английская страница получает причину по-английски."""
    MK, ck = _mod("make_kinds"), P150._ck()
    ru, en = _cats()
    ui = panel_sources()
    m = re.search(r"const KIND_REQUIRED = \[([^\]]*)\];", ui)
    required = re.findall(r'"(\w+)"', m.group(1)) if m else []
    assert required == ["template", "out"], required
    form = _js_function(ui, "async function renderKinds(")
    assert "const need = KIND_REQUIRED.includes(key);" in form and 'need ? el("span",{class:"kf-req"}, " *") : null' in form
    assert 't("kinds.l_new"), el("span",{class:"kf-req"}, " *")' in form, "у имени нового вида нет звёздочки"
    assert form.count('{class:"kf-req"}') == 3, "звёздочка где-то, кроме легенды, имени вида и обязательных полей"
    assert 'key === "publish_url" ? el("span",{class:"kf-pub"}, " · " + t("kinds.for_publish"))' in form
    assert 'class:"muted kf-legend"' in form and ru["kinds.legend"].startswith("— обязательно") \
        and en["kinds.legend"].startswith("— required"), "нет легенды «* — обязательно»"
    assert ru["kinds.for_publish"] == "нужно для публикации" and en["kinds.for_publish"] == "needed for publishing"
    assert "if (need) inp.required = true;" in form and "bad.focus()" in form
    assert ".kfield input:required:invalid" in (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    full = {f: "" for f in MK.FIELDS}
    full.update(title="Критерии", template="Templates/US.md", out="Artifacts/ac", publish_url="77",
                publish_path="{folders}", publish_title="{title}", prompt="Prompts/p.md", mcp="atlassian", review="us",
                tech_agnostic="true", ref_mask="US-x", publish_prompt="Prompts/q.md")
    assert ck.kinds_write(str(root), {"ac": full}).get("ok")
    cfg = root / "aurora.config.yaml"
    raw = cfg.read_bytes()
    for field in MK.FIELDS:
        res = ck.kinds_write(str(root), {"ac": {**full, field: ""}})
        if field in required:
            assert "error" in res and res.get("kind") == "ac" and res.get("field") == field, (field, res)
            assert "«ac»" in res["error"] and cfg.read_bytes() == raw, (field, res)
            assert not CYR.search(ck.translate_message(res["error"], "en")), res["error"]
        else:
            assert res.get("ok"), (field, res)
            assert ck.kinds_write(str(root), {"ac": full}).get("ok")
    # поле, которого запись не прислала, берётся с диска — запись без шаблона в запросе не отказ
    partial = {k: v for k, v in full.items() if k != "template"}
    assert ck.kinds_write(str(root), {"ac": partial}).get("ok") and MK.read_kinds(str(root))["ac"]["template"] == "Templates/US.md"
    # новый вид без шаблона — отказ
    assert ck.kinds_write(str(root), {"ac": full, "us": {"title": "История", "out": "Artifacts/us"}}).get("field") == "template"
