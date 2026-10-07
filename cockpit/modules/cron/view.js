/* Cron — раздел-модуль: расписание маршрутов и команд.

   Ведёт расписание сервер панели (cockpit/cron.py): страница только показывает задания,
   правит их и смотрит за идущей цепочкой. Закрыли вкладку — цепочка идёт дальше. */

const STATUS_TONE = {passed: "ok", partial: "ok", failed: "bad", stall: "warn", offline: "warn",
  stopped: "warn", interrupted: "warn", missed: "warn", running: "gold", deferred: "warn",
  skipped: "", pending: ""};
const LIVE_MS = 4000;          // как часто смотреть на идущую цепочку
let DATA = null, EDIT = null, OPEN_RUN = "", timer = null;

export function mount(ctx){
  ctx.root.dataset.module = "cron";
  ctx.$("#cronNew").onclick = () => openEditor(ctx, blankTask(ctx));
  ctx.$("#cronPreset").onclick = () => openEditor(ctx, presetTask(ctx));
}

export async function refresh(ctx){
  const d = await ctx.api("/api/cron", {quiet: true});
  if (!d || d.error) return;
  DATA = d;
  drawNow(ctx);
  drawTasks(ctx);
  drawRuns(ctx);
  drawEditor(ctx);
  live(ctx);
}

// Пока цепочка идёт и раздел открыт — смотрим на неё. Ушли из раздела — перестали.
function live(ctx){
  clearTimeout(timer);
  if (!DATA || !DATA.current) return;
  timer = setTimeout(async () => {
    if (ctx.view !== "cron") return;
    const d = await ctx.api("/api/cron", {quiet: true});
    if (!d || d.error) return;
    const was = DATA.current && DATA.current.id;
    DATA = d;
    drawNow(ctx);
    if (!d.current || d.current.id !== was){ drawTasks(ctx); drawRuns(ctx); }
    live(ctx);
  }, LIVE_MS);
}

/* ---------------------------------------------------------------- подписи */

const dayName = (ctx, n) =>
  // 2024-01-01 — понедельник: день недели берём у языка, а не из своего списка слов.
  new Date(2024, 0, n).toLocaleDateString(ctx.lang === "en" ? "en-GB" : "ru-RU",
                                         {weekday: "short"});

function whenText(ctx, task){
  const {t} = ctx;
  if (task.date) return t("cron.when_once", {date: task.date.split("-").reverse().join("."),
                                             time: task.time});
  if (!task.days || !task.days.length || task.days.length === 7)
    return t("cron.when_daily", {time: task.time});
  return t("cron.when_days", {days: task.days.map(n => dayName(ctx, n)).join(", "),
                              time: task.time});
}

function projectName(ctx, path){
  if (path === "*") return ctx.t("cron.all_projects");
  const p = (DATA.projects || []).find(x => x.path === path);
  return p ? p.name : path.split("/").pop();
}

function routeTitle(id){
  const r = (DATA.routes || []).find(x => x.id === id);
  return r ? r.title : id;
}

function stepText(ctx, st){
  const what = st.kind === "route"
    ? "«" + routeTitle(st.route) + "»" + (st.write === false ? " · " + ctx.t("cron.preview") : "")
    : (st.cmd + " " + (st.args || []).join(" ")).trim();
  return what + " — " + projectName(ctx, st.project);
}

const chip = (ctx, status) => ctx.el("span", {class: "chip " + (STATUS_TONE[status] || "")},
  ctx.t("cron.status." + status));

function note(ctx, code){
  if (!code) return "";
  const s = ctx.t("cron.note." + code);
  return s === "cron.note." + code ? code : s;
}

// Сколько шло — словами ядра (`fmt.howLong`): единицы времени переводит каталог.
const took = (ctx, a, b) => a && b
  ? ctx.fmt.howLong(Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000))) : "";

/* ------------------------------------------------------------ идёт сейчас */

