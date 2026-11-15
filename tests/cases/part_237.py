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

from harness import KIT, SCRIPTS, card, make_project, run, test


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
def test_critic_and_relink_split_prompts_are_opt_in(tmp):
    """Старые промпты остаются одной репликой; split-варианты — только по флагу."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    old = os.environ.get("AURORA_AGENT_PREFIX_SPLIT")
    os.environ.pop("AURORA_AGENT_PREFIX_SPLIT", None)
    try:
        legacy = R.prompt_messages(R.PROMPT_BUILD_CRITIC.format(
            source="S", sections="1", proposal="{}"))
        assert len(legacy) == 1 and legacy[0]["role"] == "user"
        assert "Источник: S" in legacy[0]["content"]
        os.environ["AURORA_AGENT_PREFIX_SPLIT"] = "1"
        split = R.prompt_messages(R.PROMPT_BUILD_CRITIC_SPLIT.format(
            source="S", sections="1", proposal="{}"))
        assert [m["role"] for m in split] == ["system", "user"]
        assert "Источник: S" not in split[0]["content"]
        assert "Источник: S" in split[1]["content"]
        rel = R.prompt_messages(R.PROMPT_RELINK_SPLIT.format(
            title="T", thesis="Тезис", known="- `A`\n"))
        assert [m["role"] for m in rel] == ["system", "user"]
        assert "Карточка: T" not in rel[0]["content"]
        assert "Карточка: T" in rel[1]["content"]
    finally:
        if old is None:
            os.environ.pop("AURORA_AGENT_PREFIX_SPLIT", None)
        else:
            os.environ["AURORA_AGENT_PREFIX_SPLIT"] = old


@test
def test_structured_distill_empty_and_broken_answers_are_not_theses(tmp):
    """Пустой JSON не становится тезисом: причина остаётся причиной, битый ответ — сбоем."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    ok = R.structured_thesis('{"thesis": "Тезис", "changed": "правка"}')
    assert ok == {"thesis": "Тезис", "changed": "правка", "empty": ""}, ok
    blank = R.structured_thesis('{"thesis": "", "changed": "", "empty": ""}')
    assert blank and blank["thesis"] == "" and "без причины" in blank["empty"], blank
    reason = R.structured_thesis('{"thesis": "", "empty": "только вёрстка"}')
    assert reason and reason["empty"] == "только вёрстка", reason
    placeholder = R.structured_thesis('{"thesis": "...", "empty": ""}')
    assert placeholder and placeholder["thesis"] == "" and "заглушку" in placeholder["empty"], placeholder
    broken = R.structured_thesis('{"thesis": ')
    assert broken and broken.get("error"), broken
    assert R.structured_thesis("обычный текст") is None


