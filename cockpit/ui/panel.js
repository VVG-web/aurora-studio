const TOKEN = "__AURORA_TOKEN__";
// Версия панели: под какую версию ядра она собрана. Ядро уходит вперёд быстрее, чем
// интерфейс, и молча отставший интерфейс — худший вид отставания: он выглядит рабочим.
// Правило: младшая версия должна совпадать с ядром (1.11.x ↔ kit 1.11.y), иначе панель
// честно сообщает, что новых команд и метрик в ней может не быть. Проверяется тестом.
const UI_VERSION = "1.161.0";
const S = { state:null, project:null, health:null, view:"overview", job:null, docs:[] };

const $ = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>[...r.querySelectorAll(s)];
const el = (tag, attrs={}, ...kids)=>{
  const n = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)){
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined)
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
};
// Подсказка к кнопке: что делает, пример использования и пример результата. Появляется
// после паузы наведения, а не сразу: мимолётная мышь не должна засыпать экран облачками,
// а тот, кто задержался над кнопкой, как раз и не понял, что она делает. Текст — в
// каталогах строк (`<ключ>.what`, `.how`, `.result`), кнопке достаточно `data-help`.
const HELP_DELAY = 2000;
let helpTimer = null, helpBox = null;
function hideHelp(){
  clearTimeout(helpTimer); helpTimer = null;
  if (helpBox){ helpBox.remove(); helpBox = null; }
}
function showHelp(node){
  const key = node.dataset.help;
  if (!key || !document.body.contains(node) || t(key + ".what") === key + ".what") return;
  hideHelp();
  helpBox = el("div", {class:"help-tip", role:"tooltip"},
    el("b", {}, t(key + ".what")),
    el("div", {class:"k"}, t("help.example")), el("div", {}, t(key + ".how")),
    el("div", {class:"k"}, t("help.result")), el("div", {}, t(key + ".result")));
  document.body.append(helpBox);
  const r = node.getBoundingClientRect(), b = helpBox.getBoundingClientRect();
  const top = (r.bottom + 8 + b.height <= innerHeight) ? r.bottom + 8 : Math.max(8, r.top - 8 - b.height);
  const left = Math.min(Math.max(8, r.left), innerWidth - b.width - 8);
  helpBox.style.top = top + "px"; helpBox.style.left = left + "px";
}
document.addEventListener("mouseover", e => {
  const node = e.target.closest && e.target.closest("[data-help]");
  if (!node || node.contains(e.relatedTarget)) return;
  hideHelp();
  helpTimer = setTimeout(() => showHelp(node), HELP_DELAY);
});
document.addEventListener("mouseout", e => {
  const node = e.target.closest && e.target.closest("[data-help]");
  if (node && !node.contains(e.relatedTarget)) hideHelp();
});
["mousedown", "keydown", "wheel", "scroll"].forEach(ev => document.addEventListener(ev, hideHelp, true));
const tick = s => esc(s).replace(/`([^`]+)`/g, '<code class="tick">$1</code>');
const esc = s => String(s??"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const api = async (path, opts={}) => {
  const sep = path.includes("?") ? "&" : "?";
  // Язык интерфейса сервер знает от страницы: описания команд и маршруты приходят от него
  // готовыми. `S` объявлен ниже, а первый запрос идёт до выбора языка — тогда русский.
  const lang = (!path.includes("lang=") && typeof S !== "undefined" && S.lang && S.lang !== "ru")
    ? "&lang=" + encodeURIComponent(S.lang) : "";
  const r = await fetch(path + sep + "t=" + encodeURIComponent(TOKEN) + lang,
    {...opts, headers:{"Content-Type":"application/json", ...(opts.headers||{})}});
  const d = await r.json();
  if (d.error && !opts.quiet) toast(d.error, "err");
  return d;
};
function toast(msg, kind="ok"){
  const t = el("div",{class:"toast "+kind}, msg);
  $("#toasts").append(t);
  setTimeout(()=>{ t.style.opacity=0; setTimeout(()=>t.remove(),250); }, 4200);
}

// Исключение в отрисовке не должно проходить молча. Страница настроек проекта обрывалась
// на блоке MCP, и карточка агента с моделями и ролями просто не появлялась: с экрана это
// читалось как «так задумано», а не как поломка, и прожило двенадцать дней.
let LAST_FAULT = "";
function showFault(msg){
  msg = String(msg || t("fault.unknown"));
  if (msg === LAST_FAULT) return;                 // одна поломка — одно сообщение
  LAST_FAULT = msg;
  setTimeout(()=>{ if (LAST_FAULT === msg) LAST_FAULT = ""; }, 5000);
  console.error("Сбой на странице:", msg);
  toast(t("fault.toast", {why: msg}), "err");
}
window.addEventListener("error", e => showFault(e.message));
window.addEventListener("unhandledrejection", e =>
  showFault(e.reason && (e.reason.message || e.reason)));


/* ---------------- языки ---------------- */
// Каталог строк живёт отдельным файлом (`cockpit/i18n/<язык>.json`) — тем же приёмом,
// что и темы оформления: новый язык = новый файл, панель править не нужно. Ключа нет в
// переводе — берём русский: половина экрана на чужом языке хуже, чем весь на своём.
// Русский каталог сервер подставляет в саму страницу. Так язык по умолчанию не зависит
// ни от одного запроса: сеть, старый процесс сервера или отсутствующий файл каталога
// больше не превращают панель в набор имён ключей.
const RU = (() => { const raw = "__AURORA_I18N__";
  return (raw && typeof raw === "object") ? raw : {}; })();
let I18N = RU;

function t(key, vars){
  let s = I18N[key];
  if (s === undefined) s = RU[key];         // другой язык неполон — берём русский
  if (s === undefined) return key;          // такого ключа нет вовсе — видно сразу
  // Множественное число — правило языка, а не склейка в коде: «1 блокер», «2 блокера»,
  // «5 блокеров», а в английском две формы. Строка хранит формы через «|» в порядке
  // one|few|many; сколько их взять, решает Intl по числу `n`.
  if (vars && typeof vars.n === "number" && s.includes("|")){
    const forms = s.split("|");
    const rule = new Intl.PluralRules(S.lang || "ru").select(vars.n);
    const idx = {one: 0, few: 1, many: 2, other: forms.length - 1}[rule];
    s = forms[Math.min(idx === undefined ? forms.length - 1 : idx, forms.length - 1)];
  }
  if (vars) for (const [k,v] of Object.entries(vars)) s = s.replaceAll("{"+k+"}", v);
  return s;
}

async function restartPanel(opts = {}){
  // Токен новый процесс получает от текущего, поэтому открытая вкладка продолжит
  // работать: адрес тот же, и после подъёма достаточно перечитать страницу.
  // `patient` — после обновления кита: первый старт новой версии собирает реестр команд
  // (на нагруженной машине — больше минуты), и ждать надо дольше, говоря, чего ждём.
  const say = opts.progress || (text => toast(text));
  const busy = await api("/api/jobs", {quiet:true});
  const live = (busy && busy.jobs || []).filter(j => !j.done);
  if (live.length && !confirm(
      t("restart.busy_ask", {what: live.map(j => j.cmd).join(", ")}))) return;
  const d = await api("/api/restart", {method:"POST", quiet:true, body:"{}"});
  if (d && d.error) return toast(d.error, "err");
  say(t("restart.rising"));
  const started = Date.now(), limit = (opts.patient ? 300 : 60) * 1000;
  let told = "";
  await new Promise(ok => setTimeout(ok, 1500));      // прежний процесс ещё отвечает
  while (Date.now() - started < limit){
    try {
      const r = await fetch("/api/ping?t=" + encodeURIComponent(TOKEN));
      const p = r.ok ? await r.json() : null;
      if (p && p.ready){ location.reload(); return; }
      if (p){
        const text = t("restart.registry",
          {secs: Math.round((Date.now() - started) / 1000)});
        if (opts.progress || !told){ say(text); told = text; }
      }
    } catch (e) { /* ещё не поднялась */ }
    await new Promise(ok => setTimeout(ok, 1000));
  }
  toast(t("restart.timeout", {mins: limit / 60000}), "err");
}

/* Новая версия Aurora. Проверку делает сервер (раз в шесть часов, без git у человека), а
   панель ставит отметку на «О проекте» и один раз на версию говорит об этом словами:
   отложившего обновление не надо встречать напоминанием при каждом входе. Здесь же — итог
   обновления: его показывает уже перезапущенная панель, старая до него не доживает. */
async function checkKitUpdate(){
  let done = null;
  try {
    done = JSON.parse(localStorage.getItem("aurora-kit-updated") || "null");
    localStorage.removeItem("aurora-kit-updated");
  } catch (e) { done = null; }
  if (done){
    toast(t("kit.updated", {from: done.from, to: done.to}), "ok");
    (done.notes || []).forEach(n => toast(n));
  }
  const st = await api("/api/kit/status", {quiet:true}).catch(() => null);
  if (!st || st.error || !st.newer){ setBadge("about", ""); return; }
  setBadge("about", "↑");
  let told = "";
  try { told = localStorage.getItem("aurora-kit-told") || ""; } catch (e) {}
  if (told !== st.latest){
    try { localStorage.setItem("aurora-kit-told", st.latest); } catch (e) {}
    toast(t("kit.newer", {latest: st.latest, installed: st.installed}));
  }
}

async function loadI18n(){
  const lang = localStorage.getItem("aurora-lang") || "ru";
  S.lang = "ru"; S.langs = [{id:"ru", name: t("lang.russian"), keys:Object.keys(RU).length}];
  document.documentElement.lang = "ru";
  if (lang === "ru"){
    // Вернуться на русский — значит вернуть и строки: `I18N` держит каталог прошлого
    // языка, и без этой строки панель оставалась английской до перезагрузки страницы.
    I18N = RU;
    applyI18n();   // русский уже в странице — за строками ходить некуда
    // А вот за СПИСКОМ языков — нужно: пока его не спрашивали, переключатель показывал
    // один русский, и уйти с него было некуда. Дефект дремал, пока язык был один.
    api("/api/i18n?lang=ru", {quiet:true}).then(d => {
      if (d && d.languages && d.languages.length > 1){ S.langs = d.languages; drawLangPicker(); }
    });
    return;
  }
  // За другим языком идём по сети, но её отказ не должен ломать загрузку панели:
  // не ответила — остаёмся на русском и говорим об этом, а не показываем пустые экраны.
  try {
    const d = await api("/api/i18n?lang=" + encodeURIComponent(lang), {quiet:true});
    if (d && d.strings) {
      I18N = {...RU, ...d.strings};
      S.langs = d.languages && d.languages.length ? d.languages : S.langs;
      S.lang = d.lang || "ru";
      document.documentElement.lang = S.lang;
      if (d.warning) toast(d.warning, "warn");
    } else {
      toast(t("lang.failed_one", {lang}), "warn");
    }
  } catch (e) {
    toast(t("lang.failed"), "warn");
  }
  applyI18n();
}

function applyI18n(root = document){
  // Разметка помечена `data-i18n`; новые экраны пишутся сразу так, старые переезжают
  // тогда, когда их и так правят. Разовый вынос 982 строк — правка, которая трогает
  // каждый экран и не даёт человеку ничего видимого.
  $$("[data-i18n]", root).forEach(n => { const v = I18N[n.dataset.i18n]; if (v) n.textContent = v; });
  $$("[data-i18n-ph]", root).forEach(n => { const v = I18N[n.dataset.i18nPh]; if (v) n.placeholder = v; });
  // Подсказка над кнопкой — такая же надпись: без этой строки `title` оставался русским
  // на любом языке, и заметить это можно было только наведя мышь.
  $$("[data-i18n-title]", root).forEach(n => {
    const v = I18N[n.dataset.i18nTitle]; if (v) n.title = v; });
  // Подпись для экранного диктора: её не видно глазами, и по-русски на английском экране
  // она осталась бы навсегда.
  $$("[data-i18n-aria]", root).forEach(n => {
    const v = I18N[n.dataset.i18nAria]; if (v) n.setAttribute("aria-label", v); });
  // Абзац с разметкой внутри (цветные слова, имена файлов моноширинным). Строка приходит
  // из каталога кита — своего файла, не из сети и не от человека.
  $$("[data-i18n-html]", root).forEach(n => {
    const v = I18N[n.dataset.i18nHtml]; if (v) n.innerHTML = v; });
}


function drawLangPicker(){
  const sel = $("#langSel");
  if (!sel) return;
  sel.innerHTML = "";
  for (const l of (S.langs || [])){
    const o = el("option", {value:l.id}, t("lang.keys", {name: l.name, n: l.keys}));
    if (l.id === S.lang) o.selected = true;
    sel.append(o);
  }
  const total = Math.max(...(S.langs || [{keys:0}]).map(l => l.keys), 1);
  const mine = (S.langs || []).find(l => l.id === S.lang);
  // Полноту говорим числом, а не «переведено частично»: человек должен видеть,
  // сколько экрана придёт по-русски, прежде чем выбрать язык.
  $("#langNote").textContent = mine && mine.keys < total
    ? t("lang.partial", {have: mine.keys, all: total})
    : "";
  sel.onchange = async () => {
    localStorage.setItem("aurora-lang", sel.value);
    await loadI18n();
    drawLangPicker();
    relabelModules();      // подписи разделов в меню
    // Описания команд приходят от сервера уже на языке интерфейса: без нового запроса
    // они остались бы прежними до перезагрузки страницы.
    const st = await api("/api/state", {quiet:true});
    if (st && st.commands) S.state.commands = st.commands;
    S.scenarios = null;     // маршруты тоже приходят на языке интерфейса — спросим заново
    S.health = null;        // и итог «Здоровья»: находки доктора и описания источников
    loadSkins();            // и названия скинов в списке
    await refreshModules();   // и содержимое тех, что уже подняты
    toast(t("lang.switched",
                {name: sel.options[sel.selectedIndex].text.split(" · ")[0]}));
  };
}


/* ---------------- граф базы ---------------- */
/* ---------------- файлы проекта ---------------- */
const F = { files:[], path:null, digest:"", ed:null, ro:"", loaded:false, orig:"", fm:"",
            filter:"all", open:new Set(), recent:[], changed:new Set(), createDirs:[] };
// Порог живого предпросмотра в знаках. Не выдумка: на карточке в 11 КБ редактор сам
// написал «требует 20901мс», а `_index.md` живой базы весит 171 КБ.
const PREVIEW_LIMIT = 40000;
// Потолок редактора. Выше него не «медленно», а «висит»: разбор документа в редакторе
// растёт с размером, и `_index.md` живой базы (171 КБ) подвесил бы панель молча.
// Такие файлы — не документы аналитика, а машинные оглавления: их не правят руками.
const EDIT_LIMIT = 150000;

async function renderFiles(){
  const tree = $("#fileTree");
  if (!S.project){
    tree.innerHTML = "";
    tree.append(el("div",{class:"sub"}, t("files.pick_project")));
    $("#filesGit").textContent = t("files.pick_project");
    $("#navFiles").textContent = "";
    return;
  }
  // Пустое место человек читает как «файлов нет», а не как «ещё читаю». На живом
  // проекте обход дерева и опрос git занимают около двух секунд — за это время
  // молчащая панель успевает выглядеть сломанной.
  tree.innerHTML = "";
  tree.append(el("div",{class:"sub"}, t("files.loading")));
  await drawGit();
  const d = await api("/api/files/tree?project=" + encodeURIComponent(S.project.path));
  if (d.error){
    tree.innerHTML = "";
    tree.append(el("div",{class:"sub"}, t("files.failed", {why: d.error})));
    $("#navFiles").textContent = "";
    return;
  }
  F.files = d.files || [];
  F.recent = d.recent || [];
  F.createDirs = d.create_dirs || [];
  F.changed = new Set(((S.git && S.git.dirty) || []).map(r => r.path));
  // Счётчик — все файлы проекта, а не длина списка: список бывает короче.
  $("#navFiles").textContent = d.total || F.files.length || "";
  drawFilters();
  drawTree();
  if (d.truncated) toast(t("files.truncated", {n:d.count, total:d.total}), "warn");
}

const SHOW_LIMIT = 600;      // столько рисуем зараз: 1500 кнопок — 89 метров прокрутки

// Быстрые фильтры отвечают на вопросы, которые у человека реально есть, вместо того
// чтобы заставлять его вспоминать путь. «Изменённые» — самый ценный: это «что я трогал
// сегодня», и данные для него уже есть в состоянии git.
// Имя фильтра — ключ надписи в каталоге (`files.filter.<имя>`), а не сама надпись:
// по нему же фильтр и запоминается, поэтому смена языка не сбивает выбор.
const FILTERS = {
  all:       () => true,
  changed:   f => F.changed.has(f.path),
  base:      f => f.path.startsWith("AuroraKnowledgeDB/"),
  artifacts: f => f.path.startsWith("Artifacts/") || f.path.startsWith("Deliverables/"),
  drafts:    f => f.path.startsWith("Workspaces/") || f.path.startsWith("Raw/"),
};

function fileRow(f){
  const b = el("button", {class:"btn", style:"width:100%;text-align:left;margin:1px 0",
    title:f.path, onclick:()=>openFile(f)}, f.name);
  if (f.path === F.path) b.setAttribute("aria-current", "true");
  if (F.changed.has(f.path)) b.append(el("span",{class:"pill"}, t("files.changed")));
  if (f.readonly) b.append(el("span",{class:"pill"}, "🔒"));
  if (!f.text) b.append(el("span",{class:"pill"}, t("files.outside")));
  return b;
}

function treeOf(rows){
  // Настоящее дерево, а не плоский список путей. Свёрнутый список из 382 полных путей —
  // та же стена, что 1500 файлов, только другой формы: человек ищет папку глазами
  // вместо того, чтобы раскрыть одну ветку.
  const root = {dirs:new Map(), files:[]};
  for (const f of rows){
    let node = root;
    for (const part of (f.dir ? f.dir.split("/") : [])){
      if (!node.dirs.has(part)) node.dirs.set(part, {dirs:new Map(), files:[]});
      node = node.dirs.get(part);
    }
    node.files.push(f);
  }
  return root;
}

function countIn(node){
  let n = node.files.length;
  for (const child of node.dirs.values()) n += countIn(child);
  return n;
}

function drawTree(){
  const q = ($("#fileSearch").value || "").trim().toLowerCase();
  const pass = FILTERS[F.filter] || FILTERS.all;
  const rows = F.files.filter(f => pass(f) && (!q || f.path.toLowerCase().includes(q)));
  const box = $("#fileTree");
  box.innerHTML = "";

  // Недавние — первым блоком: человек возвращается к тому, над чем работал вчера,
  // а не ищет по алфавиту среди двух тысяч карточек.
  if (!q && F.filter === "all" && (F.recent || []).length){
    const rec = F.recent.map(p => F.files.find(f => f.path === p)).filter(Boolean);
    if (rec.length){
      box.append(el("div",{class:"group", style:"margin-top:2px"}, t("files.recent")));
      rec.slice(0, 10).forEach(f => box.append(fileRow(f)));
    }
  }

  if (!rows.length){ box.append(el("div",{class:"sub"}, t("files.empty"))); return; }

  let shown = 0;
  const walk = (node, path, depth) => {
    for (const [name, child] of [...node.dirs.entries()].sort((a,b)=>a[0].localeCompare(b[0]))){
      const full = path ? path + "/" + name : name;
      // При поиске раскрываем всё: человек ищет файл, а не папку.
      const open = !!q || F.open.has(full);
      box.append(el("div",{class:"group",
        style:`margin-top:6px;padding-left:${depth * 12}px`,
        onclick:()=>{ if (F.open.has(full)) F.open.delete(full); else F.open.add(full);
                      drawTree(); }},
        (open ? "▾ " : "▸ ") + name + "  " + countIn(child)));
      if (open) walk(child, full, depth + 1);
      if (shown >= SHOW_LIMIT) return;
    }
    for (const f of node.files){
      if (shown >= SHOW_LIMIT) return;
      const b = fileRow(f);
      b.style.paddingLeft = (depth * 12 + 8) + "px";
      box.append(b);
      shown++;
    }
  };
  walk(treeOf(rows), "", 0);

  // Молчаливая обрезка — худший вид: список выглядит полным. Говорим числом.
  if (shown < rows.length)
    box.append(el("div",{class:"sub", style:"margin-top:10px"},
      t("files.shown", {shown, total: rows.length})));
}

function drawFilters(){
  const box = $("#fileFilters");
  if (!box) return;
  box.innerHTML = "";
  for (const name of Object.keys(FILTERS)){
    const n = F.files.filter(FILTERS[name]).length;
    const b = el("button",{class:"btn", style:"font-size:12px;padding:3px 8px",
      "data-help": "help.files.filter_" + name,
      onclick:()=>{ F.filter = name; drawFilters(); drawTree(); }},
      t("files.filter." + name) + (name === "all" ? "" : " " + n));
    if (F.filter === name) b.setAttribute("aria-current", "true");
    box.append(b);
  }
  box.append(el("span",{style:"flex:1"}),
    el("button",{class:"btn", style:"font-size:12px;padding:3px 8px",
      "data-help": "help.files.add", onclick:newFile}, t("files.add")));
}

async function newFile(){
  const where = F.createDirs.length ? F.createDirs : ["Workspaces"];
  const guess = (F.path && where.find(d => F.path.startsWith(d + "/"))) || where[0];
  const name = prompt(t("files.new_ask", {dirs: where.join(", ")}), guess + "/");
  if (!name) return;
  const d = await api("/api/files/create", {method:"POST", quiet:true, body: JSON.stringify(
    {project:S.project.path, path:name})});
  if (d.error) return toast(d.error, "err");
  await renderFiles();
  const f = F.files.find(x => x.path === d.path);
  if (f) await openFile(f); else toast(t("files.created", {path: d.path}));
}

async function renameFile(){
  if (!F.path || F.ro) return;
  const was = F.path.split("/").pop();
  const name = prompt(t("files.rename_ask"), was);
  if (!name || name === was) return;
  const d = await api("/api/files/rename", {method:"POST", quiet:true, body: JSON.stringify(
    {project:S.project.path, path:F.path, name})});
  if (d.error) return toast(d.error, "err");
  // Ссылки на прежнее имя стали битыми — сказать об этом обязательно: карточка,
  // на которую больше никто не ссылается, выпадает из базы молча.
  if (d.note) toast(d.note, "warn");
  await renderFiles();
  const f = F.files.find(x => x.path === d.path);
  if (f) await openFile(f);
}

async function deleteFile(){
  if (!F.path || F.ro) return;
  if (!confirm(t("files.delete_ask", {path: F.path}))) return;
  const d = await api("/api/files/delete", {method:"POST", quiet:true, body: JSON.stringify(
    {project:S.project.path, path:F.path})});
  if (d.error) return toast(d.error, "err");
  F.path = null; destroyEditor();
  $("#fileEditor").hidden = true; $("#fileNone").hidden = false;
  await renderFiles();
  toast(t("files.deleted"));
}

async function openFile(f){
  if (F.ed && F.dirty && !confirm(t("files.leave_dirty"))) return;
  $("#fileNone").hidden = true; $("#fileEditor").hidden = false;
  $("#fileName").textContent = f.path;
  if (!f.text){
    destroyEditor();
    $("#fileWarn").hidden = false; $("#fileWarn").textContent = t("files.binary");
    $("#fileSave").disabled = true; F.path = f.path; F.ro = t("files.not_text");
    return;
  }
  const d = await api("/api/files/read?project=" + encodeURIComponent(S.project.path)
                      + "&path=" + encodeURIComponent(f.path));
  if (d.error) return;
  if ((d.text || "").length > EDIT_LIMIT){
    destroyEditor();
    $("#fileFm").hidden = true;
    $("#fileStale").hidden = true;
    $("#fileWarn").hidden = false;
    $("#fileWarn").textContent = t("editor.too_big", {kb: Math.round(d.text.length / 1024)});
    $("#fileSave").disabled = true;
    F.path = d.path; F.ro = t("files.too_big_ro"); F.dirty = false;
    $("#fileGit").textContent = d.git || "";
    $("#fileRO").hidden = false;
    $("#fileRO").textContent = "🔒 " + F.ro;
    return;
  }
  F.path = d.path; F.digest = d.digest; F.ro = d.readonly || ""; F.orig = d.text; F.dirty = false;
  $("#fileRO").hidden = !F.ro;
  $("#fileRO").textContent = F.ro ? "🔒 " + t("files.readonly") + ": " + F.ro : "";
  // Запрет обязан заканчиваться действием. «Править нельзя» без «а вот как можно» —
  // это тупик, из которого человек выйдет в системный проводник, и след потеряется.
  const fixable = (d.path || "").startsWith("AuroraKnowledgeDB/") && !!F.ro
                  && !(d.path || "").includes("/meta/");
  $("#fileFix").hidden = !fixable;
  // Переименовать и удалить нельзя там же, где нельзя править: кнопка, которая всегда
  // на виду и почти всегда отказывает, учит не читать отказы.
  // Переход из графа в файл был, обратного не было. Связь односторонняя — это половина
  // навигации: посмотрел карточку, захотел увидеть окружение — иди искать её в графе
  // руками.
  const inKb = (d.path || "").startsWith("AuroraKnowledgeDB/") && d.path.endsWith(".md");
  $("#fileGraph").hidden = !inKb || !devSectionsOn();   // граф — раздел в разработке
  // «На графе» — переход в раздел с именем карточки: грузиться и искать её
  // будет он сам, а файлам про устройство графа знать незачем.
  if (inKb) $("#fileGraph").onclick = () => show("graph", {card: d.path});
  $("#fileRename").hidden = !!F.ro;
  $("#fileDelete").hidden = !!F.ro || (d.path || "").startsWith("AuroraKnowledgeDB/");
  if (fixable) $("#fileFix").onclick = () => correctCard(d.path);
  $("#fileGit").textContent = d.git || "";
  // «Опубликовать» показываем только там, где публикация применима: у видов артефактов
  // с объявленным адресом и у документов из `Deliverables/work`. Кнопка, которая всегда
  // на виду и почти всегда отказывает, учит не читать отказы.
  F.publishable = publishTarget(d.path);
  $("#filePub").hidden = !F.publishable;
  $("#fileStale").hidden = !d.stale;
  if (d.stale){
    $("#fileStale").innerHTML = "";
    $("#fileStale").append(t("editor.stale", {date: d.published}) + " ",
      el("button",{class:"btn", onclick:()=>publishFromEditor()}, t("editor.publish")));
  }
  $("#fileWarn").hidden = true;
  $("#fileLint").textContent = "";
  const [fm, body] = splitFrontmatter(d.text);
  F.fm = fm;
  $("#fileFm").hidden = !fm;
  if (fm){
    $("#fileFmText").value = fm;
    $("#fileFmHint").textContent = t("editor.fm_hint");
    $("#fileFmText").readOnly = !!F.ro;
    $("#fileFmText").oninput = markDirty;
  }
  await mountEditor(body);
  $("#fileSave").disabled = true;
  $("#fileSave").title = t("editor.nochanges");
  // Открытый файл отмечаем в дереве и подводим к нему прокрутку: без этого человек
  // теряет место и возвращается к соседнему документу поиском.
  // Раскрываем всю ветку до файла, а не одну папку: иначе он остаётся в свёрнутом
  // дереве и подсвечивать нечего.
  let acc = "";
  for (const part of (f.dir ? f.dir.split("/") : [])){
    acc = acc ? acc + "/" + part : part;
    F.open.add(acc);
  }
  drawTree();
  const mark = $('#fileTree button[aria-current="true"]');
  if (mark) mark.scrollIntoView({block:"nearest"});
}

function splitFrontmatter(text){
  // Шапка правится текстом в свёрнутом блоке, тело — редактором. Класть шапку внутрь
  // Vditor нельзя: в режиме «как в Word» он попытается её отрисовать и перепишет.
  const m = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(text || "");
  return m ? [m[0], text.slice(m[0].length)] : ["", text || ""];
}

function wholeText(){
  return (F.fm || "") + (F.ed ? F.ed.getValue() : "");
}

function markDirty(){
  if ($("#fileFmText") && !$("#fileFm").hidden) F.fm = $("#fileFmText").value;
  F.dirty = wholeText() !== F.orig;
  $("#fileSave").disabled = !F.dirty || !!F.ro;
  setDirty("file:" + F.path, t("dirty.file", {path: F.path}), F.dirty);
}

function destroyEditor(){
  if (F.ed && F.ed.destroy) { try { F.ed.destroy(); } catch(e){} }
  F.ed = null; $("#vditor").innerHTML = "";
}

async function mountEditor(text){
  destroyEditor();
  // «Как в Word» пересобирает текст по правилам markdown: в .gitignore, YAML или скрипте
  // `#` стал бы заголовком, а `*` — экранированным. Такие файлы правятся только разметкой.
  const md = /\.(md|markdown)$/i.test(F.path || "");
  const mode = md ? (localStorage.getItem("aurora-editor-mode") || "sv") : "sv";
  $("#fileMode").value = mode;
  $("#fileMode").disabled = !md;
  $("#fileMode").title = md ? "" : t("editor.mode_md_only");
  await ensureVditor();
  // Живой предпросмотр перерисовывает документ целиком. На карточке в 11 КБ редактор
  // сам сообщил про 20 секунд, а в живой базе есть карточки по 170 КБ — на них это
  // уже не «медленно», а «не открывается». Выше порога показываем разметку, а вид —
  // по требованию: лучше честно сказать, чем подвесить панель.
  const heavy = text.length > PREVIEW_LIMIT;
  F.heavy = heavy;
  F.ed = new Vditor("vditor", {
    lang: S.lang === "ru" ? "ru_RU" : "en_US",
    mode, height: Math.max(360, Math.round(
      (window.innerHeight || document.documentElement.clientHeight || 900) * 0.62)),
    cache:{enable:false},
    toolbarConfig:{pin:true},
    // Предпросмотр — у markdown; у .gitignore или YAML он рисовал бы `#` заголовком.
    preview:{ math:{engine:"KaTeX"}, mode: heavy || !md ? "editor" : "both", actions:[],
              delay: 800 },
    cdn: "/vendor/vditor",
    after(){
      F.ed.setValue(text);
      // «Без изменений» считаем сравнением ТЕКСТА, а не внутренним признаком «трогали»:
      // в режиме «как в Word» редактор пересобирает разметку своим сериализатором, и
      // файл расходится с исходным сразу после открытия, ничего не тронув. Признак
      // «трогали» загорелся бы сам, и защита от лишнего дифа исчезла бы молча.
      if (F.ro && F.ed.disabled) F.ed.disabled();   // «только для чтения» — значит не печатать
      if (heavy){
        $("#fileWarn").hidden = false;
        $("#fileWarn").innerHTML = "";
        $("#fileWarn").append(
          document.createTextNode(t("editor.heavy", {kb: Math.round(text.length / 1024)})),
          el("button",{class:"btn", style:"margin-left:8px", onclick(){
            F.ed.setPreviewMode("both"); $("#fileWarn").hidden = true;
          }}, t("editor.heavy_show")));
      }
      setTimeout(()=>{
        const now = F.ed.getValue();
        if (mode === "wysiwyg" && now !== text){
          const n = diffLines(text, now);
          $("#fileWarn").hidden = false;
          $("#fileWarn").textContent = t("editor.wysiwyg_warn", {n});
        }
        F.dirty = false; $("#fileSave").disabled = true;
      }, 60);
    },
    input(){ markDirty(); }
  });
}

