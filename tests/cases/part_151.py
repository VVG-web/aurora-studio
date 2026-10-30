"""Проверки движка Aurora, часть 151. Каркас и помощники — tests/harness.py.

Выпуск B «Confluence»: панель рисует исход публикации кнопками, каждая — явное действие с понятной надписью, без окон
подтверждения: `conflict` → «Открыть страницу», «Обновить зеркало», «Перезаписать» (`--force`); `adopt` → «Взять под
управление» (`--adopt`); `misplaced` → «Переместить по пути вида» (`--move`); `blocked` → причина и кнопка в нужную группу
настроек. Несохранённое уходит тем же кликом «Сохранить и опубликовать». Машинная строка исхода (`AURORA-PUBLISH`) в
консоль не попадает. Форма видов показывает пример пути. Справка (вики, навыки) говорит то же. Поведение страницы —
на её настоящем коде в node; сеть и модель не используются.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, _js_function, panel_sources, test


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _fn(ui: str, head: str) -> str:
    return _js_function(ui, head) + "\n}\n"


def _node(code: str, tmp: Path) -> dict:
    node = shutil.which("node")
    assert node, "нет node: поведение страницы проверить нечем — поставьте его, иначе регрессия доедет до человека"
    src = tmp / "case.mjs"
    src.write_text(code, encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[-1500:]
    return json.loads(cp.stdout)


STUBS = """
const runs = [], shown = [], polled = [];
const el = (tag, attrs, ...kids) => ({tag, attrs: attrs || {}, kids: kids.flat(), disabled: false});
const t = (k, v) => k + (v ? JSON.stringify(v) : "");
const S = {project: {path: "/p"}};
const SGROUPS = {focus: ""};
const show = v => shown.push(v);
const poll = (job, since, label) => polled.push(label);
const api = async (path, opts) => { runs.push(["api", path, JSON.parse(opts.body)]); return {job: "j1"}; };
const runPublish = (rel, args) => runs.push(["publish", rel, args]);
"""


@test
def test_the_publish_card_draws_buttons_by_the_outcome(tmp: Path):
    """`publishActions` на настоящем коде страницы: у каждого исхода свои кнопки, и каждая отправляет ровно названное
    надписью — `--force`, `--adopt`, `--move` только у своих кнопок; черновик — «Опубликовать черновик» с `--allow draft`;
    ворота, которые чинятся в документе, гасят «Отправить»; адрес не http(s) ссылкой не становится."""
    ui = panel_sources()
    code = "".join(_fn(ui, h) for h in ("function pageLink(", "function publishActions(", "function publishWhy(",
                                         "function publishHeadline("))
    out = _node(STUBS + code + """
const rel = "Artifacts/us/US-1.md";
const page = {id: "7", url: "https://wiki.example.com/pages/viewpage.action?pageId=7", version: "3"};
const sum = btns => btns.map(b => ({tag: b.tag, label: b.kids[0], disabled: b.disabled, href: b.attrs.href || ""}));
const click = btns => { const before = runs.length; btns.forEach(b => b.attrs.onclick && b.attrs.onclick()); return runs.slice(before); };
const log = {};
let b = publishActions(rel, {outcome: "ok"}, {plan: true, allow: ["--allow", "draft"], draft: true});
log.planDraft = {btns: sum(b), runs: click(b)};
b = publishActions(rel, {outcome: "ok"}, {plan: true, allow: [], blockedGate: true});
log.planBlockedGate = sum(b);
b = publishActions(rel, {outcome: "conflict", page}, {allow: ["--allow", "no-marker"]});
log.conflict = {btns: sum(b), runs: click(b.filter(x => x.tag === "button"))};
b = publishActions(rel, {outcome: "adopt", page}, {});
log.adopt = {btns: sum(b), runs: click(b.filter(x => x.tag === "button"))};
b = publishActions(rel, {outcome: "misplaced", page}, {plan: true, allow: []});
log.misplacedPlan = {btns: sum(b), runs: click(b.filter(x => x.tag === "button"))};
b = publishActions(rel, {outcome: "misplaced", page}, {});
log.misplacedDone = sum(b);
b = publishActions(rel, {outcome: "blocked", fix: "kinds"}, {});
b[0].attrs.onclick();
log.blocked = {btns: sum(b), focus: SGROUPS.focus, shown: shown.filter(v => v === "project")};
log.taken = sum(publishActions(rel, {outcome: "blocked", code: "title-taken", url: page.url}, {}));
log.unchanged = sum(publishActions(rel, {outcome: "unchanged", page}, {}));
log.badUrl = sum(publishActions(rel, {outcome: "unchanged", url: "javascript:alert(1)"}, {}));
log.why = {missing: publishWhy({code: "missing-field", fields: ["release"], kind: "us"}),
           review: publishWhy({code: "review", review: ["review-missing"]}),
           unknown: publishWhy({code: "zzz", why: "по-русски от движка"}),
           http: publishWhy({code: "http", status: 503})};
