#!/usr/bin/env python3
"""part_218 - переиспользуемое ядро ответа.

Проверяем контракт без живой модели и без сети: поиск можно подменить, пак дать готовым,
а вызов модели - обычной функцией. Ядро должно вернуть ответ с проверками или понятную
ошибку, но не пустоту и не исключение.
"""
from __future__ import annotations

import sys

from harness import KIT, test

sys.path.insert(0, str(KIT / "scripts"))
import answer_core as AC  # noqa: E402

PACK = """# Context pack: заявка

## Заявка-1

Заявка проходит согласование. Срок десять дней.

## Заявка-2

Повторная заявка отклоняется.
"""


def _call(text: str = "Срок десять дней [[Заявка-1]]."):
    def fake(cfg, role, messages, **kw):
        return {"ok": True, "text": text, "backend": 1, "model": "m",
                "seconds": 0.2, "tokens_in": 120, "tokens_out": 12}
    return fake


@test
def test_core_answers_from_an_incoming_pack_without_knowing_its_source(_t):
    seen = {}

    def fake(cfg, role, messages, **kw):
        seen["prompt"] = messages[0]["content"]
        seen["role"] = role
        return {"ok": True, "text": "Срок десять дней [[Заявка-1]].", "backend": 1,
                "model": "m", "seconds": 0.2, "tokens_in": 120, "tokens_out": 12}

    res = AC.answer("Какой срок?", cfg={"request_timeout": 30}, cwd=str(_t),
                    call=fake, pack=PACK, prompt="Промпт вопроса", prepare=False,
                    momus=False)
    assert res["ok"] and res["status"] == "full", res
    assert res["answer"].startswith("Срок") and res["cards"] == ["Заявка-1", "Заявка-2"]
    assert res["citations"]["checked"] == 0 and res["checks"]["links"]["invented"] == []
    assert seen["prompt"] == "Промпт вопроса" and seen["role"] == "worker"


@test
def test_core_builds_the_prompt_from_the_working_pack(_t):
    def fake(cfg, role, messages, **kw):
        return {"ok": True, "text": "ответ", "backend": 1, "model": "m", "log": []}

    res = AC.answer("вопрос", cfg={"request_timeout": 30}, cwd=str(_t), call=fake,
                    pack=PACK, prompt_builder=lambda pack, q, h: f"{q}:{pack}",
                    prepare=False, momus=False)
    assert res["ok"], res
    assert res["answer"] == "ответ"


@test
def test_core_search_returns_cards_and_a_human_reason_on_empty(_t):
    def found(cwd, script, args, **kw):
        assert script == "ctx_pack.py" and args[0] == "срок"
        return {"ok": True, "out": "# Пак (карточек 2)\n\n## Заявка-1\nтекст\n\n## Заявка-2\nтекст"}

    got = AC.search_context(str(_t), "срок", runner=found)
    assert got["ok"] and got["cards"] == ["Заявка-1", "Заявка-2"] and got["total"] == 2

    def empty(cwd, script, args, **kw):
        return {"ok": False, "out": "ctx_pack: в базе нет карточек"}

    bad = AC.search_context(str(_t), "нет", runner=empty)
    assert not bad["ok"] and "нет карточек" in bad["why"]
    assert bad["error"]["message"].startswith("Не удалось получить результат")


@test
def test_core_keeps_a_partial_answer_when_the_verifier_times_out(_t):
    def verifier(answer, pack, question):
        return {"ok": False, "timed_out": True, "why": "Момус не успел"}

    res = AC.answer("вопрос", cfg={"request_timeout": 30}, cwd=str(_t), call=_call(),
                    pack=PACK, prompt="P", prepare=False, momus=True,
                    verifier=verifier)
    assert res["ok"] and res["status"] == "partial", res
    assert res["answer"].startswith("Срок")
    assert "momus_timeout" in res["degraded"] and res.get("degraded_note"), res


@test
def test_core_turns_llm_failure_and_empty_answer_into_readable_errors(_t):
    def down(cfg, role, messages, **kw):
        return {"ok": False, "log": ["№1 m: URLError: timed out", "дедлайн исчерпан"]}

    bad = AC.answer("вопрос", cfg={"request_timeout": 30}, cwd=str(_t), call=down,
                    pack=PACK, prompt="P", prepare=False, momus=False)
    assert not bad["ok"] and bad["status"] == "error", bad
    assert "Не удалось получить результат" in bad["why"] and "timed out" in bad["why"]
    assert bad["error"]["next_step"]

    def empty(cfg, role, messages, **kw):
        return {"ok": True, "text": "  ", "backend": 1, "model": "m", "log": []}

    blank = AC.answer("вопрос", cfg={"request_timeout": 30}, cwd=str(_t), call=empty,
                      pack=PACK, prompt="P", prepare=False, momus=False)
    assert not blank["ok"] and "пустой ответ" in blank["why"]


@test
def test_core_retries_once_without_history_and_marks_the_fallback(_t):
    seen = []

    def flaky(cfg, role, messages, **kw):
        seen.append(bool(kw.get("history")))
        if len(seen) == 1:
            return {"ok": False, "log": ["№1 m: запрос слишком велик"]}
        return {"ok": True, "text": "Срок десять дней [[Заявка-1]].", "backend": 1,
                "model": "m", "log": []}

    res = AC.answer("вопрос", cfg={"request_timeout": 30}, cwd=str(_t), call=flaky,
                    pack=PACK, prompt="P", prepare=False, momus=False,
                    history=[{"role": "user", "content": "старый"}])
    assert res["ok"] and res["status"] == "partial", res
    assert res["fallback"]["without_history"] is True
    assert "fallback_without_history" in res["degraded"]
    assert seen == [True, False], seen


@test
def test_core_does_not_let_an_exception_or_empty_question_escape(_t):
    def boom(cfg, role, messages, **kw):
        raise RuntimeError("сеть легла")

    bad = AC.answer("вопрос", cfg={"request_timeout": 30}, cwd=str(_t), call=boom,
                    pack=PACK, prompt="P", prepare=False, momus=False)
    assert not bad["ok"] and bad["status"] == "error" and "сеть легла" in bad["why"]

    empty = AC.answer("", cfg={"request_timeout": 30}, cwd=str(_t), call=_call(),
                      pack=PACK, prompt="P", prepare=False, momus=False)
    assert not empty["ok"] and "вопрос пуст" in empty["why"]
