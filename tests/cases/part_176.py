"""Проверки движка Aurora, часть 176: значки разделов-папок в меню панели.

Каркас и помощники — tests/harness.py. Значок раздела — `icon.svg` в его папке (`cockpit/modules/<id>/`): строчный
24×24 цветом `currentColor`, без скриптов, обработчиков и ссылок наружу. Файл необязателен: нет его — в меню первая
буква названия в скруглённом квадрате. `/api/modules` говорит, есть ли значок (`icon`), сервер отдаёт файл тем же
путём, что разметку и скрипт раздела, и ничего вне папки раздела. Ядро при этом разделы по имени не узнаёт.
Страничные проверки — под node (нет node — пропускаются); модели и сеть не используются, сервер панели — на
временном порту петлевого адреса.
"""
from __future__ import annotations

import http.client
import importlib
import json
import re
import shutil
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

from cases import part_51 as P51
from harness import KIT, SCRIPTS, test  # noqa: F401

MODULES = KIT / "cockpit" / "modules"
SVG_NS = "{http://www.w3.org/2000/svg}"
FORBIDDEN_TAGS = {"script", "foreignObject", "image", "use", "style", "a", "iframe"}

_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)


def _ck():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _module_ids() -> list:
    return sorted(p.parent.name for p in MODULES.glob("*/module.json"))


def _svg_problems(text: str, where: str) -> list:
    """Что не так со значком: не svg, не 24×24, свои цвета, скрипт, обработчик или ссылка наружу."""
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return [f"{where}: не разбирается как XML ({e})"]
    if root.tag != SVG_NS + "svg":
        out.append(f"{where}: корень не svg ({root.tag})")
    if root.get("viewBox") != "0 0 24 24":
        out.append(f"{where}: viewBox {root.get('viewBox')!r}, нужен «0 0 24 24»")
    if root.get("stroke") != "currentColor":
        out.append(f"{where}: линия не цветом currentColor — значок не возьмёт цвет пункта")
    for n in root.iter():
        tag = n.tag.replace(SVG_NS, "")
        if tag in FORBIDDEN_TAGS:
            out.append(f"{where}: запрещённый элемент <{tag}>")
        for k, v in n.attrib.items():
            name = k.split("}")[-1]
            if name.lower().startswith("on"):
                out.append(f"{where}: обработчик {name}")
            if name.lower().endswith("href"):
                out.append(f"{where}: ссылка {name}={v}")
            if name in ("fill", "stroke") and v not in ("none", "currentColor"):
                out.append(f"{where}: свой цвет {name}={v}")
            if "url(" in v or "http" in v:
                out.append(f"{where}: ссылка наружу в {name}")
    if "<script" in text.lower() or "javascript:" in text.lower():
        out.append(f"{where}: скрипт в тексте")
    return out


@test
def test_every_panel_section_has_its_own_icon(_t):
    """У каждого раздела-папки кита есть `icon.svg`: разбирается как XML, корень svg 24×24, линия `currentColor`,
    своих цветов нет, нет скриптов, `foreignObject`, картинок, обработчиков и ссылок наружу; файл маленький, и
    значки разных разделов не повторяют друг друга."""
    ids = _module_ids()
    assert len(ids) >= 19, ids
    problems, shapes = [], {}
    for mid in ids:
        path = MODULES / mid / "icon.svg"
        if not path.is_file():
            problems.append(f"{mid}: нет icon.svg")
            continue
        text = path.read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) < 2048, (mid, len(text))
        problems += _svg_problems(text, mid)
        body = re.sub(r"\s+", "", text[text.index(">") + 1:])
        shapes.setdefault(body, []).append(mid)
    assert not problems, "\n".join(problems)
    twins = [v for v in shapes.values() if len(v) > 1]
    assert not twins, f"одинаковые значки: {twins}"


@test
def test_icon_check_catches_unsafe_or_foreign_svg(_t):
    """Проверка значка сама не пропускает плохое: скрипт, обработчик, внешнюю ссылку, свой цвет, чужой размер."""
    head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor">'
    good = head + '<path d="M3 12h18"/></svg>'
    assert _svg_problems(good, "good") == []
    bad = {
        "script": head + "<script>alert(1)</script></svg>",
        "onload": head.replace("<svg ", '<svg onload="x()" ') + "</svg>",
        "href": head + '<use href="https://example.org/x.svg#a"/></svg>',
        "color": head + '<path stroke="#ff0000" d="M3 12h18"/></svg>',
        "size": head.replace("0 0 24 24", "0 0 48 48") + "</svg>",
        "notxml": head + "<path d='M3 12h18'></svg>",
    }
    for name, text in bad.items():
        assert _svg_problems(text, name), f"проверка пропустила «{name}»"


