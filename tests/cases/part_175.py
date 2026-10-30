"""Проверки движка Aurora, часть 175: левое меню панели сворачивается до значков.

Каркас и помощники — tests/harness.py. Кнопка-«бургер» в шапке сворачивает меню до колонки значков и
разворачивает обратно; выбор помнит браузер (localStorage, чтение и запись в try/catch), без выбора окно до
1280 px открывается со свёрнутым меню. У встроенных пунктов значки в разметке, у групп — свой фон
(`--nav-group-*-bg`). В свёрнутом виде подписи нет, есть подсказка с названием раздела, а отметки (число, «!»)
остаются меткой у значка. На узком экране (≤980 px) меню — прежняя лента. Страничные проверки идут под node
(нет node — пропускаются); проект, сеть и модели не используются.
"""
from __future__ import annotations

import json
import re
import shutil

from cases import part_51 as P51
from harness import KIT, test  # noqa: F401

UI = KIT / "cockpit" / "ui"
CYR = re.compile("[а-яА-ЯёЁ]")


def _panel() -> str:
    return (UI / "panel.js").read_text(encoding="utf-8")


def _css() -> str:
    return (UI / "panel.css").read_text(encoding="utf-8")


def _html() -> str:
    return (UI / "index.html").read_text(encoding="utf-8")


