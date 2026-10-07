/* Модели — раздел-модуль: провайдеры, возможности, роли и цепочки запасных.

   Настройка одна на кит и все его проекты (`local/models.json`, cockpit → model_config.py).
   Страница правит копию настройки и отправляет её целиком; ключи приходят маской, и маска
   в ответе значит «ключ не трогали». Порядок работы — по мере нужды: провайдер, роль,
   провайдер и модель, «+» — запасной; провайдер можно завести прямо из строки бэкенда. */

const CAPS = ["llm", "ocr", "embeddings"];
// Имена ролей движка по умолчанию — так их пишет model_config.py. Не переименованную роль
// показываем на языке интерфейса; своё имя человека — как есть.
const ENGINE_NAMES = {worker: "Писатель: разбор, тезисы, ответы",              // данные движка
                      planner: "Планировщик: планы, вынос, встречи",           // данные движка
                      critic: "Критик: проверка решений",                      // данные движка
                      qa: "Момус: проверка опоры и ревью",                     // данные движка
                      document: "Сканы документов", index: "Индекс базы знаний"}; // данные движка
// Роль, на которую уходят задачи пустых ролей движка (model_config.DEFAULT_ROLE).
const DEFAULT_ROLE = {llm: "worker", ocr: "document", embeddings: "index"};
const MASK = "••••••";
let DATA = null, M = null, TAB = "providers", DIRTY = false, DRAG = null, PING = null;
const LISTS = {};                    // id провайдера → имена моделей, спрошенные у него

export function mount(ctx){
  ctx.root.dataset.module = "models";
}

export async function refresh(ctx){
  const d = await ctx.api("/api/models", {quiet: true});
  if (!d || !d.models) return ctx.toast((d && d.error) || ctx.t("models.load_failed"), "err");
  DATA = d;
  M = JSON.parse(JSON.stringify(d.models));
  DIRTY = false;
  draw(ctx);
}

function draw(ctx){
  drawNotes(ctx);
  drawTabs(ctx);
  drawBody(ctx);
  drawBar(ctx);
}

function touch(ctx, redraw){
  DIRTY = true;
  if (redraw) drawBody(ctx);
  drawBar(ctx);
  drawTabs(ctx);
}

const newId = (base) => {
  const taken = new Set(M.providers.map(p => p.id));
  const b = (base || "provider").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
            || "provider";
  let id = b, i = 2;
  while (taken.has(id)) id = `${b}-${i++}`;
  return id;
};
const usedBy = id => CAPS.reduce((n, c) => n + M.capabilities[c].roles
  .filter(r => r.backends.some(b => b.provider === id)).length, 0);

/* ---------------------------------------------------------------- шапка */

function drawNotes(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#modelsNotes");
  box.innerHTML = "";
  if (DATA.error) box.append(el("div", {class: "warnbox"}, DATA.error));
  box.append(el("div", {class: "muted", style: "font-size:12.5px;margin-bottom:10px"},
    t("models.scope", {n: (DATA.projects || []).length}),
    el("span", {class: "mono", style: "margin-left:6px"}, DATA.path || "")));
  (DATA.leftovers || []).forEach(p => box.append(el("div", {class: "warnbox",
      style: "margin-bottom:8px"},
    t("models.leftover", {name: p.name, keys: p.keys.slice(0, 5).join(", ")
                                             + (p.keys.length > 5 ? " …" : "")}))));
}

function drawTabs(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#modelsTabs");
  box.innerHTML = "";
  const count = c => c === "providers" ? M.providers.length
    : M.capabilities[c].roles.filter(r => r.backends.length).length;
  ["providers", ...CAPS].forEach(c => box.append(el("button", {
      class: "btn sm" + (TAB === c ? " primary" : ""), role: "tab",
      "aria-selected": String(TAB === c),
      onclick: () => { TAB = c; drawTabs(ctx); drawBody(ctx); }},
    t("models.tab." + c), el("span", {class: "muted", style: "margin-left:6px"},
                              String(count(c))))));
}

