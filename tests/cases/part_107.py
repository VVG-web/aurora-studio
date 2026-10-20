"""Проверки движка Aurora, часть 107. Каркас и помощники — tests/harness.py.

Настройки извлечения из вложений в «Настройках проекта»: раздел `ingest:` конфига пишется только через
панель (`/api/ingest/settings`), с защитой от потерянного обновления (слепок `expect`, ответ 409), атомарно, не
затирая соседние разделы, а основная форма проекта, в свою очередь, не затирает раздел `ingest:`. Группа в панели не
пересобирает страницу поверх несохранённых правок соседних групп. Нейтральные данные; сети и модели нет.
"""
from __future__ import annotations

import http.client
import importlib
import json
import re
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

CYR = re.compile("[а-яА-ЯёЁ]")
DEFAULTS = {"attachments": True, "diagrams": True, "images": False, "max_files": 200}


def _ck():
    sys.path.insert(0, str(KIT / "cockpit"))
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("aurora_cockpit")


def _server(tmp: Path):
    ck = _ck()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
        h = {"X-Aurora-Token": ck.TOKEN}
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8") or "{}")
    return srv, call


@test
def test_ingest_settings_api_state_save_conflict_and_validation(tmp: Path):
    restore = set_home(tmp / "home")
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    srv, call = _server(tmp)
    try:
        q = "?project=" + str(root)
        st, d = call("GET", "/api/ingest/settings" + q)
        assert st == 200 and d["settings"] == DEFAULTS and d["defaults"] == DEFAULTS and d["explicit"] is False, d
        assert d["ceiling"] == 100_000 and d["problem"] == "", d
        before = cfg.read_text(encoding="utf-8")
        new = {"attachments": False, "diagrams": True, "images": True, "max_files": 50}
        st, d2 = call("POST", "/api/ingest/settings/save", {"project": str(root), "expect": d["digest"], "settings": new})
        assert st == 200 and d2["ok"] and d2["settings"] == new and d2["state"]["settings"] == new, f"{st} {d2}"
        assert d2["state"]["explicit"] is True and d2["digest"] != d["digest"]
        after = cfg.read_text(encoding="utf-8")
        assert after.startswith(before.rstrip("\n")), "форма стёрла или переписала соседние разделы конфига"
        assert after.count("\ningest:") == 1 and "max_files: 50" in after, after
        # повторная запись тех же настроек файл не меняет
        bytes_now = cfg.read_bytes()
        st, d3 = call("POST", "/api/ingest/settings/save", {"project": str(root), "expect": d2["digest"], "settings": new})
        assert st == 200 and cfg.read_bytes() == bytes_now, "та же запись переписала файл"
        # устаревший слепок: раздел успели изменить руками
        cfg.write_text(cfg.read_text(encoding="utf-8").replace("max_files: 50", "max_files: 60"), encoding="utf-8")
        st, d4 = call("POST", "/api/ingest/settings/save", {"project": str(root), "expect": d2["digest"],
                                                            "settings": DEFAULTS})
        assert st == 409 and d4["conflict"] is True and d4["code"] == "conflict", f"{st} {d4}"
        assert "max_files: 60" in cfg.read_text(encoding="utf-8"), "при конфликте запись всё же прошла"
        # негодные ответы формы: 400 и код, файл не тронут
        keep = cfg.read_bytes()
        cases = [({"max_files": 0}, "max_files"), ({"max_files": 100_001}, "max_files"), ({"max_files": ""}, "max_files"),
                 ({"max_files": True}, "max_files"), ({"max_files": "много"}, "max_files"),
                 ({"images": "может быть"}, "images"), ({"attachments": 3.5}, "attachments"),
                 ({"diagrams": None}, "diagrams"), ({"новый_ключ": 1}, "unknown")]
        for settings, code in cases:
            st, bad = call("POST", "/api/ingest/settings/save", {"project": str(root), "settings": settings})
            assert st == 400 and bad["code"] == code and bad["ok"] is False, (settings, st, bad)
        st, bad = call("POST", "/api/ingest/settings/save", {"project": str(root), "settings": [1, 2]})
        assert st == 400 and bad["code"] == "unknown", (st, bad)
        assert cfg.read_bytes() == keep, "негодный ответ изменил файл"
        # строки «да/нет», как их шлёт форма, принимаются; пустой слепок — запись без проверки
        st, ok = call("POST", "/api/ingest/settings/save", {"project": str(root), "expect": "", "settings":
                                                           {"images": "false", "max_files": "75"}})
        assert st == 200 and ok["settings"]["max_files"] == 75 and ok["settings"]["images"] is False, (st, ok)
        # неизвестный проект
        st, nope = call("POST", "/api/ingest/settings/save", {"project": str(tmp / "нет"), "settings": DEFAULTS})
        assert st in (400, 403, 404), (st, nope)
        # конфиг не в UTF-8 не трогается
        cfg.write_bytes('project:\n  name: "Тест"\n'.encode("cp1251"))
        raw = cfg.read_bytes()
        st, bad = call("POST", "/api/ingest/settings/save", {"project": str(root), "settings": DEFAULTS})
        assert st == 400 and bad["code"] == "not_utf8" and cfg.read_bytes() == raw, (st, bad)
        st, d5 = call("GET", "/api/ingest/settings" + q)
        assert d5["problem"] == "not_utf8" and d5["settings"] == DEFAULTS
        assert not [p.name for p in root.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")], "остался временный файл"
        # конфига нет вовсе: сервер такой проект не знает, а сама запись отказывает кодом
        cfg.unlink()
        st, bad = call("POST", "/api/ingest/settings/save", {"project": str(root), "settings": DEFAULTS})
        assert st in (400, 403, 404) and bad.get("ok") is not True and not cfg.exists(), (st, bad)
        IC = importlib.import_module("ingest_config")
        res = IC.save(str(root), DEFAULTS)
        assert res["ok"] is False and res["code"] == "no_config" and not cfg.exists(), res
        assert IC.state(str(root))["problem"] == "no_config"
    finally:
        srv.shutdown()
        srv.server_close()
        restore()


@test
def test_two_saves_at_once_never_leave_a_half_written_config(tmp: Path):
    IC = importlib.import_module("ingest_config") if str(SCRIPTS) in sys.path else (
        sys.path.insert(0, str(SCRIPTS)) or importlib.import_module("ingest_config"))
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    results = []

    def worker(n):
        for i in range(20):
            results.append(IC.save(str(root), {"max_files": 10 * n + i + 1, "images": bool((n + i) % 2)}))
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]][:2]
    text = cfg.read_text(encoding="utf-8")
    assert text.startswith(base.rstrip("\n")) and text.count("\ningest:") == 1, text
    parsed = IC.parse(text)
    assert 1 <= parsed["max_files"] <= 100_000 and isinstance(parsed["images"], bool), parsed
    assert not [p for p in root.iterdir() if p.name.endswith(".tmp")]


