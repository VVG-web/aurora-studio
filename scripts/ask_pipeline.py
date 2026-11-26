#!/usr/bin/env python3
"""ask_pipeline.py - plan, delegate, verify and synthesize an answer to «Спросить».

The old `run_ask` is one model call. This module adds the missing orchestration
around the same building blocks:

  question
    -> search/pack (caller may pass a ready pack)
    -> planner writes a small plan, saved before any answer call
    -> each plan item is delegated to a writer, critic and Momus on its own
       slice of the pack
    -> a synthesizer joins the step results
    -> answer.md + manifest.json + plan.md stay next to the question

It deliberately reuses the existing retry/fallback ring from `agent_core`: every
call goes through the caller's `call` function, usually `agent_core.call_role`,
which already walks provider fallback chains and respects provider slots.

The pipeline is a prototype: it is wired for the CLI and the panel, but it does
not change the behaviour of the old path unless a caller asks for `pipeline=True`.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import answer_core as AC
import ask_budget as QB
import ask_state
from aurora_common import link_escapes
from pipeline_state import number, section_scalars

try:
    import agent_core as AG
except ImportError:  # the panel may import this module before scripts is on sys.path
    AG = None

try:
    import reliable_core as RC
except ImportError:  # old installation: still keep the public contract
    RC = None

ROLES = ("planner", "worker", "critic", "qa")
CHECK_ROLES = ("critic", "qa")
ANSWERED = ("done", "partial")
ROUTE_POLICY = "notice"  # 1A: an allowed substitution is visible, not a quality failure.
TOTAL_CAP = 20 * 60
LONG_SECONDS = 10 * 60
DEFAULT_TOTAL = 20 * 60
DEFAULT_STEP = 300.0
DEFAULT_WORKERS = 4
MAX_STEPS = 12
CANCEL_GRACE = 1.0
QA_ABANDONED_CAP = 2

PROMPT_PLAN = """Ты планировщик ответа на вопрос по базе знаний.

Вопрос: {question}

Ниже — краткое оглавление доступного контекста:

{pack}

Составь короткий план ответа: 2–4 небольших шага, которые можно проверить
независимо. Верни ТОЛЬКО JSON без пояснений:

{{"title": "название работы", "steps": [
  {{"id": "step-1", "title": "короткое название", "brief": "что именно проверить"}},
  {{"id": "step-2", "title": "...", "brief": "..."}}
]}}

Не добавляй шаги, для которых нет материала в контексте. Не пиши ответ.
"""

PROMPT_STEP = """Ты {role} в пайплайне ответа на вопрос по базе знаний.

ВОПРОС: {question}

ПЛАН, ПУНКТ {item}: {title}
ЗАДАЧА ПУНКТА: {brief}

КОНТЕКСТ (только он является источником фактов):

{pack}

Правила:
1. Отвечай только по контексту. Нет факта — скажи «в базе этого нет».
2. После каждого утверждения ставь ссылку [[Имя карточки]].
3. Числа, сроки, названия и цитаты переноси дословно.
4. Не пересказывай весь контекст, отвечай на пункт плана.
5. Если это критик или Момус, верни вердикт в конце отдельной строкой.

Верни результат пункта в Markdown.
"""

PROMPT_CRITIC = """Ты критик ответа на вопрос по базе знаний.

ВОПРОС: {question}
ПУНКТ ПЛАНА: {title}
КОНТЕКСТ:
{pack}

ОТВЕТ ПИСАТЕЛЯ:
{answer}

Проверь, есть ли в ответе утверждения без опоры в контексте. Верни JSON:
{{"ok": true|false, "issues": ["..."], "note": "кратко"}}
"""

PROMPT_MOMUS = """Ты Момус: проверяешь опору ответа на контекст.
ВОПРОС: {question}
КОНТЕКСТ:
{pack}

ОТВЕТ:
{answer}

На каждое утверждение — ОПОРА, НЕТ ОПОРЫ или ПРОТИВОРЕЧИЕ.
Последняя строка ровно одна:
ВЕРДИКТ: ЧИСТО
или
ВЕРДИКТ: БЕЗ ОПОРЫ N
"""

PROMPT_SYNTH = """Синтезируй итоговый ответ на вопрос из результатов пунктов плана.

ВОПРОС: {question}

ПЛАН:
{plan}

РЕЗУЛЬТАТЫ ПУНКТОВ:
{steps}

