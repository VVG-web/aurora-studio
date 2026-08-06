# Workflows — command procedures

Each command below is invoked as `/aurora-vault <command> [args]` or by natural-language
request. Shared rules: frontmatter per `frontmatter.md`, context per `retrieval.md`,
naming/aliases/links per `build.md`. All generated documents go to `Artifacts/`
(create subfolders on demand), named `YYYY-MM-DD_<тип>_<объект>.md`.

---

## ingest-raw <path> — первоисточник → карточки

Input: file(s) in `Raw/` (закон, регламент, словарь, XSD — то, что НЕ приходит синком).
1. Read the document; extract atomic topics using the rules in `build.md`
   (tables → glossary/statuses, definitions → glossary, procedures → processes).
   Для терминов/сокращений можно делегировать skill `glossary-extractor`, если доступен.
2. Create cards with `status: imported`, `source: Raw/<path>`, `trust: medium`.
3. Never modify or delete the raw file itself — it is permanent citation evidence.
4. Report: created cards + list of candidate duplicates found via alias search.

## ingest-meeting <транскрипт> — встреча с заказчиком → знания

Input: transcript file (text/markdown). Steps:
1. Store the transcript at `Raw/meetings/YYYY-MM-DD_<тема>/transcript.md` (if not
   already there). Transcripts are immutable evidence — never edit or summarize in place.
2. Generate a summary draft from `Templates/meeting_summary_template.md` →
   `Artifacts/meetings/YYYY-MM-DD_<тема>_summary.md`: участники, повестка, договорённости,
   разногласия, action items, quotes with speaker attribution for every key claim.
3. Extract candidates (each cites the transcript/protocol as `source`):
   - decisions agreed at the meeting → DR drafts (`status: proposed`) via the `decide` flow;
   - customer requirements (new or changed) → `AuroraKnowledgeDB/Requirements/REQ-NNN-*`
     cards, `status: imported`, `req_status: stated`; if an existing REQ changed —
     flag it, don't silently rewrite;
   - facts about domain/systems → ordinary cards, `status: imported`.
   - **ответы на открытые вопросы**: сверить встречу с реестром `Questions/`
     (`q_status: open|asked`) — на что ответили, закрыть через `answer <Q-NNN>`;
     новые неизвестные, всплывшие на встрече → новые карточки `Questions/`.
4. After the customer agrees on the summary, the analyst saves the agreed version as
   `Raw/meetings/YYYY-MM-DD_<тема>/protocol.md` (immutable; supersedes the draft as
   the citation target) and re-points `source` of extracted cards to the protocol.
5. Run `trace` if any requirement was touched. Report: summary path + candidates by type.

## ingest-tz <ТЗ> — разбор ТЗ в требования (разовая операция на редакцию ТЗ)

Input: ТЗ (или новая редакция) in `Raw/contract/`.
1. Walk the document по пунктам. Каждый пункт с обязательством исполнителя →
   REQ-карточка: `tz_ref: "п. X.Y.Z"`, формулировка словами ТЗ, `source: Raw/contract/...`,
   `req_status: agreed` (ТЗ подписано — требование согласовано по определению),
   `status: imported` до ревью аналитиком.
2. Новая редакция ТЗ: НЕ пересоздавать REQ. Diff по `tz_ref`: изменённые пункты →
   запись в «Уточнениях» карточки + пометка на перепроверку; исчезнувшие пункты →
   `req_status: rejected` с причиной «исключён редакцией N»; новые пункты → новые REQ.
3. Run `trace`. Report: created/changed/removed по пунктам.

## trace — таблица трассировки требований заказчика

