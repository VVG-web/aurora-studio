# RELEASE_PLAN_CURRENT — снижение конфликтов синхронизации через GitHub

Дата: 2026-11-04
База: `master` 1.207.0 (`0aed9827`)
Параллельная ветка: `feature/per-user-state` (код уже влит в master), `development` (план, QA, TS-013)
Статус: завершён

## 1. Анализ

В `master` уже влиты:

- LOCAL-файлы вынесены в канонический `.gitignore` и снимаются с index при `kit:update` без удаления с диска: `recent-files.json`, `retrieval-last.json`, `graph.json`, персональные файлы Obsidian, `aurora_version.txt`, `start-aurora.bat`, `start-aurora.command`.
- SHARD-файлы разделены на части участника: `meta/kb_updated.d/<actor>.json`, `meta/search-quality.d/<actor>.json`, журналы агента `<stamp>_<actor>_<task>.md`, холостые прогоны `agent-runs/_пустые/<actor>.md`.
- Идентичность участника: `scripts/aurora_actor.py` (`AURORA_USER`, git user, slug + digest).
- Чтение объединяет legacy и все валидные части; запись идёт только в свою часть.
- Тесты: `part_184` (LOCAL/untrack, два git-пользователя, gitignore), `part_33` (kb_updated shards), `part_13` (actor, search-quality shards).
- Версия master: 1.207.0. Следующий релиз: 1.208.0.

Остался главный общий файл, который всё ещё даёт конфликты: `AuroraKnowledgeDB/meta/manifest.json` (SHARED, 62–615 изменений в проектах). Нужен доменный merge по ключам источников, а не ignore и не whole-file last-write-wins.

## 2. Релизы

### 1.208.0 — manifest.json: merge по ключам источников

- [x] `scripts/merge_manifest.py`: трёхсторонний merge `sources` по ключам, union `cards`, максимум `updated`, сохранение top-level.
- [x] `.gitattributes`: правило `AuroraKnowledgeDB/meta/manifest.json merge=aurora-manifest`.
- [x] `install_aurora.py` / `aurora_update.py`: настройка `merge.aurora-manifest.driver` в git проекта.
- [x] `engine_manifest.txt`: новый скрипт едет в проект.
- [x] `tests/cases/part_196.py`: union источников, более новый entry побеждает, удаление при неизменной второй стороне, union cards, правило `.gitattributes`, настройка git config.
- [x] `docs/git-collaboration.md`: граница LOCAL/SHARD/SHARED и merge manifest.
- [x] `VERSION` 1.208.0, `CHANGELOG.md`, `docs/commands.md`, cockpit modules/skins/panel.
- [x] Полный гейт, тег `v1.208.0`, ветка `rel/code-1.208.0`, push.
- [x] Отметить выполнение в этом файле.

**Результат 1.208.0:** коммит `c740d615`, тег `v1.208.0`, ветка `rel/code-1.208.0`; полный гейт 2446/2446, 0 падений.

### 1.208.1 — per-user state: QA, docs и финальная регрессия

- [x] `docs/git-collaboration.md`: итоговая граница, TS-013, откат, смешанные версии.
- [x] Новый тест: документация и версия согласованы, ключевые термины на месте.
- [x] `VERSION` 1.208.1, `CHANGELOG.md`, `docs/commands.md`, cockpit modules/skins/panel.
- [x] Полный гейт, тег `v1.208.1`, ветка `rel/code-1.208.1`, push.
- [x] Отметить выполнение в этом файле.

**Результат 1.208.1:** коммит `a6ac2ff0`, тег `v1.208.1`, ветка `rel/code-1.208.1`; полный гейт 2448/2448, 0 падений.

## 3. Критерии завершения

- Для каждого релиза: тег существует, `VERSION` сдвинут, новые тесты в прогоне и зелёные.
- Полный `python3 tests/run_tests.py` зелёный после каждого релиза.
- Рабочая копия чистая, незакоммиченных изменений нет.
- `RELEASE_PLAN_NEXT.md` создан после завершения обоих релизов.

## 4. Следующий план

Следующие релизы: **1.209.0** (lint_baseline/lint_residue merge), 1.209.1, 1.209.2 — см. `RELEASE_PLAN_NEXT.md`.
- **1.210.0** — `code:check` 2.0: имена, `--fail-on`, stale-индекс (см. `RELEASE_PLAN_NEXT.md`).
- **1.210.1** — `agent:codecheck`: модельный арбитр только для спорных критериев (см. `RELEASE_PLAN_NEXT.md`).
- **1.211.0** — `agent:cleanroom`: модельный синтез Clean Room с цитатами и publish-гейтом (см. `RELEASE_PLAN_NEXT.md`).

