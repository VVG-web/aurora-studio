/* Спросить базу — раздел-модуль.

   Вопрос своими словами: движок собирает контекст из карточек, модель отвечает только по
   ним. Карточки не правятся, но разговор ложится в базу проекта и уходит в git — что
   спрашивала команда и что база ответила, видно всем.

   Функции `fillBackends` и `renderHistory` экспортированы отдельно: их поведение
   проверяется прогоном в node на заглушках DOM и сервера (`tests/run_tests.py`). */

let ASKING = false;
let THREAD = null;          // открытый разговор: id и число вопросов в нём
let SHOWN = "";             // проект, для которого нарисован разговор
let PIPE = {id:"", status:"idle", job:"", question:"", plan:[], current:0,
            warnings:[], metrics:null, answer:"", lines:[], timer:0};

const PIPE_BUSY = new Set(["planning", "running", "paused"]);
const PIPE_RETRY = new Set(["error", "cancelled", "partial"]);

function pipeEl(ctx, id, props = {}){
  const node = ctx.$("#" + id);
  if (node) Object.assign(node, props);
  return node;
}

export function pipelineButtons(state, hasState = true){
  // Матрица одна на отрисовку и на тест: во время работы «Новый запрос» и
  // «Попробовать снова» не должны уводить от живого прогона, а после —
  // наоборот, должны быть доступны.
  const busy = PIPE_BUSY.has(state);
  return {
    new: !busy,
    cancel: hasState && pipeBusyState(state),
    pause: hasState && (state === "running" || state === "paused"),
    retry: hasState && PIPE_RETRY.has(state),
  };
}

export function pipelineButtonState(state){ return pipelineButtons(state, true); }

function pipeBusyState(state){ return PIPE_BUSY.has(state); }

function setPipeButtons(ctx, state, hasState = true){
  const on = pipeBusyState(state);
  const m = pipeBusyState(state) ? {cancel:true, pause:true, retry:false}
                                 : {cancel:false, pause:false, retry:PIPE_RETRY.has(state)};
  for (const [id, show] of [["askCancel", m.cancel], ["askPause", showPause(state)],
                             ["askRetry", m.retry]]) {
    const btn = pipeEl(ctx, id);
    if (btn) btn.hidden = !hasState || !show;
  }
  const pause = pipeEl(ctx, "askPause");
  if (pause) pause.textContent = state === "paused"
      ? ctx.t("ask.resume") : ctx.t("ask.pause");
  const newBtn = pipeEl(ctx, "askNew");
  if (newBtn) newBtn.disabled = on;
}

function showPause(state){ return state === "running" || state === "paused"; }

function resetPipeline(ctx){
  if (PIPE.timer) clearInterval(PIPE.timer);
  PIPE = {id:"", status:"idle", job:"", question:"", plan:[], current:0,
          warnings:[], metrics:null, answer:"", lines:[], timer:0};
  const box = pipeEl(ctx, "askPipelineBox");
  if (box) box.hidden = true;
  for (const id of ["askPlanBox", "askLong"]) {
    const node = pipeEl(ctx, id); if (node) node.hidden = true;
  }
  setPipeButtons(ctx, "idle", false);
}

function workLine(ctx, text){
  const node = pipeEl(ctx, "askWork");
  if (!node) return;
  const line = document.createElement("div");
  line.textContent = new Date().toLocaleTimeString(ctx.lang === "en" ? "en-GB" : "ru-RU")
                     + " · " + text;
  node.append(line);
  node.scrollTop = node.scrollHeight;
}

function drawPipelinePlan(ctx){
  const box = pipeEl(ctx, "askPlanBox"), out = pipeEl(ctx, "askPlan");
  if (!box || !out) return;
  if (!PIPE.plan.length){ box.hidden = true; return; }
  box.hidden = false;
  const done = PIPE.plan.filter(s => s.status === "done" || s.status === "partial").length;
  const cur = PIPE.plan[PIPE.current] || PIPE.plan.find(s => s.status === "running");
  const idx = cur ? PIPE.plan.indexOf(cur) + 1 : Math.min(done + 1, PIPE.plan.length);
  const sum = pipeEl(ctx, "askPlanSummary");
  if (sum) sum.textContent = ctx.t("ask.plan_summary_line", {
    current: idx, total: PIPE.plan.length, title: cur ? cur.title : "—"});
  out.replaceChildren();
  PIPE.plan.forEach((step, i) => {
    const row = document.createElement("div");
    const mark = step.status === "done" ? "✅" : step.status === "partial" ? "◐"
               : step.status === "failed" ? "✗" : step.status === "running" ? "▶" : "☐";
    row.textContent = `${mark} ${i + 1}. ${step.title || step.id}`
                    + (step.brief ? ` — ${step.brief}` : "");
    out.append(row);
  });
}

