# Data Fetching (Jira & Confluence) Function
## Description
Выгрузка сырого материала из Jira и Confluence в общий кэш отчёта. Этап выполняется **один
раз на проект** (не на год): четыре шага пишут JSON, который шаги счёта позже режут по ISO-году,
поэтому смена отчётного года не стоит нового похода в корпоративные системы. В оркестраторе
(`scripts/report_analyst.py`, панель — `ops:report`) выгрузка идёт до определения годов:
какие именно годы есть у проекта, известно только из самого сырья.

Кэш — `paths.data(<name>)`, по умолчанию `.opencode/cache/reports/analyst/`. Доступы —
личными токенами из окружения: `JIRA_PERSONAL_TOKEN` и `CONFLUENCE_PAT`
(или `CONFLUENCE_PERSONAL_TOKEN`). В `aurora.config.yaml` токены не пишутся; оркестратор
подхватывает их из `.env.aurora.local` (он в `.gitignore`).

Шаги и их выход в кэш:

| Скрипт | Пишет |
|---|---|
| `fetch_issues.py` | `issues.json` |
| `fetch_subtasks.py` | `issues.json` (дописывает) |
| `fetch_full.py` | `full_status.json` |
| `fetch_confluence_metadata.py` | `confluence_raw_metadata.json` |

## Key Features
- **Общий Jira-хелпер.** `jira_api.py` — одна точка обращений, которой раньше каждый
  fetch-скрипт носил собственную копию: `api()` — Bearer-запрос с таймаутом 60 с, 4 попытки
  с экспоненциальной паузой `2**i` (1/2/4 с) и повторным броском на четвёртой;
  `search_all(jql, fields, progress)` — обход пагинации `/search` с `maxResults=100`,
  пока `startAt` не догонит `total`, с опциональным колбэком прогресса. Набор полей
  `FIELDS = key,issuetype,status,resolution,assignee,reporter,created,updated,labels,summary,parent`
  — тот, на котором держится весь дальнейший счёт.
- **Список задач по JQL.** `fetch_issues.py` берёт JQL из `atlassian.jira.default_jql`;
  нет — собирает `project = <project_key> ORDER BY key ASC`; и того и другого нет — выход
  с ошибкой «в конфиге нет ни default_jql, ни project_key». Выгрузка **не фильтруется по
  году** (год применяется при счёте), и в терминал печатается сводка «по типам» — быстрый
  способ увидеть, не поехала ли выгрузка.
- **Дотягивание подзадач.** `fetch_subtasks.py` берёт из `issues.json` задачи, которые сами
  не подзадачи (у них нет `parent` — подзадача родителем не бывает), и в чанках по 100
  ключей гоняет `parent in (…)` `ORDER BY key ASC`. В `issues.json` дописывается только то,
  чего ещё нет; в конце печатается список команд пересборки метрик (`make_analyst_metrics.py`,
  `update_analyst_metrics.py`, `verify_weekly_by_person.py`, `make_extended.py`).
- **Полная история статусов и назначений.** `fetch_full.py` для каждой задачи тянет
  `issue/<key>?expand=changelog` пулом из 8 потоков (`ThreadPoolExecutor`) и сворачивает
  changelog в истории по полям: `status_history`, `assignee_history`, `responsible_history` —
  записи `{"at", "author", "from", "to"}` (`at` — метка истории, обрезанная до секунд).
  Id кастомного поля «Ответственный» резолвится **по имени** (case-insensitive) через
  `/rest/api/2/field`, чтобы не зашивать `customfield_XXXXX`: на другой стенде id другой.
  В changelog у кастомных полей приходит имя поля, а не id, поэтому запись попадает в
  `responsible_history` по совпадению с именем **или** с `fieldId`. Каждая запись задачи
  несёт ещё `current_status`, `assignee_now`, `email`, `issuetype`, `updated`,
  `responsible_now`. Ошибки запроса не роняют прогон: запись помечается `error`,
  исключается из результата, но количество ошибок печатается в конце; код 429
  переспит 1 с и повторится.
- **Точечное обновление.** `fetch_full.py` — единственный шаг, принимающий ключи задач
  командной строкой: `python3 fetch_full.py PRJ-1300 PRJ-422` обновляет только эти записи,
  доливая их в уже выгруженный `full_status.json` (остальное сохраняется как есть).
  В конце — контрольные числа: переходы в «Аналитика - готово», «Разработка - готово»,
  «Тестирование - готово» по всей выгрузке.
- **Метаданные страниц Confluence.** `fetch_confluence_metadata.py` обходит все страницы
  пространства через `/rest/api/content` (`spaceKey`, `type=page`, `status=any`,
  `expand=version,history`, `limit=100`, пагинация по `start`) с предохранительным
  потолком в 2000 страниц. На страницу извлекаются `title`, `page_id`, `created` и
  создатель (из `history`), `updated` и автор правки (из `version`); пустой создатель
  подменяется последним автором и наоборот. Итог:
  `{"total_count", "space_key", "collection_timestamp", "pages": [...]}`.

## Related Documentation
### Technical Details
- [Analyst Report Pipeline Architecture](../../design/05-analyst-report-pipeline.md) - design overview
### Source Files
- reports/analyst/jira_api.py - общие Jira-хелперы: `token()`, `api()`, `search_all()`, `FIELDS`
- reports/analyst/fetch_issues.py - список задач по JQL → `issues.json`
- reports/analyst/fetch_subtasks.py - подзадачи выгруженных задач, дописывание в `issues.json`
- reports/analyst/fetch_full.py - changelog статусов/назначений → `full_status.json`
- reports/analyst/fetch_confluence_metadata.py - страницы пространства → `confluence_raw_metadata.json`
### Related Functions
- [Reports Configuration & Paths](./01-reports-configuration-paths.md) - адреса, токены, пути кэша
- [Analyst Metric Computation](./04-analyst-metric-computation.md) - потребляет этот сырой кэш

## Implementation Notes
`fetch_issues.py` и `fetch_subtasks.py` идут через `jira_api.py`; `fetch_full.py` исторически
старше и несёт собственный цикл запросов (свой `BASE`/`TOK`, 4 попытки, обработка 429),
не переиспользуя `api()`. `fetch_confluence_metadata.py` к Jira-хелперам не относится:
Bearer-токен из `CONFLUENCE_PAT`/`CONFLUENCE_PERSONAL_TOKEN`, лог в stderr с отметками
времени, HTTP-ошибки поднимаются наружу. Каждый скрипт запускается и автономно
(токен в окружении, рабочий каталог — корень проекта), но штатный вызов — через
`scripts/report_analyst.py`: он проверяет наличие токенов и файлов кэша и ведёт счётчик
шагов.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, reports*
