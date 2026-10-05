/* Git — раздел-модуль: сервер проекта, обновление, отправка, автоматика.

   Работу делает движок (scripts/git_sync.py): страница показывает состояние, правит
   настройку проекта и запускает действия заданиями панели — они видны в «Консоли» и в
   истории запусков. Неудача приходит кодом с действиями; слова к коду — в каталоге
   раздела, запасной текст — от движка. Секреты приходят маской: маска значит «не трогали». */

const MASK = "••••••";
const ACTION_OF = {"git:update": "update", "git:push": "push", "git:commit": "commit",
                   "git:fix": "fix", "git:status": "fetch"};
const RX = {
  url: /^https?:\/\/[^\s/@]+(:\d{1,5})?(\/\S*)?$/i,
  scp: /^[\w.-]+@[\w.-]+:\S+$/,
  ssh: /^ssh:\/\/\S+$/i,
  path: /^~?[\w.-]+(\/[\w.-]+)+$/,
  remote: /^[A-Za-z0-9][A-Za-z0-9._-]*$/,
  email: /^[^@\s]+@[^@\s]+\.[^@\s]+$/,
  local: /^(file:\/\/\S+|\/\S+|[A-Za-z]:[\\/]\S*|\\\\\S+)$/,
};
const PLACEHOLDER = /\{(\w+)\}/g;
const key = v => String(v || "").replace(/-/g, "_");     // ff-only → ff_only: ключ каталога

let D = null, TAB = "state", FORM = null, CRED = null, MCRED = [], DIRTY = false;
let CHECK = null, BUSY = "", LAST = null, ERRS = {}, CERT = null, PROJECT = "", QUICK = null;
const ERR_NODES = {};
const NO_CRED = {auth: "system", user: "", secret: "", ssh_key: "", ssh_key_text: ""};
const blankMirror = () => ({remote: "", provider: "generic", instance: "", repo: "",
                            tls: {verify: true, ca_file: ""}, with_primary: true});

export function mount(ctx){
  ctx.root.dataset.module = "gitsync";
}

export async function refresh(ctx, payload){
  if (payload && payload.tab) TAB = payload.tab;
  if (PROJECT !== ctx.project.path){
    PROJECT = ctx.project.path;
    DIRTY = false; CHECK = null; CERT = null; ERRS = {}; LAST = null; QUICK = null;
  }
  await load(ctx, true);
}

const clone = x => JSON.parse(JSON.stringify(x));

async function load(ctx, keepForm){
  const d = await ctx.api("/api/gitsync?project=" + encodeURIComponent(ctx.project.path),
                          {quiet: true});
  if (!d || d.error || !d.status){
    ctx.$("#gitsyncBody").textContent = (d && d.error) || ctx.t("gitsync.load_failed");
    return;
  }
  D = d;
  // Отметка «!» на пункте меню — та же, что считает сервер (git_sync.alert): последняя
  // автоматика не прошла. Кнопка, которая всё починила, снимает её сразу, без перезагрузки.
  const alert = Object.values(d.last || {}).some(e => e && !e.ok && e.trigger && e.trigger !== "manual");
  if (ctx.project && !!ctx.project.git_alert !== alert){
    ctx.project.git_alert = alert;
    ctx.reloadHealth();
  }
  if (!(keepForm && DIRTY)){
    FORM = clone(d.settings);
    FORM.mirrors = FORM.mirrors || [];
    CRED = {...d.credentials, ssh_key_text: ""};
    // Вход у каждого дополнительного сервера свой; массив идёт рядом с FORM.mirrors.
    MCRED = FORM.mirrors.map(m => ({...NO_CRED, ...((d.mirror_credentials || {})[m.remote] || {}),
                                    ssh_key_text: ""}));
    DIRTY = false; ERRS = {};
  } else {
    // Автоматику правят и на вкладке состояния — несохранённая настройка её не затирает.
    FORM.auto_update = clone(d.settings.auto_update);
    FORM.auto_push = clone(d.settings.auto_push);
  }
  draw(ctx);
}

// Строка каталога, а нет её — запасной текст движка.
function tr(ctx, key, fallback, vars){
  const s = ctx.t(key, vars || {});
  return s === key ? (fallback || key) : s;
}

function draw(ctx){
  drawNotes(ctx);
  drawTabs(ctx);
  const box = ctx.$("#gitsyncBody");
  box.innerHTML = "";
  box.append(TAB === "setup" ? setupTab(ctx) : stateTab(ctx));
}

/* ---------------------------------------------------------------- шапка */

function drawNotes(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#gitsyncNotes");
  box.innerHTML = "";
  if (!D.status.repo) return;
  if (!D.saved)
    box.append(el("div", {class: "note", style: "margin-bottom:10px"},
      t("gitsync.not_saved"), " ",
      el("button", {class: "btn sm", onclick: () => { TAB = "setup"; draw(ctx); }},
        t("gitsync.a.open_setup"))));
  const mod = D.status.module || {};
  if (mod.state === "missing" || mod.state === "outdated")
    box.append(el("div", {class: "warnbox"},
      el("b", {}, mod.state === "missing"
        ? t("gitsync.mod_missing", {name: mod.title || mod.id})
        : t("gitsync.mod_outdated", {name: mod.title || mod.id, have: mod.installed,
                                     kit: mod.available})),
      el("div", {class: "muted", style: "font-size:12.5px;margin:4px 0 8px"},
        t("gitsync.mod_why")),
      el("div", {class: "row", style: "gap:8px"},
        el("button", {class: "btn sm primary", onclick: () => installModule(ctx, mod.id)},
          mod.state === "missing" ? t("gitsync.a.install_module") : t("gitsync.a.update_module")),
        el("button", {class: "btn sm", onclick: () => ctx.show("install")},
          t("gitsync.a.open_install")))));
}

function drawTabs(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#gitsyncTabs");
  box.innerHTML = "";
  ["state", "setup"].forEach(id => box.append(el("button", {
    class: "btn sm" + (TAB === id ? " primary" : ""), role: "tab",
    "aria-selected": String(TAB === id),
    onclick: () => { TAB = id; draw(ctx); }},
    t("gitsync.tab." + id), id === "setup" && DIRTY
      ? el("span", {class: "chip warn", style: "margin-left:6px"}, t("gitsync.unsaved")) : null)));
}

/* ---------------------------------------------------------------- состояние */

function stateTab(ctx){
  const {el} = ctx;
  const st = D.status;
  const box = el("div", {});
  if (!st.repo){
    (st.problems || []).forEach(p => box.append(problemView(ctx, p)));
    return box;
  }
  box.append(summaryCard(ctx, st));
  const loud = (st.problems || []).filter(p => p.level !== "info"
    && !["conflict", "in_progress"].includes(p.code));
  const quiet = (st.problems || []).filter(p => p.level === "info"
    && !p.code.startsWith("module_"));
  loud.forEach(p => box.append(problemView(ctx, p)));
  quiet.forEach(p => box.append(problemView(ctx, p)));
  if ((st.conflicts || []).length || st.operation) box.append(conflictsCard(ctx, st));
  // Идёт разбор конфликта — его карточка и есть ответ; отказ, который его начал, не дублируем.
  const resolving = (st.conflicts || []).length || st.operation;
  const last = lastFailure(ctx, resolving);
  if (last) box.append(last);
  box.append(autoCard(ctx));
  box.append(changesCard(ctx, st));
  box.append(logCard(ctx));
  return box;
}

const hostOf = url => {
  const m = String(url || "").match(/^(?:\w+:\/\/)?(?:[^@/]+@)?([^/:]+)(?::(\d+))?/);
  return m ? m[1] + (m[2] ? ":" + m[2] : "") : "";
};

