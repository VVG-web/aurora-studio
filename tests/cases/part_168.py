"""Проверки движка Aurora, часть 168. Каркас и помощники — tests/harness.py, подставной Confluence — part_148.py.

Публикация по подпапкам вида. Документы вида лежат по подпапкам (`Artifacts/ac/Activity_A1_Core/Epic_1.2_Intake/…`), и в
Confluence получается то же дерево: путь `{folders}` — каждая подпапка отдельным уровнем, как через `>`; подпапок нет —
уровня нет. `{folder}` — ближайшая подпапка, `{name}` — имя файла; подчёркивания — пробелы. Подстановка без значения
останавливает публикацию с причиной, которая называет каждое поле отдельно, перечисляет поля шапки этого документа и
встроенные подстановки и говорит, что сделать; поле другим регистром — подсказка. Переложенный в другую подпапку файл —
`misplaced`, перенос только `--move`. Пустой путь по-прежнему кладёт страницу прямо под корень. Проекты — во временных
папках, Confluence — подставной на 127.0.0.1, модель не вызывается.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from cases import part_148 as W
from cases import part_151 as P151
from harness import KIT, SCRIPTS, _js_function, panel_sources, test

CYR = re.compile("[а-яА-ЯёЁ]")


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def kind_ac(root: str, path: str, title: str = "{title}") -> str:
    """Вид `ac` с папкой `Artifacts/ac`; подстановки в шаблонах — как есть (без `format`)."""
    return ('artifacts:\n  ac:\n    title: "Критерии"\n    template: "Templates/US.md"\n    out: "Artifacts/ac"\n'
            f'    publish_url: "{root}"\n    publish_path: "' + path + '"\n    publish_title: "' + title + '"\n')


# ------------------------------------------------------------------ подстановки из папок

@test
def test_publish_folders_give_a_level_per_subfolder(tmp: Path):
    """`{folders}` при 0, 1 и 2 подпапках: уровней столько же, подчёркивания — пробелы, лишние пробелы схлопнуты; нет
    подпапок — уровень выпадает, а не «нет поля». `{folder}` — ближайшая, `{name}` — имя файла словами, `{file}` — как
    было. `{folders}` уживается с другими уровнями и с текстом в том же уровне; в заголовке подпапки — через « / ».
    255 знаков и пустой заголовок — на каждом уровне. Пустой путь — страница прямо под корнем (как прежде)."""
    MK = _mod("make_kinds")
    root = tmp / "p"
    rels = {0: "Artifacts/ac/AC-1_Rules.md", 1: "Artifacts/ac/Activity_A1_Core/AC-2.md",
            2: "Artifacts/ac/Activity_A1_Core/Epic_1.2__Intake/US-1.2.1_Intake_form.md"}
    text = W.doc('type: ac\nrelease: "2.1"\n')
    for rel in rels.values():
        W.put(root, rel, text)

    def ctx(n: int, out: str = "Artifacts/ac") -> dict:
        return MK.publish_context(text, str(root / rels[n]), "ac", "PRJ-A", out, str(root))

    folders = {"publish_path": "{folders}", "publish_title": "{title}"}
    assert MK.publish_target(folders, ctx(0)) == {"levels": [], "title": "Итог", "missing": [], "problems": []}
    assert MK.publish_target(folders, ctx(1))["levels"] == ["Activity A1 Core"]
    two = MK.publish_target(folders, ctx(2))
    assert two["levels"] == ["Activity A1 Core", "Epic 1.2 Intake"] and not two["problems"], two
    c = ctx(2)
    assert c["folder"] == "Epic 1.2 Intake" and c["name"] == "US-1.2.1 Intake form" and c["file"] == "US-1.2.1_Intake_form", c
    assert ctx(0)["folder"] == "" and ctx(0)["name"] == "AC-1 Rules"
    # {folder}: у файла прямо в папке вида уровень выпадает, обязательным полем он не считается
    t = MK.publish_target({"publish_path": "{folder}"}, ctx(0))
    assert t["levels"] == [] and t["missing"] == [] and not t["problems"], t
    assert MK.publish_target({"publish_path": "{folder}"}, ctx(2))["levels"] == ["Epic 1.2 Intake"]
    # вместе с другими уровнями: «Релиз {release} > {folders} > {kind}»
    mixed = {"publish_path": "Релиз {release} > {folders} > {kind}", "publish_title": "{folders}: {name}"}
    t = MK.publish_target(mixed, ctx(2))
    assert t["levels"] == ["Релиз 2.1", "Activity A1 Core", "Epic 1.2 Intake", "ac"], t
    assert t["title"] == "Activity A1 Core / Epic 1.2 Intake: US-1.2.1 Intake form", t["title"]
    t = MK.publish_target(mixed, ctx(0))
    assert t["levels"] == ["Релиз 2.1", "ac"] and t["title"] == ": AC-1 Rules", t
    # текст в том же уровне — как если бы подпапки были написаны через «>»: «Раздел {folders}» → «Раздел A», «B»
    assert MK.publish_target({"publish_path": "Раздел {folders}"}, ctx(2))["levels"] == ["Раздел Activity A1 Core",
                                                                                          "Epic 1.2 Intake"]
    # ограничения уровня: длиннее 255 — отказ; пустой заголовок — отказ
    long = {**ctx(2), "folders": MK.LEVEL_SEP.join(["Activity A1 Core", "Я" * 300])}
    assert ("too-long", "Я" * 60) in MK.publish_target(folders, long)["problems"]
    assert MK.publish_target({"publish_title": "{folder}"}, ctx(0))["problems"] == [("empty-title", "{folder}")]
    # пустой путь — страница прямо под корнем; файл вне папки вида — подпапок нет
    assert MK.publish_target({}, ctx(2))["levels"] == []
    assert ctx(2, out="Deliverables/work")["folders"] == "" and ctx(2, out="")["folders"] == ""
    assert MK.subfolders(str(root / rels[2]), "Artifacts/ac/", str(root)) == ["Activity_A1_Core", "Epic_1.2__Intake"]
    # встроенные подстановки полей шапки не требуют
    assert {"name", "folder", "folders"} <= set(MK.BUILTIN_SLOTS)


# ------------------------------------------------------------------ понятная причина

@test
def test_publish_reason_names_each_missing_field_the_header_and_what_to_do(tmp: Path):
    """Шаблон вида ссылается на `{Activity}` и `{Epic}`, а в шапке их нет: причина называет каждое поле отдельно в кавычках
    (не «Activity, Epic» одним полем), перечисляет поля шапки ЭТОГО документа (без списков и штампа) и встроенные
    подстановки, называет три выхода — заменить подстановку у вида (поле «Путь под корнем» или «Заголовок страницы»),
    вписать поле в шапку, сделать подстановку необязательной; `activity` в шапке — подсказка про регистр. Так говорят
    серая кнопка в «Файлах» (на английской странице — по-английски, без русских остатков), `ship:publish --json` и карточка
    публикации на настоящих каталогах."""
    MK = _mod("make_kinds")
    ck = W._ck()
    root = W.project(tmp, "https://wiki.example.com", kind_ac("77", "{Activity} > {Epic}", "{name}"))
    rel = "Artifacts/ac/Activity_A1_Core/AC-3.4.7_Rules.md"
    W.put(root, rel, W.doc('type: ac\ntitle: "AC"\naliases: [a]\ntags: [b]\nstatus: draft\nactivity: A1\n'
                           'confluence_page_id: "5"\n'))
    state = ck.file_read(str(root), rel)["publish"]
    assert state["state"] == "blocked" and state["fix"] == "kinds" and state["fields"] == ["Activity", "Epic"], state
    why = state["why"]
    assert "«Activity», «Epic»" in why and "Activity, Epic" not in why, why
    assert "Поля в шапке документа: type, status, activity." in why and state["have"] == ["type", "status", "activity"], why
    assert all("{" + s + "}" in why for s in MK.BUILTIN_SLOTS), why
    assert "«Путь под корнем» или «Заголовок страницы»" in why and "вписать поле в шапку документа" in why, why
    assert "{Activity?}" in why and "«Настройки проекта → Артефакты»" in why, why
    assert "Регистр букв важен: в шапке есть «activity», а шаблон вида просит {Activity}" in why, why
    en = ck.translate_message(why, "en")
    assert en.startswith("Can't publish: the page path or title of the kind “ac”") and not CYR.search(en), en
    assert "«Activity», «Epic»" in en and "Letter case matters" in en and "{Activity?}" in en, en
    # без подсказки про регистр — тоже переводится целиком
    plain = MK.missing_why("ac", ["release"], [], [])
    assert "Поля в шапке документа: —." in plain and not CYR.search(ck.translate_message(plain, "en")), plain
    assert MK.case_slips(["Title", "epic"], ["Epic"]) == [("Title", "title"), ("epic", "Epic")]

    # ship:publish --json: причина с путём документа и данные для карточки
    cp, res = W.publish(root, rel, "--json")
    assert cp.returncode == 1 and res["code"] == "missing-field" and res["fields"] == ["Activity", "Epic"], cp.stderr
    assert res["have"] == ["type", "status", "activity"] and res["slips"] == [["Activity", "activity"]], res
    assert res["builtins"] == list(MK.BUILTIN_SLOTS) and res["why"].startswith(rel + ": Опубликовать нельзя"), res["why"]

    # карточка публикации: настоящие каталоги и настоящий `t`
    ui = panel_sources()
    cats = {lang: (KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8") for lang in ("ru", "en")}
    out = P151._node(f"const RU = {cats['ru']}, EN = {cats['en']};\nconst S = {{lang: 'ru'}};\nlet I18N = RU;\n"
                     + P151._fn(ui, "function t(") + P151._fn(ui, "function publishWhy(") + f"""
