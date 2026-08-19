# Команды Aurora Studio

Справочник собран автоматически (`kit:list`) для версии движка **1.86.0**.
Модификаторы взяты из `--help` самих скриптов, поэтому не расходятся с кодом;
остальное — из реестра `commands.txt`. Править руками этот файл бессмысленно:
он перезаписывается командой `python3 .opencode/scripts/kit_commands.py --md`.

Короткие имена в скобках — исторические алиасы, работают всегда.
«Исполнитель» показывает, где проходит граница: **скрипт** — детерминированная
механика, её результат воспроизводим; **модель** — работа со смыслом;
**скрипт+модель** — скрипт считает и готовит, решение принимает человек.

## `kit: — движок и проект`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `kit:doctor` (`doctor`) | готовность проекта: config, скиллы, секреты в git, версия движка, структура папок; `--structure` — подробно по папкам вне схемы | скрипт | `aurora_doctor.py` | `--structure` | 1.0.0 |
| `kit:hooks` | git-хуки: pre-commit с линтером и храповиком (ошибки не растут) и commit-msg — внутренние названия не уходят в историю | скрипт | `aurora_hooks.py` | `--install --uninstall --status --mode --force` | 1.3.0 |
| `kit:remap-sources` (`remap`) | перенацелить `source:` карточек после переезда зеркала (Confluence — по page_id, Jira — по ключу задачи) | скрипт | `kb_remap.py` | `--mirror --snapshot --from-git --apply --report` | 1.7.0 |
| `kit:update` | обновить движок в проекте до версии kit; `--structure-only` — только папки схемы | скрипт | `aurora_update.py` `[target]` | `--apply --structure-only` | 1.3.0 |
| `kit:skills` | скиллы Авроры в общий каталог агента (~/.claude/skills): без этого /aurora-vault и /aurora-dev не находятся ни в одном диалоге | скрипт | `install_skills.py` | `--status --apply` | 1.54.0 |
| `kit:mcp` (`mcp`) | база знаний как инструмент любого ассистента: проверка сервера и готовая строка подключения; сервер только читает — писать в базу через MCP нельзя | скрипт | `aurora_mcp.py --selftest` | — | 1.66.0 |
| `kit:list` | этот справочник: команды, модификаторы, чем исполняются, с какой версии | скрипт | `kit_commands.py` `[namespace]` | `--search --md --check` | 1.9.8 |

## `sync: — зеркала внешних систем`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `sync:sources` (`sources`) | модули источников: что установлено и какие зеркала подключены к проекту | скрипт | `sources_registry.py` | `--json` | 1.28.0 |
| `sync:confluence` | детерминированное зеркало Confluence → Sources/Confluence/ (модуль confluence-dc) | скрипт | `confluence_export.py` | `--roots --out --force --prune --verify` | 1.6.0 |
| `sync:jira` | детерминированное зеркало Jira → Sources/JIRA/ (модуль jira-dc) | скрипт | `jira_export.py` | `--jql --out --limit --force --comments --prune --verify` | 1.9.0 |
| `sync:audit` (`audit`) | целостность зеркал: missing / orphan / collision / протухшее состояние; обходит все подключённые модули | скрипт | `sync_audit.py` | `--stale-days --report --source --json --drift --all --stamp --apply --allow-dirty --confluence-only --jira-only` | 1.3.0 |
| `sync:diff` (`diff`) | дрейф: источник изменился после того, как знание проверили | скрипт | `sync_audit.py --drift` | `--stale-days --report --source --json --all --stamp --apply --allow-dirty --confluence-only --jira-only` | 1.9.1 |
| `sync:jira-status` | обратный поток: статусы задач → кандидаты в `req_status`, задачи без требований, связи по упоминаниям | скрипт | `jira_status.py` | `--apply --link --allow-dirty --report` | 1.9.9 |

