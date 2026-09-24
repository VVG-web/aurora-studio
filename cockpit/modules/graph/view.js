/* Граф базы — раздел-модуль.

   Карточки и связи между ними: способ дойти до карточки, а не отчёт. Библиотека
   раскладки весит 428 КБ и грузится при первом открытии раздела — то же правило, что у
   редактора. Сам граф считает движок (`kb:links --cards-json`), панель только рисует. */

// Сколько узлов имеет смысл раскладывать. Не выдумка: на живой базе раскладка тысячи
// узлов вешает вкладку целиком, и это выглядит как поломка панели, а не как «много».
const LIMIT = 900;

const G = {data: null, cy: null, focus: "", anyway: false, fitKey: ""};

// Насколько разводить узлы. Одной раскладки на все базы не бывает: сорок карточек и
// четыреста требуют разного, и «чёрный клубок» — это не свойство графа, а неподходящий
// разброс. Человек крутит его сам, значение запоминается.
let SPREAD = parseFloat(localStorage.getItem("aurora-graph-spread") || "1") || 1;

// Размер подписей — отдельно от масштаба: приблизить граф значит увеличить и узлы, и
// расстояния, а читать мешают только буквы. Крутится своими кнопками, запоминается.
let FONT = parseFloat(localStorage.getItem("aurora-graph-font") || "1") || 1;

// Уровни подписей по числу связей (просьба пользователя 24.09.2026): верхние TOP % узлов
// по числу связей — «много», нижние LOW % — «мало», остальные — «средне». Пороги — по
// распределению в показанном графе, а не числами из головы: у сорока карточек и у
// четырёхсот «много связей» — разное. Настраивается на панели графа.
let TIERS = (() => {
  try { return Object.assign({top: 10, low: 40},
                             JSON.parse(localStorage.getItem("aurora-graph-tiers") || "{}")); }
  catch (e) { return {top: 10, low: 40}; }
})();

/** Уровень подписи каждого узла. degree — {id: число связей}. → {tiers, hi, lo}.
 *  Одинаковое число связей — всегда один уровень: читающий граф сравнивает размеры, и две
 *  карточки с одной связью, подписанные по-разному, врали бы ему. Поэтому граница уровня
 *  встаёт между группами узлов с равным числом связей — та, что ближе к заданной доле:
 *  верхние `top` % — «много», нижние `low` % — «мало». Разброса нет — все «средне». */
export function degreeTiers(degree, top, low){
  const ids = Object.keys(degree);
  const n = ids.length;
  const counts = {};
  ids.forEach(id => { counts[degree[id]] = (counts[degree[id]] || 0) + 1; });
  const values = Object.keys(counts).map(Number).sort((a, b) => a - b);
  const pick = (order, want) => {       // значение-граница, при котором доля ближе к want
    if (want <= 0) return null;
    let best = null, bestGap = want, cum = 0;   // «никого» — тоже вариант
    for (const v of order){
      cum += counts[v];
      const gap = Math.abs(cum - want);
      if (gap < bestGap){ best = v; bestGap = gap; }
    }
    return best;
  };
  const clamp = v => Math.max(0, Math.min(100, v));
  let hi = pick([...values].reverse(), n * clamp(top) / 100);
  let lo = pick(values, n * clamp(low) / 100);
  if (hi !== null && lo !== null && hi <= lo){
    // Доли перекрылись на узком разбросе: «много» остаётся за верхней группой, а «мало»
    // уходит на ступень ниже неё — или исчезает, если ниже ничего нет.
    const below = values.filter(v => v < hi);
    lo = below.length ? below[below.length - 1] : null;
  }
  if (hi !== null && hi === values[0]) hi = null;   // у всех поровну — выделять некого
  const out = {};
  for (const id of ids){
    const d = degree[id];
    out[id] = (hi !== null && d >= hi) ? "many" : ((lo !== null && d <= lo) ? "few" : "mid");
  }
  return {tiers: out, hi, lo};
}

