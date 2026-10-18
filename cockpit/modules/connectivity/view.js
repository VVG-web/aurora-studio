/* Проверка соединения — раздел-модуль: профили, адреса проверки (probe), интернет, правила и политика.

   Настройка одна на кит (`local/connectivity.json`, cockpit → connectivity.py). Страница правит копию
   настройки и отправляет её целиком вместе со слепком файла (`expect`): если файл за это время
   меняли, сервер отказывает (409), и черновик остаётся в странице. Файл руками править не нужно —
   всё делается здесь. Состояние (профили, probe, провайдеры) приходит с сервера и обновляется
   опросом, пока раздел на виду. Сети раздел сам не трогает: проверяет сервер панели. */

const TYPES = ["tcp", "http", "icmp"];
const AGGS = ["any_healthy", "quorum", "all_healthy"];
const POLICIES = ["fail_fast", "partial", "queue", "partial_queue"];
const NET = "internet";                      // имя встроенного профиля интернета (данные движка)
const POLL_MS = 10000;
let DATA = null, C = null, DIRTY = false, TIMER = null, WIZ = null, WIZ_OFF = false;
const RES = {};                              // id probe → результат ручной проверки
const LIVE = {profiles: {}, probes: {}};     // узлы, которые обновляет опрос, не трогая поля ввода

const clone = o => JSON.parse(JSON.stringify(o));

export function mount(ctx){
  ctx.root.dataset.module = "connectivity";
}

export async function refresh(ctx){
  // Несохранённая правка — человека: возврат в раздел рисует её из памяти (как у «Моделей»).
  if (DIRTY && C){ draw(ctx); return; }
  const d = await load(ctx);
  if (!d) return;
  DATA = d; C = clone(d.config); DIRTY = false;
  draw(ctx);
}

export function onShow(ctx){
  clearInterval(TIMER);
  TIMER = setInterval(() => poll(ctx), POLL_MS);
}

export function onHide(){
  clearInterval(TIMER);
  TIMER = null;
}

async function load(ctx){
  const q = ctx.project ? "?project=" + encodeURIComponent(ctx.project.path) : "";
  const d = await ctx.api("/api/net/state" + q, {quiet: true});
  if (!d || !d.config){
    ctx.toast((d && d.error) || ctx.t("connectivity.load_failed"), "err");
    return null;
  }
  return d;
}

// Обновление состояния без перерисовки полей: человек может печатать.
async function poll(ctx){
  const d = await load(ctx);
  if (!d || !DATA) return;
  DATA.snapshot = d.snapshot;
  if (!DIRTY && d.stamp !== DATA.stamp){          // настройку изменили снаружи — перечитать
    DATA = d; C = clone(d.config); draw(ctx);
    return;
  }
  drawHead(ctx); drawProviders(ctx); drawEvents(ctx); drawLive(ctx);
}

function draw(ctx){
  drawNotes(ctx); drawHead(ctx); drawWizard(ctx); drawProfiles(ctx); drawInternet(ctx);
  drawSettings(ctx); drawProviders(ctx); drawEvents(ctx); drawBar(ctx);
}

function touch(ctx, redraw){
  DIRTY = true;
  if (redraw){ drawHead(ctx); drawWizard(ctx); drawProfiles(ctx); drawInternet(ctx); drawSettings(ctx); }
  drawBar(ctx);
}

/* ---------------------------------------------------------------- вспомогательное */

const snap = () => (DATA && DATA.snapshot) || {};
const snapProfile = name => name === NET ? snap().internet
  : (snap().profiles || []).find(p => p.name === name);
const snapProbe = id => {
  const rows = [snap().internet, ...(snap().profiles || [])].filter(Boolean);
  for (const r of rows){ const p = (r.probes || []).find(x => x.id === id); if (p) return p; }
  return null;
};
const limit = () => (DATA && DATA.limits && DATA.limits.probes) || 10;
const nextId = () => {
  const taken = new Set(C.probes.map(p => p.id));
  let n = 1;
  while (taken.has("p" + n)) n++;
  return "p" + n;
};
const probesOf = name => C.probes.filter(p => p.profile === name);
const field = (ctx, label, input, hint) => ctx.el("label", {style: "display:block;flex:1;min-width:150px"},
  ctx.el("div", {class: "muted", style: "font-size:11.5px;margin-bottom:3px"}, label), input,
  hint ? ctx.el("div", {class: "muted", style: "font-size:11px;margin-top:3px"}, hint) : null);

