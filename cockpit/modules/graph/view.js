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
  setSpread(ctx, SPREAD);
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
  const css = getComputedStyle(document.body);
  const ink = css.getPropertyValue("--text").trim() || "#ddd";
  const line = css.getPropertyValue("--border").trim() || "#555";
  const hot = css.getPropertyValue("--accent").trim() || "#e0a33e";
  G.cy = cytoscape({
    container: box,
    elements: [
      ...nodes.map(n => ({data: {id: n.id, label: n.title || n.id, path: n.path,
                                 draft: n.status === "draft" ? 1 : 0,
                                 me: n.id === G.focus ? 1 : 0}})),
      ...edges.map(e => ({data: {id: e.from + "→" + e.to, source: e.from, target: e.to}}))],
    style: [
      {selector: "node", style: {"background-color": line, "label": "data(label)",
        "color": ink, "font-size": "9px", "width": 10, "height": 10,
        "text-max-width": "120px", "text-wrap": "ellipsis"}},
      // Черновик виден отдельно: строить на нём требования нельзя, и это должно быть
      // заметно до того, как человек откроет карточку.
      {selector: "node[draft = 1]", style: {"background-color": "#8a8a8a",
        "border-width": 1, "border-style": "dashed", "border-color": line}},
      {selector: "node[me = 1]", style: {"background-color": hot, "width": 16,
        "height": 16, "font-size": "11px", "font-weight": "bold"}},
      {selector: "edge", style: {"width": 1, "line-color": line,
        "curve-style": "haystack", "opacity": 0.55}}],
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
}

function setSpread(ctx, v){
  SPREAD = Math.max(0.4, Math.min(6, Math.round(v * 10) / 10));
  localStorage.setItem("aurora-graph-spread", String(SPREAD));
  const box = ctx.$("#graphSpreadNow");
  if (box) box.textContent = "×" + SPREAD.toFixed(1);
  if (G.cy) draw(ctx);
}

export default {mount, refresh};
