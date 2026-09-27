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
      x.mcp ? mcpHint(ctx, x.mcp) : null,
      x.id === "pydantic-ai" && have ? compatLine(ctx, x, redraw) : null,
      x.id === "pydantic-ai" ? pydanticHint(ctx) : null),
    btn);
  return row;
}

function mcpHint(ctx, m){
  const {t, el} = ctx;
  const snippet = JSON.stringify({mcpServers: {[m.name]: {command: m.command, args: m.args,
                                 ...(m.env ? {env: m.env} : {})}}}, null, 2);
  return el("details", {style:"margin-top:6px"},
    el("summary", {style:"font-size:12.5px;cursor:pointer"},
      m.registered ? t("install.ex_mcp_on") : t("install.ex_mcp_off")),
    el("div", {class:"muted", style:"font-size:12px;margin:6px 0"}, t("install.ex_mcp_hint")),
    el("pre", {class:"mono", style:"font-size:11.5px;white-space:pre-wrap"}, snippet),
    ctx.ui.copyButton(snippet));
}

/* Совместимость установленной версии Pydantic AI с Авророй. Новая версия проходит
   самопроверку один раз — при обновлении из панели или при первом вызове модели; не прошла —
   движок работает прямым HTTP, а обновление из панели возвращает прежнюю версию. */
function compatLine(ctx, x, redraw){
  const {t, el} = ctx;
  const c = x.compat;
  const text = !c ? t("install.ex_compat_none")
    : c.ok ? t("install.ex_compat_ok", {at: c.at})
    : t("install.ex_compat_bad", {why: (c.problems || []).slice(0, 2).join("; ")});
  return el("div", {class:"row", style:"gap:8px;margin-top:4px;font-size:12.5px"},
    el("span", {class:"chip " + (!c ? "" : c.ok ? "ok" : "warn")}, text),
    el("button", {class:"btn sm", onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = t("install.ex_busy");
      const r = await ctx.api("/api/extras/check", {method:"POST",
        body: JSON.stringify({id: x.id}), quiet:true});
      ctx.toast(r.ok ? t("install.ex_compat_ok", {at: ""}) :
                (r.error || t("install.ex_compat_bad", {why: (r.problems || []).join("; ")})),
                r.ok ? "ok" : "err");
      redraw(false);
    }}, t("install.ex_compat_check")));
}

/* Что уходит в шлюз через Pydantic AI — по шлюзам и ролям: модель, рассуждения и поля
   chat-шаблона ровно так, как они лягут в запрос (extra_body). Раньше это знал только код
   адаптера; у части моделей рассуждения по умолчанию выключены, и человек должен видеть,
   что движок включает их явно. Грузится при раскрытии; ключей в ответе нет. */
function pydanticHint(ctx){
  const {t, el} = ctx;
  const box = el("div", {style:"margin-top:6px"});
  let loaded = false;
  const det = el("details", {style:"margin-top:6px", ontoggle: async () => {
    if (!det.open || loaded) return;
    loaded = true;
    box.innerHTML = "";
    box.append(el("span", {class:"spin"}));
    const q = ctx.project ? "?project=" + encodeURIComponent(ctx.project.path) : "";
    const d = await ctx.api("/api/agent/pydantic" + q, {quiet:true});
    box.innerHTML = "";
    if (d.error){ box.append(el("div", {class:"muted"}, d.error)); return; }
    box.append(el("div", {class:"chip " + (d.active ? "ok" : "warn"), style:"margin-bottom:6px"},
      d.active ? t("install.ex_settings_on", {v: d.venv.version})
               : t("install.ex_settings_off", {why: d.venv.ok ? t("install.ex_not_selected")
                                                               : t("install.ex_not_installed")})));
    box.append(el("div", {class:"muted", style:"font-size:12px;margin-bottom:4px"},
      t("install.ex_settings_about")));
    box.append(el("div", {class:"muted", style:"font-size:12px;margin-bottom:8px"},
      t("install.ex_settings_client")));
    if (!(d.backends || []).length){
      box.append(el("div", {class:"muted"}, t("install.ex_no_backends")));
      return;
    }
    const sent = {};
    d.backends.forEach(b => {
      box.append(el("div", {style:"font-weight:600;font-size:12.5px;margin:8px 0 4px"},
        "№" + b.n + " ", el("span", {class:"mono", style:"font-weight:400"}, b.url)));
      if (b.template_error) box.append(el("div", {class:"chip warn", style:"font-size:11.5px"},
        t("install.ex_tpl_bad", {why: b.template_error})));
      const tbl = el("table", {class:"mono", style:"font-size:11.5px;border-collapse:collapse;width:100%"},
        el("tr", {class:"muted"},
          ...[t("install.ex_role"), t("install.ex_model"), t("install.ex_thinking"),
              t("install.ex_timeout"), "extra_body"].map(h => el("td", {style:"padding:2px 8px 2px 0"}, h))));
      sent["№" + b.n] = {};
      Object.entries(b.roles || {}).forEach(([role, r]) => {
        sent["№" + b.n][role] = {model: r.model, extra_body: r.extra_body};
        tbl.append(el("tr", {},
          el("td", {style:"padding:2px 8px 2px 0"}, role),
          el("td", {style:"padding:2px 8px 2px 0"}, r.model || "—"),
          el("td", {style:"padding:2px 8px 2px 0"}, r.thinking ? t("install.ex_on") : t("install.ex_off")),
          el("td", {style:"padding:2px 8px 2px 0"}, String(r.timeout)),
          el("td", {style:"padding:2px 0;word-break:break-all"}, JSON.stringify(r.extra_body))));
      });
      box.append(el("div", {style:"overflow-x:auto"}, tbl));
    });
    box.append(el("div", {style:"margin-top:8px"},
      ctx.ui.copyButton(JSON.stringify(sent, null, 2))));
  }},
    el("summary", {style:"font-size:12.5px;cursor:pointer"}, t("install.ex_settings")),
    box);
  return det;
}

export default {mount, refresh};
