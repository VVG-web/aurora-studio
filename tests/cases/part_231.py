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
import time
from pathlib import Path

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
    res = P.run(_cfg(), str(root), "Какой срок у заявки?", pack=pack,
                call=_fake, thread_id="pipeline-test", momus=True)
    assert res["ok"], res
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
    import threading

    import ask_pipeline as P
    import ask_state as S

    root = _project(tmp)
    state = S.AskState(str(root), "pause-test", "пауза")
    state.create("пауза", {"title": "П", "steps": [
        {"id": "s1", "title": "Один", "pack": "контекст"}]})
    state.set_control(pause=True)

    def call(cfg, role, messages, **kw):
        return _fake(cfg, role, messages, **kw)

    box = {}

    def go():
        box["res"] = P.run(_cfg(seconds=10), str(root), "пауза",
                            pack="# Пак\n\n## Заявка-0\n\nСрок 10 дней.",
                            call=call, state=state, thread_id="pause-test")

    thread = threading.Thread(target=go)
    thread.start()
    time.sleep(0.25)
    assert state.control()["pause"], "пауза не удержала шаг"
    state.set_control(pause=False)
    thread.join(timeout=10)
    assert not thread.is_alive(), "пайплайн не проснулся"
    res = box.get("res") or {}
    assert res.get("ok"), res


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
