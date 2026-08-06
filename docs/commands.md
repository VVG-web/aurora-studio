# Команды Aurora Studio

Справочник собран автоматически (`kit:list`) для версии движка **1.44.2**.
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
| `kit:doctor` (`doctor`) | готовность проекта: config, скиллы, секреты в git, версия движка, структура папок | скрипт | `aurora_doctor.py` | `--structure` | 1.0.0 |
| `kit:structure` | сверить фактические папки со схемой движка (structure_dirs.txt); gitignore-папки допустимы | скрипт | `aurora_doctor.py --structure` | — | 1.3.0 |
| `kit:hooks` | git pre-commit с линтером и храповиком: число ошибок не должно расти | скрипт | `aurora_hooks.py` | `--install --uninstall --status --mode --force` | 1.3.0 |
| `kit:remap-sources` (`remap`) | перенацелить `source:` карточек после переезда зеркала (Confluence — по page_id, Jira — по ключу задачи) | скрипт | `kb_remap.py` | `--mirror --snapshot --from-git --apply --report` | 1.7.0 |
| `kit:update` | обновить движок в проекте до версии kit; `--structure-only` — только папки схемы | скрипт | `aurora_update.py` `[target]` | `--apply --structure-only` | 1.3.0 |
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
| `kb:build` (`build`) | извлечение карточек: план, партии и учёт — скриптом, само извлечение — моделью | скрипт+модель | `build_plan.py` | `--budget --max-files --partition --tasks --from --done --cards --status` | 1.0.0 |
| `kb:ingest-office` | docx/pdf/xlsx/pptx из Raw/ → markdown-транскрипты рядом с оригиналом | скрипт | `office_ingest.py` `[paths ...]` | `--root --converter --force --dry-run` | 1.5.0 |
| `kb:ingest-raw` (`ingest-raw`) | документ из Raw/ → карточки-кандидаты со ссылкой на первоисточник | модель | `workflows.md` | — | 1.0.0 |
| `kb:ingest-meeting` (`ingest-meeting`) | транскрипт встречи → резюме, решения (DR), требования (REQ), факты | модель | `workflows.md` | — | 1.4.0 |
| `kb:ingest-tz` (`ingest-tz`) | ТЗ по пунктам → REQ-карточки с `tz_ref` | модель | `workflows.md` | — | 1.4.0 |
| `kb:queue` (`queue`) | очередь верификации: что проверять первым по связям и попаданию в артефакты | скрипт | `aurora_stats.py --queue` | `--limit --theme --json --append-metrics --report` | 1.3.0 |
| `kb:verify` (`verify, promote`) | гейт imported/draft → verified: отбор человеком, запись скриптом; `verified` — верхний статус базы | скрипт+модель | `kb_verify.py` `[selector]` | `--owner --months --by-source --stubs --by-links --auto --by-jira --source-older-than --refresh --apply --allow-dirty` | 1.8.0 |
| `kb:repair` (`fix`) | ремонт: битые ссылки, гомоглифы, легаси-frontmatter | скрипт | `kb_fix.py --all` | `--links --homoglyphs --retire --frontmatter --stubs --aliases --drop-alias --dupes --merge --apply --allow-dirty --report --root` | 1.3.0 |
| `kb:retire` | убрать из карточек поля, выведенные из схемы (audience, confirmed_by; статус canonical → verified) | скрипт | `kb_fix.py --retire` | `--links --homoglyphs --frontmatter --stubs --aliases --drop-alias --dupes --all --merge --apply --allow-dirty --report --root` | 1.10.0 |
| `kb:dedupe` | двойники: поиск и слияние (`--merge`) — тот же скрипт, другой режим | скрипт | `kb_fix.py --dupes` | `--links --homoglyphs --retire --frontmatter --stubs --aliases --drop-alias --all --merge --apply --allow-dirty --report --root` | 1.3.0 |
| `kb:moc` (`moc`) | карты содержания по группировкам (термины, понятия, роли, данные…) и список брошенных карточек | скрипт | `kb_moc.py` | `--apply --suggest --orphans --allow-dirty` | 1.32.0 |
| `kb:index` (`index`) | регенерация `_index.md` разделов; рукотворные оглавления не трогает | скрипт | `kb_index.py` | `--section --root-index --apply --force` | 1.9.4 |
| `kb:scrub` (`scrub`) | персональные данные: найти и закрыть маркерами; режим — `privacy.scrub` | скрипт | `kb_scrub.py` `[path]` | `--include-raw --force --mask-contacts --apply --allow-dirty` | 1.9.6 |
| `kb:schema` | версия схемы карточек (`schema_version`) и перевод базы между версиями по объявленной цепочке | скрипт | `kb_schema.py` | `--to --apply --allow-dirty --root` | 1.12.0 |
| `kb:supersede` (`supersede`) | заменить знание с историей: deprecated → `_archive`, ссылки переписываются | скрипт | `kb_supersede.py` `old new` | `--dr --reason --apply` | 1.8.0 |
| `kb:links` (`links, graph`) | граф связей: ключи Requirement Yogi и номера историй; `--cards` переносит связи в `related:` карточек | скрипт | `kb_graph.py` | `--story --write --json --cards --apply --max-related --report --conf --jira` | 1.19.0 |
| `kb:reset` (`reset`) | обнулить базу и собрать заново: сносит всё содержимое AuroraKnowledgeDB/, за её пределами не трогает ничего; `--keep-handmade` оставляет то, чего нет в источниках; откат — из git | скрипт | `kb_reset.py` | `--apply --keep-handmade --backup --allow-dirty` | 1.24.0 |
| `kb:lint` (`lint`) | механические ошибки базы: ссылки, frontmatter, типы карточек, артефакты в знаниях, секреты | скрипт | `kb_lint.py` | `--summary` | 1.0.0 |
| `kb:question` | завести вопрос к заказчику (Q-NNN): кому, что блокирует, срок | модель | `workflows.md` | — | 1.4.0 |
| `kb:answer` | зафиксировать ответ: закрыть вопрос и разнести знание в REQ/спеку/DR | модель | `workflows.md` | — | 1.4.0 |
| `kb:decide` (`decide`) | оформить Decision Record (+supersede старой DR) | модель | `workflows.md` | — | 1.0.0 |
| `kb:garden` (`garden`) | еженедельная гигиена: чеклист из четырёх скриптов, разбор — человеком | скрипт+модель | `workflows.md` | — | 1.0.0 |

