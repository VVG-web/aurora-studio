#!/usr/bin/env python3
"""Определение исполнителя задачи на момент события.

Общий модуль для make_analyst_metrics.py и verify_weekly_by_person.py:
раньше в обоих лежала своя копия get_assignee_at, и они разъезжались.

Три источника по убыванию надёжности:
1. assignee_history — кто был назначен на этот момент;
2. assignee_now, если истории назначений нет вовсе — Jira не пишет в changelog
   исполнителя, заданного при создании, поэтому пустая история означает
   «назначили один раз и не меняли», и текущий исполнитель равен тогдашнему;
3. аналитик по аналитическим подзадачам истории — показывает исполнителя
   конкретной аналитической работы, а не обязательно владельца истории,
   поэтому применяется последним.
"""
import os
import re
import collections


def load_synced_assignees(sources_dir, key_prefix=""):
    """Свежие исполнители из карточек Sources/JIRA/*.md (их пишет sync:jira).

    Этот синк обновляется чаще, чем выгрузка fetch_full.py, и заполненный
    сегодня Assignee появляется сначала там. Берём только frontmatter.
    """
    fresh = {}
    if not os.path.isdir(sources_dir):
        return fresh
    for name in os.listdir(sources_dir):
        if not name.endswith(".md") or (key_prefix and not name.startswith(key_prefix)):
            continue
        key, assignee = name[:-3], None
        with open(os.path.join(sources_dir, name), encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i and line.startswith("---"):  # конец frontmatter
                    break
                m = re.match(r'\s*assignee:\s*"?(.*?)"?\s*$', line)
                if m:
                    assignee = m.group(1).strip()
        if assignee:
            fresh[key] = assignee
    return fresh

# Тип «BA Sub-Task» — прямой признак аналитической подзадачи; часть таких
# подзадач заведена обычной «Подзадачей», поэтому смотрим ещё и на название.
# Названия в проекте разнородные: «Проработка US», «Проработка истории 4.3.9»,
# «Написание истории», «Описание алгоритма», «Актуализация US» — всё это автор.
ANALYST_SUBTASK_RE = re.compile(
    r"проработ|написан|описан|актуализ|доработ|\bus\b|\bac\b|истори|алгоритм|маппинг|"
    r"интеграц|требован|специфик", re.I)

# Префиксы [Back]/[Front]/[Design] — это реализация, не аналитика.
IMPL_SUBTASK_RE = re.compile(r"^\s*\[(back|front|design)\]", re.I)

# Ревью — единственный вид аналитической подзадачи, исполнитель которой НЕ автор
# истории: её делает второй аналитик. Слово «проверка» сюда не годится — оно
# встречается в авторских названиях («проработка алгоритма проверки МЧД из ТК»).
REVIEW_SUBTASK_RE = re.compile(r"ревью|review|вычитк|рецензи", re.I)


def is_analyst_subtask(fields):
    """Подзадача, исполнитель которой — автор аналитики по истории."""
    summary = fields.get("summary", "")
    if IMPL_SUBTASK_RE.search(summary) or REVIEW_SUBTASK_RE.search(summary):
        return False
    return fields["issuetype"]["name"] == "BA Sub-Task" or bool(ANALYST_SUBTASK_RE.search(summary))


class AssigneeResolver:
    def __init__(self, full, issues_list, roster=None, synced_assignees=None):
        self.full = full
        self.roster = roster or {}
        self.synced = synced_assignees or {}
        # родитель -> исполнители его аналитических подзадач
        self.analyst_subtasks = collections.defaultdict(collections.Counter)
        for iss in issues_list:
            f = iss["fields"]
            parent = f.get("parent")
            if not parent or not is_analyst_subtask(f):
                continue
            a = f.get("assignee")
            if a and a.get("displayName"):
                self.analyst_subtasks[parent["key"]][a["displayName"]] += 1

    def analyst_by_subtasks(self, issue_key):
        """Аналитик истории по её аналитическим подзадачам."""
        cand = self.analyst_subtasks.get(issue_key)
        if not cand:
            return None
        analysts = {n: c for n, c in cand.items() if self.roster.get(n) == "Analyst"}
        return max((analysts or cand).items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _value_at(history, at_ts):
        """Значение поля на момент at_ts по его changelog-истории."""
        hist = sorted(history or [], key=lambda x: x["at"])
        if not hist:
            return None

        value = None
        seen_before = False
        for entry in hist:
            if entry["at"] <= at_ts:
                seen_before = True
                # to='' — значение сняли
                value = entry.get("to") or None

        # Событие раньше первой записи changelog: тогдашнее значение
        # зафиксировано в её поле from — это точно, а не догадка.
        if not seen_before and hist[0].get("from"):
            return hist[0]["from"]

        return value

    def at(self, issue_key, at_ts):
        """Исполнитель задачи на момент at_ts (или None, если его правда не было)."""
        issue = self.full.get(issue_key, {})

        assignee = self._value_at(issue.get("assignee_history"), at_ts)
        if assignee:
            return assignee

        # Истории назначений нет вовсе: исполнителя задали при создании и ни разу
        # не меняли (Jira не пишет такое в changelog), значит текущий = тогдашний.
        if not issue.get("assignee_history") and issue.get("assignee_now"):
            return issue["assignee_now"]

        # В части историй исполнителя пишут не в Assignee, а в поле «Ответственный».
        return (self._value_at(issue.get("responsible_history"), at_ts)
                or issue.get("responsible_now")
                or self.analyst_by_subtasks(issue_key)
                # Последняя надежда: исполнителя проставили задним числом, уже
                # после события. На момент события его в Jira не было, поэтому
                # источник самый слабый — но точнее, чем «Не назначен».
                # Синк Sources/JIRA свежее выгрузки, поэтому он вперёд.
                or self.synced.get(issue_key)
                or issue.get("assignee_now"))
