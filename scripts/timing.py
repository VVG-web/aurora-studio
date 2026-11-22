#!/usr/bin/env python3
"""timing.py — единый контракт времени работы и ожидания.

У новых прогонов интервалы берутся из спанов `timing.jsonl`: работа, ожидание слота,
паузы, сеть и ответы сервисов. Старые записи без спанов получают консервативную
сводку из `net.summary.blocked_s` (`legacy_timing`) и явную пометку «оценка».
Модуль не импортирует движок: он собирает сводку, пишет нормализованную строку в
`AuroraKnowledgeDB/meta/timing.d` и форматирует её для истории и отчётов.

Инвариант: work_ms + service_wait_ms + other_wait_ms + unknown_ms == active_ms,
когда работа и ожидание известны. Старые записи без снимков не получают выдуманный
ноль: у них wait_known=false, work_ms=null.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

TIMING_V = 1
SPAN_LIMIT = 5000
KEEP_DAYS = 90
SERVICE_LIMIT_MS = 30_000
HIST_BUCKETS_MS = (0, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000,
                   10000, 30000, 60000, 120000, 300000, 600000, 1800000,
                   float("inf"))
FAIL_STATUSES = {"error", "timeout", "unavailable", "throttled", "auth", "quota"}
OK_STATUSES = {"ok", "partial", ""}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _epoch_ms(value) -> float | None:
    """Миллисекунды эпохи из ISO-строки, секунд или уже миллисекунд."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number * 1000 if abs(number) < 10 ** 12 else number


def _close(a: float, b: float, tol: float = 1.0) -> bool:
    return abs(float(a) - float(b)) <= tol


@dataclass
class Span:
    """Один интервал времени. Поля намеренно плоские: строка JSONL читается без движка."""

    kind: str
    name: str = ""
    run_id: str = ""
    span_id: str = field(default_factory=lambda: secrets.token_hex(4))
    parent_id: str = ""
    lane: str = ""
    service: str = ""
    cause: str = ""
    provider: str = ""
    model: str = ""
    role: str = ""
    operation: str = ""
    wait_class: str = ""
    status: str = "ok"
    reason: str = ""
    blocking: bool = True
    attempt: int = 0
    start: str = field(default_factory=_utc_now)
    end: str = ""
    dur_ms: float | None = None
    mono_start_ns: int = field(default_factory=time.monotonic_ns)
    mono_end_ns: int = 0
    source: str = "live"
    confidence: str = "exact"
    metrics: dict = field(default_factory=dict)
    truncated: bool = False

    def finish(self, status: str | None = None) -> None:
        if self.end:
            return
        self.end = _utc_now()
        self.mono_end_ns = time.monotonic_ns()
        if not self.dur_ms:
            self.dur_ms = round(max(0.0, (self.mono_end_ns - self.mono_start_ns) / 1_000_000), 3)
        if status:
            self.status = status

    def to_dict(self) -> dict:
        row = {
            "v": TIMING_V, "type": "span", "run_id": self.run_id,
            "span_id": self.span_id, "parent_id": self.parent_id, "lane": self.lane,
            "kind": self.kind, "name": self.name, "service": self.service,
            "cause": self.cause, "provider": self.provider, "model": self.model,
            "role": self.role, "operation": self.operation, "wait_class": self.wait_class,
            "start": self.start, "end": self.end, "dur_ms": self.dur_ms,
            "mono_start_ns": self.mono_start_ns, "mono_end_ns": self.mono_end_ns,
            "status": self.status, "reason": self.reason, "blocking": self.blocking,
            "attempt": self.attempt, "source": self.source, "confidence": self.confidence,
            "metrics": dict(self.metrics or {}),
        }
        return {k: v for k, v in row.items() if v not in ("", None, 0, False, {}) or k in (
            "v", "type", "run_id", "span_id", "kind", "service", "start", "end",
            "dur_ms", "status", "blocking", "attempt", "source", "confidence")}


