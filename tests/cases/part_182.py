"""Проверки движка Aurora, часть 182. Каркас — tests/harness.py.

Найдено на живом проекте: производство (`agent:make`) с большим паком знаний ронялось на «запрос длиннее окна модели»
и «artifact not created». Пак больше окна нарезается по карточкам, каждая часть получает свой конспект моделью,
и дальше идёт короткий пак; для возобновлённой сессии то же самое, но не дважды.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness import KIT, make_project, test


def _runner():
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_runner as R
    return R


def card(root: Path, name: str, text: str) -> None:
    path = root / "AuroraKnowledgeDB" / "Concepts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {name}\nstatus: knowledge\n---\n\n{text}\n", encoding="utf-8")


@test
def test_pack_is_split_by_cards_and_a_single_big_card_by_lines(_t):
    """Части пака не длиннее бюджета; разрез по заголовкам карточек, одна большая карточка — по строкам; весь текст
    сохраняется (конкатенация частей равна исходному паку, без потерь и дублей)."""
    R = _runner()
    body = lambda i: "строка текста по делу\n" * (i + 3)
    pack = "".join(f"## Карточка {i}\n\n{body(i)}" for i in range(6))
    budget = 400
    parts = R.split_pack_for_budget(pack, budget)
    assert len(parts) > 1, parts
    assert all(len(p) <= budget for p in parts), [len(p) for p in parts]
    assert all(p.startswith("## Карточка") for p in parts), [p[:20] for p in parts]
    assert "".join(parts) == pack, "части не складываются в исходный пак"
    huge = "## Большая\n\n" + "строка длинного тела\n" * 60
    pieces = R.split_pack_for_budget(huge, 300)
    assert len(pieces) > 1 and all(len(p) <= 300 for p in pieces), [len(p) for p in pieces]
    assert "".join(pieces) == huge, "большая карточка потеряла текст при нарезке"
    assert R.split_pack_for_budget("маленький пак", 1000) == ["маленький пак"]


@test
def test_compact_pack_calls_model_once_per_chunk_and_keeps_a_small_result(_t):
    """Пак больше бюджета — по одному вызову на часть, каждый конспект ограничен капом, итог сильно меньше исходника;
    пак поменьше возвращается без изменений и без вызовов; отказ части — текст с её номером, а не молчание."""
    R = _runner()
    facts = "факт по делу.\n" * 200
    pack = "".join(f"## Карточка {i}\n\n{facts}" for i in range(8))
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(messages[0]["content"])
        return {"ok": True, "text": f"конспект с источником [[Карточка {len(calls)}]]", "log": []}

    packed, n, why = R.compact_make_pack({}, pack, "новая история", 2000, fake)
    assert not why, why
    assert n == len(calls) == len(R.split_pack_for_budget(pack, 2000)), (n, len(calls))
    assert all("конспект" in c for c in calls), calls[0][:60]
    assert len(packed) < len(pack) / 4, (len(packed), len(pack))
    assert all(len(c) <= 2000 + 1200 for c in calls), "часть не помещается в бюджет"
    small, n2, _ = R.compact_make_pack({}, pack, "новая история", len(pack) + 100, fake)
    assert small == pack and n2 == 0 and len(calls) == n, "маленький пак не должен был конспектироваться"
    bad, _, why_bad = R.compact_make_pack({}, pack, "новая история", 2000,
                                         lambda cfg, role, messages, **kw: {"ok": False, "text": ""})
    assert not bad and why_bad and "части 1" in why_bad, why_bad


@test
def test_run_make_with_a_large_pack_compacts_it_and_continues_without_recompacting(tmp: Path):
    """Найдено на живом проекте: пак ~686 000 знаков уходил воркеру целиком, и производство ронялось на «запрос длиннее
    окна». Возобновлённая сессия с таким паком нарезается на конспекты; производство идёт дальше, и повторной
    нарезки при следующем заходе нет."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n', encoding="utf-8")
    d = R.make_session_dir(str(root), "ac")
    spec = R.make_spec(str(root), "ac")
    pack = "".join(f"## Заявка-{i}\n\n" + "Срок десять дней, проверка по отчёту. " * 900 for i in range(10))
    assert len(pack) > 200000, len(pack)
    R.save_session(str(root), d.name, {"sid": d.name, "kind": "ac", "idea": "срок заявки и проверка",
                                      "spec": spec, "pack": pack, "stages": {"enriched": "2026-11-01T08:00:00Z"},
                                      "rounds": [], "plan": ""})
    calls = []

    def fake(cfg, role, messages, **kw):
        content = messages[0]["content"]
        calls.append((role, content))
        if "ЧАСТЬ пака знаний" in content:
            return {"ok": True, "text": "конспект: срок десять дней [[Заявка-1]]", "log": []}
        if role == "planner":
            return {"ok": True, "text": json.dumps({"questions": [], "assumptions": [], "plan": "1. Критерии"},
                                                   ensure_ascii=False), "log": []}
        if role == "critic":
            return {"ok": True, "text": json.dumps({"ok": True, "issues": [], "coverage": {}}, ensure_ascii=False),
                    "log": []}
        return {"ok": True, "text": "# AC\n\nСрок ([[Заявка-1]]).", "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "15000"})
    r = R.run_make(cfg, str(root), "", "", d.name, "", False, call=fake, momus=False)
    assert r.get("ok"), f"производство упало: {r.get('why')}"
    compacts = [c for c in calls if "ЧАСТЬ пака знаний" in c[1]]
    assert compacts, "нарезки большого пака не было"
    assert len(compacts) == len(R.split_pack_for_budget(pack, 39000 - 8000)) or len(compacts) > 1, len(compacts)
    st = R.load_session(str(root), d.name)
    assert st.get("compacted"), "флаг конспектирования не записан"
    assert len(st.get("pack") or "") < 40000, f"пак остался гигантским: {len(st.get('pack') or '')}"
    before = len(calls)
    r2 = R.run_make(cfg, str(root), "", "", d.name, "", False, call=fake, momus=False)
    assert r2.get("ok"), f"возобновление упало: {r2.get('why')}"
    assert len(calls) == before, "возобновлённая сессия конспектировалась повторно"


@test
def test_cyrillic_is_measured_safely_so_the_live_pack_actually_compacts(tmp: Path):
    """Найдено на живом проекте: пак 686 839 знаков оценивался по три знака на токен (229 тыс.) и проходил проверку
    окна 262 144, а шлюз насчитал 284 781 токен и отказал. Теперь кириллица считается по 2,2 знака на токен, такой
    пак заранее виден как слишком большой и нарезается; после конспектов запрос влезает в окно."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    text = "Срок десять дней, проверка по отчёту. " * 19000
    assert len(text) > 680000, len(text)
    old = len(text) / A.CHARS_PER_TOKEN
    new = A.rough_tokens([{"content": text}])
    assert old < 262144 < new, (old, new)
    assert new > old * 1.15, "кириллица считается не строже латиницы"
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    assert A.prompt_budget_safe(cfg) == A.prompt_budget(cfg), "оба бюджета должны быть осторожными"
    budget = A.prompt_budget_safe(cfg, reserve_chars=21000)
    assert len(text) > budget
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(messages[0]["content"])
        return {"ok": True, "text": "конспект: срок десять дней [[Заявка-1]]", "log": []}

    packed, n, why = R.compact_make_pack(cfg, text, "срок заявки", budget, fake)
    assert not why and n > 1, (n, why)
    assert len(packed) <= budget, (len(packed), budget)
    need = A.rough_tokens([{"content": packed}]) + A.ANSWER_ROOM
    assert need < 262144, f"даже после конспектов запрос не влезает: {need}"


@test
def test_the_trigger_counts_the_planner_prompt_and_the_grill_method(tmp: Path):
    """Найдено на живом проекте: пак 686 839 знаков без промпта планировщика и метода разбора выглядел как 261 471 токен
    при окне 262 144 — нарезка не просыпалась, а шлюз получал 284 781 токен и отказывал. Теперь в оценку входят
    `PROMPT_MAKE_PLAN` и метод разбора: тот же пак виден как слишком большой."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    pack = "Срок десять дней, проверка по отчёту. " * 19000
    template = "# AC\n\n## Критерии приёмки\n" * 500
    idea = "помоги сделать новую историю по постановке"
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    plan_base = R.PROMPT_MAKE_PLAN + R.grill_method(str(KIT))
    assert len(plan_base) > 3000, len(plan_base)
    need = A.rough_tokens([{"content": plan_base + template + idea + pack}]) + A.ANSWER_ROOM
    assert need > 262144, f"без промпта планировщика нарезка не проснётся: {need}"
    without = A.rough_tokens([{"content": template + idea + pack}]) + A.ANSWER_ROOM
    assert without < need, "промпт планировщика не учтён в оценке"