function changedCount(st){
  return new Set([...(st.staged || []), ...(st.unstaged || []), ...(st.untracked || [])]).size;
}

function summaryCard(ctx, st){
  const {t, el} = ctx;
  const changed = changedCount(st);
  const hasRemote = (st.remotes || []).includes(st.remote);
  let sync;
  if (!hasRemote) sync = t("gitsync.sync_no_remote");
  else if (!st.on_server) sync = t("gitsync.sync_not_on_server");
  else if (!st.ahead && !st.behind) sync = t("gitsync.sync_same");
  else sync = [st.behind ? t("gitsync.sync_behind", {n: st.behind}) : "",
               st.ahead ? t("gitsync.sync_ahead", {n: st.ahead}) : ""].filter(Boolean).join(" · ");
  const lc = st.last_commit || {};
  const mirrors = st.mirrors || [];
  // Пока рабочий сервер не задан, отправка идёт на дополнительные «вместе с основным».
  const canPush = hasRemote || mirrors.some(m => m.exists && m.with_primary);
  const busy = BUSY ? el("div", {class: "row", style: "gap:8px;margin-top:10px"},
    el("span", {class: "spin"}),
    el("span", {class: "muted"}, t("gitsync.busy", {what: t("gitsync.act." + ACTION_OF[BUSY])}))) : null;
  const msg = el("input", {class: "btn", style: "flex:1;min-width:200px;font-weight:400",
    placeholder: t("gitsync.commit_ph", {tpl: D.settings.message})});
  // Посреди разбора конфликта обновлять, отправлять и фиксировать нельзя — кнопки ждут.
  const resolving = (st.conflicts || []).length || st.operation;
  const off = BUSY || resolving ? "" : null;
  const updateFirst = !resolving && st.behind > 0;
  const pushFirst = canPush && !resolving && !updateFirst
    && (st.ahead > 0 || changed > 0 || !st.on_server || mirrors.some(m => m.ahead || !m.on_server));
  return el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-bottom:10px"},
      el("span", {class: "chip"}, t("gitsync.prov." + st.provider)),
      el("span", {class: "chip mono", title: t("gitsync.branch_hint")},
        st.branch || t("gitsync.no_branch")),
      hasRemote && hostOf(st.remote_url) ? el("span", {class: "chip mono", title: st.remote_url},
        hostOf(st.remote_url)) : null,
      el("span", {class: "muted mono", style: "font-size:12px"}, st.remote_url || "")),
    el("div", {style: "font-size:15px;font-weight:600"}, sync),
    ...mirrors.map(m => el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:6px;font-size:12.5px;align-items:center"},
      el("span", {class: "chip mono"}, m.remote),
      el("span", {}, !m.exists ? t("gitsync.mirror_missing") : !m.on_server ? t("gitsync.mirror_new")
        : m.ahead ? t("gitsync.mirror_ahead", {n: m.ahead}) : t("gitsync.mirror_same")),
      m.with_primary ? el("span", {class: "chip", title: t("gitsync.with_primary")},
        t("gitsync.with_primary_chip")) : null,
      m.exists ? el("button", {class: "btn sm", disabled: off,
        onclick: () => act(ctx, "git:push", ["--remote=" + m.remote])},
        t("gitsync.push_to", {name: m.remote})) : null)),
    el("div", {class: "muted", style: "font-size:12.5px;margin-top:4px"},
      st.fetched_at ? t("gitsync.fetched", {ago: ctx.fmt.ago(st.fetched_at)})
                    : t("gitsync.never_fetched")),
    el("div", {style: "margin-top:8px"},
      changed ? t("gitsync.changes_n", {n: changed}) : t("gitsync.changes_none")),
    lc.hash ? el("div", {class: "muted", style: "font-size:12.5px;margin-top:4px"},
      t("gitsync.last_commit", {subject: lc.subject, author: lc.author,
                                when: ctx.fmt.when(lc.when)})) : null,
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:14px"},
      el("button", {class: "btn" + (updateFirst ? " primary" : ""), disabled: off,
        title: t("gitsync.update_hint", {how: t("gitsync.strat." + key(D.settings.strategy))}),
        onclick: () => act(ctx, "git:update")}, t("gitsync.update")),
      el("button", {class: "btn" + (pushFirst ? " primary" : ""), disabled: off,
        title: t("gitsync.push_hint"), onclick: () => act(ctx, "git:push")}, t("gitsync.push")),
      el("button", {class: "btn", disabled: off, title: t("gitsync.fetch_hint"),
        onclick: () => act(ctx, "git:status", ["--fetch"])}, t("gitsync.fetch"))),
    changed ? el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:10px"}, msg,
      el("button", {class: "btn", disabled: off, title: t("gitsync.commit_hint"),
        onclick: () => act(ctx, "git:commit", msg.value.trim()
          ? ["--message=" + msg.value.trim()] : [])}, t("gitsync.commit"))) : null,
    busy);
}

function problemView(ctx, p, action){
  const {t, el} = ctx;
  const vars = {...p, current: p.current || "", wanted: p.wanted || "",
                name: (p.module && (p.module.title || p.module.id)) || ""};
  const tone = p.level === "info" ? "note" : p.level === "warn" ? "warnbox" : "warnbox dangerbox";
  const files = p.files || [];
  const fields = p.fields || [];
  return el("div", {class: tone, style: p.level === "info" ? "margin:8px 0" : ""},
    el("b", {}, (p.remote ? p.remote + ": " : "") + tr(ctx, "gitsync.p." + p.code, p.title, vars)),
    el("div", {class: p.level === "info" ? "" : "muted", style: "font-size:12.5px;margin-top:3px"},
      tr(ctx, "gitsync.fix." + p.code, p.fix, vars)),
    files.length ? el("div", {class: "mono", style: "font-size:12px;margin-top:6px"},
      files.slice(0, 12).join(", ") + (files.length > 12 ? " …" : "")) : null,
    fields.length ? el("div", {style: "font-size:12.5px;margin-top:6px"},
      ...fields.map(f => el("div", {}, "· " + (f.field ? fieldName(ctx, f.field) + ": " : "")
                                   + tr(ctx, "gitsync.f." + f.code, f.code)))) : null,
    (p.actions || []).length ? el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:8px"},
      ...p.actions.map((a, i) => el("button", {class: "btn sm" + (i === 0 ? " primary" : ""),
        disabled: BUSY ? "" : null, onclick: () => doAction(ctx, a, p, action)},
        t("gitsync.a." + a)))) : null,
    p.raw ? el("details", {style: "margin-top:8px"},
      el("summary", {class: "muted", style: "cursor:pointer;font-size:12px"}, t("gitsync.raw")),
      el("pre", {class: "mono", style: "font-size:11.5px;white-space:pre-wrap;margin:6px 0 0"},
        p.raw)) : null);
}

// «mirrors.0.repo» → «Дополнительный сервер 1 · Репозиторий»
function fieldName(ctx, field){
  const m = String(field).match(/^mirrors\.(\d+)\.(\w+)$/);
  return m ? ctx.t("gitsync.field.mirror_n", {n: Number(m[1]) + 1}) + " · " + ctx.t("gitsync.field." + m[2])
           : ctx.t("gitsync.field." + field);
}