function text(ctx, obj, key, attrs){
  const x = ctx.el("input", {class: "btn", style: "width:100%;font-weight:400", ...attrs});
  x.value = obj[key] == null ? "" : String(obj[key]);
  x.oninput = () => { obj[key] = x.value; touch(ctx); };
  return x;
}

// Число: пусто — поле не задано (берётся общее); хранится числом.
function num(ctx, obj, key, attrs, allowEmpty){
  const x = ctx.el("input", {class: "btn", type: "number", style: "width:100%;font-weight:400", ...attrs});
  x.value = obj[key] == null ? "" : String(obj[key]);
  x.oninput = () => {
    const v = x.value.trim();
    if (v === "" && allowEmpty) delete obj[key]; else obj[key] = v === "" ? "" : Number(v);
    touch(ctx);
  };
  return x;
}

function stateChip(ctx, state){
  return ctx.el("span", {class: "chip " + ({online: "ok", degraded: "warn", offline: "bad"}[state] || "")},
    ctx.t("connectivity.state." + (state || "unknown")));
}

function resultChip(ctx, r){
  if (!r) return ctx.el("span", {class: "chip"}, ctx.t("connectivity.res_none"));
  if (r.busy) return ctx.el("span", {class: "chip"}, ctx.el("span", {class: "spin"}), " ", ctx.t("connectivity.res_busy"));
  if (r.ok === null || r.ok === undefined) return ctx.el("span", {class: "chip"}, ctx.t("connectivity.res_none"));
  const extra = r.code === "status" || r.code === "body" || r.code === "other" || r.code === "tls"
    ? (r.detail ? " · " + r.detail : "") : "";
  return ctx.el("span", {class: "chip " + (r.ok ? "ok" : "bad")},
    r.ok ? ctx.t("connectivity.res_ok", {ms: r.ms == null ? 0 : r.ms})
         : ctx.t("connectivity.code." + (r.code || "other")) + extra);
}

function normText(ctx, p){
  return ctx.t("connectivity.norm." + p.type, {status: (p.expect_status || ["2xx", "3xx"]).join(", ")});
}

/* ---------------------------------------------------------------- шапка */

function drawNotes(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netNotes");
  box.innerHTML = "";
  if (DATA.error) box.append(el("div", {class: "warnbox", style: "margin-bottom:8px"}, DATA.error));
  (C.notes || []).forEach(n => box.append(el("div", {class: "warnbox", style: "margin-bottom:8px"}, n)));
  box.append(el("div", {class: "muted", style: "font-size:12.5px;margin-bottom:10px"},
    t("connectivity.scope"), el("span", {class: "mono", style: "margin-left:6px"}, DATA.path || "")));
}

function modeText(ctx, s){
  const {t} = ctx;
  if (!s.enabled) return t("connectivity.mode.off");
  const down = (s.profiles || []).filter(p => p.required && p.state === "offline").map(p => p.name);
  if (s.state === "offline") return t("connectivity.mode.offline");
  if ((s.state === "no_vpn" || s.state === "partial") && down.length)
    return t("connectivity.mode.down", {n: down.length, names: down.join(", ")});
  return t("connectivity.global." + (s.state || "unknown"));
}

function drawHead(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netHead");
  box.innerHTML = "";
  const s = snap();
  const on = el("input", {type: "checkbox", checked: C.enabled ? "" : null});
  on.onchange = () => { C.enabled = on.checked; touch(ctx); drawHead(ctx); };
  const reasons = (s.reasons || []).map(r => el("li", {}, t("connectivity.reason." + r.code,
    {name: r.name || "", profile: r.profile || ""})));
  box.append(el("div", {class: "card", style: "padding:16px;margin-bottom:14px"},
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap"},
      el("label", {class: "row", style: "gap:8px;font-weight:600"}, on, t("connectivity.enable")),
      s.enabled ? el("span", {class: "chip " + ({online: "ok", degraded: "warn", partial: "warn",
        no_vpn: "bad", offline: "bad"}[s.state] || "")}, t("connectivity.gchip." + (s.state || "unknown"))) : null,
      el("span", {style: "flex:1"}),
      el("span", {class: "chip"}, t("connectivity.probe_count", {n: C.probes.length, max: limit()})),
      el("button", {class: "btn sm", onclick: () => ctx.openDoc("docs/connectivity.md")}, t("connectivity.doc")),
      el("button", {class: "btn sm", onclick: () => checkAll(ctx)}, t("connectivity.check_all"))),
    el("div", {class: "muted", style: "font-size:12px;margin-top:6px"}, t("connectivity.enable_hint")),
    el("div", {style: "margin-top:10px;font-weight:600"}, modeText(ctx, s)),
    reasons.length ? el("ul", {class: "muted", style: "font-size:12.5px;margin:6px 0 0 18px;padding:0"}, ...reasons) : null));
}