// Редактор markdown «как в Файлах» — для разделов-модулей: тот же Vditor, тот же вид
// (разметка, просмотр рядом или «как в Word») и тот же выбор вида, что запомнили в «Файлах».
// → обещание редактора; `onInput` — на каждую правку.
async function markdownEditor(host, text, {onInput, height, mode} = {}){
  await ensureVditor();
  const view = mode || localStorage.getItem("aurora-editor-mode") || "sv";
  return new Promise(ok => {
    const ed = new Vditor(host, {
      lang: S.lang === "ru" ? "ru_RU" : "en_US", mode: view,
      height: height || Math.max(320, Math.round((window.innerHeight || 900) * 0.5)),
      cache: {enable: false}, toolbarConfig: {pin: true},
      preview: {math: {engine: "KaTeX"}, mode: "both", actions: [], delay: 800},
      cdn: "/vendor/vditor",
      after(){ ed.setValue(text || ""); ok(ed); },
      input(){ onInput && onInput(); }
    });
  });
}

// Раздел другого проекта: выбрать проект и открыть раздел с грузом. Нужен разделам машины
// («Cron»), у которых в строке — чужой проект, а не выбранный на Мостике.
async function openProject(path, view, payload){
  const p = (S.state && S.state.projects || []).find(x => x.path === path);
  if (p && (!S.project || S.project.path !== p.path)) await pick(p, false);
  show(view, payload);
}

function diffLines(a, b){
  const x = a.split("\n"), y = b.split("\n");
  let n = 0;
  for (let i = 0; i < Math.max(x.length, y.length); i++) if (x[i] !== y[i]) n++;
  return n;
}

// Библиотека тяжёлая (10 МБ на диске) — грузим её при первом открытии файла, а не при
// старте панели: панель обязана открываться сразу, и платить за редактор тот, кто им
// не пользуется, не должен.
let VDITOR_READY = null;
function ensureVditor(){
  if (VDITOR_READY) return VDITOR_READY;
  VDITOR_READY = new Promise((ok, fail)=>{
    const css = el("link",{rel:"stylesheet", href:"/vendor/vditor/dist/index.css"});
    document.head.append(css);
    const s = el("script",{src:"/vendor/vditor/dist/index.min.js"});
    s.onload = ok;
    s.onerror = ()=>{ toast(t("files.editor_failed"), "err"); fail(); };
    document.head.append(s);
  });
  return VDITOR_READY;
}

function publishTarget(rel){
  // Смотрим по реестру видов: папка артефакта с объявленным `publish_url` — и
  // `Deliverables/work`, откуда публикуются собранные документы.
  const r = (rel || "").replace(/\\/g, "/");
  if (r.startsWith("Deliverables/work/")) return true;
  const kinds = (S.kinds || {});
  return Object.values(kinds).some(k => k.out && k.publish_url
                                        && r.startsWith(k.out.replace(/\/$/, "") + "/"));
}

async function publishFromEditor(){
  if (!F.path) return;
  if (F.dirty && !confirm(t("editor.publish_dirty"))) return;
  if (F.dirty) await saveFile();
  const d = await api("/api/files/clean?project=" + encodeURIComponent(S.project.path)
                      + "&path=" + encodeURIComponent(F.path));
  if (d.error) return toast(d.error, "err");
  // Показываем ровно то, что уйдёт. Граница производства невидима в тексте, и человек
  // должен увидеть её собственными глазами до отправки, а не на странице у заказчика.
  const box = el("div",{class:"card", style:"padding:14px;margin-bottom:10px"},
    el("b",{}, t("editor.publish_preview")),
    el("div",{class:"sub"}, d.marked
      ? t("editor.publish_cut", {n: d.cut})
      : t("editor.publish_nomark")),
    el("pre",{class:"mono", style:"white-space:pre-wrap;max-height:260px;overflow:auto;"
      + "margin-top:8px;padding:8px;border-radius:8px;background:var(--surface-2)"}, d.clean),
    el("div",{class:"row", style:"gap:8px;margin-top:10px"},
      el("button",{class:"btn primary", onclick:async ()=>{
        box.remove();
        show("console");
        const res = await api("/api/run", {method:"POST", body: JSON.stringify(
          {project:S.project.path, cmd:"ship:publish", args:[F.path, "--apply"]})});
        if (res.job) poll(res.job, 0, "ship:publish " + F.path);
      }}, t("editor.publish_go")),
      el("button",{class:"btn", onclick:()=>box.remove()}, t("editor.publish_cancel"))));
  $("#filePane").prepend(box);
}

async function openPath(rel){
  // Общий вход в редактор по пути: им пользуются «Незаконченные», готовый артефакт
  // после производства и всё, что появится дальше.
  if (!S.project) return toast(t("files.pick_project"), "warn");
  show("files");
  if (!F.files.length) await renderFiles();
  const f = F.files.find(x => x.path === rel)
        || {path: rel, name: rel.split("/").pop(), text: true, readonly: ""};
  await openFile(f);
  $("#filePane").scrollIntoView({behavior:"smooth", block:"start"});
}

async function correctCard(rel){
  const card = rel.split("/").pop().replace(/\.md$/, "");
  const box = el("div",{class:"card", style:"padding:14px;margin-bottom:10px"},
    el("b",{}, t("editor.correct_title", {card})),
    el("p",{class:"sub"}, t("editor.correct_why")),
    el("textarea",{id:"fixText", rows:4, placeholder:t("editor.correct_ph"),
      style:"width:100%;font:inherit;padding:8px;border-radius:8px"}),
    el("div",{class:"row", style:"gap:8px;margin-top:8px"},
      el("button",{class:"btn primary", onclick:async ()=>{
        const text = ($("#fixText").value || "").trim();
        if (!text) return toast(t("editor.correct_empty"), "warn");
        show("console");
        const res = await api("/api/run", {method:"POST", body: JSON.stringify(
          {project:S.project.path, cmd:"kb:correct",
           args:["--new", card, "--text", text]})});
        if (res.job) poll(res.job, 0, "kb:correct " + card);
      }}, t("editor.correct_go")),
      el("button",{class:"btn", onclick:()=>box.remove()}, t("common.cancel"))));
  $("#filePane").prepend(box);
  setTimeout(()=>$("#fixText")?.focus(), 50);
}

async function saveFile(){
  if (!F.path || F.ro) return;
  const text = wholeText();
  const d = await api("/api/files/write", {method:"POST", quiet:true, body: JSON.stringify(
    {project:S.project.path, path:F.path, text, expect:F.digest})});
  if (d.conflict) return resolveConflict(text, d.disk);
  if (d.error) return toast(d.error, "err");
  F.digest = d.digest; F.orig = text; F.dirty = false;
  $("#fileSave").disabled = true; $("#fileGit").textContent = d.git || "";
  setDirty("file:"+F.path, "", false);
  toast(t("editor.saved"));
  drawLint(d.lint);
  drawGit();
}

function drawLint(lint){
  const box = $("#fileLint");
  if (!lint || !lint.lines){ box.textContent = ""; return; }
  const bad = lint.lines.filter(l => /ошибок [1-9]/.test(l));
  box.innerHTML = bad.length
    ? "⚠ " + esc(t("editor.lint_found", {n: bad.length})) + "<br>" + lint.lines.map(esc).join("<br>")
    : "✅ " + esc(t("editor.lint_clean"));
}

function resolveConflict(mine, disk){
  // Молча затирать чужую правку — тот же класс ошибки, что публикация поверх чужой
  // страницы: работа исчезает, и никто об этом не узнаёт.
  const box = el("div",{class:"card", style:"padding:14px"},
    el("b",{}, t("editor.conflict")),
    el("p",{class:"sub"}, t("editor.conflict_help")),
    el("div",{class:"row", style:"gap:8px"},
      el("button",{class:"btn primary", onclick:async()=>{
        const d = await api("/api/files/write", {method:"POST", body: JSON.stringify(
          {project:S.project.path, path:F.path, text:mine})});
        if (!d.error){ F.digest = d.digest; F.orig = mine; box.remove(); toast(t("editor.saved")); }
      }}, t("editor.keep_mine")),
      el("button",{class:"btn", onclick:()=>{
        F.ed.setValue(disk); F.orig = disk; F.dirty = false;
        $("#fileSave").disabled = true; box.remove();
      }}, t("editor.take_disk"))));
  $("#filePane").prepend(box);
}

/* ---------------- git проекта ---------------- */
async function drawGit(){
  const box = $("#filesGit");
  if (!S.project){ box.textContent = ""; return; }
  const g = await api("/api/git?project=" + encodeURIComponent(S.project.path), {quiet:true});
  S.git = g;
  box.innerHTML = "";
  // Не смогли спросить — не то же самое, что «репозитория нет». Устаревший токен
  // сессии выдавал «проект не под git» на проекте под git, и человек шёл чинить
  // репозиторий, с которым всё в порядке.
  if (g.error){ box.append(el("span",{class:"sub"}, t("git.unknown", {why: g.error}))); return; }
  if (!g.repo){ box.append(el("span",{class:"sub"}, g.why || t("git.norepo"))); return; }
  const head = el("div",{class:"row", style:"gap:10px;align-items:center;flex-wrap:wrap"},
    el("b",{}, t("git.title")),
    el("button",{class:"btn sm", onclick:()=>show("gitsync")}, t("git.more")),
    el("span",{class:"pill mono"}, g.branch || "—"),
    el("span",{class:"sub"}, g.count ? t("git.count",{n:g.count}) : t("git.clean")));
  if (g.ahead) head.append(el("span",{class:"pill"}, t("git.ahead", {n:g.ahead})));
  box.append(head);
  // Блок держим в одну-две строки: на живом проекте он занял 345 пикселей списком из
  // шестидесяти путей и утолкал редактор ниже экрана. Редактор — то, ради чего сюда
  // пришли; git — то, чем заканчивают.
  if (g.count){
    const list = el("details", {},
      el("summary", {class:"sub", style:"cursor:pointer"}, t("git.which")),
      el("div",{class:"mono sub", style:"max-height:160px;overflow:auto;margin-top:6px"},
        g.dirty.slice(0,200).map(r => (r.new ? "+ " : "· ") + r.path).join("\n")),
      el("div",{class:"sub", style:"margin-top:6px"}, t("git.why")));
    head.append(list);
    const msg = el("input",{id:"gitMsg", placeholder:t("git.message"),
      style:"flex:1;min-width:180px;font:inherit;padding:6px 8px;border-radius:8px"});
    const row = el("div",{class:"row", style:"gap:8px;margin-top:8px;flex-wrap:wrap"}, msg,
      el("button",{class:"btn primary", "data-help": "help.files.git_commit",
        onclick:()=>doCommit(false)}, t("git.commit")),
      el("button",{class:"btn", "data-help": "help.files.git_commit_anyway",
        onclick:()=>doCommit(true), title:t("git.ratchet_anyway")}, t("git.commit_anyway")));
    if (g.remotes && g.remotes.length)
      row.append(el("button",{class:"btn", "data-help": "help.files.git_push", onclick:doPush}, t("git.push")));
    box.append(row);
  } else if (g.remotes && g.remotes.length && g.ahead){
    box.append(el("div",{class:"row", style:"gap:8px;margin-top:8px"},
      el("button",{class:"btn", "data-help": "help.files.git_push", onclick:doPush}, t("git.push"))));
  }
}

async function doCommit(skip){
  const msg = ($("#gitMsg")?.value || "").trim();
  const d = await api("/api/git/commit", {method:"POST", quiet:true, body: JSON.stringify(
    {project:S.project.path, message:msg, skip_ratchet:!!skip})});
  if (d.error){
    toast(d.ratchet ? t("git.ratchet_blocked") : d.error, "err");
    if (d.ratchet) $("#filesGit").append(el("div",{class:"note"},
      t("git.ratchet_blocked") + " " + t("git.ratchet_anyway")));
    if (d.tail) $("#filesGit").append(el("pre",{class:"mono sub",
      style:"white-space:pre-wrap;margin-top:8px"}, d.tail));
    return;
  }
  toast(t("git.committed", {commit: d.commit}));
  drawGit();
  if (F.path) $("#fileGit").textContent = t("git.committed_file");
}

async function doPush(){
  toast(t("git.pushing"));
  const d = await api("/api/git/push", {method:"POST", quiet:true,
    body: JSON.stringify({project:S.project.path})});
  if (d.error){
    toast(d.error, "err");
    $("#filesGit").append(el("pre",{class:"mono sub",
      style:"white-space:pre-wrap;margin-top:8px"}, d.tail || ""));
    return;
  }
  toast(t("git.pushed", {remote: d.remote}));
  drawGit();
}

async function revealFile(mode){
  if (!F.path) return;
  await api("/api/files/reveal", {method:"POST", body: JSON.stringify(
    {project:S.project.path, path:F.path, mode})});
}

/* ---------------- несохранённые правки ---------------- */
// Ключ блока → его человекочитаемое имя. Нужны оба: по ключу подсвечиваем нужную кнопку,
// именем объясняем человеку, что именно он потеряет, если уйдёт.
const DIRTY = new Map();

function setDirty(key, label, on){
  if (on) DIRTY.set(key, label); else DIRTY.delete(key);
  $$(`[data-save="${key}"]`).forEach(b => b.classList.toggle("unsaved", DIRTY.has(key)));
  $$(`[data-badge="${key}"]`).forEach(n => n.style.display = DIRTY.has(key) ? "" : "none");
  sgroupsMarkDirty();
}
// Свёрнутая группа прячет и кнопку «Сохранить» с её пометкой — несохранённое видно на
// заголовке группы, иначе правку легко потерять, свернув её.
function sgroupsMarkDirty(){
  $$(".sgroup").forEach(g => {
    const mark = $(".sgroup-head > .unsaved-badge", g);
    if (!mark) return;
    const dirty = [...g.querySelectorAll("[data-save]")].some(b => DIRTY.has(b.dataset.save));
    mark.style.display = dirty ? "" : "none";
  });
}
const watch = (node, key, label) => {
  node.addEventListener("input", () => setDirty(key, label, true));
  node.addEventListener("change", () => setDirty(key, label, true));
  return node;
};
function saveButton(key, label, text, handler){
  const badge = el("span",{class:"unsaved-badge","data-badge":key,style:"display:none"},
    t("dirty.badge"));
  const btn = el("button",{class:"btn primary","data-save":key, onclick: async e=>{
    await handler(e);
  }}, text);
  return el("div",{class:"row"}, btn, badge);
}
function confirmLeave(){
  if (!DIRTY.size) return true;
  const names = [...DIRTY.values()].join(", ");
  const ok = confirm(t("dirty.ask", {names}));
  if (ok) DIRTY.clear();
  return ok;
}
addEventListener("beforeunload", e => {
  if (!DIRTY.size) return;
  e.preventDefault();          // браузер покажет свой диалог: текст задаём не мы
  e.returnValue = "";
});

/* ---------------- навигация ---------------- */
function show(view, payload){
  // уход из «Настройки» с незаписанными правками — единственное место, где панель
  // может молча потерять введённое: всё остальное она либо пишет сразу, либо не пишет
  if (S.view === "setup" && view !== "setup" && !confirmLeave()) return;
  // Раздел в разработке закрыт — в него не попасть и по адресу страницы или ссылке.
  if (devOnlyView(view) && !devSectionsOn()) view = "overview";
  S.view = view;
  $$(".view").forEach(v=>v.classList.toggle("on", v.id === "view-"+view));
  $$("nav button").forEach(b=>b.setAttribute("aria-current", String(b.dataset.view===view)));
  if (view==="overview") refreshActivity();
  if (view==="setup"){ renderSetup(); drawLangPicker(); }
  if (view==="project") renderProject();
  if (view==="files") renderFiles();
  if (view==="console"){
    renderHistory(); loadRuns(); drawTaskButton(); drawLiveJobs();
    // Маршрут мог остановиться в прошлой сессии: состояние читаем из проекта и, если сейчас
    // ничего не идёт, поднимаем «Продолжить маршрут» — её не было на перезапуске вкладки.
    if (S.project && ROUTE === null){
      api("/api/route/state?project="+encodeURIComponent(S.project.path), {quiet:true})
        .then(d=>{ showLastRoute(d && d.state); });
    }
  }   // журнал, задание и то, что идёт прямо сейчас
  if (MODULES.has(view)) mountModule(view, payload);
  location.hash = view + (S.project ? "|" + S.project.slug : "");
}
let ABOUT_TAPS = 0, ABOUT_TIMER = null;
const DEV_TAPS = 7;                       // столько же, сколько в Android до «Для разработчиков»

function devOn(){ return localStorage.getItem("aurora-dev") === "1"; }

// Разделы в разработке открываются вместе с «Разработкой»: те же семь нажатий на «О
// проекте», та же кнопка «Скрыть». Встроенный раздел помечен `data-devonly` в разметке,
// модуль — полем `"dev": true` в манифесте.
function devSectionsOn(){ return devOn() && !!(S.state && S.state.dev_available); }

function devOnlyView(view){
  return !!$(`nav button[data-view="${view}"][data-devonly]`)
    || !!(MODULES.get(view) && MODULES.get(view).manifest.dev);
}

function showDevNav(){
  const on = devSectionsOn();
  // Заголовок группы «Движок» показываем, только если в ней есть что показывать: сама
  // «Разработка» — теперь такой же раздел-папка, как остальные, и может отсутствовать.
  $("#devGroup").hidden = !on || !$('.navgroup[data-group="engine"] button');
  // Кнопка раздела-модуля в разработке тоже помечена `data-devonly` — её ставит
  // `addModuleNav`, поэтому здесь один и тот же приём на встроенные и на папки.
  $$("nav button[data-devonly]").forEach(b => { b.hidden = !on; });
}

// Спрятать разделы в разработке обратно. Зовёт сама «Разработка» — своей кнопкой;
// словами о случившемся говорит она же: строка про разделы живёт в её каталоге.
function hideDev(){
  localStorage.removeItem("aurora-dev");
  showDevNav();
  show("about");
}

function tapAbout(){
  if (devOn()) return;                    // уже открыт — считать нечего
  if (!(S.state && S.state.dev_available)){
    return;                               // панель поднята не из кита: разрабатывать нечем
  }
  ABOUT_TAPS++;
  clearTimeout(ABOUT_TIMER);
  ABOUT_TIMER = setTimeout(()=>{ ABOUT_TAPS = 0; }, 3000);   // пауза сбрасывает счёт
  const left = DEV_TAPS - ABOUT_TAPS;
  if (left > 0 && ABOUT_TAPS >= 3) toast(t("taps.left", {n: left}));
  if (left <= 0){
    localStorage.setItem("aurora-dev", "1");
    ABOUT_TAPS = 0;
    showDevNav();
    toast(t("taps.opened"), "ok");
  }
}

function navClick(b){
  const view = b.dataset.view;
  if (view === "about") tapAbout();
  // Нужен ли разделу проект, говорит его манифест (`needs`), а не список в коде ядра:
  // список приходилось править при каждом переезде, и однажды он остался пустым
  // массивом — истинным, отчего ЛЮБОЙ раздел стал требовать проект и уводить на Мостик.
  const needsProject = (MODULES.get(view)?.manifest.needs || []).includes("project");
  if (needsProject && !S.project){
    toast(t("nav.pick_project"), "warn"); show("overview"); return;
  }
  show(view);
}
$$("nav button").forEach(b => b.onclick = () => navClick(b));
$("#themeBtn").onclick = ()=>{
  const now = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = now;
  localStorage.setItem("aurora-theme", now);
};
// Выбор человека главнее; пока его нет — берём системный, а не тёмную наугад.
document.documentElement.dataset.theme = localStorage.getItem("aurora-theme")
  || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");

/* ---------------- загрузка ---------------- */
async function boot(){
  $("#towers").innerHTML = '<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
  await loadI18n();      // строки до первой отрисовки: иначе экран моргнёт с русского на выбранный
  S.state = await api("/api/state");
  showDevNav();          // раздел разработки, если его уже открывали на этой машине
  const ui = S.state.ui || {};
  $("#kitver").textContent = "cockpit " + UI_VERSION + " · kit " + S.state.kit.version;
  if (ui.behind){
    $("#kitver").classList.add("stale");
    $("#kitver").title = t("boot.stale_title",
      {ui: UI_VERSION, kit: S.state.kit.version});
    const bar = el("div",{class:"stalebar"},
      el("b",{}, t("boot.stale_bar")),
      t("boot.stale_versions", {ui: UI_VERSION, kit: S.state.kit.version}),
      t("boot.stale_why"));
    $("main").prepend(bar);
  }
  // Обновили kit, не перезапустив панель: файлы новые, процесс прежний. Страница при этом
  // отдаётся свежая и начинает просить у старого сервера то, чего он не умеет, — человек
  // получает «неизвестный маршрут» и ищет поломку там, где её нет.
  if (S.state.ui && S.state.ui.stale_process){
    // Кнопка, а не команда для терминала: панель заводилась, чтобы в терминал не ходить,
    // и отправлять туда за собственным перезапуском — расписываться в этом.
    $("main").prepend(el("div",{class:"stalebar"},
      el("b",{}, t("boot.stale_process")),
      t("boot.stale_process_why"),
      el("button",{class:"btn", style:"margin-left:8px", onclick:restartPanel},
        t("boot.restart"))));
  }
  drawAdapterAlarm(S.state.adapter_alarm);
  setBadge("commands", S.state.commands.length);
  const missing = S.state.env.items.filter(i=>!i.ok).length;
  setBadge("install", missing || "", missing > 0);
  renderOverview();
  renderHistory();
  loadSkins();
  await loadModules();   // меню собрано целиком до того, как адрес вернёт нас в раздел
  checkKitUpdate();      // новая версия Aurora — отметкой на «О проекте», не окном поверх работы
  prefetchHealth();
  const [v, slug] = (location.hash.slice(1)||"").split("|");
  // Раздел из адреса — сразу, не дожидаясь здоровья: `pick` ставит проект мгновенно, а
  // здоровье считается десятки секунд (на крупной базе — линтер всего дерева). Пока оно
  // шло, человек смотрел на Мостик и решал, что раздел из ссылки не открылся. Пришедшее
  // здоровье перерисует поднятые разделы само (`refreshModules` в `pick`).
  const p = slug ? S.state.projects.find(x => x.slug === slug) : null;
  const picking = p ? pick(p, false) : null;
  if (v) show(v);
  if (picking) await picking;
}