class Tracer:
    """Пишет спаны запуска в `timing.jsonl` и держит их для сводки.

    `run_dir` берётся из `AURORA_RUN_DIR` лениво: тесты и CLI импортируют движок до того,
    как панель выставит переменную. Без папки спаны остаются в памяти и никуда не пишутся.
    """

    def __init__(self, run_dir: str = "", run_id: str = "", max_spans: int = SPAN_LIMIT):
        self.run_dir = str(run_dir or "")
        self.run_id = str(run_id or "")
        self._explicit_dir = bool(run_dir)
        self._explicit_run = bool(run_id)
        self.max_spans = max(1, int(max_spans or SPAN_LIMIT))
        self.spans: list[Span] = []
        self.truncated = False
        self._lock = threading.RLock()
        self._local = threading.local()
        self._path: str = ""

    def _ensure(self) -> None:
        if not self.run_dir:
            self.run_dir = os.environ.get("AURORA_RUN_DIR", "")
        if not self.run_id:
            self.run_id = os.environ.get("AURORA_RUN_ID", "") or (
                os.path.basename(os.path.normpath(self.run_dir)) if self.run_dir else "")
        if self.run_dir and not self._path:
            self._path = os.path.join(self.run_dir, "timing.jsonl")

    def _stack(self) -> list[Span]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    @contextlib.contextmanager
    def span(self, kind: str, name: str = "", **kw) -> Iterator[Span]:
        self._ensure()
        stack = self._stack()
        parent_id = str(kw.pop("parent_id", "") or (stack[-1].span_id if stack else ""))
        span = Span(kind=str(kind), name=str(name), run_id=self.run_id,
                    parent_id=parent_id, **kw)
        stack.append(span)
        try:
            yield span
        except BaseException as exc:  # noqa: BLE001 — спан обязан закрыться и при исключении
            if span.status in OK_STATUSES:
                span.status = "error"
            if not span.reason:
                span.reason = type(exc).__name__
            raise
        finally:
            stack.pop()
            span.finish()
            self.emit(span)

    def emit(self, span: Span) -> None:
        """Дописать спан; ошибка записи не имеет права ронять прогон."""
        with self._lock:
            self.spans.append(span)
            if len(self.spans) > self.max_spans:
                self.spans = self.spans[-self.max_spans:]
                self.truncated = True
            path = self._path
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(span.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
        except OSError:
            pass

    def snapshot(self) -> list[Span]:
        with self._lock:
            return list(self.spans)

    def current(self) -> Span | None:
        stack = self._stack()
        return stack[-1] if stack else None

    def note_span(self, kind: str, start=None, end=None, dur_ms=None, **kw) -> Span:
        """Записать уже закончившийся интервал; `start`/`end` в секундах эпохи."""
        self._ensure()
        parent_id = str(kw.pop("parent_id", "") or (
            self.current().span_id if self.current() else ""))
        span = Span(kind=str(kind), run_id=self.run_id, parent_id=parent_id, **kw)
        span.start = _iso(start) if start is not None else span.start
        span.end = _iso(end) if end is not None else _utc_now()
        if isinstance(dur_ms, (int, float)):
            span.dur_ms = round(max(0.0, float(dur_ms)), 3)
        self.emit(span)
        return span

    def reset(self) -> None:
        with self._lock:
            self.spans.clear()
            self.truncated = False
            if not self._explicit_dir:
                self.run_dir = ""
            if not self._explicit_run:
                self.run_id = ""
            self._path = ""

    def summary(self, active_start=None, active_end=None, queue_start=None,
                slots=None) -> dict:
        summary = summarize(self.snapshot(), active_start=active_start, active_end=active_end,
                            queue_start=queue_start, slots=slots)
        if self.truncated:
            summary["quality"]["truncated"] = True
        return summary


def span(tracer: Tracer, kind: str, name: str = "", **kw):
    """`with timing.span(TIMING, "call", ...) as sp:` — короткая форма для движка."""
    return tracer.span(kind, name, **kw)


def subtree(spans: Iterable, root_id: str) -> list:
    """Спаны дерева с корнем `root_id`; порядок исходный."""
    rows = list(spans)
    ids = {str(root_id or "")}
    out = []
    for value in rows:
        row = _row(value)
        if row.get("span_id") in ids or row.get("parent_id") in ids:
            ids.add(row.get("span_id"))
            out.append(value)
    # Дети могли идти раньше родителя: повторяем, пока дерево не перестанет расти.
    for _ in range(4):
        more = [v for v in rows if v not in out and _row(v).get("parent_id") in ids]
        if not more:
            break
        for value in more:
            ids.add(_row(value).get("span_id"))
            out.append(value)
    return out


def _row(value) -> dict:
    if isinstance(value, Span):
        return value.to_dict()
    return dict(value or {})


def _bounds(row: dict) -> tuple[float, float] | None:
    start = _epoch_ms(row.get("start"))
    if start is None:
        return None
    duration = row.get("dur_ms")
    if isinstance(duration, (int, float)) and duration > 0:
        return start, start + float(duration)
    end = _epoch_ms(row.get("end"))
    if end is None and isinstance(row.get("dur_ms"), (int, float)):
        end = start + float(row["dur_ms"])
    if end is None or end < start:
        return None
    return start, end


def _clip(intervals: Iterable[tuple[float, float]], lo: float, hi: float) -> list[tuple[float, float]]:
    return [(max(lo, a), min(hi, b)) for a, b in intervals if min(hi, b) > max(lo, a)]


def _group_ms(intervals: Iterable[tuple[float, float]], rows: Iterable[dict],
              key: str, cut: Iterable[tuple[float, float]] = ()) -> dict:
    grouped: dict[str, list[tuple[float, float]]] = {}
    cuts = _merge(cut)
    for (a, b), row in zip(intervals, rows):
        name = str(row.get(key) or "unknown")
        grouped.setdefault(name, []).append((a, b))
    return {name: round(union_ms(subtract_ms(parts, cuts)), 1)
            for name, parts in grouped.items()}


def _concurrency(intervals: list[tuple[float, float]], lo: float, hi: float) -> tuple[float, int, float]:
    """(среднее число одновременных интервалов, максимум, сумма длин)."""
    if not intervals:
        return 0.0, 0, 0.0
    events = []
    for a, b in intervals:
        events.append((a, 1))
        events.append((b, -1))
    events.sort(key=lambda x: (x[0], -x[1]))
    active = 0
    peak = 0
    last = lo
    area = 0.0
    for at, delta in events:
        at = min(hi, max(lo, at))
        area += active * max(0.0, at - last)
        active += delta
        peak = max(peak, active)
        last = at
    width = max(0.0, hi - lo)
    return (area / width if width else 0.0), peak, sum(max(0.0, b - a) for a, b in intervals)


def _hist_key(bound) -> str:
    return "inf" if bound == float("inf") else str(int(bound))


def _hist_new() -> dict:
    return {_hist_key(bound): 0 for bound in HIST_BUCKETS_MS}


def hist_add(hist: dict, value_ms) -> None:
    """Прибавить одно наблюдение в первый бакет, чья верхняя граница не меньше значения."""
    if not isinstance(hist, dict) or not isinstance(value_ms, (int, float)):
        return
    value = float(value_ms)
    for bound in HIST_BUCKETS_MS:
        if value <= bound:
            key = _hist_key(bound)
            hist[key] = int(hist.get(key) or 0) + 1
            return


def hist_merge(dst: dict, src: dict) -> None:
    """Сложить счётчики гистограмм по ключам; отсутствующий ключ — ноль."""
    if not isinstance(dst, dict) or not isinstance(src, dict):
        return
    for key, count in src.items():
        if not isinstance(count, (int, float)):
            continue
        dst[key] = int(dst.get(key) or 0) + int(count)


def hist_stats(hist: dict) -> dict:
    """n и квантили из гистограммы; квантили — границы бакетов, поэтому approx=true.

    `avg` и `max` здесь не считаются: гистограмма не восстанавливает их точно.
    При n < 20 квантили не показываются (контракт, раздел 5).
    """
    counts: dict = {}
    total = 0
    for bound in HIST_BUCKETS_MS:
        key = _hist_key(bound)
        value = hist.get(key, 0) if isinstance(hist, dict) else 0
        count = int(value) if isinstance(value, (int, float)) else 0
        counts[bound] = max(0, count)
        total += max(0, count)

    def pick(q: float):
        if total < 20:
            return None
        target = max(1, int(math.ceil(q * total)))
        seen = 0
        for bound in HIST_BUCKETS_MS:
            seen += counts[bound]
            if seen >= target:
                return None if bound == float("inf") else float(bound)
        return None

    return {"n": total, "p50": pick(0.5), "p95": pick(0.95), "approx": True}


def _stat(values: Iterable[float], approx: bool = False) -> dict:
    rows = sorted(float(v) for v in values if isinstance(v, (int, float)))
    n = len(rows)
    if not n:
        return {"n": 0, "avg": None, "p50": None, "p95": None, "max": None,
                "approx": bool(approx), "hist": {}}
    def pick(q: float):
        if n < 20:
            return None
        return round(rows[min(n - 1, max(0, int(round(q * (n - 1)))))], 1)
    hist = _hist_new()
    for value in rows:
        hist_add(hist, value)
    return {"n": n, "avg": round(sum(rows) / n, 1), "p50": pick(0.5),
            "p95": pick(0.95), "max": round(rows[-1], 1), "approx": bool(approx),
            "hist": hist}


def _metrics(row: dict, key: str):
    metrics = row.get("metrics") or {}
    value = metrics.get(key)
    return value if isinstance(value, (int, float)) else None


def _int_metric(row: dict, key: str):
    value = _metrics(row, key)
    return int(value) if value is not None else None


def _unique_rows(spans: Iterable) -> list:
    """Спаны без повторов по `span_id`; строка без id остаётся как есть."""
    rows = []
    seen = set()
    for value in spans:
        row = _row(value)
        span_id = str(row.get("span_id") or "")
        if span_id:
            if span_id in seen:
                continue
            seen.add(span_id)
        rows.append(row)
    return rows


def _blank_service() -> dict:
    return {
        "calls": 0, "attempts": 0, "ok": 0, "errors": {},
        "latency_values": [], "ttft_values": [], "slot_values": [], "backoff_values": [],
        "ok_latency_values": [], "tokens_in": 0, "tokens_out": 0, "tps_basis": "",
        "bytes": 0, "bytes_bases": set(), "bytes_total": 0, "pages": 0, "issues": 0,
        "http_4xx": 0, "http_5xx": 0, "timeouts": 0, "retries": 0,
        "auth_errors": 0, "content_errors": 0, "rate_limited": 0,
        "probe_n": 0, "probe_ok": 0,
    }


def _bump_status(rec: dict, row: dict) -> None:
    """Один физический исход — ровно один счётчик; разряды не пересекаются."""
    metrics = row.get("metrics") or {}
    status = str(row.get("status") or "")
    reason = str(row.get("reason") or "")
    try:
        http_status = int(metrics.get("http_status") or 0)
    except (TypeError, ValueError):
        http_status = 0
    if status == "auth" or http_status in (401, 403) or reason in (
            "http_401", "http_403", "auth"):
        rec["auth_errors"] += 1
    elif status == "throttled" or http_status == 429 or reason == "http_429":
        rec["rate_limited"] += 1
    elif 500 <= http_status <= 599 or reason.startswith("http_5"):
        rec["http_5xx"] += 1
    elif 400 <= http_status <= 499 or reason.startswith("http_4"):
        rec["http_4xx"] += 1
    elif status == "timeout" or reason == "timeout":
        rec["timeouts"] += 1
    elif reason in ("content", "body_too_large", "too_large"):
        rec["content_errors"] += 1


def _add_bytes(rec: dict, row: dict) -> None:
    metrics = row.get("metrics") or {}
    size = metrics.get("bytes")
    if isinstance(size, (int, float)):
        rec["bytes"] += int(size)
    basis = metrics.get("bytes_basis")
    if isinstance(basis, str) and basis:
        rec["bytes_bases"].add(basis)


def _wait_value(row: dict, key: str, reason: str):
    value = _metrics(row, key)
    if value is not None:
        return value
    if str(row.get("reason") or "") == reason:
        duration = row.get("dur_ms")
        if isinstance(duration, (int, float)):
            return float(duration)
    return None


def service_metrics(spans: Iterable) -> dict:
    """Сводка по сервисам для `timing.d` и блока качества итога.

    Разряды не складываются дважды: `calls`, `latency_ms`, `ttft_ms` и токены берутся
    с листовых `call`; `attempts`, `retries` и счётчики HTTP — с `attempt`, а вызов без
    попыток считается одной попыткой сам. `bytes` — только с листа, владеющего телом.
    `pages`, `issues` и `bytes_total` — с фазы, и в листовые счётчики не попадают.
    """
    rows = _unique_rows(spans)
    call_rows = [row for row in rows if str(row.get("kind") or "") == "call"]
    call_by_id = {str(row.get("span_id") or ""): row for row in call_rows
                  if str(row.get("span_id") or "")}
    attempts_by_parent: dict = {}
    for row in rows:
        if str(row.get("kind") or "") != "attempt":
            continue
        parent_id = str(row.get("parent_id") or "")
        attempts_by_parent.setdefault(parent_id, []).append(row)

    # Верхний вызов того же сервиса — итог, а не второй вызов. Вызов другого сервиса
    # остаётся листом своего сервиса (контракт, раздел 4.2).
    nested_same_service = set()
    for row in call_rows:
        parent_id = str(row.get("parent_id") or "")
        parent = call_by_id.get(parent_id)
        if parent is not None and str(parent.get("service") or "") == str(row.get("service") or ""):
            nested_same_service.add(parent_id)
    leaf_calls = [row for row in call_rows
                  if str(row.get("span_id") or "") not in nested_same_service]
    leaf_ids = {str(row.get("span_id") or "") for row in leaf_calls}

    out: dict = {}
    for row in rows:
        service = str(row.get("service") or "")
        if service:
            out.setdefault(service, _blank_service())

    # Физические попытки: каждая строка `attempt` — одна попытка и один исход.
    for row in rows:
        if str(row.get("kind") or "") != "attempt":
            continue
        service = str(row.get("service") or "")
        if not service:
            continue
        rec = out.setdefault(service, _blank_service())
        rec["attempts"] += 1
        _bump_status(rec, row)
        _add_bytes(rec, row)

    # Вызовы: листья дают calls/ok/errors/latency/ttft/токены. Вызов без своих попыток
    # считается одной физической попыткой; число попыток из `metrics.attempts` — тоже.
    for row in call_rows:
        service = str(row.get("service") or "")
        if not service:
            continue
        rec = out.setdefault(service, _blank_service())
        span_id = str(row.get("span_id") or "")
        children = attempts_by_parent.get(span_id, []) if span_id else []
        if span_id not in leaf_ids:
            continue
        retries = _int_metric(row, "retries")
        if retries is None:
            if children:
                retries = max(0, len(children) - 1)
            else:
                attempts_field = _int_metric(row, "attempts")
                retries = max(0, attempts_field - 1) if attempts_field is not None else 0
        rec["retries"] += max(0, retries)
        rec["calls"] += 1
        if row.get("status") in OK_STATUSES:
            rec["ok"] += 1
        else:
            reason = str(row.get("reason") or row.get("status") or "error")
            rec["errors"][reason] = rec["errors"].get(reason, 0) + 1
        if not children:
            attempts_field = _int_metric(row, "attempts")
            rec["attempts"] += attempts_field if attempts_field and attempts_field > 0 else 1
            _bump_status(rec, row)
            _add_bytes(rec, row)
        latency = _metrics(row, "latency_ms")
        if latency is not None:
            rec["latency_values"].append(latency)
            if row.get("status") in OK_STATUSES:
                rec["ok_latency_values"].append(latency)
        ttft = _metrics(row, "ttft_ms")
        if ttft is not None:
            rec["ttft_values"].append(ttft)
        for key in ("tokens_in", "tokens_out"):
            value = _metrics(row, key)
            if isinstance(value, (int, float)):
                rec[key] += int(value)
        basis = (row.get("metrics") or {}).get("tps_basis")
        if isinstance(basis, str) and basis:
            if basis == "stream" or not rec["tps_basis"]:
                rec["tps_basis"] = basis

    # Попытка без вызова (веб-выгрузка): её номер уже говорит, был ли это повтор.
    for row in rows:
        if str(row.get("kind") or "") != "attempt":
            continue
        service = str(row.get("service") or "")
        if not service:
            continue
        parent = call_by_id.get(str(row.get("parent_id") or ""))
        if parent is not None and _int_metric(parent, "retries") is not None:
            continue
        parent_id = str(row.get("parent_id") or "")
        attempt_number = row.get("attempt")
        numbered = (attempt_number if isinstance(attempt_number, (int, float))
                    and not isinstance(attempt_number, bool) else 0)
        if (parent is None or parent_id not in leaf_ids) and numbered > 1:
            out[service]["retries"] += 1
        basis = (row.get("metrics") or {}).get("tps_basis")
        if isinstance(basis, str) and basis:
            if basis == "stream" or not out[service]["tps_basis"]:
                out[service]["tps_basis"] = basis

    for row in rows:
        service = str(row.get("service") or "")
        kind = str(row.get("kind") or "")
        if not service:
            continue
        rec = out.setdefault(service, _blank_service())
        if kind == "wait":
            slot = _wait_value(row, "slot_wait_ms", "slot")
            if slot is not None:
                rec["slot_values"].append(slot)
            backoff = _wait_value(row, "backoff_ms", "backoff")
            if backoff is not None:
                rec["backoff_values"].append(backoff)
        elif kind == "phase":
            metrics = row.get("metrics") or {}
            for key, target in (("bytes_total", "bytes_total"), ("pages", "pages"),
                                ("issues", "issues")):
                value = metrics.get(key)
                if isinstance(value, (int, float)):
                    rec[target] += int(value)

    # Ожидание слота и backoff обычно несут `wait`-спаны. Старый прогон без них
    # оставил итог в `call.metrics`; берём его только когда wait-спанов нет вовсе.
    for service, rec in out.items():
        if not rec["slot_values"]:
            rec["slot_values"] = [_metrics(row, "slot_wait_ms") for row in leaf_calls
                                  if str(row.get("service") or "") == service
                                  and _metrics(row, "slot_wait_ms") is not None]
        if not rec["backoff_values"]:
            rec["backoff_values"] = [_metrics(row, "backoff_ms") for row in leaf_calls
                                     if str(row.get("service") or "") == service
                                     and _metrics(row, "backoff_ms") is not None]

    for service, rec in out.items():
        # Сервис без единого `call` (веб-выгрузка шлёт только попытки) меряется
        # по попыткам: иначе задержка не показывается вовсе.
        has_calls = any(str(row.get("service") or "") == service for row in call_rows)
        if not rec["latency_values"] and not has_calls:
            rec["latency_values"] = [
                value for row in rows
                if str(row.get("service") or "") == service
                and str(row.get("kind") or "") == "attempt"
                for value in [_metrics(row, "latency_ms")] if value is not None]
        if service == "network":
            for row in call_rows:
                if str(row.get("service") or "") != "network":
                    continue
                if str(row.get("name") or "") != "probe" and str(
                        row.get("operation") or "") != "probe":
                    continue
                rec["probe_n"] += 1
                if row.get("status") in OK_STATUSES:
                    rec["probe_ok"] += 1
        ok_latencies = rec.pop("ok_latency_values")
        rec["latency_ms"] = _stat(rec.pop("latency_values"))
        rec["ttft_ms"] = _stat(rec.pop("ttft_values"))
        rec["slot_wait_ms"] = _stat(rec.pop("slot_values"))
        rec["backoff_ms"] = _stat(rec.pop("backoff_values"))
        bases = rec.pop("bytes_bases")
        if "mixed" in bases or len(bases) > 1:
            rec["bytes_basis"] = "mixed"
        elif bases:
            rec["bytes_basis"] = next(iter(bases))
        else:
            rec["bytes_basis"] = "response"
        rec["tps_basis"] = rec["tps_basis"] or "request"
        ok_avg = _stat(ok_latencies)["avg"]
        rec["tps"] = (round(rec["tokens_out"] / (ok_avg / 1000), 1)
                      if rec["tokens_out"] and ok_avg else None)
        # Медленный отказ — это ожидание сервиса, а не медленная работа. В `slow_calls`
        # попадает только успешный вызов, который всё равно занял больше предела.
        rec["slow_calls"] = sum(1 for x in ok_latencies if x >= SERVICE_LIMIT_MS)
        rec["service_calls"] = rec["calls"]
        rec["service_calls_ok"] = rec["ok"]
        probe_n = rec.pop("probe_n")
        probe_ok = rec.pop("probe_ok")
        rec["probe_ok_rate"] = {"n": probe_n, "ok": probe_ok,
                                "rate": round(probe_ok / probe_n, 4) if probe_n else None,
                                "approx": False}
    return out


def summarize(spans: Iterable, active_start=None, active_end=None, queue_start=None,
              slots=None) -> dict:
    """Собрать сводку v1 из спанов. Активное окно задаёт вызывающий, иначе границы спанов."""
    rows = [_row(s) for s in spans]
    starts = [b[0] for b in (_bounds(r) for r in rows) if b]
    ends = [b[1] for b in (_bounds(r) for r in rows) if b]
    lo = _epoch_ms(active_start)
    hi = _epoch_ms(active_end)
    if lo is None and starts:
        lo = min(starts)
    if hi is None and ends:
        hi = max(ends)
    if starts:
        lo = min(lo, min(starts)) if lo is not None else min(starts)
    if ends:
        hi = max(hi, max(ends)) if hi is not None else max(ends)
    if lo is None or hi is None or hi < lo:
        return {
            "v": TIMING_V, "scope": "run", "active_ms": None, "total_ms": None,
            "queue_ms": 0, "work_ms": None, "service_wait_ms": None,
            "other_wait_ms": 0, "unknown_ms": None, "coverage": 0.0,
            "source": "spans", "confidence": "unknown",
            "quality": {"wait_known": False, "approx": False, "legacy": False,
                        "queue_separated": False},
        }
    queue_lo = _epoch_ms(queue_start)
    queue = max(0.0, lo - queue_lo) if queue_lo is not None else 0.0
    for row, bounds in zip(rows, (_bounds(r) for r in rows)):
        if row.get("kind") == "queue" and bounds:
            queue = max(queue, bounds[1] - bounds[0])
    active = max(0.0, hi - lo)
    work: list[tuple[float, float]] = []
    service: list[tuple[float, float]] = []
    other: list[tuple[float, float]] = []
    service_rows: list[dict] = []
    other_rows: list[dict] = []
    all_bounds: list[tuple[float, float]] = []
    for row, bounds in zip(rows, (_bounds(r) for r in rows)):
        if not bounds:
            continue
        bounds = _clip([bounds], lo, hi)
        if not bounds:
            continue
        all_bounds.extend(bounds)
        kind = str(row.get("kind") or "")
        status = str(row.get("status") or "")
        wait_class = str(row.get("wait_class") or "")
        if wait_class == "other":
            wait_class = "resource"      # старый псевдоним: писатели его больше не шлют
        if kind == "wait":
            if wait_class == "service":
                service.extend(bounds)
                service_rows.extend([row] * len(bounds))
            elif wait_class:
                other.extend(bounds)
                other_rows.extend([{**row, "wait_class": wait_class}] * len(bounds))
        elif kind in ("call", "attempt", "phase", "tool", "run"):
            if status in FAIL_STATUSES:
                service.extend(bounds)
                service_rows.extend([row] * len(bounds))
            else:
                work.extend(bounds)
    service_u = subtract_ms(_merge(service), _merge(other))
    other_u = _merge(other)
    work_u = subtract_ms(_merge(work), service_u + other_u)
    service_ms = union_ms(service_u)
    other_ms = union_ms(other_u)
    work_ms = union_ms(work_u)
    unknown = max(0.0, active - (work_ms + service_ms + other_ms))
    overlap = work_ms + service_ms + other_ms > active + 1
    if overlap:
        unknown = 0.0
    by_service = _group_ms(service, service_rows, "service", other_u)
    by_cause = _group_ms(service, service_rows, "cause", other_u)
    # Классы ожидания могут пересекаться (пауза темпа и ожидание ресурса идут
    # одновременно). Объединение `other_u` теряло один из них; для разбивки берём
    # собственные отрезки каждого класса.
    other_by_kind = _group_ms(other, other_rows, "wait_class")
    call_rows = [r for r in rows if r.get("kind") == "call"]
    attempt_bounds = [b for r in rows if r.get("kind") == "attempt"
                      for b in _clip([_bounds(r)], lo, hi) if b]
    avg_busy, max_busy, lane_ms = _concurrency(attempt_bounds, lo, hi)
    slot_ms = union_ms([_bounds(r) for r in rows
                        if r.get("kind") == "wait" and r.get("reason") == "slot"
                        and _bounds(r)])
    backoff_ms = union_ms([_bounds(r) for r in rows
                           if r.get("kind") == "wait" and r.get("reason") == "backoff"
                           and _bounds(r)])
    net_ms = sum(v for k, v in by_service.items() if k == "network")
    slow_calls = sum(1 for r in call_rows
                     if r.get("status") in OK_STATUSES
                     and (_metrics(r, "latency_ms") or 0) >= SERVICE_LIMIT_MS)
    approx = any(str(r.get("confidence") or "exact") != "exact" for r in rows)
    confidence = "approx" if approx else "exact"
    lanes = sorted({str(r.get("lane")) for r in rows if r.get("lane")})
    summary = {
        "v": TIMING_V, "scope": "run", "queue_ms": round(queue, 1),
        "active_ms": round(active, 1), "total_ms": round(queue + active, 1),
        "work_ms": round(work_ms, 1), "service_wait_ms": round(service_ms, 1),
        "service_delay_ms": round(sum((_bounds(r)[1] - _bounds(r)[0])
                                      for r in rows if r.get("kind") == "attempt"
                                      and r.get("status") in FAIL_STATUSES
                                      and _bounds(r)) + backoff_ms, 1),
        "other_wait_ms": round(other_ms, 1), "unknown_ms": round(unknown, 1),
        "coverage": 1.0 if active else 0.0, "source": "spans", "confidence": confidence,
        "wait_by_service": {k: round(v, 1) for k, v in by_service.items()},
        "wait_by_cause": {k: round(v, 1) for k, v in by_cause.items()},
        "other_by_kind": {k: round(v, 1) for k, v in other_by_kind.items()},
        # `service_calls` — все логические вызовы, включая неудачные; успешные
        # считаются отдельно (`service_calls_ok`), иначе поле врёт о работе сервиса.
        "service_calls": len(call_rows),
        "service_calls_ok": sum(1 for r in call_rows if r.get("status") in OK_STATUSES),
        "slot_wait_ms": round(slot_ms, 1),
        "backoff_ms": round(backoff_ms, 1),
        "net_wait_ms": round(net_ms, 1),
        "slow_calls": slow_calls,
        "parallel": {"lanes": len(lanes), "max_lanes": max_busy,
                     "work_lane_ms": round(lane_ms, 1),
                     "parallelism": round(lane_ms / active, 2) if active else 0.0,
                     "busy_avg": round(avg_busy, 2),
                     "slots_available_avg": (round(max(0.0, float(slots) - avg_busy), 2)
                                              if isinstance(slots, (int, float)) else None)},
        "quality": {"wait_known": True, "approx": approx, "legacy": False,
                    "queue_separated": queue_lo is not None,
                    "slow_calls": slow_calls,
                    **({"error": "overlap"} if overlap else {})},
    }
    if isinstance(slots, (int, float)):
        summary["parallel"]["slots"] = int(slots)
    return summary


def _safe_name(text, fallback: str = "unknown") -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "")).strip("-")[:80]
    return name or fallback


