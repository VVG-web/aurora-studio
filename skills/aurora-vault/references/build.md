# build — extract knowledge cards from sources

> **План и учёт ведёт скрипт.** `python3 .opencode/scripts/build_plan.py` показывает, что
> осталось, режет работу на партии по бюджету и порядку обхода (ниже), помнит обработанное
> по хешам в `meta/manifest.json`. После каждого источника —
> `build_plan.py --done <файл> --cards N`: так заход возобновляется с места остановки,
> а не начинается заново. Модели остаётся то, ради чего она нужна: понять текст и выделить
> атомарные темы по правилам этого файла.

## Как начинается работа

Человек запускает `build_plan.py --partition N` и приносит напечатанный блок «ЗАДАНИЕ
АССИСТЕНТУ»: там список файлов партии по порядку и правила жизненного цикла. Список брать
оттуда, а не выбирать самому — порядок обхода не случаен. Если блока нет, спросите номер
партии и напечатайте план сами: `python3 .opencode/scripts/build_plan.py --partition N`.

Закончив партию, запустите `kb:links --cards`: связи между карточками выводятся из ключей
Requirement Yogi и номеров историй, руками их дублировать не нужно. Затем `kb:moc` —
карты содержания по группировкам и список карточек, на которые никто не ссылается.
Карточка без единого входа знанием не работает: её не найдут ни по связям, ни глазами.

Порядок после извлечения одинаков для всех проектов:
`kb:links --cards` → `kb:moc --apply` → `kb:index` → `kb:lint`.

Reference for the `build` command of the aurora skill.
Turns project documentation into a living knowledge base of atomic, interconnected notes.

## Lifecycle rules (override anything below that contradicts them)

1. **Every note created or updated by `build` gets `status: imported`** in frontmatter
   (plus `source_synced: <today>`). Human review promotes it later (see `workflows.md`).
2. **Never silently overwrite a note whose `status` is `verified` or `deprecated`.**
   Правка проверенной карточки человеком — норма: картотека живёт, и запрещать
   дописывать её значило бы превратить базу в архив. Запрещено другое — **молча
   переписать машиной то, что человек читал и подтвердил**. Отсюда правило: тело
   `verified`-карточки машина не трогает, новое из источника кладёт отдельной секцией
   `## Из источника (не проверено)` и пишет `DRIFT` в отчёт. Человек либо принимает
   дописанное (`kb:verify --refresh`), либо разводит знание по карточкам.
   Статус относится к **тексту**, а не к файлу: если тело изменилось после приёмки,
   отпечаток `verified_hash` расходится, и `kb:lint` это показывает — не запрещая правку,
   но и не давая `verified` тихо соврать.
   If the source changed and the extracted content differs from such a note:
   - do NOT modify the note body;
   - refresh `source_synced` and append the conflict to the run report:
     `⚠️ DRIFT: <note> — source changed since verification (<source path>)`;
   - the owner re-verifies via the `diff`/`verify` workflow.
   Notes with `status: imported`/`draft` (or legacy notes with no `status` field) may be
   updated in place as before.
3. Legacy notes without a `status` field are treated as `status: imported, trust: medium`.
   When touching such a note for any reason, add the missing lifecycle fields
   (see `frontmatter.md`).

## Why This Matters

Project knowledge gets scattered across Confluence pages, dictionaries, process docs, specs, and diagrams. 
A Zettelkasten approach extracts each concept into its own note, links them together, and maintains a 
navigable index — so anyone can find what they need without hunting through folders.

## Output Structure

```
AuroraKnowledgeDB/
├── index.md                    # Main entry point with domain summaries
├── _meta/
│   ├── manifest.json           # Processing state: file hashes, timestamps
│   └── conventions.md          # Naming rules, tag taxonomy
├── Concepts/                   # Domain concepts (e.g., Заявка, Смежная система)
│   └── _index.md               # Concept list with descriptions
├── Processes/                  # Business processes and workflows
│   └── _index.md
├── Glossary/                   # Terms, abbreviations, definitions
│   └── _index.md
├── Systems/                    # External systems, integrations, APIs
│   └── _index.md
├── Roles/                      # User roles, permissions, actors
│   └── _index.md
└── Statuses/                   # Status dictionaries, transitions, enums
    └── _index.md
```

## Note Format (Obsidian-Compatible)

Every note follows this structure:

