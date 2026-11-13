/* Продуктивность — раздел-модуль.

   Работа, ради которой база и нужна: написать артефакт, проверить чужую историю, собрать
   закрывающий документ и передать его наружу. Список маршрутов производства рисует общий
   с «Быстрым стартом» файл: экран один, разница в группе сценариев. */

import {renderRoutes} from "../quickstart/routes.js";

let RUNS = new Map();          // sid или временный ключ → запуск артефакта
let ACTIVE = "";               // какой запуск показан в диалоге
let RUN_SEQ = 0;
let SHOWN_PROJECT = "";
let MAKE_TICKER = null;
// Ссылки и вложения задачи: {path, label, kind: file|dir|attach}. Уходят движку `--context`;
// упоминания в тексте (`@путь`, `/навык`, `@сервер`) движок разбирает и сам.
let REFS = [];
let SUGGEST_STOP = null;       // гасит отложенный запрос подсказки и закрывает список (onHide)
// Блок «Задача» будущего документа (окно создания): его значения уходят движку `--task-field` и ложатся в шапку документа.
let MAKE_TASK = null;
let REQ_PATHS = [];            // документы/папки проверки реализованности требований
const ACCEPT = [".md", ".markdown", ".txt", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml",
  ".toml", ".ini", ".cfg", ".csv", ".tsv", ".xml", ".html", ".htm", ".svg", ".bpmn", ".puml",
  ".mmd", ".sql", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".rb",
  ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".sh", ".ps1", ".feature", ".graphql", ".proto"];

export function mount(ctx){
  ctx.root.dataset.module = "work";
  ctx.$("#makeGo").onclick = () => startMake(ctx);
  ctx.$("#makePublish").onclick = () => publish(ctx);
  const file = ctx.$("#makeFile");
  file.accept = ACCEPT.join(",");
  ctx.$("#makeAttach").onclick = () => file.click();
  file.onchange = () => attach(ctx, [...file.files]).then(() => { file.value = ""; });
  // Файл или папку проекта не печатают `@путём`, а выбирают в окне ядра; у ядра без окна кнопки нет.
  const pick = ctx.$("#makePick");
  if (ctx.ui && ctx.ui.pickPath) pick.onclick = () => pickRefs(ctx);
  else pick.hidden = true;
  wireSuggest(ctx);
  drawRefs(ctx);
  const reqPick = ctx.$("#reqPick"), reqGo = ctx.$("#reqGo"), reqExport = ctx.$("#reqExport");
  if (reqPick) reqPick.onclick = () => pickReq(ctx);
  if (reqGo) reqGo.onclick = () => runReqCheck(ctx);
  if (reqExport) reqExport.onclick = () => exportReq(ctx);
  if (ctx.$("#reqAgent")) ctx.$("#reqAgent").title = ctx.t("work.req.agent_hint");
  drawReqPaths(ctx);
}

// ---- проверка реализованности требований ----

function drawReqPaths(ctx){
  const box = ctx.$("#reqPaths");
  if (!box) return;
  const {t, el} = ctx;
  box.innerHTML = "";
  REQ_PATHS.forEach(path => box.append(el("span", {class: "chip mono", style: "gap:5px"},
    path,
    el("button", {class: "btn sm", style: "padding:0 5px;min-height:0", title: t("work.req.remove"),
      onclick: () => { REQ_PATHS = REQ_PATHS.filter(x => x !== path); drawReqPaths(ctx); }}, "×"))));
  if (!REQ_PATHS.length){
    box.append(el("span", {class: "muted", style: "font-size:12.5px"}, t("work.req.empty")));
  }
}

async function pickReq(ctx){
  if (!ctx.project) return ctx.toast(ctx.t("work.pick_project"), "warn");
  const got = await ctx.ui.pickPath({project: ctx.project.path, want: "any", multi: true,
    detail: true, title: ctx.t("work.req.pick_title")});
  (got || []).forEach(it => {
    const path = it.path || it;
    if (!REQ_PATHS.includes(path)) REQ_PATHS.push(path);
  });
  drawReqPaths(ctx);
}