async function checkAll(ctx){
  const {t} = ctx;
  if (DIRTY) return ctx.toast(t("connectivity.check_save_first"), "warn");
  ctx.toast(t("connectivity.checking"));
  const body = {project: ctx.project ? ctx.project.path : ""};
  const r = await ctx.api("/api/net/probe", {method: "POST", quiet: true, body: JSON.stringify(body)});
  if (r && r.snapshot){ DATA.snapshot = r.snapshot; draw(ctx); ctx.toast(t("connectivity.checked"), "ok"); }
  else ctx.toast((r && r.error) || t("connectivity.check_failed"), "err");
}

/* ---------------------------------------------------------------- мастер «первый профиль» */

function startWizard(target){
  WIZ = {target, step: 1, name: target === NET ? NET : suggestName(), kind: "tcp", host: "", port: "",
         url: "", result: null, busy: false};
}

function suggestName(){
  const taken = new Set(C.profiles.map(p => p.name.toLowerCase()));
  let n = 1;
  while (taken.has("vpn-" + n)) n++;
  return "VPN-" + n;
}

function wizardProbe(w){
  const p = {id: nextId(), profile: w.target === NET ? NET : w.name, type: w.kind};
  if (w.kind === "http") p.url = w.url.trim();
  else { p.host = w.host.trim(); if (w.kind === "tcp") p.port = Number(w.port || 0); }
  return p;
}

// Пустое состояние: ни профилей, ни адресов интернета, мастер не открыт и его не отменяли.
const emptyState = () => !C.profiles.length && !probesOf(NET).length && !WIZ_OFF;

function drawWizard(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netWizard");
  box.innerHTML = "";
  if (!WIZ && emptyState()){
    // Пустое состояние: объясняем, что такое probe, и предлагаем начать с мастера.
    box.append(el("div", {class: "card", style: "padding:18px;margin-bottom:14px"},
      el("b", {}, t("connectivity.empty_title")),
      el("p", {class: "muted", style: "font-size:13px;margin:8px 0"}, t("connectivity.probe_what")),
      el("p", {class: "muted", style: "font-size:13px;margin:0 0 12px"}, t("connectivity.empty_how")),
      el("button", {class: "btn primary", onclick: () => { startWizard("profile"); draw(ctx); }},
        t("connectivity.add_profile"))));
    return;
  }
  if (!WIZ) return;
  const w = WIZ;
  const total = 3;
  const card = el("div", {class: "card", style: "padding:18px;margin-bottom:14px"});
  card.append(el("div", {class: "row", style: "gap:8px;margin-bottom:8px"},
    el("b", {}, t(w.target === NET ? "connectivity.wiz_title_net" : "connectivity.wiz_title")),
    el("span", {class: "chip"}, t("connectivity.wiz_step", {step: w.step, total})),
    el("span", {style: "flex:1"}),
    el("button", {class: "btn sm", onclick: () => { WIZ = null; WIZ_OFF = true; draw(ctx); }}, t("connectivity.cancel"))));
  if (w.step === 1){
    if (w.target === NET){
      card.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 10px"}, t("connectivity.wiz1_net")));
    } else {
      const name = el("input", {class: "btn", style: "width:260px;font-weight:400", placeholder: "VPN-1"});
      name.value = w.name;
      name.oninput = () => { w.name = name.value; };
      card.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 10px"}, t("connectivity.wiz1")),
        field(ctx, t("connectivity.f_name"), name, t("connectivity.h_name")));
    }
  } else if (w.step === 2){
    card.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 10px"},
      t(w.target === NET ? "connectivity.wiz2_net" : "connectivity.wiz2")), wizardFields(ctx, w));
  } else {
    const p = wizardProbe(w);
    const out = el("div", {style: "margin:6px 0"});
    out.append(resultChip(ctx, w.busy ? {busy: true} : w.result));
    if (w.result && w.result.target) out.append(el("span", {class: "mono muted", style: "margin-left:8px;font-size:12px"}, w.result.target));
    // Родной append пишет null как текст «null»: пропуски убирает только el.
    card.append(el("div", {},
      el("p", {class: "muted", style: "font-size:13px;margin:0 0 10px"}, t("connectivity.wiz3")), out,
      el("div", {class: "muted", style: "font-size:12.5px;margin-top:6px"}, t("connectivity.norm_label"), " ", normText(ctx, p)),
      w.result && w.result.ok === false && !w.result.problems
        ? el("div", {class: "warnbox", style: "margin-top:8px"}, t("connectivity.wiz3_fail")) : null,
      w.result && w.result.problems
        ? el("div", {class: "warnbox", style: "margin-top:8px"}, problemText(ctx, w.result.problems[0])) : null,
      el("div", {style: "margin-top:8px"}, el("button", {class: "btn", onclick: () => testWizard(ctx)}, t("connectivity.test")))));
  }
  card.append(el("div", {class: "row", style: "gap:8px;margin-top:14px"},
    w.step > (w.target === NET ? 1 : 1) ? el("button", {class: "btn", onclick: () => { w.step--; drawWizard(ctx); }}, t("connectivity.back")) : null,
    w.step < total ? el("button", {class: "btn primary", onclick: () => wizardNext(ctx)}, t("connectivity.next"))
      : el("button", {class: "btn primary", onclick: () => wizardDone(ctx)}, t("connectivity.wiz_done"))));
  box.append(card);
}