def _machine_id() -> str:
    raw = os.environ.get("AURORA_MACHINE") or socket.gethostname() or "machine"
    return hashlib.sha256(_safe_name(raw, "machine").encode("utf-8")).hexdigest()[:8]


def record(project: str, run_id: str, timing: dict, services: dict | None = None,
           kind: str = "agent", task: str = "", actor_id: str = "",
           machine: str = "", at: str = "", *, record_id: str = "") -> str:
    """Записать одну нормализованную строку в `timing.d`; вернуть путь или пустую строку."""
    root = Path(project).expanduser().resolve()
    if not (root / "AuroraKnowledgeDB").is_dir() or not timing:
        return ""
    run_id = _safe_name(run_id)
    if not run_id:
        return ""
    record_id = _safe_name(record_id, "") if record_id else ""
    if not actor_id:
        try:
            from aurora_actor import actor
            actor_id, _display = actor(str(root))
        except Exception:  # noqa: BLE001 — замер не должен ронять прогон
            actor_id = "unknown"
    part = f"{_safe_name(actor_id)}-{_safe_name(machine or _machine_id())}"
    folder = root.joinpath("AuroraKnowledgeDB", "meta", "timing.d", part)
    name = f"{run_id}-{record_id}" if record_id else run_id
    payload = {
        "schema": 1, "run": run_id, "record_id": record_id, "at": at or _utc_now(),
        "project": root.name, "kind": str(kind or "agent"), "task": str(task or ""),
        "timing": timing, "services": services or {},
    }
    try:
        from aurora_fs import write_json
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        write_json(str(path), payload, indent=1)
        prune(str(root))
        return str(path)
    except Exception:  # noqa: BLE001 — замер не имеет права ронять прогон
        return ""


