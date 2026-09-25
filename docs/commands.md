# Команды Aurora Studio

Справочник собран автоматически (`kit:list`) для версии движка **1.134.0**.
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
| `kit:hooks` | git-хуки: pre-commit с линтером и храповиком (ошибки не растут) и commit-msg — внутренние названия не уходят в историю | скрипт | `aurora_hooks.py` | `--install --uninstall --status --mode --force --scan-push` | 1.3.0 |
| `kit:remap-sources` (`remap`) | перенацелить `source:` карточек после переезда зеркала (Confluence — по page_id, Jira — по ключу задачи) | скрипт | `kb_remap.py` | `--mirror --snapshot --from-git --moved --apply --report` | 1.7.0 |
| `kit:update` | обновить движок в проекте до версии kit; `--structure-only` — только папки схемы | скрипт | `aurora_update.py` `[target]` | `--apply --structure-only` | 1.3.0 |
| `kit:i18n` | языки интерфейса панели: полнота каждого каталога и сверка с разметкой. Ключ, которого нет в переводе, показывается по-русски — неполный перевод выглядит рабочим, поэтому полнота проверяется числом; `--new <код> <название>` заводит каталог из русского | скрипт | `kit_i18n.py` | `--check --lang --new` | 1.98.0 |
| `kit:skills` | скиллы Авроры в общий каталог агента (~/.claude/skills): без этого /aurora-vault и /aurora-dev не находятся ни в одном диалоге | скрипт | `install_skills.py` | `--status --apply` | 1.54.0 |
| `kit:mcp` (`mcp`) | база знаний как инструмент любого ассистента: проверка сервера и готовая строка подключения; сервер только читает — писать в базу через MCP нельзя | скрипт | `aurora_mcp.py --selftest` | — | 1.66.0 |
| `kit:list` | этот справочник: команды, модификаторы, чем исполняются, с какой версии | скрипт | `kit_commands.py` `[namespace]` | `--search --md --check` | 1.9.8 |

## `sync: — зеркала внешних систем`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `sync:sources` (`sources`) | модули источников: что установлено и какие зеркала подключены к проекту | скрипт | `sources_registry.py` | `--json` | 1.28.0 |
| `sync:confluence` | детерминированное зеркало Confluence → Sources/Confluence/ (модуль confluence-dc) | скрипт | `confluence_export.py` | `--roots --out --force --prune --verify` | 1.6.0 |
| `sync:web` | страницы по списку адресов → Sources/Web/ (модуль web); доверие ставится галочкой на каждую ссылку и уходит в шапку сохранённого файла | скрипт | `web_export.py` | `--out --apply --prune --verify` | 1.101.0 |
| `sync:jira` | детерминированное зеркало Jira → Sources/JIRA/ (модуль jira-dc) | скрипт | `jira_export.py` | `--jql --out --limit --force --comments --prune --verify` | 1.9.0 |
| `sync:audit` (`audit`) | целостность зеркал: missing / orphan / collision / протухшее состояние; обходит все подключённые модули | скрипт | `sync_audit.py` | `--stale-days --report --source --json --drift --all --stamp --apply --allow-dirty --confluence-only --jira-only` | 1.3.0 |
| `sync:diff` (`diff`) | дрейф: источник изменился после того, как знание проверили | скрипт | `sync_audit.py --drift` | `--stale-days --report --source --json --all --stamp --apply --allow-dirty --confluence-only --jira-only` | 1.9.1 |
| `sync:jira-status` | обратный поток: статусы задач → кандидаты в `req_status`, задачи без требований, связи по упоминаниям | скрипт | `jira_status.py` | `--apply --link --allow-dirty --report` | 1.9.9 |

