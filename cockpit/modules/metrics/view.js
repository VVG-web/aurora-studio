/* Замеры — kit-level раздел: сеть/VPN вокруг прогонов, сервисные метрики и токены.

   Не путать с «Отчётами» проекта: те собирают дашборды из Jira и Confluence для
   руководителя. Здесь — эксплуатационные числа самого движка. «Прогоны» и «Сервисы»
   строятся только из `timing.d`; строки сети не складываются с ними. */

const PERIODS = [1, 7, 30, 0];
const SERVICES_ORDER = ["llm", "web", "confluence", "jira", "network", "embeddings"];
const KINDS = ["run", "agent", "job", "route", "command", "step"];
let days = 7;
let projectFilter = "";
let serviceFilter = "";
let lastData = null;

export const lifecycle = 1;

export function mount(ctx){
  ctx.root.dataset.module = "metrics";
}

function num(n){
  return String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function pct(part, all){
  return all ? Math.round(1000 * part / all) / 10 : 0;
}

function warn(ctx, text){
  return ctx.el("div", {class:"warnbox", style:"padding:10px 12px;margin-bottom:10px;font-size:12.5px"},
    text);
}

function spinner(ctx){
  return ctx.el("div", {class:"card", style:"padding:24px"},
    ctx.el("span", {class:"spin"}));
}

function numOrNull(value){
  const n = Number(value);
  return value == null || value === "" || !isFinite(n) ? null : n;
}

export function statCells(stat){
  const n = Number(stat && stat.n) || 0;
  if (!n){
    return {n:0, avg:null, p50:null, p95:null, max:null, approx:false,
            low:false, notes:[], note:""};
  }
  const low = n < 20;
  const approx = !!(stat && stat.approx);
  const p50 = low ? null : numOrNull(stat && stat.p50);
  const p95 = low ? null : numOrNull(stat && stat.p95);
  const notes = [];
  if (low) notes.push("low");
  if (approx) notes.push("approx");
  if (!low && n >= 20 && (p50 == null || p95 == null)) notes.push("missing");
  return {n, avg:numOrNull(stat && stat.avg), p50, p95, max:numOrNull(stat && stat.max),
          approx, low, notes, note:notes[0] || ""};
}

export function runNote(r){
  if (!r || r.confidence === "unknown" || r.work_ms == null) return "no_data";
  if (r.source === "legacy") return "estimate";
  if ((Number(r.service_wait_ms) || 0) > 0) return "wait";
  return "work";
}

export function serviceKeys(services){
  const all = services || {};
  const known = SERVICES_ORDER.filter(name => all[name]);
  return known.concat(Object.keys(all).filter(name => !SERVICES_ORDER.includes(name)));
}

export function filterRuns(runs, project){
  return (runs || []).filter(r => !project || r.project === project);
}

export function filterServices(services, name){
  const out = {};
  serviceKeys(services).forEach(key => {
    if (!name || key === name) out[key] = (services || {})[key] || {};
  });
  return out;
}

export function errorCount(service){
  return Object.values((service && service.errors) || {})
    .reduce((sum, value) => sum + (Number(value) || 0), 0);
}

function serviceLabel(ctx, name){
  return SERVICES_ORDER.includes(name) ? ctx.t("metrics.service." + name)
    : (name || ctx.t("metrics.service.other"));
}

function kindLabel(ctx, kind){
  return KINDS.includes(kind) ? ctx.t("metrics.kind." + kind)
    : (kind || ctx.t("metrics.kind.other"));
}

function basisLabel(ctx, basis){
  const known = ["response", "request", "mixed"];
  return known.includes(basis) ? ctx.t("metrics.basis." + basis) : (basis || "");
}

function noteText(ctx, token){
  return ctx.t("metrics.note." + (token || "no_data"));
}

function durCell(ctx, ms){
  const value = numOrNull(ms);
  if (value == null) return ctx.t("metrics.no_data");
  return value < 1000 ? ctx.t("metrics.ms", {n: Math.round(value)})
    : ctx.fmt.howLong(value / 1000);
}

function statCell(ctx, ms, approx){
  const value = numOrNull(ms);
  if (value == null) return "";
  return (approx ? "~" : "") + durCell(ctx, value);
}

function toolbar(ctx){
  const {t, el} = ctx;
  const row = el("div", {class:"row", style:"gap:6px;margin:0 0 10px;flex-wrap:wrap"});
  PERIODS.forEach(d => {
    const b = el("button", {class:"btn sm" + (d === days ? " primary" : ""),
      onclick: async () => { days = d; lastData = null; await refresh(ctx); }},
      d === 0 ? t("metrics.all") : d === 1 ? t("metrics.today") : t("metrics.days", {n: d}));
    row.append(b);
  });
  row.append(el("button", {class:"btn sm", title: t("metrics.refresh_hint"),
    onclick: () => refresh(ctx)}, t("metrics.refresh")));
  return row;
}

function unique(values){
  return [...new Set(values.filter(Boolean))];
}

function projectNames(data){
  const timing = (data && data.timing) || {};
  return unique([
    ...(timing.runs || []).map(r => r.project),
    ...((data && data.net) || []).map(r => r.project),
    ...(((data && data.tokens) || {}).rows || []).map(r => r.project),
  ]).sort((a, b) => a.localeCompare(b));
}

function filterRow(ctx, data){
  const {t, el} = ctx;
  const row = el("div", {class:"row", style:"gap:8px;margin:0 0 14px;flex-wrap:wrap;align-items:center"});
  const projects = projectNames(data);
  if (projectFilter && !projects.includes(projectFilter)) projectFilter = "";
  row.append(el("span", {class:"muted", style:"font-size:12px"}, t("metrics.filter_project")));
  const project = el("select", {class:"btn sm", title: t("metrics.filter_project")},
    el("option", {value:""}, t("metrics.filter_all_projects")),
    ...projects.map(name => el("option", {value:name}, name)));
  project.value = projectFilter;
  project.onchange = () => { projectFilter = project.value; redraw(ctx); };
  row.append(project);

  const services = ((data && data.timing) || {}).services || {};
  const names = serviceKeys(services);
  if (serviceFilter && !names.includes(serviceFilter)) serviceFilter = "";
  row.append(el("span", {class:"muted", style:"font-size:12px;margin-left:4px"},
    t("metrics.filter_service")));
  const service = el("select", {class:"btn sm", title: t("metrics.filter_service")},
    el("option", {value:""}, t("metrics.filter_all_services")),
    ...names.map(name => el("option", {value:name}, serviceLabel(ctx, name))));
  service.value = serviceFilter;
  service.onchange = () => { serviceFilter = service.value; redraw(ctx); };
  row.append(service);
  return row;
}

function netCard(ctx, rows, error){
  const {t, el, fmt} = ctx;
  const box = el("div", {class:"card", style:"padding:18px;margin-bottom:16px"});
  box.append(el("h2", {style:"font-size:16px;margin-bottom:4px"}, t("metrics.net_title")));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"},
    t("metrics.net_about")));
  if (error) box.append(warn(ctx, error));
  rows = (rows || []).filter(r => !projectFilter || r.project === projectFilter);
  if (!rows.length){
    box.append(el("div", {class:"muted", style:"font-size:13px"}, t("metrics.net_empty")));
    return box;
  }
  const head = ["when", "project", "cmd", "duration", "blocked", "reason", "vpn"];
  const wrap = el("div", {style:"overflow-x:auto"});
  const tbl = el("table", {class:"table", style:"width:100%;font-size:12.5px"});
  tbl.append(el("thead", {}, el("tr", {}, head.map(h => el("th", {}, t("metrics.col." + h))))));
  const body = el("tbody");
  rows.forEach(r => {
    const reason = t("metrics.reason." + (r.reason || "unknown"));
    const tr = el("tr", {},
      el("td", {class:"mono"}, r.at ? fmt.when(r.at) : t("metrics.no_data")),
      el("td", {}, r.project || t("metrics.no_data")),
      el("td", {class:"mono", style:"overflow-wrap:anywhere"}, r.cmd || t("metrics.no_data")),
      el("td", {}, fmt.howLong(r.seconds || 0)),
      el("td", {}, r.blocked_s ? fmt.howLong(r.blocked_s) : t("metrics.no_data")),
      el("td", {}, reason),
      el("td", {}, (r.vpn || []).join(", ") || t("metrics.no_data")));
    body.append(tr);
    const details = [];
    const prof = Object.keys(r.profiles || {});
    const prov = Object.keys(r.providers || {});
    if (prof.length) details.push(t("metrics.profiles") + ": " + prof.join(", "));
    if (prov.length) details.push(t("metrics.providers") + ": " + prov.join(", "));
    if (details.length){
      body.append(el("tr", {}, el("td", {colspan: head.length, class:"muted",
        style:"font-size:11.5px;padding-top:0"}, details.join(" · "))));
    }
  });
  tbl.append(body);
  wrap.append(tbl);
  box.append(wrap);
  return box;
}