## `kb: — извлечение и жизнь знания`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `kb:build` (`build`) | извлечение карточек: план, партии и учёт — скриптом, само извлечение — моделью; `--slice` режет источник на секции, `--card` собирает карточку из них (текст переносит скрипт), `--reopen` и `--thin` возвращают в план недоразобранное | скрипт+модель | `build_plan.py` | `--budget --max-files --partition --tasks --from --done --cards --empty --status --slice --slice-chars --card --source --summary --sections --to --thin --reopen --group --apply` | 1.0.0 |
| `kb:ingest-office` | docx/pdf/xlsx/pptx из Raw/ → markdown-транскрипты рядом с оригиналом | скрипт | `office_ingest.py` `[paths ...]` | `--root --converter --force --dry-run` | 1.5.0 |
| `kb:ingest` (`ingest, ingest-raw, ingest-meeting, ingest-tz`) | документ из Raw/ → карточки со ссылкой на первоисточник; вид входа определяется по документу: ТЗ → REQ с `tz_ref`, транскрипт встречи → резюме, DR, REQ и факты | модель | `workflows.md` | — | 1.0.0 |
| `kb:queue` (`queue`) | очередь верификации: что проверять первым по связям и попаданию в артефакты | скрипт | `aurora_stats.py --queue` | `--limit --theme --json --append-metrics --report` | 1.3.0 |
| `kb:verify` (`verify, promote`) | гейт imported/draft → verified: отбор человеком, запись скриптом (итог: доля принятых растёт — это единственная команда, которая её двигает); `verified` — верхний статус базы | скрипт+модель | `kb_verify.py` `[selector]` | `--owner --months --by-source --stubs --by-links --demote --demote-machine --auto --by-jira --source-older-than --refresh --apply --allow-dirty` | 1.8.0 |
| `kb:repair` (`fix`) | ремонт: битые ссылки, гомоглифы, легаси-frontmatter, поля вне схемы, заготовки под ссылки | скрипт | `kb_fix.py --all` | `--links --homoglyphs --retire --frontmatter --stubs --aliases --split --split-min --set-alias --old --new --drop-alias --dupes --merge --merge-all --apply --allow-dirty --json --report --root` | 1.3.0 |
| `kb:dedupe` | двойники: поиск, пакетное слияние по правилу (`--merge-all`) и разбор одной пары (`--merge` «оставить» «убрать») | скрипт | `kb_fix.py --dupes` | `--links --homoglyphs --retire --frontmatter --stubs --aliases --split --split-min --set-alias --old --new --drop-alias --all --merge --merge-all --apply --allow-dirty --json --report --root` | 1.3.0 |
| `kb:split` (`split`) | разрезать раздутую карточку по её заголовкам: части становятся атомарными карточками, а сама она — картой документа со ссылками на них | скрипт | `kb_fix.py --split` | `--links --homoglyphs --retire --frontmatter --stubs --aliases --split-min --set-alias --old --new --drop-alias --dupes --all --merge --merge-all --apply --allow-dirty --json --report --root` | 1.62.0 |
| `kb:embed` (`embed`) | семантический индекс базы: вектора карточек для поиска по смыслу; индекс лежит вне git и пересобирается, тексты уходят на тот же шлюз, что и у агента | скрипт+модель | `kb_embed.py` | `--status --apply --all --query` | 1.65.0 |
| `kb:moc` (`moc`) | карты содержания по группировкам (термины, понятия, роли, данные…) и список брошенных карточек | скрипт | `kb_moc.py` | `--apply --suggest --by-source --orphans --allow-dirty` | 1.32.0 |
| `kb:index` (`index`) | регенерация `_index.md` разделов; рукотворные не трогает, но отставшие называет находкой (код 1) | скрипт | `kb_index.py` | `--section --root-index --apply --force` | 1.9.4 |
| `kb:scrub` (`scrub`) | персональные данные: найти и закрыть маркерами; режим — `privacy.scrub` | скрипт | `kb_scrub.py` `[path]` | `--include-raw --force --mask-contacts --apply --allow-dirty` | 1.9.6 |
| `kb:schema` | версия схемы карточек (`schema_version`) и перевод базы между версиями по объявленной цепочке | скрипт | `kb_schema.py` | `--to --apply --allow-dirty --root` | 1.12.0 |
| `kb:supersede` (`supersede`) | заменить знание с историей: deprecated → `_archive`, ссылки переписываются | скрипт | `kb_supersede.py` `old new` | `--dr --reason --apply` | 1.8.0 |
| `kb:links` (`links, graph`) | граф связей: ключи Requirement Yogi и номера историй; `--cards` переносит связи в `related:` карточек | скрипт | `kb_graph.py` | `--story --write --json --cards --apply --insights --allow-dirty --max-related --report --conf --jira` | 1.19.0 |
| `kb:map` (`map`) | что говорит граф: сообщества, доросшие до своей карты, мосты между темами и острова, до которых не дойти по ссылкам | скрипт | `kb_graph.py --insights` | `--story --write --json --cards --apply --allow-dirty --max-related --report --conf --jira` | 1.66.0 |
| `kb:reset` (`reset`) | обнулить базу и собрать заново: сносит всё содержимое AuroraKnowledgeDB/, за её пределами не трогает ничего; `--keep-handmade` оставляет то, чего нет в источниках; откат — из git | скрипт | `kb_reset.py` | `--apply --keep-handmade --backup --allow-dirty` | 1.24.0 |
| `kb:lint` (`lint`) | механические ошибки базы: ссылки, frontmatter, типы карточек, артефакты в знаниях, секреты | скрипт | `kb_lint.py` | `--full --summary` | 1.0.0 |
| `kb:question` | завести вопрос к заказчику (Q-NNN): кому, что блокирует, срок | модель | `workflows.md` | — | 1.4.0 |
| `kb:answer` | зафиксировать ответ: закрыть вопрос и разнести знание в REQ/спеку/DR | модель | `workflows.md` | — | 1.4.0 |
| `kb:decide` (`decide`) | оформить Decision Record (+supersede старой DR) | модель | `workflows.md` | — | 1.0.0 |
| `kb:garden` (`garden`) | еженедельная гигиена: чеклист из четырёх скриптов, разбор — человеком | скрипт+модель | `workflows.md` | — | 1.0.0 |