/* ---------------- мостик ---------------- */
// Что с проектом прямо сейчас — отметка на карточке. Работа видна только в «Консоли»
// выбранного проекта, и по Мостику было не понять, где обновление идёт, а где встало.
// Чистая функция от ответа сервера и часов: её проверяют без страницы.
function activityChips(act, now){
  if (!act) return [];
  const loc = S.lang === "en" ? "en-GB" : "ru-RU";
  const clock = ms => new Date(ms).toLocaleTimeString(loc, {hour:"2-digit", minute:"2-digit"});
  const stamp = ms => new Date(ms).toLocaleString(loc,
    {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"});
  const short = s => { s = String(s || ""); return s.length > 30 ? s.slice(0, 29) + "…" : s; };
  const running = act.running || [], agent = act.agent, route = act.route;
  const routeNote = route
    ? t("act.route_stopped_note", {title: route.title || t("act.untitled")}) : "";
  // Идущее важнее остановленного: задание могло как раз продолжить этот маршрут.
  if (running.length){
    const mins = Math.max(0, Math.round((now/1000 - running[0].started) / 60));
    return [{cls:"ok live",
      text: t("act.running", {cmd: running[0].cmd})
            + (running.length > 1 ? t("act.and_more", {n: running.length - 1}) : "")
            + (mins ? t("act.mins", {n: mins}) : t("act.just_now")),
      title: running.map(r => `${r.cmd} ${(r.args||[]).join(" ")}`.trim()
               + t("act.since", {time: clock(r.started * 1000)})).join("\n")
             + t("act.by_panel") + routeNote}];
  }
  if (agent && agent.alive) return [{cls:"ok live",
    text: t("act.agent_outside", {task: short(agent.task || t("act.agent"))}),
    title: t("act.agent_title",
             {pid: agent.pid, since: agent.since ? histWhen(agent.since) : "—"})
           + t("act.agent_not_panel") + routeNote}];
  const lockNote = agent
    ? t("act.lock_note", {task: agent.task || t("act.agent"), pid: agent.pid}) : "";
  if (route){
    const labels = {stall: t("act.reason_stall"), failed: t("act.reason_failed"),
                    offline: t("act.reason_offline"), stopped: t("act.reason_stopped"),
                    interrupted: t("act.reason_interrupted")};
    const waiting = route.reason === "offline" && route.nextRetryAt > now;
    return [{cls:"warn",
      text: (waiting ? t("act.route_waiting") : t("act.route_halted"))
            + `«${short(route.title)}»`,
      title: `«${route.title || t("act.untitled")}»`
             + (typeof route.step === "string" ? t("act.route_step", {step: route.step}) : "")
             + t("act.route_reason", {reason: labels[route.reason] || t("act.reason_other")})
             + (route.at ? ` · ${stamp(Date.parse(route.at))}` : "") + "."
             + (waiting ? t("act.route_retry", {time: clock(route.nextRetryAt)}) : "")
             + t("act.route_continue") + lockNote}];
  }
  if (agent) return [{cls:"bad",
    text: t("act.agent_broken", {task: short(agent.task || t("act.agent_run"))}),
    title: t("act.agent_broken_title",
             {task: agent.task || t("act.agent"), pid: agent.pid,
              since: agent.since ? histWhen(agent.since) : "—", at: stamp(agent.at * 1000)})
           + t("act.agent_broken_tail")}];
  return [];
}
function drawActivity(){
  if (!S.state) return;
  $$("#towers .act").forEach(box=>{
    const p = S.state.projects.find(x => x.path === box.dataset.path);
    box.replaceChildren(...activityChips(p && p.activity, Date.now())
      .map(c => el("span",{class:"chip " + c.cls, title:c.title}, c.text)));
  });
}
async function refreshActivity(){
  if (!S.state) return;
  let d = null;
  try { d = await api("/api/activity", {quiet:true}); } catch (e) { return; }
  if (!d || !d.projects) return;
  S.state.projects.forEach(p => { if (p.path in d.projects) p.activity = d.projects[p.path]; });
  drawActivity();
}
// Pydantic AI выбран, а вызовы идут мимо него: без инструментов и MCP бот «не видит» свои
// серверы, план собирается без поиска. С 1.139 по 1.160 это случалось четырежды и каждый раз
// всплывало через неделю. Полоса — поверх любого раздела, пока путь не пройдёт через адаптер.
function drawAdapterAlarm(rows){
  const old = $("#adapterbar");
  if (old) old.remove();
  if (!rows || !rows.length) return;
  $("main").prepend(el("div",{id:"adapterbar", class:"stalebar alarm"},
    el("b",{}, t("alarm.adapter")),
    ...rows.map(r => el("div",{}, t("alarm.adapter_row",
      {path: r.path, project: r.project || "—", when: r.at ? histWhen(r.at) : "—",
       calls: r.calls, why: r.why}))),
    el("div",{}, t("alarm.adapter_tail"),
      el("button",{class:"btn", style:"margin-left:8px", onclick:()=>show("install")},
        t("alarm.adapter_open")))));
}
async function refreshAdapterAlarm(){
  let d = null;
  try { d = await api("/api/adapter/alarm", {quiet:true}); } catch (e) { return; }
  if (d) drawAdapterAlarm(d.alarm);
}
const ALARM_POLL_MS = 20000;
setInterval(() => { if (!document.hidden) refreshAdapterAlarm(); }, ALARM_POLL_MS);
const ACTIVITY_POLL_MS = 5000;
setInterval(() => { if (S.view === "overview" && !document.hidden) refreshActivity(); },
            ACTIVITY_POLL_MS);

// Здоровье считается секундами, а проект за это время успевают сменить. Ответ по одному
// проекту, записанный прямо в S.health, показывался под именем другого: панель писала
// «нет .env.aurora.local» у проекта, где файл на месте, — это было замечание соседнего,
// чей ответ пришёл позже. Результат всегда ложится своему проекту, а текущим становится,
// только если этот проект всё ещё выбран.
function takeHealth(p, h){
  p.health = h;
  if (S.project && S.project.path === p.path){ S.health = h; return true; }
  return false;
}
async function prefetchHealth(){
  // последовательно: stats на большой базе занимает секунды, параллель только мешает
  for (const p of S.state.projects){
    if (p.health) continue;
    const h = await api("/api/health?project="+encodeURIComponent(p.path), {quiet:true});
    if (h && h.stats){ takeHealth(p, h);
      renderOverview(); renderProjBadge(); }
  }
  const total = S.state.projects.reduce((a,p)=>a + (p.health?.doctor.errors.length||0), 0);
  const g = $("#globalMetrics");
  if (g.firstChild) g.replaceChild(
    metric(total, t("overview.blockers_all"),
      total ? t("overview.blockers_all_go") : t("overview.blockers_all_ok"),
      total?"bad":"ok"), g.children[0]);
  stamp("#overviewStamp");
}
function aura(p){
  return {red:"var(--danger)", amber:"var(--tier-inreview)", green:"var(--primary)"}[auraWhy(p).color];
}

// Цвет карточки — обещание: «зелёный» должен быть достижимой целью, а не догадкой.
// Поэтому правило считается в одном месте и объясняется человеку теми же словами.
function auraWhy(p){
  const h = p.health, todo = [];
  const errs = h ? h.doctor.errors.length : 0;
  const lint = h ? h.lint.errors : 0, base = h ? (h.lint.baseline ?? null) : null;
  if (errs) todo.push({bad:true, code:"blockers", text: t("aura.blockers", {n: errs})});
  else todo.push({bad:false, code:"blockers", text: t("aura.no_blockers")});
  if (p.behind) todo.push({bad:true, text: t("aura.behind", {v: p.engine})});
  else todo.push({bad:false, text: t("aura.current")});
  if (base !== null && lint > base)
    todo.push({bad:true, text: t("aura.lint_worse", {n: lint, base, grew: lint - base})});
  else todo.push({bad:false, text: base !== null
    ? t("aura.lint_ratchet", {n: lint, base}) : t("aura.lint", {n: lint})});
  const color = errs ? "red" : (todo.some(x=>x.bad) ? "amber" : "green");
  return {color, todo};
}
function renderOverview(){
  const box = $("#towers"); box.innerHTML = "";
  if (!S.state.projects.length){
    box.append(el("div",{class:"card",style:"padding:26px"},
      el("b",{}, t("overview.none_title")),
      el("p",{class:"muted",style:"margin:8px 0 0"}, t("overview.none_about"))));
    return;
  }
  S.state.projects.forEach(p=>{
    const h = p.health;
    const pct = h ? (h.stats.pct_verified ?? 0) : null;
    const errs = h ? h.doctor.errors.length : null;
    const lintBad = h && h.lint.baseline !== null && h.lint.errors > h.lint.baseline;
    const why = auraWhy(p);
    const act = el("span",{class:"act"}); act.dataset.path = p.path;
    const tile = el("button",{class:"card tower", style:`--aura:${aura(p)}`,
        title: (why.color === "green" ? t("overview.green") : t("overview.to_green"))
               + why.todo.map(x=>(x.bad ? "✗ " : "✓ ") + x.text).join(" · "),
        "aria-label": t("overview.tile_label", {name: p.name, engine: p.engine}),
        onclick:()=>pick(p)},
      el("div",{class:"row",style:"align-items:flex-start;gap:14px;position:relative"},
        el("div",{style:"flex:1;min-width:0"},
          el("div",{class:"name"}, p.name),
          el("div",{class:"path"}, p.path)),
        h ? el("div",{class:"ring",style:`--p:${Math.max(pct,1.5)}`}, el("span",{}, pct+"%"))
          : el("span",{class:"spin",style:"margin-top:6px"})),
      el("div",{class:"foot"},
        el("span",{class:"chip"+(p.behind?" warn":"")}, t("overview.engine", {v: p.engine})),
        // Пока здоровье считается, место под чипы держим пустыми заглушками: без них
        // карточки подрастают в момент прихода данных, плитки разъезжаются под курсором
        // и клик «выбрать проект» уходит в никуда или в соседний проект.
        h ? el("span",{class:"chip "+(errs?"bad":"ok")},
              errs ? t("overview.blockers", {n: errs}) : t("overview.doctor_clean"))
          // Пока считается: спиннер вместо многоточия — «считаю…» рядом с числами
          // читается как обрезанное значение, а не как состояние.
          : el("span",{class:"chip"}, el("span",{class:"spin"}), " " + t("overview.counting")),
        h ? el("span",{class:"chip "+(lintBad?"bad":"")},
              t("overview.lint", {n: h.lint.errors}))
          : null,
        // Второе число рядом с долей доверия: сколько источников уже разобрано. Доверие
        // считается по знанию, разбор — по документам, и одно без другого читается криво:
        // высокая доля доверия при половине неразобранных источников — не «почти готово».
        // Карточки из одних встреч в долю доверия не входят — их число рядом, отдельно.
        h && h.stats.meetings
          ? el("span",{class:"chip", title: t("overview.meetings_hint")},
               t("overview.meetings", {n: h.stats.meetings}))
          : null,
        h && h.build && h.build.total
          ? el("span",{class:"chip "+((h.build.left||0) ? "warn" : "ok"),
                title: t("overview.sources_hint",
                  {done: h.build.total - (h.build.left||0), total: h.build.total})},
               t("overview.sources", {pct: h.build.pct}))
          : null,
        p.git_branch ? el("span",{class:"chip mono"}, p.git_branch) : null,
        p.dirty ? el("span",{class:"chip warn"}, t("overview.uncommitted", {n: p.dirty}))
                : el("span",{class:"chip ok"}, t("overview.tree_clean")),
        act,
      ));
    tile.id = "tower-"+p.slug;
    box.append(tile);
  });
  drawActivity();
  const g = $("#globalMetrics"); g.innerHTML="";
  g.append(
    metric(S.state.projects.length, t("overview.projects"), "", "ok"),
    metric(S.state.projects.filter(p=>p.behind).length,
      t("overview.behind", {v: S.state.kit.version}),
      S.state.projects.some(p=>p.behind) ? t("overview.behind_click") : t("overview.all_current"),
      S.state.projects.some(p=>p.behind)?"warn":"ok",
      S.state.projects.some(p=>p.behind) ? updateAllProjects : null),
    metric(S.state.env.items.filter(i=>!i.ok).length, t("overview.missing"),
      t("overview.missing_hint"), S.state.env.items.some(i=>!i.ok)?"warn":"ok"),
    metric(S.state.commands.length, t("overview.commands"), t("overview.commands_hint"), "ok"),
  );
}
// Обновить движок сразу во всех отставших проектах. Проектов на машине десятки, и по
// одному их не обновляют: отставший движок ломает маршрут на середине, объявив
// предыдущие шаги успешными, — но пока обновление стоит десяти кликов на проект, оно
// не делается вовсе.
//
// Сначала предпросмотр: человек видит поимённо, что именно тронется, и лишь потом
// подтверждает. Обновление переписывает движок в чужих папках — молча такое не делают.
async function updateAllProjects(){
  const behind = (S.state.projects || []).filter(p => p.behind);
  if (!behind.length) return;
  const dry = await api("/api/update-all", {method:"POST", body: JSON.stringify({})});
  if (!dry || dry.error) return toast((dry && dry.error) || t("overview.preview_failed"), "warn");
  const lines = (dry.projects || []).map(r => `• ${r.name} (${r.was} → ${dry.kit})`).join("\n");
  if (!confirm(t("overview.update_ask",
                 {kit: dry.kit, n: dry.projects.length, list: lines}))) return;
  toast(t("overview.updating"), "ok");
  const res = await api("/api/update-all", {method:"POST", body: JSON.stringify({apply:true})});
  if (!res || res.error) return toast((res && res.error) || t("overview.update_failed"), "warn");
  const bad = (res.projects || []).filter(r => !r.ok);
  toast(bad.length
    ? t("overview.updated_partly", {n: res.updated, bad: bad.length,
                                    names: bad.map(r => r.name).join(", ")})
    : t("overview.updated", {n: res.updated}), bad.length ? "warn" : "ok");
  S.state = await api("/api/state");
  renderOverview();
}

function metric(val, lbl, hint, cls, onclick){
  return el("button",{class:"card metric "+(cls||""), onclick: onclick||null},
    el("div",{class:"val"}, val), el("div",{class:"lbl"}, lbl),
    hint ? el("div",{class:"hint"}, hint) : null);
}

function renderProjBadge(){
  const b = $("#projBadge"), p = S.project;
  b.classList.toggle("none", !p);
  b.style.setProperty("--aura", p ? aura(p) : "");
  $("#projBadge .nm").textContent = p ? p.name : t("badge.none");
  b.title = p ? p.path + t("badge.engine", {engine: p.engine}) : t("badge.hint");
}
$("#projBadge").onclick = ()=>show("overview");
async function pick(p, go=true){
  S.project = p;
  // Журнал запусков тянем СРАЗУ, до тяжёлого здоровья: он читается мгновенно, а
  // здоровье зовёт несколько команд и занимает секунды. Поставить его после — значит
  // не отвязать вовсе.
  S.runs = null; loadRuns();
  renderProjBadge();
  // Автоматика Git: «обновлять при открытии проекта». Решает сервер по настройке проекта,
  // и он же не даёт дёргать сервер чаще раза в десять минут; итог — в журнале раздела «Git».
  api("/api/gitsync/event", {method:"POST", quiet:true,
    body: JSON.stringify({project: p.path, event: "open"})});
  if (go) show("health");
  S.health = null; refreshModules();
  const h = await api("/api/health?project=" + encodeURIComponent(p.path));
  // Пока считалось, человек мог выбрать другой проект: его экран дорисует свой pick,
  // а этот ответ только ложится своему проекту.
  if (!takeHealth(p, h)) return;
  renderOverview(); renderProjBadge();
  // Зеркала, установка и версия рисуются из того же здоровья. Пока оно считалось, вкладка
  // показывала «выберите проект» — и оставалась такой навсегда: перерисовать её было
  // некому, человек видел пустоту при выбранном проекте и уходил искать поломку. Теперь
  // это разделы-модули, и ядро перерисовывает все поднятые разом.
  refreshModules();
  // Настройки проекта — тоже про выбранный проект: без перерисовки вкладка осталась бы
  // с чужими значениями, а правка ушла бы не туда.
  if (S.view === "project") renderProject();
  // журнал запусков — свой у каждого проекта; его отметки стоят и в командах, и в сценариях
  renderHistory();
  // Список артефактов и дерево файлов — тоже про выбранный проект. Наполнял их только
  // переход на вкладку, поэтому выбор проекта, сделанный СТОЯ на «Продуктивности» или
  // «Файлах», оставлял экран пустым навсегда: перерисовать его было некому. Ровно та же
  // беда, что была с зеркалами двумя строками выше, — и человек снова уходил искать
  // поломку там, где её нет.
  if (S.view === "files") renderFiles();
  navBadges(p, h);
  // Пришли в проект, а в нём что-то уже идёт — сказать сразу. Молчание здесь стоило
  // человеку второго маршрута поверх первого.
  liveJobs().then(js=>{ if (js.length){ drawLiveJobs();
    toast(js.length === 1 ? t("overview.live_one", {cmd: js[0].cmd})
                          : t("overview.live_many", {n: js.length, cmd: js[0].cmd}),
          "warn"); }});
}
// Отметка у пункта меню. Раздел мог уже переехать в папку — у модуля кнопка своя
// (`nav-<id>`), у встроенного прежняя (`navMirrors`). Считает отметки по-прежнему ядро:
// числа приходят из здоровья проекта, а раздел, в который не заходили, ещё не загружен
// и сам про себя ничего сказать не может.
function setBadge(view, text, bad){
  const n = $("#nav-" + view)
        || $("#nav" + view.charAt(0).toUpperCase() + view.slice(1));
  if (!n) return;
  n.textContent = text || "";
  n.classList.toggle("bad", !!bad);
}

function navBadges(p, h){
  const errs = h.doctor.errors.length;
  setBadge("health", errs || "", errs > 0);
  const mm = Object.values(h.mirrors||{}).reduce(
    (n, x)=> n + (x.no_state ? 1 : (x.missing||0) + (x.orphan||0)), 0);
  setBadge("mirrors", mm || "", mm > 0);
  setBadge("version", p.behind ? "!" : "", !!p.behind);
  // Упавшая автоматика Git — до следующего удачного запуска: ночной отказ отправки иначе
  // заметили бы, только открыв раздел.
  setBadge("gitsync", p.git_alert ? "!" : "", !!p.git_alert);
}

// Пересчёт здоровья по требованию раздела: раздел не ходит в api сам, потому что
// результат нужен не только ему — от него зависят отметки в меню и Мостик.
async function reloadHealth(){
  const p = S.project;
  if (!p) return null;
  const h = await api("/api/health?project=" + encodeURIComponent(p.path));
  if (h && h.stats && takeHealth(p, h)) navBadges(p, h);
  return S.health;
}

/* ---------------- пересчёт показателей ---------------- */
// Числа движок считает на каждый запрос, панель их не кэширует. Но работу в базе делает
// не только панель: ассистент обогатил базу, человек принял карточки в редакторе — на
// экране всё ещё вчерашние цифры, и понять «что изменилось» не по чему. Отсюда кнопка
// и отметка времени: видно не только число, но и на какой момент оно посчитано.
function stamp(id){
  const s = $(id);
  s.textContent = t("overview.stamp", {time: new Date()
    .toLocaleTimeString(S.lang === "en" ? "en-GB" : "ru-RU",
                        {hour:"2-digit", minute:"2-digit"})});
  s.hidden = false;
}
async function busy(btn, fn){
  const was = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = ""; btn.append(el("span",{class:"spin"}), t("overview.counting"));
  try { await fn(); } finally { btn.disabled = false; btn.innerHTML = was; }
}
$("#refreshOverview").onclick = ()=> busy($("#refreshOverview"), async ()=>{
  // перечитываем и карточки проектов: ветка, незакоммиченное и версия движка меняются
  // ровно от той же работы, что и числа базы
  const path = S.project && S.project.path;
  const st = await api("/api/state");
  if (!st || !st.projects) return;
  S.state = st;
  S.project = path ? (st.projects.find(x=>x.path===path) || null) : null;
  S.health = null;
  renderOverview(); renderProjBadge();
  await prefetchHealth();
  if (S.project && S.project.health) navBadges(S.project, S.project.health);
  refreshModules();
  stamp("#overviewStamp");
  toast(t("overview.recounted", {n: st.projects.length}), "ok");
});

/* ---------------- здоровье ---------------- */
// Карточка дашборда: число, подпись и куда идти. Цвет строгий — красный только там, где
// работа СТОИТ; янтарь — есть работа, но всё функционирует; бирюза — в порядке; серый —
// не измерялось. Доля доверенных цветом не красится никогда: 29% на молодой базе это
// норма, а не авария, и порог здесь плодил бы ложную тревогу.
function metricCard(o){
  const tone = o.tone || "";
  const card = el("div",{class:"card metric" + (o.go ? " clickable" : ""),
    style:"padding:14px;min-width:0", title:o.hint || ""},
    el("div",{class:"muted",style:"font-size:12px"}, o.title),
    el("div",{style:"font-size:26px;font-weight:700;margin:2px 0 4px;line-height:1.1"
              + (tone ? ";color:var(--" + tone + ")" : "")}, String(o.value)),
    el("div",{class:"muted",style:"font-size:12px;line-height:1.35"}, o.sub || ""),
    o.go ? el("div",{class:"chip",style:"margin-top:8px"}, o.go.label) : null);
  if (o.go) card.onclick = o.go.act;
  return card;
}

// Действие карточки: либо запуск маршрута с подтверждением, либо список, с которым надо
// разбираться. Ведём туда, где решают проблему, а не туда, где о ней написано подробнее.
const goRoute = (id, label) => ({label, act: async () => {
  const d = S.scenarios || (S.scenarios = await api("/api/scenarios"));
  const sc = (d.scenarios||[]).find(s=>s.id===id);
  if (!sc) return toast(t("run.not_found_route"), "warn");
  runRoute(sc, true);
}});
const goCmd = (cmd, args, label) => ({label, act: async () => {
  const r = S.state.commands.find(x=>x.cmd===cmd);
  if (!r || !r.runnable) return toast(t("run.cmd_off", {cmd}), "warn");
  const line = (cmd + " " + (args||[]).join(" ")).trim();
  if ((args||[]).includes("--apply") && !confirm(
      t("run.write_ask", {project: S.project.name, line}))) return;
  show("console");
  const res = await api("/api/run", {method:"POST", body: JSON.stringify(
    {project:S.project.path, cmd, args:args||[]})});
  if (res.job) poll(res.job, 0, line);
}});

// Команды движка живут в скрытом разделе «Разработка»: в общем списке они были бы
// шумом — аналитик их не запускает, а показать одно и то же в двух местах значит
// разойтись при первой же правке.
const ENGINE_CMDS = ["kit:skills"];
const isEngineCmd = r => r.ns === "dev" || ENGINE_CMDS.includes(r.cmd);

// Чип исполнителя: скрипт, модель или скрипт с моделью. Живёт в ядре, потому что его
// показывают двое — список команд и окно запуска. Уехал в раздел — и окно запуска
// перестало открываться вовсе: ошибка, которую видно только при нажатии.
function kindChip(kind){
  if (kind === "скрипт") return el("span",{class:"chip ok"}, t("kind.script"));   // данные движка
  if (kind === "модель") return el("span",{class:"chip"}, t("kind.model"));       // данные движка
  return el("span",{class:"chip warn"}, t("kind.both"));
}

// Слева — имя флага, команды или аргумента, как их знает движок; справа — имя надписи
// в каталоге строк. Сама надпись живёт там же, где и весь остальной текст панели.
const DANGER_FLAGS = {
  "--apply": "apply",
  "--allow-dirty": "allow_dirty",
  "--force": "force",
  "--link": "link",
  "--include-raw": "include_raw",
  "--merge": "merge",
  "--prune": "prune",
  "--backup": "backup",
  "--refresh": "refresh",
  "--cards": "cards",
  "--source-older-than": "source_older_than",
};
const dangerFlag = f => DANGER_FLAGS[f] ? t("flag." + DANGER_FLAGS[f]) : "";
// Команды, которые не правят файлы, а удаляют их. Формулировка подтверждения «изменения
// попадут в git diff» для них — неправда: восстанавливать придётся из git, а не читать diff.
const DESTRUCTIVE = {
  "agent:aliases": "aliases",
  "agent:build": "build",
  "kb:reset": "reset",
};
const destructive = cmd => DESTRUCTIVE[cmd] ? t("kill." + DESTRUCTIVE[cmd]) : "";
// Что подставлять в аргумент: короткий пример вместо абстрактного имени переменной.
const ARG_HINTS = {
  "ctx:context": "ctx_context",
  "make:spec-pack": "make_spec_pack",
  "ops:impact": "ops_impact",
  "kb:supersede": "kb_supersede",
  "ship:release": "ship_release",
  "ship:publish": "ship_publish",
  "ship:export": "ship_export",
  "kb:scrub": "kb_scrub",
  "kit:list": "kit_list",
};
const argHint = cmd => ARG_HINTS[cmd] ? t("arg." + ARG_HINTS[cmd]) : "";

let RUN = null;
function openRun(cmdName, preFlags=[]){
  // Команду разработки открывают объектом из раздела «Разработка»: ей проект не нужен,
  // она работает в дереве кита.
  const byObj = typeof cmdName === "object" ? cmdName : null;
  if (byObj) cmdName = byObj.cmd;
  const dev = isEngineCmd(byObj || S.state.commands.find(x=>x.cmd===cmdName) || {});
  if (!dev && !S.project){ toast(t("run.pick_project"), "warn"); show("overview"); return; }
  const r = S.state.commands.find(x=>x.cmd===cmdName);
  if (!r) return toast(t("run.cmd_not_found"), "err");
  if (!r.runnable){ openDoc("skills/aurora-vault/references/"+r.impl); return; }
  RUN = {row:r, flags:new Set(preFlags), args:"", vals:{}};
  // Кнопка в консоли применяет ровно тот набор флагов, который был у предпросмотра.
  // Открыли другую команду — набор сменился, старой кнопке верить нельзя.
  PENDING_APPLY = null; LAST_TASKS = []; drawTaskButton();
  $("#consoleApply").innerHTML = "";
  drawRun();
  $("#runOverlay").classList.add("on");
  setTimeout(()=>$("#runDrawer").querySelector("button,input")?.focus(),40);
}
// Флаг со значением (`--jql JQL`, `--format {docx,pdf}`) без поля ввода превращался
// в «expected one argument»: панель отправляла его голым. Поле появляется вместе с галкой.
function flagLine(f, hint, meta, required){
  const choices = meta.startsWith("{") ? meta.slice(1,-1).split(/[\s,]+/).filter(Boolean) : null;
  let field = null;
  if (meta){
    if (choices){
      if (!RUN.vals[f]) RUN.vals[f] = choices[0];
      field = el("select",{class:"btn sm",style:"font-weight:400",
        onchange:e=>RUN.vals[f]=e.target.value},
        ...choices.map(c=>el("option",{value:c, selected:RUN.vals[f]===c?"":null}, c)));
    } else {
      field = el("input",{class:"btn sm",style:"width:100%;font-weight:400",
        placeholder: meta + (meta.includes("...") ? t("flag.space_separated") : ""),
        value: RUN.vals[f] || "", oninput:e=>RUN.vals[f]=e.target.value});
    }
    field = el("div",{style:"margin:2px 0 8px 26px;display:"+(RUN.flags.has(f)?"block":"none")}, field);
  }
  const cb = el("input",{type:"checkbox", checked: RUN.flags.has(f)?"":null,
    disabled: required ? "" : null,
    onchange:e=>{
      e.target.checked ? RUN.flags.add(f) : RUN.flags.delete(f);
      if (field) field.style.display = e.target.checked ? "block" : "none";
    }});
  return el("div",{},
    el("label",{class:"flagline"}, cb,
      el("span",{},
        el("span",{class:"mono",style:"font-weight:600"}, f + (meta ? " " + meta : "")),
        required ? el("span",{class:"chip warn",style:"margin-left:6px"},
          t("flag.required")) : null,
        hint ? el("span",{class:"muted",style:"font-size:12.5px"}, " — " + hint) : null,
        dangerFlag(f) ? el("div",{class:"muted",style:"font-size:11.5px;color:var(--tier-inreview)"},
          dangerFlag(f)) : null)),
    field);
}
function drawRun(){
  const r = RUN.row, d = $("#runDrawer");
  const writes = r.flags.includes("--apply");
  const outward = ["sync:confluence","sync:jira","ship:publish","ship:export"].includes(r.cmd);
  d.innerHTML="";
  d.append(
    el("div",{class:"row"}, el("span",{class:"mono",style:"font-size:17px;font-weight:800"}, r.cmd),
      kindChip(r.kind), writes?el("span",{class:"chip warn"}, t("run.writes_files")):null,
      outward?el("span",{class:"chip gold"}, t("run.changes_outside")):null),
    el("p",{class:"muted",style:"font-size:13.5px;margin:10px 0 4px",html:tick(r.what)}),
    el("div",{class:"mono",style:"font-size:12px;color:var(--text-muted)"},
      ".aurora/scripts/" + r.impl + (r.args? " " + r.args : "")));
  // Список флагов панель читает из kit'а, а запускает копию скрипта из движка проекта.
  // Пока движок не обновлён, новый флаг существует только на экране: скрипт ответит
  // «unrecognized argument» и кодом 2, и выглядит это как поломка панели.
  if (S.project && S.project.behind && !r.from_kit){
    d.append(el("div",{class:"warnbox"},
      t("run.engine_gap", {engine: S.project.engine, kit: S.project.kit})));
  }
  if (r.args){
    const opt = r.args.trim().startsWith("[");
    d.append(el("div",{style:"margin-top:14px"},
      el("div",{class:"muted",style:"font-size:12px;margin-bottom:6px"},
        (opt ? t("run.arg_optional") : t("run.arg_required")) + r.args +
        (argHint(r.cmd) ? " — " + argHint(r.cmd) : "")),
      el("input",{class:"btn",style:"width:100%;font-weight:400",
        placeholder: argHint(r.cmd) || r.args,
        oninput:e=>RUN.args=e.target.value})));
  }
  const flags = r.flags.filter(f=>f!=="--apply");
  if (flags.length){
    const help = r.flag_help || {}, meta = r.flag_args || {};
    const req = r.flag_required || [];
    req.forEach(f => RUN.flags.add(f));      // обязательное включено заранее
    d.append(el("div",{style:"margin-top:16px"},
      el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"},
        t("run.flags_off")),
      ...flags.map(f=>flagLine(f, help[f], meta[f] || "", req.includes(f)))));
  }
  if (writes){
    const kill = destructive(r.cmd);
    d.append(el("div",{class:"warnbox" + (kill ? " dangerbox" : "")},
      (kill ? kill + t("run.preview_first") : t("run.can_write"))
      + t("run.apply_later")));
  }
  if (outward){
    // какой токен нужен команде, знает модуль источника, а не панель
    const inst = ((S.health && S.health.sources || {}).instances || [])
      .find(i=>i.command && r.cmd.startsWith(i.command));
    const tok = inst ? (S.project.tokens||[]).includes(inst.env_prefix)
                     : S.project.confluence_token;
    if (!tok) d.append(el("div",{class:"warnbox dangerbox"},
      t("run.no_token")));
  }
  d.append(el("div",{class:"row",style:"margin-top:20px"},
    el("button",{class:"btn primary",onclick:()=>fire(false)},
      writes ? t("run.preview") : t("run.go")),
    el("div",{class:"spacer"}),
    el("button",{class:"btn sm",onclick:closeRun}, t("run.close"))));
}
// Флаги меняют то, что именно уйдёт (`--keep-handmade` у kb:reset), поэтому в вопросе
// стоит вся строка запуска, а не одно имя команды.
function applyAsk(line){
  const kill = destructive(line.split(" ")[0]);
  if (!S.project) return t("apply.ask_engine", {line});   // команда движка, не проекта
  return kill
    ? t("apply.ask_delete", {project: S.project.name, line, why: kill})
    : t("apply.ask_write", {project: S.project.name, line});
}
function closeRun(){ $("#runOverlay").classList.remove("on"); }
$("#runOverlay").onclick = e => { if (e.target.id==="runOverlay") closeRun(); };

// Предпросмотр закрывает ящик команды, а открыть его заново — значит собрать флаги
// с нуля. Поэтому «Применить» живёт в консоли: там виден вывод предпросмотра, и
// применяется ровно та же строка запуска плюс --apply.
let PENDING_APPLY = null;

async function fire(apply){
  const r = RUN.row;
  const args = [];
  const meta = r.flag_args || {};
  if (RUN.args.trim()) args.push(...RUN.args.trim().split(/\s+/));
  for (const f of RUN.flags){
    const m = meta[f] || "", v = (RUN.vals[f] || "").trim();
    if (m && !v && !m.startsWith("[")){
      // отправить флаг без значения — значит получить «expected one argument» уже в консоли
      toast(t("run.flag_needs_value", {flag: f, meta: m}), "warn"); return;
    }
    args.push(f);
    if (v) m.includes("...") ? args.push(...v.split(/\s+/)) : args.push(v);
  }
  if (apply) args.push("--apply");
  if (apply && await busyElsewhere(r.cmd)) return;
  closeRun(); show("console");
  PENDING_APPLY = null; LAST_TASKS = []; drawTaskButton();
  $("#consoleApply").innerHTML = "";
  const out = $("#consoleOut"); out.innerHTML="";
  const line = (r.cmd + " " + args.join(" ")).trim();
  // Номер попытки: 1 для свежего запуска, а для повтора того же шага после неудачи
  // (прошлый прогон того же cmd и тех же аргументов кончился кодом 2 и выше) — прежний + 1.
  const prev = S.lastStep;
  const n = (prev && prev.failed && prev.cmd === r.cmd && JSON.stringify(prev.args) === JSON.stringify(args))
    ? prev.n + 1 : 1;
  $("#consoleCmd").textContent = (n > 1 ? t("run.attempt", {n}) : "") + line;
  $("#consoleRc").textContent = t("run.running");
  $("#consoleRc").className = "chip";
  const res = await api("/api/run", {method:"POST", body: JSON.stringify(
    {project: (isEngineCmd(r) ? "" : S.project.path), cmd:r.cmd, args})});
  if (!res.job) return;
  S.lastStep = {cmd:r.cmd, args, n, failed:false, job:res.job};
  const label = (n > 1 ? t("run.attempt", {n}) : "") + line;
  if (!apply && r.flags.includes("--apply"))
    PENDING_APPLY = {line, job:res.job};
  poll(res.job, 0, label);
}
/* Кнопка «Попробовать снова» живёт только для настоящей ошибки команды — код 2 и выше.
   Отрицательный код — процесс убит сигналом (человек «Прервать» или система): это не
   ошибка команды, предлагать повтор нечего. Код 1 («есть что поправить») — команда
   отработала и нашла работу, это успех. Условие строгое: rc >= 2, без доп. флагов остановки.
   Повтор не трогает ящик команды: берёт строку запуска из сохранённого шага и шлёт её
   новым заданием; номер попытки растёт с каждым неудачным повтором. */
async function retryStep(){
  const st = S.lastStep;
  if (!st) return;
  if (st.args.includes("--apply") && await busyElsewhere(st.cmd)) return;
  show("console");
  PENDING_APPLY = null; LAST_TASKS = []; drawTaskButton();
  $("#consoleApply").innerHTML = "";
  const out = $("#consoleOut"); out.innerHTML = "";
  const line = (st.cmd + " " + st.args.join(" ")).trim();
  const n = st.n + 1;
  const label = t("run.attempt", {n}) + line;
  $("#consoleCmd").textContent = label;
  $("#consoleRc").textContent = t("run.running");
  $("#consoleRc").className = "chip";
  const res = await api("/api/run", {method:"POST", body: JSON.stringify(
    {project: (isEngineCmd(S.state.commands.find(x=>x.cmd===st.cmd) || {}) ? "" : S.project.path),
     cmd:st.cmd, args:st.args})});
  if (!res.job) return;
  st.n = n; st.failed = false; st.job = res.job;
  poll(res.job, 0, label);
}
/* ---------------- артефакты проекта ----------------

   Шаблоны у проектов разные, и знание «чем и куда» до сих пор жило в голове аналитика.
   Реестр лежит в aurora.config.yaml проекта — в git, виден любой IDE и ассистенту через
   MCP; панель здесь только удобный ввод, а не второе хранилище. Виды документов заводит
   человек: движок не знает наперёд, какие формы потребует заказчик. */
let KINDS = null;

async function renderKinds(box){
  if (!S.project) return;
  const d = await api("/api/kinds?project=" + encodeURIComponent(S.project.path));
  if (d.error) return;
  KINDS = JSON.parse(JSON.stringify(d.kinds || {}));
  const card = el("div",{class:"card",style:"padding:20px"});
  const draw = () => {
    card.innerHTML = "";
    card.append(el("p",{class:"muted",style:"font-size:13px;margin:0 0 10px"},
      t("kinds.about")));
    (d.problems||[]).forEach(p=>card.append(el("div",{class:"warnbox",style:"margin-bottom:8px"},
      `${p.kind}: ${p.why}`)));
    const rows = el("div",{});
    Object.keys(KINDS).sort().forEach(kind=>{
      const rec = KINDS[kind];
      const field = (key, ph, width) => el("input",{class:"btn mono",
        style:`width:${width};font-size:12px`, value:rec[key]||"", placeholder:ph,
        oninput:e=>{ rec[key] = e.target.value;
                     setDirty("kinds", t("dirty.kinds"), true); }});
      // Свойства задачи держим свёрнутыми: они нужны не всем типам и не каждый день,
      // а развёрнутый список из шести полей у девяти типов превращает форму в стену.
      rec.task = rec.task || {};
      const tfield = (key, ph, width) => el("input",{class:"btn mono",
        style:`width:${width};font-size:12px`,
        value: Array.isArray(rec.task[key]) ? rec.task[key].join(", ") : (rec.task[key]||""),
        placeholder:ph,
        oninput:e=>{ rec.task[key] = e.target.value;
                     setDirty("kinds", t("dirty.kinds"), true); }});
      const taskBox = el("div",{hidden:true,style:"width:100%;padding:8px 0 4px 26px"},
        el("div",{class:"muted",style:"font-size:12px;margin-bottom:6px"},
          t("kinds.task_about")),
        el("div",{class:"row",style:"gap:8px;flex-wrap:wrap"},
          tfield("project", t("kinds.f_project"), "120px"),
          tfield("type", t("kinds.f_type"), "130px"),
          tfield("assignee", t("kinds.f_assignee"), "150px"),
          tfield("epic", t("kinds.f_epic"), "120px"),
          tfield("labels", t("kinds.f_labels"), "190px"),
          tfield("components", t("kinds.f_components"), "190px")));
      rows.append(el("div",{class:"list-item",style:"gap:8px;flex-wrap:wrap"},
        el("span",{class:"chip mono",style:"flex:none"}, kind),
        field("title", t("kinds.f_title"), "170px"),
        field("template", t("kinds.f_template"), "190px"),
        field("prompt", t("kinds.f_prompt"), "180px"),
        field("out", t("kinds.f_out"), "150px"),
        field("publish_url", t("kinds.f_publish"), "230px"),
        field("mcp", t("kinds.f_mcp"), "210px"),
        // Правило включается видом документа, а не глобально: у ОПЗ стек — это предмет,
        // и общее правило заставило бы критика ругаться на каждый такой документ.
        el("label",{class:"row",style:"gap:6px;font-size:12px;flex:none",
              title: t("kinds.tech_hint")},
          el("input",{type:"checkbox",
            checked:(String(rec.tech_agnostic||"").toLowerCase()==="true")?"":null,
            onchange:e=>{ rec.tech_agnostic = e.target.checked ? "true" : "";
                          setDirty("kinds", t("dirty.kinds"), true); }}),
          t("kinds.tech_agnostic")),
        el("button",{class:"btn sm", title: t("kinds.task_hint"),
          onclick:e=>{ taskBox.hidden = !taskBox.hidden;
                       e.target.textContent = taskBox.hidden ? t("kinds.task_open")
                                                             : t("kinds.task_close"); }},
          t("kinds.task_open")),
        el("button",{class:"btn sm", title: t("kinds.drop"),
          onclick:()=>{ delete KINDS[kind]; setDirty("kinds", t("dirty.kinds"), true);
                        draw(); }},"×"),
        taskBox));
    });
    card.append(rows);
    const name = el("input",{class:"btn mono",style:"width:150px",
      placeholder: t("kinds.new_ph")});
    card.append(el("div",{class:"row",style:"gap:10px;margin-top:12px;flex-wrap:wrap"},
      name,
      el("button",{class:"btn sm",onclick:()=>{
        const key = (name.value||"").trim().toLowerCase();
        if (!/^[a-z][a-z0-9-]{1,30}$/.test(key))
          return toast(t("kinds.bad_name"), "warn");
        if (KINDS[key]) return toast(t("kinds.exists"), "warn");
        KINDS[key] = {title:"", template:"", prompt:"", out:"Artifacts/" + key,
                      publish_url:"", mcp:"", tech_agnostic:"", task:{}};
        name.value = ""; setDirty("kinds", t("dirty.kinds"), true); draw();
      }}, t("kinds.add")),
      saveButton("kinds", t("dirty.kinds"), t("kinds.save"), async ()=>{
        const r = await api("/api/kinds",{method:"POST",
          body:JSON.stringify({project:S.project.path, kinds:KINDS})});
        if (r.ok){ toast(t("kinds.saved", {file: r.target.split("/").pop(), n: r.kinds}));
                   setDirty("kinds", t("dirty.kinds"), false);
                   renderProject(); }
        else toast(r.error || t("kinds.not_saved"), "err");
      }),
      el("span",{class:"muted",style:"font-size:12px"}, t("kinds.out_note"))));
    if ((d.templates||[]).length)
      card.append(el("div",{class:"muted",style:"font-size:12px;margin-top:10px"},
        t("kinds.templates", {list: d.templates.slice(0,12).join(", ")})
        + (d.templates.length > 12 ? t("kinds.templates_more", {n: d.templates.length}) : "")));
  };
  draw();
  sgroup(box, "project:kinds", t("kinds.title", {project: S.project.name})).append(card);
}

/* ---------------- приёмка карточек ----------------

   Линтер и раньше называл эти карточки поимённо, но список жил в тексте отчёта: чтобы
   пройти его, человек копировал имена в редактор по одному. Здесь тот же список — с
   текстом карточки, диффом и двумя кнопками. Движок не научился новому: ему дали руки. */

/* ---------------- отчёты ----------------

   Панель здесь не считает ничего сама: состояние берёт у движка (`/api/report`), а
   сборку отдаёт обычному заданию (`ops:report`) — тому же, что запускается из
   «Команд». Так консоль и журнал видят прогон отчёта наравне с остальными. */

const ago = ts => {
  if (!ts) return "—";
  const m = Math.floor((Date.now()/1000 - ts) / 60);
  if (m < 1) return t("ago.now");
  if (m < 60) return t("ago.min", {n: m});
  const h = Math.floor(m/60);
  if (h < 24) return t("ago.hour", {n: h});
  return new Date(ts*1000).toLocaleDateString(S.lang === "en" ? "en-GB" : "ru-RU");
};
const kb = b => b >= 1048576 ? t("size.mb", {n: (b/1048576).toFixed(1)})
                             : t("size.kb", {n: Math.round(b/1024)});


/* ---------------- спросить базу ----------------

   Диалог с базой на месте: раньше человек шёл в чужой чат, а туда надо было ещё
   донести контекст. Контекст собирает движок (`ctx:context`), отвечает модель проекта.

   Разговор хранит не панель, а база: `meta/ask/<разговор>.md` в проекте, и он уходит в
   git вместе с карточками. История вопросов, которая живёт до перезагрузки страницы, —
   это не история: второй аналитик задаёт те же вопросы заново, а разговор, показавший
   пробел в базе, теряется вместе с вкладкой. Здесь же он остаётся основанием завести
   знание — и читается в Obsidian, как всё остальное.

   Уточнение — тот же вопрос с контекстом разговора: «а если он ИП?» само по себе не
   находит в базе ничего, тему держит предыдущий вопрос. Поэтому уточнения идут в тот же
   файл, и движок собирает пак по всему разговору. */
function mdLite(s){
  const esc = (x)=>x.replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  return esc(s)
    .replace(/\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]/g,
             (_m,a,b)=>`<span class="chip" title="${t("mdlite.card")}">${b||a}</span>`)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/_([^_]+)_/g, "<i>$1</i>")
    .replace(/^---$/gm, "<hr>");
}

