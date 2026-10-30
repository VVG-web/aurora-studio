// Токена в странице нет: он приходит в обмен на одноразовый код из адреса (`/?c=…`, его
// печатает `aurora.py cockpit`) и живёт в памяти страницы, а на перезагрузку вкладки — в
// sessionStorage (он свой у каждой вкладки и пропадает вместе с ней). Каждый запрос несёт его
// заголовком `X-Aurora-Token`; в строку адреса токен не попадает: ни в историю браузера, ни в
// ссылки, ни в Referer.
let TOKEN = "";
const SESSION_KEY = "aurora-session";
// Версия панели: под какую версию ядра она собрана. Ядро уходит вперёд быстрее, чем
// интерфейс, и молча отставший интерфейс — худший вид отставания: он выглядит рабочим.
// Правило: младшая версия должна совпадать с ядром (1.11.x ↔ kit 1.11.y), иначе панель
// честно сообщает, что новых команд и метрик в ней может не быть. Проверяется тестом.
const UI_VERSION = "1.193.0";
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
// Кнопка, за которой стоит модель, несёт ещё и `data-llm` (вызов, команда, раздел или
// `route:<id>`): в подсказке появляется блок «Модели» — роли по шагам и живые цепочки кита
// из каталога вызовов движка. Вопрос «какая модель здесь отвечает и с какими
// инструментами» больше не задаётся разработчику.
function showHelp(node){
  const key = node.dataset.help, llm = node.dataset.llm;
  const has = !!key && t(key + ".what") !== key + ".what";
  if ((!has && !llm) || !document.body.contains(node)) return;
  hideHelp();
  helpBox = el("div", {class:"help-tip", role:"tooltip"},
    ...(has ? [el("b", {}, t(key + ".what")),
      el("div", {class:"k"}, t("help.example")), el("div", {}, t(key + ".how")),
      el("div", {class:"k"}, t("help.result")), el("div", {}, t(key + ".result"))] : []));
  if (llm){
    const mine = helpBox, part = el("div", {}, el("div", {class:"k"}, t("llm.k")), "…");
    helpBox.append(part);
    llmCalls().then(d => {
      if (helpBox !== mine) return;
      const calls = llmFor(d, llm);
      if (!calls.length){ if (has) part.remove(); else hideHelp(); return; }
      part.innerHTML = "";
      part.append(el("div", {class:"k"}, t("llm.k")), llmView(d, calls));
      placeHelp(node);
    });
  }
  document.body.append(helpBox);
  placeHelp(node);
}
function placeHelp(node){
  if (!helpBox) return;
  // Мерить — с левого края: у правого края подсказка с position:fixed ужимается в узкую
  // колонку, и перестановка после подгрузки блока «Модели» мерила уже ужатую.
  helpBox.style.left = "0px"; helpBox.style.top = "0px";
  const r = node.getBoundingClientRect(), b = helpBox.getBoundingClientRect();
  const top = (r.bottom + 8 + b.height <= innerHeight) ? r.bottom + 8 : Math.max(8, r.top - 8 - b.height);
  const left = Math.min(Math.max(8, r.left), innerWidth - b.width - 8);
  helpBox.style.top = top + "px"; helpBox.style.left = left + "px";
}
document.addEventListener("mouseover", e => {
  const node = e.target.closest && e.target.closest("[data-help],[data-llm]");
  if (!node || node.contains(e.relatedTarget)) return;
  hideHelp();
  helpTimer = setTimeout(() => showHelp(node), HELP_DELAY);
});
document.addEventListener("mouseout", e => {
  const node = e.target.closest && e.target.closest("[data-help],[data-llm]");
  if (node && !node.contains(e.relatedTarget)) hideHelp();
});
["mousedown", "keydown", "wheel", "scroll"].forEach(ev => document.addEventListener(ev, hideHelp, true));
// Причина «не хватает связи» из ответа сервера — строкой на языке страницы.
function netReason(r){
  if (r.code === "profile_offline")
    return r.source ? t("net.reason.profile_offline_src", {name: r.name, source: r.source}) : t("net.reason.profile_offline", {name: r.name});
  if (r.code === "internet_offline")
    return r.source ? t("net.reason.internet_offline_src", {source: r.source}) : t("net.reason.internet_offline");
  if (r.code === "llm_down") return t("net.reason.llm_down");
  return r.code;
}
// Диалог запуска без связи (`info` — ответ 409 сервера). → "partial" | "queue" | null (отмена).
// Escape и щелчок мимо окна — отмена; новый вопрос закрывает прежний отменой, чтобы тот не завис без ответа.
let NET_CLOSE = null;
function askNet(info){
  if (NET_CLOSE) NET_CLOSE(null);
  return new Promise(done => {
    const ov = $("#netOverlay"), box = $("#netDialog");
    const close = v => { if (NET_CLOSE !== close) return; NET_CLOSE = null; ov.classList.remove("on"); box.innerHTML = ""; done(v); };
    NET_CLOSE = close;
    const items = info.cmd
      ? [{cmd: info.cmd, blocked: info.blocked}]
      : info.items || [];
    const has = c => (info.choices || []).includes(c);
    box.innerHTML = "";
    box.append(el("h2", {style:"margin:0 0 8px"}, t("net.dialog_title")),
      el("p", {class:"muted", style:"font-size:13.5px;margin:0 0 10px"},
        info.cmd ? t("net.dialog_intro_cmd", {cmd: info.cmd})
                 : t("net.dialog_intro_route", {title: info.title || info.route, n: items.length, total: info.total || items.length})),
      ...items.slice(0, 12).map(x => el("div", {style:"margin:4px 0"},
        el("span", {class:"mono", style:"font-weight:600"}, x.cmd), " — ",
        (x.blocked || []).map(netReason).join("; "))),
      items.length > 12 ? el("div", {class:"muted"}, t("net.dialog_more", {n: items.length - 12})) : null,
      el("div", {class:"muted", style:"font-size:12.5px;margin:12px 0 4px"},
        has("partial") ? t("net.hint_partial") : null),
      el("div", {class:"muted", style:"font-size:12.5px;margin:0 0 14px"},
        info.cmd ? t("net.hint_queue_cmd") : t("net.hint_queue_route")),
      el("div", {class:"row", style:"gap:8px;flex-wrap:wrap"},
        has("partial") ? el("button", {class:"btn primary", onclick: () => close("partial")}, t("net.btn_partial")) : null,
        has("queue") ? el("button", {class:"btn" + (has("partial") ? "" : " primary"), onclick: () => close("queue")}, t("net.btn_queue")) : null,
        el("div", {class:"spacer"}),
        el("button", {class:"btn sm", onclick: () => close(null)}, t("net.btn_cancel"))));
    ov.classList.add("on");
    setTimeout(() => box.querySelector("button")?.focus(), 40);
  });
}
const tick = s => esc(s).replace(/`([^`]+)`/g, '<code class="tick">$1</code>');
const esc = s => String(s??"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const api = async (path, opts={}) => {
  const sep = path.includes("?") ? "&" : "?";
  // Язык интерфейса сервер знает от страницы: описания команд и маршруты приходят от него
  // готовыми. `S` объявлен ниже, а первый запрос идёт до выбора языка — тогда русский.
  const lang = (!path.includes("lang=") && typeof S !== "undefined" && S.lang && S.lang !== "ru")
    ? sep + "lang=" + encodeURIComponent(S.lang) : "";
  const r = await fetch(path + lang, {...opts, headers:{"Content-Type":"application/json",
    "X-Aurora-Token": TOKEN, ...(opts.headers||{})}});
  const d = await r.json();
  // Проверка соединений: запуск, которому не хватает связи, спрашивает человека — запустить только доступное,
  // встать в очередь или отменить. Выбор уходит тем же запросом в поле `net`.
  if (path === "/api/run" && r.status === 409 && d.net_blocked && !opts.netAsked){
    const choice = await askNet(d.net_blocked);
    if (!choice) return {cancelled: true};
    return api(path, {...opts, netAsked: true, body: JSON.stringify({...JSON.parse(opts.body), net: choice})});
  }
  // Токен не принят — панель запущена заново или вкладка старая: поток отказов превращается
  // в одно понятное окно «обновите страницу», а не в тост на каждый опрос.
  if (r.status === 403 && d.code === "session"){ showAuthGate(); return d; }
  if (d.error && !opts.quiet) toast(d.error, "err");
  return d;
};

// Окно «нет доступа»: токена нет или он не подошёл. Закрыть его нельзя — без токена панель
// всё равно не получит ни одного ответа; выход один: перечитать страницу или взять новую
// ссылку у команды `aurora.py cockpit`.
function showAuthGate(){
  if ($("#authGate")) return;
  try { sessionStorage.removeItem(SESSION_KEY); } catch (e) { /* хранилище закрыто */ }
  TOKEN = "";
  $("body").append(el("div", {id:"authGate", role:"alertdialog",
      style:"position:fixed;inset:0;z-index:9999;display:flex;align-items:center;"
          + "justify-content:center;background:var(--bg);padding:16px"},
    el("div", {class:"card", style:"max-width:540px;padding:24px"},
      el("h2", {style:"margin:0 0 12px"}, t("auth.title")),
      el("p", {}, t("auth.body")),
      el("p", {html: tick(t("auth.how"))}),
      el("button", {class:"btn primary", onclick:()=>location.reload()}, t("auth.reload")))));
}

// Войти: код из адреса → токен (код тут же стирается из адреса), иначе токен этой вкладки.
// Адрес прежней версии (`?t=`) принимается один раз — на случай вкладки, которую панель
// перезапустила «из панели»: токен у неё прежний, а адрес в строке браузера старый.
async function acquireSession(){
  const q = new URLSearchParams(location.search);
  const code = q.get("c"), old = q.get("t");
  try {
    if (code){
      const r = await fetch("/api/session", {method:"POST", body: JSON.stringify({code}),
        headers:{"Content-Type":"application/json"}});
      TOKEN = (await r.json()).token || "";
    } else if (old) TOKEN = old;
    else TOKEN = sessionStorage.getItem(SESSION_KEY) || "";
  } catch (e) { TOKEN = TOKEN || ""; }
  if (code || old) history.replaceState(null, "", location.pathname + location.hash);
  if (TOKEN){
    try {
      const r = await fetch("/api/ping", {headers:{"X-Aurora-Token": TOKEN}});
      if (r.status === 403) TOKEN = "";
    } catch (e) { /* панель не ответила — не значит, что токен плох */ }
  }
  if (!TOKEN){ showAuthGate(); return false; }
  try { sessionStorage.setItem(SESSION_KEY, TOKEN); } catch (e) { /* хранилище закрыто */ }
  return true;
}

// Ссылка, которую открывает сам браузер (отчёт в новой вкладке), заголовка не несёт: сервер
// выдаёт на неё билет — один адрес, одно открытие. Вкладку открываем сразу, пока длится жест
// человека: после ожидания ответа блокировщик окон её отрезал бы.
async function openLink(path){
  const w = window.open("about:blank", "_blank");
  if (w) w.opener = null;
  const d = await api("/api/link", {method:"POST", body: JSON.stringify({path})});
  if (!d.url){ if (w) w.close(); return; }
  if (w) w.location.href = d.url; else window.open(d.url, "_blank", "noopener");
}
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
  if (vars) for (const [k,v] of Object.entries(vars)) s = s.replaceAll("{"+k+"}", () => String(v));   // функцией: «$'» в тексте не шаблон
  return s;
}

async function restartPanel(opts = {}){
  // Токен новый процесс получает от текущего, поэтому открытая вкладка продолжит
  // работать: адрес тот же, и после подъёма достаточно перечитать страницу.
  // `patient` — после обновления кита: первый старт новой версии собирает реестр команд
  // (на нагруженной машине — больше минуты), и ждать надо дольше, говоря, чего ждём.
  // Возвращает false, если человек передумал, и {error}, если сервер отказал (причина — в
  // error: тост через несколько секунд пропадёт, а раздел покажет её постоянно): в обоих
  // случаях старая панель жива, и вызывающий раздел вернёт кнопке рабочий вид. Возвращает
  // "timeout", если старая панель уже ушла, а новая не ответила за отведённое время.
  const say = opts.progress || (text => toast(text));
  const busy = await api("/api/jobs", {quiet:true});
  const live = (busy && busy.jobs || []).filter(j => !j.done);
  if (live.length && !confirm(
      t("restart.busy_ask", {what: live.map(j => j.cmd).join(", ")}))) return false;
  const d = await api("/api/restart", {method:"POST", quiet:true, body:"{}"});
  if (d && d.error){ toast(d.error, "err"); return {error: d.error}; }
  say(t("restart.rising"));
  const started = Date.now(), limit = (opts.patient ? 300 : 60) * 1000;
  let told = "";
  await new Promise(ok => setTimeout(ok, 1500));      // прежний процесс ещё отвечает
  while (Date.now() - started < limit){
    try {
      const r = await fetch("/api/ping", {headers:{"X-Aurora-Token": TOKEN}});
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
  return "timeout";
}

// Кнопка полосы «панель обновилась на диске»: отказ сервера остаётся в полосе, а не только
// в тосте на несколько секунд — старая панель жива, и повторять попытку человек будет здесь.
async function restartFromBar(ev){
  const bar = ev.currentTarget.closest(".stalebar");
  const r = await restartPanel();
  if (bar && r && r.error){
    bar.querySelectorAll(".warnbox").forEach(n => n.remove());
    bar.append(el("div", {class:"warnbox"}, r.error));
  }
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
    const st = await api("/api/state?part=extra", {quiet:true});
    if (st && st.commands){ S.state.commands = st.commands; S.state.env = st.env; }
    S.scenarios = null;     // маршруты тоже приходят на языке интерфейса — спросим заново
    // и итог «Здоровья»: находки доктора и описания источников — тоже на языке
    S.state.projects.forEach(x => { x.health = null; });
    S.health = null;
    if (S.project) loadHealth(S.project);
    loadSkins();            // и названия скинов в списке
    await refreshModules();   // и содержимое тех, что уже подняты
    toast(t("lang.switched",
                {name: sel.options[sel.selectedIndex].text.split(" · ")[0]}));
  };
}

// Роль рабочего места (BL-ARC-04): строка «Роль» в «Настройке» видна, только когда включён фиче-флаг
// «роли для профилей» (раздел «Разработка»). Выбор пишет `local/profile.json` на сервере; меню и маршруты
// сервер отдаёт уже отфильтрованными, поэтому после записи страница перечитывается. Флаг выключен —
// карточка скрыта и ничего не пишется.
async function drawRolePicker(){
  const card = $("#roleCard");
  if (!card) return;
  const d = await api("/api/profiles", {quiet:true});
  card.hidden = !(d && d.enabled);
  if (card.hidden) return;
  const sel = $("#roleSel");
  sel.innerHTML = "";
  for (const p of d.profiles)
    sel.append(el("option", {value: p.id, selected: p.id === d.active ? "" : null}, p.name));
  sel.value = d.active;
  const about = () => { const p = d.profiles.find(x => x.id === sel.value); return p ? p.about : ""; };
  $("#roleNote").textContent = about();
  // Опечатки и отвергнутые пресеты — строкой под выбором: роль, которая молча «не сработала», хуже шума.
  $("#roleWarn").textContent = (d.warn || []).join(" · ");
  sel.onchange = async () => {
    const r = await api("/api/profile/choose", {method: "POST", quiet: true, body: JSON.stringify({id: sel.value})});
    if (!r || !r.ok){
      toast((r && r.error) || t("role.failed"), "err");
      return drawRolePicker();
    }
    location.reload();
  };
}


/* ---------------- граф базы ---------------- */
/* ---------------- файлы проекта ---------------- */
const F = { files:[], path:null, digest:"", ed:null, ro:"", loaded:false, orig:"", fm:"",
            filter:"all", open:new Set(), recent:[], changed:new Set(), createDirs:[], reopen:"" };
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
  if (F.reopen) afterPublish(F.reopen);       // файл переписала публикация, пока «Файлы» были скрыты
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
  if ($("#filePubCard") && $("#filePubCard").dataset.rel !== f.path) $("#filePubCard").remove();   // карточка публикации — другого файла
  $("#fileNone").hidden = true; $("#fileEditor").hidden = false;
  $("#fileName").textContent = f.path;
  $("#fileEnc").hidden = true;
  if (!f.text){
    destroyEditor();
    F.publishable = false; $("#filePub").hidden = true; $("#filePubNote").hidden = true;       // кнопка и причина прежнего файла к этому не относятся
    $("#fileWarn").hidden = false; $("#fileWarn").textContent = t("files.binary");
    $("#fileSave").disabled = true; F.path = f.path; F.ro = t("files.not_text");
    return;
  }
  const d = await api("/api/files/read?project=" + encodeURIComponent(S.project.path)
                      + "&path=" + encodeURIComponent(f.path));
  if (d.error){
    // Отказ (секрет, служебное, нет файла) уже показан тостом из `api`; пустой редактор с именем
    // отказанного файла в шапке причины не объяснит и выглядит как открывшийся файл.
    destroyEditor(); F.path = null; F.dirty = false;
    $("#fileEditor").hidden = true; $("#fileNone").hidden = false;
    return;
  }
  if ((d.text || "").length > EDIT_LIMIT){
    destroyEditor();
    $("#fileFm").hidden = true;
    $("#fileStale").hidden = true;
    F.publishable = false; $("#filePub").hidden = true; $("#filePubNote").hidden = true;
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
  F.lossy = !!d.lossy;
  if (d.readonly_lossy) F.ro = t("editor.lossy_ro");   // другая причина запрета важнее и остаётся
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
  // «Опубликовать»: состояние считает сервер (`file_read` → `publish`), страница его рисует. Раньше она гадала
  // по глобалу страницы со списком видов, который перестали присваивать при переносе раздела в модуль, — и кнопка пропала у всех
  // артефактов видов. Запрет обязан заканчиваться действием: серая кнопка называет причину и ведёт в настройки.
  drawPublish(d);
  $("#fileWarn").hidden = true;
  // Кодировка и окончания строк файла сервер сохранит как были; показываем отступление
  // от привычных utf-8/LF, чтобы «почему весь файл в диффе» не искали в редакторе.
  const enc = d.encoding || "utf-8", eol = (d.eol || "lf").toUpperCase();
  $("#fileEnc").hidden = enc === "utf-8" && eol === "LF" && !d.eol_mixed;
  $("#fileEnc").textContent = enc + " · " + eol + (d.eol_mixed ? " *" : "");
  $("#fileEnc").title = d.eol_mixed ? t("editor.enc_title_mixed", {eol}) : t("editor.enc_title");
  if (d.lossy){
    $("#fileWarn").hidden = false;
    $("#fileWarn").textContent = t("editor.lossy_warn");
  }
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
  // «Опубликовать» при несохранённом сохраняет тем же кликом — и говорит это надписью, а не окном подтверждения.
  if ($("#filePub")) $("#filePub").textContent = t(F.dirty ? "editor.publish_save" : "editor.publish");
  setDirty("file:" + F.path, t("dirty.file", {path: F.path}), F.dirty);
}

function destroyEditor(){
  if (F.ed && F.ed.destroy) { try { F.ed.destroy(); } catch(e){} }
  F.ed = null; $("#vditor").innerHTML = "";
  $("#filePreview").hidden = true; $("#filePreview").innerHTML = "";
  $("#fileBody").classList.remove("split"); $("#fileFormat").hidden = true;
}

/* ---------------- вид справа: mermaid, JSON, журналы, текст ---------------- */
// Markdown рисует сам редактор. Остальные текстовые файлы, у которых есть «вид», —
// схема mermaid, JSON, журнал, простой текст — правятся слева и видны справа: схему иначе
// приходилось копировать в чужой просмотрщик, а журнал в тысячи строк читать без
// подсветки ошибок. Всё рисуется здесь же, без сети: mermaid лежит в поставке редактора.
const VIEW_KINDS = [[/\.(mmd|mermaid)$/i, "mermaid"], [/\.json$/i, "json"],
                    [/\.(log|jsonl)$/i, "log"], [/\.txt$/i, "text"]];
const viewKind = path => (VIEW_KINDS.find(([re]) => re.test(path || "")) || [])[1] || "";
const VIEW_LINES = 5000;      // журнал длиннее рисуется хвостом: последние строки и есть свежие
let VIEW_TIMER = null;
function viewSoon(){ clearTimeout(VIEW_TIMER); VIEW_TIMER = setTimeout(drawView, 400); }
function drawView(){
  const kind = viewKind(F.path), box = $("#filePreview");
  if (!kind || !F.ed) return;
  // Пустые строки в конце — не содержание: редактор дописывает перевод строки сам.
  const text = F.ed.getValue().replace(/\s+$/, "");
  box.innerHTML = "";
  try {
    if (kind === "mermaid") viewMermaid(box, text);
    else if (kind === "json") viewJson(box, text);
    else if (kind === "log") viewLog(box, text);
    else viewText(box, text);
  } catch (e) {
    box.append(el("div",{class:"pv-err"}, String(e && e.message || e)));
  }
}
function viewMermaid(box, text){
  const code = el("div",{class:"language-mermaid"}, text);
  box.append(code);
  if (!text.trim()) { code.replaceWith(el("div",{class:"sub"}, t("view.empty"))); return; }
  // Vditor рисует блоки `.language-mermaid` своей копией mermaid из `/vendor` — без сети.
  try { Vditor.mermaidRender(box, "/vendor/vditor", document.documentElement.dataset.theme === "dark" ? "dark" : "classic"); }
  catch (e) { box.append(el("div",{class:"pv-err"}, t("view.mermaid_failed", {why: e.message}))); }
}
// Строка и столбец ошибки разбора: браузеры называют позицию символа, человеку нужна строка.
function jsonWhere(text, err){
  const m = /position (\d+)/i.exec(err.message || "");
  if (!m) return "";
  const pos = +m[1], before = text.slice(0, pos);
  return t("view.json_at", {line: before.split("\n").length, col: pos - before.lastIndexOf("\n")});
}
function jsonNode(v, key){
  const head = key == null ? [] : [el("span",{class:"k"}, JSON.stringify(key)), ": "];
  if (v === null || typeof v !== "object"){
    const cls = typeof v === "string" ? "s" : v === null || typeof v === "boolean" ? "z" : "n";
    return el("div",{}, ...head, el("span",{class:cls}, JSON.stringify(v)));
  }
  const arr = Array.isArray(v), keys = Object.keys(v);
  const box = el("details",{open: keys.length <= 50 ? "" : null},
    el("summary",{}, ...head, arr ? `[${keys.length}]` : `{${keys.length}}`));
  keys.slice(0, 1000).forEach(k => box.append(jsonNode(v[k], arr ? null : k)));
  if (keys.length > 1000) box.append(el("div",{class:"z"}, t("view.more", {n: keys.length - 1000})));
  return box;
}
function viewJson(box, text){
  if (!text.trim()) { box.append(el("div",{class:"sub"}, t("view.empty"))); return; }
  let data;
  try { data = JSON.parse(text); }
  catch (e) {
    box.append(el("div",{class:"pv-err"}, t("view.json_bad", {why: e.message, where: jsonWhere(text, e)})));
    return;
  }
  box.append(el("div",{class:"pv-head"}, el("span",{class:"chip ok"}, t("view.json_ok"))),
             el("div",{class:"pv-json"}, jsonNode(data, null)));
}
// Уровень строки журнала: ошибки и предупреждения — цветом, служебное — тусклым.
const LOG_BAD = /\b(ERROR|FATAL|CRITICAL|EXCEPTION|Traceback|FAILED|ОШИБКА)\b|^\s*(■|✗|❌|⛔)/i;
const LOG_WARN = /\b(WARN(ING)?|DEPRECATED|ПРЕДУПРЕЖДЕНИЕ)\b|^\s*(⚠|⏳)/i;
const LOG_DIM = /\b(DEBUG|TRACE)\b/;
function viewLog(box, text){
  const all = text.split("\n");
  const lines = all.length > VIEW_LINES ? all.slice(-VIEW_LINES) : all;
  const cls = l => LOG_BAD.test(l) ? "l-bad" : LOG_WARN.test(l) ? "l-warn" : LOG_DIM.test(l) ? "l-dim" : "";
  const kinds = lines.map(cls);
  const bad = kinds.filter(k => k === "l-bad").length, warn = kinds.filter(k => k === "l-warn").length;
  const out = el("div",{class:"pv-log"});
  const draw = only => {
    out.innerHTML = "";
    lines.forEach((l, i) => { if (!only || kinds[i] === "l-bad" || kinds[i] === "l-warn")
      out.append(el("div",{class:kinds[i]}, l || " ")); });
  };
  const toggle = el("label",{class:"row", style:"gap:6px;font-size:12.5px"},
    el("input",{type:"checkbox", onchange: e => draw(e.target.checked)}), t("view.log_only"));
  box.append(el("div",{class:"pv-head"},
    el("span",{class:"chip" + (bad ? " bad" : " ok")}, t("view.log_errors", {n: bad})),
    el("span",{class:"chip" + (warn ? " warn" : "")}, t("view.log_warns", {n: warn})),
    el("span",{class:"sub"}, all.length > VIEW_LINES
      ? t("view.log_tail", {n: VIEW_LINES, total: all.length}) : t("view.lines", {n: all.length})),
    toggle), out);
  draw(false);
}
function viewText(box, text){
  const words = (text.match(/\S+/g) || []).length;
  box.append(el("div",{class:"pv-head"},
      el("span",{class:"sub"}, t("view.text_count", {lines: text.split("\n").length, words}))),
    el("div",{class:"pv-text"}, text));
}
// «Форматировать» — у JSON: отступы в два пробела; битый файл не трогаем, а говорим где.
function formatFile(){
  if (!F.ed || F.ro) return;
  const text = F.ed.getValue();
  try { F.ed.setValue(JSON.stringify(JSON.parse(text), null, 2) + "\n"); }
  catch (e) { return toast(t("view.json_bad", {why: e.message, where: jsonWhere(text, e)}), "warn"); }
  markDirty(); drawView();
}
$("#fileFormat").onclick = formatFile;

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
      if (heavy && !F.lossy){        // предупреждение «правка потеряет байты» важнее
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
          if (!F.lossy){             // предупреждение «правка потеряет байты» важнее и остаётся
            const n = diffLines(text, now);
            $("#fileWarn").hidden = false;
            $("#fileWarn").textContent = t("editor.wysiwyg_warn", {n});
          }
        }
        F.dirty = false; $("#fileSave").disabled = true;
      }, 60);
    },
    input(){ markDirty(); if (viewKind(F.path)) viewSoon(); }
  });
  // Вид справа — у тех, кому он есть; редактор слева остаётся тем же, со всеми проверками.
  const kind = viewKind(F.path);
  if (kind){
    $("#fileBody").classList.add("split");
    $("#filePreview").hidden = false;
    $("#fileFormat").hidden = kind !== "json" || !!F.ro;
    setTimeout(drawView, 80);
  }
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
    // `?s=1` — адрес сменился вместе с правкой при отдаче (mermaid `strict`): браузер, державший
    // прежний файл сутки, обязан взять новый, а не старый.
    const s = el("script",{src:"/vendor/vditor/dist/index.min.js?s=1"});
    s.onload = ok;
    s.onerror = ()=>{ toast(t("files.editor_failed"), "err"); fail(); };
    document.head.append(s);
  });
  return VDITOR_READY;
}

/* Что показать у «Опубликовать» и у метки «страница отстала» по ответу `file_read`. Чистая функция: решает ответ
   сервера, а не страница, — поэтому её можно проверить отдельно от разметки.
   button: `hidden` — публикация к файлу неприменима; `blocked` — кнопка серая, `why` называет причину, `fix` — где её
   чинить (`kinds` — «Настройки проекта → Артефакты», `project` — «Настройки проекта»); `ready` — можно.
   stale: опубликован и с тех пор изменён его чистовик; unknown: опубликован прежней версией кита, отпечатка нет. */
function publishView(d){
  const pub = (d && d.publish) || {};
  const state = pub.state === "ready" || pub.state === "blocked" ? pub.state : "hidden";
  return {
    button: state,
    why: state === "blocked" ? (pub.why || "") : "",
    fix: state === "blocked" ? (pub.fix || "") : "",
    gate: state === "ready" && Array.isArray(pub.gate) ? pub.gate : [],
    stale: !!(d && d.stale),
    unknown: !!(d && d.published && d.stale_known === false),
  };
}

function drawPublish(d){
  const v = publishView(d);
  F.publishable = v.button === "ready";
  const btn = $("#filePub");
  btn.hidden = v.button === "hidden";
  btn.disabled = v.button === "blocked";
  btn.title = v.why;
  btn.textContent = t(F.dirty ? "editor.publish_save" : "editor.publish");     // несохранённое уходит тем же кликом
  const note = $("#filePubNote");
  note.innerHTML = "";
  note.hidden = !(v.why || v.gate.length);
  note.className = v.why ? "note warn" : "note";
  if (v.why){
    note.append(v.why + " ");
    if (v.fix) note.append(el("button",{class:"btn sm", onclick:()=>{
      SGROUPS.focus = v.fix === "kinds" ? "project:kinds" : "project:form";
      show("project");
    }}, t(v.fix === "kinds" ? "editor.publish_fix_kinds" : "editor.publish_fix_project")));
  } else if (v.gate.length){
    note.append(t("editor.publish_gate", {why: v.gate.map(c => t("editor.gate_" + c.replace(/-/g, "_"))).join("; ")}));
  }
  const stale = $("#fileStale");
  stale.hidden = !(v.stale || v.unknown);
  stale.innerHTML = "";
  if (v.stale){
    stale.append(t("editor.stale", {date: d.published}) + " ",
      el("button",{class:"btn", onclick:()=>publishFromEditor(), disabled: v.button === "ready" ? null : ""}, t("editor.publish")));
  } else if (v.unknown){
    stale.append(t("editor.stale_unknown", {date: d.published}));
  }
}

// Скрипт публикации переписал открытый файл штампом (страница, версия, отпечаток чистовика). Редактор держит прежний
// текст и прежний слепок: следующее «Сохранить» дало бы ложный конфликт, а метка «страница отстала» не обновилась бы.
// Перечитываем файл; несохранённое в редакторе не затираем — о нём говорим. Публикация идёт из консоли, а скрытый
// редактор не пересобирают (Vditor в скрытом блоке мерит нулевую высоту): перечитаем, когда «Файлы» снова на виду.
async function afterPublish(rel){
  if (!F.path || F.path !== rel) return;
  if (S.view !== "files"){ F.reopen = rel; return; }
  F.reopen = "";
  if (F.dirty) return toast(t("editor.published_dirty"), "warn");
  const f = F.files.find(x => x.path === rel) || {path: rel, name: rel.split("/").pop(), text: true, readonly: ""};
  await openFile(f);
}

/* Публикация из «Файлов» — каждое исходящее действие явным кликом кнопки с понятной надписью, без окон подтверждения.
   1) «Опубликовать» (при несохранённом — «Сохранить и опубликовать»: сохраняет тем же кликом) считает план — сухой
      прогон `ship:publish --json` на сервере (`/api/publish/plan`) — и показывает карточку «что и куда уйдёт»: пространство,
      корень, цепочку разделов (есть / будет создан / отказ), заголовок, создание или обновление, ворота, чистовик.
   2) Кнопки карточки зависят от исхода плана: «Отправить» (черновик — «Опубликовать черновик»), «Обновить на месте» и
      «Переместить по пути вида» (`--move`), «Открыть страницу», «Обновить зеркало» (`sync:confluence`), «Перезаписать»
      (`--force`), «Взять под управление» (`--adopt`); `blocked` — причина и кнопка в нужную группу настроек.
   3) После отправки исход приходит машинной строкой `AURORA-PUBLISH` (`publishDone`) и рисуется той же карточкой. */
async function publishFromEditor(){
  if (!F.path || !F.publishable) return;
  // Несохранённое уходит тем же кликом: надпись кнопки говорит «Сохранить и опубликовать». Сохранение не удалось
  // (расхождение с диском, отказ) — публикации нет: причину показал `saveFile`.
  if (F.dirty){ await saveFile(); if (F.dirty || !F.publishable) return; }
  const rel = F.path;
  const box = publishCard(rel);
  box.append(el("div",{class:"row", style:"gap:8px"}, el("span",{class:"spin"}), t("pub.planning")));
  const q = "?project=" + encodeURIComponent(S.project.path) + "&path=" + encodeURIComponent(rel);
  const [clean, res] = await Promise.all([
    api("/api/files/clean" + q, {quiet:true}),
    api("/api/publish/plan", {method:"POST", quiet:true, body: JSON.stringify({project:S.project.path, path:rel})})]);
  if (F.path !== rel || !box.isConnected) return;
  drawPublishCard(box, rel, (res && res.plan) || null, (res && !res.plan && res.error) || "", clean || {});
}

// Карточка публикации файла в «Файлах»: одна на файл, новая заменяет прежнюю.
function publishCard(rel){
  $("#filePubCard")?.remove();
  const box = el("div",{id:"filePubCard", class:"card", "data-rel": rel, style:"padding:14px;margin-bottom:10px"});
  $("#filePane").prepend(box);
  return box;
}

const PUB_LEVELS = ["exists", "create", "created", "refused"];     // состояние уровня пути: есть / будет создан / создан / отказ

// Путь страницы словами: Пространство · Корень › Раздел (будет создан) › Заголовок.
function publishPath(plan){
  const parts = [];
  if (plan.root && (plan.root.title || plan.root.id)) parts.push(el("b",{}, plan.root.title || "#" + plan.root.id));
  (plan.chain || []).forEach(c => parts.push(el("span",{}, c.title + " ",
    el("span",{class:"chip" + (c.state === "refused" ? " bad" : c.state === "exists" ? "" : " warn"),
               style:"font-size:11px"}, t("pub.level_" + (PUB_LEVELS.includes(c.state) ? c.state : "exists"))))));
  parts.push(el("b",{}, plan.title || ""));
  const line = el("div",{class:"mono", style:"font-size:12.5px;margin-top:6px"});
  if (plan.space) line.append(t("pub.space", {space: plan.space}) + " · ");
  parts.forEach((p, i) => { if (i) line.append(" › "); line.append(p); });
  return line;
}

// Ссылка на страницу Confluence — только http(s): адрес собран движком из `confluence.base_url`.
function pageLink(url, label){
  return /^https?:\/\//i.test(String(url || "")) ? el("a",{class:"btn", href:url, target:"_blank", rel:"noopener noreferrer"}, label) : null;
}

/* Почему публикация не идёт — словами каталога по коду исхода (`code`); незнакомый код — причина движка как есть. */
function publishWhy(res){
  const c = String(res.code || "");
  const vars = {fields: (res.fields || []).join(", "), kind: res.kind || "", status: res.status || "", why: res.why || "",
                page: (res.page && res.page.id) || ""};
  if (c === "review") vars.why = (res.review || []).map(g => t("editor.gate_" + g.replace(/-/g, "_"))).join("; ");
  if (c === "gate") vars.why = (res.gate || []).filter(g => g.level === "block" && !g.allowed)
    .map(g => t("pub.gate_" + g.code.replace(/-/g, "_"))).join(" ");
  const known = ["missing_field", "empty_title", "too_long", "no_parent", "root_missing", "root_unknown", "title_taken",
                 "children_partial", "page_missing", "cross_space", "not_ours", "no_auth", "no_config", "no_space",
                 "review", "gate", "http", "network", "unsafe", "stamp"];
  const k = c.replace(/-/g, "_");
  return known.includes(k) ? t("pub.why_" + k, vars) : (res.why || t("pub.why_unknown"));
}

/* Кнопки по исходу (плана или отправки). Каждая — явное действие; окон подтверждения нет. `send` — что делает основная
   кнопка при `ok` плана: отправить с теми снятиями ворот, которые человек видит в карточке. */
function publishActions(rel, res, opts){
  const o = opts || {};
  const out = [];
  const open = (res.page && res.page.url) || res.url;
  const go = (args, label, primary) => el("button",{class:"btn" + (primary ? " primary" : ""),
    onclick:()=>runPublish(rel, args)}, label);
  switch (res.outcome){
    case "ok":
      if (o.plan){
        out.push(go(["--apply", ...(o.allow || [])], t(o.draft ? "pub.send_draft" : "pub.send"), true));
        if (o.blockedGate) out[0].disabled = true;
      } else if (open) out.push(pageLink(open, t("pub.open")));
      break;
    case "unchanged":
      if (open) out.push(pageLink(open, t("pub.open")));
      break;
    case "conflict":
      if (open) out.push(pageLink(open, t("pub.open")));
      out.push(el("button",{class:"btn", onclick: async ()=>{
        const r = await api("/api/run", {method:"POST", body: JSON.stringify({project:S.project.path, cmd:"sync:confluence", args:[]})});
        if (r.job){ show("console"); poll(r.job, 0, "sync:confluence"); }
      }}, t("pub.mirror")));
      out.push(go(["--apply", "--force", ...(o.allow || [])], t("pub.force"), false));
      break;
    case "adopt":
      if (open) out.push(pageLink(open, t("pub.open")));
      out.push(go(["--apply", "--adopt", ...(o.allow || [])], t("pub.adopt"), false));
      break;
    case "misplaced":
      if (o.plan) out.push(go(["--apply", ...(o.allow || [])], t("pub.update_here"), true));
      out.push(go(["--apply", "--move", ...(o.allow || [])], t("pub.move"), !o.plan));
      if (open) out.push(pageLink(open, t("pub.open")));
      break;
    case "blocked":
      if (res.fix === "kinds" || res.fix === "project") out.push(el("button",{class:"btn", onclick:()=>{
        SGROUPS.focus = res.fix === "kinds" ? "project:kinds" : "project:form";
        show("project");
      }}, t(res.fix === "kinds" ? "editor.publish_fix_kinds" : "editor.publish_fix_project")));
      if (res.url) out.push(pageLink(res.url, t("pub.open_taken")));
      break;
  }
  return out.filter(Boolean);
}

// Что сказать про исход одной строкой (план: «уйдёт так»; отправка: «вышло так»).
function publishHeadline(res, plan){
  const v = {url: res.url || "", version: res.version || (res.page && res.page.version) || "",
             was: (res.conflict || {}).was || "", now: (res.conflict || {}).now || "", by: (res.conflict || {}).by || "",
             when: (res.conflict || {}).when || "", title: (res.page && res.page.title) || res.title || "",
             here: ((res.where || {}).now || {}).title || ((res.where || {}).now || {}).id || "",
             there: (((res.where || {}).want || {}).path || []).join(" › "),
             created: (res.created || []).join(", ")};
  switch (res.outcome){
    case "ok": return plan ? t(res.action === "update" ? "pub.plan_update" : "pub.plan_create", v)
                           : t(res.moved ? "pub.done_moved" : "pub.done_ok", v) + (v.created ? " " + t("pub.done_created", v) : "");
    case "unchanged": return t("pub.unchanged", v);
    case "conflict": return t("pub.conflict", v);
    case "adopt": return t("pub.adopt_why", v);
    case "misplaced": return t(plan ? "pub.misplaced_plan" : "pub.misplaced_done", v);
    case "blocked": return publishWhy(res);
  }
  return res.why || "";
}

function drawPublishCard(box, rel, plan, err, clean){
  box.innerHTML = "";
  box.append(el("b",{}, t("editor.publish_preview")));
  // Ворота выходных команд: «нет границы чистовика» и «черновик» снимаются осознанно — этой карточкой и кнопкой
  // с понятной надписью; остальное (остатки шаблона, персональные данные) чинится в документе.
  const gates = plan ? (plan.gate || []).filter(g => g.level === "block") : [];
  const allow = gates.filter(g => g.code === "no-marker" || g.code === "draft").map(g => ["--allow", g.code]).flat();
  const draft = gates.some(g => g.code === "draft");
  const hard = gates.filter(g => g.code !== "no-marker" && g.code !== "draft");
  if (plan){
    box.append(el("div",{class:"sub" + (plan.outcome === "blocked" || plan.outcome === "conflict" ? " warn" : "")},
      publishHeadline(plan, true)));
    if (plan.space || plan.root && plan.root.id) box.append(publishPath(plan));
    if (plan.page && plan.page.id) box.append(el("div",{class:"muted", style:"font-size:12px"},
      t("pub.page_now", {id: plan.page.id, version: plan.page.version || "?", by: plan.page.by || "—", when: plan.page.when || "—"})));
    gates.forEach(g => box.append(el("div",{class:"sub", style:"color:var(--tier-inreview)"},
      t("pub.gate_" + g.code.replace(/-/g, "_")))));
  } else {
    // Плана нет (нет связи, движок проекта старше): прежний предпросмотр и отправка — проверку пройдёт сама команда.
    if (err) box.append(el("div",{class:"sub warn"}, t("pub.no_plan", {why: err})));
    if (!clean.marked) allow.push("--allow", "no-marker");
  }
  box.append(el("div",{class:"sub"}, clean.marked ? t("editor.publish_cut", {n: clean.cut}) : t("editor.publish_nomark")));
  box.append(el("details",{style:"margin-top:6px"}, el("summary",{}, t("pub.clean")),
    el("pre",{class:"mono", style:"white-space:pre-wrap;max-height:260px;overflow:auto;"
      + "margin-top:8px;padding:8px;border-radius:8px;background:var(--surface-2)"}, clean.clean || "")));
  const row = el("div",{class:"row", style:"gap:8px;margin-top:10px;flex-wrap:wrap"});
  if (plan) row.append(...publishActions(rel, plan, {plan: true, allow, draft, blockedGate: hard.length > 0}));
  else row.append(el("button",{class:"btn primary", onclick:()=>runPublish(rel, ["--apply", ...allow])}, t("pub.send")));
  row.append(el("button",{class:"btn", onclick:()=>box.remove()}, t("editor.publish_cancel")));
  box.append(row);
}

// Отправка: ход — в «Консоли» (в фоне), исход — в той же карточке, когда команда кончится.
async function runPublish(rel, args){
  const res = await api("/api/run", {method:"POST", body: JSON.stringify(
    {project:S.project.path, cmd:"ship:publish", args:[rel, ...args]})});
  if (!res.job) return;
  const box = $("#filePubCard");
  if (box && box.dataset.rel === rel){
    box.innerHTML = "";
    box.append(el("div",{class:"row", style:"gap:8px"}, el("span",{class:"spin"}), t("pub.sending"),
      el("button",{class:"btn sm", onclick:()=>show("console")}, t("pub.to_console"))));
  }
  poll(res.job, 0, "ship:publish " + rel);
}

// Команда публикации кончилась (из «Файлов» или из «Продуктивности»): файл перечитываем, если скрипт его проштамповал,
// и рисуем исход карточкой у файла с кнопками по исходу.
function publishDone(rel, res, rc){
  if (rc === 0 || (res && res.stamped)) afterPublish(rel);
  if (!res || !res.outcome) return;
  // В «Файлах» открыт другой документ — исход строкой, а карточку с кнопками покажет «Опубликовать» у того файла.
  if (F.path !== rel) return toast(rel + ": " + publishHeadline(res, false),
                                   res.outcome === "ok" || res.outcome === "unchanged" ? "ok" : "warn");
  const box = publishCard(rel);
  box.append(el("b",{}, t("pub.result_title")),
    el("div",{class:"sub" + (res.outcome === "ok" || res.outcome === "unchanged" ? "" : " warn")}, publishHeadline(res, false)));
  if (res.space || (res.root && res.root.id)) box.append(publishPath(res));
  const row = el("div",{class:"row", style:"gap:8px;margin-top:10px;flex-wrap:wrap"},
    // повтор («Перезаписать», «Взять под управление», «Переместить») снимает те же ворота, что сняла отправка
    ...publishActions(rel, res, {allow: (res.gate || []).filter(g => g.allowed).map(g => ["--allow", g.code]).flat()}),
    el("button",{class:"btn", onclick:()=>box.remove()}, t("pub.close")));
  box.append(row);
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
  if (d.publish) drawPublish(d);        // правка чистовика зажигает «страница отстала» сразу, а не при следующем открытии
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
        if (!d.error){ F.digest = d.digest; F.orig = mine; box.remove(); toast(t("editor.saved")); if (d.publish) drawPublish(d); }
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
// После записи одной группы страницу «Настройки проекта» пересобирают, а пересборка сбрасывает несохранённые правки
// соседних групп (в том числе «Типы карточек»). Есть такие правки — страницу не трогаем и говорим об этом.
function renderProjectAfterSave(){
  if (!DIRTY.size){ renderProject(); return; }
  toast(t("dirty.kept", {names: [...DIRTY.values()].join(", ")}), "warn");
}
addEventListener("beforeunload", e => {
  if (!DIRTY.size) return;
  e.preventDefault();          // браузер покажет свой диалог: текст задаём не мы
  e.returnValue = "";
});

/* ---------------- навигация ---------------- */
function show(view, payload, opts){
  // уход из «Настройки» с незаписанными правками — единственное место, где панель
  // может молча потерять введённое: всё остальное она либо пишет сразу, либо не пишет
  if (S.view === "setup" && view !== "setup" && !confirmLeave()) return;
  // «Настройки проекта» теряют несохранённое так же: переход в другой раздел спрашивает подтверждение
  if (S.view === "project" && view !== "project" && !confirmLeave()) return;
  // Раздел в разработке закрыт — в него не попасть и по адресу страницы или ссылке.
  if (devOnlyView(view) && !devSectionsOn()) view = "overview";
  // Раздел скрыт профилем рабочего места: меню его не показывает, ни по адресу, ни из ядра
  // он не открывается — человек получает Мостик, а не пустой экран. Молчит только автопереход,
  // который человек не просил (`pick` → «Здоровье», флаг `quiet`): упрёк за него был бы шумом при
  // каждом выборе проекта. Клик, адрес или ссылка, ведущие в скрытый раздел, называют причину.
  if (profileHidden(view)){
    if (!(opts && opts.quiet)) toast(t("modules.hidden", {id: moduleTitle(MODULES.get(view).manifest)}), "warn");
    view = "overview";
  }
  const prevView = S.view;
  S.view = view;
  if (prevView !== view) hideModule(prevView);        // уходящий раздел узнаёт об этом раньше, чем придёт новый
  $$(".view").forEach(v=>v.classList.toggle("on", v.id === "view-"+view));
  $$("nav button").forEach(b=>b.setAttribute("aria-current", String(b.dataset.view===view)));
  if (view==="overview") refreshActivity();
  if (view==="setup"){ renderSetup(); drawLangPicker(); drawRolePicker(); }
  if (view==="project") renderProject();
  if (view==="files") renderFiles();
  if (view==="console"){
    renderHistory(); loadRuns(); drawTaskButton(); drawLiveJobs().then(() => autoFollow());
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

// Раздел вне списка `modules` активного профиля: сервер пометил его `hidden`. Имён разделов
// здесь нет — решает манифест ответа `/api/modules`.
function profileHidden(view){
  return !!(MODULES.get(view) && MODULES.get(view).manifest.hidden);
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
/* ---------------- обращение на GitHub ---------------- */
// Шаблоны обращений кита — «Баг» и «Предложение» (`.github/ISSUE_TEMPLATE/`). Форма
// открывается в браузере уже заполненной тем, что панель знает наверняка: версия кита и
// панели, ОС, Python, раздел, откуда нажали. Имён проектов и путей нет: репозиторий
// открытый, а в них — имена клиентов и адреса контуров. Версия и ОС в формах — списки:
// GitHub выберет вариант только при точном совпадении, а версию старше пяти последних
// оставит человеку — отметить «old».
const ISSUE_AREA = "Панель управления (cockpit)";        // данные движка: вариант формы issue
function viewLabel(view){
  const b = $(`nav button[data-view="${view}"] .label`);
  return (b && b.textContent.trim()) || view || "—";
}
function issueUrl(kind){
  const k = (S.state && S.state.kit) || {};
  const repo = k.repo || "https://github.com/VVG-web/aurora-studio";
  const q = new URLSearchParams();
  const where = t("issue.context", {section: viewLabel(S.view), view: S.view || "—", ui: UI_VERSION,
                                    lang: S.lang || "ru", browser: navigator.userAgent});
  const bug = kind !== "feature";
  q.set("template", bug ? "bug_report.yml" : "feature_request.yml");
  if (bug) q.set("area", ISSUE_AREA);
  if (k.version) q.set("version", k.version);
  if (k.os) q.set("os", k.os);
  if (bug && k.python) q.set("python", k.python);
  q.set("extra", where);
  return repo.replace(/\/+$/, "") + "/issues/new?" + q.toString();
}
function openIssue(kind){
  window.open(issueUrl(kind), "_blank", "noopener");
}
$("#bugBtn").onclick = () => openIssue("bug");
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
  if (!await acquireSession()) return;     // без токена панели нечего показывать
  $("#towers").innerHTML = '<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
  await loadI18n();      // строки до первой отрисовки: иначе экран моргнёт с русского на выбранный
  drawGlobalMetrics();   // плитки Мостика с подписями — сразу, числа придут следом
  // Состояние — двумя половинами. Проекты и версия — за доли секунды; реестр команд после
  // обновления кита собирается полминуты (`--help` полусотни скриптов), и пока он шёл в
  // общем ответе, Мостик стоял пустым. Теперь он догружается в фоне.
  S.state = await api("/api/state?part=core");
  S.state.env = {items: []};
  S.state.commands = [];
  S.extraP = api("/api/state?part=extra", {quiet: true}).then(x => {
    if (!x || x.error) return;
    S.state.env = x.env; S.state.commands = x.commands; S.state.extra = true;
    setBadge("commands", S.state.commands.length);
    const miss = S.state.env.items.filter(i=>!i.ok).length;
    setBadge("install", miss || "", miss > 0);
    drawGlobalMetrics();
    refreshModules();
  });
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
      el("button",{class:"btn", style:"margin-left:8px", onclick:restartFromBar},
        t("boot.restart"))));
  }
  drawAdapterAlarm(S.state.adapter_alarm);
  refreshNetBar();
  showReaped(S.state.reaped);
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
  if (v) show(v);   // скрытый профилем раздел из ссылки сам скажет причину
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
                    gated: t("act.reason_gated"),
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
// Панель на старте сняла брошенные процессы прежней — сказать один раз за её запуск, а не
// на каждой перезагрузке страницы.
function showReaped(r){
  if (!r || !(r.items || []).length) return;
  let seen = "";
  try { seen = localStorage.getItem("aurora-reaped-seen") || ""; } catch (e) {}
  if (seen === String(r.at)) return;
  try { localStorage.setItem("aurora-reaped-seen", String(r.at)); } catch (e) {}
  toast(t("boot.reaped", {n: r.items.length, list: r.items.map(x => x.what).join("; ")}), "warn");
}
// Тревоги вызова модели — полосой поверх любого раздела. Это две разные беды, и полосы у них
// разные (область `area` приходит от движка):
// - `adapter`: Pydantic AI выбран, а вызовы идут мимо него — без инструментов и MCP бот «не видит»
//   свои серверы, план собирается без поиска. С 1.139 по 1.160 это случалось четырежды и каждый раз
//   всплывало через неделю. Только неисправный путь Pydantic AI: пакета нет, он не импортируется,
//   версия не прошла проверку, процесс падает, HTTP-клиент адаптера не доходит до шлюза, а прямой
//   запрос доходит. Кнопка — туда, где лечится (`fix`): «Установка» или «Версия». Уходит, когда путь
//   снова пройдёт через адаптер.
// - `link`: нет связи с бэкендом (сеть, DNS, сертификат, прокси, сам шлюз). Pydantic AI тут ни при
//   чём, поэтому про него в полосе ни слова и кнопок про него нет. Заголовок называет вид беды;
//   уходит, когда бэкенд ответит. 18.10.2026 обрыв сети выдавали за поломку Pydantic AI.
function drawAdapterAlarm(rows){
  for (const id of ["#adapterbar", "#linkbar"]){
    const old = $(id);
    if (old) old.remove();
  }
  if (!rows || !rows.length) return;
  const adapter = rows.filter(r => r.area !== "link");
  const link = rows.filter(r => r.area === "link");
  const when = r => r.at ? histWhen(r.at) : "—";
  if (link.length){
    const wait = link.some(r => r.fix !== "models");     // ожидание лечит: сеть, VPN, шлюз
    const cfg = link.some(r => r.fix === "models");      // лечит настройка: ключ, модель, срок
    $("main").prepend(el("div",{id:"linkbar", class:"stalebar alarm"},
      ...link.map(r => el("div",{},
        el("b",{}, t("alarm.link." + r.kind, Object.assign({n:"?", status:""}, r.args || {}))),
        " ", t("alarm.link_row", {path: r.path, project: r.project || "—", when: when(r), calls: r.calls}),
        el("div",{class:"muted"}, r.why))),
      el("div",{}, [wait ? t("alarm.link_tail") : "", cfg ? t("alarm.link_tail_cfg") : ""]
          .filter(Boolean).join(" "),
        ...(cfg ? [el("button",{class:"btn", style:"margin-left:8px", onclick:()=>show("models")},
          t("alarm.link_models"))] : []))));
  }
  if (adapter.length){
    $("main").prepend(el("div",{id:"adapterbar", class:"stalebar alarm"},
      el("b",{}, t("alarm.adapter")),
      ...adapter.map(r => el("div",{}, t("alarm.adapter_row",
        {path: r.path, project: r.project || "—", when: when(r), calls: r.calls, why: r.why}))),
      el("div",{}, t("alarm.adapter_tail"),
        // Чем лечить, говорит причина: нет файла адаптера в движке проекта — «Версия», нет или
        // сломан сам Pydantic AI — «Установка», иначе (прокси, сертификат) кнопки нет: правится снаружи.
        ...(adapter.some(r => r.fix === "version")
          ? [el("button",{class:"btn", style:"margin-left:8px", onclick:()=>show("version")},
              t("alarm.adapter_engine"))] : []),
        ...(adapter.some(r => r.fix === "install")
          ? [el("button",{class:"btn", style:"margin-left:8px", onclick:()=>show("install")},
              t("alarm.adapter_open"))] : []))));
  }
}
async function refreshAdapterAlarm(){
  let d = null;
  try { d = await api("/api/adapter/alarm", {quiet:true}); } catch (e) { return; }
  if (d) drawAdapterAlarm(d.alarm);
}
/* ---------------- соединения ---------------- */
// Значок в шапке: как кит видит связь прямо сейчас. Виден, только пока проверка включена; сеть
// тут не трогается — состояние приносит сервер панели (его проверяет фоновая проверка). Нажатие
// открывает раздел, где профили и адреса проверки настраиваются.
function netModeText(d){
  const down = d.down || [];
  if (d.state === "offline") return t("net.mode_offline");
  if ((d.state === "no_vpn" || d.state === "partial") && down.length)
    return t("net.mode_down", {n: down.length, names: down.join(", ")});
  return t("net.mode_" + d.state);
}
async function refreshNetBar(){
  const bar = $("#connBar");
  if (!bar) return;
  let d = null;
  try { d = await api("/api/net/brief" + (S.project ? "?project=" + encodeURIComponent(S.project.path) : ""), {quiet:true}); }
  catch (e) { return; }
  if (!d || typeof d.error !== "boolean") return;               // ответа нет (панель перезапускается): прежний вид остаётся
  if (!d.enabled){ bar.hidden = true; return; }
  bar.hidden = false;
  if (d.error){
    bar.className = "btn sm connbar bad";
    bar.textContent = t("net.bar_error");
    bar.title = t("net.mode_error");
    return;
  }
  bar.className = "btn sm connbar " + ({online:"ok", degraded:"warn", partial:"warn", no_vpn:"bad", offline:"bad"}[d.state] || "");
  bar.textContent = t("net.bar_" + d.state);
  bar.title = netModeText(d);
}
const NET_POLL_MS = 20000;
setInterval(() => { if (!document.hidden) refreshNetBar(); }, NET_POLL_MS);
$("#connBar").onclick = () => show("connectivity");

/* ---------------- движок проекта на этой машине ---------------- */
// Папка `.aurora/` — копия движка на этом компьютере, вне git: клон проекта приходит без неё.
// Нет её или она неполная — панель достраивает сама при выборе проекта (это не меняет ни файлы
// проекта, ни git), отставшую обновляет кнопка. Ядро только вызывает сервер и рисует полосу.
const ENGINE_TRIED = new Set();         // проекты, для которых автоподготовка уже была в этой вкладке
async function prepareEngine(p){
  p = p || S.project;
  if (!p || p._engineBusy) return null;
  p._engineBusy = true; p.engine_error = "";
  drawEngineBar(p);
  let d = null;
  try { d = await api("/api/engine/prepare", {method:"POST", quiet:true, body: JSON.stringify({project: p.path})}); }
  catch (e) { d = {error: String(e)}; }
  p._engineBusy = false;
  if (d && d.state) p.engine_state = d.state;
  if (d && d.error) p.engine_error = d.error;
  else if (d && d.ok){
    p.engine_prepared = {created: d.created, refreshed: d.refreshed, files: d.files || [],
                         ignored: d.ignored, ahead: d.ahead};
    toast(t("engine.prepared", {created: d.created, refreshed: d.refreshed}));
    // Часть файлов за ссылкой или вне проекта: их подготовка не трогает, и «в порядке» не наступит.
    if (d.shared && d.state && d.state.state !== "ok") p.engine_error = t("engine.shared", {n: d.shared});
  } else if (d && d.failed && d.failed.length) p.engine_error = d.failed[0];
  drawEngineBar(p);
  refreshModules();
  return d;
}
function drawEngineBar(p){
  const old = $("#enginebar");
  if (old) old.remove();
  if (!p || p !== S.project) return;
  const es = p.engine_state || {};
  const put = (alarm, ...kids) => $("main").prepend(
    el("div", {id:"enginebar", class:"stalebar" + (alarm ? " alarm" : "")}, ...kids));
  const btn = (label, fn) => el("button", {class:"btn", style:"margin-left:8px", onclick: fn}, label);
  if (p._engineBusy) return put(false, el("b", {}, t("engine.busy")));
  const done = p.engine_prepared;
  if (done && es.state === "ok"){
    return put(false, el("b", {}, t("engine.prepared", {created: done.created, refreshed: done.refreshed})),
      done.files.length ? " " + t("engine.prepared_files", {files: done.files.join(", ")}) : "",
      done.ignored === false ? " " + t("engine.not_ignored") : "",
      btn(t("engine.dismiss"), () => { p.engine_prepared = null; drawEngineBar(p); }));
  }
  const why = p.engine_error ? [el("div", {}, t("engine.failed", {why: p.engine_error}))] : [];
  if (es.legacy) return put(false, el("b", {}, t("engine.bar_legacy")), btn(t("engine.open_version"), () => show("version")));
  if (es.state === "missing")
    return put(true, el("b", {}, t("engine.bar_missing")), ...why, btn(t("engine.prepare"), () => prepareEngine(p)));
  if (es.state === "incomplete")
    return put(true, el("b", {}, t("engine.bar_incomplete",
      {n: es.missing, total: es.total, files: (es.files || []).join(", ")})), ...why,
      btn(t("engine.prepare"), () => prepareEngine(p)));
  if (es.state === "stale")
    return put(false, el("b", {}, t("engine.bar_stale", {copy: es.copy || "—", kit: p.kit})), ...why,
      btn(t("engine.update"), () => prepareEngine(p)));
}
// Выбрали проект: без движка или с неполным — достроить до того, как считаться здоровью (иначе
// первые команды пойдут на скриптах кита), один раз за вкладку; полоса остаётся с кнопкой на случай отказа.
async function ensureEngine(p){
  const es = p.engine_state || {};
  if ((es.state === "missing" || es.state === "incomplete") && !es.legacy && !ENGINE_TRIED.has(p.path)){
    ENGINE_TRIED.add(p.path);
    await prepareEngine(p);
  } else drawEngineBar(p);
}
const ALARM_POLL_MS = 20000;
setInterval(() => { if (!document.hidden) refreshAdapterAlarm(); }, ALARM_POLL_MS);
const ACTIVITY_POLL_MS = 5000;
// Пока Консоль открыта, список того, что идёт, обновляется сам: пункты цепочки сменяются.
setInterval(() => { if (S.view === "console" && !document.hidden) drawLiveJobs(); }, ACTIVITY_POLL_MS);
setInterval(() => { if (S.view === "overview" && !document.hidden) refreshActivity(); },
            ACTIVITY_POLL_MS);

// Здоровье считается секундами, а проект за это время успевают сменить. Ответ по одному
// проекту, записанный прямо в S.health, показывался под именем другого: панель писала
// «нет .env.aurora.local» у проекта, где файл на месте, — это было замечание соседнего,
// чей ответ пришёл позже.
// Поэтому часть ложится только в здоровье своего проекта (`loadHealth`), а текущим
// оно становится, только если этот проект всё ещё выбран.

// Здоровье приходит частями (`/api/health?part=`): быстрые — статистика, доктор, зеркала,
// план сборки, файлы — за секунды; линтер всей базы и дела человеку — десятки секунд.
// Целиком подряд выходила минута, и всё это время «Здоровье» и Мостик стояли пустыми.
// Теперь плитки стоят сразу, а каждая часть ложится в здоровье проекта, как придёт.
const HEALTH_QUICK = ["files", "stats", "doctor", "mirrors", "build", "templates", "rebuild"];
const HEALTH_SLOW = ["lint", "todo"];
const HEALTH_ALL = [...HEALTH_QUICK, ...HEALTH_SLOW];
// Часть пришла. Здоровье без списка частей (ответ целиком) — всё на месте.
const hasPart = (h, part) => !!h && (!h.loaded || h.loaded.includes(part));
// Подсказка к доле карточек по шаблону: сколько из скольких и на каком этапе миграция (`template_coverage`).
const templatesState = c => t("overview.templates_state_" + c.state);
// Подсказка к незаконченной пересборке (`kb:reset` пишет отметку): с чего началась, откуда вернуть прежнюю базу и
// сколько источников уже разобрано (число берётся из плана сборки, часть «build»).
const rebuildHint = (r, b) => [
  t("overview.rebuild_hint", {since: r.started ? stampText(Date.parse(r.started)) : "?", tag: r.tag || "—",
                              before: r.cards_before ?? "?"}),
  b && b.total ? t("overview.rebuild_sources", {done: b.total - (b.left || 0), total: b.total}) : ""
].filter(Boolean).join("\n");
const templatesHint = c => [
  t("overview.templates_hint", {done: c.stamped, total: c.stamped + c.left}),
  templatesState(c),
  c.unmanaged || c.records ? t("overview.templates_apart", {n: (c.unmanaged || 0) + (c.records || 0)}) : ""
].filter(Boolean).join("\n");
// Дата обновления базы (1.168.0) — общая для команды, из `kb_updated.json` в git проекта:
// полное «Обновить базу» и следом «Починить базу». → {at, text, chip, title, stale}.
// Источник — свежее из двух: здоровье проекта (после маршрута пересчитывается) или карточка.
const KB_STALE_DAYS = 7;
function kbInfo(p){
  const rec = (p && ((p.health && p.health.kb_updated) || p.kb_updated)) || {};
  const at = rec.updated ? Date.parse(rec.updated) : 0;
  const lines = [];
  if (rec.update) lines.push(t("kb.last_update", {when: stampText(Date.parse(rec.update.at)),
                                                  who: rec.update.who || "—"}));
  if (rec.fix) lines.push(t("kb.last_fix", {when: stampText(Date.parse(rec.fix.at)),
                                            who: rec.fix.who || "—"}));
  // «Обновить» прошло после последнего полного обновления, а «Починить» — ещё нет.
  const pending = rec.update && (!rec.updated || rec.update.at > rec.updated);
  if (pending) lines.push(t("kb.pending_fix"));
  const stale = at && (Date.now() - at) / 86400000 > KB_STALE_DAYS;
  return {at, stale, pending: !!pending,
          text: at ? t("kb.updated", {when: stampText(at)}) : t("kb.never"),
          chip: at ? t("kb.chip", {when: stampText(at)}) : t("kb.chip_never"),
          title: [t("kb.rule"), ...lines].join("\n")};
}
// Когда посчитано самое старое из пришедшего (мс) — ему и верить, а не часам страницы.
const healthAt = h => { const v = Object.values((h && h.at) || {}); return v.length ? Math.min(...v) * 1000 : 0; };
function stampText(ms){
  const d = new Date(ms), now = new Date(), loc = S.lang === "en" ? "en-GB" : "ru-RU";
  const time = d.toLocaleTimeString(loc, {hour: "2-digit", minute: "2-digit"});
  return d.toDateString() === now.toDateString() ? time
    : d.toLocaleDateString(loc, {day: "2-digit", month: "2-digit"}) + " " + time;
}
const waitMark = () => el("span", {class:"spin", title: t("overview.counting")});
function healthFrame(p){
  return {project: p.path, loaded: [], pending: [], stats: {}, todo: null, mirrors: {}, build: {}, templates: {}, rebuild: {},
          doctor: {rc: 0, errors: [], blocker_why: [], warns: [], engine: "—", privacy: "report"},
          lint: {cards: null, errors: null, kinds: {}, baseline: null, fresh: null},
          agent: {}, sources: {installed: [], instances: []}, runs: {}, trace: {},
          source_health: {}, index: {}, ping: {}, unfinished: {}, corrections: {}, retrieval: {}};
}
// Части лежат в памяти панели (1.166.0): посчитаны на её старте и отдаются сразу.
// `force` — пересчитать (кнопка «Обновить», команда в проекте); прежние числа стоят,
// пока не придут новые.
function loadHealth(p, parts = HEALTH_ALL, force = false){
  if (!p.health || p.health.project !== p.path || !p.health.loaded) p.health = healthFrame(p);
  const h = p.health;
  if (S.project && S.project.path === p.path) S.health = h;
  const want = parts.filter(x => !h.pending.includes(x) && (force || !h.loaded.includes(x)));
  h.pending.push(...want);
  return Promise.all(want.map(part =>
    api("/api/health?project=" + encodeURIComponent(p.path) + "&part=" + part
        + (force ? "&fresh=1" : ""), {quiet: true})
      .catch(() => null)
      .then(r => {
        h.pending = h.pending.filter(x => x !== part);
        if (p.health !== h) return;
        // Ответ — своему проекту и только своей частью. Не пришёл — часть всё равно
        // отмечается: вечный спиннер хуже прочерка.
        if (r && !r.error && r.project === p.path){
          // Когда посчитана часть — по часам панели: дата «обновлено» — у самого старого числа.
          const at = r.at || {};
          delete r.project; delete r.parts; delete r.at;
          Object.assign(h, r);
          h.at = Object.assign(h.at || {}, at);
          if (r.kb_updated) p.kb_updated = r.kb_updated;
        }
        if (!h.loaded.includes(part)) h.loaded.push(part);
        healthChanged(p);
      })));
}
// Части приходят пачкой — перерисовка одна на кадр, а не на каждую. Разделы проекта
// (те, что объявили `refreshOn` с «health») показывают только выбранный проект, и часть чужого
// проекта их не касается: на старте панель догружает здоровье всех проектов по семь
// частей, и «Установка» перерисовывалась десятки раз подряд — её блоки мигали (1.170.1).
let HEALTH_FRAME = 0, HEALTH_MINE = false;
function healthChanged(p){
  if (p && p === S.project) HEALTH_MINE = true;
  if (HEALTH_FRAME) return;
  HEALTH_FRAME = requestAnimationFrame(() => {
    HEALTH_FRAME = 0;
    renderOverview(); renderProjBadge(); drawBridgeStamp();
    if (S.project && S.health && S.project.health === S.health) navBadges(S.project, S.health);
    const mine = HEALTH_MINE;
    HEALTH_MINE = false;
    if (!mine) return;
    // Кого это касается, решают сами разделы (`refreshOn` в их коде): ядро имён не знает.
    dispatchModules("health");
  });
}
async function prefetchHealth(fresh = false){
  // Сначала быстрые части всех проектов — Мостик заполняется за секунды; потом долгие, по
  // проекту за раз: линтеры всех баз разом только мешали бы друг другу. Без `fresh` всё
  // приходит из памяти панели сразу: считается оно на её старте и по кнопке «Обновить».
  for (const p of S.state.projects) await loadHealth(p, HEALTH_QUICK, fresh);
  for (const p of S.state.projects) await loadHealth(p, HEALTH_SLOW, fresh);
  drawBridgeStamp();
}
// «Обновлено» на Мостике — время самого старого числа среди проектов, а не открытия страницы.
function drawBridgeStamp(){
  const s = $("#overviewStamp");
  const ats = ((S.state && S.state.projects) || []).map(p => healthAt(p.health)).filter(Boolean);
  if (!ats.length) return;
  s.textContent = t("overview.stamp", {time: stampText(Math.min(...ats))});
  s.hidden = false;
}
function aura(p){
  return {red:"var(--danger)", amber:"var(--tier-inreview)", green:"var(--primary)"}[auraWhy(p).color]
    || "var(--border)";
}

// Цвет карточки — обещание: «зелёный» должен быть достижимой целью, а не догадкой.
// Поэтому правило считается в одном месте и объясняется человеку теми же словами.
function auraWhy(p){
  // Пока доктор или линтер считаются, цвета нет: «зелёный» до прихода чисел — догадка.
  const h = p.health, todo = [];
  const docOk = hasPart(h, "doctor"), lintOk = hasPart(h, "lint");
  const errs = docOk ? h.doctor.errors.length : 0;
  // Линтер не дал числа (база не собрана, движок упал) — «не знаю», а не «undefined ошибок».
  const lintNum = lintOk && Number.isFinite(h.lint.errors);
  const lint = lintNum ? h.lint.errors : 0, base = lintNum ? (h.lint.baseline ?? null) : null;
  if (!docOk) todo.push({bad:false, wait:true, code:"blockers", text: t("aura.counting_doctor")});
  else if (errs) todo.push({bad:true, code:"blockers", text: t("aura.blockers", {n: errs})});
  else todo.push({bad:false, code:"blockers", text: t("aura.no_blockers")});
  if (p.behind) todo.push({bad:true, text: t("aura.behind", {v: p.engine})});
  else todo.push({bad:false, text: t("aura.current")});
  if (!lintOk) todo.push({bad:false, wait:true, text: t("aura.counting_lint")});
  else if (!lintNum) todo.push({bad:false, text: t("aura.lint_unknown")});
  else if (base !== null && lint > base)
    todo.push({bad:true, text: t("aura.lint_worse", {n: lint, base, grew: lint - base})});
  else todo.push({bad:false, text: base !== null
    ? t("aura.lint_ratchet", {n: lint, base}) : t("aura.lint", {n: lint})});
  const color = errs ? "red" : (todo.some(x=>x.bad) ? "amber" : (docOk && lintOk ? "green" : ""));
  return {color, todo};
}
function renderOverview(){
  const box = $("#towers"); box.innerHTML = "";
  if (!S.state.projects.length){
    box.append(el("div",{class:"card",style:"padding:26px"},
      el("b",{}, t("overview.none_title")),
      el("p",{class:"muted",style:"margin:8px 0 0"}, t("overview.none_about")),
      el("button",{class:"btn primary",style:"margin-top:12px",onclick:openNewProject}, t("overview.add_project"))));
    return;
  }
  S.state.projects.forEach(p=>{
    const h = p.health, has = part => hasPart(h, part);
    const pct = has("stats") ? (h.stats.pct_verified ?? 0) : null;
    const errs = has("doctor") ? h.doctor.errors.length : null;
    const lintNum = has("lint") && Number.isFinite(h.lint.errors);
    const lintBad = lintNum && h.lint.baseline !== null && h.lint.errors > h.lint.baseline;
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
        has("stats") ? el("div",{class:"ring",style:`--p:${Math.max(pct,1.5)}`}, el("span",{}, pct+"%"))
          : el("span",{class:"spin",style:"margin-top:6px"})),
      el("div",{class:"foot"},
        el("span",{class:"chip"+(p.behind?" warn":"")}, t("overview.engine", {v: p.engine})),
        // Свежесть базы — то, на чём стоят ответы и документы проекта (1.168.0).
        (k => el("span",{class:"chip" + (k.stale || !k.at ? " warn" : ""), title: k.title}, k.chip))(kbInfo(p)),
        // Пока здоровье считается, место под чипы держим пустыми заглушками: без них
        // карточки подрастают в момент прихода данных, плитки разъезжаются под курсором
        // и клик «выбрать проект» уходит в никуда или в соседний проект.
        has("doctor") ? el("span",{class:"chip "+(errs?"bad":"ok")},
              errs ? t("overview.blockers", {n: errs}) : t("overview.doctor_clean"))
          // Пока считается: спиннер вместо многоточия — «считаю…» рядом с числами
          // читается как обрезанное значение, а не как состояние.
          : el("span",{class:"chip"}, el("span",{class:"spin"}), " " + t("overview.counting")),
        // Линтер всей базы — самая долгая часть: место под него держим сразу.
        has("lint") ? el("span",{class:"chip "+(lintBad?"bad":"")},
              lintNum ? t("overview.lint", {n: h.lint.errors}) : t("overview.lint_unknown"))
          : el("span",{class:"chip"}, el("span",{class:"spin"}), " " + t("overview.lint_counting")),
        // Второе число рядом с долей доверия: сколько источников уже разобрано. Доверие
        // считается по знанию, разбор — по документам, и одно без другого читается криво:
        // высокая доля доверия при половине неразобранных источников — не «почти готово».
        // Карточки из одних встреч в долю доверия не входят — их число рядом, отдельно.
        has("stats") && h.stats.meetings
          ? el("span",{class:"chip", title: t("overview.meetings_hint")},
               t("overview.meetings", {n: h.stats.meetings}))
          : null,
        has("build") && h.build && h.build.total
          ? el("span",{class:"chip "+((h.build.left||0) ? "warn" : "ok"),
                title: t("overview.sources_hint",
                  {done: h.build.total - (h.build.left||0), total: h.build.total})},
               t("overview.sources", {pct: h.build.pct}))
          : null,
        // Доля карточек по шаблону: по ней видно, переведён ли проект на новую схему карточек.
        // Нет числа (шаблонов нет в ките, база пуста, счёт упал) — плитки нет, а не «0%».
        has("templates") && h.templates && h.templates.pct != null
          ? el("span",{class:"chip "+(h.templates.state === "complete" ? "ok" : "warn"),
                title: templatesHint(h.templates)},
               t("overview.templates", {pct: h.templates.pct}))
          : null,
        // Пересборка с нуля начата и не закончена: база сейчас неполна; подсказка называет, как продолжить или вернуть прежнюю.
        has("rebuild") && h.rebuild && h.rebuild.active
          ? el("span",{class:"chip warn", title: rebuildHint(h.rebuild, h.build)}, t("overview.rebuild"))
          : null,
        p.git_branch ? el("span",{class:"chip mono"}, p.git_branch) : null,
        p.dirty ? el("span",{class:"chip warn"}, t("overview.uncommitted", {n: p.dirty}))
                : el("span",{class:"chip ok"}, t("overview.tree_clean")),
        act,
      ));
    tile.id = "tower-"+p.slug;
    box.append(tile);
  });
  // Последняя плитка сетки — путь к новому проекту: чтобы его не искать в «Настройке кита» среди прочего.
  box.append(el("button",{class:"card tower add", "aria-label": t("overview.add_project"),
      title: t("overview.add_project_hint"), onclick: openNewProject},
    el("div",{class:"plus","aria-hidden":"true"}, "+"),
    el("div",{class:"name"}, t("overview.add_project")),
    el("div",{class:"muted",style:"font-size:12.5px"}, t("overview.add_project_about"))));
  drawActivity();
  drawGlobalMetrics();
}
// Сводка по машине. Подписи стоят с первого кадра; число, которое ещё считается, —
// спиннером: блокеры ждут доктора всех проектов, окружение и команды — реестра.
function drawGlobalMetrics(){
  const g = $("#globalMetrics"); g.innerHTML = "";
  const st = S.state, projects = (st && st.projects) || [];
  const behind = projects.filter(p => p.behind).length;
  const doctors = st && projects.every(p => hasPart(p.health, "doctor"));
  const total = doctors ? projects.reduce((a, p) => a + p.health.doctor.errors.length, 0) : 0;
  const extra = !!(st && st.extra);
  const miss = extra ? st.env.items.filter(i => !i.ok).length : 0;
  g.append(
    metric(doctors ? total : waitMark(), t("overview.blockers_all"),
      !doctors ? "" : total ? t("overview.blockers_all_go") : t("overview.blockers_all_ok"),
      !doctors ? "" : total ? "bad" : "ok"),
    metric(st ? behind : waitMark(), t("overview.behind", {v: st ? st.kit.version : "…"}),
      !st ? "" : behind ? t("overview.behind_click") : t("overview.all_current"),
      !st ? "" : behind ? "warn" : "ok", behind ? updateAllProjects : null),
    metric(extra ? miss : waitMark(), t("overview.missing"), t("overview.missing_hint"),
      !extra ? "" : miss ? "warn" : "ok"),
    metric(extra ? st.commands.length : waitMark(), t("overview.commands"),
      t("overview.commands_hint"), extra ? "ok" : ""),
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
  if (go) show("health", undefined, {quiet: true});   // автопереход: скрытый профилем «Здоровье» не упрекаем
  // Движок проекта на этой машине (`.aurora/`) — до здоровья: клон без него считался бы на скриптах кита.
  await ensureEngine(p);
  if (S.project !== p) return;       // пока готовился движок, выбрали другой проект
  // Здоровье — частями и из памяти панели: выбор проекта его не пересчитывает (1.166.0),
  // пересчитывает кнопка «Обновить» в «Здоровье».
  const counting = loadHealth(p, HEALTH_ALL);
  refreshModules();
  await counting;
  // Пока считалось, человек мог выбрать другой проект: его экран дорисует свой pick.
  if (S.project !== p) return;
  const h = p.health;
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
  if (hasPart(h, "doctor")){
    const errs = h.doctor.errors.length;
    setBadge("health", errs || "", errs > 0);
  }
  if (hasPart(h, "mirrors")){
    const mm = Object.values(h.mirrors||{}).reduce(
      (n, x)=> n + (x.no_state ? 1 : (x.missing||0) + (x.orphan||0)), 0);
    setBadge("mirrors", mm || "", mm > 0);
  }
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
  await loadHealth(p, HEALTH_ALL, true);
  return S.health;
}

/* ---------------- пересчёт показателей ---------------- */
// Числа движок считает на каждый запрос, панель их не кэширует. Но работу в базе делает
// не только панель: ассистент обогатил базу, человек принял карточки в редакторе — на
// экране всё ещё вчерашние цифры, и понять «что изменилось» не по чему. Отсюда кнопка
// и отметка времени: видно не только число, но и на какой момент оно посчитано.
async function busy(btn, fn){
  const was = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = ""; btn.append(el("span",{class:"spin"}), t("overview.counting"));
  try { await fn(); } finally { btn.disabled = false; btn.innerHTML = was; }
}
// Из Мостика — к настройкам добавления проекта: «Настройка кита» с раскрытой группой «Подключить новый проект» (локально
// или из git). Группа раскрывается и прокручивается сама: `SGROUPS.focus` читает `sgroup` при построении страницы.
function openNewProject(){
  SGROUPS.focus = "setup:new";
  show("setup");
}
$("#addProject").onclick = openNewProject;
$("#refreshOverview").onclick = ()=> busy($("#refreshOverview"), async ()=>{
  // перечитываем и карточки проектов: ветка, незакоммиченное и версия движка меняются
  // ровно от той же работы, что и числа базы
  const path = S.project && S.project.path;
  const st = await api("/api/state");
  if (!st || !st.projects) return;
  S.state = st;
  st.extra = true;          // ответ целиком: реестр и окружение уже в нём
  S.project = path ? (st.projects.find(x=>x.path===path) || null) : null;
  S.health = null;
  renderOverview(); renderProjBadge();
  await prefetchHealth(true);
  if (S.project && S.project.health) navBadges(S.project, S.project.health);
  refreshModules();
  drawBridgeStamp();
  toast(t("overview.recounted", {n: st.projects.length}), "ok");
});

/* ---------------- вызовы моделей ---------------- */
// Какие модели зовёт кнопка, команда или маршрут — из каталога вызовов движка
// (`scripts/llm_calls.json` → `/api/llm/calls`): роли по шагам и живые цепочки моделей
// кита. Кэш на минуту: правку в «Моделях» подсказка увидит без перезагрузки страницы.
let LLM = null, LLM_AT = 0;
function llmCalls(){
  if (!LLM || Date.now() - LLM_AT > 60000){
    LLM_AT = Date.now();
    LLM = api("/api/llm/calls", {quiet:true}).catch(() => null);
  }
  return LLM;
}
// Ключ — id вызова, команда (`agent:build`), раздел (`ask`) или маршрут (`route:update`).
function llmFor(d, key){
  const calls = (d && d.calls) || [];
  key = String(key || "");
  if (key.startsWith("route:")) return calls.filter(c => c.routes.some(r => r.id === key.slice(6)));
  return calls.filter(c => c.id === key || c.commands.includes(key) || c.ui.includes(key));
}
// Имя роли: своё имя человека — как есть, имя движка — на языке интерфейса.
function llmRole(d, id){
  const r = d.roles[id];
  return !r ? id : r.default_name ? t("models.role_default." + id) : r.name;
}
// В подсказке — короткое имя: у ролей движка оно до двоеточия («Планировщик: планы, …»),
// пояснение роли и так стоит строкой под шагом.
const llmShort = (d, id) => llmRole(d, id).split(":")[0];
// Цепочка роли; пустая роль движка работает на роли по умолчанию своей возможности.
function llmChain(d, id){
  let r = d.roles[id];
  if (r && !r.chain.length && d.default_role[r.cap] && d.roles[d.default_role[r.cap]])
    r = d.roles[d.default_role[r.cap]];
  return r && r.chain.length ? r.chain.map(b => b.model).join(" → ") : t("llm.no_models");
}
function llmView(d, calls){
  const box = el("div", {});
  const line = (...kids) => box.append(el("div", {style:"margin-top:3px"}, ...kids));
  if (calls.length > 2){
    // Маршрут: по вызову строка с ролями, цепочки ролей — один раз внизу.
    const roles = [];
    calls.forEach(c => {
      const rs = [...new Set(c.steps.map(s => s.role).filter(Boolean))];
      rs.forEach(r => { if (!roles.includes(r)) roles.push(r); });
      line(el("span", {style:"font-weight:600"}, c.title),
           " — " + (rs.map(r => llmShort(d, r)).join(", ") || t("llm.direct"))
           + (c.engine === "http" ? " ⚠" : ""));
    });
    roles.forEach(r => line(el("span", {class:"muted"}, llmShort(d, r) + " — " + llmChain(d, r))));
    return box;
  }
  calls.forEach(c => {
    line(el("span", {style:"font-weight:600"}, c.title));
    c.steps.forEach(s => {
      const r = d.roles[s.role];
      const thinks = s.thinking === false ? false : r ? r.thinking : null;
      const marks = [s.chosen ? t("llm.chosen") : "", thinks === false ? t("llm.no_think") : "",
                     s.tools ? t("llm.with_tools") : ""].filter(Boolean);
      line((s.role ? llmShort(d, s.role) + " — " + llmChain(d, s.role) : t("llm.direct"))
           + (marks.length ? " (" + marks.join(", ") + ")" : ""),
           el("div", {class:"muted", style:"font-size:11.5px"}, s.what));
    });
    line(el("span", {class:"muted"}, t("llm.tools", {what: c.tools})));
    if (c.engine === "http")
      line(el("span", {style:"color:var(--tier-inreview)"}, "⚠ " + t("llm.http", {why: c.why_http})));
  });
  return box;
}

/* ---------------- здоровье ---------------- */
// Карточка дашборда: число, подпись и куда идти. Цвет строгий — красный только там, где
// работа СТОИТ; янтарь — есть работа, но всё функционирует; бирюза — в порядке; серый —
// не измерялось. Доля доверенных цветом не красится никогда: 29% на молодой базе это
// норма, а не авария, и порог здесь плодил бы ложную тревогу.
function metricCard(o){
  const tone = o.tone || "";
  const card = el("div",{class:"card metric" + (o.go ? " clickable" : ""),
    style:"padding:14px;min-width:0", title:o.hint || "", "data-llm": o.go && o.go.llm || null},
    el("div",{class:"muted",style:"font-size:12px"}, o.title),
    el("div",{style:"font-size:26px;font-weight:700;margin:2px 0 4px;line-height:1.1"
              + (tone ? ";color:var(--" + tone + ")" : "")},
      o.value && o.value.nodeType ? o.value : String(o.value)),
    el("div",{class:"muted",style:"font-size:12px;line-height:1.35"}, o.sub || ""),
    o.go ? el("div",{class:"chip",style:"margin-top:8px"}, o.go.label) : null);
  if (o.go) card.onclick = o.go.act;
  return card;
}

// Действие карточки: либо запуск маршрута с подтверждением, либо список, с которым надо
// разбираться. Ведём туда, где решают проблему, а не туда, где о ней написано подробнее.
const goRoute = (id, label) => ({label, llm: "route:" + id, act: async () => {
  const d = S.scenarios || (S.scenarios = await api("/api/scenarios?all=1"));
  const sc = (d.scenarios||[]).find(s=>s.id===id);
  if (!sc) return toast(t("run.not_found_route"), "warn");
  runRoute(sc, true);
}});
const goCmd = (cmd, args, label) => ({label, llm: cmd, act: async () => {
  if (!S.state.extra && S.extraP) await S.extraP;
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
async function openRun(cmdName, preFlags=[]){
  // Реестр команд догружается после Мостика: открыли команду раньше — дождёмся его.
  if (!S.state.extra && S.extraP) await S.extraP;
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
  // Команда зовёт модель — какую, по какому промпту и с какими инструментами: из каталога
  // вызовов движка, с живыми цепочками ролей кита.
  const llmBox = el("div", {});
  d.append(llmBox);
  llmCalls().then(dd => {
    const calls = llmFor(dd, r.cmd);
    if (!calls.length || !RUN || RUN.row !== r) return;
    llmBox.append(el("div", {class:"card", style:"padding:10px 12px;margin-top:10px"},
      el("div", {class:"row", style:"gap:8px;align-items:center"},
        el("b", {style:"flex:1"}, t("llm.run_title")),
        el("button", {class:"btn sm", onclick: () => {
          $("#runOverlay").classList.remove("on");
          show("models", {call: calls[0].id});
        }}, t("llm.open"))),
      llmView(dd, calls)));
  });
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
  if (res.cancelled){      // в диалоге связи человек отказался: запуска не было, метка «идёт» не остаётся
    $("#consoleRc").textContent = t("net.cancelled");
    $("#consoleRc").className = "chip warn";
    return;
  }
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
let KINDS_DIGEST = "";
/* Пример пути вида строкой по ответу `/api/kinds/example`: для какого документа и куда ляжет его страница. Чистая
   функция: решает ответ сервера, страница только печатает. */
function kindExample(r){
  if (!r || r.error) return "";
  const path = [r.root || t("kinds.ex_no_root"), ...(r.levels || []), r.title || "{title}"].join(" › ");
  const head = r.doc ? t("kinds.ex_doc", {doc: String(r.doc).split("/").pop(), path}) : t("kinds.ex_nodoc", {path});
  const miss = (r.missing || []).length ? " " + t("kinds.ex_missing", {fields: r.missing.join(", ")}) : "";
  return head + miss + ((r.problems || []).length ? " · " + r.problems.join("; ") : "");
}       // слепок реестра, который прочитала форма; сервер сверяет его при записи (409 — файл изменился)

/* Форма вида — две рамки: «Создание» (шаблон, промпт создания, папка, MCP, маска, задача) и «Публикация в Confluence»
   (родитель, промпт публикации, ревью). Шаблон отвечает за создание, промпт — за публикацию. Промпт создания и промпт
   публикации — разные поля (`prompt` и `publish_prompt`): первое читает изготовление документа, второе пока только
   хранится — подготовка страницы по нему включится позже, и подсказка поля говорит об этом прямо. */
async function renderKinds(box){
  if (!S.project) return;
  const d = await api("/api/kinds?project=" + encodeURIComponent(S.project.path));
  if (d.error) return;
  KINDS = JSON.parse(JSON.stringify(d.kinds || {}));
  KINDS_DIGEST = d.digest || "";
  let drift = null;            // ответ сервера на расхождение: файл на диске изменился, пока форма была открыта
  const card = el("div",{class:"card",style:"padding:20px"});
  const dirty = () => setDirty("kinds", t("dirty.kinds"), true);
  const save = async force => {
    const r = await api("/api/kinds",{method:"POST", quiet:true,
      body:JSON.stringify({project:S.project.path, kinds:KINDS, expect: force ? "" : KINDS_DIGEST})});
    if (r.conflict){ drift = r; draw(); return; }
    if (r.ok){
      KINDS_DIGEST = r.digest || KINDS_DIGEST; drift = null;
      toast(t("kinds.saved", {file: r.target.split("/").pop(), n: r.kinds}));
      setDirty("kinds", t("dirty.kinds"), false);
      renderProjectAfterSave();
    } else toast(r.error || t("kinds.not_saved"), "err");
  };
  const draw = () => {
    card.innerHTML = "";
    card.append(el("p",{class:"muted",style:"font-size:13px;margin:0 0 10px"},
      t("kinds.about")));
    (d.problems||[]).forEach(p=>card.append(el("div",{class:"warnbox",style:"margin-bottom:8px"},
      `${p.kind}: ${p.why}`)));
    if (drift) card.append(el("div",{class:"warnbox",style:"margin-bottom:10px"},
      el("b",{}, t("kinds.drift")), " ", t("kinds.drift_help"),
      el("div",{class:"row",style:"gap:8px;margin-top:8px"},
        el("button",{class:"btn primary", onclick:()=>{
          KINDS = JSON.parse(JSON.stringify(drift.kinds || {})); KINDS_DIGEST = drift.digest || "";
          d.problems = drift.problems || []; drift = null;
          setDirty("kinds", t("dirty.kinds"), false); draw();
        }}, t("kinds.take_disk")),
        el("button",{class:"btn", onclick:()=>save(true)}, t("kinds.keep_mine")))));
    const rows = el("div",{});
    Object.keys(KINDS).sort().forEach(kind=>{
      const rec = KINDS[kind];
      const field = (key, ph, width) => el("input",{class:"btn mono",
        style:`width:${width};font-size:12px`, value:rec[key]||"", placeholder:ph,
        oninput:e=>{ rec[key] = e.target.value; dirty(); }});
      // Подпись над полем называет его роль: шаблон — для создания, промпт публикации — для публикации.
      const lab = (key, input, hint) => el("label",{class:"kfield"},
        el("span",{class:"kf-l"}, t("kinds.l_" + key)), input,
        hint ? el("span",{class:"kf-h"}, hint) : null);
      // Свойства задачи держим свёрнутыми: они нужны не всем типам и не каждый день,
      // а развёрнутый список из шести полей у девяти типов превращает форму в стену.
      rec.task = rec.task || {};
      const tfield = (key, ph, width) => el("input",{class:"btn mono",
        style:`width:${width};font-size:12px`,
        value: Array.isArray(rec.task[key]) ? rec.task[key].join(", ") : (rec.task[key]||""),
        placeholder:ph,
        oninput:e=>{ rec.task[key] = e.target.value; dirty(); }});
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
      const create = el("fieldset",{class:"kframe", "data-frame":"create"},
        el("legend",{}, t("kinds.frame_create")),
        el("div",{class:"muted kf-about"}, t("kinds.frame_create_about")),
        el("div",{class:"row",style:"gap:10px;flex-wrap:wrap;align-items:flex-end"},
          lab("template", field("template", t("kinds.f_template"), "200px")),
          lab("prompt", field("prompt", t("kinds.f_prompt"), "200px")),
          lab("out", field("out", t("kinds.f_out"), "160px")),
          lab("mcp", field("mcp", t("kinds.f_mcp"), "210px")),
          // Маска ключа Requirement Yogi: ключ заводит реестр, Аврора проверяет, что он написан по маске вида.
          el("span",{title: t("kinds.ref_mask_hint"), style:"flex:none"},
            lab("ref_mask", field("ref_mask", t("kinds.f_ref_mask"), "230px"))),
          // Правило включается видом документа, а не глобально: у ОПЗ стек — это предмет,
          // и общее правило заставило бы критика ругаться на каждый такой документ.
          el("label",{class:"row",style:"gap:6px;font-size:12px;flex:none",
                title: t("kinds.tech_hint")},
            el("input",{type:"checkbox",
              checked:(String(rec.tech_agnostic||"").toLowerCase()==="true")?"":null,
              onchange:e=>{ rec.tech_agnostic = e.target.checked ? "true" : ""; dirty(); }}),
            t("kinds.tech_agnostic")),
          el("button",{class:"btn sm", title: t("kinds.task_hint"),
            onclick:e=>{ taskBox.hidden = !taskBox.hidden;
                         e.target.textContent = taskBox.hidden ? t("kinds.task_open")
                                                               : t("kinds.task_close"); }},
            t("kinds.task_open"))),
        taskBox);
      // Пример пути считает сервер тем же кодом, что публикация: «для документа …: Корень › Раздел › Заголовок».
      // Значения формы — несохранённые тоже — уходят запросом; ответ приходит, пока человек печатает (с паузой).
      const example = el("div",{class:"muted kf-example", "data-kind": kind});
      let exampleTimer = null;
      const exampleNow = async () => {
        const q = new URLSearchParams({project: S.project.path, kind, out: rec.out || "", publish_url: rec.publish_url || "",
                                       publish_path: rec.publish_path || "", publish_title: rec.publish_title || ""});
        example.textContent = kindExample(await api("/api/kinds/example?" + q.toString(), {quiet:true}));
      };
      const watch = input => { input.addEventListener("input", () => { clearTimeout(exampleTimer);
                                                                       exampleTimer = setTimeout(exampleNow, 350); });
                               return input; };
      if (rec.publish_url || rec.publish_path || rec.publish_title) exampleNow();
      const publish = el("fieldset",{class:"kframe", "data-frame":"publish"},
        el("legend",{}, t("kinds.frame_publish")),
        el("div",{class:"muted kf-about"}, t("kinds.frame_publish_about")),
        el("div",{class:"row",style:"gap:10px;flex-wrap:wrap;align-items:flex-end"},
          lab("publish_url", watch(field("publish_url", t("kinds.f_publish"), "260px"))),
          // Путь под корнем — цепочка разделов через «>», подстановки из шапки документа; заголовок — по шаблону.
          lab("publish_path", watch(field("publish_path", t("kinds.f_publish_path"), "260px")), t("kinds.publish_path_hint")),
          lab("publish_title", watch(field("publish_title", t("kinds.f_publish_title"), "200px"))),
          lab("publish_prompt", field("publish_prompt", t("kinds.f_publish_prompt"), "220px"),
              t("kinds.publish_prompt_hint")),
          // Ревью по чек-листу перед публикацией: ворота `ship:publish` пускают наружу только свежее ревью.
          el("label",{class:"kfield", title: t("kinds.review_hint")},
            el("span",{class:"kf-l"}, t("kinds.review")),
            el("select",{class:"btn sm",style:"font-weight:400",
              onchange:e=>{ rec.review = e.target.value; dirty(); }},
              ...["", ...(d.review_profiles||[])].map(v=>el("option",{value:v, selected:(rec.review||"")===v?"":null},
                v ? t("kinds.review_" + v) : t("kinds.review_off")))))),
        example);
      rows.append(el("div",{class:"list-item",style:"gap:8px;flex-wrap:wrap"},
        el("span",{class:"chip mono",style:"flex:none"}, kind),
        lab("title", field("title", t("kinds.f_title"), "240px")),
        el("span",{style:"flex:1"}),
        el("button",{class:"btn sm", title: t("kinds.drop"),
          onclick:()=>{ delete KINDS[kind]; dirty(); draw(); }},"×"),
        el("div",{class:"kframes"}, create, publish)));
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
                      publish_url:"", publish_path:"", publish_title:"", publish_prompt:"", mcp:"", tech_agnostic:"", review:"",
                      ref_mask:"", task:{}};
        name.value = ""; dirty(); draw();
      }}, t("kinds.add")),
      saveButton("kinds", t("dirty.kinds"), t("kinds.save"), ()=>save(false)),
      el("span",{class:"muted",style:"font-size:12px"}, t("kinds.out_note"))));
    if ((d.templates||[]).length)
      card.append(el("div",{class:"muted",style:"font-size:12px;margin-top:10px"},
        t("kinds.templates", {list: d.templates.slice(0,12).join(", ")})
        + (d.templates.length > 12 ? t("kinds.templates_more", {n: d.templates.length}) : "")));
  };
  draw();
  sgroup(box, "project:kinds", t("kinds.title", {project: S.project.name})).append(card);
}

/* ---------------- режим доступа источников ----------------

   Как источник проекта достаётся: через интернет, через VPN-профиль или локально. В проекте хранится только ИМЯ профиля
   (блок `source_access:` конфига) — профили и адреса проверки живут на машине, в разделе «Проверка соединения». Группа
   пишет только свой блок, остальной конфиг не трогает, и правку одной группы не сбрасывает записью другой
   (`renderProject()` здесь не зовётся). Источник без записи работает как раньше: доступен всегда (у веба умолчание —
   «без VPN, требует интернет»). */
async function renderSourceAccess(box){
  if (!S.project) return;
  const base = "/api/net/access?project=" + encodeURIComponent(S.project.path);
  let d = await api(base, {quiet:true});
  if (!d || d.error || !d.sources) return;
  const host = sgroup(box, "project:access", t("access.title", {project: S.project.name}));
  const card = el("div", {class:"card", style:"padding:20px"});
  host.append(card);
  const MODES = ["internet", "vpn", "local", "mixed"];
  // Объект без прототипа: имя источника «constructor» или «__proto__» — обычный ключ, а не свойство Object.
  const fresh = src => Object.assign(Object.create(null), JSON.parse(JSON.stringify(src || {})));
  let ACC = fresh(d.explicit), digest = d.digest;
  const touched = () => setDirty("access", t("dirty.access"), true);
  const eff = src => {
    const rec = ACC[src.id] || {};
    const mode = rec.mode || src.default_mode || "";
    return {mode, vpn: (mode === "vpn" || mode === "mixed") ? (rec.vpn || "") : "",
            internet: rec.internet !== undefined ? rec.internet : mode === "internet",
            cache: rec.offline_cache !== undefined ? rec.offline_cache : (mode === "local" || mode === "mixed")};
  };
  const edit = (src, fn) => {
    const rec = {...(ACC[src.id] || {})};
    fn(rec);
    if (Object.keys(rec).length) ACC[src.id] = rec; else delete ACC[src.id];
    touched(); draw();
  };
  const selectOf = (opts, value, onchange) => {
    const sel = el("select", {class:"btn", style:"width:100%;font-weight:400"},
      ...opts.map(([v, label]) => el("option", {value:v, selected: v === value ? "" : null}, label)));
    sel.onchange = () => onchange(sel.value);
    return sel;
  };
  const labelled = (label, node, hint) => el("label", {style:"display:block;flex:1;min-width:170px"},
    el("div", {class:"muted", style:"font-size:11.5px;margin-bottom:3px"}, label), node,
    hint ? el("div", {class:"muted", style:"font-size:11px;margin-top:3px"}, hint) : null);
  const row = src => {
    const e = eff(src), rec = ACC[src.id] || {};
    const dflt = src.default_mode ? t("access.mode." + src.default_mode) : t("access.mode_unset");
    const modeSel = selectOf([["", t("access.mode_default", {what: dflt})], ...MODES.map(m => [m, t("access.mode." + m)])],
      rec.mode || "", v => edit(src, r => {
        if (v){ r.mode = v; if (v !== "vpn" && v !== "mixed") delete r.vpn; }     // имя профиля нужно только режимам с VPN
        else { delete r.mode; delete r.vpn; }
      }));
    const cells = [labelled(t("access.f_mode"), modeSel, e.mode ? t("access.h." + e.mode) : t("access.h_unset"))];
    if (e.mode === "vpn" || e.mode === "mixed"){
      const names = d.profiles || [];
      const opts = [["", t("access.vpn_none")], ...names.map(n => [n, n])];
      if (e.vpn && !names.includes(e.vpn)) opts.push([e.vpn, t("access.vpn_missing", {name: e.vpn})]);
      cells.push(labelled(t("access.f_vpn"), selectOf(opts, e.vpn, v => edit(src, r => { if (v) r.vpn = v; else delete r.vpn; })),
        names.length ? t("access.h_vpn") : t("access.no_profiles")));
    }
    const flag = (key, label, hint, value) => el("label", {class:"row", style:"gap:6px;font-size:12.5px", title: hint},
      el("input", {type:"checkbox", checked: value ? "" : null,
        onchange: ev => edit(src, r => { r[key] = ev.target.checked; })}), label);
    return el("div", {class:"list-item", style:"flex-direction:column;align-items:stretch;gap:8px"},
      el("div", {class:"row", style:"gap:8px"}, el("b", {}, src.title || src.module), el("span", {class:"chip mono"}, src.id),
        e.mode ? null : el("span", {class:"chip"}, t("access.as_before")),
        ACC[src.id] ? el("button", {class:"btn sm", style:"margin-left:auto", onclick: () => { delete ACC[src.id]; touched(); draw(); }},
          t("access.reset")) : null),
      el("div", {class:"row", style:"gap:10px;flex-wrap:wrap;align-items:flex-start"}, ...cells),
      el("div", {class:"row", style:"gap:16px;flex-wrap:wrap"},
        flag("internet", t("access.f_internet"), t("access.h_internet"), e.internet),
        flag("offline_cache", t("access.f_cache"), t("access.h_cache"), e.cache)));
  };
  const send = replace => api("/api/net/access/save", {method:"POST", quiet:true,
    body: JSON.stringify({project: S.project.path, access: ACC, expect: digest, replace_lossy: replace === true})});
  const save = async () => {
    let r = await send(false);
    // В блоке есть то, что переписывание потеряет (комментарии, лишние поля): заменяем только по согласию.
    if (r && r.code === "lossy"){
      const list = (r.lossy || []).map(k => "• " + t("access.lossy." + k)).join("\n");
      if (!confirm(t("access.lossy_ask", {list}))) return;
      r = await send(true);
    }
    if (r && r.ok){
      d = r.state; ACC = fresh(d.explicit); digest = d.digest;
      setDirty("access", t("dirty.access"), false);
      toast(t("access.saved"), "ok");
      draw();
      return;
    }
    if (r && r.conflict) return toast(t("access.conflict"), "err");
    // Код ошибки сервера → строка каталога; кода нет в таблице — текст сервера.
    const ERR = {bad_mode: () => t("access.err.bad_mode"), bad_name: () => t("access.err.bad_name"),
                 bad_flag: () => t("access.err.bad_flag"), bad_field: () => t("access.err.bad_field"),
                 bad_source: () => t("access.err.bad_source"), bad_access: () => t("access.err.bad_access"),
                 no_config: () => t("access.err.no_config"), not_utf8: () => t("access.err.not_utf8"),
                 write: () => t("access.err.write"), unsupported: () => t("access.err.unsupported")};
    const say = ERR[r && r.code];
    toast(say ? say() : ((r && r.error) || t("access.save_failed")), "err");
  };
  const draw = () => {
    card.innerHTML = "";
    card.append(el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"}, t("access.about")));
    if (!d.sources.length){
      card.append(el("div", {class:"muted", style:"font-size:13px"}, t("access.none")));
      return;
    }
    if ((d.lossy || []).length)
      card.append(el("div", {class:"warnbox", style:"margin-bottom:10px;font-size:12.5px"},
        el("div", {}, t("access.lossy_title")), ...d.lossy.map(k => el("div", {}, "• " + t("access.lossy." + k)))));
    d.sources.forEach(src => card.append(row(src)));
    card.append(el("div", {class:"row", style:"margin-top:12px;gap:10px"},
      saveButton("access", t("dirty.access"), t("access.save"), save),
      el("span", {class:"muted", style:"font-size:12px"}, t("access.note")),
      el("button", {class:"btn sm", onclick: () => show("connectivity")}, t("access.open_net"))));
    setDirty("access", t("dirty.access"), JSON.stringify(ACC) !== JSON.stringify(d.explicit || {}));
  };
  draw();
}

/* ---------------- извлечение знаний из вложений ----------------

   Что `kb:ingest-office` переводит в текст для разбора: вложения страниц (docx, xlsx, pdf…), схемы draw.io и модели BPMN,
   по желанию — изображения моделью, и сколько файлов за один запуск. Пишется только блок `ingest:` конфига проекта;
   остальной конфиг не трогается, слепок блока уходит назад как `expect` — правка файла руками между открытием страницы и
   записью не теряется (отказ 409). Страницу целиком здесь не перерисовывают: это сбросило бы несохранённые правки соседних групп. */
async function renderIngest(box){
  if (!S.project) return;
  let d = await api("/api/ingest/settings?project=" + encodeURIComponent(S.project.path), {quiet:true});
  if (!d || d.error || !d.settings) return;
  const host = sgroup(box, "project:ingest", t("ingest.title", {project: S.project.name}));
  const card = el("div", {class:"card", style:"padding:20px"});
  host.append(card);
  let cur = {...d.settings}, digest = d.digest;
  const touched = () => setDirty("ingest", t("dirty.ingest"), JSON.stringify(cur) !== JSON.stringify(d.settings));
  const flag = (key, label, hint) => el("label", {class:"row", style:"gap:8px;align-items:flex-start;font-size:13px;margin-bottom:10px"},
    el("input", {type:"checkbox", "data-key":key, checked: cur[key] ? "" : null,
      onchange: ev => { cur[key] = ev.target.checked; touched(); }}),
    el("span", {style:"flex:1"}, label, el("div", {class:"muted", style:"font-size:11.5px;margin-top:2px"}, hint)));
  const save = async () => {
    const r = await api("/api/ingest/settings/save", {method:"POST", quiet:true,
      body: JSON.stringify({project: S.project.path, settings: cur, expect: digest})});
    if (r && r.ok){
      d = r.state; cur = {...d.settings}; digest = d.digest;
      setDirty("ingest", t("dirty.ingest"), false);
      toast(t("ingest.saved"), "ok");
      draw();
      return;
    }
    if (r && r.conflict) return toast(t("ingest.conflict"), "err");
    // Код ошибки сервера → строка каталога; кода нет в таблице — текст сервера.
    const flagErr = () => t("ingest.err.flag");
    const ERR = {attachments: flagErr, diagrams: flagErr, images: flagErr, unknown: () => t("ingest.err.unknown"),
                 max_files: () => t("ingest.err.max_files", {max: d.ceiling}),
                 no_config: () => t("ingest.err.no_config"), not_utf8: () => t("ingest.err.not_utf8"),
                 write: () => t("ingest.err.write")};
    const say = ERR[r && r.code];
    toast(say ? say() : ((r && r.error) || t("ingest.save_failed")), "err");
  };
  const draw = () => {
    card.innerHTML = "";
    card.append(el("p", {class:"muted", style:"font-size:13px;margin:0 0 12px"}, t("ingest.about")));
    card.append(flag("attachments", t("ingest.f_attachments"), t("ingest.h_attachments")));
    card.append(flag("diagrams", t("ingest.f_diagrams"), t("ingest.h_diagrams")));
    card.append(flag("images", t("ingest.f_images"), t("ingest.h_images")));
    card.append(el("label", {style:"display:block;max-width:260px;margin-bottom:12px"},
      el("div", {class:"muted", style:"font-size:11.5px;margin-bottom:3px"}, t("ingest.f_max_files")),
      el("input", {class:"btn", type:"number", min:"1", max:String(d.ceiling), "data-key":"max_files",
        style:"width:100%;font-weight:400", value:String(cur.max_files),
        oninput: ev => { cur.max_files = ev.target.value === "" ? "" : Number(ev.target.value); touched(); }}),
      el("div", {class:"muted", style:"font-size:11px;margin-top:3px"}, t("ingest.h_max_files", {max: d.ceiling}))));
    card.append(el("div", {class:"row", style:"margin-top:4px;gap:10px"},
      saveButton("ingest", t("dirty.ingest"), t("ingest.save"), save),
      el("span", {class:"muted", style:"font-size:12px"}, t("ingest.note"))));
    setDirty("ingest", t("dirty.ingest"), JSON.stringify(cur) !== JSON.stringify(d.settings));
  };
  draw();
}

/* ---------------- типы карточек ----------------

   Реестр типов собирается из шаблонов `akdb_*` кита и проекта. Проект подменяет подпись и описание типа и
   выключает тип блоком `cards:` своего конфига, а текст инструкции «Как определить тип карточки» держит
   файлом `Prompts/KB_classify.md`. Панель здесь только удобный ввод: читает и пишет сервер ровно то же,
   что `kb:cards`, считать и проверять сама ничего не умеет — числа и замечания приходят от движка.

   Таблица и инструкция сохраняются порознь и друг друга не трогают: после записи одной перерисовывается
   только она. `renderProject()` здесь не зовётся — он сбросил бы несохранённые правки соседних групп. */
async function renderCardTypes(box){
  if (!S.project) return;
  const base = "/api/cards?project=" + encodeURIComponent(S.project.path);
  const d = await api(base);
  if (d.error) return;
  const host = sgroup(box, "project:cardtypes", t("cardtypes.title", {project: S.project.name}));
  const tableBox = el("div", {class:"card", style:"padding:20px"});
  const guideBox = el("div", {class:"card", style:"padding:20px;margin-top:14px"});
  const statsBox = el("div", {class:"card", style:"padding:20px;margin-top:14px"});
  const howBox = el("div", {class:"card", style:"padding:20px;margin-top:14px"});
  host.append(tableBox, guideBox, statsBox, howBox);

  let data = d, edit = {}, mark = "", drift = null;
  // Подмены, которые сейчас набраны в таблице: только отличия от умолчаний шаблонов, как их запишет сервер.
  const overridesOf = () => {
    const out = {};
    for (const r of data.types){
      const e = edit[r.id];
      if (!e) continue;
      const o = {}, name = e.name.trim(), text = e.description.trim();
      if (name && name !== r.default_name) o.title = name;
      if (text && text !== r.default_description) o.description = text;
      if (!e.enabled && !r.locked) o.enabled = false;
      if (Object.keys(o).length) out[r.id] = o;
    }
    return out;
  };
  const seed = () => {
    edit = {};
    data.types.forEach(r => { edit[r.id] = {name: r.name, description: r.description, enabled: r.enabled}; });
    mark = JSON.stringify(overridesOf());
  };
  const touched = () => setDirty("cardtypes", t("dirty.cardtypes"), JSON.stringify(overridesOf()) !== mark);

  // Поле таблицы: подпись «по умолчанию» или «изменено» и возврат к умолчанию шаблона.
  // key — поле данных (`name`, `description`: от него имя `default_<key>` в ответе сервера), field — часть ключа надписи колонки.
  const cell = (r, key, field, limit, width) => {
    const e = edit[r.id];
    const note = el("div", {class:"muted", style:"font-size:11px;margin-top:3px"});
    const input = el("input", {class:"btn", style:`width:${width};font-weight:400;font-size:12.5px`,
      maxlength:String(limit), value:e[key], "aria-label": t("cardtypes.col_" + field) + ": " + r.id});
    const back = el("button", {class:"btn sm", title: t("cardtypes.default_reset"), onclick:() => {
      e[key] = r["default_" + key]; input.value = e[key]; sync(); touched();
    }}, "↺");
    const sync = () => {
      const same = !e[key].trim() || e[key].trim() === r["default_" + key];
      note.textContent = same ? t("cardtypes.default_mark") : t("cardtypes.changed_mark");
      back.hidden = same;
    };
    input.addEventListener("input", () => { e[key] = input.value; sync(); touched(); });
    sync();
    return el("div", {}, el("div", {class:"row", style:"gap:6px;flex-wrap:nowrap"}, input, back), note);
  };

  const drawTable = () => {
    tableBox.innerHTML = "";
    tableBox.append(el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px", html: tick(t("cardtypes.about"))}));
    if (data.problems.length)
      tableBox.append(el("div", {class:"warnbox"}, el("b", {}, t("cardtypes.problems")),
        ...data.problems.slice(0, 6).map(p => el("div", {class:"mono", style:"font-size:12px;margin-top:4px"}, p)),
        data.problems.length > 6 ? el("div", {class:"muted", style:"font-size:12px;margin-top:4px"},
          t("cardtypes.problems_more", {n: data.problems.length - 6})) : null));
    if (drift) tableBox.append(driftBox());
    if (!data.types.length){
      tableBox.append(el("div", {class:"warnbox", html: tick(t("cardtypes.no_types"))}));
      return;
    }
    const head = ["name", "type", "desc", "template", "section", "class", "origin", "on"]
      .map(c => el("th", {}, t("cardtypes.col_" + c)));
    const rows = data.types.map(r => {
      const e = edit[r.id];
      const on = el("input", {type:"checkbox", checked: e.enabled ? "" : null, disabled: r.locked ? "" : null,
        title: r.locked ? t("cardtypes.locked") : t("cardtypes.on_hint"), "aria-label": t("cardtypes.col_on") + ": " + r.id,
        onchange: ev => { e.enabled = ev.target.checked; touched(); }});
      return el("tr", {},
        el("td", {}, cell(r, "name", "name", data.limits.title, "170px")),
        el("td", {}, el("span", {class:"chip mono"}, r.id)),
        el("td", {}, cell(r, "description", "desc", data.limits.description, "min(380px,60vw)")),
        el("td", {}, el("code", {class:"mono", style:"font-size:12px"}, r.template)),
        el("td", {}, el("code", {class:"mono", style:"font-size:12px"}, r.section || "—")),
        el("td", {}, t("cardtypes.class_" + r.class)),
        el("td", {}, t("cardtypes.origin_" + r.origin)),
        el("td", {style:"text-align:center"}, on, r.locked ? el("div", {class:"muted", style:"font-size:11px"},
          t("cardtypes.locked_short")) : null));
    });
    tableBox.append(el("div", {class:"scroll-x"},
      el("table", {}, el("thead", {}, el("tr", {}, head)), el("tbody", {}, rows))));
    tableBox.append(el("div", {class:"row", style:"margin-top:12px;gap:10px"},
      saveButton("cardtypes", t("dirty.cardtypes"), t("cardtypes.save"), () => saveTable()),
      el("span", {class:"muted", style:"font-size:12px"}, t("cardtypes.save_note"))));
    const cat = data.catalog;
    if (cat && cat.state)
      tableBox.append(el("div", {class:"muted", style:"font-size:12px;margin-top:10px", html:
        tick(t("cardtypes.catalog_" + cat.state, {path: cat.path}) + (cat.pinned ? " " + t("cardtypes.catalog_pinned") : ""))}));
    touched();          // таблицу перерисовали, а правки в ней остались: подсветка кнопки должна им соответствовать
  };

  // Блок `cards:` на диске изменился, пока таблица была открыта: молча затирать не будем.
  const driftBox = () => el("div", {class:"warnbox"},
    el("b", {}, t("cardtypes.conflict")),
    el("p", {class:"sub", style:"margin:6px 0"}, t("cardtypes.conflict_help")),
    el("div", {class:"row", style:"gap:8px"},
      el("button", {class:"btn primary", onclick:() => saveTable(drift.overrides_digest)}, t("editor.keep_mine")),
      el("button", {class:"btn", onclick:() => {
        data = {...data, ...drift}; drift = null; seed(); setDirty("cardtypes", t("dirty.cardtypes"), false); drawTable();
      }}, t("editor.take_disk"))));

  // Запись блока `cards:` не оставляет прежний текст конфига под рукой: поле «aurora.config.yaml» ниже читали до неё.
  const freshenYaml = async () => {
    const ta = $("[data-group='project:yaml'] textarea", box);
    if (!ta) return;
    if (DIRTY.has("yaml")){ toast(t("cardtypes.yaml_stale"), "warn"); return; }
    const cfg = await api("/api/config?project=" + encodeURIComponent(S.project.path), {quiet:true});
    if (typeof cfg.text === "string") ta.value = cfg.text;
  };

  // Запись идёт по одной: второе нажатие, пока первое не ответило, увидело бы уже сменившийся слепок и показало бы
  // ложное расхождение.
  let busy = false;
  const alone = async work => {
    if (busy) return;
    busy = true;
    try { await work(); } finally { busy = false; }
  };

  const saveTable = expect => alone(async () => {
    const r = await api("/api/cards", {method:"POST", quiet:true, body: JSON.stringify(
      {project: S.project.path, overrides: overridesOf(), expect: expect === undefined ? data.overrides_digest : expect})});
    if (r.conflict){ drift = r; drawTable(); return; }
    if (r.error){ toast(r.error, "err"); return; }
    data = r; drift = null; seed(); setDirty("cardtypes", t("dirty.cardtypes"), false);
    toast(t("cardtypes.saved") + (r.catalog_result === "written" ? " · " + t("cardtypes.catalog_written") : ""), "ok");
    if (r.catalog_result === "pinned") toast(t("cardtypes.catalog_pinned"), "warn");
    if (r.warning) toast(r.warning, "warn");
    drawTable();
    freshenYaml();
  });

  /* ---- инструкция «Как определить тип карточки» ---- */
  let gd = d.guide, ta = null, seq = 0, timer = null;
  const checkLine = c => t("cardtypes.check_" + String(c.code).replace(/-/g, "_"),
    {chars: c.chars, limit: c.limit, missing: (c.missing || []).join(", ")});

  const drawChecks = (list, area) => {
    area.innerHTML = "";
    if (!list.length){ area.append(el("div", {class:"muted", style:"font-size:12px"}, "✓ " + t("cardtypes.guide_ok"))); return; }
    list.forEach(c => area.append(el("div", {class:"warnbox", style:"margin:6px 0", html: tick(checkLine(c))})));
  };

  const drawGuide = () => {
    guideBox.innerHTML = "";
    const ro = gd.readonly;
    const counter = el("span", {class:"chip"}), checks = el("div", {style:"margin-top:8px"}), alert = el("div", {});
    ta = el("textarea", {class:"mono", spellcheck:"false", readonly: ro ? "" : null,
      style:"width:100%;min-height:340px;background:var(--console-bg);color:var(--console-ink);"
          + "border:1px solid var(--border);border-radius:12px;padding:14px 16px;font-size:12.5px;"
          + "line-height:1.6;resize:vertical", "aria-label": t("cardtypes.guide_title")}, gd.text);
    const recount = () => {
      counter.textContent = t("cardtypes.guide_chars", {n: ta.value.length, max: gd.limit});
      counter.className = "chip" + (ta.value.length > gd.limit ? " warn" : "");
    };
    const recheck = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const mine = ++seq;
        const r = await api("/api/cards/guide/check", {method:"POST", quiet:true,
          body: JSON.stringify({project: S.project.path, text: ta.value})});
        if (mine === seq && r.checks) drawChecks(r.checks, checks);
      }, 350);
    };
    ta.addEventListener("input", () => {
      recount(); recheck(); setDirty("cardguide", t("dirty.cardguide"), ta.value !== gd.text);
    });
    recount(); drawChecks(gd.checks, checks);

    const where = gd.source === "project" ? t("cardtypes.guide_src_project")
                : gd.exists ? t("cardtypes.guide_src_blank") : t("cardtypes.guide_src_kit");
    guideBox.append(
      el("h3", {style:"margin:0 0 6px"}, t("cardtypes.guide_title")),
      el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px", html: tick(t("cardtypes.guide_about"))}),
      el("div", {class:"row", style:"gap:8px;margin-bottom:8px"},
        el("span", {class:"muted", style:"font-size:12px", html: tick(where)}), counter,
        gd.source === "project" ? el("span", {class:"chip" + (gd.modified ? " warn" : " ok")},
          gd.modified ? t("cardtypes.guide_modified") : t("cardtypes.guide_same")) : null),
      ...(ro ? [el("div", {class:"warnbox", html: tick(t("cardtypes.guide_ro_" + ro))})] : []),
      alert, ta, checks,
      el("div", {class:"row", style:"margin-top:12px;gap:10px"},
        saveButton("cardguide", t("dirty.cardguide"), t("cardtypes.guide_save"), () => saveGuide(alert)),
        el("button", {class:"btn", disabled: (ro || !gd.kit_available) ? "" : null,
          title: gd.kit_available ? null : t("cardtypes.guide_no_kit"), onclick:() => resetGuide(alert)},
          t("cardtypes.guide_reset"))));
    // Кнопка «Сохранить» у чтения-только не нужна: сохранять нельзя.
    if (ro) $("[data-save='cardguide']", guideBox).disabled = true;
  };

  // Файл инструкции изменился на диске, пока текст был открыт: то же окно выбора, что у редактора «Файлов».
  const guideDrift = (r, alert) => {
    alert.innerHTML = "";
    alert.append(el("div", {class:"warnbox"},
      el("b", {}, t("cardtypes.guide_conflict")),
      el("p", {class:"sub", style:"margin:6px 0"}, t("cardtypes.guide_conflict_help")),
      el("div", {class:"row", style:"gap:8px"},
        el("button", {class:"btn primary", onclick:() => saveGuide(alert, r.digest)}, t("editor.keep_mine")),
        el("button", {class:"btn", onclick:() => {
          gd = r.guide; setDirty("cardguide", t("dirty.cardguide"), false); drawGuide();
        }}, t("editor.take_disk")))));
  };

  const guideDone = (r, text) => {
    gd = r.guide; setDirty("cardguide", t("dirty.cardguide"), false); drawGuide();
    toast(r.unchanged ? t("cardtypes.guide_unchanged") : text, "ok");
  };

  const saveGuide = (alert, expect) => alone(async () => {
    const r = await api("/api/cards/guide", {method:"POST", quiet:true, body: JSON.stringify(
      {project: S.project.path, text: ta.value, expect: expect === undefined ? gd.digest : expect})});
    if (r.conflict) return guideDrift(r, alert);
    if (r.error) return toast(r.error, "err");
    guideDone(r, t("cardtypes.guide_saved"));
  });

  const resetGuide = alert => alone(async () => {
    if (!confirm(t("cardtypes.guide_reset_ask"))) return;
    const r = await api("/api/cards/guide/reset", {method:"POST", quiet:true,
      body: JSON.stringify({project: S.project.path, expect: gd.digest})});
    if (r.conflict) return guideDrift(r, alert);
    if (r.error) return toast(r.error, "err");
    guideDone(r, t("cardtypes.guide_reset_done"));
  });

  /* ---- состояние базы по шаблонам (только чтение) ---- */
  const drawStats = st => {
    statsBox.innerHTML = "";
    statsBox.append(el("h3", {style:"margin:0 0 6px"}, t("cardtypes.stats_title")));
    if (st === undefined){ statsBox.append(el("div", {class:"muted"}, t("cardtypes.stats_wait"))); return; }
    if (!st){ statsBox.append(el("div", {class:"muted"}, t("cardtypes.stats_none"))); return; }
    const line = text => el("div", {style:"margin:3px 0"}, text);
    statsBox.append(line(t("cardtypes.stats_total", {n: st.total})));
    const names = Object.keys(st.by_template);
    statsBox.append(line(t("cardtypes.stats_templates") + (names.length ? "" : " —")));
    names.forEach(n => statsBox.append(el("div", {class:"mono", style:"font-size:12px;margin:2px 0 2px 16px"},
      `${n} — ${st.by_template[n]}`)));
    statsBox.append(line(t("cardtypes.stats_legacy", {n: st.legacy})),
      line(t("cardtypes.stats_violations", {n: st.violations})));
    statsBox.append(el("div", {class:"row", style:"margin-top:10px;gap:8px"},
      el("button", {class:"btn sm", onclick:() => {
        if (!confirmLeave()) return;
        goCmd("kb:cards", ["--check", "--summary"], t("cardtypes.stats_check")).act();
      }}, t("cardtypes.stats_check")),
      el("button", {class:"btn sm", onclick:() => loadStats()}, t("cardtypes.stats_refresh")),
      el("span", {class:"muted", style:"font-size:12px"}, t("cardtypes.stats_note"))));
  };
  async function loadStats(){
    drawStats(undefined);
    const r = await api(base + "&stats=1", {quiet:true});
    drawStats(r && r.stats);
  }

  howBox.append(el("h3", {style:"margin:0 0 6px"}, t("cardtypes.howto_title")),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px", html: tick(t("cardtypes.howto"))}),
    el("button", {class:"btn sm", onclick:() => openDoc("docs/card-templates.md")}, t("cardtypes.howto_open")));

  // Цифры считает сервер по всей базе (на 3000 карточек — больше двух секунд): не при каждом открытии страницы,
  // а когда группу раскрыли.
  let statsAsked = false;
  const askStats = () => { if (!statsAsked){ statsAsked = true; loadStats(); } };
  const group = host.closest(".sgroup");
  seed();
  drawTable();
  drawGuide();
  if (!group || !host.hidden) askStats();
  else {
    statsBox.append(el("h3", {style:"margin:0 0 6px"}, t("cardtypes.stats_title")),
      el("div", {class:"muted"}, t("cardtypes.stats_lazy")));
    const open = group.sgSet;
    group.sgSet = now => { open(now); if (now) askStats(); };
  }
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

/* ---------------- что идёт на машине ---------------- */
// Консоль показывала задания только выбранного проекта, и во время цепочки cron, идущей по
// другим проектам, стояла пустой (1.167.0). Теперь над ней — всё, что идёт на машине:
// цепочка расписания, маршруты и команды всех проектов; «Показать» переключает вывод, а
// показываемое отмечено. Писать в Консоль может только один поток: переключились — прежний
// замолкает (`FOLLOW`).
let FOLLOW = 0, SHOWN = null;      // номер текущего потока Консоли и что он показывает
const isShown = (kind, id) => !!SHOWN && SHOWN.kind === kind && (id == null || SHOWN.id === id);
async function liveAll(){
  try { return await api("/api/live", {quiet:true}) || {}; } catch (e) { return {}; }
}
async function drawLiveJobs(){
  const box = $("#consoleLive");
  if (!box) return;
  const d = await liveAll();
  box.replaceChildren();
  const mins = s => Math.max(0, Math.round((Date.now()/1000 - s) / 60));
  const card = (title, sub, shown, go) => box.append(el("div",{class:"card row",
      style:"padding:10px 12px;margin-bottom:10px;gap:12px;align-items:center"},
    el("div",{style:"flex:1;min-width:0"},
      el("div",{style:"font-weight:600"}, title),
      el("div",{class:"muted",style:"font-size:12px;margin-top:2px"}, sub)),
    shown ? el("span",{class:"chip ok"}, t("live.shown"))
          : el("button",{class:"btn", onclick: go}, t("live.show"))));
  if (d.cron) card(t("live.cron", {name: d.cron.name}),
    t("live.cron_sub", {now: d.cron.now || "—", done: d.cron.done, total: d.cron.total}),
    isShown("cron"), () => followCron());
  (d.routes || []).forEach(r => card(
    t("live.route", {name: r.name, title: r.title}) + (r.trigger === "cron" ? " · cron" : ""),
    r.bar && r.bar.cmd ? t("live.route_sub", {cmd: r.bar.cmd, mins: mins(r.started)})
                       : t("live.started", {mins: mins(r.started)}),
    isShown("route", r.id), () => attachRoute(r.id, {title: r.title, id: r.scId}, r.write, false)));
  (d.jobs || []).forEach(j => card(
    t("live.job", {name: j.name, cmd: (j.cmd + " " + (j.args||[]).join(" ")).trim()}),
    t("live.details", {mins: mins(j.started), lines: j.lines}),
    isShown("job", j.id), () => attachJob(j.id)));
  if ((d.routes || []).length || (d.jobs || []).length)
    box.append(el("div",{class:"muted",style:"font-size:12.5px;margin-bottom:10px"},
      t("live.route_warning")));
  return d;
}
// Консоль открыли, а своего вывода в ней нет — показываем то, что идёт: работу
// выбранного проекта, иначе цепочку расписания, иначе любую.
async function autoFollow(){
  if (SHOWN) return;
  const d = await liveAll();
  if (SHOWN || S.view !== "console") return;
  const mine = x => S.project && x.project === S.project.path;
  const r = (d.routes || []).find(mine), j = (d.jobs || []).find(mine);
  if (r) return attachRoute(r.id, {title: r.title, id: r.scId}, r.write, false);
  if (j) return attachJob(j.id);
  if (d.cron) return followCron();
  if ((d.routes || [])[0]){ const x = d.routes[0];
    return attachRoute(x.id, {title: x.title, id: x.scId}, x.write, false); }
  if ((d.jobs || [])[0]) return attachJob(d.jobs[0].id);
}
// Поток цепочки расписания: заголовок каждого пункта, вывод его шагов, ожидания, итоги —
// по всем проектам подряд, пока цепочка идёт.
/* ---------------- пульс шага ---------------- */
// Ответ модели приходит целиком, и шаг молчит минутами: «выполняется» без изменений не
// отличало «ждёт модель» от «завис» (PRJ-C 09.10.2026: шлюз отдавал 4 токена в секунду, и
// двадцать минут в Консоли не менялось ни слова). Пульс приходит с каждым опросом задания,
// маршрута и цепочки (`job_pulse` на сервере): жив ли процесс, когда была последняя строка,
// какие запросы к моделям идут и сколько им осталось до срока. Колесо крутится, пока шаг
// жив, а время проверки меняется на каждом опросе — это видно и без анимации.
const PULSE_TONE = {ok: "ok", wait: "ok", late: "warn", quiet: "warn", gone: "bad"};
const PULSE_HEAD = {ok: q => t("pulse.ok", {quiet: q}), wait: q => t("pulse.wait", {quiet: q}),
                    late: q => t("pulse.late", {quiet: q}), quiet: q => t("pulse.quiet", {quiet: q}),
                    gone: () => t("pulse.gone")};
function pulseParts(p){
  const parts = [(PULSE_HEAD[p.state] || PULSE_HEAD.quiet)(howLong(p.quiet)),
                 t("pulse.running", {t: howLong(p.running)})];
  if (p.calls) parts.push(t("pulse.calls", {n: p.calls, oldest: howLong(p.oldest || 0),
    limit: p.limit ? howLong(p.limit) : "—", where: p.where || "—"}));
  if (p.rings) parts.push(t("pulse.rings", {n: p.rings}));
  if (p.answer_ago != null) parts.push(t("pulse.answer_ago", {t: howLong(p.answer_ago)}));
  else if (p.calls) parts.push(t("pulse.no_answer"));
  if (p.answered || p.failed) parts.push(t("pulse.counts", {ok: p.answered, bad: p.failed}));
  if (p.state === "late") parts.push(t("pulse.late_hint"));
  if (p.state === "quiet") parts.push(p.watchdog ? t("pulse.watchdog", {t: howLong(p.watchdog_in || 0)})
                                                 : t("pulse.stop_hint"));
  return parts;
}
function drawPulse(p, me){
  if (me != null && me !== FOLLOW) return;     // Консоль уже показывает другое
  const box = $("#consolePulse");
  if (!box) return;
  if (!p){ box.hidden = true; box.replaceChildren(); return; }
  const [head, ...rest] = pulseParts(p);
  const time = new Date().toLocaleTimeString(S.lang === "en" ? "en-GB" : "ru-RU");
  box.className = "pulse " + (PULSE_TONE[p.state] || "warn");
  box.title = t("pulse.title");
  box.hidden = false;
  box.replaceChildren(el("span", {class: "spin", "aria-hidden": "true"}), el("b", {}, head),
    ...rest.map(s => el("span", {}, s)),
    el("span", {class: "pulse-at"}, t("pulse.checked", {time})));
}

async function followCron(){
  const me = ++FOLLOW;
  SHOWN = {kind: "cron"};
  if (S.view !== "console") show("console");
  const out = $("#consoleOut"); out.innerHTML = "";
  $("#consoleApply").innerHTML = ""; LAST_TASKS = []; drawTaskButton(); armStop(null);
  drawLiveJobs();
  let since = 0, stream = "", last = null;
  for (;;){
    const d = await api(`/api/cron/live?since=${since}`, {quiet:true});
    if (me !== FOLLOW) return;
    if (!d || d.error){ out.append(el("div",{class:"err"}, t("live.cron_lost"))); break; }
    if (stream && d.stream && d.stream !== stream){ out.innerHTML = ""; since = 0; stream = d.stream; continue; }
    stream = d.stream || stream;
    if (d.cut) out.append(el("div",{class:"dim"}, t("live.cron_cut")));
    (d.lines || []).forEach(l => consoleLine(out, l));
    since = d.next;
    watchScroll(out); stickToBottom(out);
    drawPulse(d.run ? d.pulse : null, me);
    const run = d.run;
    if (!run){
      // Цепочка кончилась: шапка — её имя и итог, а не последний «пройдено N из M» хода.
      $("#consoleRc").textContent = stream ? t("live.cron_ended") : t("live.cron_none");
      $("#consoleRc").className = "chip" + (stream ? " ok" : "");
      $("#consoleCmd").textContent = last ? t("live.cron_head_end", {name: last}) : "cron";
      break;
    }
    last = run.name;
    const items = run.items || [];
    const done = items.filter(i => !["pending", "running"].includes(i.status)).length;
    $("#consoleCmd").textContent = t("live.cron_head", {name: run.name, done, total: items.length});
    $("#consoleRc").textContent = t("route.running"); $("#consoleRc").className = "chip";
    await new Promise(ok => setTimeout(ok, 1000));
    if (me !== FOLLOW) return;
  }
  if (me === FOLLOW){ SHOWN = null; drawPulse(null); }
  drawLiveJobs();
}

async function attachJob(id){
  const me = ++FOLLOW;
  SHOWN = {kind: "job", id};
  if (S.view !== "console") show("console");
  const out = $("#consoleOut"); out.innerHTML = "";
  $("#consoleCmd").textContent = t("job.attached", {id});
  drawLiveJobs();
  let since = 0;
  for (;;){
    const d = await api(`/api/job?id=${id}&since=${since}`, {quiet:true});
    if (me !== FOLLOW) return;
    if (!d || d.error) { $("#consoleRc").textContent = t("job.not_found"); SHOWN = null;
                         drawPulse(null); return; }
    (d.lines||[]).forEach(l=>consoleLine(out, l));
    watchScroll(out); stickToBottom(out); since = d.next;
    drawPulse(d.done ? null : d.pulse, me);
    $("#consoleCmd").textContent = d.cmd + " " + (d.args||[]).join(" ");
    if (d.done){
      $("#consoleRc").textContent = t("run.rc", {rc: d.rc});
      $("#consoleRc").className = "chip " + (failed(d.rc) ? "bad" : d.rc === 1 ? "warn" : "ok");
      SHOWN = null;
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
  "нарушения шаблона у карточек со штампом": "word.template_violation",  // данные движка
  "карточки без шаблона после завершения миграции": "word.missing_template",  // данные движка
  "принятые решения правили": "word.decision_changed",  // данные движка
  "имена, занятые под записи": "word.reserved_names",  // данные движка
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
  const payload = {
    project: S.project.path, scId: sc.id, write,
    resume: resume ? {skipSigs: [...(resume.skipSigs || [])], attempts: resume.attempts || 0,
                      cycleAt: resume.cycleAt || null} : null};
  let res = await api("/api/route/run", {method:"POST", quiet:true, body: JSON.stringify(payload)});
  if (res && res.net_blocked){
    // Не хватает связи: человек выбирает — только доступное, очередь или отмена.
    const choice = await askNet(res.net_blocked);
    if (!choice) return toast(t("net.cancelled"), "warn");
    res = await api("/api/route/run", {method:"POST", quiet:true, body: JSON.stringify({...payload, net: choice})});
  }
  if (!res || !res.route || res.error){
    // Уже идёт маршрут (кнопка в другой вкладке, расписание) — подключаемся к нему, а не
    // заводим второй поверх.
    if (res && res.route){
      toast(res.error || "", "warn");
      const d = S.scenarios || (S.scenarios = await api("/api/scenarios?all=1"));
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
  const me = ++FOLLOW;
  SHOWN = {kind: "route", id};
  if (S.view !== "console") show("console");
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
  drawLiveJobs();
  for (;;){
    const d = await api(`/api/route/live?id=${encodeURIComponent(id)}&since=${since}`, {quiet:true});
    if (me !== FOLLOW) return leaveRoute(id);   // Консоль переключили на другое — поток молчит
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
    drawPulse(d.done ? null : d.pulse, me);
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
        el("div",{style:"flex:1;min-width:220px", html: w.net
          ? t("route.wait_net_line", {cmd: esc(w.cmd), why: esc(w.net)})
          : t("route.wait_line", {cmd: esc(w.cmd), n: w.attempt, time: when})}),
        el("button",{class:"btn sm gold", onclick: () => api("/api/route/wake",
          {method:"POST", quiet:true, body: JSON.stringify({id})})}, t("route.wait_now")),
        el("button",{class:"btn sm", onclick: () => api("/api/route/stop",
          {method:"POST", quiet:true, body: JSON.stringify({id})})}, t("route.wait_stop")));
      out.append(holder);
    } else if (!w.cmd && waitShown){ waitShown = ""; holder.hidden = true; }
    if (d.done){ finishRoute(d.result || {}, sc, write); break; }
    await new Promise(ok=>setTimeout(ok, 600));
    if (me !== FOLLOW) return leaveRoute(id);
  }
  stop.hidden = true; stop.onclick = null;
  ROUTE = null;
  SHOWN = null;
  drawRouteBar(null);
}

// Консоль ушла с маршрута на другой поток: маршрут идёт дальше на сервере, а «текущим» в
// Консоли он больше не считается — иначе «Продолжить маршрут» и полоса хода думали бы, что
// он на экране.
function leaveRoute(id){
  if (ROUTE && ROUTE.id === id){ ROUTE = null; drawRouteBar(null); }
}

/* Конец маршрута: шапка консоли, кнопки «Починить» по находкам, «Продолжить» и здоровье.
   Строки итога уже в журнале — их написал сервер, страница их только показала. */
function finishRoute(r, sc, write){
  const out = $("#consoleOut");
  const bad = r.reason !== "passed";
  // Пройден, но часть шагов осталась без связи (пропущена или отдана расписанию): маршрут частичный, его можно продолжить.
  const part = !bad && !!(r.partial || (r.net_skipped || []).length || (r.net_deferred || []).length);
  if (r.reason === "stall"){
    $("#consoleCmd").textContent = t("route.head_stalled", {title: sc.title});
    $("#consoleRc").textContent = t("route.rc_stalled");
    $("#consoleRc").className = "chip warn";
  } else {
    $("#consoleCmd").textContent = bad ? t("route.head_stopped", {title: sc.title})
                                       : t("route.head_passed", {title: sc.title});
    $("#consoleRc").textContent = bad ? t("route.rc_stopped", {cmd: r.failed || "—"})
                                      : part ? t("route.rc_partial", {n: r.steps || 0})
                                      : t("route.rc_passed", {n: r.steps || 0});
    $("#consoleRc").className = "chip " + (bad ? "bad" : part ? "warn" : "ok");
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
  if ((bad || part) && S.lastRoute && S.project){
    dropResumeButtons();
    api("/api/route/state?project="+encodeURIComponent(S.project.path), {quiet:true})
      .then(d=>{ if (d && d.state) $("#consoleApply").append(el("button",{class:"btn sm gold resume-route",
        onclick:()=>resumeLastRoute(d.state)},
        t("route.resume_btn", {title: sc.title, slug: S.project.slug}))); });
  }
  if (S.project){ const p = S.project;
    loadHealth(p, HEALTH_ALL, true).then(()=>{
      if (S.project === p){ loadRuns(); refreshModules(); }});
    if (write) api("/api/state").then(st=>{
      const me = (st.projects||[]).find(x=>x.path===p.path);
      if (me && me.dirty) out.append(el("div",{class:"warn",style:"margin-top:8px"},
        t("route.dirty", {n: me.dirty})));
    });
  }
  drawLiveJobs();              // задания маршрута кончились — карточка «идёт сейчас» не висит
  toast(r.reason === "stall" ? t("route.toast_stall")
    : r.reason === "gated" ? t("route.toast_gated", {cmd: r.failed || "—"})
    : bad ? t("route.toast_stopped", {cmd: r.failed || "—"})
    : part ? t("route.toast_partial", {title: sc.title})
    : t("route.toast_passed", {title: sc.title}), bad ? "err" : part ? "warn" : "ok");
}

/* Кнопка «Продолжить маршрут». Сделанное знает сервер: оно в состоянии маршрута, которое он
   пишет после каждого шага, — поэтому продолжение переживает и закрытую вкладку, и
   перезапуск панели. Пропускаются только шаги, завершившиеся успехом (rc 0): rc 1 —
   «отработала и нашла, что чинить», её повторять надо. */
async function resumeLastRoute(last){
  const d = S.scenarios || (S.scenarios = await api("/api/scenarios?all=1"));
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
  const d = S.scenarios || (S.scenarios = await api("/api/scenarios?all=1"));
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
    : state.reason === "gated" ? t("act.reason_gated")
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
async function poll(id, since, label, me){
  // Новый прогон забирает Консоль себе; прежний поток (другое задание, цепочка) замолкает.
  if (me == null){ me = ++FOLLOW; SHOWN = {kind: "job", id}; }
  const d = await api(`/api/job?id=${id}&since=${since}`, {quiet:true});
  if (me !== FOLLOW) return;
  const out = $("#consoleOut");
  // Кнопка «Прервать» живёт ровно столько, сколько идёт прогон. Без неё запрет на
  // перезапуск панели при работающем задании превращается в тупик: ни остановить,
  // ни перезапустить — только ждать часами.
  armStop((d && !d.done && !d.error) ? id : null);
  drawPulse((d && !d.done && !d.error) ? d.pulse : null, me);
  // Задания нет: панель перезапускали, пока команда шла. Раньше цикл продолжал спрашивать
  // про исчезнувшее задание и «выполняется…» стояло вечно — команда при этом могла давно
  // отработать. Тишина здесь дороже ошибки: человек ждёт того, чего уже не случится.
  if (!d || d.error){
    out.append(el("div",{class:"err"}, t("poll.job_lost")));
    $("#consoleRc").textContent = t("poll.job_lost_short");
    $("#consoleRc").className = "chip bad";
    drawLiveJobs();
    // Панель потеряла задание, но процесс движка мог пережить перезапуск и идти дальше
    // сиротой. Его выдаёт замок прогона в проекте — скажем прямо, что работа не встала.
    const pr = S.project;
    if (pr) api("/api/activity", {quiet:true}).then(a => {
      const ag = a && a.projects && a.projects[pr.path] && a.projects[pr.path].agent;
      if (ag && ag.alive) out.append(el("div",{class:"warn"},
        t("poll.job_orphan", {task: ag.task || "?", pid: ag.pid,
          since: ag.since ? new Date(ag.since).toLocaleTimeString(S.lang === "en" ? "en-GB" : "ru-RU",
                                                                {hour: "2-digit", minute: "2-digit"}) : "?"})));
    }).catch(() => {});
    return;
  }
  let outLines = d.lines||[];
  if (!since){ CONSOLE_LINES = []; POLL_LAST_OUT = Date.now(); POLL_SILENT = false; }
  (d.lines||[]).forEach(l=>{
    if (String(l).startsWith(SUMMARY_MARK)) return;   // машинная строка итога — для панели
    if (String(l).startsWith(GATE_MARK)) return;      // машинная строка вердикта ворот — тоже
    if (String(l).startsWith(PUBLISH_MARK)){          // исход публикации: по нему рисуются кнопки «Перезаписать» и др.
      try { PUBLISH_RESULT[id] = JSON.parse(String(l).slice(PUBLISH_MARK.length)); } catch (e) { /* строка оборвана */ }
      return;
    }
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
  if (!d.done) return setTimeout(()=>poll(id, d.next, label, me), 450);
  SHOWN = null;
  const rc = d.rc, mark = rcMark(rc);
  const badge = $("#consoleRc");
  badge.textContent = mark.what + t("poll.rc", {rc});
  badge.className = "chip " + mark.cls;
  toast(rc===0 ? t("poll.done", {cmd: label})
               : t("poll.done_rc", {cmd: mark.what + ": " + label, rc}),
        rc===0 ? "ok" : rc===1 ? "warn" : "err");
  // Публикация переписывает файл (штамп): открытый в «Файлах» документ перечитываем, а по исходу (`AURORA-PUBLISH`)
  // рисуем кнопки: «Перезаписать», «Взять под управление», «Переместить по пути вида». Метка — «ship:publish <путь>».
  if (String(label).startsWith("ship:publish ")){
    const res = PUBLISH_RESULT[id] || null;
    delete PUBLISH_RESULT[id];
    publishDone(String(label).slice("ship:publish ".length), res, rc);
  }
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
    loadHealth(p, HEALTH_ALL, true).then(()=>{
      if (S.project !== p) return;
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
const GATE_MARK = "AURORA-GATE ";       // вердикт ворот качества — тоже машинная строка
const PUBLISH_MARK = "AURORA-PUBLISH "; // исход публикации в Confluence (`ship:publish`) — тоже
const PUBLISH_RESULT = {};              // задание → исход публикации, пока опрос не дошёл до конца
function consoleLine(out, l){
  if (!String(l).startsWith(GATE_MARK) && !String(l).startsWith(SUMMARY_MARK) && !String(l).startsWith(PUBLISH_MARK))
    out.append(el("div",{class:lineClass(l)}, l));
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
const HIST_TONE = {passed:"ok", failed:"bad", gated:"warn", stall:"warn", stopped:"warn", offline:"warn",
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
    S.scenarios || (S.scenarios = await api("/api/scenarios?all=1", {quiet:true})),
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

  // 1. новый проект: локально или из git; поля после перерисовки раздела, как и прежде, пустые
  NEWP.local = {}; NEWP.git = null;
  const roots = await api("/api/roots");
  NEWP.root = (roots.roots||[""])[0] || "";
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

  await drawHiddenGroup(box);

  const newBox = newProjectCard();
  sgroup(box, "setup:new", t("setup.new_title")).append(newBox);

  // Модели — общая настройка машины и всех её проектов: раздел «Модели».
  await renderModelsCard(box, "kit");
  MCPK.kit.box = sgroup(box, "setup:mcp", t("mcpk.title"));
  await renderMcpKit();
  drawSetupJump(box);
}

/* ---------------- новый проект: локально или из git ----------------

   Два пути, и друг с другом они не смешиваются. Локальный — папка с каркасом, git к ней подключают потом
   на вкладке «Git» проекта. Из git — репозиторий уже на сервере: движок клонирует его в новую папку и сразу
   записывает настройку Git проекта (адрес, вход, ветку), так что подключать второй раз нечего. */
const NEWP = {mode: "local", local: {}, git: null, root: "", draw: () => {}};
const GIT_AUTHS = ["password", "token", "ssh", "system"];
const newGit = () => NEWP.git || (NEWP.git = {text: "", parsed: null, busy: "", check: null, failure: null,
  cert: null, lastAction: "", timer: null, branch: "", folder: "", parent: "", repoName: "", name: "", slug: "",
  space: "", jira: "",
  author_name: "", author_email: "",
  branchTouched: false, authTouched: false,
  cred: {auth: "password", user: "", secret: "", ssh_key: ""}});
// Строка вкладки «Git», а нет её (модуль выключен) — текст движка: разделы переводятся своими каталогами.
const tg = (key, fallback, vars) => (I18N[key] !== undefined || RU[key] !== undefined)
  ? t(key, vars) : (fallback || key);

function newProjectCard(){
  const card = el("div",{class:"card",style:"padding:20px"});
  NEWP.draw = () => {
    card.innerHTML = "";
    card.append(
      el("p",{class:"muted",style:"font-size:13px;margin:0 0 12px"}, t("setup.mode_about")),
      el("div",{class:"row",style:"gap:8px;margin-bottom:14px;flex-wrap:wrap",role:"group"},
        ...["local","git"].map(m => el("button",{class:"btn sm" + (NEWP.mode === m ? " primary" : ""),
          "aria-pressed": String(NEWP.mode === m),
          onclick:()=>{ NEWP.mode = m; NEWP.draw(); }}, t("setup.mode_" + m)))),
      NEWP.mode === "git" ? newGitForm() : newLocalForm());
  };
  NEWP.draw();
  return card;
}

function newLocalForm(){
  const nf = NEWP.local;
  const field = (key, label, ph, hint) => el("label",{style:"display:block;margin-bottom:10px"},
    el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"}, label +
      (hint ? " · " + hint : "")),
    watch(el("input",{class:"btn",style:"width:100%;font-weight:400",placeholder:ph,value:nf[key]||"",
      oninput:e=>nf[key]=e.target.value}), "new", t("dirty.new")));
  return el("div",{},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 14px"}, t("setup.new_about")),
    field("path", t("setup.f_path"), "" + NEWP.root + "/my-project"),
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
}

function newGitForm(){
  const g = newGit();
  const url = el("input",{class:"btn mono",style:"width:100%;font-weight:400",autocomplete:"off",
    placeholder:t("setup.g_url_ph"),value:g.text});
  url.addEventListener("input", () => {
    g.text = url.value; g.check = null; g.failure = null; g.cert = null;
    setDirty("new", t("dirty.new"), true);
    clearTimeout(g.timer); g.timer = setTimeout(parseNewGit, 300);
  });
  return el("div",{},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 14px"}, t("setup.git_about")),
    el("label",{style:"display:block;margin-bottom:10px"},
      el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"}, t("setup.g_url")), url),
    el("div",{id:"newGitRest"}, ...newGitRest()));
}

// Перерисовывается только то, что ниже строки адреса: в самой строке не должен пропадать курсор.
function drawNewGit(){
  const box = $("#newGitRest");
  if (box){ box.innerHTML = ""; box.append(...newGitRest()); }
}

async function parseNewGit(){
  const g = newGit();
  if (!g.text.trim()){ g.parsed = null; return drawNewGit(); }
  const r = await api("/api/gitsync/parse",{method:"POST",quiet:true,body:JSON.stringify({text:g.text})});
  g.parsed = r || {ok:false, code:"bad_url"};
  const p = g.parsed;
  if (p.ok){
    g.cred = {...g.cred, auth: g.authTouched ? g.cred.auth : p.auth, user: p.user || g.cred.user,
              secret: p.secret || g.cred.secret};
    if (p.branch && !g.branchTouched) g.branch = p.branch;
    // Имя репозитория — только подсказка: проекты из разных репозиториев нередко называются одинаково, а папку
    // для каждого человек называет сам (1.190.0).
    g.repoName = String(p.repo || p.url || "").replace(/[\\/]+$/, "").replace(/\.git$/i, "")
      .split(/[\\/:]/).pop().replace(/[*?"<>|]/g, "");
  }
  drawNewGit();
}

// Настройка Git проекта из разобранной строки; остальное (имя сервера в git, автор, автоматика) — по умолчанию.
function newGitSettings(g){
  const p = g.parsed;
  return {provider: p.provider, instance: p.instance, repo: p.repo, branch: g.branch.trim(),
          author_name: g.author_name.trim(), author_email: g.author_email.trim(),
          tls: {verify: true, ca_file: ""}};
}

// Итоговый путь клона: папка проектов + название папки (то же собирает сервер — `clone_target`).
function newGitTargetPath(g){
  const parent = (g.parent || NEWP.root || "").trim().replace(/[\\/]+$/, "");
  const folder = (g.folder || "").trim();
  return folder && parent ? parent + "/" + folder : "";
}
function newGitTargetText(g){
  const path = newGitTargetPath(g);
  return path ? t("setup.g_target", {path}) : t("setup.g_target_none");
}
function drawNewGitTarget(){
  const n = $("#newGitTarget");
  if (n) n.textContent = newGitTargetText(newGit());
}

function newGitRest(){
  const g = newGit(), p = g.parsed;
  if (!p) return [];
  if (!p.ok) return [el("div",{style:"font-size:12.5px;color:var(--danger);margin-top:8px"},
    tg("gitsync.q.err." + p.code, p.code))];
  const c = g.cred;
  const touch = () => setDirty("new", t("dirty.new"), true);
  const field = (label, control, hint) => el("label",{style:"display:block;flex:1;min-width:220px;margin-bottom:10px"},
    el("div",{class:"muted",style:"font-size:12px;margin-bottom:3px"}, label), control,
    hint ? el("div",{class:"muted",style:"font-size:11.5px;margin-top:3px"}, hint) : null);
  const text = (key, value, attrs) => {
    const n = el("input",{class:"btn mono",style:"width:100%;font-weight:400",autocomplete:"off",
      value:value||"", ...(attrs||{})});
    n.addEventListener("input", () => { touch(); });
    return n;
  };
  const out = [el("div",{class:"row",style:"gap:6px;flex-wrap:wrap;margin:10px 0"},
    el("span",{class:"chip ok"}, tg("gitsync.prov." + p.provider, p.provider)),
    p.instance ? el("span",{class:"chip mono"}, p.instance) : null,
    el("span",{class:"chip mono"}, p.repo))];
  if (p.secret_in_url) out.push(el("div",{class:"note",style:"margin-bottom:10px"}, t("setup.g_secret_moved")));
  const method = el("select",{class:"btn",style:"width:100%"},
    ...GIT_AUTHS.map(v => el("option",{value:v, selected: c.auth === v ? "" : null}, t("setup.g_auth_" + v))));
  method.onchange = () => { c.auth = method.value; g.authTouched = true; g.check = null; touch(); drawNewGit(); };
  out.push(field(t("setup.g_auth"), method));
  if (c.auth === "password" || c.auth === "token"){
    const user = text("user", c.user);
    user.oninput = () => { c.user = user.value.trim(); };
    const sec = el("input",{class:"btn mono",type:"password",autocomplete:"new-password",
      style:"width:100%;font-weight:400"});
    sec.value = c.secret || "";
    sec.oninput = () => { c.secret = sec.value; touch(); };
    out.push(el("div",{class:"row",style:"gap:12px;flex-wrap:wrap"},
      field(t("setup.g_user"), user),
      field(c.auth === "token" ? t("setup.g_token") : t("setup.g_password"), sec)));
  } else if (c.auth === "ssh"){
    const key = text("ssh_key", c.ssh_key, {placeholder:"~/.ssh/id_ed25519"});
    key.oninput = () => { c.ssh_key = key.value.trim(); };
    out.push(field(t("setup.g_ssh_key"), key));
  }
  const branch = text("branch", g.branch, {list:"newGitBranches"});
  branch.oninput = () => { g.branch = branch.value; g.branchTouched = true; };
  const folder = text("folder", g.folder, {placeholder: "Northwind"});
  folder.oninput = () => { g.folder = folder.value; touch(); drawNewGitTarget(); };
  const parent = text("parent", g.parent || NEWP.root, {placeholder: NEWP.root});
  parent.oninput = () => { g.parent = parent.value; touch(); drawNewGitTarget(); };
  const heads = ((g.check && g.check.steps || []).find(x => x.id === "git") || {}).branches || [];
  out.push(el("div",{class:"row",style:"gap:12px;flex-wrap:wrap"},
    field(t("setup.g_branch"), branch, t("setup.g_branch_hint")),
    el("datalist",{id:"newGitBranches"}, ...heads.map(b => el("option",{value:b})))));
  // Куда клонировать: название папки задаёт человек (у проектов из разных репозиториев имя репозитория бывает одним),
  // папка проектов — где они лежат. Итоговый путь виден сразу.
  out.push(el("div",{class:"row",style:"gap:12px;flex-wrap:wrap"},
    field(t("setup.g_folder"), folder, t("setup.g_folder_hint", {repo: g.repoName || "—"})),
    field(t("setup.g_parent"), parent, t("setup.g_parent_hint"))));
  out.push(el("div",{id:"newGitTarget",class:"muted",style:"font-size:12.5px;margin:-4px 0 10px"}, newGitTargetText(g)));
  const who = (key, label) => {
    const n = text(key, g[key]);
    n.oninput = () => { g[key] = n.value; };
    return field(label, n);
  };
  out.push(el("div",{class:"row",style:"gap:12px;flex-wrap:wrap"},
    who("author_name", tg("gitsync.field.author_name", "author_name")),
    who("author_email", tg("gitsync.field.author_email", "author_email"))));
  const extra = (key, label, ph) => {
    const n = text(key, g[key], {placeholder:ph, style:"width:100%;font-weight:400"});
    n.oninput = () => { g[key] = n.value; };
    return field(label, n);
  };
  out.push(el("details",{style:"margin:2px 0 12px"},
    el("summary",{class:"muted",style:"cursor:pointer;font-size:12.5px"}, t("setup.g_if_empty")),
    el("div",{class:"muted",style:"font-size:11.5px;margin:6px 0"}, t("setup.g_if_empty_hint")),
    el("div",{class:"row",style:"gap:12px;flex-wrap:wrap"},
      extra("name", t("setup.f_name"), "Northwind"), extra("slug", t("setup.f_slug"), "Northwind")),
    el("div",{class:"row",style:"gap:12px;flex-wrap:wrap"},
      extra("space", t("setup.f_space"), "NW"), extra("jira", t("setup.f_jira"), "NW"))));
  out.push(el("div",{class:"row",style:"gap:8px;flex-wrap:wrap;align-items:center"},
    el("button",{class:"btn",disabled: g.busy ? "" : null, onclick: checkNewGit},
      g.busy === "check" ? t("setup.g_checking") : t("setup.g_check")),
    el("button",{class:"btn primary" + (DIRTY.has("new") ? " unsaved" : ""),"data-save":"new",
      disabled: g.busy ? "" : null, onclick: createNewGit},
      g.busy === "create" ? t("setup.g_creating") : t("setup.g_create")),
    g.busy ? el("span",{class:"spin"}) : null));
  if (g.check) out.push(newGitCheck(g.check));
  if (g.failure) out.push(newGitFailure(g.failure));
  if (g.cert) out.push(newGitCert(g));
  return out;
}

function newGitCheck(r){
  return el("div",{style:"margin-top:10px"},
    el("b",{}, r.ok ? t("setup.g_check_ok") : t("setup.g_check_bad")),
    ...(r.steps || []).map(x => el("div",{class:"row",style:"gap:8px;margin-top:6px;font-size:12.5px;flex-wrap:wrap"},
      el("span",{class:"chip " + (x.ok ? "ok" : x.ok === null ? "" : "bad")}, x.ok ? "✓" : x.ok === null ? "·" : "✗"),
      el("span",{}, tg("gitsync.step." + x.id, x.id, {login: x.login || "", branch: x.detail || ""})))),
    (r.suggest || {}).empty ? el("div",{class:"note",style:"margin-top:8px"}, t("setup.g_repo_empty")) : null,
    r.problem ? newGitProblem(r.problem) : null);
}

// Отказ git — причиной и советом, как на вкладке Git; сертификат собственного сервера — кнопкой «Доверять…».
function newGitProblem(pr){
  const raw = pr.raw ? el("details",{style:"margin-top:8px"},
    el("summary",{class:"muted",style:"cursor:pointer;font-size:12px"}, tg("gitsync.raw", "")),
    el("pre",{class:"mono",style:"font-size:11.5px;white-space:pre-wrap;margin:6px 0 0"}, pr.raw)) : null;
  return el("div",{class:"warnbox dangerbox"},
    el("b",{}, tg("gitsync.p." + pr.code, pr.title)),
    el("div",{class:"muted",style:"font-size:12.5px;margin-top:3px"}, tg("gitsync.fix." + pr.code, pr.fix)),
    pr.code === "cert" ? el("div",{class:"row",style:"gap:8px;margin-top:8px"},
      el("button",{class:"btn sm primary", onclick: certNewShow}, t("setup.g_trust"))) : null,
    raw);
}

function newGitFailure(f){
  if (f.problem) return newGitProblem(f.problem);
  const lines = (f.problems || []).map(x => el("div",{},
    "· " + tg("gitsync.field." + x.field, x.field || "") + (x.field ? ": " : "") + tg("gitsync.f." + x.code, x.code)));
  return el("div",{class:"warnbox dangerbox"},
    f.error ? el("div",{}, f.error) : null, ...lines,
    f.kept ? el("div",{class:"muted",style:"font-size:12.5px;margin-top:4px"}, t("setup.g_kept")) : null,
    !f.error && !lines.length ? el("div",{}, t("setup.g_failed")) : null);
}

function newGitCert(g){
  const c = g.cert;
  if (c.busy) return el("div",{class:"row",style:"gap:8px;margin-top:8px"}, el("span",{class:"spin"}));
  if (!c.ok) return el("div",{class:"warnbox"}, c.error || t("setup.g_cert_failed"));
  return el("div",{class:"warnbox"},
    el("b",{}, t("setup.g_cert_title", {host: c.host + ":" + c.port})),
    el("div",{class:"muted",style:"font-size:12.5px;margin:4px 0"}, t("setup.g_cert_about")),
    el("div",{class:"mono",style:"font-size:12px;word-break:break-all"}, "SHA-256 " + c.sha256),
    el("div",{class:"row",style:"gap:8px;margin-top:8px"},
      el("button",{class:"btn sm primary", onclick: certNewTrust}, t("setup.g_cert_trust")),
      el("button",{class:"btn sm", onclick: () => { g.cert = null; drawNewGit(); }}, t("common.cancel"))));
}

async function certNewShow(){
  const g = newGit();
  g.cert = {busy: true}; drawNewGit();
  const r = await api("/api/project/git/cert",{method:"POST",quiet:true,
    body:JSON.stringify({action:"show", url:g.parsed.url})});
  g.cert = r || {ok:false}; drawNewGit();
}

// Доверие запоминается на машине; то, что просило довериться (проверка или создание), повторяется само.
async function certNewTrust(){
  const g = newGit();
  const r = await api("/api/project/git/cert",{method:"POST",quiet:true,
    body:JSON.stringify({action:"trust", sha256:g.cert.sha256, url:g.parsed.url})});
  g.cert = null;
  if (!r || !r.ok){
    toast((r && r.error) || t("setup.g_cert_failed"), "err");
    return drawNewGit();
  }
  toast(t("setup.g_cert_trusted", {host: r.host + ":" + r.port}), "ok");
  g.failure = null; g.check = null;
  return g.lastAction === "create" ? createNewGit() : checkNewGit();
}

async function checkNewGit(){
  const g = newGit();
  if (g.busy) return;
  if (!g.parsed || !g.parsed.ok){ g.failure = {error: t("setup.g_need_url")}; return drawNewGit(); }
  g.busy = "check"; g.check = null; g.failure = null; g.lastAction = "check"; drawNewGit();
  const r = await api("/api/project/git/check",{method:"POST",quiet:true,
    body:JSON.stringify({settings:newGitSettings(g), credentials:g.cred})});
  g.busy = ""; g.check = r || {ok:false, steps:[]};
  drawNewGit();
}

async function createNewGit(){
  const g = newGit();
  if (g.busy) return;
  if (!g.parsed || !g.parsed.ok){ g.failure = {error: t("setup.g_need_url")}; return drawNewGit(); }
  if (!g.folder.trim()){ g.failure = {error: t("setup.g_need_folder")}; return drawNewGit(); }
  g.busy = "create"; g.failure = null; g.lastAction = "create"; drawNewGit();
  const r = await api("/api/project/new",{method:"POST",quiet:true,body:JSON.stringify({
    folder: g.folder.trim(), parent: (g.parent || NEWP.root || "").trim(),
    name: g.name.trim(), slug: g.slug.trim(), space: g.space.trim(), jira: g.jira.trim(),
    git: {settings: newGitSettings(g), credentials: g.cred}})});
  g.busy = "";
  if (!r || !r.ok){ g.failure = r || {error: t("setup.g_failed")}; return drawNewGit(); }
  toast(t(r.mode === "deployed" ? "setup.g_done_deployed" : "setup.g_done_existing", {path: r.path})
        + (r.added_root ? t("setup.deployed_root", {root: r.added_root}) : ""));
  if (r.engine && !r.engine.ok && r.engine.error) toast(t("setup.g_engine_warn", {error: r.engine.error}), "warn");
  setDirty("new", t("dirty.new"), false);
  NEWP.git = null;
  S.state = await api("/api/state");
  renderOverview(); renderSetup();
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
// `id` не зависит от языка и проекта — «project:yaml», «setup:agent».
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
   (`env`, `headers`, `auth`, значение секретного флага в `args`) сервер отдаёт маской; маска,
   пришедшая обратно, значит «не менять». Своей копии маски у страницы нет — её называет
   сервер (`mask`). Проверка серверов (`/api/mcp/probe`) запускает серверы кита из папки панели (прогон — из папки проекта). */
const MCPK = {kit: {state: null, box: null, probe: null}, touched: false};

function mcpHow(spec){
  const tr = String(spec.transport || spec.type || "").toLowerCase();
  if (typeof spec.url === "string") return tr === "sse" ? "sse" : "http";
  return "stdio";
}

const mcpGet = () => api("/api/mcp/kit");
const mcpPost = body => api("/api/mcp/kit", {method:"POST", quiet:true, body: JSON.stringify(body)});

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

async function renderMcpKit(){
  const M = MCPK.kit, box = M.box, probe = M.probe;
  if (!box) return;
  const d = await mcpGet();
  M.state = d;
  box.innerHTML = "";
  const names = Object.keys(d.servers || {});
  const cards = el("div", {class:"grid tiles"});
  names.forEach(n => cards.append(mcpCard(n, d.servers[n], d.mask, probe && probe[n])));
  const good = probe ? names.filter(n => probe[n] && probe[n].ok).length : 0;
  box.append(el("div", {class:"card", style:"padding:20px"},
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 12px"}, t("mcpk.about")),
    d.error ? el("div", {class:"warnbox"}, d.error) : null,
    el("div", {class:"row", style:"gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px"},
      el("button", {class:"btn sm primary", onclick:()=>openMcpCard("")}, t("mcpk.add")),
      el("button", {class:"btn sm", onclick:()=>openMcpPaste()}, t("mcpk.paste")),
      el("button", {class:"btn sm", onclick:()=>openMcpRaw()}, t("mcpk.raw")),
      names.length ? el("button", {class:"btn sm", onclick: async e => {
        e.target.disabled = true;
        e.target.textContent = t("mcp.probing");
        const r = await api("/api/mcp/probe", {method:"POST", body:"{}", quiet:true});
        if (r.error){
          e.target.disabled = false;
          e.target.textContent = t("mcp.probe");
          return toast(r.error, "warn");
        }
        M.probe = r.servers || {};
        renderMcpKit();
      }}, t("mcp.probe")) : null,
      el("span", {class:"muted mono", style:"font-size:11.5px;overflow-wrap:anywhere"},
        d.path || "")),
    names.length ? el("div", {class:"muted", style:"font-size:12px;margin:0 0 10px"},
      probe ? t("mcp.probe_done", {ok: good, n: names.length}) : t("mcp.probe_hint")) : null,
    // Право машины: запись в Jira разрешает только кит.
    el("label", {class:"row", style:"gap:8px;font-size:13px;margin-bottom:4px;align-items:center"},
      el("input", {type:"checkbox", checked: (d.permissions || {}).jira_write ? "" : null,
        onchange: async e => {
          const r = await mcpPost({action:"set_permission", name:"jira_write",
                                   value: e.target.checked});
          if (r.error){ e.target.checked = !e.target.checked; return toast(r.error, "warn"); }
          toast(t("mcpk.perm_saved"), "ok");
        }}), t("mcpk.perm_jira")),
    el("div", {class:"muted", style:"font-size:12px;margin:0 0 14px"}, t("mcpk.perm_jira_about")),
    names.length ? cards
                 : el("div", {class:"muted", style:"font-size:13px"}, t("mcpk.empty"))));
  if (d.aurora && d.aurora.opencode) box.append(auroraSearchCard(d.aurora));
}

// Итог проверки на карточке: работает и сколько инструментов — или почему не поднялся.
function mcpProbeChip(p){
  if (!p) return null;
  return p.ok
    ? el("span", {class:"chip ok", title: (p.names || []).join(", ")},
        t("mcp.works", {n: p.tools || 0}))
    : el("span", {class:"chip warn", title: p.error || ""}, t("mcp.fails"));
}

function mcpCard(name, spec, mask, probe){
  const how = mcpHow(spec);
  const line = how === "stdio" ? [spec.command, ...(spec.args || [])].join(" ") : spec.url;
  const secrets = Object.keys(spec.env || {}).length + Object.keys(spec.headers || {}).length
                + (spec.auth === mask ? 1 : 0);
  const about = spec.about || spec.description || "";
  return el("button", {class:"card mcp-card",
      title: t("mcpk.open"),
      "aria-label": name + " · " + t("mcpk.open"),
      onclick:()=>openMcpCard(name)},
    el("div", {class:"row", style:"gap:8px;align-items:center;flex-wrap:wrap"},
      el("b", {class:"mono"}, name), el("span", {class:"chip"}, t("mcpk.how." + how)),
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

// Окно сервера машины в «Настройке кита».
function openMcpCard(name){
  const d = MCPK.kit.state || {}, mask = d.mask;
  const orig = name ? JSON.parse(JSON.stringify((d.servers || {})[name] || {})) : {};
  const drawer = mcpOpen(false);
  let how = mcpHow(orig);
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
  const stdioBox = el("div", {},
    mcpField(t("mcpk.f_command"), cmdIn), mcpField(t("mcpk.f_args"), argsIn),
    mcpField(t("mcpk.f_env"), env.box));
  const urlBox = el("div", {}, mcpField(t("mcpk.f_url"), urlIn),
    mcpField(t("mcpk.f_headers"), headers.box));
  const showHow = () => { stdioBox.hidden = how !== "stdio"; urlBox.hidden = how === "stdio"; };
  const howSel = el("select", {class:"btn", style:"font-weight:400",
      onchange: e => { how = e.target.value; showHow(); }},
    ...["stdio", "http", "sse"].map(v => el("option", {value: v, selected: v === how ? "" : null},
      t("mcpk.how." + v))));
  // Поля, которых в форме нет (timeout, cwd, auth…), переживают запись: карточка правит
  // только своё. Называем их, чтобы человек не удивлялся, откуда они в файле.
  const MANAGED = ["command", "args", "env", "url", "headers", "transport", "type", "roles",
                   "outbound", "about", "description"];
  const extra = Object.keys(orig).filter(k => !MANAGED.includes(k));
  const errBox = el("div", {});
  const done = () => { closeMcp(true); MCPK.kit.probe = null; renderMcpKit(); };
  const save = async () => {
    errBox.innerHTML = "";
    const newName = nameIn.value.trim();
    if (!newName) return errBox.append(el("div", {class:"warnbox"}, t("mcpk.need_name")));
    const spec = JSON.parse(JSON.stringify(orig));
    try {
      for (const k of ["command", "args", "url", "transport", "type"]) delete spec[k];
      delete spec.env; delete spec.headers;
      if (how === "stdio"){
        spec.command = cmdIn.value.trim();
        const args = argsIn.value.split("\n").map(a => a.trim()).filter(Boolean);
        if (args.length) spec.args = args;
        const e = env.value(); if (Object.keys(e).length) spec.env = e;
        delete spec.headers;
        delete spec.auth;
      } else {
        spec.url = urlIn.value.trim();
        spec.transport = how;
        const h = headers.value(); if (Object.keys(h).length) spec.headers = h;
        delete spec.env;
        delete spec.cwd;
      }
    } catch (e){ return errBox.append(el("div", {class:"warnbox"}, e.message)); }
    if (roles.size) spec.roles = [...roles]; else delete spec.roles;
    if (outIn.checked) spec.outbound = true; else delete spec.outbound;
    delete spec.description;
    if (aboutIn.value.trim()) spec.about = aboutIn.value.trim(); else delete spec.about;
    const r = await mcpPost({action:"save_server", name: newName, rename_from: name, spec});
    if (r.error) return errBox.append(el("div", {class:"warnbox"}, r.error));
    toast(r.unchanged ? t("mcp.unchanged") : t("mcpk.saved", {n: r.count}), "ok");
    done();
  };
  const title = name ? t("mcpk.edit_title", {name}) : t("mcpk.new_title");
  // Родной `append` печатает пустое место словом «null» — пустые части отбрасываем.
  drawer.append(...[
    el("h2", {style:"margin-top:0"}, title),
    mcpField(t("mcpk.f_name"), nameIn, t("mcpk.f_name_hint")),
    mcpField(t("mcpk.f_how"), howSel), stdioBox, urlBox,
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
    el("div", {class:"row", style:"gap:8px;margin-top:8px;flex-wrap:wrap"},
      el("button", {class:"btn primary", onclick: save}, t("mcpk.save")),
      name ? el("button", {class:"btn sm danger", onclick: async () => {
        if (!confirm(t("mcpk.delete_ask", {name}))) return;
        const r = await mcpPost({action:"delete_server", name});
        if (r.error) return errBox.append(el("div", {class:"warnbox"}, r.error));
        toast(t("mcpk.deleted", {name}), "ok");
        done();
      }}, t("mcpk.delete")) : null,
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp()}, t("mcpk.close")))].filter(Boolean));
  showHow();
}

function openMcpPaste(){
  const drawer = mcpOpen(true);
  const text = el("textarea", {class:"mcp-text", spellcheck:"false", autocomplete:"off",
    style:"min-height:30vh", placeholder: t("mcpk.paste_ph")});
  const out = el("div", {});
  const send = dry => mcpPost({action:"import", text: text.value, dry});
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
      MCPK.kit.probe = null;
      renderMcpKit();
    }}, t("mcpk.import")));
  };
  drawer.append(el("h2", {style:"margin-top:0"}, t("mcpk.paste_title")),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 10px"},
      t("mcpk.paste_about")),
    text, out,
    el("div", {class:"row", style:"gap:8px;margin-top:12px"},
      el("button", {class:"btn", onclick: parse}, t("mcpk.parse")),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp()}, t("mcpk.close"))));
  text.focus();
}

function openMcpRaw(){
  const d = MCPK.kit.state || {};
  const drawer = mcpOpen(true);
  const text = el("textarea", {class:"mcp-text", spellcheck:"false", autocomplete:"off"},
    d.text || "{\n  \"mcpServers\": {}\n}\n");
  const errBox = el("div", {});
  drawer.append(el("h2", {style:"margin-top:0"}, t("mcpk.raw_title")),
    el("p", {class:"muted", style:"font-size:13px;margin:0 0 6px"},
      t("mcpk.raw_about")),
    el("div", {class:"muted mono", style:"font-size:11.5px;margin-bottom:10px;overflow-wrap:anywhere"},
      d.path || ""),
    text, errBox,
    el("div", {class:"row", style:"gap:8px;margin-top:12px"},
      el("button", {class:"btn primary", onclick: async () => {
        errBox.innerHTML = "";
        const r = await mcpPost({action:"save_raw", text: text.value});
        if (r.error) return errBox.append(el("div", {class:"warnbox"}, r.error));
        toast(t("mcpk.raw_saved", {n: r.count}), "ok");
        closeMcp(true);
        MCPK.kit.probe = null;
        renderMcpKit();
      }}, t("mcpk.raw_save")),
      el("div", {class:"spacer"}),
      el("button", {class:"btn sm", onclick:()=>closeMcp()}, t("mcpk.close"))));
}

/* Вложения Confluence в форме проекта: режим и предел размера файла. Значения читает сервер
   (`confluence_attachments`, `confluence_attachment_max_mb` в `values`), проверяет тоже он. */
const CONF_ATTACH_MODES = ["all", "images", "none"];
function confAttachmentsRead(get){
  // неизвестное слово в конфиге форма показывает как умолчание: движок читает его так же (all)
  const mode = get("confluence_attachments").toLowerCase();
  return {conf_attachments: CONF_ATTACH_MODES.includes(mode) ? mode : "all",
          conf_attachment_max_mb: get("confluence_attachment_max_mb")};
}
/* Политику сервер получает, только если человек её изменил: сохранение несвязанных полей
   (имя, корни, Jira) политику не трогает, даже если форма показала её не так, как лежит в конфиге. */
function confAttachmentsDrop(body, shown){
  for (const k of Object.keys(shown)) if (body[k] === shown[k]) delete body[k];
  return body;
}
function confAttachmentsFields(A, inp){
  return [
    el("label",{style:"display:block;margin-bottom:12px"},
      el("div",{class:"muted",style:"font-size:12px;margin-bottom:4px"}, t("proj.f_conf_attachments")),
      watch(el("select",{class:"btn",style:"width:100%;font-weight:400","data-conf":"attachments",
        onchange:e=>A.conf_attachments=e.target.value},
        ...CONF_ATTACH_MODES.map(v=>el("option",{value:v, selected:A.conf_attachments===v?"":null},
          t("proj.att_" + v)))),
        "form", t("dirty.form"))),
    inp("conf_attachment_max_mb", t("proj.f_conf_att_max"), t("proj.f_conf_att_max_ph"),
        t("proj.f_conf_att_max_hint")),
    el("div",{class:"muted",style:"font-size:12px;margin:-4px 0 14px"}, t("proj.att_hint"))];
}

/* Длина путей: сервер присылает находки (`path_findings`) вместе с ответом записи и по запросу
   `/api/paths`. Запись не блокируется — находка стоит красной строкой у своей строки формы (корень
   Confluence, адрес сайта) и сводкой под кнопкой «Сохранить»: запуск возможен, но не всё будет загружено.
   Слова — из каталога языка; сервер не присылает ни русских подписей, ни готовых фраз. */
const PATHS = {map: {}, lines: 0, partial: false, models: {}};
function pathsSet(d){
  PATHS.map = {};
  for (const f of (d && d.findings) || []) (PATHS.map[f.key] = PATHS.map[f.key] || []).push(f);
  PATHS.lines = Object.keys(PATHS.map).length;
  PATHS.partial = !!(d && d.partial);
  PATHS.models = (d && d.models) || {};
}
function pathWhere(f){            // «atlassian → confluence → sync_roots → строка 2»
  return (f.where || []).map(p => typeof p === "number" ? t("pathlen.row", {n: p + 1}) : String(p)).join(" → ");
}
function pathText(f, full){
  const win = f.system === "windows";
  const x = {system: t("pathlen.sys." + f.system), length: f.length, limit: f.limit, name: f.sample, root: f.root,
             rootLen: f.root_len, unit: t(win || f.system === "all" ? "pathlen.unit_chars" : "pathlen.unit_bytes")};
  const what = f.kind === "name" ? t("pathlen.name", x) : f.kind === "prefix" ? t("pathlen.prefix", x)
    : f.kind === "dir" ? t("pathlen.dir", x) : f.level === "risk" ? t("pathlen.risk", x)
    : f.count ? t("pathlen.files", Object.assign({n: f.count}, x)) : t("pathlen.path", x);
  const eff = f.extra && f.extra.effect ? t("pathlen.eff." + f.extra.effect) : "";
  const cond = win && f.kind !== "name" ? " " + t("pathlen.win_cond") : "";
  const root = full ? " " + t(f.extra && f.extra.assumed ? "pathlen.root_assumed" : "pathlen.root", x) : "";
  return what + cond + (eff ? " — " + eff : "") + "." + root;
}
function pathLines(key){           // красные строки под строкой формы: пусто — находок нет
  return (PATHS.map[key] || []).map(f => el("div", {class: "pathline", "data-path": key}, pathText(f, false)));
}
/* Место под строкой формы для красных строк: наполняется на месте (`fill`), строка формы не перерисовывается —
   поздний ответ сервера не отнимает у человека фокус и каретку, пока он печатает. */
function noteBox(list, keyOf){
  const box = el("div", {class: "pathnotes"});
  const fill = () => { box.innerHTML = ""; box.append(...pathLines(keyOf())); };
  fill();
  list.push({fill});
  return box;
}
function pathBoxFill(box){
  box.innerHTML = "";
  if (!PATHS.lines) return;
  const list = el("ul", {});
  const advice = new Set();
  for (const fs of Object.values(PATHS.map)) for (const f of fs){
    list.append(el("li", {}, el("b", {}, pathWhere(f)), ": " + pathText(f, true)));
    advice.add(f.kind === "name" ? t("pathlen.advice_name") : f.system === "windows" && f.kind === "path"
               ? t("pathlen.advice_win") : t("pathlen.advice_dir"));
  }
  box.append(el("div", {class: "warnbox dangerbox pathbox"},
    el("div", {}, el("b", {}, t("pathlen.title", {n: PATHS.lines}))),
    list,
    ...[...advice].map(text => el("div", {class: "muted", style: "font-size:12px;margin-top:8px"}, text)),
    PATHS.partial ? el("div", {class: "muted", style: "font-size:12px;margin-top:4px"}, t("pathlen.partial")) : null));
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
  // Политика вложений Confluence, как её показала форма (после записи — как она записана): с ней сравнивается
  // отправляемое, иначе возврат поля к первому значению после сохранения при несохранённых правках соседней группы
  // (страница тогда не пересобирается и `get` остаётся прежним) молча отбрасывался бы.
  let shownPolicy = null;
  const A = {
    name: get("name"), slug: get("slug"),
    conf_url: get("base_url"), conf_space: get("space"),
    ...confAttachmentsRead(get),
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

  pathsSet(null);               // первый кадр — без находок прежнего проекта; настоящие придут запросом ниже
  const rootNotes = [], webNotes = [];
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
    rootNotes.length = 0;
    const twin = rootTwins();
    if (A.sync_roots.length > 1)
      rootsBox.append(el("div",{class:"row",style:"gap:6px;margin-bottom:8px"},
        el("span",{class:"muted",style:"font-size:12px"}, t("proj.roots_sort")),
        el("button",{class:"btn sm", onclick:()=>sortRoots("title")}, t("proj.sort_title")),
        el("button",{class:"btn sm", onclick:()=>sortRoots("id")}, t("proj.sort_id"))));
    A.sync_roots.forEach((r, i) => rootsBox.append(el("div",{}, el("div",{class:"row",style:"margin-bottom:8px"},
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
                     setDirty("form", t("dirty.form"), true);}},"✕")),
      noteBox(rootNotes, () => "root:" + (r.page_id || "").trim()))));
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
    webNotes.length = 0;
    A.web_pages.forEach((w, i) => webBox.append(el("div",{}, el("div",{class:"row",style:"margin-bottom:8px"},
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
                     setDirty("form", t("dirty.form"), true);}},"✕")),
      noteBox(webNotes, () => "web:" + (w.url || "").trim()))));
    webBox.append(el("button",{class:"btn sm",style:"margin-top:4px",
      onclick:()=>{A.web_pages.push({url:"",trusted:false}); drawWeb();
                   setDirty("form", t("dirty.form"), true);}},
      t("proj.add_web")));
  };
  drawWeb();
  // Длина путей: сводка под кнопкой и красные строки у корней и адресов. Находки приходят отдельным запросом
  // (обход зеркал не должен держать открытие страницы) и с ответом каждой записи.
  const pathBox = el("div", {});
  const drawPaths = () => { rootNotes.forEach(n => n.fill()); webNotes.forEach(n => n.fill()); pathBoxFill(pathBox); };
  let pathSeq = 0;              // номер запроса: ответ записи свежее запроса, ушедшего раньше него
  const showPaths = d => { pathSeq++; pathsSet(d); drawPaths(); };
  const asked = S.project.path, mine = ++pathSeq;
  api("/api/paths?project=" + encodeURIComponent(asked), {quiet:true}).then(d => {
    // ответ позднее ухода со страницы, смены проекта или более свежего ответа записи не рисуется
    if (d && Array.isArray(d.findings) && pathBox.isConnected && S.project && S.project.path === asked && mine === pathSeq) {
      pathsSet(d); drawPaths();
    }
  }).catch(() => {});

  formBox.append(el("div",{class:"card",style:"padding:20px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 14px"},
      t("proj.about")),
    inp("name", t("proj.f_name"), "My Project"),
    inp("slug", t("proj.f_slug"), "MyProject", t("proj.f_slug_hint")),
    el("div",{class:"sep"}),
    inp("conf_url", t("proj.f_conf_url"), "https://confluence.example.com"),
    inp("conf_space", t("proj.f_conf_space"), "SPACE"),
    ...confAttachmentsFields(A, inp),
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
          body:JSON.stringify(confAttachmentsDrop({project:S.project.path, ...A},
                                                  shownPolicy || (shownPolicy = confAttachmentsRead(get))))});
        e.target.disabled = false;
        if (r.ok){
          shownPolicy = {conf_attachments: A.conf_attachments, conf_attachment_max_mb: A.conf_attachment_max_mb};
          showPaths(r.path_findings);
          toast(PATHS.lines ? t("proj.saved") + " · " + t("pathlen.toast", {n: PATHS.lines}) : t("proj.saved"),
                PATHS.lines ? "warn" : "ok");
          setDirty("form", t("dirty.form"), false);
          S.state = await api("/api/state");
          S.project = S.state.projects.find(p=>p.path===S.project.path) || S.project;
          renderOverview(); renderProjectAfterSave();
        }
      }),
      el("span",{class:"muted",style:"font-size:12px"}, t("proj.slug_note"))),
    pathBox));

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
        renderProjectAfterSave(); }
    })));

  // 4. конфиг как текст — для того, чего нет в форме
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
        if (r.ok){ showPaths(r.path_findings);
          toast((r.unchanged ? t("yaml.unchanged") : t("yaml.saved", {backup: r.backup}))
                + (PATHS.lines ? " · " + t("pathlen.toast", {n: PATHS.lines}) : ""), PATHS.lines ? "warn" : "ok");
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
  renderPanelCard(box);
  await renderKinds(box);
  try { await renderSourceAccess(box); }
  catch (e){ showFault(t("modules.refresh_failed", {id: "access", why: e.message})); }
  try { await renderIngest(box); }
  catch (e){ showFault(t("modules.refresh_failed", {id: "ingest", why: e.message})); }
  // Сбой одной группы не должен унести строку переходов и всё, что ниже: так же, как у разделов (`refreshModules`).
  try { await renderCardTypes(box); }
  catch (e){ showFault(t("modules.refresh_failed", {id: "cardtypes", why: e.message})); }
  drawSetupJump(box);
}

/* ---------------- скины ---------------- */
// Оформление живёт в отдельных файлах: свой .css в cockpit/skins/ появляется в списке сам.
const SKINS_REJECTED_SHOWN = new Set();
async function loadSkins(){
  const sel = $("#skinSel");
  const d = await api("/api/skins", {quiet:true});
  const list = (d && d.skins) || [];
  // Скин, не принятый проверкой, в выборе не появится — скажем почему (как у разделов
  // `modules.broken`), иначе человек ищет свой файл в списке и считает, что сломалась панель.
  // Смена языка перечитывает список: одну и ту же причину второй раз не повторяем.
  for (const r of (d && d.rejected) || []){
    const key = r.id + "\n" + r.error;
    if (SKINS_REJECTED_SHOWN.has(key)) continue;
    SKINS_REJECTED_SHOWN.add(key);
    toast(t("skin.broken", {id: r.id, why: r.error}), "warn");
  }
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
    const r = await fetch(`/api/skin?id=${encodeURIComponent(id)}`, {headers:{"X-Aurora-Token": TOKEN}});
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
const CTX_API = 1;            // = aurora_common.EXT_API_VERSION: версия ctx для `api_version` манифеста
const CTX_LIFECYCLE = 1;      // раздел с `export const lifecycle = 1` обновляется лениво (README раздела)

// Поле раздела: из `export default {…}`, а если там нет — из именованного экспорта модуля
// (`export const refreshOn = […]` рядом с `export default {mount, refresh}`).
function modField(rec, name){
  if (rec.mod && rec.mod[name] !== undefined) return rec.mod[name];
  return rec.ns ? rec.ns[name] : undefined;
}

function moduleOn(id, event, fn){
  const rec = MODULES.get(id);
  if (!rec || typeof fn !== "function") return () => {};
  if (!rec.subs) rec.subs = new Map();
  if (!rec.subs.has(event)) rec.subs.set(event, new Set());
  rec.subs.get(event).add(fn);
  return () => { rec.subs && rec.subs.has(event) && rec.subs.get(event).delete(fn); };
}

// Хук раздела (onShow, onHide, dispose). Сбой хука — сообщение человеку, а не обрыв: соседний
// раздел и сам переход от этого не страдают.
async function callHook(id, rec, name){
  const fn = modField(rec, name);
  if (typeof fn !== "function") return;
  try { await fn.call(rec.mod, moduleCtx(id, rec.section)); }
  catch (e){ showFault(t("modules.refresh_failed", {id, why: name + ": " + e.message})); }
}

// Хуки одного раздела идут друг за другом: если из раздела ушли, пока медленный `onShow` ещё
// ждёт данных, `onHide` встаёт в очередь за ним, а не обгоняет — иначе опрос, который `onShow`
// запустит позже, остался бы работать у скрытого раздела.
function queueHook(id, rec, name){
  // Нет незаконченного хука — зовём сразу (порядок «onHide ушедшего раньше refresh нового» цел).
  const run = () => callHook(id, rec, name);
  const p = rec.hooks ? rec.hooks.then(run) : run();     // callHook не отказывает: сбой он показывает сам
  rec.hooks = p;
  p.then(() => { if (rec.hooks === p) rec.hooks = null; });
  return p;
}

// Раздел, из которого ушли: `onHide` — остановить таймеры и опросы, которые нужны только на виду.
function hideModule(id){
  const rec = MODULES.get(id);
  if (!rec || !rec.mod || !rec.shown) return;
  rec.shown = false;
  queueHook(id, rec, "onHide");
}

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
    // Раздел поднят, но с умолчанием вместо кривого поля манифеста: скажем, что починить.
    // Скрытому профилем разделу toast не нужен: его нет в меню, и открыть его нельзя.
    if (m.warn && m.warn.length && !m.hidden)
      toast(t("modules.warn", {id: m.id, why: m.warn.join("; ")}), "warn");
    MODULES.set(m.id, {manifest: m, mod: null, ns: null, section: null, subs: null, shown: false, hooks: null});
    if (!m.hidden) addModuleNav(m);       // скрытый профилем: запись есть (подписи, адрес), кнопки нет
    if (m.behind && !m.hidden)
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
    api_version: CTX_API,         // версия ctx: раздел сверяет её с той, под которую писан
    lifecycle: CTX_LIFECYCLE,     // 1 — ядро знает хуки onShow/onHide/dispose и ленивое обновление
    // Подписка на событие ядра («data», «health»): скрытый раздел с `lifecycle` получает вместо
    // полной перерисовки именно этот вызов. Возвращает функцию отписки.
    on: (event, fn) => moduleOn(id, event, fn),
    t, el, api, toast,
    $: (sel) => $(sel, root),
    $$: (sel) => $$(sel, root),
    token: TOKEN,                 // только чтение; в адреса его не класть — для ссылок есть `openLink`
    openLink,
    show, run: runStep, runWatched, busyElsewhere, openRun, openDoc, reloadHealth,
    // Маршрут ведёт ядро: он переживает перезагрузку вкладки, пишет в проект
    // и продолжается с той же точки. Раздел только показывает список.
    runRoute, poll,
    // Все маршруты кита: скрытые профилем — с `hidden: true`, показывает раздел только остальные.
    scenarios: async () => S.scenarios || (S.scenarios = await api("/api/scenarios?all=1")),
    restartPanel,
    fmt: {ago, kb, when: histWhen, esc, tick, md, mdLite, rtime, howLong},
    // Общие детали панели: раздел не рисует свою плитку метрики и свою кнопку
    // перехода к маршруту — иначе в каждом разделе они разъедутся.
    ui: {metricCard, metric, goRoute, goCmd, kindChip, skillLine, copyButton, engineWord,
         editor: markdownEditor,
         // Здоровье приходит частями: пришла ли часть и чем занять место под её число.
         has: hasPart, wait: waitMark, healthAt, stampText, kbInfo, templatesHint, templatesState, rebuildHint, openIssue, issueUrl,
         // Публикация документа тем же путём, что в «Файлах»: редактор, карточка «что и куда уйдёт», кнопки по исходу.
         publishFile: async rel => { await openPath(rel); if (F.path === rel) await publishFromEditor(); },
         // Серая кнопка ведёт туда, где её чинят: `kinds` — «Настройки проекта → Артефакты», иначе «Настройки проекта».
         openSettings: fix => { SGROUPS.focus = fix === "kinds" ? "project:kinds" : "project:form"; show("project"); }},
    openPath, isEngineCmd, hideDev, openProject,
    // Достроить/обновить движок выбранного проекта на этой машине: ту же кнопку рисует полоса ядра.
    prepareEngine: () => prepareEngine(S.project),
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
      const url = f => `/modules/${id}/${f}`;       // код раздела отдаётся без токена
      const r = await fetch(url("view.html"));
      if (!r.ok) throw new Error(t("modules.no_html", {status: r.status}));
      rec.section = el("section", {class:"view", id:"view-" + id, html: await r.text()});
      $("main").append(rec.section);
      applyI18n(rec.section);
      const mod = await import(url("view.js"));
      rec.ns = mod;
      rec.mod = mod.default || mod;
      await rec.mod.mount?.(moduleCtx(id, rec.section));
    }catch(e){
      // Раздел, успевший начать работу, освобождает то, что занял (таймеры, подписки).
      if (rec.mod) await callHook(id, rec, "dispose");
      rec.mod = null; rec.ns = null; rec.subs = null; rec.shown = false; rec.hooks = null;
      if (rec.section){ rec.section.remove(); rec.section = null; }
      toast(t("modules.failed", {id, why: e.message}), "err");
      return;
    }
  }
  rec.section.classList.toggle("on", S.view === id);
  // Груз переходa: «открой справку на этом документе», «покажи карточку на
  // графе». Без него раздел пришлось бы дёргать через глобальную переменную.
  // Сбой первой отрисовки — сообщение, но не повод лишать раздел `onShow`: он на экране, и
  // опросу, который он запускает, ещё предстоит выровнять картину.
  try { await rec.mod.refresh?.(moduleCtx(id, rec.section), payload); }
  catch (e){ showFault(t("modules.refresh_failed", {id, why: e.message})); }
  // Раздел всё ещё на виду (человек мог уйти, пока шла отрисовка) и ещё не показан: `onShow`.
  if (S.view === id && !rec.shown){
    rec.shown = true;
    await queueHook(id, rec, "onShow");
  }
}

// Данные сменились — выбрали проект, пересчитали здоровье, сохранили настройку. Разделы,
// уже поднятые в этой вкладке, обязаны перерисоваться сами: раздел, открытый до выбора
// проекта, иначе остаётся с «выберите проект» навсегда, и перерисовать его некому.
async function refreshModules(){ await dispatchModules("data"); }

// Событие ядра — разделам. Раздел без `lifecycle` ведёт себя как прежде: «data» перерисовывает
// его всегда, другое событие — только если он назвал его в `refreshOn`. Раздел с
// `export const lifecycle = 1` перерисовывается лишь на виду; скрытому достаётся подписка
// `ctx.on(event, fn)`, а не подписанный не получает ничего; при открытии раздел всё равно
// отрисовывается целиком (`mountModule` зовёт `refresh` при каждом открытии). Так скрытые разделы
// не строят карточки и не ходят за данными на каждое событие.
async function dispatchModules(event = "data"){
  const one = async (id, rec) => {
    if (!rec.mod) return;           // раздел мог не подняться, пока шла очередь
    const on = modField(rec, "refreshOn");
    const subs = rec.subs && rec.subs.get(event);
    const named = Array.isArray(on) && on.includes(event);
    const lazy = !!modField(rec, "lifecycle");
    // Упавший раздел не уносит остальные: иначе одна ошибка в одном экране оставляет
    // всю панель с вчерашними числами, и причину видно только в консоли браузера.
    try {
      if (!lazy){
        // Подписки `ctx.on` у раздела без `lifecycle` не зовутся: он перерисовывается целиком.
        if (event === "data" || named) await rec.mod.refresh?.(moduleCtx(id, rec.section));
      } else if (event === "data" || named || (subs && subs.size)){
        if (S.view === id){
          await rec.mod.refresh?.(moduleCtx(id, rec.section));
        } else if (subs && subs.size){
          // Подписчики не мешают друг другу: сбой одного — сообщение, остальные зовутся.
          for (const fn of [...subs]){
            try { await fn(moduleCtx(id, rec.section)); }
            catch (e){ showFault(t("modules.refresh_failed", {id, why: event + ": " + e.message})); }
          }
        }
      }
    }
    catch (e){ showFault(t("modules.refresh_failed", {id, why: e.message})); }
  };
  const live = [...MODULES].filter(([, rec]) => rec.mod);
  // «data» идёт по очереди, как прежде; «health» приходит часто, и медленный раздел не должен
  // задерживать перерисовку соседей, поэтому такие события обслуживаются вместе.
  if (event === "data") for (const [id, rec] of live) await one(id, rec);
  else await Promise.all(live.map(([id, rec]) => one(id, rec)));
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

// Проект в списке панели (1.190.0): панель находит проекты сама, поэтому «убрать» значит перестать показывать. Файлы, git и
// настройки остаются; вернуть проект — «Настройка кита» → «Скрытые проекты». Слепок списка (`expect`) защищает от затирания.
async function hideProject(p){
  const cur = await api("/api/hidden", {quiet: true});
  const res = await api("/api/hidden", {method: "POST", quiet: true,
    body: JSON.stringify({hide: p.path, expect: cur.stamp})});
  if (res.error) return toast(res.error, "warn");
  toast(t("hidden.done", {name: p.name}), "ok");
  S.project = null; S.health = null;
  S.state = await api("/api/state");
  renderOverview(); renderProjBadge();
  show("overview");
}
function renderPanelCard(box){
  const p = S.project;
  sgroup(box, "project:panel", t("hidden.title")).append(el("div",{class:"card",style:"padding:16px 20px;margin-bottom:18px"},
    el("p",{class:"muted",style:"font-size:13px;margin:0 0 10px"}, t("hidden.about")),
    el("button",{class:"btn sm danger", onclick:()=>hideProject(p)}, t("hidden.btn"))));
}
// «Скрытые проекты» в настройке кита: список с кнопкой «Вернуть».
async function drawHiddenGroup(box){
  const body = sgroup(box, "setup:hidden", t("hidden.setup_title"));
  const draw = async () => {
    const d = await api("/api/hidden", {quiet: true});
    body.innerHTML = "";
    body.append(el("p",{class:"muted",style:"font-size:13px;margin:0 0 10px"}, t("hidden.setup_about")));
    if (!(d.hidden || []).length){
      body.append(el("div",{class:"muted"}, t("hidden.none")));
      return;
    }
    d.hidden.forEach(h => body.append(el("div",{class:"row",style:"gap:8px;margin-bottom:6px"},
      el("div",{style:"flex:1;min-width:0"},
        el("div",{}, h.name),
        el("code",{class:"mono",style:"font-size:12px;overflow-wrap:anywhere"}, h.path
          + (h.exists ? "" : " · " + t("hidden.missing")))),
      el("button",{class:"btn sm", onclick: async () => {
        const res = await api("/api/hidden", {method: "POST", quiet: true,
          body: JSON.stringify({show: h.path, expect: d.stamp})});
        if (res.error && !res.conflict) return toast(res.error, "warn");
        if (!res.conflict) toast(t("hidden.restored", {name: h.name}), "ok");
        S.state = await api("/api/state"); renderOverview();
        await draw();
      }}, t("hidden.restore")))));
  };
  await draw();
}

/* ---------------- о проекте ---------------- */
// Открыть документ — это переход в справку с именем файла: сам показ делает раздел.
function openDoc(path){
  show("reference", {doc: path});
}
// Ссылка справки не ведёт никуда (адрес из документа не берём), а гасит переход делегатом:
// встроенный `onclick=` запрещён политикой страницы (CSP без `unsafe-inline` для скриптов).
document.addEventListener("click", e => {
  if (e.target && e.target.closest && e.target.closest("a.md-dead")) e.preventDefault();
});
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
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="#" class="md-dead">$1</a>');
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
    ...S.state.commands.map(c=>({nm:c.cmd, ds:c.what, llm:c.cmd, go:()=>openRun(c.cmd)})),
  ];
  $("#paletteOverlay").classList.add("on");
  const inp = $("#paletteInput"); inp.value=""; inp.focus(); drawPalette("");
}
function drawPalette(q){
  q = q.toLowerCase();
  const hits = PAL.items.filter(i=>(i.nm+" "+i.ds).toLowerCase().includes(q)).slice(0,40);
  PAL.hits = hits; PAL.sel = 0;
  const box = $("#paletteResults"); box.innerHTML="";
  hits.forEach((h,idx)=>box.append(el("button",{class:"res"+(idx===0?" sel":""), "data-llm": h.llm || null,
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
$("#netOverlay").onclick = e => { if (e.target.id==="netOverlay" && NET_CLOSE) NET_CLOSE(null); };
document.addEventListener("keydown", e=>{
  if ((e.metaKey||e.ctrlKey) && e.key.toLowerCase()==="k"){ e.preventDefault(); openPalette(); }
  if (e.key==="Escape"){ if (NET_CLOSE) NET_CLOSE(null); closeRun(); closeMcp(); $("#paletteOverlay").classList.remove("on"); }
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
