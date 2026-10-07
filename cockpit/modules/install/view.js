/* Что доустановить — раздел-модуль.

   Честность про недоступное: если для команды нужен токен, а его нет, строка говорит,
   чего не хватает и какие команды без этого не работают. Секретов раздел не касается —
   он знает только «заполнено» или «пусто». */

export function mount(ctx){
  ctx.root.dataset.module = "install";
}

// Проверить заново, не перезапуская панель: после установки из терминала или winget.
async function recheck(ctx){
  const r = await ctx.api("/api/env?fresh=1", {quiet: true});
  if (r && r.env){ ctx.state.env = r.env; await refresh(ctx); }
}

// Пакет — в Python панели (`python -m pip install`), затем проверка тем же Python.
async function pipInstall(ctx, item, btn){
  btn.disabled = true;
  btn.textContent = ctx.t("install.installing");
  const r = await ctx.api("/api/env/install", {method: "POST", quiet: true,
    body: JSON.stringify({name: item.pip})});
  if (r && r.ok) ctx.toast(ctx.t("install.installed_ok", {name: item.name}), "ok");
  else ctx.toast(ctx.t("install.err." + ((r && r.code) || "pip_failed"), {name: item.name})
                 + (r && r.output ? "\n" + r.output.slice(-400) : ""), "err");
  await recheck(ctx);
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#installBody");
  box.innerHTML = "";
  const e = ctx.state.env;
  box.append(el("div", {class:"row", style:"margin-bottom:6px;flex-wrap:wrap"},
    el("span", {class:"chip ok"}, "Python " + e.python),
    el("span", {class:"chip"}, "kit " + ctx.state.kit.version),
    el("span", {class:"chip mono"}, ctx.state.kit.path)));
  // Пакеты проверяются и ставятся в Python панели: на Windows `pip3` в терминале часто
  // принадлежит другому Python, и «установленное» панель не видит.
  if (e.python_path) box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:14px"},
    t("install.python_path", {path: e.python_path})));

  const card = el("div", {class:"card"});
  card.append(el("div", {class:"row", style:"gap:8px;align-items:center;margin-bottom:6px"},
    el("span", {class:"muted", style:"flex:1;font-size:12.5px"}, t("install.recheck_hint")),
    el("button", {class:"btn sm", onclick: () => recheck(ctx)}, t("install.recheck"))));
  e.items.forEach(i => {
    card.append(el("div", {class:"list-item"},
      el("span", {class:"chip " + (i.ok ? "ok" : "warn"), style:"flex:none"},
        i.ok ? t("install.have") : t("install.missing")),
      el("div", {style:"flex:1;min-width:0"},
        el("div", {style:"font-weight:600"}, i.name),
        el("div", {class:"muted", style:"font-size:12.5px;margin-top:2px"},
          t("install.enables", {what: i.enables})),
        i.ok ? null : el("div", {class:"mono",
          style:"font-size:12px;margin-top:6px;color:var(--text-muted);overflow-wrap:anywhere"}, i.install)),
      !i.ok && i.pip ? el("button", {class:"btn sm primary", style:"flex:none",
        onclick: ev => pipInstall(ctx, i, ev.target)}, t("install.do_install")) : null));
  });
  box.append(card);
  box.append(extrasCard(ctx));
  box.append(gitModsCard(ctx));
  box.append(harnessCard(ctx));

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
  if (p.git_provider && p.git_provider !== "generic") pc.append(gitModLine(ctx, p.git_provider));
  // Здоровье приходит частями: строка — когда пришла её часть.
  const h = ctx.health;
  if (ctx.ui.has(h, "lint"))
    pc.append(line(h.lint.baseline !== null, t("install.ratchet"),
      t("install.ratchet_what"), "kit:hooks"));
  if (ctx.ui.has(h, "doctor"))
    pc.append(line(!h.doctor.errors.length, t("install.structure"),
      h.doctor.errors[0] || t("install.structure_ok"), "kit:doctor"));
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

/* Модули Git-провайдеров: Gitea, GitLab, Bitbucket. Ставятся из кита в ~/.aurora/
   git-providers/; «на GitHub» — версия в последнем состоянии кита, чтобы было видно, что
   кит пора обновить. Обычный git-сервер работает без модуля: он встроен в движок. */
let GITMODS = null;

async function loadGitMods(ctx, fresh){
  GITMODS = await ctx.api("/api/gitmods" + (fresh ? "?fresh=1" : ""), {quiet:true});
  return GITMODS;
}

