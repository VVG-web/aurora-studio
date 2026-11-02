---
kind: cr-sequence
title: "Последовательность вызовов по коду"
version: "1.0"
enabled: "false"
scope: route
out: Artifacts/diagrams/code
prompt: PromptCode/cr-sequence.md
purity: traceable
order: 120
---

<!--
ШАБЛОН: cr-sequence_v1.0.md — диаграмма последовательности от точки входа.
По умолчанию выключен: полезен, когда срез вызовов подтверждён индексом.
-->

# {{TITLE}}

## Назначение

<!-- ИНСТРУКЦИЯ: Какая точка входа и какой сценарий. Только по коду. -->

## Диаграмма

```mermaid
sequenceDiagram
    participant C as {{CALLER}}
    participant S as {{SERVICE}}
    participant D as {{STORE}}
    C->>S: {{CALL}}
    S->>D: {{QUERY}}
    D-->>S: {{RESULT}}
    S-->>C: {{RESPONSE}}
```

<!-- ИНСТРУКЦИЯ: Участники и вызовы только из подтверждённого среза. Не рисуйте
     вызов, которого нет в коде. -->

## Участники

| Участник | Роль | Ссылка |
|---|---|---|
| `{{NAME}}` | {{ROLE}} | [cN] |

## Что не удалось установить по коду

<!-- ИНСТРУКЦИЯ: Обязательный раздел: асинхронные ветки, внешние системы, таймауты. -->
