"""Проверки движка Aurora, часть 145. Каркас и помощники — tests/harness.py.

Поле `publish_prompt` вида артефакта и форма «Артефакты» (выпуск A «Confluence»): для создания документа — шаблон, для
публикации — промпт; форма рисует две рамки («Создание» и «Публикация в Confluence»), промпт публикации сохраняется и
показывается, а модель по нему пока не вызывается — и подсказка поля говорит об этом словами. Запись реестра идёт через
форму и `POST /api/kinds` со слепком `expect`: расхождение — 409, соседние секции конфига байт в байт, ничего заполненного
не пропадает. Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, panel_sources, set_home, test

BOM = "﻿"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _ck():
    _mod("aurora_common")
    if str(KIT / "cockpit") not in sys.path:
        sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


def _project(tmp: Path, config: str, bom: bool = False, crlf: bool = False) -> Path:
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "AC.md").write_text("шаблон", encoding="utf-8")
    text = config.replace("\n", "\r\n") if crlf else config
    (root / "aurora.config.yaml").write_bytes(((BOM if bom else "") + text).encode("utf-8"))
    return root


# Конфиг со всем, что соседствует с секцией: комментарии, пустые строки, чужие секции до и после.
CONFIG = (
    '# Конфиг проекта\n'
    'project:\n  name: "Т"\n  slug: "T"\n\n'
    'atlassian:\n  confluence:\n    space: "T"\n    base_url: "https://wiki.example.com"\n\n'
    '# виды документов\n'
    'artifacts:\n'
    '  # комментарий внутри секции (запись его сохраняет)\n'
    '  ac:\n    title: "Критерии приёмки"\n    template: Templates/AC.md\n    out: Artifacts/ac\n'
    '    prompt: Prompts/AC.md\n    publish_url: https://wiki.example.com/display/PRJ/AC\n    review: us\n'
    '    ref_mask: "<СТРАНА>.<ПРОЕКТ>.US-x.x.x"\n'
    '    task:\n      project: PRJ\n      labels: "ac, аналитика"\n'
    '  us:\n    title: "История"\n    template: Templates/AC.md\n    out: Artifacts/us\n\n'
    '# Типы карточек проекта\n'
    'cards:\n  glossary:\n    title: "Термин"\n\n'
    'reports:\n  year: 2026\n'
)


def _outside(text: str) -> tuple:
    """Всё, что вне секции `artifacts:`: до неё и после (после — от первой строки, начинающейся не с отступа)."""
    head, rest = text.split("artifacts:", 1)
    tail = re.search(r"(?m)^(?:\r?\n)*[^\s].*", rest)
    after = rest[tail.start():] if tail else ""
    return head, after


# ------------------------------------------------------------------ поле и круг «форма → запись → чтение»

@test
def test_publish_prompt_is_a_field_of_the_kind_and_creation_stays_apart(tmp: Path):
    """`publish_prompt` описан движком вместе с остальными полями вида; поля делятся на создание и публикацию без остатка;
    запись и чтение возвращают оба промпта порознь; изготовление документа читает только промпт создания."""
    MK, ck = _mod("make_kinds"), _ck()
    assert "publish_prompt" in MK.FIELDS and "prompt" in MK.FIELDS
    assert set(MK.CREATE_FIELDS) | set(MK.PUBLISH_FIELDS) == set(MK.FIELDS) and not set(MK.CREATE_FIELDS) & set(MK.PUBLISH_FIELDS), \
        "поле вида не отнесено ни к созданию, ни к публикации — форма его не нарисует"
    assert {"template", "prompt", "out"} <= set(MK.CREATE_FIELDS) and \
        {"publish_url", "publish_path", "publish_title", "publish_prompt", "review"} == set(MK.PUBLISH_FIELDS)

    root = _project(tmp, 'project:\n  name: "Т"\n')
    rec = {"title": "Критерии", "template": "Templates/AC.md", "out": "Artifacts/ac", "prompt": "Prompts/AC_create.md",
           "publish_url": "https://wiki.example.com/pages/viewpage.action?pageId=7", "publish_prompt": "Prompts/AC_publish.md",
           "review": "us", "task": {}}
    assert ck.kinds_write(str(root), {"ac": rec}).get("ok")
    back = MK.read_kinds(str(root))["ac"]
    assert back["prompt"] == "Prompts/AC_create.md" and back["publish_prompt"] == "Prompts/AC_publish.md", back
    # создание не читает промпт публикации: поле живёт в реестре, а не в спецификации изготовления
    R = _mod("agent_runner")
    spec = R.make_spec(str(root), "ac")
    assert spec.get("prompt") == "Prompts/AC_create.md" and "publish_prompt" in spec, spec
    assert "publish_prompt" not in (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8"), \
        "изготовление документа начало читать промпт публикации — решение владельца: он только оформляет страницу"

    # значение со знаками, которые самодельный разбор читает по-своему, доезжает как есть
    odd = 'Prompts/a "b" # c.md'
    rec["publish_prompt"] = odd
    assert ck.kinds_write(str(root), {"ac": rec}).get("ok")
    assert MK.read_kinds(str(root))["ac"]["publish_prompt"] == odd, MK.read_kinds(str(root))["ac"]["publish_prompt"]


@test
def test_an_older_config_loses_nothing_when_the_form_saves_it_back(tmp: Path):
    """Проекты без `publish_prompt`: поле читается пустым, форма сохраняет всё заполненное (промпт создания, адрес, ревью,
    маску, свойства задачи, второй вид), а `--kind` не говорит про промпт публикации, пока он не задан."""
    MK, ck = _mod("make_kinds"), _ck()
    root = _project(tmp, CONFIG)
    before = MK.read_kinds(str(root))
    assert before["ac"]["publish_prompt"] == "" and before["ac"]["prompt"] == "Prompts/AC.md", before["ac"]
    assert ck.kinds_write(str(root), before).get("ok")
    after = MK.read_kinds(str(root))
    assert after == before, f"форма потеряла заполненное: {before} → {after}"
    assert after["ac"]["task"] == {"project": "PRJ", "labels": ["ac", "аналитика"]}, after["ac"]["task"]
    spec = subprocess.run([sys.executable, str(SCRIPTS / "make_kinds.py"), "--kind", "ac"], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "Промпт публикации" not in spec and "Промпт создания: Prompts/AC.md" in spec, spec


@test
def test_artifact_spec_separates_creation_from_publication(tmp: Path):
    """`make_kinds.py --kind` (его печатает MCP-инструмент `artifact_spec`): два раздела — «Для создания» и «Для публикации»;
    промпт публикации ассистенту при создании не навязывается; граница чистовика осталась строкой без отступа."""
    MK = _mod("make_kinds")
    root = _project(tmp, CONFIG)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("    review: us\n", "    review: us\n    publish_prompt: Prompts/AC_publish.md\n"),
                   encoding="utf-8")
    out = subprocess.run([sys.executable, str(SCRIPTS / "make_kinds.py"), "--kind", "ac"], cwd=str(root),
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    create, publish = out.split("## Для публикации в Confluence")
    assert "## Для создания" in create and "Шаблон: Templates/AC.md" in create and "Промпт создания: Prompts/AC.md" in create, create
    assert "Публикуется в: https://wiki.example.com/display/PRJ/AC" in publish and "Ревью перед публикацией: профиль us" in publish
    assert "Промпт публикации: Prompts/AC_publish.md" in publish and "при создании документа его не применяют" in publish, publish
    assert "Prompts/AC_publish.md" not in create, "промпт публикации попал в раздел создания"
    assert [ln for ln in out.splitlines() if MK.MADE_MARK in ln] == [MK.MADE_MARK], "граница чистовика показана не отдельной строкой"
    # у вида без настроек публикации раздела публикации нет
    assert "## Для публикации" not in subprocess.run([sys.executable, str(SCRIPTS / "make_kinds.py"), "--kind", "us"], cwd=str(root),
                                                      capture_output=True, text=True, encoding="utf-8").stdout


# ------------------------------------------------------------------ запись реестра: слепок, 409, соседи

@test
def test_kinds_write_checks_the_digest_and_keeps_the_rest_of_the_config_byte_for_byte(tmp: Path):
    """Запись секции `artifacts:` со слепком: расхождение — отказ с свежим состоянием и файл нетронут; слепок считается по
    тому, как секцию читает движок (комментарий — не изменение); BOM, CRLF и соседние секции остаются байт в байт."""
    MK, ck = _mod("make_kinds"), _ck()
    root = _project(tmp, CONFIG, bom=True, crlf=True)
    cfg = root / "aurora.config.yaml"
    raw0 = cfg.read_bytes()
    state = ck.kinds_read(str(root))
    digest0 = state["digest"]
    assert len(digest0) == 64 and state["kinds"]["ac"]["publish_prompt"] == "", state["kinds"]["ac"]

    # 1. запись со свежим слепком
    kinds = json.loads(json.dumps(state["kinds"]))
    kinds["ac"]["publish_prompt"] = "Prompts/AC_publish.md"
    res = ck.kinds_write(str(root), kinds, digest0)
    assert res.get("ok") and res["digest"] != digest0, res
    raw1 = cfg.read_bytes()
    text1 = raw1.decode("utf-8")
    assert raw1.startswith(BOM.encode("utf-8")) and "\n" not in text1.replace("\r\n", ""), "BOM или CRLF не сохранены"
    head0, after0 = _outside(raw0.decode("utf-8"))
    head1, after1 = _outside(text1)
    assert head1 == head0 and after1 == after0, f"запись задела соседние секции:\n{head0!r}\n{head1!r}\n{after0!r}\n{after1!r}"
    assert "cards:" in after1 and "reports:" in after1 and "# Типы карточек проекта" in after1
    assert MK.read_kinds(str(root))["ac"]["publish_prompt"] == "Prompts/AC_publish.md"
    assert not [p.name for p in root.iterdir() if p.is_file() and p.name.endswith(_mod("aurora_fs").TMP_SUFFIX)], \
        "после записи остался временный файл"

    # 2. повторная запись того же — файл не меняется
    again = ck.kinds_write(str(root), json.loads(json.dumps(ck.kinds_read(str(root))["kinds"])), res["digest"])
    assert again.get("ok") and cfg.read_bytes() == raw1, "повторная запись того же реестра изменила файл"

    # 3. кто-то правил файл между чтением и записью → отказ, файл как был, в ответе свежее состояние
    hand = cfg.read_bytes().decode("utf-8").replace("Критерии приёмки", "Критерии (руками)")
    cfg.write_bytes(hand.encode("utf-8"))
    mine = json.loads(json.dumps(kinds))
    mine["us"]["title"] = "Моя правка"
    clash = ck.kinds_write(str(root), mine, res["digest"])
    assert clash.get("conflict") is True and "изменились на диске" in clash["error"], clash
    assert clash["kinds"]["ac"]["title"] == "Критерии (руками)" and clash["digest"] != res["digest"], "в ответе нет свежего состояния"
    assert cfg.read_bytes() == hand.encode("utf-8"), "при расхождении файл всё равно записан"
    # «Оставить моё» — запись без слепка; «Взять с диска» — запись по свежему слепку
    assert ck.kinds_write(str(root), mine).get("ok") and MK.read_kinds(str(root))["us"]["title"] == "Моя правка"
    fresh = ck.kinds_read(str(root))
    assert ck.kinds_write(str(root), fresh["kinds"], fresh["digest"]).get("ok")

    # 4. комментарий или пустая строка внутри секции слепок не меняют: «как читает движок»
    before = ck.kinds_read(str(root))["digest"]
    cfg.write_bytes(cfg.read_bytes().replace(b"  ac:\r\n", "  # ещё комментарий\r\n\r\n  ac:\r\n".encode("utf-8"), 1))
    assert ck.kinds_read(str(root))["digest"] == before, "комментарий в секции назван расхождением"
    # а правка значения — меняет
    cfg.write_bytes(cfg.read_bytes().replace(b"Prompts/AC.md", b"Prompts/AC2.md", 1))
    assert ck.kinds_read(str(root))["digest"] != before


@test
def test_kinds_write_refuses_what_would_corrupt_the_config(tmp: Path):
    """Перевод строки или управляющий знак в значении превратил бы остаток в чужое поле; не-объект, не-UTF-8 и слепок
    не строкой — отказ, а файл не тронут."""
    ck = _ck()
    root = _project(tmp, CONFIG)
    cfg = root / "aurora.config.yaml"
    raw = cfg.read_bytes()
    kinds = ck.kinds_read(str(root))["kinds"]

    def refused(mutate, expect_text: str, **kw):
        k = json.loads(json.dumps(kinds))
        mutate(k)
        r = ck.kinds_write(str(root), k, **kw)
        assert "error" in r and expect_text in r["error"] and not r.get("ok"), (expect_text, r)
        assert cfg.read_bytes() == raw, f"отказ, а файл изменён: {expect_text}"

    refused(lambda k: k["ac"].update(publish_prompt="Prompts/a.md\nout: Evil"), "несколько строк")
    refused(lambda k: k["ac"].update(prompt="Prompts/a out: Evil"), "несколько строк")
    refused(lambda k: k["ac"]["task"].update(project="PRJ\rtype: X"), "несколько строк")
    refused(lambda k: k.update({"Ac ": {}}), "имя типа")
    assert "error" in ck.kinds_write(str(root), ["ac"]) and "error" in ck.kinds_write(str(root), {"ac": "строка"})
    assert "error" in ck.kinds_write(str(root), kinds, 123) and cfg.read_bytes() == raw
    cfg.write_bytes(b"project:\n  name: \xff\xfe\n")
    bad = ck.kinds_write(str(root), kinds)
    assert "UTF-8" in bad.get("error", "") and cfg.read_bytes() == b"project:\n  name: \xff\xfe\n", bad


class _Panel:
    """Сервер панели на временном ките: проект в обнаруживаемой папке, запросы — настоящими HTTP."""

    def __init__(self, tmp: Path):
        self.restore = set_home(tmp / "home")
        self.ck = _ck()
        self.kit = tmp / "kit"
        (self.kit / "local").mkdir(parents=True)
        self.old_kit = self.ck.KIT
        self.ck.KIT = str(self.kit)
        self.roots = tmp / "roots"
        self.roots.mkdir()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.ck.Handler)
        self.srv.roots = [str(self.roots)]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=60)
        headers = {"X-Aurora-Token": self.ck.TOKEN}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8") or "{}")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.ck.KIT = self.old_kit
        self.restore()


@test
def test_post_api_kinds_answers_409_on_a_stale_digest_in_both_languages(tmp: Path):
    """`POST /api/kinds`: слепок из `GET /api/kinds`; устаревший — 409 с причиной на языке страницы и свежим состоянием;
    свежий — 200; негодный — 400. Это единственный путь записи реестра из панели."""
    panel = _Panel(tmp)
    try:
        root = _project(panel.roots, CONFIG)
        project = str(root)
        st, got = panel.call("GET", "/api/kinds?project=" + project)
        assert st == 200 and got["digest"] and "publish_prompt" in got["kinds"]["ac"], (st, got.get("error"))
        kinds = got["kinds"]
        kinds["ac"]["publish_prompt"] = "Prompts/AC_publish.md"
        st, res = panel.call("POST", "/api/kinds", {"project": project, "kinds": kinds, "expect": got["digest"]})
        assert st == 200 and res["ok"] and res["digest"] != got["digest"], (st, res)
        # вторая вкладка пишет со старым слепком
        kinds["us"]["title"] = "Чужая вкладка"
        st, res = panel.call("POST", "/api/kinds", {"project": project, "kinds": kinds, "expect": got["digest"]})
        assert st == 409 and res["conflict"] is True and "изменились на диске" in res["error"], (st, res)
        st, res = panel.call("POST", "/api/kinds?lang=en", {"project": project, "kinds": kinds, "expect": got["digest"]})
        assert st == 409 and res["error"] == "the artifact kinds changed on disk since they were opened", res["error"]
        assert res["kinds"]["ac"]["publish_prompt"] == "Prompts/AC_publish.md", "в ответе нет свежего состояния"
        assert _mod("make_kinds").read_kinds(project)["us"]["title"] != "Чужая вкладка", "при 409 записано"
        # негодный запрос — 400 с причиной на языке страницы
        bad = json.loads(json.dumps(kinds))
        bad["ac"]["publish_prompt"] = "a\nb"
        st, res = panel.call("POST", "/api/kinds?lang=en", {"project": project, "kinds": bad, "expect": res["digest"]})
        assert st == 400 and "several lines" in res["error"], (st, res)
        st, res = panel.call("POST", "/api/kinds", {"project": project, "kinds": kinds, "expect": "не строка и не слепок"})
        assert st == 409, (st, res)
    finally:
        panel.close()


@test
def test_publish_state_reason_reaches_the_english_page_in_english(tmp: Path):
    """`GET /api/files/read?lang=en`: причина серой кнопки приходит на языке страницы, а место починки — машинным словом."""
    panel = _Panel(tmp)
    try:
        root = _project(panel.roots, CONFIG)
        doc = root / "Artifacts" / "ac" / "AC-1.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("---\ntype: ac\nstatus: ready\n---\n\n# Итог\n", encoding="utf-8")
        query = "/api/files/read?project=" + str(root) + "&path=Artifacts/ac/AC-1.md"
        st, got = panel.call("GET", query)
        assert st == 200 and got["publish"]["state"] == "ready" and got["publish"]["parent_from"] == "publish_url", got.get("error")
        # у вида убрали родителя: кнопка серая, причина — на языке страницы
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace("    publish_url: https://wiki.example.com/display/PRJ/AC\n", ""),
                       encoding="utf-8")
        st, ru = panel.call("GET", query)
        st_en, en = panel.call("GET", query + "&lang=en")
        assert st == 200 and st_en == 200, (ru.get("error"), en.get("error"))
        assert ru["publish"]["state"] == "blocked" and ru["publish"]["why"].startswith("Опубликовать некуда"), ru["publish"]
        assert en["publish"]["why"] == "Nowhere to publish: this document's kind has no parent page in Confluence.", en["publish"]
        assert en["publish"]["fix"] == "kinds" == ru["publish"]["fix"], "место починки зависит от языка"
    finally:
        panel.close()


# ------------------------------------------------------------------ форма и каталоги

def _fn(ui: str, head: str) -> str:
    start = ui.index(head)
    return ui[start:ui.index("\n}\n", start)]


@test
def test_kinds_form_has_two_frames_and_names_the_role_of_template_and_prompt(tmp: Path):
    """Форма вида: рамка «Создание» (шаблон, промпт создания, папка, MCP, маска, «без технологий», задача) и рамка
    «Публикация в Confluence» (родитель, промпт публикации, ревью); запись идёт со слепком и умеет расхождение."""
    ui = panel_sources()
    form = _fn(ui, "async function renderKinds(")
    create = form[form.index('const create = el("fieldset"'):form.index('const publish = el("fieldset"')]
    publish = form[form.index('const publish = el("fieldset"'):form.index('rows.append(el("div",{class:"list-item"')]
    for key in ("template", "prompt", "out", "ref_mask"):
        assert f'field("{key}"' in create and f'field("{key}"' not in publish, f"поле {key} не в рамке «Создание»"
    # MCP — плашки и «Выбрать MCP…» (`mcpBox`), а не поле ввода (часть 166).
    assert re.search(r't\("kinds\.l_mcp"\)(, " ", kindTip\(t\("kinds\.tip_mcp"\)\))?\), mcpBox\)', create) \
        and "mcpBox" not in publish, "поле mcp не в рамке «Создание»"
    for key in ("publish_url", "publish_prompt"):
        assert f'field("{key}"' in publish and f'field("{key}"' not in create, f"поле {key} не в рамке «Публикация»"
    assert 't("kinds.review")' in publish and 't("kinds.review")' not in create and "tech_agnostic" in create
    assert 'data-frame":"create"' in create and 'data-frame":"publish"' in publish
    assert 't("kinds.frame_create")' in create and 't("kinds.frame_publish")' in publish
    assert re.search(r'publish_prompt:"",\s*mcp:""', form) and re.search(r'review:"",[^}]*task:\{\}', form), \
        "новый вид создаётся без поля publish_prompt"
    # слепок уходит с запросом, расхождение показано и разрешимо обоими способами
    assert 'expect: force ? "" : KINDS_DIGEST' in form and "r.conflict" in form and "kinds.take_disk" in form and "kinds.keep_mine" in form
    assert "d.digest" in form and "r.digest" in form, "слепок не обновляется после чтения и записи"

    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    used = {k for k in re.findall(r't\("(kinds\.[a-z_]+)"', form) if not k.endswith("_")} | {f"kinds.l_{k}" for k in
           ("title", "template", "prompt", "out", "mcp", "ref_mask", "publish_url", "publish_prompt")}
    for lang, cat in (("ru", ru), ("en", en)):
        missing = sorted(k for k in used if k not in cat)
        assert not missing, f"нет надписей формы видов ({lang}): {missing}"
    # шапка группы — одним абзацем и главным правилом
    assert ru["kinds.about"].startswith("Создание — шаблон. Публикация — родитель и промпт.") and "\n" not in ru["kinds.about"]
    assert en["kinds.about"].startswith("Creation is the template. Publishing is the parent and the prompt.") and "\n" not in en["kinds.about"]
    # подписи называют роль
    assert "Шаблон" in ru["kinds.l_template"] and "создани" in ru["kinds.l_template"]
    assert "Промпт создания" in ru["kinds.l_prompt"] and "Промпт публикации" in ru["kinds.l_publish_prompt"]
    assert "Template" in en["kinds.l_template"] and "creat" in en["kinds.l_template"]
    assert "Creation prompt" in en["kinds.l_prompt"] and "Publishing prompt" in en["kinds.l_publish_prompt"]
    assert ru["kinds.frame_create"] == "Создание" and ru["kinds.frame_publish"] == "Публикация в Confluence"
    assert en["kinds.frame_create"] == "Creation" and en["kinds.frame_publish"] == "Publishing to Confluence"


@test
def test_publish_prompt_hint_is_honest_that_no_model_runs_yet(tmp: Path):
    """Подсказка поля говорит человеческим языком: сохраняется, подготовка страницы включится позже, пока публикация без
    модели. Пока это правда, она живёт в обоих каталогах; в публикации модель не вызывается."""
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    hint_ru, hint_en = ru["kinds.publish_prompt_hint"], en["kinds.publish_prompt_hint"]
    assert "Сохраняется" in hint_ru and "ближайших выпусков" in hint_ru and "без модели" in hint_ru, hint_ru
    assert "Saved" in hint_en and "next releases" in hint_en and "without a model" in hint_en, hint_en
    publish = (SCRIPTS / "publish_doc.py").read_text(encoding="utf-8")
    assert "call_role" not in publish and "agent_core" not in publish and "publish_prompt" not in publish, \
        "публикация начала вызывать модель, а подсказка поля всё ещё говорит «без модели»"
    llm = json.loads((SCRIPTS / "llm_calls.json").read_text(encoding="utf-8"))
    assert "ship:publish" not in json.dumps(llm, ensure_ascii=False), "в каталоге вызовов моделей появилась публикация"


@test
def test_work_section_shows_creation_and_publication_apart(tmp: Path):
    """«Продуктивность»: строка вида — две строки, «Создание:» и «Публикация:»; промпты называются по роли, в обоих языках."""
    view = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    draw = _fn(view, "async function fillKinds(")
    assert 'work.spec_head_create' in draw and 'work.spec_head_publish' in draw and "rec.publish_prompt" in draw
    create = draw[draw.index("const create = ["):draw.index("const publish = [")]
    publish = draw[draw.index("const publish = ["):draw.index("replaceChildren(")]
    assert "rec.prompt" in create and "rec.publish_prompt" not in create and "rec.publish_prompt" in publish and "rec.prompt" not in publish, \
        "промпт создания и промпт публикации в строке вида стоят не в своих строках"
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/modules/work/i18n/{lang}.json").read_text(encoding="utf-8"))
        for key in ("work.spec_head_create", "work.spec_head_publish", "work.spec_publish_prompt", "work.spec_no_publish_prompt"):
            assert cat.get(key), (lang, key)
    ru = json.loads((KIT / "cockpit/modules/work/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/modules/work/i18n/en.json").read_text(encoding="utf-8"))
    assert ru["work.spec_prompt"] == "промпт создания: {v}" and ru["work.spec_publish_prompt"] == "промпт публикации: {v}"
    assert en["work.spec_prompt"] == "creation prompt: {v}" and en["work.spec_publish_prompt"] == "publishing prompt: {v}"
