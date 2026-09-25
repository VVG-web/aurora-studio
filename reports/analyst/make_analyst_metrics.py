#!/usr/bin/env python3
"""make_analyst_metrics.py — метрики аналитиков для дашборда (JSON для фронтенда).

Собирает из уже загруженных файлов:
- full_status.json — статус-история и assignee-история
- issues.json — типы задач
- ростер проекта — роли сотрудников

Вывод: analyst_metrics.json в кэше отчёта
"""
import json, csv, collections, datetime, os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from paths import DATA_DIR, ROSTER_PATH

YEAR = paths.YEAR
FULL_STATUS_PATH = os.path.join(DATA_DIR, "full_status.json")
ISSUES_PATH = os.path.join(DATA_DIR, "issues.json")
OUTPUT_PATH = paths.out("analyst_metrics.json")

full = json.load(open(FULL_STATUS_PATH, encoding="utf-8"))
issues_list = json.load(open(ISSUES_PATH, encoding="utf-8"))

# issues_list -> маппинг key -> issuetype.name
issue_types = {}
for iss in issues_list:
    key = iss["key"]
    issue_types[key] = iss["fields"]["issuetype"]["name"]

roster = paths.roster()  # ФИО -> Роль

# ---------------------------------------------------------------------------
# Маппинг статусов
# ---------------------------------------------------------------------------
STAGE_OF = {
    "Запланировано": {"Сделать"},
    "Анализ": {"Аналитика"},
    "Анализ Готово": {"Аналитика - готово"},
    "Разработка": {"Разработка"},
    "Разработка готово": {"Разработка - готово", "Разработка готова"},
    "Тестирование": {"Тестирование"},
    "Тестирование готово": {"Тестирование - готово", "Тестирование готово"},
}
ORDER = ["Запланировано", "Анализ", "Анализ Готово", "Разработка",
         "Разработка готово", "Тестирование", "Тестирование готово"]
STAGE_OF_R = {}
for stage, sset in STAGE_OF.items():
    for s in sset:
        STAGE_OF_R[s] = stage

READY = "Аналитика - готово"

# Порядок статусов для распознавания возврата. Возврат — это уход из «Аналитика -
# готово» или любого более позднего статуса назад, в бэклог или в анализ: сданную
# работу обнулили и её придётся сдавать заново.
#
# Статусы подзадач («В работе», «Код ревью», «На ревью») в лестницу не входят
# намеренно: это отдельный workflow подзадач и BA/QA-задач, он с «Аналитика -
# готово» не пересекается. Неизвестный статус возвратом не считается — гадать
# о чужом процессе хуже, чем промолчать.
RANK = {
    "Бэклог": 0,
    "Сделать": 1,
    "Аналитика": 2,
    READY: 3,
    "Разработка": 4,
    "Разработка - готово": 5, "Разработка готова": 5,
    "Тестирование": 6,
    "Тестирование - готово": 7, "Тестирование готово": 7,
    "Закрыто": 8,
}

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
from aurora_common import local_week, parse_time  # noqa: E402 — правило времени одно на движок


def iso_week(ts):
    """ISO-неделя 'WW' отметки — по часам системы (`aurora_common.local_week`)."""
    w = local_week(ts)
    return f"{w[1]:02d}" if w else None


def iso_year(ts):
    """ISO-год отметки — по часам системы."""
    w = local_week(ts)
    return w[0] if w else None


def to_dt(ts):
    """datetime из отметки — местное время без зоны: длительности считаются между такими."""
    dt = parse_time(ts)
    return dt.astimezone().replace(tzinfo=None) if dt else None

from assignee_resolver import AssigneeResolver, load_synced_assignees

# Резолвер собирается один раз. Раньше он создавался внутри get_assignee_at, то есть
# на каждый вызов: перечитывалась вся папка Sources/JIRA (сотни файлов) и заново
# строились индексы по всей выгрузке. На одном годе это были лишние секунды, на
# нескольких — минуты пустой работы.
_resolver = AssigneeResolver(full, issues_list, roster,
                             load_synced_assignees(paths.sources_jira(),
                                                   paths.jira()["project_key"] + "-"))


def get_assignee_at(issue_key, at_ts):
    """Исполнитель задачи на момент at_ts (см. assignee_resolver)."""
    return _resolver.at(issue_key, at_ts)