function runsCard(ctx, data){
  const {t, el, fmt} = ctx;
  const box = el("div", {class:"card", style:"padding:18px;margin-bottom:16px"});
  box.append(el("h2", {style:"font-size:16px;margin-bottom:4px"}, t("metrics.runs_title")));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"},
    t("metrics.runs_about")));
  const timing = (data && data.timing) || {};
  if (data && data.timing_error) box.append(warn(ctx, data.timing_error));
  const runs = filterRuns(timing.runs || [], projectFilter);
  if (!runs.length){
    const empty = (timing.runs || []).length ? "metrics.runs_empty" : "metrics.timing_empty";
    box.append(el("div", {class:"muted", style:"font-size:13px"}, t(empty)));
    return box;
  }
  const cols = ["when", "project", "run", "kind", "work_wait", "queue", "note"];
  const wrap = el("div", {style:"overflow-x:auto"});
  const tbl = el("table", {class:"table", style:"width:100%;font-size:12.5px"});
  tbl.append(el("thead", {}, el("tr", {}, cols.map(h => el("th", {}, t("metrics.col." + h))))));
  const body = el("tbody");
  runs.forEach(r => {
    const run = el("td", {class:"mono", style:"overflow-wrap:anywhere"},
      r.run || t("metrics.no_data"));
    if (r.record_id) run.append(el("span", {class:"muted"}, " · " + r.record_id));
    const work = el("td");
    const timingRow = {id: r.run, timing: r, seconds: r.active_ms == null ? null : r.active_ms / 1000};
    if (r.work_ms == null && r.active_ms != null){
      timingRow.timing = {v: 1, work_ms: null, service_wait_ms: null,
        quality: {wait_known: false, approx: false, legacy: r.source === "legacy"}};
    }
    if (ctx.ui && ctx.ui.timingChip) work.append(ctx.ui.timingChip(timingRow));
    else work.append(el("span", {}, fmt.workWait(r, timingRow.seconds)));
    const token = runNote(r);
    const tip = fmt.timingTip ? fmt.timingTip(timingRow.timing, timingRow.seconds) : "";
    body.append(el("tr", {},
      el("td", {class:"mono"}, r.at ? fmt.when(r.at) : t("metrics.no_data")),
      el("td", {}, r.project || t("metrics.no_data")),
      run,
      el("td", {}, kindLabel(ctx, r.kind)),
      work,
      el("td", {class:"mono"}, durCell(ctx, r.queue_ms)),
      el("td", {title: tip, "aria-label": noteText(ctx, token)}, noteText(ctx, token))));
  });
  tbl.append(body);
  wrap.append(tbl);
  box.append(wrap);
  return box;
}

