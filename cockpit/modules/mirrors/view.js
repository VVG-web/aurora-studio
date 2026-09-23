/* Зеркала внешних систем — раздел-модуль.

   Что за зеркала есть, раздел не решает: список даёт реестр подключённых модулей
   источников, а числа — здоровье проекта. Панель ничего не считает сама. */

export function mount(ctx){
  ctx.root.dataset.module = "mirrors";
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#mirrorsBody");
  if (!ctx.project){
    box.innerHTML = "";
    box.append(el("div", {class:"card", style:"padding:24px"}, t("mirrors.pick_project")));
    return;
  }
  // Считается несколько секунд: сказать «нет данных» при выбранном проекте — соврать.
  if (!ctx.health){
    box.innerHTML = '<div class="grid metrics">' + '<div class="skel"></div>'.repeat(2) + '</div>';
    return;
  }
  const h = ctx.health;
  const m = h.mirrors || {}, p = ctx.project;
  const src = h.sources || {installed: [], instances: []};
  box.innerHTML = "";
  box.append(sourceCards(ctx, h.source_health || {}));

  if (!src.instances.length)
    box.append(el("div", {class:"warnbox"}, t("mirrors.none")));
  else
    box.append(el("div", {class:"grid", style:"grid-template-columns:repeat(auto-fit,minmax(320px,1fr))"},
      src.instances.map(inst => mirrorCard(ctx, inst, m, src, p))));

  box.append(sourceModules(ctx, src));
  box.append(el("h2", {}, t("mirrors.chain")));
  box.append(el("div", {class:"card"},
    ["sync:audit", "sync:diff", "sync:jira-status", "kit:remap-sources"].map(c => {
      const row = ctx.state.commands.find(x => x.cmd === c);
      if (!row) return null;
      return el("div", {class:"list-item"},
        el("span", {class:"chip mono"}, c),
        el("div", {style:"flex:1", html: ctx.fmt.tick(row.what)}),
        el("button", {class:"btn sm", onclick: () => ctx.openRun(c)}, t("mirrors.run")));
    })));
  if (!p.has_env)
    box.append(el("div", {class:"warnbox"}, t("mirrors.no_env")));
}

function mirrorCard(ctx, inst, m, src, p){
  const {t, el} = ctx;
  // до 1.28 аудит называл разделы по продукту («Jira»), а не по id зеркала («JIRA»)
  const key = Object.keys(m).find(k => k.toLowerCase() === inst.id.toLowerCase());
  const data = m[key], man = src.installed.find(x => x.id === inst.module) || {};
  const tokenOk = !inst.env_prefix || (p.tokens || []).includes(inst.env_prefix);
  const bad = data && (data.no_state || data.missing || data.orphan);
  // зеркало без файла состояния сверить не с чем: показываем это прямо, а не нулями
  const numbers = data && data.no_state
    ? el("div", {class:"row"},
        el("span", {class:"chip bad"}, t("mirrors.no_state")),
        el("span", {class:"chip"}, t("mirrors.files", {n: data.files})))
    : data ? el("div", {class:"row"},
        el("span", {class:"chip " + (data.missing ? "bad" : "ok")}, "MISSING " + data.missing),
        el("span", {class:"chip " + (data.orphan ? "bad" : "ok")}, "ORPHAN " + data.orphan),
        data.age_days != null ? el("span", {class:"chip"}, t("mirrors.age", {n: data.age_days})) : null)
    : el("div", {class:"chip"}, t("mirrors.empty"));
  return el("div", {class:"card", style:"padding:18px"},
    el("div", {class:"row"}, el("b", {}, inst.id + " · " + (inst.title || inst.module)),
      el("span", {class:"chip " + (tokenOk ? "ok" : "warn")},
        tokenOk ? t("mirrors.token_ok") : t("mirrors.token_no"))),
    el("p", {class:"muted", style:"font-size:13px;margin:10px 0"},
      (man.what || "") + " " + t("mirrors.folder", {path: inst.path})),
    numbers,
    el("div", {class:"row", style:"margin-top:14px"},
      inst.command ? el("button", {class:"btn sm primary", disabled: tokenOk ? null : "",
        onclick: () => ctx.openRun(inst.command)}, t("mirrors.sync")) : null,
      el("button", {class:"btn sm", onclick: () => ctx.openRun("sync:audit")}, t("mirrors.audit")),
      bad ? el("span", {class:"chip warn"}, t("mirrors.drifted")) : null,
      inst.kind ? null : el("span", {class:"chip bad"}, t("mirrors.not_installed"))));
}

function sourceCards(ctx, sh){
  const {t, el} = ctx;
  const names = Object.keys(sh);
  if (!names.length)
    return el("div", {class:"card", style:"padding:18px;margin-bottom:14px"},
      el("div", {class:"muted"}, t("mirrors.sources_empty")));
  const cards = names.map(name => {
    const v = sh[name];
    const pct = v.total ? Math.round(v.parsed * 100 / v.total) : 0;
    return ctx.ui.metricCard({
      title: name,
      value: pct + "%",
      sub: t("mirrors.parsed", {parsed: v.parsed, total: v.total})
           + (v.left ? " · " + t("mirrors.waiting", {n: v.left}) : "")
           + (v.archived ? " · " + t("mirrors.archived", {n: v.archived}) : ""),
      tone: v.left ? "warn" : "",
      hint: t("mirrors.parsed_hint"),
      go: v.left ? ctx.ui.goRoute("update", t("mirrors.update_base")) : null});
  });
  return el("div", {},
    el("h2", {style:"margin-top:0"}, t("mirrors.became_knowledge")),
    el("div", {class:"grid metrics", style:"margin-bottom:18px"}, ...cards));
}

function sourceModules(ctx, src){
  const {t, el} = ctx;
  const on = new Set(src.instances.map(i => i.module));
  const rows = src.installed.map(man => {
    const check = el("input", {type:"checkbox", checked: on.has(man.id) ? "" : null});
    check.dataset.module = man.id;
    return el("label", {class:"list-item", style:"cursor:pointer"}, check,
      el("div", {style:"flex:1"},
        el("b", {}, man.title || man.id),
        el("div", {class:"muted", style:"font-size:12px"},
          `${man.kind} · ${man.mirror.default_path} · ${man.what || ""}`)),
      el("span", {class:"chip mono"}, man.id));
  });
  const wrap = el("div", {class:"card"}, ...rows);
  const save = el("button", {class:"btn sm primary"}, t("mirrors.save_choice"));
  save.onclick = async () => {
    const picked = [...wrap.querySelectorAll("input[type=checkbox]")]
      .filter(b => b.checked).map(b => b.dataset.module);
    const r = await ctx.api("/api/sources", {method:"POST", body: JSON.stringify(
      {project: ctx.project.path, modules: picked})});
    if (!r || r.error) return;
    ctx.toast(t("mirrors.saved"), "ok");
    await ctx.reloadHealth();
    await refresh(ctx);
  };
  return el("div", {},
    el("h2", {}, t("mirrors.modules")),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, t("mirrors.modules_about")),
    wrap, el("div", {class:"row", style:"margin-top:12px"}, save));
}

export default {mount, refresh};
