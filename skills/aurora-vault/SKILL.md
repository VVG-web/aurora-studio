---
name: aurora-vault
description: >
  Operate the project knowledge framework: build and maintain a Zettelkasten knowledge
  database (Obsidian markdown), run the built-in LLM agent that parses sources into cards
  and distils them, compute trust from Jira task statuses (knowledge / draft / index /
  deprecated), record decisions (Decision Records), review and generate analyst
  artifacts (US, AC, specs) enriched with trusted knowledge context. Use whenever the user
  mentions Aurora / Аврора / /aurora-vault / the knowledge base / AuroraKnowledgeDB / база знаний, building or syncing knowledge
  cards, zettelkasten, obsidian notes, checking a User Story or AC against the knowledge
  base, creating a US/AC/spec from templates, decision records / журнал решений / DR,
  deprecating or superseding knowledge, context pack / обогащение контекста,
  gardening / гигиена базы, repairing broken links / дубли / доверие карточек,
  встроенный агент / разбор источников моделью / Момус,
  sync integrity / целостность зеркал, or asks "почему выбрали/почему не" about past decisions.
---

# Aurora (Аврора) — фреймворк работы со знаниями проекта

**Модель знания целиком описана в `docs/knowledge-rules.md`** (в проекте —
`.opencode/docs/knowledge-rules.md`): классы источников, два вида связей, три типа
карточек, устройство карточки, что делает человек, а что движок. Читайте её прежде, чем
решать что-либо про статус, тип или доверие карточки.

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
| `Sources/<Зеркало>/` | зеркала синка, read-only; набор папок задают подключённые модули (`sync:sources`) | объект работы, не знание |
| `Raw/` | первоисточники, положенные руками: `laws/`, `contract/` (ТЗ), `customer/`, `project/` (концепт-документы), `examples/` | доказательства, цитируются |
| `Raw/meetings/` | транскрибации встреч и согласованные протоколы — неизменяемы | доказательства, цитируются |
| `Raw/contract/` | госконтракт, ТЗ, календарный план, допсоглашения — неизменяемы | высшая доказательная база; пункты ТЗ парсятся в REQ |
| `Raw/customer/` | материалы заказчика: AS-IS схемы процессов, регламенты, презентации | доказательства; наша интерпретация — карточками |
| `AuroraKnowledgeDB/` | карточки знаний | по `status` во frontmatter |
| `AuroraKnowledgeDB/Requirements/` | карточки требований заказчика (REQ-NNN) | по `status`; жизненный цикл требования — в `req_status` |
| `AuroraKnowledgeDB/Reference/` | справочники домена, ведутся руками: аббревиатуры, подсистемы смежных систем, участники и роли | знание (по `status`); аббревиатуры подмешиваются в каждый context pack |
| `AuroraKnowledgeDB/Specs/` | спецификации фич (SPEC-NNN) — исполняемые контракты для разработки | по `status`; `knowledge` + выпущенный spec-pack = передана в разработку, неизменна для релиза |
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

## Как называть команды в ответах человеку

Человек работает через панель управления: он нажимает кнопку с именем команды, а не
набирает путь к скрипту. Поэтому в отчётах, сводках и рекомендациях **называйте команды
так, как они называются в панели и в реестре** — `kb:dedupe`, `kb:repair --aliases`,
`kb:trust`. Путь к скрипту уместен только там, где вы запускаете его сами.

Имя команды всегда есть в шапке скрипта — строка `Панель:` в его docstring, и она
сверяется с реестром тестом. Не выдумывайте файл по смыслу задачи: `kb_dedupe.py` не
существует, двойников сливает `kb:dedupe` (это `kb_fix.py --dupes --merge`).

## Commands

Команды сгруппированы в наборы (неймспейсы). Короткие имена без префикса —
исторические алиасы, работают всегда: `/aurora-vault build` ≡ `/aurora-vault kb:build`.