// Последнее действие кончилось отказом — показываем его причину и починку, пока следующее
// не пройдёт: автоматика, упавшая ночью, не должна теряться в журнале.
function lastFailure(ctx, resolving){
  const {t, el} = ctx;
  const entries = Object.values(D.last || {}).filter(e => e && e.at);
  if (!entries.length) return null;
  const e = entries.sort((a, b) => b.at.localeCompare(a.at))[0];
  if (e.ok || !e.problem) return null;
  if (resolving && ["conflict", "in_progress", "conflict_markers"].includes(e.problem.code)) return null;
  return el("div", {style: "margin:8px 0"},
    el("div", {class: "muted", style: "font-size:12.5px;margin-bottom:-4px"},
      t("gitsync.last_failed", {what: t("gitsync.act." + e.action),
                                trigger: t("gitsync.tr." + (e.trigger || "manual")),
                                when: ctx.fmt.when(e.at)})),
    problemView(ctx, {...e.problem, level: "error"}, e.action));
}

function conflictsCard(ctx, st){
  const {t, el} = ctx;
  const files = st.conflicts || [];
  const fx = (what, file) => act(ctx, "git:fix", ["--what=" + what, ...(file ? ["--file=" + file] : [])]);
  const off = BUSY ? "" : null;
  return el("div", {class: "card", id: "gitsyncConflicts", style: "padding:16px 18px;margin:12px 0"},
    el("b", {}, t("gitsync.conflicts_title", {op: t("gitsync.op." + key(st.operation || "merge"))})),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("gitsync.conflicts_about")),
    ...files.map(f => el("div", {class: "list-item", style: "flex-wrap:wrap;align-items:center"},
      el("span", {class: "mono", style: "flex:1;min-width:200px;font-size:12.5px"}, f),
      el("button", {class: "btn sm", disabled: off, title: t("gitsync.mine_hint"),
        onclick: () => fx("mine", f)}, t("gitsync.mine")),
      el("button", {class: "btn sm", disabled: off, title: t("gitsync.theirs_hint"),
        onclick: () => fx("theirs", f)}, t("gitsync.theirs")),
      el("button", {class: "btn sm", onclick: () => ctx.openPath(f)}, t("gitsync.open_file")),
      el("button", {class: "btn sm", disabled: off, title: t("gitsync.resolved_hint"),
        onclick: () => fx("resolved", f)}, t("gitsync.resolved")))),
    files.length ? null : el("div", {class: "muted", style: "font-size:12.5px"}, t("gitsync.conflicts_none_left")),
    el("div", {class: "row", style: "gap:8px;margin-top:12px"},
      el("button", {class: "btn primary", disabled: files.length || BUSY ? "" : null,
        onclick: () => fx("continue")}, t("gitsync.a.continue")),
      el("button", {class: "btn danger", disabled: off, onclick: () => fx("abort")},
        t("gitsync.a.abort"))));
}

/* ---------------------------------------------------------------- автоматика */

function autoCard(ctx){
  const {t, el} = ctx;
  const s = D.settings, o = D.options;
  const block = (group, on, title) => {
    const cur = clone(s[group]);
    const save = () => saveAuto(ctx, {[group]: cur});
    const every = el("input", {class: "btn", type: "number", min: o.min_every,
      style: "width:80px;font-weight:400", value: String(cur.every_min)});
    every.onchange = () => { cur.every_min = Math.max(o.min_every, Number(every.value) || o.min_every); save(); };
    return el("div", {style: "flex:1;min-width:260px"},
      el("label", {class: "flagline", style: "font-weight:600"},
        el("input", {type: "checkbox", checked: cur.enabled ? "" : null,
          onchange: e => { cur.enabled = e.target.checked; save(); }}), title),
      ...on.map(x => el("label", {class: "flagline", style: "padding-left:24px"},
        el("input", {type: "checkbox", checked: cur.on.includes(x) ? "" : null,
          disabled: cur.enabled ? null : "",
          onchange: e => { cur.on = e.target.checked ? [...cur.on, x] : cur.on.filter(y => y !== x); save(); }}),
        t("gitsync.on." + x),
        x === "interval" ? el("span", {class: "row", style: "gap:6px;margin-left:8px"},
          every, t("gitsync.minutes")) : null)));
  };
  return el("div", {class: "card", style: "padding:16px 18px;margin:14px 0"},
    el("b", {}, t("gitsync.auto_title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("gitsync.auto_about")),
    el("div", {class: "row", style: "gap:24px;flex-wrap:wrap;align-items:flex-start"},
      block("auto_update", o.update_on, t("gitsync.auto_update")),
      block("auto_push", o.push_on, t("gitsync.auto_push"))));
}

async function saveAuto(ctx, patch){
  const settings = {...clone(D.settings), ...patch};
  const r = await ctx.api("/api/gitsync/settings", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, settings})});
  if (!r || !r.ok){
    ERRS = fieldErrors((r && r.problems) || []);
    TAB = "setup";
    ctx.toast(ctx.t("gitsync.auto_needs_setup"), "warn");
    await load(ctx, true);
    return;
  }
  ctx.toast(ctx.t("gitsync.saved"), "ok");
  await load(ctx, true);
}

/* ---------------------------------------------------------------- изменения и журнал */

function changesCard(ctx, st){
  const {t, el} = ctx;
  const group = (key, files) => files.length ? el("div", {style: "margin-top:8px"},
    el("div", {class: "muted", style: "font-size:12px"}, t("gitsync.group." + key, {n: files.length})),
    ...files.slice(0, 200).map(f => el("div", {class: "mono", style: "font-size:12px;cursor:pointer",
      title: t("gitsync.open_file"), onclick: () => ctx.openPath(f)}, f))) : null;
  if (!changedCount(st)) return el("div", {});
  return el("details", {class: "card", style: "padding:12px 18px;margin:12px 0"},
    el("summary", {style: "cursor:pointer;font-weight:600"},
      t("gitsync.changes_title", {n: changedCount(st)})),
    group("staged", st.staged || []), group("unstaged", st.unstaged || []),
    group("untracked", st.untracked || []));
}

// Итог действия словами интерфейса: по числам записи, а не по фразе движка.
function entryText(ctx, e){
  const {t} = ctx;
  if (e.skipped) return t("gitsync.skipped." + e.skipped);
  if (!e.ok) return tr(ctx, "gitsync.p." + e.code, e.summary || e.code, {});
  if (e.action === "update") return e.nothing ? t("gitsync.ok.update_none")
    : t("gitsync.ok.update", {n: e.behind || 0, files: e.files || 0});
  if (e.action === "push" && (e.targets || []).length)
    return e.targets.map(x => !x.ok ? x.remote + ": " + tr(ctx, "gitsync.p." + x.code, x.code)
      : x.nothing ? t("gitsync.ok.target_none", {remote: x.remote})
      : t("gitsync.ok.target", {remote: x.remote, n: x.pushed || 0})).join(" · ");
  if (e.action === "push") return e.nothing ? t("gitsync.ok.push_none")
    : t("gitsync.ok.push", {n: e.pushed || 0});
  if (e.action === "commit") return e.nothing ? t("gitsync.ok.commit_none")
    : t("gitsync.ok.commit", {commit: e.commit || "", files: e.files || 0});
  if (e.action === "fetch") return t("gitsync.ok.fetch", {behind: e.behind || 0, ahead: e.ahead || 0});
  return t("gitsync.ok.fix");
}