function wizardFields(ctx, w){
  const {t, el} = ctx;
  const kind = el("select", {class: "btn", style: "width:100%"},
    ...TYPES.map(v => el("option", {value: v, selected: w.kind === v ? "" : null}, t("connectivity.type." + v))));
  kind.onchange = () => { w.kind = kind.value; w.result = null; drawWizard(ctx); };
  const inp = (key, ph, cls) => {
    const x = el("input", {class: "btn " + (cls || ""), style: "width:100%;font-weight:400", placeholder: ph});
    x.value = w[key];
    x.oninput = () => { w[key] = x.value; w.result = null; };
    return x;
  };
  const rows = [field(ctx, t("connectivity.f_type"), kind, t("connectivity.h_type." + w.kind))];
  if (w.kind === "http") rows.push(field(ctx, t("connectivity.f_url"), inp("url", "https://example.com/", "mono"), t("connectivity.h_url")));
  else {
    rows.push(field(ctx, t("connectivity.f_host"), inp("host", "example.com", "mono"), t("connectivity.h_host." + w.kind)));
    if (w.kind === "tcp") rows.push(field(ctx, t("connectivity.f_port"), inp("port", "443", "mono"), t("connectivity.h_port")));
  }
  return el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;align-items:flex-start"}, ...rows);
}

function wizardNext(ctx){
  const w = WIZ;
  if (w.step === 1 && w.target !== NET){
    const name = w.name.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$/.test(name)) return ctx.toast(ctx.t("connectivity.err.bad_name"), "warn");
    if (name.toLowerCase() === NET || C.profiles.some(p => p.name.toLowerCase() === name.toLowerCase()))
      return ctx.toast(ctx.t("connectivity.err.dup_profile"), "warn");
    w.name = name;
  }
  if (w.step === 2){
    const bad = w.kind === "http" ? !w.url.trim() : !w.host.trim() || (w.kind === "tcp" && !String(w.port).trim());
    if (bad) return ctx.toast(ctx.t("connectivity.wiz_fill"), "warn");
  }
  w.step++;
  drawWizard(ctx);
  if (w.step === 3) testWizard(ctx);
}

async function testWizard(ctx){
  const w = WIZ;
  if (!w) return;
  w.busy = true; drawWizard(ctx);
  const r = await ctx.api("/api/net/probe", {method: "POST", quiet: true,
    body: JSON.stringify({draft: wizardProbe(w), settings: C.settings})});
  w.busy = false;
  w.result = (r && r.result) || {ok: false, code: "other", detail: (r && r.error) || ""};
  if (WIZ === w) drawWizard(ctx);
}

function wizardDone(ctx){
  const w = WIZ;
  if (C.probes.length >= limit()) return ctx.toast(ctx.t("connectivity.err.probe_limit", {max: limit()}), "warn");
  if (w.target !== NET) C.profiles.push({name: w.name, aggregate: "any_healthy"});
  C.probes.push(wizardProbe(w));
  WIZ = null; WIZ_OFF = true;
  touch(ctx, true);
}

/* ---------------------------------------------------------------- профили и probe */

function addProbe(ctx, profile){
  if (C.probes.length >= limit()) return ctx.toast(ctx.t("connectivity.err.probe_limit", {max: limit()}), "warn");
  C.probes.push({id: nextId(), profile, type: "tcp", host: "", port: 443});
  touch(ctx, true);
}