function detailText(ctx, service){
  const {t} = ctx;
  const parts = [];
  if (service.attempts) parts.push(t("metrics.detail.attempts") + ": " + num(service.attempts));
  if (service.retries) parts.push(t("metrics.detail.retries") + ": " + num(service.retries));
  const slot = statCells(service.slot_wait_ms);
  if (slot.n) parts.push(t("metrics.detail.slot") + ": " + statCell(ctx, slot.avg, slot.approx));
  const ttft = statCells(service.ttft_ms);
  if (ttft.n) parts.push(t("metrics.detail.ttft") + ": " + statCell(ctx, ttft.avg, ttft.approx));
  if (service.tps != null){
    const basis = service.tps_basis ? " (" + t("metrics.tps_basis." + service.tps_basis) + ")" : "";
    parts.push(t("metrics.detail.tps") + ": " + Math.round(Number(service.tps) * 10) / 10 + basis);
  }
  if (service.http_4xx || service.http_5xx){
    parts.push(t("metrics.detail.http", {four: num(service.http_4xx), five: num(service.http_5xx)}));
  }
  if (service.timeouts) parts.push(t("metrics.detail.timeouts") + ": " + num(service.timeouts));
  if (service.auth_errors) parts.push(t("metrics.detail.auth") + ": " + num(service.auth_errors));
  if (service.content_errors) parts.push(t("metrics.detail.content") + ": " + num(service.content_errors));
  if (service.rate_limited) parts.push(t("metrics.detail.rate") + ": " + num(service.rate_limited));
  const probe = service.probe_ok_rate || {};
  if (probe.n){
    parts.push(t("metrics.detail.probe") + ": " +
      t("metrics.probe_rate", {ok: num(probe.ok), n: num(probe.n)}));
  }
  return parts.join(" · ");
}