async function runReqCheck(ctx){
  const out = ctx.$("#reqResult"), status = ctx.$("#reqStatus");
  if (!ctx.project) return ctx.toast(ctx.t("work.pick_project"), "warn");
  if (!REQ_PATHS.length) return ctx.toast(ctx.t("work.req.no_paths"), "warn");
  const agent = reqAgent(ctx);
  if (status) status.textContent = ctx.t(agent.agent ? "work.req.checking_agent" : "work.req.checking");
  if (out) out.innerHTML = "";
  const d = await ctx.api("/api/requirements/check", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, paths: REQ_PATHS, ...agent})});
  if (!d || d.error){
    if (status) status.textContent = (d && d.error) || ctx.t("work.req.failed");
    return;
  }
  const s = d.summary || {};
  if (status) status.textContent = ctx.t("work.req.summary", {total: s.total || 0,
    found: s.found || 0, partial: s.partial || 0, missing: s.not_found || 0,
    percent: s.percent || 0});
  if (!out) return;
  if (d.index_error){
    out.append(ctx.el("div", {class: "warnbox"}, ctx.t("work.req.index_error", {why: d.index_error})));
  }
  if (d.agent){
    const ag = d.agent;
    out.append(ctx.el("div", {class: "muted", style: "font-size:12.5px;margin-top:6px"},
      ctx.t("work.req.agent_summary", {calls: ag.calls || 0, accepted: ag.accepted || 0,
        skipped: ag.skipped || 0, errors: ag.errors || 0})));
  }
  (d.documents || []).forEach(doc => {
    out.append(ctx.el("div", {style: "margin-top:10px"},
      ctx.el("b", {}, doc.title || doc.path),
      ctx.el("span", {class: "muted", style: "font-size:12px"}, " · " + doc.path)));
    if (doc.error){ out.append(ctx.el("div", {class: "warnbox"}, doc.error)); return; }
    const head = ctx.el("thead", {}, ctx.el("tr", {},
      ["#", ctx.t("work.req.col_text"), ctx.t("work.req.col_verdict"),
       ctx.t("work.req.col_where"), ctx.t("work.req.col_comment")]
        .map(x => ctx.el("th", {}, x))));
    const rows = (doc.requirements || []).map(row => {
      const where = (row.evidence || []).slice(0, 2)
        .map(e => e.file + ":" + e.line).join(", ") || "—";
      const verdict = row.final_verdict || row.verdict;
      const agent = row.agent || {};
      const note = agent.accepted ? ctx.t("work.req.agent_why", {why: agent.why || agent.verdict})
        : agent.error ? ctx.t("work.req.agent_error", {error: agent.error})
        : agent.skipped ? ctx.t("work.req.agent_error", {error: agent.skipped}) : "";
      const comment = [(row.missing || []).join(", ") || "—", note].filter(Boolean).join("; ");
      return ctx.el("tr", {},
        ctx.el("td", {class: "mono"}, row.id),
        ctx.el("td", {}, row.text),
        ctx.el("td", {}, ctx.t("work.req." + verdict)),
        ctx.el("td", {class: "mono", style: "font-size:11px"}, where),
        ctx.el("td", {class: "muted", style: "font-size:11.5px"}, comment));
    });
    const table = ctx.el("table", {class: "table", style: "margin-top:6px;width:100%"},
      head, ctx.el("tbody", {}, ...rows));
    out.append(table);
  });
  if (!(d.documents || []).length) out.append(ctx.el("div", {class: "muted"}, ctx.t("work.req.none")));
}

async function exportReq(ctx){
  if (!ctx.project) return ctx.toast(ctx.t("work.pick_project"), "warn");
  if (!REQ_PATHS.length) return ctx.toast(ctx.t("work.req.no_paths"), "warn");
  const agent = reqAgent(ctx);
  const d = await ctx.api("/api/requirements/export", {method: "POST", quiet: true,
    body: JSON.stringify({project: ctx.project.path, paths: REQ_PATHS, ...agent})});
  if (!d || d.error) return ctx.toast((d && d.error) || ctx.t("work.req.export_failed"), "err");
  ctx.toast(ctx.t("work.req.exported", {path: d.path}), "ok");
}

function reqAgent(ctx){
  const on = !!(ctx.$("#reqAgent") && ctx.$("#reqAgent").checked);
  const raw = parseInt((ctx.$("#reqAgentCalls") || {}).value || "10", 10);
  const max = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : 10;
  return {agent: on, max_calls: max};
}

// ---- ссылки и вложения ----

function addRef(ctx, ref){
  if (!REFS.some(r => r.path === ref.path)) REFS.push(ref);
  drawRefs(ctx);
}

// Подписи видов — таблицей, а не склейкой ключа: проверка каталогов видит только целые ключи.
const REF_KEY = {file: "work.ref_file", dir: "work.ref_dir", attach: "work.ref_attach"};
const KIND_KEY = {file: "work.kind_file", dir: "work.kind_dir", mcp: "work.kind_mcp",
                  skill: "work.kind_skill"};

function drawRefs(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#makeRefs");
  box.innerHTML = "";
  REFS.forEach(r => box.append(el("span", {class: "chip", title: r.path,
      style: "gap:6px;align-items:center;display:inline-flex"},
    t(REF_KEY[r.kind]) + " " + r.label,
    el("button", {class: "btn sm", style: "padding:0 6px;min-height:0", title: t("work.ref_remove"),
      "data-help": "work.help.ref_remove",
      onclick: () => { REFS = REFS.filter(x => x !== r); drawRefs(ctx); }}, "×"))));
}

async function pickRefs(ctx){
  if (!ctx.project) return ctx.toast(ctx.t("work.pick_project"), "warn");
  const got = await ctx.ui.pickPath({project: ctx.project.path, want: "any", multi: true, detail: true,
                                     title: ctx.t("work.pick_title")});
  (got || []).forEach(it => addRef(ctx, {path: it.path, label: it.kind === "dir" ? it.path + "/" : it.path, kind: it.kind}));
}

async function attach(ctx, files){
  const {t} = ctx;
  if (!ctx.project) return ctx.toast(t("work.pick_project"), "warn");
  for (const f of files){
    const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (ext && !ACCEPT.includes(ext)){ ctx.toast(t("work.attach_text_only", {name: f.name}), "warn"); continue; }
    if (f.size > 1_000_000){ ctx.toast(t("work.attach_too_big", {name: f.name}), "warn"); continue; }
    const text = await f.text();
    const r = await ctx.api("/api/context/upload", {method: "POST", quiet: true,
      body: JSON.stringify({project: ctx.project.path, name: f.name, text})});
    if (!r || r.error){ ctx.toast((r && r.error) || t("work.attach_failed", {name: f.name}), "err"); continue; }
    addRef(ctx, {path: r.path, label: r.name, kind: "attach"});
  }
}

