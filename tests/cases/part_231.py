#!/usr/bin/env python3
"""part_231 - «Спросить» как воспроизводимый пайплайн, а не один вызов модели.

Ниже контракт, который до этой правки отсутствовал: план до первого ответа,
манифест с хешем каждого шага, роли на каждом куске, двадцать минут на весь
вопрос, защита цитат и норм при сжатии, восстановление и лимит хранимых
вопросов. Тесты работают на временном проекте и подменённой модели.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import KIT, card, make_project, test

sys.path.insert(0, str(KIT / "scripts"))


def _project(tmp: Path) -> Path:
    root = make_project(tmp)
    for i in range(4):
        card(root, f"Concepts/Заявка-{i}.md",
             f"Заявка {i} проходит согласование. Срок {10 + i} дней. "
             f"Норма: приказ №{100 + i}, цитата «срок не сокращается».",
             status="knowledge", kind="knowledge")
    return root


def _cfg(seconds: float = 1200.0, parallel: int = 2) -> dict:
    return {
        "request_timeout": 60,
        "parallel": parallel,
        "backends": [
            {"n": 1, "provider": "p1", "url": "u1", "key": "k", "model": "m1",
             "models": {"worker": "m1", "critic": "m1", "qa": "m1", "planner": "m1"},
             "context": 100000, "width": 2, "parallel": True, "chat": True},
            {"n": 2, "provider": "p2", "url": "u2", "key": "k", "model": "m2",
             "models": {"worker": "m2", "critic": "m2", "qa": "m2", "planner": "m2"},
             "context": 100000, "width": 2, "parallel": True, "chat": True},
        ],
        "chains": {"llm": {"worker": [], "critic": [], "qa": [], "planner": []}},
        "ask_pipeline": {
            "total_seconds": seconds,
            "step_seconds": 60,
            "keep_questions": 50,
            "max_workers": parallel,
        },
    }


def _fake(cfg, role, messages, **kw):
    prompt = "\n".join(str(m.get("content") or "") for m in messages)
    if role == "planner":
        if "Синтезируй" in prompt:
            return {"ok": True, "backend": 1, "model": "planner", "log": [],
                    "text": "Итог: срок десять дней [[Заявка-0]]."}
        return {"ok": True, "backend": 1, "model": "planner", "log": [],
                "text": json.dumps({"title": "Проверка сроков", "steps": [
                    {"id": "s1", "title": "Срок", "brief": "Найти срок"},
                    {"id": "s2", "title": "Норма", "brief": "Проверить норму"},
                    {"id": "s3", "title": "Цитата", "brief": "Сохранить цитату"},
                ]}, ensure_ascii=False)}
    if role == "worker":
        return {"ok": True, "backend": 1, "model": "writer", "log": [],
                "text": "Срок десять дней [[Заявка-0]]."}
    if role == "critic":
        return {"ok": True, "backend": 1, "model": "critic", "log": [],
                "text": json.dumps({"ok": True, "issues": []})}
    if role == "qa":
        return {"ok": True, "backend": 1, "model": "qa", "log": [],
                "text": "ВЕРДИКТ: ЧИСТО"}
    return {"ok": False, "log": [f"нет роли {role}"]}


@test
def test_ask_pipeline_creates_plan_and_hash_manifest_before_the_answer(tmp: Path):
    import ask_pipeline as P

    root = _project(tmp)
    pack = "# Пак (карточек 1)\n\n## Заявка-0\n\nСрок десять дней. «срок не сокращается»."
    recorded = []
    saved_pulse = P.AG.pulse_threads
    P.AG.pulse_threads = lambda _cfg, slots: recorded.append(list(slots))
    try:
        res = P.run(_cfg(), str(root), "Какой срок у заявки?", pack=pack,
                    call=_fake, thread_id="pipeline-test", momus=True)
    finally:
        P.AG.pulse_threads = saved_pulse
    assert res["ok"], res
    assert recorded == [res["metrics"]["pool"]["slots"]], (recorded, res["metrics"]["pool"])
    plan = Path(root) / res["plan_path"]
    manifest = Path(root) / res["manifest_path"]
    answer = Path(root) / res["answer_path"]
    assert plan.is_file() and manifest.is_file() and answer.is_file(), res
    assert "Проверка сроков" in plan.read_text(encoding="utf-8")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(data["steps"]) == 3, data
    for step in data["steps"]:
        assert step["status"] in ("done", "failed", "partial"), step
        assert len(step["sha256"]) == 64, step
        assert step["role"] in ("worker", "critic", "qa"), step
    assert "Итог" in res["answer"] and res["metrics"]["total_seconds"] >= 0, res
    assert "Проверки по шагам" in res["answer"] and "Момус: чисто" in res["answer"], \
        res["answer"]
    assert res["plan_created_before_answer"] is True, res


@test
def test_ask_pipeline_honours_the_total_deadline(tmp: Path):
    import ask_pipeline as P

    root = _project(tmp)

    def slow(cfg, role, messages, **kw):
        time.sleep(2)
        return {"ok": True, "backend": 1, "model": "slow", "log": [], "text": "поздно"}

    started = time.time()
    res = P.run(_cfg(seconds=0.25), str(root), "срок?", pack="# Пак\n\n## К\n\nтекст",
                call=slow, thread_id="deadline-test", momus=False)
    elapsed = time.time() - started
    assert not res["ok"] and res["status"] == "error", res
    assert res["error"]["code"] == "deadline", res
    assert elapsed < 1.5, f"пайплайн ждал {elapsed:.1f} с"
    assert "20 минут" in res["error"]["message"] or "срок" in res["error"]["message"], res


@test
def test_protected_quotes_laws_and_last_messages_survive_compression(tmp: Path):
    import context_store as CS

    root = _project(tmp)
    store = CS.open_ask_store(str(root), "protect-test")
    quote = "«срок не сокращается»"
    law = "приказ №100"
    body = f"## Заявка-0\n\nСрок десять дней. {law}. {quote}. " + ("шум " * 500)
    ids = [store.put("Заявка-0", body)]
    got = store.summarize(ids, compressor=lambda text, target: {
        "summary": "Кратко: срок десять дней.", "claims": []}, target_chars=80)
    summary = got["summary"]
    assert quote in summary, summary
    assert law in summary, summary
    assert "10 дней" in summary or "десять дней" in summary, summary
    store.close()


@test
def test_pipeline_uses_protected_compression_for_a_large_pack(tmp: Path):
    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + """
context_architecture:
  enabled: true
  compression:
    enabled: true
    threshold_tokens: 1
    target_ratio: 0.20
  ask_full_cards: 1
