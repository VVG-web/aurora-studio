/* Что доустановить — раздел-модуль.

   Честность про недоступное: если для команды нужен токен, а его нет, строка говорит,
   чего не хватает и какие команды без этого не работают. Секретов раздел не касается —
   он знает только «заполнено» или «пусто». */

export function mount(ctx){
  ctx.root.dataset.module = "install";
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#installBody");
  box.innerHTML = "";
  const e = ctx.state.env;
  box.append(el("div", {class:"row", style:"margin-bottom:14px"},
    el("span", {class:"chip ok"}, "Python " + e.python),
    el("span", {class:"chip"}, "kit " + ctx.state.kit.version),
    el("span", {class:"chip mono"}, ctx.state.kit.path)));

  const card = el("div", {class:"card"});
  e.items.forEach(i => {
    card.append(el("div", {class:"list-item"},
      el("span", {class:"chip " + (i.ok ? "ok" : "warn"), style:"flex:none"},
        i.ok ? t("install.have") : t("install.missing")),
      el("div", {style:"flex:1;min-width:0"},
        el("div", {style:"font-weight:600"}, i.name),
        el("div", {class:"muted", style:"font-size:12.5px;margin-top:2px"},
          t("install.enables", {what: i.enables})),
        i.ok ? null : el("div", {class:"mono",
          style:"font-size:12px;margin-top:6px;color:var(--text-muted)"}, i.install))));
  });
  box.append(card);

  if (!ctx.project) return;
  const p = ctx.project;
  box.append(el("h2", {}, t("install.project_ready", {name: p.name})));
  const pc = el("div", {class:"card"});
  const line = (ok, name, what, cmd) => el("div", {class:"list-item"},
    el("span", {class:"chip " + (ok ? "ok" : "warn"), style:"flex:none"},
      ok ? "✓" : t("install.missing")),
    el("div", {style:"flex:1"}, el("div", {style:"font-weight:600"}, name),
      el("div", {class:"muted", style:"font-size:12.5px"}, what)),
    cmd ? el("button", {class:"btn sm", onclick: () => ctx.openRun(cmd)}, t("install.fix")) : null);
  pc.append(line(p.has_env, t("install.env_file"), t("install.env_file_what")));
  pc.append(line(p.confluence_token, t("install.conf_token"), "sync:confluence, ship:publish"));
  pc.append(line(p.jira_token, t("install.jira_token"), "sync:jira, sync:jira-status"));
  pc.append(line(!p.behind, t("install.engine_ok"),
    t("install.engine_what", {engine: p.engine, kit: p.kit}), "kit:update"));
  const h = ctx.health;
  if (h){
    pc.append(line(h.lint.baseline !== null, t("install.ratchet"),
      t("install.ratchet_what"), "kit:hooks"));
    pc.append(line(!h.doctor.errors.length, t("install.structure"),
      h.doctor.errors[0] || t("install.structure_ok"), "kit:doctor"));
  }
  box.append(pc);
}

export default {mount, refresh};
