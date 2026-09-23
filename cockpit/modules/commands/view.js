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

export async function refresh(ctx){
  const {t, el} = ctx;
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
      el("div", {class:"muted", style:"font-size:13px;margin-top:5px", html: ctx.fmt.tick(r.what)}),
      r.flags.length ? el("div", {class:"mono",
        style:"font-size:11.5px;color:var(--text-muted);margin-top:6px"}, r.flags.join(" ")) : null),
    r.runnable
      ? el("button", {class:"btn sm primary", onclick: () => ctx.openRun(r.cmd)},
          t("commands.run"))
      // Команда-процедура не запускается панелью: её выполняет ассистент, а панель
      // показывает описание — обещать кнопку «Запустить» здесь было бы враньём.
      : el("button", {class:"btn sm",
          onclick: () => ctx.openDoc("skills/aurora-vault/references/" + r.impl)},
          t("commands.procedure")));
}

export default {mount, refresh};
