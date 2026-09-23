/* О проекте — раздел-модуль.

   Здесь же живёт обновление самого кита из репозитория: только перемотка вперёд и
   только по чистому дереву. Слияние с чужими правками кнопкой в браузере — не то, что
   стоит делать вслепую. */

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
  card.append(el("p", {class:"muted", style:"font-size:13px;margin:0 0 12px"}, t("about.update_why")));
  const status = el("div", {class:"row", style:"margin-bottom:12px"},
    el("span", {class:"muted", style:"font-size:13px"}, t("about.not_checked")));
  const incoming = el("div", {});

  const pull = el("button", {class:"btn gold", disabled:""}, t("about.pull"));
  pull.onclick = async () => {
    if (!confirm(t("about.pull_ask"))) return;
    pull.disabled = true; pull.textContent = t("about.pulling");
    const r = await ctx.api("/api/kit/update", {method:"POST", body:"{}"});
    pull.textContent = t("about.pull");
    if (r.ok){
      ctx.toast(r.already ? t("about.already") : t("about.updated", {v: r.version}), "ok");
      await refresh(ctx);
    }
  };

  const check = el("button", {class:"btn primary"}, t("about.check"));
  check.onclick = async () => {
    check.disabled = true; check.textContent = t("about.checking");
    const st = await ctx.api("/api/kit/status");
    check.disabled = false; check.textContent = t("about.check");
    status.innerHTML = ""; incoming.innerHTML = "";
    if (st.error) return status.append(el("span", {class:"chip bad"}, st.error));
    status.append(
      el("span", {class:"chip"}, t("about.branch_is", {branch: st.branch})),
      el("span", {class:"chip " + (st.behind ? "warn" : "ok")},
        st.behind ? t("about.behind", {n: st.behind}) : t("about.current")),
      st.dirty ? el("span", {class:"chip bad"}, t("about.dirty", {n: st.dirty})) : null,
      st.ahead ? el("span", {class:"chip"}, t("about.ahead", {n: st.ahead})) : null);
    if (st.incoming && st.incoming.length){
      incoming.append(el("div", {class:"muted", style:"font-size:12px;margin:10px 0 4px"},
        t("about.incoming")));
      st.incoming.forEach(l => incoming.append(
        el("div", {class:"mono", style:"font-size:12px;color:var(--text-muted)"}, l)));
    }
    // Обновляемся только вперёд и только по чистому дереву: иначе кнопка обещает то,
    // чего сделать не сможет.
    pull.disabled = !st.behind || !!st.dirty;
  };

  card.append(status, incoming, el("div", {class:"row"}, check, pull));
  return card;
}

export default {mount, refresh};
