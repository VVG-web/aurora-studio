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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import answer_core as AC
import ask_state
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
TOTAL_CAP = 20 * 60
LONG_SECONDS = 10 * 60
DEFAULT_TOTAL = 20 * 60
DEFAULT_STEP = 300.0
DEFAULT_WORKERS = 4
MAX_STEPS = 12

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


def _plan_failure(message: str, state: ask_state.AskState | None = None,
                  warnings=None) -> dict:
    if state is not None:
        state.update_state(status="error", error=message)
        state.event("error", message)
    return {"ok": False, "status": "error", "answer": "",
            "error": _error("plan_failed", message, "повторите запрос или уточните вопрос"),
            "steps": [], "warnings": list(warnings or []),
            "state_id": state.id if state else "", "plan_created_before_answer": bool(state)}


def _call_limited(cfg: dict, role: str, messages: list, call, deadline: float,
                  timeout: float, prefer: int = 0, history=None,
                  project: str = "", input_limit: int = 0) -> dict:
    """Call the model with a bounded outer timeout.

    `agent_core` already has the retry ring and provider timeouts. This outer
    bound protects the pipeline when a transport ignores its own deadline.
    """
    left = min(float(timeout), _remaining(deadline))
    if left <= 0:
        return {"ok": False, "timed_out": True, "error": _error(
            "deadline", "общий срок ответа истёк", "повторите запрос позже")}
    kw = {"deadline": min(deadline, time.time() + left), "prefer": prefer or 0}
    if history:
        kw["history"] = list(history)
    if project:
        kw["project"] = project
    if input_limit:
        kw["input_limit"] = int(input_limit)

    def once():
        return call(cfg, role, messages, **kw)

    started = time.time()
    if RC is not None:
        stage = RC.run_stage(f"ask:{role}", once, deadline=deadline, timeout=left)
        if not stage.ok:
            err = stage.error or _error("stage_failed", "вызов не завершился")
            if stage.timed_out or err.get("code") == "timeout":
                return {"ok": False, "timed_out": True,
                        "error": _error("deadline", "срок шага истёк",
                                        "сократите запрос или повторите позже")}
            return {"ok": False, "error": err, "seconds": stage.seconds}
        result = stage.value
    else:
        try:
            result = once()
        except Exception as exc:  # noqa: BLE001 - pipeline contract
            return {"ok": False, "error": _error("llm_failed", str(exc)),
                    "seconds": round(time.time() - started, 2)}
    if not isinstance(result, dict):
        return {"ok": False, "error": _error("llm_invalid", "модель ответила не объектом")}
    result = dict(result)
    result.setdefault("seconds", round(time.time() - started, 2))
    return result


def _role_chain(cfg: dict, role: str) -> list:
    if AG is not None:
        try:
            return list(AG.role_chain(cfg, role) or [])
        except Exception:  # noqa: BLE001
            return []
    return []


def _role_warnings(cfg: dict) -> list[str]:
    warnings = []
    chains = ((cfg or {}).get("chains") or {}).get("llm") or {}
    for role in ("worker", "critic", "qa", "planner"):
        chain = chains.get(role)
        if not chain:
            warnings.append(f"роль «{role}» не настроена: используется общая цепочка")
            continue
        if not _role_chain(cfg, role):
            warnings.append(f"у роли «{role}» нет доступной модели")
    if not (cfg or {}).get("backends") and not chains:
        warnings.append("в настройке агента нет моделей: пайплайн не сможет ответить")
    return list(dict.fromkeys(warnings))


def _fallback_warning(role: str, result: dict, cfg: dict) -> str:
    if not result.get("ok"):
        return ""
    backend = result.get("backend")
    chain = _role_chain(cfg, role)
    first = chain[0].get("n") if chain else None
    log = " ".join(str(x) for x in result.get("log") or [])
    if first is not None and backend not in (None, first):
        return f"роль «{role}» ответила запасной моделью (бэкенд №{backend})"
    if backend not in (None, 1) or "№1" in log and "down" in log.lower():
        return f"роль «{role}» использовала запасную модель или кольцо фоллбэка"
    return ""


