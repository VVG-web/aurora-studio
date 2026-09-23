/* Справка — раздел-модуль.

   Оглавление здесь, а не на сервере: список документов — часть интерфейса, а не данных.
   Любой раздел открывает документ через `ctx.openDoc(path)`; ядро переводит это в
   переход сюда с именем файла. */

// Что показываем и в каком порядке. Заголовки — в каталоге строк, пути — данные.
const DOCS = [
  ["docs/readme/README.md", "reference.doc.start"],
  ["docs/readme/01-overview.md", "reference.doc.overview"],
  ["docs/readme/02-quickstart.md", "reference.doc.quickstart"],
  ["docs/readme/03-team-rules.md", "reference.doc.team"],
  ["docs/readme/04-practice.md", "reference.doc.practice"],
  ["docs/readme/05-gardening.md", "reference.doc.gardening"],
  ["docs/readme/06-sdd.md", "reference.doc.sdd"],
  ["skills/aurora-vault/SKILL.md", "reference.doc.skill"],
  ["docs/commands.md", "reference.doc.commands"],
  ["docs/roadmap.md", "reference.doc.roadmap"],
  ["skills/aurora-vault/references/workflows.md", "reference.doc.workflows"],
  ["skills/aurora-vault/references/maintenance.md", "reference.doc.maintenance"],
  ["skills/aurora-vault/references/frontmatter.md", "reference.doc.frontmatter"],
  ["skills/aurora-vault/references/retrieval.md", "reference.doc.retrieval"],
  ["skills/aurora-vault/references/build.md", "reference.doc.build"],
  ["skills/aurora-vault/references/migration.md", "reference.doc.migration"],
  ["docs/control-panel-ui-requirements.md", "reference.doc.panel"],
  ["CHANGELOG.md", "reference.doc.changelog"],
];

export function mount(ctx){
  ctx.root.dataset.module = "reference";
  const list = ctx.$("#docList");
  list.innerHTML = "";
  for (const [path, key] of DOCS)
    list.append(ctx.el("button", {class:"list-item", style:"width:100%;text-align:left",
      onclick: () => open(ctx, path)},
      ctx.el("div", {},
        ctx.el("div", {style:"font-weight:600;font-size:13.5px"}, ctx.t(key)),
        ctx.el("div", {class:"mono", style:"font-size:11px;color:var(--text-muted);"
          + "overflow:hidden;text-overflow:ellipsis;white-space:nowrap"}, path))));
}

export async function refresh(ctx, payload){
  // Пришли из другого раздела за конкретным документом — открываем его сразу.
  if (payload && payload.doc) await open(ctx, payload.doc);
}

async function open(ctx, path){
  const body = ctx.$("#docBody");
  body.innerHTML = '<span class="spin"></span>';
  const d = await ctx.api("/api/doc?path=" + encodeURIComponent(path));
  body.innerHTML = d.text ? ctx.fmt.md(d.text)
                          : ctx.t("reference.failed", {path: ctx.fmt.esc(path)});
  body.scrollIntoView({block:"start"});
}

export default {mount, refresh};