log.head = {conflict: publishHeadline({outcome: "conflict", conflict: {was: "1", now: "2", by: "Коллега", when: "2026-10-02"}}, false),
            misplaced: publishHeadline({outcome: "misplaced", where: {now: {title: "Релиз 1.0"}, want: {path: ["Истории", "Архив"]}}}, true),
            created: publishHeadline({outcome: "ok", version: "1", created: ["Релиз 1.0"]}, false)};
await new Promise(ok => setTimeout(ok, 0));
log.mirror = runs.filter(r => r[0] === "api").map(r => r[2].cmd);
log.polled = polled;
console.log(JSON.stringify(log));
""", tmp)
    rel = "Artifacts/us/US-1.md"
    assert out["planDraft"]["btns"] == [{"tag": "button", "label": "pub.send_draft", "disabled": False, "href": ""}], out["planDraft"]
    assert out["planDraft"]["runs"] == [["publish", rel, ["--apply", "--allow", "draft"]]], out["planDraft"]["runs"]
    assert out["planBlockedGate"][0]["disabled"] is True, "ворота, которые чинятся в документе, не погасили «Отправить»"
    labels = lambda key: [x["label"] for x in out[key]["btns"]] if isinstance(out[key], dict) else [x["label"] for x in out[key]]  # noqa: E731
    assert labels("conflict") == ["pub.open", "pub.mirror", "pub.force"], out["conflict"]
    assert ["publish", rel, ["--apply", "--force", "--allow", "no-marker"]] in out["conflict"]["runs"], out["conflict"]["runs"]
    assert out["mirror"] == ["sync:confluence"] and "sync:confluence" in out["polled"], "«Обновить зеркало» не запускает синк"
    assert labels("adopt") == ["pub.open", "pub.adopt"] and out["adopt"]["runs"] == [["publish", rel, ["--apply", "--adopt"]]]
    assert labels("misplacedPlan") == ["pub.update_here", "pub.move", "pub.open"], out["misplacedPlan"]
    assert out["misplacedPlan"]["runs"] == [["publish", rel, ["--apply"]], ["publish", rel, ["--apply", "--move"]]]
    assert labels("misplacedDone") == ["pub.move", "pub.open"], out["misplacedDone"]
    assert labels("blocked") == ["editor.publish_fix_kinds"] and out["blocked"]["focus"] == "project:kinds" \
        and out["blocked"]["shown"] == ["project"], out["blocked"]
    assert out["taken"] == [{"tag": "a", "label": "pub.open_taken", "disabled": False,
                             "href": "https://wiki.example.com/pages/viewpage.action?pageId=7"}], out["taken"]
    assert labels("unchanged") == ["pub.open"] and out["badUrl"] == [], "ссылкой стал адрес не http(s)"
    assert out["why"]["missing"].startswith("pub.why_missing_field") and '"fields":"release"' in out["why"]["missing"], out["why"]
    assert "editor.gate_review_missing" in out["why"]["review"] and out["why"]["unknown"] == "по-русски от движка", out["why"]
    assert out["why"]["http"].startswith("pub.why_http") and '"status":503' in out["why"]["http"], out["why"]
    assert '"was":"1"' in out["head"]["conflict"] and '"by":"Коллега"' in out["head"]["conflict"], out["head"]
    assert out["head"]["misplaced"].startswith("pub.misplaced_plan") and "Истории › Архив" in out["head"]["misplaced"]
    assert "pub.done_created" in out["head"]["created"], out["head"]


@test
def test_no_confirmation_window_in_the_publish_flow(tmp: Path):
    """Публикация без `confirm()`: несохранённое сохраняется тем же кликом, надпись кнопки говорит «Сохранить и
    опубликовать»; черновик в «Продуктивности» — причина и кнопка с явной надписью; «Продуктивность» публикует тем же
    путём, что «Файлы». Остальные окна подтверждения панели (удаление файла, уход со страницы) — вне этой правки."""
    ui = panel_sources()
    flow = ui[ui.index("async function publishFromEditor("):ui.index("async function openPath(")]
    assert "confirm(" not in flow, "в потоке публикации окно подтверждения"
    head = _fn(ui, "async function publishFromEditor(")
    assert "await saveFile()" in head and "F.dirty" in head, "несохранённое не сохраняется тем же кликом"
    assert '"editor.publish_save"' in _fn(ui, "function markDirty(") and '"editor.publish_save"' in _fn(ui, "function drawPublish(")
    view = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    assert "confirm(" not in view and "publish_draft_ask" not in view, "в «Продуктивности» осталось окно подтверждения"
    pub = _fn(view, "async function publish(")
    assert "ctx.ui.publishFile(f.rel)" in pub and "work.publish_go_draft" in pub and "pub.why" in pub, \
        "«Продуктивность» решает о публикации сама, а не по состоянию сервера"
    assert "rec.publish_url" not in view[view.index("const ready = rec.template"):view.index("sel.onchange = draw;")], \
        "кнопку «Опубликовать…» снова гасит отсутствие publish_url у вида"
    ctx = _fn(ui, "function moduleCtx(")
    assert "publishFile:" in ctx and "openSettings:" in ctx
    readme = (KIT / "cockpit/modules/README.md").read_text(encoding="utf-8")
    assert "`ui.publishFile(path)`" in readme and "`ui.openSettings(fix)`" in readme, "контракт модулей не описывает новые входы"
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    assert ru["editor.publish_save"] == "Сохранить и опубликовать" and en["editor.publish_save"] == "Save and publish"
    for key in ("pub.force", "pub.adopt", "pub.move", "pub.mirror", "pub.open"):
        assert ru.get(key) and en.get(key), key
    assert (ru["pub.force"], ru["pub.adopt"], ru["pub.move"], ru["pub.mirror"]) == \
        ("Перезаписать", "Взять под управление", "Переместить по пути вида", "Обновить зеркало"), "надписи кнопок не те, что в решении"
    fresh = [k for k in en if k.startswith(("pub.", "kinds.ex_", "kinds.l_publish_", "kinds.f_publish_", "kinds.publish_path"))]
    assert len(fresh) > 40, fresh
    for key in fresh:
        assert key in ru, f"надпись {key} есть не в обоих каталогах"
        assert not re.search("[а-яА-ЯёЁ]", en[key]), f"русский текст в английском каталоге: {key}: {en[key]}"
    work_en = json.loads((KIT / "cockpit/modules/work/i18n/en.json").read_text(encoding="utf-8"))
    assert "work.publish_draft_ask" not in work_en and work_en["work.publish_go_draft"] == "Publish the draft…"


@test
def test_the_publish_line_stays_out_of_the_console(tmp: Path):
    """Машинная строка `AURORA-PUBLISH` — для панели: консоль, история прогона и шаги маршрута её не показывают, а опрос
    задания кладёт её исход в `PUBLISH_RESULT` и рисует кнопки по завершении."""
    RS, P = _mod("run_summary"), _mod("publish_doc")
    assert RS.MARK_PUBLISH == P.MARK_PUBLISH == "AURORA-PUBLISH " and RS.MARK_PUBLISH in RS.MARKS
    ui = panel_sources()
    assert 'const PUBLISH_MARK = "AURORA-PUBLISH "' in ui and "startsWith(PUBLISH_MARK)" in _fn(ui, "function consoleLine(")
    poll = _js_function(ui, "async function poll(")
    assert "PUBLISH_RESULT[id] = JSON.parse(" in poll and "publishDone(" in poll, poll[:200]
    assert "RS.MARK_PUBLISH" in (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "self.ck.RS.MARKS" in (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")


@test
def test_the_kinds_form_has_the_path_fields_and_an_example(tmp: Path):
    """Рамка «Публикация в Confluence»: корень, путь разделов, шаблон заголовка, промпт публикации; под ними — пример пути,
    который считает сервер (`/api/kinds/example`) по значениям формы. `kindExample` печатает ответ сервера."""
    ui = panel_sources()
    form = _js_function(ui, "async function renderKinds(")
    publish = form[form.index('const publish = el("fieldset"'):form.index('rows.append(el("div",{class:"list-item"')]
    for key in ("publish_url", "publish_path", "publish_title", "publish_prompt"):
        assert f'field("{key}"' in publish, f"поля {key} нет в рамке публикации"
    assert "/api/kinds/example?" in form and "kindExample(" in form and "example)" in publish, "примера пути нет"
    assert re.search(r'publish_path:"", publish_title:"", publish_prompt:"",\s*mcp:""', form), "новый вид без полей пути"
    out = _node("const t = (k, v) => k + (v ? JSON.stringify(v) : \"\");\n" + _fn(ui, "function kindExample(") + """
