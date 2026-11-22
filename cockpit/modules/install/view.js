/* Что доустановить — раздел-модуль.

   Честность про недоступное: если для команды нужен токен, а его нет, строка говорит,
   чего не хватает и какие команды без этого не работают. Секретов раздел не касается —
   он знает только «заполнено» или «пусто». */

export function mount(ctx){
  ctx.root.dataset.module = "install";
}

// Проверить заново, не перезапуская панель: после установки из терминала или winget. Настоящий
// запуск надстроек — тоже заново: шапка «для кого» и строки надстроек строятся снова.
async function recheck(ctx){
  access(ctx, true);
  if (CARDS){ CARDS.access = accessCard(ctx); CARDS.extras = extrasCard(ctx); }
  const r = await ctx.api("/api/env?fresh=1", {quiet: true});
  if (r && r.env) ctx.state.env = r.env;
  await refresh(ctx);
}

/* Python подсистемы: один интерпретатор для установщика, проверки совместимости,
   agent_core.py, MCP-серверов и инструментов. Карточка показывает выбранное, найденные
   интерпретаторы и даёт выбрать вручную. Ошибки здесь не автоскрываются: причина отказа
   установки должна остаться на экране, пока её не прочитают. */
function runtimeCard(ctx){
  const {t, el} = ctx;
  const wrap = el("div", {style:"margin-top:18px"});
  // Последняя ошибка действия живёт в карточке, а не в тосте: полный лог pip
  // (тысячи строк) нельзя показывать на пол-экрана, но и терять его незачем.
  let lastError = "", lastOutput = "", lastBackup = "", lastReused = false;
  const draw = async (fresh) => {
    wrap.innerHTML = "";
    wrap.append(el("h2", {}, t("install.rt_title")),
      el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, t("install.rt_about")));
    const body = el("div", {class:"card"}, el("span", {class:"spin"}));
    wrap.append(body);
    const projectQuery = ctx.project
      ? "?project=" + encodeURIComponent(ctx.project.path) : "";
    const d = await ctx.api("/api/runtime" + projectQuery + (fresh ? (projectQuery ? "&" : "?") + "fresh=1" : ""), {quiet:true});
    body.innerHTML = "";
    if (!d || d.error){
      body.append(el("div", {class:"muted"}, (d && d.error) || t("install.rt_unknown")));
      return;
    }
    const rt = d.runtime || {};
    const base = d.base || {};
    const pyd = d.pydantic || {};
    const mode = rt.isolated ? t("install.rt_isolated") : t("install.rt_global");
    const src = rt.source || (base.source || "");
    const install = rt.install ? t("install.kind." + rt.install) : "";
    const line = (label, value, mono) => el("div", {style:"font-size:12.5px;margin-top:4px"},
      el("span", {class:"muted"}, label + ": "),
      mono ? el("span", {class:"mono", style:"overflow-wrap:anywhere"}, value || "—")
           : el("span", {}, value || "—"));
    body.append(el("div", {class:"row", style:"gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:4px"},
      el("span", {class:"chip " + (d.ok ? "ok" : "warn")},
        d.ok ? t("install.rt_ok") : t("install.rt_bad")),
      el("span", {class:"chip " + (d.configured ? "ok" : "warn")},
        d.configured ? t("install.rt_configured") : t("install.rt_not_configured"))));
    if (d.reason) body.append(el("div", {class:"muted", style:"font-size:12.5px;margin-top:4px;overflow-wrap:anywhere"},
      t("install.rt_why", {why: d.reason})));
    line(t("install.rt_effective"), d.effective_python
      + (install ? " (" + install + (rt.python_version ? " " + rt.python_version : "") + ")" : ""), true);
    line(t("install.rt_home"), d.home, true);
    line(t("install.rt_base"), rt.base_python, true);
    line(t("install.rt_source"), src);
    if (install) line(t("install.rt_install"), install);
    if (rt.source !== "venv") line(t("install.rt_mode"), mode);
    line(t("install.rt_pydantic"),
      pyd && pyd.ok ? (pyd.version || "?") : t("install.rt_pydantic_missing"));
    if (rt.externally_managed && rt.source !== "venv")
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_managed")));
    if (rt.source === "venv")
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_direct_venv")));
    if (d.home_mismatch){
      const box = el("div", {class:"warnbox", style:"font-size:12.5px;margin-top:8px"});
      box.append(el("div", {}, t("install.rt_home_mismatch",
        {home: d.home || "?", project_home: d.project_home || "?"})));
      box.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_home_hint")));
      if (ctx.project){
        box.append(el("button", {class:"btn sm primary", style:"margin-top:8px",
          title: t("install.rt_home_drop_hint"),
          onclick: async (ev) => {
            if (!confirm(t("install.rt_home_drop_ask", {project: ctx.project.path}))) return;
            ev.target.disabled = true;
            const r = await ctx.api("/api/runtime", {method:"POST", quiet:true,
              body: JSON.stringify({action:"drop_project_home", project: ctx.project.path})});
            if (!r || r.error || r.ok === false){
              ev.target.disabled = false;
              return ctx.toast((r && r.error) || t("install.rt_home_drop_failed"), "err", {sticky:true});
            }
            ctx.toast(t("install.rt_home_dropped"), "ok");
            draw(true);
          }}, t("install.rt_home_drop")));
      }
      body.append(box);
    }
    // Панель, запущенная до смены конфигурации, работает под прежним Python. Это не ошибка
    // конфигурации, но объясняет, почему «поставилось, а не видно».
    const stale = !!(d.needs_reload
      || (d.process_python && d.effective_python && d.process_python !== d.effective_python));
    if (stale){
      const box = el("div", {class:"warnbox", style:"font-size:12.5px;margin-top:8px"});
      box.append(el("div", {}, t("install.rt_stale",
        {process: d.process_python || "?", effective: d.effective_python || "?"})));
      box.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_reload_about")));
      box.append(el("button", {class:"btn sm primary", style:"margin-top:8px",
        title: t("install.rt_reload_hint"),
        onclick: async (ev) => {
          ev.target.disabled = true;
          ev.target.textContent = t("install.ex_busy");
          await ctx.restartPanel({patient:true});
        }}, t("install.rt_reload")));
      body.append(box);
    }
    if (lastReused)
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_reused")));
    if (lastBackup)
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px;overflow-wrap:anywhere"},
        t("install.rt_backup", {path: lastBackup})));
    if (lastError){
      const box = el("div", {class:"warnbox", style:"font-size:12.5px;margin-top:8px"});
      box.append(el("div", {style:"overflow-wrap:anywhere"}, lastError));
      if (lastOutput){
        const det = el("details", {style:"margin-top:6px"});
        det.append(el("summary", {title: t("install.rt_details_hint")}, t("install.rt_details")));
        det.append(el("pre", {class:"mono",
          style:"max-height:28vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0 0"},
          lastOutput));
        box.append(det);
      }
      box.append(el("button", {class:"btn sm", style:"margin-top:6px",
        title: t("install.rt_dismiss_hint"),
        onclick: () => { lastError = ""; lastOutput = ""; draw(true); }},
        t("install.rt_dismiss")));
      body.append(box);
    }

    const row = el("div", {class:"row", style:"gap:8px;margin-top:10px;flex-wrap:wrap"});
    row.append(el("button", {class:"btn sm", title: t("install.rt_check_hint"),
      onclick: () => draw(true)}, t("install.rt_check")));
    row.append(el("button", {class:"btn sm" + (!d.configured || !d.ok ? " primary" : ""),
      title: t("install.rt_bootstrap_hint"),
      onclick: async (ev) => {
        ev.target.disabled = true; ev.target.textContent = t("install.ex_busy");
        lastError = ""; lastOutput = ""; lastBackup = ""; lastReused = false;
        const r = await ctx.api("/api/runtime", {method:"POST", quiet:true,
          body: JSON.stringify({action: "bootstrap"})});
        if (r && r.ok){
          lastOutput = r.output || "";
          lastBackup = r.backup || "";
          lastReused = !!r.reused;
          ctx.toast(t("install.rt_done", {path: (r.runtime || {}).effective_python || "?"}), "ok");
        } else {
          lastError = (r && (r.error || r.reason)) || "?";
          lastOutput = (r && r.output) || "";
          lastBackup = (r && r.backup) || "";
          ctx.toast(t("install.rt_error", {why: lastError}), "err", {sticky:true});
        }
        draw(true);
      }}, t("install.rt_bootstrap")));
    row.append(el("button", {class:"btn sm", title: t("install.rt_install_pydantic_hint"),
      onclick: async (ev) => {
        ev.target.disabled = true; ev.target.textContent = t("install.ex_busy");
        lastError = ""; lastOutput = ""; lastBackup = ""; lastReused = false;
        const r = await ctx.api("/api/runtime", {method:"POST", quiet:true,
          body: JSON.stringify({action: "install_pydantic"})});
        const ok = r && r.ok && (r.pydantic || {}).ok;
        lastOutput = (r && r.output) || "";
        lastBackup = (r && r.backup) || "";
        lastReused = !!(r && r.reused);
        if (ok) ctx.toast(t("install.rt_pydantic_done", {version: ((r.pydantic || {}).version || "?")}), "ok");
        else {
          lastError = (r && (r.error || (r.pydantic || {}).error)) || "?";
          ctx.toast(t("install.rt_pydantic_failed", {why: lastError}), "err", {sticky:true});
        }
        draw(true);
      }}, t("install.rt_install_pydantic")));
    body.append(row);

    const cands = ((d.candidates || {}).valid) || [];
    if (cands.length){
      const box = el("details", {style:"margin-top:10px"});
      box.append(el("summary", {style:"font-size:12.5px;cursor:pointer"},
        t("install.rt_choose", {n: cands.length})));
      box.append(el("div", {class:"muted", style:"font-size:12px;margin:6px 0"},
        t("install.rt_choose_about")));
      const isolate = el("input", {type:"checkbox", checked: rt.isolated === false ? null : "checked"});
      box.append(el("label", {style:"display:flex;align-items:center;gap:6px;font-size:12.5px;margin:4px 0"},
        isolate, t("install.rt_isolated_hint")));
      cands.forEach(c => {
        const b = el("button", {class:"btn sm", style:"margin:2px 6px 2px 0",
          title: c.venv
            ? t("install.rt_candidate_venv_hint")
            : t("install.rt_candidate_hint") + (c.reason ? " " + c.reason : ""),
          onclick: async (ev) => {
            ev.target.disabled = true;
            lastError = ""; lastOutput = ""; lastBackup = ""; lastReused = false;
            const r = await ctx.api("/api/runtime", {method:"POST", quiet:true,
              body: JSON.stringify({action:"configure", source: c.source, base: c.executable,
                                    isolated: !!isolate.checked})});
            if (r && r.ok){
              lastOutput = r.output || "";
              lastBackup = r.backup || "";
              lastReused = !!r.reused;
              ctx.toast(t("install.rt_done", {path: (r.runtime || {}).effective_python || "?"}), "ok");
              draw(true);
            } else {
              ev.target.disabled = false;
              lastError = (r && (r.error || r.reason)) || "?";
              lastOutput = (r && r.output) || "";
              lastBackup = (r && r.backup) || "";
              ctx.toast(t("install.rt_error", {why: lastError}), "err", {sticky:true});
              draw(true);
            }
          }},
          [(c.install ? t("install.kind." + c.install) : c.source), c.version].filter(Boolean).join(" · ")
            + " — " + (c.executable || "?")
            + (c.venv ? " · " + t("install.rt_candidate_venv") : ""));
        box.append(b);
      });
      body.append(box);
    } else {
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:8px"},
        t("install.rt_none")));
    }
  };
  // Первое открытие раздела — сразу со списком интерпретаторов: до записи конфигурации
  // без кандидатов карточка не даёт выбрать базу, а «Настроить автоматически» не всегда
  // подходит (например, нужен конкретный pre-release 3.15). Обход запускается один раз
  // на карточку, а не на каждую перерисовку состояния.
  draw(true);
  return wrap;
}

