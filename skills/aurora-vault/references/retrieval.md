# Retrieval policy — how to feed AuroraKnowledgeDB into LLM context

> **Собирается скриптом.** Правила ниже реализованы в `.opencode/scripts/ctx_pack.py`:
> `ctx_pack.py "<тема>" [--mode generate|review|ask|evaluate] [--budget N]`. Агент
> запускает его и работает с готовым паком, а не собирает контекст «по памяти» — так
> шапки доверия, фильтр статусов, релизный фильтр и запись в `meta/usage.log`
> выполняются всегда. Этот файл остаётся описанием правил (и того, что делать в
> нестандартных случаях), а не инструкцией по ручной сборке.

Applies to `context`, `ask`, `review`, `create` and any other task that enriches a prompt
with knowledge cards. The point: the model must always know HOW MUCH to trust each piece.

## Rule 0 — object vs knowledge

The artifact being worked on (a US under review, a draft AC, a Confluence page) is the
**object**, never knowledge. It enters the prompt in its own section («Объект работы»),
even if an identical copy exists in `Sources/` or `AuroraKnowledgeDB/`. Knowledge context comes
only from `AuroraKnowledgeDB/` cards filtered by status.

## Status filter by task type

| Task | Include statuses |
|---|---|
| generate artifact (US/AC/spec) | `verified` |
| review / quality check | `verified` (+ flag conflicts with `imported` copies) |
| answer «почему / почему не X» | + `deprecated` cards and `rejected`/`superseded` DRs, clearly labeled |
| evaluate / promote a draft | + the `draft`/`in-review`/`imported` cards under evaluation |
| exploratory question | `verified`; mention that drafts exist if relevant |

Never include `Artifacts/` or `Workspaces/` content as knowledge. Never include `Sources/` mirrors as
knowledge — they may only serve as the object or as citation evidence. `Deliverables/`
are products too — cite them as evidence («что мы сдали»), not as knowledge.

## BOOTSTRAP MODE (active while verified < 20% of cards)

The base is young: most cards are `imported`/legacy and a strict filter would return
near-empty packs. Until verified share reaches 20%:
- generate/review tasks MAY include `imported`/legacy cards, each with a loud header:
  `[BOOTSTRAP | НЕ ПРОВЕРЕНО ЧЕЛОВЕКОМ | сверься с источником]`;
- verified cards always rank first; the pack preamble must state that
  bootstrap mode is on and name the share of verified cards;
- every pack ends with a nudge: «Эти карточки ждут верификации — примите с дефолтами
  после беглой проверки» + list of included unverified cards.
Check the share cheaply: count `^status: verified` across `AuroraKnowledgeDB/**.md`
vs total cards. When ≥ 20%, this section stops applying (strict rules above).

## Release filter (applies_to)

Determine the task's release: explicit in the request → use it; otherwise `current` from
`AuroraKnowledgeDB/meta/releases.md`; if the file doesn't exist — skip release filtering.
- Card without `applies_to` → applies to every release, include.
- Card with `applies_to` not containing the task's release → EXCLUDE from facts. If it is
  the direct predecessor knowledge (linked via `related`), it may be included as history,
  labeled `[R2-only | для текущего релиза см. [[...]]]`.
- Cross-release questions («что изменилось в R3?») include both sides, each labeled with
  its releases.

## Reference lists (AuroraKnowledgeDB/Reference/)

Every context pack ALWAYS includes the abbreviations reference (compact, at the top,
header `[reference | справочник домена]`) — so the model can decode domain acronyms.
Other reference cards (participants, subsystems, code mappings) are included when the
topic touches them. Reference cards obey the usual status rules.

## Card header format

Every card included in a prompt is prefixed with a one-line trust header:

```
[verified | проверено 2026-07-05 | владелец @vadim | годно до 2026-10-05]
[verified | ПРОСРОЧЕНО: review_by 2026-06-01 — возможно устарело, перепроверь]
[deprecated | заменено: [[новая-карточка]] | только исторический контекст]
[imported | НЕ ПРОВЕРЕНО ЧЕЛОВЕКОМ | не считать фактом]
```

## Assembly order (context pack)

1. Resolve the topic → seed cards (search titles, aliases, tags; then 1 hop by `related`
   and body wiki-links; stop at 2 hops or ~15 cards, prefer higher status & fresher `verified`).
2. Sort: verified → (task-specific extras). Expired cards go last with the
   ПРОСРОЧЕНО header.
3. Prepend the pack preamble:

```
Ниже — карточки базы знаний проекта. Уровень доверия указан в шапке каждой карточки.
verified — факты; imported/draft — материал для оценки, не факты;
deprecated — история, не применять. При противоречии верь карточке с более высоким
статусом и более свежей датой verified; противоречие verified-карточек — это ошибка,
о которой надо сообщить.
```

4. Cite cards in the answer as `[[filename]]`, so the reader can jump in Obsidian.

## Usage log (обязательно для каждого пака)

После сборки пака дописать в `AuroraKnowledgeDB/meta/usage.log` по одной строке на каждую
включённую карточку (файл создать при первом использовании, формат TSV):

```
2026-07-26	context	Основной-объект
2026-07-26	review	Проверка-на-границе
```

Зачем: это сигнал о том, какие знания реально спрашивают. Лог append-only, растёт медленно
(строка на карточку), чистить его не нужно; он не является знанием и в контекст не подаётся.

Очередь верификации (`aurora_stats.py --queue`) по нему **не** строится: в живых проектах
лог оказался пустым — политику ретрива никто не выполнял, а вес по нему молча обнулялся.
Очередь считает то, что видно в самих файлах: входящие ссылки и попадание карточки в
артефакты и поставляемые документы.