function drawBar(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#modelsBar");
  box.innerHTML = "";
  // Родной append пишет null как текст «null»; пропуски убирает только ctx.el.
  box.append(...[
    el("button", {class: "btn primary", disabled: DIRTY ? null : "", onclick: () => save(ctx)},
      t("models.save")),
    el("button", {class: "btn", disabled: DIRTY ? null : "",
      onclick: () => { M = JSON.parse(JSON.stringify(DATA.models)); DIRTY = false; draw(ctx); }},
      t("models.revert")),
    DIRTY ? el("span", {class: "chip warn"}, t("models.unsaved")) : null,
    el("span", {style: "flex:1"}),
    el("button", {class: "btn sm", onclick: () => ping(ctx)}, t("models.ping"))].filter(Boolean));
  if (PING) box.append(PING);
}

async function save(ctx){
  const {t} = ctx;
  // Бэкенд без провайдера или модели сервер отбросит — скажем это заранее, а не после.
  const holes = [];
  CAPS.forEach(c => M.capabilities[c].roles.forEach(r => r.backends.forEach((b, i) => {
    if (!b.provider || !String(b.model || "").trim())
      holes.push(`${t("models.tab." + c)} · ${r.name} · ${i + 1}`);
  })));
  if (holes.length && !confirm(t("models.holes_ask", {list: holes.join("\n")}))) return;
  const r = await ctx.api("/api/models/save", {method: "POST", quiet: true,
    body: JSON.stringify({models: M})});
  if (!r || !r.ok) return ctx.toast((r && r.error) || t("models.save_failed"), "err");
  ctx.toast(t("models.saved"), "ok");
  (r.notes || []).forEach(n => ctx.toast(n, "warn"));
  await refresh(ctx);
}

async function ping(ctx){
  const {t, el} = ctx;
  if (DIRTY) return ctx.toast(t("models.ping_save_first"), "warn");
  PING = el("div", {style: "flex-basis:100%"}, el("span", {class: "spin"}));
  drawBar(ctx);
  const r = await ctx.api("/api/agent/ping", {method: "POST", quiet: true,
    body: JSON.stringify({project: ""})});
  PING = el("div", {style: "flex-basis:100%"},
    ...((r && r.backends) || []).map(b => el("div", {class: "mono", style: "font-size:12.5px;margin-top:4px"},
      (b.ok ? "✅ " : "✗ ") + (b.name || "№" + b.n) + " · " + (b.model || "—") + " · "
      + (b.ok ? t("models.ping_ok", {sec: b.seconds}) : b.status))),
    !r || r.error ? el("div", {class: "warnbox"}, (r && r.error) || t("models.ping_failed")) : null);
  drawBar(ctx);
}

/* ---------------------------------------------------------------- тело */

function drawBody(ctx){
  const box = ctx.$("#modelsBody");
  box.innerHTML = "";
  if (TAB === "providers") return box.append(providersTab(ctx));
  box.append(capabilityTab(ctx, TAB));
}

// Имя роли так, как его видит человек: своё — как есть, имя движка — на языке интерфейса.
const roleName = (ctx, r) => r.builtin && r.name === ENGINE_NAMES[r.id]
  ? ctx.t("models.role_default." + r.id) : r.name;

const field = (ctx, label, input) => ctx.el("label", {style: "display:block;flex:1;min-width:160px"},
  ctx.el("div", {class: "muted", style: "font-size:11.5px;margin-bottom:3px"}, label), input);