// Пакет — в Python панели (`python -m pip install`), затем проверка тем же Python.
async function pipInstall(ctx, item, btn){
  btn.disabled = true;
  btn.textContent = ctx.t("install.installing");
  const r = await ctx.api("/api/env/install", {method: "POST", quiet: true,
    body: JSON.stringify({name: item.pip})});
  if (r && r.ok) ctx.toast(ctx.t("install.installed_ok", {name: item.name}), "ok");
  else ctx.toast(ctx.t("install.err." + ((r && r.code) || "pip_failed"), {name: item.name})
                 + (r && r.output ? "\n" + r.output.slice(-400) : ""), "err", {sticky:true});
  await recheck(ctx);
}

// Блоки, которые спрашивают сеть и машину: надстройки движка, модули Git-провайдеров, MCP
// в ассистентах. Создаются один раз (и заново — при смене языка), а перерисовка раздела
// переставляет их как есть. Прежде каждая перерисовка — а ядро зовёт её на каждую часть
// здоровья и каждую смену состояния — стирала их и спрашивала сервер снова: блок мигал
// «колесо → данные → колесо» десятки раз подряд (1.170.1). Свежие данные — по их кнопкам
// «Проверить» и «Найти» и после установки.
let CARDS = null;

function cards(ctx){
  if (!CARDS || CARDS.lang !== ctx.lang)
    CARDS = {lang: ctx.lang, project: projectKey(ctx), runtime: runtimeCard(ctx),
             access: accessCard(ctx), extras: extrasCard(ctx),
             gitmods: gitModsCard(ctx), mcp: mcpCard(ctx), harness: harnessCard(ctx)};
  // Другой проект — другая копия движка, которой пойдёт прогон: «для вас» проверяется заново.
  if (CARDS.project !== projectKey(ctx))
    Object.assign(CARDS, {project: projectKey(ctx), runtime: runtimeCard(ctx),
                          access: accessCard(ctx), extras: extrasCard(ctx)});
  return CARDS;
}

