# Aurora Vault — Frontmatter Schema (v4) Function

## Description

Единая frontmatter-схема, которой обладают все карточки `AuroraKnowledgeDB/` — контракт,
который читают и пишут все остальные кластеры. Схема v4 lifecycle-driven: `status` — это
класс доверия (вычисляется движком, а не назначается человеком), `kind` решает, кто может
переписать тело карточки, а `schema_version` позволяет `kb:schema` мигрировать базу между
версиями. Канонический референс — `skills/aurora-vault/references/frontmatter.md`; модель
смысла доверия — `docs/knowledge-rules.md` (одна страница — `docs/knowledge-rules-tldr.md`).

Перенос версий: `python3 .opencode/scripts/kb_schema.py` (что в базе и что изменится) и
`--apply` (записать). Карточка без отметки считается версией 1 (легаси-база), а если у неё
уже есть `status` и `type` — версией 2.

## Key Features

- **Правило «тип решает, кто правит тело»** — поле `kind: dictionary | document |
  knowledge` (ставит `kb:kind`): `dictionary` (словари, перечисления) переносится
  целиком, модель не трогает; `document` (договор, ТЗ, регламент) — дословно, менять
  запрещено; `knowledge` — модель пишет тезис и переосмысляет его. Выбор человека сильнее
  правила: если `kind` уже стоит, движок его не перетирает.
- **Status semantics** — закрытая шкала `knowledge · draft · index · deprecated`,
  вычисляется движком по статусам связанных задач Jira (`ops:trace-table` → `kb:trust`):
  `knowledge` — все задачи в доверенных статусах (факт); `draft` — хоть одна в работе либо
  связей нет вовсе (идея, требует проверки); `index` — служебная карта/навигация;
  `deprecated` — заменено через `kb:supersede` (история, цитировать для «почему»).
  Переходы не «разрешаются» — они происходят сами при следующем `kb:trust`. Старые значения
  (`imported`, `in-review`, `verified`, `accepted`) читаются (`verified` считается
  доверенным), но больше не назначаются.
- **Полная схема (v4)** — `title`, `aliases`, `tags`, `type`
  (`concept | process | glossary | system | role | status-model | reference | decision |
  spec | moc`), `schema_version`, `status`, `kind`, `built: machine` (метку ставит машина;
  её отсутствие НЕ значит «писал человек»), `trust` (класс источника: `raw | trusted |
  draft | unknown`), `trust_basis` (почему — с ключом задачи и её статусом), `trust_checked`,
  `distilled`, `created`, `updated`, `source`, `source_synced`, `supersedes`,
  `superseded_by` (заполняется ТОЛЬКО у deprecated), `based_on` (для spec и артефактов — из
  каких карточек собрано), `applies_to` (релизы; пусто = все), `related`.
- **Extras по типам** — `type: requirement` (`REQ-NNN-…`, `req_id`,
  `req_status: stated → agreed → implemented | rejected`, `tz_ref` — пункт ТЗ, корень
  трассировки, `jira`, `acceptance`, `pmi`; `rejected` требует причину, `implemented` —
  запись в `jira`; таблица `MOC/Трассировка-требований.md` генерируется, не правится
  руками). `type: reference` (`Reference/`, живые справочные карточки, один список = одна
  карточка). `type: question` (`Questions/`, `Q-NNN-…`, `q_status: open → asked → answered
  | closed-no-answer`; `answered` требует `answered` + `answer_source` — ответ «на словах»
  не ответ; `closed-no-answer` требует DR с допущением; один вопрос = одно неизвестное).
  `type: decision` (`Decisions/DR-NNNN-…`, `proposed → accepted | rejected → superseded`;
  append-only, accepted/rejected неизменяемы, DR не удаляется). `type: spec` (`Specs/`,
  `SPEC-NNN-…`, `spec_id`, `implements`, `based_on` обязательно; после передачи в
  разработку неизменна для релиза, сценарии EARS). `type: acceptance`
  (`Artifacts/acceptance/`, `verdict: passed | passed-with-remarks | failed`;
  `req_status: implemented` проставляется только при пройденном пункте ПМИ).
  `type: deliverable` (`Deliverables/work/`, `doc: ОПЗ|ПМИ|РП|…`, `based_on` обязательно;
  `release` замораживает копию в `Deliverables/released/` — неизменяемо, `based_on` даёт
  обратную трассировку «какие сданные документы устарели»).
- **Версионирование схемы** — v1 (до 1.3.0): шапка без гарантий, часто только `title`;
  v2 (1.3.0): обязательные `status`, `trust`, `type` по разделу; v3 (1.10.0): убраны
  ступень `canonical` и поле `audience`; v4 (1.35.0): поле `trust` выведено — доверие
  выражает `status`.
- **Диаграммы, ассеты и инварианты** — диаграммы (ERD, архитектура, sequence, BPMN) живут
  в карточках как mermaid (diffable, reviewable); бинарные (drawio, png, xlsx) — только в
  `_assets/` с обёрточной карточкой (status/owner/`review_by`), бинарник без обёртки —
  сирота (garden помечает). Инварианты перед любой записью: `status` есть и в шкале;
  `deprecated` ⇒ `superseded_by` или ссылка на DR; `kind` есть; `knowledge`-карточка не
  ссылается на `deprecated`; есть хотя бы одна связь; deprecated уезжают в `_archive/`
  (wiki-ссылки работают независимо от папки).

## Related Documentation

### Technical Details
- [Skills Support Files Design](../../design/02-skills-support-files.md) - skill layout and registration

### Source Files
- skills/aurora-vault/references/frontmatter.md - каноническая схема v4, status semantics, extras по типам, инварианты
- skills/aurora-vault/references/build.md - правила alias и извлечения, с которыми схема взаимодействует
- examples/sample_card.md - лёгкое scaffold-образец карточки в старом словаре (`status: imported`, `trust: medium`, `owner`, `audience`, `verified`, `review_by`) — живой пример того, что мигрирует `kb:schema`
- scripts/kb_schema.py - перенос `schema_version` (`--to N`, `--apply`, `--allow-dirty`; текущая версия движка — 4)
- docs/knowledge-rules.md / docs/knowledge-rules-tldr.md - доверие как свойство источника и правило «тип решает, кто правит тело»

### Related Functions
- [Extraction & Lifecycle](./01-aurora-vault-extraction.md) - как вычисляется trust и откуда приходят связи
- [Sources & Maintenance](./02-aurora-vault-maintenance.md) - `kb:supersede` переносит deprecated в `_archive`

## Implementation Notes

`applies_to`: пусто/отсутствует = верно для всех релизов. Список релизов проекта и текущий
релиз ведутся в `AuroraKnowledgeDB/meta/releases.md` (одна строка на релиз + маркер
`current`). Знание, меняющееся от релиза к релизу, — это ДВЕ карточки: старая получает
`applies_to: [R2]` и остаётся `knowledge` (она верна для своего релиза — это не
deprecated!), новая — `applies_to: [R3+]`, взаимные ссылки в `related`. `deprecated` —
только когда знание неверно для ВСЕХ релизов. Легаси-карточки (только
`title/aliases/tags/created/updated/source/related`) валидны, но считаются `status: draft`
до апгрейда; апгрейд ленивый — при первом касании любым воркфлоу.

---
*Last updated: 2026-08-28*
*Areas: skills, aurora-vault, schema, frontmatter*