function providerForm(ctx, p, onDone){
  // Один и тот же вид у провайдера во вкладке и у провайдера, заведённого из строки бэкенда.
  const {t, el} = ctx;
  const inp = (k, attrs) => {
    const x = el("input", {class: "btn", style: "width:100%;font-weight:400", ...attrs});
    x.value = p[k] == null ? "" : String(p[k]);
    x.oninput = () => { p[k] = attrs && attrs.type === "number" ? Number(x.value || 0) : x.value;
                        touch(ctx); };
    return x;
  };
  const type = el("select", {class: "btn"}, ...(DATA.types || ["openai"]).map(v =>
    el("option", {value: v, selected: p.type === v ? "" : null}, t("models.type." + v))));
  type.onchange = () => { p.type = type.value; touch(ctx); };
  const key = el("input", {class: "btn mono", type: "password", style: "width:100%;font-weight:400",
    autocomplete: "off",
    placeholder: p.key === MASK ? t("models.key_set") : t("models.key_none")});
  const hadKey = p.key === MASK;
  key.oninput = () => { p.key = key.value || (hadKey ? MASK : ""); touch(ctx); };
  const tpl = el("textarea", {class: "btn mono", rows: 2,
    style: "width:100%;font-weight:400;font-size:12px",
    placeholder: '{"reasoning_effort": "xhigh"}'});
  tpl.value = p.template && Object.keys(p.template).length ? JSON.stringify(p.template) : "";
  const tplBad = el("span", {class: "chip warn", hidden: ""}, t("models.template_bad"));
  tpl.oninput = () => {
    try { p.template = tpl.value.trim() ? JSON.parse(tpl.value) : {}; tplBad.hidden = true; }
    catch (e){ tplBad.hidden = false; return; }
    touch(ctx);
  };
  const parallel = el("input", {type: "checkbox", checked: p.parallel !== false ? "" : null});
  parallel.onchange = () => { p.parallel = parallel.checked; touch(ctx); };
  return el("div", {},
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap"},
      field(ctx, t("models.p_name"), inp("name", {placeholder: t("models.p_name_ph")})),
      field(ctx, t("models.p_type"), type)),
    el("div", {class: "row", style: "gap:10px;flex-wrap:wrap;margin-top:8px"},
      field(ctx, t("models.p_url"), inp("url", {class: "btn mono",
        placeholder: "https://llm.example.com/v1"})),
      field(ctx, t("models.p_key"), key),
      hadKey ? el("button", {class: "btn sm", style: "align-self:flex-end",
        onclick: () => { p.key = ""; touch(ctx, true); }}, t("models.key_drop")) : null),
    onDone ? null : el("div", {class: "row", style: "gap:12px;flex-wrap:wrap;margin-top:8px;align-items:flex-end"},
      el("label", {style: "display:block"},
        el("div", {class: "muted", style: "font-size:11.5px", title: t("models.p_width_hint")},
          t("models.p_width")),
        inp("width", {type: "number", min: 0, style: "width:90px;font-weight:400"})),
      el("label", {class: "row", style: "gap:6px", title: t("models.p_parallel_hint")},
        parallel, t("models.p_parallel")),
      el("details", {style: "flex:1;min-width:220px"},
        el("summary", {class: "muted", style: "cursor:pointer"}, t("models.p_template")),
        tpl, tplBad)),
    onDone ? el("div", {class: "row", style: "gap:8px;margin-top:10px"},
      el("button", {class: "btn sm primary", onclick: () => onDone(true)}, t("models.p_add")),
      el("button", {class: "btn sm", onclick: () => onDone(false)}, t("models.cancel"))) : null);
}

