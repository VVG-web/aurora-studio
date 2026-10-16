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
  // Флаг ролей спрашиваем ДО очистки контейнера и собираем всё одним синхронным проходом: два
  // refresh подряд (повторный показ раздела и событие «data») иначе стёрли бы друг друга посреди
  // await и дорисовали каждый своё — карточки задвоились бы. Сбой сети = флаг «выключен».
  let pf = null;
  try { pf = await ctx.api("/api/profiles", {quiet: true}); } catch (e) { pf = null; }
  const rolesOn = !!(pf && pf.enabled);
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

  // Фиче-флаг «роли для профилей» (BL-ARC-04): выключен по умолчанию, хранится на машине
  // (`local/features.json`). Переключатель живёт здесь, а не в «Настройке»: сама роль появляется
  // там только после включения. Меню и маршруты зависят от флага на сервере — страница перечитывается.
  box.append(el("div", {class: "card", style: "padding:16px 20px;margin-bottom:18px"},
    el("div", {class: "row", style: "justify-content:space-between;gap:12px"},
      el("div", {},
        el("b", {}, t("dev.roles_title")),
        el("span", {class: "pill", id: "devRolesState", style: "margin-left:8px"},
          rolesOn ? t("dev.roles_on") : t("dev.roles_off")),
        el("div", {class: "muted", style: "font-size:13px;margin-top:4px"}, t("dev.roles_about"))),
      el("button", {class: "btn sm", id: "devRolesToggle", onclick: async () => {
        const r = await ctx.api("/api/features/set", {method: "POST", quiet: true,
          body: JSON.stringify({name: "profiles", on: !rolesOn})});
        if (!r || !r.ok) return ctx.toast((r && r.error) || t("dev.roles_failed"), "err");
        location.reload();
      }}, rolesOn ? t("dev.roles_disable") : t("dev.roles_enable")))));

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

// refresh спрашивает у сервера только флаг ролей и рисует список команд движка из ctx.state:
// скрытому раздел не нужен.
export const lifecycle = 1;
export default {mount, refresh};
