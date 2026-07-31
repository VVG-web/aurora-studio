---
name: aurora-vault
description: >
  Operate the project knowledge framework: build and maintain a Zettelkasten knowledge
  database (Obsidian markdown), manage knowledge lifecycle (imported/draft/in-review/
  verified/deprecated), record decisions (Decision Records), review and generate analyst
  artifacts (US, AC, specs) enriched with trusted knowledge context. Use whenever the user
  mentions Aurora / Аврора / /aurora-vault / the knowledge base / AuroraKnowledgeDB / база знаний, building or syncing knowledge
  cards, zettelkasten, obsidian notes, checking a User Story or AC against the knowledge
  base, creating a US/AC/spec from templates, decision records / журнал решений / DR,
  deprecating or superseding knowledge, context pack / обогащение контекста,
  gardening / гигиена базы, repairing broken links / дубли / очередь верификации,
  sync integrity / целостность зеркал, or asks "почему выбрали/почему не" about past decisions.
---

# Aurora (Аврора) — фреймворк работы со знаниями проекта

One skill, many commands. Detailed procedures live in `references/` — read ONLY the file
needed for the requested command (progressive disclosure, keep context small).

## Project settings

**Source of truth for project-specific constants:** `aurora.config.yaml` at repo root
(Confluence space / sync_roots, Jira project_key / default_jql, recommended skills, paths).

- Personal secrets: `.env.aurora.local` (gitignored) or Cursor MCP user login — never in skills/git.
- Приватность: `privacy.scrub` (off / report / mask) — режим `kb:scrub` для этого контура.
- Onboarding check: `python3 .opencode/scripts/aurora_doctor.py`
- Do not hardcode another project's space/JQL inside this skill.

## Folder semantics (trust layers)

Структура папок **фиксирована движком** (`.opencode/structure_dirs.txt`) и одинакова во
всех проектах Авроры. Новые папки верхнего и структурного второго уровня появляются
только через релиз kit'а. Всё, что не укладывается в таксономию, живёт в
`Workspaces/<задача>/` — см. инвариант 9.

| Folder | Role | Trust |
|---|---|---|
| `Sources/` (`Confluence/`, `JIRA/`) | зеркала синка, read-only | объект работы, не знание |
| `Raw/` | первоисточники, положенные руками: `laws/`, `contract/` (ТЗ), `customer/`, `project/` (концепт-документы), `examples/` | доказательства, цитируются |
| `Raw/meetings/` | транскрибации встреч и согласованные протоколы — неизменяемы | доказательства, цитируются |
| `Raw/contract/` | госконтракт, ТЗ, календарный план, допсоглашения — неизменяемы | высшая доказательная база; пункты ТЗ парсятся в REQ |
| `Raw/customer/` | материалы заказчика: AS-IS схемы процессов, регламенты, презентации | доказательства; наша интерпретация — карточками |
| `AuroraKnowledgeDB/` | карточки знаний | по `status` во frontmatter |
| `AuroraKnowledgeDB/Requirements/` | карточки требований заказчика (REQ-NNN) | по `status`; жизненный цикл требования — в `req_status` |
| `AuroraKnowledgeDB/Reference/` | справочники домена, ведутся руками: аббревиатуры, подсистемы смежных систем, участники и роли | знание (по `status`); аббревиатуры подмешиваются в каждый context pack |
| `AuroraKnowledgeDB/Specs/` | спецификации фич (SPEC-NNN) — исполняемые контракты для разработки | по `status`; verified + выпущенный spec-pack = передана в разработку, неизменна для релиза |
| `AuroraKnowledgeDB/Questions/` | вопросы к заказчику/смежникам (Q-NNN): незнание как объект — кому задан, что блокирует, где ответ | сам вопрос — не факт; ответ становится знанием в REQ/спеке/DR |
| `AuroraKnowledgeDB/Decisions/` | журнал решений, append-only | accepted = истина; rejected/superseded = история |
| `AuroraKnowledgeDB/MOC/` | карты контента (ролевые, тематические) | навигация |
| `AuroraKnowledgeDB/_archive/` | deprecated-карточки | только история |
| `AuroraKnowledgeDB/_assets/` | бинарные схемы (drawio, png) — только с карточкой-обёрткой | статус и владельца несёт карточка-обёртка |
| `AuroraKnowledgeDB/meta/` | служебное: `manifest.json`, `conventions.md`, `golden_questions.md`, `metrics.md`, `usage.log`, `aurora_version.txt` | механика базы |
| `Artifacts/` | продукты ИИ+аналитика: произведённые документы (US, AC, алгоритмы, экраны, схемы, приёмка, ревью, отчёты…). **Полный список типов и их знаниевые двойники — `meta/conventions.md`; сами папки — `.opencode/structure_dirs.txt`. Список закрыт**, новый тип только релизом kit'а | НЕ знание, в контекст не подавать; публикуются после ревью |
| `Deliverables/work/` | рабочие версии поставляемых документов (ОПЗ, ПМИ, РП) | продукт из знаний, собирается по `based_on` |
| `Deliverables/released/` | переданные заказчику версии — неизменяемые снапшоты | доказательство «что сдали», цитируется |
| `Workspaces/` | рабочие пространства больших задач (одна задача = одна папка): подборки, версии, черновики, любые вспомогательные файлы; завершённые → `_archive/` | НЕ знание, в контекст не подавать; результат уезжает в Deliverables/Artifacts/AuroraKnowledgeDB |
| `Templates/`, `Prompts/` | рецепты производства артефактов | инструкции, не факты |