## 5. Релиз 1.212.0

`1.212.0` — cockpit: настройки артефактов Code, проверка требований в «Продуктивности» и
карточка продолжения остановленного прогона. Релиз выпущен из коммита `2b3d2c73`,
тег `v1.212.0`; GitHub Release и CI `engine-tests` успешны. Описание и тесты:
`CHANGELOG.md`, `part_206`–`part_208`, `docs/testing-cockpit-1.212.0.md`.

## 6. Релиз 1.212.1

`1.212.1` — cockpit: один ответ `/api/kinds`, вложенные AC в «Продуктивности» и корневой
`Code/*` в `.gitignore`. Коммит `7971ae3f`, тег `v1.212.1`; GitHub Release и CI
`engine-tests` успешны. Описание и тесты: `CHANGELOG.md`, `part_209`.

## 7. Релиз 1.212.2

`1.212.2` — cockpit: вкладка «Код» отдаёт форму сразу, полный расчёт идёт в фоне;
аналитика: зависшие после возврата (`returned_open_raw`); «Спросить»: срок проверки
«Момуса» ограничен полом 90 с и потолком 240 с. Описание и тесты: `CHANGELOG.md`,
`part_09`, `part_12`.

## 8. Релиз 1.213.0

`1.213.0` — cockpit: GitLab-группы и discovery новых репозиториев, настройки
артефактов Code только в разделе Code, устойчивость async-состояния Code и
регрессионные тесты скорости Code. Описание: `CHANGELOG.md`; тесты `part_210`–`part_212`,
`part_206`, существующие `part_207`/`part_208` и `code repos`/`code sync`.

## 9. Релиз 1.214.0

`1.214.0` — LLM-арбитр спорных требований в «Продуктивности»: opt-in, только
доказательства, цитаты для `found`, лимиты вызовов, LLM-комментарии в таблице и
Markdown-экспорте. Включает подготовленный локально `1.213.1` (механический страж
`gitignore-review`). Описание: `CHANGELOG.md`; тест `part_213`.

## 10. Релиз 1.215.0

`1.215.0` — сгенерированные publish-скрипты переезжают в `Scripts/Generated`;
старый `Publish/` читается для совместимости. Схема папок, справка, UI/CLI и тесты
обновлены. Описание: `CHANGELOG.md`; тесты `part_144`, `part_145`, `part_182`.

## 11. Релиз 1.216.0

`1.216.0` - управление контекстом: SQLite-хранилище и инструменты памяти, роль
`compressor` с порогом, менеджер бюджета с `per_request_input_tokens_limit`,
политика стадий и needle-in-a-haystack тест. Секция `context_architecture`
включается в проекте; без неё поведение прежнее. Тесты: `part_214`-`part_217`.
Описание: `CHANGELOG.md`.

## 12. Релиз 1.217.0

`1.217.0` - переиспользуемое ядро ответа «Спросить», Product-пайплайн
поиск -> grill -> промпт -> ядро -> артефакт, контракт полного/частичного/ошибочного
ответа, watchdog и quality-гейт с baseline. Тесты: `part_218`-`part_221`.
Описание: `CHANGELOG.md`.

## 13. Релиз 1.217.1

`1.217.1` - кэш `ctx_pack.words()`: реальный `agent:make` ускорился на 18.1% по p50
без изменения выдачи пака. Здесь же возвращён выбор входа в GitLab-группах Code
(системный, токен, логин-пароль, LDAP) и Basic-авторизация для LDAP и пароля.
Тесты `part_222` и `part_223`. Описание: `CHANGELOG.md`.

## 14. Релиз 1.217.2

`1.217.2` - одна ссылка Code: автоопределение репозитория или папки GitLab,
общий выбор входа для папки, добавление проектов без дублей и кнопка «+» для
следующей ссылки. Тест `part_224`. Описание: `CHANGELOG.md`.

## 15. Релиз 1.217.3

`1.217.3` - версия поднята после исправления метрики провайдеров, чтобы тег
релиза содержал зелёный источник. Функциональность 1.217.2 не меняется.
Описание: `CHANGELOG.md`.