Rebuild `AuroraKnowledgeDB/MOC/Трассировка-требований.md` from `Requirements/` cards
(the table is generated — manual edits will be lost):
1. One row per REQ: req_id | пункт ТЗ (tz_ref) | суть (title) | req_status | источник |
   SPEC | Jira (Epic/US) | AC | ПМИ | приёмка | вопросы | DR | releases (applies_to) |
   владелец | verified. (SPEC берётся из Specs-карточек, чьё `implements` содержит этот REQ;
   приёмка — из `Artifacts/acceptance/*` по полю `covers`; вопросы — из `Questions/*`
   со статусом `open`/`asked`, у которых REQ указан в `blocks`.)
2. Sort by tz_ref (пункты ТЗ первыми), затем req_id. Highlight problems below the table:
   - пункт ТЗ без покрытия US/Jira — риск приёмки;
   - требование, заблокированное открытым вопросом (особенно с просроченным `due`);
   - `implemented` без пройденного пункта ПМИ (статус поставлен без приёмки);
   - `implemented` без AC или без пункта ПМИ;
   - требования без Jira при `req_status: agreed` (30+ дней);
   - rejected без причины; REQ с протухшей карточкой (`review_by` истёк).
3. Cross-check `jira:` keys against the `Sources/JIRA/` mirror when available; flag dead keys.

## spec <тема> — собрать спецификацию фичи (SDD)

1. Inputs: REQ cards (`req_status: agreed`) the feature implements, verified knowledge
   per `retrieval.md` (generate mode), related DRs. Template: `Templates/spec_template.md`.
2. Create `AuroraKnowledgeDB/Specs/SPEC-NNN-<фича>.md`, `status: draft`, `based_on` = FULL list
   of cards used. Scenarios in Given/When/Then, wording EARS-style
   («Когда <триггер>, система должна <реакция>»).
3. Anything uncertain goes to «Открытые вопросы» — never silently invent.
4. Run the DoR checklist (below); report gaps.

## Definition of Ready — гейт передачи спеки в разработку

A spec may move to `verified` (согласована) and be handed off ONLY when:
- [ ] все термины резолвятся в verified+ карточки Glossary/Concepts;
- [ ] все `implements` REQ имеют `req_status: agreed`;
- [ ] нестандартные выборы оформлены DR (accepted) и слинкованы;
- [ ] раздел «Открытые вопросы» ПУСТ — то есть **нет карточек `Questions/` со статусом
      `open`/`asked`, у которых в `blocks` эта спека** (проверяется механически, см. `ops:questions`);
      вопрос, закрытый как `closed-no-answer`, допустим только вместе с DR о допущении;
- [ ] каждый сценарий имеет строку в «Критериях приёмки»;
- [ ] в `based_on` нет карточек ниже verified (или они явно помечены как допущения).
`verified` + выпущенный spec-pack = передана в разработку. После этого
изменение спеки = новая версия (`applies_to`/`supersede`) + дельта-задачи в Jira —
прямая правка кода или US в обход спеки запрещена.

## spec-pack <SPEC-NNN> — бандл для внешней разработки

Разработка живёт в ДРУГОМ репозитории/контуре: spec-pack — главный передаваемый продукт
аналитики. **Сборка механическая — скриптом**, модели тут делать нечего:

```bash
python3 .opencode/scripts/spec_pack.py SPEC-012           # состав и DoR-риски
python3 .opencode/scripts/spec_pack.py SPEC-012 --apply   # записать бандл
```

Скрипт складывает один самодостаточный markdown-файл:
1. Шапка: SPEC-id, версия, дата, git-commit базы, DoR-статус, релиз.
2. Спека целиком → приложения: тела всех `based_on`-карточек с шапками доверия →
   связанные DR (accepted; для «почему» допустимы superseded с меткой) → справочник
   аббревиатур из Reference.
3. Wiki-ссылки разрезолвить во внутренние якоря файла — снаружи базы ссылки не работают.
4. Output: `Deliverables/work/spec-packs/SPEC-NNN_v<версия>.md`; факт передачи
   фиксируется командой `release` (снапшот в `Deliverables/released/`).