function logCard(ctx){
  const {t, el} = ctx;
  const rows = D.log || [];
  return el("details", {class: "card", style: "padding:12px 18px;margin:12px 0"},
    el("summary", {style: "cursor:pointer;font-weight:600"}, t("gitsync.log_title")),
    rows.length ? null : el("div", {class: "muted", style: "margin-top:8px"}, t("gitsync.log_empty")),
    ...rows.map(e => el("div", {class: "list-item", style: "padding:6px 0;align-items:center"},
      el("span", {class: "muted", style: "min-width:120px;font-size:12px"}, ctx.fmt.when(e.at)),
      el("span", {class: "chip " + (e.skipped ? "" : e.ok ? "ok" : "bad"), style: "flex:none"},
        t("gitsync.act." + e.action)),
      el("span", {class: "muted", style: "font-size:12px;min-width:120px"},
        t("gitsync.tr." + (e.trigger || "manual"))),
      el("span", {style: "flex:1;font-size:12.5px"}, entryText(ctx, e)))));
}

/* ---------------------------------------------------------------- действия */

async function act(ctx, cmd, args = []){
  if (BUSY) return;
  BUSY = cmd; LAST = {cmd, args};
  draw(ctx);
  let res = null;
  try { res = await ctx.run(cmd, args); } finally { BUSY = ""; }
  await load(ctx, true);
  const what = ACTION_OF[cmd];
  const e = (D.last || {})[what];
  if (res && res.refused) ctx.toast(res.refused, "err");
  else if (e && e.ok) ctx.toast(entryText(ctx, e), "ok");
  else if (e && e.problem) ctx.toast((e.problem.remote ? e.problem.remote + ": " : "")
    + tr(ctx, "gitsync.p." + e.problem.code, e.problem.title, e.problem), "err");
  else if (res && res.rc) ctx.toast(ctx.t("gitsync.failed_console"), "err");
}

function doAction(ctx, id, p, action){
  const run = (cmd, args) => act(ctx, cmd, args || []);
  const tab = (name, after) => { TAB = name; draw(ctx); if (after) after(); };
  ({
    open_setup: () => tab("setup"),
    open_credentials: () => tab("setup", () => ctx.$("#gitsyncAuth")?.scrollIntoView({behavior: "smooth"})),
    check: () => tab("setup", () => runCheck(ctx)),
    retry: () => LAST ? run(LAST.cmd, LAST.args) : run(action === "push" ? "git:push" : "git:update"),
    push: () => run("git:push"),
    commit: () => run("git:commit"),
    set_upstream: () => run("git:fix", ["--what=set-upstream"]),
    update_then_push: () => run("git:push", ["--update-first"]),
    update_merge: () => run("git:update", ["--strategy=merge"]),
    update_rebase: () => run("git:update", ["--strategy=rebase"]),
    show_conflicts: () => ctx.$("#gitsyncConflicts")?.scrollIntoView({behavior: "smooth"}),
    abort: () => run("git:fix", ["--what=abort"]),
    continue: () => run("git:fix", ["--what=continue"]),
    commit_then_update: () => run("git:update", ["--commit-first"]),
    commit_anyway: () => run(action === "push" ? "git:push" : "git:commit", ["--skip-ratchet"]),
    trust_cert: () => tab("setup", () => certShow(ctx, p && p.remote)),
    init: () => run("git:fix", ["--what=init"]),
    checkout_branch: () => run("git:fix", ["--what=checkout"]),
    use_current_branch: () => run("git:fix", ["--what=use-branch"]),
    strip_url: () => run("git:fix", ["--what=strip-url"]),
    fix_key_perms: () => run("git:fix", ["--what=fix-key-perms"]),
    install_module: () => installModule(ctx, (p && p.module && p.module.id) || D.settings.provider),
    update_module: () => installModule(ctx, (p && p.module && p.module.id) || D.settings.provider),
    open_install: () => ctx.show("install"),
  }[id] || (() => {}))();
}

async function installModule(ctx, id){
  const r = await ctx.api("/api/gitmods/install", {method: "POST", quiet: true,
    body: JSON.stringify({id})});
  ctx.toast(r && r.ok ? ctx.t("gitsync.mod_done", {name: id, v: r.version})
                      : (r && r.error) || ctx.t("gitsync.mod_failed"), r && r.ok ? "ok" : "err");
  await load(ctx, true);
}

/* ---------------------------------------------------------------- настройка */

