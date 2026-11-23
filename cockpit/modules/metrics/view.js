/* Замеры — дашборд расхода токенов и нижние эксплуатационные логи.

   Верх читает только `/api/usage` (схема 2, `usage.d`). Приблизительные числа
   `timing.d` приходят отдельным блоком и никогда не складываются с точным итогом.
   Нижние таблицы читают старый `/api/metrics` и остаются доступны, даже если
   панель работает со старым сервером без схемы 2. */

const STORE_KEY = "aurora-metrics-v1";
const DAYS = [1, 7, 30, 90, 0];
const COMPARE_DAYS = new Set([1, 7, 30]);
const FILTER_KEYS = ["project", "model", "provider", "role"];
const LOG_KEYS = ["runs", "services", "network", "rows"];

export const lifecycle = 1;

let STATE = freshState();
let PROJECT_ALIASES = new Map();

function freshState(){
  return {
    days: 7,
    compare: true,
    filters: {project: [], model: [], provider: [], role: []},
    sort: {
      projects: {key: "tokens_total", dir: -1},
      models: {key: "tokens_total", dir: -1},
      providers: {key: "tokens_total", dir: -1},
    },
    open: {runs: false, services: false, network: false, rows: false},
    usage: null,
    logs: null,
    usageError: "",
    logsError: "",
    oldServer: false,
    loading: false,
    updatedAt: 0,
    focusRole: "",
    focusFilter: "",
    seq: 0,
    abort: null,
  };
}

// ---------------------------------------------------------------- чистые правила