@test
def test_the_project_form_keeps_the_ingest_block_and_the_ingest_form_keeps_everything_else(tmp: Path):
    """Основная форма проекта переносит неизвестные ей разделы (в том числе `ingest:`); форма извлечения не трогает чужие."""
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_setup")
    IC = importlib.import_module("ingest_config")
    import contextlib
    import io
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "\nreports:\n  weekly:\n    output: Artifacts/reports\n\ncards:\n  concept:\n    title: Понятие\n",
                   encoding="utf-8")
    assert IC.save(str(root), {"attachments": False, "images": True, "max_files": 33})["ok"]
    block = IC.render({"attachments": False, "diagrams": True, "images": True, "max_files": 33})
    with contextlib.redirect_stdout(io.StringIO()):
        A.run_answers(root, {"name": "Новое имя"})
    text = cfg.read_text(encoding="utf-8")
    assert text.count("\ningest:") == 1 and block in text, "основная форма стёрла или переписала раздел ingest:"
    assert IC.parse(text) == {"attachments": False, "diagrams": True, "images": True, "max_files": 33}
    assert "Новое имя" in text and "reports:" in text and "title: Понятие" in text
    before = text
    assert IC.save(str(root), {"max_files": 34})["ok"]
    after = cfg.read_text(encoding="utf-8")
    assert after.replace("max_files: 34", "max_files: 33") == before.replace("true     # текст", "true     # текст") or \
        IC.parse(after)["max_files"] == 34
    assert "Новое имя" in after and "reports:" in after and "title: Понятие" in after
    assert re.sub(r"(?s)\ningest:.*?(?=\n\S|\Z)", "", after) == re.sub(r"(?s)\ningest:.*?(?=\n\S|\Z)", "", before), \
        "запись раздела ingest: изменила другие разделы"


# --------------------------------------------------------------------------- панель

def _fn() -> tuple:
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    start = panel.index("async function renderIngest(box){")
    fn = panel[start:panel.index("\n}\n", start) + 2]
    return panel, fn