function validBranch(b){
  if (!b || /^[-/]/.test(b) || /(\/|\.|\.lock)$/.test(b) || b === "@") return false;
  if (["..", "@{", "//", "\\"].some(x => b.includes(x))) return false;
  return !/[\x00-\x20\x7f~^:?*[]/.test(b);
}

function isUrl(v){ return RX.url.test(v) || RX.scp.test(v) || RX.ssh.test(v) || RX.local.test(v); }

// Те же правила, что у движка (git_sync.normalize): подсветка сразу, а не после сохранения.
function validate(){
  const e = {}, f = FORM, c = CRED;
  if (f.instance && !RX.url.test(f.instance)) e.instance = "url";
  const repo = (f.repo || "").trim();
  if (repo && !isUrl(repo)){
    if (!RX.path.test(repo.replace(/^\/+|\/+$/g, "").replace(/\.git$/, ""))) e.repo = "repo";
    else if (!f.instance && f.provider !== "github") e.instance = "required";   // у GitHub пусто — github.com
  }
  if (!RX.remote.test(f.remote || "")) e.remote = "name";
  if (f.branch && !validBranch(f.branch)) e.branch = "branch";
  if (f.author_email && !RX.email.test(f.author_email)) e.author_email = "email";
  const unknown = [...(f.message || "").matchAll(PLACEHOLDER)].map(m => m[1])
    .filter(x => !D.options.placeholders.includes(x));
  if (unknown.length) e.message = "placeholder";
  if ((c.auth === "token" || c.auth === "password") && !c.secret) e.secret = "required";
  if (c.auth === "password" && !c.user) e.user = "required";
  if (c.auth === "token" && !c.user && !["gitlab", "github"].includes(f.provider)
      && !(f.provider === "bitbucket" && /bitbucket\.org/.test(f.instance || ""))) e.user = "required_for_token";
  if (c.ssh_key_text && !/PRIVATE KEY/.test(c.ssh_key_text)) e.ssh_key_text = "key_format";
  const names = new Set([f.remote]);
  (f.mirrors || []).forEach((m, i) => {
    const pre = `mirrors.${i}.`;
    if (!RX.remote.test(m.remote || "")) e[pre + "remote"] = "name";
    else if (names.has(m.remote)) e[pre + "remote"] = "duplicate";
    names.add(m.remote);
    if (m.instance && !RX.url.test(m.instance)) e[pre + "instance"] = "url";
    const r = (m.repo || "").trim();
    if (!r) e[pre + "repo"] = "required";
    else if (!isUrl(r)){
      if (!RX.path.test(r.replace(/^\/+|\/+$/g, "").replace(/\.git$/, ""))) e[pre + "repo"] = "repo";
      else if (!m.instance && m.provider !== "github") e[pre + "instance"] = "required";
    }
    const mc = MCRED[i] || NO_CRED;
    if ((mc.auth === "token" || mc.auth === "password") && !mc.secret) e[pre + "secret"] = "required";
    if (mc.auth === "password" && !mc.user) e[pre + "user"] = "required";
  });
  return e;
}

function fieldErrors(problems){
  const e = {};
  (problems || []).forEach(p => { if (p.field) e[p.field] = p.code; });
  return e;
}

function showErrs(ctx){
  Object.entries(ERR_NODES).forEach(([k, n]) => {
    n.textContent = ERRS[k] ? tr(ctx, "gitsync.f." + ERRS[k], ERRS[k]) : "";
  });
}

// Логин в адресе (так даёт строку клонирования Bitbucket) уходит в «Вход», адрес — без
// него. Пароль в адресе не трогаем: движок уберёт его с замечанием.
function pullUser(srv, cred){
  const m = /^(https?:\/\/)([^\/@\s:]+)@(.*)$/i.exec(srv.repo || "");
  if (!m) return;
  srv.repo = m[1] + m[3];
  if (cred && !cred.user) cred.user = decodeURIComponent(m[2]);
}

function touch(ctx){
  DIRTY = true;
  pullUser(FORM, CRED);
  (FORM.mirrors || []).forEach((m, i) => pullUser(m, MCRED[i]));
  ERRS = validate();
  showErrs(ctx);
  drawTabs(ctx);
  const bar = ctx.$("#gitsyncSave");
  if (bar) bar.disabled = false;
}

function field(ctx, key, label, input, hint){
  const {el} = ctx;
  const err = el("div", {style: "font-size:12px;color:var(--danger);min-height:0"});
  ERR_NODES[key] = err;
  return el("label", {style: "display:block;flex:1;min-width:220px;margin-bottom:10px"},
    el("div", {class: "muted", style: "font-size:12px;margin-bottom:3px"}, label),
    input, hint ? el("div", {class: "muted", style: "font-size:11.5px;margin-top:3px"}, hint) : null, err);
}

function input(ctx, obj, key, attrs){
  const x = ctx.el("input", {class: "btn" + (attrs && attrs.mono ? " mono" : ""),
    style: "width:100%;font-weight:400", ...(attrs || {}), mono: null});
  x.value = obj[key] == null ? "" : String(obj[key]);
  x.oninput = () => { obj[key] = x.value; touch(ctx); };
  return x;
}

function select(ctx, obj, key, values, labelOf, redraw){
  const s = ctx.el("select", {class: "btn", style: "width:100%"},
    ...values.map(v => ctx.el("option", {value: v, selected: obj[key] === v ? "" : null}, labelOf(v))));
  s.onchange = () => { obj[key] = s.value; touch(ctx); if (redraw) draw(ctx); };
  return s;
}

function preview(ctx){
  const now = new Date();
  const pad = n => String(n).padStart(2, "0");
  const vals = {project: ctx.project.name, branch: FORM.branch || D.status.branch || "main",
    date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    time: `${pad(now.getHours())}:${pad(now.getMinutes())}`, count: "3",
    files: "a.md, b.md, c.md", trigger: ctx.t("gitsync.tr.manual")};
  return (FORM.message || "").replace(PLACEHOLDER, (m, k) => vals[k] ?? m);
}

function setupTab(ctx){
  const {t, el} = ctx;
  for (const k of Object.keys(ERR_NODES)) delete ERR_NODES[k];
  if (!D.status.repo){
    const box = el("div", {});
    (D.status.problems || []).forEach(p => box.append(problemView(ctx, p)));
    return box;
  }
  const o = D.options, f = FORM, c = CRED;
  const mod = (D.modules || {})[f.provider] || {};
  const modChip = f.provider === "generic" ? el("span", {class: "chip"}, t("gitsync.mod_builtin"))
    : mod.state === "ok" ? el("span", {class: "chip ok"}, t("gitsync.mod_ok", {v: mod.installed}))
    : el("span", {class: "row", style: "gap:6px"},
        el("span", {class: "chip warn"}, mod.state === "outdated"
          ? t("gitsync.mod_old", {v: mod.installed}) : t("gitsync.mod_none")),
        el("button", {class: "btn sm", onclick: () => installModule(ctx, f.provider)},
          mod.state === "outdated" ? t("gitsync.a.update_module") : t("gitsync.a.install_module")));
  const where = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("gitsync.s_server")),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;margin-top:10px;align-items:flex-start"},
      field(ctx, "provider", t("gitsync.field.provider"),
        select(ctx, f, "provider", o.providers, v => t("gitsync.prov." + v), true)),
      el("div", {style: "padding-top:22px"}, modChip)),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap"},
      field(ctx, "instance", t("gitsync.field.instance"),
        input(ctx, f, "instance", {mono: true, placeholder: "https://git.example.com:3000"}),
        t("gitsync.h.instance")),
      field(ctx, "repo", t("gitsync.field.repo"),
        input(ctx, f, "repo", {mono: true, placeholder: t("gitsync.ph.repo." + f.provider)}),
        t("gitsync.h.repo"))),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap"},
      field(ctx, "branch", t("gitsync.field.branch"),
        input(ctx, f, "branch", {mono: true, placeholder: D.status.branch || "main",
                                 list: "gitsyncBranches"}), t("gitsync.h.branch")),
      field(ctx, "remote", t("gitsync.field.remote"),
        input(ctx, f, "remote", {mono: true, placeholder: "origin"}), t("gitsync.h.remote"))),
    el("datalist", {id: "gitsyncBranches"},
      ...(((CHECK && CHECK.steps || []).find(s => s.id === "git") || {}).branches || [])
        .map(b => el("option", {value: b}))));

  const auth = el("div", {class: "card", id: "gitsyncAuth", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("gitsync.s_auth")),
    ...authFields(ctx, c, f.provider, "", f.instance),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:center;margin-top:4px"},
      el("label", {class: "flagline"},
        el("input", {type: "checkbox", checked: f.tls.verify ? "" : null,
          onchange: e => { f.tls.verify = e.target.checked; touch(ctx); }}),
        t("gitsync.tls_verify")),
      el("button", {class: "btn sm", onclick: () => certShow(ctx)}, t("gitsync.a.trust_cert"))),
    f.tls.ca_file ? el("div", {class: "muted mono", style: "font-size:11.5px"},
      t("gitsync.ca_file", {path: f.tls.ca_file})) : null,
    certBox(ctx));

  const commitCard = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("gitsync.s_commit")),
    el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;margin-top:10px"},
      field(ctx, "author_name", t("gitsync.field.author_name"), input(ctx, f, "author_name")),
      field(ctx, "author_email", t("gitsync.field.author_email"),
        input(ctx, f, "author_email", {placeholder: "name@example.com"}))),
    field(ctx, "message", t("gitsync.field.message"), input(ctx, f, "message", {mono: true}),
      t("gitsync.h.message", {list: o.placeholders.map(x => "{" + x + "}").join(" ")})),
    el("div", {class: "muted", style: "font-size:12px;margin:-4px 0 10px"},
      t("gitsync.preview", {text: preview(ctx)})),
    field(ctx, "strategy", t("gitsync.field.strategy"),
      select(ctx, f, "strategy", o.strategies, v => t("gitsync.strat." + key(v)), true),
      t("gitsync.strat_hint." + key(f.strategy))),
    el("label", {class: "flagline"},
      el("input", {type: "checkbox", checked: f.commit_on_push ? "" : null,
        onchange: e => { f.commit_on_push = e.target.checked; touch(ctx); }}),
      t("gitsync.commit_on_push")));

  const bar = el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin:6px 0 14px"},
    el("button", {class: "btn primary", id: "gitsyncSave", disabled: DIRTY ? null : "",
      onclick: () => saveSetup(ctx)}, t("gitsync.save")),
    el("button", {class: "btn", onclick: () => { DIRTY = false; load(ctx, false); }}, t("gitsync.revert")),
    el("span", {style: "flex:1"}),
    el("button", {class: "btn", onclick: () => runCheck(ctx)}, t("gitsync.a.check")));
  const box = el("div", {}, quickCard(ctx), where, demoteBox(ctx), auth, mirrorsCard(ctx), commitCard, bar,
    checkView(ctx));
  setTimeout(() => showErrs(ctx), 0);
  return box;
}