console.log(JSON.stringify({
  doc: kindExample({doc: "Artifacts/us/US-7.md", root: "Истории", levels: ["Релиз 2.1", "PRJ-7"], title: "[US] Итог", missing: []}),
  miss: kindExample({doc: "Artifacts/us/US-8.md", root: "", levels: [], title: "Итог", missing: ["release"]}),
  none: kindExample({doc: "", root: "#7", levels: ["A", "{b}"], title: "{title}"}),
  err: kindExample({error: "x"}),
}));
""", tmp)
    assert out["doc"] == 'kinds.ex_doc{"doc":"US-7.md","path":"Истории › Релиз 2.1 › PRJ-7 › [US] Итог"}', out["doc"]
    assert out["miss"].startswith('kinds.ex_doc{"doc":"US-8.md","path":"kinds.ex_no_root › Итог"}') and "kinds.ex_missing" in out["miss"], out
    assert out["none"].startswith("kinds.ex_nodoc") and "#7 › A › {b} › {title}" in out["none"] and out["err"] == "", out


@test
def test_docs_describe_the_path_chain_and_the_outcomes(tmp: Path):
    """Вики (ru и en) и навыки говорят о цепочке разделов, подстановках, исходах и новых полях штампа одно и то же;
    `make:kinds --kind` называет путь и поля шапки, которые нужно заполнить при создании."""
    ru = " ".join((KIT / "wiki/ru/Artifacts-and-Deliverables.md").read_text(encoding="utf-8").split())
    en = " ".join((KIT / "wiki/Artifacts-and-Deliverables.md").read_text(encoding="utf-8").split())
    common = ("`publish_path`", "`publish_title`", "`published_input`", "`published_space`", "`published_parent`", "`misplaced`",
              "`--move`", "`--force`", "`--adopt`", "`unchanged`", "`conflict`", "`{title}`")
    for text, parts in ((ru, common + ("Путь в Confluence — цепочка", "что и куда уйдёт", "окон подтверждения нет",
                                       "Сохранить и опубликовать", "незнакомые поля вида и комментарии")),
                        (en, common + ("The path in Confluence is a chain", "what goes where", "no confirmation windows",
                                       "Save and publish", "unknown kind fields and comments"))):
        for part in parts:
            assert part in text, f"в вики нет «{part}»"
    flow = (KIT / "skills/aurora-vault/references/workflows.md").read_text(encoding="utf-8")
    for part in ("publish_path", "publish_title", "AURORA-PUBLISH", "`--move`", "published_input", "решение человека"):
        assert part in flow, f"в workflows.md нет «{part}»"
    head = (KIT / "skills/aurora-vault/references/frontmatter.md").read_text(encoding="utf-8")
    for part in ("published_input", "published_space", "published_parent", "publish_path"):
        assert part in head, f"в frontmatter.md нет «{part}»"
    root = tmp / "p"
    root.mkdir()
    (root / "Templates").mkdir()
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    (root / "Artifacts" / "us").mkdir(parents=True)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Т"\nartifacts:\n  us:\n    template: Templates/US.md\n    out: Artifacts/us\n'
        '    publish_path: "Релиз {release} > {epic?}"\n    publish_title: "[US] {title} {jira}"\n', encoding="utf-8")
    spec = subprocess.run([sys.executable, str(SCRIPTS / "make_kinds.py"), "--kind", "us"], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "Путь под корнем: Релиз {release} > {epic?}" in spec and "заполните в шапке документа: jira, release" in spec, spec
