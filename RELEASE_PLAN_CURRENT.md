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