```markdown
---
title: "Заявка. Статус: Черновик"
aliases: ["DRAFT статус", "Черновик заявки"]
tags: [dopp, статус, процесс]
created: 2026-03-25
updated: 2026-05-17
source: Sources/Confluence/SM - Алгоритмы/20. Алгоритмы для Сервиса Заявителя/...
related:
  - "[[Заявка]]"
  - "[[Заявка. Статус: Зарегистрирована]]"
---

# Заявка. Статус: Черновик

## Определение
Черновик — начальное состояние заявки, когда её создаёт пользователь 
в личном кабинете и ещё не отправлена в смежную систему.

## Поведение
- Документ может быть отредактирован
- Заявка не видна в смежной системе
- Документ не может быть подписан

## Переходы
- → [[Заявка. Статус: Зарегистрирована]] (при отправке в смежную систему)
- → Удаление (по инициативе автора)

## См. также
- [[Формирование заявки из уведомления]]
- [[Удаление заявки]]
```

### Why This Format Works

- **YAML frontmatter** gives obsidian metadata for search/filters
- **Aliases** catch alternative names users might search for
- **Tags** enable faceted browsing
- **`related`** field creates explicit graph connections
- **Obsidian wiki-links** `[[Title]]` enable click-to-navigate

## Processing Workflow

Process each source file independently and sequentially:

### Step 1: Load Manifest

```json
// _meta/manifest.json
{
  "sources": {
    "Sources/Confluence/SM - Алгоритмы/...": {
      "hash": "abc123",
      "processed": "2026-05-17T12:00:00",
      "notes": ["Concepts/dopp-status-draft.md", "Processes/..."]
    }
  }
}
```

### Step 2: Read Source File

1. Read one source file at a time
2. Compute content hash (first 16 chars of md5)
3. Compare with manifest — if unchanged, skip entirely

### Step 3: Extract Topics

For each source file, identify **atomic topics** — one concept per note.

**Topic Extraction Rules:**

- **One idea per note**: If a file describes 5 statuses, create 5 notes
- **Self-contained**: Each note makes sense without reading the source
- **Minimal cross-reference**: Link to related notes, don't duplicate content
- **Atomic but not trivial**: A note should explain something meaningful

**What Counts as a Topic:**

| Source Content | Creates Note |
|---|---|
| A defined term with meaning | `Glossary/term-name.md` |
| A status with transitions | `Statuses/status-name.md` |
| A process step in a workflow | `Processes/process-step.md` |
| A system with integration details | `Systems/system-name.md` |
| A role with permissions | `Roles/role-name.md` |
| A concept referenced elsewhere | `Concepts/concept-name.md` |
| A heading with no unique content | Skip (not atomic) |
| A link to external doc | Skip (not a topic) |

### Step 4: Create or Update Notes

For each extracted topic:

1. **Generate filename**: Slug from Russian title (e.g., "Заявка. Статус: Черновик" → `zayavka-status-chernovik.md`)
2. **Check if exists**: Read existing note if filename matches
3. **Compare content**: If topic content unchanged (key facts same), skip
4. **Write note**: Create/update with full template
5. **Resolve links**: Scan for wiki-links that should point to existing notes
6. **Update manifest**: Record source→notes mapping

### Step 5: Update Indices

After processing a source file:

1. Add new notes to appropriate `_index.md`
2. Update `index.md` if new domains appeared
3. Keep indices sorted and with short descriptions

## Source File Handling

Process files in this order (file-by-file, not in parallel):

### Primary Sources (always process)

Process every content `.md` under `Raw/project/` — это первичные документы проекта,
положенные командой (обзоры, процессы, статусные модели, сиквенсы, реестры Epic/US).
Файлы-версии (`*Old*`, `*копия*`, `*beta*`) пропускать. Тип topic'а определяется по
содержимому: процесс → `Processes/`, схема систем → `Systems/`, статусная модель →
`Statuses/`, реестр требований → `Concepts/` + карточки требований.

> Ниже по тексту примеры нормализации имён и извлечения используют образцовый проект
> (налоговый домен: заявки, RU.PRJ.ALG-*) — это иллюстрации алгоритма, а не обязательная
> конфигурация. Подставляйте документы своего проекта.

### Confluence Sources

All `.md` files under `Sources/Confluence/` that contain actual content (skip `index.md` files, state files).

Focus on pages with substantial body content. Each Confluence page is a potential source of multiple atomic notes.