function probeRow(ctx, p){
  const {t, el} = ctx;
  const type = el("select", {class: "btn", style: "width:100%"},
    ...TYPES.map(v => el("option", {value: v, selected: p.type === v ? "" : null}, t("connectivity.type." + v))));
  type.onchange = () => {
    p.type = type.value;
    if (p.type === "http") { delete p.host; delete p.port; p.url = p.url || ""; }
    else { delete p.url; delete p.expect_status; delete p.expect_body; p.host = p.host || ""; if (p.type === "tcp") p.port = p.port || 443; else delete p.port; }
    delete RES[p.id];
    touch(ctx, true);
  };
  const cells = [field(ctx, t("connectivity.f_type"), type, t("connectivity.h_type." + p.type))];
  if (p.type === "http"){
    cells.push(field(ctx, t("connectivity.f_url"), text(ctx, p, "url", {class: "btn mono", placeholder: "https://example.com/"}), t("connectivity.h_url")));
    const st = ctx.el("input", {class: "btn mono", style: "width:100%;font-weight:400", placeholder: "2xx, 3xx"});
    st.value = (p.expect_status || []).join(", ");
    st.oninput = () => {
      const parts = st.value.split(/[\s,;]+/).filter(Boolean);
      if (parts.length) p.expect_status = parts; else delete p.expect_status;
      touch(ctx);
    };
    cells.push(field(ctx, t("connectivity.f_status"), st, t("connectivity.h_status")));
    const body = text(ctx, p, "expect_body", {placeholder: "OK"});
    body.oninput = () => { if (body.value) p.expect_body = body.value; else delete p.expect_body; touch(ctx); };
    cells.push(field(ctx, t("connectivity.f_body"), body, t("connectivity.h_body")));
  } else {
    cells.push(field(ctx, t("connectivity.f_host"), text(ctx, p, "host", {class: "btn mono", placeholder: "example.com"}), t("connectivity.h_host." + p.type)));
    if (p.type === "tcp")
      cells.push(field(ctx, t("connectivity.f_port"), num(ctx, p, "port", {placeholder: "443", min: 1, max: 65535}), t("connectivity.h_port")));
  }
  const res = el("span", {});
  LIVE.probes[p.id] = {node: res, p};
  paintProbe(ctx, p.id);
  const adv = el("details", {style: "margin-top:6px"},
    el("summary", {class: "muted", style: "cursor:pointer;font-size:12px"}, t("connectivity.advanced")),
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;margin-top:8px;align-items:flex-start"},
      field(ctx, t("connectivity.f_timeout"), num(ctx, p, "timeout", {placeholder: String(DATA.defaults.timeout), min: 1, max: 30}, true), t("connectivity.h_timeout")),
      field(ctx, t("connectivity.f_interval"), num(ctx, p, "interval", {placeholder: String(DATA.defaults.interval), min: 10}, true), t("connectivity.h_interval")),
      field(ctx, t("connectivity.f_label"), text(ctx, p, "label", {placeholder: t("connectivity.ph_label")}), t("connectivity.h_label"))));
  return el("div", {class: "list-item", style: "flex-direction:column;align-items:stretch;gap:6px"},
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;align-items:flex-start"}, ...cells),
    el("div", {class: "row", style: "gap:8px;align-items:center"},
      el("span", {class: "chip mono"}, p.id), res,
      el("span", {style: "flex:1"}),
      el("button", {class: "btn sm", onclick: () => testProbe(ctx, p)}, t("connectivity.test")),
      el("button", {class: "btn sm danger", onclick: () => {
        C.probes = C.probes.filter(x => x !== p); delete RES[p.id]; touch(ctx, true); }}, t("connectivity.drop"))),
    adv);
}

function paintProbe(ctx, id){
  const live = LIVE.probes[id];
  if (!live) return;
  const {el} = ctx;
  live.node.innerHTML = "";
  const r = RES[id] || snapProbe(id);
  live.node.append(resultChip(ctx, r));
  if (r && r.at && !r.busy) live.node.append(el("span", {class: "muted", style: "margin-left:6px;font-size:11.5px"}, ctx.fmt.when(r.at)));
  if (RES[id] && RES[id].ok && !RES[id].busy)
    live.node.append(el("span", {class: "muted", style: "margin-left:6px;font-size:11.5px"}, ctx.t("connectivity.norm_label") + " " + normText(ctx, live.p)));
}