## `ctx: — использование знаний`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ctx:context` (`context`) | context pack: отбор, фильтр статусов, шапки доверия, запись в `usage.log` | скрипт | `ctx_pack.py` `topic` | `--mode --max-cards --budget --release --save --no-log --no-semantic --index` | 1.8.0 |
| `ctx:ask` (`ask`) | ответ по базе с цитатами; «почему не X» — включая отклонённые DR | модель | `workflows.md` | — | 1.0.0 |
| `ctx:eval` (`eval`) | регрессионный прогон golden questions после синков и миграций | модель | `workflows.md` | — | 1.0.0 |
| `ctx:retro` (`retro`) | выученные уроки: чего база не знала, когда мы ошиблись | модель | `workflows.md` | — | 1.0.0 |

## `make: — производство артефактов`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `make:kinds` (`kinds`) | реестр артефактов проекта: какой шаблон брать и куда класть результат; объявляется в aurora.config.yaml, читается ассистентом через MCP | скрипт | `make_kinds.py` | `--kind --json --root` | 1.69.0 |
| `make:create` (`create`) | артефакт в `Artifacts/<тип>/` — только стандартный тип из conventions.md | модель | `workflows.md` | — | 1.1.0 |
| `make:review` (`review`) | проверка качества артефакта против базы знаний | модель | `workflows.md` | — | 1.0.0 |
| `make:spec` (`spec`) | спецификация фичи из REQ и verified-знаний (SDD) | модель | `workflows.md` | — | 1.0.0 |
| `make:spec-pack` (`spec-pack`) | бандл спеки: основания, DR, аббревиатуры, DoR-риски — самодостаточный файл | скрипт | `spec_pack.py` `spec` | `--version --apply` | 1.9.4 |
| `make:validate` (`validate`) | сверить реализацию и тесты подрядчика со сценариями спеки | модель | `workflows.md` | — | 1.0.0 |
| `make:assemble` (`assemble`) | собрать поставляемый документ (ОПЗ/ПМИ/РП) из базы по шаблону | модель | `workflows.md` | — | 1.0.0 |

## `ship: — наружу`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ship:publish` (`publish`) | артефакт → generated-страница Confluence; карточки знаний наружу не идут | скрипт | `publish_doc.py` `path` | `--parent --title --adopt --apply` | 1.9.6 |
| `ship:export` (`export`) | поставляемый документ → docx/pdf (pandoc, фирменный шаблон) | скрипт | `ship_doc.py --export docx` `document` | `--release --reference --out --keep-links --version --date --binary --apply` | 1.5.0 |
| `ship:release` (`release`) | заморозить переданную версию: снапшот, коммит базы, дата | скрипт | `ship_doc.py --release` `document` | `--export --reference --out --keep-links --version --date --binary --apply` | 1.9.1 |
| `ship:acceptance` | результаты приёмки и разбор замечаний заказчика | модель | `workflows.md` | — | 1.4.0 |

