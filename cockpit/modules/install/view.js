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
    const d = await ctx.api("/api/runtime" + (fresh ? "?fresh=1" : ""), {quiet:true});
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
    line(t("install.rt_effective"), d.effective_python, true);
    line(t("install.rt_base"), rt.base_python, true);
    line(t("install.rt_source"), src);
    line(t("install.rt_mode"), mode);
    line(t("install.rt_pydantic"),
      pyd && pyd.ok ? (pyd.version || "?") : t("install.rt_pydantic_missing"));
    if (rt.externally_managed && rt.source !== "venv")
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_managed")));
    if (rt.source === "venv")
      body.append(el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
        t("install.rt_direct_venv")));
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
          [c.source, c.version].filter(Boolean).join(" · ") + " — " + (c.executable || "?")
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
             gitmods: gitModsCard(ctx), harness: harnessCard(ctx)};
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
  [keep.runtime, keep.access, keep.extras, keep.gitmods, keep.harness].forEach(n => n.remove());
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
  box.append(keep.extras, keep.gitmods, keep.harness);

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
                        : (r && r.error) || t("install.hs_failed"), r && r.ok ? "ok" : "err",
              {sticky: !(r && r.ok)});
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

// Жизнь раздела (cockpit/modules/README.md): скрытая «Установка» не перерисовывается на каждое
// событие — три карточки с запросами к серверу строятся при открытии; часть здоровья на виду обновляет её.
export const lifecycle = 1;
export const refreshOn = ["health"];
export default {mount, refresh};