5. Гейт DoR проверяется механически: REQ не в `agreed`, открытые вопросы из
   `Questions/` с `blocks: [[SPEC-NNN]]`, основания ниже `verified`. Нарушения не
   блокируют сборку, но печатаются и попадают в раздел «Риски передачи» — подрядчик
   должен видеть, на чём построен контракт.
Вопросы разработчиков по спеке возвращаются НЕ устно: каждый ответ = уточнение
спеки/REQ (новая версия или запись в «Уточнениях») — иначе контракт разъезжается.

## validate <SPEC-NNN> <объект> — сверка реализации со спекой

Объект: описание реализации, экспорт PR/диффа, тест-кейсы, документация подрядчика.
1. По каждому сценарию спеки вердикт: покрыт / противоречит / не реализован /
   не проверяемо по представленному объекту.
2. Output `Artifacts/reviews/YYYY-MM-DD_validate_SPEC-NNN.md`: таблица покрытия,
   расхождения с цитатами. Каждое расхождение — кандидат: дефект реализации ИЛИ
   устаревшая спека (решает человек; во втором случае — дельта спеки, не молчаливое
   принятие).

## assemble <ОПЗ|ПМИ|РП|...> — сборка поставляемого документа

1. Template from `Templates/`; knowledge context per `retrieval.md`
   (generate mode: verified, с учётом релиза документа).
2. Для ПМИ: секция проверок строится от трассировки — каждый REQ с `req_status: agreed`
   должен получить хотя бы один тест; недостающие — в отчёт сборки.
3. Output: `Deliverables/work/<doc>_v<version>.md`, frontmatter per `frontmatter.md`
   (type: deliverable, based_on — полный список использованных карточек).
4. Документ ревьюит и дорабатывает человек; сборка — это черновик.

## publish <артефакт> — наружу, в Confluence/Jira

Позиция фреймворка: **git — истина, Confluence — витрина**. Витрину надо обновлять, иначе
заказчик читает устаревшее, а команда возвращается к правке страниц руками.
**Публикация — скриптом:**

```bash
python3 .opencode/scripts/publish_doc.py Artifacts/reports/итог.md              # что уйдёт
python3 .opencode/scripts/publish_doc.py Artifacts/reports/итог.md --apply
python3 .opencode/scripts/publish_doc.py <файл> --parent <page_id> --apply      # новая страница
```

Без `--apply` не отправляется ничего. Новой странице нужен `--parent`, иначе корни
рассыплются по пространству. Чужая страница с таким же заголовком берётся под управление
только с `--adopt` — молча затирать написанное человеком публикация не должна. Если
storage совпал с текущим телом страницы, версия не поднимается: пустых правок в истории
Confluence быть не должно.

1. Публикуются только артефакты после ревью человеком (`review` пройден) и документы из
   `Deliverables/work/`. Карточки знаний не публикуются — они внутренний слой.
2. Страница помечается как **generated**: в начало ставится баннер «страница генерируется
   из git (<путь>, коммит <hash>), правки здесь будут потеряны — комментируйте под
   страницей». Ручные правки на generated-странице запрещены регламентом.
3. Соответствие «файл ↔ страница» хранится в самом артефакте: `confluence_page_id`,
   `published` (дата), `published_commit`. Нет id → создаём страницу и записываем id.
4. Wiki-ссылки разрезолвить (снаружи базы они не работают): либо в текст, либо в ссылки
   на опубликованные страницы, если те существуют.
5. US/AC в Jira: создание задач по шаблону `Templates/jira_us_create_template.json`
   (если он есть в проекте); ключ созданной задачи записывается в артефакт и в поле
   `jira` соответствующего REQ — иначе трассировка снова разойдётся.
6. Перед публикацией — предупредить, если в `based_on` есть карточки ниже verified:
   наружу уходит непроверенное знание.
7. После публикации: `sync:confluence` (зеркало подхватит опубликованное) и `sync:audit`.

## export <документ> — офисный формат для передачи