/* Для кого проверено (1.196.0). Корпоративный ноутбук: панель однажды запустили «от имени
   администратора» другой учётной записью — venv лёг в её профиль, и «Установка» была зелёной глазами
   того процесса, а прогон под обычной учётной записью шёл мимо Pydantic AI. Сервер называет
   пользователя процесса, права, снимаемые для venv переменные и запускает надстройки так, как прогон
   выбранного проекта; ответ держится в памяти панели до «Проверить снова» или установки. */
let ACCESS = null;

const projectKey = ctx => ctx.project ? ctx.project.path : "";

function access(ctx, fresh){
  const key = projectKey(ctx);
  if (!ACCESS || ACCESS.key !== key || fresh){
    const q = [key ? "project=" + encodeURIComponent(key) : "", fresh ? "fresh=1" : ""].filter(Boolean).join("&");
    ACCESS = {key, promise: ctx.api("/api/install/access" + (q ? "?" + q : ""), {quiet:true})};
  }
  return ACCESS.promise;
}

const rights = (t, me) => me.elevated === true ? t("install.acc_admin")
  : me.elevated === false ? t("install.acc_plain") : t("install.acc_unknown");

function accessCard(ctx){
  const {t, el} = ctx;
  const wrap = el("div", {style:"margin-bottom:12px"}, el("span", {class:"spin"}));
  access(ctx, false).then(a => {
    wrap.innerHTML = "";
    if (!a || a.error || !a.me){
      if (a && a.error) wrap.append(el("div", {class:"muted"}, a.error));
      return;
    }
    const me = a.me;
    wrap.append(el("div", {class:"muted", style:"font-size:12.5px"},
      t("install.acc_for", {user: me.user || "?", home: me.home, rights: rights(t, me)})));
    // Панель не от того, кто за ней сидит, или с правами администратора: поставленное отсюда ляжет в
    // чужой профиль. Это полоса, а не вопрос: ставить или нет — решает человек.
    if (me.foreign || me.elevated)
      wrap.append(el("div", {class:"stalebar", style:"margin-top:8px"},
        me.foreign ? t("install.acc_warn_other", {user: me.user || "?", session: me.session_user || "?", home: me.home})
                   : t("install.acc_warn_admin", {user: me.user || "?", home: me.home})));
    const env = a.python_env || [];
    if (env.length) wrap.append(el("div", {class:"muted", style:"font-size:12.5px;margin-top:4px"},
      t(env.length > 1 ? "install.acc_pyenv_many" : "install.acc_pyenv_one", {names: env.join(", ")})));
  });
  return wrap;
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#installBody");
  // Постоянные блоки вынимаем до очистки: `innerHTML = ""` у родителя их не губит, но
  // так явно видно, что они переезжают, а не строятся заново.
  const keep = cards(ctx);
  [keep.runtime, keep.access, keep.extras, keep.gitmods, keep.mcp, keep.harness]
    .forEach(n => n.remove());
  box.innerHTML = "";
  const e = ctx.state.env;
  box.append(el("div", {class:"row", style:"margin-bottom:6px;flex-wrap:wrap"},
    el("span", {class:"chip ok"}, "Python " + e.python),
    el("span", {class:"chip"}, "kit " + ctx.state.kit.version),
    el("span", {class:"chip mono"}, ctx.state.kit.path)));
  // Пакеты проверяются и ставятся в Python панели: на Windows `pip3` в терминале часто
  // принадлежит другому Python, и «установленное» панель не видит.
  if (e.python_path) box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:6px"},
    t("install.python_path", {path: e.python_path})));
  // Python — первым: пока он не выбран и не проверен, остальное ставить некуда.
  box.append(keep.runtime);
  box.append(keep.access);

  const card = el("div", {class:"card"});
  card.append(el("div", {class:"row", style:"gap:8px;align-items:center;margin-bottom:6px"},
    el("span", {class:"muted", style:"flex:1;font-size:12.5px"}, t("install.recheck_hint")),
    el("button", {class:"btn sm", title: t("install.recheck_hint"),
      onclick: () => recheck(ctx)}, t("install.recheck"))));
  e.items.forEach(i => {
    card.append(el("div", {class:"list-item"},
      el("span", {class:"chip " + (i.ok ? "ok" : "warn"), style:"flex:none"},
        i.ok ? t("install.have") : i.level === "warn" ? t("install.py_warn") : i.level ? t("install.py_error") : t("install.missing")),
      el("div", {style:"flex:1;min-width:0"},
        el("div", {style:"font-weight:600"}, i.name),
        el("div", {class:"muted", style:"font-size:12.5px;margin-top:2px"},
          t("install.enables", {what: i.enables})),
        i.ok ? null : el("div", {class:"mono",
          style:"font-size:12px;margin-top:6px;color:var(--text-muted);overflow-wrap:anywhere"}, i.install),
        // Без прав администратора: пакеты — в папку пользователя (`--user`), git и pandoc — winget для пользователя.
        !i.ok && i.user ? el("div", {class:"muted", style:"font-size:12px;margin-top:4px"}, t("install.acc_user_site")) : null,
        !i.ok && i.install_user ? el("div", {class:"muted", style:"font-size:12px;margin-top:4px;overflow-wrap:anywhere"},
          t("install.acc_bin_user", {cmd: i.install_user})) : null),
      !i.ok && i.pip ? el("button", {class:"btn sm primary", style:"flex:none",
        title: t("install.do_install_hint"),
        onclick: ev => pipInstall(ctx, i, ev.target)}, t("install.do_install")) : null));
  });
  box.append(card);
  box.append(keep.extras, keep.gitmods, keep.mcp, keep.harness);

  if (!ctx.project) return;
  const p = ctx.project;
  box.append(el("h2", {}, t("install.project_ready", {name: p.name})));
  const pc = el("div", {class:"card"});
  const line = (ok, name, what, cmd) => el("div", {class:"list-item"},
    el("span", {class:"chip " + (ok ? "ok" : "warn"), style:"flex:none"},
      ok ? "✓" : t("install.missing")),
    el("div", {style:"flex:1"}, el("div", {style:"font-weight:600"}, name),
      el("div", {class:"muted", style:"font-size:12.5px"}, what)),
    cmd ? el("button", {class:"btn sm", title: t("install.fix_hint"),
      onclick: () => ctx.openRun(cmd)}, t("install.fix")) : null);
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
    const [d, a] = await Promise.all([ctx.api("/api/extras" + (fresh ? "?fresh=1" : ""), {quiet:true}),
                                      access(ctx, fresh)]);
    body.innerHTML = "";
    (d.extras || []).forEach(x => body.append(extraRow(ctx, x, draw, a)));
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
                      : (r && r.error) || ctx.t("install.gm_failed"), r && r.ok ? "ok" : "err",
            {sticky: !(r && r.ok)});
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
        // Модуль поставлен — блоки раздела спросят сервер заново (один раз), а не покажут прежнее.
        await installGitMod(ctx, m.id, () => { GITMODS = null; CARDS = null; ctx.show("install"); });
      }}, m.installed ? t("install.gm_update", {v: m.kit}) : t("install.gm_install")));
  })();
  return row;
}