async function installGitMod(ctx, id, after){
  const r = await ctx.api("/api/gitmods/install", {method:"POST", quiet:true,
    body: JSON.stringify({id})});
  ctx.toast(r && r.ok ? ctx.t("install.gm_done", {name: id, v: r.version})
                      : (r && r.error) || ctx.t("install.gm_failed"), r && r.ok ? "ok" : "err");
  after(false);
}

function gitModsCard(ctx){
  const {t, el} = ctx;
  const wrap = el("div", {style:"margin-top:18px"});
  const draw = async (fresh) => {
    wrap.innerHTML = "";
    wrap.append(el("h2", {}, t("install.gm_title")),
      el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, t("install.gm_about")));
    const body = el("div", {class:"card"}, el("span", {class:"spin"}));
    wrap.append(body);
    const d = await loadGitMods(ctx, fresh);
    body.innerHTML = "";
    ((d && d.modules) || []).forEach(m => {
      const have = m.installed;
      const label = !have ? t("install.gm_install") : m.update ? t("install.gm_update", {v: m.kit})
                  : t("install.gm_latest");
      const about = (m.about && (m.about[ctx.lang] || m.about.ru)) || "";
      body.append(el("div", {class:"list-item", style:"align-items:flex-start"},
        el("span", {class:"chip " + (have ? (m.update ? "warn" : "ok") : ""), style:"flex:none"},
          have ? (m.update ? t("install.ex_old") : t("install.have")) : t("install.missing")),
        el("div", {style:"flex:1;min-width:0"},
          el("div", {style:"font-weight:600"}, m.title),
          el("div", {class:"muted", style:"font-size:12.5px;margin-top:2px"}, about),
          el("div", {style:"font-size:12.5px;margin-top:4px"},
            [have ? t("install.gm_installed", {v: have}) : t("install.ex_missing"),
             m.kit ? t("install.gm_kit", {v: m.kit}) : "",
             m.github ? t("install.gm_github", {v: m.github}) : ""].filter(Boolean).join(" · ")),
          m.github_newer ? el("div", {class:"muted", style:"font-size:12px"},
            t("install.gm_github_newer", {v: m.github})) : null),
        el("button", {class:"btn sm" + (!have || m.update ? " primary" : ""),
          disabled: have && !m.update ? "" : null, title: m.path,
          onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = t("install.ex_busy");
            await installGitMod(ctx, m.id, draw);
          }}, label)));
    });
    // Без модуля работает сам git: API нет, спрашивать сервер, кто он, не о чем. Отдельной
    // версии у этого пути нет — он часть движка и обновляется вместе с китом.
    body.append(el("div", {class:"list-item"},
      el("span", {class:"chip ok", style:"flex:none"}, t("install.have")),
      el("div", {style:"flex:1"}, el("div", {style:"font-weight:600"}, t("install.gm_generic")),
        el("div", {class:"muted", style:"font-size:12.5px;margin-top:2px"}, t("install.gm_generic_about")),
        el("div", {style:"font-size:12.5px;margin-top:4px"},
          t("install.gm_generic_version", {v: ctx.state.kit.version})))));
    if (d && d.error) body.append(el("div", {class:"muted"}, d.error));
    body.append(el("div", {class:"row", style:"margin-top:8px"},
      el("button", {class:"btn sm", onclick: () => draw(true)}, t("install.ex_check"))));
  };
  draw(false);
  return wrap;
}

