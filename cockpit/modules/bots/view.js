/* Боты — раздел-модуль: повторяемые задания модели с MCP, навыками, вложениями и расписанием.

   Бот — файл `bots/<имя>.md` проекта: шапка YAML — что ему дано, тело — промпт. Пишет и
   проверяет движок (scripts/bots.py), запускает команда `bot:run` — по кнопке или по
   расписанию раздела «Cron». Промпт правится тем же редактором, что в «Файлах»; вложения
   подбираются как в «Продуктивности» — ссылками на файлы и папки проекта, не копиями. */

const LIST_KEYS = ["mcp", "skills", "attachments"];
const MODES = ["sv", "ir", "wysiwyg"];                                     // виды Vditor
let D = null, CUR = null, FORM = null, BODY = "", ORIG = "", ED = null, PROJECT = "";
let CHECK = null, CHECK_TIMER = null, RUNNING = false, SKILL_FILTER = "", CARET = false;

export function mount(ctx){
  ctx.root.dataset.module = "bots";
}

export async function refresh(ctx, payload){
  if (PROJECT !== ctx.project.path){
    PROJECT = ctx.project.path;
    CUR = null; FORM = null;
  }
  await loadList(ctx);
  const want = (payload && payload.file) || (CUR && CUR.file) || (D.bots[0] && D.bots[0].file);
  if (want && (!CUR || CUR.file !== want || !dirty())) await open(ctx, want, true);
  else drawEditor(ctx);
}

const q = ctx => "project=" + encodeURIComponent(ctx.project.path);
const clone = x => JSON.parse(JSON.stringify(x));
// Редактор при загрузке может поправить хвост текста — сравниваем без хвостовых пробелов,
// иначе бот считался бы изменённым, едва открывшись.
const norm = text => String(text || "").replace(/\s+$/, "");
const snapshot = () => JSON.stringify([FORM, norm(ED ? ED.getValue() : BODY)]);
const dirty = () => !!CUR && snapshot() !== ORIG;

async function loadList(ctx){
  const d = await ctx.api("/api/bots?" + q(ctx), {quiet: true});
  D = d && d.bots ? d : {bots: [], mcp: [], skills: [], presets: []};
  drawList(ctx);
}

// Пути — буквально: проверка «панель просит только то, что есть у сервера» ищет их в тексте.
async function post(ctx, path, body){
  return ctx.api(path, {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, ...body})});
}

/* ---------------------------------------------------------------- список */

function scheduleChip(ctx, sch){
  const {t, el} = ctx;
  if (!sch || !sch.set) return el("span", {class: "chip"}, t("bots.no_schedule"));
  if (sch.error) return el("span", {class: "chip warn"}, t("bots.schedule_bad"));
  if (!sch.on) return el("span", {class: "chip"}, t("bots.schedule_off"));
  return el("span", {class: "chip ok", title: (sch.next || []).map(ctx.fmt.when).join("\n")},
    sch.next && sch.next[0] ? t("bots.schedule_next", {when: ctx.fmt.when(sch.next[0])}) : t("bots.schedule_on"));
}

function lastChip(ctx, last){
  const {t, el} = ctx;
  if (!last || !last.at) return null;
  return el("span", {class: "chip " + (last.ok ? "ok" : "bad"), title: ctx.fmt.when(last.at)},
    last.ok ? t("bots.last_ok", {when: ctx.fmt.when(last.at)})
            : t("bots.last_bad", {when: ctx.fmt.when(last.at)}));
}

