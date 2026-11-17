/* Замеры — kit-level раздел: сеть/VPN вокруг прогонов и расход токенов.

   Не путать с «Отчётами» проекта: те собирают дашборды из Jira и Confluence для
   руководителя. Здесь — эксплуатационные числа самого движка. */

const PERIODS = [1, 7, 30, 0];
let days = 7;

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

function toolbar(ctx){
  const {t, el} = ctx;
  const row = el("div", {class:"row", style:"gap:6px;margin:0 0 14px;flex-wrap:wrap"});
  PERIODS.forEach(d => {
    const b = el("button", {class:"btn sm" + (d === days ? " primary" : ""),
      onclick: async () => { days = d; await refresh(ctx); }},
      d === 0 ? t("metrics.all") : d === 1 ? t("metrics.today") : t("metrics.days", {n: d}));
    row.append(b);
  });
  row.append(el("button", {class:"btn sm", title: t("metrics.refresh_hint"),
    onclick: () => refresh(ctx)}, t("metrics.refresh")));
  return row;
}

function netCard(ctx, rows){
  const {t, el, fmt} = ctx;
  const box = el("div", {class:"card", style:"padding:18px;margin-bottom:16px"});
  box.append(el("h2", {style:"font-size:16px;margin-bottom:4px"}, t("metrics.net_title")));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"},
    t("metrics.net_about")));
  if (!rows.length){
    box.append(el("div", {class:"muted", style:"font-size:13px"}, t("metrics.net_empty")));
    return box;
  }
  const head = ["when", "project", "cmd", "duration", "blocked", "reason", "vpn"];
  const tbl = el("table", {class:"table", style:"width:100%;font-size:12.5px"});
  tbl.append(el("thead", {}, el("tr", {}, head.map(h => el("th", {}, t("metrics.col." + h))))));
  const body = el("tbody");
  rows.forEach(r => {
    const reason = t("metrics.reason." + (r.reason || "unknown"));
    const tr = el("tr", {},
      el("td", {class:"mono"}, r.at ? fmt.when(r.at) : "—"),
      el("td", {}, r.project || "—"),
      el("td", {class:"mono", style:"overflow-wrap:anywhere"}, r.cmd || "—"),
      el("td", {}, fmt.howLong(r.seconds || 0)),
      el("td", {}, r.blocked_s ? fmt.howLong(r.blocked_s) : "—"),
      el("td", {}, reason),
      el("td", {}, (r.vpn || []).join(", ") || "—"));
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
  box.append(tbl);
  return box;
}

function tokenCard(ctx, data){
  const {t, el} = ctx;
  const box = el("div", {class:"card", style:"padding:18px;margin-bottom:16px"});
  box.append(el("h2", {style:"font-size:16px;margin-bottom:4px"}, t("metrics.tokens_title")));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"},
    t("metrics.tokens_about")));
  const totals = (data && data.totals) || {};
  if (!data || !data.records){
    box.append(el("div", {class:"muted", style:"font-size:13px"}, t("metrics.tokens_empty")));
    return box;
  }
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
      el("div", {style:"font-size:16px"}, num(data.records)))));
  if (data.old) box.append(el("div", {class:"muted", style:"font-size:11.5px;margin-bottom:8px"},
    t("metrics.old", {n: data.old})));
  const head = ["project", "role", "model", "calls", "in", "out", "total", "share"];
  const tbl = el("table", {class:"table", style:"width:100%;font-size:12.5px"});
  tbl.append(el("thead", {}, el("tr", {}, head.map(h => el("th", {}, t("metrics.col." + h))))));
  const body = el("tbody");
  (data.rows || []).forEach(r => {
    const total = (r.tokens_in || 0) + (r.tokens_out || 0);
    body.append(el("tr", {},
      el("td", {}, r.project || "—"),
      el("td", {}, r.role || "—"),
      el("td", {class:"mono", style:"overflow-wrap:anywhere"}, r.model || "—"),
      el("td", {}, num(r.calls)),
      el("td", {}, num(r.tokens_in)),
      el("td", {}, num(r.tokens_out)),
      el("td", {}, num(total)),
      el("td", {}, pct(total, all) + "%")));
  });
  tbl.append(body);
  box.append(tbl);
  return box;
}

export async function refresh(ctx){
  const box = ctx.$("#metricsBody");
  if (!box) return;
  box.replaceChildren(ctx.el("div", {class:"card", style:"padding:24px"},
    ctx.el("span", {class:"spin"})));
  const d = await ctx.api("/api/metrics?days=" + days, {quiet:true});
  box.replaceChildren(toolbar(ctx));
  if (!d || d.error){
    box.append(ctx.el("div", {class:"warnbox", style:"padding:14px"},
      (d && d.error) || ctx.t("metrics.failed")));
    return;
  }
  box.append(netCard(ctx, d.net || []));
  box.append(tokenCard(ctx, d.tokens || {}));
}

export default {mount, refresh};