## Commands

Команды сгруппированы в наборы (неймспейсы). Короткие имена без префикса —
исторические алиасы, работают всегда: `/aurora-vault build` ≡ `/aurora-vault kb:build`.

### `kit:` — движок и проект

| Command | What it does | Reference |
|---|---|---|
| `kit:list` | справочник всех команд: модификаторы, чем исполняется, с какой версии (`kit_commands.py`) | `docs/commands.md` |
| `kit:doctor` (`doctor`) | онбординг: config, skills, секреты в git, версия движка | `.opencode/scripts/aurora_doctor.py` |
| `kit:structure` | сверить фактические папки с фиксированной схемой движка | `aurora_doctor.py --structure` |
| `kit:hooks` | поставить git pre-commit с `kb_lint` (чтобы ошибки не копились) | `.opencode/scripts/aurora_hooks.py` |
| `kit:remap-sources` | перенацелить `source:` карточек после переезда зеркала (по page_id) | `references/migration.md` |

Развёртывание и обновление движка выполняются из kit'а, не из скилла:
`python3 <kit>/aurora.py new|setup|update <project>`.
Панель управления (все проекты машины, здоровье баз, запуск команд, справка):
`python3 <kit>/aurora.py cockpit` — см. `cockpit/README.md`.

### `sync:` — зеркала внешних систем

| Command | What it does | Reference |
|---|---|---|
| `sync:confluence` | зеркало Confluence → `Sources/Confluence/` (детерминированный скрипт) | `references/maintenance.md` |
| `sync:jira` | зеркало Jira → `Sources/JIRA/` (детерминированный скрипт) | `references/maintenance.md` |
| `sync:jira-status` | обратный поток: статусы задач → кандидаты в `req_status`, работа без требований (`jira_status.py`) | `references/maintenance.md` |
| `sync:audit` | целостность зеркала: missing / orphan / collision / протухшее состояние | `references/maintenance.md` |
| `sync:diff` (`diff`) | дрейф: источник изменился после сверки (сравнение хешей, скрипт) | `references/maintenance.md` |

### `kb:` — извлечение и жизнь знания

| Command | What it does | Reference |
|---|---|---|
| `kb:build` (`build`) | извлечь карточки: план и учёт — `build_plan.py`, само извлечение — модель. `--partition N` печатает готовое задание на партию: список файлов и правила | `references/build.md` |
| `kb:ingest-office [path]` | docx/pdf/xlsx/pptx из Raw/ → markdown-транскрипты рядом с оригиналом | `references/maintenance.md` |
| `kb:ingest-raw <path>` (`ingest-raw`) | обработать документ из Raw/ в карточки-кандидаты | `references/workflows.md` |
| `kb:ingest-meeting <транскрипт>` (`ingest-meeting`) | транскрипт → резюме, решения (DR), требования (REQ), факты | `references/workflows.md` |
| `kb:ingest-tz <ТЗ>` (`ingest-tz`) | разобрать ТЗ по пунктам в REQ-карточки с tz_ref | `references/workflows.md` |
| `kb:links` (`links`, `graph`) | граф связей: ключи Requirement Yogi и номера историй; `--cards` переносит связи в `related:` карточек (`kb_graph.py`) | `references/build.md` |
| `kb:queue` | очередь верификации: что верифицировать первым (употребление × связи × протухание) | `references/maintenance.md` |
| `kb:verify` (`verify`, `promote`) | гейт: imported/draft → verified — отбор человеком, запись скриптом; `--source-older-than N` — пакетно принять то, что давно не менялось в источнике (основание пишется в карточку) | `references/maintenance.md` |
| `kb:repair` | ремонт: битые ссылки, гомоглифы, легаси-frontmatter (`kb_fix.py --all`) | `references/maintenance.md` |
| `kb:retire` | убрать поля, выведенные из схемы (`kb_fix.py --retire`); `canonical` → `verified` | `references/maintenance.md` |
| `kb:dedupe` | двойники: поиск и слияние (`kb_fix.py --dupes` / `--merge`) — тот же скрипт, другой режим | `references/maintenance.md` |
| `kb:index` | регенерация `_index.md` разделов (рукотворные не трогает) | `references/maintenance.md` |
| `kb:scrub` | персональные данные: найти и закрыть маркерами (`kb_scrub.py`); режим — `privacy.scrub` в конфиге; доказательства не правит | `references/maintenance.md` |
| `kb:schema` | версия схемы карточек и миграция между версиями (`kb_schema.py`) | `references/frontmatter.md` |
| `kb:classify` | артефакты, попавшие в знания (US/AC/Epic/задачи); типы карточек | `references/maintenance.md` |
| `kb:question <суть>` | завести вопрос к заказчику (Q-NNN): кому, что блокирует, срок | `references/workflows.md` |
| `kb:answer <Q-NNN>` | зафиксировать ответ: закрыть вопрос и разнести знание в REQ/спеку/DR | `references/workflows.md` |
| `kb:decide <тема>` (`decide`) | оформить Decision Record (+supersede старой DR) | `references/workflows.md` |
| `kb:supersede <card>` (`supersede`) | заменить знание, сохранив историю: deprecated → `_archive`, ссылки переписываются (`kb_supersede.py`) | `references/maintenance.md` |
| `kb:garden` (`garden`) | еженедельная гигиена: протухшее, сироты, битые ссылки | `references/workflows.md` |