// ---- подсказки по @ и / ----

function tokenAtCaret(area){
  const before = area.value.slice(0, area.selectionStart);
  const m = before.match(/(^|\s)([@/][^\s]*)$/);
  return m ? {text: m[2], start: before.length - m[2].length, end: area.selectionStart} : null;
}

function wireSuggest(ctx){
  const area = ctx.$("#makeIdea"), list = ctx.$("#makeSuggest");
  let items = [], pick = 0, timer = null, tok = null, gen = 0;
  const close = () => { list.hidden = true; items = []; };
  // Уход из раздела: таймер подсказки снят, запрос, уже ушедший к серверу, список не откроет.
  SUGGEST_STOP = () => { clearTimeout(timer); timer = null; gen++; close(); };
  const choose = it => {
    if (!tok) return close();
    const value = (it.kind === "file" || it.kind === "dir")
      ? "@" + (/\s/.test(it.value) ? '"' + it.value + '"' : it.value) : it.value;
    area.value = area.value.slice(0, tok.start) + value + " " + area.value.slice(tok.end);
    const at = tok.start + value.length + 1;
    area.setSelectionRange(at, at);
    area.focus();
    if (it.kind === "file" || it.kind === "dir") addRef(ctx, {path: it.value, label: it.label, kind: it.kind});
    close();
  };
  const draw = () => {
    const {el, t} = ctx;
    list.innerHTML = "";
    if (!items.length){ list.hidden = true; return; }
    items.forEach((it, i) => list.append(el("div", {class: "list-item",
        style: "cursor:pointer;padding:4px 8px" + (i === pick ? ";background:var(--surface-2,rgba(128,128,128,.15))" : ""),
        onmousedown: e => { e.preventDefault(); choose(it); }},
      el("span", {class: "chip", style: "flex:none"}, t(KIND_KEY[it.kind])),
      el("span", {class: "mono", style: "font-size:12px"}, it.label))));
    list.hidden = false;
  };
  area.addEventListener("input", () => {
    clearTimeout(timer);
    tok = tokenAtCaret(area);
    if (!tok || !ctx.project) return close();
    const mine = gen;
    timer = setTimeout(async () => {
      const d = await ctx.api("/api/context/suggest?project=" + encodeURIComponent(ctx.project.path)
        + "&q=" + encodeURIComponent(tok.text), {quiet: true});
      if (mine !== gen) return;
      items = (d && d.items) || []; pick = 0; draw();
    }, 180);
  });
  area.addEventListener("keydown", e => {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp"){
      e.preventDefault();
      pick = (pick + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length; draw();
    } else if (e.key === "Enter" || e.key === "Tab"){
      e.preventDefault(); choose(items[pick]);
    } else if (e.key === "Escape"){ close(); }
  });
  area.addEventListener("blur", () => setTimeout(close, 150));
}

export async function refresh(ctx){
  // На какой базе соберётся документ: дата её полного обновления, общая для команды (1.168.0).
  const kbLine = ctx.$("#workKb");
  if (kbLine){
    const kb = ctx.ui.kbInfo(ctx.project);
    kbLine.hidden = !ctx.project;
    kbLine.textContent = kb.at ? ctx.t("work.kb", {when: ctx.ui.stampText(kb.at)}) : ctx.t("work.kb_never");
    kbLine.title = kb.title;
    kbLine.style.color = kb.stale || !kb.at ? "var(--tier-inreview)" : "";
  }
  await fillKinds(ctx);
  await loadRuns(ctx);
  if ([...RUNS.values()].some(r => RUN_BUSY.has(r.status))) ensureTicker(ctx);
  await renderRoutes(ctx, "продуктивность", ctx.$("#workBody"));   // данные движка
}

async function fillKinds(ctx){
  const {t, el} = ctx;
  const sel = ctx.$("#makeKind");
  if (!sel) return;
  if (!ctx.project){
    sel.innerHTML = "";
    sel.append(el("option", {value: ""}, t("work.pick_project_first")));
    return;
  }
  const d = await ctx.api("/api/kinds?project=" + encodeURIComponent(ctx.project.path),
                          {quiet: true});
  const kinds = d.kinds || {};
  // Список чистим, только когда есть чем наполнить. Иначе один сорвавшийся запрос
  // стирал настроенные виды артефактов, и это выглядело как «настройки пропали».
  if (!Object.keys(kinds).length){
    sel.innerHTML = "";
    sel.append(el("option", {value: ""}, d.error
      ? t("work.kinds_failed", {why: d.error}) : t("work.kinds_none")));
    ctx.$("#makeSpec").textContent = "";
    ctx.$("#makeGo").disabled = true;
    return;
  }
  sel.innerHTML = "";
  Object.keys(kinds).sort().forEach(k => {
    const o = document.createElement("option");
    o.value = k;
    o.textContent = (kinds[k].title || k) + "  · " + k;
    sel.append(o);
  });
  const draw = () => {
    const rec = kinds[sel.value] || {};
    // Два дела — две строки: что относится к созданию документа и что к его публикации в Confluence.
    const create = [
      rec.template ? t("work.spec_template", {v: rec.template}) : t("work.spec_no_template"),
      rec.out ? t("work.spec_out", {v: rec.out}) : t("work.spec_no_out"),
      rec.prompt ? t("work.spec_prompt", {v: rec.prompt}) : t("work.spec_no_prompt"),
    ];
    const publish = [
      rec.publish_url ? t("work.spec_publish", {v: rec.publish_url}) : t("work.spec_no_publish"),
      ...(rec.publish_path ? [t("work.spec_path", {v: rec.publish_path})] : []),
      rec.publish_prompt ? t("work.spec_publish_prompt", {v: rec.publish_prompt}) : t("work.spec_no_publish_prompt"),
    ];
    ctx.$("#makeSpec").replaceChildren(
      el("div", {}, el("b", {}, t("work.spec_head_create") + " "), create.join(" · ")),
      el("div", {}, el("b", {}, t("work.spec_head_publish") + " "), publish.join(" · ")));
    drawMakeTask(ctx, sel.value);
    const ready = rec.template && rec.out;
    ctx.$("#makeGo").disabled = !ready;
    // Можно ли публиковать, решает сервер по каждому документу (то же состояние, что у кнопки в «Файлах»): корень
    // бывает и в шапке документа, и в конфиге, а не только у вида.
    ctx.$("#makePublish").disabled = !sel.value;
    ctx.$("#makePublish").title = t("work.publish_hint");
  };
  sel.onchange = draw;
  draw();
}

