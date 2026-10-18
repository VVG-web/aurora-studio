/* Команды — раздел-модуль.

   Реестр движка как есть: что команда делает, кто исполнитель, какие у неё модификаторы,
   с какой версии она существует и чем кончился последний запуск. Опасность видна до
   нажатия: «пишет» и «наружу» — отдельные метки, а не строчка в описании. */

// Команды наружу названы поимённо: признак «пишет в чужую систему» из реестра не
// вычисляется, а спутать его с записью в свои файлы — дорого.
const OUTWARD = ["sync:confluence", "sync:jira", "ship:publish", "ship:export"];

export function mount(ctx){
  ctx.root.dataset.module = "commands";
  ctx.$("#cmdSearch").oninput = () => refresh(ctx);
}

// Зависимости команд от связи (`command_deps.json`, профили — из проекта): что нужно команде, чтобы она заработала.
let NET = {};

async function loadNet(ctx){
  const q = ctx.project ? "?project=" + encodeURIComponent(ctx.project.path) : "";
  const d = await ctx.api("/api/net/commands" + q, {quiet: true});
  NET = (d && d.commands) || {};
}

export async function refresh(ctx){
  const {t, el} = ctx;
  await loadNet(ctx);
  const q = (ctx.$("#cmdSearch").value || "").toLowerCase().trim();
  const all = ctx.state.commands.filter(r => !ctx.isEngineCmd(r));
  const rows = all.filter(r => !q ||
    (r.cmd + " " + r.alias + " " + r.what + " " + r.impl).toLowerCase().includes(q));
  ctx.$("#cmdCount").textContent = t("commands.count", {shown: rows.length, total: all.length});

  const box = ctx.$("#cmdList");
  box.innerHTML = "";
  const groups = {};
  rows.forEach(r => (groups[r.ns] ||= []).push(r));
  for (const [ns, list] of Object.entries(groups)){
    // Неизвестный неймспейс показываем как есть: имя ключа на экране хуже, чем «kb».
    const title = t("commands.ns." + ns);
    box.append(el("h2", {}, title.startsWith("commands.ns.") ? ns : title));
    const card = el("div", {class:"card"});
    list.forEach(r => card.append(row(ctx, r)));
    box.append(card);
  }
}

// Строка зависимостей: тип-ярлык (1 — источники или сеть, 2 — модель, 3 — ни то ни другое; только подпись), VPN-профили,
// нужна ли модель, работает ли без связи, какие источники использует. Решение о запуске принимают сами метаданные.
function netChips(ctx, n){
  const {t, el} = ctx;
  if (!n) return null;
  const chips = [el("span", {class:"chip", title: t("commands.net.type_hint")}, t("commands.net.type", {types: n.types.join("+")}))];
  if (n.profiles.length) chips.push(el("span", {class:"chip warn", title: t("commands.net.vpn_hint")}, t("commands.net.vpn", {names: n.profiles.join(", ")})));
  if (n.llm) chips.push(el("span", {class:"chip", title: n.llm_profiles.length ? t("commands.net.llm_vpn", {names: n.llm_profiles.join(", ")}) : t("commands.net.llm_hint")}, t("commands.net.llm")));
  if (n.internet && !n.profiles.length) chips.push(el("span", {class:"chip warn"}, t("commands.net.internet")));
  chips.push(n.offline ? el("span", {class:"chip ok", title: t("commands.net.offline_hint")}, t("commands.net.offline"))
                       : el("span", {class:"chip", title: t("commands.net.needs_net_hint")}, t("commands.net.needs_net")));
  if (n.variant) chips.push(el("span", {class:"chip", title: t("commands.net.variant_hint")}, t("commands.net.variant." + n.variant)));
  if (n.modules.length) chips.push(el("span", {class:"chip mono", title: t("commands.net.sources_hint")},
    t("commands.net.sources", {list: n.sources.length ? n.sources.join(", ") : n.modules.join(", ")})
    + (n.sources.length ? "" : " · " + t("commands.net.no_source"))));
  return el("div", {class:"row", style:"gap:6px;margin-top:6px;flex-wrap:wrap"}, ...chips);
}

function row(ctx, r){
  const {t, el} = ctx;
  const writes = r.flags.includes("--apply");
  const outward = OUTWARD.includes(r.cmd);
  const last = ctx.runs.last(r.cmd), mark = last ? ctx.runs.mark(last.rc) : null;
  return el("div", {class:"list-item"},
    el("div", {style:"flex:1;min-width:0"},
      el("div", {class:"row", style:"gap:8px"},
        el("span", {class:"mono", style:"font-weight:700"}, r.cmd),
        r.alias ? el("span", {class:"muted", style:"font-size:12px"}, "(" + r.alias + ")") : null,
        ctx.ui.kindChip(r.kind),
        writes ? el("span", {class:"chip warn"}, t("commands.writes")) : null,
        outward ? el("span", {class:"chip gold"}, t("commands.outward")) : null,
        // `since` в реестре — версия движка, а не отметка времени. Её прогоняли через
        // формат времени, и «1.3.0» превращалось в «03.01 00:00»: у половины команд в
        // списке вместо версии появления стояла выдуманная дата.
        el("span", {class:"chip"}, t("commands.since", {v: r.since})),
        mark ? el("span", {class:"chip " + mark.cls,
          title: t("commands.last_hint", {label: last.label, what: mark.what,
                                          rc: last.rc, kit: last.kit || "?"})},
          mark.what + " · " + ctx.fmt.when(last.at)) : null),
      netChips(ctx, NET[r.cmd]),
      el("div", {class:"muted", style:"font-size:13px;margin-top:5px", html: ctx.fmt.tick(r.what)}),
      r.flags.length ? el("div", {class:"mono",
        style:"font-size:11.5px;color:var(--text-muted);margin-top:6px"}, r.flags.join(" ")) : null),
    r.runnable
      ? el("button", {class:"btn sm primary", "data-llm": r.cmd, onclick: () => ctx.openRun(r.cmd)},
          t("commands.run"))
      // Команда-процедура не запускается панелью: её выполняет ассистент, а панель
      // показывает описание — обещать кнопку «Запустить» здесь было бы враньём.
      : el("button", {class:"btn sm",
          onclick: () => ctx.openDoc("skills/aurora-vault/references/" + r.impl)},
          t("commands.procedure")));
}

// Раздел чисто рисует реестр из ctx.state: скрытому перерисовываться незачем, при открытии refresh придёт.
export const lifecycle = 1;
export default {mount, refresh};