Скрипт: `python3 .opencode/scripts/ship_doc.py <файл> --export docx|pdf [--reference <шаблон.docx>]`.
Убирает frontmatter, разрезолвливает wiki-ссылки, конвертирует pandoc'ом, кладёт результат
рядом. Требует pandoc. Экспорт — производная копия: правки вносятся в markdown и документ
экспортируется заново; правка docx «на месте» разрывает связь с базой. Факт передачи
фиксируется командой `release`.

## sync:jira-status — статусы задач обратно в требования

Механика вынесена в скрипт (`jira_status.py`), процедура — `references/maintenance.md`.
Аналитику здесь остаётся содержательная часть: разобрать список кандидатов (закрытые
задачи ≠ выполненное требование), решить судьбу работы без требований и довести
подтверждённые требования до `implemented` через приёмку. Затем `ops:trace` — таблица
покажет новые разрывы.

## release <документ> — фиксация переданной версии

1. Copy `Deliverables/work/<doc>...` → `Deliverables/released/<doc>_v<version>_<YYYY-MM-DD>.md`
   (+ переданный бинарник, если есть). Snapshot немедленно неизменяем.
2. Set `released:` date in the work copy; в REQ-карточках, покрытых документом
   (например ПМИ), обновить поле `pmi`.
3. Report: какие карточки из `based_on` имеют статус ниже verified — риск: сдали документ,
   собранный на непроверенном знании.

## verify / promote [card|folder|all-imported] — гейт качества

Запись решения делает скрипт `kb_verify.py` (см. `maintenance.md`): он же не пропустит
карточку без `source`, с битыми ссылками или уже deprecated. Ниже — то, что остаётся
человеку и агенту.

1. For each card: show the analyst a short digest (title, source, key claims,
   duplicates by alias, broken links).
2. On approval: set `status: verified`, `owner`, `verified: today`,
   `review_by: today + 3 months` (или срок, названный владельцем). Fix links, update `_index.md`.
   **Quick mode («прими с дефолтами» / "apply defaults"):** no questions asked — owner =
   the requesting analyst, review_by = +3 months, applies_to untouched. One-line
   confirmation per card.
3. Team mode: batch the changes into one git commit/PR so a second analyst reviews;
   `verified` is the top status of the base: there is no further step.

## diff — дрейф после синка (запускать после confluence-sync/jira-export)

1. Compare `Sources/` state vs cards: for every `verified` card whose `source`
   file hash ≠ `source_synced` state, extract what changed.
2. Output `Artifacts/reports/YYYY-MM-DD_drift.md`: card, owner, what changed in source,
   suggested action (re-verify / supersede / ignore formatting change).
3. Do NOT touch the cards themselves (see build.md lifecycle rule 2). Group findings by
   owner so each analyst gets their re-verification list.

## review <US|AC|page> — проверка качества артефакта

Object: Confluence page (via MCP or `Sources/Confluence/...`) or local file.
1. Build knowledge context per `retrieval.md` (review mode): terms from `Glossary/`,
   linked algorithms from `Processes/`, statuses, related DRs.
2. Check: терминология против глоссария; ссылки на существующие алгоритмы/статусы;
   противоречия с verified-карточками; полнота по шаблону
   (`Templates/user_story_template.md` / `AC_template.md`); тестируемость критериев;
   противоречия с accepted DRs.
3. Output `Artifacts/reviews/YYYY-MM-DD_review_<ID>.md`: verdict, findings ranked by
   severity, each finding cites the card `[[...]]` or DR it contradicts. Never edit the
   object itself unless explicitly asked.

## create <тип> <тема> — генерация артефакта (us|ac|algorithms|dictionaries|screens|contracts|mappings|role-model|diagrams|reviews|reports|meetings|drafts|spec)

1. Template from `Templates/` (US → user_story_template, AC → AC_template, ПР →
   proektnoe_reshenie_template); if user's `Prompts/` has a matching prompt (US_create,
   AC_new) — follow its instructions too.