const RUN_BUSY = new Set(["starting", "planning", "running", "paused"]);
const RUN_CONTINUE = new Set(["partial", "error", "cancelled"]);
const MAX_PARALLEL_RUNS = 3;

export function makePipelineButtons(status, hasState = true, hasReport = false){
  const busy = RUN_BUSY.has(status);
  return {
    cancel: busy,
    pause: status === "running" || status === "paused",
    resume: status === "paused",
    continue: hasState && RUN_CONTINUE.has(status),
    report: Boolean(hasReport),
    open: hasState,
  };
}

function logLine(text, at){
  const date = at ? new Date(at) : new Date();
  const stamp = Number.isNaN(date.getTime()) ? ""
    : date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) + " ";
  return stamp + String(text || "");
}

export function applyMakeEvent(run, event){
  if (!run || !event) return run;
  const kind = event.kind;
  const log = text => {
    run.log = (run.log || []).concat(logLine(text)).slice(-80);
    run.lastAt = Date.now();
  };
  if (kind === "run"){
    run.sid = event.sid || run.sid;
    run.status = "running";
    log("run");
  } else if (kind === "plan"){
    run.plan = (event.steps || []).map(s => ({...s, status: s.status || "pending"}));
    run.status = "running";
    log("plan " + run.plan.length);
  } else if (kind === "stage"){
    run.current = event.stage || run.current;
    run.status = "running";
    log(event.stage + (event.chars ? " · " + event.chars : ""));
  } else if (kind === "check"){
    run.current = event.stage || run.current;
    run.status = "running";
    log((event.stage || "check") + ": " + (event.status || "done")
      + (event.issues != null ? " · issues " + event.issues : "")
      + (event.unsupported != null ? " · unsupported " + event.unsupported : ""));
  } else if (kind === "adapter_fallback"){
    log(event.message || "инструменты отключены");
  } else if (kind === "done"){
    run.status = event.status || "done";
    run.finishedAt = Date.now();
    run.artifact = event.artifact || run.artifact;
    run.report = event.report || run.report;
    run.metrics = event.metrics || run.metrics;
    log(run.status);
  } else if (kind === "error"){
    run.status = event.status || "error";
    run.finishedAt = Date.now();
    run.error = event.message || event.error || "";
    run.artifact = event.artifact || run.artifact;
    run.report = event.report || run.report;
    log(run.status + (run.error ? " · " + run.error : ""));
  }
  return run;
}

function runStatus(ctx, status){
  return ctx.t("work.status." + status);
}

export function runElapsed(run){
  // У завершённого запуска время считается до последнего обновления, а не до сейчас:
  // иначе после F5 «прошло» растёт вместе с возрастом вкладки.
  const end = run.finishedAt || (RUN_BUSY.has(run.status) ? Date.now() : run.lastAt || Date.now());
  const sec = Math.max(0, Math.round((end - (run.startedAt || end)) / 1000));
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}

export function runTimeLabel(ctx, run){
  const finished = Boolean(run.finishedAt) && !RUN_BUSY.has(run.status);
  return finished
    ? ctx.t("work.finished_at", {time: ctx.ui.stampText(run.finishedAt)})
    : ctx.t("work.last_event", {when: Math.max(0, Math.round((Date.now() - run.lastAt) / 1000))});
}

function ensureTicker(ctx){
  if (MAKE_TICKER) return;
  MAKE_TICKER = setInterval(() => {
    if (!RUNS.size) return;
    renderRuns(ctx);
    if (ACTIVE) drawRun(ctx);
  }, 1000);
}

function makeNewRun(ctx, kind, title){
  RUN_SEQ += 1;
  return {key: "local-" + RUN_SEQ, sid: "", job: "", kind, title,
          status: "starting", plan: [], current: "", log: [], questions: [],
          metrics: null, warnings: [], reportText: "", report: "", artifact: "",
          startedAt: Date.now(), lastAt: Date.now(), finishedAt: 0, hydrated: true,
          updated: "",
          rendered: false, reportLines: []};
}

