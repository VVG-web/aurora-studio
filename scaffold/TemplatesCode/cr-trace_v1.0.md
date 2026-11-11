---
kind: cr-trace
title: "Матрица трассировки по коду"
title_en: "Traceability matrix from code"
version: "1.0"
enabled: "true"
scope: module
out: Artifacts/tests/code
prompt: PromptCode/cr-trace.md
purity: traceable
order: 145
---

# {{TITLE}}

## Матрица

| Требование/история | Критерий | Код | Тест | Ссылка |
|---|---|---|---|---|
| {{REQ}} | {{AC}} | {{CODE}} | {{TEST}} | [cN] |

## Пробелы

Что не связано ни с критерием, ни с тестом.

## Что не удалось установить по коду

ID требований, US/UC/AC и связи между ними: код даёт только координаты.