function servicesCard(ctx, data){
  const {t, el} = ctx;
  const box = el("div", {class:"card", style:"padding:18px;margin-bottom:16px"});
  box.append(el("h2", {style:"font-size:16px;margin-bottom:4px"}, t("metrics.services_title")));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"},
    t("metrics.services_about")));
  if (data && data.timing_error) box.append(warn(ctx, data.timing_error));
  const timing = (data && data.timing) || {};
  const services = filterServices(timing.services || {}, serviceFilter);
  const names = Object.keys(services);
  if (!names.length){
    const empty = (timing.runs || []).length ? "metrics.services_empty" : "metrics.timing_empty";
    box.append(el("div", {class:"muted", style:"font-size:13px"}, t(empty)));
    return box;
  }
  const cols = ["service", "n", "calls", "errors", "avg", "p50", "p95", "max",
                "bytes", "pages", "issues", "basis"];
  const wrap = el("div", {style:"overflow-x:auto"});
  const tbl = el("table", {class:"table", style:"width:100%;font-size:12.5px"});
  tbl.append(el("thead", {}, el("tr", {}, cols.map(h => el("th", {}, t("metrics.col." + h))))));
  const body = el("tbody");
  names.forEach(name => {
    const service = services[name] || {};
    const lat = statCells(service.latency_ms);
    const notes = lat.notes.map(token => token === "low"
      ? t("metrics.low_n", {n: lat.n}) : t("metrics." + token)).filter(Boolean);
    body.append(el("tr", {},
      el("td", {}, serviceLabel(ctx, name)),
      el("td", {}, num(lat.n)),
      el("td", {}, num(service.calls)),
      el("td", {}, num(errorCount(service))),
      el("td", {}, statCell(ctx, lat.avg, lat.approx)),
      el("td", {}, statCell(ctx, lat.p50, lat.approx)),
      el("td", {}, statCell(ctx, lat.p95, lat.approx)),
      el("td", {}, statCell(ctx, lat.max, lat.approx)),
      el("td", {}, num(service.bytes)),
      el("td", {}, service.pages ? num(service.pages) : ""),
      el("td", {}, service.issues ? num(service.issues) : ""),
      el("td", {}, basisLabel(ctx, service.bytes_basis))));
    const details = detailText(ctx, service);
    const tail = notes.concat(details ? [details] : []).join(" · ");
    if (tail){
      body.append(el("tr", {}, el("td", {colspan: cols.length, class:"muted",
        style:"font-size:11.5px;padding-top:0"}, tail)));
    }
  });
  tbl.append(body);
  wrap.append(tbl);
  box.append(wrap);
  return box;
}

function tokenTotals(rows){
  const totals = {tokens_in:0, tokens_out:0, calls:0};
  (rows || []).forEach(r => {
    totals.tokens_in += Number(r.tokens_in) || 0;
    totals.tokens_out += Number(r.tokens_out) || 0;
    totals.calls += Number(r.calls) || 0;
  });
  return totals;
}