### `kit:` — движок и проект

| Command | What it does | Reference |
|---|---|---|
| `kit:list` | справочник всех команд: модификаторы, чем исполняется, с какой версии (`kit_commands.py`) | `docs/commands.md` |
| `kit:doctor` (`doctor`) | онбординг: config, skills, секреты в git, версия движка, структура папок (`--structure` — подробно) | `.opencode/scripts/aurora_doctor.py` |
| `kit:hooks` | git-хуки: pre-commit с `kb_lint` (ошибки не копятся) и commit-msg (внутренние названия не уходят в историю) | `.opencode/scripts/aurora_hooks.py` |
| `kit:remap-sources` | перенацелить `source:` карточек после переезда зеркала (по page_id) | `references/migration.md` |
| `kit:update` | обновить движок проекта до версии kit'а; `--structure-only` — только папки схемы | `references/migration.md` |
| `kit:skills` | скиллы Авроры в общий каталог агента (`~/.claude/skills`): без этого `/aurora-vault` не находится ни в одном диалоге | `references/maintenance.md` |
| `kit:mcp` | база как инструмент любого ассистента: проверка сервера и строка подключения. Сервер только читает — писать в базу через MCP нельзя | `references/retrieval.md` |

Развёртывание и обновление движка выполняются из kit'а, не из скилла:
`python3 <kit>/aurora.py new|setup|update <project>`.
Панель управления (все проекты машины, здоровье баз, запуск команд, справка):
`python3 <kit>/aurora.py cockpit` — см. `cockpit/README.md`.

### `sync:` — зеркала внешних систем

| Command | What it does | Reference |
|---|---|---|
| `sync:sources` | какие модули источников установлены и какие зеркала подключены (`sources_registry.py`) | `references/maintenance.md` |
| `sync:confluence` | зеркало Confluence → `Sources/Confluence/` (модуль `confluence-dc`) | `references/maintenance.md` |
| `sync:jira` | зеркало Jira → `Sources/JIRA/` (модуль `jira-dc`) | `references/maintenance.md` |
| `sync:jira-status` | обратный поток: статусы задач → кандидаты в `req_status`, работа без требований (`jira_status.py`) | `references/maintenance.md` |
| `sync:audit` | целостность зеркал: missing / orphan / collision / протухшее состояние; обходит все подключённые модули | `references/maintenance.md` |
| `sync:diff` (`diff`) | дрейф: источник изменился после сверки (сравнение хешей, скрипт) | `references/maintenance.md` |

### `kb:` — извлечение и жизнь знания