let CYTO = null;
function ensureCyto(ctx){
  if (CYTO) return CYTO;
  CYTO = new Promise((ok, fail) => {
    const s = ctx.el("script", {src: "/vendor/cytoscape/dist/cytoscape.min.js"});
    s.onload = ok;
    s.onerror = () => { ctx.toast(ctx.t("graph.nolib"), "err"); fail(); };
    document.head.append(s);
  });
  return CYTO;
}

export function mount(ctx){
  ctx.root.dataset.module = "graph";

  ctx.$("#graphFind").addEventListener("change", e => {
    const v = (e.target.value || "").trim();
    if (!G.data) return;
    const hit = G.data.nodes.find(n => n.id === v)
             || G.data.nodes.find(n => (n.title || "").toLowerCase() === v.toLowerCase());
    if (!hit) return ctx.toast(ctx.t("graph.nocard", {id: v}), "warn");
    G.focus = hit.id;
    draw(ctx);
  });
  ctx.$("#graphDepth").addEventListener("change", () => draw(ctx));
  ctx.$("#graphOut").addEventListener("click", () => setSpread(ctx, SPREAD * 1.4));
  ctx.$("#graphIn").addEventListener("click", () => setSpread(ctx, SPREAD / 1.4));
  ctx.$("#graphAll").addEventListener("click", () => {
    G.focus = "";
    ctx.$("#graphFind").value = "";
    draw(ctx);
  });
  ctx.$("#graphRebuild").addEventListener("click", () => load(ctx, true));
  ctx.$("#graphFontUp").addEventListener("click", () => setFont(ctx, FONT * 1.2));
  ctx.$("#graphFontDown").addEventListener("click", () => setFont(ctx, FONT / 1.2));
  const top = ctx.$("#graphTierTop"), low = ctx.$("#graphTierLow");
  top.value = TIERS.top; low.value = TIERS.low;
  const onTiers = () => {
    TIERS = {top: clampPct(top.value, 10), low: clampPct(low.value, 40)};
    try { localStorage.setItem("aurora-graph-tiers", JSON.stringify(TIERS)); } catch (e) {}
    restyle(ctx);
  };
  top.addEventListener("change", onTiers);
  low.addEventListener("change", onTiers);
  setSpread(ctx, SPREAD);
  setFont(ctx, FONT);
}

function clampPct(v, dflt){
  const n = parseFloat(v);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : dflt;
}

export async function refresh(ctx, payload){
  // Пришли из файла: «показать эту карточку на графе». Раздел сам решает, что для
  // этого нужно загрузиться — зовущему знать об этом незачем.
  const card = payload && payload.card
    ? payload.card.split("/").pop().replace(/\.md$/, "") : "";
  if (!G.data || card) await load(ctx, false);
  if (!card || !G.data) return;
  if (!G.data.nodes.some(n => n.id === card))
    return ctx.toast(ctx.t("graph.nocard", {id: card}), "warn");
  G.focus = card;
  ctx.$("#graphFind").value = card;
  draw(ctx);
}

async function load(ctx, rebuild){
  const note = ctx.$("#graphNote");
  if (!ctx.project){ note.textContent = ctx.t("graph.pick_project"); return; }
  note.textContent = ctx.t("graph.loading");
  const d = await ctx.api("/api/graph?project=" + encodeURIComponent(ctx.project.path)
                          + (rebuild ? "&rebuild=1" : ""), {quiet: true});
  if (d.error){ note.textContent = d.error; return; }
  G.data = d;
  ctx.$("#graphStamp").textContent = ctx.t("graph.when",
    {when: d.when ? ctx.fmt.when(d.when) : "—"});
  // Пересчёт сорвался, а картинка прежняя — сказать вслух. Молча показанный вчерашний
  // граф человек примет за сегодняшний и построит на нём решение.
  if (d.stale_reason) ctx.toast(ctx.t("graph.stale", {why: d.stale_reason}), "warn");
  const list = ctx.$("#graphList");
  list.innerHTML = "";
  // Список для подсказки ограничиваем: три тысячи <option> браузер рисует заметно.
  d.nodes.slice(0, 1200).forEach(n => list.append(ctx.el("option", {value: n.id}, n.title)));
  note.textContent = ctx.t("graph.counts",
    {nodes: d.nodes.length, edges: d.edges.length, orphans: d.orphans});
  await ensureCyto(ctx);
  draw(ctx);
}