function drawList(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#botsList");
  box.innerHTML = "";
  const name = el("input", {class: "btn", style: "flex:1;min-width:0;font-weight:400",
    placeholder: t("bots.new_ph")});
  const create = async () => {
    if (!(await leave(ctx))) return;
    const r = await post(ctx, "/api/bots/create", {name: name.value.trim()});
    if (!r || !r.ok) return ctx.toast((r && r.error) || t("bots.failed"), "err");
    await loadList(ctx);
    await open(ctx, r.file, false);
  };
  name.onkeydown = e => { if (e.key === "Enter") create(); };
  box.append(el("div", {class: "card", style: "padding:12px;margin-bottom:10px"},
    el("div", {class: "row", style: "gap:6px"}, name,
      el("button", {class: "btn sm primary", onclick: create}, t("bots.create"))),
    el("button", {class: "btn sm", style: "margin-top:8px", title: t("bots.example_hint"),
      onclick: async () => {
        if (!(await leave(ctx))) return;
        const r = await post(ctx, "/api/bots/example", {});
        if (!r || !r.ok) return ctx.toast((r && r.error) || t("bots.failed"), "err");
        ctx.toast(r.template ? t("bots.example_made_tpl", {tpl: r.template}) : t("bots.example_made"), "ok");
        await loadList(ctx);
        await open(ctx, r.file, false);
      }}, t("bots.example"))));
  if (!D.bots.length){
    box.append(el("div", {class: "card", style: "padding:14px"},
      el("div", {class: "muted", style: "font-size:13px"}, t("bots.empty"))));
    return;
  }
  box.append(el("div", {class: "card"}, ...D.bots.map(b => el("div", {
      class: "list-item", style: "cursor:pointer;flex-direction:column;align-items:stretch;gap:4px"
        + (CUR && CUR.file === b.file ? ";background:var(--surface-2)" : ""),
      onclick: async () => { if (!CUR || CUR.file !== b.file){ if (await leave(ctx)) open(ctx, b.file, false); } }},
    el("div", {class: "row", style: "gap:6px;align-items:center"},
      el("b", {style: "flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis"}, b.meta.name),
      (b.problems || []).length ? el("span", {class: "chip warn", title: b.problems.map(p =>
        tr(ctx, "bots.p." + p.code, p.code) + detailOf(ctx, p)).join("\n")},
        "⚠ " + b.problems.length) : null),
    el("div", {class: "muted mono", style: "font-size:11.5px"}, b.file),
    el("div", {class: "row", style: "gap:6px;flex-wrap:wrap"}, scheduleChip(ctx, b.schedule),
      lastChip(ctx, b.last))))));
}

/* ---------------------------------------------------------------- открыть и сохранить */

async function leave(ctx){
  return !dirty() || confirm(ctx.t("bots.leave_ask"));
}

async function open(ctx, file, quiet){
  const d = await ctx.api("/api/bots/file?" + q(ctx) + "&file=" + encodeURIComponent(file), {quiet: true});
  if (!d || d.error){
    if (!quiet) ctx.toast((d && d.error) || ctx.t("bots.failed"), "err");
    CUR = null; drawList(ctx); drawEditor(ctx);
    return;
  }
  CUR = d; FORM = clone(d.meta); BODY = d.body;
  CHECK = {problems: d.problems, schedule: d.schedule};
  ED = null;
  ORIG = JSON.stringify([FORM, norm(BODY)]);
  drawList(ctx);
  drawEditor(ctx);
}

async function save(ctx){
  if (!CUR) return false;
  BODY = ED ? ED.getValue() : BODY;
  const r = await post(ctx, "/api/bots/save", {file: CUR.file, meta: FORM, body: BODY});
  if (!r || !r.ok){ ctx.toast((r && r.error) || ctx.t("bots.failed"), "err"); return false; }
  ORIG = snapshot();
  ctx.toast(ctx.t("bots.saved"), "ok");
  await loadList(ctx);
  CUR = {...CUR, ...(D.bots.find(b => b.file === CUR.file) || {})};
  CHECK = {problems: r.problems || [], schedule: (D.bots.find(b => b.file === CUR.file) || {}).schedule};
  drawTop(ctx); drawConfig(ctx);
  return true;
}

function touch(ctx){
  drawTop(ctx);
  clearTimeout(CHECK_TIMER);
  // Проверка — у движка: те же правила, что при запуске, а не их копия в странице.
  CHECK_TIMER = setTimeout(async () => {
    const r = await post(ctx, "/api/bots/validate", {meta: FORM, body: ED ? ED.getValue() : BODY});
    // Только места замечаний и ближайших запусков: поле, в котором печатают, не трогаем.
    if (r && r.problems){ CHECK = r; drawChecks(ctx); drawTop(ctx); }
  }, 350);
}

