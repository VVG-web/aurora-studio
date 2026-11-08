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
let FIND = null, PACK = null;
let ART = null;
let GURL = "", GNAME = "", GINCLUDE = true, GDISC = null, GSEL = null;
let GCRED = {auth: "system", user: "", secret: ""};
let POLL_TIMER = null;
const KIND_KEYS = {symbol: "code.index.kind.symbol", file: "code.index.kind.file",
  route: "code.index.kind.route", table: "code.index.kind.table", column: "code.index.kind.column",
  topic: "code.index.kind.topic", config: "code.index.kind.config", string: "code.index.kind.string",
  test: "code.index.kind.test", reqtag: "code.index.kind.reqtag", commit: "code.index.kind.commit"};

export function mount(ctx){
  ctx.root.dataset.module = "code";
}

export async function refresh(ctx){
  if (!ctx.project) return;
  if (PROJECT !== ctx.project.path){
    PROJECT = ctx.project.path;
    DIRTY = false; OPEN = ""; ADD = null; CREDS = {}; ART = null;
    GURL = ""; GNAME = ""; GINCLUDE = true; GDISC = null; GSEL = null;
    GCRED = {auth: "system", user: "", secret: ""};
  }
  MSG = null;                  // сообщение прошлой записи к новому показу раздела не относится
  if (POLL_TIMER) { clearTimeout(POLL_TIMER); POLL_TIMER = null; }
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
  if (!ART || ART.project !== path || ART.digest !== (d.artifacts || {}).digest) {
    ART = {project: path, digest: (d.artifacts || {}).digest || "", rows: {}};
    ((d.artifacts || {}).kinds || []).forEach(k => {
      ART.rows[k.kind] = {out: k.out || "", template: k.template_path || "", enabled: !!k.enabled};
    });
  }
  const p = await ctx.api("/api/code/pack?project=" + encodeURIComponent(path), {quiet: true});
  if (!ctx.project || ctx.project.path !== path) return;
  PACK = p && !p.error ? p : {stories: [], error: (p && p.error) || ""};
  if (!(keepForm && DIRTY)){
    FORM = {};
    d.repos.forEach(r => { FORM[r.name] = Object.fromEntries(FIELDS.map(f => [f, r[f]])); });
    CREDS = {};
    DIRTY = false;
  }
  draw(ctx);
  // Полный расчёт состояния идёт в фоне: опросить через 3 секунды, если проект не сменился.
  if (d.loading && !POLL_TIMER) {
    POLL_TIMER = setTimeout(() => {
      POLL_TIMER = null;
      if (ctx.project && ctx.project.path === path) load(ctx, keepForm);
    }, 3000);
  }
}

function draw(ctx){
  CTX = ctx;
  const box = ctx.$("#codeBody");
  box.innerHTML = "";
  // Фоновый расчёт упал: показать причину, иначе раздел выглядит пустым без объяснения.
  if (D && D.failed) box.append(ctx.el("div", {class: "warnbox"}, ctx.t("code.load_failed") + ": " + D.failed));
  if (MSG) box.append(message(ctx));
  box.append(groupsCard(ctx));
  box.append(addCard(ctx));
  const names = Object.keys(FORM).sort((a, b) => a.localeCompare(b));
  if (!names.length) box.append(ctx.el("div", {class: "card muted", style: "padding:18px"}, ctx.t("code.empty")));
  names.forEach(n => box.append(repoCard(ctx, n)));
  box.append(indexCard(ctx));
  box.append(artifactsCard(ctx));
  box.append(packCard(ctx));
  box.append(footer(ctx));
}

/* ---------------------------------------------------------------- GitLab-группы */