## `kb: — извлечение и жизнь знания`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `kb:build` (`build`) | извлечение карточек: план, партии и учёт — скриптом, само извлечение — моделью; `--slice` режет источник на секции, `--card` собирает карточку из них (текст переносит скрипт), `--reopen` и `--thin` возвращают в план недоразобранное | скрипт+модель | `build_plan.py` | `--budget --max-files --partition --tasks --from --done --cards --empty --status --slice --slice-chars --by --card --append --source --summary --sections --paras --to --thin --reopen --group --apply --retry-failed` | 1.0.0 |
| `kb:ingest-office` | docx/pdf/xlsx/pptx из Raw/ → markdown-транскрипты рядом с оригиналом | скрипт | `office_ingest.py` `[paths ...]` | `--root --converter --no-ocr --force --dry-run` | 1.5.0 |
| `kb:ingest` (`ingest, ingest-raw, ingest-meeting, ingest-tz`) | документ из Raw/ → карточки со ссылкой на первоисточник; вид входа определяется по документу: ТЗ → REQ с `tz_ref`, транскрипт встречи → резюме, DR, REQ и факты | модель | `workflows.md` | — | 1.0.0 |
| `kb:repair` (`fix`) | ремонт: битые ссылки, гомоглифы, легаси-frontmatter, поля вне схемы, заготовки под ссылки; `--terms` заводит заготовки под понятия, названные в базе словами, с расшифровкой из словаря проекта; `--rename СТАРОЕ НОВОЕ` называет карточку иначе, прежнее имя уходит в синонимы; `--names` снимает код документа с имени карточки (знание называется по объекту, код и прежнее имя уходят в синонимы), `--sections` развозит карточки по разделам, отвечающим их типу — раздел это тип, записанный папкой | скрипт | `kb_fix.py --all` | `--links --homoglyphs --retire --frontmatter --stubs --themes --unparsed --drop-jira --meetings --stale-stubs --drop-code-stubs --rename --terms --names --sections --aliases --split --split-min --set-alias --old --new --drop-alias --dupes --merge --merge-all --apply --allow-dirty --json --report --root` | 1.3.0 |
| `kb:translit` (`translit`) | словарь имён латиница→кириллица: находит карточки с транслитерованным именем при русском содержимом, ведёт один перевод на понятие в `meta/translit.md` и переименовывает по нему (`--rename`), уводя старое написание в синонимы | скрипт | `kb_translit.py` | `--apply --rename --allow-dirty` | 1.100.31 |
| `kb:dedupe` | двойники: поиск, пакетное слияние по правилу (`--merge-all`) и разбор одной пары (`--merge` «оставить» «убрать») | скрипт | `kb_fix.py --dupes` | `--links --homoglyphs --retire --frontmatter --stubs --themes --unparsed --drop-jira --meetings --stale-stubs --drop-code-stubs --rename --terms --names --sections --aliases --split --split-min --set-alias --old --new --drop-alias --all --merge --merge-all --apply --allow-dirty --json --report --root` | 1.3.0 |
| `kb:twins` (`twins`) | карточки, несущие одно знание разными именами: `kb:dedupe` сверяет имена, а этот — тексты (общие куски по восемь слов, вёрстка отброшена). Ничего не сливает: называет группу, предлагает кого оставить и почему | скрипт | `kb_twins.py` | `--min --limit --report` | 1.100.31 |
| `kb:split` (`split`) | разрезать раздутую карточку по её заголовкам: части становятся атомарными карточками, а сама она — картой документа со ссылками на них | скрипт | `kb_fix.py --split` | `--links --homoglyphs --retire --frontmatter --stubs --themes --unparsed --drop-jira --meetings --stale-stubs --drop-code-stubs --rename --terms --names --sections --aliases --split-min --set-alias --old --new --drop-alias --dupes --all --merge --merge-all --apply --allow-dirty --json --report --root` | 1.62.0 |
| `kb:embed` (`embed`) | семантический индекс базы: вектора карточек для поиска по смыслу; индекс лежит вне git и пересобирается, тексты уходят на тот же шлюз, что и у агента | скрипт+модель | `kb_embed.py` | `--status --apply --all --query` | 1.65.0 |
| `kb:moc` (`moc`) | карты содержания по группировкам (термины, понятия, роли, данные…) и список брошенных карточек | скрипт | `kb_moc.py` | `--apply --suggest --by-code --by-source --orphans --allow-dirty` | 1.32.0 |
| `kb:index` (`index`) | регенерация `_index.md` разделов; рукотворные не трогает, но отставшие называет находкой (код 1) | скрипт | `kb_index.py` | `--section --root-index --apply --force` | 1.9.4 |
| `kb:scrub` (`scrub`) | персональные данные: найти и закрыть маркерами; режим — `privacy.scrub` | скрипт | `kb_scrub.py` `[path]` | `--include-raw --force --mask-contacts --apply --allow-dirty` | 1.9.6 |
| `kb:schema` | версия схемы карточек (`schema_version`) и перевод базы между версиями по объявленной цепочке | скрипт | `kb_schema.py` | `--to --apply --allow-dirty --root` | 1.12.0 |
| `kb:correct` (`correct`) | корректирующие артефакты: человек пишет своими словами, что в карточке неверно и как на самом деле. Живут в `Raw/corrections/` и применяются при КАЖДОЙ сборке — иначе приоритет над Confluence держится до следующего синка. Доверие получают существующим правилом «первоисточник в Raw/», второго способа получить доверие не появляется | скрипт | `kb_corrections.py` | `--new --text --list --check --retire --reason --apply` | 1.99.0 |
| `kb:supersede` (`supersede`) | заменить знание с историей: deprecated → `_archive`, ссылки переписываются. Требование без `--changed` и `--migration` не заменяется: момент замены — единственный, когда человек помнит, что и почему изменилось | скрипт | `kb_supersede.py` `old new` | `--dr --reason --changed --migration --apply` | 1.8.0 |
| `kb:links` (`links, graph`) | граф связей: ключи Requirement Yogi и номера историй; `--cards` переносит связи в `related:` карточек; `--cards-json` выгружает граф самой базы для панели — карточки и то, что между ними написано (ссылки в тексте и related), зеркало Confluence для этого не нужно | скрипт | `kb_graph.py` | `--story --write --json --cards-json --cards --apply --insights --allow-dirty --max-related --report --conf --jira` | 1.19.0 |
| `kb:map` (`map`) | что говорит граф: сообщества, доросшие до своей карты, мосты между темами и острова, до которых не дойти по ссылкам | скрипт | `kb_graph.py --insights` | `--story --write --json --cards-json --cards --apply --allow-dirty --max-related --report --conf --jira` | 1.66.0 |
| `kb:reset` (`reset`) | обнулить базу и собрать заново: сносит всё содержимое AuroraKnowledgeDB/, за её пределами не трогает ничего; `--keep-handmade` оставляет то, чего нет в источниках; откат — из git | скрипт | `kb_reset.py` | `--apply --drop-unknown --list-unknown --backup --allow-dirty` | 1.24.0 |
| `kb:lint` (`lint`) | механические ошибки базы: ссылки, frontmatter, типы карточек, артефакты в знаниях, секреты | скрипт | `kb_lint.py` | `--full --summary --residue --only --only-from` | 1.0.0 |
| `kb:question` | завести вопрос к заказчику (Q-NNN): кому, что блокирует, срок | модель | `workflows.md` | — | 1.4.0 |
| `kb:answer` | зафиксировать ответ: закрыть вопрос и разнести знание в REQ/спеку/DR | модель | `workflows.md` | — | 1.4.0 |
| `kb:decide` (`decide`) | оформить Decision Record (+supersede старой DR) | модель | `workflows.md` | — | 1.0.0 |
| `kb:garden` (`garden`) | еженедельная гигиена: чеклист из четырёх скриптов, разбор — человеком | скрипт+модель | `workflows.md` | — | 1.0.0 |
| `kb:trust` (`trust`) | пересчитать класс доверия карточек по таблице трассировки и статусам задач (итог: статус knowledge/draft и основание словами; человек доверие не присваивает) | скрипт | `kb_trust.py` | `--apply --root` | 1.89.0 |
| `kb:kind` (`kind`) | тип карточки: словарь, документ или знание — от него зависит, можно ли модели переписывать тело (итог: поле kind; выбор человека движок не перетирает) | скрипт | `kb_kind.py` | `--apply --root` | 1.90.0 |

