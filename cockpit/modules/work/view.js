/* Продуктивность — раздел-модуль.

   Работа, ради которой база и нужна: написать артефакт, проверить чужую историю, собрать
   закрывающий документ и передать его наружу. Список маршрутов производства рисует общий
   с «Быстрым стартом» файл: экран один, разница в группе сценариев. */

import {renderRoutes} from "../quickstart/routes.js";

let MAKE = null;               // {sid, kind} — идущее производство
// Ссылки и вложения задачи: {path, label, kind: file|dir|attach}. Уходят движку `--context`;
// упоминания в тексте (`@путь`, `/навык`, `@сервер`) движок разбирает и сам.
let REFS = [];
const ACCEPT = [".md", ".markdown", ".txt", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml",
  ".toml", ".ini", ".cfg", ".csv", ".tsv", ".xml", ".html", ".htm", ".svg", ".bpmn", ".puml",
  ".mmd", ".sql", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".rb",
  ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".sh", ".ps1", ".feature", ".graphql", ".proto"];

export function mount(ctx){
  ctx.root.dataset.module = "work";
  ctx.$("#makeGo").onclick = () => startMake(ctx);
  ctx.$("#makePublish").onclick = () => publish(ctx);
  const file = ctx.$("#makeFile");
  file.accept = ACCEPT.join(",");
  ctx.$("#makeAttach").onclick = () => file.click();
  file.onchange = () => attach(ctx, [...file.files]).then(() => { file.value = ""; });
  wireSuggest(ctx);
  drawRefs(ctx);
}

// ---- ссылки и вложения ----

function addRef(ctx, ref){
  if (!REFS.some(r => r.path === ref.path)) REFS.push(ref);
  drawRefs(ctx);
}

// Подписи видов — таблицей, а не склейкой ключа: проверка каталогов видит только целые ключи.
const REF_KEY = {file: "work.ref_file", dir: "work.ref_dir", attach: "work.ref_attach"};
const KIND_KEY = {file: "work.kind_file", dir: "work.kind_dir", mcp: "work.kind_mcp",
                  skill: "work.kind_skill"};

function drawRefs(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#makeRefs");
  box.innerHTML = "";
  REFS.forEach(r => box.append(el("span", {class: "chip", title: r.path,
      style: "gap:6px;align-items:center;display:inline-flex"},
    t(REF_KEY[r.kind]) + " " + r.label,
    el("button", {class: "btn sm", style: "padding:0 6px;min-height:0", title: t("work.ref_remove"),
      onclick: () => { REFS = REFS.filter(x => x !== r); drawRefs(ctx); }}, "×"))));
}

async function attach(ctx, files){
  const {t} = ctx;
  if (!ctx.project) return ctx.toast(t("work.pick_project"), "warn");
  for (const f of files){
    const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (ext && !ACCEPT.includes(ext)){ ctx.toast(t("work.attach_text_only", {name: f.name}), "warn"); continue; }
    if (f.size > 1_000_000){ ctx.toast(t("work.attach_too_big", {name: f.name}), "warn"); continue; }
    const text = await f.text();
    const r = await ctx.api("/api/context/upload", {method: "POST", quiet: true,
      body: JSON.stringify({project: ctx.project.path, name: f.name, text})});
    if (!r || r.error){ ctx.toast((r && r.error) || t("work.attach_failed", {name: f.name}), "err"); continue; }
    addRef(ctx, {path: r.path, label: r.name, kind: "attach"});
  }
}

// ---- подсказки по @ и / ----

function tokenAtCaret(area){
  const before = area.value.slice(0, area.selectionStart);
  const m = before.match(/(^|\s)([@/][^\s]*)$/);
  return m ? {text: m[2], start: before.length - m[2].length, end: area.selectionStart} : null;
}

