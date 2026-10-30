/* Код — раздел-модуль: репозитории кода проекта и их клоны в Code/.

   Работу делает движок: реестр `code_repos:` пишет `code_repos.py` (со слепком — чужая правка не затирается,
   расхождение — 409), клонирует и обновляет команда `code:sync`, состояние, языки и размер считает `code:status`.
   Страница показывает и правит реестр, а синхронизацию запускает заданием панели — оно видно в «Консоли».
   Секреты приходят маской: маска значит «не трогали». */

const MASK = "••••••";
const NO_CRED = {auth: "system", user: "", secret: "", ssh_key: "", ssh_key_text: ""};
const FIELDS = ["url", "provider", "branch", "pin", "include", "exclude", "access", "models", "publish"];

let CTX = null;
let D = null, FORM = null, CREDS = {}, DIRTY = false, OPEN = "", PROJECT = "", ADD = null, MSG = null;

export function mount(ctx){
  ctx.root.dataset.module = "code";
}

export async function refresh(ctx){
  if (!ctx.project) return;
  if (PROJECT !== ctx.project.path){
    PROJECT = ctx.project.path;
    DIRTY = false; OPEN = ""; ADD = null; CREDS = {};
  }
  MSG = null;                  // сообщение прошлой записи к новому показу раздела не относится
  await load(ctx, true);
}

const clone = x => JSON.parse(JSON.stringify(x));

async function load(ctx, keepForm){
  const path = ctx.project.path;
  const d = await ctx.api("/api/code?project=" + encodeURIComponent(path), {quiet: true});
  // Проект успели сменить, пока шёл запрос: ответ про прежний, форму он не трогает.
  if (!ctx.project || ctx.project.path !== path) return;
  if (!d || d.error || !Array.isArray(d.repos)){
    ctx.$("#codeBody").textContent = (d && d.error) || ctx.t("code.load_failed");
    return;
  }
  D = d;
  if (!(keepForm && DIRTY)){
    FORM = {};
    d.repos.forEach(r => { FORM[r.name] = Object.fromEntries(FIELDS.map(f => [f, r[f]])); });
    CREDS = {};
    DIRTY = false;
  }
  draw(ctx);
}

function draw(ctx){
  CTX = ctx;
  const box = ctx.$("#codeBody");
  box.innerHTML = "";
  if (MSG) box.append(message(ctx));
  box.append(addCard(ctx));
  const names = Object.keys(FORM).sort((a, b) => a.localeCompare(b));
  if (!names.length) box.append(ctx.el("div", {class: "card muted", style: "padding:18px"}, ctx.t("code.empty")));
  names.forEach(n => box.append(repoCard(ctx, n)));
  box.append(footer(ctx));
}

/* ---------------------------------------------------------------- сообщения */

function message(ctx){
  const {t, el} = ctx;
  const m = MSG;
  const kids = [el("div", {}, m.text)];
  if (m.problems) m.problems.forEach(p => kids.push(el("div", {style: "font-size:12.5px"},
    "· " + (p.name ? p.name + ": " : "") + fieldName(ctx, p.field) + " — " + errText(ctx, p.code))));
  if (m.conflict) kids.push(el("div", {class: "row", style: "gap:8px;margin-top:8px"},
    el("button", {class: "btn sm", onclick: () => { MSG = null; DIRTY = false; load(ctx, false); }}, t("code.take_disk")),
    el("button", {class: "btn sm", onclick: () => save(ctx, true)}, t("code.keep_mine"))));
  return el("div", {class: m.kind === "ok" ? "note" : "warnbox", style: "margin-bottom:12px"}, ...kids);
}

function fieldName(ctx, f){
  const s = ctx.t("code.field." + f);
  return s === "code.field." + f ? f : s;
}

function errText(ctx, code){
  const s = ctx.t("code.err." + code);
  return s === "code.err." + code ? code : s;
}

// Неудача git приходит кодом с текстом движка; известные коды — словами каталога, остальное — как прислал движок.
function problemText(ctx, p){
  if (!p) return "";
  const s = ctx.t("code.p." + p.code);
  return s === "code.p." + p.code ? [p.title, p.fix].filter(Boolean).join(" — ") : s;
}