function providersTab(ctx){
  const {t, el} = ctx;
  const box = el("div", {});
  box.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 12px"},
    t("models.providers_about")));
  if (!M.providers.length)
    box.append(el("div", {class: "card", style: "padding:18px;margin-bottom:12px"},
      t("models.providers_empty")));
  M.providers.forEach((p, i) => {
    const list = LISTS[p.id];
    box.append(el("div", {class: "card", style: "padding:16px;margin-bottom:12px"},
      el("div", {class: "row", style: "gap:8px;margin-bottom:10px"},
        el("b", {}, p.name || t("models.p_unnamed")),
        el("span", {class: "chip mono"}, p.id),
        el("span", {class: "chip"}, t("models.used_in", {n: usedBy(p.id)})),
        list ? el("span", {class: "chip ok"}, t("models.list_n", {n: list.length})) : null,
        el("span", {style: "flex:1"}),
        el("button", {class: "btn sm", onclick: () => fetchList(ctx, p, true)},
          t("models.list_fetch")),
        el("button", {class: "btn sm danger", onclick: () => {
          const n = usedBy(p.id);
          if (!confirm(n ? t("models.p_drop_used", {name: p.name || p.id, n})
                         : t("models.p_drop_ask", {name: p.name || p.id}))) return;
          M.providers.splice(i, 1);
          CAPS.forEach(c => M.capabilities[c].roles.forEach(r => {
            r.backends = r.backends.filter(b => b.provider !== p.id); }));
          touch(ctx, true);
        }}, t("models.drop"))),
      providerForm(ctx, p)));
  });
  box.append(el("button", {class: "btn", onclick: () => {
    M.providers.push({id: newId("provider"), name: "", type: "openai", url: "", key: "",
                      width: 0, parallel: true, template: {}});
    touch(ctx, true);
  }}, t("models.add_provider")));
  return box;
}

async function fetchList(ctx, p, loud){
  const {t} = ctx;
  if (!p || !String(p.url || "").startsWith("http")) {
    if (loud) ctx.toast(t("models.list_no_url"), "warn");
    return [];
  }
  const r = await ctx.api("/api/models/list", {method: "POST", quiet: true,
    body: JSON.stringify({id: p.id, url: p.url, key: p.key})});
  if (r && r.models){
    LISTS[p.id] = r.models;
    if (loud){ ctx.toast(t("models.list_got", {n: r.models.length, name: p.name || p.id}), "ok");
               drawBody(ctx); }
    return r.models;
  }
  if (loud) ctx.toast((r && r.error) || t("models.list_failed"), "warn");
  return [];
}

/* ------------------------------------------------------------ возможность */

function capabilityTab(ctx, cap){
  const {t, el} = ctx;
  const box = el("div", {});
  box.append(el("p", {class: "muted", style: "font-size:13px;margin:0 0 12px"},
    t("models.cap_about." + cap)));
  if (cap === "ocr"){
    const num = (k, w) => {
      const x = el("input", {class: "btn", type: "number", style: `width:${w}px;font-weight:400`});
      x.value = M.capabilities.ocr[k];
      x.oninput = () => { M.capabilities.ocr[k] = Number(x.value || 0); touch(ctx); };
      return x;
    };
    box.append(el("div", {class: "row", style: "gap:14px;margin-bottom:12px"},
      el("label", {class: "row", style: "gap:6px"}, t("models.ocr_dpi"), num("dpi", 80)),
      el("label", {class: "row", style: "gap:6px"}, t("models.ocr_pages"), num("max_pages", 80))));
  }
  M.capabilities[cap].roles.forEach((r, ri) => box.append(roleCard(ctx, cap, r, ri)));
  // Своя роль — по мере нужды: имя, и она ждёт провайдера с моделью.
  const name = el("input", {class: "btn", style: "width:260px;font-weight:400",
    placeholder: t("models.role_name_ph")});
  box.append(el("div", {class: "row", style: "gap:8px;margin-top:4px"}, name,
    el("button", {class: "btn", onclick: () => {
      const nm = name.value.trim();
      if (!nm) return ctx.toast(t("models.role_need_name"), "warn");
      const taken = new Set(M.capabilities[cap].roles.map(x => x.id));
      let id = nm.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "role";
      for (let i = 2; taken.has(id); i++) id = id.replace(/-\d+$/, "") + "-" + i;
      M.capabilities[cap].roles.push({id, name: nm, builtin: false, backends: [],
                                      ...(cap === "llm" ? {thinking: true} : {})});
      touch(ctx, true);
    }}, t("models.add_role"))));
  if (cap === "llm") box.append(runSettings(ctx));
  return box;
}