/* ---------------- маршрут: сценарий одной кнопкой ----------------

   Цикл обновления базы — четырнадцать команд, и почти каждая сначала показывает
   план, а потом просит нажать ещё раз с --apply. Работы там пятнадцать секунд, а
   кликов — под тридцать, и порядок всегда один и тот же. Панель знает и шаги, и
   флаги: пусть проходит их сама, а человек смотрит и вмешивается, когда надо.

   Код возврата 1 — это «команда отработала и нашла, что чинить», он маршрут не
   останавливает. Останавливает 2 и выше: команда не отработала, и следующие шаги
   пойдут по неверному состоянию. */
let ROUTE = null;

/* ---------------- задания, которые идут прямо сейчас ----------------

   Задание живёт в процессе панели, а консоль — в открытой странице. Перезагрузили её
   (или обновили kit и открыли заново) — и работающая команда становится невидимой:
   консоль пуста, будто ничего не запускалось. Человек делает единственный разумный
   вывод — «оборвалось» — и запускает второй маршрут поверх первого.

   Очереди у панели нет и не будет: каждый запуск — это отдельный процесс, они идут
   параллельно. Два синка или два агента на одном проекте не встанут друг за другом,
   они подерутся за git и за зеркало. Поэтому живые задания видно, к ним можно
   подключиться, а второй запуск поверх первого панель переспрашивает. */

async function liveJobs(){
  if (!S.project) return [];
  const d = await api("/api/jobs?project=" + encodeURIComponent(S.project.path),
                      {quiet:true});
  return (d && d.jobs) || [];
}

// Провайдер упал, агент ушёл на медленного запасного и держит основного «в дауне»
// 15 минут — иначе каждый источник начинается с минуты ожидания мёртвого шлюза. Кнопка
// говорит «он вернулся, проверь сейчас»: агент подхватит на следующем источнике.
$("#retryPrimary").onclick = async ()=>{
  const r = await api("/api/agent/retry-primary", {method:"POST", body:"{}"});
  if (r && r.ok) toast(r.note || t("console.retry_primary"), "ok");
};

async function drawLiveJobs(){
  const box = $("#consoleLive");
  if (!box) return;
  const jobs = await liveJobs();
  box.replaceChildren();
  jobs.forEach(j=>{
    const mins = Math.round((Date.now()/1000 - j.started) / 60);
    box.append(el("div",{class:"card row",
        style:"padding:10px 12px;margin-bottom:10px;gap:12px;align-items:center"},
      el("div",{style:"flex:1;min-width:0"},
        el("div",{style:"font-weight:600"}, t("live.now", {cmd: j.cmd})
          + (j.args||[]).join(" ")),
        el("div",{class:"muted",style:"font-size:12px;margin-top:2px"},
          t("live.details", {mins, lines: j.lines}))),
      el("button",{class:"btn", onclick:()=>attachJob(j.id)}, t("live.show"))));
  });
  if (jobs.length) box.append(el("div",{class:"muted",style:"font-size:12.5px;margin-bottom:10px"},
    t("live.route_warning")));
}

async function attachJob(id){
  show("console");
  const out = $("#consoleOut"); out.innerHTML = "";
  $("#consoleCmd").textContent = t("job.attached", {id});
  let since = 0;
  for (;;){
    const d = await api(`/api/job?id=${id}&since=${since}`, {quiet:true});
    if (!d || d.error) { $("#consoleRc").textContent = t("job.not_found"); return; }
    (d.lines||[]).forEach(l=>consoleLine(out, l));
    watchScroll(out); stickToBottom(out); since = d.next;
    $("#consoleCmd").textContent = d.cmd + " " + (d.args||[]).join(" ");
    if (d.done){
      $("#consoleRc").textContent = t("run.rc", {rc: d.rc});
      $("#consoleRc").className = "chip " + (failed(d.rc) ? "bad" : d.rc === 1 ? "warn" : "ok");
      drawLiveJobs();
      return;
    }
    $("#consoleRc").textContent = t("run.running");
    await new Promise(ok=>setTimeout(ok, 450));
  }
}

// Второй запуск поверх работающего — не очередь, а гонка. Спрашиваем до, а не объясняем
// после: два `agent:build` на одном проекте дерутся за git-чекпойнт и портят откат.
async function busyElsewhere(what){
  const jobs = await liveJobs();
  if (!jobs.length) return false;
  const list = jobs.map(j=>"  " + j.cmd + " " + (j.args||[]).join(" ")).join("\n");
  return !confirm(t("job.busy_ask", {list, what}));
}

// Консоль прокручивается сама, пока человек смотрит конец. Стоит ему отмотать вверх —
// перестаёт: он читает, когда всё началось, или ищет, где кольцо переключилось на другую
// модель, а страница каждые полсекунды возвращала его в конец. Возвращается к слежению
// само, когда он домотает обратно вниз.
const NEAR_BOTTOM = 40;   // px: «внизу» — не пиксель в пиксель, иначе слежение теряется
// Молчание шага: команда жива, но за две минуты не выдала ни строки. Скорее всего, зависла —
// но убивать её нельзя: честная `kb:build` идёт часами. Предупреждаем один раз и ждём дальше;
// «Прервать» — уже решение человека, а не автомата.
const SILENCE_MS = 120000;
// Ожидание сети, обороты и застой маршрута считает сервер (`cockpit/route_runner.py`):
// маршрут идёт в процессе панели, а страница только показывает его журнал.
function stickToBottom(box){
  if (box.dataset.follow === "0") return;
  box.scrollTop = box.scrollHeight;
}
function followAgain(box){
  box.dataset.follow = "1";
  box.scrollTop = box.scrollHeight;
  const hint = $("#consoleTail"); if (hint) hint.hidden = true;
}
function watchScroll(box){
  if (box.dataset.watched) return;
  box.dataset.watched = "1"; box.dataset.follow = "1";
  box.addEventListener("scroll", ()=>{
    const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight <= NEAR_BOTTOM;
    box.dataset.follow = atEnd ? "1" : "0";
    const hint = $("#consoleTail");
    if (hint) hint.hidden = atEnd;
  });
}

function armStop(jobId){
  // Кнопка «Прервать» — одна на все способы запустить команду. Была повешена только на
  // одиночный запуск, а маршрут опрашивает задание своим циклом: именно он идёт часами,
  // и именно там кнопки не оказалось.
  const b = $("#consoleStop");
  if (!b) return;
  b.hidden = !jobId;
  if (!jobId){ b.onclick = null; return; }
  b.onclick = async () => {
    if (!confirm(t("console.stop_ask"))) return;
    const r = await api("/api/job/stop", {method:"POST", quiet:true,
      body: JSON.stringify({id: jobId})});
    if (r.error) toast(r.error, "warn");
    else toast(t("console.stopped"));
  };
}