### `ctx:` — использование знаний

| Command | What it does | Reference |
|---|---|---|
| `ctx:context <тема>` (`context`) | context pack: отбор, фильтр статусов, шапки доверия, `usage.log` — скриптом (`ctx_pack.py`) | `references/retrieval.md` |
| `ctx:ask <вопрос>` (`ask`) | ответ по базе с цитатами; «почему не X» — включая отклонённые DR | `references/workflows.md` |
| `ctx:eval` (`eval`) | регрессионный прогон golden questions (после синков/миграций) | `references/workflows.md` |
| `ctx:retro <событие>` (`retro`) | выученные уроки: чего база не знала, когда мы ошиблись | `references/workflows.md` |

### `make:` — производство артефактов

| Command | What it does | Reference |
|---|---|---|
| `make:create <тип> <тема>` (`create`) | сгенерировать артефакт в `Artifacts/<тип>/` — **только стандартный тип** | `references/workflows.md` |
| `make:review <US/AC>` (`review`) | проверка качества артефакта против базы знаний | `references/workflows.md` |
| `make:spec <тема>` (`spec`) | собрать спецификацию фичи из REQ + verified-знаний (SDD) | `references/workflows.md` |
| `make:spec-pack <SPEC-NNN>` (`spec-pack`) | бандл спеки: основания, DR, аббревиатуры, DoR-риски — сборка скриптом (`spec_pack.py`) | `references/workflows.md` |
| `make:validate <SPEC> <объект>` (`validate`) | сверить реализацию/тесты подрядчика со сценариями спеки | `references/workflows.md` |
| `make:assemble <документ>` (`assemble`) | собрать поставляемый документ (ОПЗ/ПМИ/РП) из базы по шаблону | `references/workflows.md` |

### `ship:` — наружу

| Command | What it does | Reference |
|---|---|---|
| `ship:publish <артефакт>` | артефакт → generated-страница Confluence (`publish_doc.py`); карточки знаний наружу не идут | `references/workflows.md` |
| `ship:export <документ>` | поставляемый документ → docx/pdf (pandoc, фирменный шаблон) | `references/workflows.md` |
| `ship:acceptance <объект>` | зафиксировать результаты приёмки/испытаний и разобрать замечания заказчика | `references/workflows.md` |
| `ship:release <документ>` (`release`) | заморозить переданную версию: снапшот + коммит базы + дата (`release_doc.py`) | `references/maintenance.md` |

### `ops:` — управление и отчётность

| Command | What it does | Reference |
|---|---|---|
| `ops:stats` (`status`) | дашборд здоровья базы: статусы, риски, метрики (`--append-metrics` — строка в `meta/metrics.md`) | `references/maintenance.md` |
| `ops:impact <карточка>` | что зависит от карточки; `--explain <документ>` — на чём он собран | `references/maintenance.md` |
| `ops:trace` (`trace`) | перестроить трассировку: пункт ТЗ → REQ → SPEC → Jira → AC → ПМИ → приёмка | `references/workflows.md` |
| `ops:questions` | реестр вопросов: открытые, просроченные, что блокируют (числа — из `aurora_stats.py`) | `references/workflows.md` |