# ---------------------------------------------------------------------------
# 1) Недельные переходы в «Аналитика - готово»
# ---------------------------------------------------------------------------
BUCKETS = {"stories": 0, "others": 0, "rework_stories": 0, "rework_others": 0}
weeks_data = collections.defaultdict(lambda: dict(BUCKETS))
persons_set = set()  # assignee в переходах в «Аналитика - готово»
rework_raw = []      # повторные сдачи: что вернули, откуда и кто сдавал заново


def deliveries(issue):
    """Сдачи задачи в «Аналитика - готово», свёрнутые по (ISO-год, неделя).

    Задачу могут вернуть и принять заново несколько раз за одну неделю — у
    PRJ-A-490 это три сдачи за неделю 06. Считать их тремя артефактами нельзя:
    работа за неделю сделана один раз, а счёт вырос бы втрое. Поэтому неделя,
    в которую задачу сдали, засчитывается один раз, сколько бы раз за эту
    неделю сдачу ни повторяли.

    Порядок сохраняется: первый элемент — самая ранняя сдача за всю историю
    задачи, остальные пришли после возврата.
    """
    seen, out = set(), []
    for tr in issue.get("status_history", []):
        if tr["to"] != READY:
            continue
        y, w = iso_year(tr["at"]), iso_week(tr["at"])
        if y is None or w is None or (y, w) in seen:
            continue
        seen.add((y, w))
        out.append((y, w, tr["at"]))
    return out


def returned_from(issue, before_ts):
    """Статус, из которого задачу вернули в работу перед этой сдачей.

    Возврат — переход из «Аналитика - готово» или любого более позднего этапа
    (разработка, тестирование, закрыто) назад в бэклог или в анализ. Именно он
    обнуляет уже сданную работу и заставляет сдавать её заново.
    """
    last = None
    edge = parse_time(before_ts)
    for tr in issue.get("status_history", []):
        at = parse_time(tr["at"])
        if at and edge and at >= edge:
            break
        a, b = RANK.get(tr.get("from")), RANK.get(tr["to"])
        if a is not None and b is not None and a >= RANK[READY] and b < RANK[READY]:
            last = tr.get("from")
    return last


for key, issue in full.items():
    typ = issue_types.get(key, "?")
    # BA-SA Task считается отдельным ведром по переходу в «Закрыто»
    # (update_analyst_metrics.py). Если засчитывать её ещё и здесь, одна задача
    # попадает и в others, и в ba_sa — итог отчёта завышается, а сверка по людям
    # (verify_weekly_by_person.py) перестаёт сходиться: она так не считает.
    if typ == "BA-SA Task":
        continue
    for i, (y, w, at) in enumerate(deliveries(issue)):
        if y != YEAR:
            continue

        # assignee на момент перехода
        assignee = get_assignee_at(key, at)
        if assignee:
            persons_set.add(assignee)

        # Первая сдача за всю историю задачи — обычная работа. Всё, что после
        # неё, случилось потому, что задачу вернули: работа была сделана и
        # обнулена. Для сотрудника это такая же сделанная и сданная работа,
        # поэтому она считается — но отдельным ведром, чтобы возвраты было
        # видно, а не растворялись в общем столбце.
        base = "stories" if typ == "История" else "others"
        bucket = base if i == 0 else "rework_" + base
        weeks_data[w][bucket] += 1
        if i:
            rework_raw.append({"issue": key, "week": w, "assignee": assignee,
                               "issue_type": typ, "returned_from": returned_from(issue, at)})

# ---------------------------------------------------------------------------
# 2) transitions_raw: длительности переходов между этапами (только Истории)
# ---------------------------------------------------------------------------
transitions_raw = []
types_set = set()