/* ---------------------------------------------------------------- редактор */

function tr(ctx, key, fallback, vars){
  const s = ctx.t(key, vars || {});
  return s === key ? fallback : s;
}

// Подробность замечания: у расписания движок присылает код поля — называем его словами.
function detailOf(ctx, p){
  if (!p.detail) return "";
  return ": " + (p.code === "cron_invalid" ? tr(ctx, "bots.cron_field." + p.detail, p.detail) : p.detail);
}

function drawEditor(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#botsEditor");
  box.innerHTML = "";
  ED = null;
  if (!CUR){
    box.append(el("div", {class: "card", style: "padding:20px"},
      el("div", {class: "muted"}, t("bots.pick"))));
    return;
  }
  box.append(el("div", {id: "botsTop"}),
    el("div", {class: "card", style: "padding:14px 16px;margin:12px 0"},
      el("div", {class: "row", style: "gap:8px;align-items:center;margin-bottom:8px"},
        el("b", {style: "flex:1"}, t("bots.prompt")),
        modeSelect(ctx)),
      el("div", {class: "muted", style: "font-size:12px;margin-bottom:8px"}, t("bots.prompt_hint")),
      el("div", {id: "botsVditor"})),
    el("div", {id: "botsConfig"}),
    el("div", {id: "botsLast"}));
  drawTop(ctx);
  drawConfig(ctx);
  drawLast(ctx);
  const host = ctx.$("#botsVditor");
  CARET = false;
  host.addEventListener("focusin", () => { CARET = true; });       // курсор поставил человек
  ctx.ui.editor(host, BODY, {onInput: () => touch(ctx)}).then(ed => {
    if (CUR && host.isConnected) ED = ed;
  });
}

function modeSelect(ctx){
  const {t, el} = ctx;
  const cur = localStorage.getItem("aurora-editor-mode") || "sv";
  const s = el("select", {class: "btn sm", title: t("bots.mode_hint")},
    ...MODES.map(m => el("option", {value: m, selected: m === cur ? "" : null}, t("bots.mode." + m))));
  s.onchange = () => {
    BODY = ED ? ED.getValue() : BODY;
    try { localStorage.setItem("aurora-editor-mode", s.value); } catch (e) { /* без памяти — без неё */ }
    drawEditor(ctx);
  };
  return s;
}

function drawTop(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#botsTop");
  if (!box || !CUR) return;
  if (box.dataset.ready !== CUR.file){
    box.dataset.ready = CUR.file;
    box.innerHTML = "";
    const name = el("input", {class: "btn", style: "flex:1;min-width:200px;font-weight:600"});
    name.value = FORM.name;
    name.oninput = () => { FORM.name = name.value; touch(ctx); };
    const desc = el("input", {class: "btn", style: "width:100%;font-weight:400;margin-top:8px",
      placeholder: t("bots.description_ph")});
    desc.value = FORM.description || "";
    desc.oninput = () => { FORM.description = desc.value; touch(ctx); };
    box.append(el("div", {class: "card", style: "padding:14px 16px"},
      el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"}, name,
        el("span", {id: "botsDirty"})),
      desc,
      el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px"},
        el("span", {class: "muted mono", style: "font-size:12px;flex:1"}, CUR.file),
        el("button", {class: "btn sm primary", id: "botsSave", onclick: () => save(ctx)}, t("bots.save")),
        el("button", {class: "btn sm", id: "botsRun", onclick: () => runNow(ctx), title: t("bots.run_hint")},
          t("bots.run")),
        el("button", {class: "btn sm", onclick: async () => {
          if (!(await leave(ctx))) return;
          const r = await post(ctx, "/api/bots/duplicate", {file: CUR.file});
          if (!r || !r.ok) return ctx.toast((r && r.error) || t("bots.failed"), "err");
          await loadList(ctx); await open(ctx, r.file, false);
        }}, t("bots.duplicate")),
        el("button", {class: "btn sm", title: t("bots.rename_hint"), onclick: async () => {
          if (dirty() && !(await save(ctx))) return;
          const r = await post(ctx, "/api/bots/rename", {file: CUR.file, name: FORM.name});
          if (!r || !r.ok) return ctx.toast((r && r.error) || t("bots.failed"), "err");
          ctx.toast(t("bots.renamed", {file: r.file}), "ok");
          await loadList(ctx); await open(ctx, r.file, false);
        }}, t("bots.rename")),
        el("button", {class: "btn sm danger", onclick: async () => {
          if (!confirm(t("bots.delete_ask", {name: FORM.name, file: CUR.file}))) return;
          const r = await post(ctx, "/api/bots/delete", {file: CUR.file});
          if (!r || !r.ok) return ctx.toast((r && r.error) || t("bots.failed"), "err");
          CUR = null; ORIG = "";
          await loadList(ctx);
          if (D.bots[0]) await open(ctx, D.bots[0].file, true); else drawEditor(ctx);
        }}, t("bots.delete")))));
  }
  const mark = ctx.$("#botsDirty");
  mark.innerHTML = "";
  if (dirty()) mark.append(el("span", {class: "chip warn"}, t("bots.unsaved")));
  ctx.$("#botsSave").disabled = !dirty();
  ctx.$("#botsRun").disabled = RUNNING;
}

