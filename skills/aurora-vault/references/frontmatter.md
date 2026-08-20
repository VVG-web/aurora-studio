# Frontmatter schema v4 — lifecycle

Every card in `AuroraKnowledgeDB/` carries these fields. Legacy cards (only
`title/aliases/tags/created/updated/source/related`) are valid but are treated as
`status: imported` until upgraded. Upgrade lazily: whenever any workflow
touches a legacy card, add the missing fields.

## Full schema

```yaml
---
title: "Алгоритм смены статусов заявки"
aliases: []               # см. build.md — правила alias обязательны
tags: [process.algorithm]
type: process             # concept | process | glossary | system | role | status-model |
                          # reference | decision | spec | moc
schema_version: 3         # версия схемы карточки; проставляет kb:schema
status: knowledge         # knowledge | draft · служебные: index, deprecated
kind: knowledge           # dictionary | document | knowledge — кому позволено править тело
built: machine            # метку ставит машина; её отсутствие НЕ значит «писал человек»
trust: trusted            # класс источника: raw | trusted | draft | unknown
trust_basis: "все связанные задачи в доверенных статусах, например PRJ-123 — «Закрыто»"
trust_checked: 2026-08-21 # когда доверие пересчитывалось (kb:trust)
distilled: 2026-08-21     # когда модель написала тезис (kind: knowledge)
created: 2026-05-17
updated: 2026-07-05
source: "Sources/Confluence/SM - Алгоритмы/04. ..."   # или Raw/..., или Sources/JIRA/...
source_synced: 2026-07-05 # версия источника на момент последней обработки
supersedes: []            # wiki-ссылки на карточки, которые эта заменила
superseded_by:            # заполняется ТОЛЬКО у deprecated
based_on: []              # только для type: spec и артефактов — из каких карточек собрано
applies_to: [R2, R3]      # релизы, для которых карточка верна; пусто = все релизы
related: []
---
```

`applies_to` semantics: empty/absent = верно для всех релизов. Список релизов проекта и
текущий релиз ведутся в `AuroraKnowledgeDB/meta/releases.md` (создать при первом использовании:
одна строка на релиз + маркер `current`). Когда знание меняется от релиза к релизу —
это ДВЕ карточки: старая получает `applies_to: [R2]` (остаётся `knowledge` — она верна
для своего релиза, это не deprecated!), новая — `applies_to: [R3+]`, взаимные ссылки в
`related`. `deprecated` — только когда знание неверно для ВСЕХ релизов.

## Status semantics

Шкала закрыта и **вычисляется движком**. Человек статус не назначает: он выводится из
статусов задач Jira, связанных с источником карточки (`ops:trace-table` → `kb:trust`).

| status | Откуда берётся | LLM treats as |
|---|---|---|
| `knowledge` | все связанные задачи в доверенных статусах | факт |
| `draft` | хоть одна задача в работе, либо связей нет вовсе | идея, требует проверки |
| `index` | служебный: карта содержания, `_index.md` | навигация, не знание |
| `deprecated` | заменено через `kb:supersede` | история; НЕ применять, цитировать для «почему» |

Переходы не «разрешаются» — они **происходят сами** при следующем `kb:trust`: задача
вернулась в работу, и карточка стала черновиком. Почему именно — записано в
`trust_basis` той же карточки, с ключом задачи и её статусом.

Старые базы содержат `imported`, `in-review`, `verified`, `accepted`. Движок их **читает**
(`verified` считается доверенным), но больше не назначает. `kb:trust` переводит базу на
новую шкалу за один прогон.

## Версия схемы

Схема карточки меняется вместе с движком, и без отметки в самой карточке узнать, прошла
ли миграция, невозможно. `schema_version` проставляет `kb:schema`; карточка без отметки
считается версией 1 (легаси-база), а если у неё уже есть `status` и `type` — версией 2.

| Версия | Что появилось | Когда |
|---|---|---|
| 1 | шапка без гарантий: часто только `title` | до 1.3.0 |
| 2 | обязательные `status`, `trust`, `type` по разделу | 1.3.0 |
| 4 | поле `trust` выведено: доверие выражает `status` | 1.35.0 |
| 3 | убраны ступень `canonical` и поле `audience` | 1.10.0 |

