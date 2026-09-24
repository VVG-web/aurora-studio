/* Спросить базу — раздел-модуль.

   Вопрос своими словами: движок собирает контекст из карточек, модель отвечает только по
   ним. Карточки не правятся, но разговор ложится в базу проекта и уходит в git — что
   спрашивала команда и что база ответила, видно всем.

   Функции `fillBackends` и `renderHistory` экспортированы отдельно: их поведение
   проверяется прогоном в node на заглушках DOM и сервера (`tests/run_tests.py`). */

let ASKING = false;
let THREAD = null;          // открытый разговор: id и число вопросов в нём
let SHOWN = "";             // проект, для которого нарисован разговор

export function mount(ctx){
  ctx.root.dataset.module = "ask";

  ctx.$("#askGo").onclick = () => askBase(ctx);
  ctx.$("#askText").addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") askBase(ctx);
  });
  ctx.$("#askNew").onclick = () => {
    THREAD = null;
    ctx.$("#askBody").innerHTML = "";
    drawThreadHint(ctx);
    drawExport(ctx);
  };
  ctx.$("#askExport").onclick = () => exportMd(ctx);
  ctx.$("#askPing").onclick = e => pingPrimary(ctx, e.target);
  ctx.$("#askPrimary").onclick = async () => {
    const r = await ctx.api("/api/agent/retry-primary", {method: "POST", body: "{}"});
    if (r && r.ok) ctx.toast(r.note || ctx.t("ask.primary_note"), "ok");
  };
}

export async function refresh(ctx){
  // Разговор принадлежит проекту: открытым его нести в другой нельзя. Раньше это
  // делало ядро прямо в выборе проекта — знание о разговоре жило вне раздела.
  const project = ctx.project ? ctx.project.path : "";
  if (project !== SHOWN){
    SHOWN = project;
    THREAD = null;
    ctx.$("#askBody").innerHTML = "";
  }
  ctx.$("#askText").focus();
  await renderHistory(ctx);
  drawThreadHint(ctx);
  drawExport(ctx);
  await fillBackends(ctx);
}