// Замечания движка к полю бота — под его карточкой; ближайшие запуски и «в расписании» —
// в карточке расписания. Перерисовываются отдельно от полей ввода.
function drawChecks(ctx){
  const {t, el} = ctx;
  const probs = (CHECK && CHECK.problems) || [];
  for (const field of ["mcp", "skills", "attachments", "cron", "body"]){
    const slot = ctx.$("#botsProb-" + field);
    if (!slot) continue;
    slot.innerHTML = "";
    probs.filter(p => p.field === field).forEach(p => slot.append(el("div", {
        style: "font-size:12px;color:var(--danger);margin-top:6px"},
      tr(ctx, "bots.p." + p.code, p.code) + detailOf(ctx, p)
        + " — " + tr(ctx, "bots.fix." + p.code, ""))));
  }
  const sch = (CHECK && CHECK.schedule) || {};
  const next = ctx.$("#botsNext");
  if (next){
    next.innerHTML = "";
    if (FORM.cron && !sch.error && (sch.next || []).length)
      next.append(el("div", {style: "font-size:12.5px;margin-top:4px"},
        t(FORM.enabled ? "bots.next_runs" : "bots.next_runs_off",
          {list: sch.next.map(ctx.fmt.when).join(" · ")})));
  }
  const state = ctx.$("#botsCronState");
  if (state){
    state.innerHTML = "";
    const scheduled = FORM.cron && FORM.enabled && !sch.error && !dirty();
    state.append(scheduled
      ? el("span", {class: "chip ok"}, t("bots.in_cron"))
      : el("button", {class: "btn sm primary", title: t("bots.add_to_cron_hint"),
          onclick: () => addToCron(ctx)}, t("bots.add_to_cron")));
  }
}

function drawConfig(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#botsConfig");
  if (!box || !CUR) return;
  box.innerHTML = "";
  box.append(el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;align-items:stretch"},
    configCard(ctx, t("bots.mcp"), t("bots.mcp_about"), mcpPanel(ctx), "mcp"),
    configCard(ctx, t("bots.skills"), t("bots.skills_about"), skillsPanel(ctx), "skills"),
    configCard(ctx, t("bots.attachments"), t("bots.attachments_about"), attachmentsPanel(ctx), "attachments"),
    configCard(ctx, t("bots.cron"), t("bots.cron_about"), cronPanel(ctx), "cron")),
    el("div", {id: "botsProb-body"}));
  drawChecks(ctx);
}

function configCard(ctx, title, about, panel, field){
  const {el} = ctx;
  return el("div", {class: "card", style: "padding:14px 16px;flex:1 1 300px;min-width:260px"},
    el("b", {}, title),
    el("div", {class: "muted", style: "font-size:12px;margin:4px 0 10px"}, about),
    panel, el("div", {id: "botsProb-" + field}));
}

