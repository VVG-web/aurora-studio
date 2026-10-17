/* Версия и миграция — раздел-модуль.

   Панель здесь ничего не делает сама: она показывает две версии и открывает окно
   запуска. Обновление — всегда предпросмотр, и только потом запись: `--apply` панель
   не подставляет. */

export function mount(ctx){
  ctx.root.dataset.module = "version";
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#versionBody");
  box.innerHTML = "";
  const p = ctx.project;
  if (!p){
    box.append(el("div", {class:"card", style:"padding:24px"}, t("version.pick_project")));
    return;
  }
  box.append(el("div", {class:"card", style:"padding:20px"},
    el("div", {class:"row"},
      el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("version.in_project")),
        el("div", {class:"mono", style:"font-size:24px;font-weight:700"}, p.engine)),
      el("div", {style:"font-size:22px;color:var(--text-muted)"}, "→"),
      el("div", {}, el("div", {class:"muted", style:"font-size:12px"}, t("version.in_kit")),
        el("div", {class:"mono", style:"font-size:24px;font-weight:700;color:var(--gold)"}, p.kit)),
      el("div", {class:"spacer"}),
      p.behind ? el("span", {class:"chip warn"}, t("version.behind"))
               : el("span", {class:"chip ok"}, t("version.current"))),
    el("div", {class:"row", style:"margin-top:18px"},
      el("button", {class:"btn primary", onclick: () => ctx.openRun("kit:update")},
        t("version.preview")),
      el("button", {class:"btn sm", onclick: () => ctx.openDoc("CHANGELOG.md")},
        t("version.whats_new")))));

  box.append(engineCard(ctx, p));

  box.append(el("div", {class:"warnbox"}, t("version.warn")));

  box.append(el("h2", {}, t("version.migration")));
  const row = (cmd, what, flags) => el("div", {class:"list-item"},
    el("span", {class:"chip mono"}, cmd + (flags ? " " + flags.join(" ") : "")),
    el("div", {style:"flex:1"}, what),
    el("button", {class:"btn sm", onclick: () => ctx.openRun(cmd, flags || [])},
      t("version.open")));
  // Третьей строкой здесь звали `kb:retire` — команду, которой нет в реестре с 1.45.0.
  // Кнопка открывала окно запуска несуществующей команды и молчала об этом.
  box.append(el("div", {class:"card"},
    row("kit:update", t("version.engine_only"), ["--engine-only"]),
    row("kit:update", t("version.structure_only"), ["--structure-only"]),
    row("kit:doctor", t("version.structure"), ["--structure"])));
}

// Движок на этом компьютере: `.aurora/` вне git, клон проекта приходит без неё. Состояние считает
// сервер (`engine_state` в карточке проекта); кнопка — то же действие, что у полосы ядра.
function engineCard(ctx, p){
  const {t, el} = ctx;
  const es = p.engine_state || {state: "unknown"};
  const chips = {ok: ["ok", "version.engine_ok"], missing: ["warn", "version.engine_missing"],
                 incomplete: ["warn", "version.engine_incomplete"], stale: ["warn", "version.engine_stale"],
                 unknown: ["", "version.engine_unknown"]};
  const [cls, label] = es.legacy ? ["warn", "version.engine_legacy"] : (chips[es.state] || chips.unknown);
  const fix = es.state === "stale" ? "version.engine_update" : "version.engine_prepare";
  const need = !es.legacy && ["missing", "incomplete", "stale"].includes(es.state);
  const done = p.engine_prepared;
  return el("div", {class:"card", style:"padding:20px;margin-top:16px"},
    el("div", {class:"row"},
      el("b", {}, t("version.engine_title")), el("div", {class:"spacer"}),
      el("span", {class:"chip " + cls}, t(label))),
    el("p", {class:"muted", style:"font-size:13px;margin:8px 0 0"}, t("version.engine_about")),
    es.state === "incomplete" || es.state === "missing"
      ? el("p", {class:"mono", style:"font-size:12px;margin:8px 0 0"},
          t("version.engine_missing_n", {n: es.missing, total: es.total})
          + (es.files && es.files.length ? ": " + es.files.join(", ") + (es.missing > es.files.length ? " …" : "") : ""))
      : null,
    es.state === "stale" && !es.legacy
      ? el("p", {style:"font-size:13px;margin:8px 0 0"}, t("version.engine_old", {copy: es.copy || "—", kit: p.kit})) : null,
    es.legacy ? el("p", {style:"font-size:13px;margin:8px 0 0"}, t("version.engine_legacy_about")) : null,
    done ? el("p", {style:"font-size:13px;margin:8px 0 0"},
      t("version.engine_done", {created: done.created, refreshed: done.refreshed})
      + (done.files && done.files.length ? " " + t("version.engine_done_files", {files: done.files.join(", ")}) : "")
      + (done.ignored === false ? " " + t("version.engine_not_ignored") : "")) : null,
    need ? el("div", {class:"row", style:"margin-top:14px"},
      el("button", {class:"btn primary", onclick: () => prepare(ctx, p)}, t(fix))) : null);
}

// Страница, открытая до обновления кита, ещё не знает `ctx.prepareEngine`: тогда прямой запрос.
async function prepare(ctx, p){
  if (typeof ctx.prepareEngine === "function") return ctx.prepareEngine();
  const d = await ctx.api("/api/engine/prepare", {method: "POST", body: JSON.stringify({project: p.path})});
  if (d && d.ok) ctx.toast(ctx.t("version.engine_done", {created: d.created, refreshed: d.refreshed}));
  return d;
}

// refresh только рисует две версии проекта; побочных действий нет — скрытому не нужен.
export const lifecycle = 1;
export default {mount, refresh};
