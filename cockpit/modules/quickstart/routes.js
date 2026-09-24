/* Список маршрутов — общий экран двух разделов-близнецов.

   «Быстрый старт» показывает маршруты обслуживания базы, «Продуктивность» —
   производства. Разница между ними одна: группа сценариев. Код один, потому что и
   экран один: разойдись он на две копии, они начнут расходиться и в поведении.

   Раздел «Продуктивность» берёт этот файл импортом (`../quickstart/routes.js`) — это
   видимая зависимость близнеца, а не скрытая связь через ядро. Строки общие и живут в
   каталоге ядра (`routes.*`): переводить одно и то же дважды значит однажды перевести
   по-разному. */

// Какие маршруты человек развернул: решение принимает он, и оно должно пережить
// перерисовку от прихода здоровья или прогона команды.
const OPEN = new Set();

export async function renderRoutes(ctx, group, box){
  const {t, el} = ctx;
  // Сценарии читаем один раз, а рисуем каждый раз: отметки последнего запуска меняются
  // от прогонов и от смены проекта, поэтому кэшировать саму разметку нельзя.
  const d = await ctx.scenarios();
  box.innerHTML = "";
  // Каждый шаг маршрута — команда в проекте. Без выбранного проекта список кнопок
  // выглядит рабочим, но первая же из них скажет «сначала выберите проект».
  if (!ctx.project){
    box.append(el("div", {class: "card", style: "padding:24px"},
      el("div", {}, t("routes.pick_project")),
      el("button", {class: "btn sm primary", style: "margin-top:12px",
        onclick: () => ctx.show("overview")}, t("routes.to_bridge"))));
    return;
  }
  (d.scenarios || []).filter(sc => (sc.group || "база") === group)   // данные движка
    .forEach(sc => box.append(routeCard(ctx, sc)));
}

function routeCard(ctx, sc){
  const {t, el} = ctx;
  const auto = sc.steps.filter(s => !s.manual).length;
  const open = OPEN.has(sc.id);
  // Свёрнуто по умолчанию: человеку нужна кнопка, а не список из четырнадцати команд.
  // Разворачивает он сам, когда решил посмотреть, из чего маршрут состоит.
  const toggle = el("button", {class: "btn sm mono", title: t("routes.show_steps"),
    style: "flex:none;width:30px;justify-content:center"}, open ? "−" : "+");
  // «Привести базу в порядок» включает в себя остальные маршруты — её и надо нажимать
  // первой. «Пересобрать с нуля» сносит содержимое базы вместе с принятым доверием.
  const kind = sc.id === "all" ? "route-all" : sc.id === "rebuild" ? "route-danger" : "";
  const spent = sc.steps.filter(s => !s.manual)
    .map(s => (ctx.runs.last(s.cmd) || {}).secs || 0)
    .reduce((a, b) => a + b, 0);
  const card = el("div", {class: "card " + kind, style: "padding:20px;margin-bottom:16px"},
    el("div", {class: "row"}, toggle, el("b", {style: "font-size:15px"}, sc.title),
      sc.when ? el("span", {class: "chip"}, sc.when) : null,
      el("span", {class: "chip"}, auto === sc.steps.length
        ? t("routes.steps", {n: sc.steps.length})
        : t("routes.steps_auto", {auto, total: sc.steps.length})),
      spent ? el("span", {class: "chip", title: t("routes.est_hint")},
        t("routes.est", {time: ctx.fmt.howLong(spent)})) : null,
      kind === "route-all"
        ? el("span", {class: "chip accent", title: t("routes.all_in_one_hint")},
             t("routes.all_in_one")) : null,
      kind === "route-danger"
        ? el("span", {class: "chip warn", title: t("routes.danger_hint")},
             t("routes.danger")) : null,
      // Порядок шагов один и тот же всегда, флаги известны — нажимать их по одному
      // человеку незачем. Слева «посмотреть» (те же шаги без записи), справа проход.
      el("div", {class: "row-right"},
        auto ? el("button", {class: "btn sm", onclick: () => ctx.runRoute(sc, false)},
          t("routes.preview")) : null,
        auto ? el("button", {class: "btn sm " + (kind === "route-danger" ? "danger" : "primary"),
          onclick: () => ctx.runRoute(sc, true)}, t("routes.go")) : null)));

  const steps = el("div", {});
  steps.hidden = !open;
  toggle.onclick = () => {
    const now = !steps.hidden;
    steps.hidden = now;
    toggle.textContent = now ? "+" : "−";
    if (now) OPEN.delete(sc.id); else OPEN.add(sc.id);
  };
  sc.steps.forEach((st, i) => steps.append(stepRow(ctx, st, i)));
  card.append(steps);
  return card;
}