async function runStep(cmd, args){
  const r = S.state.commands.find(x=>x.cmd===cmd);
  if (!r || !r.runnable) return {rc:0, skipped:true, lines:[]};
  const res = await api("/api/run", {method:"POST", body: JSON.stringify(
    {project: (isEngineCmd(r) ? "" : S.project.path), cmd, args, route:true})});
  const out = $("#consoleOut");
  if (!res.job){
    // Сервер отказал маршруту (например, движок проекта отстал от кита). Шагу остаётся
    // вернуть код 2, чтобы маршрут встал здесь, а не прошёл половину пути по половине
    // движка. Причина — в консоль и в события шага, а не только во всплывающее окно на
    // четыре секунды: в PRJ-A 21.09 sync:confluence дважды «не отработал» с кодом 2, и
    // по архиву нельзя было понять, что команда даже не запускалась.
    const why = t("step.not_started", {why: res.error || t("step.refused")});
    out.append(el("div",{class:"err"}, "■ " + why));
    return {rc:2, lines:[why], refused: why};
  }
  let since = 0, lines = [];
  let lastLineAt = Date.now(), silenceShown = false;   // слежение за тишиной шага, см. SILENCE_MS
  armStop(res.job);
  try {
    for (;;){
      const d = await api(`/api/job?id=${res.job}&since=${since}`, {quiet:true});
      if (!d || d.error){
        out.append(el("div",{class:"err"}, t("step.job_lost")));
        return {rc:2, lines};      // код 2 — маршрут останавливается, и это правильно
      }
      (d.lines||[]).forEach(l=>{ lines.push(l); consoleLine(out, l); });
      if (d.lines && d.lines.length){ lastLineAt = Date.now(); silenceShown = false; }
      watchScroll(out); stickToBottom(out);
      since = d.next;
      if (d.done) return {rc:d.rc, lines};
      // Жива, но молчит дольше порога — показываем предупреждение один раз за «эпизод тишины».
      // Само задание не трогаем: честный длинный шаг не должен умирать по таймеру.
      if (!silenceShown && Date.now() - lastLineAt > SILENCE_MS){
        silenceShown = true;
        out.append(el("div",{class:"warn"}, t("step.silent")));
      }
      await new Promise(ok=>setTimeout(ok, 450));
    }
  } finally {
    armStop(null);
  }
}

// Что именно нашла команда и чем это чинят. «Отработала и нашла, что чинить» —
// честно, но бесполезно: человек всё равно идёт листать консоль. Заголовки разделов
// отчёта («## битые ссылки: 20») — это и есть находки, а лечение известно заранее.
// Лечение, которое панель умеет запустить сама. Подсказка словами («kb:repair --links»)
// оставляет человека наедине с поиском: где эта команда, какие у неё галочки, что из
// написанного — флаг. Здесь то же самое, но кнопкой. Всё, что требует суждения (перенести
// карточку, слить двойников, отдать раздел движку с --force), кнопки не получает.
// Слева — слова самого движка: так их печатают lint и аудит.
const FIX_RUN = {
  "битые ссылки": {cmd:"kb:repair", args:["--links", "--apply", "--allow-dirty"]},  // данные движка
  "заготовки": {cmd:"kb:repair", args:["--stubs", "--apply", "--allow-dirty"]},     // данные движка
  "оглавление отстало": {cmd:"kb:index", args:["--apply"]},                         // данные движка
  "MISSING": {cmd:"sync:confluence", args:[]},
};

function fixButton(what){
  const key = Object.keys(FIX_RUN).find(k=>what.toLowerCase().includes(k.toLowerCase()));
  if (!key) return null;
  const fix = FIX_RUN[key];
  const r = S.state.commands.find(x=>x.cmd===fix.cmd);
  if (!r || !r.runnable) return null;
  return el("button",{class:"btn sm", onclick:async ()=>{
    if (!confirm(t("fix.ask", {cmd: fix.cmd, args: fix.args.join(" ")}))) return;
    if (await busyElsewhere(fix.cmd)) return;
    const {rc} = await runStep(fix.cmd, fix.args);
    toast(failed(rc) ? t("fix.failed", {cmd: fix.cmd, rc}) : t("fix.done", {cmd: fix.cmd}),
          failed(rc) ? "err" : "ok");
  }}, t("fix.button", {cmd: fix.cmd}));
}
// Слева — слова самого движка (так их печатают lint и аудит), справа — совет человеку.
// Первое не переводится и остаётся как есть, второе живёт в каталоге строк.
const FIX_HINT = {
  "битые ссылки": "kb:repair --links",                        // данные движка
  "одинаковые alias": "@aliases",                             // данные движка
  "правка после приёмки": "@trust",                           // данные движка
  "артефакты, попавшие в базу знаний": "@artifacts",          // данные движка
  "тип не по разделу": "@type",                               // данные движка
  "карточки-двойники": "kb:dedupe",                           // данные движка
  "оглавление отстало": "@index",                             // данные движка
  "ORPHAN": "@orphan",
  "MISSING": "@missing",
  "COLLISION": "@collision",
};
// Слова самого движка на экране: так печатают линтер, статистика и аудит. Ключом остаются они
// (по ним панель ищет числа), а подпись человеку берётся из каталога строк.
const ENGINE_WORDS = {
  "битые ссылки": "word.broken_links",  // данные движка
  "оглавления отстали от базы": "word.index_behind",  // данные движка
  "контрольные вопросы без карточки-источника": "word.golden_orphan",  // данные движка
  "одинаковые alias у разных карточек": "word.dup_alias",  // данные движка
  "карточки без связей": "word.no_links",  // данные движка
  "поле шапки попало в текст карточки": "word.header_leak",  // данные движка
  "статус вне шкалы": "word.bad_status",  // данные движка
  "карточки без шапки": "word.no_header",  // данные движка
  "артефакты, попавшие в базу знаний": "word.artifacts_in_kb",  // данные движка
  "карточки без типа": "word.no_type",  // данные движка
  "тип не по разделу": "word.type_mismatch",  // данные движка
  "прочее": "word.other",  // данные движка
  "доверенные": "word.trusted",  // данные движка
  "из встреч (вне доли)": "word.meetings",  // данные движка
  "связей с задачами нет": "word.no_tasks",  // данные движка
  "задачи ещё в работе": "word.tasks_open",  // данные движка
  "доверие не считалось": "word.not_computed",  // данные движка
  "(нет status)": "word.no_status",  // данные движка
  "(нет kind)": "word.no_kind",  // данные движка
  "ПОСТОРОННИЕ": "word.foreign",  // данные движка
};
const engineWord = n => ENGINE_WORDS[n] ? t(ENGINE_WORDS[n]) : n;

// Совет — либо готовая строка команды, либо надпись из каталога (собачка впереди).
const fixHint = v => v && v[0] === "@" ? t("fix.hint." + v.slice(1)) : v;