function wireSuggest(ctx){
  const area = ctx.$("#makeIdea"), list = ctx.$("#makeSuggest");
  let items = [], pick = 0, timer = null, tok = null;
  const close = () => { list.hidden = true; items = []; };
  const choose = it => {
    if (!tok) return close();
    const value = (it.kind === "file" || it.kind === "dir")
      ? "@" + (/\s/.test(it.value) ? '"' + it.value + '"' : it.value) : it.value;
    area.value = area.value.slice(0, tok.start) + value + " " + area.value.slice(tok.end);
    const at = tok.start + value.length + 1;
    area.setSelectionRange(at, at);
    area.focus();
    if (it.kind === "file" || it.kind === "dir") addRef(ctx, {path: it.value, label: it.label, kind: it.kind});
    close();
  };
  const draw = () => {
    const {el, t} = ctx;
    list.innerHTML = "";
    if (!items.length){ list.hidden = true; return; }
    items.forEach((it, i) => list.append(el("div", {class: "list-item",
        style: "cursor:pointer;padding:4px 8px" + (i === pick ? ";background:var(--surface-2,rgba(128,128,128,.15))" : ""),
        onmousedown: e => { e.preventDefault(); choose(it); }},
      el("span", {class: "chip", style: "flex:none"}, t(KIND_KEY[it.kind])),
      el("span", {class: "mono", style: "font-size:12px"}, it.label))));
    list.hidden = false;
  };
  area.addEventListener("input", () => {
    clearTimeout(timer);
    tok = tokenAtCaret(area);
    if (!tok || !ctx.project) return close();
    timer = setTimeout(async () => {
      const d = await ctx.api("/api/context/suggest?project=" + encodeURIComponent(ctx.project.path)
        + "&q=" + encodeURIComponent(tok.text), {quiet: true});
      items = (d && d.items) || []; pick = 0; draw();
    }, 180);
  });
  area.addEventListener("keydown", e => {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp"){
      e.preventDefault();
      pick = (pick + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length; draw();
    } else if (e.key === "Enter" || e.key === "Tab"){
      e.preventDefault(); choose(items[pick]);
    } else if (e.key === "Escape"){ close(); }
  });
  area.addEventListener("blur", () => setTimeout(close, 150));
}

export async function refresh(ctx){
  await fillKinds(ctx);
  await renderRoutes(ctx, "продуктивность", ctx.$("#workBody"));   // данные движка
}

async function fillKinds(ctx){
  const {t, el} = ctx;
  const sel = ctx.$("#makeKind");
  if (!sel) return;
  if (!ctx.project){
    sel.innerHTML = "";
    sel.append(el("option", {value: ""}, t("work.pick_project_first")));
    return;
  }
  const d = await ctx.api("/api/kinds?project=" + encodeURIComponent(ctx.project.path),
                          {quiet: true});
  const kinds = d.kinds || {};
  // Список чистим, только когда есть чем наполнить. Иначе один сорвавшийся запрос
  // стирал настроенные виды артефактов, и это выглядело как «настройки пропали».
  if (!Object.keys(kinds).length){
    sel.innerHTML = "";
    sel.append(el("option", {value: ""}, d.error
      ? t("work.kinds_failed", {why: d.error}) : t("work.kinds_none")));
    ctx.$("#makeSpec").textContent = "";
    ctx.$("#makeGo").disabled = true;
    return;
  }
  sel.innerHTML = "";
  Object.keys(kinds).sort().forEach(k => {
    const o = document.createElement("option");
    o.value = k;
    o.textContent = (kinds[k].title || k) + "  · " + k;
    sel.append(o);
  });
  const draw = () => {
    const rec = kinds[sel.value] || {};
    const bits = [
      rec.template ? t("work.spec_template", {v: rec.template}) : t("work.spec_no_template"),
      rec.out ? t("work.spec_out", {v: rec.out}) : t("work.spec_no_out"),
      rec.prompt ? t("work.spec_prompt", {v: rec.prompt}) : t("work.spec_no_prompt"),
      rec.publish_url ? t("work.spec_publish", {v: rec.publish_url}) : t("work.spec_no_publish"),
    ];
    ctx.$("#makeSpec").textContent = bits.join(" · ");
    const ready = rec.template && rec.out;
    ctx.$("#makeGo").disabled = !ready;
    ctx.$("#makePublish").disabled = !rec.publish_url;
    ctx.$("#makePublish").title = rec.publish_url
      ? t("work.publish_hint") : t("work.publish_no_url");
  };
  sel.onchange = draw;
  draw();
}