export function numberOrNull(value){
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function sumKnown(a, b){
  const x = numberOrNull(a), y = numberOrNull(b);
  return x === null || y === null ? null : x + y;
}

export function rowTotal(row){
  return numberOrNull(row && row.tokens_total) ?? sumKnown(row && row.tokens_in, row && row.tokens_out);
}

export function compareAllowed(days){
  return COMPARE_DAYS.has(Number(days));
}

export function resetPrefs(){
  return {
    days: 7,
    compare: true,
    filters: {project: [], model: [], provider: [], role: []},
    open: {runs: false, services: false, network: false, rows: false},
  };
}

export function isOldServer(usage){
  return !usage || usage.schema !== 2;
}

export function keyStatus(value){
  if (value === true) return "present";
  if (value === false) return "absent";
  return "unknown";
}

export function exactCards(usage){
  if (usage && usage.quality && usage.quality.empty === true){
    return ["total", "in", "out", "cache", "runs", "per_run"]
      .map(id => ({id, value: null}));
  }
  const totals = (usage && usage.totals) || {};
  const total = numberOrNull(totals.tokens_total) ?? sumKnown(totals.tokens_in, totals.tokens_out);
  const runs = numberOrNull(totals.runs);
  let perRun = numberOrNull(totals.tokens_per_run);
  if (perRun === null && total !== null && runs) perRun = total / runs;
  return [
    {id: "total", value: total},
    {id: "in", value: numberOrNull(totals.tokens_in)},
    {id: "out", value: numberOrNull(totals.tokens_out)},
    {id: "cache", value: numberOrNull(totals.cached_in)},
    {id: "runs", value: runs},
    {id: "per_run", value: perRun},
  ];
}

export function deltaPercent(current, previous){
  const now = numberOrNull(current), before = numberOrNull(previous);
  if (now === null || before === null || before === 0) return null;
  return (now - before) * 100 / before;
}

export function previousValue(usage, id){
  const totals = usage && usage.previous && usage.previous.totals;
  if (!totals) return null;
  if (id === "total") return numberOrNull(totals.tokens_total)
    ?? sumKnown(totals.tokens_in, totals.tokens_out);
  if (id === "per_run"){
    const direct = numberOrNull(totals.tokens_per_run);
    if (direct !== null) return direct;
    const total = numberOrNull(totals.tokens_total)
      ?? sumKnown(totals.tokens_in, totals.tokens_out);
    const runs = numberOrNull(totals.runs);
    return total === null || !runs ? null : total / runs;
  }
  return numberOrNull(totals[id]);
}

export function seriesPoints(usage){
  const rows = Array.isArray(usage && usage.series) ? usage.series : [];
  return rows.map(row => {
    const label = String((row && (row.at || row.day || row.date)) || "");
    return {
      label,
      total: rowTotal(row),
      runs: numberOrNull(row && row.runs),
      calls: numberOrNull(row && row.calls),
      failed: numberOrNull(row && row.tokens_failed),
    };
  }).filter(point => point.label).sort((a, b) => a.label.localeCompare(b.label));
}

export function approxValues(usage){
  const approx = usage && usage.approx;
  if (!approx || typeof approx !== "object") return null;
  const records = numberOrNull(usage.quality && usage.quality.approx_records);
  if (records === 0) return null;
  const values = {
    runs: numberOrNull(approx.runs),
    calls: numberOrNull(approx.calls),
    tokens_in: numberOrNull(approx.tokens_in),
    tokens_out: numberOrNull(approx.tokens_out),
    tokens_total: numberOrNull(approx.tokens_total)
      ?? sumKnown(approx.tokens_in, approx.tokens_out),
    source: String(approx.source || "timing.d"),
    approximate: approx.approximate !== false,
  };
  if ([values.runs, values.calls, values.tokens_in, values.tokens_out,
    values.tokens_total].every(value => value === null || value === 0)) return null;
  return values;
}

export function parsePrefs(raw){
  let source = raw;
  if (typeof raw === "string"){
    try { source = JSON.parse(raw); } catch (e) { source = {}; }
  }
  if (!source || typeof source !== "object") source = {};
  const days = DAYS.includes(Number(source.days)) ? Number(source.days) : 7;
  const filters = {};
  FILTER_KEYS.forEach(key => {
    const value = source.filters && source.filters[key] !== undefined
      ? source.filters[key] : source[key];
    filters[key] = Array.isArray(value)
      ? value.map(v => String(v)).filter(Boolean)
      : (typeof value === "string" && value ? [value] : []);
    if (key !== "role") filters[key] = filters[key].slice(0, 1);
  });
  const open = {};
  LOG_KEYS.forEach(key => { open[key] = Boolean(source.open && source.open[key]); });
  return {days, compare: source.compare !== false && compareAllowed(days), filters, open};
}

export function serializePrefs(prefs){
  return JSON.stringify({
    days: prefs.days,
    compare: prefs.compare,
    filters: prefs.filters,
    open: prefs.open,
  });
}

export function buildUsageQuery(prefs){
  const q = new URLSearchParams();
  q.set("days", String(prefs.days));
  if (prefs.compare && compareAllowed(prefs.days)) q.set("compare", "1");
  FILTER_KEYS.forEach(key => {
    (prefs.filters[key] || []).forEach(value => {
      if (value) q.append(key, value);
    });
  });
  return "/api/usage?" + q.toString();
}

function sortValue(row, key){
  if (key === "name"){
    const value = String(row.project || row.project_id || row.model
      || row.provider_name || row.provider || row.role || "").toLocaleLowerCase();
    return value || null;
  }
  if (key === "key_present"){
    if (row.key_present === true) return 2;
    if (row.key_present === false) return 1;
    return 0;
  }
  if (key === "last_at"){
    const stamp = Date.parse(row.last_at || "");
    return Number.isFinite(stamp) ? stamp : null;
  }
  if (key === "tokens_total") return rowTotal(row);
  if (key === "tokens_per_call"){
    const calls = numberOrNull(row.calls);
    const total = rowTotal(row);
    return total === null || !calls ? null : total / calls;
  }
  return numberOrNull(row[key]);
}

export function sortRows(rows, key, dir = 1){
  const decorated = (rows || []).map((row, index) => ({row, index}));
  decorated.sort((a, b) => {
    const x = sortValue(a.row, key), y = sortValue(b.row, key);
    if (x === null && y === null) return a.index - b.index;
    if (x === null) return 1;
    if (y === null) return -1;
    const cmp = typeof x === "string" || typeof y === "string"
      ? String(x).localeCompare(String(y))
      : Number(x) - Number(y);
    return (dir < 0 ? -cmp : cmp) || a.index - b.index;
  });
  return decorated.map(item => item.row);
}

// ---------------------------------------------------------------- общие мелочи

function num(value){
  const n = numberOrNull(value);
  return n === null ? "" : String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function numCell(ctx, value){
  const n = numberOrNull(value);
  return n === null ? ctx.t("metrics.no_data") : num(n);
}

function pct(value){
  const n = numberOrNull(value);
  return n === null ? null : Math.round(n * 1000) / 10 + "%";
}

function shareValue(row, total){
  const direct = numberOrNull(row && row.share);
  if (direct !== null) return direct;
  const own = rowTotal(row), all = numberOrNull(total);
  return own === null || !all ? null : own / all;
}

function deltaText(ctx, value){
  const n = numberOrNull(value);
  if (n === null) return ctx.t("metrics.delta_unavailable");
  const abs = Math.abs(n);
  if (abs < 0.05) return ctx.t("metrics.delta_flat");
  return ctx.t(n > 0 ? "metrics.delta_up" : "metrics.delta_down",
    {n: String(Math.round(abs * 10) / 10)});
}

function text(ctx, value){ return value == null ? ctx.t("metrics.no_data") : String(value); }

function button(ctx, attrs, label, hint){
  const a = {type: "button", ...attrs};
  if (!a.title) a.title = hint || String(label || "");
  if (!a["aria-label"]) a["aria-label"] = hint || String(label || "");
  return ctx.el("button", a, label);
}

function warn(ctx, message){
  return ctx.el("div", {class: "warnbox", style: "padding:10px 12px;margin-bottom:10px;font-size:12.5px"},
    message);
}

function empty(ctx, key){
  return ctx.el("div", {class: "muted", style: "font-size:13px;padding:6px 0"}, ctx.t(key));
}

function scrollBox(ctx, content, label){
  // Таблица шире экрана: даём ей собственный фокус, иначе с клавиатуры
  // до правых колонок не добраться, а диктор не знает, что тут прокрутка.
  return ctx.el("div", {
    class: "scroll-x", tabindex: "0", role: "region", "aria-label": label,
  }, content);
}

function spinner(ctx){
  return ctx.el("div", {class: "card", style: "padding:24px"}, ctx.el("span", {class: "spin"}));
}

function shortDate(value){
  const raw = String(value || "");
  const m = raw.match(/^\d{4}-(\d\d)-(\d\d)/);
  return m ? m[2] + "." + m[1] : raw;
}

// ---------------------------------------------------------------- состояние и запросы

function loadPrefs(){
  if (typeof localStorage === "undefined") return;
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return;
    const prefs = parsePrefs(raw);
    STATE.days = prefs.days;
    STATE.compare = prefs.compare;
    STATE.filters = prefs.filters;
    STATE.open = prefs.open;
  } catch (e) { /* испорченное значение не должно ломать раздел */ }
}

function savePrefs(){
  if (typeof localStorage === "undefined") return;
  try { localStorage.setItem(STORE_KEY, serializePrefs(STATE)); } catch (e) { /* без памяти */ }
}

function rememberProjects(usage){
  PROJECT_ALIASES = new Map();
  ((usage && usage.by_project) || []).forEach(row => {
    if (row.project_id && row.project){
      PROJECT_ALIASES.set(String(row.project_id), String(row.project));
    }
  });
}

function projectMatches(name){
  const selected = STATE.filters.project || [];
  return !selected.length || selected.some(value =>
    String(value) === String(name || "")
    || PROJECT_ALIASES.get(String(value)) === String(name || ""));
}

function filterRuns(runs){
  return (runs || []).filter(row => projectMatches(row.project));
}

function serviceKeys(services){
  const order = ["llm", "web", "confluence", "jira", "network", "embeddings"];
  const all = services || {};
  return order.filter(name => all[name])
    .concat(Object.keys(all).filter(name => !order.includes(name)));
}

// ---------------------------------------------------------------- панель управления

function dayLabel(ctx, days){
  if (days === 1) return ctx.t("metrics.today");
  if (days === 0) return ctx.t("metrics.all");
  return ctx.t("metrics.days", {n: days});
}

function filterValue(value, key){
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (key === "project") return String(value.project_id || value.project || value.id || value.name || "");
  if (key === "model") return String(value.model || value.id || value.name || "");
  if (key === "provider") return String(value.provider || value.id || value.name || "");
  if (key === "role") return String(value.role || value.id || value.name || "");
  return "";
}

function optionsFor(usage, key, rows){
  const source = usage && usage.options && usage.options[key];
  const raw = Array.isArray(source) && source.length ? source : (rows || []);
  return [...new Set(raw.map(value => filterValue(value, key)).filter(Boolean))];
}

function buildControls(ctx){
  const host = ctx.$("#metricsControls");
  if (!host) return;
  host.replaceChildren();
  const top = ctx.el("div", {class: "row", style: "gap:6px;margin:0 0 8px;flex-wrap:wrap;align-items:center"});
  DAYS.forEach(days => {
    top.append(button(ctx, {
      class: "btn sm", "data-days": String(days),
      onclick: () => chooseDays(ctx, days),
    }, dayLabel(ctx, days), ctx.t("metrics.period_hint", {period: dayLabel(ctx, days)})));
  });
  top.append(button(ctx, {class: "btn sm", id: "metricsCompare",
    onclick: () => toggleCompare(ctx)}, ctx.t("metrics.compare"), ctx.t("metrics.compare_hint")));
  top.append(button(ctx, {class: "btn sm", id: "metricsRefresh",
    onclick: () => refresh(ctx)}, ctx.t("metrics.refresh"), ctx.t("metrics.refresh_hint")));
  host.append(top);

  const filters = ctx.el("div", {class: "row", style: "gap:8px;margin:0 0 8px;flex-wrap:wrap;align-items:center"});
  ["project", "model", "provider"].forEach(key => {
    const label = ctx.t("metrics.filter." + key);
    const id = "metricsFilter" + key[0].toUpperCase() + key.slice(1);
    const select = ctx.el("select", {class: "btn sm", id, "aria-label": label, title: label},
      ctx.el("option", {value: ""}, ctx.t("metrics.filter.all")));
    select.onchange = () => {
      STATE.filters[key] = select.value ? [select.value] : [];
      savePrefs();
      syncControls(ctx);
      renderActive(ctx);
      refresh(ctx);
    };
    filters.append(ctx.el("label", {class: "row", style: "gap:6px;align-items:center;font-size:12px"},
      ctx.el("span", {class: "muted"}, label), select));
  });
  filters.append(button(ctx, {class: "btn sm", id: "metricsReset",
    onclick: () => resetFilters(ctx)}, ctx.t("metrics.filter.reset"),
    ctx.t("metrics.filter.reset_hint")));
  host.append(filters);

  host.append(ctx.el("div", {class: "row", id: "metricsActive",
    style: "gap:6px;margin:0 0 10px;flex-wrap:wrap;align-items:center"}));
}

function syncControls(ctx){
  const host = ctx.$("#metricsControls");
  if (!host) return;
  host.querySelectorAll("[data-days]").forEach(node => {
    const on = Number(node.dataset.days) === STATE.days;
    node.classList.toggle("primary", on);
    node.setAttribute("aria-pressed", on ? "true" : "false");
  });
  const compare = ctx.$("#metricsCompare");
  if (compare){
    const allowed = compareAllowed(STATE.days);
    const on = allowed && STATE.compare;
    compare.disabled = !allowed;
    compare.classList.toggle("primary", on);
    compare.setAttribute("aria-pressed", on ? "true" : "false");
    compare.title = allowed ? ctx.t("metrics.compare_hint") : ctx.t("metrics.compare_off");
  }
  ["project", "model", "provider"].forEach(key => {
    const id = "metricsFilter" + key[0].toUpperCase() + key.slice(1);
    const select = ctx.$("#" + id);
    if (select) select.value = (STATE.filters[key] || [])[0] || "";
  });
}

function logProjects(logs){
  const timing = (logs && logs.timing) || {};
  const rows = [
    ...(timing.runs || []),
    ...((logs && logs.net) || []),
    ...(((logs && logs.tokens) || {}).rows || []),
  ];
  return [...new Set(rows.map(row => String(row.project || "")).filter(Boolean))];
}

function populateFilters(ctx, usage, logs){
  const rows = {
    project: usage ? usage.by_project : logProjects(logs).map(project => ({project})),
    model: usage ? usage.by_model : [],
    provider: usage ? usage.by_provider : [],
  };
  ["project", "model", "provider"].forEach(key => {
    const id = "metricsFilter" + key[0].toUpperCase() + key.slice(1);
    const select = ctx.$("#" + id);
    if (!select) return;
    const current = (STATE.filters[key] || [])[0] || "";
    if (!usage && key !== "project") return;
    const values = optionsFor(usage, key, rows[key]);
    if (current && !values.includes(current)) values.unshift(current);
    select.replaceChildren(
      ctx.el("option", {value: ""}, ctx.t("metrics.filter.all")),
      ...values.map(value => ctx.el("option", {value},
        key === "project" ? (PROJECT_ALIASES.get(value) || value) : value)));
    select.value = current;
  });
}

function renderActive(ctx){
  const host = ctx.$("#metricsActive");
  if (!host) return;
  host.replaceChildren();
  let any = false;
  FILTER_KEYS.forEach(key => {
    (STATE.filters[key] || []).forEach(value => {
      any = true;
      const label = ctx.t("metrics.filter." + key) + ": " + value;
      const chip = ctx.el("span", {class: "chip", style: "gap:4px"},
        ctx.el("span", {}, label),
        button(ctx, {
          class: "chip-x", "data-filter": key, "data-value": value,
          onclick: () => removeFilter(ctx, key, value),
        }, "×", ctx.t("metrics.filter.remove", {value})));
      host.append(chip);
    });
  });
  host.hidden = !any;
}

function chooseDays(ctx, days){
  STATE.days = DAYS.includes(Number(days)) ? Number(days) : 7;
  if (!compareAllowed(STATE.days)) STATE.compare = false;
  savePrefs();
  syncControls(ctx);
  refresh(ctx);
}

function toggleCompare(ctx){
  if (!compareAllowed(STATE.days)) return;
  STATE.compare = !STATE.compare;
  savePrefs();
  syncControls(ctx);
  refresh(ctx);
}

function toggleRole(ctx, role){
  const selected = STATE.filters.role || [];
  STATE.filters.role = selected.includes(role)
    ? selected.filter(value => value !== role) : selected.concat(role);
  STATE.focusRole = role;
  savePrefs();
  renderActive(ctx);
  refresh(ctx);
}

function removeFilter(ctx, key, value){
  STATE.filters[key] = (STATE.filters[key] || []).filter(item => item !== value);
  STATE.focusFilter = key;
  savePrefs();
  syncControls(ctx);
  renderActive(ctx);
  refresh(ctx);
}

function resetFilters(ctx){
  const fresh = resetPrefs();
  STATE.days = fresh.days;
  STATE.compare = fresh.compare;
  STATE.filters = fresh.filters;
  savePrefs();
  syncControls(ctx);
  renderActive(ctx);
  refresh(ctx);
}

// ---------------------------------------------------------------- верхний дашборд

function deltaFor(ctx, usage, id){
  return deltaText(ctx, deltaPercent(
    exactCards(usage).find(card => card.id === id).value,
    previousValue(usage, id)));
}

function cardsGrid(ctx, usage){
  const cards = exactCards(usage);
  const totals = usage.totals || {};
  const quality = usage.quality || {};
  const empty = quality.empty === true;
  const specs = {
    total: {
      label: ctx.t("metrics.card.total"),
      sub: empty ? "" : ctx.t("metrics.card.total_sub", {calls: numCell(ctx, totals.calls)}),
      hint: ctx.t("metrics.card.total_hint"),
    },
    in: {
      label: ctx.t("metrics.card.in"),
      sub: ctx.t("metrics.card.in_sub"),
      hint: ctx.t("metrics.card.in_hint"),
    },
    out: {
      label: ctx.t("metrics.card.out"),
      sub: numberOrNull(totals.reasoning_out) === null
        ? ctx.t("metrics.card.out_unknown") : ctx.t("metrics.card.out_sub"),
      hint: ctx.t("metrics.card.out_hint"),
    },
    cache: {
      label: ctx.t("metrics.card.cache"),
      sub: quality.cached_known === false || numberOrNull(totals.cached_in) === null
        ? ctx.t("metrics.card.cache_unknown") : ctx.t("metrics.card.cache_sub"),
      hint: ctx.t("metrics.card.cache_hint"),
    },
    runs: {
      label: ctx.t("metrics.card.runs"),
      sub: ctx.t("metrics.card.runs_sub"),
      hint: ctx.t("metrics.card.runs_hint"),
    },
    per_run: {
      label: ctx.t("metrics.card.per_run"),
      sub: ctx.t("metrics.card.per_run_sub"),
      hint: ctx.t("metrics.card.per_run_hint"),
    },
  };
  const grid = ctx.el("div", {class: "grid metrics", "aria-label": ctx.t("metrics.cards_aria")});
  cards.forEach(card => {
    const spec = specs[card.id];
    const value = card.value === null ? ctx.t("metrics.no_data") : num(card.value);
    grid.append(ctx.el("div", {
      class: "card metric", "data-card": card.id, role: "group",
      "aria-label": spec.label + ": " + value,
      title: spec.hint,
    },
      ctx.el("div", {class: "muted", style: "font-size:12px"}, spec.label),
      ctx.el("div", {
        class: "val" + (card.value === null ? " muted" : ""),
        style: card.value === null ? "font-size:16px;font-weight:600;line-height:1.2" : null,
      }, value),
      ctx.el("div", {class: "muted", style: "font-size:12px;line-height:1.35"}, spec.sub),
      ctx.el("div", {class: "muted", style: "font-size:11.5px;margin-top:3px"},
        deltaFor(ctx, usage, card.id))));
  });
  return grid;
}

function qualityBanner(ctx, usage){
  const q = usage.quality || {};
  const notes = [];
  if (q.empty){
    const reason = String(q.empty_reason || "unknown");
    const key = "metrics.empty." + reason;
    const label = ctx.t(key);
    notes.push(label === key ? ctx.t("metrics.empty.unknown") : label);
  }
  if (q.truncated) notes.push(ctx.t("metrics.truncated"));
  if (q.records_bad) notes.push(ctx.t("metrics.bad_records", {n: num(q.records_bad)}));
  if (q.records_old_schema) notes.push(ctx.t("metrics.records_old", {n: num(q.records_old_schema)}));
  if (q.records_no_calls) notes.push(ctx.t("metrics.records_no_calls", {n: num(q.records_no_calls)}));
  if (q.records_no_breakdown) notes.push(ctx.t("metrics.records_no_breakdown",
    {n: num(q.records_no_breakdown)}));
  if (q.error) notes.push(ctx.t("metrics.source_error", {why: q.error}));
  if (!notes.length) return null;
  return ctx.el("div", {class: "warnbox",
    style: "padding:10px 12px;margin-bottom:12px;font-size:12.5px"},
    notes.join(" · "));
}

function chartCard(ctx, usage){
  const points = seriesPoints(usage);
  const card = ctx.el("div", {class: "card", style: "padding:18px;margin-bottom:16px",
    "data-block": "trend"});
  card.append(ctx.el("h2", {style: "font-size:16px;margin:0 0 4px"},
    ctx.t("metrics.chart.title")));
  card.append(ctx.el("div", {class: "muted", style: "font-size:12.5px;margin-bottom:10px"},
    ctx.t("metrics.chart.about")));
  if (!points.length){
    card.append(empty(ctx, "metrics.chart.empty"));
    return card;
  }
  const max = Math.max(0, ...points.map(point => point.total || 0));
  const total = points.reduce((sum, point) => sum + (point.total || 0), 0);
  const chart = ctx.el("div", {
    class: "scroll-x", role: "img", tabindex: "0",
    "aria-label": ctx.t("metrics.chart.aria", {
      from: usage.window && usage.window.from || "",
      to: usage.window && usage.window.to || "",
      points: points.length,
      total: num(total),
    }),
    style: "padding-bottom:4px",
  });
  const inner = ctx.el("div", {
    style: "display:flex;align-items:flex-end;gap:3px;height:150px;min-width:"
      + Math.max(320, points.length * 22) + "px",
  });
  const labels = ctx.el("div", {
    style: "display:flex;gap:3px;min-width:" + Math.max(320, points.length * 22) + "px;margin-top:4px",
  });
  const step = Math.max(1, Math.ceil(points.length / 14));
  points.forEach((point, index) => {
    const height = max ? Math.max(1, Math.round(100 * (point.total || 0) / max)) : 0;
    const title = point.label + " · " + (point.total === null
      ? ctx.t("metrics.no_data") : num(point.total));
    inner.append(ctx.el("div", {
      title, "aria-hidden": "true",
      style: "flex:1 1 0;min-width:6px;height:100%;display:flex;flex-direction:column;"
        + "justify-content:flex-end;align-items:stretch",
    }, ctx.el("div", {
      style: "height:" + height + "%;min-height:" + (point.total ? "2px" : "0")
        + ";background:var(--primary);border-radius:2px 2px 0 0",
    })));
    labels.append(ctx.el("div", {
      style: "flex:1 1 0;min-width:6px;font-size:10.5px;color:var(--text-muted);"
        + "text-align:center;white-space:nowrap",
    }, index % step === 0 || index === points.length - 1 ? shortDate(point.label) : ""));
  });
  chart.append(inner, labels);
  card.append(chart);

  const details = ctx.el("details", {style: "margin-top:10px"});
  details.append(ctx.el("summary", {style: "cursor:pointer;font-size:12.5px"},
    ctx.t("metrics.chart.table")));
  const table = ctx.el("table", {style: "margin-top:8px"});
  table.append(ctx.el("caption", {
    style: "text-align:left;padding:0 0 8px;color:var(--text-muted);font-size:12px",
  }, ctx.t("metrics.chart.caption")));
  table.append(ctx.el("thead", {}, ctx.el("tr", {},
    ["date", "tokens", "runs", "calls", "failed"].map(key =>
      ctx.el("th", {scope: "col"}, ctx.t("metrics.col." + key))))));
  const body = ctx.el("tbody");
  points.forEach(point => {
    body.append(ctx.el("tr", {},
      ctx.el("td", {class: "mono"}, point.label),
      ctx.el("td", {}, numCell(ctx, point.total)),
      ctx.el("td", {}, numCell(ctx, point.runs)),
      ctx.el("td", {}, numCell(ctx, point.calls)),
      ctx.el("td", {}, numCell(ctx, point.failed))));
  });
  table.append(body);
  details.append(scrollBox(ctx, table, ctx.t("metrics.chart.caption")));
  card.append(details);
  return card;
}

function sortHeader(ctx, tableId, column){
  const active = STATE.sort[tableId];
  const on = active.key === column.key;
  const mark = on ? (active.dir < 0 ? " ▼" : " ▲") : "";
  return button(ctx, {
    class: "muted", "data-sort": tableId + ":" + column.key,
    style: "border:0;background:none;padding:0;font:inherit;color:inherit;"
      + "text-transform:inherit;letter-spacing:inherit;cursor:pointer;text-align:left",
    onclick: () => setSort(ctx, tableId, column.key),
  }, column.label + mark, ctx.t("metrics.sort_by", {column: column.label}));
}

function setSort(ctx, tableId, key){
  const current = STATE.sort[tableId];
  STATE.sort[tableId] = {key, dir: current.key === key ? -current.dir : -1};
  renderAll(ctx);
  const node = ctx.root.querySelector('[data-sort="' + tableId + ":" + key + '"]');
  if (node) node.focus();
}

function tableCard(ctx, spec){
  const card = ctx.el("div", {class: "card", style: "padding:18px;margin-bottom:16px",
    "data-table": spec.id});
  card.append(ctx.el("h2", {style: "font-size:16px;margin:0 0 4px"}, spec.title));
  card.append(ctx.el("div", {class: "muted", style: "font-size:12.5px;margin-bottom:10px"},
    spec.about));
  if (spec.error) card.append(warn(ctx, spec.error));
  if (!spec.rows.length){
    card.append(empty(ctx, spec.empty));
    return card;
  }
  if (spec.truncated) card.append(ctx.el("div", {class: "muted",
    style: "font-size:11.5px;margin-bottom:8px"}, ctx.t("metrics.truncated")));
  const sorted = sortRows(spec.rows, STATE.sort[spec.id].key, STATE.sort[spec.id].dir);
  const table = ctx.el("table");
  table.append(ctx.el("caption", {
    style: "text-align:left;padding:0 0 8px;color:var(--text-muted);font-size:12px",
  }, spec.caption));
  const headRow = ctx.el("tr");
  spec.columns.forEach(column => {
    const active = STATE.sort[spec.id].key === column.sort;
    const attrs = {scope: "col"};
    if (column.sort) attrs["aria-sort"] = active
      ? (STATE.sort[spec.id].dir < 0 ? "descending" : "ascending") : "none";
    const th = ctx.el("th", attrs);
    th.append(column.sort ? sortHeader(ctx, spec.id, column) : column.label);
    headRow.append(th);
  });
  table.append(ctx.el("thead", {}, headRow));
  const body = ctx.el("tbody");
  sorted.forEach(row => {
    const tr = ctx.el("tr");
    spec.columns.forEach(column => {
      const td = ctx.el("td", {
        class: column.mono ? "mono" : null,
        style: column.key !== "name" && column.key !== "trend" && column.key !== "projects"
          ? "white-space:nowrap" : null,
      });
      td.append(column.cell(row));
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(body);
  card.append(scrollBox(ctx, table, spec.title));
  return card;
}

function spark(ctx, values, label){
  const points = Array.isArray(values) ? values.map(v => numberOrNull(v) || 0) : [];
  if (!points.length) return ctx.el("span", {class: "muted"}, ctx.t("metrics.no_data"));
  const max = Math.max(1, ...points);
  const host = ctx.el("span", {
    role: "img", "aria-label": label,
    style: "display:inline-flex;align-items:flex-end;gap:1px;height:18px;width:74px;overflow:hidden",
  });
  points.forEach(value => {
    host.append(ctx.el("span", {
      "aria-hidden": "true",
      style: "flex:1 1 0;min-width:0;height:" + Math.max(1, Math.round(18 * value / max))
        + "px;background:var(--primary);opacity:.7;border-radius:1px",
    }));
  });
  return host;
}

function projectsCard(ctx, usage){
  const rows = usage.by_project || [];
  const total = usage.totals && usage.totals.tokens_total;
  return tableCard(ctx, {
    id: "projects", title: ctx.t("metrics.projects_title"),
    about: ctx.t("metrics.projects_about"), rows,
    empty: "metrics.projects.empty", caption: ctx.t("metrics.projects_caption"),
    truncated: !!(usage.quality && usage.quality.truncated),
    columns: [
      {key: "name", label: ctx.t("metrics.col.project"), sort: "name",
        cell: row => text(ctx, row.project || row.project_id)},
      {key: "total", label: ctx.t("metrics.col.total"), sort: "tokens_total",
        mono: true, cell: row => numCell(ctx, rowTotal(row))},
      {key: "share", label: ctx.t("metrics.col.share"), sort: "share",
        cell: row => text(ctx, pct(shareValue(row, total)))},
      {key: "delta", label: ctx.t("metrics.col.delta"), sort: "delta_pct",
        cell: row => deltaText(ctx, row.delta_pct)},
      {key: "runs", label: ctx.t("metrics.col.runs"), sort: "runs",
        cell: row => numCell(ctx, row.runs)},
      {key: "calls", label: ctx.t("metrics.col.calls"), sort: "calls",
        cell: row => numCell(ctx, row.calls)},
      {key: "last", label: ctx.t("metrics.col.last"), sort: "last_at",
        cell: row => row.last_at ? (ctx.fmt.when(row.last_at) || text(ctx, row.last_at))
          : ctx.t("metrics.no_data")},
      {key: "trend", label: ctx.t("metrics.col.trend"), sort: null,
        cell: row => spark(ctx, row.spark, ctx.t("metrics.spark_aria",
          {name: row.project || row.project_id || ctx.t("metrics.no_data")}))},
    ],
  });
}

function modelsCard(ctx, usage){
  const rows = usage.by_model || [];
  const total = usage.totals && usage.totals.tokens_total;
  return tableCard(ctx, {
    id: "models", title: ctx.t("metrics.models_title"),
    about: ctx.t("metrics.models_about"), rows,
    empty: "metrics.models.empty", caption: ctx.t("metrics.models_caption"),
    truncated: !!(usage.quality && usage.quality.truncated),
    columns: [
      {key: "name", label: ctx.t("metrics.col.model"), sort: "name", mono: true,
        cell: row => text(ctx, row.model)},
      {key: "calls", label: ctx.t("metrics.col.calls"), sort: "calls",
        cell: row => numCell(ctx, row.calls)},
      {key: "in", label: ctx.t("metrics.col.in"), sort: "tokens_in",
        cell: row => numCell(ctx, row.tokens_in)},
      {key: "out", label: ctx.t("metrics.col.out"), sort: "tokens_out",
        cell: row => numCell(ctx, row.tokens_out)},
      {key: "total", label: ctx.t("metrics.col.total"), sort: "tokens_total",
        mono: true, cell: row => numCell(ctx, rowTotal(row))},
      {key: "share", label: ctx.t("metrics.col.share"), sort: "share",
        cell: row => text(ctx, pct(shareValue(row, total)))},
      {key: "per_call", label: ctx.t("metrics.col.per_call"), sort: "tokens_per_call",
        cell: row => {
          const direct = numberOrNull(row.tokens_per_call);
          const calls = numberOrNull(row.calls);
          const own = rowTotal(row);
          return numCell(ctx, direct === null
            ? (own === null || !calls ? null : own / calls) : direct);
        }},
      {key: "last", label: ctx.t("metrics.col.last"), sort: "last_at",
        cell: row => row.last_at ? (ctx.fmt.when(row.last_at) || text(ctx, row.last_at))
          : ctx.t("metrics.no_data")},
    ],
  });
}

function providersCard(ctx, usage){
  const rows = usage.by_provider || [];
  const total = usage.totals && usage.totals.tokens_total;
  return tableCard(ctx, {
    id: "providers", title: ctx.t("metrics.providers_title"),
    about: ctx.t("metrics.providers_about"), rows,
    empty: "metrics.providers.empty", caption: ctx.t("metrics.providers_caption"),
    truncated: !!(usage.quality && usage.quality.truncated),
    columns: [
      {key: "name", label: ctx.t("metrics.col.provider"), sort: "name",
        cell: row => {
          const name = row.provider_name || row.provider;
          if (!name) return ctx.t("metrics.no_data");
          return row.provider_name && row.provider
            ? ctx.el("span", {}, row.provider_name,
              ctx.el("span", {class: "muted mono"}, " · " + row.provider))
            : name;
        }},
      {key: "key", label: ctx.t("metrics.col.key"), sort: "key_present",
        cell: row => {
          const status = keyStatus(row.key_present);
          return ctx.el("span", {class: "chip"}, ctx.t("metrics.key." + status));
        }},
      {key: "calls", label: ctx.t("metrics.col.calls"), sort: "calls",
        cell: row => numCell(ctx, row.calls)},
      {key: "total", label: ctx.t("metrics.col.total"), sort: "tokens_total",
        mono: true, cell: row => numCell(ctx, rowTotal(row))},
      {key: "share", label: ctx.t("metrics.col.share"), sort: "share",
        cell: row => text(ctx, pct(shareValue(row, total)))},
      {key: "projects", label: ctx.t("metrics.col.projects"), sort: null,
        cell: row => {
          const names = Array.isArray(row.projects)
            ? row.projects.map(value => typeof value === "string"
              ? value : (value && (value.project || value.name)) || "").filter(Boolean)
            : [];
          return names.length ? names.join(", ") : ctx.t("metrics.no_data");
        }},
      {key: "last", label: ctx.t("metrics.col.last"), sort: "last_at",
        cell: row => row.last_at ? (ctx.fmt.when(row.last_at) || text(ctx, row.last_at))
          : ctx.t("metrics.no_data")},
    ],
  });
}

function rolesCard(ctx, usage){
  const card = ctx.el("div", {class: "card", style: "padding:18px;margin-bottom:16px",
    "data-block": "roles"});
  card.append(ctx.el("h2", {style: "font-size:16px;margin:0 0 4px"},
    ctx.t("metrics.roles_title")));
  card.append(ctx.el("div", {class: "muted", style: "font-size:12.5px;margin-bottom:10px"},
    ctx.t("metrics.roles_about")));
  const host = ctx.el("div", {class: "row", style: "gap:6px;flex-wrap:wrap"});
  const roles = optionsFor(usage, "role", usage.by_role);
  if (!roles.length){
    card.append(empty(ctx, "metrics.roles.empty"));
    return card;
  }
  const byRole = new Map((usage.by_role || []).map(row => [String(row.role || ""), row]));
  const total = usage.totals && usage.totals.tokens_total;
  roles.forEach(role => {
    const row = byRole.get(role) || {};
    const on = (STATE.filters.role || []).includes(role);
    const share = pct(shareValue(row, total));
    host.append(button(ctx, {
      class: "chip" + (on ? " accent" : ""),
      "data-role": role,
      "aria-pressed": on ? "true" : "false",
      onclick: () => toggleRole(ctx, role),
    }, (role || ctx.t("metrics.role_unknown")) + (share ? " · " + share : ""),
    ctx.t("metrics.role_filter", {role: role || ctx.t("metrics.role_unknown")})));
  });
  card.append(host);
  return card;
}

function approxCard(ctx, usage){
  const values = approxValues(usage);
  const card = ctx.el("div", {class: "card", style: "padding:18px;margin-bottom:16px",
    "data-block": "approx", "data-approx": "1"});
  const title = ctx.el("div", {class: "row", style: "gap:8px;flex-wrap:wrap;align-items:center"},
    ctx.el("h2", {style: "font-size:16px;margin:0"}, ctx.t("metrics.approx_title")),
    ctx.el("span", {class: "chip gold"}, ctx.t("metrics.approx")));
  card.append(title);
  card.append(ctx.el("div", {class: "muted", style: "font-size:12.5px;margin:4px 0 10px"},
    ctx.t("metrics.approx_note")));
  if (!values){
    card.append(empty(ctx, "metrics.approx.empty"));
    return card;
  }
  card.append(ctx.el("div", {class: "muted mono", style: "font-size:11.5px;margin-bottom:10px"},
    ctx.t("metrics.approx_meta", {source: values.source,
      approx: String(values.approximate)})));
  card.append(ctx.el("div", {class: "row", style: "gap:18px;flex-wrap:wrap;margin-bottom:10px"},
    elValue(ctx, "metrics.approx.runs", values.runs),
    elValue(ctx, "metrics.approx.calls", values.calls),
    elValue(ctx, "metrics.approx.in", values.tokens_in),
    elValue(ctx, "metrics.approx.out", values.tokens_out),
    elValue(ctx, "metrics.approx.total", values.tokens_total)));
  const rows = Array.isArray(usage.approx_by_project) ? usage.approx_by_project : [];
  if (rows.length){
    const table = ctx.el("table", {style: "margin-top:6px"});
    table.append(ctx.el("caption", {
      style: "text-align:left;padding:0 0 8px;color:var(--text-muted);font-size:12px",
    }, ctx.t("metrics.approx_caption")));
    table.append(ctx.el("thead", {}, ctx.el("tr", {},
      ["project", "runs", "calls", "tokens"].map(key =>
        ctx.el("th", {scope: "col"}, ctx.t("metrics.col." + key))))));
    const body = ctx.el("tbody");
    rows.forEach(row => {
      body.append(ctx.el("tr", {},
        ctx.el("td", {}, text(ctx, row.project || row.project_id)),
        ctx.el("td", {}, numCell(ctx, row.runs)),
        ctx.el("td", {}, numCell(ctx, row.calls)),
        ctx.el("td", {}, numCell(ctx, rowTotal(row)))));
    });
    table.append(body);
    card.append(scrollBox(ctx, table, ctx.t("metrics.approx_caption")));
  }
  return card;
}

function elValue(ctx, labelKey, value){
  const n = numberOrNull(value);
  return ctx.el("div", {},
    ctx.el("div", {class: "muted", style: "font-size:11.5px"}, ctx.t(labelKey)),
    ctx.el("div", {style: "font-size:17px;font-weight:600"},
      n === null ? ctx.t("metrics.no_data") : num(n)));
}

// ---------------------------------------------------------------- нижние логи

function statCells(stat){
  const n = numberOrNull(stat && stat.n) || 0;
  if (!n) return {n: 0, avg: null, p50: null, p95: null, max: null,
    approx: false, low: false, notes: [], note: ""};
  const low = n < 20;
  const approx = !!(stat && stat.approx);
  const p50 = low ? null : numberOrNull(stat.p50);
  const p95 = low ? null : numberOrNull(stat.p95);
  const notes = [];
  if (low) notes.push("low");
  if (approx) notes.push("approx");
  if (!low && n >= 20 && (p50 === null || p95 === null)) notes.push("missing");
  return {n, avg: numberOrNull(stat.avg), p50, p95, max: numberOrNull(stat.max),
    approx, low, notes, note: notes[0] || ""};
}

function runNote(row){
  if (!row || row.confidence === "unknown" || row.work_ms == null) return "no_data";
  if (row.source === "legacy") return "estimate";
  if ((Number(row.service_wait_ms) || 0) > 0) return "wait";
  return "work";
}

function errorCount(service){
  return Object.values((service && service.errors) || {})
    .reduce((sum, value) => sum + (Number(value) || 0), 0);
}

function serviceLabel(ctx, name){
  const known = ["llm", "web", "confluence", "jira", "network", "embeddings"];
  return known.includes(name) ? ctx.t("metrics.service." + name)
    : (name || ctx.t("metrics.service.other"));
}

function kindLabel(ctx, kind){
  const known = ["run", "agent", "job", "route", "command", "step"];
  return known.includes(kind) ? ctx.t("metrics.kind." + kind)
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
  const value = numberOrNull(ms);
  if (value === null) return ctx.t("metrics.no_data");
  return value < 1000 ? ctx.t("metrics.ms", {n: Math.round(value)})
    : ctx.fmt.howLong(value / 1000);
}

function statCell(ctx, ms, approx){
  const value = numberOrNull(ms);
  return value === null ? "" : (approx ? "~" : "") + durCell(ctx, value);
}

function logTable(ctx, columns, rows, cells, label){
  const table = ctx.el("table", {style: "font-size:12.5px"});
  table.append(ctx.el("thead", {}, ctx.el("tr", {}, columns.map(column =>
    ctx.el("th", {scope: "col"}, colLabel(ctx, column))))));
  const body = ctx.el("tbody");
  rows.forEach(row => body.append(ctx.el("tr", {}, cells(row).map(value => {
    const td = ctx.el("td");
    td.append(value == null ? ctx.t("metrics.no_data") : value);
    return td;
  }))));
  table.append(body);
  return scrollBox(ctx, table, label);
}

function colLabel(ctx, key){
  const special = {service: "metrics.col.service"};
  return special[key] ? ctx.t(special[key]) : ctx.t("metrics.col." + key);
}

function runsTable(ctx, timing){
  const rows = filterRuns(timing.runs);
  if (!rows.length){
    return empty(ctx, (timing.runs || []).length
      ? "metrics.runs_empty" : "metrics.timing_empty");
  }
  return logTable(ctx,
    ["when", "project", "run", "kind", "work_wait", "queue", "note"],
    rows,
    row => {
      const work = ctx.el("span");
      const timingRow = {id: row.run, timing: row,
        seconds: row.active_ms == null ? null : row.active_ms / 1000};
      if (row.work_ms == null && row.active_ms != null){
        timingRow.timing = {v: 1, work_ms: null, service_wait_ms: null,
          quality: {wait_known: false, approx: false, legacy: row.source === "legacy"}};
      }
      if (ctx.ui && ctx.ui.timingChip) work.append(ctx.ui.timingChip(timingRow));
      else work.append(ctx.fmt.workWait(row, timingRow.seconds));
      const token = runNote(row);
      const tip = ctx.fmt.timingTip ? ctx.fmt.timingTip(timingRow.timing, timingRow.seconds) : "";
      const note = ctx.el("span", {title: tip, "aria-label": noteText(ctx, token)},
        noteText(ctx, token));
      return [
        row.at ? ctx.fmt.when(row.at) : ctx.t("metrics.no_data"),
        row.project || ctx.t("metrics.no_data"),
        row.run || ctx.t("metrics.no_data"),
        kindLabel(ctx, row.kind),
        work,
        durCell(ctx, row.queue_ms),
        note,
      ];
    }, ctx.t("metrics.runs_title"));
}

function servicesTable(ctx, timing){
  const services = timing.services || {};
  const names = serviceKeys(services);
  if (!names.length){
    return empty(ctx, (timing.runs || []).length
      ? "metrics.services_empty" : "metrics.timing_empty");
  }
  const rows = names.map(name => ({name, service: services[name] || {}}));
  return logTable(ctx,
    ["service", "n", "calls", "errors", "avg", "p50", "p95", "max",
      "bytes", "pages", "issues", "basis"],
    rows,
    row => {
      const service = row.service;
      const lat = statCells(service.latency_ms);
      return [
        serviceLabel(ctx, row.name),
        numCell(ctx, lat.n),
        numCell(ctx, service.calls),
        numCell(ctx, errorCount(service)),
        statCell(ctx, lat.avg, lat.approx),
        statCell(ctx, lat.p50, lat.approx),
        statCell(ctx, lat.p95, lat.approx),
        statCell(ctx, lat.max, lat.approx),
        numCell(ctx, service.bytes),
        numCell(ctx, service.pages),
        numCell(ctx, service.issues),
        basisLabel(ctx, service.bytes_basis),
      ];
    }, ctx.t("metrics.services_title"));
}

function netTable(ctx, net){
  const rows = (net || []).filter(row => projectMatches(row.project));
  if (!rows.length) return empty(ctx, "metrics.net_empty");
  return logTable(ctx,
    ["when", "project", "cmd", "duration", "blocked", "reason", "vpn"],
    rows,
    row => [
      row.at ? ctx.fmt.when(row.at) : ctx.t("metrics.no_data"),
      row.project || ctx.t("metrics.no_data"),
      row.cmd || ctx.t("metrics.no_data"),
      row.seconds == null ? ctx.t("metrics.no_data") : ctx.fmt.howLong(row.seconds),
      row.blocked_s ? ctx.fmt.howLong(row.blocked_s) : ctx.t("metrics.no_data"),
      ctx.t("metrics.reason." + (row.reason || "unknown")),
      (Array.isArray(row.vpn) ? row.vpn.join(", ") : String(row.vpn || ""))
        || ctx.t("metrics.no_data"),
    ], ctx.t("metrics.net_title"));
}

function tokenLinesTable(ctx, tokens){
  const rows = (tokens.rows || []).filter(row => projectMatches(row.project));
  if (!rows.length){
    return empty(ctx, tokens.records ? "metrics.tokens_empty_filter" : "metrics.tokens_empty");
  }
  const all = (numberOrNull(tokens.totals && tokens.totals.tokens_in) || 0)
    + (numberOrNull(tokens.totals && tokens.totals.tokens_out) || 0);
  return logTable(ctx,
    ["project", "role", "model", "calls", "in", "out", "total", "share"],
    rows,
    row => {
      const total = rowTotal(row);
      const share = all && total !== null ? pct(total / all) : null;
      return [
        row.project || ctx.t("metrics.no_data"),
        row.role || ctx.t("metrics.no_data"),
        row.model || ctx.t("metrics.no_data"),
        numCell(ctx, row.calls),
        numCell(ctx, row.tokens_in),
        numCell(ctx, row.tokens_out),
        numCell(ctx, total),
        text(ctx, share),
      ];
    }, ctx.t("metrics.tokens_title"));
}

function logDetails(ctx, key, title, count, content){
  const details = ctx.el("details", {
    class: "card", "data-log": key,
    style: "padding:12px 18px;margin-bottom:10px",
  });
  if (STATE.open[key]) details.open = true;
  details.ontoggle = () => { STATE.open[key] = details.open; savePrefs(); };
  details.append(ctx.el("summary", {style: "cursor:pointer;font-weight:600;font-size:14px"},
    title + (count === null ? "" : " (" + count + ")")));
  details.append(ctx.el("div", {style: "padding-top:8px"}, content));
  return details;
}

function renderLogs(ctx, logs, error){
  const host = ctx.$("#metricsLogs");
  if (!host) return;
  host.replaceChildren();
  if (error) host.append(warn(ctx, error));
  logs = logs || {};
  const timing = logs.timing || {};
  const net = logs.net || [];
  const tokens = logs.tokens || {};
  const timingWarn = () => logs.timing_error ? warn(ctx, logs.timing_error) : null;
  const netWarn = () => logs.net_error ? warn(ctx, logs.net_error) : null;
  const tokenWarn = () => tokens.error ? warn(ctx, tokens.error) : null;
  host.append(logDetails(ctx, "runs", ctx.t("metrics.runs_title"),
    (timing.runs || []).length,
    ctx.el("div", {}, timingWarn(), runsTable(ctx, timing))));
  host.append(logDetails(ctx, "services", ctx.t("metrics.services_title"),
    serviceKeys(timing.services).length,
    ctx.el("div", {}, timingWarn(), servicesTable(ctx, timing))));
  host.append(logDetails(ctx, "network", ctx.t("metrics.net_title"),
    net.length, ctx.el("div", {}, netWarn(), netTable(ctx, net))));
  host.append(logDetails(ctx, "rows", ctx.t("metrics.tokens_title"),
    (tokens.rows || []).length,
    ctx.el("div", {}, tokenWarn(), tokenLinesTable(ctx, tokens))));
}

// ---------------------------------------------------------------- жизнь раздела

function oldServerCard(ctx){
  const host = ctx.$("#metricsOld");
  if (!host) return;
  host.replaceChildren();
  host.hidden = !STATE.oldServer;
  if (!STATE.oldServer) return;
  host.append(ctx.el("b", {}, ctx.t("metrics.old_server")));
  host.append(ctx.el("div", {class: "muted", style: "font-size:12.5px;margin-top:4px"},
    ctx.t("metrics.old_server_hint")));
  if (STATE.usageError){
    host.append(ctx.el("div", {class: "muted", style: "font-size:11.5px;margin-top:6px"},
      ctx.t("metrics.old_server_error", {why: STATE.usageError})));
  }
}

function renderStatus(ctx){
  const host = ctx.$("#metricsMessage");
  if (!host) return;
  host.replaceChildren();
  if (STATE.loading) host.append(ctx.t("metrics.loading"));
  else if (STATE.updatedAt){
    const time = new Date(STATE.updatedAt).toLocaleTimeString(
      ctx.lang === "en" ? "en-GB" : "ru-RU", {hour: "2-digit", minute: "2-digit"});
    host.append(ctx.t("metrics.updated", {time}));
  }
}

function renderDashboard(ctx){
  const host = ctx.$("#metricsDash");
  if (!host) return;
  host.replaceChildren();
  host.hidden = STATE.oldServer;
  host.setAttribute("aria-busy", STATE.loading ? "true" : "false");
  if (STATE.oldServer) return;
  if (!STATE.usage){
    host.append(STATE.loading ? spinner(ctx) : warn(ctx, ctx.t("metrics.failed")));
    return;
  }
  const usage = STATE.usage;
  const banner = qualityBanner(ctx, usage);
  if (banner) host.append(banner);
  host.append(cardsGrid(ctx, usage));
  host.append(chartCard(ctx, usage));
  host.append(projectsCard(ctx, usage));
  host.append(modelsCard(ctx, usage));
  host.append(rolesCard(ctx, usage));
  host.append(providersCard(ctx, usage));
  host.append(approxCard(ctx, usage));
  const source = (usage.totals && usage.totals.source) || "usage.d";
  host.append(ctx.el("div", {class: "muted", style: "font-size:11.5px;margin:-6px 0 14px"},
    ctx.t("metrics.source_footer", {source})));
}

function renderAll(ctx){
  rememberProjects(STATE.usage);
  renderStatus(ctx);
  oldServerCard(ctx);
  renderDashboard(ctx);
  renderLogs(ctx, STATE.logs, STATE.logsError);
  populateFilters(ctx, STATE.usage, STATE.logs);
  renderActive(ctx);
  syncControls(ctx);
}

function restoreFocus(ctx){
  if (STATE.focusRole){
    const node = ctx.root.querySelector('[data-role="' + STATE.focusRole + '"]');
    STATE.focusRole = "";
    if (node) node.focus();
    return;
  }
  if (STATE.focusFilter){
    const key = STATE.focusFilter;
    STATE.focusFilter = "";
    const id = "metricsFilter" + key[0].toUpperCase() + key.slice(1);
    const node = key === "role"
      ? ctx.root.querySelector("[data-role]") : ctx.$("#" + id);
    if (node) node.focus();
  }
}

export async function refresh(ctx){
  if (!ctx) return;
  const seq = ++STATE.seq;
  if (STATE.abort) STATE.abort.abort();
  const ac = new AbortController();
  STATE.abort = ac;
  STATE.loading = true;
  renderAll(ctx);

  const usageUrl = buildUsageQuery(STATE);
  const logsUrl = "/api/metrics?days=" + STATE.days;
  const [usageResult, logsResult] = await Promise.allSettled([
    ctx.api(usageUrl, {quiet: true, signal: ac.signal}),
    ctx.api(logsUrl, {quiet: true, signal: ac.signal}),
  ]);
  if (seq !== STATE.seq) return;
  STATE.loading = false;
  STATE.updatedAt = Date.now();

  if (usageResult.status === "rejected"){
    STATE.usage = null;
    STATE.oldServer = true;
    STATE.usageError = String(usageResult.reason && (usageResult.reason.message
      || usageResult.reason) || ctx.t("metrics.failed"));
  } else {
    const usage = usageResult.value;
    if (!isOldServer(usage)){
      STATE.usage = usage;
      STATE.oldServer = false;
      STATE.usageError = usage.error ? String(usage.error) : "";
    } else {
      STATE.usage = null;
      STATE.oldServer = true;
      STATE.usageError = usage && usage.error ? String(usage.error) : "";
    }
  }

  if (logsResult.status === "rejected"){
    STATE.logs = null;
    STATE.logsError = String(logsResult.reason && (logsResult.reason.message
      || logsResult.reason) || ctx.t("metrics.failed"));
  } else {
    STATE.logs = logsResult.value || null;
    STATE.logsError = STATE.logs && STATE.logs.error ? String(STATE.logs.error) : "";
  }

  renderAll(ctx);
  restoreFocus(ctx);
}

export function mount(ctx){
  ctx.root.dataset.module = "metrics";
  loadPrefs();
  buildControls(ctx);
  renderActive(ctx);
  syncControls(ctx);
}

export default {mount, refresh};
