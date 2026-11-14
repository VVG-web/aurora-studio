"""Проверки движка: формы ответа, быстрая роль, батчи и реранкер (часть 237).

Ни один тест не ходит в сеть: ответы моделей и `/rerank` подменяются. Проверяется
то, что можно проверить без модели: форма JSON, порядок сообщений, выбор быстрой
модели по замеру, разбор батча и откат реранкера.
"""
import json
import os
import sys
import time
from pathlib import Path

from harness import SCRIPTS, card, make_project, test


@test
def test_schemas_accept_the_engine_shapes(tmp):
    """Один перечень полей на адаптер и прямой HTTP: битая форма не проходит."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_schemas as S

    assert S.validate("BuildPlan", {"cards": [{"title": "X", "sections": "1"}]})[0]
    assert S.validate("BuildPlan", {"cards": [{"sections": "1"}]})[0] is False
    assert S.validate("BuildPlan", {"cards": [{"title": "X", "sections": "1",
                                               "to": "Concepts", "summary": "о чём"}]})[0]
    assert S.validate("BuildPlan", {"cards": [{"title": "X", "sections": "1", "to": 1}]})[0] is False
    assert S.validate("BuildPlan", {"empty": "одно оглавление"})[0]
    assert S.validate("CriticVerdict", {"ok": True, "why": "", "issues": []})[0]
    assert S.validate("CriticVerdict", {"ok": "да", "why": "", "issues": []})[0] is False
    assert S.validate("ExtractResult", {"extract": [{"term": "X", "definition": "Y"}]})[0]
    assert S.validate("RelinkResult", {"links": [{"phrase": "X", "card": "Y"}]})[0]
    assert S.validate("DistillResult", {"thesis": "X"})[0]
    assert S.validate("TranslitBatch", {"items": [{"id": 1, "cyrillic": "Икс"}]})[0]
    assert S.validate("TwinsBatch", {"groups": [{"id": 1, "same": True, "keep": "X"}]})[0]


@test
def test_checked_json_reports_the_broken_field(tmp):
    """Ответ не проходит форму — причина называет поле, а не «не JSON»."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    data, why = R.checked_json('{"cards": [{"sections": "1"}]}', "BuildPlan")
    assert data is None and "title" in why and "into" in why, why
    data, why = R.checked_json('{"ok": true, "why": "", "issues": []}', "CriticVerdict")
    assert data and not why