// bitbucket.org: git ходит с именем пользователя и API-токеном вместо пароля — подсказки свои.
const bbCloud = (provider, instance) => provider === "bitbucket"
  && /^(https?:\/\/)?(www\.)?bitbucket\.org(\/|:|$)/i.test(instance || "");

function secretHint(ctx, auth, provider, instance){
  if (bbCloud(provider, instance)) return ctx.t("gitsync.h.bbcloud." + (auth === "token" ? "token" : "password"));
  return auth === "token" ? ctx.t("gitsync.h.token." + provider) : ctx.t("gitsync.h.password");
}

function userHint(ctx, auth, provider, instance){
  if (bbCloud(provider, instance) && auth === "password") return ctx.t("gitsync.h.bbcloud.user");
  return ctx.t("gitsync.h.user." + (auth === "token" ? "token" : "password"));
}

// Вход одного сервера: способ, логин и токен (пароль) или SSH-ключ. `pre` — приставка поля
// у дополнительного сервера («mirrors.0.»): так подсветка находит его поле, а не основного.
function authFields(ctx, c, provider, pre, instance){
  const {t, el} = ctx;
  const o = D.options;
  const sec = el("input", {class: "btn mono", type: "password", autocomplete: "off",
    style: "width:100%;font-weight:400",
    placeholder: c.secret === MASK ? t("gitsync.secret_set") : t("gitsync.secret_none")});
  const had = c.secret === MASK;
  sec.oninput = () => { c.secret = sec.value || (had ? MASK : ""); touch(ctx); };
  const keyText = el("textarea", {class: "btn mono", rows: 3, style: "width:100%;font-weight:400;font-size:11.5px",
    placeholder: "-----BEGIN OPENSSH PRIVATE KEY-----"});
  keyText.oninput = () => { c.ssh_key_text = keyText.value; touch(ctx); };
  return [
    el("div", {style: "margin-top:10px"},
      field(ctx, pre + "auth", t("gitsync.field.auth"),
        select(ctx, c, "auth", o.auths, v => t("gitsync.auth." + v), true), t("gitsync.h.auth." + c.auth))),
    c.auth === "token" || c.auth === "password" ? el("div", {class: "row", style: "gap:12px;flex-wrap:wrap"},
      field(ctx, pre + "user", t("gitsync.field.user"), input(ctx, c, "user", {autocomplete: "off"}),
        userHint(ctx, c.auth, provider, instance)),
      field(ctx, pre + "secret", c.auth === "token" ? t("gitsync.field.token") : t("gitsync.field.password"),
        sec, had ? t("gitsync.h.secret_keep") : secretHint(ctx, c.auth, provider, instance))) : null,
    c.auth === "ssh" ? el("div", {},
      field(ctx, pre + "ssh_key", t("gitsync.field.ssh_key"),
        input(ctx, c, "ssh_key", {mono: true, placeholder: "~/.ssh/id_ed25519"}), t("gitsync.h.ssh_key")),
      field(ctx, pre + "ssh_key_text", t("gitsync.field.ssh_key_text"), keyText, t("gitsync.h.ssh_key_text"))) : null,
  ];
}

// Основной сервер — в дополнительные: `git remote rename origin gitea` вместе с настройкой
// и входом. Основным станет рабочий сервер, когда его укажут выше.
function demoteBox(ctx){
  const {t, el} = ctx;
  const st = D.status;
  if (!(st.remotes || []).includes(D.settings.remote) || DIRTY) return null;
  const name = el("input", {class: "btn mono", style: "width:140px;font-weight:400", value: "gitea"});
  return el("details", {class: "card", style: "padding:12px 18px;margin-bottom:14px"},
    el("summary", {style: "cursor:pointer;font-weight:600"}, t("gitsync.demote_title", {remote: D.settings.remote})),
    el("p", {class: "muted", style: "font-size:12.5px;margin:6px 0 10px"},
      t("gitsync.demote_about", {remote: D.settings.remote})),
    el("div", {class: "row", style: "gap:8px;align-items:center"},
      el("span", {class: "muted"}, t("gitsync.field.name")), name,
      el("button", {class: "btn sm primary", disabled: BUSY ? "" : null,
        onclick: () => act(ctx, "git:fix", ["--what=demote-origin", "--name=" + name.value.trim()])},
        t("gitsync.demote"))));
}

function mirrorsCard(ctx){
  const {t, el} = ctx;
  const o = D.options, f = FORM;
  const box = el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("gitsync.s_mirrors")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("gitsync.mirrors_about")));
  f.mirrors.forEach((m, i) => {
    const pre = `mirrors.${i}.`;
    MCRED[i] = MCRED[i] || {...NO_CRED};
    box.append(el("div", {class: "card", style: "padding:12px 14px;margin-bottom:10px"},
      el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:flex-start"},
        field(ctx, pre + "remote", t("gitsync.field.mirror_remote"),
          input(ctx, m, "remote", {mono: true, placeholder: "gitea"})),
        field(ctx, pre + "provider", t("gitsync.field.provider"),
          select(ctx, m, "provider", o.providers, v => t("gitsync.prov." + v), true)),
        el("button", {class: "btn sm danger", style: "margin-top:20px", onclick: () => {
          f.mirrors.splice(i, 1); MCRED.splice(i, 1); touch(ctx); draw(ctx); }},
          t("gitsync.mirror_remove"))),
      el("div", {class: "row", style: "gap:12px;flex-wrap:wrap"},
        field(ctx, pre + "instance", t("gitsync.field.instance"),
          input(ctx, m, "instance", {mono: true, placeholder: "https://git.example.com:3000"})),
        field(ctx, pre + "repo", t("gitsync.field.repo"),
          input(ctx, m, "repo", {mono: true, placeholder: t("gitsync.ph.repo." + m.provider)}))),
      ...authFields(ctx, MCRED[i], m.provider, pre, m.instance),
      el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:center"},
        el("label", {class: "flagline"},
          el("input", {type: "checkbox", checked: m.with_primary ? "" : null,
            onchange: e => { m.with_primary = e.target.checked; touch(ctx); }}),
          t("gitsync.with_primary")),
        m.remote ? el("button", {class: "btn sm", onclick: () => runCheck(ctx, m.remote)},
          t("gitsync.check_server", {name: m.remote})) : null)));
  });
  box.append(el("button", {class: "btn sm", onclick: () => {
    f.mirrors.push(blankMirror()); MCRED.push({...NO_CRED}); touch(ctx); draw(ctx); }},
    t("gitsync.mirror_add")));
  return box;
}

async function saveSetup(ctx){
  ERRS = validate();
  if (Object.keys(ERRS).length){ showErrs(ctx); return ctx.toast(ctx.t("gitsync.save_fix"), "warn"); }
  const r = await ctx.api("/api/gitsync/settings", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, settings: FORM, credentials: CRED,
                          mirror_credentials: Object.fromEntries(
                            FORM.mirrors.map((m, i) => [m.remote, MCRED[i] || NO_CRED]))})});
  if (!r || !r.ok){
    ERRS = fieldErrors(r && r.problems);
    showErrs(ctx);
    return ctx.toast((r && r.error) || ctx.t("gitsync.save_fix"), "err");
  }
  ctx.toast(ctx.t("gitsync.saved"), "ok");
  (r.notes || []).forEach(n => ctx.toast(n, "ok"));
  DIRTY = false;
  await load(ctx, false);
}

async function runCheck(ctx, remote){
  CHECK = {busy: true};
  draw(ctx);
  const i = remote ? FORM.mirrors.findIndex(m => m.remote === remote) : -1;
  const r = await ctx.api("/api/gitsync/check", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, settings: FORM, remote: remote || "",
                          credentials: i >= 0 ? MCRED[i] : CRED})});
  CHECK = {...(r || {ok: false, steps: []}), remote: remote || "", index: i};
  draw(ctx);
}