Правила:
1. Не добавляй фактов, которых нет в результатах и контексте.
2. Сохрани ссылки [[...]] и дословные цитаты.
3. Если пункт не выполнен, назови это в конце отдельным разделом «Частичный результат».
4. Пиши по-русски, коротко, с заголовками по необходимости.
"""


def pipeline_config(cfg: dict | None = None, cwd: str | Path = ".") -> dict:
    """Read pipeline limits from the agent config or project YAML.

    The hard cap is twenty minutes even if a caller passes a larger value. Step
    timeout is capped by the remaining total as well.
    """
    raw = dict((cfg or {}).get("ask_pipeline") or {})
    if not raw:
        raw = section_scalars(cwd, "ask_pipeline")
    total = number(raw.get("total_seconds") or raw.get("total_timeout"), DEFAULT_TOTAL)
    total = max(0.05, min(float(TOTAL_CAP), total))
    step = number(raw.get("step_seconds") or raw.get("step_timeout"), DEFAULT_STEP)
    step = max(0.05, min(total, step))
    workers = int(number(raw.get("max_workers") or raw.get("parallel"), DEFAULT_WORKERS))
    workers = max(1, min(16, workers, MAX_STEPS))
    keep = int(number(raw.get("keep_questions"), ask_state.DEFAULT_KEEP))
    return {"total_seconds": total, "step_seconds": step,
            "max_workers": workers, "keep_questions": max(1, keep),
            "long_seconds": LONG_SECONDS}


def _remaining(deadline: float) -> float:
    return max(0.0, float(deadline) - time.time())


@dataclass(frozen=True)
class _RunWindow:
    started: float
    end: float
    reserve: float

    @property
    def answer(self) -> float:
        return self.end - self.reserve

    def timing(self, synthesis_seconds: float = 0.0) -> dict:
        now = time.time()
        return {"answer_seconds": round(now - self.started, 3),
                "deadline_left_s": round(max(0.0, self.end - now), 3),
                "answer_deadline_s": round(self.answer - self.started, 3),
                "synthesis_reserve_s": round(self.reserve, 3),
                "synthesis_seconds": round(synthesis_seconds, 3)}


def is_long(seconds: float) -> bool:
    """The UI marks an answer as long after ten minutes, not sooner."""
    return float(seconds or 0) > LONG_SECONDS


def _error(code: str, reason: str, action: str = "") -> dict:
    if RC is not None:
        return RC.human_error(code, reason, action)
    return {"code": code, "reason": reason,
            "message": f"Не удалось получить результат, потому что {reason}."
                       + (f" Что можно сделать: {action}" if action else ""),
            "next_step": action}


def _cancel_error() -> dict:
    return _error("cancelled", "вопрос отменён", "запустите новый вопрос")


@dataclass
class _QAAttempt:
    id: str
    backends: frozenset[int]
    started: float
    on_late: Callable[[str, dict], None]
    cancel: Callable[[str], bool] | None
    done: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    phase: str = "queued"
    cancelling: bool = False
    abandoned: bool = False
    request_stopped: bool | None = None
    slot_released: bool | None = None
    cancel_sent: bool = False
    control_returned: bool = False

    def invoke(self, fn):
        with _QA_LOCK:
            if self.phase != "queued":
                return
            self.phase = "running"
            self.thread = threading.current_thread()
        result = None
        try:
            result = fn()
        finally:
            late = None
            with _QA_LOCK:
                self._record_transport(result)
                self.phase = "done"
                self.done.set()
                self.wake.set()
                late = self._retire()
            if late is not None:
                self.on_late(self.id, late)

    def _retire(self):  # with _QA_LOCK held
        if self.phase == "done" and not self.cancelling:
            _QA_ACTIVE.pop(self.id, None)
            return self.facts() if self.abandoned else None
        return None

    def _signal_cancel(self, cancelled):
        stopped = False
        try:
            stopped = cancelled(self.id) is True
        except Exception:  # noqa: BLE001 - the transport did not confirm stop
            pass
        finally:
            with _QA_LOCK:
                if stopped:
                    self.request_stopped = True
                self.cancelling = False
                late = self._retire()
            if late is not None:
                self.on_late(self.id, late)

    def watch(self, call, deadline, cancelled):
        response = []

        def capture():
            try:
                result = call()
            except TimeoutError:
                result = {"ok": False, "error": _error("timeout", "транспорт вернул таймаут")}
            except BaseException as exc:  # returned to run_stage, not a lost daemon error
                response.append(exc)
                return None
            response.append(result)
            return result

        # run_stage waits for this watcher. The request itself remains owned by
        # the tracked thread, so a user cancel can stop waiting before the deadline.
        threading.Thread(target=self.invoke, args=(capture,), name="ask-qa-request", daemon=True).start()
        while not self.done.is_set():
            if cancelled and cancelled():
                return {"ok": False, "error": _cancel_error()}
            left = _remaining(deadline)
            if left <= 0 or (self.wake.wait(min(0.1, left)) and not self.done.is_set()):
                return _QA_OUTER_TIMEOUT
        result = response[0] if response else _QA_OUTER_TIMEOUT
        if isinstance(result, BaseException):
            raise result
        return result

    def _record_transport(self, result) -> None:
        if not isinstance(result, dict):
            return
        facts = result.get("transport")
        if isinstance(facts, dict):
            if type(facts.get("request_stopped")) is bool and self.request_stopped is not True:
                self.request_stopped = facts["request_stopped"]
            if type(facts.get("slot_released")) is bool:
                self.slot_released = facts["slot_released"]
        # A returned agent_core ring with a logical attempt has passed its
        # slot-owning finally. Absence of a trace proves no release or stop.
        trace = result.get("ring_trace")
        req = result.get("req")
        if (self.slot_released is None and isinstance(trace, dict) and trace.get("attempts")
                and isinstance(req, dict) and req.get("role") == "qa" and req.get("attempts")):
            self.slot_released = True

    def facts(self) -> dict:
        return {"control_returned": self.control_returned, "cancel_sent": self.cancel_sent,
                "request_stopped": (False if self.phase == "running" and self.request_stopped is None
                                    else self.request_stopped),
                "slot_released": False if self.phase == "running" else self.slot_released,
                "abandoned": self.abandoned and (self.phase != "done" or self.cancelling),
                "stuck_previous": False}

    def detach(self, *, returned, deadline):
        completed_late = False
        cancel = None
        with _QA_LOCK:
            self.control_returned = returned
            if self.phase == "queued":
                self.phase = "closed"
                _QA_ACTIVE.pop(self.id, None)
                self.done.set()
            elif self.phase == "running":
                self.abandoned = True
                cancel = self.cancel
                self.cancel_sent = self.cancelling = cancel is not None
            else:
                completed_late = returned and self.phase == "done"
            self.wake.set()
        # A broken cancel hook must not hold up the caller or allow more work
        # to pile up. Keep this admission until both hook and request return.
        grace = min(CANCEL_GRACE, _remaining(deadline))
        if cancel is not None:
            signal = threading.Thread(target=self._signal_cancel, args=(cancel,),
                                      name="ask-qa-cancel", daemon=True)
            until = time.monotonic() + grace
            try:
                signal.start()
            except RuntimeError:
                with _QA_LOCK:
                    self.cancel_sent = self.cancelling = False
                    late = self._retire()
                if late is not None:
                    self.on_late(self.id, late)
            else:
                signal.join(max(0.0, until - time.monotonic()))
            grace = max(0.0, until - time.monotonic())
        if self.abandoned:
            self.done.wait(grace)
        with _QA_LOCK:
            facts = self.facts()
        if completed_late:
            self.on_late(self.id, facts)
        return facts


_QA_LOCK = threading.Lock()
_QA_ACTIVE: dict[str, _QAAttempt] = {}
_QA_OUTER_TIMEOUT = object()


def _reserve_qa(backends: frozenset[int], deadline, cancelled, on_late, cancel):
    # Reserve all possible backends before spawning. Admission after timeout
    # would allow an entire burst already running to become abandoned at once.
    while True:
        if cancelled():
            return None, "cancelled"
        with _QA_LOCK:
            if _remaining(deadline) < QB.QA_MIN_RUN:
                return None, "remaining_below_min"
            live = tuple(_QA_ACTIVE.values())
            overlaps = tuple(a for a in live if backends & a.backends)
            if (any(a.abandoned for a in overlaps)
                    or sum(a.abandoned for a in live) >= QA_ABANDONED_CAP):
                return None, "stuck_previous"
            if not overlaps and len(live) < QA_ABANDONED_CAP:
                attempt = _QAAttempt(uuid4().hex, backends, time.time(), on_late, cancel)
                _QA_ACTIVE[attempt.id] = attempt
                return attempt, ""
            # Healthy work is allowed to finish. It is not a stuck predecessor.
            blocker = overlaps[0] if overlaps else next(a for a in live if not a.abandoned)
        blocker.done.wait(min(0.1, _remaining(deadline)))


def _call_limited(cfg: dict, role: str, messages: list, call, deadline: float,
                  timeout: float, prefer: int = 0, history=None,
                  project: str = "", input_limit: int = 0, *,
                  inner_timeout: float | None = None, cancelled=None,
                  qa_attempt: _QAAttempt | None = None,
                  started: float | None = None, **call_options) -> dict:
    """Call the model with a bounded outer timeout.

    `agent_core` already has the retry ring and provider timeouts. This outer
    bound protects the pipeline when a transport ignores its own deadline.
    """
    started = time.time() if started is None else started
    outer_end = min(deadline, started + float(timeout))
    left = _remaining(outer_end)
    failure = _error("deadline", "срок шага истёк", "сократите запрос или повторите позже")
    # reliable_core cannot wait for less than 100 ms. Don't start a call if
    # even that floor no longer fits inside the applicable absolute deadline.
    if left <= 0 or (RC is not None and _remaining(deadline) < 0.1):
        result = {"ok": False, "timed_out": True, "error": failure}
        if qa_attempt is not None:
            result["transport"] = qa_attempt.detach(returned=False, deadline=outer_end)
        return result
    inner_end = outer_end if inner_timeout is None else min(outer_end, started + inner_timeout)
    kw = {**call_options, "deadline": inner_end, "prefer": prefer or 0}
    if inner_timeout is not None:
        kw["request_timeout"] = min(inner_timeout, float(timeout))
    if qa_attempt is not None:
        kw["hard_deadline"] = inner_end
        if qa_attempt.cancel is not None:
            kw["attempt_id"] = qa_attempt.id
    if history:
        kw["history"] = list(history)
    if project:
        kw["project"] = project
    if input_limit:
        kw["input_limit"] = int(input_limit)

    def invoke():
        if cancelled and cancelled():
            return {"ok": False, "error": _cancel_error()}
        if _remaining(inner_end) <= 0:
            return {"ok": False, "timed_out": True, "error": failure}
        return call(cfg, role, messages, **kw)

    def once():
        if qa_attempt is None:
            return invoke()
        return qa_attempt.watch(invoke, outer_end, cancelled)

    outer_timeout = False
    if RC is not None:
        stage = RC.run_stage(f"ask:{role}", once, deadline=outer_end, timeout=left)
        if not stage.ok:
            err = stage.error or _error("stage_failed", "вызов не завершился")
            outer_timeout = stage.timed_out or err.get("code") in ("timeout", "deadline")
            result = {"ok": False, "error": failure if outer_timeout else err,
                      "seconds": stage.seconds, "timed_out": outer_timeout}
        elif stage.value is _QA_OUTER_TIMEOUT or time.time() > outer_end:
            outer_timeout = True
            result = {"ok": False, "error": failure, "timed_out": True}
        else:
            result = stage.value
    elif qa_attempt is not None:
        result = {"ok": False, "error": _error("stage_failed", "внешний сторож проверки недоступен")}
    else:
        try:
            result = once()
        except Exception as exc:  # noqa: BLE001 - pipeline contract
            result = {"ok": False, "error": _error("llm_failed", str(exc))}
    if not isinstance(result, dict):
        result = {"ok": False, "error": _error("llm_invalid", "модель ответила не объектом")}
    result = dict(result)
    if qa_attempt is not None:
        result["qa_used_s"] = max(0.0, time.time() - started)
        result["transport"] = qa_attempt.detach(returned=outer_timeout, deadline=deadline)
        result["outer_guard"] = RC is not None
    result.setdefault("seconds", round(time.time() - started, 2))
    return result


def _role_chain(cfg: dict, role: str) -> list:
    if AG is not None:
        try:
            return list(AG.role_chain(cfg, role) or [])
        except Exception:  # noqa: BLE001
            return []
    return []


@dataclass(frozen=True)
class _RouteIssue:
    code: str
    warning: str
    proven: bool
    degraded: bool


class _RunIssues:
    def __init__(self):
        self.warnings: list[str] = []
        self.notices: list[str] = []
        self.degraded: list[str] = []

    def add(self, code: str = "", warning: str = "", *, degraded: bool = False) -> None:
        if code:
            (self.degraded if degraded else self.notices).append(code)
        if warning:
            self.warnings.append(warning)

    def route(self, issue: _RouteIssue | None) -> None:
        if issue is not None:
            self.add(issue.code, issue.warning, degraded=issue.degraded)


def _role_notices(cfg: dict, issues: _RunIssues) -> None:
    chains = ((cfg or {}).get("chains") or {}).get("llm") or {}
    for role in ROLES:
        if not chains.get(role):
            issues.add(f"role_config:{role}:chain", f"роль «{role}» не настроена: используется общая цепочка")
        elif not _role_chain(cfg, role):
            issues.add(f"role_config:{role}:no_model", f"у роли «{role}» нет доступной модели")
    if not (cfg or {}).get("backends") and not chains:
        issues.add("role_config:all:empty", "в настройке агента нет моделей: пайплайн не сможет ответить")


def _backend_number(value) -> bool:
    return type(value) is int and value > 0


def _fallback_from_trace(trace, backend) -> tuple[int, int] | None:
    if not isinstance(trace, dict) or not _backend_number(backend):
        return None
    order, attempts, winner = trace.get("order"), trace.get("attempts"), trace.get("winner")
    if not isinstance(order, list) or not order or not all(map(_backend_number, order)):
        return None
    ring = set(order)
    if len(ring) != len(order) or not isinstance(attempts, list) or not attempts:
        return None
    ns = [a.get("n") if isinstance(a, dict) else None for a in attempts]
    if not all(_backend_number(n) and n in ring for n in ns):
        return None
    if not _backend_number(winner) or winner != backend or ns[-1] != winner:
        return None
    assigned = trace.get("assigned")
    first = trace.get("first_attempt", ns[0])
    prefer = trace.get("prefer", 0)
    if (not _backend_number(assigned) or assigned != order[0]
            or not _backend_number(first) or first != ns[0]
            or type(prefer) is not int or prefer < 0):
        return None
    return next(((n, winner) for n in reversed(ns[:-1]) if n != winner), None)


_LOG_SKIP = re.compile(r"пропущен|слот|занят|нет модели|карантин|вернёмся через|"
                       r"подождать|лимит скорости|длиннее окна|не начат|\bHTTP\s+429\b", re.I)
_LOG_FAILURE = re.compile(r"\b(?:down|timeout)\b|не отвечал|не ответил|не уложился|"
                          r"connection refused|timed out|\bHTTP\s+[45]\d\d\b", re.I)


def _fallback_from_legacy_log(backend, log) -> tuple[int, int] | None:
    if not _backend_number(backend) or not isinstance(log, list):
        return None
    for line in reversed(log):
        if not isinstance(line, str) or _LOG_SKIP.search(line) or not _LOG_FAILURE.search(line):
            continue
        number = re.search(r"№([0-9]{1,12})(?![0-9])", line)
        if number and (n := int(number[1])) > 0 and n != backend:
            return n, backend
    return None


def _route_issue(stage: str, result: dict, cfg: dict, policy: str = ROUTE_POLICY) -> _RouteIssue | None:
    if not isinstance(result, dict) or result.get("cached") is True or result.get("ok") is not True:
        return None
    proven = "ring_trace" in result
    backend = result.get("backend")
    pair = (_fallback_from_trace(result["ring_trace"], backend) if proven
            else _fallback_from_legacy_log(backend, result.get("log")))
    if pair is None:
        return None
    if not proven:
        chain = _role_chain(cfg, "planner" if stage == "synth" else stage)
        if chain and not set(pair).issubset({b.get("n") for b in chain}):
            return None
    before, after = pair
    detail = "" if proven else " (по старому журналу, разрешённая цепочка не подтверждена)"
    return _RouteIssue(f"routing_fallback:{stage}:{before}->{after}",
                       f"роль «{stage}»: запасной путь №{before}->№{after}{detail}",
                       proven, proven and policy == "degraded")


def _parse_json(text: str, default=None, *, embedded: bool = True):
    raw = str(text or "").removeprefix("\ufeff").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.I | re.S)
    if fence:
        raw = fence[1]
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        pass
    if not embedded:
        return default
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, RecursionError):
            pass
    return default


def _normalize_plan(got, pack: str) -> dict:
    data = got if isinstance(got, dict) else {}
    steps = []
    raw_steps = data.get("steps")
    ids = set()
    for i, raw in enumerate(raw_steps[:MAX_STEPS] if isinstance(raw_steps, list) else [], 1):
        if not isinstance(raw, dict):
            continue
        item = ask_state.safe_id(raw.get("id") or f"step-{i}")
        if item in ids:
            item = f"step-{i}"
            while item in ids:
                item += "-alt"
        ids.add(item)
        item = {
            "id": item,
            "title": str(raw.get("title") or f"Шаг {i}")[:160],
            "brief": str(raw.get("brief") or "")[:800],
        }
        steps.append(item)
    return {"title": str(data.get("title") or "Ответ на вопрос")[:160],
            "steps": steps or _fallback_steps(pack), "planner_fallback": not steps}


def _split_pack(pack: str, count: int) -> list[str]:
    """Split a pack into at most `count` card groups, preserving each card whole."""
    cards = []
    try:
        import context_store as CS
        _preamble, parsed = CS._split_cards(pack)
        cards = [c["text"] if c["text"].startswith("##") else f"## {c['text']}" for c in parsed]
    except Exception:  # noqa: BLE001
        cards = []
    if not cards:
        return [str(pack or "")]
    count = max(1, min(int(count or 1), len(cards)))
    groups = [[] for _ in range(count)]
    for i, card in enumerate(cards):
        groups[i % count].append(card)
    # Keep separators as well as headers. Subheadings inside a card must not
    # become independent sources when the QA evidence parses this fragment.
    return ["# Пак\n\n" + "\n\n".join(f"---\n\n{card}" for card in group) for group in groups]


def _fallback_steps(pack: str) -> list[dict]:
    chunks = _split_pack(pack, 3)
    return [{"id": f"step-{i}", "title": f"Часть {i}", "brief": "Ответить по своему фрагменту контекста"}
            for i in range(1, len(chunks) + 1)]


def _decorate_plan(plan: dict, pack: str) -> dict:
    steps = plan.get("steps") or []
    chunks = _split_pack(pack, max(1, len(steps)))
    for i, step in enumerate(steps):
        step["pack"] = chunks[i] if i < len(chunks) else (chunks[-1] if chunks else pack)
    return plan


def _prepare_pack(pack: str, cfg: dict, cwd: str | Path, call, state: ask_state.AskState,
                  issues: _RunIssues, deadline: float, timeout: float, cancelled) -> tuple[str, dict]:
    """Compress a large pack only through the context layer, with protection.

    `context_store.summarize` appends quotes/law references verbatim; the first
    N full cards remain available as the working set. With the feature disabled
    or without a configured compressor the input pack is returned unchanged.
    """
    try:
        import context_budget as CB
        import context_runtime as CR
        import context_store as CS
    except Exception:  # noqa: BLE001
        return pack, {"enabled": False}
    try:
        cfg_ctx = CR.config(cwd)
    except Exception as exc:  # noqa: BLE001
        return pack, {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    win = CB.window(cfg)
    if not cfg_ctx.get("enabled") or win <= 0:
        return pack, {"enabled": False, "window": win}
    if call is None:
        return pack, {"enabled": True, "triggered": False, "window": win,
                      "error": "модель не передана"}
    used = CB.estimate_tokens(pack)
    if not CB.should_compress(used, win, cfg_ctx):
        return pack, {"enabled": True, "triggered": False, "used_tokens": used,
                      "window": win}
    store = None
    try:
        store = CS.open_ask_store(cwd, state.id)
        ids = store.put_pack(pack)

        def bounded_call(agent_cfg, role, messages, **options):
            return _call_limited(agent_cfg, role, messages, call, deadline, timeout,
                                 cancelled=cancelled, **options)

        compressor = CR.compressor_callable(bounded_call, cfg)
        if compressor is None:
            issues.add(warning="роль «compressor» не настроена: сжатие экстрактивное, "
                               "цитаты и нормы сохранены отдельно")
        compact = store.summarize(ids, compressor=compressor, level="ask",
                                  target_chars=CB.target_chars(win, cfg_ctx))
        first = int(cfg_ctx.get("ask_full_cards") or 10)
        working = store.planner_pack(pack, max_full=first)
        summary = str(compact.get("summary") or "").strip()
        out = working.rstrip() + ("\n\n## Сжатый контекст\n\n" + summary if summary else "")
        return out, {"enabled": True, "triggered": True, "used_tokens": used,
                     "window": win, "compressed_tokens": compact.get("tokens", 0),
                     "protected": len(compact.get("protected") or []),
                     "working_cards": first}
    except Exception as exc:  # noqa: BLE001 - compression must not cancel the answer
        issues.add("compression_failed", f"сжатие контекста не выполнено: {type(exc).__name__}")
        return pack, {"enabled": True, "triggered": False,
                      "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if store is not None:
            store.close()


def _plan_prompt(question: str, pack: str) -> str:
    # Planner gets the card table, not every full card: enough to choose steps,
    # small enough to keep the plan call cheap.
    try:
        import context_store as CS
        _preamble, cards = CS._split_cards(pack)
        if cards:
            lines = ["| id | доверие | суть |", "|---|---|---|"]
            for card in cards[:30]:
                lines.append(f"| {card['id']} | {card.get('trust') or '—'} | "
                             f"{' '.join(card['text'].split())[:180]} |")
            pack = "\n".join(lines)
    except Exception:  # noqa: BLE001
        pack = str(pack or "")[:12000]
    return PROMPT_PLAN.format(question=question, pack=pack[:20000])


def _step_prompt(question: str, step: dict, role: str = "worker") -> str:
    return PROMPT_STEP.format(question=question, role=role,
                              item=step.get("id"), title=step.get("title"),
                              brief=step.get("brief"), pack=step.get("pack") or "")


def _critic_prompt(question: str, step: dict, answer: str) -> str:
    return PROMPT_CRITIC.format(question=question, title=step.get("title"),
                                pack=step.get("pack") or "", answer=answer)


def _momus_prompt(question: str, step: dict, answer: str) -> str:
    return PROMPT_MOMUS.format(question=question, pack=step.get("pack") or "", answer=answer)


def momus_verdict(text: str) -> tuple[str, int]:
    lines = str(text or "").removeprefix("\ufeff").rstrip().splitlines()
    line = lines[-1].strip() if lines else ""
    match = re.fullmatch(r"ВЕРДИКТ:\s*(ЧИСТО|БЕЗ\s+ОПОРЫ\s+(\d+))[.!]?", line, re.I)
    if match is None:
        return "missing", 0
    if match[2] is None:
        return "pass", 0
    try:
        return "unsupported", int(match[2])
    except ValueError:
        return "missing", 0


def critic_verdict(result: dict | None = None, file_text: str = "") -> str:
    if result is not None and result.get("ok") is not True:
        return "failed"
    text = str(result.get("text") or "") if result is not None else re.sub(
        r"\A# Критик · [^\r\n]*(?:\r\n?|\n)", "", file_text.removeprefix("\ufeff"))
    data = _parse_json(text, embedded=False)
    if not isinstance(data, dict) or type(data.get("ok")) is not bool:
        return "missing"
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        return "missing"
    return "pass" if data["ok"] and not issues else "remarks"


def _step_metadata(result: dict) -> tuple[str, str]:
    model = str(result.get("model") or "")
    provider = str(result.get("provider") or result.get("backend") or "")
    return model, provider


def _momus_timeout(cfg: dict, answered_in: float) -> float:
    from agent_runner import momus_timeout

    return momus_timeout(cfg, answered_in)


def _qa_order(cfg: dict, slot: int, call) -> list[dict]:
    if AG is None or not cfg.get("backends"):
        return []
    order = AG.ring_order(cfg, slot, "qa")
    # An injected caller can implement a legacy transport even if no chain is
    # supplied. call_role itself has no such fallback for an empty kit chain.
    if not order and call is not AG.call_role:
        order = AG.ring_order({**cfg, "chains": None}, slot, "qa")
    return order


def _qa_requested(cfg: dict, order: list[dict], slot: int) -> dict:
    if not order:
        return {"model": "", "provider": "", "source": "auto"}
    backend = order[0]
    in_chain = bool(_role_chain(cfg, "qa"))
    valid_prefer = slot == backend["n"] and (in_chain or cfg.get("chains") is None)
    source = "prefer" if valid_prefer else "chain" if in_chain else "auto"
    return {"model": AG._ring_label(AG.role_model(backend, "qa")),
            "provider": (AG._ring_label(backend.get("provider"))
                         or AG._ring_label(backend.get("name")) or str(backend["n"])),
            "source": source}


def _qa_actual(order: list[dict], result: dict) -> dict | None:
    if AG is None:
        return None
    model, _ = _step_metadata(result)
    trace = result.get("ring_trace")
    attempts = trace.get("attempts") if isinstance(trace, dict) else None
    last = attempts[-1] if isinstance(attempts, list) and attempts else None
    last = last if isinstance(last, dict) else {}
    backend = result.get("backend") or last.get("n")
    provider = AG._ring_label(result.get("provider"))
    model = AG._ring_label(model or (last.get("model") if last.get("n") == backend else ""))
    if not provider and _backend_number(backend):
        candidate = next((b for b in order if b["n"] == backend), {})
        provider = (AG._ring_label(last.get("provider") if last.get("n") == backend else "")
                    or AG._ring_label(candidate.get("provider"))
                    or AG._ring_label(candidate.get("name")) or str(backend))
    return {"model": model, "provider": provider} if model or provider else None


def _run_qa(state, cfg, step, messages, call, slot, window, cancelled, writer_seconds) -> dict:
    answer_dl = window.answer
    order = _qa_order(cfg, slot, call)
    requested = _qa_requested(cfg, order, slot)
    want = _momus_timeout(cfg, writer_seconds)
    transport = {"control_returned": False, "cancel_sent": False, "request_stopped": None,
                 "slot_released": None, "abandoned": False, "stuck_previous": False}

    def late(attempt_id, facts):
        state.record_late_qa(step["id"], attempt_id, facts)

    # No transport cancel API in the built-in call_role. An injected caller can
    # accept attempt_id and expose cancel(attempt_id), with True acknowledging stop.
    cancel = getattr(call, "cancel", None)
    attempt, skip = None, ""
    if not requested["model"]:
        result = {"ok": False, "error": _error("qa_config", "у Момуса нет настроенной модели")}
    else:
        attempt, skip = _reserve_qa(frozenset(b["n"] for b in order), answer_dl, cancelled,
                                    late, cancel if callable(cancel) else None)
        result = {"ok": True}  # A skipped required check is P1 done/missing, not failed.
    now = time.time()
    budget = QB.qa_budget(want, answer_dl - now, window.end - now, AC.VERIFY_CAP)
    if attempt is not None and (cancelled() or budget.given_s < QB.QA_MIN_RUN):
        attempt.detach(returned=False, deadline=answer_dl)
        attempt, skip = None, "cancelled" if cancelled() else "remaining_below_min"
    if attempt is not None:
        result = _call_limited(cfg, "qa", messages, call, answer_dl, budget.outer_s,
                               prefer=slot, project=state.cwd, inner_timeout=budget.given_s,
                               cancelled=cancelled, qa_attempt=attempt, started=now)
        transport = result.pop("transport", transport)
    if cancelled():
        skip = "cancelled"
    transport["stuck_previous"] = skip == "stuck_previous"
    timed_out = not skip and QB.qa_timed_out(result)
    used = result.pop("qa_used_s", 0.0)
    actual = None if timed_out or skip else _qa_actual(order, result)
    result.update(actual or {k: requested[k] for k in ("model", "provider")})
    if timed_out:
        result["ok"] = False
        if not result.get("error"):
            result["error"] = _error("timeout", "Момус не уложился в срок")
    source = step.get("pack") or ""
    info = {"budget": budget.evidence(used, AG.ROLE_FLOOR["qa"] if AG is not None else want),
            "source": {"sha256": ask_state.sha256(source), "cards": _source_count(source)},
            "transport": transport, "requested": requested,
            "model_source": "actual" if actual is not None else "requested",
            "timed_out": timed_out, "skip_reason": skip,
            "attempt_id": attempt.id if attempt is not None else "",
            "outer_guard": result.pop("outer_guard", RC is not None)}
    if actual is not None:
        info["actual"] = actual
    measured = result.get("timing")
    measured = measured if isinstance(measured, dict) else {}
    for key in ("ttft_ms", "latency_ms", "slot_wait_ms"):
        value = result.get(key, measured.get(key))
        info[key] = value if type(value) in (float, int) else None
    result.update(seconds=round(used, 3), _qa=info)
    return result


def _source_count(pack: str) -> int:
    from context_store import _split_cards

    return len(_split_cards(pack)[1])


def _run_step(state: ask_state.AskState, cfg: dict, question: str, step: dict,
              call, window: _RunWindow, step_timeout: float, slot: int, history,
              contract: tuple[str, ...], emit, cancelled) -> dict:
    item = step["id"]
    deadline = window.answer
    state.mark_step(item, status="running", started=ask_state.utc_now(), attempts=1)
    state.update_state(status="running", current=item)
    if emit:
        emit({"kind": "step_start", "step": item, "title": step.get("title"),
              "role": "worker", "slot": slot})
    started = time.time()

    if cancelled():
        return _cancel_step(state, item, "worker")
    while state.control().get("pause") and not cancelled():
        state.update_state(status="paused", current=item)
        if emit:
            emit({"kind": "paused", "step": item})
        if _remaining(deadline) <= 0:
            state.mark_step(item, status="failed", error="срок истёк во время паузы")
            return {"id": item, "status": "failed", "answer": "",
                    "error": "deadline"}
        time.sleep(min(0.2, _remaining(deadline)))
    if cancelled():
        return _cancel_step(state, item, "worker")
    state.mark_step(item, checks=[])
    for role in CHECK_ROLES:
        path = state.check_path(item, role)
        # Do not resurrect the previous attempt through a legacy orphan file.
        if path.parent.resolve().is_relative_to(state.dir.resolve()):
            path.unlink(missing_ok=True)
    if not _source_count(step.get("pack") or ""):
        err = _error("no_sources", "в пункте нет карточек", "уточните вопрос")
        return _finish_step(state, cfg, question, step, call, window, step_timeout, slot,
                            contract, emit, {"ok": False, "error": err}, 0.0, cancelled)
    res = _call_limited(cfg, "worker", [{"role": "user",
                        "content": _step_prompt(question, step)}], call, deadline,
                        step_timeout, prefer=slot, history=history, project=state.cwd,
                        cancelled=cancelled)
    if cancelled() and res.get("ok") is not True:
        return _cancel_step(state, item, "worker")
    seconds = round(time.time() - started, 2)
    return _finish_step(state, cfg, question, step, call, window, step_timeout, slot,
                        contract, emit, res, seconds, cancelled)


def _cancel_step(state, item, before, row=None):
    state.event("cancel_checkpoint", "Пользователь остановил дальнейшие вызовы", step=item,
                cancelled_before=before)
    if row is None:
        state.mark_step(item, status="cancelled", error="отменено пользователем")
        row = {"id": item, "status": "cancelled", "answer": "", "error": "cancel"}
    return {**row, "cancelled_before": before}


def _finish_step(state: ask_state.AskState, cfg: dict, question: str, step: dict,
                 call, window: _RunWindow, step_timeout: float, slot: int,
                 contract: tuple[str, ...], emit, writer: dict, seconds: float, cancelled) -> dict:
    item = step["id"]
    if writer.get("ok") is not True:
        err = _step_error(writer.get("error"), "llm_failed", "писатель не ответил")
        reason = err.get("message") or err.get("reason") or "ошибка модели"
        state.write_step(item, f"# {step.get('title')}\n\n## Ошибка\n\n{reason}\n",
                         role="worker", status="failed", seconds=seconds,
                         error=reason, attempts=1, answer_chars=0)
        if emit:
            emit({"kind": "step_error", "step": item, "error": err.get("code") or "failed",
                  "message": err.get("message") or err.get("reason") or ""})
        return {"id": item, "status": "failed", "answer": "", "error": err,
                "seconds": seconds, "warnings": [err.get("reason", "")]}
    answer = str(writer.get("text") or "").strip()
    model, provider = _step_metadata(writer)
    text = (f"# {step.get('title')}\n\n## Задание\n\n{step.get('brief') or ''}\n\n"
            f"## Ответ\n\n{answer}\n")
    state.write_step(item, text, role="worker", model=model, provider=provider,
                     status="done", seconds=seconds, attempts=1, answer_chars=len(answer))
    routes = [_route_issue("worker", writer, cfg)]
    if emit:
        emit({"kind": "step_done", "step": item, "role": "worker",
              "model": model, "provider": provider, "seconds": seconds,
              "tokens": len(step.get("pack") or "") // 3,
              "preview": answer[:400]})
    row = {"id": item, "status": "done", "answer": answer, "model": model,
           "provider": provider, "seconds": seconds}

    for index, role in enumerate(contract):
        if cancelled():
            return _cancel_step(state, item, role, row)
        prompt = _momus_prompt if role == "qa" else _critic_prompt
        messages = [{"role": "user", "content": prompt(question, step, str(writer.get("text") or ""))}]
        if role == "qa":
            answered_in = max(0.0, number(writer.get("seconds"), 0.0))
            result = _run_qa(state, cfg, step, messages, call, slot, window, cancelled,
                             answered_in)
        else:
            result = _call_limited(cfg, role, messages, call, window.answer,
                                   step_timeout, prefer=slot, project=state.cwd, cancelled=cancelled)
        if cancelled():
            if role == "qa":
                state.event("qa_cancelled", "Момус: отменено", step=item, **result["_qa"])
            following = contract[index + 1] if index + 1 < len(contract) else "synthesis"
            return _cancel_step(state, item, following, row)
        _write_check(state, step, role, result, emit)
        routes.append(_route_issue(role, result, cfg))
    return {**row, "_routes": [r for r in routes if r is not None]}


_CHECK_LABELS = {"critic": "Критик", "qa": "Момус"}
_CHECK_MARKS = {
    "pass": {"critic": "опора", "qa": "чисто"},
    "failed": dict.fromkeys(CHECK_ROLES, "не выполнен"),
    "missing": dict.fromkeys(CHECK_ROLES, "без вердикта"),
    "remarks": {"critic": "замечания"},
}


def _qa_note(info: dict) -> str:
    skip = info.get("skip_reason")
    budget = info.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    if skip == "remaining_below_min":
        left = budget.get("deadline_left_s")
        return f"пропущено, остаток {left:g} с" if type(left) in (int, float) else "пропущено по сроку"
    if skip == "stuck_previous":
        return "пропущено, предыдущий вызов не завершён"
    if info.get("timed_out"):
        given = budget.get("given_s")
        return f"не выполнен (лимит {given:g} с)" if type(given) in (int, float) else "не выполнен (таймаут)"
    return ""


def _write_check(state: ask_state.AskState, step: dict, role: str, result: dict, emit) -> None:
    ok = result.get("ok") is True
    text = str(result.get("text") or "") if ok else ""
    error = result.get("error")
    info = result.get("_qa") or {}
    if info.get("skip_reason"):
        verdict, count = "missing", 0
        text = _qa_note(info)
    elif not ok:
        verdict, count = "failed", 0
        text = _step_error(error, "", "проверка не выполнена")["message"]
    elif role == "critic":
        verdict, count = critic_verdict(result), 0
    else:
        verdict, count = momus_verdict(text)
    model, provider = _step_metadata(result)
    status = "done" if ok or info.get("skip_reason") else "failed"
    req = (result.get("req") or {}) if role == "qa" else {}
    attempts = req.get("attempts") if isinstance(req, dict) else None
    state.write_step(step["id"], f"# {_CHECK_LABELS[role]} · {step.get('title')}\n\n"
                     f"{text}\n",
                     role=role, model=model, provider=provider, status=status,
                     seconds=float(result.get("seconds") or 0), check=True,
                     error="" if ok else str(error or ""), verdict=verdict, unsupported=count,
                     attempts=attempts if type(attempts) is int else 0, check_diagnostics=info)
    if info:
        state.event("check", _qa_note(info) or "Момус завершил проверку", step=step["id"],
                    role=role, verdict=verdict, **info)
    if emit:
        if role == "qa":
            status = "clean" if verdict == "pass" else "unproven"
        event = {"kind": "check", "step": step["id"], "role": role, "status": status}
        if info:
            event.update(verdict=verdict, **{k: info[k] for k in ("skip_reason", "timed_out", "budget")})
        emit(event)


def last_role_record(step: dict, role: str) -> dict | None:
    checks = step.get("checks")
    if not isinstance(checks, list):
        return None
    return next((r for r in reversed(checks) if isinstance(r, dict) and r.get("role") == role), None)


def _evidence_path(state: ask_state.AskState, default: Path, record: dict) -> Path | None:
    rel = record.get("file")
    if rel is not None and rel != "":
        if not isinstance(rel, str) or Path(rel).is_absolute():
            return None
        default = state.dir / rel
    try:
        path = default.resolve()
        return path if path.is_relative_to(state.dir.resolve()) else None
    except (OSError, RuntimeError, ValueError):
        return None


def _evidence_text(path: Path | None, record: dict) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    expected = record.get("sha256")
    return None if expected and ask_state.sha256(text) != expected else text


def _recorded_verdict(record: dict, role: str) -> tuple[str, int]:
    verdict, count = record["verdict"], record.get("unsupported", 0)
    if type(count) is not int or count < 0:
        return "missing", 0
    if role == "qa" and verdict == "unsupported":
        return verdict, count
    if count == 0 and (verdict in ("pass", "missing", "failed") or role == "critic" and verdict == "remarks"):
        return verdict, count
    return "missing", 0


def _check_verdict(record: dict | None, role: str, path: Path | None) -> tuple[str, int]:
    if record is not None:
        if record.get("status") == "failed":
            return "failed", 0
        if record.get("status") != "done" or path is None:
            return "missing", 0
        if "verdict" in record:
            return _recorded_verdict(record, role)
    text = _evidence_text(path, record or {})
    if text is None:
        return "missing", 0
    return (critic_verdict(file_text=text), 0) if role == "critic" else momus_verdict(text)


def normalize_step_checks(step: dict, contract: tuple[str, ...], state: ask_state.AskState) -> list[dict]:
    checks = []
    for role in CHECK_ROLES:
        record = last_role_record(step, role)
        path = _evidence_path(state, state.check_path(step["plan_item_id"], role), record or {})
        if record is None and role not in contract and (path is None or not path.is_file()):
            continue
        required = role in contract or (record or {}).get("required") is not False
        verdict, count = _check_verdict(record, role, path)
        check = {"role": role, "verdict": verdict, "unsupported": count,
                 "required": required, "effective_required": required}
        if role == "qa" and record is not None and isinstance(record.get("budget"), dict):
            # Display diagnostics only for the corresponding canonical verdict.
            applicable = (verdict == "missing" and record.get("skip_reason")) or (
                verdict == "failed" and record.get("timed_out"))
            if applicable:
                check["detail"] = _qa_note(record)
        checks.append(check)
    return checks


def _read_step_answer(state: ask_state.AskState, record: dict) -> str | None:
    path = _evidence_path(state, state.step_path(record["plan_item_id"]), record)
    text = _evidence_text(path, record)
    if text is None:
        return None
    marker = re.search(r"^##[ \t]+Ответ[ \t]*(?:\r?\n|$)", text, re.M)
    if marker is None:
        return None
    length = record.get("answer_chars")
    if type(length) is not int:
        return text[marker.end():].strip()  # Legacy step, no saved body length.
    if length == 0:
        return ""
    # A planner's brief and the writer's Markdown can both contain headings.
    # The saved body length locates the writer's exact text in a verified file.
    end = text.rstrip()
    start = len(end) - length
    delimiter = "\n## Ответ\n\n"
    if length > 0 and start >= marker.end() and end.startswith(delimiter, start - len(delimiter)):
        return end[start:]
    return None


def _step_error(error, code: str, message: str) -> dict:
    if isinstance(error, dict):
        return {"code": code, "message": error.get("reason") or message, **error}
    return {"code": code, "message": str(error) if error else message}


def _check_mark(check: dict) -> str:
    role, verdict = check["role"], check["verdict"]
    text = (f"без опоры {check['unsupported']}" if verdict == "unsupported"
            else _CHECK_MARKS[verdict][role])
    return f"{_CHECK_LABELS[role]}: {check.get('detail') or text}"


def _check_issue(item: str, title: str, check: dict) -> tuple[str, str]:
    role, verdict = check["role"], check["verdict"]
    if verdict == "remarks":
        return f"critic_remarks:{item}", ""
    if verdict == "unsupported":
        return f"momus_unsupported:{item}:{check['unsupported']}", ""
    return f"check_{verdict}:{role}:{item}", f"пункт «{title}»: {_check_mark(check)}"


def _inspect_step(row: dict, record: dict | None, contract: tuple[str, ...],
                  state: ask_state.AskState) -> list[tuple[str, str]]:
    item, title, status = row["id"], row["title"], row["status"]
    saved = (record or {}).get("status")
    if record is not None and status in ANSWERED and saved in ANSWERED:
        answer = _read_step_answer(state, record)
        if answer is not None:
            row["answer"] = answer
            row["checks"] = normalize_step_checks(record, contract, state)
            reasons = []
            if not row["answer"].strip():
                reasons.append((f"worker_empty:{item}", f"пункт «{title}»: Ответ: пустой"))
            reasons.extend(_check_issue(item, title, c) for c in row["checks"]
                           if c["effective_required"] and c["verdict"] != "pass")
            if "partial" in (status, saved) and not reasons:
                reasons.append((f"step_partial:{item}", f"пункт «{title}»: частичный результат"))
            row["status"] = "partial" if reasons else "done"
            return reasons
        row["error"] = {"code": "step_failed", "message": "файл шага недоступен"}
    status = "cancelled" if record is not None and "cancelled" in (status, saved) else "failed"
    code = f"step_{status}"
    error = _step_error(row.get("error") or (record or {}).get("error"), code, "не выполнен")
    row.update(status=status, error=error, answer="", checks=[])
    return [(f"{code}:{item}", f"пункт «{title}»: {error['message']}")]


def normalize_steps(steps: list[dict], plan: dict, contract: tuple[str, ...],
                    state: ask_state.AskState, issues: _RunIssues) -> list[dict]:
    returned = {s["id"]: s for s in steps}
    records = {s.get("plan_item_id"): s for s in state.manifest().get("steps") or []}
    normalized = []
    for item in plan.get("steps") or []:
        ident = item["id"]
        record = records.get(ident)
        row = {"id": ident, "status": "failed", **returned.get(ident, {}),
               "title": item.get("title") or ident}
        routes = row.pop("_routes", [])
        for route in routes:
            issues.route(route)
        reasons = _inspect_step(row, record, contract, state)
        row["warnings"] = [r.warning for r in routes] + [w for _, w in reasons if w]
        for code, warning in reasons:
            issues.add(code, warning, degraded=True)
        if record is None:
            state.ensure_step(ident, row["title"], error=row["error"])
        elif row["status"] != record.get("status"):
            state.mark_step(ident, status=row["status"], error=row.get("error", ""))
        elif row["status"] != item.get("status"):
            state.mark_plan_item(ident, row["status"])
        normalized.append(row)
    return normalized


def completion_of(steps: list[dict]) -> dict:
    counts = dict.fromkeys(("done", "partial", "failed", "cancelled"), 0)
    checks = dict.fromkeys(("passed", "remarks", "unsupported", "failed", "missing"), 0)
    for step in steps:
        counts[step["status"]] += 1
        if step["status"] in ANSWERED:
            for check in step["checks"]:
                if check["effective_required"]:
                    verdict = check["verdict"]
                    checks["passed" if verdict == "pass" else verdict] += 1
    return {"steps": {"total": len(steps), **counts},
            "checks": {"required": sum(checks.values()), **checks}}


def full_status(steps: list[dict], synth_fallback: bool, planner_fallback: bool, degraded: list[str]) -> bool:
    return (bool(steps) and all(s["status"] == "done" for s in steps)
            and all(c["verdict"] == "pass" for s in steps for c in s["checks"] if c["effective_required"])
            and not synth_fallback and not planner_fallback and not degraded)


def map_status(full: bool) -> tuple[str, str]:
    return ("full", "done") if full else ("partial", "partial")


def _synthesize(cfg: dict, question: str, plan: dict, steps: list, call,
                deadline: float, step_timeout: float, history, state,
                issues: _RunIssues, emit, cancelled) -> tuple[str, bool]:
    ok_steps = [s for s in steps if s["status"] in ANSWERED and s["answer"]]
    if not ok_steps or cancelled():
        return "", False
    plan_text = "\n".join(f"{i}. {s.get('title')}" for i, s in enumerate(plan.get("steps") or [], 1))
    step_text = "\n\n".join(f"## {s.get('id')} · {s.get('title')}\n{s.get('answer')}"
                            for s in ok_steps)
    failed = [s for s in steps if s["status"] not in ANSWERED]
    prompt = PROMPT_SYNTH.format(question=question, plan=plan_text, steps=step_text)
    res = (_call_limited(cfg, "planner", [{"role": "user", "content": prompt}],
                         call, deadline, step_timeout, history=history,
                         project=state.cwd, cancelled=cancelled) if _remaining(deadline) > 0 else {})
    if cancelled():
        return "", False
    issues.route(_route_issue("synth", res, cfg))
    answer = str(res.get("text") or "").strip() if res.get("ok") is True else ""
    fallback = not answer
    if not fallback:
        if emit:
            emit({"kind": "synth", "status": "done", "model": _step_metadata(res)[0]})
    else:
        answer = "\n\n".join(f"### {s.get('title')}\n\n{s.get('answer')}" for s in ok_steps)
        issues.add("synth_fallback", "итоговый агент не ответил: итог собран из результатов шагов",
                   degraded=True)
        if emit:
            emit({"kind": "synth", "status": "fallback"})
    if failed:
        answer += "\n\n## Частичный результат\n\n" + "\n".join(
            f"- {s.get('title')}: {((s.get('error') or {}).get('message') or s.get('status'))}"
            for s in failed)
    answer += _checks_section(steps)
    return answer, fallback


def _checks_section(steps: list) -> str:
    """Marks each plan item's Writer/Critic/Momus outcome under the final answer."""
    rows = []
    for step in steps:
        marks = [_check_mark(check) for check in step["checks"]]
        if step["status"] == "partial" and not step["answer"].strip():
            marks.insert(0, "Ответ: пустой")
        rows.append(f"- {step.get('title') or step.get('id')}: "
                    + (", ".join(marks) if marks else "проверок нет"))
    return "\n\n## Проверки по шагам\n\n" + "\n".join(rows) if rows else ""