function drawPipelineMetrics(ctx){
  const box = pipeEl(ctx, "askMetrics");
  if (!box) return;
  const m = PIPE.metrics;
  if (!m){ box.textContent = ""; return; }
  const pool = m.pool || {};
  box.textContent = ctx.t("ask.metrics", {
    seconds: Math.round(m.total_seconds || 0),
    ok: Math.round((m.success_rate || 0) * 100),
    errors: m.errors || 0,
    retries: m.retries || 0,
    tokens: m.context_tokens || 0,
    pool: pool.width || 0,
    models: (m.models || []).join(", ") || "—",
  });
}

export function mount(ctx){
  ctx.root.dataset.module = "ask";

  ctx.$("#askGo").onclick = () => askBase(ctx);
  ctx.$("#askText").addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") askBase(ctx);
  });
  ctx.$("#askNew").onclick = () => {
    THREAD = null;
    ctx.$("#askBody").innerHTML = "";
    resetPipeline(ctx);
    drawThreadHint(ctx);
    drawExport(ctx);
  };
  if (ctx.$("#askCancel")) ctx.$("#askCancel").onclick = () => controlPipe(ctx, "cancel");
  if (ctx.$("#askPause")) ctx.$("#askPause").onclick = () =>
    controlPipe(ctx, PIPE.status === "paused" ? "resume" : "pause");
  if (ctx.$("#askRetry")) ctx.$("#askRetry").onclick = () => retryPipe(ctx);
  ctx.$("#askExport").onclick = () => exportMd(ctx);
  ctx.$("#askPing").onclick = e => pingPrimary(ctx, e.target);
  ctx.$("#askPrimary").onclick = async () => {
    const r = await ctx.api("/api/agent/retry-primary", {method: "POST", body: "{}"});
    if (r && r.ok) ctx.toast(r.note || ctx.t("ask.primary_note"), "ok");
  };
}

export async function refresh(ctx){
  // Разговор принадлежит проекту: открытым его нести в другой нельзя. Раньше это
  // делало ядро прямо в выборе проекта — знание о разговоре жило вне раздела.
  const project = ctx.project ? ctx.project.path : "";
  // На какой базе стоит ответ: дата её полного обновления, общая для команды (1.168.0).
  const kbLine = ctx.$("#askKb");
  if (kbLine){
    const kb = ctx.ui.kbInfo(ctx.project);
    kbLine.hidden = !ctx.project;
    kbLine.textContent = kb.at ? ctx.t("ask.kb", {when: ctx.ui.stampText(kb.at)}) : ctx.t("ask.kb_never");
    kbLine.title = kb.title;
    kbLine.style.color = kb.stale || !kb.at ? "var(--tier-inreview)" : "";
  }
  if (project !== SHOWN){
    SHOWN = project;
    THREAD = null;
    ctx.$("#askBody").innerHTML = "";
  }
  ctx.$("#askText").focus();
  drawWho(ctx);                // подпись «кто ответил» — на языке интерфейса, и после его смены
  await renderHistory(ctx);
  drawThreadHint(ctx);
  drawExport(ctx);
  await fillBackends(ctx);
}

