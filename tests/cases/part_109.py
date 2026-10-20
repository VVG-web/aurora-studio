"""Проверки движка Aurora, часть 109. Каркас и помощники — tests/harness.py.

Раздел «Модели»: «Сохранить» гаснет после записи и от пустых событий браузера (автозаполнение, IME), «Проверить связь»
не отказывает без правок; слоты параллельного прогона идут в порядке цепочки, так что основной провайдер не
остаётся без заданий, а страница говорит, сколько потоков получит каждый провайдер. Сети и настоящей настройки
машины нет: настройка собирается в памяти.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401


def _mods():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("model_config"), importlib.import_module("agent_core")


def _config(old_width: int, new_width: int, cap, new_first: bool = True, parallel_old: bool = True):
    """Два провайдера в одной цепочке всех ролей LLM: прежний с шириной и новый, заведённый позже."""
    MC, _AG = _mods()
    order = ["new", "old"] if new_first else ["old", "new"]
    data, _notes = MC.normalize({
        "providers": [
            {"id": "old", "name": "Старый", "url": "http://old.example/v1", "key": "k1", "width": old_width,
             "parallel": parallel_old},
            {"id": "new", "name": "Новый", "url": "http://new.example/v1", "key": "k2", "width": new_width}],
        "capabilities": {"llm": {"roles": [
            {"id": rid, "backends": [{"provider": p, "model": "m-" + p} for p in order]}
            for rid in ("worker", "planner", "critic", "qa")]}},
        "settings": {"parallel": cap}})
    return MC.to_config(data)


@test
def test_pool_hands_out_slots_in_chain_order_so_the_primary_is_not_starved(tmp: Path):
    _MC, AG = _mods()
    n = {b["provider"]: b["n"] for b in _config(8, 0, "auto")["backends"]}
    new, old = n["new"], n["old"]
    # «авто»: прежний шлюз объявил ширину 8, новый (основной) нет — ему один слот, но ПЕРВЫЙ: с него начинается прогон
    auto = _config(8, 0, "auto")
    assert AG.pool(auto, "worker") == [new] + [old] * 8, AG.pool(auto, "worker")
    # потолок меньше ширины прежнего: раньше основной новый не получал ни одного задания (слоты шли прежнему)
    capped = _config(8, 0, 4)
    slots = AG.pool(capped, "worker")
    assert slots == [new, old, old, old] and slots.count(new) == 1, slots
    first = [AG.ring_order(capped, prefer=slots[i % len(slots)], role="worker")[0]["provider"] for i in range(8)]
    assert first.count("new") == 2, first
    # у основного ширина объявлена и хватает на потолок — прежний ждёт как запасной (как и было)
    both = _config(8, 8, 4)
    assert AG.pool(both, "worker") == [new] * 4
    # порядок цепочки обратный: основной прежний — он и занят первым, новый не вытесняет его
    flipped = _config(8, 0, 4, new_first=False)
    assert AG.pool(flipped, "worker") == [old] * 4, "основной стал вторым в пуле"
    assert AG.pool(_config(8, 0, "auto", new_first=False), "worker") == [old] * 8 + [new]
    # никто ширину не объявил — делят потолок поровну, как прежде
    assert sorted(AG.pool(_config(0, 0, 4), "worker")) == sorted([new, old] * 2)
    # провайдер, снятый с параллельного прогона, в пул не входит; основной — всегда
    assert AG.pool(_config(8, 0, "auto", parallel_old=False), "worker") == [new]
    # пул и семафор шлюза — одно правило: шлюзу не больше слотов, чем он получил
    for cfg in (auto, capped, both, flipped):
        for role in ("worker", "qa"):
            for k in set(AG.pool(cfg, role)):
                assert AG.pool(cfg, role).count(k) <= AG.gateway_width(cfg, k), (role, k)


@test
def test_models_state_tells_the_page_how_many_threads_each_provider_gets(tmp: Path):
    MC, AG = _mods()
    ck = importlib.import_module("aurora_cockpit")
    cfg = _config(8, 0, "auto")
    data, _ = MC.normalize({
        "providers": [{"id": "old", "name": "Старый", "url": "http://old.example/v1", "key": "k1", "width": 8},
                      {"id": "new", "name": "Новый", "url": "http://new.example/v1", "key": "k2"}],
        "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [
            {"provider": "new", "model": "m-new"}, {"provider": "old", "model": "m-old"}]}]}}})
    saved = ck.models_data
    ck.models_data = lambda: data
    try:
        state = ck.models_state([])
    finally:
        ck.models_data = saved
    by = state["slots_by_provider"]
    assert by == {"old": 8, "new": 1} and state["slots"] == 9, by
    assert [list(x) for x in state["slot_split"]] == [[1, 8], [2, 1]], state["slot_split"]
    assert set(by) == {b["provider"] for b in cfg["backends"]}
    assert "k1" not in json.dumps(state), "ключ провайдера ушёл в ответ страницы"


HARNESS = r"""
import fs from "node:fs";
class FakeNode {
  constructor(tag){ this.tag = tag; this.attrs = {}; this.kids = []; this.ls = {}; this.className = ""; this.nodeType = 1;
    this._v = undefined; this.style = {}; this.hidden = false; this.dataset = {}; }
  setAttribute(k, v){ this.attrs[k] = v; if (k === "value") this._v = String(v); }
  addEventListener(ev, f){ (this.ls[ev] = this.ls[ev] || []).push(f); }
  append(...k){ for (const x of k) this.kids.push(x && x.nodeType ? x : {nodeType: 3, text: String(x)}); }
  set innerHTML(v){ this.kids = []; }
  get textContent(){ return this.kids.map(k => k.text !== undefined ? k.text : k.textContent).join(""); }
  get value(){ return this._v !== undefined ? this._v : ""; }
  set value(v){ this._v = String(v); }
}
globalThis.document = {createElement: tag => new FakeNode(tag), createTextNode: s => ({nodeType: 3, text: s}),
                       getElementById: () => null};