@test
def test_structured_distill_empty_answer_does_not_reach_the_card(tmp):
    """Пустой JSON не попадает в тело карточки даже при включённом флаге."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    root = make_project(tmp)
    path = card(root, "Concepts/Проверка.md", "Это исходный текст карточки.",
                status="knowledge", kind="knowledge")

    def fake(cfg, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "tps": 1, "log": [],
                "text": '{"thesis":"","changed":"","empty":""}'}

    old = os.environ.get("AURORA_AGENT_STRUCTURED_DISTILL")
    os.environ["AURORA_AGENT_STRUCTURED_DISTILL"] = "1"
    try:
        step = R.distill_card({"request_timeout": 60}, str(path), call=fake, momus=False)
    finally:
        if old is None:
            os.environ.pop("AURORA_AGENT_STRUCTURED_DISTILL", None)
        else:
            os.environ["AURORA_AGENT_STRUCTURED_DISTILL"] = old
    assert step["status"] == "знания нет", step
    assert step.get("head") is None, "пустой ответ дошёл до записи"
    assert '{"thesis"' not in path.read_text(encoding="utf-8")


@test
def test_raptor_builds_a_tree_from_moc_without_a_model(tmp):
    """RAPTOR собирает обзор из MOC и тезисов и находит узел по запросу."""
    root = make_project(tmp)
    card(root, "Concepts/Карточка-А.md", "Первая карточка о ядре системы.",
         status="knowledge")
    card(root, "Concepts/Карточка-Б.md", "Вторая карточка о границах системы.",
         status="knowledge")
    card(root, "MOC/Карта-ядра.md",
         "## Состав\n\n- [[Карточка-А]]\n- [[Карточка-Б]]\n", status="index")

    cp = run("kb_raptor.py", "--build", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads((root / "AuroraKnowledgeDB/meta/raptor.json").read_text(encoding="utf-8"))
    assert any(n["id"] == "moc:Карта-ядра" and
               {"Карточка-А", "Карточка-Б"} <= set(n["cards"]) for n in data["nodes"]), data
    cp = run("kb_raptor.py", "--query", "Карточка-А", "--json", cwd=root)
    assert cp.returncode == 0, cp.stderr
    rows = json.loads(cp.stdout.strip().splitlines()[-1])
    assert rows and rows[0]["id"] == "moc:Карта-ядра", rows
    assert "Карточка-А" in rows[0]["cards"], rows[0]
    card(root, "Concepts/Карточка-В.md", "Третья карточка.", status="knowledge")
    cp = run("kb_raptor.py", "--check", cwd=root)
    assert cp.returncode == 1 and "устарело" in cp.stdout, cp.stdout


@test
def test_raptor_query_matches_epik_word_forms_and_stopwords(tmp):
    """«какие системы входят в эпик» находит карту `Epic`, а не карточку с тем же словом."""
    root = make_project(tmp)
    card(root, "Systems/Ядро-системы.md", "Ядро системы описано здесь.", status="knowledge")
    card(root, "MOC/Epic.md", "## Состав\n\n- [[Ядро-системы]]\n", status="index")

    cp = run("kb_raptor.py", "--build", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    cp = run("kb_raptor.py", "--query", "какие системы входят в эпик", "--json", cwd=root)
    assert cp.returncode == 0, cp.stderr
    rows = json.loads(cp.stdout.strip().splitlines()[-1])
    assert rows and rows[0]["id"] == "moc:Epic", rows


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
    data["settings"]["rerank"] = True
    assert MC.to_config(data)["rerank"]["enabled"] is True, \
        "настройка кита не включает реранкер"
    old = os.environ.get("AURORA_RERANK")
    os.environ["AURORA_RERANK"] = "0"
    try:
        assert MC.to_config(data)["rerank"]["enabled"] is False, \
            "переменная окружения не сильнее настройки"
    finally:
        if old is None:
            os.environ.pop("AURORA_RERANK", None)
        else:
            os.environ["AURORA_RERANK"] = old


@test
def test_models_page_has_a_reranker_switch_with_a_hint(tmp):
    """Переключатель есть в настройках прогона, подсказка объясняет, когда он полезен."""
    view = (KIT / "cockpit/modules/models/view.js").read_text(encoding="utf-8")
    assert "models.rerank" in view and "models.rerank_hint" in view, \
        "в настройках моделей нет переключателя реранкера или подсказки"
    for lang in ("ru", "en"):
        data = json.loads((KIT / f"cockpit/modules/models/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert data.get("models.rerank") and data.get("models.rerank_hint"), lang


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

    cards = {n: Fake(n, "общий текст") for n in ("Альфа", "Бета", "Гамма")}
    P.measure_rarity(cards)
    old = E.rerank
    E.rerank = lambda q, docs, cfg, limit=20: [(docs[-1][0], 0.9)] + [
        (n, 0.1) for n, _t in docs[:-1]]
    try:
        got = [c.stem for _w, c in P.fuse(cards, "Альфа общий текст", {},
                                          rerank={"enabled": True})]
        blended = [c.stem for _w, c in P.fuse(cards, "Альфа общий текст", {},
                                              rerank={"enabled": True, "blend": 0.3})]
    finally:
        E.rerank = old
    assert got[0] == "Гамма", got
    assert blended[0] == "Альфа", blended


@test
def test_raptor_paraphrases_and_global_classifier(tmp):
    """Пересказ находит раздел, а точный вопрос не уходит в обзор."""
    root = make_project(tmp)
    card(root, "Systems/Ядро.md", "Ядро системы описано здесь.", status="knowledge")
    card(root, "MOC/Системы.md", "## Состав\n\n- [[Ядро]]\n", status="index")
    cp = run("kb_raptor.py", "--build", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    cp = run("kb_raptor.py", "--query", "какие сервисы есть в решении", "--json", cwd=root)
    rows = json.loads(cp.stdout.strip().splitlines()[-1])
    assert rows and rows[0]["id"] == "moc:Системы", rows

    cp = run("kb_raptor.py", "--query", "какие сервисы есть в решении",
             "--global", "--json", cwd=root)
    assert json.loads(cp.stdout.strip().splitlines()[-2])["global"] is True, cp.stdout
    cp = run("kb_raptor.py", "--query", "как настроить систему", "--global", "--json", cwd=root)
    assert json.loads(cp.stdout.strip().splitlines()[-2])["global"] is False, cp.stdout
    cp = run("kb_raptor.py", "--query", "какие карточки про US 6.5.1",
             "--global", "--json", cwd=root)
    assert json.loads(cp.stdout.strip().splitlines()[-2])["global"] is False, cp.stdout


@test
def test_raptor_uses_vector_scores_when_the_index_exists(_t):
    """Без общих слов узел находит семантический индекс, а не только словарь."""
    sys.path.insert(0, str(SCRIPTS))
    import kb_embed as E
    import kb_raptor as R

    old_load, old_search = R.load, E.search
    R.load = lambda: {"nodes": [
        {"id": "moc:Альфа", "title": "Альфа", "level": 1, "text": "Альфа", "cards": ["a"]},
        {"id": "moc:Бета", "title": "Бета", "level": 1, "text": "Бета", "cards": ["b"]},
    ]}
    E.search = lambda q, cfg, model, limit=40: [("b", 0.9)]
    try:
        rows = R.rank("нечто", cfg={"embed": {"model": "m"}}, vectors=True)
    finally:
        R.load, E.search = old_load, old_search
    assert rows and rows[0][1]["title"] == "Бета", rows


@test
def test_raptor_eval_writes_metrics_and_checks_the_gate(tmp):
    """Замер пишет число пересказов и отдельно проверяет порог 75 из 100."""
    root = make_project(tmp)
    card(root, "Systems/Ядро.md", "Ядро системы описано здесь.", status="knowledge")
    card(root, "MOC/Системы.md", "## Состав\n\n- [[Ядро]]\n", status="index")
    assert run("kb_raptor.py", "--build", "--apply", cwd=root).returncode == 0
    questions = root / "questions.json"
    questions.write_text(json.dumps([
        {"id": 1, "question": "что входит в Системы", "expected_node": "Системы"},
        {"id": 2, "question": "какие сервисы есть", "expected_node": "Системы"},
    ], ensure_ascii=False), encoding="utf-8")
    cp = run("kb_raptor_eval.py", "--questions", str(questions),
             "--paraphrase-from", "2", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    data = json.loads((root / "AuroraKnowledgeDB/meta/raptor_metrics.json")
                      .read_text(encoding="utf-8"))
    assert data["top1"]["paraphrase"] == 1.0, data
    assert data["gate"]["passed"] is True, data


@test
def test_raptor_metrics_skips_without_questions_instead_of_failing(tmp):
    """Маршрут починки не краснеет, если файла вопросов ещё нет."""
    root = make_project(tmp)
    card(root, "Systems/Ядро.md", "Ядро системы описано здесь.", status="knowledge")
    card(root, "MOC/Системы.md", "## Состав\n\n- [[Ядро]]\n", status="index")
    cp = run("kb_raptor.py", "--metrics", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "замер пропущен" in cp.stdout, cp.stdout


@test
def test_ask_route_prepends_only_new_overview_cards(tmp):
    """Обзор добавляется к обычному паку, а уже найденная карточка не дублируется."""
    sys.path.insert(0, str(SCRIPTS))
    import answer_core as AC

    root = make_project(tmp)
    seen = []

    def runner(cwd, script, args, **kw):
        return {"ok": True, "out": "# Пак (карточек 1)\n\n## Обычная\n\nтекст"}

    def overview(question, cfg, skip=()):
        seen.append(set(skip))
        return {"used": True, "title": "Системы", "cards": ["Ядро"],
                "pack": "# Обзор базы: Системы\n\n## Ядро\n\nтекст\n"}

    old = AC._raptor_context
    AC._raptor_context = overview
    try:
        got = AC.search_context(str(root), "какие системы", runner=runner,
                                cfg={}, raptor=True)
    finally:
        AC._raptor_context = old
    assert seen == [{"Обычная"}], seen
    assert got["ok"] and got["pack"].startswith("# Обзор базы"), got
    assert got["cards"] == ["Ядро", "Обычная"], got["cards"]


@test
def test_search_context_carries_rerank_choice_to_ctx_pack(_t):
    """Выбор «качество/скорость» доходит до отдельного процесса сборки пака."""
    sys.path.insert(0, str(SCRIPTS))
    import answer_core as AC

    seen = []

    def runner(cwd, script, args, **kw):
        seen.append(args)
        return {"ok": True, "out": "# Пак (карточек 1)\n\n## Карточка\n\nтекст"}

    AC.search_context(".", "вопрос", runner=runner, rerank=True, rerank_blend=0.3)
    AC.search_context(".", "вопрос", runner=runner, rerank=False)
    assert "--rerank" in seen[0] and "--rerank-blend" in seen[0], seen[0]
    assert "--no-rerank" in seen[1], seen[1]


@test
def test_ctx_pack_rerank_choice_uses_the_measured_blend(_t):
    """Явное «качество» включает 70/30, явное «скорость» выключает реранкер."""
    sys.path.insert(0, str(SCRIPTS))
    import ctx_pack as P

    on = P.rerank_config(enabled=True)
    off = P.rerank_config(enabled=False)
    custom = P.rerank_config(blend=0.5)
    assert on["enabled"] is True and on["blend"] == 0.3, on
    assert off["enabled"] is False and off["blend"] == 0.0, off
    assert custom["enabled"] is True and custom["blend"] == 0.5, custom


@test
def test_ask_page_has_quality_and_overview_switches(tmp):
    """Оба выбора стоят в форме вопроса, а не только в отдельной команде."""
    page = (KIT / "cockpit/modules/ask/view.html").read_text(encoding="utf-8")
    js = (KIT / "cockpit/modules/ask/view.js").read_text(encoding="utf-8")
    assert 'id="askQuality"' in page and 'id="askRaptor"' in page, page
    assert "--rerank" in js and "--raptor" in js, js
    for lang in ("ru", "en"):
        data = json.loads((KIT / f"cockpit/modules/ask/i18n/{lang}.json").read_text(encoding="utf-8"))
        for key in ("ask.quality", "ask.quality_hint", "ask.raptor",
                    "ask.raptor_hint", "ask.raptor_metrics"):
            assert data.get(key), (lang, key)


@test
def test_reranker_switch_uses_measured_blend(tmp):
    """Включённый реранкер не заменяет выдачу, а смешивается 70/30."""
    sys.path.insert(0, str(SCRIPTS))
    import model_config as MC

    data = MC.normalize({"version": 1, "providers": [],
                         "capabilities": {"llm": {"roles": []}},
                         "settings": {"rerank": True}})[0]
    cfg = MC.to_config(data)
    assert cfg["rerank"]["enabled"] is True and cfg["rerank"]["blend"] == 0.3, cfg["rerank"]


@test
def test_raptor_state_reads_metrics_for_the_ask_panel(tmp):
    """Панель получает сводку обзора без пересчёта и без тела всех вопросов."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as C

    root = make_project(tmp)
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "raptor.json").write_text(json.dumps({"nodes": [{"id": "moc:X"}],
                                                   "cards": {"X": "moc:X"}}), encoding="utf-8")
    (meta / "raptor_metrics.json").write_text(json.dumps({
        "generated_at": "2026-11-15T00:00:00Z", "questions": 20,
        "top1": {"all": 0.75, "paraphrase": 0.75, "n_paraphrase": 20},
        "gate": {"target": 0.75, "passed": True}, "rows": [{"question": "не надо"}]}),
        encoding="utf-8")
    got = C.raptor_state(str(root))
    assert got["tree"] == {"nodes": 1, "cards": 1}, got
    assert got["metrics"]["top1"]["paraphrase"] == 0.75, got
    assert "rows" not in got["metrics"], "панели отдано тело всех вопросов"