```bash
python3 .opencode/scripts/kb_schema.py            # что в базе и что изменится
python3 .opencode/scripts/kb_schema.py --apply
```

## Requirement extras (type: requirement)

Requirement cards live in `AuroraKnowledgeDB/Requirements/`, named `REQ-NNN-<кратко-суть>.md`
(NNN — next free number in `Requirements/_index.md`). Card `status` tracks knowledge
status as usual; the requirement's own lifecycle is separate:

```yaml
type: requirement
req_id: REQ-042
req_status: stated        # stated → agreed → implemented | rejected
stated: 2026-07-08        # когда заказчик заявил требование
source: "Raw/contract/ТЗ_ред2.pdf"   # ТЗ/протокол/письмо/закон
tz_ref: "п. 4.2.1"        # пункт ТЗ (если требование из ТЗ) — корень трассировки
jira: [PROJ-123]          # Epic/US, реализующие требование
acceptance: []            # wiki-ссылки на AC-карточки
pmi: []                   # пункты ПМИ, проверяющие требование
decisions: []             # wiki-ссылки на связанные DR
applies_to: []            # релизы
```

`req_status` rules: `rejected` requires a reason in the body (and a DR link if the
rejection was a team decision). `implemented` requires at least one `jira` entry.
The traceability table `AuroraKnowledgeDB/MOC/Трассировка-требований.md` is GENERATED from
these cards by the `trace` workflow — never edit the table by hand.

## Reference extras (type: reference)

Domain reference lists in `AuroraKnowledgeDB/Reference/`, hand-maintained by the team:
abbreviations, adjacent-subsystem lists, participants and roles, code mappings.
One list = one card (`type: reference`) with the normal lifecycle fields; owner keeps it
current like any `knowledge` card. These are living cards — editing them is normal (unlike
Raw/, which is immutable evidence). `build` may extract atomic Glossary cards FROM
reference lists (they are manifest sources), but the list itself stays the editable master.

## Question extras (type: question)

Вопросы к заказчику (и к смежникам) — карточки в `AuroraKnowledgeDB/Questions/`,
имя `Q-NNN-<кратко-суть>.md`. Смысл: незнание — это объект, а не строчка в чате.
Пока вопрос открыт, он виден в трассировке и блокирует DoR спеки.

```yaml
type: question
q_id: Q-007
q_status: open            # open → asked → answered | closed-no-answer
owner: "@vadim"           # кто ведёт вопрос (не тот, кто отвечает)
asked_to: "заказчик, отдел X"
channel: "встреча"        # встреча | письмо | Confluence | звонок
asked: 2026-07-20         # когда реально задан
due: 2026-07-31           # к какой дате нужен ответ
answered:                 # дата ответа
answer_source: "Raw/meetings/2026-07-30_.../protocol.md"   # ГДЕ ответ зафиксирован
blocks: ["[[REQ-042]]", "[[SPEC-012]]"]
```

Правила:
- `q_status: answered` требует `answered` и `answer_source` — ответ «на словах» не ответ.
- Ответ не остаётся только в карточке вопроса: он идёт в REQ/спеку/DR, а карточка
  вопроса становится историей («когда и почему мы это узнали»).
- `closed-no-answer` — заказчик не отвечает, работаем по допущению: допущение
  фиксируется DR, ссылка на него обязательна в теле.
- Один вопрос = одно неизвестное. Пакет вопросов к встрече — это несколько карточек
  плюс список в повестке, а не одна карточка «вопросы по заявкам».

## Acceptance extras (type: acceptance, артефакт)

Результаты приёмки/испытаний — `Artifacts/acceptance/YYYY-MM-DD_acceptance_<объект>.md`
(шаблон `Templates/acceptance_report_template.md`). Это артефакт, не знание.