### Dictionary Sources

All `.md` files under `AuroraKnowledgeDB/Reference/`.

Each dictionary entry is typically one note. Status dictionaries become `Statuses/` notes, 
abbreviation dictionaries become `Glossary/` notes.

## Filename Normalization (STRICT — Obsidian Link Rules)

Every wiki-link `[[target]]` in Obsidian requires a file `target.md` with that exact name (case-sensitive). No fuzzy matching. No fallbacks.

**The ONLY way links work: `link_target == filename_without_extension`**

### Normalization Algorithm (Source Title → Filename)

Given any title from an external source (Confluence, JIRA, dictionary):

```
1. Remove smart/straight quotes: «» " "  → nothing
2. Remove outer parentheses: (Title) → Title
3. Replace inner parens with hyphen: (sub) → -sub
4. Replace ALL separators with single hyphen -:
     space → -
     dot . → -  (RU.PRJ.ALG-035 → RU-крупном проекте-ALG-035)
     colon : → -  (TCP:5432 → TCP-5432)
     slash / → -
     em-dash — → -
     en-dash – → -
5. Replace № → No
6. Collapse multiple hyphens: -- → -
7. Strip leading/trailing hyphens
8. KEEP Cyrillic as-is (do NOT transliterate)
```

### Real Examples (Cases That Broke Links)

| Source Title | Normalized Filename | Rules Applied |
| --- | --- | --- |
| `ALG-043 "Сумма (акциз адв.)"` | `ALG-043-Сумма-акциз-адв` | quotes, parens → nothing; spaces → - |
| `RU.PRJ.ALG-035 Выбор данных из НСИ для поля "Курс"` | `RU-крупном проекте-ALG-035-Выбор-данных-из-НСИ-для-поля-Курс` | dots → -; quotes → nothing |
| `Способ авторизации: ЮЛ / Представитель ЮЛ / УКЭП + МЧД` | `Способ-авторизации-ЮЛ-Представитель-ЮЛ-УКЭП-+-МЧД` | colon, slashes → -; keep + |
| `(Oracle СЦВ НСИ TCP:??)` | `Oracle-СЦВ-НСИ-TCP-??` | outer parens, colon → - |
| `ИС «Ядро-2»` | `ИС-Ядро-2` | smart quotes → nothing |
| `Epic 3.1 Прием заявки от НП (ДО12)` | `Epic-3-1-Прием-заявки-от-НП-ДО12` | dots, parens → - |
| `заявки Статус: Зарегистрирован` | `заявки-Статус-Зарегистрирован` | spaces, colon → - |
| `Заполнение признака "Не показывать больше приветственную страницу"` | `Заполнение-признака-Не-показывать-больше-приветственную-страницу` | quotes, spaces → - |

### Alias Registration (MANDATORY — Every Note)

Every note frontmatter MUST include `aliases:` with ALL name variants that could link to this note:

```yaml
title: "ALG-043 Расчет Сумма (акциз адв.)"
aliases:
  - "ALG-043 Расчет «Сумма (акциз адв.)»"     # Original with smart quotes
  - "ALG-043 Расчет "Сумма (акциз адв.)""   # Original with straight quotes
  - "ALG-043 Расчет Сумма акциз адв"          # Stripped quotes and parens
  - "ALG-043"                                  # Short code prefix
  - "акциз адв"                                # Key phrase
tags: [process.algorithm, prj.calculation]
created: 2026-05-17
updated: 2026-05-17
source: Sources/Confluence/SM - Алгоритмы/.../ALG-043 Расчет Сумма (акциз адв.).md
```

Alias categories to always include:

| Variant | When | Example |
| --- | --- | --- |
| Original source title | Always, with original quotes/punctuation | `"ALG-043 «Сумма (акциз адв.)»"` |
| Source filename stem | Always, exact basename without .md | `"RU.PRJ.ALG-035 Выбор данных из НСИ для поля Курс"` |
| Without special chars | If original has quotes/parens | `"Заполнение признака Не показывать больше..."` |
| Short code / prefix | If identifiable | `"RU.PRJ.ALG-035"`, `"ALG-043"` |
| Colon/space from source | If source uses colons/spaces | `"заявки Статус: Зарегистрирован"` |
| Dot-separated variant | If source used dots | `"RU.PRJ.ALG-035 Выбор..."` |
| Plus variant | If source has + | `"логин+пароль"` |