@test
def test_prompt_messages_keeps_a_stable_system_prefix(tmp):
    """Стабильные правила уходят в system, источник — в user; префикс одинаков."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    prompt = "ПРАВИЛА\n\n@@ДАННЫЕ@@\n\nИСТОЧНИК: A"
    one = R.prompt_messages(prompt, prefix="кандидаты")
    assert one[0]["role"] == "user", "без флага поведение осталось прежним"
    assert "@@ДАННЫЕ@@" not in one[0]["content"], "служебная метка ушла модели"
    old = os.environ.get("AURORA_AGENT_PREFIX_SPLIT")
    os.environ["AURORA_AGENT_PREFIX_SPLIT"] = "1"
    try:
        first = R.prompt_messages(prompt, prefix="кандидаты")
        second = R.prompt_messages(prompt.replace("A", "B"), prefix="другие")
    finally:
        if old is None:
            os.environ.pop("AURORA_AGENT_PREFIX_SPLIT", None)
        else:
            os.environ["AURORA_AGENT_PREFIX_SPLIT"] = old
    assert first[0] == second[0] == {"role": "system", "content": "ПРАВИЛА"}
    assert first[1]["content"].startswith("кандидаты")
    assert "ИСТОЧНИК: A" in first[1]["content"] and "ИСТОЧНИК: B" in second[1]["content"]
    assert "@@ДАННЫЕ@@" not in first[1]["content"], "метка осталась в реплике"


@test
def test_fast_choice_picks_the_fastest_and_remembers_for_20_minutes(tmp):
    """Авто выбирает минимум по замеру и не пересчитывает выбор каждую минуту."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_core as AG

    cfg = {"fast": {"auto": True}, "chains": {"llm": {
        "worker": [{"url": "u1", "model": "slow", "provider": "p1", "chat": True},
                   {"url": "u2", "model": "fast", "provider": "p2", "chat": True}],
        "fast": []}}}
    old_probe, old_cache, old_mem = AG._probe_fast_one, AG.FAST_CACHE, dict(AG._FAST_MEM)
    calls = []

    def probe(b, timeout=5.0):
        calls.append(b["model"])
        return True, 2.0 if b["model"] == "slow" else 0.2, b["model"], ""

    AG._probe_fast_one = probe
    AG.FAST_CACHE = Path(tmp) / "fast.json"
    AG._FAST_MEM.update(at=0.0, backend=None)
    try:
        win = AG.fast_choice(cfg, force=True)
        assert win and win["model"] == "fast", win
        assert sorted(calls) == ["fast", "slow"], calls
        first = len(calls)
        assert AG.fast_choice(cfg)["model"] == "fast"
        assert len(calls) == first, "второй вызов пошёл мерить заново"
        AG.fast_forget({"url": "u1", "model": "slow"})
        assert AG._FAST_MEM["backend"], "чужая ошибка сбросила выбранную модель"
        AG.fast_forget(AG._FAST_MEM["backend"])
        assert AG._FAST_MEM["backend"] is None, "ошибка выбранной модели не сбросила выбор"
        assert not AG.FAST_CACHE.exists(), "сброшенный выбор остался в кеше"
        cfg["fast"]["auto"] = False
        assert AG.fast_choice(cfg) is None, "выключенное авто всё равно выбирает"
    finally:
        AG._probe_fast_one, AG.FAST_CACHE = old_probe, old_cache
        AG._FAST_MEM.clear()
        AG._FAST_MEM.update(old_mem)


@test
def test_model_config_adds_the_fast_role_and_keeps_auto(tmp):
    """Роль fast появляется в старой настройке, auto по умолчанию включён."""
    sys.path.insert(0, str(SCRIPTS))
    import model_config as MC

    data = MC.normalize({"version": 1, "providers": [], "capabilities": {"llm": {"roles": []}},
                         "settings": {}})[0]
    fast = next(r for r in data["capabilities"]["llm"]["roles"] if r["id"] == "fast")
    assert fast["auto"] is True and fast["thinking"] is False, fast
    cfg = MC.to_config(data)
    assert cfg["rerank"]["enabled"] is False, \
        "реранкер снова включён по умолчанию без замера на живой базе"


@test
def test_translit_batch_is_on_by_default(tmp):
    """Два имени уходят одним батчевым вызовом без отдельного флага."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    paths = [card(root, f"Concepts/Karta-{i}.md", f"русский текст {i}", status="knowledge")
             for i in range(2)]
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(kw.get("output_model"))
        payload = {"items": [{"id": i + 1, "cyrillic": f"Карточка-{i}"}
                             for i in range(len(paths))]}
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False),
                "backend": 1, "model": "m", "tps": 9, "log": []}

    old = os.environ.get("AURORA_AGENT_BATCH_TRANSLIT")
    os.environ.pop("AURORA_AGENT_BATCH_TRANSLIT", None)
    try:
        res = R.run_translit({"budget_min": 5, "max_steps": 10}, str(root),
                             apply=False, call=fake)
    finally:
        if old is not None:
            os.environ["AURORA_AGENT_BATCH_TRANSLIT"] = old
    assert calls == ["TranslitBatch"], calls
    assert [s["status"] for s in res["steps"]] == ["переведено", "переведено"], res["steps"]


@test
def test_translit_batch_translates_20_names_in_one_call(tmp):
    """Батч переводит 20 имён одним вызовом и раскладывает ответ по id."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    paths = [card(root, f"Concepts/Card-{i}.md", f"текст {i}", status="knowledge")
             for i in range(3)]
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append((role, kw.get("output_model")))
        payload = {"items": [{"id": i + 1, "cyrillic": f"Карточка-{i}"}
                             for i in range(len(paths))]}
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False),
                "backend": 1, "model": "m", "tps": 9, "log": []}

    steps = R.solve_translit_batch({"budget_min": 5}, paths, call=fake, deadline=time.time() + 60)
    assert len(calls) == 1 and calls[0][0] == "fast" and calls[0][1] == "TranslitBatch", calls
    assert [s["status"] for s in steps] == ["переведено"] * len(paths), steps
    assert [s["cyrillic"] for s in steps] == ["Карточка-0", "Карточка-1", "Карточка-2"], steps