// Проект пользуется провайдером, чей модуль не стоит или отстал от кита, — строка с
// прямой установкой, без похода в раздел «Git».
function gitModLine(ctx, provider){
  const {t, el} = ctx;
  const row = el("div", {class:"list-item"}, el("span", {class:"spin"}));
  (async () => {
    const d = GITMODS || await loadGitMods(ctx, false);
    const m = ((d && d.modules) || []).find(x => x.id === provider);
    row.innerHTML = "";
    if (!m) return row.remove();
    const ok = m.installed && !m.update;
    row.append(
      el("span", {class:"chip " + (ok ? "ok" : "warn"), style:"flex:none"}, ok ? "✓" : t("install.missing")),
      el("div", {style:"flex:1"}, el("div", {style:"font-weight:600"}, t("install.gm_project", {name: m.title})),
        el("div", {class:"muted", style:"font-size:12.5px"},
          !m.installed ? t("install.gm_project_missing") : m.update
            ? t("install.gm_project_old", {have: m.installed, kit: m.kit}) : t("install.gm_project_ok", {v: m.installed}))),
      ok ? null : el("button", {class:"btn sm primary", onclick: async (e) => {
        e.target.disabled = true;
        await installGitMod(ctx, m.id, () => ctx.show("install"));
      }}, m.installed ? t("install.gm_update", {v: m.kit}) : t("install.gm_install")));
  })();
  return row;
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

/* Aurora MCP в ассистентах машины (1.162.0). Каталог ассистентов — `scripts/harnesses.json`,
   он едет с китом. «Найти» спрашивает версии у самих команд; «Подключить» делает копию файла
   ассистента, вставляет запись и проверяет файл; «Вернуть» кладёт копию обратно. */
let HARNESS = null;

function harnessCard(ctx){
  const {t, el} = ctx;
  const wrap = el("div", {style:"margin-top:18px"});
  const draw = async (versions) => {
    wrap.innerHTML = "";
    wrap.append(el("h2", {}, t("install.hs_title")),
      el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, t("install.hs_about")));
    const body = el("div", {class:"card"}, el("span", {class:"spin"}));
    wrap.append(body);
    HARNESS = await ctx.api("/api/harness" + (versions ? "?versions=1" : ""), {quiet:true});
    body.innerHTML = "";
    if (!HARNESS || HARNESS.error){
      body.append(el("div", {class:"muted"}, (HARNESS && HARNESS.error) || t("install.hs_failed")));
      return;
    }
    const found = HARNESS.harnesses.filter(h => h.found);
    body.append(el("div", {class:"row", style:"gap:8px;align-items:center;margin-bottom:6px"},
      el("span", {class:"muted", style:"flex:1;font-size:12.5px"},
        t("install.hs_found", {n: found.length, all: HARNESS.harnesses.length})),
      el("button", {class:"btn sm", onclick: () => draw(true)}, t("install.hs_find"))));
    found.forEach(h => body.append(harnessRow(ctx, h, () => draw(versions))));
    if (!found.length) body.append(el("div", {class:"muted"}, t("install.hs_none")));
    body.append(el("div", {class:"muted mono", style:"font-size:12px;margin-top:10px;overflow-wrap:anywhere"},
      t("install.hs_server", {cmd: [HARNESS.command, ...HARNESS.args].join(" ")})));
  };
  draw(false);
  return wrap;
}

function harnessRow(ctx, h, redraw){
  const {t, el} = ctx;
  const chip = h.aurora === true ? el("span", {class:"chip ok", style:"flex:none"}, t("install.hs_on"))
    : h.aurora === false ? el("span", {class:"chip warn", style:"flex:none"}, t("install.hs_off"))
    : el("span", {class:"chip bad", style:"flex:none"}, t("install.hs_broken"));
  const snip = el("pre", {class:"mono", hidden:"", style:"font-size:12px;white-space:pre-wrap;margin:6px 0 0"},
    h.snippet);
  const act = async (what, btn) => {
    if (what === "restore" && !confirm(t("install.hs_restore_ask", {name: h.name}))) return;
    btn.disabled = true;
    const r = await ctx.api(what === "add" ? "/api/harness/add" : "/api/harness/restore",
      {method:"POST", quiet:true,
      body: JSON.stringify({id: h.id})});
    btn.disabled = false;
    ctx.toast(r && r.ok ? (what === "add" ? t("install.hs_added", {name: h.name})
                                          : t("install.hs_restored", {name: h.name}))
                        : (r && r.error) || t("install.hs_failed"), r && r.ok ? "ok" : "err");
    redraw();
  };
  const note = t("install.hs_note." + h.id);
  return el("div", {class:"list-item", style:"align-items:flex-start"},
    chip,
    el("div", {style:"flex:1;min-width:0"},
      el("div", {style:"font-weight:600"}, h.name, h.version ? el("span", {class:"muted",
        style:"font-weight:400;margin-left:8px;font-size:12.5px"}, h.version) : null),
      el("div", {class:"muted mono", style:"font-size:12px;margin-top:2px;overflow-wrap:anywhere"},
        h.config + (h.config_exists ? "" : " · " + t("install.hs_new_file"))),
      note !== "install.hs_note." + h.id ? el("div", {class:"muted", style:"font-size:12.5px;margin-top:4px"}, note) : null,
      snip),
    el("div", {class:"row", style:"gap:6px;flex:none;flex-wrap:wrap;justify-content:flex-end"},
      el("button", {class:"btn sm", onclick: () => { snip.hidden = !snip.hidden; }}, t("install.hs_show")),
      h.aurora === false ? el("button", {class:"btn sm primary",
        onclick: ev => act("add", ev.target)}, t("install.hs_add")) : null,
      h.backups ? el("button", {class:"btn sm", onclick: ev => act("restore", ev.target)},
        t("install.hs_restore", {n: h.backups})) : null));
}

export default {mount, refresh};
