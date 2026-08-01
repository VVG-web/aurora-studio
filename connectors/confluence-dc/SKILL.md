---
name: confluence-sync-{{PROJECT_SLUG}}
description: >
  Confluence for {{PROJECT_NAME}}: ad-hoc чтение страниц через MCP и запуск
  детерминированного зеркала. Зеркало Sources/Confluence/ пишет ТОЛЬКО скрипт
  confluence_export.py. Use when the user asks to sync, read or export Confluence
  for {{PROJECT_NAME}}.
version: "2.0.0"
entrypoint: SKILL.md
---

# Confluence — {{PROJECT_NAME}}

## Зеркало делает скрипт, не модель

```bash
python3 .opencode/scripts/confluence_export.py            # обновить зеркало
python3 .opencode/scripts/confluence_export.py --verify   # гейт детерминизма
python3 .opencode/scripts/sync_audit.py                   # целостность после синка
```

Почему так: когда markdown пишет LLM, одна и та же страница выгружается каждый раз чуть
иначе — git показывает правку там, где её нет, и синк перестают запускать. Конвертация
кодом даёт байт-в-байт одинаковый файл. Процедура целиком —
`.opencode/skills/aurora-vault/references/maintenance.md`, раздел `sync:confluence`.

Настройки — `aurora.config.yaml` (`base_url`, `space`, `sync_roots`).
Доступ — `CONFLUENCE_PAT` в `.env.aurora.local` (Data Center 7.9+) либо
`CONFLUENCE_USER`/`CONFLUENCE_PASSWORD`. Токены в git не кладём никогда.

## Что делает этот скилл (через MCP Atlassian)

Только **чтение** и разовые справки — то, ради чего запускать зеркало избыточно:

- посмотреть страницу или её версию, найти страницу по заголовку/CQL;
- узнать `page_id` перед добавлением корня в `sync_roots`;
- собрать список дочерних страниц, чтобы оценить объём будущего синка;
- прочитать комментарии заказчика под страницей (их триаж — `ship:respond`).

## Жёсткие правила

1. **Не писать в `Sources/Confluence/` из модели.** Зеркало принадлежит скрипту: любая
   рукотворная или LLM-выгрузка ломает состояние синка, детерминизм и `sync_audit`.
   Нужна страница в зеркале — добавьте её корень в `sync_roots` и запустите скрипт.
2. Страница уже внутри синкаемого корня — отдельным корнем её добавлять не нужно:
   экспортёр такие корни пропускает (иначе страница легла бы вторым файлом в корень зеркала).
3. Скрипт недоступен (нет `beautifulsoup4`/`markdownify` или закрыт доступ) — это не повод
   выгружать зеркало моделью. Сообщите об этом и поставьте зависимости:
   `pip install beautifulsoup4 markdownify` (или `uvx --with beautifulsoup4 --with markdownify`).
4. Публикация НАРУЖУ (артефакт → страница) — отдельная процедура `ship:publish`
   (`references/workflows.md`), а не этот скилл.
5. Синк не трогает `AuroraKnowledgeDB/`: карточки появляются только через `build`.

## Про прежний LLM-синк

Он выведен из эксплуатации в 1.6: тела старых скиллов остались в истории git
(`git log -- .opencode/skills/confluence-sync-*`). Держать его «резервным путём» не стоит —
два писателя в одно зеркало возвращают ровно ту проблему, ради которой появился скрипт.
Резерв здесь не второй писатель, а git: зеркало восстанавливается `git revert`/`git checkout`.