/* ---------------------------------------------------------------- добавление строкой git clone */

const add = () => ADD || (ADD = {text: "", parsed: null, name: "", cred: {...NO_CRED}, models: false, publish: false,
  busy: false, check: null, timer: null});

function addCard(ctx){
  const {t, el} = ctx;
  const a = add();
  const text = el("input", {class: "btn mono", style: "width:100%;font-weight:400", autocomplete: "off",
    placeholder: "git clone https://git.example.com/team/service.git"});
  text.value = a.text;
  text.oninput = () => {
    a.text = text.value; a.check = null;
    clearTimeout(a.timer);
    a.timer = setTimeout(() => parseAdd(ctx), 300);
  };
  return el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("code.add.title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("code.add.about")),
    text, el("div", {id: "codeAdd"}, ...addBody(ctx)));
}

function drawAdd(ctx){
  const box = ctx.$("#codeAdd");
  if (box){ box.innerHTML = ""; box.append(...addBody(ctx)); }
}

async function parseAdd(ctx){
  const a = add();
  if (!a.text.trim()){ a.parsed = null; return drawAdd(ctx); }
  const r = await ctx.api("/api/code/parse", {method: "POST", quiet: true, body: JSON.stringify({text: a.text})});
  a.parsed = r || {ok: false, code: "bad_url"};
  if (a.parsed.ok){
    a.cred = {...a.cred, auth: a.parsed.auth, user: a.parsed.user || a.cred.user, secret: a.parsed.secret || a.cred.secret};
    if (!a.name) a.name = a.parsed.name || "";
  }
  drawAdd(ctx);
}

function addBody(ctx){
  const {t, el} = ctx;
  const a = add(), p = a.parsed;
  if (!p) return [];
  if (!p.ok) return [el("div", {style: "font-size:12.5px;color:var(--danger);margin-top:8px"}, errText(ctx, p.code))];
  const out = [el("div", {class: "row", style: "gap:6px;flex-wrap:wrap;margin:10px 0"},
    el("span", {class: "chip ok"}, t("code.prov." + p.provider)),
    el("span", {class: "chip mono"}, p.url),
    p.branch ? el("span", {class: "chip"}, t("code.branch_chip", {branch: p.branch})) : null)];
  if (p.secret_in_url) out.push(el("div", {class: "note", style: "margin-bottom:10px"}, t("code.add.secret_moved")));
  const name = el("input", {class: "btn mono", style: "width:100%;font-weight:400", value: a.name});
  name.oninput = () => { a.name = name.value.trim(); };
  out.push(field(ctx, t("code.field.name"), name, t("code.h.name")));
  out.push(...authFields(ctx, a.cred, p.provider, () => drawAdd(ctx)));
  out.push(permRow(ctx, a, () => {}));
  out.push(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"},
    el("button", {class: "btn primary", disabled: a.busy ? "" : null, onclick: () => addRepo(ctx, false)}, t("code.add.go")),
    a.busy ? el("span", {class: "spin"}) : null,
    a.check && !a.check.ok && !a.busy ? el("button", {class: "btn", onclick: () => addRepo(ctx, true)}, t("code.add.anyway")) : null));
  if (a.check) out.push(checkView(ctx, a.check));
  return out;
}

async function addRepo(ctx, force){
  const a = add(), p = a.parsed;
  if (!p || !p.ok || a.busy) return;
  if (!a.name){ MSG = {kind: "err", text: ctx.t("code.err.name_empty")}; return draw(ctx); }
  if (Object.keys(FORM).some(n => n.toLowerCase() === a.name.toLowerCase())){
    MSG = {kind: "err", text: ctx.t("code.err.name_duplicate")}; return draw(ctx);
  }
  const rec = {url: p.url, provider: p.provider, branch: p.branch || "", pin: "", include: "", exclude: "",
               access: "", models: a.models, publish: a.publish};
  if (!force){
    a.busy = true; a.check = null; drawAdd(ctx);
    a.check = await ctx.api("/api/code/check", {method: "POST", quiet: true,
      body: JSON.stringify({project: ctx.project.path, name: a.name, repo: rec, credentials: a.cred})}) || {ok: false};
    a.busy = false;
    if (!a.check.ok) return drawAdd(ctx);
  }
  FORM[a.name] = rec;
  CREDS[a.name] = {...a.cred};
  DIRTY = true;
  const ok = await save(ctx, false);
  if (ok) ADD = null;
  draw(ctx);
}