async function askBase(ctx){
  const {t, el} = ctx;
  if (ASKING) return;
  if (!ctx.project) return ctx.toast(t("ask.pick_project"), "warn");
  const q = ctx.$("#askText").value.trim();
  if (!q) return ctx.$("#askText").focus();
  const mode = ctx.$("#askMode").value;
  const box = ctx.$("#askBody");
  ASKING = true;
  const args = ["--question", q, "--mode", mode];
  // «Роль или модель»: роль — цепочкой с запасными, модель — ровно она (1.162.0).
  const pick = (ctx.$("#askBackend") || {}).value || "0";
  if (pick.startsWith("role:")) args.push("--role", pick.slice(5));
  else if (pick.startsWith("model:")) args.push("--model", pick.slice(6));
  if (THREAD) args.push("--thread", THREAD.id);
  if (ctx.$("#askProject") && !ctx.$("#askProject").checked) args.push("--no-semantic");
  const item = el("div", {class: "card", style: "padding:18px;margin-top:14px"},
    el("div", {style: "font-weight:700"}, (THREAD ? t("ask.refine_prefix") : "") + q),
    el("div", {class: "muted", style: "font-size:12.5px;margin-top:4px"},
      THREAD ? t("ask.asking_thread") : t("ask.asking")));
  box.prepend(item);

  const pipeline = ctx.$("#askPipeline") && ctx.$("#askPipeline").checked;
  if (pipeline) return askPipeline(ctx, q, args, item, box);

  const res = await ctx.api("/api/run", {method: "POST", body: JSON.stringify(
    {project: ctx.project.path, cmd: "agent:ask", args})});
  if (!res.job){ ASKING = false; return; }
  let since = 0, lines = [];
  for (;;){
    const d = await ctx.api(`/api/job?id=${res.job}&since=${since}`, {quiet: true});
    lines = lines.concat(d.lines || []);
    since = d.next;
    if (d.done) break;
    await new Promise(ok => setTimeout(ok, 500));
  }
  ASKING = false;
  // Первые строки — служебная шапка отчёта; человеку нужен сам ответ и подпись под ним.
  // Строки про файл разговора — тоже служебные: сам файл панель показывает иначе.
  const body = lines.filter(l => !/^# Ответ базы|^\*\*Вопрос:|^Разговор: |^Уточнить, не/
                                  .test(l)).join("\n").trim();   // данные движка
  // Куда лёг разговор, движок печатает сам: панель не угадывает имя файла, а читает его.
  const said = (lines.find(l => /^Разговор: /.test(l)) || "").match(/`([^`]+)`/);  // данные движка
  if (said) THREAD = {id: said[1].split("/").pop().replace(/\.md$/, "")};
  item.innerHTML = "";
  // Исходный markdown держим на самой карточке: на экране он уже разобран в HTML, а
  // выгружать надо то, что писала модель, — со ссылками `[[…]]`, живыми в Obsidian.
  item.dataset.q = q;
  item.dataset.a = body;
  item.dataset.at = new Date().toLocaleString(ctx.lang === "en" ? "en-GB" : "ru-RU");
  item.append(el("div", {style: "font-weight:700"}, q),
    el("div", {class: "ansbody", style: "margin-top:10px;white-space:pre-wrap",
               html: ctx.fmt.mdLite(body)}));
  // Подпись ответа печатает сам движок: «модель: X (бэкенд №N) · N с». Берём оттуда, а не
  // из выбора в форме: по кольцу мог ответить не тот, кого просили, и человек должен это
  // видеть — иначе медленный запасной ответ выглядит как ответ основной модели.
  const who = (lines.find(l => /модель:/.test(l)) || "")                      // данные движка
    .match(/модель:\s*([^\s(·]+)[^)]*\(бэкенд №(\d+)\)/);                     // данные движка
  if (who){
    drawWho(ctx, {kind: "answer", model: who[1], n: who[2]});
    if (who[2] !== "1") ctx.toast(t("ask.spare_toast"), "warn");
  }
  ctx.$("#askText").value = "";
  drawThreadHint(ctx);
  drawExport(ctx);
  await renderHistory(ctx);
}

async function askPipeline(ctx, question, args, item, box){
  const {t} = ctx;
  if (PIPE.timer) clearInterval(PIPE.timer);
  PIPE = {id:"", status:"planning", job:"", question, plan:[], current:0,
          warnings:[], metrics:null, answer:"", lines:[], args:[...args],
          startedAt: Date.now(), timer: 0};
  PIPE.timer = setInterval(() => {
    if (pipeBusyState(PIPE.status) && Date.now() - PIPE.startedAt > 600000)
      nodeHidden(pipeEl(ctx, "askLong"), false);
  }, 10000);
  const boxPanel = pipeEl(ctx, "askPipelineBox");
  if (boxPanel) boxPanel.hidden = false;
  if (ctx.$("#askWork")) ctx.$("#askWork").replaceChildren();
  if (ctx.$("#askWarn")) ctx.$("#askWarn").textContent = "";
  if (ctx.$("#askPlan")) ctx.$("#askPlan").replaceChildren();
  pipeEl(ctx, "askPipelineState", {textContent: t("ask.pipeline_planning")});
  setPipeButtons(ctx, "planning", true);
  let res;
  try {
    res = await ctx.api("/api/run", {method: "POST", body: JSON.stringify(
      {project: ctx.project.path, cmd: "agent:ask",
       args: [...args, "--pipeline", "--emit-events"]})});
  } catch (e) { res = {error: String(e && e.message || e)}; }
  if (!res || res.error || !res.job){
    clearInterval(PIPE.timer); PIPE.timer = 0;
    ASKING = false;
    PIPE.status = "error";
    const why = (res && (res.error || res.net_blocked?.blocked)) || ctx.t("ask.no_answer");
    pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_error")});
    if (ctx.$("#askWarn")) ctx.$("#askWarn").textContent = String(why);
    setPipeButtons(ctx, "error", true);
    item.append(ctx.el("div", {class:"muted", style:"margin-top:8px"}, String(why)));
    return;
  }
  PIPE.job = res.job;
  let since = 0;
  for (;;){
    let d;
    try { d = await ctx.api(`/api/job?id=${encodeURIComponent(res.job)}&since=${since}`,
                             {quiet: true}); }
    catch (e) { d = {lines: [], done: true, error: String(e && e.message || e)}; }
    for (const line of (d.lines || [])){
      if (line.startsWith("ASK_EVENT ")){
        try { handlePipeEvent(ctx, JSON.parse(line.slice(10))); } catch (e) { /* не тот формат */ }
      } else {
        PIPE.lines.push(line);
      }
    }
    since = d.next || since;
    if (d.done) break;
    await new Promise(ok => setTimeout(ok, 500));
  }
  ASKING = false;
  clearInterval(PIPE.timer); PIPE.timer = 0;
  if (!PIPE.answer && PIPE.status !== "done" && PIPE.status !== "partial"){
    PIPE.status = "error";
    pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_error")});
    if (ctx.$("#askWarn") && !ctx.$("#askWarn").textContent)
      ctx.$("#askWarn").textContent = ctx.t("ask.no_answer");
  }
  if (!PIPE.answer){
    PIPE.answer = PIPE.lines.filter(l => !/^# Ответ базы|^\*\*Вопрос:|^Разговор: |^Уточнить, не/.test(l))
      .join("\n").trim();   // данные движка: служебная шапка отчёта
  }
  const said = (PIPE.lines.find(l => /^Разговор: /.test(l)) || "").match(/`([^`]+)`/);
  if (!PIPE.id && said) PIPE.id = said[1].split("/").pop().replace(/\.md$/, "");
  if (PIPE.lines.length && PIPE.status === "planning") PIPE.status = "done";
  if (PIPE.id) THREAD = {id: PIPE.id};
  item.dataset.q = question;
  item.dataset.a = PIPE.answer || (ctx.$("#askWarn")?.textContent || "");
  item.dataset.at = new Date().toLocaleString(ctx.lang === "en" ? "en-GB" : "ru-RU");
  item.innerHTML = "";
  item.append(ctx.el("div", {style:"font-weight:700"}, question),
    ctx.el("div", {class:"ansbody", style:"margin-top:10px;white-space:pre-wrap",
                   html: ctx.fmt.mdLite(PIPE.answer || ctx.t("ask.no_answer"))}));
  if (PIPE.answer) ctx.$("#askText").value = "";
  drawThreadHint(ctx);
  drawExport(ctx);
  setPipeButtons(ctx, PIPE.status, Boolean(PIPE.id));
  await renderHistory(ctx);
}

