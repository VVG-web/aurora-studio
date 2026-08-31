# Aurora Vault — Extraction & Lifecycle Function

## Description

Скилл aurora-vault превращает проектную документацию в живую базу знаний из атомарных,
связанных заметок. Кластер покрывает, как знание извлекается из первоисточников, ведётся
по жизненному циклу и получает класс доверия. Работа делится между детерминированными скриптами
движка (`.opencode/scripts/*.py`) и моделью: скрипты ведут планирование, механику и
бухгалтерию, модель принимает семантические решения (что за одна тема, как она
называется, в какой раздел попадает).

Все команды вызываются как `/aurora-vault <command> [args]` или естественным языком.
Короткие имена без префикса — исторические алиасы, которые всегда работают (например
`/aurora-vault build` ≡ `/aurora-vault kb:build`).

## Key Features

- **`kit:` engine commands** — `kit:list`, `kit:doctor`/`doctor`, `kit:hooks`,
  `kit:remap-sources`, `kit:update`, `kit:i18n`, `kit:skills`, `kit:mcp`.
- **`kb:build` (`build`)** — извлечение карточек: план и бухгалтерия — `build_plan.py`,
  фактическое извлечение — модель. `--partition N` печатает готовое распределение партии
  (список файлов + правила жизненного цикла).
- **`kb:ingest <path>` (`ingest`, `ingest-raw`, `ingest-meeting`, `ingest-tz`)**, плюс
  `kb:ingest-office [path]` для docx/pdf/xlsx/pptx → markdown-транскрипты. TZ-ветка
  поднимает REQ-карточки с `tz_ref`, meeting-ветка — саммари, DR, REQ и факты, остальное
  даёт атомарные карточки.
- **`kb:dedupe`** — поиск и слияние дублей (`kb_fix.py --dupes` / `--merge`), включая
  пакетный `--merge-all` с объявленным правилом приоритета.
- **`kb:trust` (`trust`)** — класс доверия карточки, вычисляется по таблице трассировки и
  статусам связанных задач Jira; человек никогда не назначает. Четыре класса источника:
  `raw` (папка `Raw/`, подписанный документ заказчика — правда по определению), `trusted`
  (все связанные задачи в доверенных статусах → `status: knowledge`), `draft` (хоть одна
  задача в статусе черновика → `status: draft`), `unknown` (связей с задачами нет —
  «под вопросом»). Приоритет: одна черновая задача перевешивает десять готовых; прямая
  связь сильнее косвенной. Понижение класса знание не стирает: тело остаётся, в подвал
  пишется строка «класс понижен <дата>, задача вернулась в работу». `--apply` записывает
  классы в карточки.
- **`kb:kind` (`kind`)** — тип карточки (`dictionary` / `document` / `knowledge`), который
  решает, кому позволено править тело.
- **`kb:links` (`links`, `graph`)** — граф связей: ключи Requirement и номера story;
  `--cards` переносит ссылки в `related:` карточек.
- **`kb:moc` (`moc`)** — карты содержания из группировок `moc_groups.txt` плюс список
  сиротских карточек (`kb_moc.py`); `--suggest` предлагает новые карты по четырём сигналам.
- **`kb:split`, `kb:garden` (`garden`)** — деление переросшей карточки и недельный
  гигиенический проход.
- **`ops:trace-table` / трассировка (`kb_trace_table.py`, `kb_trace.py`)** — таблица
  «артефакт ↔ задача» в `AuroraKnowledgeDB/meta/trace/` (не в `Sources/`, которую sync
  затрёт) и один граф связей (wiki-ссылки + `based_on`) с тремя вопросами:
  `--impact <карточка>` — что устареет, если она изменилась; `--explain <файл>` — на чём
  собран документ и чему он верит; `--requirements` — сквозная таблица «пункт ГК → ТЗ →
  работа → REQ → Epic → US». Панель: `ops:impact`, `ops:trace`.
- **`agent:` built-in agent commands** — `agent:build`, `agent:distill`, `agent:ping`,
  `agent:width`, `agent:aliases`, `agent:ask`; раннер, которым управляет модель, с
  контекстными окнами кольца LLM-бэкендов, ролью планировщика и ролью верификатора
  («Момус»).

## Related Documentation

### Technical Details
- [Skills Support Files Design](../../design/02-skills-support-files.md) - skill layout and registration

### Source Files
- skills/aurora-vault/SKILL.md - main skill with folder semantics, commands and invariants
- skills/aurora-vault/references/build.md - правила извлечения, жизненный цикл, нормализация имён, отчётность
- skills/aurora-vault/references/frontmatter.md - card frontmatter schema v4, status & trust semantics
- scripts/kb_trace_table.py - таблица трассировки «артефакт ↔ задача» (прямые и косвенные связи)
- scripts/kb_trust.py - вычисление класса доверия по статусам задач
- scripts/kb_trace.py - трассировка: `--impact` / `--explain` / `--requirements` (с 1.44.0 объединяет бывшие `kb_impact.py` и `aurora_trace.py`)
- docs/knowledge-rules.md / docs/knowledge-rules-tldr.md - правила базы: доверие, связи, типы карточек

### Related Functions
- [Frontmatter Schema v4](./06-vault-frontmatter-schema.md) - схема карточки, статусы и кто правит тело
- [Sources & Maintenance](./02-aurora-vault-maintenance.md) - sync-зеркала, из которых приходит зеркало задач
- [Retrieval & Production](./03-aurora-vault-production.md) - context packs and artifact workflow

## Implementation Notes

Контракт извлечения гарантируется скриптами, а не дисциплиной модели: `build_plan.py --done
<файл>` сверяется с базой — метка отказывается (exit 1), если хотя бы одна карточка с этим
`source:` не существует. Имена карточек даёт `aurora_common.card_stem` — один общий
парсер для всего движка: копии `os.path.splitext` в отдельных скриптах ломали имена на
точках (`US-3.6.2-…` → `US-3.6`).

Цепочка доверия: `sync:jira` зеркалирует задачи → `ops:trace-table` связывает артефакты с
задачами → `kb:trust` читает статусы задач и ставит класс на каждую карточку. Прогоняется
на каждом проходе — смена статуса в Jira обязана двигать базу вместе с собой; доверие
пересчитывается заново, кэша нет. Прямая связь — либо совпадение номера на границе токена
(`AC-10.3.1` в заголовке артефакта и `US-10.3.1` в теме задачи; `10.3.11` — уже другая
история, это единственный способ не склеить две соседние story), либо видимая ссылка
(ключ задачи в тексте артефакта или `page_id` страницы в задаче — достаточно одной
стороны). Косвенная связь — через артефакты, глубиной до двух переходов: третий переход в
большой базе связал бы всё со всем.

Жизненный цикл закрыт и вычисляется движком: `knowledge` · `draft` · `index` · `deprecated`;
легаси-статусы читаются, но не назначаются. Центральный принцип правил базы (`docs/
knowledge-rules.md`): доверие — свойство источника, а не карточки, поэтому человек его ни
к кому не «подтверждает».

---
*Last updated: 2026-08-28*
*Areas: skills, aurora-vault, extraction, trust, tracing*