/* ---------------------------------------------------------------- вход */

function authFields(ctx, c, provider, redraw){
  const {t, el} = ctx;
  const method = el("select", {class: "btn", style: "width:100%"},
    ...((D && D.options && D.options.auths) || ["system", "token", "password", "ssh"])
      .map(v => el("option", {value: v, selected: c.auth === v ? "" : null}, t("code.auth." + v))));
  method.onchange = () => { c.auth = method.value; touch(); redraw(); };
  const out = [field(ctx, t("code.field.auth"), method, t("code.h.auth." + c.auth))];
  if (c.auth === "token" || c.auth === "password"){
    const user = el("input", {class: "btn", style: "width:100%;font-weight:400", autocomplete: "off", value: c.user || ""});
    user.oninput = () => { c.user = user.value.trim(); touch(); };
    const had = c.secret === MASK;
    const sec = el("input", {class: "btn mono", type: "password", autocomplete: "new-password",
      style: "width:100%;font-weight:400", placeholder: had ? t("code.secret_set") : ""});
    if (!had) sec.value = c.secret || "";
    sec.oninput = () => { c.secret = sec.value || (had ? MASK : ""); touch(); };
    const bb = provider === "bitbucket";
    out.push(el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:flex-start"},
      field(ctx, t("code.field.user"), user, bb ? t("code.h.user_bitbucket") : t("code.h.user")),
      field(ctx, c.auth === "token" ? t("code.field.token") : t("code.field.password"), sec,
        had ? t("code.h.secret_keep") : bb ? t("code.h.secret_bitbucket") : t("code.h.secret"))));
  } else if (c.auth === "ssh"){
    const key = el("input", {class: "btn mono", style: "width:100%;font-weight:400", placeholder: "~/.ssh/id_ed25519",
      value: c.ssh_key || ""});
    key.oninput = () => { c.ssh_key = key.value.trim(); touch(); };
    out.push(field(ctx, t("code.field.ssh_key"), pick(ctx, key, {preset: "ssh"}), t("code.h.ssh_key")));
  }
  return out;
}

// Поле пути с кнопкой «Выбрать…» — из ядра (`ctx.ui.pathField`); у ядра вкладки, открытой до обновления кита, её
// нет — тогда остаётся одно поле.
const pick = (ctx, input, opts) => ctx.ui && ctx.ui.pathField ? ctx.ui.pathField(input, opts) : input;

// Кнопки решаются при отрисовке, а ввод раздел не перерисовывает (поле потеряло бы фокус): правка сразу включает
// «Сохранить» и «Отменить» и гасит синхронизацию того, что ещё не сохранено.
function touch(){
  DIRTY = true;
  if (!CTX) return;
  CTX.$$('[data-dirty="on"]').forEach(b => { b.disabled = false; });
  CTX.$$('[data-dirty="off"]').forEach(b => { b.disabled = true; });
}

function field(ctx, label, control, hint){
  const {el} = ctx;
  return el("label", {style: "display:block;flex:1;min-width:220px;margin-bottom:10px"},
    el("div", {class: "muted", style: "font-size:12px;margin-bottom:3px"}, label), control,
    hint ? el("div", {class: "muted", style: "font-size:11.5px;margin-top:3px"}, hint) : null);
}