globalThis.confirm = () => true;
@EL@
const walk = function*(n){ yield n; for (const k of n.kids || []) if (k && k.tag) yield* walk(k); };
const all = (root, pred) => [...walk(root)].filter(pred);
const fire = async (n, ev) => { if (n["on" + ev]) await n["on" + ev]({target: n}); for (const f of n.ls[ev] || []) await f({target: n}); };
const boxes = {}, calls = [], toasts = [];
const ctx = {
  root: new FakeNode("div"), t: (k, v) => v ? k + JSON.stringify(v) : k, el,
  toast: (m, kind) => toasts.push([m, kind]),
  $: sel => boxes[sel] || (boxes[sel] = new FakeNode("div")),
  api: async (url, opts) => { calls.push([url, opts && opts.body ? JSON.parse(opts.body) : null]); return RESPOND[url.split("?")[0]]; },
  runWatched: () => {},
};
const DATA = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const RESPOND = {"/api/models": DATA, "/api/models/save": {ok: true, notes: []}, "/api/agent/ping": {backends: []}};
const mod = await import(process.argv[3]);
const fails = [];
const ok = (c, m) => { if (!c) fails.push(m); };
const bar = () => boxes["#modelsBar"];
const saveBtn = () => bar().kids[0];
const saveOff = () => saveBtn().attrs.disabled === "";
const chipText = () => bar().textContent;
const nameInputs = () => all(boxes["#modelsBody"], n => n.tag === "input" && n.attrs.placeholder === "models.p_name_ph");
const body = () => boxes["#modelsBody"].textContent;
(async () => {
  await mod.refresh(ctx);
  ok(saveOff(), "после открытия «Сохранить» горит");
  ok(!chipText().includes("models.unsaved"), "после открытия есть «несохранённые правки»");
  // провайдер без `health` в ответе сервера: показ вкладки дописывает поле, но настройкой это не считается
  const inputs = nameInputs();
  ok(inputs.length === 2, "полей названия провайдеров: " + inputs.length);
  ok(saveOff(), "показ вкладки сам изменил настройку");
  // пустое событие (автозаполнение браузера подставило то же значение, IME): настройка прежняя
  await fire(inputs[0], "input");
  ok(saveOff() && !chipText().includes("models.unsaved"), "пустое событие input зажгло «Сохранить»");
  // правка и возврат к прежнему значению
  const was = inputs[0].value;
  inputs[0].value = "Другое имя"; await fire(inputs[0], "input");
  ok(!saveOff() && chipText().includes("models.unsaved"), "правка не зажгла «Сохранить»");
  await mod.refresh(ctx);                      // возврат в раздел не теряет правку
  ok(!saveOff(), "возврат в раздел потерял несохранённую правку");
  const again = nameInputs()[0];
  again.value = was; await fire(again, "input");
  ok(saveOff() && !chipText().includes("models.unsaved"), "возврат к прежнему значению не погасил «Сохранить»");
  // запись: правка → «Сохранить» → кнопка гаснет, а проверка связи идёт, не жалуясь на несохранённое
  const edit = nameInputs()[1];
  edit.value = "Новое имя"; await fire(edit, "input");
  // сервер отвечает на повторное чтение не сразу (на Windows — секунды): «Сохранить» обязана погаснуть до ответа
  const later = JSON.parse(JSON.stringify(DATA));
  later.models.providers[1].name = "Новое имя";
  let release;
  RESPOND["/api/models"] = new Promise(res => { release = () => res(later); });
  const pending = saveBtn().ls.click[0]();
  await new Promise(r => setTimeout(r, 20));
  ok(saveOff() && !chipText().includes("models.unsaved"), "до ответа сервера на повторное чтение «Сохранить» горит");
  release();
  await pending;
  const sent = calls.filter(c => c[0] === "/api/models/save").at(-1)[1];
  ok(sent.models.providers[1].name === "Новое имя" && sent.expect === DATA.stamp, "в запись ушло не то: " + JSON.stringify(sent).slice(0, 200));
  ok(toasts.at(-1)[0] === "models.saved", "нет сообщения о записи: " + JSON.stringify(toasts.at(-1)));
  ok(saveOff() && !chipText().includes("models.unsaved"), "после записи «Сохранить» горит или есть «несохранённые правки»");
  const pingBtn = bar().kids.find(k => k.tag === "button" && k.attrs["data-llm"] === "ping");
  await pingBtn.ls.click[0]();
  ok(!toasts.some(t => t[0] === "models.ping_save_first"), "проверка связи отказала после записи");
  ok(calls.some(c => c[0] === "/api/agent/ping"), "проверка связи не запущена");
  // событие браузера уже после записи: по-прежнему ничего не зажигает
  await fire(nameInputs()[0], "input");
  ok(saveOff(), "пустое событие после записи зажгло «Сохранить»");
  // потоки провайдеров: чипы у карточек и строка над ними; «ширина не объявлена» больше не бывает
  const text = body();
  ok(text.includes('models.p_slots{"n":8}') && text.includes('models.p_slots{"n":1}'), "чипы потоков: " + text.slice(0, 400));
  ok(!text.includes("p_width_lost"), "страница всё ещё говорит о необъявленной ширине");
  ok(text.includes('models.run_slots{"n":9,"split":"Старый×8, Новое имя×1"}'), "строка потоков над карточками: " + text.slice(0, 300));
  // ширина — не меньше 1: у поля min=1, а ноль, пустое и дробь не доходят до настройки
  const widths = all(boxes["#modelsBody"], n => n.tag === "input" && n.attrs.type === "number");
  ok(widths.length === 2 && widths.every(w => String(w.attrs.min) === "1"), "поля ширины без min=1: " + widths.map(w => w.attrs.min));
  const wide = widths[1];
  for (const [typed, want] of [["0", 1], ["", 1], ["-4", 1], ["2.9", 2], ["12", 12]]){
    wide.value = typed; await fire(wide, "input"); await fire(wide, "change");
    ok(wide.value === String(want), `в поле набрали «${typed}», на уходе из него «${wide.value}», ждали ${want}`);
  }
  wide.value = "0"; await fire(wide, "input");
  RESPOND["/api/models"] = JSON.parse(JSON.stringify(DATA));
  await saveBtn().ls.click[0]();
  const sentW = calls.filter(c => c[0] === "/api/models/save").at(-1)[1];
  ok(sentW.models.providers.every(p => p.width >= 1), "в запись ушла ширина меньше 1: " + JSON.stringify(sentW.models.providers.map(p => p.width)));
  // новый провайдер заводится с шириной 1, а не 0
  await fire(all(boxes["#modelsBody"], n => n.tag === "button" && n.textContent === "models.add_provider")[0], "click");
  const after = all(boxes["#modelsBody"], n => n.tag === "input" && n.attrs.type === "number");
  ok(after.length === 3 && after[2].value === "1", "новый провайдер заведён с шириной «" + (after[2] && after[2].value) + "», ждали 1");
  console.log(JSON.stringify({fails}));
})();
"""


@test
def test_the_models_page_unsaved_mark_follows_the_data_not_the_event(tmp: Path):
    view = (KIT / "cockpit/modules/models/view.js").read_text(encoding="utf-8")
    assert 'autocomplete: "new-password"' in view, "поле ключа провайдера снова просит браузер подставлять пароль"
    assert "canon() !== SNAP" in view and "DIRTY = true;\n  if (redraw)" not in view, \
        "признак «несохранённое» снова защёлкивается любым событием"
    node = shutil.which("node")
    if not node:
        return
    MC, _AG = _mods()
    models, _ = MC.normalize({
        "providers": [{"id": "old", "name": "Старый", "url": "http://old.example/v1", "key": "k1", "width": 8},
                      {"id": "new", "name": "Новый", "url": "http://new.example/v1", "key": "k2"}],
        "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [
            {"provider": "new", "model": "m-new"}, {"provider": "old", "model": "m-old"}]}]}}})
    for p in models["providers"]:
        p.pop("health", None)                  # сервер старого вида: поле связи страница дописывает сама
        p["key"] = MC.MASK
    data = {"models": models, "error": "", "path": "/kit/local/models.json", "stamp": "s1", "types": list(MC.PROVIDER_TYPES),
            "projects": ["P"], "leftovers": [], "slots": 9, "slot_split": [[1, 8], [2, 1]],
            "slots_by_provider": {"old": 8, "new": 1}, "net_profiles": [], "health_checks": list(MC.HEALTH_CHECKS)}
    (tmp / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (tmp / "view.mjs").write_text(view, encoding="utf-8")
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    a = panel.index("const el = (tag")
    el_src = panel[a:panel.index("\n};\n", a) + 4]
    (tmp / "run.mjs").write_text(HARNESS.replace("@EL@", el_src), encoding="utf-8")
    cp = subprocess.run([node, str(tmp / "run.mjs"), str(tmp / "data.json"), (tmp / "view.mjs").as_uri()],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, cwd=str(tmp))
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1800]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


@test
def test_the_models_catalogues_name_the_thread_hints_and_do_not_promise_the_old_rule(tmp: Path):
    ru = json.loads((KIT / "cockpit/modules/models/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/modules/models/i18n/en.json").read_text(encoding="utf-8"))
    view = (KIT / "cockpit/modules/models/view.js").read_text(encoding="utf-8")
    keys = ("models.p_slots", "models.p_slots_hint", "models.slots_about")
    assert "models.p_width_lost" not in ru and "models.p_width_lost" not in en, "осталась строка про необъявленную ширину"
    for k in keys:
        assert k in ru and k in en, f"{k}: нет в одном из каталогов"
        assert re.search("[А-Яа-я]", ru[k]) and not re.search("[А-Яа-я]", en[k]), f"{k}: язык строки не тот"
        assert re.findall(r"\{(\w+)\}", ru[k]) == re.findall(r"\{(\w+)\}", en[k]), f"{k}: подстановки разошлись"
        assert f't("{k}"' in view, f"страница не спрашивает {k}"
    # прежняя подсказка обещала «провайдер делит общий потолок» и при объявленной у другого ширине была неправдой
    assert "не меньше 1" in ru["models.p_width_hint"] and "at least 1" in en["models.p_width_hint"], \
        "подсказка ширины не говорит, что ширина — от 1"
    assert "0 — не объявлено" not in ru["models.p_width_hint"] and "0 — not declared" not in en["models.p_width_hint"]


def _raw_pool(providers: list, cap, used=("p1",)) -> list:
    """Слоты «как до правки»: настройка без нормализации, с нулями в ширине, прямо в `to_config` и пул."""
    MC, AG = _mods()
    data = MC.empty()
    data["providers"] = [{"id": f"p{i}", "name": f"P{i}", "type": "openai", "url": f"http://p{i}.example/v1", "key": "",
                          "width": w, "parallel": True, "template": {}, "vpn": "",
                          "health": {"check": "off", "path": "/models"}} for i, w in enumerate(providers, 1)]
    data["capabilities"]["llm"]["roles"][0]["backends"] = [
        {"provider": u, "model": "m", "enabled": True, "context": 0, "url": ""} for u in used]
    data["settings"]["parallel"] = cap
    return sorted(AG.pool(MC.to_config(data), "worker"))


def _norm_pool(providers: list, cap, used=("p1",)):
    MC, AG = _mods()
    raw = {"providers": [{"id": f"p{i}", "name": f"P{i}", "url": f"http://p{i}.example/v1", "width": w}
                         for i, w in enumerate(providers, 1)],
           "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [{"provider": u, "model": "m"} for u in used]}]}},
           "settings": {"parallel": cap}}
    data, _notes = MC.normalize(raw)
    return data, sorted(AG.pool(MC.to_config(data), "worker"))


@test
def test_provider_width_is_never_below_one_and_an_old_zero_keeps_what_it_gave(tmp: Path):
    MC, _AG = _mods()

    def widths(provs, cap="auto"):
        data, _ = _norm_pool(provs, cap, used=("p1",))
        return [p["width"] for p in data["providers"]]
    # ноль, пусто, отрицательное и мусор — не меньше 1; дробь и строка-число принимаются как есть
    assert widths([0]) == [1] and widths([None]) == [1] and widths([-3]) == [1] and widths(["x"]) == [1]
    assert widths([0, 8]) == [1, 8] and widths(["5", 8.9]) == [5, 8]
    # повторная нормализация ничего не меняет: сохранённое не «плывёт»
    once, _ = _norm_pool([0, 0], 6)
    assert MC.normalize(once)[0]["providers"] == once["providers"]
    # что давал ноль, то он и даёт: слоты до и после совпадают во всех прежних случаях
    for provs, cap, used in (([0], 6, ("p1",)),                  # один провайдер, потолок числом: все потоки ему
                             ([0, 0], 6, ("p1", "p2")),          # два провайдера цепочки: потолок поровну
                             ([0, 0], 5, ("p1", "p2")),          # нечётный потолок: остаток первому
                             ([0], "auto", ("p1",)),             # «авто»: по одному потоку
                             ([0, 0], "auto", ("p1", "p2")),
                             ([8, 0], 6, ("p1", "p2")),          # ширину объявил другой: этому один поток
                             ([0, 0], 6, ("p1",))):              # второй провайдер только векторов: потолок не делит
        assert _norm_pool(provs, cap, used)[1] == _raw_pool(provs, cap, used), (provs, cap, used)
    # провайдер, не берущийся в параллельный прогон, потолок не делит
    data, _ = MC.normalize({"providers": [{"id": "a", "url": "http://a.example/v1"},
                                          {"id": "b", "url": "http://b.example/v1", "parallel": False}],
                            "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [
                                {"provider": "a", "model": "m"}, {"provider": "b", "model": "m"}]}]}},
                            "settings": {"parallel": 6}})
    assert [p["width"] for p in data["providers"]] == [6, 1]
    # тот же минимум держит и панель: сервер при записи не принимает ширину меньше 1
    for body in ({"width": 0}, {"width": -1}, {}):
        assert MC.normalize({"providers": [{"id": "a", "url": "http://a.example/v1", **body}]})[0]["providers"][0]["width"] >= 1