Frontmatter schema for ALL cards: `references/frontmatter.md`.
Context assembly for ALL prompt-enrichment: `references/retrieval.md`.
Механические процедуры (repair, dedupe, queue, audit, stats): `references/maintenance.md`.

## Правило «скрипт vs модель»

У каждой массовой операции есть детерминированный скрипт в `.opencode/scripts/`. Агент
**обязан сначала запустить скрипт** и работать с его отчётом, а не обходить базу
самостоятельно: обход тысяч файлов моделью дорог и порождает ошибки. Модель принимает
решения там, где нужно суждение (что слить, что верифицировать, что признать дрейфом).

| Задача | Скрипт | Роль модели |
|---|---|---|
| проверка базы | `kb_lint.py` | интерпретация ошибок |
| ремонт ссылок/имён | `kb_fix.py` | решение по неоднозначным случаям |
| дубли | `kb_fix.py --dupes` | выбор победителя и слияние тел |
| очередь верификации | `kb_queue.py` | сама верификация карточек |
| переезд зеркала | `kb_remap.py` | разбор несопоставленных источников |
| маршрутизация карточек | `kb_classify.py` | решение «артефакт или знание» по каждой находке |
| сборка context pack | `ctx_pack.py` | сам ответ по паку |
| простановка verified | `kb_verify.py` | решение, каким карточкам верить |
| замена знания | `kb_supersede.py` | решение «это устарело» и текст преемника |
| обратная трассировка | `kb_impact.py` | что делать с затронутыми документами |
| зеркало Confluence | `confluence_export.py` | разбор новых и изменившихся страниц |
| зеркало Jira | `jira_export.py` | разбор новых и изменившихся задач |
| дрейф источников | `sync_diff.py` | что делать с изменившимся знанием |
| фиксация переданного | `release_doc.py` | что сдаём и с каким риском |
| целостность зеркал | `sync_audit.py` | что досинхронизировать |
| метрики и дашборд | `aurora_stats.py` | комментарий к динамике |
| план извлечения | `build_plan.py` | само извлечение карточек из источника |
| офисные первоисточники | `office_ingest.py` | извлечение карточек из транскрипта |
| экспорт документа | `export_doc.py` | вычитка результата перед передачей |
| трассировка | `aurora_trace.py` | разбор разрывов |
| готовность проекта | `aurora_doctor.py` | починка конфига |

## Invariants (never violate, any command)

1. **Артефакт ≠ знание.** Проверяемый/генерируемый документ — «объект», знание — только
   карточки AuroraKnowledgeDB с фильтром по статусу. Artifacts/ и Sources/ в контекст знаний
   не попадают.
2. **Ничего не удалять.** Устаревшее знание → `supersede` (deprecated + _archive).
   Decision Records неизменяемы после accepted/rejected.
3. **Синк не перезаписывает проверенное.** `verified`-карточки меняет только
   человек (или агент с явным подтверждением владельца); build при конфликте рапортует DRIFT.
4. **Каждое включение карточки в промпт — с шапкой доверия** (см. retrieval.md).
5. **Верификация — работа человека.** Скрипт записывает решение, но не принимает его:
   у каждой `verified`-карточки есть owner и review_by. `verified` — верхний статус базы.
6. **Поставленное неизменяемо.** `Deliverables/released/` и доказательная часть `Raw/`
   (contract, meetings, laws, customer) не редактируются никогда. Исключение —
   `Raw/project/` и `Raw/examples/`: живые рукотворные документы проекта.
7. **Схемы — как код.** Диаграммы (ERD, архитектура, сиквенсы, BPMN) — mermaid в теле
   карточки. Бинарники (drawio/png) — только в `_assets/` с карточкой-обёрткой.
8. **Релизы.** Карточки могут быть ограничены релизами (`applies_to`); при сборке
   контекста учитывать релиз задачи (см. retrieval.md).
9. **Структура папок фиксирована движком.** Список папок — `.opencode/structure_dirs.txt`;
   исключение — пути, закрытые `.gitignore` (служебное состояние инструментов, кэши,
   `node_modules`): в git они не едут и схему не ломают, `doctor` их пропускает.
   он одинаков во всех проектах Авроры. НЕ создавать новые папки верхнего уровня, новые
   разделы `AuroraKnowledgeDB/` и новые типы `Artifacts/<тип>/`. Неизвестный тип в
   `create` → отказать, предложить стандартный тип или `Workspaces/<задача>/`. Нужен новый
   тип по-настоящему → изменение kit'а (PR в `structure_dirs.txt` + таблицы выше + CHANGELOG),
   чтобы тип появился сразу во всех проектах.
10. **Массовая механика — скриптом.** См. раздел «скрипт vs модель»: сначала скрипт,
    потом суждение по его отчёту.