function roleCard(ctx, cap, r, ri){
  const {t, el} = ctx;
  const name = el("input", {class: "btn", style: "flex:1;min-width:180px;font-weight:600"});
  name.value = roleName(ctx, r);
  name.oninput = () => { r.name = name.value; touch(ctx); };
  const think = cap === "llm" ? el("label", {class: "row", style: "gap:6px;font-size:12.5px",
      title: t("models.thinking_hint")},
    el("input", {type: "checkbox", checked: r.thinking !== false ? "" : null,
      onchange: e => { r.thinking = e.target.checked; touch(ctx); }}),
    t("models.thinking")) : null;
  const list = el("div", {});
  r.backends.forEach((b, bi) => list.append(backendRow(ctx, cap, r, b, bi)));
  if (!r.backends.length){
    // Пустая роль движка работает на роли по умолчанию; пустая роль по умолчанию — значит,
    // возможность выключена, и это надо сказать прямо, а не отсылать роль к самой себе.
    const dflt = M.capabilities[cap].roles.find(x => x.id === DEFAULT_ROLE[cap]);
    list.append(el("div", {class: "muted", style: "font-size:12.5px;padding:6px 0"},
      r.id === DEFAULT_ROLE[cap] ? t("models.role_empty_default." + cap)
      : r.builtin ? t("models.role_empty_engine", {name: dflt ? roleName(ctx, dflt) : DEFAULT_ROLE[cap]})
      : t("models.role_empty")));
  }
  return el("div", {class: "card", style: "padding:16px;margin-bottom:12px"},
    el("div", {class: "row", style: "gap:8px;margin-bottom:8px"},
      name,
      el("span", {class: "chip mono", title: t("models.role_id_hint")}, r.id),
      el("span", {class: "chip" + (r.builtin ? " gold" : "")},
        r.builtin ? t("models.role_engine") : t("models.role_own")),
      think,
      r.builtin ? null : el("button", {class: "btn sm danger", onclick: () => {
        if (!confirm(t("models.role_drop_ask", {name: r.name}))) return;
        M.capabilities[cap].roles.splice(ri, 1);
        touch(ctx, true);
      }}, t("models.drop"))),
    // Что роль делает — у ролей движка строкой под именем: имя короткое, а модель для
    // роли выбирают по задачам, которые на ней идут.
    r.builtin && cap === "llm" ? el("div", {class: "muted", style: "font-size:12.5px;margin:-2px 0 8px"},
      t("models.role_about." + r.id)) : null,
    list,
    el("button", {class: "btn sm", style: "margin-top:8px", onclick: () => {
      // Запасной по умолчанию — следующий провайдер после последнего в цепочке: так «+» почти
      // всегда даёт то, что и хотели, — второй шлюз, а не повтор первого.
      const last = r.backends.length ? r.backends[r.backends.length - 1].provider : "";
      const ids = M.providers.map(p => p.id);
      const next = ids[(ids.indexOf(last) + 1) % Math.max(1, ids.length)] || "";
      r.backends.push({provider: next, model: "", enabled: true, context: 0, url: "",
                       _new: !ids.length});
      touch(ctx, true);
    }}, r.backends.length ? t("models.add_fallback") : t("models.add_backend")));
}