def _parse_json(text: str, default=None):
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    return default


def _normalize_plan(got, question: str, pack: str) -> dict:
    data = got if isinstance(got, dict) else {}
    steps = []
    for i, raw in enumerate((data.get("steps") or [])[:MAX_STEPS], 1):
        if not isinstance(raw, dict):
            continue
        item = {
            "id": ask_state.safe_id(raw.get("id") or f"step-{i}"),
            "title": str(raw.get("title") or f"Шаг {i}")[:160],
            "brief": str(raw.get("brief") or "")[:800],
        }
        steps.append(item)
    if not steps:
        steps = _fallback_steps(pack)
    return {"title": str(data.get("title") or "Ответ на вопрос")[:160],
            "steps": steps[:MAX_STEPS]}


def _split_pack(pack: str, count: int) -> list[str]:
    """Split a pack into at most `count` card groups, preserving each card whole."""
    cards = []
    try:
        import context_store as CS
        _preamble, parsed = CS._split_cards(pack)
        cards = [c["text"] for c in parsed]
    except Exception:  # noqa: BLE001
        cards = []
    if not cards:
        return [str(pack or "")]
    count = max(1, min(int(count or 1), len(cards)))
    groups = [[] for _ in range(count)]
    for i, card in enumerate(cards):
        groups[i % count].append(card)
    return ["# Пак\n\n" + "\n\n".join(g) for g in groups if g] or [str(pack or "")]


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
                  warnings: list) -> tuple[str, dict]:
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
        compressor = CR.compressor_callable(call, cfg)
        if compressor is None:
            warnings.append("роль «compressor» не настроена: сжатие экстрактивное, "
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
        warnings.append(f"сжатие контекста не выполнено: {type(exc).__name__}")
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


def _verdict(text: str) -> dict:
    raw = str(text or "")
    m = re.search(r"ВЕРДИКТ:\s*(ЧИСТО|БЕЗ\s+ОПОРЫ\s+(\d+))", raw, re.IGNORECASE)
    if not m:
        return {"ok": False, "clean": False, "unsupported": 0,
                "why": "Момус не дал вердикт"}
    n = int(m.group(2) or 0)
    return {"ok": True, "clean": m.group(1).upper() == "ЧИСТО", "unsupported": n,
            "text": raw}


def _step_metadata(result: dict) -> tuple[str, str]:
    model = str(result.get("model") or "")
    provider = str(result.get("provider") or result.get("backend") or "")
    return model, provider


def _run_step(state: ask_state.AskState, cfg: dict, question: str, step: dict,
              call, deadline: float, step_timeout: float, slot: int,
              history, momus: bool, warnings: list, warn_lock: threading.Lock,
              emit) -> dict:
    item = step["id"]
    state.mark_step(item, status="running", started=ask_state.utc_now(), attempts=1)
    state.update_state(status="running", current=item)
    if emit:
        emit({"kind": "step_start", "step": item, "title": step.get("title"),
              "role": "worker", "slot": slot})
    started = time.time()
    # A cooperative pause/cancel check happens before each expensive call.
    control = state.control()
    if control.get("cancel"):
        state.mark_step(item, status="cancelled", error="отменено пользователем")
        return {"id": item, "status": "cancelled", "answer": "", "error": "cancel"}
    while state.control().get("pause"):
        state.update_state(status="paused", current=item)
        if emit:
            emit({"kind": "paused", "step": item})
        if _remaining(deadline) <= 0:
            state.mark_step(item, status="failed", error="срок истёк во время паузы")
            return {"id": item, "status": "failed", "answer": "",
                    "error": "deadline"}
        time.sleep(0.2)
    res = _call_limited(cfg, "worker", [{"role": "user",
                        "content": _step_prompt(question, step)}], call, deadline,
                        step_timeout, prefer=slot, history=history, project=state.cwd)
    seconds = round(time.time() - started, 2)
    return _finish_step(state, cfg, question, step, call, deadline, step_timeout, slot,
                       history, momus, warnings, warn_lock, emit, res, started, seconds)


def _finish_step(state: ask_state.AskState, cfg: dict, question: str, step: dict,
                 call, deadline: float, step_timeout: float, slot: int,
                 history, momus: bool, warnings: list, warn_lock: threading.Lock,
                 emit, writer: dict, started: float, seconds: float) -> dict:
    item = step["id"]
    if not writer.get("ok"):
        err = writer.get("error") or _error("llm_failed", "писатель не ответил")
        reason = err.get("message") or err.get("reason") or "ошибка модели"
        state.write_step(item, f"# {step.get('title')}\n\n## Ошибка\n\n{reason}\n",
                         role="worker", status="failed", seconds=seconds,
                         error=reason, attempts=1)
        state.mark_step(item, status="failed", error=err.get("code") or "llm_failed")
        with warn_lock:
            warnings.append(f"пункт «{step.get('title')}»: {err.get('code') or 'ошибка'}")
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
                     status="done", seconds=seconds, attempts=1)
    fallback = _fallback_warning("worker", writer, cfg)
    if fallback:
        with warn_lock:
            warnings.append(fallback)
    if emit:
        emit({"kind": "step_done", "step": item, "role": "worker",
              "model": model, "provider": provider, "seconds": seconds,
              "tokens": len(step.get("pack") or "") // 3,
              "preview": answer[:400]})
    checks = []
    if call is not None:
        # Critic and Momus get the writer's result and the same slice, so a
        # failure in one step cannot hide behind a clean final answer.
        critic = _call_limited(cfg, "critic", [{"role": "user",
                           "content": _critic_prompt(question, step, writer.get("text") or "")}],
                               call, deadline, step_timeout, prefer=slot,
                               project=state.cwd)
        cstatus = "done" if critic.get("ok") else "failed"
        state.write_step(item, f"# Критик · {step.get('title')}\n\n"
                               f"{critic.get('text') or (critic.get('error') or {}).get('message', '')}\n",
                         role="critic", model=_step_metadata(critic)[0],
                         provider=_step_metadata(critic)[1], status=cstatus,
                         seconds=float(critic.get("seconds") or 0), check=True,
                         error="" if critic.get("ok") else str(critic.get("error") or ""))
        checks.append({"role": "critic", "ok": bool(critic.get("ok")),
                       "clean": bool(critic.get("ok") and critic_result(call_result=critic)),
                       "note": str((critic.get("error") or {}).get("reason") or "")[:300]})
        if emit:
            emit({"kind": "check", "step": item, "role": "critic",
                  "status": "done" if critic.get("ok") else "failed"})
        if momus:
            qa = _call_limited(cfg, "qa", [{"role": "user",
                            "content": _momus_prompt(question, step, writer.get("text") or "")}],
                               call, deadline, step_timeout, prefer=slot, project=state.cwd)
            qa_text = str(qa.get("text") or "") if qa.get("ok") else ""
            state.write_step(item, f"# Момус · {step.get('title')}\n\n"
                                   f"{qa_text or (qa.get('error') or {}).get('message', '')}\n",
                             role="qa", model=_step_metadata(qa)[0],
                             provider=_step_metadata(qa)[1],
                             status="done" if qa.get("ok") else "failed",
                             seconds=float(qa.get("seconds") or 0), check=True,
                             error="" if qa.get("ok") else str(qa.get("error") or ""))
            verdict = _verdict(qa_text) if qa.get("ok") else {"ok": False, "clean": False}
            checks.append({"role": "qa", **verdict})
            if emit:
                emit({"kind": "check", "step": item, "role": "qa",
                      "status": "clean" if verdict.get("clean") else "unproven"})
            if not qa.get("ok"):
                with warn_lock:
                    warnings.append(f"пункт «{step.get('title')}»: Момус не проверил ответ")
    status = "partial" if any(not c.get("ok") or c.get("clean") is False for c in checks if c) else "done"
    state.mark_step(item, status=status, checks=_manifest_step(state, item).get("checks", []))
    return {"id": item, "status": status, "answer": answer, "model": model,
            "provider": provider, "seconds": seconds, "checks": checks,
            "warnings": [fallback] if fallback else []}


