# Cockpit 1.212.0 — тестовые сценарии

Все автоматические сценарии входят в `tests/run_tests.py` и полный CI-гейт.

## Автоматические

| Сценарий | Файл | Что проверяет |
|---|---|---|
| Артефакты Code | `tests/cases/part_206.py` | `code_artifacts:` round-trip, `ArtifactsCode/<kind>`, сохранение соседних секций, отказ путей/шаблонов, 409, частичное сохранение, API, UI/i18n и схема `ArtifactsCode` |
| Проверка требований | `tests/cases/part_207.py` | парсер Markdown, подчёркивания в именах, проценты, отсутствие индекса, синтетический индекс, папка, экспорт и защита пути, UI/API/i18n |
| Продолжение прогона | `tests/cases/part_208.py` | `/api/route/state`, `skipSigs`/`attempts`/`cycleAt`, ru/en, два конкурентных refresh без дубля |
| Мультирепозиторный реестр | `part_154`, `part_155`, `part_156` | реестр, синхронизация нескольких репозиториев, контракт Code |
| Схема и обновление | `part_206`, `folders` | doctor принимает `ArtifactsCode`, update создаёт README |

Локальный прогон:

```bash
python3 tests/run_tests.py --only='code artifact' --no-invariants
python3 tests/run_tests.py --only='requirements' --no-invariants
python3 tests/run_tests.py --only='quickstart resume' --no-invariants
python3 tests/run_tests.py --only='folders' --no-invariants
```

## Живые сценарии

1. Code → «Артефакты по коду»: изменить путь `cr-api`, сохранить, проверить блок
   `code_artifacts` в `aurora.config.yaml`.
2. Изменить конфиг в обход открытой формы, сохранить снова: ожидается сообщение о
   конфликте, перечитывание формы и успешное повторное сохранение.
3. «Продуктивность» → выбрать `spec.md` → «Проверить по коду» → экспорт: таблица,
   процент и `Источник: spec.md`.
4. Выбрать папку с двумя Markdown-документами: две строки и оба источника в отчёте.
5. При остановленном маршруте открыть «Быстрый старт»: одна карточка, причина
   «застой», кнопка продолжения и payload с `skipSigs`/`attempts`/`cycleAt`.

Живые сценарии выполнялись headless Chrome + CDP на временном проекте; evidence лежит
в run-каталоге (`validation/task-005-ui-smoke.txt`,
`validation/task-006-code-artifact-click-save.txt`,
`validation/task-006-code-artifact-click.txt`,
`validation/task-007-requirements-ui.txt`,
`validation/task-011-requirements-folder-ui.txt`,
`validation/task-008-resume-click.txt`).