2. Knowledge context per `retrieval.md` (generate mode: verified only).
3. Output: `Artifacts/<тип>/` строго по существующей папке типа; спека →
   `AuroraKnowledgeDB/Specs/`; frontmatter `status: draft`,
   `based_on: [список использованных карточек]`, `template: <какой шаблон>`.
   **Список типов закрыт (инвариант 9).** Неизвестный тип → НЕ создавать папку:
   назвать ближайший стандартный тип и предложить выбор, либо, если работа
   исследовательская/разовая, положить в `Workspaces/<задача>/`. Если тип нужен всем
   проектам — это изменение kit'а (PR в `structure_dirs.txt` + таблицы SKILL.md и
   `conventions.md` + CHANGELOG), а не папка в одном проекте.
4. Remind: артефакт публикуется в Sources/Confluence/Jira только после ревью человеком; в
   AuroraKnowledgeDB он не попадает — вернётся туда через синк и обычный ingest-гейт.

## question <суть> — вопрос к заказчику (Q-NNN)

Незнание — объект, а не строчка в переписке. Пока вопрос не оформлен, он не виден ни в
трассировке, ни в DoR, и всплывает на приёмке.

1. Create `AuroraKnowledgeDB/Questions/Q-NNN-<кратко>.md` from `Templates/question_template.md`
   (NNN — следующий свободный номер). Обязательно: `owner` (кто ведёт, не кто отвечает),
   `asked_to`, `blocks` (какие REQ/SPEC стоят), `due`.
2. Одно неизвестное = одна карточка. Сформулировать так, чтобы можно было отправить
   заказчику без редактуры; приложить 2 гипотезы («A или B?» отвечается быстрее открытого вопроса).
3. Проверить базу ДО создания: `ask` по теме — возможно, ответ уже есть в verified-карточке
   или в отклонённой DR. Спрашивать заказчика о том, что мы уже знаем, дорого.
4. Отметить блокировку: в спеке раздел «Открытые вопросы» ссылается на `[[Q-NNN]]`
   (а не пересказывает вопрос) — так DoR считается механически.
5. При отправке — `q_status: asked` + дата `asked` и канал.

## answer <Q-NNN> — ответ получен

1. Заполнить в карточке: `q_status: answered`, `answered`, `answer_source` (протокол
   встречи, письмо, страница) и текст ответа. Без `answer_source` ответ не принимается —
   «сказали на созвоне» через месяц не доказательство.
2. **Разнести ответ** (главный шаг, без него вопрос закрыт формально):
   - уточнение требования → правка [[REQ-NNN]] (формулировка, `req_status`);
   - выбор из вариантов → DR через `decide`, ссылка из карточки вопроса;
   - новый факт о домене → обычная карточка знания (ответ живёт в базе, а не в вопросе);
   - снять блокер в спеке: убрать пункт из «Открытых вопросов».
3. Если ответа нет и он уже не придёт: `q_status: closed-no-answer` + DR с допущением,
   по которому работаем. Допущение без DR запрещено — иначе оно станет «фактом» само.
4. Run `trace`, если затронуты требования.

## questions — реестр вопросов (дежурный, еженедельно)

Числа берутся из `aurora_stats.py` (секция «Вопросы»). Отчёт: открытые по владельцам,
просроченные (`due` в прошлом), что именно они блокируют (REQ/SPEC), самые старые.
Просроченный вопрос — повод не «подождать ещё», а решение: переспросить, эскалировать
или закрыть допущением через DR.

## acceptance <объект> — приёмка и испытания

Конец цикла: то, ради чего собирались ПМИ и трассировка.

1. Вход: программа испытаний (ПМИ), протокол/замечания заказчика, состав проверенного.
   Артефакт: `Artifacts/acceptance/YYYY-MM-DD_acceptance_<объект>.md` из
   `Templates/acceptance_report_template.md`, `covers` = проверенные REQ.
2. По каждому пункту ПМИ вердикт: пройдено / пройдено с замечанием / не пройдено /
   не проверялось (с причиной). Вердикт связывается с REQ — иначе приёмка не попадёт в трассировку.