function extraRow(ctx, x, redraw, a){
  const {t, el} = ctx;
  const have = x.installed;
  const me = (a && a.me) || {};
  const comp = (a && a.components && a.components[x.id]) || null;
  const ver = have ? t("install.ex_installed", {v: x.installed}) : t("install.ex_missing");
  const git = x.git ? t("install.ex_git", {v: x.git}) : "";
  const pypi = x.pypi && x.pypi !== x.git ? t("install.ex_pypi", {v: x.pypi}) : "";
  const label = !have ? t("install.ex_install")
    : x.update ? t("install.ex_update", {v: x.target || x.pypi}) : t("install.ex_latest");
  const btn = el("button", {class:"btn sm" + (!have || x.update ? " primary" : ""),
    disabled: have && !x.update ? "" : null,
    // Права повышены или панель не от того, кто за ней сидит: подсказка говорит, в чей профиль ляжет установка.
    title: me.elevated || me.foreign ? t("install.acc_where_other", {path: x.venv, user: me.user || "?"})
                                     : t("install.ex_where", {path: x.venv}),
    onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = t("install.ex_busy");
      const r = await ctx.api("/api/extras/install", {method:"POST",
        body: JSON.stringify({id: x.id}), quiet:true});
      ctx.toast(r.ok ? t("install.ex_done", {name: x.title, v: r.version})
                     : (r.error || t("install.ex_failed", {log: (r.log || "").slice(-200)})),
                r.ok ? "ok" : "err", {sticky: !r.ok});
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
      comp ? accessLines(ctx, x, comp, me) : null,
      x.mcp ? mcpHint(ctx, x.mcp) : null,
      x.id === "pydantic-ai" && have ? compatLine(ctx, x, redraw) : null,
      x.id === "pydantic-ai" ? pydanticHint(ctx) : null),
    btn);
  return row;
}

/* Работает ли надстройка ДЛЯ ВАС — настоящим запуском, как в прогоне (версия видна и чужому venv);
   у кого она стоит, если не у вас; как поставить себе без прав администратора. */
const CLI = {"pydantic-ai": "python .aurora/scripts/agent_core.py --venv-install",
             "graphify": "python .aurora/scripts/aurora_extras.py --install graphify"};

