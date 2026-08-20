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
оттуда, а не выбирать самому — порядок обхода не случаен.

**Граница ответственности.** Разбор — это чтение источника и решения о темах: где кончается
одна тема, как она называется, в какой раздел ложится. Перенос текста, шапка, имя файла,
поиск целей для ссылок, синонимы, индексы, связи — работа движка, и он делает её один раз
на всю базу. Если по ходу разбора вы открываете чужие карточки, ищете похожие имена или
проверяете существование цели ссылки — вы выполняете работу скрипта вручную, и партия из
сорока источников растягивается на часы вместо минут. Если блока нет, спросите номер
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
   дописанное (`agent:distill` пересобирает тезис), либо разводит знание по карточкам.
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
tags: [[доменный термин убран], статус, процесс]
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
      "notes": ["Concepts/[доменный термин убран]-status-draft.md", "Processes/..."]
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

### Step 4: Create Notes

Одна команда на тему — файл пишет движок:

```
build_plan.py --card "Имя карточки" --source <файл> --sections 1,3-5 --to Concepts --apply
```

Она сама даёт имя файла по алгоритму нормализации, заполняет шапку (`status`, `type`,
`source`, `source_synced`, даты) и переносит текст секций. **Не пишите файл руками и не
перепечатывайте текст источника.**

### Доводка: форма меняется, факты — нет

`--card` переносит текст секций дословно — это черновик, а не готовая карточка. Дальше её
надо перечитать и довести, и здесь важно не перестараться.

**Оставить дословно.** Пересказ таких вещей своими словами только портит результат:
юридические формулировки и цитаты из нормативных актов, определения терминов, названия
систем, ролей и статусов, коды и ключи (RY, ERD, `ALG-…`, `SPR-…`, ключи задач), таблицы
маппинга полей, перечни значений, форматы данных. Карточка на таком материале и должна
быть точной копией — её ценность в точности.

**Сократить и переписать.** Всё, что относится к источнику, а не к знанию: «см. рисунок
ниже», «описано в разделе 3», «в рамках данной страницы», ссылки на страницы, повторы
соседних секций, пустые заголовки после нарезки.

Признак неверно выбранной темы: после доводки карточка читается как пересказ файла целиком.
Значит границу провели не там — разделите на несколько.

### Step 5: После партии это делает движок, а не вы

Ничего из списка ниже не делайте вручную: на каждой карточке это превращается в обход всей
базы, а на партии — в часы. Скрипты выполняют ту же работу один раз и за секунды.

| Работа | Команда | Почему не модели |
|---|---|---|
| проверить, что цель ссылки существует | `kb:lint` | модель смотрит по одному файлу, скрипт — всю базу разом |
| найти карточку по синониму, регистру, разделителям | `kb:repair --links` | резолвер знает свёртку регистра, гомоглифы и разделители |
| завести карточки под ссылки в никуда | `kb:repair --stubs` | ссылка появляется раньше знания — это нормально |
| расставить `related:` | `kb:links --cards` | связи выводятся из ключей и номеров историй |
| развести конфликтующие синонимы | `kb:repair --aliases` | конфликт виден только на всей базе |
| обновить `_index.md` | `kb:index` | индекс — производная от файлов |
| дописать недостающие поля шапки | `kb:repair --frontmatter` | правило одно на всю базу |

Значит: **поставили `[[ссылку]]` — и дальше.** Не проверяйте, есть ли цель, не ищите
похожие карточки, не открывайте индексы.

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

### Синонимы: один, а не семь

Раньше здесь требовалось перечислять у каждой карточки все варианты написания — с
кавычками, без кавычек, с точками, короткий код. Это давало по шесть-семь строк на
карточку и, что хуже, **один синоним у разных карточек**: на живой базе так набралось
полсотни конфликтов, после которых ссылка по имени не ведёт никуда.

- `aliases:` заполняйте, только если карточку **действительно знают под другим именем**:
  код `ALG-043`, аббревиатура, официальное название из договора;
- варианты написания одного имени (кавычки, точки, регистр) не нужны — резолвер
  сворачивает их сам;
- если синоним занят, `kb:repair --aliases` покажет конфликт.

### Ссылки

1. Ставьте `[[Имя-карточки]]` там, где нужна связь по смыслу.
2. Имя — по алгоритму нормализации выше. Не совпало с существующим файлом — не страшно:
   `kb:repair --links` найдёт цель, `kb:repair --stubs` заведёт заготовку под то, чего нет.
3. **Не проверяйте существование цели перед записью** — это обход базы на каждую ссылку.
4. В `_index.md` ничего не правьте: индексы генерируются.

## Упоминание понятия в тексте

Поставьте `[[ссылку]]` и продолжайте. Существует ли такая карточка, в каком она разделе,
знают ли её под другим именем — выясняет движок после партии, за один проход.

Ручной поиск «а есть ли уже такая карточка» — самая дорогая ошибка разбора: он превращает
работу с одним источником в обход тысячи файлов.

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
- [[Status Model [доменный термин убран]]] — All status transitions
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
- `[доменный термин убран].status` — заявки statuses
- `[доменный термин убран].process` — заявки workflows
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

## Раскадровка: текст переносит скрипт

Извлечение — не перепечатывание. Границы тем в документе уже расставлены заголовками (а
после конвертации из docx — строками, выделенными жирным целиком), и разрезать источник по
ним умеет скрипт. Модель решает то, чего механика не знает: **где кончается одна тема и
как она называется**.

```
build_plan.py --slice <источник>          # список секций: номер, заголовок, размер, превью
build_plan.py --card "Имя" --source <источник> --sections 1,3-5 --to Concepts --apply
```

`--card` собирает карточку сам: шапка (`status`, `type`, `source`, `source_synced`, даты),
имя файла по правилу нормализации, тело — **дословный текст указанных секций**. Модель не
выводит ни строчки содержимого, поэтому партия из сорока источников укладывается в минуты,
а не в часы.

Что остаётся модели:

- объединить соседние секции в одну тему (`--sections 3-5`) или, наоборот, взять одну;
- назвать карточку по смыслу, а не по номеру секции;
- выбрать раздел (`--to Glossary` для термина, `--to Processes` для алгоритма);
- пропустить то, что знанием не является: оглавления, «Историю изменений», служебные
  таблицы — просто не упоминать их номера.

Раскадровка пуста (`секций: 0`) — значит структуры в файле нет: сплошной текст, скан,
короткая заметка. Такой источник разбирается чтением, карточки создаются руками по правилам
ниже. На живой базе так выглядит примерно каждый пятый источник.

Связи и синонимы после партии расставляют `kb:links --cards` и `kb:repair --aliases` —
модели этого делать не нужно.

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