```yaml
type: acceptance
acceptance_of: "ПМИ этап 2"
covers: [REQ-042, REQ-043]        # какие требования проверялись
verdict: passed-with-remarks      # passed | passed-with-remarks | failed
held: 2026-09-15
protocol: "Deliverables/released/Протокол_испытаний_2026-09-15.md"
based_on: []
```

`req_status: implemented` проставляется требованию только при пройденном пункте ПМИ —
источник истины об этом здесь. Замечания заказчика разбираются на четыре типа
(дефект / новое требование / вопрос / разночтение), см. шаблон.

## Decision Record extras (type: decision)

```yaml
type: decision
status: accepted          # proposed → accepted | rejected → superseded
date: 2026-07-01
supersedes: ["[[DR-0007-...]]"]
```

DR rules (append-only):
- Accepted/rejected DRs are **immutable**. The only allowed edits: set
  `status: superseded` + `superseded_by` when a newer DR replaces it.
- Never delete a DR. Rejected options stay inside the DR body forever.
- Naming: `Decisions/DR-NNNN-<кратко-суть>.md`, NNNN — next free number in `Decisions/_index.md`.

## Spec extras (type: spec)

Specs live in `AuroraKnowledgeDB/Specs/`, named `SPEC-NNN-<фича>.md` (next number in
`Specs/_index.md`). Template: `Templates/spec_template.md`.

```yaml
type: spec
spec_id: SPEC-012
implements: ["[[REQ-042]]"]   # какие требования реализует
decisions: ["[[DR-0007]]"]    # решения-ограничения
jira: [PROJ-234]              # Epic/US, созданные ИЗ спеки
based_on: []                  # карточки-основания (status: knowledge) — обязательно
applies_to: [R2]
```

Lifecycle: `draft → knowledge` (движок пересчитывает по статусам задач)
(передана в разработку через spec-pack). Gate между `knowledge` и handoff — Definition of
Ready (см. workflows.md). После передачи в разработку спека неизменна для своего релиза: изменение =
новая версия (`applies_to`/`supersede`) + дельта-задачи. Сценарии — Given/When/Then,
формулировки EARS-style.

## Deliverable extras (type: deliverable)

Files in `Deliverables/work/`, assembled from the knowledge base:

```yaml
type: deliverable
doc: ОПЗ                  # ОПЗ | ПМИ | РП | ...
version: "2.1-draft"
based_on: []              # карточки, из которых собран документ — обязательно
template: "Templates/..."
released:                 # дата передачи заказчику; заполняется командой release
applies_to: [R2]
```

`release` freezes a copy to `Deliverables/released/<doc>_v<version>_<date>.md` (plus the
delivered binary if any) — released snapshots are IMMUTABLE. `based_on` enables reverse
tracing: when a card changes, `garden`/`diff` can list released documents built on it
(«какие сданные документы устарели»).

## Diagrams and assets

- Diagrams (ERD, architecture, sequence, BPMN) live INSIDE cards as mermaid code —
  diffable, reviewable. One diagram = one wrapper card in `Systems/` or `Processes/`
  with normal lifecycle fields; atomic cards link to the wrapper.
- Binary diagrams (drawio, png, xlsx) only when mermaid can't express them: file goes to
  `AuroraKnowledgeDB/_assets/`, and a wrapper card carries status/owner/review_by and embeds it
  (`![[...]]`). A binary without a wrapper card is an orphan (garden flags it).
- ERD cards: `source` must name the real source of truth (DDL/migrations + release);
  keep `review_by` short (1–2 months) — БД дрейфует молча, синка на неё нет.

## Invariants (check before any write)

- [ ] `status` present and valid; `deprecated` ⇒ `superseded_by` or DR link present
- [ ] `status` внутри шкалы: `knowledge` · `draft` · `index` · `deprecated`
- [ ] у карточки есть `kind` — иначе неизвестно, можно ли переписывать тело
- [ ] `knowledge`-карточка не ссылается на `deprecated`
- [ ] у карточки есть хоть одна связь: до неё должно быть можно дойти
      (link to the successor instead)
- [ ] deprecated cards move to `AuroraKnowledgeDB/_archive/` (create the folder if missing);
      wiki-links keep working in Obsidian regardless of folder
