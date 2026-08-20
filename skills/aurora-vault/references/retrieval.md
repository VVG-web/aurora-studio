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
| generate artifact (US/AC/spec) | `knowledge` |
| review / quality check | `knowledge` (+ flag conflicts with `draft` copies) |
| answer «почему / почему не X» | + `deprecated` cards and `rejected`/`superseded` DRs, clearly labeled |
| evaluate a draft | + the `draft` cards under evaluation, each labeled |
| exploratory question | `knowledge`; mention that drafts exist if relevant |

Служебные карточки (`status: index` — карты содержания, `_index.md`) в пак не идут
никогда: это навигация, а не знание.

Never include `Artifacts/` or `Workspaces/` content as knowledge. Never include `Sources/` mirrors as
knowledge — they may only serve as the object or as citation evidence. `Deliverables/`
are products too — cite them as evidence («что мы сдали»), not as knowledge.

## BOOTSTRAP MODE (active while `knowledge` < 20% of cards)

База молода: большинство карточек `draft`, и строгий фильтр вернёт пустой пак. Пока доля
`knowledge` не дошла до 20%:
- задачи generate/review МОГУТ включать `draft`, каждую с громкой шапкой
  `[BOOTSTRAP | ЧЕРНОВИК | сверься с источником]`;
- `knowledge` всегда идут первыми; преамбула пака обязана сказать, что режим включён, и
  назвать долю;
- пак заканчивается подсказкой: «Эти карточки — черновики. Доля растёт не приёмкой, а
  движением задач в Jira: `ops:trace-table` → `kb:trust`».

Долю считает `ops:stats` — служебные карточки (`index`) в знаменатель не входят.

**Чего в этом режиме делать нельзя** — «принять» карточки, чтобы поднять долю: доверие
вычисляется, а не присваивается. Низкая доля означает либо что задачи ещё в работе, либо
что связей между карточками и задачами не нашлось. Второе лечится трассировкой, а не
статусами.

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
[knowledge | задачи закрыты: PRJ-123 «Закрыто» | сверено 2026-08-21]
[draft | PRJ-456 в работе — знание может измениться]
[draft | связей с задачами нет — источник не подтверждён]
[deprecated | заменено: [[новая-карточка]] | только исторический контекст]
```

Основание берётся из `trust_basis` самой карточки: шапка не пересказывает статус, а
показывает, **почему** он такой. Человеку и модели это разные вещи: «черновик» ничего не
говорит, «задача вернулась в работу» говорит всё.

## Assembly order (context pack)

1. Resolve the topic → seed cards (search titles, aliases, tags; then 1 hop by `related`
   and body wiki-links; stop at 2 hops or ~15 cards, prefer `knowledge` and fresher
   `trust_checked`).
2. Sort: `knowledge` → (task-specific extras). Черновики идут последними, каждый со своей
   причиной из `trust_basis`.
3. Prepend the pack preamble:

```
Ниже — карточки базы знаний проекта. Уровень доверия указан в шапке каждой карточки
вместе с основанием: доверие вычислено по статусам связанных задач, а не присвоено
человеком. knowledge — факты; draft — материал для оценки, не факты; deprecated —
история, не применять. При противоречии верь карточке с более свежей `trust_checked`;
противоречие двух knowledge-карточек — это ошибка базы, о которой надо сообщить.
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
