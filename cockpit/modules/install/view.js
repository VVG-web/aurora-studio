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
  box.append(extrasCard(ctx));

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

/* Надстройки движка: Pydantic AI и graphify. Каждая — в своём venv под ~/.aurora/.
   Строка показывает, что стоит, что вышло в git (последний выпуск на GitHub) и что
   ставит pip (PyPI); кнопка ставит или обновляет. Сеть спрашивается раз в шесть часов,
   «Проверить» — сейчас. */
function extrasCard(ctx){
  const {t, el} = ctx;
  const wrap = el("div", {style:"margin-top:18px"});
  const draw = async (fresh) => {
    wrap.innerHTML = "";
    wrap.append(el("h2", {}, t("install.extras")),
      el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, t("install.extras_about")));
    const body = el("div", {class:"card"}, el("span", {class:"spin"}));
    wrap.append(body);
    const d = await ctx.api("/api/extras" + (fresh ? "?fresh=1" : ""), {quiet:true});
    body.innerHTML = "";
    (d.extras || []).forEach(x => body.append(extraRow(ctx, x, draw)));
    if (d.error) body.append(el("div", {class:"muted"}, d.error));
    body.append(el("div", {class:"row", style:"margin-top:8px"},
      el("button", {class:"btn sm", onclick: () => draw(true)}, t("install.ex_check"))));
  };
  draw(false);
  return wrap;
}

function extraRow(ctx, x, redraw){
  const {t, el} = ctx;
  const have = x.installed;
  const ver = have ? t("install.ex_installed", {v: x.installed}) : t("install.ex_missing");
  const git = x.git ? t("install.ex_git", {v: x.git}) : "";
  const pypi = x.pypi && x.pypi !== x.git ? t("install.ex_pypi", {v: x.pypi}) : "";
  const label = !have ? t("install.ex_install")
    : x.update ? t("install.ex_update", {v: x.pypi}) : t("install.ex_latest");
  const btn = el("button", {class:"btn sm" + (!have || x.update ? " primary" : ""),
    disabled: have && !x.update ? "" : null,
    title: t("install.ex_where", {path: x.venv}),
    onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = t("install.ex_busy");
      const r = await ctx.api("/api/extras/install", {method:"POST",
        body: JSON.stringify({id: x.id}), quiet:true});
      ctx.toast(r.ok ? t("install.ex_done", {name: x.title, v: r.version})
                     : (r.error || t("install.ex_failed", {log: (r.log || "").slice(-200)})),
                r.ok ? "ok" : "err");
      redraw(true);
    }}, label);
  const row = el("div", {class:"list-item", style:"align-items:flex-start"},
    el("span", {class:"chip " + (have ? (x.update ? "warn" : "ok") : ""), style:"flex:none"},
      have ? (x.update ? t("install.ex_old") : t("install.have")) : t("install.missing")),
    el("div", {style:"flex:1;min-width:0"},
      el("div", {style:"font-weight:600"}, x.title, " ",
        el("a", {href: x.repo, target:"_blank", class:"muted",
                 style:"font-weight:400;font-size:12px"}, "GitHub")),
      el("div", {class:"muted", style:"font-size:12.5px;margin-top:2px"},
        t("install.enables", {what: x.enables})),
      el("div", {style:"font-size:12.5px;margin-top:4px"},
        [ver, git, pypi].filter(Boolean).join(" · ")),
      x.pypi_behind ? el("div", {class:"muted", style:"font-size:12px"},
        t("install.ex_pypi_behind", {git: x.git, pypi: x.pypi})) : null,
      x.error ? el("div", {class:"muted", style:"font-size:12px"}, x.error) : null,
      x.mcp ? mcpHint(ctx, x.mcp) : null),
    btn);
  return row;
}

function mcpHint(ctx, m){
  const {t, el} = ctx;
  const snippet = JSON.stringify({mcpServers: {[m.name]: {command: m.command, args: m.args}}},
                                 null, 2);
  return el("details", {style:"margin-top:6px"},
    el("summary", {style:"font-size:12.5px;cursor:pointer"},
      m.registered ? t("install.ex_mcp_on") : t("install.ex_mcp_off")),
    el("div", {class:"muted", style:"font-size:12px;margin:6px 0"}, t("install.ex_mcp_hint")),
    el("pre", {class:"mono", style:"font-size:11.5px;white-space:pre-wrap"}, snippet),
    ctx.ui.copyButton(snippet));
}

export default {mount, refresh};
