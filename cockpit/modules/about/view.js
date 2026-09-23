/* О проекте — раздел-модуль.

   Здесь же живёт обновление самой Aurora: одна кнопка, без git и ручных архивов. Кит
   узнаёт новую версию на GitHub сам, обновляется (клон — через git, установка из архива —
   новым архивом, заменённое — копией) и перезапускает панель. Человеку не показываем ни
   веток, ни коммитов: только «у вас такая, доступна такая, вот что нового». */

export function mount(ctx){
  ctx.root.dataset.module = "about";
}

export async function refresh(ctx){
  const {t, el} = ctx;
  const box = ctx.$("#aboutBody");
  box.innerHTML = '<span class="spin"></span>';
  const a = await ctx.api("/api/about");
  box.innerHTML = "";

  box.append(el("p", {class:"sub"}, t("about.what")));
  // Навыки — часть поставки, а не приложение к ней: одну инструкцию читают и модель
  // внутри панели, и ассистент в чате. Разойтись двум копиям негде, потому что копия одна.
  box.append(el("p", {class:"sub"}, t("about.skills")));

  box.append(el("div", {class:"grid metrics"},
    ctx.ui.metric(a.kit, t("about.kit_version"), t("about.panel", {v: a.ui}), "ok"),
    ctx.ui.metric(a.commands, t("about.commands"), t("about.commands_hint"), "ok"),
    ctx.ui.metric(a.commit || "—", t("about.commit"), a.commit_date || "", "ok"),
    ctx.ui.metric(a.license, t("about.license"), t("about.license_hint"), "ok")));

  box.append(el("h2", {}, t("about.repo")));
  const row = (label, node) => el("div", {class:"list-item"},
    el("span", {class:"chip", style:"flex:none"}, label), node);
  box.append(el("div", {class:"card"},
    row(t("about.address"), a.repo
      ? el("a", {href: a.repo, target:"_blank", rel:"noreferrer", style:"flex:1"}, a.repo)
      : el("span", {class:"muted", style:"flex:1"}, t("about.no_remote"))),
    row(t("about.branch"), el("span", {class:"mono", style:"flex:1"}, a.branch || "—")),
    row(t("about.author"), el("span", {style:"flex:1"}, a.author)),
    row(t("about.on_disk"),
      el("span", {class:"mono", style:"flex:1;word-break:break-all"}, a.path))));

  box.append(el("h2", {}, t("about.update_kit")));
  box.append(updateCard(ctx));

  if (a.releases && a.releases.length){
    box.append(el("h2", {}, t("about.releases")));
    const card = el("div", {class:"card"});
    a.releases.forEach(r => card.append(el("div", {class:"list-item"},
      el("span", {class:"mono", style:"font-weight:600;flex:none;width:70px"}, r.split(" ")[0]),
      el("div", {style:"flex:1"}, r.split(" ").slice(1).join(" ").replace(/^—\s*/, "")))));
    card.append(el("div", {class:"list-item"},
      el("button", {class:"btn sm", onclick: () => ctx.openDoc("CHANGELOG.md")},
        t("about.full_history"))));
    box.append(card);
  }
}

function updateCard(ctx){
  const {t, el} = ctx;
  const card = el("div", {class:"card", style:"padding:20px"});
  const draw = async (fresh) => {
    card.innerHTML = "";
    card.append(el("div", {class:"muted", style:"font-size:13px"},
      el("span", {class:"spin"}), " ", t("about.checking")));
    const st = await ctx.api("/api/kit/status" + (fresh ? "?fresh=1" : ""), {quiet:true})
      .catch(() => null);
    card.innerHTML = "";
    const again = el("button", {class:"btn sm", onclick: () => draw(true)}, t("about.check_again"));
    if (!st || st.error){
      card.append(el("p", {class:"muted", style:"font-size:13px;margin:0 0 12px"},
        (st && st.error) || t("about.no_answer")), again);
      return;
    }
    if (!st.newer){
      card.append(el("div", {class:"row"},
        el("span", {class:"chip ok"}, t("about.latest", {v: st.installed})),
        el("div", {class:"spacer"}), again));
      return;
    }
    card.append(el("div", {style:"font-size:18px;font-weight:600;margin-bottom:6px"},
      t("about.available", {v: st.latest, have: st.installed})));
    if (st.notes && st.notes.length){
      card.append(el("div", {class:"muted", style:"font-size:12px;margin:10px 0 4px"},
        t("about.whats_new")));
      st.notes.forEach(n => card.append(el("div", {style:"font-size:13px;margin:2px 0"}, "• " + n)));
    }
    card.append(el("p", {class:"muted", style:"font-size:13px;margin:12px 0"}, t("about.update_how")));
    const result = el("div", {});
    const go = el("button", {class:"btn gold"}, t("about.update_to", {v: st.latest}));
    if (st.busy && st.busy.length){
      go.disabled = true;
      result.append(el("div", {class:"warnbox"}, t("about.busy", {what: st.busy.join(", ")})));
    }
    go.onclick = async () => {
      if (!confirm(t("about.update_ask", {v: st.latest}))) return;
      go.disabled = true; go.textContent = t("about.updating");
      const r = await ctx.api("/api/kit/update", {method:"POST", body:"{}", quiet:true})
        .catch(() => null);
      if (!r || r.error){
        go.disabled = false; go.textContent = t("about.update_to", {v: st.latest});
        result.innerHTML = "";
        result.append(el("div", {class:"warnbox"}, (r && r.error) || t("about.no_answer")));
        return;
      }
      if (r.already){ ctx.toast(t("about.already"), "ok"); return draw(true); }
      // Итог показывает уже новая панель: эта сейчас перезапустится.
      try {
        localStorage.setItem("aurora-kit-updated",
          JSON.stringify({from: r.from, to: r.to, notes: r.notes || []}));
      } catch (e) { /* не сохранилось — обновление от этого не хуже */ }
      go.textContent = t("about.restarting");
      await ctx.restartPanel({patient: true, progress: text => { go.textContent = text; }});
    };
    card.append(el("div", {class:"row"}, go, el("div", {class:"spacer"}), again), result);
  };
  draw(false);
  return card;
}

export default {mount, refresh};