// Галочки «из того, что есть» и сверху — выбранные, которых больше нет: их видно и можно снять.
function checklist(ctx, key, have, filter){
  const {t, el} = ctx;
  const picked = FORM[key] || [];
  const names = [...picked.filter(n => !have.includes(n)), ...have]
    .filter(n => !filter || picked.includes(n) || n.toLowerCase().includes(filter.toLowerCase()));
  const list = el("div", {style: "max-height:220px;overflow:auto"});
  names.forEach(n => list.append(el("label", {class: "flagline", style: "padding:3px 0"},
    el("input", {type: "checkbox", checked: picked.includes(n) ? "" : null,
      onchange: e => {
        FORM[key] = e.target.checked ? [...picked.filter(x => x !== n), n] : picked.filter(x => x !== n);
        touch(ctx); drawConfig(ctx);
      }}),
    el("span", {class: "mono", style: "font-size:12.5px"}, n),
    have.includes(n) ? null : el("span", {class: "chip warn"}, t("bots.missing")))));
  return list;
}

function mcpPanel(ctx){
  const {t, el} = ctx;
  if (!D.mcp.length && !(FORM.mcp || []).length)
    return el("div", {},
      el("div", {class: "muted", style: "font-size:12.5px"}, t("bots.mcp_none")),
      el("button", {class: "btn sm", style: "margin-top:8px", onclick: () => ctx.show("setup")},
        t("bots.a.open_setup")));
  return checklist(ctx, "mcp", D.mcp, "");
}

function skillsPanel(ctx){
  const {t, el} = ctx;
  const box = el("div", {});
  const filter = el("input", {class: "btn", style: "width:100%;font-weight:400;margin-bottom:6px",
    placeholder: t("bots.skills_filter"), value: SKILL_FILTER});
  const list = el("div", {});
  const draw = () => { list.innerHTML = ""; list.append(checklist(ctx, "skills", D.skills, SKILL_FILTER)); };
  filter.oninput = () => { SKILL_FILTER = filter.value; draw(); };
  draw();
  box.append(filter, list);
  return box;
}

/* ---------------------------------------------------------------- вложения */

// Ссылка в промпт — относительно файла бота (он лежит в bots/), как её поймут и предпросмотр,
// и Obsidian, и человек, открывший файл.
function linkFor(path, kind){
  const name = path.replace(/\/$/, "").split("/").pop();
  return `[${name}](../${encodeURI(path)}${kind === "dir" && !path.endsWith("/") ? "/" : ""})`;
}

// Курсор в промпте — ссылка встаёт туда, отделённая пробелами; курсора не было — в конец
// отдельной строкой, а не вплотную к первому слову.
function insertLink(ctx, path, kind){
  const link = linkFor(path, kind);
  if (ED && ED.insertValue && CARET){ ED.insertValue(" " + link + " "); ED.focus && ED.focus(); }
  else if (ED){ ED.setValue(norm(ED.getValue()) + "\n\n" + link + "\n"); }
  else { BODY = norm(BODY) + "\n\n" + link + "\n"; drawEditor(ctx); }
  touch(ctx);
}

