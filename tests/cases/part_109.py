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
  // потоки провайдеров: чипы у карточек и предупреждение про необъявленную ширину
  const text = body();
  ok(text.includes('models.p_slots{"n":8}') && text.includes('models.p_slots{"n":1}'), "чипы потоков: " + text.slice(0, 400));
  ok((text.match(/models\.p_width_lost/g) || []).length === 1, "предупреждение о необъявленной ширине — ровно у одного провайдера: " + text.slice(0, 400));
  ok(text.includes('models.run_slots{"n":9,"split":"Старый×8, Новое имя×1"}'), "строка потоков над карточками: " + text.slice(0, 300));
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
    keys = ("models.p_slots", "models.p_slots_hint", "models.p_width_lost", "models.p_width_lost_hint", "models.slots_about")
    for k in keys:
        assert k in ru and k in en, f"{k}: нет в одном из каталогов"
        assert re.search("[А-Яа-я]", ru[k]) and not re.search("[А-Яа-я]", en[k]), f"{k}: язык строки не тот"
        assert re.findall(r"\{(\w+)\}", ru[k]) == re.findall(r"\{(\w+)\}", en[k]), f"{k}: подстановки разошлись"
        assert f't("{k}"' in view, f"страница не спрашивает {k}"
    # прежняя подсказка обещала «провайдер делит общий потолок» и при объявленной у другого ширине была неправдой
    assert "один поток" in ru["models.p_width_hint"] and "single thread" in en["models.p_width_hint"], \
        "подсказка ширины снова не говорит, что необъявленная ширина при объявленной у другого — один поток"