// Вызов производства — тот же путь, что у любой команды: панель не знает другого способа
// запускать движок, и это правильно, иначе появился бы второй.
async function makeCall(ctx, args){
  const {t, el} = ctx;
  const box = ctx.$("#makeDialog");
  box.innerHTML = "";
  box.append(el("div", {class: "card", style: "padding:14px"},
    el("span", {class: "spin"}),
    el("span", {style: "margin-left:8px"}, t("work.making"))));
  const res = await ctx.api("/api/run", {method: "POST", body: JSON.stringify(
    {project: ctx.project.path, cmd: "agent:make", args})});
  if (!res.job){ box.innerHTML = ""; return null; }
  let since = 0, lines = [];
  for (;;){
    const d = await ctx.api(`/api/job?id=${res.job}&since=${since}`, {quiet: true});
    if (!d || d.error){ box.innerHTML = ""; ctx.toast(t("work.job_lost"), "err"); return null; }
    lines = lines.concat(d.lines || []);
    since = d.next;
    if (d.done) break;
    await new Promise(ok => setTimeout(ok, 600));
  }
  return lines;
}

async function startMake(ctx){
  if (!ctx.project) return ctx.toast(ctx.t("work.pick_project"), "warn");
  const idea = ctx.$("#makeIdea").value.trim();
  if (!idea) return ctx.$("#makeIdea").focus();
  MAKE = null;
  const context = REFS.flatMap(r => ["--context", r.path]);
  const lines = await makeCall(ctx, ["--kind", ctx.$("#makeKind").value, "--idea", idea, ...context]);
  if (lines) drawMake(ctx, lines);
}

function drawMake(ctx, lines){
  const {t, el} = ctx;
  const box = ctx.$("#makeDialog");
  box.innerHTML = "";
  const text = (lines || []).join("\n");
  const sid = (text.match(/Сессия: `([^`]+)`/) || [])[1];              // данные движка
  if (sid) MAKE = {sid, kind: ctx.$("#makeKind").value};
  const body = text.split("\n").filter(l => !/^Артефакт: |^  /.test(l)).join("\n").trim();  // данные движка
  box.append(el("div", {class: "card", style: "padding:18px"},
    el("div", {class: "ansbody", style: "white-space:pre-wrap", html: ctx.fmt.mdLite(body)})));
  // Планировщик спрашивает — значит человеку есть что ответить, и поле для ответа должно
  // быть здесь же. «Хватит, работай» доступна с первого раунда: сколько раундов нужно
  // этой задаче, знает тот, кто её ставил.
  const done = (text.match(/Документ: `([^`]+)`/) || [])[1];           // данные движка
  if (done && /Момус|checked/.test(text) && !/Планировщик спрашивает/.test(text)){   // данные движка
    box.append(el("div", {class: "row", style: "gap:10px;margin-top:10px"},
      el("button", {class: "btn primary", onclick: () => ctx.openPath(done)},
        t("work.open_in_editor"))));
    // Открываем сами: человек просил, чтобы готовый документ сразу оказывался в
    // редакторе. Кнопка рядом — на случай, если он уже ушёл на другой экран.
    setTimeout(() => { if (ctx.view === "work") ctx.openPath(done); }, 400);
  }
  if (/Планировщик спрашивает/.test(text)){                            // данные движка
    const ans = el("textarea", {class: "btn", rows: 3, placeholder: t("work.answers_ph"),
      style: "width:100%;font-weight:400;margin-top:10px;resize:vertical"});
    box.append(ans, el("div", {class: "row", style: "gap:10px;margin-top:8px"},
      el("button", {class: "btn primary", onclick: async () => {
        const l = await makeCall(ctx, ["--session", MAKE.sid, "--answers", ans.value.trim()]);
        if (l) drawMake(ctx, l);
      }}, t("work.answer")),
      el("button", {class: "btn", title: t("work.enough_hint"), onclick: async () => {
        const l = await makeCall(ctx, ["--session", MAKE.sid, "--enough"]);
        if (l) drawMake(ctx, l);
      }}, t("work.enough"))));
  }
}