def critic_result(call_result: dict) -> bool:
    data = _parse_json(str(call_result.get("text") or ""), {}) or {}
    return bool(data.get("ok", False))


def _manifest_step(state: ask_state.AskState, item: str) -> dict:
    for step in state.manifest().get("steps") or []:
        if step.get("plan_item_id") == item:
            return step
    return {}


def _synthesize(cfg: dict, question: str, plan: dict, steps: list, call,
                deadline: float, step_timeout: float, history, state,
                warnings: list, warn_lock: threading.Lock, emit) -> tuple[str, str]:
    ok_steps = [s for s in steps if s.get("status") in ("done", "partial") and s.get("answer")]
    plan_text = "\n".join(f"{i}. {s.get('title')}" for i, s in enumerate(plan.get("steps") or [], 1))
    step_text = "\n\n".join(f"## {s.get('id')} · {s.get('title')}\n{s.get('answer')}"
                            for s in ok_steps)
    failed = [s for s in steps if s.get("status") not in ("done", "partial")]
    if not ok_steps:
        return "", "partial"
    prompt = PROMPT_SYNTH.format(question=question, plan=plan_text, steps=step_text)
    res = _call_limited(cfg, "planner", [{"role": "user", "content": prompt}],
                        call, deadline, step_timeout, history=history,
                        project=state.cwd)
    if res.get("ok") and str(res.get("text") or "").strip():
        answer = str(res["text"]).strip()
        fallback = _fallback_warning("planner", res, cfg)
        if fallback:
            with warn_lock:
                warnings.append(fallback)
        if emit:
            emit({"kind": "synth", "status": "done", "model": _step_metadata(res)[0]})
    else:
        answer = "\n\n".join(f"### {s.get('title')}\n\n{s.get('answer')}" for s in ok_steps)
        with warn_lock:
            warnings.append("итоговый агент не ответил: итог собран из результатов шагов")
        if emit:
            emit({"kind": "synth", "status": "fallback"})
    if failed:
        answer += "\n\n## Частичный результат\n\n" + "\n".join(
            f"- {s.get('title')}: {((s.get('error') or {}).get('message') or s.get('status'))}"
            for s in failed)
    answer += _checks_section(steps)
    return answer, "partial" if failed else "full"