## `ctx: — использование знаний`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ctx:context` (`context`) | context pack: отбор, фильтр статусов, шапки доверия, запись в `usage.log` | скрипт | `ctx_pack.py` `topic` | `--mode --max-cards --budget --release --save --no-log --no-semantic --retrieval --index` | 1.8.0 |
| `ctx:ask` (`ask`) | ответ по базе с цитатами; «почему не X» — включая отклонённые DR | модель | `workflows.md` | — | 1.0.0 |
| `ctx:eval` (`eval`) | регрессионный прогон golden questions после синков и миграций | модель | `workflows.md` | — | 1.0.0 |
| `ctx:retro` (`retro`) | выученные уроки: чего база не знала, когда мы ошиблись | модель | `workflows.md` | — | 1.0.0 |

## `make: — производство артефактов`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `make:kinds` (`kinds`) | реестр артефактов проекта: шаблон, папка результата, промпт проекта, правило «без технологий» для вида и граница чистовика (куда писать уточнения и допущения, чтобы они не уехали заказчику); объявляется в aurora.config.yaml, читается чужим ассистентом через MCP (artifact_spec) — всё, чего здесь нет, для него не существует | скрипт | `make_kinds.py` | `--kind --json --root` | 1.69.0 |
| `make:create` (`create`) | артефакт в `Artifacts/<тип>/` — только стандартный тип из conventions.md | модель | `workflows.md` | — | 1.1.0 |
| `make:review` (`review`) | проверка качества артефакта против базы знаний | модель | `workflows.md` | — | 1.0.0 |
| `make:review-auto` (`review-auto`) | ревью истории или алгоритма без человека: закрытый чек-лист из шаблона review_v2.0.md, три прогона модели с голосованием по каждому вопросу, оценку и вердикт считает скрипт. Одна страница — отчёт с подсказками, что исправить; пакет по CQL (`--cql`, `--as-of` — по состоянию на дату) — ретроспектива со сводкой и продолжением после обрыва | скрипт+модель | `review_run.py` | `--page --cql --pages-file --name --profile --as-of --runs --depth --max-pages --limit --free --by-author --budget-min --template --summary --apply` | 1.119.0 |
| `make:spec` (`spec`) | спецификация фичи из REQ и verified-знаний (SDD) | модель | `workflows.md` | — | 1.0.0 |
| `make:spec-pack` (`spec-pack`) | бандл спеки: основания, DR, аббревиатуры, DoR-риски — самодостаточный файл | скрипт | `spec_pack.py` `spec` | `--version --apply` | 1.9.4 |
| `make:validate` (`validate`) | сверить реализацию и тесты подрядчика со сценариями спеки | модель | `workflows.md` | — | 1.0.0 |
| `make:assemble` (`assemble`) | собрать поставляемый документ (ОПЗ/ПМИ/РП) из базы по шаблону | модель | `workflows.md` | — | 1.0.0 |