| Command | What it does | Reference |
|---|---|---|
| `kb:build` (`build`) | извлечь карточки: план и учёт — `build_plan.py`, само извлечение — модель. `--partition N` печатает готовое задание на партию: список файлов и правила | `references/build.md` |
| `kb:ingest-office [path]` | docx/pdf/xlsx/pptx из Raw/ → markdown-транскрипты рядом с оригиналом | `references/maintenance.md` |
| `kb:ingest <path>` (`ingest`, `ingest-raw`, `ingest-meeting`, `ingest-tz`) | документ из Raw/ → карточки; ветка определяется по документу: ТЗ → REQ с `tz_ref`, транскрипт встречи → резюме, DR, REQ и факты, прочее → атомарные карточки | `references/workflows.md` |
| `kb:reset` (`reset`) | обнулить базу и собрать заново: сносит из `AuroraKnowledgeDB/` то, что соберётся заново (карточка с живым `source:`), за её пределами не трогает ничего; карточки неизвестного происхождения остаются — `--list-unknown` покажет их, `--drop-unknown` снесёт; откат — из git (`kb_reset.py`) | `references/maintenance.md` |
| `kb:links` (`links`, `graph`) | граф связей: ключи Requirement Yogi и номера историй; `--cards` переносит связи в `related:` карточек (`kb_graph.py`) | `references/build.md` |
| `kb:trust` (`trust`) | класс доверия карточек: считается по таблице трассировки и статусам связанных задач. Человек доверие не присваивает | `references/maintenance.md` |
| `ops:trace-table` | таблица связей «артефакт ↔ задача»: прямые и через трассировку, с доказательством каждой | `references/maintenance.md` |
| `kb:kind` (`kind`) | тип карточки: словарь, документ или знание — от него зависит, кому позволено править тело | `docs/knowledge-rules.md` |
| `kb:repair` | ремонт: битые ссылки, гомоглифы, легаси-frontmatter, поля вне схемы, заготовки под ссылки (`kb_fix.py --all`); режимы по отдельности — флагами | `references/maintenance.md` |
| `kb:dedupe` | двойники: поиск и слияние (`kb_fix.py --dupes` / `--merge`) — тот же скрипт, другой режим | `references/maintenance.md` |
| `kb:lint` | механические ошибки базы: ссылки, frontmatter, типы карточек, артефакты в знаниях, секреты, карточки без связей | `references/maintenance.md` |
| `kb:split` | разрезать раздутую карточку по её заголовкам: части становятся атомарными, а сама она — картой документа со ссылками на них | `references/maintenance.md` |
| `kb:embed` | семантический индекс базы: вектора карточек для поиска по смыслу. Индекс лежит вне git и пересобирается; тексты уходят на тот же шлюз, что и у агента | `references/retrieval.md` |
| `kb:map` | что говорит граф: сообщества, доросшие до своей карты, мосты между темами и острова, до которых не дойти по ссылкам | `references/build.md` |
| `kb:moc` (`moc`) | карты содержания по группировкам из `moc_groups.txt` + список брошенных карточек (`kb_moc.py`) | `references/build.md` |
| `kb:index` | регенерация `_index.md` разделов (рукотворные не трогает) | `references/maintenance.md` |
| `kb:scrub` | персональные данные: найти и закрыть маркерами (`kb_scrub.py`); режим — `privacy.scrub` в конфиге; доказательства не правит | `references/maintenance.md` |
| `kb:schema` | версия схемы карточек и миграция между версиями (`kb_schema.py`) | `references/frontmatter.md` |
| `kb:question <суть>` | завести вопрос к заказчику (Q-NNN): кому, что блокирует, срок | `references/workflows.md` |
| `kb:answer <Q-NNN>` | зафиксировать ответ: закрыть вопрос и разнести знание в REQ/спеку/DR | `references/workflows.md` |
| `kb:decide <тема>` (`decide`) | оформить Decision Record (+supersede старой DR) | `references/workflows.md` |
| `kb:supersede <card>` (`supersede`) | заменить знание, сохранив историю: deprecated → `_archive`, ссылки переписываются (`kb_supersede.py`) | `references/maintenance.md` |
| `kb:garden` (`garden`) | еженедельная гигиена: протухшее, сироты, битые ссылки | `references/workflows.md` |

### `agent:` — встроенный агент (модель работает сама)

Кольцо LLM-бэкендов настраивается в панели («Настройка» → «Агент») или в
`.env.aurora.local`. У каждого бэкенда объявляется **окно контекста**: заведомо большой
запрос уходит модели с окном пошире, а не гасит провайдера ошибкой.

**Параллельность — свойство шлюза.** У каждого бэкенда две независимые роли: «в
параллельную работу» (держит поток заданий) и «запасной» (подменяет упавшего). Первый
бэкенд всегда и то, и другое. Своя ширина («потоков», `_WIDTH`) — предел этого шлюза;
`AURORA_AGENT_PARALLEL` — потолок на весь прогон. Ширина не объявлена — бэкенд делит
потолок с такими же.

Сбой на одной карточке прогон не роняет; три сбоя подряд его останавливают — это шлюз, а
не карточки.