// Вызов производства — тот же путь, что у любой команды: панель не знает другого способа
// запускать движок. Запуск идёт фоном, поэтому со страницы можно начать второй артефакт.
async function makeCall(ctx, args, run){
  run.status = "starting";
  run.finishedAt = 0;
  run.reportLines = [];
  run.reportText = "";
  run.error = "";
  run.rendered = false;
  renderRuns(ctx);
  if (ACTIVE === run.key) drawRun(ctx);
  const res = await ctx.api("/api/run", {method: "POST", body: JSON.stringify(
    {project: ctx.project.path, cmd: "agent:make", args: args.concat(["--emit-events"])})});
  if (!res || !res.job){
    run.status = "error";
    run.error = ctx.t("work.job_lost");
    renderRuns(ctx); if (ACTIVE === run.key) drawRun(ctx);
    return null;
  }
  run.job = res.job;
  ensureTicker(ctx);
  pollRun(ctx, run);
  return res.job;
}

async function pollRun(ctx, run){
  let since = 0;
  for (;;){
    const d = await ctx.api(`/api/job?id=${run.job}&since=${since}`, {quiet: true});
    if (!d || d.error){
      run.status = "error";
      run.error = ctx.t("work.job_lost");
      renderRuns(ctx); if (ACTIVE === run.key) drawRun(ctx);
      return;
    }
    for (const line of d.lines || []){
      if (line.startsWith("MAKE_EVENT ")){
        try { applyMakeEvent(run, JSON.parse(line.slice(11))); }
        catch (_e) { run.log = (run.log || []).concat(line); }
      } else {
        run.reportLines.push(line);
      }
    }
    if (run.sid && run.key !== run.sid){
      const old = run.key;
      RUNS.delete(old);
      run.key = run.sid;
      RUNS.set(run.key, run);
      if (ACTIVE === old) ACTIVE = run.key;
    }
    since = d.next || since + (d.lines || []).length;
    if (d.done){
      run.reportText = run.reportLines.join("\n").trim();
      if (!run.status || run.status === "starting") run.status = "done";
      await hydrateRun(ctx, run);
      renderRuns(ctx); if (ACTIVE === run.key) drawRun(ctx);
      return;
    }
    renderRuns(ctx); if (ACTIVE === run.key) drawRun(ctx);
    await new Promise(ok => setTimeout(ok, 700));
  }
}

async function hydrateRun(ctx, run){
  if (!run.sid) return;
  const d = await ctx.api(`/api/make/state?project=${encodeURIComponent(ctx.project.path)}`
    + `&sid=${encodeURIComponent(run.sid)}`, {quiet: true});
  if (!d || d.error) return;
  run.plan = (d.plan && d.plan.steps) || run.plan || [];
  run.state = (d.state && d.state.status) || run.state;
  run.questions = (d.state && d.state.questions) || run.questions || [];
  run.artifact = (d.artifact && d.artifact.path) || run.artifact;
  run.report = d.report_md || run.report;
  run.metrics = (d.report && d.report.metrics) || run.metrics;
  run.warnings = (d.state && d.state.warnings) || run.warnings;
  if (run.state && run.state !== "running" && run.status === "done") run.status = run.state;
  // Журнал после F5 собирается из сохранённых событий. Гоняем их на копии, чтобы
  // восстановление не переписывало статус и время завершения живыми часами.
  const replay = {plan: [], log: []};
  (d.events || []).forEach(event => {
    // В файле событий plan хранит число шагов, а в live-событии — массив. Готовый
    // message есть у обоих, поэтому он и становится строкой журнала; остальное
    // разбирает applyMakeEvent, не роняя восстановление на незнакомом виде.
    if (typeof event.message === "string" && event.message){
      replay.log = (replay.log || []).concat(logLine(event.message, event.at)).slice(-80);
      return;
    }
    try { applyMakeEvent(replay, event); }
    catch (_e) { /* одно событие не должно стирать весь журнал */ }
  });
  if (replay.log.length) run.log = replay.log;
  const updated = Date.parse((d.state && d.state.updated) || "") || 0;
  if (updated){
    run.lastAt = updated;
    if (!RUN_BUSY.has(run.state)) run.finishedAt = updated;
  }
  run.updated = (d.state && d.state.updated) || run.updated;
  run.hydrated = true;
}

function renderRuns(ctx){
  const box = ctx.$("#makeRuns");
  if (!box) return;
  box.innerHTML = "";
  if (!RUNS.size){ box.hidden = true; return; }
  box.hidden = false;
  const rows = [...RUNS.values()].sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
  rows.forEach(run => {
    const label = (run.kind || ctx.t("work.artifact")) + " · " + (run.title || run.sid || ctx.t("work.new_run"))
      + " · " + runStatus(ctx, run.status);
    const b = ctx.el("button", {class: "btn sm" + (run.key === ACTIVE ? " primary" : ""),
      title: label, onclick: () => { ACTIVE = run.key; drawRun(ctx); }}, label);
    box.append(b);
  });
  if (!ACTIVE && rows.length){ ACTIVE = rows[0].key; drawRun(ctx); }
}