""", encoding="utf-8")
    quote = "«срок не сокращается»"
    pack = ("# Пак\n\n## Заявка-0\n\nСрок десять дней. приказ №100. " + quote
            + "\n\n" + ("шум " * 1200) + "\n\n## Заявка-1\n\nВторой фрагмент.")
    state = S.AskState(str(root), "compress-test", "вопрос")
    res = P.run(_cfg(), str(root), "вопрос", pack=pack, call=_fake, state=state)
    assert res["ok"], res
    assert res["metrics"]["context"].get("triggered"), res["metrics"]
    assert res["metrics"]["context"].get("protected", 0) >= 1, res["metrics"]
    plan = state.plan()
    assert any(quote in (step.get("pack") or "") for step in plan["steps"]), \
        "защищённая цитата не дошла до плана"


@test
def test_pipeline_keeps_a_partial_answer_when_one_step_fails(tmp: Path):
    import ask_pipeline as P

    root = _project(tmp)

    def call(cfg, role, messages, **kw):
        prompt = "\n".join(str(m.get("content") or "") for m in messages)
        if role == "planner" and "Синтезируй" not in prompt:
            return {"ok": True, "backend": 1, "model": "p", "log": [],
                    "text": json.dumps({"title": "П", "steps": [
                        {"id": "s1", "title": "Один"}, {"id": "s2", "title": "Два"}]})}
        if role == "worker" and "ПЛАН, ПУНКТ s2" in prompt:
            return {"ok": False, "log": ["№1 m: connection refused"],
                    "error": {"code": "llm_unavailable", "message": "писатель недоступен"}}
        return _fake(cfg, role, messages, **kw)

    res = P.run(_cfg(), str(root), "вопрос", pack="# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                call=call, thread_id="partial-test", momus=True)
    assert res["ok"] and res["status"] == "partial", res
    assert "Частичный результат" in res["answer"], res["answer"]
    assert any(s.get("status") == "failed" for s in res["steps"]), res["steps"]


@test
def test_pipeline_project_context_uses_hybrid_and_can_fall_back_to_words(tmp: Path):
    import answer_core as AC
    import ask_pipeline as P

    root = _project(tmp)
    seen = []
    real = AC.search_context

    def fake_search(cwd, question, **kw):
        seen.append(kw.get("extra_args"))
        return {"ok": True, "pack": "# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                "cards": ["Заявка-0"], "total": 1, "why": "", "seconds": 0.0}

    AC.search_context = fake_search
    try:
        P.run(_cfg(), str(root), "срок?", call=_fake, thread_id="hybrid-test")
        P.run(_cfg(), str(root), "срок?", call=_fake, thread_id="words-test",
              enrich=False)
    finally:
        AC.search_context = real
    assert seen[0] == (), seen
    assert seen[1] == ("--no-semantic",), seen


@test
def test_ask_pipeline_warns_about_missing_roles_and_uses_a_fallback(tmp: Path):
    import ask_pipeline as P

    root = _project(tmp)
    cfg = _cfg()
    cfg["chains"] = {"llm": {"worker": []}}
    seen = []

    def call(cfg_, role, messages, **kw):
        seen.append((role, kw.get("prefer")))
        if role == "planner":
            return {"ok": True, "backend": 2, "model": "fallback", "log": ["№1 down"],
                    "text": json.dumps({"title": "П", "steps": [
                        {"id": "s1", "title": "Один", "brief": "Проверить"}]})}
        return {"ok": True, "backend": 2, "model": "fallback", "log": ["№1 down"],
                "text": "ответ [[Заявка-0]]" if role == "worker" else
                        ("ВЕРДИКТ: ЧИСТО" if role == "qa" else
                         json.dumps({"ok": True, "issues": []}))}

    res = P.run(cfg, str(root), "срок?", pack="# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                call=call, thread_id="fallback-test", momus=True)
    assert res["ok"], res
    assert any("роль" in w.lower() or "модел" in w.lower() for w in res["warnings"]), res
    assert any("fallback" in w.lower() or "запас" in w.lower() for w in res["warnings"]), res
    assert any(role == "qa" for role, _ in seen), "Момус не был вызван"


@test
def test_state_resume_resets_a_corrupted_step_and_quarantines_orphans(tmp: Path):
    import ask_state as S

    root = _project(tmp)
    state = S.AskState(str(root), "resume-test", "вопрос")
    state.create("вопрос", {"title": "П", "steps": [
        {"id": "s1", "title": "Один"}, {"id": "s2", "title": "Два"}]})
    state.write_step("s1", "готовый результат", role="worker", status="done")
    state.step_path("s1").write_text("испорчено", encoding="utf-8")
    (state.dir / "steps" / "ghost.md").write_text("осиротел", encoding="utf-8")
    got = state.validate_resume()
    assert got["steps"]["s1"]["status"] == "pending", got
    assert (state.dir / "quarantine" / "ghost.md").is_file(), got
    manifest = state.manifest()
    s1 = next(x for x in manifest["steps"] if x["plan_item_id"] == "s1")
    assert s1["status"] == "pending" and "хеш" in s1["error"], s1
    assert not list(state.dir.rglob("*.tmp")), "временный файл остался после записи"


@test
def test_question_retention_removes_the_oldest_directories(tmp: Path):
    import ask_state as S

    root = _project(tmp)
    for i in range(4):
        state = S.AskState(str(root), f"old-{i}", f"вопрос {i}")
        state.create(f"вопрос {i}", {"title": "П", "steps": [{"id": "s1"}]})
        state.dir.touch()
        time.sleep(0.01)
    newest = S.AskState(str(root), "newest", "новый", keep=2)
    newest.create("новый", {"title": "П", "steps": [{"id": "s1"}]})
    left = sorted(p.name for p in S.root_for(root).iterdir() if p.is_dir())
    assert len(left) == 2, left
    assert "newest" in left and "old-3" in left, left

    journals = root / "AuroraKnowledgeDB" / "meta" / "ask"
    journals.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (journals / f"q{i}.md").write_text(f"# q{i}\n", encoding="utf-8")
        (journals / f"q{i}.md").touch()
        time.sleep(0.01)
    removed = S.prune_journals(str(root), 2)
    assert len(list(journals.glob("*.md"))) == 2, removed
    assert any(x.startswith("journal:") for x in removed), removed


@test
def test_pipeline_limits_are_capped_and_long_is_ten_minutes(_tmp: Path):
    import ask_pipeline as P

    cfg = {"ask_pipeline": {"total_seconds": 99999, "step_seconds": 99999,
                             "max_workers": 99}}
    got = P.pipeline_config(cfg)
    assert got["total_seconds"] == 20 * 60, got
    assert got["step_seconds"] <= got["total_seconds"], got
    assert got["max_workers"] <= 16, got
    assert P.is_long(601) and not P.is_long(599), (P.is_long(601), P.is_long(599))


@test
def test_pipeline_pool_respects_provider_slots(_tmp: Path):
    import ask_pipeline as P

    cfg = _cfg(parallel=3)
    cfg["chains"] = {"llm": {
        "worker": cfg["backends"], "critic": cfg["backends"],
        "qa": cfg["backends"], "planner": cfg["backends"]}}
    slots = P._slots(cfg, 6, {"max_workers": 4})
    assert 2 <= len(slots) <= 4, slots
    assert P._slots(cfg, 1, {"max_workers": 4}) == [slots[0]], slots


@test
def test_pipeline_cancel_leaves_a_clear_state(tmp: Path):
    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    state = S.AskState(str(root), "cancel-test", "отменить")
    state.create("отменить", {"title": "П", "steps": [
        {"id": "s1", "title": "Один", "pack": "контекст"}]})
    state.set_control(cancel=True)
    res = P.run(_cfg(seconds=10), str(root), "отменить",
                pack="# Пак\n\n## Заявка-0\n\nСрок десять дней.",
                call=_fake, state=state, thread_id="cancel-test")
    assert res["status"] == "cancelled" and not res["ok"], res
    assert state.control()["cancel"], state.control()


@test
def test_pipeline_pauses_and_resumes_between_steps(tmp: Path):
    import ask_pipeline as P

    root = _project(tmp)
    state = P.ask_state.AskState(str(root), "pause-test", "пауза")
    started = now = 1_000_000.0
    workers, pauses = [], []

    def call(cfg, role, messages, **kw):
        if role == "worker":
            workers.append((now, state.control()["pause"]))
        return _fake(cfg, role, messages, **kw)

    def emit(event):
        if event["kind"] == "step_done" and event["step"] == "s1":
            state.set_control(pause=True)

    def sleep(seconds):
        nonlocal now
        assert state.control()["pause"], "пауза не удержала шаг"
        saved = state.state()
        assert (saved["status"], saved["current"]) == ("paused", "s2"), saved
        assert len(workers) == 1 and not state.step_path("s2").exists(), workers
        assert seconds > 0
        pauses.append(seconds)
        now += seconds
        if len(pauses) == 2:
            state.set_control(pause=False)

    # The real watchdog must see the same absolute deadline clock as the pipeline.
    clock = SimpleNamespace(time=lambda: now, sleep=sleep)
    wall_started = time.monotonic()
    with mock.patch.object(P, "time", clock), mock.patch.object(P.RC, "time", clock):
        res = P.run(_cfg(seconds=97, parallel=1), str(root), "пауза",
                    pack="# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                    call=call, state=state, emit=emit)
    assert len(pauses) == 2, ("пауза не удержала второй шаг", pauses, workers)
    assert time.monotonic() - wall_started < 10, "пайплайн не проснулся"
    assert res["ok"] and res["status"] == "full", res
    assert not state.control()["pause"] and state.state()["status"] == "done", res
    assert workers == [(started, False), (now, False), (now, False)], workers
    assert res["metrics"]["total_seconds"] == round(now - started, 2), res["metrics"]


@test
def test_pipeline_does_not_start_work_with_zero_working_budget(tmp: Path):
    import ask_pipeline as P

    root = _project(tmp)
    state = P.ask_state.AskState(str(root), "zero-work-budget", "срок?")
    clock = SimpleNamespace(time=lambda: 1_000_000.0)
    call = mock.Mock(wraps=_fake, spec=_fake)
    events = []
    with mock.patch.object(P, "time", clock), mock.patch.object(P.RC, "time", clock):
        res = P.run(_cfg(seconds=10), str(root), "срок?",
                    pack="# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                    call=call, state=state, emit=events.append)

    call.assert_not_called()
    assert not res["ok"] and res["status"] == "error", res
    assert res["error"]["code"] == "deadline" and res["answer"] == "" and res["steps"] == [], res
    assert [(event["kind"], event.get("error")) for event in events] == [("error", "deadline")], events
    assert state.plan_path.is_file() and state.manifest_path.is_file(), res
    assert not state.answer_path.exists(), res
    saved = state.state()
    assert saved["status"] == "error", saved
    timing = saved["timing"]
    assert (timing["answer_deadline_s"], timing["synthesis_reserve_s"],
            timing["deadline_left_s"]) == (0, 10, 10), timing


@test
def test_plan_file_and_version_exist_before_the_first_answer(tmp: Path):
    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    state = S.AskState(str(root), "before-answer", "вопрос")
    seen = {}

    def call(cfg, role, messages, **kw):
        prompt = "\n".join(str(m.get("content") or "") for m in messages)
        if role == "planner" and "Синтезируй" not in prompt:
            return {"ok": True, "backend": 1, "model": "p", "log": [],
                    "text": json.dumps({"title": "П", "steps": [
                        {"id": "s1", "title": "Один"}]}, ensure_ascii=False)}
        if role == "worker":
            seen["plan_exists"] = state.plan_path.is_file()
            seen["plan_json_exists"] = state.plan_json.is_file()
            seen["version"] = state.plan().get("version")
            return {"ok": True, "backend": 1, "model": "w", "log": [],
                    "text": "Ответ [[Заявка-0]]."}
        if role == "critic":
            return {"ok": True, "backend": 1, "model": "c", "log": [],
                    "text": json.dumps({"ok": True, "issues": []})}
        if role == "qa":
            return {"ok": True, "backend": 1, "model": "q", "log": [],
                    "text": "ВЕРДИКТ: ЧИСТО"}
        return {"ok": True, "backend": 1, "model": "p", "log": [],
                "text": "Итог [[Заявка-0]]."}

    res = P.run(_cfg(), str(root), "вопрос", pack="# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                call=call, state=state, thread_id="before-answer")
    assert res["ok"], res
    assert seen.get("plan_exists") and seen.get("plan_json_exists"), seen
    assert seen.get("version") and seen["version"] >= 2, seen


@test
def test_pipeline_buttons_follow_the_state_matrix(tmp: Path):
    node = shutil.which("node")
    if not node:
        return
    view = Path(__file__).resolve().parents[2] / "cockpit/modules/ask/view.js"
    harness = tmp / "buttons.mjs"
    harness.write_text(
        f"const m = await import({view.as_uri()!r});\n"
        "const states = ['idle', 'running', 'paused', 'done', 'error', 'cancelled', 'partial'];\n"
        "const out = {};\n"
        "for (const s of states) out[s] = m.pipelineButtonState(s);\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8")
    cp = subprocess.run([node, str(harness)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60, check=False)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr, cp.stdout)
    data = json.loads(cp.stdout.strip().splitlines()[-1])
    assert data["running"]["new"] is False and data["running"]["cancel"] is True
    assert data["running"]["pause"] is True and data["running"]["retry"] is False
    assert data["paused"]["pause"] is True and data["paused"]["new"] is False
    assert data["done"]["new"] is True and data["done"]["retry"] is False
    assert data["error"]["retry"] is True and data["error"]["new"] is True
    assert data["cancelled"]["retry"] is True


@test
def test_pipeline_api_hides_pack_and_writes_control(tmp: Path):
    import ask_state as S

    root = _project(tmp)
    state = S.AskState(str(root), "api-test", "вопрос")
    state.create("вопрос", {"title": "П", "steps": [
        {"id": "s1", "title": "Один", "pack": "секретный рабочий контекст"}]})
    state.write_answer("Ответ [[Заявка-0]].", status="done")
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib

    import aurora_cockpit as CK
    importlib.reload(CK)
    got = CK.ask_pipeline_state(str(root), "api-test")
    assert got["plan"]["steps"][0]["title"] == "Один", got
    assert "pack" not in got["plan"]["steps"][0], "рабочий контекст уехал в API"
    assert got["answer"].startswith("Ответ"), got
    paused = CK.ask_pipeline_control(str(root), {"id": "api-test", "action": "pause"})
    assert paused["ok"] and paused["control"]["pause"], paused
    resumed = CK.ask_pipeline_control(str(root), {"id": "api-test", "action": "resume"})
    assert resumed["ok"] and not resumed["control"]["pause"], resumed
    bad = CK.ask_pipeline_state(str(root), "../escape")
    assert bad.get("error"), bad


_P1_PACK = "# Пак\n\n## Карточка\n\nСрок 10 дней."


def _p1_cfg(*, primary=1):
    cfg = _cfg(parallel=1)
    first, second = cfg["backends"]
    first.update(name="p1", fallback=True)
    second.update(n=3, name="p3", provider="p3", fallback=True)
    chain = [first, second] if primary == 1 else [second, first]
    cfg["chains"]["llm"] = dict.fromkeys(("planner", "worker", "critic", "qa"), chain)
    return cfg


def _p1_call(*, n_steps=1, **results):
    def call(cfg, role, messages, **kw):
        prompt = messages[0]["content"]
        if role == "planner" and "Синтезируй" not in prompt:
            result = {"ok": True, "backend": 1, "text": json.dumps({"steps": [
                {"id": f"s{i}", "title": f"Пункт {i}"} for i in range(1, n_steps + 1)]})}
        else:
            if role == "planner":
                role = "synth"
            result = _fake(cfg, "planner" if role == "synth" else role, messages, **kw)
        result = results.get(role, result)
        return result(cfg, role, messages, **kw) if callable(result) else result
    return call


def _p1_run(root, call, *, cfg=None, ident="p1", momus=True, resume=False):
    import ask_pipeline as P
    import ask_state as S

    state = S.AskState(root, ident, "Тестовый вопрос")
    events = []
    res = P.run(cfg or _p1_cfg(), root, "Тестовый вопрос", pack=_P1_PACK, call=call,
                state=state, momus=momus, resume=resume, emit=events.append)
    return res, state, events


def _p1_outcome(outcome, *, degraded=(), notices=(), warnings=0, retries=0):
    res, state, events = outcome
    assert res["ok"] and res["status"] == ("partial" if degraded else "full"), res
    status = "partial" if degraded else "done"
    assert res["degraded"] == list(degraded) and res["notices"] == list(notices), res
    assert len(res["warnings"]) == warnings and res["metrics"]["retries"] == retries, res
    done = [ev for ev in events if ev["kind"] == "done"]
    assert len(done) == 1, events
    plan = [ev for ev in events if ev["kind"] == "plan"]
    assert len(plan) == 1 and isinstance(plan[0]["warnings"], list), events
    manifest, saved = state.manifest(), state.state()
    assert "ring_trace" not in json.dumps((manifest, saved, events))
    assert done[0]["status"] == manifest["answer"]["status"] == saved["status"] == status
    assert done[0]["warnings"] == res["warnings"] == saved["warnings"]
    assert done[0]["completion"] == manifest["answer"]["completion"] == saved["completion"] == res["completion"]
    assert done[0]["steps"] == res["steps"]
    assert saved["degraded"] == list(degraded) and saved["notices"] == list(notices)
    assert "checks" not in manifest and not {"degraded", "notices"}.intersection(manifest["answer"])
    assert [(row["plan_item_id"], row["status"]) for row in manifest["steps"]] == [
        (row["id"], row["status"]) for row in state.plan()["steps"]]
    steps, checks = res["completion"]["steps"], res["completion"]["checks"]
    assert steps["total"] == sum(steps[k] for k in ("done", "partial", "failed", "cancelled"))
    assert checks["required"] == sum(checks[k] for k in ("passed", "remarks", "unsupported", "failed", "missing"))
    return res


@test
def test_p1_ring_trace_full_answer_shows_real_fallback(tmp: Path):
    import agent_core as AG

    chat = []
    traces = []

    def transport(op, backend, payload, timeout):
        if op != "chat":
            return 404, {}, None, 0.0
        chat.append(backend["n"])
        if backend["n"] == 1:
            return 401, {}, "denied", 0.01
        return 200, {"choices": [{"message": {"content": "Срок [[Карточка]]."}}]}, None, 0.01

    def worker(cfg, role, messages, **kw):
        result = AG.call_role(cfg, role, messages, **kw, transport=transport, sleep=AG.NO_WAIT)
        traces.append(result)
        return result

    outcome = _p1_run(_project(tmp), _p1_call(worker=worker))
    assert chat == [1, 3], chat
    res = _p1_outcome(outcome, notices=("routing_fallback:worker:1->3",), warnings=1, retries=1)
    assert "запас" in res["warnings"][0] and "№1->№3" in res["warnings"][0], res
    r = traces[0]
    assert r["ring_trace"]["order"] == [1, 3]
    assert r["ring_trace"]["first_attempt"] == 1 and r["ring_trace"]["winner"] == 3
    assert len(r["ring_trace"]["attempts"]) == r["req"]["attempts"] == 2


@test
def test_p1_full_requires_pass_not_only_no_warning(tmp: Path):
    root = _project(tmp)
    cases = (
        ("clean", {}, (), 0, "Момус: чисто"),
        ("B6", {"qa": {"ok": True, "text": "ВЕРДИКТ: ЧИСТО\r\nВЕРДИКТ: БЕЗ ОПОРЫ   \r\n"}},
         ("check_missing:qa:s1",), 1, "Момус: без вердикта"),
        ("B8", {"critic": {"ok": True, "text": '{"ok":true,"issues":["замечание"]}'}},
         ("critic_remarks:s1",), 0, "Критик: замечания"),
        ("B7", {"qa": {"ok": True, "text": "ВЕРДИКТ: БЕЗ ОПОРЫ 0!"}},
         ("momus_unsupported:s1:0",), 0, "Момус: без опоры 0"),
        ("B12", {"critic": {"ok": True, "text": '{"ok":"false"}'}},
         ("check_missing:critic:s1",), 1, "Критик: без вердикта"),
        ("B13", {"critic": {"ok": True, "text": '[{"ok":true}]'}},
         ("check_missing:critic:s1",), 1, "Критик: без вердикта"),
    )
    for ident, replacements, degraded, warnings, label in cases:
        outcome = _p1_run(root, _p1_call(**replacements), ident=ident)
        res = _p1_outcome(outcome, degraded=degraded, warnings=warnings)
        assert label in res["answer"], (ident, res)
        assert res["completion"]["checks"]["required"] == 2, (ident, res)


def _p1_trace(order, attempts, **fields):
    return {"order": order, "attempts": [{"n": n} for n in attempts],
            "assigned": order[0], "first_attempt": attempts[0], "winner": attempts[-1], **fields}


@test
def test_p1_ring_trace_rejects_unknown_and_classifies_both_policies(tmp: Path):
    import ask_pipeline as P

    valid = _p1_trace([1, 3], [1, 3])
    useful = {"ok": True, "backend": 3, "log": ["№1 down"], "ring_trace": valid}
    cases = (
        ("A1", {"ring_trace": _p1_trace([3, 1], [3])}, ""),
        ("A2", {"backend": 1, "ring_trace": _p1_trace([1, 3], [1], prefer=1)}, ""),
        ("A3", {}, "1->3"),
        ("A4", {"ring_trace": _p1_trace([1, 3], [3])}, ""),
        ("A5", {"ring_trace": _p1_trace([3, 1], [3, 3])}, ""),
        ("A6", {"backend": 1, "ring_trace": _p1_trace([3, 1], [3, 1])}, "3->1"),
        ("A7", {"ring_trace": _p1_trace([1, 3], [1, 3, 1, 3])}, "1->3"),
        ("A8", {"cached": True}, ""),
        ("A10", {"ring_trace": dict(valid, attempts="bad")}, ""),
        ("A11", {"backend": 2}, ""),
        ("A12", {"backend": 5, "ring_trace": _p1_trace([1, 3], [1, 5])}, ""),
        ("null-trace", {"ring_trace": None}, ""),
        ("failed", {"ok": False}, ""),
        ("only-bool-ok", {"ok": 1}, ""),
        ("bool-backend", {"backend": True, "ring_trace": _p1_trace([3, 1], [3, 1])}, ""),
        ("duplicates", {"ring_trace": dict(valid, order=[1, 3, 3])}, ""),
        ("wrong-last", {"ring_trace": _p1_trace([1, 3], [3, 1], winner=3)}, ""),
        ("wrong-assigned", {"ring_trace": dict(valid, assigned=3)}, ""),
        ("bool-attempt", {"ring_trace": _p1_trace([1, 3], [True, 3])}, ""),
        ("null-first", {"ring_trace": dict(valid, first_attempt=None)}, ""),
        ("wrong-first", {"ring_trace": dict(valid, first_attempt=3)}, ""),
        ("bool-prefer", {"ring_trace": dict(valid, prefer=True)}, ""),
        ("negative-prefer", {"ring_trace": dict(valid, prefer=-1)}, ""),
        ("optional-fields", {"ring_trace": {k: v for k, v in valid.items()
                                               if k not in ("prefer", "first_attempt")}}, "1->3"),
        ("last-distinct", {"ring_trace": _p1_trace([1, 2, 3], [1, 2, 3, 3])}, "2->3"),
    )
    for policy in ("notice", "degraded"):
        for ident, fields, pair in cases:
            issues = P._RunIssues()
            issues.route(P._route_issue("worker", {**useful, **fields}, _p1_cfg(), policy))
            code = [f"routing_fallback:worker:{pair}"] if pair else []
            assert issues.degraded == (code if policy == "degraded" else []), (ident, policy)
            assert issues.notices == (code if policy == "notice" else []), (ident, policy)
            assert len(issues.warnings) == bool(pair), (ident, policy, issues.warnings)
            if pair:
                before, after = pair.split("->")
                assert f"№{before}->№{after}" in issues.warnings[0] and "запас" in issues.warnings[0]
        legacy = {"ok": True, "backend": 2, "log": ["№1 down"]}
        issue = P._route_issue("worker", legacy, _cfg(), policy)
        assert issue.code == "routing_fallback:worker:1->2" and not issue.proven and not issue.degraded
        assert "не подтверждена" in issue.warning
        assert P._route_issue("worker", legacy, _p1_cfg(), policy) is None
        for log in ([], ["№1 не отвечал, вернёмся через 900 с"], ["№1 слот занят, down"],
                    ["№1 HTTP 429"], ["№1 пропущен, HTTP 503"], ["№2 down"]):
            assert P._route_issue("worker", {**legacy, "log": log}, _cfg(), policy) is None, log


def _p1_ring(cfg, statuses=(200,), **kw):
    import agent_core as AG

    calls = []
    replies = iter(statuses)

    def transport(op, backend, payload, timeout):
        if op != "chat":
            return 404, {}, None, 0.0
        calls.append((backend["n"], "chat_template_kwargs" in payload))
        status = next(replies)
        if status == 400:
            return 400, {}, "unexpected chat_template_kwargs", 0.01
        if status != 200:
            return status, {}, "synthetic refusal", 0.01
        return 200, {"choices": [{"message": {"content": "Срок подтверждён."}}]}, None, 0.01

    result = AG.call_role(cfg, "worker", [{"role": "user", "content": "synthetic prompt value"}],
                          deadline=time.time() + 3, transport=transport, sleep=AG.NO_WAIT, **kw)
    return result, calls


@test
def test_p1_ring_trace_attempts_exclude_skips_and_same_backend_http_retry(tmp: Path):
    from unittest.mock import patch

    import agent_core as AG
    import ask_pipeline as P

    skip = _p1_cfg()
    skip["backends"][0].update(models={}, model="")
    persist = _p1_cfg()
    persist["persist"] = True
    cases = (
        (_p1_cfg(primary=3), {}, [3, 1], [3], (200,)),
        (_p1_cfg(primary=3), {"prefer": 1}, [1, 3], [1], (200,)),
        (skip, {}, [1, 3], [3], (200,)),
        (persist, {}, [1, 3], [3], (200,)),
        (_p1_cfg(primary=3), {}, [3, 1], [3], (400, 200)),
    )
    for cfg, kw, order, attempts, statuses in cases:
        with patch.dict(AG.SLOW, {1: time.time() + 10}, clear=True):
            result, calls = _p1_ring(cfg, statuses, **kw)
        assert result["ok"], result
        trace = result["ring_trace"]
        assert (trace["order"], trace["assigned"], trace["prefer"]) == (order, order[0], kw.get("prefer", 0))
        assert [a["n"] for a in trace["attempts"]] == attempts
        assert trace["first_attempt"] == trace["winner"] == attempts[0]
        assert result["req"]["attempts"] == len(attempts)
        assert P._route_issue("worker", result, cfg) is None
        assert calls == [(attempts[0], True)] + ([(attempts[0], False)] if statuses[0] == 400 else []), calls
    early, calls = _p1_ring({"backends": [], "request_timeout": 1}, ())
    assert not early["ok"] and not calls and "ring_trace" not in early, early


@test
def test_p1_ring_trace_forbidden_replacement_never_reaches_transport(tmp: Path):
    from harness import forget_engine_state

    root = _project(tmp)
    for form in ("role-chain", "legacy-fallback-zero"):
        forget_engine_state()
        cfg = _p1_cfg()
        if form == "role-chain":
            cfg["chains"]["llm"]["worker"] = cfg["backends"][:1]
        else:
            del cfg["chains"]
            cfg["backends"][1]["fallback"] = False
        seen = []

        def worker(conf, role, messages, **kw):
            reply, calls = _p1_ring(conf, (401,), prefer=kw["prefer"])
            seen.append((reply, calls))
            return reply

        res, _, events = _p1_run(root, _p1_call(worker=worker), cfg=cfg, ident=form)
        assert res["status"] == "error" and res["error"]["code"] == "no_answer", res
        assert not any(e["kind"] == "done" for e in events)
        assert len(seen) == 1 and seen[0][1] == [(1, True)], seen
        reply = seen[0][0]
        trace = reply["ring_trace"]
        assert not reply["ok"] and trace["winner"] is None and trace["order"] == [1], trace
        assert len(trace["attempts"]) == reply["req"]["attempts"] == 1
        assert not any("запас" in warning for warning in res["warnings"]), res


@test
def test_p1_ring_trace_redaction_and_cache_projection(tmp: Path):
    from unittest.mock import patch

    import agent_core as AG

    cfg = _p1_cfg()
    backend = cfg["backends"][0]
    backend.update(provider="https://hidden.invalid/path", key="unused-key-fixture", url="https://unused.invalid",
                   models={"worker": "TOKEN=hidden-value"})
    reply, _ = _p1_ring(cfg)
    trace = reply["ring_trace"]
    assert trace["attempts"] == [{"n": 1, "provider": "", "model": ""}]
    packed = json.dumps(trace)
    for marker in ("url", "key", "headers", "prompt", "reasoning", "hidden", "unused", "Срок"):
        assert marker not in packed, packed
    assert "ring_trace" not in AG.call_diag(reply)
    backend.update(provider="p\n1" + "x" * 130, models={"worker": "m@7\u0000"})
    clean, _ = _p1_ring(cfg)
    labels = clean["ring_trace"]["attempts"][0]
    assert labels["model"] == "m@7" and labels["provider"] == "p1" + "x" * 118, labels
    with patch.object(AG, "LLM_CACHE", tmp / "llm-cache"), patch.object(AG, "_cache_on", return_value=True):
        prompt = [{"role": "user", "content": "fixture cache"}]
        key = AG._cache_key(cfg, "worker", prompt, AG.role_thinks(cfg, "worker"), None, "")
        AG._cache_put(key, clean)
        with patch.object(AG, "_call_role", side_effect=AssertionError("cache must not call a provider")):
            cached = AG.call_role(cfg, "worker", prompt)
    assert cached["cached"] is True and "ring_trace" not in cached, cached


_CRITIC_CLEAN = '# Критик · Пункт 1\n\n```json\n{"ok":true,"issues":[]}\n```\n'
_QA_CLEAN = "# Момус · Пункт 1\n\nВЕРДИКТ: ЧИСТО\r\n"


def _p1_legacy(root, ident, checks, *, files=None, status="done", n_steps=1,
               answer="Срок подтверждён."):
    import ask_state as S

    state = S.AskState(root, ident)
    state.create("Тестовый вопрос", {"title": "Старый план", "steps": [
        {"id": f"s{i}", "title": f"Пункт {i}", "pack": _P1_PACK}
        for i in range(1, n_steps + 1)]})
    for i in range(1, n_steps + 1):
        state.write_step(f"s{i}", f"# Шаг\n\n## Ответ\n\n{answer if i == 1 else 'Срок подтверждён.'}\n",
                         status=status if i == 1 else "done")
        state.mark_step(f"s{i}", checks=checks)
    for role, text in (files or {}).items():
        path = state.check_path("s1", role)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    S.write_json(state.state_path, {k: v for k, v in state.state().items()
                                   if k not in ("planner_fallback", "degraded", "notices", "completion")})
    return state


def _p1_resume(state, *, momus=True, synth=None):
    calls = []

    def call(cfg, role, messages, **kw):
        calls.append((role, "Синтезируй" in messages[0]["content"]))
        return synth if synth is not None else _fake(cfg, role, messages, **kw)

    outcome = _p1_run(state.cwd, call, ident=state.id, momus=momus, resume=True)
    assert calls == [("planner", True)], calls
    return outcome


@test
def test_p1_resume_legacy_relies_on_last_record_and_never_erases_required_checks(tmp: Path):
    import ask_state as S

    root = _project(tmp)
    critic = {"role": "critic", "status": "done"}
    qa = {"role": "qa", "status": "done"}
    clean = {"critic": _CRITIC_CLEAN, "qa": _QA_CLEAN}
    pass_qa = dict(qa, verdict="pass")
    missing = ("check_missing:critic:s1", "check_missing:qa:s1")
    cases = (
        ("C1-C7", [critic, qa], clean, True, (), 0, (2, 2)),
        ("C2", [dict(critic, status="failed", verdict="pass")], {"critic": _CRITIC_CLEAN}, False,
         ("check_failed:critic:s1",), 1, (1, 0)),
        ("C3", [critic, qa], {}, True, missing, 2, (2, 0)),
        ("C4-B22", [], {}, True, missing, 2, (2, 0)),
        ("C5", [critic, dict(qa, status="failed"), pass_qa], clean, True, (), 0, (2, 2)),
        ("C6", [dict(critic, verdict="remarks"), pass_qa], clean, True,
         ("critic_remarks:s1",), 0, (2, 1)),
        ("B23", [pass_qa], {}, True, missing[:1], 1, (2, 1)),
        ("B6-last-record", [critic, pass_qa, dict(qa, status="failed")], clean, True,
         ("check_failed:qa:s1",), 1, (2, 1)),
        ("B6-last-record-broken", [critic, pass_qa, dict(qa, verdict="missing")], clean, True,
         missing[1:], 1, (2, 1)),
        ("B6-qa-remarks", [critic, dict(qa, verdict="remarks")], clean, True, missing[1:], 1, (2, 1)),
        ("B6-pass-unsupported", [critic, dict(pass_qa, unsupported=5)], clean, True, missing[1:], 1, (2, 1)),
        ("B6-qa-pending", [critic, dict(pass_qa, status="pending")], clean, True, missing[1:], 1, (2, 1)),
        ("recover-orphan-file", [], clean, True, (), 0, (2, 2)),
        ("old-qa-still-required", [critic, dict(qa, status="failed")], clean, False,
         ("check_failed:qa:s1",), 1, (2, 1)),
        ("optional-qa", [critic, dict(qa, status="failed", required=False)], clean, False, (), 0, (1, 1)),
        ("required-qa", [critic, dict(qa, status="failed", required=False)], clean, True,
         ("check_failed:qa:s1",), 1, (2, 1)),
        ("unknown-role", [critic, pass_qa, {"role": "advisory", "status": "failed", "required": True}],
         clean, True, (), 0, (2, 2)),
        ("bad-utf8", [critic, qa], clean | {"qa": b"\xff"}, True, missing[1:], 1, (2, 1)),
    )
    for ident, records, files, momus, degraded, warnings, counts in cases:
        state = _p1_legacy(root, ident, records, files=files)
        if ident == "C1-C7":
            paths = [state.plan_json, state.plan_path, state.manifest_path, state.state_path]
            before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
            assert S.read_state(root, state.id)["state"]["status"] == "planned"
            assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before
        result = _p1_outcome(_p1_resume(state, momus=momus), degraded=degraded, warnings=warnings)
        totals = result["completion"]["checks"]
        assert (totals["required"], totals["passed"]) == counts, (ident, totals)
        assert state.manifest()["steps"][0]["checks"] == records, (ident, state.manifest())
        assert state.plan()["steps"][0]["status"] == ("partial" if degraded else "done"), ident
        assert ("[~]" if degraded else "[x]") in state.plan_path.read_text(encoding="utf-8"), ident


@test
def test_p1_resume_unsafe_path_and_hash_do_not_fall_back_to_clean_check_file(tmp: Path):
    from unittest.mock import patch

    import ask_state as S

    root = _project(tmp)
    outside = root / "external-qa.md"
    outside.write_text(_QA_CLEAN, encoding="utf-8")
    for case in ("absolute", "absolute-inside", "relative-outside", "symlink", "hash", "no-default-fallback"):
        state = _p1_legacy(root, case, [], files={"critic": _CRITIC_CLEAN, "qa": _QA_CLEAN})
        clean_path = state.check_path("s1", "qa")
        if case == "symlink":
            try:
                (clean_path.parent / "link.md").symlink_to(outside)
            except (OSError, NotImplementedError):
                continue  # Windows without symlink privileges still exercises the other paths.
        location = {
            "absolute": str(outside), "absolute-inside": str(clean_path),
            "relative-outside": "../../../../external-qa.md", "symlink": "checks/link.md",
            "hash": str(clean_path.relative_to(state.dir)), "no-default-fallback": "checks/missing.md",
        }[case]
        qa = {"role": "qa", "status": "done", "file": location}
        if case == "hash":
            qa["sha256"] = S.sha256("old checksum body")
        state.mark_step("s1", checks=[qa])
        observed = []
        original = Path.read_bytes

        def read(path):
            observed.append(path)
            return original(path)

        with patch.object(Path, "read_bytes", read):
            outcome = _p1_resume(state)
        result = _p1_outcome(outcome, degraded=("check_missing:qa:s1",), warnings=1)
        assert "Момус: без вердикта" in result["answer"], (case, result)
        assert outside.resolve() not in observed, (case, observed)
        assert (clean_path.resolve() in observed) == (case == "hash"), (case, observed)


@test
def test_p1_resume_preserves_stale_partial_but_not_synth_failure_or_route_warning(tmp: Path):
    root = _project(tmp)
    clean = [{"role": role, "status": "done", "verdict": "pass"} for role in ("critic", "qa")]
    stale = _p1_legacy(root, "B24", clean, status="partial")
    _p1_outcome(_p1_resume(stale), degraded=("step_partial:s1",), warnings=1)
    failed_qa = _p1_legacy(root, "C12", [clean[0], {"role": "qa", "status": "failed"}], status="partial")
    _p1_outcome(_p1_resume(failed_qa), degraded=("check_failed:qa:s1",), warnings=1)
    heuristic = _p1_run(root, _p1_call(planner={"ok": True, "text": '{"steps":[]}'}), ident="C13")
    _p1_outcome(heuristic, degraded=("planner_fallback",), warnings=1)
    assert heuristic[1].state()["planner_fallback"] is True
    _p1_outcome(_p1_resume(heuristic[1]), degraded=("planner_fallback",), warnings=1)
    transient = _p1_run(root, _p1_call(synth={"ok": False}, worker={
        "ok": True, "text": "Ответ", "backend": 3, "ring_trace": _p1_trace([1, 3], [1, 3])}), ident="C14-C17")
    _p1_outcome(transient, degraded=("synth_fallback",), notices=("routing_fallback:worker:1->3",),
                warnings=2, retries=1)
    state = transient[1]
    _p1_outcome(_p1_resume(state))
    state.step_path("orphan").write_text("лишний шаг", encoding="utf-8")
    _p1_outcome(_p1_resume(state), notices=("resume_orphans",), warnings=1)
    assert (state.dir / "quarantine" / "orphan.md").read_text(encoding="utf-8") == "лишний шаг"


@test
def test_p1_resume_reexecuted_step_clears_stale_proofs_and_check_write_is_upsert(tmp: Path):
    root = _project(tmp)
    critic = {"role": "critic", "status": "failed"}
    qa = {"role": "qa", "status": "failed"}
    for status, momus in (("failed", True), ("pending", False)):
        old = _p1_legacy(root, status, [critic, qa, critic], status=status,
                         files={"qa": "ВЕРДИКТ: БЕЗ ОПОРЫ 3"})
        result = _p1_outcome(_p1_run(root, _p1_call(), ident=old.id, resume=True, momus=momus))
        saved = old.manifest()["steps"][0]
        assert saved["answer_chars"] == len(result["steps"][0]["answer"])
        assert [c["role"] for c in saved["checks"]] == (["critic", "qa"] if momus else ["critic"])
        assert old.check_path("s1", "qa").exists() is momus
        assert all(c["verdict"] == "pass" and c["required"] is True and c["unsupported"] == 0
                   for c in saved["checks"]), saved
    direct = _p1_legacy(root, "upsert", [critic, qa, critic])
    direct.write_step("s1", _CRITIC_CLEAN, role="critic", check=True, verdict="pass")
    assert [(c["role"], c.get("verdict")) for c in direct.manifest()["steps"][0]["checks"]] == [
        ("qa", None), ("critic", "pass")]
    assert "answer_chars" not in direct.manifest()["steps"][0]


@test
def test_p1_empty_and_failed_steps_are_reconciled_before_completion(tmp: Path):
    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    for label, first, second, reason, expected in (
        ("B15", "", "Ответ", "worker_empty:s1", (1, 1, 0, 4)),
        ("B20", "Ответ", None, "step_failed:s2", (1, 0, 1, 2)),
    ):
        def writer(cfg, role, messages, **kw):
            text = second if "ПУНКТ s2" in messages[0]["content"] else first
            return {"ok": True, "text": text} if text is not None else {"ok": False, "error": "сбой шага"}

        result = _p1_outcome(_p1_run(root, _p1_call(n_steps=2, worker=writer), ident=label),
                             degraded=(reason,), warnings=1)
        steps, checks = result["completion"]["steps"], result["completion"]["checks"]
        assert (steps["done"], steps["partial"], steps["failed"], checks["required"]) == expected, result
        assert [row["id"] for row in result["steps"]] == ["s1", "s2"]
    empty, state, events = _p1_run(root, _p1_call(worker={"ok": True, "text": "  "}), ident="no-answer")
    assert empty["status"] == "error" and empty["error"]["code"] == "no_answer", empty
    assert not state.answer_path.exists() and not any(e["kind"] in ("synth", "done") for e in events)
    assert len([e for e in events if e["kind"] == "check"]) == 2
    assert "Ответ: пустой" in empty["warnings"][0] and empty["steps"][0]["status"] == "partial"
    checks = [{"role": role, "status": "done", "verdict": "pass"} for role in ("critic", "qa")]
    old = _p1_legacy(root, "C15", checks, n_steps=2, answer="  ")
    _p1_outcome(_p1_resume(old), degraded=("worker_empty:s1",), warnings=1)
    gone = _p1_legacy(root, "B19", checks, n_steps=2)
    manifest = gone.manifest()
    manifest["steps"].pop()
    S.write_json(gone.manifest_path, manifest)
    issues = P._RunIssues()
    steps = P.normalize_steps([{"id": "s1", "status": "done"}], gone.plan(), P.CHECK_ROLES, gone, issues)
    assert [s["status"] for s in steps] == ["done", "failed"] and issues.degraded == ["step_failed:s2"]
    assert steps[-1]["error"] == {"code": "step_failed", "message": "не выполнен"}
    assert gone.plan()["steps"][-1]["status"] == "failed" and "[!]" in gone.plan_path.read_text(encoding="utf-8")
    failed = gone.manifest()["steps"][-1]
    assert failed["plan_item_id"] == "s2" and failed["status"] == "failed" and failed["checks"] == []
    assert failed["answer_chars"] == 0 and failed["sha256"] == failed["file"] == ""


@test
def test_p1_qa_failed_twice_is_not_a_route_retry_and_disabled_qa_is_not_missing(tmp: Path):
    root = _project(tmp)
    q1 = _p1_run(root, _p1_call(n_steps=2, qa={"ok": False, "error": "timeout"}), ident="Q1-like")
    res = _p1_outcome(q1, degraded=("check_failed:qa:s1", "check_failed:qa:s2"), warnings=2)
    assert res["completion"]["checks"] == {
        "required": 4, "passed": 2, "remarks": 0, "unsupported": 0, "failed": 2, "missing": 0}
    cfg = _p1_cfg()
    del cfg["chains"]["llm"]["critic"]
    b14 = _p1_run(root, _p1_call(), cfg=cfg, momus=False, ident="B14-A13")
    res = _p1_outcome(b14, notices=("role_config:critic:chain",), warnings=1)
    assert res["completion"]["checks"]["required"] == res["completion"]["checks"]["passed"] == 1
    assert [evt["role"] for evt in b14[2] if evt["kind"] == "check"] == ["critic"]


@test
def test_p1_cancel_after_answer_attempt_is_json_and_has_no_done_event(tmp: Path):
    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    state = S.AskState(root, "cancel-after-fallback")
    events = []

    def emit(evt):
        events.append(evt)
        if evt["kind"] == "check" and evt["role"] == "qa":
            state.set_control(cancel=True)

    call = _p1_call(worker={"ok": True, "text": "Ответ", "backend": 3,
                            "ring_trace": _p1_trace([1, 3], [1, 3])})
    res = P.run(_p1_cfg(), root, "Тестовый вопрос", pack=_P1_PACK, call=call, state=state, emit=emit)
    assert res["status"] == "cancelled" and events[-1]["kind"] == "cancelled", (res, events)
    assert not state.answer_path.exists() and not any(e["kind"] == "done" for e in events)
    assert "_routes" not in json.dumps(res)


@test
def test_p1_ui_mount_displays_real_events_checks_route_notice_and_retry(tmp: Path):
    import agent_runner as R
    import ask_state as S

    node = shutil.which("node")
    if not node:
        return
    root = _project(tmp)
    planner = {"ok": True, "backend": 3, "ring_trace": _p1_trace([1, 3], [1, 3]),
               "text": '{"steps":[{"id":"s1","title":"Проверить срок"}]}'}
    full = _p1_run(root, _p1_call(planner=planner), ident="ui-full")
    _p1_outcome(full, notices=("routing_fallback:planner:1->3",), warnings=1, retries=1)
    missing = _p1_run(root, _p1_call(qa={"ok": True, "text": "ВЕРДИКТ: ЧИСТО\nнет итога"}), ident="ui-missing")
    remarks = _p1_run(root, _p1_call(critic={"ok": True, "text": '{"ok":false,"issues":[]}'}), ident="ui-remarks")
    unsupported = _p1_run(root, _p1_call(qa={"ok": True, "text": "ВЕРДИКТ: БЕЗ ОПОРЫ 2"}), ident="ui-unsupported")
    synth = _p1_run(root, _p1_call(synth={"ok": False}), ident="ui-synth")
    resumed = _p1_resume(synth[1])

    def cancel_before_plan(cfg, role, messages, **kw):
        S.AskState(root, "ui-cancelled").set_control(cancel=True)
        return _fake(cfg, role, messages, **kw)

    cancelled = _p1_run(root, _p1_call(planner=cancel_before_plan), ident="ui-cancelled")
    S.AskState(root, "ui-no-plan").set_control(cancel=True)
    unplanned = _p1_run(root, _p1_call(), ident="ui-no-plan")
    failed = _p1_run(root, _p1_call(worker={"ok": False}), ident="ui-error")
    assert cancelled[2][-1]["kind"] == "cancelled"
    assert not unplanned[0]["plan_created_before_answer"]
    reports = {key: R.report_pipeline(run[0], "Тестовый вопрос").splitlines()
               for key, run in (("cancelled", cancelled), ("cancelled_followup", cancelled),
                                ("cancelled_no_plan", unplanned), ("error", failed), ("legacy_stdout", full))}
    fixture = {key: value[2] for key, value in zip(
        ("full", "missing", "remarks", "unsupported", "synth", "resumed",
         "cancelled_followup", "cancelled", "cancelled_no_plan", "error"),
        (full, missing, remarks, unsupported, synth, resumed, cancelled, cancelled, unplanned, failed), strict=True)}
    fixture["legacy_stdout"] = []
    view = (KIT / "cockpit/modules/ask/view.js").as_uri()
    harness = tmp / "p1-ui.mjs"
    harness.write_text(f"const {{mount, markdown}} = await import({view!r});\n"
                       f"const fixtures = {json.dumps(fixture, ensure_ascii=False)};\n"
                       f"const reports = {json.dumps(reports, ensure_ascii=False)};\n" + r"""
