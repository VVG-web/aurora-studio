/* Отчёты — первый раздел, переехавший из монолита в папку.

   Модуль знает про панель ровно то, что даёт ему ctx: строки, запросы к серверу, консоль
   и выбранный проект. Ни одного обращения к глобальным переменным панели здесь нет —
   именно поэтому раздел можно выключить, заменить или написать свой. */

export function mount(ctx){
  // Разметка уже на месте: раздел статичный, подписываться не на что.
  ctx.root.dataset.module = "reports";
}

export async function refresh(ctx){
  const box = ctx.$("#reportsBody");
  if (!ctx.project){
    box.innerHTML = "";
    box.append(ctx.el("div", {class:"card", style:"padding:24px"}, ctx.t("reports.pick_project")));
    return;
  }
  box.innerHTML = '<div class="grid metrics"><div class="skel"></div></div>';
  const d = await ctx.api("/api/report?project=" + encodeURIComponent(ctx.project.path));
  box.innerHTML = "";
  if (d.error) return;
  for (const r of (d.reports || [])) box.append(card(ctx, r));
}

function card(ctx, r){
  const {t, el, fmt} = ctx;
  const built = r.output.exists;
  const box = el("div", {class:"card", style:"padding:18px"});
  box.append(el("div", {class:"row", style:"justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap"},
    el("h2", {style:"font-size:16px"}, r.title),
    el("span", {class:"chip"}, `${r.project} · ${r.year}`)));

  box.append(el("div", {class:"muted", style:"font-size:13px;margin:6px 0 14px"},
    built ? t("reports.built", {when: fmt.ago(r.output.mtime), size: fmt.kb(r.output.size),
                                path: r.output.path})
          : t("reports.never")));

  // Чего не хватает — говорим до нажатия, а не ошибкой в консоли после.
  const gaps = [];
  if (!r.roster.exists) gaps.push(t("reports.gap_roster", {path: r.roster.path}));
  if (!r.events.exists) gaps.push(t("reports.gap_events", {path: r.events.path}));
  if (!r.cached) gaps.push(t("reports.gap_cache"));
  if (gaps.length)
    box.append(el("ul", {class:"muted", style:"font-size:12.5px;margin:0 0 14px 18px"},
      gaps.map(g => el("li", {}, g))));

  const row = el("div", {class:"row", style:"gap:10px;flex-wrap:wrap"});
  row.append(el("button", {class:"btn primary", onclick: () => build(ctx, r, false)},
    built ? t("reports.rebuild") : t("reports.build")));
  // Пересчёт по уже выгруженному: правка ростера или событий не требует похода в Jira,
  // а поход занимает минуты.
  if (r.cached)
    row.append(el("button", {class:"btn", title: t("reports.recalc_hint"),
      onclick: () => build(ctx, r, true)}, t("reports.recalc")));
  if (built)
    row.append(el("a", {class:"btn", target:"_blank",
      href: `/api/report/file?project=${encodeURIComponent(ctx.project.path)}`
          + `&id=${encodeURIComponent(r.id)}&t=${encodeURIComponent(ctx.token)}`},
      t("reports.open")));
  box.append(row);

  // История версий. Отчёт собирается в один и тот же файл, и каждая сборка затирает
  // прежний: ошибка в выгрузке или в ростере — и сравнить показатели с прошлой неделей
  // уже не с чем. Копия делается сама при взгляде на вкладку, потому что отчёт собирают
  // и кнопкой, и маршрутом, и из терминала.
  const hist = r.history || [];
  if (hist.length){
    const list = el("div", {style:"margin-top:6px"});
    const draw = () => {
      list.innerHTML = "";
      for (const v of hist){
        list.append(el("div", {class:"list-item"},
          el("div", {style:"flex:1"},
            el("span", {class:"mono", style:"font-size:12.5px"}, fmt.when(v.when)),
            el("span", {class:"muted", style:"font-size:12px;margin-left:8px"}, fmt.kb(v.size))),
          el("a", {class:"btn", target:"_blank",
            href: `/api/report/file?project=${encodeURIComponent(ctx.project.path)}`
                + `&id=${encodeURIComponent(r.id)}&stamp=${encodeURIComponent(v.stamp)}`
                + `&t=${encodeURIComponent(ctx.token)}`}, t("reports.open")),
          el("button", {class:"btn", onclick: async () => {
            if (!confirm(t("reports.delete_ask", {when: fmt.when(v.when)}))) return;
            const res = await ctx.api("/api/report/forget", {method:"POST", body: JSON.stringify(
              {project: ctx.project.path, id: r.id, stamp: v.stamp})});
            if (!res.error){ hist.splice(hist.indexOf(v), 1); draw(); ctx.toast(t("reports.deleted")); }
          }}, t("reports.delete"))));
      }
    };
    draw();
    box.append(el("details", {style:"margin-top:12px"},
      el("summary", {class:"sub", style:"cursor:pointer"}, t("reports.history", {n: hist.length})),
      el("div", {class:"muted", style:"font-size:12px;margin:6px 0"}, t("reports.history_about")),
      list));
  }
  return box;
}

async function build(ctx, r, skipFetch){
  // Сборка идёт минутами (первая — с походом в Jira), и смотреть на неё человек будет
  // в консоли: этим занимается ядро, одинаково для всех разделов.
  const res = await ctx.runWatched(r.cmd, skipFetch ? ["--skip-fetch"] : []);
  if (res.busy) return;
  if (res.rc === 0) ctx.toast(ctx.t("reports.done"), "ok");
  // Человек остался в консоли и смотрит вывод — возвращать его силой нельзя; раздел
  // обновляем молча, чтобы к возвращению он уже показывал свежую сборку.
  await refresh(ctx);
}

export default {mount, refresh};