function accessLines(ctx, x, comp, me){
  const {t, el} = ctx;
  const run = comp.run || {};
  const box = el("div", {style:"margin-top:4px;font-size:12.5px"});
  if (run.ok) box.append(el("span", {class:"chip ok"}, t("install.acc_run_ok", {user: me.user || "?"})));
  else if (x.installed || run.kind !== "venv")
    box.append(el("div", {class:"chip warn", style:"white-space:normal"},
      t(x.installed ? "install.acc_run_seen" : "install.acc_run_bad", {why: run.why || "?"})));
  if (comp.note) box.append(el("div", {class:"muted", style:"margin-top:4px"}, comp.note));
  (comp.others || []).forEach(o => box.append(el("div", {style:"margin-top:4px"},
    t("install.acc_other", {name: x.title, v: o.version || "?", user: o.user || "?",
      at: (o.at || "").slice(0, 10) || "?", admin: o.elevated ? t("install.acc_other_admin") : "",
      path: o.path || "?", me: me.user || "?"}))));
  box.append(el("div", {class:"muted", style:"margin-top:4px;overflow-wrap:anywhere"},
    t("install.acc_how", {user: me.user || "?", path: x.venv}), " ",
    CLI[x.id] ? el("span", {class:"mono"}, CLI[x.id]) : null));
  return box;
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
                r.ok ? "ok" : "err", {sticky: !r.ok});
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
               : t("install.ex_settings_off", {why: d.fallback_why || (d.venv.ok
                   ? t("install.ex_not_selected") : t("install.ex_not_installed"))})));
    if (d.effective_python)
      box.append(el("div", {class:"muted mono", style:"font-size:12px;margin-bottom:4px;overflow-wrap:anywhere"},
        t("install.ex_settings_python", {path: d.effective_python})));
    if (!d.active && d.fallback_why)
      box.append(el("div", {class:"chip warn", style:"margin-bottom:6px;white-space:normal"},
        t("install.ex_settings_blocked", {why: d.fallback_why})));
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

/* MCP: доступ агента, токены и подтверждения. Режим и токены живут в одной карточке:
   человеку важно видеть, что именно разрешено удалённому агенту. Секрет токена приходит
   один раз и после закрытия окна стирается из разметки; список его не отдаёт. */
const MCP_SERVERS = ["aurora", "aurora-remote"];

function projectSlugs(ctx){
  return [...new Set((ctx.state.projects || [])
    .map(p => String(p.slug || p.name || "").trim())
    .filter(x => x && x !== "*"))];
}

function shortDigest(value){
  const text = String(value || "");
  return text.length > 18 ? text.slice(0, 10) + "…" + text.slice(-6) : text;
}

function showSecretOnce(ctx, title, secret, about){
  const {t, el} = ctx;
  let value = String(secret || "");
  const overlay = el("div", {style:"position:fixed;inset:0;z-index:9000;display:flex;"
    + "align-items:center;justify-content:center;background:rgba(0,0,0,.55);padding:16px"});
  const input = el("input", {class:"btn mono", readonly:"", value,
    style:"width:100%;font-weight:400", spellcheck:"false", autocomplete:"off",
    onfocus: e => e.target.select()});
  const close = () => { input.value = ""; value = ""; overlay.remove(); };
  overlay.append(el("div", {class:"card", style:"max-width:680px;width:100%;padding:22px"},
    el("h2", {style:"margin-top:0"}, title),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, about),
    input,
    el("div", {class:"row", style:"gap:8px;margin-top:12px;flex-wrap:wrap"},
      ctx.ui.copyButton(value),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm primary", onclick: close},
        t("install.token_once_close")))));
  document.body.append(overlay);
  input.focus();
}

function mcpCard(ctx){
  const {t, el} = ctx;
  const wrap = el("div", {style:"margin-top:18px"});
  const draw = async () => {
    wrap.innerHTML = "";
    wrap.append(el("h2", {}, t("install.token_mcp_title")),
      el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"},
        t("install.token_mcp_about")));
    const body = el("div", {class:"card"}, el("span", {class:"spin"}));
    wrap.append(body);
    const [kit, kn, rm, conf] = await Promise.all([
      ctx.api("/api/mcp/kit", {quiet:true}),
      ctx.api("/api/mcp/tokens?kind=knowledge", {quiet:true}),
      ctx.api("/api/mcp/tokens?kind=remote", {quiet:true}),
      ctx.api("/api/mcp/confirmations", {quiet:true}),
    ]);
    body.innerHTML = "";
    body.append(modeBlock(ctx, kit || {}, draw));
    body.append(tokensBlock(ctx, kn || {}, rm || {}, draw));
    body.append(confirmBlock(ctx, conf || {}, draw));
  };
  draw();
  return wrap;
}

function modeBlock(ctx, kit, redraw){
  const {t, el} = ctx;
  const remote = kit.remote || {};
  const mode = remote.mode === "full" ? "full" : "safe";
  const box = el("div", {style:"margin-bottom:16px"});
  box.append(el("h3", {style:"margin:0 0 6px"}, t("install.token_mode_title")));
  box.append(el("div", {class:"row", style:"gap:8px;align-items:center;flex-wrap:wrap"},
    el("span", {class:"chip " + (mode === "full" ? "bad" : "ok")},
      mode === "full" ? t("install.token_mode_full") : t("install.token_mode_safe")),
    el("span", {class:"muted", style:"font-size:12.5px"}, t("install.token_mode_effective")),
    remote.admin_network
      ? el("span", {class:"chip warn"}, t("install.token_mode_admin_on"))
      : el("span", {class:"chip"}, t("install.token_mode_admin_off"))));
  box.append(el("div", {class:"muted", style:"font-size:12.5px;margin-top:4px"},
    t("install.token_mode_stored", {mode: remote.stored || "safe",
      source: t("install.token_mode_source." + (remote.source || "default"))})));
  const projects = remote.projects || {};
  if (Object.keys(projects).length)
    box.append(el("div", {class:"row", style:"gap:6px;flex-wrap:wrap;margin-top:6px"},
      el("span", {class:"muted", style:"font-size:12.5px"}, t("install.token_mode_projects")),
      ...Object.entries(projects).map(([name, value]) =>
        el("span", {class:"chip mono"}, name + " · " + value))));
  if (remote.reason)
    box.append(el("div", {class:"warnbox", style:"font-size:12.5px;margin-top:8px"},
      t("install.token_mode_reason", {why: remote.reason})));
  if (mode === "full")
    box.append(el("div", {class:"warnbox", style:"font-size:12.5px;margin-top:8px"},
      t("install.token_mode_full_warn")));
  const setMode = async (target, btn) => {
    if (target === "full" && !confirm(t("install.token_mode_full_ask"))) return;
    btn.disabled = true;
    const r = await ctx.api("/api/mcp/kit", {method:"POST", quiet:true,
      body: JSON.stringify({action:"set_remote_mode", mode: target, project: ""})});
    btn.disabled = false;
    const ok = !!(r && r.ok);
    ctx.toast(ok ? t(target === "full" ? "install.token_mode_full_done"
                                      : "install.token_mode_safe_done")
                 : (r && r.error) || t("install.token_mode_failed"),
              ok ? "ok" : "err", {sticky: !ok});
    if (ok) redraw();
  };
  box.append(el("div", {class:"row", style:"gap:8px;margin-top:10px;flex-wrap:wrap"},
    el("button", {class:"btn sm danger", onclick: ev => setMode("full", ev.target)},
      t("install.token_mode_full")),
    el("button", {class:"btn sm primary", onclick: ev => setMode("safe", ev.target)},
      t("install.token_mode_safe"))));
  const deny = (remote.deny || []).join(", ");
  if (deny)
    box.append(el("details", {style:"margin-top:8px"},
      el("summary", {style:"font-size:12.5px;cursor:pointer"}, t("install.token_mode_deny")),
      el("div", {class:"muted mono", style:"font-size:12px;margin-top:4px;overflow-wrap:anywhere"},
        deny)));
  return box;
}

