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


def _stat(values: Iterable[float], approx: bool = False) -> dict:
    rows = sorted(float(v) for v in values if isinstance(v, (int, float)))
    n = len(rows)
    if not n:
        return {"n": 0, "avg": None, "p50": None, "p95": None, "max": None,
                "approx": bool(approx)}
    def pick(q: float):
        if n < 20:
            return None
        return round(rows[min(n - 1, max(0, int(round(q * (n - 1)))))], 1)
    return {"n": n, "avg": round(sum(rows) / n, 1), "p50": pick(0.5),
            "p95": pick(0.95), "max": round(rows[-1], 1), "approx": bool(approx)}


def _metrics(row: dict, key: str):
    metrics = row.get("metrics") or {}
    value = metrics.get(key)
    return value if isinstance(value, (int, float)) else None


def service_metrics(spans: Iterable) -> dict:
    """Сводка по сервисам для `timing.d` и блока качества итога."""
    rows = [_row(s) for s in spans]
    out: dict = {}
    for row in rows:
        service = str(row.get("service") or "")
        kind = str(row.get("kind") or "")
        if not service or kind not in ("call", "attempt", "wait"):
            continue
        rec = out.setdefault(service, {
            "calls": 0, "attempts": 0, "ok": 0, "errors": {},
            "latency_ms": [], "ttft_ms": [], "slot_wait_ms": [], "backoff_ms": [],
            "ok_latency_ms": [], "tokens_in": 0, "tokens_out": 0, "tps_basis": "",
        })
        if kind == "call":
            rec["calls"] += 1
            if row.get("status") in OK_STATUSES:
                rec["ok"] += 1
            else:
                reason = str(row.get("reason") or row.get("status") or "error")
                rec["errors"][reason] = rec["errors"].get(reason, 0) + 1
        if kind == "attempt":
            rec["attempts"] += 1
        # Время запроса и токены уже сведены в `call`: попытка внутри него — та же
        # работа, и складывать её второй раз значит удваивать скорость и токены.
        if kind == "call":
            for key in ("latency_ms", "ttft_ms"):
                value = _metrics(row, key)
                if value is not None:
                    rec[key].append(value)
                    if key == "latency_ms" and row.get("status") in OK_STATUSES:
                        rec["ok_latency_ms"].append(value)
            for key in ("tokens_in", "tokens_out"):
                value = _metrics(row, key)
                if isinstance(value, (int, float)):
                    rec[key] += int(value)
        elif kind == "wait":
            for key in ("slot_wait_ms", "backoff_ms"):
                value = _metrics(row, key)
                if value is not None:
                    rec[key].append(value)
        # Это строка, а не число: `_metrics` вернул бы для неё None и навсегда оставил
        # `request`. Поток важнее: если хоть одна попытка реально стримила, сервис стримил.
        basis = (row.get("metrics") or {}).get("tps_basis")
        if isinstance(basis, str) and basis:
            if basis == "stream" or not rec["tps_basis"]:
                rec["tps_basis"] = basis
    for rec in out.values():
        ok_latencies = rec.pop("ok_latency_ms")
        rec["latency_ms"] = _stat(rec["latency_ms"])
        rec["ttft_ms"] = _stat(rec["ttft_ms"])
        rec["slot_wait_ms"] = _stat(rec["slot_wait_ms"])
        rec["backoff_ms"] = _stat(rec["backoff_ms"])
        rec["tps_basis"] = rec["tps_basis"] or "request"
        ok_avg = _stat(ok_latencies)["avg"]
        rec["tps"] = (round(rec["tokens_out"] / (ok_avg / 1000), 1)
                      if rec["tokens_out"] and ok_avg else None)
        # Медленный отказ — это ожидание сервиса, а не медленная работа. В `slow_calls`
        # попадает только успешный вызов, который всё равно занял больше предела.
        rec["slow_calls"] = sum(1 for x in ok_latencies if x >= SERVICE_LIMIT_MS)
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
        if kind == "wait":
            if wait_class == "service":
                service.extend(bounds)
                service_rows.extend([row] * len(bounds))
            elif wait_class:
                other.extend(bounds)
                other_rows.extend([row] * len(bounds))
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
        "service_calls": sum(1 for r in call_rows if r.get("status") in OK_STATUSES),
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
           machine: str = "", at: str = "") -> str:
    """Записать одну нормализованную строку в `timing.d`; вернуть путь или пустую строку."""
    root = Path(project).expanduser().resolve()
    if not (root / "AuroraKnowledgeDB").is_dir() or not timing:
        return ""
    run_id = _safe_name(run_id)
    if not run_id:
        return ""
    if not actor_id:
        try:
            from aurora_actor import actor
            actor_id, _display = actor(str(root))
        except Exception:  # noqa: BLE001 — замер не должен ронять прогон
            actor_id = "unknown"
    part = f"{_safe_name(actor_id)}-{_safe_name(machine or _machine_id())}"
    folder = root.joinpath("AuroraKnowledgeDB", "meta", "timing.d", part)
    payload = {
        "schema": 1, "run": run_id, "at": at or _utc_now(),
        "project": root.name, "kind": str(kind or "agent"), "task": str(task or ""),
        "timing": timing, "services": services or {},
    }
    try:
        from aurora_fs import write_json
        folder.mkdir(parents=True, exist_ok=True)
        write_json(str(folder / f"{run_id}.json"), payload, indent=1)
        prune(str(root))
        return str(folder / f"{run_id}.json")
    except Exception:  # noqa: BLE001 — замер не имеет права ронять прогон
        return ""


def prune(project: str, days: int = KEEP_DAYS) -> int:
    """Удалить строки `timing.d` старше срока; ошибка уборки не роняет прогон."""
    root = Path(project).expanduser().resolve()
    folder = root.joinpath("AuroraKnowledgeDB", "meta", "timing.d")
    cutoff = time.time() - max(1, int(days)) * 86400
    gone = 0
    try:
        for path in folder.glob("*/*.json"):
            try:
                if path.stat().st_mtime < cutoff:
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
    перестаёт быть оценкой.
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
    exact = (None if service_ms is None else
             max(0, min(active, int(round(float(service_ms or 0))))))
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
        return {
            "v": TIMING_V, "scope": "run",
            "active_ms": active, "total_ms": total, "queue_ms": queue,
            "work_ms": None, "service_wait_ms": None,
            "other_wait_ms": other, "unknown_ms": None,
            "coverage": 0.0,
            "source": "other", "confidence": "unknown",
            "other_by_kind": {"other": other},
            "quality": {"wait_known": False, "approx": False, "legacy": False,
                        "queue_separated": queue_ms is not None},
        }
    wait = min(max(exact or 0, blocked), active)
    other = min(other or 0, max(0, active - wait))
    approx = exact is None or blocked > exact
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
        "other_by_kind": {"other": other} if other else {},
        "quality": {"wait_known": True, "approx": approx, "legacy": False,
                    "clamped": max(exact or 0, blocked) + (other_raw or 0) > active,
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