@test
def test_api_modules_says_which_sections_have_an_icon(tmp):
    """`/api/modules` несёт `icon`: у разделов кита — true; у раздела-папки без `icon.svg` — false (панель нарисует
    букву), и раздел от этого не ломается и предупреждений не получает."""
    ck = _ck()
    real = {m["id"]: m for m in ck.modules()}
    assert all(real[mid].get("icon") is True for mid in _module_ids()), \
        [mid for mid in _module_ids() if real[mid].get("icon") is not True]
    root = tmp / "modules"
    for mid, icon in (("plain", False), ("drawn", True)):
        (root / mid).mkdir(parents=True)
        (root / mid / "module.json").write_text(json.dumps({"id": mid, "name": {"ru": "Р", "en": "S"}}),
                                                encoding="utf-8")
        if icon:
            (root / mid / "icon.svg").write_text((MODULES / "about" / "icon.svg").read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    keep = ck.MODULES_DIR
    ck.MODULES_DIR = str(root)
    try:
        by = {m["id"]: m for m in ck.modules()}
    finally:
        ck.MODULES_DIR = keep
    assert by["plain"]["icon"] is False and "warn" not in by["plain"] and "error" not in by["plain"], by["plain"]
    assert by["drawn"]["icon"] is True, by["drawn"]


@test
def test_the_server_serves_a_section_icon_and_nothing_outside_the_section(tmp):
    """Значок отдаётся так же, как разметка раздела: без токена (браузер тянет его сам), с типом `image/svg+xml` и
    без кэша. Путь наружу из папки раздела — `..`, закодированные точки, чужой раздел через `..`, файл ядра —
    даёт 404; файла нет — 404; чужое расширение в папке раздела не отдаётся."""
    ck = _ck()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
        c.request("GET", path)                       # без токена: так грузит браузер
        r = c.getresponse()
        return r.status, r.getheader("Content-Type") or "", r.getheader("Cache-Control") or "", r.read()
    try:
        st, ctype, cache, body = get("/modules/about/icon.svg")
        assert st == 200 and ctype.startswith("image/svg+xml") and cache == "no-store", (st, ctype, cache)
        assert body == (MODULES / "about" / "icon.svg").read_bytes()
        for path in ("/modules/about/../../ui/panel.js", "/modules/about/%2e%2e/%2e%2e/ui/panel.js",
                     "/modules/about/../ask/icon.svg/../../../aurora_cockpit.py", "/modules/../ui/index.html",
                     "/modules/about/../../aurora_cockpit.py", "/modules/about/nope.svg",
                     "/modules/nope/icon.svg", "/modules/about/view.py"):
            st, _c, _k, body = get(path)
            assert st == 404, (path, st, body[:80])
    finally:
        srv.shutdown()
        srv.server_close()
    assert ck.module_file("about", "icon.svg")
    for rel in ("../ask/icon.svg", "../../ui/panel.js", "/etc/hosts", "..", ""):
        assert ck.module_file("about", rel) == "", rel


NAV_HARNESS = r"""
const fetched = [];
let reply = {ok: false};
const fetch = async url => { fetched.push(url); return reply; };
const el = (tag, attrs, ...kids) => {
  kids = kids.filter(k => k !== null && k !== undefined);
  return {tag, attrs, kids, dataset: {}, className: attrs.class, textContent: kids.filter(k => typeof k === "string").join("")};
};
const added = [];
const group = {insertBefore: b => added.push(b)};
const $ = sel => sel.startsWith(".navgroup") ? group : null;
const $$ = () => [];
const S = {lang: "ru"};
const MODULES = new Map();
const devSectionsOn = () => false, navClick = () => {};
@NAV@
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
(async () => {
  addModuleNav({id: "plain", name: {ru: "  пример раздела", en: "Sample"}, group: "project", order: 5});
  const plain = added[0], ico = plain.kids[0];
  check(ico.className === "ico letter" && ico.textContent === "П" && ico.attrs["aria-hidden"] === "true",
        "раздел без значка — буква названия в квадрате: " + JSON.stringify(ico));
  check(plain.kids[1].attrs.class === "label" && plain.attrs["aria-label"] === "  пример раздела",
        "подпись и aria-label кнопки на месте");
  check(fetched.length === 0, "за значком раздела без icon.svg ходить незачем: " + fetched.join());
  addModuleNav({id: "drawn", name: {ru: "Рисунок"}, group: "project", order: 6, icon: true});
  const drawn = added[1], dico = drawn.kids[0];
  check(dico.className === "ico" && dico.textContent === "", "пока значок идёт, буква не мелькает: " + JSON.stringify(dico));
  check(fetched.join() === "/modules/drawn/icon.svg", "значок — файл раздела: " + fetched.join());
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  check(dico.className === "ico letter" && dico.textContent === "Р", "значок не пришёл — буква: " + JSON.stringify(dico));
  S.lang = "en";
  addModuleNav({id: "later", name: {ru: "Позже", en: "later one"}, group: "machine", order: 1});
  check(added[2].kids[0].textContent === "L", "буква — на языке панели: " + added[2].kids[0].textContent);
  console.log(JSON.stringify({fails}));
})().catch(e => console.log(JSON.stringify({fails: ["исключение: " + (e && e.stack || e)]})));
"""


@test
def test_a_section_without_an_icon_gets_a_letter_in_the_menu(tmp):
    """`addModuleNav` (под node): раздел без `icon` — сразу буква названия на языке панели в скруглённом квадрате;
    раздел с `icon` — пустое место, пока идёт `/modules/<id>/icon.svg` (буква не мелькает на каждой загрузке), а не
    пришёл файл — буква. Подпись, `aria-label` и порядок кнопки прежние. Смена языка обновляет и букву, и
    `aria-label` раздела (`relabelModules`); вставляемый svg чистится от скриптов, обработчиков и ссылок."""
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    nav = panel[panel.index("function moduleTitle(m){"):panel.index("// Всё, чем ядро делится")]
    relabel = panel[panel.index("function relabelModules(){"):]
    relabel = relabel[:relabel.index("\n}\n")]
    assert 'b.setAttribute("aria-label", moduleTitle(rec.manifest));' in relabel, "aria-label не на языке панели"
    assert "letter.textContent = navLetter(rec.manifest);" in relabel, "буква не на языке панели"
    assert "/^(script|foreignObject|image|use|style)$/i" in nav and "/^on|href$/i" in nav, "svg вставляется без чистки"
    if not shutil.which("node"):
        return
    res = P51._run_node(tmp, "nav_icons.mjs", NAV_HARNESS.replace("@NAV@", nav))
    assert res["fails"] == [], "\n".join(res["fails"])


@test
def test_menu_icons_do_not_teach_the_core_section_names(_t):
    """Значки не сделали ядро знающим разделы по имени: ни разметка, ни стили панели не упоминают разделы-папки
    (`modules/<id>`, `/api/<id>`, `data-view="<id>"`), код меню в panel.js — тоже; храповик метрики не вырос."""
    import ext_footprint as EF
    ui = KIT / "cockpit" / "ui"
    panel = (ui / "panel.js").read_text(encoding="utf-8")
    menu = panel[panel.index("/* ---------------- меню: свернуть до значков"):
                 panel.index("/* ---------------- обращение на GitHub")]
    menu += panel[panel.index("function moduleTitle(m){"):panel.index("// Всё, чем ядро делится")]
    texts = {"index.html": (ui / "index.html").read_text(encoding="utf-8"),
             "panel.css": (ui / "panel.css").read_text(encoding="utf-8"), "menu of panel.js": menu}
    for mid in _module_ids():
        for name, text in texts.items():
            for pat in (r"modules[/\\]+" + re.escape(mid) + r"\b", r"/api/" + re.escape(mid) + r"\b",
                        r'data-view="' + re.escape(mid) + '"', r'["\']' + re.escape(mid) + r'["\']'):
                assert not re.search(pat, text), f"{name} называет раздел «{mid}» ({pat})"
    fp = EF.footprint()
    assert not {"cockpit/ui/index.html", "cockpit/ui/panel.css"} & set(fp["panel_module"]), fp["panel_module"]
    problems, _hints = EF.ratchet_verdict(EF.counts(), EF.load_baseline())
    assert not problems, problems