function permRow(ctx, obj, onChange){
  const {t, el} = ctx;
  const box = (key) => {
    const c = el("input", {type: "checkbox", checked: obj[key] ? "" : null});
    c.onchange = () => { obj[key] = c.checked; touch(); onChange(); };
    return el("label", {style: "display:flex;gap:8px;align-items:flex-start;flex:1;min-width:260px;cursor:pointer"}, c,
      el("div", {style: "flex:1"}, el("div", {}, t("code.perm." + key)),
        el("div", {class: "muted", style: "font-size:11.5px"}, t("code.perm." + key + "_hint"))));
  };
  return el("div", {class: "row", style: "gap:14px;flex-wrap:wrap;align-items:flex-start;margin:4px 0 12px"},
    box("models"), box("publish"));
}

function checkView(ctx, check){
  const {t, el} = ctx;
  if (check.ok) return el("div", {class: "note", style: "margin-top:10px"},
    check.empty ? t("code.check.empty") : t("code.check.ok", {n: (check.branches || []).length}));
  const kids = [el("b", {}, t("code.check.bad"))];
  if (check.problem) kids.push(el("div", {style: "font-size:12.5px;margin-top:4px"}, problemText(ctx, check.problem)));
  (check.problems || []).forEach(p => kids.push(el("div", {style: "font-size:12.5px"},
    "· " + fieldName(ctx, p.field) + " — " + errText(ctx, p.code))));
  return el("div", {class: "warnbox", style: "margin-top:10px"}, ...kids);
}

/* ---------------------------------------------------------------- репозиторий */

function repoRow(name){
  return (D.repos || []).find(r => r.name === name) || {name, status: {}, credentials: {...NO_CRED}, cloned: false};
}

function repoCard(ctx, name){
  const {t, el} = ctx;
  const rec = FORM[name], row = repoRow(name), st = row.status || {};
  const saved = (D.repos || []).some(r => r.name === name);
  const chips = [el("span", {class: "chip " + stateTone(st.state)}, t("code.state." + (saved ? (st.state || "missing") : "unsaved")))];
  if (st.rev) chips.push(el("span", {class: "chip mono"}, st.rev.slice(0, 8)));
  if (st.date) chips.push(el("span", {class: "chip"}, t("code.rev_date", {date: String(st.date).slice(0, 10)})));
  if (st.behind > 0) chips.push(el("span", {class: "chip warn"},
    t(rec.pin ? "code.ahead_of_pin" : "code.behind", {n: st.behind})));
  if (st.behind === 0) chips.push(el("span", {class: "chip ok"}, t("code.even")));
  if (rec.models) chips.push(el("span", {class: "chip"}, t("code.chip.models")));
  if (rec.publish) chips.push(el("span", {class: "chip"}, t("code.chip.publish")));
  const last = st.last || {};
  const body = [
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"},
      el("b", {class: "mono"}, name), ...chips),
    el("div", {class: "muted mono", style: "font-size:12px;margin:6px 0"}, rec.url +
      (rec.pin ? "  @" + rec.pin : rec.branch ? "  ⎇ " + rec.branch : "")),
  ];
  if (last.at) body.push(el("div", {class: "muted", style: "font-size:12px"},
    t(last.ok ? "code.last_ok" : "code.last_bad", {when: ctx.fmt.when(last.at), code: last.problem || ""})));
  if (st.dirty) body.push(el("div", {class: "warnbox", style: "margin:8px 0"}, t("code.dirty", {n: st.dirty})));
  if (st.state === "foreign") body.push(el("div", {class: "warnbox", style: "margin:8px 0"}, t("code.foreign")));
  if (st.path_overflow) body.push(el("div", {class: "warnbox", style: "margin:8px 0"}, t("code.long_paths")));
  body.push(statsView(ctx, st.stats));
  body.push(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:10px"},
    el("button", {class: "btn sm primary", "data-dirty": "off", disabled: saved && !DIRTY ? null : "", title: DIRTY ? t("code.save_first") : null,
      onclick: () => runSync(ctx, name, false)}, t("code.sync")),
    st.state === "dirty" || st.state === "foreign" ? el("button", {class: "btn sm", "data-dirty": "off", disabled: DIRTY ? "" : null,
      onclick: () => runSync(ctx, name, true)}, t("code.reset")) : null,
    el("button", {class: "btn sm", onclick: () => { OPEN = OPEN === name ? "" : name; draw(ctx); }},
      OPEN === name ? t("code.edit_close") : t("code.edit")),
    el("button", {class: "btn sm", onclick: () => removeRepo(ctx, name)}, t("code.remove"))));
  if (OPEN === name) body.push(editForm(ctx, name));
  return el("div", {class: "card", style: "padding:16px 18px;margin-bottom:12px"}, ...body);
}