def read_spans(run_dir: str) -> list[dict]:
    """Прочитать `timing.jsonl` целиком, без предела памяти `Tracer`.

    Битая строка пропускается: один недописанный спан не должен терять весь прогон.
    `run_dir` может быть и папкой прогона, и самим файлом.
    """
    if not run_dir:
        return []
    path = Path(str(run_dir)).expanduser()
    if path.is_dir():
        path = path / "timing.jsonl"
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def record_run(project: str, run_id: str, run_dir: str, kind: str = "run", task: str = "",
               record_id: str = "", actor_id: str = "", machine: str = "",
               at: str = "") -> str:
    """Запись `timing.d` владельцем прогона: полный `timing.jsonl`, а не снимок памяти."""
    root = Path(str(project)).expanduser().resolve()
    if not (root / "AuroraKnowledgeDB").is_dir():
        return ""
    run_id = str(run_id or "")
    if not run_id:
        return ""
    rows = [row for row in read_spans(run_dir)
            if str(row.get("run_id") or "") == run_id]
    if not rows:
        return ""
    summary = summarize(rows)
    summary.pop("services", None)
    services = service_metrics(rows)
    return record(str(root), run_id, summary, services, kind=kind, task=task,
                  actor_id=actor_id, machine=machine, at=at, record_id=record_id)


