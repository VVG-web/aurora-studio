# Command Reference Function

## Description

`docs/commands.md` — полный справочник команд движка Aurora Studio. Файл **не рукописный**: по его шапке он собран автоматически командой `kit:list` для версии движка **1.90.0** (снимок на момент последней регенерации; актуальная версия кита — в `VERSION`). Модификаторы взяты из `--help` самих скриптов и не расходятся с кодом, остальное — из реестра `commands.txt`. Файл перезаписывается командой `python3 .opencode/scripts/kit_commands.py --md`, поэтому править его руками бессмысленно. Каждая строка называет команду, что она делает, **исполнителя** (скрипт / модель / скрипт+модель — граница между детерминированной механикой и работой со смыслом), скрипт, который её исполняет, модификаторы и с какой версии движка команда существует.

## Key Features

Команды сгруппированы по неймспейсам, отражающим реестр `commands.txt`:

- **`kit:` — движок и проект** — `kit:doctor` (`doctor`), `kit:hooks`, `kit:remap-sources` (`remap`), `kit:update`, `kit:skills`, `kit:mcp` (`mcp`), `kit:list` (семь команд в снимке 1.90.0).
- **`sync:` — зеркала внешних систем** — `sync:sources` (`sources`), `sync:confluence`, `sync:jira`, `sync:audit` (`audit`), `sync:diff` (`diff`), `sync:jira-status`.
- **`kb:` — извлечение и жизнь знания** — `kb:build` (`build`), `kb:ingest-office`, `kb:ingest` (алиасы `ingest`, `ingest-raw`, `ingest-meeting`, `ingest-tz`), `kb:repair` (`fix`), `kb:dedupe`, `kb:split` (`split`), `kb:embed` (`embed`), `kb:moc` (`moc`), `kb:index` (`index`), `kb:scrub` (`scrub`), `kb:schema`, `kb:supersede` (`supersede`), `kb:links` (`links`, `graph`), `kb:map` (`map`), `kb:reset` (`reset`), `kb:lint` (`lint`), `kb:question`, `kb:answer`, `kb:decide` (`decide`), `kb:garden` (`garden`), `kb:trust` (`trust`), `kb:kind` (`kind`).
- **`ctx:` — использование знаний** — `ctx:context` (`context`), `ctx:ask` (`ask`), `ctx:eval` (`eval`), `ctx:retro` (`retro`).
- **`make:` — производство артефактов** — `make:kinds` (`kinds`), `make:create` (`create`), `make:review` (`review`), `make:spec` (`spec`), `make:spec-pack` (`spec-pack`), `make:validate` (`validate`), `make:assemble` (`assemble`).
- **`ship:` — наружу** — `ship:publish` (`publish`), `ship:export` (`export`), `ship:release` (`release`), `ship:acceptance`.
- **`ops:` — управление и отчётность** — `ops:stats` (`status`, `stats`), `ops:todo` (`todo`), `ops:impact` (`impact`), `ops:trace` (`trace`), `ops:trace-table`, `ops:questions`, `ops:report` (`report`).
- **`agent:` — встроенный агент** — `agent:aliases`, `agent:build`, `agent:distill`, `agent:ask`, `agent:ping`; все, кроме `ping`, исполняет `agent_runner.py` с разными `--task`, а `ping` — `agent_core.py --ping`.
- **`dev:` — разработка движка (только в ките)** — `dev:qa-list`, `dev:qa-check`, `dev:qa-gap`, `dev:qa-cover`, `dev:qa-run`, `dev:qa-new`; все исполняются `dev_qa.py` с разными подкомандами.

Закрывает файл таблица **Развёртывание** — entry points на стороне кита (`python3 aurora.py new / setup / update <target>`), которые запускаются из клона кита, а не из проекта.

## Related Documentation

### Source Files
- docs/commands.md — сгенерированный справочник (снимок на момент регенерации версии 1.90.0)
- commands.txt — реестр, из которого собирается справочник; единственный источник правды; формат строки: `неймспейс | команда | алиасы | исполнитель | реализация | с версии | что делает`
- scripts/kit_commands.py — сборщик: `--md` регенерирует `docs/commands.md`, модификаторы берёт живьём из `--help` скриптов
- aurora.py — единая точка входа; команды обслуживания идут через словарь `TOOLS` (`list`, `doctor`, `stats`, `lint`, `fix`, `queue`, …)

### Related Functions
- [Engine Structure & Roadmap](./01-engine-structure-roadmap.md) — rationale неймспейсов (семь наборов, у каждого свой владелец ритуала) и фиксированной структуры
- [Docs for Humans](./02-docs-for-humans.md) — таблицы «ситуация → команда» документа практики отсылают в этот реестр
- [Installation & Rollout](./04-install-rollout.md) — entry points из закрывающей таблицы развёртывания

## Implementation Notes

Колонка «исполнитель» — сознательный дизайн-сигнал: **скрипт** — детерминированная механика, результат воспроизводим; **модель** — работа со смыслом; **скрипт+модель** — скрипт считает и готовит, решение принимает человек. У команд с исполнителем «модель» нет скрипта: колонка «реализация» в реестре указывает на `workflows.md` в `skills/aurora-vault/references/` — процедуру для ассистента, а не файл в `scripts/`.

Короткие имена в скобках (например `doctor`, `sources`, `build`) — исторические алиасы, они работают всегда.

`commands.txt` — живой источник правды, `docs/commands.md` — сгенерированный артефакт, который отстаёт от реестра на время с последней регенерации. Новые команды уже есть в реестре, но ещё нет в справочнике: `kit:i18n`, `kb:correct`, `ops:retrieval`, `agent:make`, `agent:width`, `dev:qa-retrieval` — они появятся в `docs/commands.md` только после регенерации `kit:list --md`. Текущая версия кита зафиксирована в `VERSION` (1.100.3), а в шапке справочника — версия движка 1.90.0 на момент его последней сборки.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, engine*