// Группа — источник списка репозиториев. Сеть трогает только discovery: page
// открывается без запросов, а список приходит по кнопке «Найти репозитории».
function groupsCard(ctx){
  const {t, el} = ctx;
  const data = (D && D.groups) || {groups: [], problems: []};
  const card = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("code.groups.title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("code.groups.about")));
  (data.problems || []).forEach(p => card.append(el("div", {class: "warnbox", style: "margin-bottom:8px"},
    (p.name ? p.name + ": " : "") + (p.message || p.code || t("code.groups.bad")))));
  (data.groups || []).forEach(g => {
    const selected = (g.selected || []).length;
    card.append(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0"},
      el("b", {class: "mono"}, g.name),
      el("span", {class: "chip mono"}, g.url),
      el("span", {class: "chip"}, t("code.groups.selected_n", {n: selected})),
      g.include_subgroups ? el("span", {class: "chip"}, t("code.groups.subgroups")) : null,
      el("button", {class: "btn sm", onclick: () => {
        GNAME = g.name; GURL = g.url; GINCLUDE = !!g.include_subgroups;
        GCRED = {auth: GCRED.auth || "system", user: "", secret: ""};
        groupDiscover(ctx);
      }}, t("code.groups.open"))));
  });
  const url = el("input", {class: "btn mono", style: "width:100%;font-weight:400", autocomplete: "off",
    placeholder: "https://gitlab.example.com/group/subgroup"});
  url.value = GURL;
  url.oninput = () => { GURL = url.value.trim(); };
  const name = el("input", {class: "btn mono", style: "width:100%;font-weight:400", autocomplete: "off",
    placeholder: t("code.groups.name_ph")});
  name.value = GNAME;
  name.oninput = () => { GNAME = name.value.trim(); };
  const incl = el("input", {type: "checkbox", checked: GINCLUDE ? "" : null});
  incl.onchange = () => { GINCLUDE = incl.checked; };
  card.append(el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;align-items:flex-start;margin-top:10px"},
    field(ctx, t("code.groups.url"), url, t("code.groups.url_hint")),
    field(ctx, t("code.groups.name"), name, t("code.groups.name_hint")),
    el("label", {style: "display:flex;gap:8px;align-items:center;margin:22px 0 0;flex:1;min-width:220px"},
      incl, el("span", {style: "font-size:12.5px"}, t("code.groups.include")))),
  ...authFields(ctx, GCRED, "gitlab", () => draw(ctx),
    {dirty: false, auths: ["system", "token", "password", "ldap"]}),
  el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"},
    el("button", {class: "btn primary", disabled: GDISC && GDISC.busy ? "" : null,
      onclick: () => groupDiscover(ctx)}, t("code.groups.discover")),
    GDISC && GDISC.busy ? el("span", {class: "spin"}) : null));
  if (GDISC && !GDISC.busy) card.append(discoveryView(ctx));
  return card;
}

function discoveryView(ctx){
  const {t, el} = ctx;
  if (!GDISC.ok){
    const out = [el("div", {class: "warnbox", style: "margin-top:12px"},
      t("code.groups.failed") + ": " + (GDISC.error || "error"),
      GDISC.hint ? el("div", {style: "font-size:12.5px;margin-top:4px"}, GDISC.hint) : null,
      GDISC.detail ? el("div", {class: "muted mono", style: "font-size:11.5px;margin-top:4px"}, GDISC.detail) : null)];
    (GDISC.problems || []).forEach(p => out.push(el("div", {style: "font-size:12.5px"},
      "· " + (p.name ? p.name + ": " : "") + (p.code || ""))));
    return el("div", {}, ...out);
  }
  const projects = GDISC.projects || [];
  const rows = projects.map(p => {
    const cb = el("input", {type: "checkbox", checked: GSEL.has(p.path) ? "" : null});
    cb.onchange = () => {
      if (cb.checked) GSEL.add(p.path); else GSEL.delete(p.path);
      const btn = ctx.$("#groupSave");
      if (btn) btn.textContent = t("code.groups.save_n", {n: GSEL.size});
    };
    return el("label", {style: "display:flex;gap:9px;align-items:flex-start;padding:7px 0;border-top:1px solid var(--border);cursor:pointer"},
      cb,
      el("div", {style: "flex:1;min-width:0"},
        el("div", {class: "row", style: "gap:7px;flex-wrap:wrap;align-items:center"},
          el("span", {class: "mono", style: "font-size:12.5px;overflow-wrap:anywhere"}, p.path),
          p.new ? el("span", {class: "chip ok"}, t("code.groups.new")) : null,
          p.default_branch ? el("span", {class: "chip"}, p.default_branch) : null),
        el("div", {class: "muted", style: "font-size:11.5px"},
          [p.last_activity_at ? t("code.groups.activity", {date: String(p.last_activity_at).slice(0, 10)}) : "",
           p.created_at ? t("code.groups.created", {date: String(p.created_at).slice(0, 10)}) : ""]
            .filter(Boolean).join(" · "))));
  });
  const out = [el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px"},
    el("b", {}, t("code.groups.found", {n: projects.length})),
    GDISC.new ? el("span", {class: "chip ok"}, t("code.groups.new_n", {n: GDISC.new})) : null,
    GDISC.auth ? el("span", {class: "chip ok"}, t("code.groups.authed")) : el("span", {class: "chip warn"}, t("code.groups.noauth")))];
  if (!projects.length) out.push(el("div", {class: "muted", style: "margin-top:8px"}, t("code.groups.empty")));
  out.push(el("div", {style: "max-height:380px;overflow:auto;margin-top:6px"}, ...rows));
  out.push(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center"},
    el("button", {class: "btn primary", id: "groupSave",
      onclick: () => saveGroup(ctx)}, t("code.groups.save_n", {n: GSEL.size})),
    el("button", {class: "btn", onclick: () => { GDISC = null; draw(ctx); }}, t("code.groups.close"))));
  return el("div", {}, ...out);
}