async function testProbe(ctx, p){
  RES[p.id] = {busy: true};
  paintProbe(ctx, p.id);
  const send = clone(p);
  delete send.profile;
  const r = await ctx.api("/api/net/probe", {method: "POST", quiet: true,
    body: JSON.stringify({draft: send, settings: C.settings})});
  const res = (r && r.result) || {ok: false, code: "other", detail: (r && r.error) || ""};
  RES[p.id] = res.problems ? {ok: false, code: "other", detail: problemText(ctx, res.problems[0])} : {...res, at: new Date().toISOString()};
  paintProbe(ctx, p.id);
}

function problemText(ctx, pr){
  const key = "connectivity.err." + pr.code;
  const s = ctx.t(key, {max: limit()});
  return s === key ? (pr.text || pr.code) : s;
}

function addProbeButton(ctx, profile){
  const {t, el} = ctx;
  const full = C.probes.length >= limit();
  return el("div", {class: "row", style: "gap:8px;align-items:center;margin-top:8px"},
    el("button", {class: "btn sm", disabled: full ? "" : null, onclick: () => addProbe(ctx, profile)}, t("connectivity.add_probe")),
    full ? el("span", {class: "muted", style: "font-size:12px"}, t("connectivity.limit_note", {max: limit()})) : null);
}

function rulePart(ctx, rule){
  const {t, el} = ctx;
  const agg = el("select", {class: "btn", style: "width:100%"},
    ...AGGS.map(v => el("option", {value: v, selected: (rule.aggregate || "any_healthy") === v ? "" : null}, t("connectivity.agg." + v))));
  agg.onchange = () => { rule.aggregate = agg.value; if (rule.aggregate !== "quorum") delete rule.quorum; else rule.quorum = rule.quorum || 2; touch(ctx, true); };
  const cells = [field(ctx, t("connectivity.f_rule"), agg, t("connectivity.h_agg." + (rule.aggregate || "any_healthy")))];
  if (rule.aggregate === "quorum")
    cells.push(field(ctx, t("connectivity.f_quorum"), num(ctx, rule, "quorum", {min: 1, max: limit()}), t("connectivity.h_quorum")));
  cells.push(field(ctx, t("connectivity.f_fail_after"), num(ctx, rule, "fail_after", {placeholder: String(C.settings.fail_after), min: 1, max: 20}, true), t("connectivity.h_fail_after")),
    field(ctx, t("connectivity.f_ok_after"), num(ctx, rule, "ok_after", {placeholder: String(C.settings.ok_after), min: 1, max: 20}, true), t("connectivity.h_ok_after")));
  return el("details", {style: "margin:6px 0"},
    el("summary", {class: "muted", style: "cursor:pointer;font-size:12.5px"}, t("connectivity.rule_summary", {rule: t("connectivity.agg." + (rule.aggregate || "any_healthy"))})),
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;margin-top:8px;align-items:flex-start"}, ...cells));
}

function drawProfiles(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netProfiles");
  box.innerHTML = "";
  LIVE.profiles = {};
  Object.keys(LIVE.probes).forEach(k => { if (!C.probes.some(p => p.id === k)) delete LIVE.probes[k]; });
  box.append(el("h2", {style: "margin:18px 0 8px"}, t("connectivity.profiles_title")));
  box.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 10px"}, t("connectivity.profiles_about")));
  C.profiles.forEach(p => {
    const chip = el("span", {});
    LIVE.profiles[p.name] = chip;
    const name = el("input", {class: "btn mono", style: "width:200px;font-weight:600"});
    name.value = p.name;
    name.onchange = () => {
      const nm = name.value.trim();
      const clash = C.profiles.some(x => x !== p && x.name.toLowerCase() === nm.toLowerCase()) || nm.toLowerCase() === NET;
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$/.test(nm) || clash){
        name.value = p.name; ctx.toast(t(clash ? "connectivity.err.dup_profile" : "connectivity.err.bad_name"), "warn"); return;
      }
      probesOf(p.name).forEach(x => { x.profile = nm; });
      p.name = nm; touch(ctx, true);
    };
    const note = text(ctx, p, "note", {placeholder: t("connectivity.ph_note")});
    box.append(el("div", {class: "card", style: "padding:14px;margin-bottom:12px"},
      el("div", {class: "row", style: "gap:10px;margin-bottom:6px"},
        name, chip, ((snapProfile(p.name) || {}).required === false)
          ? el("span", {class: "chip", title: t("connectivity.not_required_hint")}, t("connectivity.not_required")) : null,
        el("span", {style: "flex:1"}),
        el("button", {class: "btn sm danger", onclick: () => {
          const n = probesOf(p.name).length;
          if (!confirm(t("connectivity.profile_drop_ask", {name: p.name, n}))) return;
          C.profiles = C.profiles.filter(x => x !== p);
          C.probes = C.probes.filter(x => x.profile !== p.name);
          touch(ctx, true);
        }}, t("connectivity.drop"))),
      field(ctx, t("connectivity.f_note"), note, t("connectivity.h_note")),
      rulePart(ctx, p),
      ...probesOf(p.name).map(x => probeRow(ctx, x)),
      !probesOf(p.name).length ? el("div", {class: "muted", style: "font-size:12.5px;padding:6px 0"}, t("connectivity.no_probes")) : null,
      addProbeButton(ctx, p.name)));
  });
  if (!emptyState() || WIZ)
    box.append(el("button", {class: "btn", onclick: () => { startWizard("profile"); WIZ_OFF = false; drawWizard(ctx); }}, t("connectivity.add_profile")));
  drawLive(ctx);
}