@test
def test_a_lost_batch_item_is_asked_again_alone(tmp):
    """Пропавшее в батче имя доспрашивается одиночным вызовом, а не теряется."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    for i in range(2):
        card(root, f"Concepts/Karta-{i}.md", f"русский текст {i}", status="knowledge")
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(kw.get("output_model") or "single")
        if kw.get("output_model") == "TranslitBatch":
            payload = {"items": [{"id": 1, "cyrillic": "Карточка-1"}]}
        else:
            payload = {"cyrillic": "Карточка-2"}
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False),
                "backend": 1, "model": "m", "tps": 9, "log": []}

    old = os.environ.get("AURORA_AGENT_BATCH_TRANSLIT")
    os.environ["AURORA_AGENT_BATCH_TRANSLIT"] = "1"
    try:
        res = R.run_translit({"budget_min": 5, "max_steps": 10}, str(root),
                             apply=False, call=fake)
    finally:
        if old is None:
            os.environ.pop("AURORA_AGENT_BATCH_TRANSLIT", None)
        else:
            os.environ["AURORA_AGENT_BATCH_TRANSLIT"] = old
    assert [s["status"] for s in res["steps"]] == ["переведено", "переведено"], res["steps"]
    assert calls == ["TranslitBatch", "single"], calls


@test
def test_twins_batch_decides_two_groups_in_one_call(tmp):
    """Батч двойников решает несколько групп одним вызовом и не сливает при apply=False."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    card(root, "Concepts/А.md", "общий текст", status="knowledge")
    card(root, "Concepts/Б.md", "общий текст", status="knowledge")
    card(root, "Concepts/В.md", "другой текст", status="knowledge")
    card(root, "Concepts/Г.md", "другой текст", status="knowledge")
    groups = [["А", "Б"], ["В", "Г"]]
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(kw.get("output_model"))
        payload = {"groups": [{"id": 1, "same": True, "keep": "А", "why": "одно"},
                              {"id": 2, "same": False, "keep": "", "why": "разное"}]}
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False),
                "backend": 1, "model": "m", "log": []}

    steps = R.solve_twins_batch({"budget_min": 5}, str(root), groups, False, call=fake, deadline=time.time() + 60)
    assert calls == ["TwinsBatch"], calls
    assert [s["status"] for s in steps] == ["слил бы", "оставлено"], steps
    assert steps[0]["keep"] == "А"