## `ops: — управление и отчётность`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ops:stats` (`status, stats`) | дашборд здоровья базы: статусы, риски, метрики | скрипт | `aurora_stats.py` | `--queue --limit --theme --json --append-metrics --report` | 1.3.0 |
| `ops:todo` (`todo`) | что осталось человеку: остаток приёмки, документы в базе, двойники, неразобранные источники — одним списком с объяснением, почему это нельзя сделать кнопкой | скрипт | `aurora_todo.py` | — | 1.84.0 |
| `ops:impact` (`impact`) | что зависит от карточки; `--explain` — на чём собран документ | скрипт | `kb_trace.py --impact` `[target]` | `--explain --requirements` | 1.8.0 |
| `ops:trace` (`trace`) | трассировка: пункт ТЗ → REQ → SPEC → Jira → AC → ПМИ → приёмка | скрипт | `kb_trace.py --requirements` `[target]` | `--impact --explain` | 1.0.0 |
| `ops:questions` | реестр вопросов: открытые, просроченные, что блокируют | скрипт+модель | `workflows.md` | — | 1.4.0 |
| `ops:report` (`report`) | дашборд эффективности аналитиков: недельная активность по Jira и Confluence, переходы задач; настройки — в секции reports: конфига | скрипт | `report_analyst.py` | `--skip-fetch --serve` | 1.78.0 |

## `agent: — встроенный агент`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `agent:aliases` | агент разбирает конфликты синонимов: уточняет там, где карточки разные, и откладывает человеку дубли; правит только через команды движка | скрипт+модель | `agent_runner.py --task aliases` | `--question --mode --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.57.0 |
| `agent:build` | агент разбирает партию источников на карточки (итог: новые карточки со статусом imported, доверие не присваивается): раскадровка, границы тем, имена, отметка о разборе; тело карточек переносит движок, а не модель; `--until-done` разбирает план целиком партиями (первичная сборка, часы) | скрипт+модель | `agent_runner.py --task build` | `--question --mode --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.58.0 |
| `agent:ask` | спросить базу своими словами: движок собирает контекст, модель отвечает только по карточкам и ставит ссылку на каждое утверждение; ответ проверяет Момус (роль qa) и разбор ссылок по базе; `--backend N` спрашивает конкретную модель из списка; разговор пишется в `meta/ask/` и уходит в git с базой, `--thread` продолжает его уточняющим вопросом | скрипт+модель | `agent_runner.py --task ask` | `--question --mode --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.63.0 |
| `agent:ping` | встроенный агент: проверить цепочку моделей — каждый бэкенд живым запросом, пустой ответ считается отказом | скрипт | `agent_core.py --ping` | `--show --venv-status --venv-install --json` | 1.56.0 |

## `dev: — разработка движка (только в ките)`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `dev:qa-list` | QA движка: какие есть кейсы и сценарии, что закрыто автотестом, какие кейсы не гоняются ни разу | скрипт | `dev_qa.py --list` | `--check --gap --cover --base --run --apply --record --new` | 1.51.0 |
| `dev:qa-check` | целостность реестра QA: дубли номеров, ссылки на несуществующие кейсы, отставшие версии | скрипт | `dev_qa.py --check` | `--list --gap --cover --base --run --apply --record --new` | 1.51.0 |
| `dev:qa-gap` | что изменено в коде и чем покрыто; решение «автотест или кейс» принимает модель | скрипт+модель | `dev_qa.py --gap` | `--list --check --cover --base --run --apply --record --new` | 1.51.0 |
| `dev:qa-cover` | покрыть сделанное: таблица покрытия и готовое задание ассистенту — дополнить автотесты, кейсы и сценарии | скрипт+модель | `dev_qa.py --cover` | `--list --check --gap --base --run --apply --record --new` | 1.53.0 |
| `dev:qa-run` | прогон сценария с записью журнала: автотесты, чек-лист шагов, отчёт в Development/QA/runs/ | скрипт+модель | `dev_qa.py --run` | `--list --check --gap --cover --base --apply --record --new` | 1.51.0 |
| `dev:qa-new` | завести тест-кейс или сценарий из шаблона со следующим свободным номером | скрипт | `dev_qa.py --new` | `--list --check --gap --cover --base --run --apply --record` | 1.51.0 |

## Развёртывание (из клона kit'а, не из проекта)

| Команда | Что делает |
|---|---|
| `python3 aurora.py new <target>` | развернуть Aurora в проект: скелет, движок, интерактивная настройка |
| `python3 aurora.py setup <target>` | перенастроить проект (Confluence, Jira, приватность, пороги) |
| `python3 aurora.py update <target>` | обновить движок до версии kit; `--apply` пишет, `--structure-only` — только папки |

Любую команду обслуживания можно звать и из kit'а: `python3 aurora.py <команда> <target> [флаги]`.

