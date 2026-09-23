/* Здоровье базы — раздел-модуль.

   Числа считает движок (`stats --json`, `lint`, `doctor`, `audit`), раздел только
   показывает. Каждая плитка кликабельна и ведёт к команде, которая с этим работает:
   метрика, из которой некуда пойти, бесполезна. */

export function mount(ctx){
  ctx.root.dataset.module = "health";
  const btn = ctx.$("#refreshHealth");
  btn.title = ctx.t("health.refresh_hint");
  btn.onclick = async () => {
    if (!ctx.project) return ctx.toast(ctx.t("health.pick_first"), "warn");
    const was = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "";
    btn.append(ctx.el("span", {class:"spin"}), " " + ctx.t("health.counting"));
    try {
      await ctx.reloadHealth();
      await refresh(ctx);
      const stamp = ctx.$("#healthStamp");
      stamp.textContent = ctx.t("health.updated", {
        time: new Date().toLocaleTimeString(ctx.lang === "en" ? "en-GB" : "ru-RU",
          {hour: "2-digit", minute: "2-digit"})});
      stamp.hidden = false;
      ctx.toast(ctx.t("health.recounted", {name: ctx.project.name}), "ok");
    } finally {
      btn.disabled = false;
      btn.innerHTML = was;
    }
  };
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#healthBody");
  ctx.$("#healthTitle").textContent = ctx.project
    ? t("health.title_of", {name: ctx.project.name}) : t("health.title");
  if (!ctx.project){
    box.innerHTML = "";
    box.append(el("div", {class:"card", style:"padding:24px"}, t("health.pick_project")));
    return;
  }
  // Ответ сам называет свой проект. Чужой не рисуем ни при каком порядке присваиваний:
  // замечание соседнего проекта под этим именем читается как поломка этого.
  const h = ctx.health;
  const foreign = h && h.project && h.project !== ctx.project.path;
  if (!h || foreign){
    box.innerHTML = '<div class="grid metrics">' + '<div class="skel"></div>'.repeat(8) + '</div>';
    return;
  }
  const st = h.stats || {};
  box.innerHTML = "";

  // Что мешает зелёному — прямо здесь, а не только подсказкой на Мостике: человек
  // приходит сюда именно с вопросом «я всё сделал, почему не зелёное».
  const why = ctx.aura(ctx.project);
  const colorName = why.color === "green" ? t("health.green")
                  : why.color === "red" ? t("health.red") : t("health.amber");
  box.append(el("div", {class:"card", style:"padding:16px;margin-bottom:14px"},
    el("div", {class:"row"},
      el("span", {class:"chip " + (why.color === "green" ? "ok" : why.color === "red" ? "bad" : "warn")},
        colorName),
      el("b", {}, why.color === "green" ? t("health.all_good") : t("health.to_green"))),
    ...why.todo.map(x => el("div", {class:"list-item"},
      el("span", {class:"chip " + (x.bad ? "bad" : "ok")}, x.bad ? "✗" : "✓"),
      el("div", {}, x.text))),
    el("div", {class:"muted", style:"font-size:12.5px;margin-top:8px"}, t("health.ratchet_note"))));

  box.append(baseCards(ctx, h, st));
  box.append(el("div", {class:"card", style:"padding:16px;margin-bottom:14px"},
    el("div", {style:"font-weight:700;margin-bottom:10px"}, t("health.statuses")),
    statusBar(ctx, st.statuses || {})));

  const ag = h.agent || {};
  const b = h.build || {};
  if (b.total && ag.file){
    box.append(el("div", {class:"card", style:"padding:12px 16px;margin-top:-4px"},
      el("div", {class:"list-item"},
        el("span", {class:"chip " + (ag.ok ? "ok" : "warn")},
          ag.ok ? t("health.agent_ok") : t("health.agent_partial")),
        el("div", {},
          el("div", {}, t("health.agent_last", {task: ag.task, why: ag.why || "—"})),
          el("div", {class:"muted", style:"font-size:12.5px;margin-top:3px"},
            "AuroraKnowledgeDB/meta/agent-runs/" + ag.file
            + (ag.left ? " · " + t("health.agent_left", {n: ag.left}) : ""))))));
  }

  const lintBad = h.lint.baseline !== null && h.lint.errors > h.lint.baseline;
  box.append(el("h2", {}, t("health.mechanics")));
  box.append(el("div", {class:"grid metrics"},
    ctx.ui.metric(h.lint.errors ?? "—", t("health.lint_errors"),
      h.lint.baseline !== null ? t("health.ratchet", {n: h.lint.baseline}) : t("health.no_baseline"),
      lintBad ? "bad" : (h.lint.errors ? "warn" : "ok"), () => ctx.openRun("kb:lint")),
    ctx.ui.metric(st.missing_source_count ?? "—", t("health.broken_sources"),
      t("health.broken_sources_sub"),
      st.missing_source_count ? "bad" : "ok", () => ctx.openRun("kit:remap-sources")),
    ctx.ui.metric(st.stubs ?? "—", t("health.stubs"), t("health.stubs_sub"),
      st.stubs ? "warn" : "ok", () => ctx.openRun("kb:repair"))));

  box.append(el("h2", {}, t("health.requirements")));
  box.append(el("div", {class:"grid metrics"},
    ctx.ui.metric(st.req_total ?? 0, t("health.req"), t("health.req_sub"), "ok",
      () => ctx.openRun("ops:trace")),
    ctx.ui.metric(st.req_agreed_no_jira ?? 0, t("health.req_no_jira"), t("health.req_no_jira_sub"),
      st.req_agreed_no_jira ? "warn" : "ok", () => ctx.openRun("sync:jira-status")),
    ctx.ui.metric(st.questions_open ?? 0, t("health.questions"),
      st.questions_overdue_count ? t("health.questions_overdue", {n: st.questions_overdue_count})
                                 : t("health.questions_sub"),
      st.questions_overdue_count ? "bad" : "warn"),
    ctx.ui.metric(st.specs?.total ?? 0, t("health.specs"), t("health.specs_sub"), "ok",
      () => ctx.openRun("make:spec-pack")),
    ctx.ui.metric(st.artifacts_with_based_on ?? 0, t("health.artifacts_based"),
      t("health.artifacts_of", {n: st.artifacts_total ?? 0}), "ok"),
    ctx.ui.metric((st.risky_deliverables || []).length, t("health.risky"),
      t("health.risky_sub"), (st.risky_deliverables || []).length ? "bad" : "ok")));

  box.append(el("h2", {}, t("health.readiness")));
  if (h.doctor.errors.length || h.doctor.warns.length){
    const card = el("div", {class:"card"});
    h.doctor.errors.forEach(e => card.append(el("div", {class:"list-item"},
      el("span", {class:"chip bad"}, t("health.blocker")), el("div", {}, e))));
    h.doctor.warns.forEach(w => card.append(el("div", {class:"list-item"},
      el("span", {class:"chip warn"}, t("health.warning")), el("div", {}, w))));
    box.append(card);
  } else {
    box.append(el("div", {class:"card",
      style:"padding:22px;display:flex;gap:12px;align-items:center"},
      el("span", {class:"chip ok"}, t("health.clean")),
      el("div", {}, t("health.doctor_clean"))));
  }
}