function stateTone(state){
  return state === "ok" ? "ok" : state === "dirty" || state === "foreign" ? "bad" : "warn";
}

function statsView(ctx, s){
  const {t, el} = ctx;
  if (!s || !s.files) return el("div", {class: "muted", style: "font-size:12px;margin-top:6px"}, t("code.no_stats"));
  const langs = Object.entries(s.languages || {});
  const rows = langs.slice(0, 8).map(([lang, v]) => el("span", {class: "chip"},
    t("code.lang_row", {lang, files: v.files, lines: v.lines})));
  if (langs.length > 8) rows.push(el("span", {class: "chip muted"}, t("code.more_langs", {n: langs.length - 8})));
  const skipped = Object.entries(s.skipped || {}).map(([k, v]) => t("code.skip." + k) + " " + v).join(", ");
  return el("div", {style: "margin-top:8px"},
    el("div", {class: "row", style: "gap:6px;flex-wrap:wrap"}, ...rows),
    el("div", {class: "muted", style: "font-size:12px;margin-top:6px"},
      t("code.size", {files: s.files, counted: s.counted, size: ctx.fmt.kb(s.bytes || 0), other: s.other || 0})
      + (skipped ? " · " + t("code.skipped", {list: skipped}) : "")));
}

function editForm(ctx, name){
  const {t, el} = ctx;
  const rec = FORM[name];
  const row = repoRow(name);
  if (!CREDS[name]) CREDS[name] = {...NO_CRED, ...(row.credentials || {}), ssh_key_text: ""};
  const input = (key, attrs) => {
    const x = el("input", {class: "btn mono", style: "width:100%;font-weight:400", ...(attrs || {})});
    x.value = rec[key] == null ? "" : String(rec[key]);
    x.oninput = () => { rec[key] = x.value.trim(); touch(); };
    return x;
  };
  const prov = el("select", {class: "btn", style: "width:100%"},
    ...((D.options && D.options.providers) || []).map(v => el("option", {value: v, selected: rec.provider === v ? "" : null},
      t("code.prov." + v))));
  prov.onchange = () => { rec.provider = prov.value; touch(); };
  const modes = ["", "internet", "local", ...(D.profiles || []).map(p => "vpn:" + p)];
  if (rec.access && !modes.includes(rec.access)) modes.push(rec.access);
  // Маска по папке клона: выбранная папка дописывается маской `папка/**` через запятую. Клона нет — выбирать не из чего,
  // кнопки нет (маска набирается руками, как прежде).
  const base = row.path || "Code/" + name;
  const mask = (key, attrs) => row.cloned ? pick(ctx, input(key, attrs), {want: "dir", start: base, base, multi: true,
    title: t("code.pick_mask"),
    put: (list, old) => {
      const have = String(old || "").split(",").map(x => x.trim()).filter(Boolean);
      const add = list.map(p => p.slice(base.length + 1) + "/**").filter(m => !have.includes(m));
      return [...have, ...add].join(", ");
    }}) : input(key, attrs);
  const access = el("select", {class: "btn", style: "width:100%"},
    ...modes.map(v => el("option", {value: v, selected: rec.access === v ? "" : null},
      v.startsWith("vpn:") ? t("code.access.vpn", {name: v.slice(4)}) : t("code.access." + (v || "none")))));
  access.onchange = () => { rec.access = access.value; touch(); };
  return el("div", {style: "margin-top:14px;border-top:1px solid var(--border);padding-top:12px"},
    field(ctx, t("code.field.url"), input("url"), t("code.h.url")),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:flex-start"},
      field(ctx, t("code.field.provider"), prov, ""),
      field(ctx, t("code.field.branch"), input("branch", {placeholder: "main"}), t("code.h.branch")),
      field(ctx, t("code.field.pin"), input("pin"), t("code.h.pin"))),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:flex-start"},
      field(ctx, t("code.field.include"), mask("include"), t("code.h.include")),
      field(ctx, t("code.field.exclude"), mask("exclude", {placeholder: "**/generated/**, *.min.js"}), t("code.h.exclude"))),
    field(ctx, t("code.field.access"), access, t("code.h.access")),
    permRow(ctx, rec, () => draw(ctx)),
    ...authFields(ctx, CREDS[name], rec.provider, () => draw(ctx)),
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap"},
      el("button", {class: "btn sm primary", "data-dirty": "on", disabled: DIRTY ? null : "", onclick: () => save(ctx, false)}, t("code.save")),
      el("button", {class: "btn sm", onclick: () => checkRepo(ctx, name)}, t("code.check")),
      el("button", {class: "btn sm", "data-dirty": "on", disabled: DIRTY ? null : "", onclick: () => { DIRTY = false; OPEN = ""; load(ctx, false); }},
        t("code.cancel"))),
    row.check ? checkView(ctx, row.check) : null);
}