function tokensBlock(ctx, knowledge, remote, redraw){
  const {t, el} = ctx;
  const box = el("div", {style:"margin-bottom:16px"});
  box.append(el("h3", {style:"margin:0 0 4px"}, t("install.token_knowledge_title")),
    el("p", {class:"muted", style:"font-size:12.5px;margin:0 0 8px"},
      t("install.token_knowledge_about")));
  box.append(tokenCreateForm(ctx, "knowledge", redraw));
  box.append(tokenRows(ctx, knowledge.tokens || [], redraw, knowledge.error));
  box.append(el("h3", {style:"margin:18px 0 4px"}, t("install.token_remote_title")),
    el("p", {class:"muted", style:"font-size:12.5px;margin:0 0 8px"},
      t("install.token_remote_about")));
  box.append(tokenCreateForm(ctx, "remote", redraw));
  box.append(tokenRows(ctx, remote.tokens || [], redraw, remote.error));
  return box;
}

function tokenCreateForm(ctx, kind, redraw){
  const {t, el} = ctx;
  const slugs = projectSlugs(ctx);
  const name = el("input", {class:"btn", style:"width:100%;font-weight:400",
    placeholder: t("install.token_name_ph"), autocomplete:"off"});
  const user = el("input", {class:"btn", style:"width:100%;font-weight:400",
    placeholder: t("install.token_user_ph"), autocomplete:"off"});
  const days = el("input", {class:"btn", type:"number", min:"1", max:"365", value:"90",
    style:"width:110px;font-weight:400"});
  const picked = new Set(slugs.length === 1 ? slugs : []);
  const boxes = slugs.map(s => el("label", {class:"row", style:"gap:6px;font-size:12.5px"},
    el("input", {type:"checkbox", checked: picked.has(s) ? "" : null,
      onchange: e => { if (e.target.checked) picked.add(s); else picked.delete(s); }}),
    el("span", {class:"mono"}, s)));
  const scopes = new Set(kind === "knowledge" ? ["knowledge:read"] : ["remote:read"]);
  const scopeBoxes = kind === "remote" ? [
    ["remote:read", t("install.token_scope_read")],
    ["remote:write", t("install.token_scope_write")],
    ["admin", t("install.token_scope_admin")],
  ].map(([value, label]) => el("label", {
      class:"row", style:"gap:6px;font-size:12.5px;"
        + (value === "admin" ? "color:var(--danger)" : "")},
    el("input", {type:"checkbox", checked: scopes.has(value) ? "" : null,
      onchange: e => { if (e.target.checked) scopes.add(value); else scopes.delete(value); }}),
    el("span", {class: value === "admin" ? "mono" : ""}, label))) : [];
  const form = el("div", {style:"margin:6px 0 12px"});
  form.append(
    el("div", {class:"row", style:"gap:8px;flex-wrap:wrap;margin-bottom:6px"},
      el("div", {style:"flex:1;min-width:180px"}, name),
      el("div", {style:"flex:1;min-width:160px"}, user)),
    el("div", {class:"muted", style:"font-size:12px;margin:6px 0 4px"},
      t("install.token_projects")),
    slugs.length
      ? el("div", {class:"row", style:"gap:10px;flex-wrap:wrap"}, ...boxes)
      : el("div", {class:"warnbox", style:"font-size:12.5px"}, t("install.token_no_projects")),
    el("div", {class:"muted", style:"font-size:12px;margin:6px 0 0"},
      t("install.token_project_star")),
    kind === "remote"
      ? el("div", {class:"muted", style:"font-size:12px;margin:8px 0 2px"},
          t("install.token_scopes"))
      : null,
    kind === "remote" ? el("div", {class:"row", style:"gap:12px;flex-wrap:wrap"}, ...scopeBoxes) : null,
    el("div", {class:"row", style:"gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap"},
      el("span", {class:"muted", style:"font-size:12.5px"}, t("install.token_expiry")),
      days,
      el("button", {class:"btn sm primary", onclick: async ev => {
        ev.target.disabled = true;
        const r = await ctx.api("/api/mcp/tokens/create", {method:"POST", quiet:true,
          body: JSON.stringify({kind, name: name.value.trim(), user: user.value.trim(),
            scopes: [...scopes], projects: [...picked],
            expires_days: Number(days.value) || 90})});
        ev.target.disabled = false;
        if (r && r.token){
          ctx.toast(t("install.token_created", {name: name.value.trim() || "?"}), "ok");
          showSecretOnce(ctx, t("install.token_once_title"), r.token,
            t("install.token_once_about"));
          redraw();
        } else {
          ctx.toast((r && r.error) || t("install.token_failed"), "err", {sticky: true});
        }
      }}, t("install.token_create"))));
  return form;
}

function tokenRows(ctx, rows, redraw, error){
  const {t, el} = ctx;
  if (error) return el("div", {class:"warnbox", style:"font-size:12.5px"}, error);
  if (!rows.length) return el("div", {class:"muted", style:"font-size:12.5px;margin:4px 0 8px"},
    t("install.token_empty"));
  return el("div", {}, ...rows.map(row => tokenRow(ctx, row, redraw)));
}