def _cat(lang: str) -> dict:
    return json.loads((KIT / "cockpit" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def _media(css: str, head: str) -> str:
    """Тело блока `@media …{…}` по его началу: до парной закрывающей скобки."""
    a = css.index(head) + len(head)
    depth, i = 1, a
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[a:i - 1]


@test
def test_the_menu_burger_names_itself_in_both_catalogs_and_points_at_the_menu(_t):
    """Кнопка `#navToggle` в шапке: `aria-controls` ведёт на `<nav id="mainNav">`, `aria-expanded` есть, подпись и
    подсказка — ключом каталога, а не голым текстом. «Свернуть меню» и «Развернуть меню» есть в ru и en, разные, и
    английская без кириллицы. Скрипт берёт подпись по состоянию из каталога и меняет ключ вместе с ним — смена
    языка (`applyI18n`) возьмёт верный. На узком экране кнопки нет."""
    html, panel, css = _html(), _panel(), _css()
    tag = re.search(r'<button[^>]*id="navToggle"[^>]*>', html, re.S)
    assert tag, "кнопки «бургер» нет в шапке"
    tag = tag.group(0)
    for attr in ('aria-controls="mainNav"', 'aria-expanded="true"', 'data-i18n-aria="nav.collapse"',
                 'data-i18n-title="nav.collapse"'):
        assert attr in tag, (attr, tag)
    assert html.index('id="navToggle"') < html.index("</header>"), "кнопка не в шапке"
    assert '<nav id="mainNav">' in html, "меню без id — aria-controls ведёт в никуда"
    ru, en = _cat("ru"), _cat("en")
    for cat in (ru, en):
        assert cat.get("nav.collapse") and cat.get("nav.expand"), "нет подписей кнопки"
        assert cat["nav.collapse"] != cat["nav.expand"]
    assert not CYR.search(en["nav.collapse"] + en["nav.expand"]), en
    assert 'b.setAttribute("aria-expanded", String(!on));' in panel
    assert 't(on ? "nav.expand" : "nav.collapse")' in panel, "подпись кнопки не из каталога"
    assert 'b.dataset.i18nAria = b.dataset.i18nTitle = on ? "nav.expand" : "nav.collapse";' in panel, \
        "ключ подписи не меняется с состоянием: смена языка вернёт «Свернуть» свёрнутому меню"
    narrow = _media(css, "@media (max-width:980px){")
    assert "#navToggle{display:none}" in narrow.replace("nav .ico,", ""), "на узком экране кнопка осталась"


NAV_HARNESS = r"""
let innerWidth = 1200, store = {}, failGet = false, failSet = false, narrow = false;
const localStorage = {
  getItem(k){ if (failGet) throw new Error("denied"); return k in store ? store[k] : null; },
  setItem(k, v){ if (failSet) throw new Error("denied"); store[k] = String(v); },
};
const cls = new Set(), appended = [], winListeners = {};
const document = {body: {classList: {toggle(c, on){ on ? cls.add(c) : cls.delete(c); }, contains: c => cls.has(c)},
                         append(n){ appended.push(n); }}};
const node = () => ({attrs: {}, dataset: {}, style: {}, hidden: false, listeners: {}, title: "",
  setAttribute(k, v){ this.attrs[k] = String(v); }, getAttribute(k){ return this.attrs[k]; },
  addEventListener(e, f){ this.listeners[e] = f; }});
const addEventListener = (e, f) => { winListeners[e] = f; };
const matchMedia = () => ({matches: narrow});
const t = k => "T:" + k;
const el = (tag, attrs) => {
  const n = Object.assign(node(), {tag, className: attrs.class, textContent: ""});
  for (const [k, v] of Object.entries(attrs)) if (k !== "class") n.attrs[k] = String(v);
  return n;
};
let toggle = null, nav = null;
const $ = s => s === "#navToggle" ? toggle : s === "#mainNav" ? nav : null;
function run(){
  toggle = node(); nav = node(); cls.clear();
@CODE@
  return {navMini, setNavMini};
}
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
let r;
innerWidth = 1200; store = {}; r = run();
check(r.navMini(), "окно 1200 px без выбора — меню свёрнуто");
check(toggle.attrs["aria-expanded"] === "false" && toggle.attrs["aria-label"] === "T:nav.expand"
      && toggle.title === "T:nav.expand", "свёрнутое меню: кнопка говорит «Развернуть»: " + JSON.stringify(toggle.attrs));
check(toggle.dataset.i18nAria === "nav.expand" && toggle.dataset.i18nTitle === "nav.expand", "ключ подписи не сменился");
check(!("aurora-nav-mini" in store), "умолчание по ширине записалось как выбор человека");
innerWidth = 1440; store = {}; r = run();
check(!r.navMini() && toggle.attrs["aria-expanded"] === "true" && toggle.attrs["aria-label"] === "T:nav.collapse",
      "окно 1440 px без выбора — меню развёрнуто, кнопка говорит «Свернуть»");
innerWidth = 1280; store = {}; r = run();
check(r.navMini(), "1280 px — ещё свёрнуто (граница включительно)");
innerWidth = 1000; store = {"aurora-nav-mini": "0"}; r = run();
check(!r.navMini(), "выбор «развёрнуто» главнее узкого окна");
innerWidth = 1600; store = {"aurora-nav-mini": "1"}; r = run();
check(r.navMini(), "выбор «свёрнуто» главнее широкого окна");
innerWidth = 1600; store = {}; r = run();
toggle.onclick();
check(r.navMini() && store["aurora-nav-mini"] === "1", "щелчок сворачивает и запоминает: " + JSON.stringify(store));
toggle.onclick();
check(!r.navMini() && store["aurora-nav-mini"] === "0", "второй щелчок разворачивает и запоминает");
failGet = true; failSet = true; innerWidth = 1100; store = {};
try {
  r = run();
  check(r.navMini(), "закрытое хранилище: умолчание по ширине");
  toggle.onclick();
  check(!r.navMini(), "закрытое хранилище: кнопка всё равно переключает");
} catch (e){ fails.push("закрытое хранилище роняет меню: " + e.message); }
failGet = failSet = false;
// подсказка
innerWidth = 1100; store = {}; appended.length = 0; r = run();
const label = {textContent: "  Git  "};
const btn = {querySelector: s => s === ".label" ? label : null,
             getBoundingClientRect: () => ({right: 54, top: 100, height: 40})};
nav.listeners.mouseover({target: {closest: () => btn}});
const tip = appended[0];
check(tip && tip.textContent === "Git" && tip.hidden === false && tip.style.left === "64px" && tip.style.top === "120px",
      "наведение: название раздела справа от значка: " + JSON.stringify(tip && {t: tip.textContent, h: tip.hidden, s: tip.style}));
check(tip && tip.attrs["aria-hidden"] === "true", "подсказка повторяет aria-label кнопки — диктору она не нужна");
nav.listeners.mouseleave();
check(tip.hidden === true, "мышь ушла с меню — подсказка спрятана");
nav.listeners.focusin({target: {closest: () => btn}});
check(tip.hidden === false, "фокус с клавиатуры тоже показывает подсказку");
nav.listeners.focusout();
check(tip.hidden === true, "фокус ушёл — подсказка спрятана");
nav.listeners.mouseover({target: {closest: () => btn}});
winListeners.resize();
check(tip.hidden === true, "смена размера окна прячет подсказку");
narrow = true;
nav.listeners.mouseover({target: {closest: () => btn}});
check(tip.hidden === true, "на узком экране (лента) подсказки нет");
narrow = false;
nav.listeners.mouseover({target: {closest: () => null}});
check(tip.hidden === true, "не пункт меню (заголовок группы) — подсказки нет");
nav.listeners.mouseover({target: {closest: () => btn}});
toggle.onclick();
check(tip.hidden === true && !r.navMini(), "развернули меню — подсказка спрятана");
nav.listeners.mouseover({target: {closest: () => btn}});
check(tip.hidden === true, "в развёрнутом меню подпись видна и так — подсказки нет");
console.log(JSON.stringify({fails}));
"""


@test
def test_the_menu_state_lives_in_this_browser_and_defaults_by_window_width(tmp):
    """Свёрнуто или развёрнуто — удобство одного зрителя: localStorage, чтение и запись в try/catch. Без выбора окно
    до 1280 px включительно открывается со свёрнутым меню, шире — с развёрнутым; выбор главнее ширины; закрытое
    хранилище не ломает ни старт, ни кнопку. Подсказка свёрнутого меню — название пункта справа от значка, сразу при
    наведении и при фокусе с клавиатуры; уходит с мышью, фокусом, прокруткой и сменой размера окна, на ленте и в
    развёрнутом меню её нет."""
    panel = _panel()
    a = panel.index('const NAV_MINI_KEY = "aurora-nav-mini";')
    code = panel[a:panel.index("/* ---------------- обращение на GitHub")]
    assert "try { return localStorage.getItem(NAV_MINI_KEY); } catch (e){ return null; }" in code
    assert "try { localStorage.setItem(NAV_MINI_KEY" in code, "запись выбора без try/catch"
    assert "const NAV_MINI_WIDTH = 1280;" in code
    if not shutil.which("node"):
        return
    res = P51._run_node(tmp, "nav_mini.mjs", NAV_HARNESS.replace("@CODE@", code))
    assert res["fails"] == [], "\n".join(res["fails"])


BADGE_HARNESS = r"""
let txt = "";                     // как в DOM: textContent — всегда строка
const n = {get textContent(){ return txt; }, set textContent(v){ txt = String(v); },
           cls: new Set(), classList: {toggle(c, on){ on ? n.cls.add(c) : n.cls.delete(c); }}};
const $ = s => s === "#nav-x" ? n : null;
@CODE@
const fails = [];
const seen = () => [...n.cls].sort().join(" ");
const cases = [[150, false, "over"], [99, false, ""], [100, false, "over"], ["!", true, "bad flag"], ["↑", false, "flag"],
               ["", false, ""], [7, true, "bad"], [0, false, ""], ["1000", false, "over"]];
for (const [text, bad, want] of cases){
  setBadge("x", text, bad);
  if (seen() !== want) fails.push(JSON.stringify([text, bad]) + " → «" + seen() + "», нужно «" + want + "»");
}
setBadge("x", 150);
if (n.textContent !== "150") fails.push("в развёрнутом меню число остаётся полным: " + n.textContent);
console.log(JSON.stringify({fails}));
"""


@test
def test_the_collapsed_menu_keeps_marks_as_small_badges(tmp):
    """Отметки в свёрнутом меню не прячутся: правило свёрнутого вида делает `.count` маленькой меткой у угла значка
    (позиция, кегль меньше обычного), число больше 99 — «99+», знак («!», «↑») — точкой, красная остаётся красной;
    пустая отметка скрыта, как прежде. Классы ставит `setBadge`: `over` — число больше 99, `flag` — не число; текст
    отметки не меняется. Счётчик «Файлов» идёт через тот же `setBadge`."""
    css, panel = _css(), _panel()
    mini = _media(css, "@media (min-width:981px){")
    rule = re.search(r"body\.nav-mini nav \.count\{([^}]*)\}", mini)
    assert rule and "position:absolute" in rule.group(1) and "font-size:8.5px" in rule.group(1), \
        "в свёрнутом меню отметка не стала меткой у значка"
    assert not re.search(r"\.count[^{]*\{[^}]*display:none", mini), "свёрнутое меню прячет отметки"
    assert 'body.nav-mini nav .count.over::after{content:"99+"' in mini, "больше 99 не сжимается до «99+»"
    assert re.search(r"body\.nav-mini nav \.count\.flag\{[^}]*width:10px", mini), "знак не стал точкой"
    assert "body.nav-mini nav .count.bad{background:var(--danger)" in mini, "красная отметка не красная"
    assert "nav .count:empty{display:none}" in css, "пустая отметка снова видна"
    assert 'setBadge("files", d.total || F.files.length || "");' in panel, "счётчик «Файлов» мимо setBadge"
    if not shutil.which("node"):
        return
    a = panel.index("function setBadge(view, text, bad){")
    code = panel[a:panel.index("\n}\n", a) + 3]
    res = P51._run_node(tmp, "nav_badge.mjs", BADGE_HARNESS.replace("@CODE@", code))
    assert res["fails"] == [], "\n".join(res["fails"])


@test
def test_built_in_menu_items_carry_icons_and_groups_have_their_own_background(_t):
    """У каждой встроенной кнопки меню (Мостик, Настройка кита, Настройки проекта, Файлы, Консоль) — строчный
    значок 24×24 без своих цветов, значки разные. Группа — блок с заголовком внутри: у «Машины», «Проекта» и
    «Движка» свой фон из переменных `--nav-group-*-bg` (умолчание — `color-mix` от поверхности и цвета группы),
    пустая группа не оставляет блока. Короткая подпись группы для свёрнутого меню — в каталогах обоих языков.
    Свёрнутый вид — класс на `<body>` (без перерисовки разметки), только на широком экране; на узком лента
    прежняя: группы прозрачны (`display:contents`), значков нет."""
    html, css = _html(), _css()
    nav = html[html.index('<nav id="mainNav">'):html.index("</nav>")]
    buttons = re.findall(r'<button data-view="([a-z]+)"(.*?)</button>', nav, re.S)
    assert {v for v, _ in buttons} == {"overview", "setup", "project", "files", "console"}, buttons
    shapes = set()
    for view, body in buttons:
        m = re.search(r'<span class="ico" aria-hidden="true"><svg viewBox="0 0 24 24">(.*?)</svg></span>', body, re.S)
        assert m, f"у кнопки «{view}» нет значка"
        assert not re.search(r"(fill|stroke)=", m.group(1)), f"значок «{view}» красит себя сам, а не currentColor"
        shapes.add(m.group(1))
        assert body.index('class="ico"') < body.index('class="label"'), f"значок «{view}» не перед подписью"
    assert len(shapes) == len(buttons), "у встроенных разделов одинаковые значки"
    ru, en = _cat("ru"), _cat("en")
    for group in ("machine", "project", "engine"):
        block = re.search(r'<div class="navgroup" data-group="%s">\s*<div class="group"[^>]*>(.*?)</div>' % group,
                          nav, re.S)
        assert block, f"у группы «{group}» заголовок не внутри блока"
        for key in (f"nav.group_{group}", f"nav.group_{group}_short"):
            assert f'data-i18n="{key}"' in block.group(1), (group, key)
            assert ru.get(key) and en.get(key), (group, key)
        var = f"--nav-group-{group}-bg"
        assert re.search(re.escape(var) + r":color-mix\(in oklab,var\(--\w+\) \d+%,var\(--surface-1\)\);", css), var
    assert '.navgroup[data-group="project"]{background:var(--nav-group-project-bg)}' in css
    assert '.navgroup[data-group="engine"]{background:var(--nav-group-engine-bg)}' in css
    assert "background:var(--nav-group-machine-bg)" in css
    assert ".navgroup:not(:has(button:not([hidden]))){display:none}" in css, "пустая группа оставляет блок"
    mini = _media(css, "@media (min-width:981px){")
    for rule in ("body.nav-mini #app{grid-template-columns:var(--nav-mini-w) 1fr}",
                 "body.nav-mini nav .label{display:none}", "body.nav-mini nav .ico{width:22px;height:22px",
                 "body.nav-mini nav .group .short{display:block"):
        assert rule in mini, rule
    assert re.search(r"\nnav \.ico\{[^}]*width:15px;height:15px", css), "в развёрнутом меню значок не мелкий"
    narrow = _media(css, "@media (max-width:980px){")
    assert ".navgroup{display:contents}" in narrow and "nav .ico,#navToggle{display:none}" in narrow, \
        "лента на узком экране изменилась"
    panel = _panel()
    assert 'document.body.classList.toggle("nav-mini", on);' in panel


@test
def test_the_contrast_skin_keeps_menu_groups_distinct(_t):
    """«Контраст» переопределяет фон блоков групп заметнее умолчания и рамку блока — полной линией; смесь с фоном
    страницы, поэтому одна запись годится светлой и тёмной теме. Остальные скины берут умолчание панели."""
    skin = (KIT / "cockpit" / "skins" / "contrast.css").read_text(encoding="utf-8")
    for group in ("machine", "project", "engine"):
        assert re.search(r"--nav-group-%s-bg:color-mix\(in oklab,var\(--\w+\) 1\d%%,var\(--bg\)\);" % group, skin), group
    assert "--nav-group-border:var(--border);" in skin