@test
def test_ingest_group_keeps_unsaved_edits_sends_the_digest_and_maps_error_codes(tmp: Path):
    panel, fn = _fn()
    project = panel[panel.index("async function renderProject(){"):]
    project = project[:project.index("\n}\n") + 2]
    order = [project.index(x) for x in ("await renderSourceAccess(box)", "await renderIngest(box)", "await renderCardTypes(box)",
                                        "drawSetupJump(box);")]
    assert order == sorted(order) and len(set(order)) == 4, "группа извлечения стоит не между доступом и типами карточек"
    assert 'sgroup(box, "project:ingest"' in fn and 'saveButton("ingest"' in fn
    assert "renderProject()" not in fn, "группа сбрасывает несохранённые правки соседних групп"
    assert set(re.findall(r'"(/api/[\w/]+)', fn)) == {"/api/ingest/settings", "/api/ingest/settings/save"}
    assert "expect: digest" in fn and "r.conflict" in fn
    node = shutil.which("node")
    if not node:
        return

    def cut(start: str, end: str) -> str:
        a = panel.index(start)
        return panel[a:panel.index(end, a) + len(end)]
    harness = r"""
class FakeNode {
  constructor(tag){ this.tag = tag; this.attrs = {}; this.kids = []; this.ls = {}; this.className = ""; this.nodeType = 1;
    this._v = undefined; this.checked = false; this.style = {}; }
  setAttribute(k, v){ this.attrs[k] = v; if (k === "value") this._v = String(v); if (k === "checked") this.checked = true; }
  addEventListener(ev, f){ (this.ls[ev] = this.ls[ev] || []).push(f); }
  append(...k){ for (const x of k) this.kids.push(x && x.nodeType ? x : {nodeType: 3, text: String(x)}); }
  set innerHTML(v){ this.kids = []; }
  get textContent(){ return this.kids.map(k => k.text !== undefined ? k.text : k.textContent).join(""); }
  get value(){ return this._v !== undefined ? this._v : ""; }
  set value(v){ this._v = String(v); }
}
const document = {createElement: tag => new FakeNode(tag), createTextNode: s => ({nodeType: 3, text: s})};
@EL@
const DIRTY = new Map();
const setDirty = (key, label, on) => { if (on) DIRTY.set(key, label); else DIRTY.delete(key); };
const t = (k, v) => v ? k + JSON.stringify(v) : k;
const S = {project: {path: "/p", name: "P"}};
const toasts = [], calls = [];
const toast = (m, kind) => toasts.push([m, kind]);
let respond = {};
const api = async (url, opts) => { calls.push([url, opts && opts.body ? JSON.parse(opts.body) : null]); return respond[url.split("?")[0]]; };
const sgroup = (box, id, title) => { const g = new FakeNode("div"); g.attrs["data-id"] = id; box.append(g); return g; };
const saveButton = (key, label, text, handler) => { const b = new FakeNode("button"); b.attrs["data-save"] = key; b.handler = handler; return b; };
@FN@
const walk = function*(n){ yield n; for (const k of n.kids || []) if (k && k.tag) yield* walk(k); };
const all = (root, pred) => [...walk(root)].filter(pred);
const fire = (n, ev) => { for (const f of n.ls[ev] || []) f({target: n}); };
const fails = [];
const ok = (c, m) => { if (!c) fails.push(m); };
const state = (settings, digest) => ({settings, defaults: {attachments: true, diagrams: true, images: false, max_files: 200},
                                       digest, explicit: false, ceiling: 100000, problem: ""});
(async () => {
  respond["/api/ingest/settings"] = state({attachments: true, diagrams: false, images: false, max_files: 120}, "d1");
  const box = new FakeNode("div");
  await renderIngest(box);
  const group = box.kids[0];
  ok(group && group.attrs["data-id"] === "project:ingest", "группа не создана");
  const boxes = all(group, n => n.tag === "input" && n.attrs.type === "checkbox");
  ok(boxes.map(b => b.attrs["data-key"]).join() === "attachments,diagrams,images", "переключатели: " + boxes.map(b => b.attrs["data-key"]));
  ok(boxes.map(b => b.checked).join() === "true,false,false", "состояние переключателей из конфига: " + boxes.map(b => b.checked));
  const num = all(group, n => n.tag === "input" && n.attrs["data-key"] === "max_files")[0];
  ok(num && num.value === "120" && num.attrs.max === "100000" && num.attrs.min === "1", "поле потолка: " + (num && num.value));
  ok(!DIRTY.size, "после открытия есть несохранённое");
  // правка помечает группу несохранённой, возврат к прежнему — снимает пометку
  boxes[1].checked = true; fire(boxes[1], "change");
  ok(DIRTY.has("ingest"), "правка переключателя не пометила группу");
  boxes[1].checked = false; fire(boxes[1], "change");
  ok(!DIRTY.has("ingest"), "возврат к прежнему не снял пометку");
  boxes[2].checked = true; fire(boxes[2], "change");
  num.value = "5"; fire(num, "input");
  const save = all(group, n => n.attrs["data-save"] === "ingest")[0];
  ok(save, "кнопки сохранения нет");
  // конфликт: слепок устарел → пометка остаётся, правки не пропадают
  respond["/api/ingest/settings/save"] = {ok: false, conflict: true, code: "conflict"};
  await save.handler();
  const sent = calls.filter(c => c[0] === "/api/ingest/settings/save")[0][1];
  ok(sent.expect === "d1" && sent.project === "/p" && sent.settings.images === true && sent.settings.max_files === 5
     && sent.settings.diagrams === false, "в запись ушло не то: " + JSON.stringify(sent));
  ok(toasts.at(-1)[0] === "ingest.conflict" && toasts.at(-1)[1] === "err" && DIRTY.has("ingest"), "конфликт: " + JSON.stringify(toasts.at(-1)));
  // код ошибки → строка каталога
  respond["/api/ingest/settings/save"] = {ok: false, code: "max_files", error: "русский текст сервера"};
  await save.handler();
  ok(toasts.at(-1)[0] === 'ingest.err.max_files{"max":100000}', "код max_files: " + toasts.at(-1)[0]);
  respond["/api/ingest/settings/save"] = {ok: false, code: "images"};
  await save.handler();
  ok(toasts.at(-1)[0] === "ingest.err.flag", "код images: " + toasts.at(-1)[0]);
  respond["/api/ingest/settings/save"] = {ok: false, code: "что-то новое", error: "текст сервера"};
  await save.handler();
  ok(toasts.at(-1)[0] === "текст сервера", "неизвестный код: показывается текст сервера");
  respond["/api/ingest/settings/save"] = null;
  await save.handler();
  ok(toasts.at(-1)[0] === "ingest.save_failed", "пустой ответ: " + toasts.at(-1)[0]);
  // успех: слепок и настройки берутся из ответа, пометка снимается, страница не пересобирается
  respond["/api/ingest/settings/save"] = {ok: true, state: state({attachments: true, diagrams: false, images: true, max_files: 5}, "d2")};
  await save.handler();
  ok(toasts.at(-1)[0] === "ingest.saved" && toasts.at(-1)[1] === "ok" && !DIRTY.has("ingest"), "после записи: " + JSON.stringify(toasts.at(-1)));
  await save.handler();
  const next = calls.filter(c => c[0] === "/api/ingest/settings/save").at(-1)[1];
  ok(next.expect === "d2", "второй записи не ушёл новый слепок: " + next.expect);
  // сервер не ответил на чтение — группы нет, остальная страница не страдает
  respond["/api/ingest/settings"] = {error: "нет"};
  const empty = new FakeNode("div");
  await renderIngest(empty);
  ok(empty.kids.length === 0, "при ошибке чтения группа всё же нарисована");
  console.log(JSON.stringify({fails}));
})();
"""
    code = harness.replace("@EL@", cut("const el = (tag", "\n};\n")).replace("@FN@", fn)
    src = tmp / "ingest_panel.mjs"
    src.write_text(code, encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


@test
def test_ingest_catalogues_are_complete_and_honest(tmp: Path):
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    _panel, fn = _fn()
    asked = set(re.findall(r'\bt\("(ingest\.[a-z0-9_.]+)"', fn)) | {"dirty.ingest"}
    mine = sorted(k for k in ru if k.startswith("ingest.") or k == "dirty.ingest")
    assert sorted(k for k in en if k.startswith("ingest.") or k == "dirty.ingest") == mine
    assert asked <= set(ru), f"страница спрашивает ключи, которых нет: {sorted(asked - set(ru))}"
    assert set(mine) - asked == set(), f"в каталоге ключи, которых страница не спрашивает: {sorted(set(mine) - asked)}"
    for k in mine:
        assert CYR.search(ru[k]) and not CYR.search(en[k]), f"{k}: русский без кириллицы или английский с ней"
        assert re.findall(r"\{(\w+)\}", ru[k]) == re.findall(r"\{(\w+)\}", en[k]), f"{k}: подстановки разошлись"
    # коды ошибок, которые отдаёт сервер, названы страницей; подсказки не обещают того, чего код не делает
    IC = importlib.import_module("ingest_config") if str(SCRIPTS) in sys.path else (
        sys.path.insert(0, str(SCRIPTS)) or importlib.import_module("ingest_config"))
    for code in IC.ERRORS:
        if code != "conflict":
            assert f'{code}:' in fn or f'{code}: ' in fn or code in ("attachments", "diagrams", "images"), f"код {code} не разобран страницей"
    assert "Raw/" in ru["ingest.h_max_files"] and "Raw/" in en["ingest.h_max_files"], "подсказка потолка не говорит, что Raw/ им не ограничен"
    assert "Raw/Extracted" in ru["ingest.h_attachments"]