function drawInternet(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netInternet");
  box.innerHTML = "";
  const rows = probesOf(NET);
  const chip = el("span", {});
  LIVE.profiles[NET] = chip;
  box.append(el("h2", {style: "margin:22px 0 8px"}, t("connectivity.internet_title")));
  box.append(el("div", {class: "card", style: "padding:14px"},
    el("p", {class: "muted", style: "font-size:13px;margin:0 0 8px"}, t("connectivity.internet_about")),
    el("div", {class: "row", style: "gap:8px;margin-bottom:6px"}, el("b", {}, t("connectivity.internet_name")), chip),
    !rows.length ? el("p", {class: "muted", style: "font-size:12.5px;margin:6px 0"}, t("connectivity.internet_none")) : null,
    rulePart(ctx, C.internet),
    ...rows.map(x => probeRow(ctx, x)),
    el("div", {class: "row", style: "gap:8px;margin-top:8px"},
      el("button", {class: "btn sm", onclick: () => { startWizard(NET); WIZ_OFF = false; drawWizard(ctx); }}, t("connectivity.internet_wizard")),
      rows.length ? addProbeButton(ctx, NET) : null)));
  drawLive(ctx);
}

// Метки состояния профилей и результаты probe обновляет опрос; поля ввода он не трогает.
function drawLive(ctx){
  for (const [name, node] of Object.entries(LIVE.profiles)){
    const sp = snapProfile(name);
    node.innerHTML = "";
    node.append(stateChip(ctx, sp ? sp.state : "unknown"));
    if (sp && sp.trend && (sp.trend.worse || sp.trend.better)){
      const more = sp.trend.worse ? ctx.t("connectivity.trend_worse", {n: sp.trend.worse, of: sp.fail_after})
                                  : ctx.t("connectivity.trend_better", {n: sp.trend.better, of: sp.ok_after});
      node.append(ctx.el("span", {class: "muted", style: "margin-left:8px;font-size:12px"}, more));
    }
    if (sp && sp.checked) node.append(ctx.el("span", {class: "muted", style: "margin-left:8px;font-size:12px"}, ctx.fmt.when(sp.checked)));
  }
  Object.keys(LIVE.probes).forEach(id => paintProbe(ctx, id));
}

/* ---------------------------------------------------------------- общие настройки и политика */

function drawSettings(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netSettings");
  box.innerHTML = "";
  const pol = el("select", {class: "btn", style: "width:100%"},
    ...POLICIES.map(v => el("option", {value: v, selected: C.policy.auto === v ? "" : null}, t("connectivity.policy." + v))));
  pol.onchange = () => { C.policy.auto = pol.value; touch(ctx, true); };
  box.append(el("h2", {style: "margin:22px 0 8px"}, t("connectivity.settings_title")));
  box.append(el("div", {class: "card", style: "padding:14px"},
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;align-items:flex-start"},
      field(ctx, t("connectivity.f_interval"), num(ctx, C.settings, "interval", {min: 10}), t("connectivity.h_interval_all")),
      field(ctx, t("connectivity.f_timeout"), num(ctx, C.settings, "timeout", {min: 1, max: 30}), t("connectivity.h_timeout_all")),
      field(ctx, t("connectivity.f_fail_after"), num(ctx, C.settings, "fail_after", {min: 1, max: 20}), t("connectivity.h_fail_after_all")),
      field(ctx, t("connectivity.f_ok_after"), num(ctx, C.settings, "ok_after", {min: 1, max: 20}), t("connectivity.h_ok_after_all"))),
    el("div", {class: "sep"}),
    field(ctx, t("connectivity.f_policy"), pol, t("connectivity.h_policy." + C.policy.auto)),
    el("div", {class: "muted", style: "font-size:12px;margin-top:8px"}, t("connectivity.policy_note"))));
}