def _load_resume_plan(state: ask_state.AskState, question: str) -> tuple[dict, str]:
    if any(link_escapes(str(path), str(state.cwd)) for path in
           (state.plan_json, state.manifest_path, state.state_path, state.control_path)):
        return {}, ""
    plan, manifest = state.plan(), state.manifest()
    if (not isinstance(plan, dict) or not isinstance(manifest, dict)
            or plan.get("id") != state.id or manifest.get("question_id") != state.id
            or plan.get("question") != question or manifest.get("question") != question):
        return {}, ""
    steps, records = plan.get("steps"), manifest.get("steps")
    if (not isinstance(steps, list) or not steps or not isinstance(records, list)
            or not all(isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"] for s in steps)
            or not all(isinstance(s, dict) for s in records)):
        return {}, ""
    return plan, str(manifest.get("mode") or "")


def run(cfg: dict, cwd: str | Path, question: str, *, pack: str = "",
        call=None, mode: str = "ask", max_cards: int = 40, history=(),
        role: str = "worker", thread_id: str = "", keep: int = 0,
        momus: bool = True, emit=None, resume: bool = False,
        state: ask_state.AskState | None = None, enrich: bool = True,
        raptor: bool = False) -> dict:
    """Run the plan → delegate → check → synthesize pipeline.

    `pack` is optional: without it the normal `ctx_pack` search is used. `call`
    is normally `agent_core.call_role`; tests pass a deterministic stub.
    """
    if call is None and AG is not None:
        call = AG.call_role
    contract = CHECK_ROLES if call is not None and momus is True else ("critic",) if call is not None else ()
    if not str(question or "").strip():
        return {"ok": False, "status": "error", "answer": "",
                "error": _error("empty_question", "вопрос пуст", "сформулируйте вопрос")}
    settings = pipeline_config(cfg, cwd)
    state = state or ask_state.AskState(
        cwd, thread_id, question, keep=keep or settings["keep_questions"])

    def emit_ready():
        if emit:
            emit({"kind": "state_ready", "state_id": state.id})

    if resume:
        plan, saved_mode = _load_resume_plan(state, question)
        if not plan:
            error = _error("resume_state", "для этого id и вопроса нет сохранённого плана и манифеста",
                           "проверьте id и текст исходного вопроса")
            if emit:
                emit({"kind": "error", "error": error["code"], "message": error["message"]})
            return {"ok": False, "status": "error", "answer": "", "error": error}
        mode = saved_mode or mode
        saved = state.state()
        if saved.get("status") in ("error", "cancelled", "partial", "done"):
            # Old controls belong to the finished run, not the explicit retry.
            control = state.control()
            if control.get("pause") or control.get("cancel"):
                state.set_control(pause=False, cancel=False)
        emit_ready()
    started = time.time()
    total_limit, step_timeout = settings["total_seconds"], settings["step_seconds"]
    window = _RunWindow(started, started + total_limit, QB.synthesis_reserve(total_limit, step_timeout))
    issues = _RunIssues()
    _role_notices(cfg, issues)
    warnings = issues.warnings
    planner_fallback = False
    context_meta: dict = {}
    raptor_meta: dict = {}
    # A plan at this thread id may belong to the previous turn.
    plan_created = resume
    steps = []
    synthesis_seconds = 0.0
    cancellation = threading.Event()

    def cancelled():
        if not cancellation.is_set() and state.control().get("cancel"):
            cancellation.set()
        return cancellation.is_set()

    def stop(error, *, before=""):
        code = error["code"]
        is_cancel = code == "cancelled"
        status = "cancelled" if is_cancel else "error"
        for row in steps:
            row.pop("_routes", None)
        if plan_created:
            state.update_state(status=status, current="", error=error["message"],
                               timing=window.timing(synthesis_seconds))
            details = {"cancelled_before": before} if before else {}
            state.event("cancel" if is_cancel else "error", error["message"], **details)
        if emit:
            emit({"kind": status, "error": code, "message": error["message"],
                  "state_id": state.id if plan_created else "", "resumable": plan_created})
        result = {"ok": False, "status": status, "answer": "", "error": error, "steps": steps,
                  "warnings": warnings, "state_id": state.id if plan_created else "",
                  "plan_created_before_answer": plan_created}
        if plan_created:
            result.update(plan_path=_rel(state.plan_path, cwd), manifest_path=_rel(state.manifest_path, cwd))
        return result

    def deadline_error():
        return _error("deadline", "общий срок ответа истёк", "повторите запрос позже")

    if cancelled():
        return stop(_cancel_error(), before="planner")
    if resume:
        planner_fallback = saved.get("planner_fallback") is True
        checked = state.validate_resume()
        if checked.get("orphans"):
            issues.add("resume_orphans", "осиротевшие файлы шагов убраны в карантин")
    else:
        if not pack:
            if _remaining(window.answer) <= 0:
                return stop(deadline_error())
            found = AC.search_context(cwd, question, mode=mode, max_cards=max_cards,
                                      deadline=window.answer,
                                      extra_args=() if enrich else ("--no-semantic",),
                                      cfg=cfg, raptor=raptor,
                                      rerank=(cfg.get("rerank") or {}).get("enabled"),
                                      rerank_blend=(cfg.get("rerank") or {}).get("blend"))
            if cancelled():
                return stop(_cancel_error(), before="planner")
            if not found.get("ok"):
                return stop(found.get("error") or _error(
                    "search_empty", found.get("why") or "поиск не дал карточек", "уточните вопрос"))
            pack = found.get("pack") or ""
            raptor_meta = found.get("raptor") or {}
        if not _source_count(pack):
            return stop(_error("search_empty", "поиск не дал карточек", "уточните вопрос"))
        # The preliminary plan is saved before the planner call: if the planner
        # times out, the UI still has plan.md/manifest.json to retry from.
        plan = state.create(question, _decorate_plan(_normalize_plan({}, pack), pack), mode=mode)
        plan_created = True
        emit_ready()
        if cancelled():
            return stop(_cancel_error(), before="planner")
        if _remaining(window.answer) <= 0:
            return stop(deadline_error())
        pack, context_meta = _prepare_pack(pack, cfg, cwd, call, state, issues,
                                          window.answer, step_timeout, cancelled)
        if cancelled():
            return stop(_cancel_error(), before="planner")
        if context_meta.get("triggered"):
            plan = state.set_plan(
                _decorate_plan(_normalize_plan({}, pack), pack),
                mode=mode, question=question)
        if call is not None:
            plan_res = _call_limited(
                cfg, "planner", [{"role": "user", "content": _plan_prompt(question, pack)}],
                call, window.answer, step_timeout, project=str(cwd), cancelled=cancelled)
            if cancelled():
                return stop(_cancel_error(), before="worker")
            issues.route(_route_issue("planner", plan_res, cfg))
            if plan_res.get("ok") is True:
                proposed = _normalize_plan(_parse_json(plan_res.get("text") or ""), pack)
                if not proposed["planner_fallback"]:
                    plan = state.set_plan(
                        _decorate_plan(proposed, pack), mode=mode, question=question)
    # A legacy plan without either marker keeps the old interpretation (C7a).
    planner_fallback = planner_fallback or plan.get("planner_fallback") is True
    if planner_fallback:
        issues.add(warning="планировщик не дал пригодного плана: план собран по фрагментам контекста")
    state.update_state(status="running", started=started, deadline=window.end,
                       warnings=warnings, planner_fallback=planner_fallback,
                       degraded=[], notices=issues.notices, completion={})
    if emit:
        emit({"kind": "plan", "id": state.id, "title": plan.get("title"),
              "steps": [{k: v for k, v in step.items() if k != "pack"}
                        for step in plan.get("steps") or []],
              "plan_path": _rel(state.plan_path, cwd),
              "manifest_path": _rel(state.manifest_path, cwd),
              "warnings": list(warnings)})
    # A resumed run keeps already valid steps; a fresh run starts with pending.
    done = {s.get("plan_item_id"): s for s in state.manifest().get("steps") or []
            if s.get("status") in ANSWERED and s.get("sha256")}
    pending = []
    for step in plan.get("steps") or []:
        if (record := done.get(step["id"])) is not None:
            steps.append({"id": step["id"], "status": record["status"],
                          "seconds": float(record.get("seconds") or 0),
                          "model": record.get("model", ""), "provider": record.get("provider", "")})
        else:
            pending.append(step)
    slots = _slots(cfg, len(pending), settings)
    if AG is not None and slots:
        try:
            AG.pulse_threads(cfg, slots)     # шапка панели: ширина пула этого вопроса
        except Exception:  # noqa: BLE001 - пульс не должен ронять ответ
            pass
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(slots) or 1),
                                thread_name_prefix="ask-step") as pool:
            futs = {
                pool.submit(_run_step, state, cfg, question, step, call, window,
                            step_timeout, slots[i % len(slots)] if slots else 1,
                            list(history), contract, emit, cancelled): step
                for i, step in enumerate(pending)
            }
            for fut in as_completed(futs):
                step = futs[fut]
                try:
                    steps.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - one bad step must not kill the run
                    error = {"code": "step_failed", "message": str(exc) or type(exc).__name__}
                    state.mark_step(step["id"], status="failed", error=error)
                    steps.append({"id": step["id"], "title": step.get("title"),
                                  "status": "failed", "answer": "", "error": error})
    except Exception as exc:  # noqa: BLE001
        issues.add("parallel_stopped", f"параллельная обработка остановлена: {type(exc).__name__}")
    if cancelled():
        return stop(_cancel_error(), before="synthesis")
    steps = normalize_steps(steps, plan, contract, state, issues)
    completion = completion_of(steps)
    if planner_fallback:
        issues.add("planner_fallback", degraded=True)
    synth_started = time.time()
    answer, synth_fallback = _synthesize(cfg, question, plan, steps, call, window.end,
                                         step_timeout, list(history), state, issues, emit, cancelled)
    synthesis_seconds = time.time() - synth_started
    if cancelled():
        return stop(_cancel_error(), before="delivery")
    if not answer:
        return stop(deadline_error() if _remaining(window.answer) <= 0 else _error(
            "no_answer", "ни один пункт не дал результата", "уточните вопрос или повторите"))
    api_status, ui_status = map_status(full_status(steps, synth_fallback, planner_fallback, issues.degraded))
    summary = {"completion": completion, "degraded": issues.degraded, "notices": issues.notices}
    state.write_answer(answer, status=ui_status, completion=completion)
    total = round(time.time() - started, 2)
    metrics = {
        "total_seconds": round(time.time() - started, 2),
        "step_seconds": {s["id"]: round(float(s.get("seconds") or 0), 2) for s in steps},
        "success_rate": round(sum(1 for s in steps if s.get("status") in ("done", "partial"))
                              / max(1, len(steps)), 3),
        "errors": len([s for s in steps if s.get("status") == "failed"]),
        "retries": sum(1 for w in warnings if "фоллбэк" in w or "запас" in w),
        "context_tokens": int(len(pack) / 3.5) if pack else 0,
        "providers": sorted({str(s.get("provider")) for s in steps if s.get("provider")}),
        "models": sorted({str(s.get("model")) for s in steps if s.get("model")}),
        "pool": {"width": len(slots), "slots": list(slots)},
        "long": is_long(time.time() - started),
        "project_context": bool(enrich),
        "context": context_meta,
        "raptor": raptor_meta,
    }
    timing = window.timing(synthesis_seconds)
    state.update_state(status=ui_status, current="", metrics=metrics, warnings=warnings, timing=timing, **summary)
    state.event("answer", "Пайплайн завершён", status=ui_status,
                seconds=timing["answer_seconds"], warnings=len(warnings), **timing)
    if emit:
        emit({"kind": "done", "status": ui_status,
              "answer": answer, "state_id": state.id,
              "answer_path": _rel(state.answer_path, cwd), "metrics": metrics,
              "warnings": warnings, "steps": steps, "raptor": raptor_meta, **summary})
    return {
        "ok": True, "status": api_status,
        "answer": answer, "answer_path": _rel(state.answer_path, cwd),
        "plan_path": _rel(state.plan_path, cwd),
        "manifest_path": _rel(state.manifest_path, cwd),
        "state_path": _rel(state.state_path, cwd), "state_id": state.id,
        "plan": state.plan().get("steps") or [], "steps": steps, "warnings": warnings,
        "metrics": metrics, "plan_created_before_answer": plan_created,
        "mode": mode, "question": question, "seconds": total, "raptor": raptor_meta, **summary,
    }


def _rel(path: Path, cwd: str | Path) -> str:
    try:
        return str(Path(path).relative_to(Path(cwd)))
    except ValueError:
        return str(path)


def _slots(cfg: dict, count: int, settings: dict) -> list[int]:
    slots = []
    if AG is not None:
        try:
            slots = list(AG.pool(cfg, "worker") or [])
        except Exception:  # noqa: BLE001
            slots = []
    if not slots:
        try:
            slots = [1] * max(1, int(cfg.get("parallel") or 1))
        except (TypeError, ValueError):
            slots = [1]
    width = min(len(slots), int(cfg.get("parallel") or len(slots)),
                int(settings.get("max_workers") or DEFAULT_WORKERS), max(1, int(count)))
    return slots[:max(1, width)] or [1]
