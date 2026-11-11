---
kind: cr-json
title: "JSON-контракты по коду"
title_en: "JSON contracts from code"
version: "1.0"
enabled: "true"
scope: pair
out: Artifacts/contracts/code
prompt: PromptCode/cr-json.md
purity: traceable
order: 105
---

# {{TITLE}}

## Модели и поля

| Модель | Поле | Тип | Обязательно | Ссылка |
|---|---|---|---|---|
| {{MODEL}} | {{FIELD}} | {{TYPE}} | {{REQUIRED}} | [cN] |

## Контракты

| Контракт | Направление | Формат | Ссылка |
|---|---|---|---|
| {{CONTRACT}} | {{DIRECTION}} | {{FORMAT}} | [cN] |

## Примеры

Запрос, ответ, ошибка и пустой результат.

## Что не удалось установить по коду

JSON Schema/OpenAPI, примеры, ошибки, версионирование и обратная совместимость.