function handlePipeEvent(ctx, ev){
  if (!ev || !PIPE) return;
  const event = ev;
  const kind = event.kind;
  if (kind === "plan"){
    PIPE.id = String(event.id || "");
    PIPE.plan = (event.steps || []).map(s => ({...s, status: "pending"}));
    PIPE.status = "running";
    pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_running")});
    drawPipelinePlan(ctx);
    setPipeButtons(ctx, "running", Boolean(PIPE.id));
    workLine(ctx, ctx.t("ask.delegated", {n: PIPE.plan.length}));
    return;
  }
  if (kind === "step_start"){
    const i = PIPE.plan.findIndex(s => s.id === event.step);
    if (i >= 0){ PIPE.plan[i].status = "running"; PIPE.current = i; }
    drawPipelinePlan(ctx);
    workLine(ctx, ctx.t("ask.step_start", {step: event.step, role: event.role || "worker",
                                            slot: event.slot ?? "—"}));
    return;
  }
  if (kind === "step_done" || kind === "step_error"){
    const i = PIPE.plan.findIndex(s => s.id === event.step);
    if (i >= 0) PIPE.plan[i].status = kind === "step_done" ? "done" : "failed";
    drawPipelinePlan(ctx);
    const head = kind === "step_done"
      ? ctx.t("ask.step_done", {role: event.role || "worker", model: event.model || "—",
                                 seconds: event.seconds ?? 0, tokens: event.tokens || 0})
      : ctx.t("ask.step_error", {step: event.step, error: event.message || event.error || ""});
    workLine(ctx, head);
    if (event.preview) workLine(ctx, "… " + String(event.preview).slice(0, 300));
    return;
  }
  if (kind === "check"){
    workLine(ctx, ctx.t("ask.check", {role: event.role || "", status: event.status || ""}));
    return;
  }
  if (kind === "paused"){
    PIPE.status = "paused";
    pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_paused")});
    setPipeButtons(ctx, "paused", Boolean(PIPE.id));
    return;
  }
  if (kind === "synth"){
    workLine(ctx, ctx.t("ask.synth", {status: event.status || ""}));
    return;
  }
  if (kind === "done"){
    PIPE.answer = String(event.answer || PIPE.answer || "");
    PIPE.metrics = event.metrics || null;
    PIPE.warnings = event.warnings || [];
    PIPE.status = event.status === "done" ? "done" : "partial";
    pipeEl(ctx, "askPipelineState", {textContent:
      PIPE.status === "done" ? ctx.t("ask.pipeline_done") : ctx.t("ask.pipeline_partial")});
    if (event.steps){
      PIPE.plan = (PIPE.plan || []).map((s, i) => ({...s, status:
        (event.steps.find(x => x.id === s.id) || {}).status || s.status}));
    }
    drawPipelinePlan(ctx);
    drawPipelineMetrics(ctx);
    const warn = pipeEl(ctx, "askWarn");
    if (warn) warn.textContent = (PIPE.warnings || []).join(" · ");
    const long = pipeEl(ctx, "askLong");
    if (long) nodeHidden(long, !(metricsLong(PIPE.metrics)));
    setPipeButtons(ctx, PIPE.status, Boolean(PIPE.id));
    return;
  }
  if (kind === "error"){
    PIPE.status = "error";
    pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_error")});
    if (ctx.$("#askWarn")) ctx.$("#askWarn").textContent = event.message || event.error || "";
    setPipeButtons(ctx, "error", Boolean(PIPE.id));
  }
}