function drawRun(ctx){
  const run = RUNS.get(ACTIVE);
  const box = ctx.$("#makeDialog");
  if (!box) return;
  if (!run){ box.innerHTML = ""; return; }
  const {t, el} = ctx;
  const done = run.artifact
    || ((run.reportText || "").match(/Документ: `([^`]+)`/) || [])[1] || "";
  box.innerHTML = "";
  const head = el("div", {class: "row", style: "gap:10px;align-items:center;flex-wrap:wrap"},
    el("b", {}, run.title || run.kind || t("work.artifact")),
    el("span", {class: "chip"}, runStatus(ctx, run.status)),
    el("span", {class: "muted", style: "font-size:12px"}, t("work.elapsed", {time: runElapsed(run)})),
    el("span", {class: "muted", style: "font-size:12px"}, runTimeLabel(ctx, run)));
  box.append(el("div", {class: "card", style: "padding:16px"}, head));
  if (run.plan && run.plan.length){
    const done = run.plan.filter(s => s.status === "done" || s.status === "partial").length;
    const plan = el("div", {class: "card", style: "padding:12px;margin-top:8px"},
      el("div", {class: "muted", style: "font-size:12px;margin-bottom:5px"},
        t("work.plan_summary", {done, total: run.plan.length})));
    run.plan.forEach((step, i) => {
      const mark = step.status === "done" ? "✅" : step.status === "partial" ? "◐"
        : step.status === "failed" ? "✗" : step.status === "running" ? "▶" : "☐";
      plan.append(el("div", {style: "font-size:12.5px"}, mark + " " + (i + 1) + ". "
        + (step.title || step.id)));
    });
    box.append(plan);
  }
  if (run.log && run.log.length){
    const log = el("div", {class: "card", style: "padding:12px;margin-top:8px;max-height:190px;overflow:auto"},
      el("div", {class: "muted", style: "font-size:12px;margin-bottom:5px"}, t("work.worklog")));
    run.log.slice(-14).forEach(line => log.append(el("div", {style: "font-size:12px"}, line)));
    box.append(log);
  }
  if (run.metrics){
    box.append(el("div", {class: "muted", style: "font-size:12px;margin-top:8px"},
      t("work.metrics", {calls: run.metrics.calls || 0, seconds: run.metrics.seconds || 0,
        tokens: (run.metrics.tokens_in || 0) + (run.metrics.tokens_out || 0),
        models: (run.metrics.models || []).join(", ") || "—"})));
  }
  if (run.warnings && run.warnings.length){
    box.append(el("div", {class: "warnbox"}, run.warnings.join(" · ")));
  }
  if (run.error){
    box.append(el("div", {class: "warnbox"}, run.error));
  }
  if (run.status === "starting"){
    box.append(el("div", {class: "muted", style: "font-size:12px;margin-top:8px"},
      t("work.making")));
  }
  if (run.status === "planning"){
    if (run.questions && run.questions.length){
      const q = el("div", {class: "card", style: "padding:12px;margin-top:8px"});
      run.questions.forEach((item, i) => q.append(el("div", {style: "font-size:12.5px"},
        (i + 1) + ". " + (item.q || item) + (item.rec ? " → " + item.rec : ""))));
      box.append(q);
    }
    const ans = el("textarea", {class: "btn", rows: 3, placeholder: t("work.answers_ph"),
      style: "width:100%;font-weight:400;margin-top:8px;resize:vertical"});
    box.append(ans, el("div", {class: "row", style: "gap:8px;margin-top:8px"},
      el("button", {class: "btn primary", "data-help": "work.help.answer",
        onclick: () => answerRun(ctx, run, ans.value.trim())},
        t("work.answer")),
      el("button", {class: "btn", title: t("work.enough_hint"),
        "data-help": "work.help.enough",
        onclick: () => enoughRun(ctx, run)}, t("work.enough"))));
  }
  if (run.reportText && !RUN_BUSY.has(run.status)){
    const body = run.reportText.split("\n").filter(l => !/^MAKE_EVENT /.test(l)).join("\n");
    box.append(el("div", {class: "card", style: "padding:16px;margin-top:8px"},
      el("div", {class: "ansbody", style: "white-space:pre-wrap", html: ctx.fmt.mdLite(body)})));
  }
  if (run.status === "done" && done && !run.opened){
    run.opened = true;
    setTimeout(() => { if (ctx.view === "work") ctx.openPath(done); }, 400);
  }
  const on = makePipelineButtons(run.status, Boolean(run.sid), Boolean(run.report));
  const row = el("div", {class: "row", style: "gap:8px;margin-top:10px;flex-wrap:wrap"});
  if (on.cancel) row.append(el("button", {class: "btn", onclick: () => controlRun(ctx, run, "cancel")},
    t("work.cancel")));
  if (on.pause) row.append(el("button", {class: "btn", onclick: () => controlRun(ctx, run, run.status === "paused" ? "resume" : "pause")},
    t(run.status === "paused" ? "work.resume" : "work.pause")));
  if (on.continue) row.append(el("button", {class: "btn primary", onclick: () => continueRun(ctx, run)},
    t("work.continue")));
  if (done) row.append(el("button", {class: "btn", "data-help": "work.help.open_done",
    onclick: () => ctx.openPath(done)},
    t("work.open_in_editor")));
  if (on.report) row.append(el("button", {class: "btn", onclick: () => ctx.openPath(run.report)},
    t("work.open_report")));
  box.append(row);
  run.rendered = true;
}

// Блок «Задача» в окне создания: свойства задачи нового документа (умолчания — из вида и настроек трекера, эпик — из
// списка эпиков проекта). Сохранять и заводить здесь нечего: документа ещё нет, значения уходят движку при «Создать».
async function drawMakeTask(ctx, kind){
  const host = ctx.$("#makeTask");
  if (!host || !ctx.ui.taskBlock) return;
  host.innerHTML = "";
  MAKE_TASK = null;
  if (!ctx.project || !kind) return;
  const box = await ctx.ui.taskBlock({kind, draft: true});
  if (ctx.$("#makeKind").value !== kind) return;
  MAKE_TASK = box;
  host.replaceChildren(box);
}

async function startMake(ctx){
  if (!ctx.project) return ctx.toast(ctx.t("work.pick_project"), "warn");
  const idea = ctx.$("#makeIdea").value.trim();
  if (!idea) return ctx.$("#makeIdea").focus();
  const busy = [...RUNS.values()].filter(r => RUN_BUSY.has(r.status)).length;
  if (busy >= MAX_PARALLEL_RUNS) return ctx.toast(ctx.t("work.run_limit", {n: MAX_PARALLEL_RUNS}), "warn");
  const kind = ctx.$("#makeKind").value;
  const run = makeNewRun(ctx, kind, kind);
  RUNS.set(run.key, run);
  ACTIVE = run.key;
  renderRuns(ctx);
  drawRun(ctx);
  const context = REFS.flatMap(r => ["--context", r.path]);
  const task = Object.entries(MAKE_TASK && MAKE_TASK.values ? MAKE_TASK.values() : {})
    .flatMap(([k, v]) => ["--task-field", k + "=" + v]);
  await makeCall(ctx, ["--kind", kind, "--idea", idea, ...context, ...task], run);
}

async function controlRun(ctx, run, action){
  if (!run.sid){
    if (action === "cancel" && run.job)
      await ctx.api("/api/job/stop", {method: "POST", body: JSON.stringify({id: run.job})});
    run.status = action === "cancel" ? "cancelled" : run.status;
    if (action === "cancel") run.finishedAt = Date.now();
    renderRuns(ctx); if (ACTIVE === run.key) drawRun(ctx);
    return;
  }
  const d = await ctx.api("/api/make/control", {method: "POST", body: JSON.stringify({
    project: ctx.project.path, sid: run.sid, action})});
  if (!d || d.error) return ctx.toast((d && d.error) || ctx.t("work.control_failed"), "err");
  if (action === "cancel") run.status = "cancelled";
  else if (action === "pause") run.status = "paused";
  else if (action === "resume") run.status = "running";
  if (action === "cancel") run.finishedAt = Date.now();
  renderRuns(ctx); if (ACTIVE === run.key) drawRun(ctx);
}

async function continueRun(ctx, run){
  if (!run.sid) return;
  await controlRun(ctx, run, "continue");
  await makeCall(ctx, ["--session", run.sid], run);
}

async function answerRun(ctx, run, answers){
  if (!run.sid) return;
  if (!answers) return ctx.toast(ctx.t("work.answer_empty"), "warn");
  await makeCall(ctx, ["--session", run.sid, "--answers", answers], run);
}

async function enoughRun(ctx, run){
  if (!run.sid) return;
  await makeCall(ctx, ["--session", run.sid, "--enough"], run);
}

export async function loadRuns(ctx){
  const project = ctx.project ? ctx.project.path : "";
  if (project !== SHOWN_PROJECT){
    RUNS.clear(); ACTIVE = ""; SHOWN_PROJECT = project;
  }
  if (!project){ renderRuns(ctx); return; }
  const d = await ctx.api(`/api/make/runs?project=${encodeURIComponent(project)}`, {quiet: true});
  (d && d.runs || []).forEach(row => {
    const key = row.sid;
    let run = RUNS.get(key);
    if (!run){
      RUN_SEQ += 1;
      run = {key, sid: key, job: "", kind: row.kind || "", title: row.title || key,
             status: row.status || "unknown", plan: [], current: row.stage || "", log: [],
             questions: [], metrics: null, warnings: [], reportText: "", report: row.report || "",
             artifact: row.artifact || "", startedAt: Date.parse(row.started || "") || Date.now(),
             lastAt: Date.parse(row.updated || "") || Date.now(),
             finishedAt: RUN_BUSY.has(row.status) ? 0 : Date.parse(row.updated || "") || 0,
             updated: row.updated || "",
             hydrated: false,
             rendered: false, reportLines: []};
      RUNS.set(key, run);
    } else if (!run.job){
      const changed = Boolean(row.updated) && row.updated !== run.updated;
      run.kind = row.kind || run.kind;
      run.title = row.title || run.title;
      run.status = row.status || run.status;
      run.artifact = row.artifact || run.artifact;
      run.report = row.report || run.report;
      if (!RUN_BUSY.has(run.status)) run.finishedAt = run.finishedAt || Date.parse(row.updated || "") || 0;
      // Запуск мог измениться в другой вкладке. Перечитываем состояние, когда
      // сервер говорит, что оно новее, а не один раз за всю жизнь карточки.
      if (changed){
        run.updated = row.updated;
        run.hydrated = false;
      }
    }
  });
  // План, журнал и метрики лежат в состоянии запуска, а не в списке. Без этого
  // после F5 остаётся только заголовок, хотя работа уже сделана.
  for (const run of RUNS.values()){
    if (!run.sid || run.hydrated) continue;
    try { await hydrateRun(ctx, run); }
    catch (_e) { /* список уже показан; следующий refresh повторит */ }
  }
  renderRuns(ctx);
}

// Снимок для проверок раздела: состояние карты запусков не должно быть видно
// production-коду, но тесту нужен именно результат loadRuns.
export function runsSnapshot(){
  return [...RUNS.values()];
}

// Публикация: выбрать из готового, а не набирать путь руками — первая же опечатка ушла
// бы в Confluence чужой страницей. Состояние у каждого документа считает сервер (`/api/artifacts` → `publish`, то же,
// что у кнопки в «Файлах»); публикация идёт тем же путём, что в «Файлах»: документ открывается в редакторе с карточкой
// «что и куда уйдёт» и кнопками по исходу. Черновик — причина словами и кнопка с явной надписью, без окна подтверждения.
// Уточняющий промпт остаётся заданием ассистенту: правку текста перед отправкой делает тот, кто её видит.
async function publish(ctx){
  const {t, el} = ctx;
  if (!ctx.project) return ctx.toast(t("work.pick_project"), "warn");
  const kind = ctx.$("#makeKind").value;
  const box = ctx.$("#makeDialog");
  box.innerHTML = "";
  const d = await ctx.api(`/api/artifacts?project=${encodeURIComponent(ctx.project.path)}`
                          + `&kind=${encodeURIComponent(kind)}`);
  const files = d.files || [];
  if (!files.length)
    return box.append(el("div", {class: "card", style: "padding:18px"}, t("work.publish_empty")));

  const sel = el("select", {class: "btn", style: "font-weight:400;min-width:300px"});
  files.forEach(f => {
    const o = document.createElement("option");
    o.value = f.rel;
    o.textContent = f.name + " · " + f.status
      + (f.published ? " · " + t("work.published_at", {when: f.published})
                     : " · " + t("work.never_published"));
    sel.append(o);
  });
  const note = el("textarea", {class: "btn", rows: 2, placeholder: t("work.publish_note_ph"),
    style: "width:100%;font-weight:400;margin-top:10px;resize:vertical"});
  const hint = el("div", {class: "muted", style: "font-size:12px;margin-top:6px"});
  const fix = el("div", {class: "row", style: "gap:8px;margin-top:6px", hidden: ""});
  // Блок «Задача» выбранного документа: свойства задачи, «Сохранить» в шапку, «Создать задачу» или ссылка на неё.
  const taskHost = el("div", {style: "margin-top:10px"});
  const drawTask = async rel => {
    taskHost.innerHTML = "";
    if (!rel || !ctx.ui.taskBlock) return;
    const tb = await ctx.ui.taskBlock({path: rel});
    if (sel.value === rel) taskHost.replaceChildren(tb);
  };
  const go = el("button", {class: "btn primary", "data-help": "work.help.publish_go", onclick: () => {
    const f = files.find(x => x.rel === sel.value) || {};
    if (f.rel) ctx.ui.publishFile(f.rel);
  }}, t("work.publish_go"));
  const draw = () => {
    const f = files.find(x => x.rel === sel.value) || {};
    const pub = f.publish || {};
    const draft = f.status === "draft";
    fix.innerHTML = "";
    fix.hidden = true;
    go.disabled = pub.state !== "ready";
    go.textContent = t(draft ? "work.publish_go_draft" : "work.publish_go");
    if (pub.state === "blocked"){
      hint.textContent = pub.why || "";
      if (pub.fix){
        fix.hidden = false;
        fix.append(el("button", {class: "btn sm", onclick: () => ctx.ui.openSettings(pub.fix)},
          t(pub.fix === "kinds" ? "work.publish_fix_kinds" : "work.publish_fix_project")));
      }
    } else if (pub.state !== "ready"){
      hint.textContent = t("work.publish_none");
    } else {
      // Путь, который посчитал сервер: корень вида › разделы › заголовок — до всякой отправки.
      const where = (pub.path || []).concat(pub.title ? [pub.title] : []).join(" › ");
      hint.textContent = [draft ? t("work.publish_draft_warn")
        : (f.url ? t("work.publish_again", {url: f.url}) : t("work.publish_new")),
        where ? t("work.publish_where", {where}) : ""].filter(Boolean).join(" · ");
    }
  };
  sel.onchange = () => { draw(); drawTask(sel.value); };
  draw();
  drawTask(sel.value);

  box.append(el("div", {class: "card", style: "padding:18px"},
    el("div", {style: "font-weight:700;margin-bottom:8px"}, t("work.publish_title")),
    sel, hint, fix, taskHost, note,
    el("div", {class: "row", style: "gap:10px;margin-top:10px"},
      go,
      el("button", {class: "btn", "data-help": "work.help.copy_task", onclick: () => {
        const f = files.find(x => x.rel === sel.value) || {};
        const task = t("work.publish_task", {rel: f.rel})
          + (note.value.trim() ? "\n" + t("work.publish_task_note", {note: note.value.trim()}) : "")
          + "\n" + t("work.publish_task_tail");
        navigator.clipboard.writeText(task)
          .then(() => ctx.toast(t("work.task_copied"), "ok"))
          .catch(() => ctx.toast(t("work.task_clipboard_off"), "warn"));
      }}, t("work.copy_task")))));
}

// Подсказки по `@` и `/` нужны только на виду. Опрос задания (`makeCall`) и ответ планировщика
// уходом не прерываются: результат ложится прямо в разметку раздела и ждёт возвращения.
export function onHide(){
  if (SUGGEST_STOP) SUGGEST_STOP();
  if (MAKE_TICKER){ clearInterval(MAKE_TICKER); MAKE_TICKER = null; }
}

export const lifecycle = 1;
export default {mount, refresh, onHide};