function baseCards(ctx, h, st){
  const {t, el} = ctx;
  const card = ctx.ui.metricCard;
  const lint = h.lint || {}, kinds = lint.kinds || {}, b = h.build || {};
  const k = st.kinds || {}, why = st.trust_why || {};
  const noKind = k["(нет kind)"] || 0;  // данные движка
  const orphans = kinds["карточки без связей"] || 0;  // данные движка
  const broken = kinds["битые ссылки"] || 0;  // данные движка
  // «Починить» зовём, только когда есть что чинить: последний шаг «Починить базу»
  // запоминает остаток, и то, что починке не по силам, ведёт к списку решений.
  const fresh = lint.fresh ?? lint.errors ?? 0;
  const trace = h.trace || {};
  const fixOrDecide = () => fresh ? ctx.ui.goRoute("fix", t("health.go_fix"))
                                  : ctx.ui.goCmd("ops:todo", [], t("health.go_decide"));
  return el("div", {class:"grid metrics", style:"margin-bottom:14px"},
    card({title: t("health.contents"), value: st.total ?? "—",
      sub: t("health.contents_sub", {knowledge: st.statuses?.knowledge ?? st.trusted ?? 0,
                                     drafts: st.statuses?.draft ?? 0}),
      hint: t("health.contents_hint")}),
    card({title: t("health.trust"), value: (st.pct_verified ?? 0) + "%",
      sub: Object.entries(why).filter(([n]) => n !== "доверенные")  // данные движка
             .map(([n, v]) => `${v} — ${n}`).join(" · ") || t("health.trust_why_old"),
      hint: t("health.trust_hint"),
      go: ctx.ui.goRoute("update", t("health.go_update"))}),
    // Пустой `kinds` — это «движок проекта старый и типов не считает», а не «у всех
    // проставлен». Разница между «не измеряли» и «в порядке» — та самая догадка,
    // выданная за факт, за которую мы уже платили дважды.
    card({title: t("health.kinds"),
      value: Object.keys(k).length ? (st.total ?? 0) - noKind : "—",
      sub: !Object.keys(k).length ? t("health.kinds_old")
           : noKind ? t("health.kinds_missing", {n: noKind}) : t("health.kinds_all"),
      tone: noKind ? "warn" : "",
      go: Object.keys(k).length && noKind
        ? ctx.ui.goCmd("kb:kind", ["--apply"], t("health.go_kinds")) : null}),
    card({title: t("health.links"), value: orphans + broken,
      sub: orphans ? t("health.links_both", {orphans, broken})
                   : broken ? t("health.links_broken", {n: broken}) : t("health.links_ok"),
      tone: (orphans + broken) ? "warn" : "",
      go: (orphans + broken) ? fixOrDecide() : null}),
    card({title: t("health.trace"),
      value: trace.direct != null ? (trace.direct + (trace.indirect || 0)) : "—",
      sub: trace.direct != null
        ? t("health.trace_sub", {direct: trace.direct, indirect: trace.indirect || 0,
                                 orphan: trace.orphan || 0})
        : t("health.trace_none"),
      go: ctx.ui.goCmd("ops:trace-table", ["--apply"], t("health.go_trace"))}),
    card({title: t("health.errors"), value: lint.errors ?? "—",
      sub: ((lint.errors || 0) && !fresh ? t("health.errors_stuck") + " · " : "")
           + (Object.entries(kinds).slice(0, 3).map(([n, v]) => `${v} ${n}`).join(" · ")
              || t("health.errors_none")),
      tone: (lint.errors || 0) ? "warn" : "",
      go: (lint.errors || 0) ? fixOrDecide() : null}),
    card({title: t("health.freshness"), value: b.left != null ? b.left : "—",
      sub: b.left != null ? t("health.freshness_sub", {total: b.total, pct: b.pct})
                          : t("health.freshness_none"),
      tone: (b.left || 0) ? "warn" : "",
      go: (b.left || 0) ? ctx.ui.goRoute("update", t("health.go_update")) : null}),
    // Связь и индекс — здоровье не базы, а того, чем она обслуживается. Без них
    // «поиск по смыслу» тихо вырождается в поиск по словам, и заметить это нельзя.
    card({title: t("health.models"),
      value: (h.ping && h.ping.when) ? `${h.ping.alive}/${h.ping.alive + h.ping.dead}` : "—",
      sub: (h.ping && h.ping.when)
        ? t("health.models_when", {when: ctx.fmt.when(h.ping.when)})
          + (h.ping.dead ? " · " + t("health.models_dead", {n: h.ping.dead}) : "")
        : t("health.models_none"),
      tone: (h.ping && h.ping.dead) ? "warn" : "",
      hint: t("health.models_hint"),
      go: {label: t("health.go_ping"), act: async () => {
        ctx.toast(t("health.pinging"), "ok");
        const r = await ctx.api("/api/agent/ping?project=" + encodeURIComponent(ctx.project.path));
        h.ping = r;
        await refresh(ctx);
        ctx.toast(r.dead ? t("health.ping_dead", {n: r.dead}) : t("health.ping_ok"),
          r.dead ? "warn" : "ok");
      }}}),
    card({title: t("health.index"),
      value: (h.index && h.index.built) ? (h.index.missing + h.index.stale) : "—",
      sub: !(h.index && h.index.built) ? t("health.index_none")
        : t("health.index_sub", {missing: h.index.missing, stale: h.index.stale,
                                 model: h.index.model}),
      tone: (h.index && h.index.built && (h.index.missing + h.index.stale)) ? "warn" : "",
      hint: t("health.index_hint"),
      go: ctx.ui.goCmd("kb:embed", ["--apply"], t("health.go_index"))}),
    // Ранжирование — то, на чём стоит и ответ базы, и обогащение перед производством.
    // Менять его вслепую нельзя, а «стало лучше» — не проверка.
    card({title: t("health.retrieval"),
      value: (h.retrieval && h.retrieval.when) ? h.retrieval.queries : "—",
      sub: (h.retrieval && h.retrieval.when)
        ? t("health.retrieval_sub", {when: ctx.fmt.when(h.retrieval.when)})
        : t("health.retrieval_none"),
      hint: t("health.retrieval_hint"),
      go: ctx.ui.goCmd("ops:retrieval", [], t("health.go_retrieval"))}),
    // Файл артефакта рождается сразу после обогащения — значит брошенная работа
    // остаётся видимой. Удалять её движок не должен: срок автоудаления никто не
    // подберёт правильно, а потеря необратима.
    card({title: t("health.unfinished"),
      value: (h.unfinished && h.unfinished.count) || 0,
      sub: (h.unfinished && h.unfinished.count)
        ? t("health.unfinished_sub", {days: h.unfinished.oldest})
        : t("health.unfinished_none"),
      tone: (h.unfinished && h.unfinished.count) ? "warn" : "",
      hint: t("health.unfinished_hint"),
      go: (h.unfinished && h.unfinished.count)
        ? {label: t("health.go_list"), act: () => showList(ctx,
            t("health.unfinished_title"), t("health.unfinished_about"),
            h.unfinished.items.map(x => el("div", {class:"list-item"},
              el("span", {class:"chip"}, x.kind),
              el("div", {style:"flex:1"},
                el("div", {class:"mono", style:"font-size:12.5px"}, x.path),
                el("div", {class:"muted", style:"font-size:12px"},
                  t("health.unfinished_at", {stopped: x.stopped, days: x.days}))),
              // Список без перехода — это отчёт, а не находка: человек всё равно
              // пойдёт искать файл руками.
              el("button", {class:"btn", onclick: () => ctx.openPath(x.path)},
                t("health.open")))))} : null}),
    card({title: t("health.corrections"),
      value: (h.corrections && h.corrections.count) || 0,
      sub: (h.corrections && h.corrections.ask)
        ? t("health.corrections_ask", {n: h.corrections.ask})
        : t("health.corrections_sub"),
      tone: (h.corrections && h.corrections.ask) ? "warn" : "",
      go: (h.corrections && h.corrections.ask)
        ? {label: t("health.go_corrections"), act: () => showList(ctx,
            t("health.corrections_title"), t("health.corrections_about"),
            h.corrections.items.map(x => el("div", {class:"list-item"},
              el("div", {style:"flex:1"},
                el("div", {class:"mono", style:"font-size:12.5px"}, x.name),
                el("div", {class:"muted", style:"font-size:12px"},
                  t("health.corrections_card", {card: x.card}))),
              el("button", {class:"btn", onclick: () => ctx.openPath(
                "Raw/corrections/" + x.name + ".md")}, t("health.open_correction")))))} : null}),
    card({title: t("health.left_to_human"), value: h.todo != null ? h.todo : "—",
      sub: t("health.left_sub"),
      go: ctx.ui.goCmd("ops:todo", [], t("health.go_list"))}));
}