function drawNow(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#cronNow");
  box.innerHTML = "";
  const cur = DATA.current;
  if (!cur){
    if (DATA.queue && DATA.queue.length)
      box.append(el("div", {class: "warnbox"}, t("cron.queued", {n: DATA.queue.length})));
    return;
  }
  const items = cur.items || [];
  const done = items.filter(it => !["pending", "running"].includes(it.status)).length;
  const now = items.find(it => it.status === "running");
  const head = el("div", {class: "row"},
    el("b", {}, t("cron.running", {name: cur.name})),
    el("span", {class: "chip gold"}, t("cron.progress", {done, of: items.length})),
    el("span", {class: "muted"}, t("cron.since", {when: ctx.fmt.when(cur.started)})),
    el("span", {style: "flex:1"}),
    now && now.job ? el("button", {class: "btn sm", onclick: () => {
      ctx.show("console");
      // Подпись — команда шага, а не название маршрута: консоль показывает один шаг.
      const step = (now.jobs || []).slice(-1)[0];
      ctx.poll(now.job, 0, (step && step.cmd) || now.cmd || routeTitle(now.route));
    }}, t("cron.watch")) : null,
    el("button", {class: "btn sm danger", onclick: async () => {
      if (!confirm(t("cron.stop_ask"))) return;
      await ctx.api("/api/cron/stop", {method: "POST", quiet: true, body: "{}"});
      ctx.toast(t("cron.stopping"));
      refresh(ctx);
    }}, t("cron.stop")));
  const card = el("div", {class: "card", style: "padding:16px;margin-bottom:14px"}, head,
    el("div", {style: "margin-top:10px"}, items.map(it => itemRow(ctx, it))));
  if (DATA.queue && DATA.queue.length)
    card.append(el("div", {class: "muted", style: "margin-top:8px"},
      t("cron.queued", {n: DATA.queue.length})));
  box.append(card);
}

function itemRow(ctx, it){
  const {el} = ctx;
  const what = it.kind === "route" ? "«" + (it.title || routeTitle(it.route)) + "»"
    : (it.cmd + " " + (it.args || []).join(" ")).trim();
  return el("div", {class: "list-item", style: "padding:6px 0"},
    chip(ctx, it.status),
    it.retry ? el("span", {class: "chip", title: it.first ? ctx.t("cron.first_try", {status:
      ctx.t("cron.status." + it.first.status)}) : ""}, ctx.t("cron.retry")) : null,
    el("span", {style: "font-weight:600"}, it.project_name),
    el("span", {class: it.kind === "route" ? "" : "mono"}, what),
    it.failed ? el("span", {class: "mono muted"}, it.failed) : null,
    el("span", {class: "muted", style: "flex:1"}, note(ctx, it.note)),
    el("span", {class: "muted"}, took(ctx, it.started, it.finished)));
}

/* ---------------------------------------------------------------- задания */

function drawTasks(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#cronTasks");
  box.innerHTML = "";
  if (!DATA.tasks.length){
    box.append(el("div", {class: "card", style: "padding:20px"}, t("cron.empty")));
    return;
  }
  box.append(el("div", {class: "card"}, DATA.tasks.map(task => taskRow(ctx, task))));
}

function taskRow(ctx, task){
  const {t, el} = ctx;
  const post = (path, body) => ctx.api(path, {method: "POST", quiet: true,
    body: JSON.stringify({id: task.id, ...(body || {})})});
  const last = task.last;
  if (task.source === "bot") return botRow(ctx, task, post);
  return el("div", {class: "list-item", style: "align-items:flex-start"},
    el("div", {style: "flex:1;min-width:0"},
      el("div", {class: "row", style: "gap:8px"},
        el("b", {}, task.name),
        el("span", {class: "chip " + (task.enabled ? "ok" : "")},
          task.enabled ? t("cron.on") : t("cron.off")),
        el("span", {class: "chip"}, whenText(ctx, task)),
        task.next ? el("span", {class: "muted"}, t("cron.next", {when: ctx.fmt.when(task.next)}))
          : null,
        last ? el("span", {class: "chip " + (STATUS_TONE[last.status] || ""),
          title: ctx.fmt.when(last.started)},
          t("cron.last", {status: t("cron.status." + last.status)})) : null),
      el("ol", {class: "muted", style: "margin:8px 0 0;padding-left:20px;font-size:13px"},
        task.steps.map(st => el("li", {}, stepText(ctx, st)))),
      el("div", {class: "muted", style: "font-size:12px;margin-top:4px"},
        t(task.order === "steps" ? "cron.order_steps" : "cron.order_projects") + " · "
        + t(task.on_fail === "stop" ? "cron.fail_stop" : "cron.fail_next"))),
    el("div", {class: "row", style: "gap:6px"},
      el("button", {class: "btn sm primary", onclick: async () => {
        const r = await post("/api/cron/start");
        if (r && r.ok) ctx.toast(t("cron.started", {name: task.name}), "ok");
        else ctx.toast(t("cron.err." + ((r && r.error) || "bad_task")), "warn");
        refresh(ctx);
      }}, t("cron.run_now")),
      el("button", {class: "btn sm", onclick: () => openEditor(ctx, JSON.parse(JSON.stringify(task)))},
        t("cron.edit")),
      el("button", {class: "btn sm", onclick: async () => {
        await post("/api/cron/toggle", {enabled: !task.enabled});
        refresh(ctx);
      }}, task.enabled ? t("cron.disable") : t("cron.enable")),
      el("button", {class: "btn sm danger", onclick: async () => {
        if (!confirm(t("cron.delete_ask", {name: task.name}))) return;
        await post("/api/cron/delete");
        refresh(ctx);
      }}, t("cron.delete"))));
}

