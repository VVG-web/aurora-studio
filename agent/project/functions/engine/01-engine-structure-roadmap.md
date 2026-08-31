# Engine Structure & Roadmap Function

## Description

Кластер «как сам кит устроен»: фиксированная схема папок, правила изменения кита, концептуальный документ, зафиксировавший дизайн набора команд (roadmap), и модель, по которой внешние системы становятся подключаемыми модулями источников. Это самая developer-ориентированная часть движка: Аврора как артефакт, который строят и расширяют, а не просто используют. Канонические описания — в `docs/STRUCTURE.md`, `docs/CONTRIBUTING.md`, `docs/roadmap.md`, `docs/connectors.md`; сама схема папок — в `structure_dirs.txt` в корне кита.

## Key Features

- **`structure_dirs.txt`** — единый источник правды о фиксированной схеме папок: `Sources/`, `Raw/` (laws, contract, customer, project, meetings, examples, corrections), разделы `AuroraKnowledgeDB/` (Concepts, Processes, …, Requirements, Specs, Questions, Decisions, MOC, `_archive`, `meta`), `Artifacts/`, `Deliverables/`, `Workspaces/`, `Templates/`, `Prompts/`, `Settings/`. `install` и `update` читают его и создают отсутствующие папки (идемпотентно, без удаления и перемещений), `doctor --structure` сверяет факт с схемой и рапортует о самодеятельных папках. Добавленная в файл папка приезжает во все проекты следующим обновлением.
- **`docs/STRUCTURE.md`** — одно размеченное дерево корня кита: `aurora.py` (точка входа: `new`/`setup`/`update` + обслуживание), движок `scripts/` (`install_aurora.py`, `aurora_setup.py`, `aurora_update.py`, `kb_lint.py`, `kb_fix.py`, `sources_core.py`, `sources_registry.py`, `sync_audit.py`, `aurora_stats.py`, `aurora_hooks.py`, `kb_trace.py`, `aurora_doctor.py`), `skills/aurora-vault/`, `connectors/` (`confluence-dc/`, `jira-dc/`), `templates/`, `scaffold/`, `docs/`, `examples/`. Что именно обновляет `aurora.py update`, перечисляет `engine_manifest.txt`.
- **`docs/CONTRIBUTING.md`** — кит остаётся *project-agnostic*-инсталлятором, а не клиентской базой знаний: списки Do/Don't (не коммитить клиентские `Raw/` и Jira-дампы, не хардкодить space/project-ключи одной компании, не ломать skip-existing защиту инсталлятора без пути `--force`), dev-цикл (`install_aurora.py --target … --force`, затем `kb_lint.py --summary` в полученном проекте), версионирование `vMAJOR.MINOR.PATCH` и трёхуровневая пирамила проверок: фикстуры (`tests/run_tests.py`), золотой корпус (`tests/corpus/`, числа зафиксированы в `tests/corpus/EXPECTED.json`, пересобирается `tests/make_corpus.py`) и живая база (`tests/smoke_live.py <проект>`, снимок лежит в `meta/smoke_snapshot.json` самого проекта).
- **`docs/roadmap.md`** — концепция набора команд, написанная от эксплуатации (базовая точка: движок 1.2.0, два живых проекта, июль 2026). Три вывода, определивших дизайн: механику делает скрипт, суждение — LLM; круг не замкнут наружу (нужен `ship:`); верификация масштабируется по употреблению, а не по алфавиту. Раздел 2 — зафиксированное решение: структура папок одинакова во всех проектах Авроры, проект не придумывает свои типы артефактов, а новый тип — это изменение кита (PR в `structure_dirs.txt` + таблицы в `SKILL.md`/`conventions.md` + запись в CHANGELOG). Раздел 6 — восемь инвариантов, которых дорожная карта не имеет права нарушить (артефакт ≠ знание, ничего не удаляется, синк не перезаписывает проверенное и т.д.).
- **`docs/connectors.md`** — модель модулей источников: движок про Confluence и Jira ничего не знает, он знает два **вида** хранилищ — `wiki` (дерево страниц со стабильными номерами; папки повторяют иерархию, файл состояния `sync_state.md`) и `board` (плоский список задач со стабильными ключами; файл на задачу, файл состояния `update_log.md`). `confluence-dc` и `jira-dc` идут в комплекте и устанавливаются всегда; всё остальное (Notion, SharePoint, YouTrack, GitLab Issues…) добавляется папкой в `connectors/`: манифест `connector.json`, шаблон sync-скилла `SKILL.md`, скрипт выгрузки. Общая часть — `scripts/sources_core.py`: REST-клиент с токеном, состояние зеркала, поиск лишних файлов (`extra_files`), защита от удаления того, на что ссылаются карточки (`cited_by_cards`), гейт детерминизма `--verify` (две выгрузки и побайтовая сверка).

## Related Documentation

### Source Files
- docs/STRUCTURE.md — размеченное дерево кита
- docs/CONTRIBUTING.md — как менять кит
- docs/roadmap.md — rationale дизайна набора команд и дорожная карта
- docs/connectors.md — модель модулей источников
- structure_dirs.txt — фиксированная схема папок
- engine_manifest.txt — манифест того, что обновляет `aurora.py update`
- tests/ — три уровня проверок (`run_tests.py`, `corpus/`, `smoke_live.py`)

### Related Functions
- [Command Reference](./03-command-reference.md) — описанные здесь `sync:` и `dev:` неймспейсы исполняются теми же скриптами, что и строки справочника
- [Installation & Rollout](./04-install-rollout.md) — развёртывание копирует в проект именно эту структуру

## Implementation Notes

`structure_dirs.txt` и `docs/connectors.md` — канонические описания: первое — для схемы папок, второе — для видов хранилищ. Папки зеркал внутри `Sources/` в схеме намеренно не перечисляются: их заявляют подключённые модули (`.opencode/connectors/`), а заводит `update` по реестру. Отключённый модуль оставляет папку зеркала «ничьей» — `kit:doctor` назовёт это замечанием, а не ошибкой, и решение (удалить, перенести в `Raw/`, подключить обратно) принимает человек.

Манифест модуля — JSON, а не YAML: его читает движок, а не человек, и разбирать нужно без внешних зависимостей. Движок по `kind` выбирает раскладку и правила аудита; модуль реализует только продуктовое — как ходить в API, как превращать разметку продукта в markdown, какие поля класть в шапку файла. Обязательное для приёмки модуля: `--verify` проходит (детерминизм), в шапке файлов нет даты экспорта, состояние пишется полными путями от корня зеркала — по ним работает `sync:audit`.

Кухня разработчика `Development/` (внутри `QA/` — шаблоны и кейсы, сценарии, журналы прогонов) закрыта `.gitignore`: в поставку идёт инструмент, а не то, как его проверяют. В свежем клоне папки нет, восстанавливается командой `git worktree add Development development`.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, engine*