def _checks_section(steps: list) -> str:
    """Marks each plan item's Writer/Critic/Momus outcome under the final answer."""
    rows = []
    for step in steps:
        marks = []
        for check in step.get("checks") or []:
            role = str(check.get("role") or "")
            if role == "critic":
                marks.append("Критик: " + ("опора" if check.get("clean") else "замечания"))
            elif role == "qa":
                if not check.get("ok"):
                    marks.append("Момус: без вердикта")
                elif check.get("clean"):
                    marks.append("Момус: чисто")
                else:
                    marks.append(f"Момус: без опоры {int(check.get('unsupported') or 0)}")
        rows.append(f"- {step.get('title') or step.get('id')}: "
                    + (", ".join(marks) if marks else "проверок нет"))
    return "\n\n## Проверки по шагам\n\n" + "\n".join(rows) if rows else ""


def run(cfg: dict, cwd: str | Path, question: str, *, pack: str = "",
        call=None, mode: str = "ask", max_cards: int = 40, history=(),
        role: str = "worker", thread_id: str = "", keep: int = 0,
        momus: bool = True, emit=None, resume: bool = False,
        state: ask_state.AskState | None = None, enrich: bool = True) -> dict:
    """Run the plan → delegate → check → synthesize pipeline.

    `pack` is optional: without it the normal `ctx_pack` search is used. `call`
    is normally `agent_core.call_role`; tests pass a deterministic stub.
    """
    if call is None and AG is not None:
        call = AG.call_role
    if not str(question or "").strip():
        return {"ok": False, "status": "error", "answer": "",
                "error": _error("empty_question", "вопрос пуст", "сформулируйте вопрос")}
    settings = pipeline_config(cfg, cwd)
    state = state or ask_state.AskState(
        cwd, thread_id, question, keep=keep or settings["keep_questions"])
    started = time.time()
    deadline = started + settings["total_seconds"]
    warnings = _role_warnings(cfg)
    warn_lock = threading.Lock()
    context_meta: dict = {}
    if resume and state.dir.is_dir():
        checked = state.validate_resume()
        if checked.get("orphans"):
            warnings.append("осиротевшие файлы шагов убраны в карантин")
        plan = state.plan()
        if not plan.get("steps"):
            plan = _decorate_plan(_normalize_plan({}, question, pack), pack)
            state.create(question, plan, mode=mode)
    else:
        if not pack:
            found = AC.search_context(cwd, question, mode=mode, max_cards=max_cards,
                                       deadline=deadline,
                                       extra_args=() if enrich else ("--no-semantic",))
            if not found.get("ok"):
                message = found.get("why") or "поиск не дал карточек"
                if emit:
                    emit({"kind": "error",
                          "error": (found.get("error") or {}).get("code", "search_empty"),
                          "message": message, "state_id": ""})
                return {**_plan_failure(message, None, warnings),
                        "error": found.get("error") or _error("search_empty", message,
                                                              "уточните вопрос")}
            pack = found["pack"]
        # The preliminary plan is saved before the planner call: if the planner
        # times out, the UI still has plan.md/manifest.json to retry from.
        plan = _decorate_plan(_normalize_plan({}, question, pack), pack)
        state.create(question, plan, mode=mode)
        pack, context_meta = _prepare_pack(pack, cfg, cwd, call, state, warnings)
        if context_meta.get("triggered"):
            plan = state.set_plan(
                _decorate_plan(_normalize_plan({}, question, pack), pack),
                mode=mode, question=question)
        if call is not None:
            plan_res = _call_limited(
                cfg, "planner", [{"role": "user", "content": _plan_prompt(question, pack)}],
                call, deadline, settings["step_seconds"], project=str(cwd))
            if plan_res.get("ok"):
                parsed = _parse_json(plan_res.get("text") or "", None)
                if parsed:
                    plan = state.set_plan(
                        _decorate_plan(_normalize_plan(parsed, question, pack), pack),
                        mode=mode, question=question)
                fallback = _fallback_warning("planner", plan_res, cfg)
                if fallback:
                    warnings.append(fallback)
            else:
                warnings.append("планировщик не ответил: план собран по фрагментам контекста")
            if _remaining(deadline) <= 0:
                if emit:
                    emit({"kind": "error", "error": "deadline",
                          "message": "общий срок ответа истёк", "state_id": state.id})
                return {**_plan_failure("общий срок ответа истёк до первого шага", state, warnings),
                        "error": _error("deadline", "общий срок ответа истёк",
                                        "повторите запрос позже")}
    plan_created = True
    state.update_state(status="running", started=started, deadline=deadline,
                       warnings=warnings)
    if emit:
        emit({"kind": "plan", "id": state.id, "title": plan.get("title"),
              "steps": [{k: v for k, v in step.items() if k != "pack"}
                        for step in plan.get("steps") or []],
              "plan_path": _rel(state.plan_path, cwd),
              "manifest_path": _rel(state.manifest_path, cwd),
              "warnings": warnings})
    # A resumed run keeps already valid steps; a fresh run starts with pending.
    done = {s.get("plan_item_id"): s for s in state.manifest().get("steps") or []
            if s.get("status") in ("done", "partial") and s.get("sha256")}
    steps = []
    pending = []
    for step in plan.get("steps") or []:
        if step["id"] in done:
            steps.append({"id": step["id"], "title": step.get("title"),
                          "status": done[step["id"]].get("status", "done"),
                          "answer": _read_step(state, step["id"]),
                          "seconds": float(done[step["id"]].get("seconds") or 0),
                          "checks": done[step["id"]].get("checks") or []})
        else:
            pending.append(step)
    slots = _slots(cfg, len(pending), settings)
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(slots) or 1),
                                thread_name_prefix="ask-step") as pool:
            futs = {
                pool.submit(_run_step, state, cfg, question, step, call, deadline,
                            settings["step_seconds"], slots[i % len(slots)] if slots else 1,
                            list(history), bool(momus), warnings, warn_lock, emit): step
                for i, step in enumerate(pending)
            }
            for fut in as_completed(futs):
                step = futs[fut]
                try:
                    steps.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - one bad step must not kill the run
                    with warn_lock:
                        warnings.append(f"пункт «{step.get('title')}»: {type(exc).__name__}")
                    state.mark_step(step["id"], status="failed", error=str(exc))
                    steps.append({"id": step["id"], "title": step.get("title"),
                                  "status": "failed", "answer": "", "error":
                                  {"code": "step_failed", "message": str(exc)}})
    except Exception as exc:  # noqa: BLE001
        with warn_lock:
            warnings.append(f"параллельная обработка остановлена: {type(exc).__name__}")
    if state.control().get("cancel"):
        state.update_state(status="cancelled", current="")
        state.event("cancel", "вопрос отменён пользователем")
        if emit:
            emit({"kind": "cancelled", "error": "cancelled",
                  "message": "вопрос отменён пользователем", "state_id": state.id})
        return {"ok": False, "status": "cancelled", "answer": "", "steps": steps,
                "warnings": warnings, "state_id": state.id,
                "plan_path": _rel(state.plan_path, cwd),
                "manifest_path": _rel(state.manifest_path, cwd),
                "plan_created_before_answer": plan_created,
                "error": _error("cancelled", "вопрос отменён", "запустите новый вопрос")}
    if _remaining(deadline) <= 0:
        state.update_state(status="error", current="")
        state.event("error", "общий срок ответа истёк")
        if emit:
            emit({"kind": "error", "error": "deadline",
                  "message": "общий срок ответа истёк, 20 минут", "state_id": state.id})
        return {"ok": False, "status": "error", "answer": "", "steps": steps,
                "warnings": warnings, "state_id": state.id,
                "plan_path": _rel(state.plan_path, cwd),
                "manifest_path": _rel(state.manifest_path, cwd),
                "plan_created_before_answer": plan_created,
                "error": _error("deadline", "общий срок ответа истёк, 20 минут",
                                "повторите запрос позже или обратитесь в поддержку")}
    answer, _ = _synthesize(cfg, question, plan, steps, call, deadline,
                            settings["step_seconds"], list(history),
                            state, warnings, warn_lock, emit)
    if not answer:
        state.update_state(status="error", current="", error="нет результатов шагов")
        if emit:
            emit({"kind": "error", "message": "ни один пункт не дал результата",
                  "error": "no_answer", "state_id": state.id})
        return {"ok": False, "status": "error", "answer": "", "steps": steps,
                "warnings": warnings, "state_id": state.id,
                "plan_path": _rel(state.plan_path, cwd),
                "manifest_path": _rel(state.manifest_path, cwd),
                "plan_created_before_answer": plan_created,
                "error": _error("no_answer", "ни один пункт не дал результата",
                                "уточните вопрос или повторите")}
    state.write_answer(answer, status="done" if not warnings else "partial")
    total = round(time.time() - started, 2)
    failed = [s for s in steps if s.get("status") not in ("done", "partial")]
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
    }
    state.update_state(status="done" if not warnings else "partial", current="",
                       metrics=metrics, warnings=warnings)
    state.event("answer", "Пайплайн завершён", status="done" if not warnings else "partial",
                seconds=settings["total_seconds"], warnings=len(warnings))
    if emit:
        emit({"kind": "done", "status": "done" if not warnings else "partial",
              "answer": answer, "state_id": state.id,
              "answer_path": _rel(state.answer_path, cwd), "metrics": metrics,
              "warnings": warnings, "steps": steps})
    return {
        "ok": True, "status": "partial" if (warnings or failed) else "full",
        "answer": answer, "answer_path": _rel(state.answer_path, cwd),
        "plan_path": _rel(state.plan_path, cwd),
        "manifest_path": _rel(state.manifest_path, cwd),
        "state_path": _rel(state.state_path, cwd), "state_id": state.id,
        "plan": plan.get("steps") or [], "steps": steps, "warnings": warnings,
        "metrics": metrics, "plan_created_before_answer": plan_created,
        "mode": mode, "question": question, "seconds": total,
    }


def _read_step(state: ask_state.AskState, item: str) -> str:
    path = state.step_path(item)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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
