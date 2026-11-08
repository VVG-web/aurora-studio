#!/usr/bin/env python3
"""production_core.py - надстройка «Продуктивности» над ядром обоснованного ответа.

Цепочка одна и та же у панели, теста и будущей процедуры:

    поиск в базе -> grill-me -> итоговый промпт -> ответ ядра -> артефакт

Grill-me остаётся слоем диалога: он уточняет вопрос и решения, но не заменяет поиск.
Ядро получает уже собранный пак и готовый промпт. Шаблон артефакта и путь берутся из
настроек вида (`artifacts:`), а запись выполняет переданная функция движка: модуль не
знает про файловую систему проекта больше, чем ему дали.
"""
from __future__ import annotations

import time

import answer_core


def search(cwd: str, question: str, *, mode: str = "generate", max_cards: int = 40,
           runner=None, refusal=None, deadline=None, extra_args=()) -> dict:
    """Шаг 1: тот же поиск, что в «Спросить»."""
    return answer_core.search_context(cwd, question, mode=mode, max_cards=max_cards,
                                      runner=runner, refusal=refusal, deadline=deadline,
                                      extra_args=extra_args)


def grill_round(payload: dict | None, *, force: bool = False) -> dict:
    """Шаг 2: нормализовать ответ планировщика в явный результат grill-раунда.

    Grill-me задаёт один раунд вопросов с рекомендациями. Пока есть вопросы и человек
    не сказал «хватит», результат помечен `requires_input`; это не ошибка и не пустой
    ответ, а явное состояние, которое панель показывает человеку.
    """
    data = payload if isinstance(payload, dict) else {}
    questions = [q for q in (data.get("questions") or []) if isinstance(q, dict) and q.get("q")]
    plan = str(data.get("plan") or "").strip()
    assumptions = [str(a) for a in (data.get("assumptions") or []) if str(a).strip()]
    requires = bool(questions) and not force
    return {"questions": questions, "plan": "" if requires else plan,
            "assumptions": assumptions, "ready": bool(plan) and not requires,
            "requires_input": requires,
            "reason": ("нужен ответ человека на вопросы grill-me" if requires
                       else "план готов" if plan else "планировщик не вернул план")}


def build_prompt(template: str, *, title: str, plan: str, form: str, extra: str,
                 pack: str, agnostic: str = "") -> str:
    """Шаг 3: итоговый промпт к ядру. Текст шаблона передаёт вызывающий."""
    return template.format(title=title, plan=plan, template=form, extra=extra,
                           pack=pack, agnostic=agnostic)


def answer_artifact(question: str, *, pack: str, prompt: str, cfg: dict, cwd: str,
                    call, role: str = "worker", input_limit: int = 0,
                    deadline: float | None = None, policy: dict | None = None) -> dict:
    """Шаг 4: ответ ядра на готовый промпт и пак. Шаблон уже в промпте."""
    policy = dict(policy or {})
    if input_limit:
        policy["input_limit"] = input_limit
    return answer_core.answer(
        question, cfg=cfg, cwd=cwd, call=call, pack=pack, prompt=prompt,
        prepare=False, role=role, momus=False, deadline=deadline, policy=policy,
    )


def render(write, *args, **kwargs):
    """Шаг 5: вызвать движковую запись артефакта, не зная её внутренностей.

    `write` - `agent_runner.write_artifact` или тестовая функция. Ошибка записи
    превращается в понятный результат, а не в исключение наверх.
    """
    try:
        path = write(*args, **kwargs)
        return {"ok": True, "path": path, "error": None}
    except Exception as exc:  # noqa: BLE001 - контракт гарантированного ответа
        from reliable_core import human_error
        return {"ok": False, "path": "", "error": human_error(
            "artifact_write", f"не удалось записать артефакт: {type(exc).__name__}: {exc}",
            "проверьте папку результата и права на запись")}


def chain(question: str, *, search_fn, grill_fn, prompt_fn, answer_fn, render_fn,
          force: bool = False) -> dict:
    """Полная цепочка с инъекцией шагов: так её проверяют без модели и сети.

    `grill_fn` возвращает payload планировщика; `prompt_fn` получает пак и grill-итог;
    `answer_fn` - вызов ядра; `render_fn` - запись артефакта.
    """
    started = time.time()
    found = search_fn(question)
    if not found.get("ok"):
        return {"ok": False, "status": "error", "stage": "search",
                "error": found.get("error") or {"code": "search_empty",
                                                "message": found.get("why") or "поиск пуст"},
                "seconds": round(time.time() - started, 2)}
    pack = found.get("pack") or ""
    grilled = grill_round(grill_fn(pack), force=force)
    if grilled["requires_input"]:
        return {"ok": True, "status": "requires_input", "stage": "grill",
                "questions": grilled["questions"], "pack": pack,
                "seconds": round(time.time() - started, 2)}
    if not grilled["ready"]:
        return {"ok": False, "status": "error", "stage": "grill",
                "error": {"code": "grill_no_plan", "message": grilled["reason"]},
                "seconds": round(time.time() - started, 2)}
    prompt = prompt_fn(pack, grilled)
    answered = answer_fn(pack, prompt, grilled)
    if not answered.get("ok"):
        return {"ok": False, "status": "error", "stage": "answer",
                "error": answered.get("error") or {"code": "answer_failed",
                                                   "message": answered.get("why") or "нет ответа"},
                "seconds": round(time.time() - started, 2)}
    rendered = render_fn(answered["answer"], grilled, answered)
    if not rendered.get("ok"):
        return {"ok": False, "status": "error", "stage": "render",
                "error": rendered.get("error"), "seconds": round(time.time() - started, 2)}
    return {"ok": True, "status": "done", "stage": "artifact",
            "answer": answered["answer"], "path": rendered.get("path", ""),
            "grill": grilled, "seconds": round(time.time() - started, 2)}