function neighbourhood(id, depth){
  // Окрестность вместо всей базы: полторы тысячи узлов на экране — это клубок, из
  // которого не выбрать ни одного. Человеку нужен ответ на «с чем это связано и куда
  // идти дальше», а не портрет базы целиком.
  const near = new Set([id]);
  let edge = new Set([id]);
  for (let i = 0; i < depth; i++){
    const next = new Set();
    for (const e of G.data.edges){
      if (edge.has(e.from) && !near.has(e.to)) next.add(e.to);
      if (edge.has(e.to) && !near.has(e.from)) next.add(e.from);
    }
    next.forEach(x => near.add(x));
    edge = next;
    if (!next.size) break;
  }
  return near;
}

function draw(ctx){
  if (!G.data || !window.cytoscape) return;
  const box = ctx.$("#graphBox");
  const depth = parseInt(ctx.$("#graphDepth").value, 10) || 2;
  const want = G.focus ? neighbourhood(G.focus, depth) : null;
  const nodes = want ? G.data.nodes.filter(n => want.has(n.id)) : G.data.nodes;
  const ids = new Set(nodes.map(n => n.id));
  const edges = G.data.edges.filter(e => ids.has(e.from) && ids.has(e.to));
  // Порог считаем по тому, что реально пойдёт в раскладку, а не по тому, откуда набор
  // взялся: у карточки-концентратора окрестность оказывается почти всей базой.
  // Порог — предупреждение с проходом, а не запрет: решать за человека, что ему не
  // нужен большой граф, мы не вправе — разброс как раз и делает такой граф читаемым.
  if (nodes.length > LIMIT && !G.anyway){
    const note = ctx.$("#graphNote");
    note.innerHTML = "";
    note.append(document.createTextNode(G.focus
      ? ctx.t("graph.hub", {id: G.focus, n: nodes.length, depth})
      : ctx.t("graph.toobig", {n: nodes.length})), " ",
      ctx.el("button", {class: "btn", style: "margin-left:6px", "data-help": "graph.help.anyway", onclick(){
        G.anyway = true; draw(ctx); G.anyway = false;
      }}, ctx.t("graph.anyway")));
    if (G.cy){ G.cy.destroy(); G.cy = null; }
    box.innerHTML = "";
    return;
  }
  if (G.cy){ G.cy.destroy(); G.cy = null; }
  const degree = Object.fromEntries(nodes.map(n => [n.id, 0]));
  for (const e of edges){ degree[e.from]++; degree[e.to]++; }
  G.degree = degree;
  const res = degreeTiers(degree, TIERS.top, TIERS.low);
  const tiers = res.tiers;
  G.cy = cytoscape({
    container: box,
    elements: [
      ...nodes.map(n => ({data: {id: n.id, label: n.title || n.id, path: n.path,
                                 draft: n.status === "draft" ? 1 : 0,
                                 me: n.id === G.focus ? 1 : 0, tier: tiers[n.id]}})),
      ...edges.map(e => ({data: {id: e.from + "→" + e.to, source: e.from, target: e.to}}))],
    style: sheet(),
    // `fit: false` намеренно: с автоподгонкой раскладка всегда вписывается в окно, и
    // «развести узлы» гасится обратным масштабированием — человек жмёт «+», а картинка
    // не меняется. Первый показ подгоняем сами, дальше масштаб в руках человека.
    layout: {name: "cose", animate: false, fit: false, randomize: true,
             nodeRepulsion: Math.round(9000 * SPREAD * SPREAD),
             idealEdgeLength: Math.round(60 * SPREAD),
             nestingFactor: 1.2, gravity: 1 / SPREAD},
    wheelSensitivity: 0.25
  });
  if (G.fitKey !== G.focus + "|" + depth){
    G.fitKey = G.focus + "|" + depth;
    setTimeout(() => { try { G.cy.fit(undefined, 40); } catch (e) {} }, 30);
  }
  // Клик ведёт в карточку. Граф, из которого нельзя попасть в файл, — картинка
  // «смотрите, красиво»: её посмотрят один раз.
  G.cy.on("tap", "node", evt => ctx.openPath(evt.target.data().path));
  G.cy.on("cxttap", "node", evt => {
    G.focus = evt.target.id();
    ctx.$("#graphFind").value = G.focus;
    draw(ctx);
  });
  ctx.$("#graphNote").textContent = G.focus
    ? ctx.t("graph.around", {id: G.focus, n: nodes.length, depth})
    : ctx.t("graph.counts", {nodes: G.data.nodes.length, edges: G.data.edges.length,
                             orphans: G.data.orphans});
  tierNote(ctx, res);
}