### Link Creation Rules

When creating any wiki-link from any context:

1. Apply normalization to source title → get target filename
2. `[[target]]` must match EXACTLY an existing `.md` filename (without extension)
3. If target differs from display, use `[[filename|Display Name]]`
4. Verify file exists BEFORE writing the link
5. Add original source name as `alias:` to the target note
6. In `_index.md` files, use normalized filename form ONLY

### External Source Import — Name Resolution Flow

```
1. Get raw title from source (Confluence, JIRA, dictionary)
2. normalize_to_filename(raw_title) → target_filename
3. Does AuroraKnowledgeDB/target_filename.md exist?
   YES → link to it, add any new aliases to note frontmatter
   NO  → search all existing notes by aliases for partial match
            FOUND → link to existing note, add new aliases
            NOT FOUND → create new note with ALL source variants as aliases
```

### Pre-Write Validation (Every Note)

Before creating/updating ANY note:

- [ ] Filename passes normalization algorithm
- [ ] Frontmatter `aliases:` includes original source title (exact match)
- [ ] Frontmatter `aliases:` includes source filename stem (without .md)
- [ ] Frontmatter `aliases:` includes short-form / code prefix
- [ ] Frontmatter `source:` records exact source file path
- [ ] All `[[links]]` in body point to EXISTING filenames (case-sensitive)
- [ ] Case of all Cyrillic letters matches existing filename exactly (ГОСТу ≠ ГОСту)

## Link Resolution

When a note mentions a concept that exists in the AuroraKnowledgeDB:

1. Search existing notes (check `Glossary/`, `Concepts/`, `Statuses/` indices)
2. If found, replace the mention with `[[Note Title]]`
3. Add the note to the `related:` frontmatter if not already there

**Common link targets to check:**
- заявки statuses (Черновик, Зарегистрирован, etc.)
- System acronyms (сокращения смежных систем и подсистем)
- Document types (заявки, ДПП, ТК, УКЭП)
- Roles (актёры и роли вашего домена)

## Index File Format

Each `_index.md` in a domain folder:

```markdown
# Concepts

| Note | Description |
|------|-------------|
| [[заявки]] | Документ оплаты публичного платежа — основной документ системы |
| [[Смежная-система]] | внешняя система, принимающая заявки |
| [[УКЭП]] | Усиленная квалифицированная электронная подпись |
```

Root `index.md`:
```markdown
# Project Knowledge Base

## Domains
- [[Concepts Index]] — Domain concepts and terminology
- [[Processes Index]] — Business processes and workflows
- [[Glossary Index]] — Abbreviations and definitions
- [[Systems Index]] — External systems and integrations
- [[Roles Index]] — User roles and permissions
- [[Statuses Index]] — Status dictionaries and transitions

## Quick Links
- [[заявки]] — Main document type
- [[Main Process]] — Core workflow
- [[Status Model DOPP]] — All status transitions
```

## Conventions (stored in `_meta/conventions.md`)

```markdown
# Knowledge DB Conventions

## Naming
- Filenames: Cyrillic with hyphens for spaces (заявки-Статус-Черновик.md)
- Titles in frontmatter: Human-readable with proper capitalization
- Aliases: Include English abbreviations, alternative terms

## Tags
Taxonomy: {domain}.{subdomain}.{concept}
- `dopp.status` — заявки statuses
- `dopp.process` — заявки workflows
- `system.<код>` — интеграция со смежной системой
- `system.<код2>` — интеграция со второй смежной системой
- `role.actor` — User roles
- `process.workflow` — Business processes

## Updates
- `created` — never changes after first write
- `updated` — timestamp of last content change
- `source` — original source file(s)
```

## Incremental Processing

The manifest tracks file hashes. On each run:

1. **New source** → process all topics
2. **Modified source** → re-extract topics, diff against existing notes
3. **Unchanged source** → skip entirely
4. **Deleted source** → flag orphaned notes (don't auto-delete)

After processing, report:
- Sources processed
- Notes created
- Notes updated
- Notes skipped (unchanged)
- Orphaned notes detected (if any)

## Error Handling

- **Missing source file** → log warning, continue with next
- **Empty source file** → skip, log "no extractable content"
- **Confluence index.md** → skip (metadata only, no content)
- **State/log files** → skip (sync_state.md, update_log.md)
- **Template files** → skip (anything with `_template` in name)

## Content Pattern Extraction Rules

### Mermaid Diagrams (Flowchart, Sequence, StateDiagram)
When a source contains Mermaid diagrams, extract:
- **subgraphs** → Process stages, network zones, container groups
- **nodes** → Services, systems, statuses, actors
- **edges** → Connections, transitions, message flows
- **classDefs** → Semantic categories (terminal, active, system_type, etc.)

For each subgraph, create a `Processes/` note. For each node with unique behavior, create a `Systems/`, `Statuses/`, or `Roles/` note. For state diagrams, each state and each transition becomes a `Statuses/` note.

### Markdown Tables
| Column Count | Pattern | Note Type | Destination |
|---|---|---|---|
| 2+ cols with code/term | Enum/dictionary entry | Glossary or Status | `Glossary/`, `Statuses/` |
| 2+ cols with stage/status | Process definition | Status or Process | `Statuses/`, `Processes/` |
| 2+ cols with system/port | System integration | System | `Systems/` |
Each row in an enum table creates one independent note.

### Hierarchical Bullet Lists with Trace IDs
Pattern: H2/H3 headings with nested bullets containing `(REQ-XXX) [CP-XXX] [BP-XXX]`
- H2 → Activity (update `_index.md` only)
- H3 → Epic (create `Concepts/` note if substantial)
- Bullet with REQ/CP/BP → User Story (atomic note)

### Confluence Metadata Headers
Pattern: `- **ID:**`, `- **URL:**`, `- **Updated:**` in frontmatter.
Extract body content for topic analysis. Use `# {title}` as base name.

### Enum/Code Tables with State Diagrams
Pattern: Table with code column + Mermaid stateDiagram.
Create one `Statuses/` note per code with code, name, description, transitions, and inverse links.


## Processing Order (Critical)
Process in this order so the link graph builds correctly (сначала терминология, потом то,
что на неё ссылается):
1. **Dictionaries / Reference** — `AuroraKnowledgeDB/Reference/*` (аббревиатуры, справочники) → эталонная терминология
2. **Status dictionaries** — справочники кодов статусов → status codes
3. **Status model** — документ статусной модели из `Raw/project/` → core status definitions
4. **Main process** — основной процесс из `Raw/project/` → uses statuses from step 3
5. **Architecture / net diagram** — схема систем из `Raw/project/` → system architecture
6. **Sequence specs** — сиквенс-диаграммы → reference statuses + systems
7. **Activity/requirements tracker** — реестр Epic/US из `Raw/project/` → references all above
8. **Confluence sources** — all content files under `Sources/Confluence/` → deepest detail
After each file, update `related:` links in new notes to point to existing notes from prior steps.

## Report Format
After each source: `✅ Source: {path} | +{created} 💡 {updated} ⏭️ {skipped} notes`
At end: `📊 AuroraKnowledgeDB Summary — Sources: X | Created: Y | Updated: Z | Skipped: W | Domains: C/A P/B G/C S/D R/E St/F`

## Учёт разбора и почему прогресс может врать

Что уже разобрано, помнит `AuroraKnowledgeDB/meta/manifest.json`: для каждого источника
хеш, дата и число извлечённых карточек. Состояний три — **новый** (записи нет),
**изменён** (хеш не совпал), **обработан**. В план и в задания попадают только первые два,
поэтому повторный `kb:build` безопасен: он не выдаст заново то, что уже разобрано, и
ничего не перемешает. Партии при этом нумеруются заново от текущего остатка — «партия 6»
до и после перезапуска это разные наборы файлов.

Отметку ставит ассистент командой `build_plan.py --done <файл>` после каждого файла — но
с 1.47.0 она **проверяется по базе**: если карточек с `source: <файл>` нет, отметка не
ставится, и скрипт возвращает ошибку. Число карточек тоже берётся из базы, а не из флага
`--cards`: он остался как заявление ассистента и служит только для сверки. Источник, из
которого знание не выходит по природе (служебная страница, задача без постановки),
отмечается явно: `--done <файл> --empty "почему пусто"`.

Остаётся одна ошибка, которую механика поймать не может: **разобрал, но не отметил** —
файл вернётся в план, и второй разбор наплодит двойников. Лечится `kb:dedupe`.

До 1.47.0 отметка означала «ассистент сказал, что разобрал»: на живой базе так набралось
356 отметок с нулём карточек — источники выпали из плана, не дав знания.

Есть и третий случай, который не ловит ни одна отметка: **карточка создана, но источник
разобран до середины** — модель прочла первые разделы и остановилась. Такой источник
считается сделанным и в план не вернётся. Видно это без модели, по двум признакам:
объём исходника на одну карточку и число структурных заголовков против числа карточек.
Показывает `kb:build --thin`, вернуть в план — `--thin --reopen --apply`. Пороги: 15 КБ
на карточку либо заголовков втрое больше, чем карточек (на живой базе медиана — 3,6 КБ на
карточку). Это подозрение, а не приговор: пересказ на сорок страниц законно даёт одну
карточку.

Случай «отметил, но карточек не создал» чинится `kb:build --reopen`: он сверяется не со счётчиком в манифесте, а с базой —
есть ли хоть одна карточка с таким `source`, — и возвращает в план тех, кто ничего не дал.
Ноль карточек бывает законным (задача Jira, готовый справочник в `Reference/`), поэтому
проверяйте вывод и при необходимости сужайте: `--reopen --group Sources/Confluence`.

## `kb:moc` — карты содержания и когда заводить новую

Карточка без входящих ссылок знанием не работает: её не найдут ни по связям, ни глазами.
Карта содержания (MOC) — вход по смыслу, а не по папке.

`kb:moc` собирает карты по группировкам из `moc_groups.txt` (тип карточки, раздел, метка,
шаблон заголовка) и отдельно — карту брошенных. Всё, что не попало ни в одну группу,
уходит в «Разное»: это сигнал, что правила отстали от базы.

`kb:moc --suggest` показывает, где база доросла до новой карты, по четырём признакам:

- **скопление по метке** — восемь и больше карточек с общей меткой, которой нет ни в одном
  правиле: похоже на тему, у которой уже есть своя жизнь;
- **узел** — карточка, на которую ссылаются десятки других. Она уже работает как карта,
  просто об этом никто не договаривался: вынесите её содержание в MOC либо сделайте узлом
  группы;
- **переросшая группа** — карта длиннее шестидесяти позиций перестаёт быть навигацией;
  скрипт подскажет метки, по которым её делить;
- **общий префикс заголовков** (`ALG-`, `SPR-`) — де-факто класс артефактов.

Что решает человек, а не скрипт: **как карта называется и чем полезна**. «Организация N»
имеет смысл заводить, когда вокруг неё собрались проекты, люди и договоры, — тогда карта
отвечает на вопрос «что мы про них знаем». Скрипт видит скопление, но не видит смысла.

Порядок: `kb:moc --suggest` → строка в `moc_groups.txt` → `kb:moc --apply` →
`kb:links --cards` для тех, кто так и остался без входа.

## Ссылки и синонимы при извлечении

Две беды, которые после сборки с нуля дают сотни строк в `kb:lint`, и обе рождаются в
момент извлечения:

**Ссылка на несуществующее.** Модель видит в тексте термин и ставит `[[Термин]]`, хотя
карточки под него нет и не будет в этой партии. Правило: ссылаться только на то, что уже
есть в базе или создаётся здесь же; остальное — обычный текст. Если термин важен, лучше
завести на него карточку, чем оставить ссылку в никуда.

**Один синоним у двух карточек.** `aliases` — это другие имена ЭТОЙ карточки, а не тема,
к которой она относится. Общий синоним у двух карточек делает `[[Синоним]]` неразрешимым.

Чинится после факта, и по-разному:

- `kb:repair --links` подтягивает то, что находится по алиасу, регистру и гомоглифам;
- **`kb:repair --stubs` заводит карточку-заготовку** под каждую ссылку, которой не на что
  указывать. Так работает картотека: ссылка появляется раньше знания, и правильный ответ
  на `[[УТС]]` — пустая карточка, которая ждёт наполнения, а не удаление ссылки. Когда
  придут данные, они лягут в готовую карточку, и переписывать ссылки не придётся.
  Заготовка помечена `status: draft`, меткой `заготовка` и списком тех, кто её ждёт;
- **`kb:repair --aliases` только показывает** конфликт синонимов и печатает задание
  ассистенту: снять синоним значит потерять имя, под которым карточку знают. Уточнять
  синонимы — работа со смыслом: «Обеспечение (этап процесса)» против «Обеспечение (эпик)».
  Механическое снятие осталось за отдельным ключом `--drop-alias`.