function tokenCard(ctx, data){
  const {t, el} = ctx;
  const box = el("div", {class:"card", style:"padding:18px;margin-bottom:16px"});
  box.append(el("h2", {style:"font-size:16px;margin-bottom:4px"}, t("metrics.tokens_title")));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"},
    t("metrics.tokens_about")));
  if (data && data.error) box.append(warn(ctx, data.error));
  const rows = ((data && data.rows) || []).filter(r => !projectFilter || r.project === projectFilter);
  if (!data || !rows.length){
    const empty = data && data.records ? "metrics.tokens_empty_filter" : "metrics.tokens_empty";
    box.append(el("div", {class:"muted", style:"font-size:13px"}, t(empty)));
    return box;
  }
  const totals = projectFilter ? tokenTotals(rows) : ((data && data.totals) || {});
  const all = (totals.tokens_in || 0) + (totals.tokens_out || 0);
  box.append(el("div", {class:"row", style:"gap:18px;flex-wrap:wrap;margin:6px 0 12px"},
    el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("metrics.tokens_total")),
      el("div", {style:"font-size:20px;font-weight:600"}, num(all))),
    el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("metrics.tokens_in")),
      el("div", {style:"font-size:16px"}, num(totals.tokens_in))),
    el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("metrics.tokens_out")),
      el("div", {style:"font-size:16px"}, num(totals.tokens_out))),
    el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("metrics.calls")),
      el("div", {style:"font-size:16px"}, num(totals.calls))),
    el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("metrics.records")),
      el("div", {style:"font-size:16px"}, num(projectFilter ? rows.length : data.records)))));
  if (data.old) box.append(el("div", {class:"muted", style:"font-size:11.5px;margin-bottom:8px"},
    t("metrics.old", {n: data.old})));
  const head = ["project", "role", "model", "calls", "in", "out", "total", "share"];
  const wrap = el("div", {style:"overflow-x:auto"});
  const tbl = el("table", {class:"table", style:"width:100%;font-size:12.5px"});
  tbl.append(el("thead", {}, el("tr", {}, head.map(h => el("th", {}, t("metrics.col." + h))))));
  const body = el("tbody");
  rows.forEach(r => {
    const total = (r.tokens_in || 0) + (r.tokens_out || 0);
    body.append(el("tr", {},
      el("td", {}, r.project || t("metrics.no_data")),
      el("td", {}, r.role || t("metrics.no_data")),
      el("td", {class:"mono", style:"overflow-wrap:anywhere"}, r.model || t("metrics.no_data")),
      el("td", {}, num(r.calls)),
      el("td", {}, num(r.tokens_in)),
      el("td", {}, num(r.tokens_out)),
      el("td", {}, num(total)),
      el("td", {}, pct(total, all) + "%")));
  });
  tbl.append(body);
  wrap.append(tbl);
  box.append(wrap);
  return box;
}

function draw(ctx, box, data){
  box.replaceChildren(toolbar(ctx), filterRow(ctx, data));
  const netError = (data && (data.net_error || data.error)) || "";
  box.append(netCard(ctx, (data && data.net) || [], netError));
  box.append(runsCard(ctx, data || {}));
  box.append(servicesCard(ctx, data || {}));
  box.append(tokenCard(ctx, (data && data.tokens) || {}));
}

function redraw(ctx){
  const box = ctx.$("#metricsBody");
  if (!box) return;
  if (lastData) draw(ctx, box, lastData);
  else refresh(ctx);
}

export async function refresh(ctx){
  const box = ctx.$("#metricsBody");
  if (!box) return;
  box.replaceChildren(spinner(ctx));
  let data = null;
  try {
    data = await ctx.api("/api/metrics?days=" + days, {quiet:true});
  } catch (e){
    data = {error: String((e && e.message) || e || ctx.t("metrics.failed"))};
  }
  if (!data) data = {error: ctx.t("metrics.failed")};
  lastData = data;
  draw(ctx, box, data);
}

export default {mount, refresh};
