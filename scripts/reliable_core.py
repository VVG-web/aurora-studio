#!/usr/bin/env python3
"""reliable_core.py - общие сроки, fallback и понятные ошибки для процедур Авроры.

Модуль не знает, что именно выполняется: поиск, вызов модели, grill-раунд или запись
артефакта. Он даёт один контракт на все этапы:

- у этапа есть срок и общий дедлайн;
- исключение не выходит наружу, а превращается в `StageResult` с причиной;
- повтор ограничен и явно назван;
- по истечении срока возвращается частичный результат, а не исключение;
- ошибка для человека состоит из причины и следующего шага.

Это не замена транспортному кольцу `agent_core`: кольцо выбирает бэкенд, а этот слой
не позволяет цепочке этапов висеть без предела.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_STAGE_TIMEOUT = 300.0
DEFAULT_TOTAL_DEADLINE = 900.0


def human_error(code: str, reason: str, action: str = "") -> dict:
    """Ошибка в одном виде: код, причина и что человеку делать дальше."""
    reason = " ".join(str(reason or "").split())[:600]
    action = " ".join(str(action or "").split())[:400]
    message = f"Не удалось получить результат, потому что {reason or 'причина не названа'}."
    if action:
        message += f" Что можно сделать: {action}"
    return {"code": str(code or "unknown"), "reason": reason,
            "message": message, "next_step": action}


def error_text(error: dict | None) -> str:
    """Текст ошибки для CLI и панели; пусто, если ошибки нет."""
    return str((error or {}).get("message") or (error or {}).get("reason") or "")


def remaining(deadline: float | None, now: float | None = None) -> float:
    """Сколько секунд осталось до общего дедлайна; без дедлайна - бесконечность."""
    if not deadline:
        return float("inf")
    return max(0.0, float(deadline) - (time.time() if now is None else now))


def stage_timeout(deadline: float | None, default: float = DEFAULT_STAGE_TIMEOUT,
                  now: float | None = None) -> float:
    """Срок этапа: не больше остатка общего дедлайна и не меньше 0.1 с."""
    left = remaining(deadline, now)
    if left == float("inf"):
        return max(0.1, float(default))
    return max(0.1, min(float(default), left))


@dataclass
class StageResult:
    """Итог одного ограниченного этапа."""

    ok: bool
    value: Any = None
    error: dict | None = None
    seconds: float = 0.0
    timed_out: bool = False
    attempts: int = 1
    degraded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "value": self.value, "error": self.error,
                "seconds": round(self.seconds, 3), "timed_out": self.timed_out,
                "attempts": self.attempts, "degraded": list(self.degraded)}


def _run_in_thread(fn: Callable[[], Any], timeout: float) -> tuple[bool, Any, BaseException | None]:
    """Выполнить fn, но не ждать дольше timeout.

    Поток объявлен daemon: если он всё ещё ждёт сеть после срока, процесс сможет
    завершиться, а вызывающий получит явный таймаут. Это сторож последнего рубежа;
    штатные вызовы должны иметь собственный deadline внутри.
    """
    box: queue.Queue = queue.Queue(maxsize=1)

    def target():
        try:
            box.put((True, fn(), None))
        except BaseException as exc:  # noqa: BLE001 - причина уходит в StageResult
            box.put((False, None, exc))

    thread = threading.Thread(target=target, name="aurora-reliable-stage", daemon=True)
    thread.start()
    try:
        ok, value, exc = box.get(timeout=max(0.05, float(timeout)))
        return ok, value, exc
    except queue.Empty:
        return False, None, TimeoutError(f"этап не уложился в {timeout:.1f} с")


def run_stage(name: str, fn: Callable[[], Any], *, deadline: float | None = None,
              timeout: float = DEFAULT_STAGE_TIMEOUT, retries: int = 0,
              retry_on: Callable[[BaseException], bool] | None = None,
              on_error: Callable[[BaseException], dict] | None = None) -> StageResult:
    """Выполнить этап с ограничением времени, повторами и понятной ошибкой.

    Повтор ограничен `retries` (0 или 1). Повторяем только исключения, которые прошли
    `retry_on`; таймаут не повторяется автоматически, потому что он уже съел срок.
    """
    started = time.time()
    limit = stage_timeout(deadline, timeout)
    last_error: BaseException | None = None
    attempts = 0
    for attempt in range(max(0, int(retries)) + 1):
        attempts = attempt + 1
        if remaining(deadline) <= 0:
            err = TimeoutError("общий дедлайн процедуры исчерпан")
            return StageResult(False, None, human_error("deadline", str(err),
                                                        "повторите позже или сократите запрос"),
                               round(time.time() - started, 3), True, attempts)
        left = stage_timeout(deadline, limit)
        ok, value, exc = _run_in_thread(fn, left)
        if ok:
            return StageResult(True, value, None, round(time.time() - started, 3), False, attempts)
        last_error = exc or RuntimeError("этап не дал результата")
        if isinstance(last_error, TimeoutError):
            reason = f"этап «{name}» не уложился в {left:.1f} с"
            return StageResult(False, None, human_error("timeout", reason,
                                                        "повторите запрос или уменьшите объём"),
                               round(time.time() - started, 3), True, attempts)
        if attempt >= int(retries):
            break
        if retry_on is not None and not retry_on(last_error):
            break
    reason = f"этап «{name}» завершился ошибкой: {type(last_error).__name__}: {last_error}"
    err = (on_error(last_error) if on_error else human_error(
        "stage_failed", reason, "повторите запрос; если ошибка повторяется, проверьте настройки"))
    return StageResult(False, None, err, round(time.time() - started, 3), False, attempts)


def safe_call(fn: Callable[[], Any], *, code: str = "unexpected",
              reason: str = "непредвиденная ошибка", action: str = "") -> tuple[Any, dict | None]:
    """Вызов, который не выпускает исключение наружу."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - контракт гарантированного ответа
        return None, human_error(code, f"{reason}: {type(exc).__name__}: {exc}", action)


def deadline_from(cfg: dict, default: float = DEFAULT_TOTAL_DEADLINE) -> float:
    """Общий дедлайн процедуры из настройки; значение всегда положительное."""
    try:
        value = float((cfg or {}).get("request_timeout") or default)
    except (TypeError, ValueError):
        value = default
    return time.time() + max(1.0, value)


def merge_degraded(*values) -> list[str]:
    """Склеить метки деградации без дублей, сохранив порядок."""
    out: list[str] = []
    for value in values:
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return out