function checkView(ctx){
  const {t, el} = ctx;
  if (!CHECK) return el("div", {});
  if (CHECK.busy) return el("div", {class: "row", style: "gap:8px"}, el("span", {class: "spin"}),
    el("span", {class: "muted"}, t("gitsync.checking")));
  const sg = CHECK.suggest || {};
  const apply = (label, fn) => el("button", {class: "btn sm", onclick: () => { fn(); touch(ctx); draw(ctx); }}, label);
  const mi = CHECK.index >= 0 ? CHECK.index : -1;
  const target = mi >= 0 ? FORM.mirrors[mi] : FORM, cred = mi >= 0 ? MCRED[mi] : CRED;
  return el("div", {class: "card", style: "padding:14px 18px;margin-bottom:14px"},
    el("b", {}, (CHECK.remote ? CHECK.remote + ": " : "") + (CHECK.ok ? t("gitsync.check_ok") : t("gitsync.check_bad"))),
    ...stepsView(ctx, CHECK),
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:8px"},
      sg.provider && target && sg.provider !== target.provider ? apply(t("gitsync.use_provider",
        {name: t("gitsync.prov." + sg.provider)}), () => { target.provider = sg.provider; }) : null,
      sg.user && cred && !cred.user ? apply(t("gitsync.use_user", {user: sg.user}), () => { cred.user = sg.user; }) : null,
      sg.branch && mi < 0 && sg.branch !== FORM.branch ? apply(t("gitsync.use_branch", {branch: sg.branch}),
        () => { FORM.branch = sg.branch; }) : null),
    sg.empty ? el("div", {class: "note", style: "margin-top:8px"}, t("gitsync.repo_empty")) : null,
    CHECK.problem ? problemView(ctx, {...CHECK.problem, level: "error"}, "check") : null);
}

function stepsView(ctx, check){
  const {t, el} = ctx;
  return (check.steps || []).map(s => el("div", {class: "row", style: "gap:8px;margin-top:6px;font-size:12.5px;flex-wrap:wrap"},
    el("span", {class: "chip " + (s.ok ? "ok" : s.ok === null ? "" : "bad")},
      s.ok ? "✓" : s.ok === null ? "·" : "✗"),
    el("span", {}, t("gitsync.step." + s.id, {login: s.login || "", branch: s.detail || ""})),
    s.id === "api" && s.ok === null ? el("span", {class: "muted"},
      s.note ? t("gitsync.step." + s.note, {reason: tr(ctx, "gitsync.p." + s.code, s.code)}) : t("gitsync.step.api_off")) : null,
    s.detail && s.id !== "branch" && !(s.id === "api" && s.ok === null && !s.note)
      ? el("span", {class: "muted mono", style: "font-size:11.5px"}, s.detail) : null));
}

/* ---------------------------------------------------------------- подключение строкой git clone */

// Строку со страницы репозитория (кнопка Clone) вставляют целиком: сервер, репозиторий,
// ветку и логин разбирает движок (git_sync.parse_clone), человек выбирает способ входа и
// вводит пароль. «Проверить и подключить» — проверка тем же входом, затем сохранение.
const quick = () => QUICK || (QUICK = {text: "", parsed: null, cred: {...NO_CRED, auth: "password"},
  target: "", name: "", busy: false, check: null, problems: null, done: null, timer: null});

function quickCard(ctx){
  const {t, el} = ctx;
  const q = quick();
  const text = el("input", {class: "btn mono", style: "width:100%;font-weight:400", autocomplete: "off",
    placeholder: "git clone https://name@bitbucket.org/team/project.git"});
  text.value = q.text;
  text.oninput = () => {
    q.text = text.value; q.check = null; q.problems = null; q.done = null;
    clearTimeout(q.timer);
    q.timer = setTimeout(() => parseQuick(ctx), 300);
  };
  return el("div", {class: "card", style: "padding:16px 18px;margin-bottom:14px"},
    el("b", {}, t("gitsync.q.title")),
    el("p", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"}, t("gitsync.q.about")),
    text,
    el("div", {id: "gitsyncQuick"}, ...quickBody(ctx)));
}

function drawQuick(ctx){
  const box = ctx.$("#gitsyncQuick");
  if (box){ box.innerHTML = ""; box.append(...quickBody(ctx)); }
}

async function parseQuick(ctx){
  const q = quick();
  if (!q.text.trim()){ q.parsed = null; return drawQuick(ctx); }
  const r = await ctx.api("/api/gitsync/parse", {method: "POST", quiet: true,
    body: JSON.stringify({text: q.text})});
  q.parsed = r || {ok: false, code: "bad_url"};
  if (q.parsed.ok){
    q.cred = {...q.cred, auth: q.parsed.auth, user: q.parsed.user || q.cred.user,
              secret: q.parsed.secret || q.cred.secret};
    const mainSet = !!(D.settings.repo || "").trim();
    q.target = q.target || (mainSet ? "mirror" : "main");
    if (!q.name) q.name = freeName(q.parsed.provider === "generic" ? "server" : q.parsed.provider);
  }
  drawQuick(ctx);
}

function freeName(base){
  const taken = new Set([D.settings.remote, ...(D.settings.mirrors || []).map(m => m.remote),
                         ...(D.status.remotes || [])]);
  let name = base, n = 2;
  while (taken.has(name)) name = base + "-" + n++;
  return name;
}

function quickBody(ctx){
  const {t, el} = ctx;
  const q = quick(), p = q.parsed;
  if (!p) return [];
  if (!p.ok) return [el("div", {style: "font-size:12.5px;color:var(--danger);margin-top:8px"},
    t("gitsync.q.err." + p.code))];
  const c = q.cred;
  const out = [el("div", {class: "row", style: "gap:6px;flex-wrap:wrap;margin:10px 0"},
    el("span", {class: "chip ok"}, t("gitsync.prov." + p.provider)),
    p.instance ? el("span", {class: "chip mono"}, p.instance) : null,
    el("span", {class: "chip mono"}, p.repo),
    p.branch ? el("span", {class: "chip"}, t("gitsync.q.branch", {branch: p.branch})) : null)];
  if (p.secret_in_url) out.push(el("div", {class: "note", style: "margin-bottom:10px"}, t("gitsync.q.secret_moved")));
  const method = el("select", {class: "btn", style: "width:100%"},
    ...D.options.auths.map(v => el("option", {value: v, selected: c.auth === v ? "" : null}, t("gitsync.auth." + v))));
  method.onchange = () => { c.auth = method.value; q.check = null; drawQuick(ctx); };
  out.push(qField(ctx, t("gitsync.field.auth"), method, t("gitsync.h.auth." + c.auth)));
  if (c.auth === "password" || c.auth === "token"){
    const user = el("input", {class: "btn", style: "width:100%;font-weight:400", autocomplete: "off", value: c.user || ""});
    user.oninput = () => { c.user = user.value.trim(); };
    const sec = el("input", {class: "btn mono", type: "password", autocomplete: "new-password",
      style: "width:100%;font-weight:400"});
    sec.value = c.secret || "";
    sec.oninput = () => { c.secret = sec.value; };
    out.push(el("div", {class: "row", style: "gap:12px;flex-wrap:wrap"},
      qField(ctx, t("gitsync.field.user"), user, userHint(ctx, c.auth, p.provider, p.instance)),
      qField(ctx, c.auth === "token" ? t("gitsync.field.token") : t("gitsync.field.password"), sec,
        secretHint(ctx, c.auth, p.provider, p.instance))));
  } else if (c.auth === "ssh"){
    const key = el("input", {class: "btn mono", style: "width:100%;font-weight:400",
      placeholder: "~/.ssh/id_ed25519", value: c.ssh_key || ""});
    key.oninput = () => { c.ssh_key = key.value.trim(); };
    out.push(qField(ctx, t("gitsync.field.ssh_key"), key, t("gitsync.h.ssh_key")));
  }
  const target = el("select", {class: "btn", style: "width:100%"},
    ...["main", "mirror"].map(v => el("option", {value: v, selected: q.target === v ? "" : null},
      t("gitsync.q.target." + v, {remote: D.settings.remote || "origin"}))));
  target.onchange = () => { q.target = target.value; drawQuick(ctx); };
  const row = el("div", {class: "row", style: "gap:12px;flex-wrap:wrap"},
    qField(ctx, t("gitsync.q.where"), target,
      q.target === "main" && (D.settings.repo || "").trim() ? t("gitsync.q.replace_main") : ""));
  if (q.target === "mirror"){
    const name = el("input", {class: "btn mono", style: "width:100%;font-weight:400", value: q.name});
    name.oninput = () => { q.name = name.value.trim(); };
    row.append(qField(ctx, t("gitsync.field.mirror_remote"), name, t("gitsync.q.name_hint")));
  }
  out.push(row);
  out.push(el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"},
    el("button", {class: "btn primary", disabled: q.busy ? "" : null, onclick: () => connectQuick(ctx, false)},
      t("gitsync.q.connect")),
    q.busy ? el("span", {class: "spin"}) : null,
    q.check && !q.check.ok && !q.busy ? el("button", {class: "btn", onclick: () => connectQuick(ctx, true)},
      t("gitsync.q.save_anyway")) : null));
  if (q.check) out.push(el("div", {style: "margin-top:10px"},
    el("b", {}, q.check.ok ? t("gitsync.check_ok") : t("gitsync.check_bad")), ...stepsView(ctx, q.check),
    (q.check.suggest || {}).empty ? el("div", {class: "note", style: "margin-top:8px"}, t("gitsync.repo_empty")) : null,
    q.check.problem ? problemView(ctx, {...q.check.problem, level: "error", actions: []}, "check") : null));
  if (q.problems) out.push(problemView(ctx, {code: "settings", level: "error", fields: q.problems, actions: []}, "check"));
  if (q.done) out.push(el("div", {class: "note", style: "margin-top:10px"},
    el("div", {}, t("gitsync.q.done", {remote: q.done.remote, url: q.done.url})),
    el("div", {class: "row", style: "gap:8px;margin-top:8px"},
      el("button", {class: "btn sm primary", disabled: BUSY ? "" : null,
        onclick: () => act(ctx, "git:push", q.done.main ? [] : ["--remote=" + q.done.remote])},
        t("gitsync.q.push_now")))));
  return out;
}

