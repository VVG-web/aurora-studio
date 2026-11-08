#!/usr/bin/env python3
"""part_219 - надстройка «Продуктивности»: поиск, grill, промпт, ядро, артефакт.

Цепочка проверяется с подставными шагами, а интеграция - на настоящем `run_make` с
заглушкой модели. Живая база и сеть не нужны.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
import answer_core as AC  # noqa: E402
import production_core as PC  # noqa: E402

PACK = "# Пак (карточек 1)\n\n## Заявка-1\n\nСрок десять дней."


def _fake_answer(text: str = "# Док\n\nСрок десять дней [[Заявка-1]]."):
    def call(cfg, role, messages, **kw):
        return {"ok": True, "text": text, "backend": 1, "model": "m",
                "seconds": 0.1, "tokens_in": 50, "tokens_out": 8}
    return call


@test
def test_production_chain_goes_from_search_to_artifact(_t):
    seen = {}

    def search(question):
        seen["question"] = question
        return {"ok": True, "pack": PACK, "cards": ["Заявка-1"], "total": 1}

    def grill(pack):
        seen["grill_pack"] = pack
        return {"questions": [], "plan": "1. Критерии", "assumptions": []}

    def prompt(pack, grilled):
        seen["prompt"] = PC.build_prompt(
            "# {title}\n{plan}\n{template}\n{extra}\n{pack}\n{agnostic}",
            title="AC", plan=grilled["plan"], form="шаблон", extra="", pack=pack,
            agnostic="")
        return seen["prompt"]

    def answer(pack, prompt_text, grilled):
        seen["answer_pack"] = pack
        return AC.answer("срок", cfg={"request_timeout": 20}, cwd=str(_t),
                         call=_fake_answer(), pack=pack, prompt=prompt_text,
                         prepare=False, momus=False)

    def render(text, grilled, answered):
        path = Path(_t) / "ArtifactsCode" / "ac" / "doc.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"ok": True, "path": str(path)}

    res = PC.chain("срок", search_fn=search, grill_fn=grill, prompt_fn=prompt,
                   answer_fn=answer, render_fn=render)
    assert res["ok"] and res["status"] == "done" and res["stage"] == "artifact", res
    assert seen["question"] == "срок" and seen["grill_pack"] == PACK
    assert "1. Критерии" in seen["prompt"] and "Срок десять дней" in seen["answer_pack"]
    assert "Срок десять дней" in (Path(_t) / "ArtifactsCode/ac/doc.md").read_text(encoding="utf-8")


@test
def test_production_chain_reports_grill_input_and_empty_search_without_inventing(_t):
    def search_ok(question):
        return {"ok": True, "pack": PACK}

    def grill_questions(pack):
        return {"questions": [{"q": "Какой срок?", "rec": "10 дней"}], "plan": ""}

    got = PC.chain("срок", search_fn=search_ok, grill_fn=grill_questions,
                   prompt_fn=lambda *a: "p", answer_fn=lambda *a: {"ok": True, "answer": "x"},
                   render_fn=lambda *a: {"ok": True, "path": "x"})
    assert got["ok"] and got["status"] == "requires_input" and got["questions"], got

    def search_empty(question):
        return {"ok": False, "why": "в базе не найдено", "error": {
            "message": "Не удалось получить результат, потому что в базе не найдено."}}

    bad = PC.chain("нет", search_fn=search_empty, grill_fn=lambda p: {},
                   prompt_fn=lambda *a: "", answer_fn=lambda *a: {"ok": True, "answer": ""},
                   render_fn=lambda *a: {"ok": True})
    assert not bad["ok"] and bad["stage"] == "search"
    assert "в базе не найдено" in bad["error"]["message"]


@test
def test_production_chain_does_not_call_the_model_before_the_plan_is_ready(_t):
    called = {"answer": 0}

    def answer(*a):
        called["answer"] += 1
        return {"ok": True, "answer": "x"}

    got = PC.chain("q", search_fn=lambda q: {"ok": True, "pack": PACK},
                   grill_fn=lambda p: {"questions": [{"q": "?"}], "plan": ""},
                   prompt_fn=lambda *a: "p", answer_fn=answer,
                   render_fn=lambda *a: {"ok": True, "path": "x"})
    assert got["status"] == "requires_input" and called["answer"] == 0


@test
def test_run_make_uses_the_answer_core_for_the_document(tmp: Path):
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    from harness import card
    for i in range(4):
        card(root, f"Concepts/Заявка-{i}.md",
             f"Заявка проходит согласование. Срок {10 + i} дней. [[Заявка-{(i + 1) % 4}]]",
             status="knowledge", kind="knowledge")

    def fake(cfg_, role, messages, **kw):
        base = {"backend": 1, "model": "m", "tps": 9, "log": [], "seconds": 0.1}
        if role == "planner":
            return dict(base, ok=True, text=json.dumps({"questions": [], "plan": "1. Критерии"},
                                                        ensure_ascii=False))
        if role == "critic":
            return dict(base, ok=True, text=json.dumps({"ok": True, "issues": []},
                                                        ensure_ascii=False))
        return dict(base, ok=True, text="# AC\n\nСрок десять дней [[Заявка-0]].")

    cfg = __import__("agent_core").parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    import agent_runner as R
    res = R.run_make(cfg, str(root), "ac", "статусы заявки", "", "", True,
                     call=fake, momus=False)
    assert res["ok"] and res["stage"] == "done", res
    st = R.load_session(str(root), res["sid"])
    assert st["core"]["used"] is True and st["core"]["status"] == "full", st.get("core")
    assert st["core"]["cards"] >= 1
    assert (root / st["path"]).is_file()