// Бот проекта: расписание живёт в его файле (`bots/*.md`), правят его в разделе «Боты».
// Здесь — запуск, включение и переход к боту; удалять бота отсюда незачем.
function botRow(ctx, task, post){
  const {t, el} = ctx;
  const last = task.last;
  return el("div", {class: "list-item", style: "align-items:flex-start"},
    el("div", {style: "flex:1;min-width:0"},
      el("div", {class: "row", style: "gap:8px;flex-wrap:wrap"},
        el("span", {class: "chip gold"}, t("cron.bot")),
        el("b", {}, task.name),
        el("span", {class: "chip " + (task.enabled ? "ok" : "")},
          task.enabled ? t("cron.on") : t("cron.off")),
        el("span", {class: "chip mono", title: t("cron.bot_cron_hint")}, task.cron),
        task.cron_error ? el("span", {class: "chip warn"}, t("cron.bot_cron_bad")) : null,
        task.next ? el("span", {class: "muted"}, t("cron.next", {when: ctx.fmt.when(task.next)}))
          : null,
        last ? el("span", {class: "chip " + (STATUS_TONE[last.status] || ""),
          title: ctx.fmt.when(last.started)},
          t("cron.last", {status: t("cron.status." + last.status)})) : null),
      el("div", {class: "muted", style: "font-size:13px;margin-top:6px"},
        t("cron.bot_where", {file: task.bot, project: task.project_name || task.project}))),
    el("div", {class: "row", style: "gap:6px"},
      el("button", {class: "btn sm primary", onclick: async () => {
        const r = await post("/api/cron/start");
        if (r && r.ok) ctx.toast(t("cron.started", {name: task.name}), "ok");
        else ctx.toast(t("cron.err." + ((r && r.error) || "bad_task")), "warn");
        refresh(ctx);
      }}, t("cron.run_now")),
      el("button", {class: "btn sm", onclick: () => ctx.openProject(task.project, "bots", {file: task.bot})},
        t("cron.bot_edit")),
      task.cron_error ? null : el("button", {class: "btn sm", onclick: async () => {
        await post("/api/cron/toggle", {enabled: !task.enabled});
        refresh(ctx);
      }}, task.enabled ? t("cron.disable") : t("cron.enable"))));
}

/* ---------------------------------------------------------------- редактор */

function blankTask(ctx){
  return {id: "", name: "", enabled: true, time: "20:00", days: [], date: "",
          order: "projects", on_fail: "next", lang: ctx.lang || "ru",
          steps: [{kind: "route", project: "*", route: "update", write: true}]};
}

// «Пройти все маршруты базы во всех проектах вечером»: обслуживание — это маршруты
// группы «база», кроме пересборки с нуля: её запускают осознанно, а не по расписанию.
function presetTask(ctx){
  const base = blankTask(ctx);
  base.name = ctx.t("cron.preset_name");
  const routes = ((DATA && DATA.routes) || [])
    .filter(r => r.group === "база" && r.id !== "rebuild");                 // данные движка
  base.steps = routes.map(r => ({kind: "route", project: "*", route: r.id, write: true}));
  return base;
}

function openEditor(ctx, task){
  EDIT = task;
  drawEditor(ctx);
  ctx.$("#cronEditor").scrollIntoView({behavior: "smooth", block: "start"});
}

