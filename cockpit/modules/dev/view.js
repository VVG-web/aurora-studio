/* Разработка движка — раздел-модуль.

   Сам себе доказательство: раздел, который открывается семью нажатиями на «О проекте»,
   лежит такой же папкой, как остальные, и помечен `"dev": true` в манифесте.

   Команды выполняются в дереве кита, а не в проекте с Мостика: `dev:` относится к самому
   движку. */

export function mount(ctx){
  ctx.root.dataset.module = "dev";
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#devBody");
  box.innerHTML = "";
  const cmds = (ctx.state.commands || []).filter(ctx.isEngineCmd);

  box.append(el("div", {class: "card", style: "padding:20px;margin-bottom:18px"},
    el("p", {class: "muted", style: "font-size:13px;margin:0 0 10px"}, t("dev.about")),
    el("p", {class: "muted", style: "font-size:13px;margin:0 0 14px"}, t("dev.where")),
    el("div", {class: "row"},
      el("button", {class: "btn sm danger", onclick: () => {
        ctx.hideDev();
        ctx.toast(t("dev.hidden"));
      }}, t("dev.hide")))));

  if (!cmds.length){
    box.append(el("div", {class: "card", style: "padding:20px"}, t("dev.not_from_kit")));
    return;
  }

  // порядок по частоте использования, а не по алфавиту: прогон — то, ради чего заходят
  const order = ["dev:qa-run", "dev:qa-cover", "dev:qa-gap", "dev:qa-list", "dev:qa-check",
                 "dev:qa-new", "kit:skills"];
  const rank = c => { const i = order.indexOf(c.cmd); return i < 0 ? 99 : i; };
  cmds.sort((a, b) => rank(a) - rank(b));

  for (const c of cmds)
    box.append(el("div", {class: "card", style: "padding:16px 20px;margin-bottom:10px"},
      el("div", {class: "row", style: "justify-content:space-between;gap:12px"},
        el("div", {},
          el("b", {class: "mono"}, c.cmd),
          el("div", {class: "muted", style: "font-size:13px;margin-top:4px"}, c.what)),
        el("button", {class: "btn sm", onclick: () => ctx.openRun(c)}, t("dev.run")))));

  box.append(el("p", {class: "muted", style: "font-size:12px;margin-top:14px"}, t("dev.note")));
}

export default {mount, refresh};
