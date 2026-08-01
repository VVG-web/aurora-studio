# Модули источников

Зеркала в `Sources/` наливают подключаемые модули. Движок про Confluence и Jira ничего
не знает: он знает два **вида** хранилищ и умеет обслуживать любой модуль, который
объявил свой вид.

| Вид | Про что | Раскладка | Файл состояния |
|---|---|---|---|
| `wiki` | дерево страниц со стабильными номерами | папки повторяют иерархию; страница с детьми → папка и `index.md` | `sync_state.md` |
| `board` | плоский список задач со стабильными ключами | файл на задачу, имя — ключ | `update_log.md` |

Confluence Data Center (`confluence-dc`) и Jira Data Center (`jira-dc`) идут в комплекте
с kit'ом и устанавливаются всегда. Всё остальное — Notion, SharePoint, Confluence Cloud,
YouTrack, GitLab Issues — добавляется папкой в `connectors/`.

## Что делает движок, а что модуль

Общая часть — `scripts/sources_core.py`:

- REST-клиент с токеном (`RestApi`), чтение секретов из `.env.aurora.local`;
- файл состояния зеркала: запись, разбор, слияние с прошлым прогоном;
- поиск лишних файлов (`extra_files`) с учётом регистра и нормализации Unicode;
- защита от удаления того, на что ссылаются карточки (`cited_by_cards`);
- гейт детерминизма `--verify`: две выгрузки и побайтовая сверка.

Модуль реализует только продуктовое: как ходить в API, как превращать разметку продукта
в markdown, какие поля класть в шапку файла. Детерминизм — обязателен: одна и та же
страница должна давать байт-в-байт один и тот же файл, иначе git показывает правки там,
где их нет, а `sync:audit` не может проверить состояние.

## Устройство модуля

```
connectors/<id>/
├── connector.json     # манифест: чем модуль является и как его запускать
├── SKILL.md           # шаблон sync-скилла проекта (плейсхолдеры {{PROJECT_SLUG}} и др.)
└── <script>.py        # скрипт выгрузки (у встроенных лежит в scripts/ kit'а)
```

`connector.json`:

```json
{
  "id": "notion",
  "title": "Notion",
  "kind": "wiki",
  "since": "1.29.0",
  "what": "Страницы базы Notion деревом.",
  "mirror": {"default_path": "Sources/Notion", "state": "sync_state.md"},
  "run": {"script": "notion_export.py", "command": "sync:notion", "skill": "notion-sync"},
  "auth": {"env_prefix": "NOTION", "what": "интеграционный токен"},
  "settings_block": "notion",
  "settings": [{"key": "database_id", "what": "какая база выгружается", "required": true}]
}
```

- `kind` — `wiki` или `board`; движок по нему выбирает и раскладку, и правила аудита;
- `mirror.default_path` — папка зеркала; она же становится легитимной в `Sources/`
  (`kit:structure` перестаёт считать её ничьей);
- `auth.env_prefix` — из него выводятся имена переменных: `NOTION_PAT`,
  `NOTION_PERSONAL_TOKEN`, `NOTION_USER` + `NOTION_PASSWORD`;
- `settings_block` — где в `aurora.config.yaml` модуль ищет свои настройки. Реестр их
  не разбирает: у каждого продукта они свои (у Confluence, например, список корней
  синка — список словарей).
- `mirror.legacy_path_key` есть только у двух встроенных модулей: он включает их в
  проектах, где секции `sources:` ещё нет.

Манифест — JSON, а не YAML: его читает движок, а не человек, и разбирать его нужно без
внешних зависимостей.

## Как модуль попадает в проект

1. Папка кладётся в `connectors/` kit'а.
2. `aurora.py update <проект> --apply` разносит манифест в `.opencode/connectors/<id>.json`,
   скрипт — в `.opencode/scripts/`, а тело sync-скилла — в существующие папки скиллов
   проекта (перезаписи нет: kit-версия ложится рядом как `.new`).
3. Модуль подключается к проекту в `aurora.config.yaml`:

```yaml
sources:
  - id: Notion            # он же имя папки в Sources/
    module: notion
    path: Sources/Notion
```

   То же самое делает панель: «Зеркала» → «Модули источников» → отметить и сохранить.
4. Папку зеркала заводит `update` (или `--structure-only`).

Проверить, что получилось: `python3 .opencode/scripts/sources_registry.py`.

## Отключение

Снятая отметка убирает модуль из `sources:`, но папку зеркала не трогает: выгрузка — это
данные, а данные движок не удаляет. `kit:doctor` после этого назовёт папку ничьей —
замечанием, а не ошибкой. Решение (удалить, перенести в `Raw/`, подключить обратно)
принимает человек.

## Минимальный скрипт выгрузки

```python
from sources_core import BoardMirror, RestApi, read_secret, verify

class Api(RestApi):
    agent = "aurora-notion-export/1.0"

class Mirror(BoardMirror):
    banner = "Notion sync state — генерируется notion_export.py, не править руками"
```

Дальше — цикл по записям источника: сложить `mirror.rows`, вызвать `mirror.write_state()`,
показать `mirror.extra_files(...)` и удалить их по `--prune`. Готовые образцы —
`scripts/jira_export.py` (board, 324 строки) и `scripts/confluence_export.py` (wiki).

Обязательное для приёмки модуля:

- `--verify` проходит (детерминизм);
- в шапке файлов нет даты экспорта и версий в именах файлов — иначе каждый прогон
  даёт дифф на всю папку;
- состояние пишется полными путями от корня зеркала — по ним работает `sync:audit`.