function findings(lines){
  const out = [];
  const add = (what, n) => {
    if (!n || n === "0") return;
    const hint = Object.keys(FIX_HINT).find(k=>what.toLowerCase().includes(k.toLowerCase()));
    out.push({what: what.trim(), n, fix: hint ? fixHint(FIX_HINT[hint]) : ""});
  };
  lines.forEach(l=>{
    const head = l.match(/^##+\s*(.+?):\s*\*{0,2}(\d+)\*{0,2}\s*$/);
    if (head) return add(head[1], head[2]);
    // Аудит зеркал печатает все счётчики одной строкой:
    // «- MISSING: **0** · MOVED: **0** · ORPHAN: **27** · …». Разбираем каждый.
    if (/^-\s*(MISSING|MOVED|ORPHAN|CASE|COLLISION|ПОСТОРОННИЕ)/.test(l))
      [...l.matchAll(/([A-ZА-Я]{4,}):\s*\*\*(\d+)\*\*/g)].forEach(m=>add(m[1], m[2]));
  });
  // одна строка на находку: у аудита те же счётчики повторяются по каждому зеркалу
  const seen = new Set();
  return out.filter(x => !seen.has(x.what + x.n) && seen.add(x.what + x.n)).slice(0, 8);
}

// Полоса хода: «шаг 9 из 16 · agent:build». Пусто — маршрут кончился, полоса прячется.
// Отрицательный код — процесс убит сигналом: человек нажал «Прервать» или система
// его сняла. Проверка «код 2 и выше» такое пропускала, и маршрут после осознанного
// прерывания шёл дальше — ровно то, чего человек и не хотел.
const failed = rc => rc >= 2 || rc < 0;

function drawRouteBar(bar0){
  const bar = $("#routeBar");
  if (!bar) return;
  const b = bar0 || {};
  if (!b.cmd || !ROUTE){ bar.hidden = true; bar.textContent = ""; return; }
  bar.hidden = false;
  bar.innerHTML = "";
  bar.append(el("span",{}, b.lap
      ? t("route.bar_lap", {lap: b.lap, step: b.inLap, of: b.cycleSize})
      : t("route.bar_step", {step: b.done, of: b.total})),
             el("span",{class:"rb-dim"}, " · " + b.cmd));
}

/* Строка консоли — по виду записи журнала. Вывод шага красим по маркерам самого движка
   (`lineClass`), ход маршрута — по смыслу: заголовок шага, пометка, предупреждение, итог.
   Тот же рисовальщик у живого маршрута и у раскрытой строки истории: что было видно,
   пока прогон шёл, то и видно потом. */
function lineClass(l){
  /* Красили по вхождению слова: путь к файлу, в имени которого есть «ошибки»,
     становился красным, хотя ничего не сломалось. Красим только сообщения самого
     движка — те, что начинаются с маркера или знака, — и никогда пункты списка. */
  const listItem = /^\s*(\d+\.|[-•])\s/.test(l);
  return listItem ? ""
    : /^\s*(❌|ERROR\b|FAIL\b)|^\s*[a-z_]+: (ошибка|не найден|отказ)/i.test(l) ? "err"
    : /^\s*(✅|OK:)|детерминизм подтверждён/i.test(l) ? "ok"
    : /^\s*(⚠️|WARN\b)/i.test(l) ? "warn"
    : /^\s*(#|—|\.\.\.)/.test(l) ? "dim" : "";
}
let SUM_FIRST = true;            // первая строка итога — заголовок, остальные — его строки
function entryNode(e){
  const s = String(e.s == null ? "" : e.s);
  if (e.k === "head") return el("div",{class:"ok",style:"margin-top:10px"}, s);
  if (e.k === "note") return el("div",{class:"dim"}, s);
  if (e.k === "warn" || e.k === "err" || e.k === "ok") return el("div",{class:e.k}, s);
  if (e.k === "sum"){
    const first = SUM_FIRST; SUM_FIRST = false;
    return el("div", first ? {class:"ok",style:"margin-top:12px"}
                           : {class:"dim",style:"white-space:pre"}, s);
  }
  return el("div",{class:lineClass(s)}, s);
}
function drawEntries(out, entries){
  SUM_FIRST = true;
  (entries||[]).forEach(e=>out.append(entryNode(e)));
}

/* ---------------- маршрут ----------------

   Маршрут ведёт процесс панели (`cockpit/route_runner.py`): тот же исполнитель, что у
   расписания и `aurora.py route`. Страница его запускает и смотрит журнал. Закрыли вкладку —
   маршрут идёт дальше; открыли снова — консоль подключается к нему с того же места. Раньше
   маршрут жил в странице, умирал вместе с ней, а в архиве от него оставалась россыпь шагов. */
function routeSteps(sc, write){
  const steps = [];
  for (const st of sc.steps.filter(s=>!s.manual && !s.cycle)){
    const r = S.state.commands.find(x=>x.cmd===st.cmd);
    if (!r || !r.runnable) continue;
    steps.push({cmd: st.cmd, args: (st.flags||[]).filter(f=>write || f!=="--apply")});
  }
  return steps;
}

async function runRoute(sc, write, resume){
  if (!S.project) return toast(t("route.pick_project"), "warn");
  if (await busyElsewhere(sc.title)) return;
  const steps = routeSteps(sc, write);
  const writes = steps.filter(st=>st.args.includes("--apply"));
  if (!resume && write && writes.length && !confirm(t("route.ask", {title: sc.title,
      steps: steps.length, writes: writes.length,
      list: writes.map(s=>"  " + s.cmd + " " + s.args.join(" ")).join("\n")}))) return;
  const res = await api("/api/route/run", {method:"POST", quiet:true, body: JSON.stringify({
    project: S.project.path, scId: sc.id, write,
    resume: resume ? {skipSigs: [...(resume.skipSigs || [])], attempts: resume.attempts || 0,
                      cycleAt: resume.cycleAt || null} : null})});
  if (!res || !res.route || res.error){
    // Уже идёт маршрут (кнопка в другой вкладке, расписание) — подключаемся к нему, а не
    // заводим второй поверх.
    if (res && res.route){
      toast(res.error || "", "warn");
      const d = S.scenarios || (S.scenarios = await api("/api/scenarios"));
      const live = (d.scenarios || []).find(x=>x.id === res.scId) || sc;
      return attachRoute(res.route, live, res.write !== false);
    }
    show("console");
    $("#consoleOut").append(el("div",{class:"err"},
      "■ " + t("step.not_started", {why: (res && res.error) || t("step.refused")})));
    return toast((res && res.error) || t("step.refused"), "err");
  }
  attachRoute(res.route, sc, write, !!resume);
}

async function attachRoute(id, sc, write, resumed){
  show("console");
  const out = $("#consoleOut");
  if (!resumed) out.innerHTML = "";
  $("#consoleApply").innerHTML = ""; PENDING_APPLY = null; LAST_TASKS = []; drawTaskButton();
  ROUTE = {id, title: sc.title, scId: sc.id, write};
  S.lastRoute = {scId: sc.id, write, runId: id, title: sc.title};
  SUM_FIRST = true;
  const stop = $("#consoleStop");
  stop.hidden = false;
  stop.onclick = async () => {
    if (!confirm(t("console.stop_ask"))) return;
    await api("/api/route/stop", {method:"POST", quiet:true, body: JSON.stringify({id})});
    toast(t("console.stopped"));
  };
  const holder = el("div",{class:"warn", style:"margin-top:10px;gap:8px;align-items:center;flex-wrap:wrap", hidden:""});
  let since = 0, waitShown = "";
  for (;;){
    const d = await api(`/api/route/live?id=${encodeURIComponent(id)}&since=${since}`, {quiet:true});
    if (!d || d.error){
      out.append(el("div",{class:"err"}, t("step.job_lost")));
      break;
    }
    (d.entries||[]).forEach(e=>{
      if (e.k === "out") CONSOLE_LINES.push(e.s);
      out.append(entryNode(e));
    });
    since = d.next;
    watchScroll(out); stickToBottom(out);
    drawRouteBar(d.bar);
    if (d.bar && d.bar.cmd){
      $("#consoleCmd").textContent = `${sc.title} · ` + (d.bar.lap
        ? t("route.where_lap", {step: d.bar.inLap, of: d.bar.cycleSize})
          + t("route.lap_mark", {lap: d.bar.lap})
        : t("route.where", {step: d.bar.done, of: d.bar.total})) + ": " + d.bar.cmd;
      $("#consoleRc").textContent = t("route.running"); $("#consoleRc").className = "chip";
    }
    // Ожидание сети ведёт сервер; страница даёт разбудить его или не ждать вовсе.
    const w = d.wait || {};
    if (w.cmd && waitShown !== w.at){
      waitShown = w.at;
      holder.innerHTML = ""; holder.hidden = false;
      const when = new Date(w.at).toLocaleTimeString(S.lang === "en" ? "en-GB" : "ru-RU",
                                                     {hour:"2-digit", minute:"2-digit"});
      holder.append(el("span",{class:"chip warn"}, t("route.wait_chip")),
        el("div",{style:"flex:1;min-width:220px", html: t("route.wait_line",
          {cmd: esc(w.cmd), n: w.attempt, of: w.of, time: when})}),
        el("button",{class:"btn sm gold", onclick: () => api("/api/route/wake",
          {method:"POST", quiet:true, body: JSON.stringify({id})})}, t("route.wait_now")),
        el("button",{class:"btn sm", onclick: () => api("/api/route/stop",
          {method:"POST", quiet:true, body: JSON.stringify({id})})}, t("route.wait_stop")));
      out.append(holder);
    } else if (!w.cmd && waitShown){ waitShown = ""; holder.hidden = true; }
    if (d.done){ finishRoute(d.result || {}, sc, write); break; }
    await new Promise(ok=>setTimeout(ok, 600));
  }
  stop.hidden = true; stop.onclick = null;
  ROUTE = null;
  drawRouteBar(null);
}

/* Конец маршрута: шапка консоли, кнопки «Починить» по находкам, «Продолжить» и здоровье.
   Строки итога уже в журнале — их написал сервер, страница их только показала. */
function finishRoute(r, sc, write){
  const out = $("#consoleOut");
  const bad = r.reason !== "passed";
  if (r.reason === "stall"){
    $("#consoleCmd").textContent = t("route.head_stalled", {title: sc.title});
    $("#consoleRc").textContent = t("route.rc_stalled");
    $("#consoleRc").className = "chip warn";
  } else {
    $("#consoleCmd").textContent = bad ? t("route.head_stopped", {title: sc.title})
                                       : t("route.head_passed", {title: sc.title});
    $("#consoleRc").textContent = bad ? t("route.rc_stopped", {cmd: r.failed || "—"})
                                      : t("route.rc_passed", {n: r.steps || 0});
    $("#consoleRc").className = "chip " + (bad ? "bad" : "ok");
  }
  // Что нашли шаги и чем это чинят — кнопками. В самой «Починить базу» кнопка «Починить:
  // kb:repair» — круг: ремонт только что прошёл, и остаток ему не по силам.
  (r.found || []).forEach(f=>{
    findings(f.lines || []).forEach(x=>{
      const btn = sc.id === "fix" ? null : fixButton(x.what);
      out.append(el("div",{class:"dim row",style:"gap:10px;align-items:center"},
        el("span",{style:"flex:1"},
          `      ${f.cmd} · ${engineWord(x.what)}: ${x.n}` + (x.fix ? `  →  ${x.fix}` : "")),
        btn));
    });
  });
  if (bad && S.lastRoute && S.project){
    dropResumeButtons();
    api("/api/route/state?project="+encodeURIComponent(S.project.path), {quiet:true})
      .then(d=>{ if (d && d.state) $("#consoleApply").append(el("button",{class:"btn sm gold resume-route",
        onclick:()=>resumeLastRoute(d.state)},
        t("route.resume_btn", {title: sc.title, slug: S.project.slug}))); });
  }
  if (S.project){ const p = S.project;
    api("/api/health?project="+encodeURIComponent(p.path)).then(h=>{ const mine = takeHealth(p, h);
      renderOverview();
      if (mine){ loadRuns(); refreshModules(); }});
    if (write) api("/api/state").then(st=>{
      const me = (st.projects||[]).find(x=>x.path===p.path);
      if (me && me.dirty) out.append(el("div",{class:"warn",style:"margin-top:8px"},
        t("route.dirty", {n: me.dirty})));
    });
  }
  drawLiveJobs();              // задания маршрута кончились — карточка «идёт сейчас» не висит
  toast(r.reason === "stall" ? t("route.toast_stall")
    : bad ? t("route.toast_stopped", {cmd: r.failed || "—"})
    : t("route.toast_passed", {title: sc.title}), bad ? "err" : "ok");
}

/* Кнопка «Продолжить маршрут». Сделанное знает сервер: оно в состоянии маршрута, которое он
   пишет после каждого шага, — поэтому продолжение переживает и закрытую вкладку, и
   перезапуск панели. Пропускаются только шаги, завершившиеся успехом (rc 0): rc 1 —
   «отработала и нашла, что чинить», её повторять надо. */
async function resumeLastRoute(last){
  const d = S.scenarios || (S.scenarios = await api("/api/scenarios"));
  const sc2 = (d.scenarios||[]).find(s=>s.id===last.scId);
  if (!sc2) return toast(t("route.not_found"), "warn");
  runRoute(sc2, last.write, {skipSigs: last.done || [], attempts: last.attempts,
                             cycleAt: last.cycleAt});
}

/* Кнопки «Продолжить маршрут» — ровно одна, и она про ТЕКУЩИЙ проект.

   Консоль читает состояние при каждом входе и дорисовывала кнопку, не убирая прежнюю.
   Переключился между проектами несколько раз — и в консоли висят четыре кнопки, три из
   них одинаковые, а одна вообще из другого проекта. Понять, какая к чему относится и
   какую жать, нельзя: подпись у всех одна. Убираем прежние перед отрисовкой и пишем в
   подписи имя проекта — тогда путаницы не остаётся даже в чужой вкладке. */
function dropResumeButtons(){
  $("#consoleApply").querySelectorAll(".resume-route").forEach(b => b.remove());
}

/* Вход в консоль: идёт маршрут — подключаемся к нему; не идёт, а прошлый остановился
   (застой, отказ, сеть, перезапуск панели) — даём «Продолжить маршрут». */
async function showLastRoute(state){
  dropResumeButtons();
  if (!S.project || ROUTE !== null) return;
  const d = S.scenarios || (S.scenarios = await api("/api/scenarios"));
  const live = await api("/api/routes?project="+encodeURIComponent(S.project.path), {quiet:true});
  const now = ((live && live.routes) || [])[0];
  if (now){
    const sc = (d.scenarios||[]).find(s=>s.id===now.scId);
    if (sc) return attachRoute(now.id, sc, now.write, false);
  }
  if (!state || !(d.scenarios||[]).some(s=>s.id===state.scId)) return;
  const when = state.at ? new Date(state.at)
    .toLocaleTimeString(S.lang === "en" ? "en-GB" : "ru-RU",
                        {hour:"2-digit",minute:"2-digit"}) : "";
  const label = state.reason === "stall" ? t("act.reason_stall")
    : state.reason === "failed" ? t("act.reason_failed")
    : state.reason === "offline" ? t("act.reason_offline")
    : state.reason === "stopped" ? t("act.reason_stopped")
    : state.reason === "interrupted" ? t("act.reason_interrupted")
    : t("act.reason_other");
  $("#consoleApply").append(el("button",{class:"btn sm gold resume-route",
    onclick:()=>resumeLastRoute(state)},
    t("resume.stopped_at", {title: state.title, slug: S.project.slug, when, why: label})));
}

let CONSOLE_LINES = [];      // строки текущего прогона: из них собирается задание
// Тишина одиночного задания: poll уходит в setTimeout и возвращается, а между вызовами состояние
// надо держать снаружи. Сброс — на старте нового потока (since===0), см. SILENCE_MS.
let POLL_LAST_OUT = 0, POLL_SILENT = false;
async function poll(id, since, label){
  const d = await api(`/api/job?id=${id}&since=${since}`, {quiet:true});
  const out = $("#consoleOut");
  // Кнопка «Прервать» живёт ровно столько, сколько идёт прогон. Без неё запрет на
  // перезапуск панели при работающем задании превращается в тупик: ни остановить,
  // ни перезапустить — только ждать часами.
  armStop((d && !d.done && !d.error) ? id : null);
  // Задания нет: панель перезапускали, пока команда шла. Раньше цикл продолжал спрашивать
  // про исчезнувшее задание и «выполняется…» стояло вечно — команда при этом могла давно
  // отработать. Тишина здесь дороже ошибки: человек ждёт того, чего уже не случится.
  if (!d || d.error){
    out.append(el("div",{class:"err"}, t("poll.job_lost")));
    $("#consoleRc").textContent = t("poll.job_lost_short");
    $("#consoleRc").className = "chip bad";
    drawLiveJobs();
    return;
  }
  let outLines = d.lines||[];
  if (!since){ CONSOLE_LINES = []; POLL_LAST_OUT = Date.now(); POLL_SILENT = false; }
  (d.lines||[]).forEach(l=>{
    if (String(l).startsWith(SUMMARY_MARK)) return;   // машинная строка итога — для панели
    CONSOLE_LINES.push(l);
    out.append(el("div",{class:lineClass(l)}, l));
  });
  watchScroll(out); stickToBottom(out);
  if (outLines.length){ POLL_LAST_OUT = Date.now(); POLL_SILENT = false; }
  // Та же забота о подвисшей команде, что в маршруте: один раз сообщаем про тишину,
  // а не молчим до бесконечности с «выполняется…».
  if (!d.done && !POLL_SILENT && Date.now() - POLL_LAST_OUT > SILENCE_MS){
    POLL_SILENT = true;
    out.append(el("div",{class:"warn"}, t("step.silent")));
  }
  if (!d.done) return setTimeout(()=>poll(id, d.next, label), 450);
  const rc = d.rc, mark = rcMark(rc);
  const badge = $("#consoleRc");
  badge.textContent = mark.what + t("poll.rc", {rc});
  badge.className = "chip " + mark.cls;
  toast(rc===0 ? t("poll.done", {cmd: label})
               : t("poll.done_rc", {cmd: mark.what + ": " + label, rc}),
        rc===0 ? "ok" : rc===1 ? "warn" : "err");
  // Задание ассистенту команда печатает сама; из прокручиваемой консоли его неудобно
  // выделять мышью — даём кнопку, которая кладёт блок целиком в буфер.
  LAST_TASKS = assistantTasks(CONSOLE_LINES);
  drawTaskButton();
  // Код 1 — «отработала и нашла, что чинить», и записывать после него как раз и надо:
  // это `kb:repair`, `kb:moc`, `kb:index`. Условие `rc===0` отбирало у них кнопку
  // «Применить», и человек шёл искать флаг руками — которого в панели и нет.
  if (rc<=1 && PENDING_APPLY && PENDING_APPLY.job===id){
    const line = PENDING_APPLY.line;
    $("#consoleApply").append(el("button",{class:"btn sm gold",
      onclick:()=>{ if (confirm(applyAsk(line))) fire(true); }},
      t("poll.apply", {cmd: line + " --apply"})));
  }
  // Кнопка «Попробовать снова» — только для настоящей ошибки команды: код 2 и выше.
  // Код 1 — команда отработала и нашла, что чинить, это успех; отрицательный код — процесс
  // снят сигналом («Прервать» или система), это не ошибка команды. Повтор шлёт ту же строку
  // запуска новым заданием, номер попытки растёт; на успехе или коде 1 кнопки нет.
  if (S.lastStep && S.lastStep.job === id){
    S.lastStep.failed = (rc >= 2);
    if (rc >= 2) $("#consoleApply").append(el("button",{class:"btn sm gold",
      onclick:retryStep}, t("poll.retry")));
  }
  // журнал пишет сервер — перечитываем его вместе со здоровьем, а не ведём копию в вкладке
  if (S.project) { const p = S.project;
    api("/api/health?project="+encodeURIComponent(p.path)).then(h=>{ const mine = takeHealth(p, h);
      renderOverview(); if (!mine) return;
      refreshModules();
      renderHistory(); refreshModules();}); }
}
/* Код возврата в Авроре означает три разных вещи, и красить их одинаково — врать.
   0 — сделано; 1 — команда отработала и нашла, что чинить (doctor с ошибками, аудит с
   расхождениями); 2 и выше — не отработала вовсе: не пустил git-гейт, не найден kit,
   не разобраны аргументы. Первое зелёное, второе жёлтое, третье красное. */
/* Блок задания печатают команды, у которых дальше работа модели (`kb:build`). Он обрамлён
   линейками — по ним и находим, чтобы не тащить в буфер весь вывод. */
const TASK_EDGE = "─────";
function assistantTasks(lines){
  /* Задание оформлено для человека: линейки-рамки, заголовок «ЗАДАНИЕ АССИСТЕНТУ · … —
     скопируйте блок целиком в чат». Ассистенту всё это не нужно и только сбивает его с
     толку: он получает указание «скопируйте блок», адресованное не ему. В буфер идёт
     тело — от строки после второй рамки до рамки, которая тело закрывает. */
  const heads = [];
  lines.forEach((l, i) => { if (l.includes("ЗАДАНИЕ АССИСТЕНТУ")) heads.push(i); }); // данные движка
  return heads.map((start, k) => {
    let from = start + 1;
    while (from < lines.length && !lines[from].startsWith(TASK_EDGE)) from++;
    from++;                                   // первая строка тела — обычно /aurora-vault
    const limit = k + 1 < heads.length ? heads[k+1] : lines.length;
    let to = limit;
    for (let i = from; i < limit; i++){
      if (lines[i].startsWith(TASK_EDGE)){ to = i; break; }   // рамка в тело не идёт
    }
    // Заданий бывает два вида: партия разбора и разовая работа («уточнить синонимы»).
    // Подпись берём из заголовка, иначе кнопка врёт про партию там, где партий нет.
    const head = lines[start];
    const num = (head.match(/ПАРТИЯ\s+(\d+)/) || [])[1] || "";            // данные движка
    const label = num ? t("task.batch", {n: num})
      : ((head.match(/ЗАДАНИЕ АССИСТЕНТУ\s*·\s*([^—]+)/) || [])[1]         // данные движка
         || t("task.one")).trim();
    return {num, label, text: lines.slice(from, to).join("\n").trim()};
  }).filter(t => t.text.split("\n").length > 3);
}
let LAST_TASKS = [];         // задания из последнего прогона: переживают уход с вкладки
const TASK_SLOTS = 5;        // столько партий показываем кнопками: дальше идут по плану
function drawTaskButton(){
  const box = $("#consoleTask");
  box.innerHTML = "";
  if (!LAST_TASKS.length) return;
  box.append(el("span",{class:"muted",style:"font-size:12px;margin-right:6px"},
    t("task.label")));
  // Пустые слоты показываем неактивными: видно, что партий меньше, а не что кнопка пропала
  for (let i = 0; i < TASK_SLOTS; i++){
    const task = LAST_TASKS[i];
    box.append(el("button",{class:"btn sm" + (task ? " primary" : ""),
      disabled: task ? null : "",
      title: task ? t("task.copy", {label: task.label,
                                    n: task.text.split("\n").length})
                  : t("task.no_batch"),
      onclick: task ? async()=>{
        try { await navigator.clipboard.writeText(task.text);
              toast(t("task.copied", {label: task.label}), "ok"); }
        catch(e){ toast(t("task.copy_failed"), "warn"); }
      } : null},
      task ? task.label : t("task.batch", {n: i + 1})));
  }
}
function rcMark(rc){
  // Код 1 — это «нашла, что чинить», а не «сломалась»: красить их одинаково — врать.
  return rc === 0 ? {cls:"ok", what:t("run.ok")}
    : rc === 1 ? {cls:"warn", what:t("run.warn")}
    : {cls:"bad", what:t("run.bad")};
}
/* Журнал запусков живёт в проекте (`AuroraKnowledgeDB/meta/run_log.md`) и ездит по git вместе с ним:
   «когда последний раз обновляли зеркала» — вопрос ко всей команде, а не к одному
   браузеру. Панель его только показывает; пишет сервер после каждого запуска. */
// Журнал запусков живёт отдельно от здоровья: читается мгновенно, а здоровье зовёт
// несколько команд и занимает секунды. Пока они ехали вместе, «Консоль» показывала
// «выберите проект» при выбранном проекте — и это читалось как «журнал потерян».
function runs(){ return S.runs || (S.health && S.health.runs) || {}; }

function lastRun(cmd){ return runs()[cmd]; }

/* Список переживает сессии, поэтому одного времени мало: без даты «14:20» назавтра
   означает что угодно. Сегодняшнее показываем часами, вчерашнее — с датой. */
// Итог прогона агент печатает и для человека, и одной машинной строкой для панели: её
// консоль не показывает, а итог маршрута собирает из неё числа шагов.
const SUMMARY_MARK = "AURORA-SUMMARY ";
function consoleLine(out, l){
  if (!String(l).startsWith(SUMMARY_MARK)) out.append(el("div",{class:lineClass(l)}, l));
}

function histWhen(iso){
  // Движок хранит время в UTC: «…Z» в записях и «… UTC» в заголовках журналов. Обе формы
  // приводим к одной и показываем в часовом поясе системы; запись без зоны — прежняя,
  // местная, её браузер и так читает как местную.
  const s = String(iso || "").replace(/^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d(?::\d\d)?) UTC$/, "$1T$2Z");
  const d = new Date(s);
  if (isNaN(d)) return iso || "";
  const loc = S.lang === "en" ? "en-GB" : "ru-RU";
  const hm = d.toLocaleTimeString(loc, {hour:"2-digit", minute:"2-digit"});
  return d.toDateString()===new Date().toDateString() ? hm
    : d.toLocaleDateString(loc, {day:"2-digit", month:"2-digit"}) + " " + hm;
}
// Метка для имён в формате серверного архива прогонов: YYYYMMDD-HHMMSS. Без разделителей
// в дате — так лежат папки в .aurora/runs, и наш id не спутать с чужим.
function rtime(d){
  d = d || new Date();
  const p = n => String(n).padStart(2, "0");
  return "" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-"
    + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

// Секунды компактно, как в житейском таймере: до минуты «Xс», дальше «Mм Sс».
function fmtDur(sec){
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return t("dur.sec", {n: sec});
  return t("dur.min_sec", {m: Math.floor(sec / 60), s: sec % 60});
}

// «Когда» архивного прогона — по его имени: первый отрезок id это время старта
function archiveWhen(runId){
  // С «Z» — UTC (так движок пишет имена прогонов теперь), без — прежнее имя по местному времени.
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(Z?)/.exec(runId || "");
  if (!m) return runId || "";
  return histWhen(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}${m[7]}`);
}

/* История запусков: одна строка — один запуск.

   Было две половинчатые картины: журнал (строка на команду, только последний прогон) и
   архив (строка на папку). Один маршрут «Обновить базу» давал в архиве полсотни строк — по
   одной на шаг, а раскрытая строка показывала кусок одного шага. Теперь строка — то, что
   запустили: команда, маршрут или цепочка расписания, — а раскрытие показывает весь вывод
   так же, как он шёл в консоли: шаги, обороты, итог. */
const HIST_TONE = {passed:"ok", failed:"bad", stall:"warn", stopped:"warn", offline:"warn",
                   interrupted:"warn", running:"gold", missed:"warn"};
function histStatus(r){
  if (r.legacy) return {cls:"", what: t("hist.legacy")};
  if (r.status === "running") return {cls:"gold", what: t("hist.running")};
  if (r.kind === "command" || r.kind === "step"){
    const m = rcMark(r.rc);
    return {cls: m.cls, what: r.rc === 0 ? "ok" : t("history.rc", {rc: r.rc})};
  }
  return {cls: HIST_TONE[r.status] || "", what: t("hist.status." + (r.status || "failed"))};
}
function histTitle(r){
  if (r.kind === "route" && r.scId && S.scenarios){
    const sc = (S.scenarios.scenarios || []).find(x=>x.id === r.scId);
    if (sc) return sc.title;
  }
  return r.title || r.id;
}
async function loadRuns(){
  if (!S.project) return;
  const [d, h] = await Promise.all([
    api("/api/runlog?project=" + encodeURIComponent(S.project.path), {quiet:true}),
    api("/api/history?project=" + encodeURIComponent(S.project.path), {quiet:true}),
    S.scenarios || (S.scenarios = await api("/api/scenarios", {quiet:true})),
  ]);
  if (d && d.runs) S.runs = d.runs;
  S.history = (h && h.runs) || [];
  renderHistory();
}
function renderHistory(){
  const box = $("#historyBox"); box.innerHTML="";
  if (!S.project){
    box.append(el("div",{class:"list-item muted"}, t("history.pick_project"))); return; }
  const rows = S.history || [];
  if (!rows.length){
    box.append(el("div",{class:"list-item muted"}, t("history.empty"))); return; }
  rows.forEach(r=>{
    const st = histStatus(r);
    const arrow = el("span",{class:"muted",style:"font-size:12px"},"▸");
    const view = el("div",{class:"console",style:"max-height:560px;margin:0 14px 10px"});
    const body = el("div",{hidden:""}, view);
    let loaded = false;
    const when = r.started ? histWhen(r.started) : archiveWhen(r.id);
    box.append(el("div",{class:"list-item",style:"cursor:pointer", title: t("hist.expand"),
      onclick: async ()=>{
        body.hidden = !body.hidden;
        arrow.textContent = body.hidden ? "▸" : "▾";
        // Идущий запуск дочитываем при каждом раскрытии: его журнал ещё растёт.
        if (body.hidden || (loaded && r.status !== "running")) return;
        loaded = true;
        view.innerHTML = "";
        view.append(el("div",{class:"dim"}, t("archive.loading")));
        const d = await api("/api/history/run?project=" + encodeURIComponent(S.project.path)
          + "&id=" + encodeURIComponent(r.id), {quiet:true});
        view.innerHTML = "";
        if (!d || d.error) return view.append(el("div",{class:"err"}, (d && d.error) || t("archive.failed")));
        drawEntries(view, d.entries);
      }},
      arrow,
      el("span",{class:"chip " + st.cls, title: st.what}, st.what),
      el("span",{class:"chip"}, t("hist.kind." + (r.kind || "command"))),
      el("span",{class: r.kind === "command" ? "mono" : "",
                 style:"flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"},
        histTitle(r) + (r.kind === "route" && r.write === false ? " · " + t("hist.preview") : "")
        + (r.kind === "cron" && r.items ? " · " + t("hist.items", {ok: r.passed || 0, of: r.items}) : "")),
      r.trigger === "cron" ? el("span",{class:"chip gold"}, t("hist.by_cron")) : null,
      r.who ? el("span",{class:"chip"}, r.who) : null,
      r.kit ? el("span",{class:"chip mono", title: t("history.kit_hint")}, "kit " + r.kit) : null,
      r.seconds != null ? el("span",{class:"muted",style:"font-size:12px"}, fmtDur(r.seconds)) : null,
      el("span",{class:"muted",style:"font-size:12px"}, when)), body);
  });
}
$("#projectReload").onclick = ()=>{ if (confirmLeave()) renderProject(); };
$("#clearConsole").onclick = ()=>{ $("#consoleOut").innerHTML=""; followAgain($("#consoleOut")); };
// Очистка и кнопка «вывод продолжается» возвращают слежение: человек сам сказал, что
// снова смотрит конец.
$("#consoleTail").onclick = ()=> followAgain($("#consoleOut"));
$("#exportMd").onclick = ()=>{
  // Берём строки текущего прогона, а если его нет — текст консоли. Кнопка полезна и после
  // маршрута, который в CONSOLE_LINES не пишет (там свой цикл опроса).
  const lines = CONSOLE_LINES.length
    ? CONSOLE_LINES.slice()
    : $("#consoleOut").innerText.split("\n");
  if (!lines.some(l => l.trim())){
    toast(t("console.export_empty"), "warn"); return; }
  const stamp = new Date().toLocaleString(S.lang === "en" ? "en-GB" : "ru-RU");
  const md = [t("console.md_title"), "",
    t("console.md_project", {path: S.project ? S.project.path : "—"}),
    t("console.md_when", {when: stamp}), ""].concat(lines).join("\n");
  const blob = new Blob([md], {type:"text/markdown;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "aurora-console-" + rtime() + ".md";
  document.body.append(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 5000);
  toast(t("console.exported"), "ok");
};
// Полоса хода — не украшение: по ней возвращаются к прогону с любой вкладки.
$("#routeBar").onclick = ()=> show("console");

/* ---------------- настройка ---------------- */
async function renderSetup(){
  const box = $("#setupBody"); box.innerHTML = "";
  DIRTY.clear();     // раздел перерисован заново — значения в полях снова совпадают с файлом

  // 1. новый проект
  const nf = {};
  const field = (key, label, ph, hint) => el("label",{style:"display:block;margin-bottom:10px"},
    el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"}, label +
      (hint ? " · " + hint : "")),
    watch(el("input",{class:"btn",style:"width:100%;font-weight:400",placeholder:ph,
      oninput:e=>nf[key]=e.target.value}), "new", t("dirty.new")));
  const roots = await api("/api/roots");
  const rootsBox2 = el("div",{style:"margin-top:6px"});
  const drawSearchRoots = (list) => {
    rootsBox2.innerHTML = "";
    (list||[]).forEach(r => rootsBox2.append(el("div",{class:"row",style:"gap:8px;margin-bottom:6px"},
      el("code",{class:"mono",style:"font-size:12px;flex:1;overflow-wrap:anywhere"}, r),
      el("button",{class:"btn sm danger", title: t("setup.drop_root"),
        onclick:async()=>{ const rr = await api("/api/roots",{method:"POST",
          body:JSON.stringify({drop:r})}); if (rr.ok){ drawSearchRoots(rr.roots);
          S.state = await api("/api/state"); renderOverview(); } }},"✕"))));
    const inp2 = el("input",{class:"btn mono",style:"flex:1;font-weight:400",
      placeholder: t("setup.root_ph")});
    rootsBox2.append(el("div",{class:"row",style:"gap:8px;margin-top:4px"}, inp2,
      el("button",{class:"btn sm",onclick:async()=>{
        if (!inp2.value.trim()) return;
        const rr = await api("/api/roots",{method:"POST",body:JSON.stringify({add:inp2.value})});
        if (rr.ok){ inp2.value=""; drawSearchRoots(rr.roots);
          S.state = await api("/api/state"); renderOverview();
          toast(t("setup.root_added")); }
      }}, t("setup.add_root"))));
  };
  drawSearchRoots(roots.roots);
  sgroup(box, "setup:roots", t("setup.roots_title")).append(
    el("div",{class:"card",style:"padding:20px"},
      el("p",{class:"muted",style:"font-size:13px;margin:0 0 12px"},
        t("setup.roots_about", {file: roots.file || "~/.aurora/cockpit-roots.txt"})),
      rootsBox2));

  const newBox = el("div",{class:"card",style:"padding:20px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 14px"}, t("setup.new_about")),
    field("path", t("setup.f_path"), "" + ((roots.roots||[""])[0] || "") + "/my-project"),
    field("name", t("setup.f_name"), "Northwind", t("setup.f_name_hint")),
    field("slug", t("setup.f_slug"), "Northwind", t("setup.f_slug_hint")),
    field("space", t("setup.f_space"), "NW", t("setup.later")),
    field("jira", t("setup.f_jira"), "NW", t("setup.later")),
    el("div",{class:"row",style:"margin-top:6px"},
      saveButton("new", t("dirty.new"), t("setup.deploy"), async e=>{
        e.target.disabled = true; e.target.textContent = t("setup.deploying");
        const r = await api("/api/project/new",{method:"POST",body:JSON.stringify(nf)});
        e.target.disabled = false; e.target.textContent = t("setup.deploy");
        if (r.ok){
          toast(t("setup.deployed", {path: r.path})
                + (r.added_root ? t("setup.deployed_root", {root: r.added_root}) : ""));
          setDirty("new", t("dirty.new"), false);
          S.state = await api("/api/state");
          renderOverview(); renderSetup();
        }
      }),
      el("span",{class:"muted",style:"font-size:12px"}, t("setup.git_note"))));
  sgroup(box, "setup:new", t("setup.new_title")).append(newBox);

  // Модели — общая настройка машины и всех её проектов: раздел «Модели».
  await renderModelsCard(box, "kit");
  MCPK.kit.box = sgroup(box, "setup:mcp", t("mcpk.title"));
  await renderMcpKit();
  drawSetupJump(box);
}

/* ---------------- группы настроек ----------------

   Страницы настроек длинные: одно кольцо шлюзов — несколько экранов, и то, что под ним,
   человек не находил. Каждая часть страницы — группа, которая сворачивается, как маршрут
   «Быстрого старта»: «+» развернуть, «−» свернуть. Что раскрыто, помнит браузер — от
   перерисовки, смены проекта и перезагрузки (решение пользователя 29.09.2026). */
const SGROUPS = {focus: ""};
const SGROUP_KEY = "aurora-settings-open";

function sgroupsOpen(){
  try { return new Set(JSON.parse(localStorage.getItem(SGROUP_KEY) || "[]")); }
  catch (e){ return new Set(); }
}
function sgroupRemember(id, open){
  const s = sgroupsOpen();
  if (open) s.add(id); else s.delete(id);
  try { localStorage.setItem(SGROUP_KEY, JSON.stringify([...s])); } catch (e){ /* без памяти */ }
}

// Группа в `box`: заголовок с «+/−» и тело. → тело: в него части страницы и кладут себя.
// `id` не зависит от языка и проекта — «project:mcp», «setup:agent».
function sgroup(box, id, title){
  const focus = SGROUPS.focus === id;
  const open = focus || sgroupsOpen().has(id);
  const toggle = el("button", {class:"btn sm mono", title: t("sgroup.toggle"),
    "aria-expanded": String(open), style:"flex:none;width:30px;justify-content:center"},
    open ? "−" : "+");
  const head = el("h2", {class:"sgroup-title"}, title);
  const body = el("div", {class:"sgroup-body"});
  body.hidden = !open;
  const wrap = el("section", {class:"sgroup", "data-group": id},
    el("div", {class:"row sgroup-head"}, toggle, head,
      el("span", {class:"unsaved-badge", style:"display:none"}, t("dirty.badge"))), body);
  wrap.sgSet = now => {
    body.hidden = !now;
    toggle.textContent = now ? "−" : "+";
    toggle.setAttribute("aria-expanded", String(now));
    sgroupRemember(id, now);
  };
  toggle.onclick = () => wrap.sgSet(body.hidden);
  head.onclick = () => wrap.sgSet(body.hidden);
  box.append(wrap);
  if (focus){
    SGROUPS.focus = "";
    setTimeout(() => wrap.scrollIntoView({behavior:"smooth"}), 60);
  }
  return body;
}

// Строка переходов сверху: называет все группы страницы, раскрывает и ведёт к нужной;
// «развернуть всё» и «свернуть всё» — для тех, кто ищет глазами.
function drawSetupJump(box){
  const groups = $$(".sgroup", box);
  if (groups.length < 2) return;
  const row = el("div", {class:"row setup-jump"},
    el("span", {class:"muted", style:"font-size:12px"}, t("setup.jump")));
  groups.forEach(g => row.append(el("button", {class:"btn sm", onclick:()=>{
    g.sgSet(true);
    g.scrollIntoView({behavior:"smooth"});
  }}, $(".sgroup-title", g).textContent)));
  row.append(el("div", {class:"spacer"}),
    el("button", {class:"btn sm", onclick:()=>groups.forEach(g=>g.sgSet(true))},
      t("sgroup.all_open")),
    el("button", {class:"btn sm", onclick:()=>groups.forEach(g=>g.sgSet(false))},
      t("sgroup.all_close")));
  box.prepend(row);
}

/* ---------------- MCP-серверы машины ----------------

   Серверы, общие для всех проектов: `<кит>/local/mcp.json`, стандартная форма. Карточка на
   сервер, окно правки, вставка готовой настройки из JSON и весь файл текстом. Секреты
   (`env`, `headers`, `auth`) сервер отдаёт маской; маска, пришедшая обратно, значит «не
   менять». Своей копии маски у страницы нет — её называет сервер (`mask`). */
const MCPK = {kit: {state: null, box: null}, project: {state: null, box: null, probe: null},
              touched: false};

function mcpHow(spec){
  const tr = String(spec.transport || spec.type || "").toLowerCase();
  if (typeof spec.url === "string") return tr === "sse" ? "sse" : "http";
  return "stdio";
}

// Куда смотрит и куда пишет раздел: машина или выбранный проект. Процедура одна на обе
// области — правила серверов одни, различается только файл. Серверы машины видны и в
// проекте, но только для чтения — как кольцо шлюзов (решение пользователя 29.09.2026).
function mcpGet(scope){
  return api(scope === "project" ? "/api/mcp?project=" + encodeURIComponent(S.project.path)
                                 : "/api/mcp/kit");
}
function mcpPost(scope, body){
  return api(scope === "project" ? "/api/mcp" : "/api/mcp/kit", {method:"POST", quiet:true,
    body: JSON.stringify(scope === "project" ? {project: S.project.path, ...body} : body)});
}
// Откуда брать настройку сервера для окна: свои серверы области или машинные в проекте.
function mcpSource(scope){
  if (scope === "kit-ro") return (MCPK.project.state || {}).kit || {};
  return (MCPK[scope].state || {}).servers || {};
}

// Поиск Авроры для других агентов: один сервер на все проекты машины, проект — аргумент
// `project` каждого инструмента. Настройку отдаёт сам сервер (`aurora_mcp.py --all --configs`).
function auroraSearchCard(a){
  const block = (title, obj) => {
    const text = JSON.stringify(obj, null, 2);
    return el("div", {style:"margin-top:12px"},
      el("div", {class:"row", style:"gap:8px;align-items:center"},
        el("b", {style:"font-size:13px"}, title), el("div", {class:"spacer"}), copyButton(text)),
      el("pre", {class:"mono", style:"font-size:11.5px;white-space:pre-wrap;overflow-wrap:anywhere;"
        + "margin:6px 0 0;padding:10px;border-radius:8px;background:var(--surface-2)"}, text));
  };
  return el("div", {class:"card", style:"padding:20px;margin-top:14px"},
    el("b", {}, t("mcpk.aurora_title")),
    el("p", {class:"muted", style:"font-size:13px;margin:6px 0 0"}, t("mcpk.aurora_about")),
    (a.projects || []).length ? el("div", {class:"row", style:"gap:6px;flex-wrap:wrap;margin-top:8px"},
      el("span", {class:"muted", style:"font-size:12.5px"}, t("mcpk.aurora_projects")),
      ...a.projects.map(x => el("span", {class:"chip mono"}, x))) : null,
    block(t("mcpk.aurora_opencode"), a.opencode),
    block(t("mcpk.aurora_claude"), a.mcpServers));
}

async function renderMcpKit(){ return renderMcp("kit"); }

async function renderMcp(scope){
  const M = MCPK[scope], box = M.box;
  if (!box || (scope === "project" && !S.project)) return;
  const d = await mcpGet(scope);
  M.state = d;
  box.innerHTML = "";
  const probe = scope === "project" ? M.probe : null;
  const names = Object.keys(d.servers || {});
  const cards = el("div", {class:"grid tiles"});
  names.forEach(n => cards.append(mcpCard(scope, n, d.servers[n], d.mask,
                                          probe && probe[n])));
  box.append(el("div", {class:"card", style:"padding:20px"},
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 12px"}, t(scope === "project" ? "mcp.about" : "mcpk.about")),
    d.error ? el("div", {class:"warnbox"}, d.error) : null,
    el("div", {class:"row", style:"gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px"},
      el("button", {class:"btn sm primary", onclick:()=>openMcpCard(scope, "")}, t("mcpk.add")),
      el("button", {class:"btn sm", onclick:()=>openMcpPaste(scope)}, t("mcpk.paste")),
      el("button", {class:"btn sm", onclick:()=>openMcpRaw(scope)}, t("mcpk.raw")),
      el("span", {class:"muted mono", style:"font-size:11.5px;overflow-wrap:anywhere"},
        d.path || "")),
    names.length ? cards
                 : el("div", {class:"muted", style:"font-size:13px"}, t(scope === "project" ? "mcp.empty" : "mcpk.empty"))));
  if (scope !== "project"){ if (d.aurora && d.aurora.opencode) box.append(auroraSearchCard(d.aurora)); return; }

  // Серверы машины работают и в этом проекте, а правятся только в «Настройке кита».
  const kit = d.kit || {};
  const kitNames = Object.keys(kit);
  const kitCards = el("div", {class:"grid tiles"});
  kitNames.forEach(n => kitCards.append(mcpCard("kit-ro", n, kit[n], d.mask,
                                                probe && probe[n], names.includes(n))));
  box.append(el("div", {class:"card", style:"padding:20px;margin-top:14px"},
    el("div", {class:"row", style:"gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px"},
      el("b", {}, t("mcp.kit_title")),
      el("span", {class:"chip"}, t("mcp.readonly")),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick: mcpToKit}, t("mcp.to_kit"))),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 12px"}, t("mcp.kit_about")),
    d.kit_error ? el("div", {class:"warnbox"}, d.kit_error) : null,
    kitNames.length ? kitCards
                    : el("div", {class:"muted", style:"font-size:13px"}, t("mcp.kit_empty"))));

  // Проверка — то, что получит прогон: оба вида серверов, в папке проекта.
  const all = [...new Set([...names, ...kitNames])];
  const good = probe ? all.filter(n => probe[n] && probe[n].ok).length : 0;
  box.append(el("div", {class:"row", style:"gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px"},
    all.length ? el("button", {class:"btn sm", onclick: async e => {
      e.target.disabled = true;
      e.target.textContent = t("mcp.probing");
      const r = await api("/api/mcp/probe?project=" + encodeURIComponent(S.project.path),
                          {quiet:true});
      if (r.error){
        e.target.disabled = false;
        e.target.textContent = t("mcp.probe");
        return toast(r.error, "warn");
      }
      M.probe = r.servers || {};
      renderMcp("project");
    }}, t("mcp.probe")) : null,
    el("span", {class:"muted", style:"font-size:12px"},
      !all.length ? "" : probe ? t("mcp.probe_done", {ok: good, n: all.length})
                               : t("mcp.probe_hint"))));
}

// Итог проверки на карточке: работает и сколько инструментов — или почему не поднялся.
function mcpProbeChip(p){
  if (!p) return null;
  return p.ok
    ? el("span", {class:"chip ok", title: (p.names || []).join(", ")},
        t("mcp.works", {n: p.tools || 0}))
    : el("span", {class:"chip warn", title: p.error || ""}, t("mcp.fails"));
}

function mcpCard(scope, name, spec, mask, probe, overridden){
  const how = mcpHow(spec);
  const ro = scope === "kit-ro";
  const line = how === "stdio" ? [spec.command, ...(spec.args || [])].join(" ") : spec.url;
  const secrets = Object.keys(spec.env || {}).length + Object.keys(spec.headers || {}).length
                + (spec.auth === mask ? 1 : 0);
  const about = spec.about || spec.description || "";
  return el("button", {class:"card mcp-card" + (ro ? " ro" : ""),
      title: ro ? t("mcp.ro_open") : t("mcpk.open"),
      "aria-label": name + " · " + (ro ? t("mcp.ro_open") : t("mcpk.open")),
      onclick:()=>openMcpCard(scope, name)},
    el("div", {class:"row", style:"gap:8px;align-items:center;flex-wrap:wrap"},
      el("b", {class:"mono"}, name), el("span", {class:"chip"}, t("mcpk.how." + how)),
      ro ? el("span", {class:"chip"}, t("mcp.from_kit")) : null,
      overridden ? el("span", {class:"chip gold", title: t("mcp.overridden_hint")},
                      t("mcp.overridden")) : null,
      mcpProbeChip(probe)),
    el("div", {class:"mono muted mcp-line", title: line || ""}, line || "—"),
    (secrets || (spec.roles || []).length || spec.outbound)
      ? el("div", {class:"row", style:"gap:6px;flex-wrap:wrap"},
          secrets ? el("span", {class:"chip warn"}, t("mcpk.secrets", {n: secrets})) : null,
          (spec.roles || []).length
            ? el("span", {class:"chip"}, t("mcpk.roles_only", {list: spec.roles.join(", ")})) : null,
          spec.outbound ? el("span", {class:"chip gold"}, t("mcpk.outbound_chip")) : null)
      : null,
    probe && !probe.ok ? el("div", {class:"muted", style:"font-size:12px;overflow-wrap:anywhere"},
                            probe.error || "") : null,
    about ? el("div", {class:"muted", style:"font-size:12px"}, about) : null);
}

// Из проекта — к серверам машины: вкладка «Настройка кита», группа MCP раскрыта.
function mcpToKit(){
  closeMcp(true);
  SGROUPS.focus = "setup:mcp";
  show("setup");
}

function mcpOpen(wide){
  MCPK.touched = false;
  const d = $("#mcpDrawer");
  d.innerHTML = "";
  d.classList.toggle("wide", !!wide);
  $("#mcpOverlay").classList.add("on");
  return d;
}
// Любая правка в окне — повод спросить перед закрытием: и текст, и галочки ролей.
["input", "change"].forEach(ev =>
  $("#mcpDrawer").addEventListener(ev, () => { MCPK.touched = true; }));
function closeMcp(force){
  if (!$("#mcpOverlay").classList.contains("on")) return;
  if (!force && MCPK.touched && !confirm(t("mcpk.leave_ask"))) return;
  $("#mcpOverlay").classList.remove("on");
  $("#mcpDrawer").innerHTML = "";          // вставленные токены не остаются в разметке
  MCPK.touched = false;
}
const mcpField = (label, node, hint) => el("label", {style:"display:block;margin-bottom:12px"},
  el("div", {class:"muted", style:"font-size:12px;margin-bottom:4px"},
    label + (hint ? " · " + hint : "")), node);
const mcpInput = (value, extra) => el("input", Object.assign({class:"btn mono",
  style:"width:100%;font-weight:400", value: value || "", spellcheck:"false",
  autocomplete:"off"}, extra || {}));

// Пары «имя → значение» для env и headers. Значение, скрытое маской, в поле не попадает:
// пустое поле с пометкой «задано» при записи превращается обратно в маску.
function mcpPairs(entries, mask, addLabel){
  const rows = entries.map(([k, v]) => ({key: k, value: "", masked: v === mask,
                                         plain: v === mask ? "" : v}));
  const box = el("div", {});
  const draw = () => {
    box.innerHTML = "";
    rows.forEach((r, i) => box.append(el("div", {class:"mcp-kv"},
      mcpInput(r.key, {placeholder: t("mcpk.f_key"), oninput: e => { r.key = e.target.value; }}),
      el("input", {class:"btn mono", type:"password", autocomplete:"new-password",
        style:"width:100%;font-weight:400", value: r.plain,
        placeholder: r.masked ? t("mcpk.kept") : t("mcpk.f_value"),
        oninput: e => { r.plain = e.target.value; }}),
      el("button", {class:"btn sm danger", onclick:()=>{ rows.splice(i, 1);
        MCPK.touched = true; draw(); }}, "✕"))));
    box.append(el("button", {class:"btn sm", onclick:()=>{
      rows.push({key:"", plain:"", masked:false}); draw(); }}, addLabel));
  };
  draw();
  return {box, value(){
    const out = {};
    for (const r of rows){
      const key = r.key.trim();
      if (!key) continue;
      if (r.plain) out[key] = r.plain;
      else if (r.masked) out[key] = mask;
      else throw new Error(t("mcpk.need_value", {key}));
    }
    return out;
  }};
}

// Окно сервера. `scope`: "kit" — сервер машины в «Настройке кита»; "project" — сервер
// проекта; "kit-ro" — сервер машины, открытый из проекта: только посмотреть. `seed` —
// настройка для нового сервера проекта, перекрывающего машинный (без секретов).
function openMcpCard(scope, name, seed){
  const ro = scope === "kit-ro";
  const home = ro ? "project" : scope;
  const d = MCPK[home].state || {}, mask = d.mask;
  const orig = seed ? JSON.parse(JSON.stringify(seed))
             : name ? JSON.parse(JSON.stringify(mcpSource(scope)[name] || {})) : {};
  const drawer = mcpOpen(false);
  let how = mcpHow(orig);
  // Токены живут у машины: `mcp.json` проекта уезжает в git проекта. В окне проекта полей
  // для секретов нет, а то, что уже лежит в файле (положили руками), переживает запись.
  const secretsHere = scope === "kit" || ro;
  const nameIn = mcpInput(name, {placeholder:"github"});
  const cmdIn = mcpInput(orig.command, {placeholder:"npx"});
  const argsIn = el("textarea", {class:"btn mono", spellcheck:"false", rows:"4",
    style:"width:100%;font-weight:400;resize:vertical"}, (orig.args || []).join("\n"));
  const urlIn = mcpInput(orig.url, {placeholder:"https://…/mcp"});
  const env = mcpPairs(Object.entries(orig.env || {}), mask, t("mcpk.add_var"));
  const headers = mcpPairs(Object.entries(orig.headers || {}), mask, t("mcpk.add_header"));
  const roles = new Set(orig.roles || []);
  const outIn = el("input", {type:"checkbox", checked: orig.outbound ? "" : null});
  const aboutIn = mcpInput(orig.about || orig.description, {class:"btn",
    style:"width:100%;font-weight:400"});
  const noSecrets = el("div", {class:"muted", style:"font-size:12px;margin-bottom:12px"},
    t("mcp.secrets_in_kit"));
  const stdioBox = el("div", {},
    mcpField(t("mcpk.f_command"), cmdIn), mcpField(t("mcpk.f_args"), argsIn),
    secretsHere ? mcpField(t("mcpk.f_env"), env.box) : null);
  const urlBox = el("div", {}, mcpField(t("mcpk.f_url"), urlIn),
    secretsHere ? mcpField(t("mcpk.f_headers"), headers.box) : null);
  const showHow = () => { stdioBox.hidden = how !== "stdio"; urlBox.hidden = how === "stdio"; };
  const howSel = el("select", {class:"btn", style:"font-weight:400",
      onchange: e => { how = e.target.value; showHow(); }},
    ...["stdio", "http", "sse"].map(v => el("option", {value: v, selected: v === how ? "" : null},
      t("mcpk.how." + v))));
  // Поля, которых в форме нет (timeout, cwd, auth…), переживают запись: карточка правит
  // только своё. Называем их, чтобы человек не удивлялся, откуда они в файле.
  const MANAGED = ["command", "args", "env", "url", "headers", "transport", "type", "roles",
                   "outbound", "about", "description"];
  const managed = secretsHere ? MANAGED : MANAGED.filter(k => !["env", "headers"].includes(k));
  const extra = Object.keys(orig).filter(k => !managed.includes(k));
  const errBox = el("div", {});
  const done = () => { closeMcp(true); MCPK[home].probe = null; renderMcp(home); };
  const save = async () => {
    errBox.innerHTML = "";
    const newName = nameIn.value.trim();
    if (!newName) return errBox.append(el("div", {class:"warnbox"}, t("mcpk.need_name")));
    const spec = JSON.parse(JSON.stringify(orig));
    try {
      for (const k of ["command", "args", "url", "transport", "type"]) delete spec[k];
      if (secretsHere){ delete spec.env; delete spec.headers; }
      if (how === "stdio"){
        spec.command = cmdIn.value.trim();
        const args = argsIn.value.split("\n").map(a => a.trim()).filter(Boolean);
        if (args.length) spec.args = args;
        if (secretsHere){ const e = env.value(); if (Object.keys(e).length) spec.env = e; }
        delete spec.headers;
        delete spec.auth;
      } else {
        spec.url = urlIn.value.trim();
        spec.transport = how;
        if (secretsHere){ const h = headers.value(); if (Object.keys(h).length) spec.headers = h; }
        delete spec.env;
        delete spec.cwd;
      }
    } catch (e){ return errBox.append(el("div", {class:"warnbox"}, e.message)); }
    if (roles.size) spec.roles = [...roles]; else delete spec.roles;
    if (outIn.checked) spec.outbound = true; else delete spec.outbound;
    delete spec.description;
    if (aboutIn.value.trim()) spec.about = aboutIn.value.trim(); else delete spec.about;
    const r = await mcpPost(scope, {action:"save_server", name: newName,
                                    rename_from: seed ? "" : name, spec});
    if (r.error) return errBox.append(el("div", {class:"warnbox"}, r.error));
    toast(r.unchanged ? t("mcp.unchanged")
          : t(scope === "project" ? "mcp.saved" : "mcpk.saved", {n: r.count}), "ok");
    done();
  };
  const title = ro ? t("mcp.ro_title", {name})
              : name && !seed ? t("mcpk.edit_title", {name})
              : seed ? t("mcp.override_title", {name}) : t("mcpk.new_title");
  // Родной `append` печатает пустое место словом «null» — пустые части отбрасываем.
  drawer.append(...[
    el("h2", {style:"margin-top:0"}, title),
    ro ? el("div", {class:"warnbox", style:"margin-bottom:12px"}, t("mcp.ro_about")) : null,
    mcpField(t("mcpk.f_name"), nameIn, t("mcpk.f_name_hint")),
    mcpField(t("mcpk.f_how"), howSel), stdioBox, urlBox,
    secretsHere ? null : noSecrets,
    el("div", {style:"margin-bottom:12px"},
      el("div", {class:"muted", style:"font-size:12px;margin-bottom:4px"},
        t("mcpk.f_roles") + " · " + t("mcpk.f_roles_hint")),
      el("div", {class:"row", style:"gap:14px;flex-wrap:wrap"},
        ...(d.roles || []).map(r => el("label", {class:"row", style:"gap:6px;font-size:13px"},
          el("input", {type:"checkbox", checked: roles.has(r) ? "" : null,
            onchange: e => { e.target.checked ? roles.add(r) : roles.delete(r); }}), r)))),
    el("label", {class:"row", style:"gap:8px;font-size:13px;margin-bottom:12px"}, outIn,
      t("mcpk.f_outbound")),
    mcpField(t("mcpk.f_about"), aboutIn),
    extra.length ? el("div", {class:"muted", style:"font-size:12px;margin-bottom:12px"},
      t("mcpk.kept_fields", {list: extra.join(", ")})) : null,
    errBox,
    ro ? el("div", {class:"row", style:"gap:8px;margin-top:8px;flex-wrap:wrap"},
      el("button", {class:"btn primary", onclick: mcpToKit}, t("mcp.to_kit")),
      // Перекрыть поля машинного сервера для одного проекта: имя то же, токены — машины.
      el("button", {class:"btn sm", onclick: () => {
        const base = JSON.parse(JSON.stringify(orig));
        delete base.env; delete base.headers; delete base.auth;
        closeMcp(true);
        openMcpCard("project", name, base);
      }}, t("mcp.override")),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp(true)}, t("mcpk.close")))
    : el("div", {class:"row", style:"gap:8px;margin-top:8px;flex-wrap:wrap"},
      el("button", {class:"btn primary", onclick: save}, t("mcpk.save")),
      name && !seed ? el("button", {class:"btn sm danger", onclick: async () => {
        if (!confirm(t("mcpk.delete_ask", {name}))) return;
        const r = await mcpPost(scope, {action:"delete_server", name});
        if (r.error) return errBox.append(el("div", {class:"warnbox"}, r.error));
        toast(t("mcpk.deleted", {name}), "ok");
        done();
      }}, t("mcpk.delete")) : null,
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp()}, t("mcpk.close")))].filter(Boolean));
  showHow();
  // Сервер машины из проекта — только посмотреть: поля видны, но не правятся.
  if (ro){
    drawer.querySelectorAll("input, select, textarea").forEach(n => { n.disabled = true; });
    [stdioBox, urlBox].forEach(b => b.querySelectorAll("button")
      .forEach(n => { n.disabled = true; }));
  }
}

function openMcpPaste(scope){
  const drawer = mcpOpen(true);
  const text = el("textarea", {class:"mcp-text", spellcheck:"false", autocomplete:"off",
    style:"min-height:30vh", placeholder: t("mcpk.paste_ph")});
  const out = el("div", {});
  const send = dry => mcpPost(scope, {action:"import", text: text.value, dry});
  const parse = async () => {
    out.innerHTML = "";
    const r = await send(true);
    if (r.error) return out.append(el("div", {class:"warnbox"}, r.error));
    out.append(el("div", {style:"font-weight:600;margin:12px 0 6px"},
      t("mcpk.found", {n: r.servers.length})));
    r.servers.forEach(s => out.append(el("div", {class:"list-item",
        style:"gap:8px;flex-wrap:wrap;align-items:center"},
      el("b", {class:"mono"}, s.name),
      el("span", {class:"chip " + (s.replace ? "warn" : "ok")},
        s.replace ? t("mcpk.will_replace") : t("mcpk.will_add")),
      el("span", {class:"chip"}, t("mcpk.how." + (s.how === "url" ? "http" : "stdio"))),
      s.env.length ? el("span", {class:"muted mono", style:"font-size:12px"},
        t("mcpk.env_keys", {list: s.env.join(", ")})) : null,
      s.headers.length ? el("span", {class:"muted mono", style:"font-size:12px"},
        t("mcpk.header_keys", {list: s.headers.join(", ")})) : null)));
    // Что разбор достроил сам (сервер из расширения Zed) или пропустил (выключен в источнике).
    (r.notes || []).forEach(n => out.append(el("div", {class:"muted",
      style:"font-size:12px;margin:6px 0 0"}, n)));
    out.append(el("button", {class:"btn primary", style:"margin-top:12px", onclick: async () => {
      const w = await send(false);
      if (w.error) return out.append(el("div", {class:"warnbox"}, w.error));
      toast(t("mcpk.imported", {n: w.servers.length}), "ok");
      closeMcp(true);
      MCPK[scope].probe = null;
      renderMcp(scope);
    }}, t("mcpk.import")));
  };
  drawer.append(el("h2", {style:"margin-top:0"}, t("mcpk.paste_title")),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"},
      t(scope === "project" ? "mcp.paste_about" : "mcpk.paste_about")),
    text, out,
    el("div", {class:"row", style:"gap:8px;margin-top:12px"},
      el("button", {class:"btn", onclick: parse}, t("mcpk.parse")),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp()}, t("mcpk.close"))));
  text.focus();
}

function openMcpRaw(scope){
  const d = MCPK[scope].state || {};
  const drawer = mcpOpen(true);
  const text = el("textarea", {class:"mcp-text", spellcheck:"false", autocomplete:"off"},
    d.text || "{\n  \"mcpServers\": {}\n}\n");
  const errBox = el("div", {});
  drawer.append(el("h2", {style:"margin-top:0"}, t("mcpk.raw_title")),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 6px"},
      t(scope === "project" ? "mcp.raw_about" : "mcpk.raw_about")),
    el("div", {class:"muted mono", style:"font-size:11.5px;margin-bottom:10px;overflow-wrap:anywhere"},
      d.path || ""),
    text, errBox,
    el("div", {class:"row", style:"gap:8px;margin-top:12px"},
      el("button", {class:"btn primary", onclick: async () => {
        errBox.innerHTML = "";
        const r = await mcpPost(scope, {action:"save_raw", text: text.value});
        if (r.error) return errBox.append(el("div", {class:"warnbox"}, r.error));
        toast(t(scope === "project" ? "mcp.raw_saved" : "mcpk.raw_saved", {n: r.count}), "ok");
        closeMcp(true);
        MCPK[scope].probe = null;
        renderMcp(scope);
      }}, t("mcpk.raw_save")),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp()}, t("mcpk.close"))));
}

/* ---------------- настройки проекта ----------------

   Отдельная вкладка появилась не ради порядка. Одна форма держала и общее, и частное:
   человек правил поле, оно ложилось то в кит, то в проект — в зависимости от того, был
   ли выбран проект, — и вопрос «почему на другом проекте не изменилось» повторялся. */
async function renderProject(){
  const box = $("#projectBody"); box.innerHTML = "";
  DIRTY.clear();
  if (!S.project){
    box.append(el("div",{class:"card",style:"padding:22px"}, t("proj.pick")));
    return;
  }

  // 2. настройка проекта формой — те же вопросы, что задаёт setup в терминале
  const formBox = sgroup(box, "project:form", t("proj.title", {project: S.project.name}));
  const cfgRaw = await api("/api/config?project=" + encodeURIComponent(S.project.path));
  // Скаляры конфига приходят с сервера уже разобранными движком: своей копии правила
  // у панели нет — иначе формы записи YAML («JQL с датой в кавычках») читались бы в
  // форме и в синках по-разному.
  const get = (k, d="") => {
    const v = cfgRaw.values ? cfgRaw.values[k] : "";
    return (v === undefined || v === null || v === "") ? d : String(v).trim();
  };
  const A = {
    name: get("name"), slug: get("slug"),
    conf_url: get("base_url"), conf_space: get("space"),
    jira_url: get("jira_base_url"),
    jira_key: get("project_key"), jira_jql: get("default_jql"),
    trust_statuses: (cfgRaw.text.match(/trust_statuses:\s*\[([^\]]*)\]/)||[])[1]||"",
    assumption_statuses: (cfgRaw.text.match(/assumption_statuses:\s*\[([^\]]*)\]/)||[])[1]||"",
    trusted_sources: (cfgRaw.text.match(/trusted_sources:\s*\[([^\]]*)\]/)||[])[1]||"",
    trusted_branches: (cfgRaw.text.match(/trusted_branches:\s*\[([^\]]*)\]/)||[])[1]||"",
    scrub: get("scrub","report"), threshold: get("verified_threshold_pct","20"),
    // важно принимать не только цифры: корень, который не удалось разрешить, всё равно
    // должен показаться в форме — иначе «сохранено», а строки нет, и человек в тупике
    // Веб-страницы — СВОЙ список, не смешанный с корнями Confluence: это разные
    // источники, и доверяются они по разным правилам. У каждой ссылки своя галочка.
    web_pages: [...(cfgRaw.text.matchAll(/-\s*url:\s*(\S+)\s*\n\s*trusted:\s*(\S+)/g))]
      .map(m => ({url: m[1], trusted: /^(true|yes|да)$/i.test(m[2])})),
    // Корни синка — пунктами списка: page_id, название, ссылка и галочка «доверять»
    // (решение пользователя 27.09.2026). Разбор тот же, что у движка (`sync_roots`).
    sync_roots: (() => {
      const m = cfgRaw.text.match(/^(\s*)sync_roots\s*:[^\n]*\n((?:\1\s+[^\n]*\n?|\s*\n)*)/m);
      if (!m) return [];
      return m[2].split(/^\s*-\s+/m).slice(1).map(item => {
        const f = k => ((item.match(new RegExp("^\\s*" + k + "\\s*:\\s*(.*?)\\s*$", "m")) || [])[1]
                        || "").replace(/^["']|["']$/g, "");
        return {page_id: f("page_id"), title: f("title"),
                trusted: /^(true|yes|да)$/i.test(f("trusted"))};
      }).filter(r => r.page_id);
    })(),
  };
  const inp = (key, label, ph, hint) => el("label",{style:"display:block;margin-bottom:12px"},
    el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"},
      label + (hint ? " · " + hint : "")),
    watch(el("input",{class:"btn",style:"width:100%;font-weight:400",placeholder:ph,
      value:A[key]||"", oninput:e=>A[key]=e.target.value}), "form", t("dirty.form")));

  const rootsBox = el("div",{});
  // Повтор корня — тот же номер страницы или то же название. В PRJ-C корень был записан
  // дважды с 10.08: синк обходил поддерево два раза, а среди девятнадцати строк в порядке
  // добавления повтор глазом не находился. Сортировка ставит повторы рядом, метка называет.
  const rootKey = r => (r.title || "").trim().toLowerCase().replace(/\s+/g, " ");
  const rootTwins = () => {
    const ids = {}, names = {};
    A.sync_roots.forEach(r => {
      const id = (r.page_id || "").trim(), nm = rootKey(r);
      if (id) ids[id] = (ids[id] || 0) + 1;
      if (nm) names[nm] = (names[nm] || 0) + 1;
    });
    return r => ids[(r.page_id || "").trim()] > 1 || names[rootKey(r)] > 1;
  };
  const sortRoots = by => {
    const val = r => by === "id" ? (r.page_id || "").trim().padStart(20, "0") : rootKey(r);
    A.sync_roots.sort((a, b) => val(a).localeCompare(val(b), S.lang || "ru"));
    drawRoots();
    setDirty("form", t("dirty.form"), true);
  };
  const drawRoots = () => {
    rootsBox.innerHTML = "";
    const twin = rootTwins();
    if (A.sync_roots.length > 1)
      rootsBox.append(el("div",{class:"row",style:"gap:6px;margin-bottom:8px"},
        el("span",{class:"muted",style:"font-size:12px"}, t("proj.roots_sort")),
        el("button",{class:"btn sm", onclick:()=>sortRoots("title")}, t("proj.sort_title")),
        el("button",{class:"btn sm", onclick:()=>sortRoots("id")}, t("proj.sort_id"))));
    A.sync_roots.forEach((r, i) => rootsBox.append(el("div",{class:"row",style:"margin-bottom:8px"},
      watch(el("input",{class:"btn mono",style:"width:190px;font-weight:400",
        placeholder: t("proj.root_id_ph"), value:r.page_id,
        oninput:e=>r.page_id=e.target.value}), "form", t("dirty.form")),
      watch(el("input",{class:"btn",style:"flex:1;min-width:160px;font-weight:400",
        placeholder: t("proj.root_title_ph"), value:r.title,
        oninput:e=>r.title=e.target.value}), "form", t("dirty.form")),
      // Галочка у раздела, как у веб-ссылки: отмечен — доверен целиком и сразу; не
      // отмечен — доверие по правилам (справочник, задача, история).
      el("label",{class:"muted",style:"font-size:12px;display:flex;align-items:center;gap:6px",
        title: t("proj.root_trust_hint")},
        watch(el("input",{type:"checkbox", checked: r.trusted ? "" : null,
          onchange:e=>r.trusted=e.target.checked}), "form", t("dirty.form")),
        t("proj.trust")),
      twin(r) ? el("span",{class:"chip warn", title: t("proj.root_dup_hint")}, t("proj.root_dup"))
              : null,
      el("button",{class:"btn sm danger", title: t("proj.drop"),
        onclick:()=>{A.sync_roots.splice(i,1); drawRoots();
                     setDirty("form", t("dirty.form"), true);}},"✕"))));
    rootsBox.append(el("button",{class:"btn sm",style:"margin-top:4px",
      onclick:()=>{A.sync_roots.push({page_id:"",title:"",trusted:false}); drawRoots();
                   setDirty("form", t("dirty.form"), true);}},
      t("proj.add_root")));
  };
  drawRoots();

  // Веб-страницы — отдельный блок. Ссылка в интернете и страница корпоративной вики
  // приходят из разных мест, настраиваются порознь и доверяются по разным правилам:
  // у задачи доверие считается по статусу, у документа — объявляется тем, кто его
  // подключил. Поэтому галочка «доверять» стоит у КАЖДОЙ ссылки, а не у модуля.
  const webBox = el("div",{});
  const drawWeb = () => {
    webBox.innerHTML = "";
    A.web_pages.forEach((w, i) => webBox.append(el("div",{class:"row",style:"margin-bottom:8px"},
      watch(el("input",{class:"btn mono",style:"flex:1;min-width:200px;font-weight:400",
        placeholder:"https://…", value:w.url,
        oninput:e=>w.url=e.target.value}), "form", t("dirty.form")),
      el("label",{class:"muted",style:"font-size:12px;display:flex;align-items:center;gap:6px",
        title: t("proj.web_trust_hint")},
        watch(el("input",{type:"checkbox", checked: w.trusted ? "" : null,
          onchange:e=>w.trusted=e.target.checked}), "form", t("dirty.form")),
        t("proj.trust")),
      el("button",{class:"btn sm danger", title: t("proj.drop"),
        onclick:()=>{A.web_pages.splice(i,1); drawWeb();
                     setDirty("form", t("dirty.form"), true);}},"✕"))));
    webBox.append(el("button",{class:"btn sm",style:"margin-top:4px",
      onclick:()=>{A.web_pages.push({url:"",trusted:false}); drawWeb();
                   setDirty("form", t("dirty.form"), true);}},
      t("proj.add_web")));
  };
  drawWeb();

  formBox.append(el("div",{class:"card",style:"padding:20px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 14px"},
      t("proj.about")),
    inp("name", t("proj.f_name"), "My Project"),
    inp("slug", t("proj.f_slug"), "MyProject", t("proj.f_slug_hint")),
    el("div",{class:"sep"}),
    inp("conf_url", t("proj.f_conf_url"), "https://confluence.example.com"),
    inp("conf_space", t("proj.f_conf_space"), "SPACE"),
    el("div",{class:"muted",style:"font-size:12px;margin:14px 0 6px"}, t("proj.roots_note")),
    rootsBox,
    el("div",{class:"sep"}),
    inp("jira_url", t("proj.f_jira_url"), "https://jira.example.com"),
    inp("jira_key", t("proj.f_jira_key"), "PROJ"),
    inp("jira_jql", t("proj.f_jira_jql"), "project = PROJ ORDER BY updated DESC"),
    inp("trust_statuses", t("proj.f_trust_statuses"), t("proj.f_trust_statuses_ph"),
        t("proj.f_trust_statuses_hint")),
    inp("assumption_statuses", t("proj.f_assumption_statuses"),
        t("proj.f_assumption_statuses_ph"), t("proj.f_assumption_statuses_hint")),
    el("div",{class:"sep"}),
    el("div",{class:"muted",style:"font-size:12px;margin:0 0 6px"}, t("proj.web_note")),
    webBox,
    el("div",{class:"sep"}),
    inp("trusted_sources", t("proj.f_trusted_sources"), t("proj.f_trusted_sources_ph"),
        t("proj.f_trusted_sources_hint")),
    inp("trusted_branches", t("proj.f_trusted_branches"), t("proj.f_trusted_branches_ph"),
        t("proj.f_trusted_branches_hint")),
    el("div",{class:"sep"}),
    el("label",{style:"display:block;margin-bottom:12px"},
      el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"}, t("proj.scrub_note")),
      watch(el("select",{class:"btn",style:"width:100%;font-weight:400",
        onchange:e=>A.scrub=e.target.value},
        ...["report","off","mask"].map(v=>el("option",{value:v, selected:A.scrub===v?"":null}, v))),
        "form", t("dirty.form"))),
    inp("threshold", t("proj.f_threshold"), "20", t("proj.f_threshold_hint")),
    el("div",{class:"row",style:"margin-top:8px"},
      saveButton("form", t("dirty.form"), t("proj.save"), async e=>{
        e.target.disabled = true;
        // ссылка вида …/display/ПРОСТРАНСТВО/Заголовок номера не содержит — спрашиваем
        // его у Confluence, пока человек ещё на странице и может поправить адрес
        const unresolved = A.sync_roots.filter(r => (r.page_id||"").trim() &&
          !/^\d+$/.test(r.page_id.trim()) && !/pageId=\d+/.test(r.page_id));
        if (unresolved.length){
          e.target.textContent = t("proj.resolving");
          const res = await api("/api/confluence/resolve",{method:"POST",
            body:JSON.stringify({project:S.project.path, refs: unresolved.map(r=>r.page_id)})});
          (res.refs||[]).forEach(item=>{
            const row = A.sync_roots.find(r=>r.page_id === item.raw);
            if (!row) return;
            if (item.page_id){
              row.page_id = item.page_id;
              if (item.title && !row.title.trim()) row.title = item.title;
            } else if (item.error){
              toast(`${item.raw}: ${item.error}`, "err");
            }
          });
          e.target.textContent = t("proj.save");
          drawRoots();
          if ((res.refs||[]).some(i=>!i.page_id)){
            e.target.disabled = false;
            return;      // не пишем заведомо нерабочий корень
          }
        }
        // Один корень — одна строка: повтор по номеру страницы сводится при сохранении,
        // галочка «доверять» остаётся, если стояла хоть у одного из повторов.
        const seen = new Map(), before = A.sync_roots.length;
        A.sync_roots = A.sync_roots.filter(r => {
          const id = (r.page_id || "").trim();
          if (!id || !seen.has(id)){ if (id) seen.set(id, r); return true; }
          const first = seen.get(id);
          first.trusted = first.trusted || r.trusted;
          if (!(first.title || "").trim()) first.title = r.title;
          return false;
        });
        if (A.sync_roots.length < before){ drawRoots(); toast(t("proj.roots_deduped")); }
        const r = await api("/api/setup",{method:"POST",
          body:JSON.stringify({project:S.project.path, ...A})});
        e.target.disabled = false;
        if (r.ok){
          toast(t("proj.saved"));
          setDirty("form", t("dirty.form"), false);
          S.state = await api("/api/state");
          S.project = S.state.projects.find(p=>p.path===S.project.path) || S.project;
          renderOverview(); renderProject();
        }
      }),
      el("span",{class:"muted",style:"font-size:12px"}, t("proj.slug_note")))));

  // 3. доступы
  const tokBox = sgroup(box, "project:tokens", t("tokens.title", {project: S.project.name}));
  const tk = {};
  const tokenField = (key, label, filled) => el("label",{style:"display:block;margin-bottom:12px"},
    el("div",{class:"row",style:"gap:8px;margin-bottom:4px"},
      el("span",{class:"muted",style:"font-size:12px"}, label),
      el("span",{class:"chip "+(filled?"ok":"warn")},
        filled ? t("tokens.filled") : t("tokens.empty"))),
    watch(el("input",{class:"btn",type:"password",autocomplete:"off",
      style:"width:100%;font-weight:400",
      placeholder: filled ? t("tokens.replace_ph") : t("tokens.ph"),
      oninput:e=>tk[key]=e.target.value}), "tokens", t("dirty.tokens")));
  tokBox.append(el("div",{class:"card",style:"padding:20px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 14px"},
      t("tokens.about")),
    tokenField("CONFLUENCE_PERSONAL_TOKEN","Confluence", S.project.confluence_token),
    tokenField("JIRA_PERSONAL_TOKEN","Jira", S.project.jira_token),
    saveButton("tokens", t("dirty.tokens"), t("tokens.save"), async ()=>{
      if (!Object.values(tk).some(v=>(v||"").trim())) return toast(t("tokens.nothing"), "warn");
      const r = await api("/api/tokens",{method:"POST",
        body:JSON.stringify({project:S.project.path, ...tk})});
      if (r.ok){ toast(t("tokens.saved")); setDirty("tokens", t("dirty.tokens"), false);
        S.state = await api("/api/state");
        S.project = S.state.projects.find(p=>p.path===S.project.path) || S.project;
        renderProject(); }
    })));

  // 4. MCP-серверы — проекта и машины. Машинные здесь только для чтения: они работают в
  // каждом проекте, а правятся в «Настройке кита» — как кольцо шлюзов.
  MCPK.project.box = sgroup(box, "project:mcp", t("mcp.title", {project: S.project.name}));
  MCPK.project.probe = null;
  await renderMcp("project");

  // 5. конфиг как текст — для того, чего нет в форме
  const yamlBox = sgroup(box, "project:yaml", t("yaml.title"));
  const cfg = await api("/api/config?project=" + encodeURIComponent(S.project.path));
  const ta = watch(el("textarea",{class:"mono",spellcheck:"false",
    style:"width:100%;min-height:340px;background:var(--console-bg);color:var(--console-ink);"+
          "border:1px solid var(--border);"+
          "border-radius:12px;padding:14px 16px;font-size:12.5px;line-height:1.6;resize:vertical"},
    cfg.text || ""), "yaml", t("dirty.yaml"));
  yamlBox.append(el("div",{class:"card",style:"padding:18px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 12px"},
      t("yaml.about")),
    ta,
    el("div",{class:"row",style:"margin-top:12px"},
      saveButton("yaml", t("dirty.yaml"), t("yaml.save"), async ()=>{
        const r = await api("/api/config",{method:"POST",
          body:JSON.stringify({project:S.project.path, text: ta.value})});
        if (r.ok){ toast(r.unchanged ? t("yaml.unchanged") : t("yaml.saved", {backup: r.backup}));
          setDirty("yaml", t("dirty.yaml"), false);
          S.state = await api("/api/state");
          S.project = S.state.projects.find(p=>p.path===S.project.path) || S.project;
          renderOverview(); }
      }),
      el("button",{class:"btn sm",onclick:()=>{ if (confirmLeave()) renderProject(); }},
        t("yaml.reread")),
      el("span",{class:"muted",style:"font-size:12px"}, t("yaml.note")))));

  await renderModelsCard(box, "project");
  renderGitCard(box);
  await renderKinds(box);
  drawSetupJump(box);
}

/* ---------------- скины ---------------- */
// Оформление живёт в отдельных файлах: свой .css в cockpit/skins/ появляется в списке сам.
async function loadSkins(){
  const sel = $("#skinSel");
  const d = await api("/api/skins", {quiet:true});
  const list = (d && d.skins) || [];
  // по умолчанию «Зин»; список отсортирован по алфавиту, и брать из него первый
  // попавшийся значило бы менять облик панели от появления нового файла
  const has = id => list.some(s => s.id === id);
  const saved = localStorage.getItem("aurora-skin");
  const chosen = (saved && has(saved)) ? saved
               : (has("zine") ? "zine" : (list[0] && list[0].id) || "zine");
  sel.innerHTML = "";
  // Скин красит и то, чего в панели ещё не было, когда его писали: новый элемент выйдет
  // в цветах по умолчанию, и по виду это не отличить от задуманного. Поэтому у отставшего
  // скина версия стоит прямо в списке.
  list.forEach(s => sel.append(el("option",{value:s.id,
    title: t("skin.about_for", {about: s.about, "for": s["for"]}),
    selected: s.id===chosen ? "" : null},
    s.name + (s.behind ? t("skin.behind", {"for": s["for"]}) : ""))));
  await applySkin(chosen);
  const stale = s => s && s.behind &&
    toast(t("skin.stale", {name: s.name, "for": s["for"], kit: S.state.kit.version}), "warn");
  stale(list.find(x=>x.id===chosen));
  sel.onchange = async () => {
    await applySkin(sel.value);
    const s = list.find(x=>x.id===sel.value);
    if (s && s.about) toast(t("skin.picked", {name: s.name, about: s.about.slice(0,110)}), "ok");
    stale(s);
  };
}
async function applySkin(id){
  try{
    const r = await fetch(`/api/skin?id=${encodeURIComponent(id)}&t=${encodeURIComponent(TOKEN)}`);
    if (!r.ok) throw new Error(t("skin.no_such"));
    $("#skin").textContent = await r.text();
    localStorage.setItem("aurora-skin", id);
  }catch(e){
    // запасные токены остались в разметке — панель читается даже без скина
    toast(t("skin.failed"), "warn");
  }
}

/* ---------------- модули ----------------
   Раздел панели — папка в `cockpit/modules/<id>/`: манифест, разметка, скрипт, строки.
   Ядро знает о модуле ровно три вещи: где он в меню, нужен ли ему выбранный проект и
   как его показать. Внутрь модуля ядро не смотрит, модуль в ядро ходит только через ctx —
   поэтому раздел можно выключить, заменить или написать свой, не трогая панель.

   Разметка и скрипт приезжают по требованию: раздел, в который не заходили, не стоит
   ни одного запроса и ни одного байта разобранного кода. */
const MODULES = new Map();

function moduleTitle(m){
  return (m.name && (m.name[S.lang] || m.name.ru)) || m.id;
}

async function loadModules(){
  // Старый процесс сервера про модули не знает: файлы на диске новые, а отвечает
  // прежний код. Панель обязана подняться и в этом виде — с полосой «обновилась на
  // диске, работает прежним процессом» и кнопкой перезапуска, а не с пустым экраном.
  let d = null;
  try { d = await api("/api/modules", {quiet:true}); }
  catch (e){ console.warn("разделы не спрошены:", e.message); return; }
  for (const m of (d && d.modules) || []){
    if (m.error){
      // Молча пропасть раздел не имеет права: человек ищет кнопку, которой нет, и
      // решает, что сломалась панель.
      toast(t("modules.broken", {id: m.id, why: m.error}), "err");
      continue;
    }
    MODULES.set(m.id, {manifest: m, mod: null, section: null});
    addModuleNav(m);
    if (m.behind)
      console.warn(`модуль ${m.id} собран под ядро ${m["for"]}, установлено ${S.state.kit.version}`);
  }
}

function addModuleNav(m){
  const group = $(`.navgroup[data-group="${m.group}"]`) || $('.navgroup[data-group="project"]');
  if (!group || $(`nav button[data-view="${m.id}"]`)) return;
  const btn = el("button", {"data-view": m.id, "data-order": m.order, "aria-label": moduleTitle(m)},
    el("span", {class:"label"}, moduleTitle(m)),
    el("span", {class:"count", id:"nav-" + m.id}));
  if (m.dev){            // раздел в разработке — виден вместе с «Разработкой»
    btn.dataset.devonly = "";
    btn.hidden = !devSectionsOn();
  }
  btn.onclick = () => navClick(btn);
  const after = $$("button", group).find(b => Number(b.dataset.order || 999) > m.order);
  group.insertBefore(btn, after || null);
}

// Всё, чем ядро делится с разделом. Список короткий намеренно: чем шире вход, тем
// сильнее модуль прирастает к панели и тем меньше смысла в самой папке.
function moduleCtx(id, root){
  return {
    id, root,
    t, el, api, toast,
    $: (sel) => $(sel, root),
    $$: (sel) => $$(sel, root),
    token: TOKEN,
    show, run: runStep, runWatched, busyElsewhere, openRun, openDoc, reloadHealth,
    // Маршрут ведёт ядро: он переживает перезагрузку вкладки, пишет в проект
    // и продолжается с той же точки. Раздел только показывает список.
    runRoute, poll,
    scenarios: () => S.scenarios || (S.scenarios = api("/api/scenarios")),
    restartPanel,
    fmt: {ago, kb, when: histWhen, esc, tick, md, mdLite, rtime, howLong},
    // Общие детали панели: раздел не рисует свою плитку метрики и свою кнопку
    // перехода к маршруту — иначе в каждом разделе они разъедутся.
    ui: {metricCard, metric, goRoute, goCmd, kindChip, skillLine, copyButton, engineWord,
         editor: markdownEditor},
    openPath, isEngineCmd, hideDev, openProject,
    // Журнал запусков ведёт ядро: отметка «последний запуск» стоит в
    // нескольких разделах сразу, и считать её каждому по-своему нельзя.
    runs: {last: lastRun, mark: rcMark},
    // Цвет проекта и его причина считаются в ядре: тот же ответ нужен Мостику.
    aura: auraWhy,
    get project(){ return S.project; },
    get state(){ return S.state; },
    get health(){ return S.health; },
    get lang(){ return S.lang; },
    get view(){ return S.view; },
  };
}

async function mountModule(id, payload){
  const rec = MODULES.get(id);
  if (!rec) return;
  if (!rec.mod){
    try{
      const url = f => `/modules/${id}/${f}?t=${encodeURIComponent(TOKEN)}`;
      const r = await fetch(url("view.html"));
      if (!r.ok) throw new Error(t("modules.no_html", {status: r.status}));
      rec.section = el("section", {class:"view", id:"view-" + id, html: await r.text()});
      $("main").append(rec.section);
      applyI18n(rec.section);
      const mod = await import(url("view.js"));
      rec.mod = mod.default || mod;
      await rec.mod.mount?.(moduleCtx(id, rec.section));
    }catch(e){
      rec.mod = null;
      if (rec.section){ rec.section.remove(); rec.section = null; }
      toast(t("modules.failed", {id, why: e.message}), "err");
      return;
    }
  }
  rec.section.classList.toggle("on", S.view === id);
  // Груз переходa: «открой справку на этом документе», «покажи карточку на
  // графе». Без него раздел пришлось бы дёргать через глобальную переменную.
  await rec.mod.refresh?.(moduleCtx(id, rec.section), payload);
}

// Данные сменились — выбрали проект, пересчитали здоровье, сохранили настройку. Разделы,
// уже поднятые в этой вкладке, обязаны перерисоваться сами: раздел, открытый до выбора
// проекта, иначе остаётся с «выберите проект» навсегда, и перерисовать его некому.
async function refreshModules(){
  for (const [id, rec] of MODULES){
    if (!rec.mod) continue;
    // Упавший раздел не уносит остальные: иначе одна ошибка в одном экране оставляет
    // всю панель с вчерашними числами, и причину видно только в консоли браузера.
    try { await rec.mod.refresh?.(moduleCtx(id, rec.section)); }
    catch (e){ showFault(t("modules.refresh_failed", {id, why: e.message})); }
  }
}

// Подписи разделов в меню живут в манифесте, а не в каталоге строк: сменили язык —
// перечитываем их, иначе меню останется на прежнем языке до перезагрузки страницы.
function relabelModules(){
  for (const [id, rec] of MODULES){
    const label = $(`nav button[data-view="${id}"] .label`);
    if (label) label.textContent = moduleTitle(rec.manifest);
  }
}

// Запуск, за которым человек смотрит в консоли. Вынесено из «Отчётов»: так же ведёт
// себя любой раздел, откуда запускают долгую команду, — консоль одна на панель.
async function runWatched(cmd, args = []){
  if (await busyElsewhere(cmd)) return {rc: -1, busy: true};
  show("console");
  $("#consoleApply").innerHTML = "";
  $("#consoleOut").innerHTML = "";
  $("#consoleCmd").textContent = (cmd + " " + args.join(" ")).trim();
  $("#consoleRc").textContent = t("run.running");
  $("#consoleRc").className = "chip";
  const res = await runStep(cmd, args);
  $("#consoleRc").textContent = res.rc === 0 ? t("run.done") : t("run.rc", {rc: res.rc});
  $("#consoleRc").className = "chip " + (res.rc === 0 ? "ok" : "err");
  return res;
}

/* ---------------- быстрый старт ---------------- */
// Какие маршруты человек развернул: решение принимает он, и оно должно пережить
// перерисовку от прихода здоровья или прогона команды.
const OPEN_ROUTES = new Set();

function howLong(secs){
  if (secs < 60) return t("long.sec", {n: secs});
  const m = Math.round(secs / 60);
  return m < 60 ? t("long.min", {n: m})
                : t("long.hour", {h: Math.floor(m / 60), m: m % 60});
}

function skillLine(text){
  return el("div",{class:"row",style:"gap:8px;margin-top:6px"},
    el("span",{class:"muted",style:"font-size:12px"}, t("skill.to_chat")),
    el("code",{class:"tick",style:"font-size:12.5px"}, text));
}
function copyButton(text){
  const b = el("button",{class:"btn sm",onclick:async()=>{
    try { await navigator.clipboard.writeText(text); toast(t("copy.done", {text}), "ok"); }
    catch(e){ toast(t("copy.manual", {text}), "warn"); }
  }}, t("copy.btn"));
  return b;
}

/* ---------------- разработка движка ---------------- */
/* Раздел открывается семью нажатиями на «О проекте» — как «Для разработчиков» в Android.
   Прятать его нужно не ради секретности, а ради честности интерфейса: команды `dev:`
   относятся к самому движку, аналитику они ничего не дают и только шумят в меню. */
/* Модели — одни на кит и все его проекты (раздел «Модели»). Здесь — только где они живут:
   прежняя карточка «Агент» писала переменные в `.env` кита или проекта, и проект мог молча
   перекрыть модель кита. С 1.153.0 проектной настройки моделей нет. */
async function renderModelsCard(box, scope){
  const a = await api("/api/agent", {quiet:true});
  const chains = (a && a.backends || []).filter(b => b.chat).length;
  sgroup(box, scope === "project" ? "project:agent" : "setup:agent",
         t("models_link.title")).append(el("div",{class:"card",style:"padding:16px 20px;margin-bottom:18px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 10px"},
      scope === "project" ? t("models_link.project") : t("models_link.kit")),
    el("div",{class:"row",style:"gap:10px"},
      el("span",{class:"chip " + (chains ? "ok" : "warn")},
        chains ? t("models_link.ready", {n: chains}) : t("models_link.empty")),
      el("button",{class:"btn sm primary", onclick:()=>show("models")}, t("models_link.open")))));
}

// Git проекта настраивается в своём разделе: сервер, вход, автоматика. Здесь — только
// указатель на него, чтобы настройка не жила в двух местах.
function renderGitCard(box){
  const p = S.project;
  sgroup(box, "project:git", t("git_link.title")).append(el("div",{class:"card",style:"padding:16px 20px;margin-bottom:18px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 10px"}, t("git_link.about")),
    el("div",{class:"row",style:"gap:10px"},
      p && p.git_alert ? el("span",{class:"chip bad"}, t("git_link.alert")) : null,
      el("button",{class:"btn sm primary", onclick:()=>show("gitsync")}, t("git_link.open")))));
}

/* ---------------- о проекте ---------------- */
// Открыть документ — это переход в справку с именем файла: сам показ делает раздел.
function openDoc(path){
  show("reference", {doc: path});
}
function md(src){
  if (src.startsWith("---")){            // YAML-шапка документа читателю не нужна
    const end = src.indexOf("\n---", 3);
    if (end !== -1) src = src.slice(src.indexOf("\n", end + 1) + 1);
  }
  const lines = src.split("\n"); let out=[], inCode=false, inTable=false;
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g,"<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="#" onclick="return false">$1</a>');
  for (let i=0;i<lines.length;i++){
    const l = lines[i];
    if (l.startsWith("```")){ out.push(inCode?"</code></pre>":"<pre><code>"); inCode=!inCode; continue; }
    if (inCode){ out.push(esc(l)); continue; }
    if (/^\s*\|.*\|\s*$/.test(l)){
      const cells = l.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
      if (cells.every(c=>/^:?-{2,}:?$/.test(c))) continue;
      if (!inTable){ out.push('<div class="scroll-x"><table>'); inTable=true;
        out.push("<tr>"+cells.map(c=>`<th>${inline(c)}</th>`).join("")+"</tr>"); continue; }
      out.push("<tr>"+cells.map(c=>`<td>${inline(c)}</td>`).join("")+"</tr>"); continue;
    } else if (inTable){ out.push("</table></div>"); inTable=false; }
    const h = l.match(/^(#{1,4})\s+(.*)$/);
    if (h){ out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^>\s?/.test(l)){ out.push("<blockquote>"+inline(l.replace(/^>\s?/,""))+"</blockquote>"); continue; }
    if (/^\s*[-*]\s+/.test(l)){ out.push("<li>"+inline(l.replace(/^\s*[-*]\s+/,""))+"</li>"); continue; }
    if (/^\s*\d+[.)]\s+/.test(l)){ out.push("<li>"+inline(l.replace(/^\s*\d+[.)]\s+/,""))+"</li>"); continue; }
    if (!l.trim()){ out.push(""); continue; }
    out.push("<p>"+inline(l)+"</p>");
  }
  if (inTable) out.push("</table></div>");
  return out.join("\n").replace(/(<li>[\s\S]*?<\/li>\n?)+/g, m=>"<ul>"+m+"</ul>");
}

/* ---------------- палитра ⌘K ---------------- */
let PAL = {items:[], sel:0};
function openPalette(){
  // Разделы берём из меню, а не из списка рядом: список приходилось править при каждом
  // переезде раздела в папку, и он молча расходился — пункт вёл в никуда или пропадал.
  // Заодно подписи приходят на текущем языке: меню их уже знает.
  PAL.items = [
    ...$$("nav button").filter(b => !b.hidden).map(b => ({
      nm: b.querySelector(".label").textContent,
      ds: t("palette.section"),
      go: () => navClick(b)})),
    // Два действия, которых в меню нет: это не разделы, а то, зачем в них заходят.
    {nm: t("palette.new_project"), ds: t("palette.section"), go: () => show("setup")},
    {nm: t("palette.update_kit"), ds: t("palette.section"), go: () => show("about")},
    ...S.state.projects.map(p=>({nm:p.name, ds: t("palette.project", {path: p.path}),
                                go:()=>pick(p)})),
    ...S.state.commands.map(c=>({nm:c.cmd, ds:c.what, go:()=>openRun(c.cmd)})),
  ];
  $("#paletteOverlay").classList.add("on");
  const inp = $("#paletteInput"); inp.value=""; inp.focus(); drawPalette("");
}
function drawPalette(q){
  q = q.toLowerCase();
  const hits = PAL.items.filter(i=>(i.nm+" "+i.ds).toLowerCase().includes(q)).slice(0,40);
  PAL.hits = hits; PAL.sel = 0;
  const box = $("#paletteResults"); box.innerHTML="";
  hits.forEach((h,idx)=>box.append(el("button",{class:"res"+(idx===0?" sel":""),
    onclick:()=>{ $("#paletteOverlay").classList.remove("on"); h.go(); }},
    el("span",{class:"nm mono"}, h.nm), el("span",{class:"ds",html:tick(h.ds)}))));
}
$("#paletteInput").addEventListener("input", e=>drawPalette(e.target.value));
$("#paletteInput").addEventListener("keydown", e=>{
  const items = $$("#paletteResults .res");
  if (e.key==="ArrowDown" || e.key==="ArrowUp"){
    e.preventDefault();
    PAL.sel = Math.max(0, Math.min(items.length-1, PAL.sel + (e.key==="ArrowDown"?1:-1)));
    items.forEach((n,i)=>n.classList.toggle("sel", i===PAL.sel));
    items[PAL.sel]?.scrollIntoView({block:"nearest"});
  }
  if (e.key==="Enter"){ items[PAL.sel]?.click(); }
  if (e.key==="Escape"){ $("#paletteOverlay").classList.remove("on"); }
});
$("#paletteBtn").onclick = openPalette;
$("#paletteOverlay").onclick = e => { if (e.target.id==="paletteOverlay") e.currentTarget.classList.remove("on"); };
document.addEventListener("keydown", e=>{
  if ((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==="k"){ e.preventDefault(); openPalette(); }
  if (e.key==="Escape"){ closeRun(); closeMcp(); $("#paletteOverlay").classList.remove("on"); }
});

/* ---------------- обработчики раздела «Файлы» ---------------- */
$("#fileSearch")?.addEventListener("input", drawTree);
$("#fileSave")?.addEventListener("click", saveFile);
$("#fileFolder")?.addEventListener("click", ()=>revealFile("folder"));
$("#fileOpen")?.addEventListener("click", ()=>revealFile("open"));
$("#filePub")?.addEventListener("click", publishFromEditor);
$("#fileRename")?.addEventListener("click", renameFile);
// Перетаскивание ширины: имена карточек длинные и у каждого проекта свои — одной
// ширины на всех не бывает. Запоминаем выбранную, чтобы не настраивать каждый раз.
(function(){
  const split = $("#filesSplit"), panel = $("#filesPanel");
  if (!split || !panel) return;
  const saved = parseInt(localStorage.getItem("aurora-files-width") || "0", 10);
  if (saved >= 200) panel.style.flexBasis = saved + "px";
  let from = 0, base = 0;
  const move = e => {
    const w = Math.max(200, Math.min(900, base + (e.clientX - from)));
    panel.style.flexBasis = w + "px";
  };
  const stop = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", stop);
    document.body.style.userSelect = "";
    localStorage.setItem("aurora-files-width", parseInt(panel.style.flexBasis, 10) || 320);
  };
  split.addEventListener("mousedown", e => {
    from = e.clientX; base = panel.getBoundingClientRect().width;
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", stop);
    e.preventDefault();
  });
})();
$("#fileDelete")?.addEventListener("click", deleteFile);
$("#fileMode")?.addEventListener("change", async e=>{
  localStorage.setItem("aurora-editor-mode", e.target.value);
  if (F.path && F.ed) await mountEditor(F.dirty ? F.ed.getValue() : F.orig);
});
document.addEventListener("keydown", e=>{
  if ((e.metaKey || e.ctrlKey) && e.key === "s" && S.view === "files"){
    e.preventDefault();
    if (!$("#fileSave").disabled) saveFile();
  }
});

boot();