async function groupDiscover(ctx){
  const name = GNAME || (GURL ? GURL.replace(/\/+$/, "").split("/").pop().replace(/\.git$/i, "") : "");
  if (!GURL){ ctx.toast(ctx.t("code.groups.url_required"), "err"); return; }
  if (!name){ ctx.toast(ctx.t("code.groups.name_required"), "err"); return; }
  GDISC = {busy: true};
  draw(ctx);
  const existing = ((D && D.groups && D.groups.groups) || []).find(g => g.name === name);
  const credentials = GCRED.auth === "system" || !GCRED.secret ? null
    : {auth: GCRED.auth, user: GCRED.user, secret: GCRED.secret};
  const r = await ctx.api("/api/code/groups/discover", {method: "POST", quiet: true, body: JSON.stringify({
    project: ctx.project.path, name, url: GURL, include_subgroups: GINCLUDE,
    credentials})}) || {ok: false, error: "network"};
  GDISC = r;
  GSEL = new Set(r && r.ok ? ((existing && existing.selected) || []).filter(p =>
    (r.projects || []).some(x => x.path === p)) : []);
  draw(ctx);
}

async function saveGroup(ctx){
  if (!GDISC || !GDISC.ok) return;
  if (DIRTY){
    MSG = {kind: "err", text: ctx.t("code.groups.save_dirty")};
    draw(ctx);
    return;
  }
  const credentials = GCRED.auth === "system" || !GCRED.secret ? null
    : {auth: GCRED.auth, user: GCRED.user, secret: GCRED.secret};
  const r = await ctx.api("/api/code/groups/save", {method: "POST", quiet: true, body: JSON.stringify({
    project: ctx.project.path, name: GNAME || GDISC.group.name, url: GURL,
    include_subgroups: GINCLUDE, selected: [...GSEL], projects: GDISC.projects || [],
    repo_expect: (D && D.digest) || "",
    credentials})}) || {ok: false};
  if (!r.ok){
    MSG = {kind: "err", text: ctx.t("code.groups.save_failed") + ": " + (r.error || "error")};
    draw(ctx);
    return;
  }
  ctx.toast(ctx.t("code.groups.saved", {n: r.repos || 0}));
  GDISC = null; GCRED = {auth: "system", user: "", secret: ""};
  await load(ctx, false);
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

function authFields(ctx, c, provider, redraw, opts = {}){
  const {t, el} = ctx;
  const auths = opts.auths || ((D && D.options && D.options.auths)
    || ["system", "token", "password", "ldap", "ssh"]);
  const mark = opts.dirty === false ? () => {} : touch;
  const method = el("select", {class: "btn", style: "width:100%"},
    ...auths.map(v => el("option", {value: v, selected: c.auth === v ? "" : null}, t("code.auth." + v))));
  method.onchange = () => { c.auth = method.value; mark(); redraw(); };
  const out = [field(ctx, t("code.field.auth"), method, t("code.h.auth." + c.auth))];
  if (c.auth === "token" || c.auth === "password" || c.auth === "ldap"){
    const user = el("input", {class: "btn", style: "width:100%;font-weight:400", autocomplete: "off", value: c.user || ""});
    user.oninput = () => { c.user = user.value.trim(); mark(); };
    const had = c.secret === MASK;
    const sec = el("input", {class: "btn mono", type: "password", autocomplete: "new-password",
      style: "width:100%;font-weight:400", placeholder: had ? t("code.secret_set") : ""});
    if (!had) sec.value = c.secret || "";
    sec.oninput = () => { c.secret = sec.value || (had ? MASK : ""); mark(); };
    const bb = provider === "bitbucket";
    out.push(el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:flex-start"},
      field(ctx, t("code.field.user"), user,
        c.auth === "ldap" ? t("code.h.user.ldap")
          : bb ? t("code.h.user_bitbucket") : t("code.h.user")),
      field(ctx, c.auth === "token" ? t("code.field.token") : t("code.field.password"), sec,
        had ? t("code.h.secret_keep")
          : c.auth === "ldap" ? t("code.h.secret_ldap")
          : bb ? t("code.h.secret_bitbucket") : t("code.h.secret"))));
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
    // После сохранения полный расчёт состояния идёт в фоне: code_state отдаёт
    // loading:true с пустыми artifacts/index/profiles. Без повторного load() карточки
    // артефактов, индекса и профилей останутся пустыми до ручного обновления.
    if (r.state && r.state.loading) load(ctx, true);
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

function indexCard(ctx){
  const {t, el} = ctx;
  const idx = (D && D.index) || {repos: []};
  const box = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"});
  box.append(el("b", {}, t("code.index.title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("code.index.about")));
  if (idx.failed) box.append(el("div", {class: "warnbox"}, t("code.index.failed", {error: idx.failed})));
  const repos = idx.repos || [];
  if (!repos.length) box.append(el("div", {class: "muted", style: "font-size:12.5px"}, t("code.empty")));
  repos.forEach(r => {
    const detail = r.built
      ? [r.files, r.symbols, r.routes, r.tables, r.topics, r.commits].map(n => n || 0).join(" / ")
      : t("code.index.not_built");
    box.append(el("div", {class: "row", style: "gap:8px;align-items:center;margin:4px 0;flex-wrap:wrap"},
      el("span", {class: "chip mono"}, r.name),
      el("span", {class: "muted", style: "font-size:12.5px"}, detail)));
  });
  const q = el("input", {class: "btn", style: "flex:1;min-width:220px;font-weight:400",
    placeholder: t("code.index.search")});
  q.id = "codeFind";
  q.value = FIND ? FIND.query : "";
  const kind = el("select", {class: "btn", style: "font-weight:400"});
  kind.id = "codeFindKind";
  ["", "symbol", "file", "route", "table", "column", "topic", "config", "string", "test", "reqtag", "commit"]
    .forEach(k => {
      const o = el("option", {value: k}, k ? t(KIND_KEYS[k]) : t("code.index.all"));
      if (FIND && FIND.kind === k) o.selected = true;
      kind.append(o);
    });
  box.append(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:12px"},
    q, kind,
    el("button", {class: "btn", onclick: () => runFind(ctx)}, t("code.index.find")),
    el("button", {class: "btn primary", onclick: () => buildIndex(ctx)}, t("code.index.build"))));
  if (FIND && FIND.missing && FIND.missing.length)
    box.append(el("div", {class: "note", style: "margin-top:8px"},
      t("code.index.missing", {names: FIND.missing.join(", ")})));
  if (FIND && FIND.error) box.append(el("div", {class: "warnbox", style: "margin-top:8px"}, FIND.error));
  if (FIND && FIND.results){
    if (!FIND.results.length) box.append(el("div", {class: "muted", style: "margin-top:10px"}, t("code.index.empty")));
    else {
      const list = el("div", {style: "margin-top:10px"});
      FIND.results.forEach(r => {
        const where = r.file ? `  ${r.file}${r.line ? ":" + r.line : ""}` : "";
        list.append(el("div", {style: "font-size:12.5px;margin:3px 0"},
          el("span", {class: "chip"}, r.type), " ",
          el("span", {class: "mono"}, r.name),
          r.detail ? " — " + r.detail : "",
          where));
      });
      box.append(list);
    }
  }
  return box;
}

async function buildIndex(ctx){
  const res = await ctx.runWatched("code:index", ["--apply"]);
  if (res && res.busy) return;
  if (ctx.project && ctx.project.path === PROJECT) await load(ctx, true);
}

async function runFind(ctx){
  const q = ctx.$("#codeFind"), kind = ctx.$("#codeFindKind");
  FIND = {query: q ? q.value : "", kind: kind ? kind.value : "", results: null, error: "", missing: []};
  if (!FIND.query.trim()){ FIND.results = []; return draw(ctx); }
  const url = "/api/code/find?project=" + encodeURIComponent(ctx.project.path)
    + "&q=" + encodeURIComponent(FIND.query) + "&kind=" + encodeURIComponent(FIND.kind);
  const r = await ctx.api(url, {quiet: true}) || {};
  FIND.results = r.results || [];
  FIND.error = r.error || "";
  FIND.missing = r.missing || [];
  draw(ctx);
}

function artifactsCard(ctx){
  const {t, el} = ctx;
  const data = (D && D.artifacts) || {kinds: [], problems: []};
  const box = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"});
  box.append(el("b", {}, t("code.artifacts.title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("code.artifacts.about")));
  const kinds = data.kinds || [];
  if (!kinds.length) box.append(el("div", {class: "muted", style: "font-size:12.5px"},
    t("code.artifacts.empty")));
  if (ART) Object.keys(ART.rows).sort().forEach(kind => {
    const row = ART.rows[kind], original = kinds.find(k => k.kind === kind) || {};
    const out = el("input", {class: "btn mono", style: "flex:1;min-width:190px;font-weight:400",
      value: row.out || "", placeholder: "ArtifactsCode/" + kind});
    out.oninput = () => { row.out = out.value.trim(); };
    const tpl = el("input", {class: "btn mono", style: "flex:1;min-width:190px;font-weight:400",
      value: row.template || ""});
    tpl.oninput = () => { row.template = tpl.value.trim(); };
    const on = el("input", {type: "checkbox"});
    on.checked = row.enabled !== false;
    on.onchange = () => { row.enabled = on.checked; };
    box.append(el("div", {class: "row", style: "gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap"},
      el("span", {class: "mono", style: "min-width:90px"}, kind),
      field(ctx, t("code.artifacts.path"), out, t("code.artifacts.path_hint")),
      field(ctx, t("code.artifacts.template"), pick(ctx, tpl,
        {project: ctx.project.path, want: "file", title: t("code.artifacts.pick_template")}),
        t("code.artifacts.template_hint")),
      el("label", {class: "row", style: "gap:6px;align-items:center;font-size:12.5px"},
        on, t("code.artifacts.enabled"))));
    if (original.problems) original.problems.forEach(p => box.append(el("div", {class: "warnbox"},
      `${kind}: ${p.message || p.code}`)));
  });
  (data.problems || []).forEach(p => box.append(el("div", {class: "warnbox", style: "margin-top:6px"},
    `${p.kind}: ${p.message || p.code}`)));
  box.append(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:12px"},
    el("button", {class: "btn primary", onclick: () => saveArtifacts(ctx)}, t("code.artifacts.save")),
    el("button", {class: "btn", onclick: () => addArtifact(ctx)}, t("code.artifacts.add")),
    el("button", {class: "btn", onclick: () => ctx.runWatched("code:artifacts", ["--check"])},
      t("code.artifacts.check")),
    el("button", {class: "btn", onclick: () => ctx.runWatched("code:artifacts", [])},
      t("code.artifacts.list"))));
  return box;
}

async function saveArtifacts(ctx){
  if (!ART) return;
  // В форме лежат и пути китовых шаблонов (они вне проекта). Писать их в проектные
  // переопределения нельзя: сохраняем только поля, которые человек действительно изменил.
  const kinds = {};
  Object.keys(ART.rows).forEach(kind => {
    const row = ART.rows[kind];
    const orig = ((D && D.artifacts && D.artifacts.kinds) || []).find(k => k.kind === kind);
    const patch = orig ? {} : {out: row.out || "", template: row.template || "",
                               enabled: row.enabled !== false};
    if (orig){
      if ((row.out || "") !== (orig.out || "")) patch.out = row.out || "";
      if ((row.template || "") !== (orig.template_path || "")) patch.template = row.template || "";
      if ((row.enabled !== false) !== !!orig.enabled) patch.enabled = row.enabled !== false;
    }
    if (Object.keys(patch).length) kinds[kind] = patch;
  });
  if (!Object.keys(kinds).length){
    ctx.toast(ctx.t("code.artifacts.saved"), "ok");
    return;
  }
  const r = await ctx.api("/api/code/artifacts", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, kinds, expect: ART.digest})});
  if (r && r.conflict){
    // Пока форма была открыта, блок настроек изменили на диске. Не затираем чужую
    // правку и не оставляем человека с непонятным «не сохранено»: перечитываем.
    ART = null;
    ctx.toast(ctx.t("code.artifacts.conflict"), "warn");
    await load(ctx, true);
    return;
  }
  if (!r || !r.ok){
    ctx.toast((r && r.error) || ctx.t("code.artifacts.save_failed"), "err");
    return;
  }
  if (r.state && D) D.artifacts = r.state;
  ART = null;
  ctx.toast(ctx.t("code.artifacts.saved"), "ok");
  await load(ctx, true);
}

async function addArtifact(ctx){
  const got = await ctx.ui.pickPath({project: ctx.project.path, want: "file",
    title: ctx.t("code.artifacts.pick_template")});
  if (!got) return;
  const path = Array.isArray(got) ? got[0] : got;
  const file = String(path || "").split("/").pop() || "";
  const kind = file.replace(/\.md$/i, "").split("_v", 1)[0];
  if (!kind || !ART) return;
  ART.rows[kind] = {out: "ArtifactsCode/" + kind, template: path, enabled: true};
  draw(ctx);
}

function packCard(ctx){
  const {t, el} = ctx;
  const data = PACK || {stories: []};
  const box = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"});
  box.append(el("b", {}, t("code.pack.title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("code.pack.about")));
  if (data.error) box.append(el("div", {class: "warnbox", style: "margin-bottom:8px"}, data.error));
  const stories = data.stories || [];
  if (!stories.length){
    box.append(el("div", {class: "muted", style: "font-size:12.5px"}, t("code.pack.empty")));
  } else {
    const sel = el("select", {class: "input", id: "codePackStory"});
    stories.forEach(s => sel.append(el("option", {value: s.story},
      `${s.story} — ${s.title} (${s.criteria})`)));
    box.append(el("div", {class: "row", style: "gap:8px;align-items:center;flex-wrap:wrap"},
      sel, el("button", {class: "btn", onclick: () => runPack(ctx)}, t("code.pack.build"))));
  }
  const pack = data.pack;
  if (pack){
    if (!pack.found){
      box.append(el("div", {class: "warnbox", style: "margin-top:10px"}, t("code.pack.not_found")));
    } else {
      box.append(el("div", {style: "margin-top:10px"},
        el("b", {}, pack.story + " · " + pack.title),
        el("span", {class: "muted", style: "font-size:12.5px"},
          ` · ${pack.complete ? t("code.pack.complete") : t("code.pack.incomplete")} · ` +
          `${(pack.criteria || []).length} ${t("code.pack.criteria")} · ` +
          `${(pack.related || []).length} ${t("code.pack.related")}`)));
      (pack.criteria || []).forEach(c => box.append(el("div", {style: "font-size:12.5px;margin:4px 0"},
        el("span", {class: "mono", style: "font-size:11px"}, c.id + " "), c.text)));
      (pack.gaps || []).forEach(g => box.append(el("div", {class: "warnbox", style: "margin-top:4px"},
        `${g.code}: ${g.text}`)));
    }
  }
  box.append(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:12px"},
    el("button", {class: "btn", onclick: () => ctx.runWatched("code:pack", ["--list"])},
      t("code.pack.list")),
    el("button", {class: "btn", onclick: () => ctx.runWatched("code:cleanroom", ["--apply"])},
      t("code.pack.cleanroom")),
    el("button", {class: "btn", onclick: () => ctx.runWatched("code:health", ["--check"])},
      t("code.pack.health")),
    ...(pack && pack.found ? [el("button", {class: "btn",
      onclick: () => ctx.runWatched("code:pack", ["--story", pack.story])}, t("code.pack.show")),
      el("button", {class: "btn",
        onclick: () => ctx.runWatched("code:check", ["--story", pack.story])},
        t("code.pack.check"))] : [])));
  return box;
}

async function runPack(ctx){
  const sel = ctx.$("#codePackStory");
  const story = sel ? sel.value : "";
  if (!story) return;
  const r = await ctx.api("/api/code/pack?project=" + encodeURIComponent(ctx.project.path)
    + "&story=" + encodeURIComponent(story), {quiet: true}) || {};
  PACK = {...(PACK || {}), ...r};
  draw(ctx);
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