function qField(ctx, label, control, hint){
  const {el} = ctx;
  return el("label", {style: "display:block;flex:1;min-width:220px;margin-bottom:10px"},
    el("div", {class: "muted", style: "font-size:12px;margin-bottom:3px"}, label), control,
    hint ? el("div", {class: "muted", style: "font-size:11.5px;margin-top:3px"}, hint) : null);
}

async function connectQuick(ctx, force){
  const q = quick(), p = q.parsed;
  if (!p || !p.ok || q.busy) return;
  if (DIRTY && !confirm(ctx.t("gitsync.q.dirty_ask"))) return;
  const settings = clone(D.settings);
  settings.mirrors = settings.mirrors || [];
  const server = {provider: p.provider, instance: p.instance, repo: p.repo, tls: {verify: true, ca_file: ""}};
  const main = q.target !== "mirror";
  const remote = main ? (settings.remote || "origin") : q.name;
  if (main){ Object.assign(settings, server); if (p.branch) settings.branch = p.branch; }
  else settings.mirrors.push({remote, ...server, with_primary: true});
  const cred = {...NO_CRED, ...q.cred};
  q.problems = null; q.done = null;
  if (!force){
    q.busy = true; q.check = null; drawQuick(ctx);
    const r = await ctx.api("/api/gitsync/check", {method: "POST", quiet: true,
      body: JSON.stringify({project: ctx.project.path, settings, remote: main ? "" : remote, credentials: cred})});
    q.busy = false;
    q.check = r || {ok: false, steps: []};
    if (!q.check.ok) return drawQuick(ctx);
  }
  const mc = {};
  (D.settings.mirrors || []).forEach(m => { mc[m.remote] = (D.mirror_credentials || {})[m.remote] || NO_CRED; });
  if (!main) mc[remote] = cred;
  const r = await ctx.api("/api/gitsync/settings", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, settings,
                          credentials: main ? cred : D.credentials, mirror_credentials: mc})});
  if (!r || !r.ok){
    q.problems = (r && r.problems) || [{field: "", code: "required"}];
    return drawQuick(ctx);
  }
  (r.notes || []).forEach(n => ctx.toast(n, "ok"));
  q.done = {remote, main, url: p.url};
  q.cred = {...q.cred, secret: ""};
  DIRTY = false;
  await load(ctx, false);
}

/* ---------------------------------------------------------------- сертификат */

// Сертификат основного сервера или дополнительного (`remote`) — по его адресу.
function certUrl(remote){
  if (!remote) return FORM.instance || D.status.remote_url;
  const m = (FORM.mirrors || []).find(x => x.remote === remote) || {};
  const st = (D.status.mirrors || []).find(x => x.remote === remote) || {};
  return m.instance || st.url || m.repo || "";
}

async function certShow(ctx, remote){
  CERT = {busy: true};
  draw(ctx);
  const r = await ctx.api("/api/gitsync/cert", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, action: "show", url: certUrl(remote)})});
  CERT = {...(r || {ok: false}), remote: remote || ""};
  draw(ctx);
}

function certBox(ctx){
  const {t, el} = ctx;
  if (!CERT) return null;
  if (CERT.busy) return el("div", {class: "row", style: "gap:8px;margin-top:8px"}, el("span", {class: "spin"}));
  if (!CERT.ok) return el("div", {class: "warnbox"}, CERT.error || t("gitsync.cert_failed"));
  return el("div", {class: "warnbox"},
    el("b", {}, t("gitsync.cert_title", {host: CERT.host + ":" + CERT.port})),
    el("div", {class: "muted", style: "font-size:12.5px;margin:4px 0"}, t("gitsync.cert_about")),
    el("div", {class: "mono", style: "font-size:12px;word-break:break-all"}, "SHA-256 " + CERT.sha256),
    el("div", {class: "row", style: "gap:8px;margin-top:8px"},
      el("button", {class: "btn sm primary", onclick: async () => {
        const r = await ctx.api("/api/gitsync/cert", {method: "POST", quiet: true,
          body: JSON.stringify({project: ctx.project.path, action: "trust", sha256: CERT.sha256,
                                url: certUrl(CERT.remote), remote: CERT.remote || ""})});
        ctx.toast(r && r.ok ? t("gitsync.cert_trusted") : (r && r.error) || t("gitsync.cert_failed"),
                  r && r.ok ? "ok" : "err");
        CERT = null;
        await load(ctx, false);
      }}, t("gitsync.cert_trust")),
      el("button", {class: "btn sm", onclick: () => { CERT = null; draw(ctx); }}, t("gitsync.cancel"))));
}

export default {mount, refresh};