function tokenRow(ctx, row, redraw){
  const {t, el} = ctx;
  const status = row.status === "active" ? "ok" : row.status === "revoked" ? "bad" : "warn";
  const act = async (what, btn) => {
    const ask = what === "rotate" ? t("install.token_rotate_ask", {name: row.name})
                                  : t("install.token_revoke_ask", {name: row.name});
    if (!confirm(ask)) return;
    btn.disabled = true;
    const r = await ctx.api("/api/mcp/tokens/" + what, {method:"POST", quiet:true,
      body: JSON.stringify({id: row.id})});
    btn.disabled = false;
    if (r && r.error){
      ctx.toast(r.error, "err", {sticky: true});
      return;
    }
    if (what === "rotate" && r.token){
      ctx.toast(t("install.token_rotated", {name: row.name}), "ok");
      showSecretOnce(ctx, t("install.token_once_title"), r.token,
        t("install.token_once_about"));
    } else {
      ctx.toast(t("install.token_revoked", {name: row.name}), "ok");
    }
    redraw();
  };
  return el("div", {class:"list-item", style:"align-items:flex-start"},
    el("span", {class:"chip " + status, style:"flex:none"},
      t("install.token_status." + (row.status || "active"))),
    el("div", {style:"flex:1;min-width:0"},
      el("div", {style:"font-weight:600"}, row.name || row.id,
        el("span", {class:"muted mono", style:"font-weight:400;margin-left:8px;font-size:12px"},
          row.prefix || "")),
      el("div", {class:"row", style:"gap:6px;flex-wrap:wrap;margin-top:4px"},
        ...(row.scopes || []).map(s => el("span", {class:"chip mono"}, s)),
        ...(row.projects || []).map(p => el("span", {class:"chip mono"}, p))),
      el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.token_dates", {created: (row.created_at || "").slice(0, 10) || "?",
          expires: (row.expires_at || "").slice(0, 10) || "?",
          user: row.user || "?"})),
      el("div", {class:"muted", style:"font-size:12px;margin-top:2px"},
        t("install.token_last_used", {at: row.last_used_at || t("install.token_never"),
          ip: row.last_used_ip || t("install.token_no_ip")}))),
    el("div", {class:"row", style:"gap:6px;flex:none;flex-wrap:wrap;justify-content:flex-end"},
      el("button", {class:"btn sm", onclick: () =>
        ctx.toast(t("install.token_show_gone"), "warn")}, t("install.token_show")),
      row.status === "active" ? el("button", {class:"btn sm",
        onclick: ev => act("rotate", ev.target)}, t("install.token_rotate")) : null,
      row.status !== "revoked" ? el("button", {class:"btn sm danger",
        onclick: ev => act("revoke", ev.target)}, t("install.token_revoke")) : null));
}

function confirmBlock(ctx, conf, redraw){
  const {t, el} = ctx;
  const box = el("div", {style:"margin-top:8px"});
  box.append(el("h3", {style:"margin:0 0 4px"}, t("install.confirm_title")),
    el("p", {class:"muted", style:"font-size:12.5px;margin:0 0 8px"},
      t("install.confirm_about")));
  const rows = conf.pending || [];
  if (!rows.length){
    box.append(el("div", {class:"muted", style:"font-size:12.5px"}, t("install.confirm_empty")));
    return box;
  }
  rows.forEach(row => {
    const act = async (what, btn) => {
      btn.disabled = true;
      const path = what === "approve" ? "/api/mcp/confirm/approve" : "/api/mcp/confirm/reject";
      const r = await ctx.api(path, {method:"POST", quiet:true,
        body: JSON.stringify({request_id: row.request_id})});
      btn.disabled = false;
      if (!r || r.error){
        ctx.toast((r && r.error) || t("install.confirm_failed"), "err", {sticky: true});
        return;
      }
      if (what === "approve" && r.confirm_id){
        ctx.toast(t("install.confirm_approved"), "ok");
        showSecretOnce(ctx, t("install.confirm_once_title"), r.confirm_id,
          t("install.confirm_once_about"));
      } else {
        ctx.toast(t("install.confirm_rejected"), "ok");
      }
      redraw();
    };
    box.append(el("div", {class:"list-item", style:"align-items:flex-start"},
      el("span", {class:"chip warn", style:"flex:none"}, t("install.confirm_pending")),
      el("div", {style:"flex:1;min-width:0"},
        el("div", {style:"font-weight:600"}, row.summary || row.tool),
        el("div", {class:"row", style:"gap:6px;flex-wrap:wrap;margin-top:4px"},
          el("span", {class:"chip mono"}, row.tool),
          row.project ? el("span", {class:"chip mono"}, row.project) : null,
          el("span", {class:"chip mono", title: row.args_digest},
            t("install.confirm_digest", {digest: shortDigest(row.args_digest)}))),
        el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
          t("install.confirm_who", {user: row.user || "?",
            token: row.token_id || "?", expires: row.expires_at || "?"}))),
      el("div", {class:"row", style:"gap:6px;flex:none;flex-wrap:wrap;justify-content:flex-end"},
        el("button", {class:"btn sm primary",
          onclick: ev => act("approve", ev.target)}, t("install.confirm_approve")),
        el("button", {class:"btn sm danger",
          onclick: ev => act("reject", ev.target)}, t("install.confirm_reject")))));
  });
  return box;
}

/* Aurora MCP в ассистентах машины. Каталог ассистентов — `scripts/harnesses.json`, он едет
   с китом. Серверов два: знания (`aurora`) и управление (`aurora-remote`). «Найти»
   спрашивает версии у самих команд; «Подключить» делает копию файла ассистента, вставляет
   запись и проверяет файл; «Вернуть» кладёт копию выбранного сервера обратно. */
let HARNESS = null;