3. **Разбор замечаний заказчика — по четырём типам**, каждое замечание получает адрес:
   дефект → задача в Jira; новое требование → REQ (`req_status: stated`) и разговор об
   объёме работ; вопрос → карточка `Questions/`; разночтение → уточнение REQ/спеки
   (+ кандидат в DR). Замечание без адреса теряется — это главный риск шага.
4. Обновить базу: `req_status: implemented` — **только** для требований с пройденным
   пунктом ПМИ; поле `pmi` в REQ; подписанный протокол → `Deliverables/released/`
   (неизменяем) или `Raw/customer/`, ссылка в `protocol`.
5. Run `trace`, затем `retro <приёмка>` — 15 минут: чего база не знала, когда мы ошиблись.

## decide <тема> — Decision Record

1. Interview the analyst (or parse the provided text): контекст, рассмотренные варианты
   (минимум 2, включая отклонённые — с причинами), решение, следствия, затронутые карточки.
2. Create `AuroraKnowledgeDB/Decisions/DR-NNNN-<суть>.md` from
   `Templates/decision_record_template.md`, `status: proposed` (or `accepted` if the
   decision is already made by the team).
3. If it replaces an older DR: set old DR `status: superseded`, `superseded_by: [[DR-NNNN]]`
   — the ONLY edit allowed to an accepted DR.
4. Update affected knowledge cards: link the DR under `## Обоснование`; if the decision
   changes current truth — run `supersede` on those cards.

## supersede <old-card> — замена знания без потери истории

Механику выполняет `kb_supersede.py <старая> <преемник> --apply` (deprecated,
`superseded_by`, история, переезд в `_archive/`, переписывание входящих ссылок).
Человеку остаётся решение и текст карточки-преемника.

1. Create/point to the successor card (status per its provenance).
2. Old card: `status: deprecated`, `superseded_by: [[successor]]`, move file to
   `AuroraKnowledgeDB/_archive/`, keep all aliases (links keep resolving).
3. Successor card: `supersedes: [[old]]`, add `## История` line (когда и почему заменили,
   ссылка на DR если есть).
4. Fix inbound links: verified cards must not link to the deprecated card (frontmatter.md
   invariant); update `_index.md`.

## garden — еженедельная гигиена (дежурный аналитик)

Четыре скрипта подряд, процедуры каждого — в `maintenance.md`:

```
kb_lint.py            # что сломано
kb_fix.py --all       # починить механическое (сначала dry-run)
aurora_stats.py       # числа: статусы, протухшее, сироты, битые источники, риски поставки
aurora_stats.py --queue           # что верифицировать первым
```

Модели остаётся то, что скрипты не решают, — и только это:
- разобрать нерешённые ссылки и группы двойников из отчёта `kb_fix`;
- сверить AGENTS.md с фактической структурой корня (изменилась — обновить);
- раздать находки владельцам: протухшие карточки, карточки без owner, `imported` старше
  30 дней (промотировать или деприкейтнуть через `supersede`);
- записать итог в `Artifacts/reports/YYYY-MM-DD_garden.md`: одноэкранная сводка из
  `aurora_stats` + список задач по владельцам.

## context <тема> — context pack для любого запроса

Запустить `python3 .opencode/scripts/ctx_pack.py "<тема>" [--mode …] [--budget …]` и
работать с готовым паком; вручную контекст не собирать (правила и режимы —
`retrieval.md`, процедура — `maintenance.md`). Сохранить копию для внешнего чата:
`--save` кладёт файл в `Artifacts/drafts/`.

## ask <вопрос> — ответ по базе с цитатами

