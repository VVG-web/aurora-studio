#!/usr/bin/env python3
"""answer_core.py - переиспользуемое ядро обоснованного ответа Авроры.

Ядро не знает, откуда пришёл контекст. На вход ему дают вопрос, готовый пак карточек
или функцию поиска, набор политик и функцию вызова модели. На выходе - один контракт:

    полный ответ с цитатами;
    частичный ответ с явной пометкой, что не удалось;
    понятная ошибка с причиной и следующим шагом.

«Спросить» использует ядро для поиска, вызова и механических проверок. «Продуктивность»
передаёт в него пак, собранный для документа, и получает ответ для шаблона. Промпт
остаётся у вызывающего: у вопроса и у документа он разный.

Модуль не ходит в сеть сам: вызов модели и поиск передаются функциями. Это позволяет
проверять ядро без модели и не дублировать сборку контекста в каждой процедуре.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import card_types as CT
import fidelity as FID
from aurora_common import aliases, body as body_of, fold, frontmatter, head_text, link_escapes

try:
    from reliable_core import (deadline_from, human_error, merge_degraded, remaining,
                               run_stage, stage_timeout)
except ImportError:  # старая установка без нового модуля: ядро всё равно должно работать
    def human_error(code: str, reason: str, action: str = "") -> dict:
        text = f"Не удалось получить результат, потому что {reason or 'причина не названа'}."
        if action:
            text += f" Что можно сделать: {action}"
        return {"code": code, "reason": reason, "message": text, "next_step": action}

    def merge_degraded(*values) -> list:
        out = []
        for value in values:
            for item in value or []:
                if item and item not in out:
                    out.append(item)
        return out

    def deadline_from(cfg: dict, default: float = 900.0) -> float:
        return time.time() + max(1.0, float((cfg or {}).get("request_timeout") or default))

    def remaining(deadline, now=None):
        return float("inf") if not deadline else max(0.0, deadline - (
            time.time() if now is None else now))

    def stage_timeout(deadline, default=300.0, now=None):
        left = remaining(deadline, now)
        return max(0.1, default if left == float("inf") else min(default, left))

    def run_stage(name, fn, *, deadline=None, timeout=300.0, retries=0, **_kw):
        try:
            return type("Stage", (), {"ok": True, "value": fn(), "error": None})()
        except Exception as exc:  # noqa: BLE001
            return type("Stage", (), {"ok": False, "value": None,
                                      "error": human_error("stage_failed", str(exc))})()


CITED_SECTION = "Documents"
QUOTE_RE = re.compile(r"«(?P<q>[^»]{25,}?)»\s*\(?\s*(?:\[[^\]]*\]\s*)*\[\[(?P<card>[^\]|#\n]+)", re.S)
ELLIPSIS_RE = re.compile(r"\[(?:\u2026|\.\.\.)\]")


def _run_script(cwd: str, script: str, args: list, timeout: int = 300) -> dict:
    """Запустить скрипт движка без зависимости от agent_runner."""
    path = Path(cwd) / ".aurora" / "scripts" / script
    if not path.is_file():
        path = Path(__file__).resolve().parent / script
    try:
        p = subprocess.run([sys.executable, str(path), *args], cwd=cwd,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(
            "utf-8", "replace")
        return {"ok": False, "rc": None, "out": out.strip(),
                "why": f"{script} не уложился в {timeout} с", "timeout": True}
    except OSError as exc:
        return {"ok": False, "rc": None, "out": "",
                "why": f"{script} не запустился: {type(exc).__name__}: {exc}"}
    return {"ok": p.returncode == 0, "rc": p.returncode,
            "out": ((p.stdout or "") + (p.stderr or "")).strip()}


def _refusal(out: str, fallback: str = "поиск не дал карточек") -> str:
    """Причина отказа поиска: последняя содержательная строка, а не пустая тишина."""
    text = " ".join(str(out or "").split())
    return (text[-600:] if text else fallback) or fallback


def search_context(cwd: str, question: str, *, mode: str = "ask", max_cards: int = 40,
                   budget: int = 0, runner=None, refusal=None, deadline=None,
                   extra_args=()) -> dict:
    """Собрать пак карточек по вопросу. → {ok, pack, cards, total, why, seconds}.

    `runner` - совместимый с `agent_runner.run_command`; в тестах его подменяют, чтобы
    не запускать `ctx_pack.py` и не читать живую базу.
    """
    started = time.time()
    runner = runner or _run_script
    args = [question, "--mode", mode, "--max-cards", str(max_cards), "--no-log"]
    if budget:
        args += ["--budget", str(budget)]
    args += [str(x) for x in extra_args or ()]
    left = remaining(deadline)
    if left <= 0:
        return {"ok": False, "pack": "", "cards": [], "total": 0,
                "why": "общий дедлайн поиска исчерпан", "seconds": 0.0,
                "error": human_error("search_deadline", "поиск не начался: срок истёк",
                                     "повторите запрос позже")}
    timeout = int(max(1, min(300, left))) if left != float("inf") else 300
    try:
        try:
            r = runner(cwd, "ctx_pack.py", args, timeout=timeout)
        except TypeError:
            # Старые тестовые раннеры принимают только три аргумента. Это совместимость
            # с подменой `run_command`, а не второй путь поиска.
            r = runner(cwd, "ctx_pack.py", args)
    except Exception as exc:  # noqa: BLE001 - поиск не должен ронять процедуру
        reason = f"поиск завершился ошибкой: {type(exc).__name__}: {exc}"
        return {"ok": False, "pack": "", "cards": [], "total": 0, "why": reason,
                "seconds": round(time.time() - started, 2),
                "error": human_error("search_failed", reason, "проверьте базу знаний и повторите")}
    out = str((r or {}).get("out") or "")
    if not (r or {}).get("ok") or "## " not in out:
        why = (refusal(out) if refusal else _refusal(out))
        if (r or {}).get("timeout"):
            why = str((r or {}).get("why") or why)
        return {"ok": False, "pack": "", "cards": [], "total": 0, "why": why,
                "seconds": round(time.time() - started, 2),
                "error": human_error("search_empty", why,
                                     "уточните вопрос или заведите карточку в базе")}
    cards = FID.pack_cards(out)
    m = re.search(r"карточек (\d+)", out)
    return {"ok": True, "pack": out, "cards": cards,
            "total": int(m.group(1)) if m else len(cards),
            "why": "", "seconds": round(time.time() - started, 2)}


def prepare_context(pack: str, cfg: dict, cwd: str, call, *, thread: str = "default",
                    store=None, level: str = "ask", full_cards: int = 0,
                    critic_cards: int = 0, role: str = "worker") -> dict:
    """Оглавление, рабочий набор, сжатие и лимиты для готового пака.

    Контекстный слой опционален: без `context_architecture.enabled` возвращается тот же
    пак, и вызывающая процедура работает как раньше. Ошибка хранилища не отменяет ответ:
    она попадает в `degraded` и в метрики.
    """
    out = {"ask_pack": pack, "momus_pack": pack, "context": {}, "input_limit": 0,
           "momus_limit": 0, "degraded": [], "store_path": ""}
    try:
        import context_budget as CB
        import context_runtime as CR
    except Exception as exc:  # noqa: BLE001 - старый движок без контекстного слоя
        out["context"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return out
    try:
        ctx_cfg = CR.config(cwd)
    except Exception as exc:  # noqa: BLE001
        out["context"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return out
    if not ctx_cfg.get("enabled"):
        return out
    opened = None
    try:
        if store is None:
            opened = CR.open_ask(cwd, thread)
            store = opened.__enter__()
        out["store_path"] = str(getattr(store, "path", "") or "")
        ids = store.put_pack(pack)
        ask_full = int(full_cards or ctx_cfg.get("ask_full_cards") or 10)
        critic_full = int(critic_cards or ctx_cfg.get("critic_cards") or 8)
        out["ask_pack"] = store.planner_pack(pack, max_full=ask_full)
        out["momus_pack"] = store.planner_pack(pack, max_full=critic_full)
        if (ctx_cfg.get("compression") or {}).get("enabled"):
            compact = store.maybe_compress(
                ids, CB.window(cfg), ctx_cfg,
                compressor=CR.compressor_callable(call, cfg), level=level)
            if compact and compact.get("summary"):
                out["context"]["compressed"] = True
                out["context"]["summary_tokens"] = compact.get("tokens", 0)
        out["context"].update({
            "pack_tokens": CB.estimate_tokens(pack),
            "ask_tokens": CB.estimate_tokens(out["ask_pack"]),
            "momus_tokens": CB.estimate_tokens(out["momus_pack"]),
            "level": level,
        })
        out["input_limit"] = CR.input_limit(cfg, role, ctx_cfg)
        out["momus_limit"] = CR.input_limit(cfg, "qa", ctx_cfg)
        out["context"]["input_limit"] = out["input_limit"]
    except Exception as exc:  # noqa: BLE001 - контекст не должен отменять ответ
        out["context"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        out["ask_pack"], out["momus_pack"] = pack, pack
        out["degraded"].append("context_store_failed")
    finally:
        if opened is not None:
            try:
                opened.__exit__(None, None, None)
            except Exception:  # noqa: BLE001,F841
                pass
    return out


def _model_fail_note(result: dict) -> str:
    """Понятная причина отказа модели по журналу кольца."""
    log = [str(x) for x in (result or {}).get("log") or [] if str(x).strip()]
    per = []
    for line in log:
        if line.startswith("№") and line not in per:
            per.append(line)
    tail = log[-1] if log else "причина не названа"
    return "; ".join((per[-3:] + ([tail] if tail not in per else []))[:4])[:500]


def generate(prompt: str, *, cfg: dict, cwd: str, call, role: str = "worker",
             history: list | None = None, input_limit: int = 0,
             deadline: float | None = None, max_tokens: int | None = None) -> dict:
    """Один вызов модели с нормализованным результатом; исключение не выходит наружу."""
    deadline = deadline or (time.time() + float(cfg.get("request_timeout") or 300))
    kwargs = {"deadline": deadline, "input_limit": int(input_limit or 0)}
    if history:
        kwargs["history"] = list(history)
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)
    stage = run_stage("model", lambda: call(cfg, role, [{"role": "user", "content": prompt}],
                                            **kwargs),
                      deadline=deadline, timeout=stage_timeout(
                          deadline, remaining(deadline) if deadline else
                          float(cfg.get("request_timeout") or 300)))
    if not stage.ok:
        error = stage.error or human_error("llm_failed", "вызов модели не завершился",
                                           "повторите запрос")
        if error.get("code") == "timeout":
            error = human_error("llm_timeout", error.get("reason", "модель не успела"),
                                "повторите запрос или увеличьте срок")
        return {"ok": False, "text": "", "error": error, "raw": {}}
    r = stage.value
    if not (r or {}).get("ok"):
        reason = _model_fail_note(r or {})
        return {"ok": False, "text": "", "raw": r or {}, "error": human_error(
            "llm_unavailable", reason, "проверьте связь и настройки модели, затем повторите")}
    text = str((r or {}).get("text") or "").strip()
    if not text:
        return {"ok": False, "text": "", "raw": r or {}, "error": human_error(
            "llm_empty", "модель вернула пустой ответ", "повторите запрос или смените модель")}
    return {"ok": True, "text": text, "raw": r or {}, "error": None}


def base_names(cwd: str) -> set:
    """Имена и синонимы всех карточек базы - для трёх корзин ссылок."""
    root = Path(cwd) / "AuroraKnowledgeDB"
    names = set()
    if not root.is_dir():
        return names
    for path in root.rglob("*.md"):
        if "/meta/" in path.as_posix() or path.name.startswith("_"):
            continue
        names.add(path.stem)
        names |= set(aliases(head_text(str(path))))
    return names


def cited_bodies(cwd: str, names: list) -> dict:
    """{имя карточки: тело} только для карточек типа `cited-document`."""
    out = {}
    for name in names:
        if not name or any(ch in name for ch in "/\\") or name.startswith("."):
            continue
        path = Path(cwd) / "AuroraKnowledgeDB" / CITED_SECTION / f"{name}.md"
        if not path.is_file() or link_escapes(str(path)):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if (frontmatter(text).get("type") or "").strip().strip('"') == "cited-document":
            out[name] = body_of(text)
    return out


def check_quotes(answer: str, pack_cards: dict) -> dict:
    """Проверить дословность цитат ответа; ответ не блокируется."""
    from aurora_common import fold_soft
    bodies = {fold_soft(n): (n, CT.norm_strict(b)) for n, b in (pack_cards or {}).items()}
    checked, missing = 0, []
    for m in QUOTE_RE.finditer(answer or ""):
        hit = bodies.get(fold_soft(m.group("card").strip()))
        if hit is None:
            continue
        checked += 1
        parts = [CT.norm_strict(p) for p in ELLIPSIS_RE.split(m.group("q"))]
        if not all(p in hit[1] for p in parts if p):
            missing.append({"quote": " ".join(m.group("q").split())[:240], "card": hit[0]})
    return {"checked": checked, "missing": missing}


def classify_links(text: str, cards: list, pack: str, cwd: str) -> dict:
    """Разложить ссылки ответа: в паке, в базе, выдумано."""
    from aurora_common import fold_soft
    named = {n.strip() for n in re.findall(r"\[\[([^\]|#]+)", text)}
    if not named:
        return {"outside": [], "mentioned": [], "invented": []}
    known = {fold_soft(c) for c in cards}
    issues = set(re.findall(r"\b([A-Z][A-Z0-9]+-\d+)(?![\w]|[.\-]\d)", pack))
    base = base_names(cwd)
    base_f = {fold_soft(b) for b in base}

    def by_code(name: str, names) -> bool:
        low = fold(name).strip()
        if not re.search(r"\d", low):
            return False
        for c in names:
            full = fold(c)
            if not full.startswith(low):
                continue
            rest = full[len(low):]
            if not rest or rest[0] in " _:·—" or (rest[0] in ".-" and not re.match(r"[.-]?\d", rest)):
                return True
        return False

    outside, mentioned, invented = [], [], []
    for n in sorted(named):
        if fold_soft(n) in known or n in issues or by_code(n, cards):
            continue
        if fold_soft(n) in base_f or by_code(n, base):
            outside.append(n)
        elif n.lower() in pack.lower():
            mentioned.append(n)
        else:
            invented.append(n)
    return {"outside": outside, "mentioned": mentioned, "invented": invented}


def verify(answer: str, *, pack: str, cards: list, cwd: str,
           verifier=None, momus_pack: str | None = None, question: str = "",
           deadline: float | None = None) -> dict:
    """Механические проверки ответа и необязательный Момус.

    `verifier(answer, pack, question)` возвращает результат `run_momus`. Его ошибка не
    отменяет ответ: это проверка, а не источник ответа. Она попадает в `degraded`.
    """
    links = classify_links(answer, cards, pack, cwd)
    # Цитаты проверяются только когда они есть. Но если раздел документов в базе есть,
    # `cites` должен честно сказать «проверено 0»: отчёт «Спросить» различает «цитат нет»
    # и «проверять нечего». В базе без раздела документов обход не нужен.
    has_cited = (Path(cwd) / "AuroraKnowledgeDB" / CITED_SECTION).is_dir()
    cited = cited_bodies(cwd, cards) if ("«" in (answer or "") or has_cited) else {}
    cites = {"cards": sorted(cited), **check_quotes(answer, cited)} if cited else {}
    checks = {"links": links, "cites": cites, "momus": None}
    degraded: list[str] = []
    if verifier is not None:
        stage = run_stage("verify", lambda: verifier(answer, momus_pack or pack, question),
                          deadline=deadline, timeout=stage_timeout(deadline, 240.0))
        if not stage.ok:
            checks["momus"] = {"ok": False, "timed_out": bool(stage.timed_out),
                               "why": (stage.error or {}).get("reason") or "проверка не ответила"}
            degraded.append("momus_timeout" if stage.timed_out else "momus_failed")
        else:
            mo = stage.value
            checks["momus"] = mo
            if isinstance(mo, dict) and not mo.get("ok", True):
                degraded.append("momus_timeout" if mo.get("timed_out") else "momus_failed")
    return {"links": links, "cites": cites, "momus": checks["momus"],
            "degraded": degraded}


def _error_result(error: dict, *, cards=None, seconds: float = 0.0,
                  phases: dict | None = None, total: int = 0) -> dict:
    return {"ok": False, "status": "error", "answer": "", "cards": cards or [],
            "total": total, "why": error.get("message") or error.get("reason", ""),
            "error": error, "seconds": round(seconds, 2), "phases": phases or {},
            "degraded": [], "checks": {}, "citations": {"cards": [], "missing": []}}


def answer(question: str, *, cfg: dict, cwd: str, call, prompt: str | None = None,
           pack: str | None = None, mode: str = "ask", max_cards: int = 40,
           history: list | None = None, role: str = "worker", momus: bool = True,
           verifier=None, runner=None, refusal=None, prepare: bool = True,
           context_thread: str = "default", context_store=None,
           prompt_builder=None, deadline: float | None = None,
           policy: dict | None = None) -> dict:
    """Полный путь обоснованного ответа по контракту.

    `prompt` - готовый промпт; `prompt_builder(pack, question, history)` - если промпт
    зависит от рабочего набора. `pack` - готовый контекст; без него ядро ищет само.
    """
    started = time.time()
    policy = policy or {}
    deadline = deadline or deadline_from(cfg, float(policy.get("deadline") or 900))
    phases: dict = {}
    total = 0
    cards: list = []
    if not str(question or "").strip():
        return _error_result(human_error("empty_question", "вопрос пуст",
                                         "сформулируйте вопрос"), seconds=time.time() - started)
    if remaining(deadline) <= 0:
        return _error_result(human_error("deadline", "общий срок ответа истёк",
                                         "повторите запрос позже"),
                             seconds=time.time() - started, phases={"pack": 0.0})
    if pack is None:
        found = search_context(cwd, question, mode=mode, max_cards=max_cards,
                               runner=runner, refusal=refusal, deadline=deadline)
        phases["pack"] = found.get("seconds", 0.0)
        if not found["ok"]:
            return _error_result(found.get("error") or human_error(
                "search_failed", found.get("why") or "поиск не дал результата",
                "уточните вопрос или добавьте карточку"), cards=found.get("cards"),
                seconds=time.time() - started, phases=phases)
        pack = found["pack"]
        cards, total = found["cards"], found["total"]
    else:
        cards = FID.pack_cards(pack)
        total = len(cards)
        phases["pack"] = 0.0
    if prepare:
        prepared = prepare_context(pack, cfg, cwd, call, thread=context_thread,
                                   store=context_store, level="ask", role=role)
        ask_pack = prepared["ask_pack"]
        momus_pack = prepared["momus_pack"]
        ctx_metrics = prepared.get("context") or {}
        input_limit = int(prepared.get("input_limit") or 0)
        degraded = list(prepared.get("degraded") or [])
    else:
        ask_pack, momus_pack = pack, pack
        ctx_metrics, input_limit, degraded = {}, 0, []
    if prompt is None:
        if prompt_builder is None:
            return _error_result(human_error("prompt_missing", "не задан промпт ответа",
                                             "передайте prompt или prompt_builder"),
                                 cards=cards, total=total, seconds=time.time() - started,
                                 phases=phases)
        try:
            prompt = prompt_builder(ask_pack, question, history or [])
        except Exception as exc:  # noqa: BLE001
            return _error_result(human_error("prompt_failed", str(exc),
                                             "проверьте шаблон и повторите"),
                                 cards=cards, total=total, seconds=time.time() - started,
                                 phases=phases)
    limit = int(policy.get("input_limit") or input_limit or 0)
    gen = generate(prompt, cfg=cfg, cwd=cwd, call=call, role=role,
                   history=history, input_limit=limit, deadline=deadline,
                   max_tokens=policy.get("max_tokens"))
    phases["answer"] = round(float((gen.get("raw") or {}).get("seconds") or 0), 2)
    fallback_used = False
    if not gen["ok"] and history and not policy.get("no_history_fallback"):
        # История может быть причиной отказа по размеру или формату. Один повтор без
        # неё ограничен: процедура не имеет права крутить дорогие вызовы без конца.
        gen = generate(prompt, cfg=cfg, cwd=cwd, call=call, role=role,
                       history=None, input_limit=limit, deadline=deadline,
                       max_tokens=policy.get("max_tokens"))
        fallback_used = bool(gen.get("ok"))
        if fallback_used:
            degraded = merge_degraded(degraded, ["fallback_without_history"])
    if not gen["ok"]:
        return _error_result(gen["error"], cards=cards, total=total,
                             seconds=time.time() - started, phases=phases)
    a = gen["raw"]
    text = gen["text"]
    checks = verify(text, pack=pack, cards=cards, cwd=cwd,
                    verifier=(verifier if momus else None), momus_pack=momus_pack,
                    question=question, deadline=deadline)
    degraded = merge_degraded(degraded, checks.get("degraded"))
    links = checks["links"]
    out = {"ok": True, "status": "partial" if degraded else "full",
           "answer": text, "cards": cards, "total": total,
           "ghosts": links["invented"], "mentioned": links["mentioned"],
           "outside": links["outside"], "backend": (a or {}).get("backend"),
           "model": (a or {}).get("model"), "checks": checks,
           "citations": {"cards": sorted((checks.get("cites") or {}).get("cards") or []),
                         "missing": (checks.get("cites") or {}).get("missing") or [],
                         "checked": (checks.get("cites") or {}).get("checked", 0)},
           "degraded": degraded, "context": ctx_metrics,
           "fallback": {"without_history": fallback_used},
           "phases": dict(phases, prompt_tokens=int((a or {}).get("tokens_in") or 0)
                          or int(len(prompt) / 3.5),
                          answer_tokens=int((a or {}).get("tokens_out") or 0),
                          momus=round(float(((checks.get("momus") or {}).get("seconds") or 0)), 2)),
           "seconds": round(time.time() - started, 2)}
    if checks.get("cites"):
        out["cites"] = checks["cites"]
    if checks.get("momus"):
        out["momus"] = checks["momus"]
    import ctx_pack as CP
    if re.search(r"^> ⚠️ " + re.escape(CP.WEAK_MARK), pack, re.M):
        out["weak_basis"] = True
    if ctx_metrics:
        out["context"] = ctx_metrics
    if degraded:
        out["degraded_note"] = ("Ответ получен в упрощённом режиме: "
                                + ", ".join(degraded))
    return out


def ask(cwd: str, question: str, *, cfg: dict, call, prompt: str,
        mode: str = "ask", max_cards: int = 40, history: list | None = None,
        role: str = "worker", momus: bool = True, verifier=None, runner=None,
        refusal=None, context_thread: str = "default", deadline: float | None = None,
        policy: dict | None = None) -> dict:
    """Удобная обёртка «Спросить»: поиск + готовый пак + промпт."""
    return answer(question, cfg=cfg, cwd=cwd, call=call, prompt=prompt, mode=mode,
                  max_cards=max_cards, history=history, role=role, momus=momus,
                  verifier=verifier, runner=runner, refusal=refusal,
                  context_thread=context_thread, deadline=deadline, policy=policy)