function attachmentsPanel(ctx){
  const {t, el} = ctx;
  const box = el("div", {});
  const bad = new Set(((CHECK && CHECK.problems) || []).filter(p => p.field === "attachments").map(p => p.detail));
  (FORM.attachments || []).forEach(path => box.append(el("div", {class: "row",
      style: "gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap"},
    el("span", {class: "chip mono" + (bad.has(path) ? " warn" : ""), title: path,
      style: "max-width:100%;overflow:hidden;text-overflow:ellipsis"}, path),
    el("button", {class: "btn sm", title: t("bots.insert_link_hint"),
      onclick: () => insertLink(ctx, path, path.endsWith("/") ? "dir" : "file")}, t("bots.insert_link")),
    el("button", {class: "btn sm", style: "padding:0 8px", title: t("bots.remove"),
      onclick: () => { FORM.attachments = FORM.attachments.filter(x => x !== path); touch(ctx); drawConfig(ctx); }}, "×"))));
  // Подбор — как в «Продуктивности»: файлы и папки проекта по части имени.
  const wrap = el("div", {style: "position:relative"});
  const input = el("input", {class: "btn mono", style: "width:100%;font-weight:400;font-size:12.5px",
    placeholder: t("bots.attach_ph")});
  const list = el("div", {class: "card", hidden: "",
    style: "position:absolute;left:0;right:0;z-index:20;max-height:240px;overflow:auto;padding:4px"});
  let timer = null;
  const add = it => {
    const value = it.kind === "dir" ? it.value.replace(/\/?$/, "/") : it.value;
    if (!(FORM.attachments || []).includes(value)) FORM.attachments = [...(FORM.attachments || []), value];
    input.value = ""; list.hidden = true;
    touch(ctx); drawConfig(ctx);
  };
  input.oninput = () => {
    clearTimeout(timer);
    const text = input.value.trim();
    if (!text){ list.hidden = true; return; }
    timer = setTimeout(async () => {
      const d = await ctx.api("/api/context/suggest?" + q(ctx) + "&q=" + encodeURIComponent("@" + text), {quiet: true});
      const items = ((d && d.items) || []).filter(it => it.kind === "file" || it.kind === "dir").slice(0, 30);
      list.innerHTML = "";
      items.forEach(it => list.append(el("div", {class: "list-item", style: "cursor:pointer;padding:4px 8px",
          onmousedown: e => { e.preventDefault(); add(it); }},
        el("span", {class: "chip", style: "flex:none"}, t(it.kind === "dir" ? "bots.kind_dir" : "bots.kind_file")),
        el("span", {class: "mono", style: "font-size:12px"}, it.label))));
      list.hidden = !items.length;
    }, 180);
  };
  input.onblur = () => setTimeout(() => { list.hidden = true; }, 150);
  wrap.append(input, list);
  box.append(wrap);
  return box;
}

/* ---------------------------------------------------------------- расписание */

function cronPanel(ctx){
  const {t, el} = ctx;
  const expr = el("input", {class: "btn mono", style: "flex:1;min-width:140px;font-weight:400",
    placeholder: "0 9 * * 1-5", value: FORM.cron || ""});
  expr.oninput = () => {
    FORM.cron = expr.value.trim();
    preset.value = (D.presets || []).some(p => p.cron === FORM.cron) ? FORM.cron : "";
    touch(ctx);
  };
  const preset = el("select", {class: "btn sm"},
    el("option", {value: ""}, t("bots.preset_pick")),
    ...(D.presets || []).map(p => el("option", {value: p.cron, selected: p.cron === FORM.cron ? "" : null},
      t("bots.preset." + p.id))));
  preset.onchange = () => { if (preset.value){ FORM.cron = preset.value; touch(ctx); drawConfig(ctx); } };
  const on = el("input", {type: "checkbox", checked: FORM.enabled ? "" : null,
    onchange: e => { FORM.enabled = e.target.checked; touch(ctx); drawChecks(ctx); }});
  return el("div", {},
    el("div", {class: "row", style: "gap:6px;flex-wrap:wrap"}, expr, preset),
    el("div", {class: "muted", style: "font-size:11.5px;margin-top:4px"}, t("bots.cron_format")),
    el("label", {class: "flagline", style: "margin-top:6px"}, on, t("bots.enabled")),
    el("div", {id: "botsNext"}),
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:10px"},
      el("span", {id: "botsCronState"}),
      el("button", {class: "btn sm", onclick: () => ctx.show("cron")}, t("bots.open_cron"))));
}