async function checkRepo(ctx, name){
  const row = repoRow(name);
  row.check = await ctx.api("/api/code/check", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, name, repo: FORM[name], credentials: CREDS[name] || null})}) || {ok: false};
  draw(ctx);
}

function removeRepo(ctx, name){
  if (!confirm(ctx.t("code.remove_ask", {name}))) return;
  delete FORM[name];
  delete CREDS[name];
  if (OPEN === name) OPEN = "";
  DIRTY = true;
  save(ctx, false);
}

/* ---------------------------------------------------------------- запись реестра */

async function save(ctx, force){
  const r = await ctx.api("/api/code/repos", {method: "POST", quiet: true, body: JSON.stringify({
    project: ctx.project.path, repos: FORM, credentials: CREDS, expect: force ? "" : (D && D.digest) || ""})});
  if (r && r.ok){
    MSG = {kind: "ok", text: ctx.t("code.saved")};
    DIRTY = false; CREDS = {};
    if (r.state){ D = r.state; FORM = {}; D.repos.forEach(x => { FORM[x.name] = Object.fromEntries(FIELDS.map(f => [f, x[f]])); }); }
    draw(ctx);
    return true;
  }
  if (r && r.conflict) MSG = {kind: "err", text: ctx.t("code.conflict"), conflict: true};
  else MSG = {kind: "err", text: ctx.t("code.save_failed"), problems: (r && r.problems) || [{field: "", code: (r && r.error) || "failed"}]};
  draw(ctx);
  return false;
}

/* ---------------------------------------------------------------- синхронизация */

async function runSync(ctx, name, reset){
  if (reset && !confirm(ctx.t("code.reset_ask", {name}))) return;
  const args = ["--apply"];
  if (name) args.push("--repo=" + name);
  if (reset) args.push("--reset");
  const res = await ctx.runWatched("code:sync", args);
  if (res && res.busy) return;
  // Человек смотрит вывод в консоли — возвращать его силой нельзя; раздел обновляется молча.
  if (ctx.project && ctx.project.path === PROJECT) await load(ctx, true);
}

function footer(ctx){
  const {t, el} = ctx;
  const doc = ctx.lang === "en" ? "docs/en/code-room.md" : "docs/code-room.md";
  return el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:6px"},
    el("button", {class: "btn sm primary", "data-dirty": "off", disabled: Object.keys(FORM).length && !DIRTY ? null : "",
      onclick: () => runSync(ctx, "", false)}, t("code.sync_all")),
    el("button", {class: "btn sm", onclick: () => ctx.runWatched("code:status", [])}, t("code.status")),
    el("button", {class: "btn sm", onclick: () => ctx.openDoc(doc)}, t("code.doc")));
}

// Скрытый раздел не перерисовывается: при открытии refresh придёт сам, побочных действий у него нет.
export const lifecycle = 1;
export default {mount, refresh};