def _project_roots(projects) -> list:
    if isinstance(projects, (str, os.PathLike)):
        projects = [projects]
    roots = []
    for project in projects or []:
        path = project.get("path") if isinstance(project, dict) else project
        if not path:
            continue
        root = Path(str(path)).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    return roots


def _record_int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _normalize_record(data: dict, root: Path) -> dict:
    """Строка `timing.d` в форме раздела 6 контракта; `services` внутри `timing` не читается."""
    record_data = dict(data)
    timing = record_data.get("timing")
    timing = dict(timing) if isinstance(timing, dict) else {}
    timing.pop("services", None)
    services = record_data.get("services")
    record_data["schema"] = _record_int(record_data.get("schema"), 1) or 1
    record_data["run"] = str(record_data.get("run") or "")
    record_data["record_id"] = str(record_data.get("record_id") or "")
    record_data["at"] = str(record_data.get("at") or "")
    record_data["project"] = str(record_data.get("project") or root.name)
    record_data["kind"] = str(record_data.get("kind") or "")
    record_data["task"] = str(record_data.get("task") or "")
    record_data["timing"] = timing
    record_data["services"] = services if isinstance(services, dict) else {}
    return record_data


def _record_ts_ms(data: dict, path: Path) -> float:
    stamp = _epoch_ms(data.get("at"))
    if stamp is not None:
        return stamp
    try:
        return path.stat().st_mtime * 1000
    except OSError:
        return 0.0