Контекст берётся паком: `ctx_pack.py "<тема>" --mode ask` (для «почему/почему не» этот
режим добавляет `deprecated`-карточки и отклонённые DR как историю). Собирать контекст
вручную не нужно — правила уже в скрипте. «Почему/почему не» questions MUST pull `Decisions/` including
rejected/superseded DRs (labeled as history). Every claim in the answer cites `[[card]]`.
If the base has no answer — say so explicitly, never fill gaps silently; suggest which
card is missing and offer to draft it into `_inbox` state. Если ответа нет **и знать его
может только заказчик** — предложить оформить вопрос (`question`), а не гадать: так
незнание попадает в реестр и в DoR, вместо того чтобы всплыть на приёмке.

## eval — регрессионный прогон golden questions

Run after big syncs, migrations, refactorings (и по запросу). Source:
`AuroraKnowledgeDB/meta/golden_questions.md`.
1. For each question: answer it via the `ask` flow (retrieval rules apply, bootstrap
   included), then compare with the эталон: СОВПАЛО / РАСХОЖДЕНИЕ / НЕТ ОТВЕТА.
2. Meta-questions (M*) check mechanics: the base must refuse to invent.
3. Output `Artifacts/reports/YYYY-MM-DD_eval.md`: score (N/total), diffs with details.
   Any РАСХОЖДЕНИЕ = регрессия знаний — разобраться, что сломалось (карточка потеряна,
   ссылка битая, знание перезаписано), починить, добавить вопрос-регрессию если новый класс.
4. When verifying important knowledge or after «база ответила неверно» incidents —
   add a golden question.

## retro <событие> — выученные уроки (после приёмки/инцидента)

15 минут после значимого события (приёмка, инцидент, провальное ревью, расхождение в
validate). Вопрос один: **чего база не знала или знала неверно, когда мы ошиблись?**
1. Соберите 2–3 факта/решения, которых не хватило → карточки (draft/imported) или DR.
2. Если ошибка от устаревшего знания — `supersede` + короче `review_by` у соседей темы.
3. Если ИИ ответил неверно на знании из базы — добавьте golden question на этот случай.
4. Report: что добавлено, в `Artifacts/reports/YYYY-MM-DD_retro_<событие>.md`.

## status — здоровье базы

Числа даёт `python3 .opencode/scripts/aurora_stats.py` (статусы, % verified+, режим
bootstrap, протухшее, сироты, битые источники, REQ/DR/спеки, артефакты с `based_on`,
поставляемые документы на непроверенных основаниях). Модель их не пересчитывает —
комментирует динамику и называет три ближайших действия. Подробно — `maintenance.md`.
Monthly (последняя пятница, после garden): `aurora_stats.py --append-metrics` дописывает
строку в `AuroraKnowledgeDB/meta/metrics.md`; замечания на артефакт и eval score
проставляет человек.

## Правка проверенной карточки

Zettelkasten живёт дописыванием: карточку уточняют, связывают, переписывают. Статус в
Авроре — не замок на файле, а утверждение про текст: «этот текст человек сверил с
источником и отвечает за него».

Отсюда порядок, который не мешает работать и не даёт статусу врать:

1. **Связи, метки, попадание в MOC** статуса не меняют — это не утверждение о фактах.
   `kb:links --cards` и `kb:moc` спокойно правят `verified`-карточки.
2. **Человек дописал тело** — статус остаётся `verified` ровно до ближайшего `kb:lint`:
   отпечаток `verified_hash` разошёлся, линтер называет карточку. Дальше решает человек —
   `kb:verify --refresh` (перечитал, отвечаю за новый текст) либо понизить до `draft`
   (дописал начерно, вернусь).
3. **Машина принесла новое из источника** — тело не переписывается никогда. Новое идёт
   отдельной секцией «Из источника (не проверено)», в отчёт пишется `DRIFT`.

Дублировать карточку ради черновика не нужно: два файла об одном понятии — это то, с чем
`kb:dedupe` потом борется, а слияние придётся делать руками. Черновик живёт в той же
карточке, отдельной секцией; если черновик перерос в самостоятельную мысль — это повод
завести **новую карточку про другое**, а не копию старой.