function nodeHidden(node, hidden){ if (node) node.hidden = Boolean(hidden); }

function metricsLong(m){ return Boolean(m && (m.long || Number(m.total_seconds || 0) > 600)); }

async function controlPipe(ctx, action){
  if (!PIPE.id && !PIPE.job){
    if (action === "cancel" && PIPE.job)
      return ctx.api("/api/job/stop", {method:"POST", body: JSON.stringify({id: PIPE.job})});
    return;
  }
  if (!PIPE.id && PIPE.job && action === "cancel")
    return ctx.api("/api/job/stop", {method:"POST", body: JSON.stringify({id: PIPE.job})});
  if (!PIPE.id) return;
  const r = await ctx.api("/api/ask/control", {method:"POST", body: JSON.stringify(
    {project: ctx.project.path, id: PIPE.id, action})});
  if (r && r.ok){
    if (action === "pause"){ PIPE.status = "paused"; setPipeButtons(ctx, "paused", true);
      pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_paused")}); }
    if (action === "resume"){ PIPE.status = "running"; setPipeButtons(ctx, "running", true);
      pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_running")}); }
    if (action === "cancel"){ PIPE.status = "cancelled"; setPipeButtons(ctx, "cancelled", true);
      pipeEl(ctx, "askPipelineState", {textContent: ctx.t("ask.pipeline_cancelled")}); }
  } else {
    ctx.toast((r && r.error) || ctx.t("ask.no_answer"), "warn");
  }
}

async function retryPipe(ctx){
  if (!PIPE.question || !PIPE.args || !PIPE.id || ASKING) return;
  const body = ctx.$("#askBody");
  const item = ctx.el("div", {class:"card", style:"padding:18px;margin-top:14px"},
    ctx.el("div", {style:"font-weight:700"}, PIPE.question),
    ctx.el("div", {class:"muted", style:"font-size:12.5px;margin-top:4px"},
           ctx.t("ask.retrying")));
  body.prepend(item);
  PIPE.status = "planning";
  PIPE.answer = "";
  ASKING = true;
  await askPipeline(ctx, PIPE.question,
                    [...PIPE.args.filter(x => x !== "--resume"), "--resume",
                     "--thread", PIPE.id], item, body);
}

// Подпись «кто ответил» хранит не готовый текст, а то, из чего он собран: язык интерфейса
// сменили — строка собирается заново на новом языке, а выгрузка берёт модель из данных, а не
// отрезает приставку от надписи (по-русски «модель: », по-английски — другая, и отрезать было
// нечего: в файл попадало «Model: model: X»).
export function drawWho(ctx, state){
  const chip = ctx.$("#askWho"), {t} = ctx;
  if (state) Object.assign(chip.dataset, {kind: state.kind || "", model: state.model || "",
                                          n: state.n || ""});
  const {kind, model, n} = chip.dataset;
  chip.textContent = kind === "answer"
      ? t("ask.who_line", {model, n}) + (n === "1" ? "" : t("ask.who_spare"))
    : kind === "ping_ok" ? t("ask.primary_answers") + (model || "—")
    : kind === "ping_fail" ? t("ask.primary_silent") + (model || "—")
    : t("ask.who_none");
}

function drawThreadHint(ctx){
  const {t, el} = ctx;
  const hint = ctx.$("#askThreadHint"), btn = ctx.$("#askNew");
  if (!THREAD){
    hint.hidden = true;
    btn.hidden = true;
    ctx.$("#askGo").textContent = t("ask.go");
    ctx.$("#askText").placeholder = t("ask.placeholder");
    return;
  }
  hint.hidden = false;
  btn.hidden = false;
  ctx.$("#askGo").textContent = t("ask.refine");
  ctx.$("#askText").placeholder = t("ask.placeholder_thread");
  hint.innerHTML = "";
  hint.append(el("span", {}, t("ask.thread_before")),
              el("span", {class: "mono"}, THREAD.id),
              el("span", {}, t("ask.thread_after")));
}

// Ответ читают не только в панели: его несут в задачу, в письмо, в саму базу — а
// перенос выделением теряет ссылки `[[…]]` и переносы строк. Выгружаем тот markdown,
// который написала модель, целиком: вопрос, ответ и подпись, откуда он взялся.
function cards(ctx){
  return Array.from(ctx.$("#askBody").querySelectorAll("[data-a]"));
}

function drawExport(ctx){
  const btn = ctx.$("#askExport");
  if (btn) btn.hidden = !cards(ctx).length;
}

export function markdown(ctx){
  const {t} = ctx;
  // Карточки на экране лежат свежим сверху; в файле разговор читают с начала.
  const rows = cards(ctx).reverse();
  const head = [t("ask.md_title"), "",
    t("ask.md_project", {path: ctx.project ? ctx.project.path : "—"}),
    t("ask.md_when", {when: new Date().toLocaleString(ctx.lang === "en" ? "en-GB" : "ru-RU")}),
    t("ask.md_model", {model: ctx.$("#askWho").dataset.model || "—"})];
  // Канонический экземпляр разговора лежит в базе и уходит в git. Файл выгрузки — копия
  // для переноса, и об этом надо сказать, иначе правки понесут в копию.
  if (THREAD) head.push(t("ask.md_thread", {id: THREAD.id}));
  head.push("");
  const body = rows.map(c => ["## " + c.dataset.q,
                              c.dataset.at ? "_" + c.dataset.at + "_" : "",
                              "", c.dataset.a, ""].join("\n"));
  return head.concat(body).join("\n").replace(/\n{3,}/g, "\n\n");
}

function exportMd(ctx){
  if (!cards(ctx).length) return ctx.toast(ctx.t("ask.export_empty"), "warn");
  const blob = new Blob([markdown(ctx)], {type: "text/markdown;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "aurora-otvet-" + (THREAD ? THREAD.id + "-" : "") + ctx.fmt.rtime() + ".md";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  ctx.toast(ctx.t("ask.exported"), "ok");
}

// Модель, которая отвечает, — не деталь настройки: у основной и запасной разные скорость
// и качество, и человек должен видеть, чей ответ читает. Здесь же — выбор модели вручную
// (когда сравнивают ответы), живая проверка основной и возврат на неё.
export async function fillBackends(ctx){
  const sel = ctx.$("#askBackend"), note = ctx.$("#askBackendNote");
  if (!sel) return;
  // Модели у каждого проекта свои: список, собранный для прошлого проекта, врёт о текущем.
  // И собранным он считается только после ответа сервера. Раньше отметка ставилась
  // до ответа, и один сбой запроса оставлял в выборе одно «по кольцу» до перезагрузки.
  const project = ctx.project ? ctx.project.path : "";
  if (sel.dataset.project === project) return;
  const say = msg => { if (note){ note.textContent = msg; note.hidden = !msg; } };
  let a;
  try {
    a = await ctx.api("/api/agent" + (project ? "?project=" + encodeURIComponent(project) : ""),
                      {quiet: true});
  } catch (e) { a = {error: String(e && e.message || e)}; }
  if (!a || a.error)
    return say(ctx.t("ask.models_failed", {why: (a && a.error) || ctx.t("ask.no_answer")}));
  const was = sel.value;
  const choices = a.choices || [], roles = a.roles || [];
  drawChoices(ctx, sel, roles, choices);
  if ([...sel.options].some(o => o.value === was)) sel.value = was;
  if (choices.length) sel.dataset.project = project; else delete sel.dataset.project;
  say(choices.length ? "" : ctx.t("ask.models_empty"));
  sel.onchange = () => { if (sel.value === "more") moreModels(ctx, sel, roles, choices); };
}

// Роли и модели провайдеров в одном списке. Первая строка — роль по умолчанию (worker).
function drawChoices(ctx, sel, roles, choices){
  const {t, el} = ctx;
  // Сначала группы, потом одиночные строки: строка внутри группы — тоже `options`.
  [...sel.querySelectorAll("optgroup")].forEach(g => g.remove());
  while (sel.options.length > 1) sel.remove(1);
  const roleName = r => r.name || t("ask.role_name." + r.id);
  const other = roles.filter(r => r.id !== "worker");
  if (other.length) sel.append(el("optgroup", {label: t("ask.group_roles")},
    ...other.map(r => el("option", {value: "role:" + r.id},
      roleName(r) + (r.models.length ? " · " + r.models.join(", ") : "")))));
  choices.forEach(c => sel.append(el("optgroup", {label: t("ask.group_provider", {name: c.name || c.provider})},
    ...c.models.map(m => el("option", {value: `model:${c.provider}/${m}`}, m)))));
  if (choices.length) sel.append(el("option", {value: "more"}, t("ask.more_models")));
}

// Полный список моделей у самих провайдеров — по кнопке: это сетевые запросы.
async function moreModels(ctx, sel, roles, choices){
  sel.value = "0";
  const full = [];
  for (const c of choices){
    let d = null;
    try { d = await ctx.api("/api/agent/models?n=" + c.n, {quiet: true}); } catch (e) { d = null; }
    const names = ((d && d.models) || []).map(m => typeof m === "string" ? m : (m.id || m.name))
      .filter(Boolean);
    full.push({...c, models: [...new Set([...c.models, ...names])]});
  }
  drawChoices(ctx, sel, roles, full);
  ctx.toast(ctx.t("ask.more_loaded", {n: full.reduce((a, c) => a + c.models.length, 0)}), "ok");
}

async function pingPrimary(ctx, btn){
  const {t} = ctx;
  const was = btn.textContent;
  btn.textContent = t("ask.ping_checking");
  btn.disabled = true;
  const r = await ctx.api("/api/agent/ping", {method: "POST", body: JSON.stringify(
    {project: ctx.project ? ctx.project.path : ""})});
  btn.textContent = was;
  btn.disabled = false;
  const first = (r && (r.backends || r.results) || [])[0];
  if (!first) return ctx.toast((r && r.error) || t("ask.ping_none"), "warn");
  const ok = first.ok !== false;
  drawWho(ctx, {kind: ok ? "ping_ok" : "ping_fail", model: first.model || ""});
  ctx.toast(ok ? t("ask.ping_ok", {model: first.model || ""})
               : t("ask.ping_fail", {why: first.error || first.why || t("ask.no_answer")}),
    ok ? "ok" : "warn");
}

export async function renderHistory(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#askHistory");
  if (!ctx.project) return box.replaceChildren(
    el("div", {class: "muted"}, t("ask.history_pick_project")));
  let d;
  try {
    d = await ctx.api("/api/ask/threads?project=" + encodeURIComponent(ctx.project.path),
                      {quiet: true});
  } catch (e) { d = {error: String(e && e.message || e)}; }
  // Сбой чтения — не «разговоров нет»: пустой список без причины человек принимает за
  // потерю истории и ищет её не там.
  if (!d || d.error) return box.replaceChildren(el("div", {class: "muted"},
    t("ask.history_failed", {why: (d && d.error) || t("ask.no_answer")})));
  const rows = d.threads || [];
  box.replaceChildren();
  if (!rows.length) return box.append(el("div", {class: "muted"}, t("ask.history_empty")));
  rows.forEach(thread => {
    const open = THREAD && THREAD.id === thread.id;
    box.append(el("div", {class: "card row",
        style: "padding:12px 14px;margin-top:8px;gap:12px;align-items:center;cursor:pointer"
               + (open ? ";outline:2px solid var(--primary)" : ""),
        onclick: () => openThread(ctx, thread.id)},
      el("div", {style: "flex:1;min-width:0"},
        el("div", {style: "font-weight:600"}, thread.title),
        el("div", {class: "muted", style: "font-size:12px;margin-top:2px"},
          t("ask.thread_turns", {turns: thread.turns, last: thread.last || "—",
                                 path: thread.path}))),
      el("button", {class: "btn"}, open ? t("ask.opened") : t("ask.open"))));
  });
}

async function openThread(ctx, id){
  const {t, el} = ctx;
  const d = await ctx.api(`/api/ask/thread?project=${encodeURIComponent(ctx.project.path)}`
                          + `&id=${encodeURIComponent(id)}`);
  if (!d || d.error) return ctx.toast(d && d.error || t("ask.thread_failed"), "warn");
  THREAD = {id: d.id};
  const box = ctx.$("#askBody");
  box.replaceChildren();
  // Свежий вопрос сверху — как в самой вкладке: последнее сказанное читают первым.
  d.turns.slice().reverse().forEach(turn => {
    const card = el("div", {class: "card", style: "padding:18px;margin-top:14px"},
      el("div", {style: "font-weight:700"}, turn.q),
      el("div", {class: "muted", style: "font-size:12px;margin-top:2px"}, ctx.fmt.when(turn.at)),
      el("div", {class: "ansbody", style: "margin-top:10px;white-space:pre-wrap",
                 html: ctx.fmt.mdLite(turn.a)}));
    card.dataset.q = turn.q;
    card.dataset.a = turn.a;
    card.dataset.at = ctx.fmt.when(turn.at);
    box.append(card);
  });
  drawThreadHint(ctx);
  drawExport(ctx);
  await renderHistory(ctx);
  ctx.$("#askText").focus();
}

// Скрытому разделу перерисовка не нужна: при открытии refresh перечитает историю. Идущий вопрос
// (`askBase`) уходом не прерывается — ответ ложится на свою карточку в разметке и ждёт возвращения.
export const lifecycle = 1;
export default {mount, refresh};