function drawEditor(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#cronEditor");
  box.innerHTML = "";
  if (!EDIT || !DATA) return;
  const task = EDIT;
  const input = (attrs) => el("input", {class: "btn", ...attrs});
  const name = input({value: task.name, placeholder: t("cron.name_ph"),
                      style: "flex:1;min-width:220px"});
  name.oninput = () => task.name = name.value;
  const time = input({type: "time", value: task.time});
  time.oninput = () => task.time = time.value;
  const once = el("input", {type: "checkbox", checked: task.date ? "" : null});
  const date = input({type: "date", value: task.date || "", hidden: task.date ? null : ""});
  once.onchange = () => {
    date.hidden = !once.checked;
    if (once.checked && !date.value) date.value = new Date().toISOString().slice(0, 10);
    task.date = once.checked ? date.value : "";
    days.hidden = once.checked;
  };
  date.oninput = () => task.date = date.value;
  const days = el("div", {class: "row", style: "gap:6px", hidden: task.date ? "" : null},
    [1, 2, 3, 4, 5, 6, 7].map(n => {
      const b = el("input", {type: "checkbox", checked: task.days.includes(n) ? "" : null});
      b.onchange = () => {
        task.days = b.checked ? [...new Set([...task.days, n])].sort()
                              : task.days.filter(x => x !== n);
      };
      return el("label", {class: "chip", style: "cursor:pointer"}, b, " " + dayName(ctx, n));
    }),
    el("span", {class: "muted", style: "font-size:12px"}, t("cron.days_hint")));
  const select = (value, opts, on) => {
    const s = el("select", {class: "btn"},
      opts.map(([v, label]) => el("option", {value: v, selected: v === value ? "" : null}, label)));
    s.onchange = () => on(s.value);
    return s;
  };
  const order = select(task.order, [["projects", t("cron.order_projects")],
                                    ["steps", t("cron.order_steps")]], v => task.order = v);
  const onFail = select(task.on_fail, [["next", t("cron.fail_next")],
                                       ["stop", t("cron.fail_stop")]], v => task.on_fail = v);
  const steps = el("div", {});
  const drawSteps = () => {
    steps.innerHTML = "";
    task.steps.forEach((st, i) => steps.append(stepEditor(ctx, task, st, i, drawSteps, select)));
  };
  drawSteps();
  const save = async () => {
    task.lang = ctx.lang || task.lang;
    const r = await ctx.api("/api/cron/save", {method: "POST", quiet: true,
      body: JSON.stringify({task})});
    if (!r || !r.ok) return ctx.toast(t("cron.err." + ((r && r.error) || "bad_task")), "warn");
    ctx.toast(t("cron.saved"), "ok");
    EDIT = null;
    refresh(ctx);
  };
  box.append(el("div", {class: "card", style: "padding:18px;margin-bottom:16px"},
    el("h2", {style: "margin-top:0"}, task.id ? t("cron.edit_title") : t("cron.new_title")),
    el("div", {class: "row"}, name),
    el("div", {class: "row", style: "margin-top:10px"},
      el("span", {}, t("cron.at")), time,
      el("label", {class: "row", style: "gap:6px;cursor:pointer"}, once, t("cron.once")), date),
    el("div", {style: "margin-top:10px"}, days),
    el("h3", {style: "margin:16px 0 6px"}, t("cron.chain")),
    steps,
    el("div", {class: "row", style: "margin-top:8px"},
      el("button", {class: "btn sm", onclick: () => {
        task.steps.push({kind: "route", project: "*", route: "fix", write: true});
        drawSteps();
      }}, t("cron.add_step"))),
    el("div", {class: "row", style: "margin-top:14px"},
      el("span", {}, t("cron.order")), order, el("span", {}, t("cron.on_fail")), onFail),
    el("div", {class: "row", style: "margin-top:16px"},
      el("button", {class: "btn primary", onclick: save}, t("cron.save")),
      el("button", {class: "btn", onclick: () => { EDIT = null; drawEditor(ctx); }},
        t("cron.cancel")))));
}