// Разворачиваемый список поверх плиток: находка, а не отдельный экран.
function showList(ctx, title, about, rows){
  const {el} = ctx;
  const box = ctx.$("#healthBody");
  const list = el("div", {class:"card", style:"padding:18px;margin-bottom:14px"},
    el("h2", {style:"margin-top:0"}, title),
    el("div", {class:"muted", style:"font-size:12.5px;margin-bottom:10px"}, about),
    ...rows);
  box.prepend(list);
  list.scrollIntoView({behavior:"smooth", block:"center"});
}

function statusBar(ctx, statuses){
  const {el} = ctx;
  const order = ["verified", "in-review", "draft", "imported", "deprecated"];
  const color = {verified:"--tier-verified", "in-review":"--tier-inreview", draft:"--tier-draft",
    imported:"--tier-imported", deprecated:"--tier-deprecated"};
  const total = Object.values(statuses).reduce((a, b) => a + b, 0) || 1;
  const bar = el("div", {style:"display:flex;height:12px;border-radius:8px;overflow:hidden;"
    + "background:var(--surface-2)"});
  const legend = el("div", {class:"row", style:"margin-top:12px"});
  const keys = [...order.filter(k => statuses[k]),
                ...Object.keys(statuses).filter(k => !order.includes(k))];
  keys.forEach(k => {
    const v = statuses[k];
    if (!v) return;
    const c = color[k] ? `var(${color[k]})` : "var(--tier-imported)";
    bar.append(el("div", {style:`width:${v / total * 100}%;background:${c}`, title:`${k}: ${v}`}));
    legend.append(el("span", {class:"chip tier"},
      el("span", {class:"dot", style:`background:${c}`}), `${k} · ${v}`));
  });
  return el("div", {}, bar, legend);
}

export default {mount, refresh};
