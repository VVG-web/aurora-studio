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

# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def iso_week(ts):
    """Вернуть ISO-неделю как строку 'WW' (00-padded)."""
    d = None
    for c in [ts, ts[:10]]:
        try:
            d = datetime.date.fromisoformat(c[:10])
            break
        except Exception:
            pass
    if d is None:
        return None
    return f"{d.isocalendar()[1]:02d}"

def iso_year(ts):
    """Вернуть ISO-год."""
    d = None
    for c in [ts, ts[:10]]:
        try:
            d = datetime.date.fromisoformat(c[:10])
            break
        except Exception:
            pass
    if d is None:
        return None
    return d.isocalendar()[0]


def to_dt(ts):
    """datetime из timestamp."""
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", ""))
    except Exception:
        return None

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
weeks_data = collections.defaultdict(lambda: {"stories": 0, "others": 0})
persons_set = set()  # assignee в переходах в «Аналитика - готово»

for key, issue in full.items():
    typ = issue_types.get(key, "?")
    # BA-SA Task считается отдельным ведром по переходу в «Закрыто»
    # (update_analyst_metrics.py). Если засчитывать её ещё и здесь, одна задача
    # попадает и в others, и в ba_sa — итог отчёта завышается, а сверка по людям
    # (verify_weekly_by_person.py) перестаёт сходиться: она так не считает.
    if typ == "BA-SA Task":
        continue
    for tr in issue.get("status_history", []):
        if tr["to"] != "Аналитика - готово":
            continue
        y = iso_year(tr["at"])
        w = iso_week(tr["at"])
        if y is None or w is None or y != YEAR:
            continue
        
        # assignee на момент перехода
        assignee = get_assignee_at(key, tr["at"])
        if assignee:
            persons_set.add(assignee)
        
        if typ == "История":
            weeks_data[w]["stories"] += 1
        else:
            weeks_data[w]["others"] += 1

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
    "role_of": role_of
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
total_all = total_stories + total_others

print(f"\n=== WEEKLY ({YEAR}) ===")
print(f"stories: {total_stories}")
print(f"others: {total_others}")
print(f"total: {total_all}")

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