function stepEditor(ctx, task, st, i, redraw, select){
  const {t, el} = ctx;
  const kind = select(st.kind, [["route", t("cron.kind_route")], ["command", t("cron.kind_command")],
                                ["bot", t("cron.kind_bot")]],
    v => {
      task.steps[i] = v === "route" ? {kind: "route", project: st.project, route: "update", write: true}
                    : v === "bot" ? {kind: "bot", project: st.project, bot: "*"}
                    : {kind: "command", project: st.project, cmd: "kb:lint", args: []};
      redraw();
    });
  // Бот конкретного проекта выбирается из его ботов; «все проекты» — только «все боты».
  const project = select(st.project, [["*", t("cron.all_projects")],
    ...(DATA.projects || []).map(p => [p.path, p.name])],
    v => { st.project = v; if (st.kind === "bot") { st.bot = "*"; redraw(); } });
  let what;
  if (st.kind === "bot"){
    const bots = st.project === "*" ? [] : ((DATA.bots || {})[st.project] || []);
    what = [select(st.bot || "*", [["*", t("cron.all_bots")],
      ...bots.map(b => [b.file, b.name + (b.enabled ? "" : " · " + t("cron.bot_off"))])],
      v => st.bot = v)];
  } else if (st.kind === "route"){
    const route = select(st.route, (DATA.routes || []).map(r => [r.id, r.title]), v => st.route = v);
    const write = el("input", {type: "checkbox", checked: st.write !== false ? "" : null});
    write.onchange = () => st.write = write.checked;
    what = [route, el("label", {class: "row", style: "gap:6px;cursor:pointer"}, write,
                      t("cron.write"))];
  } else {
    const cmds = (ctx.state.commands || []).filter(r => r.runnable && !ctx.isEngineCmd(r));
    const cmd = select(st.cmd, cmds.map(r => [r.cmd, r.cmd]), v => { st.cmd = v; redraw(); });
    const row = cmds.find(r => r.cmd === st.cmd);
    const args = el("input", {class: "btn mono", value: (st.args || []).join(" "),
      placeholder: row && row.flags.length ? row.flags.slice(0, 4).join(" ") : t("cron.args_ph"),
      style: "flex:1;min-width:200px"});
    args.oninput = () => st.args = args.value.split(/\s+/).filter(Boolean);
    what = [cmd, args];
  }
  const move = (d) => {
    const j = i + d;
    if (j < 0 || j >= task.steps.length) return;
    [task.steps[i], task.steps[j]] = [task.steps[j], task.steps[i]];
    redraw();
  };
  return el("div", {class: "list-item", style: "gap:8px;flex-wrap:wrap"},
    el("span", {class: "chip"}, String(i + 1)), kind, ...what,
    el("span", {class: "muted"}, t("cron.in")), project,
    el("span", {style: "flex:1"}),
    el("button", {class: "btn sm", title: t("cron.up"), onclick: () => move(-1)}, "↑"),
    el("button", {class: "btn sm", title: t("cron.down"), onclick: () => move(1)}, "↓"),
    el("button", {class: "btn sm danger", title: t("cron.remove"), onclick: () => {
      task.steps.splice(i, 1);
      redraw();
    }}, "✕"));
}

/* ---------------------------------------------------------------- история */

function drawRuns(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#cronRuns");
  box.innerHTML = "";
  const runs = (DATA.runs || []).filter(r => !DATA.current || r.id !== DATA.current.id);
  if (!runs.length){
    box.append(el("div", {class: "muted"}, t("cron.no_runs")));
    return;
  }
  box.append(el("div", {class: "card"}, runs.map(run => runRow(ctx, run))));
}

function runRow(ctx, run){
  const {t, el} = ctx;
  const items = run.items || [];
  const ok = items.filter(it => it.status === "passed").length;
  const detail = el("div", {hidden: OPEN_RUN === run.id ? null : ""});
  const row = el("div", {class: "list-item", style: "cursor:pointer"},
    el("span", {class: "muted", style: "min-width:96px"}, ctx.fmt.when(run.started)),
    el("b", {}, run.name),
    chip(ctx, run.status),
    el("span", {class: "chip"}, t("cron.trigger." + (run.trigger || "manual"))),
    items.length ? el("span", {class: "muted"}, t("cron.items_ok", {ok, of: items.length})) : null,
    el("span", {style: "flex:1"}),
    el("span", {class: "muted"}, took(ctx, run.started, run.finished)));
  row.onclick = async () => {
    OPEN_RUN = OPEN_RUN === run.id ? "" : run.id;
    detail.hidden = OPEN_RUN !== run.id;
    if (!detail.hidden && !detail.childNodes.length) await drawDetail(ctx, run.id, detail);
  };
  if (OPEN_RUN === run.id) drawDetail(ctx, run.id, detail);
  return el("div", {}, row, detail);
}

async function drawDetail(ctx, id, box){
  const {t, el} = ctx;
  const run = await ctx.api("/api/cron/run?id=" + encodeURIComponent(id), {quiet: true});
  box.innerHTML = "";
  if (!run || run.error) return box.append(el("div", {class: "muted"}, t("cron.no_detail")));
  if (run.status === "missed")
    box.append(el("div", {class: "muted", style: "padding:6px 12px"},
      t(run.note === "already_running" ? "cron.missed_running" : "cron.missed_line",
        {when: ctx.fmt.when(run.slot)})));
  if (run.error) box.append(el("div", {class: "err"}, run.error));
  (run.items || []).forEach(it => {
    box.append(el("div", {style: "padding:0 12px"}, itemRow(ctx, it)));
    if (it.log && it.log.length)
      box.append(el("details", {style: "padding:0 12px 8px 24px"},
        el("summary", {class: "muted", style: "cursor:pointer"}, t("cron.log", {n: it.log.length})),
        el("pre", {class: "mono", style: "white-space:pre-wrap;font-size:12px;max-height:320px;overflow:auto"},
          it.log.join("\n"))));
  });
}

export default {mount, refresh};