// Стиль узлов: размер букв — от уровня подписи и от ручки «Шрифт». Узлы и связи от
// шрифта не зависят — меняются только подписи, раскладка остаётся.
function sheet(){
  const css = getComputedStyle(document.body);
  const ink = css.getPropertyValue("--text").trim() || "#ddd";
  const line = css.getPropertyValue("--border").trim() || "#555";
  const hot = css.getPropertyValue("--accent").trim() || "#e0a33e";
  const px = v => (Math.round(v * FONT * 10) / 10) + "px";
  return [
    {selector: "node", style: {"background-color": line, "label": "data(label)",
      "color": ink, "font-size": px(9), "width": 10, "height": 10,
      "text-max-width": "120px", "text-wrap": "ellipsis"}},
    {selector: "node[tier = 'many']", style: {"font-size": px(13), "font-weight": "bold",
      "text-max-width": "180px"}},
    {selector: "node[tier = 'few']", style: {"font-size": px(6.5)}},
    // Черновик виден отдельно: строить на нём требования нельзя, и это должно быть
    // заметно до того, как человек откроет карточку.
    {selector: "node[draft = 1]", style: {"background-color": "#8a8a8a",
      "border-width": 1, "border-style": "dashed", "border-color": line}},
    {selector: "node[me = 1]", style: {"background-color": hot, "width": 16,
      "height": 16, "font-size": px(13), "font-weight": "bold"}},
    {selector: "edge", style: {"width": 1, "line-color": line,
      "curve-style": "haystack", "opacity": 0.55}}];
}

// Шрифт и уровни меняют только стиль: граф не перекладывается, человек не теряет место.
function restyle(ctx){
  if (!G.cy || !G.degree) return;
  const res = degreeTiers(G.degree, TIERS.top, TIERS.low);
  G.cy.batch(() => G.cy.nodes().forEach(n => n.data("tier", res.tiers[n.id()])));
  G.cy.style(sheet());
  tierNote(ctx, res);
}

function tierNote(ctx, res){
  const box = ctx.$("#graphTierNow");
  if (!box) return;
  const c = {many: 0, mid: 0, few: 0};
  Object.values(res.tiers).forEach(v => { c[v] = (c[v] || 0) + 1; });
  box.textContent = ctx.t("graph.tier_now", {...c,
    hi: res.hi === null ? "—" : res.hi, lo: res.lo === null ? "—" : res.lo});
}

function setFont(ctx, v){
  FONT = Math.max(0.5, Math.min(3, Math.round(v * 100) / 100));
  try { localStorage.setItem("aurora-graph-font", String(FONT)); } catch (e) {}
  const box = ctx.$("#graphFontNow");
  if (box) box.textContent = "×" + FONT.toFixed(1);
  if (G.cy) G.cy.style(sheet());
}

function setSpread(ctx, v){
  SPREAD = Math.max(0.4, Math.min(6, Math.round(v * 10) / 10));
  localStorage.setItem("aurora-graph-spread", String(SPREAD));
  const box = ctx.$("#graphSpreadNow");
  if (box) box.textContent = "×" + SPREAD.toFixed(1);
  if (G.cy) draw(ctx);
}

export default {mount, refresh};
