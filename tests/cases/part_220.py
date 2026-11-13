#!/usr/bin/env python3
"""part_220 - режимы отказа: процедура завершается, ответ не пустой и понятен.

Проверяем пять критичных случаев из миссии: LLM недоступна, поиск пуст, grill ждёт
человека, шаблон артефакта отсутствует, общий срок истёк. Живая модель не нужна.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from harness import KIT, card, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
import answer_core as AC  # noqa: E402
import production_core as PC  # noqa: E402
import reliable_core as RC  # noqa: E402


def _cfg() -> dict:
    import agent_core as A
    return A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})


@test
def test_ask_llm_failure_is_a_readable_error_not_an_exception(_t):
    import agent_runner as R
    real = R.run_command
    R.run_command = lambda cwd, script, args, **kw: {
        "ok": True, "out": "# Пак (карточек 1)\n\n## Заявка-1\n\nСрок десять дней."}
    try:
        def down(cfg, role, messages, **kw):
            return {"ok": False, "log": ["№1 m: URLError: timed out", "дедлайн исчерпан"]}
        t0 = time.time()
        res = R.run_ask({"request_timeout": 5}, str(_t), "срок?", "ask", 40,
                        call=down, momus=False)
    finally:
        R.run_command = real
    assert not res["ok"] and res["status"] == "error", res
    assert res.get("error", {}).get("code") == "llm_unavailable", res
    assert "Не удалось получить результат" in res["why"] and res["why"].strip()
    assert time.time() - t0 < 3


@test
def test_ask_empty_search_says_so_and_does_not_return_blank(_t):
    import agent_runner as R
    real = R.run_command
    R.run_command = lambda cwd, script, args, **kw: {
        "ok": False, "out": "ctx_pack: в базе не найдено ни одной карточки"}
    try:
        res = R.run_ask({"request_timeout": 5}, str(_t), "квантовая криптография",
                        "ask", 40, call=lambda *a, **k: {"ok": True, "text": "x"},
                        momus=False)
    finally:
        R.run_command = real
    assert not res["ok"] and res["status"] == "error", res
    assert "не найдено" in res["why"] and res["error"]["message"]


@test
def test_ask_keeps_the_answer_when_momus_cannot_check_it(_t):
    import agent_runner as R
    real = R.run_command
    R.run_command = lambda cwd, script, args, **kw: {
        "ok": True, "out": "# Пак (карточек 1)\n\n## Заявка-1\n\nСрок десять дней."}

    def fake(cfg, role, messages, **kw):
        if role == "qa":
            return {"ok": False, "log": ["№1 m: дедлайн исчерпан"]}
        return {"ok": True, "text": "Срок десять дней [[Заявка-1]].", "backend": 1,
                "model": "m", "log": []}
    try:
        res = R.run_ask({"request_timeout": 5}, str(_t), "срок?", "ask", 40,
                        call=fake, momus=True)
    finally:
        R.run_command = real
    assert res["ok"] and res["status"] == "partial", res
    assert res["answer"] and "momus" in res
    assert any(x.startswith("momus") for x in res["degraded"]), res


@test
def test_core_deadline_is_bounded_even_if_the_model_ignores_its_deadline(_t):
    def slow(cfg, role, messages, **kw):
        time.sleep(3)
        return {"ok": True, "text": "поздно", "backend": 1, "model": "m", "log": []}

    t0 = time.time()
    res = AC.answer("вопрос", cfg={"request_timeout": 0.2}, cwd=str(_t), call=slow,
                    pack="# Пак\n\n## К\n\nтекст", prompt="P", prepare=False, momus=False,
                    deadline=time.time() + 0.2)
    spent = time.time() - t0
    assert not res["ok"] and res["status"] == "error", res
    assert spent < 1.5, f"ядро ждало модель {spent:.1f} с"
    assert "не уложился" in res["why"] or res["error"]["code"] == "llm_timeout", res


@test
def test_run_make_missing_template_names_the_template_and_stops(tmp: Path):
    root = make_project(tmp)
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/Нет.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    import agent_runner as R
    t0 = time.time()
    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=lambda *a, **k: {"ok": False, "log": []}, momus=False)
    assert not res["ok"] and res["stage"] == "error" or not res["ok"], res
    assert "Нет.md" in res["why"], res
    assert time.time() - t0 < 3


@test
def test_run_make_keeps_an_early_partial_artifact_when_the_writer_fails(tmp: Path):
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Заявка-{i}.md", "Срок заявки десять дней.", status="knowledge")

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return {"ok": True, "text": json.dumps({"questions": [], "plan": "1. Критерии"}),
                    "backend": 1, "model": "m", "log": []}
        return {"ok": False, "log": ["№1 m: URLError: connection refused"]}

    import agent_runner as R
    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=False)
    assert not res["ok"] and res["status"] == "partial", res
    assert res["path"] and (root / res["path"]).is_file(), res
    assert "Не удалось получить результат" in res["why"] or "воркер" in res["why"], res


@test
def test_run_make_keeps_the_draft_when_the_critic_or_momus_fails(tmp: Path):
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Заявка-{i}.md", "Срок заявки десять дней.", status="knowledge")
    import agent_runner as R

    def critic_down(cfg, role, messages, **kw):
        if role == "planner":
            return {"ok": True, "text": json.dumps({"questions": [], "plan": "1. Критерии"}),
                    "backend": 1, "model": "m", "log": []}
        if role == "critic":
            return {"ok": False, "log": ["№1 m: critic down"]}
        return {"ok": True, "text": "# AC\n\nСрок десять дней [[Заявка-0]].",
                "backend": 1, "model": "m", "log": []}

    got = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=critic_down, momus=False)
    assert not got["ok"] and got["status"] == "partial" and got["path"], got
    assert (root / got["path"]).is_file()

    def momus_down(cfg, role, messages, **kw):
        if role == "planner":
            return {"ok": True, "text": json.dumps({"questions": [], "plan": "1. Критерии"}),
                    "backend": 1, "model": "m", "log": []}
        if role == "critic":
            return {"ok": True, "text": json.dumps({"ok": True, "issues": []}),
                    "backend": 1, "model": "m", "log": []}
        if role == "qa":
            return {"ok": False, "log": ["№1 m: momus down"]}
        return {"ok": True, "text": "# AC\n\nСрок десять дней [[Заявка-0]].",
                "backend": 1, "model": "m", "log": []}

    got = R.run_make(_cfg(), str(root), "ac", "статусы заявки", "", "", True,
                     call=momus_down, momus=True)
    assert not got["ok"] and got["status"] == "partial" and got["path"], got
    assert (root / got["path"]).is_file()


@test
def test_run_make_turns_a_write_failure_into_a_message(tmp: Path):
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Заявка-{i}.md", "Срок заявки десять дней.", status="knowledge")
    import agent_runner as R
    real = R.write_artifact

    def broken(*a, **kw):
        raise PermissionError("нет прав на папку")

    R.write_artifact = broken
    try:
        res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                         call=lambda *a, **k: {"ok": True, "text": "x"}, momus=False)
    finally:
        R.write_artifact = real
    assert not res["ok"] and res["error"]["code"] == "artifact_write", res
    assert "нет прав" in res["why"] and res["status"] == "error", res


@test
def test_run_make_stops_a_hanging_planner_within_the_overall_deadline(tmp: Path):
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Заявка-{i}.md", "Срок заявки десять дней.", status="knowledge")
    import agent_runner as R

    def slow_planner(cfg, role, messages, **kw):
        if role == "planner":
            time.sleep(6)
        return {"ok": True, "text": json.dumps({"questions": [], "plan": "1. Критерии"}),
                "backend": 1, "model": "m", "log": []}

    cfg = _cfg()
    # Обогащение идёт до планировщика: дедлайн должен пережить его, иначе проверка
    # ловит медленный старт, а не зависший planning.
    cfg.setdefault("make_pipeline", {})["hard_timeout_seconds"] = 5
    t0 = time.time()
    res = R.run_make(cfg, str(root), "ac", "срок заявки", "", "", True,
                     call=slow_planner, momus=False)
    assert not res["ok"] and res["stage"] == "planning", res
    assert time.time() - t0 < 7
    assert "не уложился" in res["why"] or res["error"]["code"] == "timeout", res


@test
def test_grill_without_an_answer_is_an_explicit_requires_input_state(_t):
    got = PC.chain("вопрос", search_fn=lambda q: {"ok": True, "pack": "# Пак\n\n## К\n\nтекст"},
                   grill_fn=lambda p: {"questions": [{"q": "Срок?", "rec": "10 дней"}], "plan": ""},
                   prompt_fn=lambda *a: "p", answer_fn=lambda *a: {"ok": True, "answer": "x"},
                   render_fn=lambda *a: {"ok": True, "path": "x"})
    assert got["ok"] and got["status"] == "requires_input", got
    assert got["questions"][0]["q"] == "Срок?"


@test
def test_reliable_stage_returns_timeout_in_bounded_time(_t):
    t0 = time.time()
    stage = RC.run_stage("slow", lambda: time.sleep(3), timeout=0.2,
                         deadline=time.time() + 0.2)
    assert not stage.ok and stage.timed_out and stage.error["code"] == "timeout", stage
    assert time.time() - t0 < 1.5
