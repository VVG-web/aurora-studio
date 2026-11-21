#!/usr/bin/env python3
"""timing.py — единый контракт времени работы и ожидания.

Первый этап учитывает только то, что уже есть в `net.summary.blocked_s`: это оценка
простоя сети по снимкам, а не точные интервалы. Модуль не импортирует движок и не
пишет файлы: он только собирает сводку и форматирует её для истории и отчётов.

Инвариант: work_ms + service_wait_ms + other_wait_ms + unknown_ms == active_ms,
когда работа и ожидание известны. Старые записи без снимков не получают выдуманный
ноль: у них wait_known=false, work_ms=null.
"""
from __future__ import annotations

from typing import Iterable

TIMING_V = 1


def union_ms(intervals: Iterable[tuple[float, float]]) -> float:
    """Длина объединения интервалов; пересечения не считаются дважды."""
    rows = sorted((max(0, a), max(0, b)) for a, b in intervals if b > a)
    total = 0
    cur_a = cur_b = None
    for a, b in rows:
        if cur_a is None:
            cur_a, cur_b = a, b
        elif a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    if cur_a is not None:
        total += cur_b - cur_a
    return total


def subtract_ms(intervals: Iterable[tuple[float, float]],
                cut: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Вычесть из интервалов объединение `cut`; границы остаются миллисекундами."""
    rows = list(intervals)
    cuts = _merge(cut)
    out = []
    for a, b in rows:
        parts = [(a, b)]
        for c, d in cuts:
            nxt = []
            for x, y in parts:
                if d <= x or c >= y:
                    nxt.append((x, y))
                    continue
                if x < c:
                    nxt.append((x, c))
                if d < y:
                    nxt.append((d, y))
            parts = nxt
        out.extend(parts)
    return out


def _merge(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    rows = sorted((max(0, a), max(0, b)) for a, b in intervals if b > a)
    out = []
    for a, b in rows:
        if not out or a > out[-1][1]:
            out.append([a, b])
        else:
            out[-1][1] = max(out[-1][1], b)
    return [(a, b) for a, b in out]


def legacy_timing(seconds, net_summary: dict | None) -> dict:
    """Сводка для записей без спанов: только существующий `blocked_s`.

    `seconds` и `blocked_s` не меняются. Без снимков `work_ms` и `service_wait_ms`
    остаются None: это честное «нет данных», а не ноль секунд простоя. Если
    `blocked_s` больше длительности, ожидание обрезается до неё и `clamped=true`.
    """
    total = max(0, int(round(float(seconds or 0) * 1000)))
    if not net_summary or int(net_summary.get("snapshots") or 0) <= 0:
        return {
            "v": TIMING_V, "scope": "run",
            "active_ms": total, "total_ms": total, "queue_ms": 0,
            "work_ms": None, "service_wait_ms": None,
            "other_wait_ms": 0, "unknown_ms": None,
            "coverage": 0.0,
            "source": "legacy", "confidence": "unknown",
            "quality": {"wait_known": False, "approx": False, "legacy": True,
                        "queue_separated": False},
        }
    blocked = max(0, int(round(float(net_summary.get("blocked_s") or 0) * 1000)))
    wait = min(blocked, total)
    return {
        "v": TIMING_V, "scope": "run",
        "active_ms": total, "total_ms": total, "queue_ms": 0,
        "work_ms": total - wait, "service_wait_ms": wait,
        "other_wait_ms": 0, "unknown_ms": 0,
        "coverage": 1.0,
        "source": "net_snapshots", "confidence": "approx",
        "wait_by_service": {"network": wait},
        "wait_by_cause": {"network": wait},
        "quality": {"wait_known": True, "approx": True, "legacy": False,
                    "clamped": blocked > total, "queue_separated": False},
    }


def timing_problems(timing: dict | None) -> list[str]:
    """Проверить схему v1 и инварианты; пусто — сводка корректна."""
    if not isinstance(timing, dict):
        return ["timing: нет объекта"]
    problems = []
    if timing.get("v") != TIMING_V:
        problems.append(f"timing.v: {timing.get('v')!r}, нужен {TIMING_V}")
    for key in ("active_ms", "total_ms", "queue_ms", "other_wait_ms"):
        if not isinstance(timing.get(key), (int, float)):
            problems.append(f"timing.{key}: не число")
    active, total = timing.get("active_ms"), timing.get("total_ms")
    queue = timing.get("queue_ms")
    if all(isinstance(x, (int, float)) for x in (active, total, queue)):
        if min(active, total, queue) < 0:
            problems.append("timing: отрицательная длительность")
        if abs(queue + active - total) > 1:
            problems.append("timing: queue + active != total")
    for key in ("work_ms", "service_wait_ms", "unknown_ms"):
        value = timing.get(key)
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            problems.append(f"timing.{key}: не неотрицательное число")
    work, wait = timing.get("work_ms"), timing.get("service_wait_ms")
    unknown, other = timing.get("unknown_ms"), timing.get("other_wait_ms")
    if all(isinstance(x, (int, float)) for x in (work, wait, unknown, other, active)):
        if abs(work + wait + other + unknown - active) > 1:
            problems.append("timing: work + wait + other + unknown != active")
    coverage = timing.get("coverage")
    if not isinstance(coverage, (int, float)) or not 0 <= coverage <= 1:
        problems.append("timing.coverage: вне 0..1")
    return problems


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _words(value: int, lang: str, unit: str) -> str:
    if lang == "en":
        forms = {
            "s": ("second", "seconds"), "m": ("minute", "minutes"),
            "h": ("hour", "hours"),
        }
        one, many = forms[unit]
        return f"{value} {one if value == 1 else many}"
    forms = {
        "s": ("секунда", "секунды", "секунд"),
        "m": ("минута", "минуты", "минут"),
        "h": ("час", "часа", "часов"),
    }
    return f"{value} {plural_ru(value, *forms[unit])}"


def format_duration(ms, style: str = "compact", lang: str = "ru") -> str:
    """Длительность из миллисекунд: `3 мин 05 с`, `2 ч 05 мин`, полные слова."""
    total = max(0, int(round(float(ms or 0) / 1000)))
    if total < 60:
        if style == "full":
            return _words(total, lang, "s")
        return f"{total} с" if lang == "ru" else f"{total} s"
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if not hours:
        if style == "full":
            return _words(minutes, lang, "m") + (f" {_words(seconds, lang, 's')}" if seconds else "")
        return f"{minutes} мин" + (f" {seconds:02d} с" if seconds else "") if lang == "ru" \
            else f"{minutes} min" + (f" {seconds:02d} s" if seconds else "")
    if style == "full":
        return _words(hours, lang, "h") + (f" {_words(minutes, lang, 'm')}" if minutes else "")
    return f"{hours} ч" + (f" {minutes:02d} мин" if minutes else "") if lang == "ru" \
        else f"{hours} h" + (f" {minutes:02d} min" if minutes else "")


def _wait_phrase(ms, style: str, lang: str, approx: bool) -> str:
    value = format_duration(ms, style=style, lang=lang)
    approx = approx and ms != 0
    if style == "full":
        word = "примерно " if approx and lang == "ru" else "approximately " if approx else ""
        tail = "ожидания" if lang == "ru" else "of waiting"
        return f"{word}{value} {tail}"
    return f"{'~' if approx else ''}{value} ожидания" if lang == "ru" \
        else f"{'~' if approx else ''}{value} of waiting"


def format_work_wait(timing: dict | None, style: str = "compact", lang: str = "ru") -> str:
    """Две цифры для истории: работа и только простой внешних сервисов."""
    timing = timing or {}
    work = timing.get("work_ms")
    wait = timing.get("service_wait_ms")
    if not isinstance(work, (int, float)):
        # Старая запись: работа неизвестна, но общая длительность есть. Показываем её
        # как верхнюю цифру и честно говорим, что разбивки простоя нет.
        active = timing.get("active_ms")
        if wait is None and isinstance(active, (int, float)):
            if lang == "en":
                tail = "downtime: no data"
            else:
                tail = "время простоя: нет данных" if style == "full" else "простой: нет данных"
            return f"{format_duration(active, style, lang)} ({tail})"
        return "нет данных" if lang == "ru" else "no data"
    if not isinstance(wait, (int, float)):
        if lang == "en":
            tail = "downtime: no data"
        else:
            tail = "время простоя: нет данных" if style == "full" else "простой: нет данных"
        return f"{format_duration(work, style, lang)} ({tail})"
    quality = timing.get("quality") or {}
    approx = bool(quality.get("approx") or timing.get("confidence") == "approx")
    work_text = format_duration(work, style, lang)
    return f"{work_text} (+ {_wait_phrase(wait, style, lang, approx)})"