## `ship: — наружу`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ship:publish` (`publish`) | артефакт → generated-страница Confluence; карточки знаний наружу не идут. Тело режется по строке-маркеру: «Уточнения», «Допущения», «Под вопросом» остаются в черновике аналитика, локальный файл не меняется | скрипт | `publish_doc.py` `path` | `--parent --title --force --adopt --apply` | 1.9.6 |
| `ship:export` (`export`) | поставляемый документ → docx/pdf (pandoc, фирменный шаблон) | скрипт | `ship_doc.py --export docx` `document` | `--release --reference --out --keep-links --version --date --binary --apply` | 1.5.0 |
| `ship:release` (`release`) | заморозить переданную версию: снапшот, коммит базы, дата | скрипт | `ship_doc.py --release` `document` | `--export --reference --out --keep-links --version --date --binary --apply` | 1.9.1 |
| `ship:acceptance` | результаты приёмки и разбор замечаний заказчика | модель | `workflows.md` | — | 1.4.0 |

## `ops: — управление и отчётность`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `ops:stats` (`status, stats`) | дашборд здоровья базы: статусы, риски, метрики | скрипт | `aurora_stats.py` | `--queue --limit --theme --json --append-metrics --report` | 1.3.0 |
| `ops:retrieval` | какие карточки приходят первыми по реальным запросам аналитиков и что изменилось с прошлого раза: сторож ранжирования. Запросы берутся из разговоров `meta/ask/`, точка сравнения — `--save` | скрипт | `kb_retrieval.py` | `--query --json --limit --no-semantic --save` | 1.96.0 |
| `ops:search-quality` (`search-quality, поиск`) | качество поиска числом: самопоиск — тезис карточки как вопрос, правильный ответ известен по построению, разметка не нужна. R@1/R@5/MRR и запас до лучшего чужого ответа; ведёт историю замеров и печатает разницу с прошлым разом. `--compare "base,related,pagerank=0.25,…"` — стенд сравнения вариантов ретрива (dry-run, в историю не пишет) | скрипт | `kb_search_quality.py` | `--sample --golden --apply --golden-remap --compare` | 1.100.28 |
| `ops:gaps` (`gaps`) | смысловые дыры базы: понятие названо в карточках, а своей у него нет; связь названа, но не поставлена; одинокие карточки; тезис отстал от источника; источник исчез. Механика линтера этого не видит — а именно от этого база перестаёт быть базой | скрипт | `kb_gaps.py` | `--min-mentions --limit --report` | 1.100.35 |
| `ops:todo` (`todo`) | что осталось человеку: остаток приёмки, документы в базе, двойники, неразобранные источники — одним списком с объяснением, почему это нельзя сделать кнопкой | скрипт | `aurora_todo.py` | — | 1.84.0 |
| `ops:impact` (`impact`) | что зависит от карточки; `--explain` — на чём собран документ | скрипт | `kb_trace.py --impact` `[target]` | `--explain --requirements` | 1.8.0 |
| `ops:trace` (`trace`) | трассировка: пункт ТЗ → REQ → SPEC → Jira → AC → ПМИ → приёмка | скрипт | `kb_trace.py --requirements` `[target]` | `--impact --explain` | 1.0.0 |
| `ops:trace-table` | таблица трассировки: артефакт ↔ задачи, прямые связи и трассировка глубиной 2; итог — `meta/trace/` и свод `MOC/Трассировка.md` | скрипт | `kb_trace_table.py` | `--apply --root` | 1.89.0 |
| `ops:questions` | реестр вопросов: открытые, просроченные, что блокируют | скрипт+модель | `workflows.md` | — | 1.4.0 |
| `ops:report` (`report`) | дашборд эффективности аналитиков: недельная активность по Jira и Confluence, переходы задач; настройки — в секции reports: конфига | скрипт | `report_analyst.py` | `--skip-fetch --serve` | 1.78.0 |