// Одним нажатием: расписания нет — по будням в 9:00; включить; сохранить. Бот сразу
// виден в «Cron» — источник расписания один, файл бота.
async function addToCron(ctx){
  if (!FORM.cron) FORM.cron = ((D.presets || []).find(p => p.id === "weekdays_9") || {}).cron || "0 9 * * 1-5";
  // Неверное выражение в расписание не ставим: сначала его исправит человек.
  const r = await post(ctx, "/api/bots/validate", {meta: {...FORM, enabled: true}, body: ED ? ED.getValue() : BODY});
  const bad = ((r && r.problems) || []).find(p => p.code === "cron_invalid");
  if (bad){
    CHECK = r; drawConfig(ctx);
    return ctx.toast(tr(ctx, "bots.p.cron_invalid", bad.code) + detailOf(ctx, bad), "warn");
  }
  FORM.enabled = true;
  if (!(await save(ctx))) return;
  const sch = (D.bots.find(b => b.file === CUR.file) || {}).schedule || {};
  if (sch.error) return ctx.toast(ctx.t("bots.p.cron_invalid"), "warn");
  ctx.toast(ctx.t("bots.added_to_cron", {when: sch.next && sch.next[0] ? ctx.fmt.when(sch.next[0]) : "—"}), "ok");
}

/* ---------------------------------------------------------------- запуск и итог */

async function runNow(ctx){
  if (!CUR || RUNNING) return;
  if (dirty() && !(await save(ctx))) return;
  RUNNING = true;
  drawTop(ctx); drawLast(ctx);
  try { await ctx.run("bot:run", ["--bot=" + CUR.file]); }
  finally { RUNNING = false; }
  const file = CUR.file;
  await loadList(ctx);
  const d = await ctx.api("/api/bots/file?" + q(ctx) + "&file=" + encodeURIComponent(file), {quiet: true});
  if (d && !d.error && CUR && CUR.file === file) CUR.last = d.last;
  drawTop(ctx); drawLast(ctx);
  const last = (CUR && CUR.last) || {};
  ctx.toast(last.ok ? ctx.t("bots.run_ok") : ctx.t("bots.run_bad"), last.ok ? "ok" : "err");
}

function doAction(ctx, id){
  ({open_setup: () => ctx.show("setup"), open_models: () => ctx.show("models"),
    open_install: () => ctx.show("install"), retry: () => runNow(ctx),
    open_bots: () => ctx.$("#botsVditor")?.scrollIntoView({behavior: "smooth"})}[id] || (() => {}))();
}

function drawLast(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#botsLast");
  if (!box || !CUR) return;
  box.innerHTML = "";
  if (RUNNING){
    box.append(el("div", {class: "card", style: "padding:14px 16px;margin-top:12px"},
      el("span", {class: "spin"}), el("span", {style: "margin-left:8px"}, t("bots.running"))));
    return;
  }
  const last = CUR.last || {};
  if (!last.at) return;
  const p = last.problem;
  box.append(el("div", {class: p ? "warnbox dangerbox" : "card", style: "padding:14px 16px;margin-top:12px"},
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"},
      el("b", {}, last.ok ? t("bots.last_title_ok") : t("bots.last_title_bad")),
      el("span", {class: "muted"}, ctx.fmt.when(last.at) + " · " + t("bots.trigger." + (last.trigger || "manual"))
        + (last.seconds ? " · " + ctx.fmt.howLong(Math.round(last.seconds)) : ""))),
    p ? el("div", {style: "margin-top:6px"},
      el("div", {}, tr(ctx, "bots.p." + p.code, p.code) + detailOf(ctx, p)),
      el("div", {class: "muted", style: "font-size:12.5px;margin-top:2px"}, tr(ctx, "bots.fix." + p.code, "")),
      (p.actions || []).length ? el("div", {class: "row", style: "gap:8px;margin-top:8px;flex-wrap:wrap"},
        ...p.actions.map((a, i) => el("button", {class: "btn sm" + (i === 0 ? " primary" : ""),
          onclick: () => doAction(ctx, a)}, t("bots.a." + a)))) : null) : null,
    last.summary ? el("div", {style: "font-size:13px;margin-top:8px"}, last.summary) : null,
    el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;margin-top:8px"},
      last.report ? el("button", {class: "btn sm", onclick: () => ctx.openPath(last.report)}, t("bots.open_report")) : null,
      ...(last.outputs || []).filter(x => !x.endsWith("/report.md")).slice(0, 8).map(x =>
        el("button", {class: "btn sm mono", style: "font-size:11.5px", onclick: () => ctx.openPath(x)},
          x.split("/").pop())))));
}

export default {mount, refresh};