@test
def test_a_lost_twins_group_is_asked_again_alone(tmp):
    """Группа, пропавшая из ответа батча, решается отдельным вызовом."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    for name in ("А", "Б", "В", "Г"):
        card(root, f"Concepts/{name}.md", "общий текст" if name in "АБ" else "другой текст",
             status="knowledge")
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(kw.get("output_model") or "single")
        if kw.get("output_model") == "TwinsBatch":
            payload = {"groups": [{"id": 1, "same": True, "keep": "А", "why": "одно"}]}
        else:
            payload = {"merge": False, "why": "разные"}
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False),
                "backend": 1, "model": "m", "log": []}

    old_groups, old_flag = R.twin_groups, os.environ.get("AURORA_AGENT_BATCH_TWINS")
    R.twin_groups = lambda cwd, limit=0: [["А", "Б"], ["В", "Г"]]
    os.environ["AURORA_AGENT_BATCH_TWINS"] = "1"
    try:
        res = R.run_twins({"budget_min": 5}, str(root), apply=False, call=fake)
    finally:
        R.twin_groups = old_groups
        if old_flag is None:
            os.environ.pop("AURORA_AGENT_BATCH_TWINS", None)
        else:
            os.environ["AURORA_AGENT_BATCH_TWINS"] = old_flag
    assert [s["status"] for s in res["steps"]] == ["слил бы", "оставлено"], res["steps"]
    assert calls == ["TwinsBatch", "single"], calls


@test
def test_a_prepared_twins_base_gives_ten_groups(tmp):
    """Десять пар с уникальными шинглами находятся как десять групп, а не как шум."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    for g in range(1, 11):
        body = " ".join(f"слово{g:02d}x{i:03d}" for i in range(120))
        card(root, f"Concepts/Дубль-{g:02d}-А.md", body, status="knowledge")
        card(root, f"Concepts/Дубль-{g:02d}-Б.md", body, status="knowledge")
    groups = R.twin_groups(str(root))
    assert len(groups) == 10 and all(len(g) == 2 for g in groups), groups


@test
def test_card_texts_add_the_section_to_each_piece(tmp):
    """Второй кусок длинной карточки знает свой раздел, а хвост не срезан."""
    sys.path.insert(0, str(SCRIPTS))
    import kb_embed as E

    root = make_project(tmp)
    body = "Начало. " + ("слово " * 400) + "\n\n## Второй раздел\n\n" + ("хвост " * 300)
    card(root, "Concepts/Длинная.md", body, status="knowledge")
    texts = E.card_texts(str(root / "AuroraKnowledgeDB"))
    pieces = "\n".join(v for k, v in texts.items() if "¶" in k)
    assert "Второй раздел" in pieces, "кусок потерял свой раздел"
    assert "хвост" in pieces, "заголовок съел хвост куска"


@test
def test_rerank_reorders_and_falls_back(tmp):
    """Реранкер переставляет по счёту, а ошибка ручки оставляет прежний порядок."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_core as AG
    import kb_embed as E

    old_http, old_model = AG.http_json, E._RERANK_MODEL
    cfg = {"rerank": {"enabled": True, "url": "http://x", "key": "", "model": "r"}}
    try:
        AG.http_json = lambda *a, **k: (200, {"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1}]}, "", 0.1)
        got = E.rerank("q", [("A", "a"), ("B", "b")], cfg)
        assert got and got[0][0] == "B", got
        AG.http_json = lambda *a, **k: (404, None, "нет", 0.1)
        assert E.rerank("q", [("A", "a"), ("B", "b")], cfg) is None
    finally:
        AG.http_json, E._RERANK_MODEL = old_http, old_model


@test
def test_fuse_uses_rerank_only_for_the_top(tmp):
    """fuse передаёт реранкеру верх выдачи и сохраняет остальное."""
    sys.path.insert(0, str(SCRIPTS))
    import ctx_pack as P
    import kb_embed as E

    class Fake:
        def __init__(self, stem, text):
            self.stem = self.title = stem
            self.path = f"AuroraKnowledgeDB/Concepts/{stem}.md"
            self.text, self.summary, self.aliases, self.tags = text, "", [], ""
            self.fm = {}

    cards = {n: Fake(n, f"{n} общий текст") for n in ("А", "Б", "В")}
    P.measure_rarity(cards)
    old = E.rerank
    E.rerank = lambda q, docs, cfg, limit=20: [(docs[-1][0], 0.9)] + [
        (n, 0.1) for n, _t in docs[:-1]]
    try:
        got = [c.stem for _w, c in P.fuse(cards, "общий текст", {},
                                          rerank={"enabled": True})]
    finally:
        E.rerank = old
    assert got[0] == "В", got