| Command | What it does | Reference |
|---|---|---|
| `agent:ping` | проверить цепочку моделей живым запросом: каждый бэкенд, роли, скорость. Пустой ответ считается отказом | `references/build.md` |
| `agent:build` | разобрать партию источников на карточки: раскадровка, границы тем, имена, отметка о разборе. Тело переносит движок, а не модель. `--until-done` — план целиком, партиями (часы) | `references/build.md` |
| `agent:distill` | тезисы для карточек типа «знание»: модель пишет определение, дословный источник уезжает под тезис. Каждую проверяет Момус. Словарь или документ длиннее окна режется планировщиком на связанные части — тело при этом не переписывается | `docs/knowledge-rules.md` |
| `agent:aliases` | разобрать конфликты синонимов: уточнить там, где карточки разные, отложить человеку настоящие дубли | `references/maintenance.md` |
| `agent:ask` | спросить базу своими словами: движок собирает контекст, модель отвечает только по карточкам и ставит ссылку на каждое утверждение. Ответ проверяет **Момус** (роль `qa`); разговор пишется в `meta/ask/` и уходит в git | `references/retrieval.md` |

**Планировщик** (роль `planner`) выбирает границы там, где их не расставил автор:
источник без заголовков и карточка, переросшая окно модели. Он видит **опись** абзацев —
номер, размер, первые слова, — а не текст, поэтому работает и с телом, которое в окно не
влезает. Текст по его границам переносит движок дословно: к телу словаря и документа
модель не прикасается.

**Момус** — вторая модель, читающая ответ по утверждениям: у каждого либо опора с цитатой,
либо «нет опоры», либо противоречие. Он мнение, а не оракул: ответ не переписывает и не
отменяет, решение принимает человек. Утверждение с пометкой «без опоры» в артефакт не
переносится, даже если выглядит разумным.

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
| `make:kinds` | реестр артефактов проекта: какой шаблон брать и куда класть результат. Объявляется в `aurora.config.yaml`, читается ассистентом через MCP | `references/workflows.md` |
| `make:review <US/AC>` (`review`) | проверка качества артефакта против базы знаний | `references/workflows.md` |
| `make:spec <тема>` (`spec`) | собрать спецификацию фичи из REQ и карточек со статусом `knowledge` (SDD) | `references/workflows.md` |
| `make:spec-pack <SPEC-NNN>` (`spec-pack`) | бандл спеки: основания, DR, аббревиатуры, DoR-риски — сборка скриптом (`spec_pack.py`) | `references/workflows.md` |
| `make:validate <SPEC> <объект>` (`validate`) | сверить реализацию/тесты подрядчика со сценариями спеки | `references/workflows.md` |
| `make:assemble <документ>` (`assemble`) | собрать поставляемый документ (ОПЗ/ПМИ/РП) из базы по шаблону | `references/workflows.md` |

### `ship:` — наружу

| Command | What it does | Reference |
|---|---|---|
| `ship:publish <артефакт>` | артефакт → generated-страница Confluence (`publish_doc.py`); карточки знаний наружу не идут | `references/workflows.md` |
| `ship:export <документ>` | поставляемый документ → docx/pdf (pandoc, фирменный шаблон) | `references/workflows.md` |
| `ship:acceptance <объект>` | зафиксировать результаты приёмки/испытаний и разобрать замечания заказчика | `references/workflows.md` |
| `ship:release <документ>` (`release`) | заморозить переданную версию: снапшот + коммит базы + дата (`ship_doc.py --release`) | `references/maintenance.md` |

### `ops:` — управление и отчётность