def iter_records(projects, since: float = 0.0, limit: int = 200) -> list[dict]:
    """Записи `timing.d` по проектам: свежие сверху, без повторов ключа.

    Ключ повтора — `(проект, run, record_id)`, где проект — его корень; побеждает
    запись с новым `at`, при равенстве — лексикографически больший путь. Повторы
    не суммируются.
    """
    since_ms = max(0.0, float(since or 0.0)) * 1000
    found = []
    for root in _project_roots(projects):
        folder = root.joinpath("AuroraKnowledgeDB", "meta", "timing.d")
        try:
            parts = sorted(path for path in folder.iterdir() if path.is_dir())
        except OSError:
            continue
        for part in parts:
            try:
                files = sorted(part.glob("*.json"))
            except OSError:
                continue
            for path in files:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                record_data = _normalize_record(data, root)
                stamp = _record_ts_ms(record_data, path)
                if since_ms and stamp < since_ms:
                    continue
                found.append((stamp, str(path), root, record_data))
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    out = []
    seen = set()
    for _stamp, _path, root, record_data in found:
        key = (str(root),
               str(record_data.get("run") or ""),
               str(record_data.get("record_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(record_data)
        if limit and len(out) >= int(limit):
            break
    return out


_WINDOW_STAT_KEYS = ("latency_ms", "ttft_ms", "slot_wait_ms", "backoff_ms")
_WINDOW_SUM_KEYS = ("calls", "service_calls_ok", "attempts", "ok", "tokens_in",
                    "tokens_out", "slow_calls", "bytes", "bytes_total", "pages",
                    "issues", "http_4xx", "http_5xx", "timeouts", "retries",
                    "auth_errors", "content_errors", "rate_limited")


def _blank_stat_acc() -> dict:
    return {"n": 0, "sum": 0.0, "max": None, "hist": {}, "hist_ok": True,
            "approx": False, "weights": True}


def _merge_stat_acc(acc: dict, stat) -> None:
    if not isinstance(stat, dict):
        return
    n = _record_int(stat.get("n"), 0)
    if n <= 0:
        return
    acc["n"] += n
    average = stat.get("avg")
    if isinstance(average, (int, float)) and not isinstance(average, bool):
        acc["sum"] += float(average) * n
    else:
        acc["weights"] = False
    maximum = stat.get("max")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        acc["max"] = float(maximum) if acc["max"] is None else max(acc["max"], float(maximum))
    hist = stat.get("hist")
    if isinstance(hist, dict) and hist:
        hist_merge(acc["hist"], hist)
    else:
        acc["hist_ok"] = False
    if stat.get("approx"):
        acc["approx"] = True


def _final_stat(acc: dict) -> dict:
    n = acc["n"]
    if n <= 0:
        return {"n": 0, "avg": None, "p50": None, "p95": None, "max": None,
                "approx": bool(acc["approx"]), "hist": {}}
    average = round(acc["sum"] / n, 1) if acc["weights"] else None
    p50 = p95 = None
    reported = False
    if n >= 20 and acc["hist_ok"]:
        merged = hist_stats(acc["hist"])
        p50, p95 = merged["p50"], merged["p95"]
        reported = p50 is not None or p95 is not None
    approx = bool(reported or (n >= 20 and not acc["hist_ok"]) or acc["approx"])
    return {"n": n, "avg": average, "p50": p50, "p95": p95, "max": acc["max"],
            "approx": approx, "hist": dict(acc["hist"])}


def _blank_window_service() -> dict:
    acc = {key: 0 for key in _WINDOW_SUM_KEYS}
    acc["errors"] = {}
    acc["stats"] = {key: _blank_stat_acc() for key in _WINDOW_STAT_KEYS}
    acc["bases"] = set()
    acc["tps_bases"] = set()
    acc["probe_n"] = 0
    acc["probe_ok"] = 0
    acc["probe_approx"] = False
    return acc


def window_metrics(projects, days: int = 7, limit: int = 200) -> dict:
    """Сервисные метрики за окно: прогоны, суммы и гистограммы из `timing.d`.

    `avg` взвешен по `n`, `max` — максимум записей, квантили — по объединённой
    гистограмме и только когда `hist` есть у каждой записи и `n >= 20`. Иначе
    квантили `null`, а `quality.approx` честно говорит, почему.
    """
    days = max(0, int(days or 0))
    since_epoch = (time.time() - days * 86400) if days else 0.0
    records = iter_records(projects, since=since_epoch, limit=limit)
    runs = []
    accumulators: dict = {}
    truncated = False
    for record_data in records:
        timing = record_data.get("timing") or {}
        quality = timing.get("quality") or {}
        if quality.get("truncated"):
            truncated = True
        runs.append({
            "run": str(record_data.get("run") or ""),
            "record_id": str(record_data.get("record_id") or ""),
            "project": str(record_data.get("project") or ""),
            "kind": str(record_data.get("kind") or ""),
            "at": str(record_data.get("at") or ""),
            "active_ms": timing.get("active_ms"),
            "work_ms": timing.get("work_ms"),
            "service_wait_ms": timing.get("service_wait_ms"),
            "other_wait_ms": timing.get("other_wait_ms", 0),
            "queue_ms": timing.get("queue_ms", 0),
            "unknown_ms": timing.get("unknown_ms"),
            "coverage": timing.get("coverage", 0.0),
            "source": str(timing.get("source") or ""),
            "confidence": str(timing.get("confidence") or ""),
        })
        services = record_data.get("services") or {}
        if not isinstance(services, dict):
            continue
        for name, service in services.items():
            if not isinstance(service, dict):
                continue
            acc = accumulators.setdefault(str(name), _blank_window_service())
            for key in _WINDOW_SUM_KEYS:
                value = service.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    acc[key] += int(value)
            errors = service.get("errors")
            if isinstance(errors, dict):
                for reason, count in errors.items():
                    if isinstance(count, (int, float)) and not isinstance(count, bool):
                        acc["errors"][str(reason)] = (
                            acc["errors"].get(str(reason), 0) + int(count))
            for key in _WINDOW_STAT_KEYS:
                _merge_stat_acc(acc["stats"][key], service.get(key))
            basis = service.get("bytes_basis")
            if isinstance(basis, str) and basis:
                acc["bases"].add(basis)
            tps_basis = service.get("tps_basis")
            if isinstance(tps_basis, str) and tps_basis:
                acc["tps_bases"].add(tps_basis)
            probe = service.get("probe_ok_rate")
            if isinstance(probe, dict):
                acc["probe_n"] += max(0, _record_int(probe.get("n"), 0))
                acc["probe_ok"] += max(0, _record_int(probe.get("ok"), 0))
                if probe.get("approx"):
                    acc["probe_approx"] = True
    services = {}
    approx = False
    for name, acc in accumulators.items():
        merged = {key: int(acc[key]) for key in _WINDOW_SUM_KEYS}
        merged["errors"] = dict(acc["errors"])
        for key in _WINDOW_STAT_KEYS:
            merged[key] = _final_stat(acc["stats"][key])
            approx = approx or bool(merged[key]["approx"])
        bases = acc["bases"]
        if "mixed" in bases or len(bases) > 1:
            merged["bytes_basis"] = "mixed"
        elif bases:
            merged["bytes_basis"] = next(iter(bases))
        else:
            merged["bytes_basis"] = "response"
        merged["tps_basis"] = (
            "stream" if "stream" in acc["tps_bases"]
            else "request" if "request" in acc["tps_bases"] else "request")
        merged["tps"] = None
        merged["service_calls"] = merged["calls"]
        probe_n, probe_ok = acc["probe_n"], acc["probe_ok"]
        merged["probe_ok_rate"] = {
            "n": probe_n, "ok": probe_ok,
            "rate": round(probe_ok / probe_n, 4) if probe_n else None,
            "approx": bool(acc["probe_approx"]),
        }
        services[name] = merged
    since_text = ""
    if days:
        since_text = datetime.fromtimestamp(since_epoch, timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    return {
        "window": {"days": days, "since": since_text, "records": len(records)},
        "runs": runs,
        "services": services,
        "quality": {"approx": approx, "records": len(records),
                    "truncated": truncated,
                    "services": ["timing.d"]},
    }


def _fresh_by_at(path: Path, cutoff: float) -> bool:
    """Свежий `at` важнее старого времени файла: копия записи не удаляется."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    stamp = _epoch_ms(data.get("at"))
    return stamp is not None and stamp / 1000.0 >= cutoff


def prune(project: str, days: int = KEEP_DAYS) -> int:
    """Удалить строки `timing.d` старше срока; запись со свежим `at` остаётся."""
    root = Path(project).expanduser().resolve()
    folder = root.joinpath("AuroraKnowledgeDB", "meta", "timing.d")
    cutoff = time.time() - max(1, int(days)) * 86400
    gone = 0
    try:
        for path in folder.glob("*/*.json"):
            try:
                if path.stat().st_mtime >= cutoff or _fresh_by_at(path, cutoff):
                    continue
                path.unlink()
                gone += 1
            except OSError:
                continue
    except OSError:
        pass
    return gone


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


def legacy_timing(seconds, net_summary: dict | None, queue_ms: int | float | None = None,
                  service_ms: int | float | None = None,
                  other_ms: int | float | None = None) -> dict:
    """Сводка для записей без спанов: только существующий `blocked_s`.

    `seconds` и `blocked_s` не меняются. Без снимков `work_ms` и `service_wait_ms`
    остаются None: это честное «нет данных», а не ноль секунд простоя. Если
    `blocked_s` больше длительности, ожидание обрезается до неё и `clamped=true`.
    `queue_ms` отделяет очередь запуска, а `service_ms` — точное ожидание сервиса
    (например, из `wait_restored`); `other_ms` — измеренное ожидание, не связанное
    с внешним сервисом (занятость, пауза человека, backoff). С ними сводка
    перестаёт быть оценкой. Если `service_ms` или `other_ms` больше активного
    времени, значение обрезается, а `confidence` и `quality.clamped` говорят об
    этом честно, а не выдают обрезанное за точное.
    Без самого `seconds` неизвестна и длительность: `active_ms` и `total_ms` остаются
    None, чтобы отсутствие данных не превратилось в известный ноль.
    """
    if seconds is None or seconds == "":
        return {
            "v": TIMING_V, "scope": "run",
            "active_ms": None, "total_ms": None, "queue_ms": 0,
            "work_ms": None, "service_wait_ms": None,
            "other_wait_ms": 0, "unknown_ms": None,
            "coverage": 0.0,
            "source": "legacy", "confidence": "unknown",
            "quality": {"wait_known": False, "approx": False, "legacy": True,
                        "queue_separated": False},
        }
    total = max(0, int(round(float(seconds or 0) * 1000)))
    queue = (0 if queue_ms is None else
             max(0, min(total, int(round(float(queue_ms or 0))))))
    active = max(0, total - queue)
    has_snaps = bool(net_summary and int(net_summary.get("snapshots") or 0) > 0)
    blocked = 0
    if has_snaps:
        blocked = max(0, int(round(float(net_summary.get("blocked_s") or 0) * 1000)))
    exact_raw = (None if service_ms is None else
                 max(0, int(round(float(service_ms or 0)))))
    exact = None if exact_raw is None else min(active, exact_raw)
    service_clamped = exact_raw is not None and exact_raw > active
    other_raw = (None if other_ms is None else
                 max(0, int(round(float(other_ms or 0)))))
    other = other_raw
    service_known = exact is not None or has_snaps
    if not service_known:
        if not other:
            return {
                "v": TIMING_V, "scope": "run",
                "active_ms": active, "total_ms": total, "queue_ms": queue,
                "work_ms": None, "service_wait_ms": None,
                "other_wait_ms": 0, "unknown_ms": None,
                "coverage": 0.0,
                "source": "legacy", "confidence": "unknown",
                "quality": {"wait_known": False, "approx": False, "legacy": True,
                            "queue_separated": queue_ms is not None},
            }
        # Прочее ожидание измерено, а сервисное — нет: ноль вместо него был бы выдумкой.
        other = min(other_raw or 0, active)
        other_clamped = (other_raw or 0) > active
        return {
            "v": TIMING_V, "scope": "run",
            "active_ms": active, "total_ms": total, "queue_ms": queue,
            "work_ms": None, "service_wait_ms": None,
            "other_wait_ms": other, "unknown_ms": None,
            "coverage": 0.0,
            "source": "other", "confidence": "unknown",
            "other_by_kind": {"resource": other} if other else {},
            "quality": {"wait_known": False, "approx": other_clamped, "legacy": False,
                        "clamped": other_clamped,
                        "queue_separated": queue_ms is not None},
        }
    wait = min(max(exact or 0, blocked), active)
    other = min(other or 0, max(0, active - wait))
    other_clamped = (other_raw or 0) > other
    approx = exact is None or blocked > exact or service_clamped or other_clamped
    return {
        "v": TIMING_V, "scope": "run",
        "active_ms": active, "total_ms": total, "queue_ms": queue,
        "work_ms": active - wait - other, "service_wait_ms": wait,
        "other_wait_ms": other, "unknown_ms": 0,
        "coverage": 1.0,
        "source": ("net_snapshots" if exact is None and other_raw is None else
                   ("spans+net" if blocked else
                    ("spans+other" if exact is not None and other_raw is not None else
                     ("spans" if exact is not None else "other")))),
        "confidence": "exact" if exact is not None and not approx else "approx",
        "wait_by_service": {"network": wait} if wait else {},
        "wait_by_cause": {"network": wait} if wait else {},
        "other_by_kind": {"resource": other} if other else {},
        "quality": {"wait_known": True, "approx": approx, "legacy": False,
                    "clamped": (service_clamped or other_clamped
                                or max(exact or 0, blocked) + (other_raw or 0) > active),
                    "queue_separated": queue_ms is not None},
    }


def timing_problems(timing: dict | None) -> list[str]:
    """Проверить схему v1 и инварианты; пусто — сводка корректна."""
    if not isinstance(timing, dict):
        return ["timing: нет объекта"]
    problems = []
    if timing.get("v") != TIMING_V:
        problems.append(f"timing.v: {timing.get('v')!r}, нужен {TIMING_V}")
    for key in ("active_ms", "total_ms", "queue_ms", "other_wait_ms"):
        value = timing.get(key)
        if value is None and key in ("active_ms", "total_ms"):
            continue
        if not isinstance(value, (int, float)):
            problems.append(f"timing.{key}: не число")
    active, total = timing.get("active_ms"), timing.get("total_ms")
    queue = timing.get("queue_ms")
    if (active is None) != (total is None):
        problems.append("timing: active_ms и total_ms должны быть оба null или числами")
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
            return (f"{format_duration(active, style, lang)} ({tail})"
                    + _other_tail(timing, style, lang))
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
    text = f"{work_text} (+ {_wait_phrase(wait, style, lang, approx)})"
    return text + _other_tail(timing, style, lang)


def _other_tail(timing: dict, style: str, lang: str) -> str:
    other = timing.get("other_wait_ms")
    if not isinstance(other, (int, float)) or other <= 0:
        return ""
    tail = "прочее ожидание" if lang == "ru" else "other waiting"
    return f" · {tail} {format_duration(other, style, lang)}"