async function askBase(ctx){
  const {t, el} = ctx;
  if (ASKING) return;
  if (!ctx.project) return ctx.toast(t("ask.pick_project"), "warn");
  const q = ctx.$("#askText").value.trim();
  if (!q) return ctx.$("#askText").focus();
  const mode = ctx.$("#askMode").value;
  const box = ctx.$("#askBody");
  ASKING = true;
  const args = ["--question", q, "--mode", mode];
  const pick = (ctx.$("#askBackend") || {}).value || "0";
  if (pick !== "0") args.push("--backend", pick);
  if (THREAD) args.push("--thread", THREAD.id);
  const item = el("div", {class: "card", style: "padding:18px;margin-top:14px"},
    el("div", {style: "font-weight:700"}, (THREAD ? t("ask.refine_prefix") : "") + q),
    el("div", {class: "muted", style: "font-size:12.5px;margin-top:4px"},
      THREAD ? t("ask.asking_thread") : t("ask.asking")));
  box.prepend(item);

  const res = await ctx.api("/api/run", {method: "POST", body: JSON.stringify(
    {project: ctx.project.path, cmd: "agent:ask", args})});
  if (!res.job){ ASKING = false; return; }
  let since = 0, lines = [];
  for (;;){
    const d = await ctx.api(`/api/job?id=${res.job}&since=${since}`, {quiet: true});
    lines = lines.concat(d.lines || []);
    since = d.next;
    if (d.done) break;
    await new Promise(ok => setTimeout(ok, 500));
  }
  ASKING = false;
  // Первые строки — служебная шапка отчёта; человеку нужен сам ответ и подпись под ним.
  // Строки про файл разговора — тоже служебные: сам файл панель показывает иначе.
  const body = lines.filter(l => !/^# Ответ базы|^\*\*Вопрос:|^Разговор: |^Уточнить, не/
                                  .test(l)).join("\n").trim();   // данные движка
  // Куда лёг разговор, движок печатает сам: панель не угадывает имя файла, а читает его.
  const said = (lines.find(l => /^Разговор: /.test(l)) || "").match(/`([^`]+)`/);  // данные движка
  if (said) THREAD = {id: said[1].split("/").pop().replace(/\.md$/, "")};
  item.innerHTML = "";
  // Исходный markdown держим на самой карточке: на экране он уже разобран в HTML, а
  // выгружать надо то, что писала модель, — со ссылками `[[…]]`, живыми в Obsidian.
  item.dataset.q = q;
  item.dataset.a = body;
  item.dataset.at = new Date().toLocaleString(ctx.lang === "en" ? "en-GB" : "ru-RU");
  item.append(el("div", {style: "font-weight:700"}, q),
    el("div", {class: "ansbody", style: "margin-top:10px;white-space:pre-wrap",
               html: ctx.fmt.mdLite(body)}));
  // Подпись ответа печатает сам движок: «модель: X (бэкенд №N) · N с». Берём оттуда, а не
  // из выбора в форме: по кольцу мог ответить не тот, кого просили, и человек должен это
  // видеть — иначе медленный запасной ответ выглядит как ответ основной модели.
  const who = (lines.find(l => /модель:/.test(l)) || "")                      // данные движка
    .match(/модель:\s*([^\s(·]+)[^)]*\(бэкенд №(\d+)\)/);                     // данные движка
  if (who){
    ctx.$("#askWho").textContent = t("ask.who_line", {model: who[1], n: who[2]})
      + (who[2] === "1" ? "" : t("ask.who_spare"));
    if (who[2] !== "1") ctx.toast(t("ask.spare_toast"), "warn");
  }
  ctx.$("#askText").value = "";
  drawThreadHint(ctx);
  drawExport(ctx);
  await renderHistory(ctx);
}

function drawThreadHint(ctx){
  const {t, el} = ctx;
  const hint = ctx.$("#askThreadHint"), btn = ctx.$("#askNew");
  if (!THREAD){
    hint.hidden = true;
    btn.hidden = true;
    ctx.$("#askGo").textContent = t("ask.go");
    ctx.$("#askText").placeholder = t("ask.placeholder");
    return;
  }
  hint.hidden = false;
  btn.hidden = false;
  ctx.$("#askGo").textContent = t("ask.refine");
  ctx.$("#askText").placeholder = t("ask.placeholder_thread");
  hint.innerHTML = "";
  hint.append(el("span", {}, t("ask.thread_before")),
              el("span", {class: "mono"}, THREAD.id),
              el("span", {}, t("ask.thread_after")));
}

// Ответ читают не только в панели: его несут в задачу, в письмо, в саму базу — а
// перенос выделением теряет ссылки `[[…]]` и переносы строк. Выгружаем тот markdown,
// который написала модель, целиком: вопрос, ответ и подпись, откуда он взялся.
function cards(ctx){
  return Array.from(ctx.$("#askBody").querySelectorAll("[data-a]"));
}

function drawExport(ctx){
  const btn = ctx.$("#askExport");
  if (btn) btn.hidden = !cards(ctx).length;
}

function markdown(ctx){
  const {t} = ctx;
  // Карточки на экране лежат свежим сверху; в файле разговор читают с начала.
  const rows = cards(ctx).reverse();
  const head = [t("ask.md_title"), "",
    t("ask.md_project", {path: ctx.project ? ctx.project.path : "—"}),
    t("ask.md_when", {when: new Date().toLocaleString(ctx.lang === "en" ? "en-GB" : "ru-RU")}),
    t("ask.md_model", {model: (ctx.$("#askWho").textContent || "—").replace(/^модель:\s*/, "")})];
  // Канонический экземпляр разговора лежит в базе и уходит в git. Файл выгрузки — копия
  // для переноса, и об этом надо сказать, иначе правки понесут в копию.
  if (THREAD) head.push(t("ask.md_thread", {id: THREAD.id}));
  head.push("");
  const body = rows.map(c => ["## " + c.dataset.q,
                              c.dataset.at ? "_" + c.dataset.at + "_" : "",
                              "", c.dataset.a, ""].join("\n"));
  return head.concat(body).join("\n").replace(/\n{3,}/g, "\n\n");
}

function exportMd(ctx){
  if (!cards(ctx).length) return ctx.toast(ctx.t("ask.export_empty"), "warn");
  const blob = new Blob([markdown(ctx)], {type: "text/markdown;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "aurora-otvet-" + (THREAD ? THREAD.id + "-" : "") + ctx.fmt.rtime() + ".md";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  ctx.toast(ctx.t("ask.exported"), "ok");
}

// Модель, которая отвечает, — не деталь настройки: у основной и запасной разные скорость
// и качество, и человек должен видеть, чей ответ читает. Здесь же — выбор модели вручную
// (когда сравнивают ответы), живая проверка основной и возврат на неё.
export async function fillBackends(ctx){
  const sel = ctx.$("#askBackend"), note = ctx.$("#askBackendNote");
  if (!sel) return;
  // Модели у каждого проекта свои: список, собранный для прошлого проекта, врёт о текущем.
  // И собранным он считается только после ответа сервера. Раньше отметка ставилась
  // до ответа, и один сбой запроса оставлял в выборе одно «по кольцу» до перезагрузки.
  const project = ctx.project ? ctx.project.path : "";
  if (sel.dataset.project === project) return;
  const say = msg => { if (note){ note.textContent = msg; note.hidden = !msg; } };
  let a;
  try {
    a = await ctx.api("/api/agent" + (project ? "?project=" + encodeURIComponent(project) : ""),
                      {quiet: true});
  } catch (e) { a = {error: String(e && e.message || e)}; }
  if (!a || a.error)
    return say(ctx.t("ask.models_failed", {why: (a && a.error) || ctx.t("ask.no_answer")}));
  const rows = a.backends || [], was = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  rows.forEach(b => {
    const worker = (b.models && b.models.worker) || b.model || "—";
    sel.append(ctx.el("option", {value: String(b.n)}, `№${b.n} · ${worker}`));
  });
  if ([...sel.options].some(o => o.value === was)) sel.value = was;
  if (rows.length) sel.dataset.project = project; else delete sel.dataset.project;
  say(rows.length ? "" : ctx.t("ask.models_empty"));
}

async function pingPrimary(ctx, btn){
  const {t} = ctx;
  const was = btn.textContent;
  btn.textContent = t("ask.ping_checking");
  btn.disabled = true;
  const r = await ctx.api("/api/agent/ping", {method: "POST", body: JSON.stringify(
    {project: ctx.project ? ctx.project.path : ""})});
  btn.textContent = was;
  btn.disabled = false;
  const first = (r && (r.backends || r.results) || [])[0];
  if (!first) return ctx.toast((r && r.error) || t("ask.ping_none"), "warn");
  const ok = first.ok !== false;
  ctx.$("#askWho").textContent = (ok ? t("ask.primary_answers") : t("ask.primary_silent"))
    + (first.model || "—");
  ctx.toast(ok ? t("ask.ping_ok", {model: first.model || ""})
               : t("ask.ping_fail", {why: first.error || first.why || t("ask.no_answer")}),
    ok ? "ok" : "warn");
}

export async function renderHistory(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#askHistory");
  if (!ctx.project) return box.replaceChildren(
    el("div", {class: "muted"}, t("ask.history_pick_project")));
  let d;
  try {
    d = await ctx.api("/api/ask/threads?project=" + encodeURIComponent(ctx.project.path),
                      {quiet: true});
  } catch (e) { d = {error: String(e && e.message || e)}; }
  // Сбой чтения — не «разговоров нет»: пустой список без причины человек принимает за
  // потерю истории и ищет её не там.
  if (!d || d.error) return box.replaceChildren(el("div", {class: "muted"},
    t("ask.history_failed", {why: (d && d.error) || t("ask.no_answer")})));
  const rows = d.threads || [];
  box.replaceChildren();
  if (!rows.length) return box.append(el("div", {class: "muted"}, t("ask.history_empty")));
  rows.forEach(thread => {
    const open = THREAD && THREAD.id === thread.id;
    box.append(el("div", {class: "card row",
        style: "padding:12px 14px;margin-top:8px;gap:12px;align-items:center;cursor:pointer"
               + (open ? ";outline:2px solid var(--primary)" : ""),
        onclick: () => openThread(ctx, thread.id)},
      el("div", {style: "flex:1;min-width:0"},
        el("div", {style: "font-weight:600"}, thread.title),
        el("div", {class: "muted", style: "font-size:12px;margin-top:2px"},
          t("ask.thread_turns", {turns: thread.turns, last: thread.last || "—",
                                 path: thread.path}))),
      el("button", {class: "btn"}, open ? t("ask.opened") : t("ask.open"))));
  });
}

async function openThread(ctx, id){
  const {t, el} = ctx;
  const d = await ctx.api(`/api/ask/thread?project=${encodeURIComponent(ctx.project.path)}`
                          + `&id=${encodeURIComponent(id)}`);
  if (!d || d.error) return ctx.toast(d && d.error || t("ask.thread_failed"), "warn");
  THREAD = {id: d.id};
  const box = ctx.$("#askBody");
  box.replaceChildren();
  // Свежий вопрос сверху — как в самой вкладке: последнее сказанное читают первым.
  d.turns.slice().reverse().forEach(turn => {
    const card = el("div", {class: "card", style: "padding:18px;margin-top:14px"},
      el("div", {style: "font-weight:700"}, turn.q),
      el("div", {class: "muted", style: "font-size:12px;margin-top:2px"}, ctx.fmt.when(turn.at)),
      el("div", {class: "ansbody", style: "margin-top:10px;white-space:pre-wrap",
                 html: ctx.fmt.mdLite(turn.a)}));
    card.dataset.q = turn.q;
    card.dataset.a = turn.a;
    card.dataset.at = ctx.fmt.when(turn.at);
    box.append(card);
  });
  drawThreadHint(ctx);
  drawExport(ctx);
  await renderHistory(ctx);
  ctx.$("#askText").focus();
}

export default {mount, refresh};