| Command | What it does | Reference |
|---|---|---|
| `ops:stats` (`status`) | дашборд здоровья базы: статусы, риски, метрики (`--append-metrics` — строка в `meta/metrics.md`) | `references/maintenance.md` |
| `ops:impact <карточка>` | что зависит от карточки; `--explain <документ>` — на чём он собран | `references/maintenance.md` |
| `ops:trace` (`trace`) | перестроить трассировку: пункт ТЗ → REQ → SPEC → Jira → AC → ПМИ → приёмка | `references/workflows.md` |
| `ops:todo` | что осталось человеку: одним списком, с объяснением, почему это нельзя сделать кнопкой | `references/maintenance.md` |
| `ops:report` | дашборд эффективности аналитиков: недельная активность по Jira и Confluence; настройки — в секции `reports:` конфига | `references/maintenance.md` |
| `ops:questions` | реестр вопросов: открытые, просроченные, что блокируют (числа — из `aurora_stats.py`) | `references/workflows.md` |

Модель знания (классы источников, связи, три типа карточек): `docs/knowledge-rules.md`.
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
| класс доверия | `kb_trust.py` | ничего: доверие считается по статусам задач |
| таблица трассировки | `kb_trace_table.py` | разбор карточек, оставшихся без связей |
| тип карточки | `kb_kind.py` | спорные случаи: словарь это или знание |
| разбор источников | `agent_runner.py --task build` | сам разбор — это и есть модель |
| тезисы карточек | `agent_runner.py --task distill` | написать тезис, Момус — проверить |
| переезд зеркала | `kb_remap.py` | разбор несопоставленных источников |
| маршрутизация карточек | `kb_lint.py` | решение «артефакт или знание» по каждой находке |
| сборка context pack | `ctx_pack.py` | сам ответ по паку |
| замена знания | `kb_supersede.py` | решение «это устарело» и текст преемника |
| обратная трассировка | `kb_trace.py --impact` | что делать с затронутыми документами |
| зеркало Confluence | `confluence_export.py` | разбор новых и изменившихся страниц |
| зеркало Jira | `jira_export.py` | разбор новых и изменившихся задач |
| дрейф источников | `sync_audit.py --drift` | что делать с изменившимся знанием |
| фиксация переданного | `ship_doc.py --release` | что сдаём и с каким риском |
| целостность зеркал | `sync_audit.py` | что досинхронизировать |
| метрики и дашборд | `aurora_stats.py` | комментарий к динамике |
| план извлечения | `build_plan.py` | само извлечение карточек из источника |
| офисные первоисточники | `office_ingest.py` | извлечение карточек из транскрипта |
| экспорт документа | `ship_doc.py --export` | вычитка результата перед передачей |
| трассировка | `kb_trace.py --requirements` | разбор разрывов |
| готовность проекта | `aurora_doctor.py` | починка конфига |

## Invariants (never violate, any command)

1. **Артефакт ≠ знание.** Проверяемый/генерируемый документ — «объект», знание — только
   карточки AuroraKnowledgeDB с фильтром по статусу. Artifacts/ и Sources/ в контекст знаний
   не попадают.
2. **Ничего не удалять.** Устаревшее знание → `supersede` (deprecated + _archive).
   Decision Records неизменяемы после accepted/rejected.
3. **Доверие вычисляется, а не присваивается.** Класс карточки выводится из статусов
   связанных задач Jira через таблицу трассировки (`ops:trace-table` → `kb:trust`).
   Человек доверие не ставит и не снимает: если задача вернулась в работу, карточка
   становится черновиком сама. Шкала закрыта: `knowledge` · `draft` · служебные `index`
   и `deprecated`. Статусов `verified`, `in-review`, `accepted` больше нет — они
   читаются на старых базах, но не назначаются.
4. **Каждое включение карточки в промпт — с шапкой доверия** (см. retrieval.md).
5. **Тип карточки решает, кому позволено править тело.** `dictionary` переносится
   целиком, `document` — дословно и не переписывается никогда, `knowledge` модель
   переосмысляет в тезис, оставляя источник под ним. Тип, выбранный человеком, движок
   не перезаписывает. Подробно — `docs/knowledge-rules.md`.
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