## `agent: — встроенный агент`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `agent:aliases` | агент разбирает конфликты синонимов: уточняет там, где карточки разные, и откладывает человеку дубли; правит только через команды движка | скрипт+модель | `agent_runner.py --task aliases` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.57.0 |
| `agent:build` | агент разбирает партию источников на карточки (итог: новые карточки со статусом imported, доверие не присваивается): раскадровка, границы тем, имена, отметка о разборе; тело карточек переносит движок, а не модель; `--until-done` разбирает план целиком партиями (первичная сборка, часы) | скрипт+модель | `agent_runner.py --task build` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.58.0 |
| `agent:distill` | написать тезисы для карточек типа «знание»: модель читает перенесённый текст и пишет определение, дословный источник уезжает под тезис; каждую проверяет Момус (итог: карточки-знания вместо кусков страниц); `--until-done` — вся очередь одним проходом на всю ширину шлюзов, с фиксацией по ходу | скрипт+модель | `agent_runner.py --task distill` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.90.0 |
| `agent:extract` (`extract`) | вынести из карточек чужие определения: знание о другой сущности, объяснённой попутно, переезжает в свою карточку, а на его место встаёт ссылка. Перенос дословный — движок вырезает и вставляет, не меняя ни символа; `--until-done` — вся очередь в окне `--hours`, а не в бюджете шага | скрипт+модель | `agent_runner.py --task extract` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.100.32 |
| `agent:twins` | решить судьбу карточек с совпадающим текстом: одна это сущность или разные, решает модель по тексту, а не человек. Слияние идёт через `kb:dedupe --merge` и обратимо — проигравшая уходит в архив со ссылкой на победителя | скрипт+модель | `agent_runner.py --task twins` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.100.32 |
| `agent:tasks` (`tasks`) | вернуть предмету знание задач Jira, осевших отдельными карточками: задача — это работа, а не сущность, и её код со ссылкой дописываются в карточку предмета, где по ним считается доверие. Карточка без предмета в базе называется по предмету, прежнее имя уходит в синонимы | скрипт+модель | `agent_runner.py --task tasks` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.100.38 |
| `agent:clashes` (`clashes`) | противоречия между карточками об одном предмете: где обе нельзя считать верными одновременно. Обе стороны цитируются дословно, кто прав — решает человек по источникам | скрипт+модель | `agent_runner.py --task clashes` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.100.35 |
| `agent:relink` (`relink`) | расставить связи в готовых тезисах, не переписывая текст: модель вставляет только разметку, движок снимает её с ответа и сверяет с исходным текстом посимвольно — не совпало, ответ отброшен целиком | скрипт+модель | `agent_runner.py --task relink` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.100.36 |
| `agent:translit` | заполнить словарь имён «латиницей ↔ кириллицей» для карточек, названных транслитом: перевод — работа со смыслом, его делает модель. Пара пишется в словарь, и по ней сущность находится под обоими написаниями ещё до переименования | скрипт+модель | `agent_runner.py --task translit` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.101.0 |
| `agent:make` | произвести артефакт по типу из реестра проекта: обогащение базой, план с вопросами к аналитику (grill), воркер, критик, Момус. Файл рождается сразу после обогащения, ответы аналитика пишутся по ходу — обрыв не уносит сказанное. В шапке видно, на чём документ стоит (based_on — процитированное, а не весь пак), что предположили и чьё это допущение, зрелость по осям (coverage) и сколько утверждений без опоры. В задаче понимает @путь, /навык и @сервер MCP; --context — файлы и вложения из панели | скрипт+модель | `agent_runner.py --task make` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.95.0 |
| `agent:ask` | спросить базу своими словами: движок собирает контекст, модель отвечает только по карточкам и ставит ссылку на каждое утверждение; ответ проверяет Момус (роль qa) и разбор ссылок по базе; `--backend N` спрашивает конкретную модель из списка; разговор пишется в `meta/ask/` и уходит в git с базой, `--thread` продолжает его уточняющим вопросом | скрипт+модель | `agent_runner.py --task ask` | `--question --mode --kind --idea --session --answers --enough --context --thread --threads --no-journal --backend --no-momus --apply --critic --limit --partition --until-done --hours --no-checkpoint` | 1.63.0 |
| `agent:width` | замерить, сколько одновременных запросов держит каждый шлюз: наращиваем нагрузку, пока растёт пропускная способность. Перестала расти — дальше очередь на стороне шлюза, и потоки добавлять бессмысленно. Число знать неоткуда, поэтому не спрашиваем, а меряем | скрипт | `agent_core.py --probe-width` | `--ping --heavy --show --venv-status --venv-install --json` | 1.99.2 |
| `agent:ping` | встроенный агент: проверить цепочку моделей — каждый бэкенд живым запросом, пустой ответ считается отказом | скрипт | `agent_core.py --ping` | `--probe-width --heavy --show --venv-status --venv-install --json` | 1.56.0 |
| `agent:probe` (`probe`) | живая проверка связи: опрашивает каждый шлюз сейчас, карантин не читает; различает «нет связи», «ключ» и «нет такой модели» и показывает список моделей шлюза; `--why` проверяет запрос послойно — какой именно кусок шлюз не принимает | скрипт | `agent_probe.py` | `--timeout --models --why` | 1.100.27 |