/* ---------------------------------------------------------------- провайдеры и журнал */

function drawProviders(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netProviders");
  box.innerHTML = "";
  const rows = snap().providers || [];
  box.append(el("h2", {style: "margin:22px 0 8px"}, t("connectivity.providers_title")));
  box.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 8px"}, t("connectivity.providers_about")));
  if (!rows.length) box.append(el("div", {class: "muted", style: "font-size:12.5px"}, t("connectivity.providers_none")));
  rows.forEach(r => box.append(el("div", {class: "list-item"},
    el("span", {class: "mono", style: "font-weight:600"}, r.name || r.id),
    el("span", {class: "chip"}, r.profile ? t("connectivity.prov_profile", {name: r.profile}) : t("connectivity.prov_novpn")),
    r.check !== "off" ? el("span", {class: "chip"}, t("connectivity.prov_check." + r.check)) : null,
    stateChip(ctx, r.state === "online" ? "online" : r.state),
    r.why ? el("span", {class: "muted", style: "font-size:12px"}, t("connectivity.prov_why." + r.why)) : null)));
  box.append(el("button", {class: "btn sm", style: "margin-top:8px", onclick: () => ctx.show("models")}, t("connectivity.providers_open")));
}

function drawEvents(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netEvents");
  box.innerHTML = "";
  const rows = (snap().events || []).slice().reverse();
  box.append(el("h2", {style: "margin:22px 0 8px"}, t("connectivity.events_title")));
  if (!rows.length) return box.append(el("div", {class: "muted", style: "font-size:12.5px"}, t("connectivity.events_none")));
  rows.forEach(e => box.append(el("div", {class: "list-item", style: "font-size:12.5px"},
    el("span", {class: "muted"}, ctx.fmt.when(e.at)),
    el("span", {class: "mono"}, e.scope === "global" ? t("connectivity.events_kit") : e.name),
    el("span", {}, (e.scope === "global" ? t("connectivity.gchip." + e.from) : t("connectivity.state." + e.from))
      + " → " + (e.scope === "global" ? t("connectivity.gchip." + e.to) : t("connectivity.state." + e.to))))));
}

/* ---------------------------------------------------------------- запись */

function drawBar(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#netBar");
  box.innerHTML = "";
  box.append(...[
    el("button", {class: "btn primary", disabled: DIRTY ? null : "", onclick: () => save(ctx)}, t("connectivity.save")),
    el("button", {class: "btn", disabled: DIRTY ? null : "",
      onclick: async () => { DIRTY = false; WIZ = null; await refresh(ctx); }}, t("connectivity.revert")),
    DIRTY ? el("span", {class: "chip warn"}, t("connectivity.unsaved")) : null].filter(Boolean));
}

function payload(){
  // Служебных полей страницы нет: уходит ровно то, что показано, а лишнее сервер переносит из файла сам.
  const send = clone(C);
  delete send.notes; delete send.error; delete send.no_kit;
  return send;
}

async function save(ctx, replace){
  const {t} = ctx;
  const r = await ctx.api("/api/net/save", {method: "POST", quiet: true,
    body: JSON.stringify({config: payload(), expect: DATA.stamp, replace: replace === true,
                          project: ctx.project ? ctx.project.path : ""})});
  if (r && r.ok){
    ctx.toast(t("connectivity.saved"), "ok");
    DIRTY = false;
    if (r.state){ DATA = r.state; C = clone(r.state.config); }
    draw(ctx);
    return;
  }
  if (r && r.code === "broken_file"){
    if (confirm(t("connectivity.err.broken_file"))) return save(ctx, true);
    return;
  }
  if (r && r.conflict){
    ctx.toast(t("connectivity.err.conflict"), "err");
    return;
  }
  const first = r && r.errors && r.errors[0];
  ctx.toast(first ? problemText(ctx, first) : (r && r.code ? problemText(ctx, r) : t("connectivity.save_failed")), "err");
}

// Состояние живёт на сервере и обновляется опросом, пока раздел на виду: скрытому перерисовываться незачем.
export const lifecycle = 1;
export default {mount, refresh, onShow, onHide};