function backendRow(ctx, cap, r, b, bi){
  const {t, el} = ctx;
  const first = r.backends[0];
  const prov = el("select", {class: "btn", style: "min-width:150px"},
    el("option", {value: "", selected: b.provider ? null : ""}, t("models.pick_provider")),
    ...M.providers.map(p => el("option", {value: p.id, selected: p.id === b.provider ? "" : null},
                                 p.name || p.id)),
    el("option", {value: "__new__"}, t("models.new_provider")));
  prov.onchange = async () => {
    if (prov.value === "__new__"){ b._new = true; return touch(ctx, true); }
    b.provider = prov.value;
    touch(ctx, true);
    if (b.provider && !LISTS[b.provider])
      await fetchList(ctx, M.providers.find(p => p.id === b.provider), false) && drawBody(ctx);
  };
  const dl = `models-${cap}-${r.id}-${bi}`;
  const model = el("input", {class: "btn mono", list: dl, style: "flex:1;min-width:170px;font-weight:400",
    placeholder: t("models.model_ph")});
  model.value = b.model || "";
  model.oninput = () => { b.model = model.value; touch(ctx); };
  // Пометка «другая модель» у запасного векторов зависит от имени: показать её, когда
  // человек закончил ввод, а не на каждой букве (перерисовка уводит фокус).
  if (cap === "embeddings") model.onchange = () => drawBody(ctx);
  model.onfocus = async () => {
    if (b.provider && !LISTS[b.provider]){
      const got = await fetchList(ctx, M.providers.find(p => p.id === b.provider), false);
      const box = document.getElementById(dl);
      if (box && got.length){ box.innerHTML = ""; got.forEach(m => box.append(el("option", {value: m}))); }
    }
  };
  const datalist = el("datalist", {id: dl}, ...(LISTS[b.provider] || []).map(m => el("option", {value: m})));
  const ctxIn = cap === "llm" ? el("input", {class: "btn", type: "number", min: 0,
    style: "width:110px;font-weight:400", title: t("models.context_hint"),
    placeholder: t("models.context_ph")}) : null;
  if (ctxIn){ ctxIn.value = b.context || ""; ctxIn.oninput = () => { b.context = Number(ctxIn.value || 0); touch(ctx); }; }
  const url = cap !== "llm" ? el("input", {class: "btn mono", style: "width:220px;font-weight:400;font-size:12px",
    title: t("models.url_hint"), placeholder: t("models.url_ph")}) : null;
  if (url){ url.value = b.url || ""; url.oninput = () => { b.url = url.value; touch(ctx); }; }
  const on = el("input", {type: "checkbox", checked: b.enabled !== false ? "" : null,
    onchange: e => { b.enabled = e.target.checked; touch(ctx, true); }});
  const move = d => {
    const j = bi + d;
    if (j < 0 || j >= r.backends.length) return;
    [r.backends[bi], r.backends[j]] = [r.backends[j], r.backends[bi]];
    touch(ctx, true);
  };
  // Вектора другой модели лежат в другом пространстве: такой запасной в кольцо не войдёт.
  const otherSpace = cap === "embeddings" && bi > 0 && first && b.model
    && first.model && b.model.trim().toLowerCase() !== first.model.trim().toLowerCase();
  const row = el("div", {class: "list-item", draggable: "true",
      style: "gap:8px;flex-wrap:wrap;align-items:center;padding:8px 0;"
             + (b.enabled === false ? "opacity:.55" : "")},
    el("span", {class: "muted", style: "cursor:grab;user-select:none", title: t("models.drag")}, "⋮⋮"),
    el("span", {class: "chip" + (bi === 0 ? " ok" : "")},
      bi === 0 ? t("models.primary") : t("models.fallback_n", {n: bi})),
    prov, model, datalist, ctxIn, url,
    el("label", {class: "row", style: "gap:4px;font-size:12px", title: t("models.enabled_hint")},
      on, t("models.enabled")),
    otherSpace ? el("span", {class: "chip warn", title: t("models.other_space_hint")},
                    t("models.other_space")) : null,
    el("span", {style: "flex:1"}),
    el("button", {class: "btn sm", title: t("models.up"), onclick: () => move(-1)}, "↑"),
    el("button", {class: "btn sm", title: t("models.down"), onclick: () => move(1)}, "↓"),
    el("button", {class: "btn sm danger", title: t("models.remove"), onclick: () => {
      r.backends.splice(bi, 1); touch(ctx, true); }}, "✕"));
  // Перетаскивание — внутри одной роли: цепочка запасных — порядок, а не перенос между ролями.
  row.addEventListener("dragstart", e => { DRAG = {cap, role: r.id, from: bi};
    e.dataTransfer.effectAllowed = "move"; });
  row.addEventListener("dragover", e => {
    if (DRAG && DRAG.cap === cap && DRAG.role === r.id){ e.preventDefault(); row.style.outline = "1px dashed var(--primary)"; }
  });
  row.addEventListener("dragleave", () => { row.style.outline = ""; });
  row.addEventListener("drop", e => {
    e.preventDefault(); row.style.outline = "";
    if (!DRAG || DRAG.cap !== cap || DRAG.role !== r.id || DRAG.from === bi) return;
    const [x] = r.backends.splice(DRAG.from, 1);
    r.backends.splice(bi, 0, x);
    DRAG = null;
    touch(ctx, true);
  });
  if (!b._new) return row;
  // Новый провайдер прямо из строки бэкенда: заводим, выбираем — и дальше по цепочке.
  const fresh = {id: "", name: "", type: "openai", url: "", key: "", width: 0, parallel: true,
                 template: {}};
  return el("div", {}, row, el("div", {class: "card", style: "padding:12px;margin:4px 0 8px 24px"},
    el("div", {class: "muted", style: "font-size:12px;margin-bottom:6px"}, t("models.new_provider_about")),
    providerForm(ctx, fresh, ok => {
      delete b._new;
      if (ok){
        if (!String(fresh.url || "").startsWith("http")) {
          b._new = true;
          return ctx.toast(t("models.list_no_url"), "warn");
        }
        fresh.id = newId(fresh.name || fresh.url.replace(/^https?:\/\//, "").split(/[/:]/)[0]);
        M.providers.push(fresh);
        b.provider = fresh.id;
        fetchList(ctx, fresh, false).then(got => { if (got.length) drawBody(ctx); });
      }
      touch(ctx, true);
    })));
}

/* ------------------------------------------------------------ прогон */

function runSettings(ctx){
  const {t, el} = ctx;
  const s = M.settings;
  const num = (k, w) => {
    const x = el("input", {class: "btn", style: `width:${w}px;font-weight:400`});
    x.value = s[k];
    x.oninput = () => { s[k] = /^\d+$/.test(x.value) ? Number(x.value) : x.value; touch(ctx); };
    return x;
  };
  const adapter = el("select", {class: "btn"}, ...["pydantic_ai", "openai_compat"].map(v =>
    el("option", {value: v, selected: s.adapter === v ? "" : null}, v)));
  adapter.onchange = () => { s.adapter = adapter.value; touch(ctx); };
  const debug = el("input", {type: "checkbox", checked: s.debug ? "" : null,
    onchange: e => { s.debug = e.target.checked; touch(ctx); }});
  return el("details", {class: "card", style: "padding:14px 16px;margin-top:16px"},
    el("summary", {style: "cursor:pointer;font-weight:600"}, t("models.run_title")),
    el("p", {class: "muted", style: "font-size:12.5px"}, t("models.run_about")),
    el("div", {class: "row", style: "gap:14px;flex-wrap:wrap"},
      el("label", {class: "row", style: "gap:6px"}, t("models.run_parallel"), num("parallel", 80)),
      el("label", {class: "row", style: "gap:6px"}, t("models.run_timeout"), num("request_timeout", 80)),
      el("label", {class: "row", style: "gap:6px"}, t("models.run_steps"), num("max_steps", 64)),
      el("label", {class: "row", style: "gap:6px"}, t("models.run_budget"), num("budget_min", 64)),
      el("label", {class: "row", style: "gap:6px"}, t("models.run_adapter"), adapter),
      el("label", {class: "row", style: "gap:6px", title: t("models.run_debug_hint")},
        debug, t("models.run_debug"))),
    el("div", {class: "row", style: "gap:10px;margin-top:10px"},
      // Ширину сервера человек знать не обязан: замер посылает растущие пачки запросов и
      // говорит, сколько шлюз держит на самом деле.
      el("button", {class: "btn sm", title: t("models.measure_hint"),
        onclick: () => ctx.runWatched("agent:width", [])}, t("models.measure"))),
    el("div", {class: "muted", style: "font-size:12px;margin-top:8px"},
      t("models.run_slots", {n: DATA.slots || 0, split: (DATA.slot_split || [])
        .map(([n, k]) => ((DATA.models.providers[n - 1] || {}).name || "№" + n) + "×" + k)
        .join(", ") || "—"})));
}

export default {mount, refresh};