## `dev: — разработка движка (только в ките)`

| Команда | Что делает | Исполнитель | Чем | Модификаторы | С версии |
|---|---|---|---|---|---|
| `dev:qa-list` | QA движка: какие есть кейсы и сценарии, что закрыто автотестом, какие кейсы не гоняются ни разу | скрипт | `dev_qa.py --list` | `--save --retrieval --check --gap --cover --base --run --apply --record --new` | 1.51.0 |
| `dev:qa-retrieval` | выдача по эталонному корпусу кита: сторож ранжирования в git. Падает сам, когда порядок карточек изменился, — и показывает, как именно | скрипт | `dev_qa.py --retrieval` | `--list --save --check --gap --cover --base --run --apply --record --new` | 1.96.0 |
| `dev:qa-check` | целостность реестра QA: дубли номеров, ссылки на несуществующие кейсы, отставшие версии | скрипт | `dev_qa.py --check` | `--list --save --retrieval --gap --cover --base --run --apply --record --new` | 1.51.0 |
| `dev:qa-gap` | что изменено в коде и чем покрыто; решение «автотест или кейс» принимает модель | скрипт+модель | `dev_qa.py --gap` | `--list --save --retrieval --check --cover --base --run --apply --record --new` | 1.51.0 |
| `dev:qa-cover` | покрыть сделанное: таблица покрытия и готовое задание ассистенту — дополнить автотесты, кейсы и сценарии | скрипт+модель | `dev_qa.py --cover` | `--list --save --retrieval --check --gap --base --run --apply --record --new` | 1.53.0 |
| `dev:qa-run` | прогон сценария с записью журнала: автотесты, чек-лист шагов, отчёт в Development/QA/runs/ | скрипт+модель | `dev_qa.py --run` | `--list --save --retrieval --check --gap --cover --base --apply --record --new` | 1.51.0 |
| `dev:qa-new` | завести тест-кейс или сценарий из шаблона со следующим свободным номером | скрипт | `dev_qa.py --new` | `--list --save --retrieval --check --gap --cover --base --run --apply --record` | 1.51.0 |

## Развёртывание (из клона kit'а, не из проекта)

| Команда | Что делает |
|---|---|
| `python3 aurora.py new <target>` | развернуть Aurora в проект: скелет, движок, интерактивная настройка |
| `python3 aurora.py setup <target>` | перенастроить проект (Confluence, Jira, приватность, пороги) |
| `python3 aurora.py update <target>` | обновить движок до версии kit; `--apply` пишет, `--structure-only` — только папки |

Любую команду обслуживания можно звать и из kit'а: `python3 aurora.py <команда> <target> [флаги]`.

