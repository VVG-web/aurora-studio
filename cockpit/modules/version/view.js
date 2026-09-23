/* Версия и миграция — раздел-модуль.

   Панель здесь ничего не делает сама: она показывает две версии и открывает окно
   запуска. Обновление — всегда предпросмотр, и только потом запись: `--apply` панель
   не подставляет. */

export function mount(ctx){
  ctx.root.dataset.module = "version";
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#versionBody");
  box.innerHTML = "";
  const p = ctx.project;
  if (!p){
    box.append(el("div", {class:"card", style:"padding:24px"}, t("version.pick_project")));
    return;
  }
  box.append(el("div", {class:"card", style:"padding:20px"},
    el("div", {class:"row"},
      el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("version.in_project")),
        el("div", {class:"mono", style:"font-size:24px;font-weight:700"}, p.engine)),
      el("div", {style:"font-size:22px;color:var(--text-muted)"}, "→"),
      el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("version.in_kit")),
        el("div", {class:"mono", style:"font-size:24px;font-weight:700;color:var(--gold)"}, p.kit)),
      el("div", {class:"spacer"}),
      p.behind ? el("span", {class:"chip warn"}, t("version.behind"))
               : el("span", {class:"chip ok"}, t("version.current"))),
    el("div", {class:"row", style:"margin-top:18px"},
      el("button", {class:"btn primary", onclick: () => ctx.openRun("kit:update")},
        t("version.preview")),
      el("button", {class:"btn sm", onclick: () => ctx.openDoc("CHANGELOG.md")},
        t("version.whats_new")))));

  box.append(el("div", {class:"warnbox"}, t("version.warn")));

  box.append(el("h2", {}, t("version.migration")));
  const row = (cmd, what, flags) => el("div", {class:"list-item"},
    el("span", {class:"chip mono"}, cmd + (flags ? " " + flags.join(" ") : "")),
    el("div", {style:"flex:1"}, what),
    el("button", {class:"btn sm", onclick: () => ctx.openRun(cmd, flags || [])},
      t("version.open")));
  // Третьей строкой здесь звали `kb:retire` — команду, которой нет в реестре с 1.45.0.
  // Кнопка открывала окно запуска несуществующей команды и молчала об этом.
  box.append(el("div", {class:"card"},
    row("kit:update", t("version.structure_only"), ["--structure-only"]),
    row("kit:doctor", t("version.structure"), ["--structure"])));
}

export default {mount, refresh};
