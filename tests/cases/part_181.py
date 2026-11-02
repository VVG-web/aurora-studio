"""Проверки движка Aurora, часть 181. Каркас — tests/harness.py, подставная вики — part_148.py, сервер — part_107.py.

Выбор места публикации в окне самой публикации: операция `children` вики с признаком `has_children` (папка или лист —
дешёвый вопрос «limit=1», публикация тот же список без признака), кэш списка на час и `fresh=1`; API панели
`/api/publish/tree` (спуск по дереву от корня вида/шапки/конфига, уже лежащая страница — выбора нет) и
`/api/publish/place` (поле `publish_parent` шапки со слепком и 409, не-номер — 400); строка каталога в обоих языках.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

from cases import part_107 as S
from cases import part_148 as W
from cases import part_170 as T
from harness import make_project, set_home, test

ENV = {"CONFLUENCE_PAT": T.TOKEN, "CONFLUENCE_PERSONAL_TOKEN": "", "CONFLUENCE_USER": "", "CONFLUENCE_PASSWORD": ""}

KIND_US = ('artifacts:\n'
           '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
           '    publish_url: "{root}"\n'
           '    publish_path: "Релиз {{release}}"\n'
           '    publish_title: "[US] {{title}}"\n')


def _q(root: Path, **kw) -> str:
    return "?" + urllib.parse.urlencode({"project": str(root), **kw})


@test
def test_wiki_children_report_has_children_by_a_cheap_probe_and_cache(tmp: Path):
    """`children` с `deep` — каждой странице `has_children` (папка/лист): по одному дешёвому запросу «limit=1», не
    списку целиком; публикация зовёт список без признака (`deep: false`). Список кэшируется на час, `fresh=True`
    обходит кэш; ключ кэша различает родителей."""
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url)
    rid = wiki.add("Корень", None, "DOC")
    folder = wiki.add("Релиз 2.1", rid, "DOC")
    wiki.add("Раздел", folder, "DOC")
    wiki.add("Лист", rid, "DOC")
    CO = T._mod("connector_ops")
    try:
        with T.env(**ENV):
            got = CO.call(str(root), "wiki", "children", {"page_id": rid, "deep": True})
            assert got["ok"], got
            pages = {p["title"]: p for p in got["result"]["pages"]}
            assert pages["Релиз 2.1"]["has_children"] is True, pages
            assert pages["Лист"]["has_children"] is False, pages
            # дешёвый вопрос — limit=1, не полный список: в журнале по одному на страницу
            probes = [p for m, p, _b in wiki.log if m == "GET" and "child/page" in p and "limit=1" in p]
            assert len(probes) == 2 and any(f"/{folder}/child/page" in p for p in probes), probes
            # публикация — тот же список без признака: ни одного дешёвого вопроса
            wiki.log.clear()
            plain = CO.call(str(root), "wiki", "children", {"page_id": rid, "deep": False})
            assert plain["ok"] and all("has_children" not in p for p in plain["result"]["pages"]), plain
            assert not [p for m, p, _b in wiki.log if "limit=1" in p], wiki.log
            # кэш на час: второй раз без сети, fresh — снова к серверу
            wiki.srv.shutdown()
            again = CO.call(str(root), "wiki", "children", {"page_id": rid, "deep": True})
            assert again["ok"] and again["cached"], again
            other = CO.call(str(root), "wiki", "children", {"page_id": folder, "deep": True})
            assert not other.get("cached"), other
    finally:
        wiki.close()


@test
def test_publish_tree_panel_api_walks_from_the_kind_root_and_refuses_without_one(tmp: Path):
    """`GET /api/publish/tree` — дети корня публикации (порядок `ship:publish`: вид → шапка → конфиг) с признаком
    `has_children`; `parent` — спуск внутрь выбранной папки; корня нет — причина и куда идти; страница уже лежит
    (`confluence_page_id`) — `fixed`, выбора нет; файл менялся на диске — слепок тот же сервис читает."""
    restore = set_home(tmp / "home")
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url, KIND_US)
    rid = wiki.add("Корень", None, "DOC")
    folder = wiki.add("Релиз 2.1", rid, "DOC")
    wiki.add("Раздел", folder, "DOC")
    leaf = wiki.add("Лист", rid, "DOC")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("{root}", rid), encoding="utf-8")
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: us\n---\n\n# Итог\n", encoding="utf-8")
    srv, call = S._server(tmp)
    try:
        with T.env(**ENV):
            st, d = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and d["root"] == rid, d
            titles = {p["title"]: p["has_children"] for p in d["pages"]}
            assert titles == {"Релиз 2.1": True, "Лист": False}, d
            st, deep = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md", parent=folder))
            assert st == 200 and deep["root"] == folder and [x["title"] for x in deep["pages"]] == ["Раздел"], deep
            st, cached = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert cached.get("cached"), cached
            st, fresh = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md", fresh="1"))
            assert not fresh.get("cached"), fresh
            # корень и шапки нет: вид без publish_url, конфиг без publish_parent — причина и «настройки проекта»
            (tmp / "elsewhere").mkdir(exist_ok=True)
            other = make_project(tmp / "elsewhere")
            (other / "Artifacts" / "us").mkdir(parents=True, exist_ok=True)
            (other / "Artifacts" / "us" / "US-1.md").write_text("---\ntype: us\n---\n\n# Итог\n", encoding="utf-8")
            st, nope = call("GET", "/api/publish/tree" + _q(other, path="Artifacts/us/US-1.md"))
            assert st == 200 and nope["pages"] == [] and nope["why"], nope
            # страница уже лежит: confluence_page_id в шапке — выбора нет
            doc.write_text(f"---\ntype: us\nconfluence_page_id: {leaf}\n---\n\n# Итог\n", encoding="utf-8")
            st, fixed = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and fixed["fixed"] and fixed["pages"] == [], fixed
    finally:
        srv.shutdown()
        wiki.close()
        restore()


@test
def test_publish_tree_base_flag_starts_at_kind_root_not_at_the_saved_place(tmp: Path):
    """Окно выбора места начинает спуск с корня вида (`base=1`), а не с сохранённого `publish_parent`: иначе после
    выбора папки она становится корнем окна и вернуться выше нельзя. Без флага API по-прежнему отдаёт точное место."""
    restore = set_home(tmp / "home")
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url, KIND_US)
    rid = wiki.add("Корень", None, "DOC")
    folder = wiki.add("Релиз 2.1", rid, "DOC")
    wiki.add("Раздел", folder, "DOC")
    wiki.add("Лист", rid, "DOC")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("{root}", rid), encoding="utf-8")
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(f"---\ntype: us\npublish_parent: {folder}\n---\n\n# Итог\n", encoding="utf-8")
    srv, call = S._server(tmp)
    try:
        with T.env(**ENV):
            st, saved = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and saved["root"] == folder, saved
            assert [p["title"] for p in saved["pages"]] == ["Раздел"], saved
            st, base = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md", base="1"))
            assert st == 200 and base["root"] == rid, base
            assert {p["title"] for p in base["pages"]} == {"Релиз 2.1", "Лист"}, base
    finally:
        srv.shutdown()
        wiki.close()
        restore()


@test
def test_publish_place_panel_api_writes_the_header_with_a_snapshot_and_409(tmp: Path):
    """`POST /api/publish/place` — поле `publish_parent` шапки той же записью, что редактор: слепок обязателен,
    файл менялся — 409 и файл не тронут; не-номер — 400; повтор тем же номером — `unchanged`, файл не тронут;
    BOM и CRLF сохраняются (шапку пишет `set_field`, файл — `file_write`)."""
    restore = set_home(tmp / "home")
    root = make_project(tmp)
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("﻿---\ntype: us\r\n---\r\n\r\n# Итог\r\n", encoding="utf-8-sig")
    srv, call = S._server(tmp)
    try:
        st, first = call("GET", "/api/files/read" + _q(root, path="Artifacts/us/US-1.md"))
        digest = first.get("digest") or ""
        assert digest, first
        body = {"project": str(root), "path": "Artifacts/us/US-1.md", "parent": "506912207", "expect": digest}
        st, res = call("POST", "/api/publish/place", body)
        assert st == 200 and res["ok"] and res["parent"] == "506912207", res
        raw = doc.read_bytes()
        text = raw.decode("utf-8-sig")
        assert b"publish_parent: 506912207" in raw, raw[:80]
        assert raw.startswith(b"\xef\xbb\xbf"), "BOM потерян"
        assert "\r\n" in text, "CRLF потерян"
        assert "\r\n\r\n# Итог\r\n" in text, repr(text[-40:])
        st, again = call("POST", "/api/publish/place", {**body, "expect": res.get("digest") or digest})
        assert st == 200 and again["ok"] and again["unchanged"], again
        st, stale = call("POST", "/api/publish/place", {**body, "expect": "not-the-digest"})
        assert st == 409 and stale["conflict"], stale
        st, bad = call("POST", "/api/publish/place", {**body, "parent": "../etc", "expect": res.get("digest") or digest})
        assert st == 400 and bad["error"], bad
    finally:
        srv.shutdown()
        restore()


@test
def test_publish_place_texts_and_button_are_in_both_catalogs_and_the_script(tmp: Path):
    """Строки окна выбора места — в русском и английском каталогах без остатков `{…}`; кнопка «Выбрать место…» стоит в
    карточке публикации и сохраняет выбор полем `publish_parent` шапки; окно спрашивает `/api/publish/tree`, различает
    лист (выбор сразу) и папку («Положить в корень»)."""
    import json
    kit = Path(__file__).resolve().parent.parent.parent
    base = kit / "cockpit" / "i18n"
    ru = json.loads((base / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    keys = [k for k in ru if k.startswith("pub.place")]
    assert len(keys) >= 12, keys
    assert all(k in en for k in keys), [k for k in keys if k not in en]
    assert ru["pub.place_root"] == "Положить в корень «{title}»" and "{title}" in en["pub.place_root"]
    src = (kit / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert "function pickPublishPlace" in src, "окна выбора места нет в panel.js"
    assert "/api/publish/tree" in src and "/api/publish/place" in src, "окно не спрашивает API дерева/места"
    assert "publish_parent" not in src.split("pickPublishPlace")[1].split("\n}")[0], "окно само пишет поле шапки"
    card = src.split("function drawPublishCard")[1].split("\n}", 1)[0]
    assert 't("pub.place")' in card, "кнопки «Выбрать место…» нет в карточке публикации"
    assert "pickPublishPlace({" in card and "parent: place.id" in card, card[-400:]
    assert "has_children" in src, "лист/папка не различаются"
    dialog = src.split("async function pickPublishPlace")[1].split("\n}\n", 1)[0]
    assert "pub.place_up" in dialog and "chain.pop()" in dialog, "из папки нельзя вернуться на уровень выше"
    assert 'base: "1"' in dialog and "pub.place_base_root" in dialog, "окно не начинает с корня вида"
    for k in ("pub.place_up", "pub.place_base", "pub.place_base_root"):
        assert k in ru and k in en, k


@test
def test_kinds_page_title_is_a_named_choice_with_file_name_and_tooltips(tmp: Path):
    """Требование владельца: заголовок страницы выбирается понятным списком, а не свободным шаблоном; вариант «имя
    файла» заменяет «_» пробелами, у каждого варианта есть подсказка, как именно получится название."""
    import json
    kit = Path(__file__).resolve().parent.parent.parent
    base = kit / "cockpit" / "i18n"
    ru = json.loads((base / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    keys = ("kinds.title_default", "kinds.title_h1", "kinds.title_name", "kinds.title_file", "kinds.title_custom",
            "kinds.title_tip_default", "kinds.title_tip_h1", "kinds.title_tip_name", "kinds.title_tip_file",
            "kinds.title_tip_custom")
    assert all(k in ru and k in en for k in keys), [k for k in keys if k not in ru or k not in en]
    src = (kit / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    block = src.split("const titlePresets")[1].split("const publish =", 1)[0]
    assert '"{name}"' in block and '"{file}"' in block, "нет вариантов имени файла"
    assert 't("kinds.title_name")' in block and 't("kinds.title_tip_" + key)' in block, "нет подписи или подсказки"
    assert "titleHint.textContent" in block and "titleTip.title" in block, "подсказка не обновляется по выбору"


# ------------------------------------------------------------------ регрессии выпуска 1.202.0
# Каждая проверка ниже — найденная на живом проекте ошибка или требование владельца, а не выдуманный случай.

@test
def test_publish_tree_accepts_a_root_given_as_a_confluence_link(tmp: Path):
    """Найдено на живом проекте: `publish_url` вида — ссылка `…/pages/viewpage.action?pageId=NNN`, и окно получало отказ
    «page_id: нужен номер страницы, а не «https://…»» — пустое окно. Корень приводится к номеру тем же разбором, что
    у публикации: ссылка с `pageId`, голый номер и `/display/ПРОСТРАНСТВО/Заголовок` (последний — поиском страницы)."""
    restore = set_home(tmp / "home")
    wiki = W.Wiki()
    root = W.project(tmp, wiki.url, KIND_US)
    rid = wiki.add("Корень", None, "DOC")
    wiki.add("Релиз 2.1", rid, "DOC")
    cfg = root / "aurora.config.yaml"
    link = f"{wiki.url}/pages/viewpage.action?pageId={rid}"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("{root}", link), encoding="utf-8")
    doc = root / "Artifacts" / "us" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: us\n---\n\n# Итог\n", encoding="utf-8")
    srv, call = S._server(tmp)
    try:
        with T.env(**ENV):
            st, d = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and not d.get("error"), d
            assert d["root"] == rid and [p["title"] for p in d["pages"]] == ["Релиз 2.1"], d
            assert all(p.get("has_children") is False for p in d["pages"]), d
            # ссылка /display/… номера не несёт — страницу ищут по пространству и заголовку
            cfg.write_text(cfg.read_text(encoding="utf-8").replace(link, f"{wiki.url}/display/DOC/Корень"), encoding="utf-8")
            st, named = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and not named.get("error") and named["root"] == rid, named
            # неразобранный корень — причина словами, а не пустой список без объяснения
            cfg.write_text(cfg.read_text(encoding="utf-8").replace(f"{wiki.url}/display/DOC/Корень", "просто-строка"),
                           encoding="utf-8")
            st, bad = call("GET", "/api/publish/tree" + _q(root, path="Artifacts/us/US-1.md"))
            assert st == 200 and bad["pages"] == [] and "не разобрана" in bad["why"], bad
    finally:
        srv.shutdown()
        wiki.close()
        restore()


@test
def test_publish_place_dialog_shows_a_reason_instead_of_an_empty_window(tmp: Path):
    """Найдено на живом проекте: при отказе окно выбора места выглядело пустым. Отказ операции (нет доступа, нет связи) приходит
    причиной и подробностью; окно показывает причину строкой, а не молчит; ответ без списка и без причины — тоже
    причина, а не пустота."""
    src = (Path(__file__).resolve().parent.parent.parent / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    dialog = src.split("async function pickPublishPlace")[1].split("\n}\n", 1)[0]
    assert "pub.place_err" in dialog and "(r.detail || r.error)" in dialog, "окно молчит при отказе"
    assert "if (!r || r.error)" in dialog and "if (r.why)" in dialog, dialog[:0]
    assert "catch" in dialog or "try" in dialog, "сбой запроса оставит пустое окно"
    server = (Path(__file__).resolve().parent.parent.parent / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    body = server.split("def publish_tree(")[1].split("\ndef ")[0]
    assert '"detail": got.get("detail")' in body.replace("'", '"') or 'detail": got.get("detail")' in body, \
        "подробность отказа не доходит до окна"


@test
def test_publish_converter_folds_wrapped_items_and_keeps_nested_lists_valid(tmp: Path):
    """Найдено на живой публикации AC-4.3.15: жёсткий перенос продолжения пункта и вложенный список через пустую
    строку давали `Unexpected close tag </li>; expected </ol>` и Confluence отвечал 400. Переносы склеиваются, пустая
    строка перед вложенным списком не закрывает родительский, XHTML остаётся разбираемым."""
    import sys
    import xml.etree.ElementTree as ET
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    import publish_doc as PD
    md = ("1. Система логирует каждую отправку:\n\n"
          "   1. Признак документа.\n"
          "   2. Значение параметра.\n"
          "2. Система логирует изменение: прежнее значение, новое значение,\n"
          "   дату, время и инициатора изменения.\n\n"
          "3. Журнал хранится 5 лет.\n")
    html = PD.to_storage(md, {})
    ET.fromstring('<r xmlns:ac="http://example.com/content" xmlns:ri="http://example.com/resource/identifier">'
                  + html + "</r>")
    assert "Система логирует каждую отправку:<ol>" in html, html
    assert "прежнее значение, новое значение, дату, время" in html, "перенос пункта остался отдельным абзацем"


@test
def test_publish_card_has_no_draft_or_marker_chatter_and_auto_allows_them(tmp: Path):
    """Требование владельца (31.10): «зачем столько сообщений, что это черновик и draft» — публикуется открытый
    документ по правилам вида и промпта. «Черновик» и «нет границы чистовика» снимаются молча (`--allow`), в карточке
    остаются только те ворота, что правда останавливают отправку (остатки шаблона, персональные данные)."""
    src = (Path(__file__).resolve().parent.parent.parent / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    card = src.split("function drawPublishCard")[1].split("\n}\n", 1)[0]
    assert "const draft = false;" in card, "«черновик» снова просит отдельного клика"
    assert "hard.forEach(g => box.append" in card, "сообщения ворот печатаются не по списку останавливающих"
    assert "editor.publish_cut" not in card and "editor.publish_nomark" not in card, "лишние строки вернулись"
    assert 'code === "no-marker" || g.code === "draft"' in card, "молчаливое снятие ворот потеряно"
    import json
    kit = Path(__file__).resolve().parent.parent.parent
    for name in ("ru.json", "en.json"):
        cat = json.loads((kit / "cockpit" / "i18n" / name).read_text(encoding="utf-8"))
        assert "editor.publish_cut" not in cat and "editor.publish_nomark" not in cat, name


@test
def test_kind_form_has_no_root_path_field_and_the_place_is_chosen_in_publishing(tmp: Path):
    """Требование владельца (31.10): поле «Path under the root» в настройках вида убрать — как его заполнять, непонятно,
    и оно не отвечает реальному процессу. Место выбирается окном в карточке публикации и пишется в шапку; у формы
    вида не осталось поля пути, справки о подстановках пути и примера пути."""
    import json
    kit = Path(__file__).resolve().parent.parent.parent
    src = (kit / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    form = src.split("async function renderKinds(")[1].split("\n}\n", 1)[0]
    assert 'lab("publish_path"' not in form, "поле пути вернулось в форму вида"
    assert "kf-help" not in form and "kindFields(r)" not in form, "справка/пример пути вернулись в форму"
    assert '"data-field":"publish_title"' in form and "titlePresets" in form, "заголовок страницы из формы пропал"
    for name in ("ru.json", "en.json"):
        cat = json.loads((kit / "cockpit" / "i18n" / name).read_text(encoding="utf-8"))
        assert not [k for k in cat if k.startswith("kinds.path_help")], name
        assert "kinds.f_publish_path" not in cat and "kinds.publish_path_hint" not in cat, name
    # путь вида в конфиге остаётся совместимым, но точное место документа его вытесняет
    assert "EXACT_PLACE" in (kit / "scripts" / "publish_doc.py").read_text(encoding="utf-8")
    assert '"publish_parent", fm.get("publish_parent")' in (kit / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")


@test
def test_a_saved_place_is_exact_and_the_kind_path_no_longer_blocks_it(tmp: Path):
    """Найдено на живом проекте: у вида путь `{Activity}` требовал поля, которого нет ни у одного ac-документа, и публикация
    оставалась серой даже с выбранным местом. Место из шапки (`publish_parent`) — точное: путь вида не применяется,
    ворота «нет поля» не срабатывают, уровней нет, страница идёт прямо под выбранную папку."""
    restore = set_home(tmp / "home")
    kinds_text = ('artifacts:\n  ac:\n    title: "AC"\n    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n'
                  '    publish_url: "1"\n    publish_path: "{Activity}"\n    publish_title: "{title}"\n')
    root = W.project(tmp, "http://127.0.0.1:9", kinds_text)
    doc = root / "Artifacts" / "ac" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: ac\npublish_parent: \"2\"\n---\n\n# Итог\n", encoding="utf-8")
    srv, call = S._server(tmp)
    try:
        st, d = call("GET", "/api/files/read" + _q(root, path="Artifacts/ac/US-1.md"))
        pub = d.get("publish") or {}
        assert st == 200 and pub.get("state") == "ready", pub
        assert pub["parent_from"] == "publish_parent" and pub["parent"] == "2", pub
        assert pub["path"] == [] and not pub["fields"], pub
        assert pub["title"] == "Итог", pub
    finally:
        srv.shutdown()
        restore()