const res = {json.dumps(res, ensure_ascii=False)};
const out = {{ru: publishWhy(res)}};
I18N = EN; S.lang = "en"; out.en = publishWhy(res);
console.log(JSON.stringify(out));
""", tmp)
    for lang, text in out.items():
        assert "«Activity», «Epic»" in text and "type, status, activity" in text and "{folders}" in text, (lang, text)
        assert "{Activity?}" in text and "«activity»" in text and "{Activity}" in text and "{kind}" in text, (lang, text)
        assert "“ac”" in text or "«ac»" in text, (lang, text)
    assert out["ru"].startswith("Публикация остановлена") and "Регистр букв важен" in out["ru"], out["ru"]
    assert out["en"].startswith("Publishing stopped") and "Letter case matters" in out["en"] and not CYR.search(out["en"]), out


# ------------------------------------------------------------------ дерево в Confluence и перенос файла

@test
def test_publish_a_file_moved_to_another_subfolder_is_misplaced_until_move(tmp: Path):
    """Путь `{folders}`, заголовок `{title}`: документ из `Activity_A1_Core/Epic_1.2_Intake` ложится в корень › «Activity A1
    Core» › «Epic 1.2 Intake» (разделы созданы один раз), файл прямо в папке вида — прямо под корень. Файл переложили в
    `Epic_1.3_Rules`: план показывает новый раздел к созданию, публикация обновляет страницу на месте с исходом
    `misplaced` (отпечаток входов новый, раздел не создан), перенос — только `--move`; повтор после него — `unchanged`."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        rid = W.root_page(wiki)
        root = W.project(tmp, wiki.url, kind_ac(rid, "{folders}"))
        rel = "Artifacts/ac/Activity_A1_Core/Epic_1.2_Intake/AC-1.md"
        path = W.put(root, rel, W.doc("type: ac\nstatus: ready\n"))
        cp, res = W.publish(root, rel, "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok", cp.stdout + cp.stderr
        assert res["created"] == ["Activity A1 Core", "Epic 1.2 Intake"], res["created"]
        act = wiki.by_title("Activity A1 Core")[0]["id"]
        epic = wiki.by_title("Epic 1.2 Intake")[0]["id"]
        pid = res["page"]["id"]
        assert wiki.pages[act]["parent"] == rid and wiki.pages[epic]["parent"] == act and wiki.pages[pid]["parent"] == epic
        before = AC.frontmatter(path.read_text(encoding="utf-8"))["published_input"]
        # прямо в папке вида — уровня нет, страница под корнем
        W.put(root, "Artifacts/ac/AC-0.md", W.doc("type: ac\nstatus: ready\n", "# Плоский\n\nТекст.\n"))
        cp, res = W.publish(root, "Artifacts/ac/AC-0.md", "--apply")
        assert res["outcome"] == "ok" and res["chain"] == [] and wiki.pages[res["page"]["id"]]["parent"] == rid, res

        moved = "Artifacts/ac/Activity_A1_Core/Epic_1.3_Rules/AC-1.md"
        os.renames(str(path), str(root / moved))
        path = root / moved
        _cp, plan = W.publish(root, moved, "--json")
        assert plan["outcome"] == "misplaced", plan
        assert [(c["title"], c["state"]) for c in plan["chain"]] == [("Activity A1 Core", "exists"), ("Epic 1.3 Rules", "create")]
        writes = len(wiki.writes())
        cp, res = W.publish(root, moved, "--apply")
        assert cp.returncode == 1 and res["outcome"] == "misplaced" and res["stamped"] is True, cp.stdout + cp.stderr
        assert wiki.pages[pid]["parent"] == epic and not wiki.by_title("Epic 1.3 Rules"), "без --move страница перенесена"
        assert [m for m, _p, _b in wiki.writes()[writes:]] == ["PUT"], "без --move записано не только обновление"
        assert AC.frontmatter(path.read_text(encoding="utf-8"))["published_input"] != before, "отпечаток входов не учёл новый путь"
        cp, res = W.publish(root, moved, "--apply", "--move")
        assert cp.returncode == 0 and res["outcome"] == "ok" and res["moved"] is True, cp.stdout + cp.stderr
        rules = wiki.by_title("Epic 1.3 Rules")[0]["id"]
        assert wiki.pages[rules]["parent"] == act and wiki.pages[pid]["parent"] == rules, "перенос не по пути подпапок"
        _cp, res = W.publish(root, moved, "--apply")
        assert res["outcome"] == "unchanged", res["outcome"]
    finally:
        wiki.close()


# ------------------------------------------------------------------ спецификация вида и справка

@test
def test_make_kinds_spec_asks_no_header_field_for_folder_placeholders(tmp: Path):
    """`make:kinds --kind`: `{folders}`, `{folder}` и `{name}` полей шапки не требуют — назван только `jira`; при подпапках в
    пути ассистенту сказано класть документ в нужную подпапку."""
    root = tmp / "p"
    (root / "Templates").mkdir(parents=True)
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    (root / "Artifacts" / "ac").mkdir(parents=True)
    (root / "aurora.config.yaml").write_text('project:\n  name: "PRJ-A"\n' + kind_ac("", "{folders} > {folder?}", "{name} {jira}"),
                                             encoding="utf-8")
    out = subprocess.run([sys.executable, str(SCRIPTS / "make_kinds.py"), "--kind", "ac"], cwd=str(root),
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert re.search(r"заполните в шапке документа: jira$", out, re.M), out
    assert "Подпапки внутри Artifacts/ac/ становятся разделами пути" in out, out


@test
def test_publish_folders_are_described_in_the_wiki_and_the_skill(tmp: Path):
    """Вики (ru и en) и справка навыка называют новые подстановки и раскладку «как в папках»: путь `{folders}`, заголовок
    `{title}`; причина отказа и перенос переложенного файла описаны."""
    ru = " ".join((KIT / "wiki/ru/Artifacts-and-Deliverables.md").read_text(encoding="utf-8").split())
    en = " ".join((KIT / "wiki/Artifacts-and-Deliverables.md").read_text(encoding="utf-8").split())
    for text, words in ((ru, ("Как в папках", "Путь `{folders}`, заголовок `{title}`", "одно из трёх", "регистром")),
                        (en, ("Like the folders", "Path `{folders}`, title `{title}`", "one of three", "letter case"))):
        for part in ("`{folders}`", "`{folder}`", "`{name}`", "`misplaced`", "`--move`", "Activity_A1_Core") + words:
            assert part in text, f"в вики нет «{part}»"
    flow = (KIT / "skills/aurora-vault/references/workflows.md").read_text(encoding="utf-8")
    head = (KIT / "skills/aurora-vault/references/frontmatter.md").read_text(encoding="utf-8")
    for part in ("`{folders}`", "`{folder}`", "`{name}`", "как в", "`misplaced`"):
        assert part in flow, f"в workflows.md нет «{part}»"
    assert "`{folders}`" in head and "`{name}`" in head, "в frontmatter.md нет новых подстановок"
    form = _js_function(panel_sources(), "async function renderKinds(")
    assert "confirm(" not in form, "в форме видов появилось окно подтверждения"
