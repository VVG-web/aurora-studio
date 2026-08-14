#!/usr/bin/env python3
"""
Скрипт для обновления analyst_metrics.json с добавлением BA-SA Task событий.

Правила:
1. BA-SA Task: событие = переход в статус "Закрыто" (первый раз)
2. Неделя события = ISO-неделя по дате перехода в Закрыто
3. Assignee = assignee_now на дату перехода
4. Role = из role_of по ФИО
5. В weekly: stories++ только для Историй, others++ для прочих типов
6. В weekly добавить поле ba_sa для отдельного счёта BA-SA Task событий
7. BA-SA Task НЕ добавляется в transitions_raw (нет длительностей этапов)
8. types_available: добавить 'BA-SA Task' после 'История'
9. persons_available: добавить всех assignee BA-SA Task событий
"""

import json
from datetime import datetime
from collections import defaultdict
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from paths import DATA_DIR

YEAR = paths.YEAR

# Загрузка данных
METRICS_PATH = paths.out("analyst_metrics.json")
FULL_STATUS_PATH = os.path.join(DATA_DIR, "full_status.json")

with open(METRICS_PATH, 'r', encoding='utf-8') as f:
    metrics = json.load(f)

with open(FULL_STATUS_PATH, 'r', encoding='utf-8') as f:
    full_status = json.load(f)

# Извлекаем role_of из существующих метрик
role_of = metrics.get('role_of', {})

# 1. Находим все BA-SA Task события (первый переход в "Закрыто")
ba_sa_events = []
for key, issue in full_status.items():
    if issue.get('issuetype') == 'BA-SA Task':
        status_history = issue.get('status_history', [])
        # Находим ПЕРВЫЙ переход в 'Закрыто'
        for transition in status_history:
            if transition.get('to') == 'Закрыто':
                date_str = transition.get('at', '')
                assignee = issue.get('assignee_now')

                # Извлекаем ISO-неделю
                try:
                    dt = datetime.fromisoformat(date_str)
                except ValueError:
                    break
                # Фильтра по году здесь не было вовсе. На проекте, живущем один год,
                # это незаметно; на проекте с историей за несколько лет закрытия
                # 2021-го и 2024-го ложились в недели отчётного года — счёт вырастал
                # в разы (на TAXKG 185 вместо 19), и сверка по людям не сходилась.
                iso_year, iso_week, _ = dt.isocalendar()
                if iso_year != YEAR:
                    break
                week = f"{iso_week:02d}"
                
                # Определяем роль
                role = role_of.get(assignee)
                
                ba_sa_events.append({
                    'issue': key,
                    'date': date_str,
                    'week': week,
                    'assignee': assignee,
                    'role': role
                })
                break  # Только ПЕРВЫЙ переход в Закрыто

print(f"Найдено BA-SA Task событий: {len(ba_sa_events)}")

# 2. Обновляем weekly: добавляем ba_sa счётчик (stories НЕ включает ba_sa)
for event in ba_sa_events:
    week = event['week']
    if week in metrics['weekly']:
        # BA-SA Task НЕ добавляется в stories (stories только для Историй)
        # Добавляем только ba_sa счётчик
        metrics['weekly'][week]['ba_sa'] = metrics['weekly'][week].get('ba_sa', 0) + 1
    else:
        # Неделя не существует - создаём с ba_sa=1, stories=0, others=0
        metrics['weekly'][week] = {
            'stories': 0,
            'others': 0,
            'ba_sa': 1
        }

# Добавляем ba_sa:0 для всех недель, где его нет (для консистентности)
for week in metrics['weekly']:
    if 'ba_sa' not in metrics['weekly'][week]:
        metrics['weekly'][week]['ba_sa'] = 0

# 3. types_available: добавляем 'BA-SA Task' после 'История'
types_available = metrics.get('types_available', [])
if 'BA-SA Task' not in types_available:
    try:
        idx = types_available.index('История')
        types_available.insert(idx + 1, 'BA-SA Task')
    except ValueError:
        types_available.append('BA-SA Task')
    metrics['types_available'] = types_available

# 4. persons_available: добавляем всех assignee BA-SA Task событий
persons_available = set(metrics.get('persons_available', []))
for event in ba_sa_events:
    if event['assignee']:
        persons_available.add(event['assignee'])
metrics['persons_available'] = sorted(list(persons_available))

# 5. role_of: обновляем для новых assignee
for event in ba_sa_events:
    if event['assignee'] and event['assignee'] not in role_of:
        role_of[event['assignee']] = event['role']
metrics['role_of'] = role_of

# Сохраняем обновлённые метрики
with open(METRICS_PATH, 'w', encoding='utf-8') as f:
    json.dump(metrics, f, ensure_ascii=False, indent=2)

print("Сохранено analyst_metrics.json")

# === ОТЧЁТ ===
print("\n=== ОТЧЁТ ===")

# Контрольные числа
stories_total = sum(w.get('stories', 0) for w in metrics['weekly'].values())
others_total = sum(w.get('others', 0) for w in metrics['weekly'].values())
ba_sa_total = sum(w.get('ba_sa', 0) for w in metrics['weekly'].values())

print(f"\nWeekly totals ({YEAR}):")
print(f"  stories: {stories_total}")
print(f"  others: {others_total}")
print(f"  ba_sa: {ba_sa_total}")
print(f"  total: {stories_total + others_total}")

# Длительности переходов (только Истории)
transitions_raw = metrics.get('transitions_raw', [])

def calc_stats(days_list):
    if not days_list:
        return 0, 0, 0
    n = len(days_list)
    filtered = [d for d in days_list if d <= 100]
    mean100 = sum(filtered) / len(filtered) if filtered else 0
    # Вычисляем персентиль без numpy для совместимости с stdlib
    sorted_data = sorted(filtered)
    idx = int(len(sorted_data) * 0.95)
    p95 = sorted_data[min(idx, len(sorted_data)-1)] if sorted_data else 0
    return n, round(mean100, 1), round(p95, 1)

print("\nДлительности переходов (только Истории):")
for from_stage, to_stage in [
    ("Запланировано", "Анализ"),
    ("Анализ", "Анализ Готово"),
    ("Анализ Готово", "Разработка"),
    ("Разработка", "Разработка готово"),
    ("Разработка готово", "Тестирование"),
    ("Тестирование", "Тестирование готово"),
]:
    days = [t['days'] for t in transitions_raw 
            if t['from'] == from_stage and t['to'] == to_stage and t['issue_type'] == 'История']
    n, mean100, p95 = calc_stats(days)
    print(f"  {from_stage} → {to_stage}: N={n}, mean100={mean100}, p95={p95}")

# Распределение BA-SA Task по неделям
print(f"\nBA-SA Task события по неделям ({ba_sa_total} всего):")
week_counts = defaultdict(int)
for event in ba_sa_events:
    week_counts[event['week']] += 1
for week in sorted(week_counts.keys()):
    print(f"  неделя {week}: {week_counts[week]}")

# Кто assignee чаще всего
print("\nBA-SA Task assignee:")
assignee_counts = defaultdict(int)
for event in ba_sa_events:
    if event['assignee']:
        assignee_counts[event['assignee']] += 1
for assignee, count in sorted(assignee_counts.items(), key=lambda x: -x[1]):
    print(f"  {assignee}: {count}")

print("\n=== КОНЕЦ ОТЧЁТА ===")