class Node {
  constructor() { this.children = []; this.dataset = {}; this.hidden = false; this.value = ''; }
  addEventListener() {}
  set innerHTML(_) { this.replaceChildren(); }
  append(...xs) { this.children.push(...xs); }
  prepend(x) { this.children.unshift(x); }
  replaceChildren(...xs) { this.children = xs; }
  querySelectorAll(selector) {
    if (selector !== '[data-a]') throw new Error(selector);
    return this.children.filter(x => Object.hasOwn(x.dataset, 'a'));
  }
}
globalThis.document = {createElement: () => new Node()};
const nodes = new Map();
const $ = name => {
  if (!nodes.has(name)) nodes.set(name, new Node());
  return nodes.get(name);
};
const requests = [], snapshots = {}, stops = [];
let job, planWarning, poll;
let pollStarted, stopStarted, replyPoll, replyStop;
const ctx = {
  $, root: new Node(), project: {path: 'synthetic'}, lang: 'ru',
  el: (_tag, props, ...children) => Object.assign(new Node(), props, {children}),
  t: (key, data) => key === 'ask.metrics' ? `retries=${data.retries}` : key,
  ui: {copyButton: () => Object.assign(new Node(), {copy: true})},
  toast: message => { throw new Error(message); },
  api: async (path, opts) => {
    requests.push([path, opts?.body ? JSON.parse(opts.body) : null]);
    if (path === '/api/run') { poll = 0; return {job}; }
    if (path.startsWith('/api/job?')) {
      if (job.startsWith('stop_')) {
        if (++poll === 1 && job.startsWith('stop_control')) return {done: false, next: 1,
          lines: ['ASK_EVENT {"kind":"state_ready","state_id":"ui-planning"}']};
        return new Promise(resolve => { replyPoll = resolve; pollStarted(); });
      }
      const lines = fixtures[job].map(event => 'ASK_EVENT ' + JSON.stringify(event));
      const first = fixtures[job].findIndex(event => event.kind === 'plan') + 1 || 1;
      if (++poll === 1) return {lines: lines.slice(0, first), next: first, done: false};
      planWarning = $('#askWarn').textContent;
      const report = reports[job] || [];
      return {lines: lines.slice(first).concat(report), next: lines.length + report.length,
              done: true, rc: report.length && job !== 'legacy_stdout' ? 1 : 0};
    }
    if (path === '/api/job/stop' || path === '/api/ask/control') {
      return new Promise(resolve => { replyStop = resolve; stopStarted(); });
    }
    if (path.startsWith('/api/ask/threads?')) return {threads: []};
    return {error: 'unexpected API call'};
  },
};
$('#askPipeline').checked = true;
$('#askMode').value = 'ask';
mount(ctx);
for (job of Object.keys(fixtures)) {
  if (job === 'resumed') {
    await $('#askRetry').onclick();
  } else {
    if (job !== 'cancelled_followup') $('#askNew').onclick();
    $('#askText').value = 'Тестовый вопрос';
    await $('#askGo').onclick();
  }
  const card = $('#askBody').children[0];
  snapshots[job] = {
    state: $('#askPipelineState').textContent,
    marks: $('#askPlan').children.map(row => row.textContent),
    retryHidden: $('#askRetry').hidden,
    warning: $('#askWarn').textContent,
    planWarning,
    metrics: $('#askMetrics').textContent,
    answer: card.children[1].children.join(''),
    answerSource: card.dataset.a,
    copy: card.children[0].children.some(x => x?.copy === true),
    input: $('#askText').value,
    exportHidden: $('#askExport').hidden,
    exported: markdown(ctx),
  };
}
for (const source of ['control', 'job']) {
  for (const order of ['get_first', 'post_first']) {
    job = `stop_${source}_${order}`;
    $('#askNew').onclick();
    $('#askText').value = 'Вопрос до плана';
    const polling = new Promise(resolve => { pollStarted = resolve; });
    const stopping = new Promise(resolve => { stopStarted = resolve; });
    const run = $('#askGo').onclick();
    await polling;
    const planning = $('#askPipelineState').textContent;
    const cancel = $('#askCancel').onclick();
    await stopping;
    const request = requests.at(-1);
    const finish = () => replyPoll({done: true, rc: 143, state: source === 'job' ? 'stopped' : 'done',
      lines: source === 'job' ? [] : ['ASK_EVENT {"kind":"cancelled","state_id":"ui-planning","resumable":true}']});
    if (order === 'get_first') { finish(); await run; replyStop({ok: true}); await cancel; }
    else { replyStop({ok: true}); await cancel; finish(); await run; }
    stops.push({source, order, planning, request, state: $('#askPipelineState').textContent,
      retryHidden: $('#askRetry').hidden, answer: $('#askBody').children[0].dataset.a,
      input: $('#askText').value, exportHidden: $('#askExport').hidden});
  }
}
console.log(JSON.stringify({requests, snapshots, stops}));
""", encoding="utf-8")
    cp = subprocess.run([node, str(harness)], capture_output=True, text=True, encoding="utf-8",
                        timeout=30, check=False)
    assert cp.returncode == 0, cp.stderr
    observed = json.loads(cp.stdout)
    rows = observed["snapshots"]
    assert rows["full"]["state"] == "ask.pipeline_done" and rows["full"]["retryHidden"] is True, rows
    assert rows["full"]["marks"][0].startswith("✅") and rows["full"]["metrics"] == "retries=1", rows
    assert "№1->№3" in rows["full"]["warning"] == rows["full"]["planWarning"], rows
    for key, proof in (("missing", "Момус: без вердикта"), ("remarks", "Критик: замечания"),
                       ("unsupported", "Момус: без опоры 2")):
        row = rows[key]
        assert row["state"] == "ask.pipeline_partial" and row["retryHidden"] is False, row
        assert row["marks"][0].startswith("◐") and proof in row["answer"], row
        assert bool(row["warning"]) == (key == "missing") and row["metrics"] == "retries=0", row
    assert rows["synth"]["state"] == "ask.pipeline_partial" and rows["synth"]["marks"][0].startswith("✅")
    assert rows["resumed"]["state"] == "ask.pipeline_done" and rows["resumed"]["retryHidden"] is True, rows
    assert rows["resumed"]["warning"] == "" and "Момус: чисто" in rows["resumed"]["answer"], rows
    assert all(path in ("/api/run", "/api/ask/control", "/api/job/stop")
               or path.startswith(("/api/job?", "/api/ask/threads?"))
               for path, _ in observed["requests"]), observed["requests"]
    request, = [body for path, body in observed["requests"]
                if path == "/api/run" and "--resume" in body["args"]]
    assert request["cmd"] == "agent:ask" and "--emit-events" in request["args"], request
    assert request["args"][request["args"].index("--resume") + 1:][:2] == ["--thread", "ui-synth"], request
    for key in ("cancelled", "cancelled_followup", "cancelled_no_plan", "error"):
        row = rows[key]
        status = "error" if key == "error" else "cancelled"
        assert row["state"] == "ask.pipeline_" + status, row
        assert row["retryHidden"] is (key == "cancelled_no_plan"), row
        assert row["answerSource"] == "" and not row["copy"], row
        assert row["input"] == "Тестовый вопрос" and row["warning"] == row["answer"], row
        assert row["warning"] not in row["exported"] and "Ответа нет" not in row["exported"], row
        assert row["exportHidden"] is (key != "cancelled_followup"), row
    assert rows["resumed"]["answerSource"] in rows["cancelled_followup"]["exported"]
    legacy = rows["legacy_stdout"]
    assert legacy["state"] == "ask.pipeline_done" and legacy["input"] == "" and legacy["copy"], legacy
    assert legacy["answerSource"] == full[0]["answer"] and legacy["answerSource"] in legacy["exported"], legacy
    assert len(observed["stops"]) == 4
    for stop in observed["stops"]:
        expected = (["/api/ask/control", {"project": "synthetic", "id": "ui-planning", "action": "cancel"}]
                    if stop["source"] == "control" else ["/api/job/stop", {"id": f"stop_job_{stop['order']}"}])
        assert stop["planning"] == "ask.pipeline_planning" and stop["request"] == expected, stop
        assert stop["state"] == "ask.pipeline_cancelled" and stop["retryHidden"] is (stop["source"] == "job"), stop
        assert stop["answer"] == "" and stop["input"] == "Вопрос до плана" and stop["exportHidden"], stop


@test
def test_ask_rc_resume_first_question_without_journal(tmp: Path):
    import agent_runner as R
    import ask_pipeline as P

    root = _project(tmp)
    failed, state, _ = _p1_run(root, _p1_call(worker={"ok": False}), ident="first-interrupted")
    assert failed["status"] == "error" and failed["plan_created_before_answer"], failed
    journal = R.thread_path(str(root), state.id)
    saved_plan = state.plan()
    assert not journal.exists()
    question = saved_plan["question"]
    argv = ["agent_runner", "--task", "ask", "--pipeline", "--thread", state.id,
            "--question", question]
    call = mock.Mock(side_effect=_p1_call(), spec=_fake)
    with (mock.patch.object(sys, "argv", argv), mock.patch("os.getcwd", return_value=str(root)),
          mock.patch.object(R.AG, "config", return_value=_p1_cfg()),
          mock.patch.object(R.AG, "call_role", call),
          mock.patch.object(P.AC, "search_context", side_effect=AssertionError("resume fetched context"))):
        assert R.main() == 1  # A refinement without a journal is still rejected.
        argv.extend(["--resume", "--question", "Другой вопрос"])
        assert R.main() == 1
        call.assert_not_called()
        assert state.plan() == saved_plan and not journal.exists()
        argv[-1] = question
        assert R.main() == 0, "explicit resume rejected a saved first plan without a journal"
    assert [c.args[1] for c in call.call_args_list] == ["worker", "critic", "qa", "planner"]
    assert "Синтезируй" in call.call_args.args[2][0]["content"]
    assert state.plan()["version"] == saved_plan["version"]
    turns = R.read_thread(journal)
    assert len(turns) == 1 and turns[0]["q"] == question and turns[0]["a"], turns


@test
def test_ask_rc_resume_clears_terminal_controls(tmp: Path):
    import ask_state as S

    root = _project(tmp)
    ident = "cancel-first"

    def interrupt(cfg, role, messages, **kw):
        S.AskState(root, ident).set_control(pause=True, cancel=True)
        return _fake(cfg, role, messages, **kw)

    first, state, _ = _p1_run(root, _p1_call(worker=interrupt), ident=ident)
    assert first["status"] == "cancelled" and state.control()["cancel"] and state.control()["pause"]
    resumed, restored, _ = _p1_run(root, _p1_call(), ident=ident, resume=True)
    assert resumed["ok"], resumed
    assert not restored.control()["cancel"] and not restored.control()["pause"]


def _ask_cli_events(output):
    return [json.loads(line.removeprefix("ASK_EVENT ")) for line in output.getvalue().splitlines()
            if line.startswith("ASK_EVENT ")]


def _ask_endpoint(ck, project, path, body=None, *, before_reply=None):
    """Dispatch the actual authenticated-session route without binding a socket."""
    handler = object.__new__(ck.Handler)
    handler.path = path
    handler.auth = SimpleNamespace(kind="session")
    handler.guarded = lambda _q: True
    handler.need_project = lambda p: str(project) == p
    handler.read_json_body = lambda: body
    replies = []

    def send_json(payload, status=200, _headers=None):
        if before_reply:
            before_reply()
        replies.append((status, payload))

    handler.send_json = send_json
    (handler.do_GET if body is None else handler.do_POST)()
    reply, = replies
    return reply


@test
def test_ask_c_publishes_state_before_planner_and_resumes_its_own_question(tmp: Path):
    import agent_runner as R
    import ask_pipeline as P
    import ask_state as S
    import aurora_cockpit as CK

    root = _project(tmp)
    question = "Какой срок у заявки?"
    args = ["agent_runner", "--task", "ask", "--pipeline", "--emit-events", "--question", question]
    output = StringIO()
    during_planning = []

    def planner(cfg, role, messages, **kw):
        events = _ask_cli_events(output)
        ready = next((event for event in events if event["kind"] == "state_ready"), {})
        ident = ready.get("state_id")
        if not ident:
            saved = [p.parent.name for p in S.root_for(root).glob("*/manifest.json")]
            during_planning.append(("state_id not yet published", saved))
        else:
            state = S.AskState(root, ident)
            during_planning.append((events, state.plan_path.is_file(), state.plan().get("question"),
                                    state.manifest().get("question"), _ask_endpoint(
                                        CK, root, "/api/ask/control", {"project": str(root),
                                                                      "id": ident, "action": "cancel"})))
        return _fake(cfg, role, messages, **kw)

    call = mock.Mock(side_effect=_p1_call(planner=planner), spec=_fake)
    with (mock.patch.object(sys, "argv", args), mock.patch("os.getcwd", return_value=str(root)),
          mock.patch.object(R.AG, "config", return_value=_p1_cfg()),
          mock.patch.object(R.AG, "call_role", call),
          mock.patch.object(P.AC, "search_context", return_value={"ok": True, "pack": _P1_PACK}) as search,
          redirect_stdout(output)):
        rc = R.main()
        events = _ask_cli_events(output)
        kinds = [e["kind"] for e in events]
        assert kinds == ["state_ready", "cancelled"], (rc, kinds, during_planning)
        ready, cancelled = events
        ident = ready["state_id"]
        snapshot, has_plan, plan_question, manifest_question, (http, control) = during_planning[0]
        assert (snapshot, has_plan, plan_question, manifest_question) == ([ready], True, question, question)
        assert http == 200 and control["ok"] and control["control"]["cancel"], control
        assert rc == 1 and cancelled["state_id"] == ident and cancelled["resumable"], events
        state = S.AskState(root, ident)
        journal = R.thread_path(str(root), ident)
        assert state.state()["status"] == "cancelled" and not journal.exists()
        call.reset_mock()
        call.side_effect = _p1_call()
        args.extend(["--resume", "--thread", ident, "--question", "Другой вопрос"])
        output.seek(0)
        output.truncate()
        assert R.main() == 1 and _ask_cli_events(output)[-1]["error"] == "resume_state"
        call.assert_not_called()
        args[-1] = question
        output.seek(0)
        output.truncate()
        assert R.main() == 0
        restarted = _ask_cli_events(output)
        assert restarted[0] == ready and restarted[-1]["kind"] == "done", restarted
        assert search.call_count == 1 and not state.control()["cancel"]
    turn, = R.read_thread(journal)
    assert turn["q"] == question and turn["a"], turn


@test
def test_ask_c_job_poll_reports_stopped_before_the_stop_response(tmp: Path):
    import aurora_cockpit as CK

    root = _project(tmp)
    script = tmp / "before_ask_state.py"
    script.write_text("import time\nprint('searching', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    command = {"cmd": "agent:ask", "ns": "agent", "runnable": True, "script": "agent_runner.py",
               "flags": ["--pipeline", "--emit-events", "--question"], "fixed_flags": ["--task", "ask"]}
    ready, deliver = threading.Event(), threading.Event()

    def hold_response():
        ready.set()
        assert deliver.wait(10), "test did not release the POST response"

    def poll_until(job, condition):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            status, reply = _ask_endpoint(CK, root, f"/api/job?id={job}")
            assert status == 200, reply
            if condition(reply):
                return reply
            time.sleep(0.01)
        raise AssertionError(reply)

    with (mock.patch.object(CK, "command_by_name", return_value=command),
          mock.patch.object(CK, "script_path", return_value=str(script)),
          mock.patch.object(CK, "python_executable", return_value=sys.executable),
          mock.patch.object(CK, "net_enabled", return_value=False),
          mock.patch.object(CK.NO, "ensure_started"),
          ThreadPoolExecutor(max_workers=1) as pool):
        status, started = _ask_endpoint(CK, root, "/api/run", {
            "project": str(root), "cmd": "agent:ask",
            "args": ["--pipeline", "--emit-events", "--question", "Остановка до плана"]})
        assert status == 200 and started.get("job"), started
        job = started["job"]
        try:
            poll_until(job, lambda reply: "searching" in reply["lines"])
            stop = pool.submit(_ask_endpoint, CK, root, "/api/job/stop", {"id": job},
                               before_reply=hold_response)
            try:
                assert ready.wait(10), "stop did not reach the response boundary"
                polled = poll_until(job, lambda reply: reply["done"])
                assert not stop.done(), "expected the terminal GET before the stop response"
                assert polled["state"] == "stopped" and polled["rc"] != 0, polled
            finally:
                deliver.set()
            assert stop.result(timeout=10) == (200, {"ok": True})
        finally:
            with CK.JOBS_LOCK:
                proc = (CK.JOBS.get(job) or {}).get("proc")
            if proc and proc.poll() is None:
                proc.kill()
                poll_until(job, lambda reply: reply["done"])
            with CK.JOBS_LOCK:
                CK.JOBS.pop(job, None)


@test
def test_ask_f_failed_new_refinement_does_not_claim_or_change_the_previous_plan(tmp: Path):
    import agent_runner as R
    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    args = ["agent_runner", "--task", "ask", "--pipeline", "--emit-events", "--question", "Первый вопрос"]
    call = mock.Mock(side_effect=_p1_call(), spec=_fake)
    output = StringIO()
    with (mock.patch.object(sys, "argv", args), mock.patch("os.getcwd", return_value=str(root)),
          mock.patch.object(R.AG, "config", return_value=_p1_cfg()), mock.patch.object(R.AG, "call_role", call),
          mock.patch.object(P.AC, "search_context", side_effect=[{"ok": True, "pack": _P1_PACK},
                            {"ok": False, "why": "нет карточек"}]), redirect_stdout(output)):
        assert R.main() == 0
        done = _ask_cli_events(output)[-1]
        assert done["kind"] == "done", done
        state = S.AskState(root, done["state_id"])
        journal = R.thread_path(str(root), state.id)
        files = [journal, *(path for path in state.dir.rglob("*") if path.is_file())]
        before = {path: path.read_bytes() for path in files}
        call.reset_mock()
        output.seek(0)
        output.truncate()
        args.extend(["--thread", state.id, "--question", "Несвязанное уточнение"])
        assert R.main() == 1
        call.assert_not_called()
        failure, = _ask_cli_events(output)
        changed = [path.name for path, contents in before.items() if path.read_bytes() != contents]
        added = [path.name for path in state.dir.rglob("*") if path.is_file() and path not in before]
        assert (failure["kind"], failure["error"], failure["resumable"], failure["state_id"], changed, added) == (
            "error", "search_empty", False, "", [], []), (failure, changed, added)


@test
def test_pipeline_refinement_passes_original_markdown_as_messages(tmp: Path):
    import agent_runner as R
    import ask_pipeline as P

    root = _project(tmp)
    source = "## Контекст\n\n1. Пункт [[Заявка-0]].  \n\n---\n\n```txt\n  строка\n```\n"
    R.append_turn(R.thread_path(str(root), "refine"), "Первый вопрос", source, "fixture", "ask")
    argv = ["agent_runner", "--task", "ask", "--pipeline", "--thread", "refine",
            "--question", "Уточнение", "--no-journal"]
    with (mock.patch.object(sys, "argv", argv), mock.patch("os.getcwd", return_value=str(root)),
          mock.patch.object(R.AG, "config", return_value=_p1_cfg()),
          mock.patch.object(P, "run", return_value={"ok": True, "answer": "Уточнено"}) as pipeline):
        assert R.main() == 0
    assert pipeline.call_args.kwargs["history"] == [
        {"role": "user", "content": "Первый вопрос"}, {"role": "assistant", "content": source}]


@test
def test_p1_check_parsers_use_strict_final_evidence(tmp: Path):
    import ask_pipeline as P

    for text, expected in (
        ("\ufeffанализ\r  вердикт: ЧИСТО.  \r\n\t\n", ("pass", 0)),
        ("ВЕРДИКТ: ЧИСТО   \n\t\n", ("pass", 0)),
        ("анализ\r\nВЕРДИКТ: ЧИСТО\r\n", ("pass", 0)),
        ("ВЕРДИКТ: ЧИСТО\r\nВЕРДИКТ: БЕЗ  ОПОРЫ 2!", ("unsupported", 2)),
        ("ВЕРДИКТ: БЕЗ ОПОРЫ 0", ("unsupported", 0)),
        ("ВЕРДИКТ: ЧИСТО\nВЕРДИКТ: БЕЗ ОПОРЫ ", ("missing", 0)),
        ("ВЕРДИКТ: ЧИСТО\nпроверка завершена", ("missing", 0)),
        ("ВЕРДИКТ: ЧИСТО, потому что…", ("missing", 0)),
        ("рассуждение без итога", ("missing", 0)),
    ):
        assert P.momus_verdict(text) == expected, (text, expected)
    for text, expected in (
        ('{"ok":true}', "pass"),
        ('{"ok":false,"issues":[]}', "remarks"),
        ('{"ok":true,"issues":["проверить"]}', "remarks"),
        ('{"ok":"true"}', "missing"),
        ('{"ok":"false"}', "missing"),
        ('{"ok":1}', "missing"),
        ('{"ok":true,"issues":null}', "missing"),
        ('проза {"ok":true}', "missing"),
        ('[true]', "missing"),
        ('[' * 2000 + 'true' + ']' * 2000, "missing"),
    ):
        assert P.critic_verdict({"ok": True, "text": text}) == expected, (text, expected)


@test
def test_p1_route_policy_is_shared_by_the_five_pipeline_stages(tmp: Path):
    root = _project(tmp)
    stub = _p1_call()

    def call(cfg, role, messages, **kw):
        return {**stub(cfg, role, messages, **kw), "backend": 3,
                "ring_trace": _p1_trace([1, 3], [1, 3])}

    stages = ("planner", "worker", "critic", "qa", "synth")
    notices = [f"routing_fallback:{stage}:1->3" for stage in stages]
    outcome = _p1_run(root, call, ident="stages")
    res = _p1_outcome(outcome, notices=notices, warnings=5, retries=5)
    for warning, stage in zip(res["warnings"], stages, strict=True):
        assert f"«{stage}»" in warning and "№1->№3" in warning, warning
    assert next(e for e in outcome[2] if e["kind"] == "plan")["warnings"] == res["warnings"][:1]
    assert res["completion"]["steps"]["done"] == 1 and res["completion"]["checks"]["passed"] == 2


@test
def test_p1_resume_keeps_exact_writer_body_with_markdown_headings(tmp: Path):
    root = _project(tmp)
    writer_text = "Срок 10 дней.\n\n## Ответ\n\nУточнение: рабочие дни."
    planner = {"ok": True, "text": json.dumps({"steps": [
        {"id": "s1", "title": "Пункт 1", "brief": "Найди срок.\n## Ответ\nТекст плана."}]})}
    first = _p1_run(root, _p1_call(planner=planner, worker={"ok": True, "text": writer_text}))
    for outcome in (first, _p1_resume(first[1])):
        res = _p1_outcome(outcome)
        assert res["steps"][0]["answer"] == writer_text, res["steps"]
    first[1].mark_step("s1", answer_chars=len(writer_text) + 1)
    res, state, events = _p1_run(root, _p1_call(), ident=first[1].id, resume=True)
    assert res["error"]["code"] == "no_answer" and res["steps"][0]["status"] == "failed", res
    assert state.plan()["steps"][0]["status"] == "failed" and not any(e["kind"] == "done" for e in events)