## `ctx: — использование знаний`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ctx:context` (`context`) | context pack: отбор, фильтр статусов, шапки доверия, запись в `usage.log` | скрипт | `ctx_pack.py` `topic` | `--mode --max-cards --budget --release --save --no-log` | 1.8.0 |
| `ctx:ask` (`ask`) | ответ по базе с цитатами; «почему не X» — включая отклонённые DR | модель | `workflows.md` | — | 1.0.0 |
| `ctx:eval` (`eval`) | регрессионный прогон golden questions после синков и миграций | модель | `workflows.md` | — | 1.0.0 |
| `ctx:retro` (`retro`) | выученные уроки: чего база не знала, когда мы ошиблись | модель | `workflows.md` | — | 1.0.0 |

## `make: — производство артефактов`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
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
| `ops:impact` (`impact`) | что зависит от карточки; `--explain` — на чём собран документ | скрипт | `kb_trace.py --impact` `[target]` | `--explain --requirements` | 1.8.0 |
| `ops:trace` (`trace`) | трассировка: пункт ТЗ → REQ → SPEC → Jira → AC → ПМИ → приёмка | скрипт | `kb_trace.py --requirements` `[target]` | `--impact --explain` | 1.0.0 |
| `ops:questions` | реестр вопросов: открытые, просроченные, что блокируют | скрипт+модель | `workflows.md` | — | 1.4.0 |

## Развёртывание (из клона kit'а, не из проекта)

| Команда | Что делает |
|---|---|
| `python3 aurora.py new <target>` | развернуть Aurora в проект: скелет, движок, интерактивная настройка |
| `python3 aurora.py setup <target>` | перенастроить проект (Confluence, Jira, приватность, пороги) |
| `python3 aurora.py update <target>` | обновить движок до версии kit; `--apply` пишет, `--structure-only` — только папки |

Любую команду обслуживания можно звать и из kit'а: `python3 aurora.py <команда> <target> [флаги]`.