for key, issue in full.items():
    typ = issue_types.get(key, "?")
    if typ != "История":
        continue
    
    # первое вхождение в каждый этап
    seen = {}  # stage -> datetime
    for tr in issue.get("status_history", []):
        stage = STAGE_OF_R.get(tr["to"])
        if stage is None:
            continue
        if stage in seen:
            continue
        dt = to_dt(tr["at"])
        if dt is None:
            continue
        seen[stage] = dt
    
    # переходы по порядку
    for i in range(len(ORDER) - 1):
        a, b = ORDER[i], ORDER[i+1]
        if a not in seen or b not in seen:
            continue
        if seen[a] > seen[b]:
            continue
        
        days = (seen[b] - seen[a]).total_seconds() / 86400.0
        dt_b = seen[b]
        week_b = iso_week(dt_b.strftime("%Y-%m-%dT%H:%M:%S"))
        year_b = iso_year(dt_b.strftime("%Y-%m-%dT%H:%M:%S"))
        
        # assignee на момент входа в b
        assignee = get_assignee_at(key, dt_b.strftime("%Y-%m-%dT%H:%M:%S"))
        role = roster.get(assignee, None) if assignee else None
        
        transitions_raw.append({
            "from": a,
            "to": b,
            "days": round(days, 1),
            "week": week_b,
            "assignee": assignee,
            "role": role,
            "issue_type": typ,
            "issue": key
        })
        types_set.add(typ)

# ---------------------------------------------------------------------------
# Собираем итоговый JSON
# ---------------------------------------------------------------------------
weeks_sorted = sorted(weeks_data.keys())
weekly = {w: weeks_data[w] for w in weeks_sorted}

persons_available = sorted(persons_set)
role_of = {p: roster.get(p, None) for p in persons_available}

output = {
    "weeks": weeks_sorted,
    "weekly": weekly,
    "transitions_raw": transitions_raw,
    "types_available": sorted(types_set),
    "persons_available": persons_available,
    "role_of": role_of,
    # Повторные сдачи: какую задачу вернули, откуда и кто сдавал заново.
    # По ним дашборд подписывает красный сектор — иначе столбик «возвратов 3»
    # виден, но разобраться в нём нечем.
    "rework_raw": rework_raw,
}

# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------
with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"WROTE {OUTPUT_PATH}")

# ---------------------------------------------------------------------------
# Контрольные числа
# ---------------------------------------------------------------------------
# weekly totals
total_stories = sum(weeks_data[w]["stories"] for w in weeks_sorted)
total_others = sum(weeks_data[w]["others"] for w in weeks_sorted)
total_rework = sum(weeks_data[w]["rework_stories"] + weeks_data[w]["rework_others"]
                   for w in weeks_sorted)
total_all = total_stories + total_others + total_rework

print(f"\n=== WEEKLY ({YEAR}) ===")
print(f"stories: {total_stories}")
print(f"others: {total_others}")
print(f"rework (повторные сдачи после возврата): {total_rework}")
print(f"total: {total_all}")
if rework_raw:
    by = collections.Counter(r["returned_from"] or "—" for r in rework_raw)
    print("  откуда возвращали:", ", ".join(f"{k}: {v}" for k, v in by.most_common()))

# transitions stats
print(f"\n=== TRANSITIONS_RAW COUNT ===")
print(f"total transitions: {len(transitions_raw)}")

# duration stats per transition
print(f"\n=== DURATION STATS (mean100/p95) ===")
TRANSITIONS = [(ORDER[i], ORDER[i+1]) for i in range(len(ORDER)-1)]
TRANS_LABEL = {
    ("Запланировано", "Анализ"): "Запланировано → Анализ",
    ("Анализ", "Анализ Готово"): "Анализ → Анализ Готово",
    ("Анализ Готово", "Разработка"): "Анализ Готово → Разработка",
    ("Разработка", "Разработка готово"): "Разработка → Разработка готово",
    ("Разработка готово", "Тестирование"): "Разработка готово → Тестирование",
    ("Тестирование", "Тестирование готово"): "Тестирование → Тестирование готово",
}

def percentile_mean(samples, pct):
    if not samples:
        return None
    if pct >= 100:
        return sum(samples) / len(samples)
    s = sorted(samples)
    cut = max(1, int(round(len(s) * pct / 100.0)))
    return sum(s[:cut]) / len(s[:cut])

for (a, b) in TRANSITIONS:
    samples = [t["days"] for t in transitions_raw if t["from"] == a and t["to"] == b]
    if not samples:
        continue
    n = len(samples)
    mean100 = sum(samples) / n
    mean95 = percentile_mean(samples, 95)
    print(f"{TRANS_LABEL[(a,b)]}: N={n}, mean100={mean100:.1f}, p95={mean95:.1f}")