// Публикация: выбрать из готового, а не набирать путь руками — первая же опечатка ушла
// бы в Confluence чужой страницей. Уточняющий промпт остаётся заданием ассистенту:
// правку текста перед отправкой делает тот, кто её видит.
async function publish(ctx){
  const {t, el} = ctx;
  if (!ctx.project) return ctx.toast(t("work.pick_project"), "warn");
  const kind = ctx.$("#makeKind").value;
  const box = ctx.$("#makeDialog");
  box.innerHTML = "";
  const d = await ctx.api(`/api/artifacts?project=${encodeURIComponent(ctx.project.path)}`
                          + `&kind=${kind}`);
  const files = d.files || [];
  if (!files.length)
    return box.append(el("div", {class: "card", style: "padding:18px"}, t("work.publish_empty")));

  const sel = el("select", {class: "btn", style: "font-weight:400;min-width:300px"});
  files.forEach(f => {
    const o = document.createElement("option");
    o.value = f.rel;
    o.textContent = f.name + " · " + f.status
      + (f.published ? " · " + t("work.published_at", {when: f.published})
                     : " · " + t("work.never_published"));
    sel.append(o);
  });
  const note = el("textarea", {class: "btn", rows: 2, placeholder: t("work.publish_note_ph"),
    style: "width:100%;font-weight:400;margin-top:10px;resize:vertical"});
  const hint = el("div", {class: "muted", style: "font-size:12px;margin-top:6px"});
  const draw = () => {
    const f = files.find(x => x.rel === sel.value) || {};
    hint.textContent = f.status === "draft"
      ? t("work.publish_draft_warn")
      : (f.url ? t("work.publish_again", {url: f.url}) : t("work.publish_new"));
  };
  sel.onchange = draw;
  draw();

  box.append(el("div", {class: "card", style: "padding:18px"},
    el("div", {style: "font-weight:700;margin-bottom:8px"}, t("work.publish_title")),
    sel, hint, note,
    el("div", {class: "row", style: "gap:10px;margin-top:10px"},
      el("button", {class: "btn primary", onclick: async () => {
        const f = files.find(x => x.rel === sel.value) || {};
        if (f.status === "draft" && !confirm(t("work.publish_draft_ask"))) return;
        ctx.show("console");
        const res = await ctx.api("/api/run", {method: "POST", body: JSON.stringify(
          {project: ctx.project.path, cmd: "ship:publish", args: [f.rel, "--apply"]})});
        if (res.job) ctx.poll(res.job, 0, "ship:publish " + f.rel);
      }}, t("work.publish_go")),
      el("button", {class: "btn", onclick: () => {
        const f = files.find(x => x.rel === sel.value) || {};
        const task = t("work.publish_task", {rel: f.rel})
          + (note.value.trim() ? "\n" + t("work.publish_task_note", {note: note.value.trim()}) : "")
          + "\n" + t("work.publish_task_tail");
        navigator.clipboard.writeText(task)
          .then(() => ctx.toast(t("work.task_copied"), "ok"))
          .catch(() => ctx.toast(t("work.task_clipboard_off"), "warn"));
      }}, t("work.copy_task")))));
}

export default {mount, refresh};
