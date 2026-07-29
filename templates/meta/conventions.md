# Knowledge DB Conventions (Aurora)

## Naming
- Filenames: domain language with hyphens for spaces (Status-Draft.md / Статус-Черновик.md)
- Titles in frontmatter: human-readable
- Aliases: English abbreviations, alternative terms, legacy IDs

## Tags
Taxonomy: `{domain}.{subdomain}.{concept}`
Examples (adapt to your domain):
- `process.workflow`
- `system.integration`
- `role.actor`
- `req.trace`

## Updates
- `created` — never changes after first write
- `updated` — timestamp of last content change
- `source` — original source file(s)

## Repository-wide naming rules

### Что вне схемы допустимо

Папка вне схемы допустима, **если она закрыта `.gitignore`**: такое не попадает в
репозиторий, значит не разъезжается между проектами (кэши линтеров, состояние
редакторов и инструментов, `node_modules`, локальные песочницы). `doctor --structure`
читает `.gitignore` и такие пути нарушением не считает. Всё, что едет в git, обязано
лежать по схеме — либо в `Workspaces/<задача>/`.

### Hard rules (entire repo)
1. No names that differ only by case (macOS/Windows case-insensitive).
2. Forbidden chars → `_`: `< > : " / \ | ? *`; name must not end with `.` or space.
   For Obsidian also avoid `# ^ [ ] %`.
3. Spaces → `_` in infrastructure; dates only ISO `YYYY-MM-DD` at the start of artifact names.
4. Prefix `_` only for service paths (`_index.md`, `_archive/`, `_assets/`, `_inbox/`).
5. Cyrillic allowed in content card names; infrastructure folders = Latin PascalCase.

### Folders
- Top-level trust layers & AuroraKnowledgeDB sections — PascalCase: `Sources`, `Raw`, `AuroraKnowledgeDB`,
  `Artifacts`, `Deliverables`, `Templates`, `Prompts`, `Concepts`, `Decisions`, …
- Category subfolders — lowercase: `laws`, `contract`, `meetings`, `reviews`, `work`, `released`.

### Files by type
- AuroraKnowledgeDB cards: normalize separators to hyphen; prefixes `REQ-NNN-`, `DR-NNNN-`,
  `SPEC-NNN-`, `Q-NNN-` (вопросы к заказчику в `Questions/`).
- Artifacts: `YYYY-MM-DD_<type>_<object>.md`; type ∈ {review, report, drift, garden,
  summary, context, draft-us, draft-ac}.
- Deliverables: work — `<DOC>_v<version>.md`; released — `<DOC>_v<version>_<YYYY-MM-DD>.md`.
- Templates: snake_case + `_template.md`; prompts: `<ACRONYM>_<action>.md`.

### Sources/ (synced — names belong to the sync)
- Keep source title; replace forbidden chars with `_`.
- Case-only collisions → append stable ID (Confluence pageId / Jira key).

### Reference (AuroraKnowledgeDB/Reference/)
- Living domain lists (`type: reference`): abbreviations, adjacent systems, roles.
- Not Raw (Raw is immutable evidence).

### Workspaces
- One large task = one folder. Content is **not** knowledge (not fed into LLM context by default).
- Finished workspaces → `Workspaces/_archive/`.
- Track actives in `Workspaces/README.md`.

## Таксономия артефактов (Artifacts/) и разграничение со знаниями

`Artifacts/<тип>/` — **произведённый документ** (черновик → ревью → публикация), НЕ знание.

**Список типов закрыт и одинаков во всех проектах Авроры** (источник правды —
`.opencode/structure_dirs.txt`). Свои типы в проекте не заводятся: `create <неизвестный тип>`
отказывает. Всё нестандартное — черновики, подборки, эксперименты, вспомогательные файлы,
картинки — живёт в `Workspaces/<задача>/`, где ограничений нет. Нужен новый тип всем
проектам → PR в kit (`structure_dirs.txt` + таблицы `SKILL.md`/`conventions.md` + CHANGELOG),
и он приезжает во все проекты через `aurora.py update`. Проверка факта:
`python3 .opencode/scripts/aurora_doctor.py --structure`.

Стандартные типы (**этот список — источник правды**; в SKILL.md на него ссылка):

| Artifacts/<тип> | Что это | Знаниевый двойник (атомарные verified-карточки) |
|---|---|---|
| `us/`, `ac/` | User Stories, Acceptance Criteria | Requirements/ (требования) |
| `algorithms/` | описания/спеки алгоритмов | Processes/ (карточки алгоритмов) |
| `dictionaries/` | произведённые глоссарии/списки терминов | Reference/ (живые справочники, питают контекст) |
| `screens/` | экранные формы, макеты, описания UI | Concepts/ + Systems/ (элементы UI как знание) |
| `contracts/` | контракты данных/интеграций (схемы, форматы обмена) | Systems/ (интеграции). **≠ `Raw/contract/`** — там юридический госконтракт (доказательство) |
| `mappings/` | маппинги данных между системами/форматами | Systems/ (интеграции) |
| `role-model/` | ролевая модель (матрица роли×права) для передачи | Roles/ (атомарные карточки ролей) |
| `diagrams/` | схемы как отдельный документ: сиквенс-диаграммы, flowchart, ERD, BPMN (mermaid) | Systems/ + Processes/ — там диаграмма живёт **внутри карточки** (инвариант «диаграмма как код»); бинарники — `_assets/` с карточкой-обёрткой |
| `acceptance/` | результаты приёмки и испытаний: вердикты по пунктам ПМИ, разбор замечаний заказчика (`covers`, `verdict`) | Requirements/ (`req_status: implemented` ставится только по пройденному пункту ПМИ); подписанный протокол — `Deliverables/released/` или `Raw/customer/` |

**Схемы отдельно:** инвариант «диаграмма как код» не отменяется — верифицированная схема живёт
mermaid-блоком *внутри карточки* Systems/Processes. `Artifacts/diagrams/` — для схем как
*самостоятельного документа*: черновик к обсуждению, схема для спеки/передачи, вариант «на подумать».
Когда схема стала истиной — она переезжает в карточку через verify-гейт, а не остаётся в артефактах.

**Правило:** артефакт — это *документ-результат* (публикуется в Confluence/Jira, попадает в
Deliverables); знаниевый двойник — *дистиллированная истина* атомарными карточками. Не дублируйте:
из одного и того же можно и произвести документ (Artifacts), и извлечь карточки (AuroraKnowledgeDB),
но документ не подаётся в context pack, а карточки — подаются по статусу доверия.