function stepRow(ctx, st, i){
  const {t, el} = ctx;
  const num = el("span", {class: "chip mono",
    style: "flex:none;width:26px;justify-content:center"}, String(i + 1));

  if (st.manual){
    return el("div", {class: "list-item"}, num,
      el("div", {style: "flex:1;min-width:0"},
        el("div", {style: "font-weight:600"}, st.title,
          el("span", {class: "chip", style: "margin-left:8px"}, t("routes.assistant"))),
        el("div", {class: "muted", style: "font-size:12.5px;margin-top:3px"}, st.why),
        st.skill ? ctx.ui.skillLine(st.skill) : null),
      st.skill ? ctx.ui.copyButton(st.skill)
               : el("span", {class: "muted", style: "font-size:12px"}, t("routes.not_run")));
  }

  const row = ctx.state.commands.find(c => c.cmd === st.cmd);
  // Команда есть в реестре, но исполняет её модель, а не скрипт: панель показывает,
  // что сказать ассистенту, вместо кнопки, которая всё равно ничего не запустит.
  if (row && !row.runnable){
    const say = "/aurora-vault " + st.cmd;
    return el("div", {class: "list-item"}, num,
      el("div", {style: "flex:1;min-width:0"},
        el("div", {class: "row", style: "gap:8px"},
          el("span", {class: "mono", style: "font-weight:700"}, st.cmd),
          el("span", {class: "chip"}, t("routes.assistant"))),
        el("div", {class: "muted", style: "font-size:13px;margin-top:4px"}, st.why),
        ctx.ui.skillLine(say)),
      ctx.ui.copyButton(say));
  }

  const last = ctx.runs.last(st.cmd), mark = last ? ctx.runs.mark(last.rc) : null;
  return el("div", {class: "list-item"}, num,
    el("div", {style: "flex:1;min-width:0"},
      el("div", {class: "row", style: "gap:8px"},
        el("span", {class: "mono", style: "font-weight:700"}, st.cmd),
        row && row.flags.includes("--apply")
          ? el("span", {class: "chip warn"}, t("routes.writes")) : null,
        st.flags && st.flags.length
          ? el("span", {class: "chip mono"}, st.flags.join(" ")) : null,
        mark ? el("span", {class: "chip " + mark.cls,
          title: t("routes.last_hint", {what: mark.what, rc: last.rc, kit: last.kit || "?"})
                 + (last.who ? t("routes.last_who", {who: last.who}) : "")},
          mark.what + " · " + ctx.fmt.when(last.at)) : null,
        // Сколько заняло в прошлый раз: у команд разброс от секунды до получаса,
        // и без этого числа «идёт» неотличимо от «повисло».
        last && last.secs ? el("span", {class: "chip", title: t("routes.took_hint")},
          t("routes.took", {time: ctx.fmt.howLong(last.secs)})) : null),
      el("div", {class: "muted", style: "font-size:13px;margin-top:4px"}, st.why)),
    row && row.runnable
      ? el("button", {class: "btn sm primary",
          onclick: () => ctx.openRun(st.cmd, st.flags || [])}, t("routes.run"))
      : el("span", {class: "chip"}, t("routes.no_command")));
}