function groupHarness(rows){
  const groups = {};
  (rows || []).forEach(row => { (groups[row.id] ||= []).push(row); });
  return Object.values(groups);
}

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
    const groups = groupHarness(HARNESS.harnesses);
    const found = groups.filter(g => g[0].found);
    body.append(el("div", {class:"row", style:"gap:8px;align-items:center;margin-bottom:6px"},
      el("span", {class:"muted", style:"flex:1;font-size:12.5px"},
        t("install.hs_found", {n: found.length, all: groups.length})),
      el("button", {class:"btn sm", onclick: () => draw(true)}, t("install.hs_find"))));
    found.forEach(g => body.append(harnessRow(ctx, g, () => draw(versions))));
    if (!found.length) body.append(el("div", {class:"muted"}, t("install.hs_none")));
    const servers = HARNESS.servers || {};
    body.append(el("div", {class:"muted mono", style:"font-size:12px;margin-top:10px;overflow-wrap:anywhere"},
      t("install.hs_server", {cmd: Object.entries(servers).map(([name, value]) =>
        name + ": " + [value.command, ...(value.args || [])].join(" ")).join("\n")})));
  };
  draw(false);
  return wrap;
}

function harnessRow(ctx, group, redraw){
  const {t, el} = ctx;
  const h = group[0];
  const byServer = Object.fromEntries(group.map(row => [row.server, row]));
  const act = async (what, server, btn) => {
    if (what === "restore" &&
        !confirm(t("install.hs_restore_ask", {name: h.name + " · " + server}))) return;
    btn.disabled = true;
    const r = await ctx.api(what === "add" ? "/api/harness/add" : "/api/harness/restore",
      {method:"POST", quiet:true, body: JSON.stringify({id: h.id, server, mode: "local"})});
    btn.disabled = false;
    if (!r || !r.ok){
      ctx.toast((r && r.error) || t("install.hs_failed"), "err", {sticky: true});
      return;
    }
    if (r.legacy_shared || r.warning){
      ctx.toast(r.warning || t("install.hs_restore_legacy"), "warn", {sticky: true});
    } else if (r.preserved && r.preserved.length){
      ctx.toast(t("install.hs_restored_preserved", {name: h.name,
        server: r.preserved.join(", ")}), "ok");
    } else {
      ctx.toast(t(what === "add" ? "install.hs_added" : "install.hs_restored",
        {name: h.name + " · " + server}), "ok");
    }
    redraw();
  };
  const noteKey = "install.hs_note." + h.id;
  const note = t(noteKey);
  const blocks = MCP_SERVERS.map(server => {
    const row = byServer[server] || {};
    const present = row.present;
    const chip = present === true
      ? el("span", {class:"chip ok", style:"flex:none"}, t("install.hs_on"))
      : present === false
        ? el("span", {class:"chip warn", style:"flex:none"}, t("install.hs_off"))
        : el("span", {class:"chip bad", style:"flex:none"}, t("install.hs_broken"));
    const snip = el("pre", {class:"mono", hidden:"",
      style:"font-size:12px;white-space:pre-wrap;margin:6px 0 0"},
      row.snippet || "");
    return el("div", {style:"margin-top:10px"},
      el("div", {class:"row", style:"gap:8px;align-items:center;flex-wrap:wrap"},
        chip,
        el("b", {style:"font-size:13px"},
          server === "aurora" ? t("install.hs_knowledge") : t("install.hs_manage")),
        el("span", {class:"muted mono", style:"font-size:11.5px;overflow-wrap:anywhere"},
          server + " · " + (row.config || "") + (row.config_exists ? "" : " · " + t("install.hs_new_file"))),
        el("div", {class:"spacer"}),
        el("button", {class:"btn sm", onclick: () => { snip.hidden = !snip.hidden; }},
          t("install.hs_show")),
        present === false ? el("button", {class:"btn sm primary",
          onclick: ev => act("add", server, ev.target)}, t("install.hs_add")) : null,
        row.backups ? el("span", {class:"chip"}, t("install.hs_backups", {n: row.backups})) : null),
      snip);
  });
  const restore = el("select", {class:"btn", style:"font-weight:400"},
    ...MCP_SERVERS.map(server => el("option", {value: server},
      (server === "aurora" ? t("install.hs_knowledge") : t("install.hs_manage"))
      + " · " + t("install.hs_backups", {n: (byServer[server] || {}).backups || 0}))));
  const restoreBtn = el("button", {class:"btn sm",
    onclick: ev => act("restore", restore.value, ev.target)}, t("install.hs_restore",
      {n: (byServer[MCP_SERVERS[0]] || {}).backups || 0}));
  restore.onchange = () => {
    restoreBtn.textContent = t("install.hs_restore",
      {n: (byServer[restore.value] || {}).backups || 0});
  };
  const missing = MCP_SERVERS.filter(server => (byServer[server] || {}).present !== true);
  return el("div", {class:"list-item", style:"align-items:flex-start"},
    el("span", {class:"chip " + (missing.length ? "warn" : "ok"), style:"flex:none"},
      missing.length ? t("install.hs_partial", {n: missing.length})
                     : t("install.hs_on")),
    el("div", {style:"flex:1;min-width:0"},
      el("div", {style:"font-weight:600"}, h.name, h.version ? el("span", {class:"muted",
        style:"font-weight:400;margin-left:8px;font-size:12.5px"}, h.version) : null),
      note !== noteKey ? el("div", {class:"muted",
        style:"font-size:12.5px;margin-top:4px"}, note) : null,
      ...blocks,
      el("div", {class:"row", style:"gap:8px;margin-top:10px;flex-wrap:wrap"},
        restore,
        restoreBtn,
        missing.length ? el("button", {class:"btn sm primary", onclick: async ev => {
          ev.target.disabled = true;
          for (const server of missing){
            const r = await ctx.api("/api/harness/add", {method:"POST", quiet:true,
              body: JSON.stringify({id: h.id, server, mode: "local"})});
            if (!r || !r.ok){
              ctx.toast((r && r.error) || t("install.hs_failed"), "err", {sticky: true});
              break;
            }
          }
          ev.target.disabled = false;
          redraw();
        }}, t("install.hs_both")) : null)));
}

// Жизнь раздела (cockpit/modules/README.md): скрытая «Установка» не перерисовывается на каждое
// событие — три карточки с запросами к серверу строятся при открытии; часть здоровья на виду обновляет её.
export const lifecycle = 1;
export const refreshOn = ["health"];
export default {mount, refresh};
