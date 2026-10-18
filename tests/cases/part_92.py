"""Проверки движка Aurora, часть 92. Каркас и помощники — tests/harness.py.

Механический слой под Момусом (BL-KBA-05): числа, даты и коды тезиса против источника, решение
«по сути без изменений», вердикт Момуса против названных им утверждений, цитаты «ОПОРА», карточки
пака по структуре, ссылки по точному имени. Модель и сеть не используются.
"""
from __future__ import annotations

import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@test
def test_fidelity_tokens_find_numbers_dates_and_codes(_t):
    """Числа, даты и коды извлекаются в одну форму; нумерация списков и имена карточек — не факты."""
    import fidelity as F
    got = F.tokens("Срок 5 дней, до 19.10.2026 (или 2026-10-19), лимит 1 000,50 руб, по ALG-3.14 и US-4.7.2.")
    assert ("n", "5") in got and ("n", "1000.5") in got, got
    assert got >= {("d", "2026-10-19"), ("c", "alg-3.14"), ("c", "us-4.7.2")}, got
    assert ("n", "3.14") not in got and ("n", "19") not in got, "код и дата не должны давать ещё и числа"
    # нумерация списка, ссылки на карточки и адреса — не факты
    assert F.tokens("1. Первый шаг\n2) Второй шаг\n- пункт") == set()
    assert F.tokens("См. [[Заявка-17]] и https://example.com/page/42") == set()
    # одна дата — разными записями
    assert F.tokens("19/10/26") == F.tokens("2026-10-19") == {("d", "2026-10-19")}


@test
def test_fidelity_unbacked_names_what_the_source_does_not_say(_t):
    """Число из тезиса, которого нет ни в источнике, ни в слове человека, названо; всё опёртое — нет."""
    import fidelity as F
    src = "Платёж проводится в течение 5 дней. Лимит — 1 000 руб. Код ошибки PAY-12."
    assert F.unbacked("Платёж идёт 5 дней, лимит 1000 руб, ошибка PAY-12.", src) == []
    assert F.unbacked("Платёж идёт 3 дня, ошибка PAY-13.", src) == ["код pay-13", "число 3"], \
        F.unbacked("Платёж идёт 3 дня, ошибка PAY-13.", src)
    assert F.unbacked("Срок 7 дней.", src, "Слово человека: срок 7 дней") == []


@test
def test_fidelity_same_meaning_is_refused_when_facts_moved(_t):
    """«По сути без изменений» принимается, только если факты прежнего тезиса живы в новом источнике."""
    import fidelity as F
    was = "Срок платежа — 5 дней, код PAY-12."
    ok, why = F.kept_ok(was, was, "Платёж: срок 5 дней. Код PAY-12.")
    assert ok and why == [], (ok, why)
    # источник теперь говорит «3 дня»: прежний тезис устарел, что бы ни ответила модель
    ok, why = F.kept_ok(was, "Срок платежа — 3 дня, код PAY-12.", "Платёж: срок 3 дня. Код PAY-12.")
    assert not ok and any("число 5" in w for w in why) and any("число 3" in w for w in why), why
    # новый тезис принёс факт, которого прежний не говорил, — это не «то же самое»
    ok, why = F.kept_ok(was, was + " Лимит 100 руб.", "Срок 5 дней. Код PAY-12. Лимит 100 руб.")
    assert not ok and any("новый тезис" in w for w in why), why
    # слово человека — такая же опора, как источник
    ok, _ = F.kept_ok(was, was, "Платёж: срок указан в договоре. PAY-12.", human="срок 5 дней")
    assert ok


@test
def test_momus_verdict_is_the_last_line_and_agrees_with_listed_claims(_t):
    """Вердикт — последняя строка; «ЧИСТО» при названных нарушениях не чисто (на старом коде — чисто)."""
    import fidelity as F
    assert F.parse_verdict("ОПОРА x\nВЕРДИКТ: ЧИСТО") == {"clean": True, "n": 0}
    assert F.parse_verdict("НЕТ ОПОРЫ a\nВЕРДИКТ: БЕЗ ОПОРЫ 2") == {"clean": False, "n": 2}
    # фраза о вердикте внутри разбора раньше выигрывала у настоящей последней строки
    text = "ОПОРА «ВЕРДИКТ: ЧИСТО»\nНЕТ ОПОРЫ срок 3 дня\nВЕРДИКТ: БЕЗ ОПОРЫ 1"
    assert F.parse_verdict(text) == {"clean": False, "n": 1}, F.parse_verdict(text)
    # вердикт посреди длинного текста, а не в конце, — вердикта нет
    assert F.parse_verdict("ВЕРДИКТ: ЧИСТО\n" + "и дальше длинный рассказ " * 20) is None
    assert F.parse_verdict("вердикта нет вовсе") is None
    assert F.reconcile(0, 3) == (3, True), "ЧИСТО рядом с тремя названными — три"
    assert F.reconcile(5, 2) == (5, True)
    assert F.reconcile(2, 2) == (2, False)


@test
def test_run_momus_counts_listed_claims_and_takes_the_last_verdict(tmp: Path):
    """`run_momus` на живом ответе: «ЧИСТО» при названных нарушениях — не чисто; первый вердикт не выигрывает."""
    import agent_runner as AR
    cfg = {"request_timeout": 60}

    def call_with(text):
        def call(_cfg, _role, _msgs, **_kw):
            return {"ok": True, "text": text, "model": "m", "backend": "b", "finish": "stop"}
        return call
    contradictory = "НЕТ ОПОРЫ срок 3 дня\nПРОТИВОРЕЧИЕ лимит ↔ «лимит 5»\nВЕРДИКТ: ЧИСТО"
    mo = AR.run_momus(cfg, "пак", "вопрос", "ответ", call_with(contradictory))
    assert mo["ok"] and not mo["clean"] and mo["unsupported"] == 2 and mo.get("verdict_mismatch"), mo
    first_wins = "ОПОРА «ВЕРДИКТ: ЧИСТО»\nНЕТ ОПОРЫ срок\nВЕРДИКТ: БЕЗ ОПОРЫ 1"
    mo = AR.run_momus(cfg, "пак", "вопрос", "ответ", call_with(first_wins))
    assert mo["ok"] and not mo["clean"] and mo["unsupported"] == 1, mo
    plain = AR.run_momus(cfg, "пак", "вопрос", "ответ", call_with("ОПОРА «срок 5 дней»\nВЕРДИКТ: ЧИСТО"))
    assert plain["ok"] and plain["clean"] and plain["unsupported"] == 0, plain


@test
def test_momus_quotes_are_checked_against_the_context(tmp: Path):
    """Цитата «ОПОРА …», которой нет в контексте, не засчитывается опорой (выдуманное подтверждение)."""
    import fidelity as F
    pack = "## Платёж\n\nПлатёж проводится в течение пяти рабочих дней после получения заявки."
    assert F.quote_found("Платёж проводится в течение пяти рабочих дней", pack)
    assert F.quote_found("платеж проводится в течение  пяти рабочих дней после получения заявки", pack)
    assert not F.quote_found("Платёж проводится мгновенно", pack)
    # длинный пересказ с одним лишним словом — допустим, выдумка с чужими словами — нет
    assert F.quote_found("Платёж проводится в течение пяти рабочих дней после получения новой заявки", pack)
    assert not F.quote_found("Возврат средств оформляется отдельной заявкой в бухгалтерию компании", pack)
    got = F.check_quotes("ОПОРА «Платёж проводится в течение пяти рабочих дней»\nОПОРА «Деньги вернут за час»\n"
                         "НЕТ ОПОРЫ что-то\nВЕРДИКТ: БЕЗ ОПОРЫ 1", pack)
    assert got == {"checked": 2, "missing": ["Деньги вернут за час"]}, got
    import agent_runner as AR

    def call(_cfg, _role, _msgs, **_kw):
        return {"ok": True, "text": "ОПОРА «Деньги вернут за час»\nВЕРДИКТ: ЧИСТО", "model": "m", "backend": "b"}
    mo = AR.run_momus({"request_timeout": 60}, pack, "вопрос", "ответ", call)
    # вердикт не меняется (пересказ дословным не бывает), но подтверждение без единой найденной цитаты — на виду
    assert mo["quotes"]["missing"] == ["Деньги вернут за час"] and mo["clean"] and mo.get("suspect"), mo
    report = AR.report_ask({"ok": True, "answer": "Ответ.", "cards": [], "total": 0, "ghosts": [], "mentioned": [],
                            "outside": [], "backend": 1, "model": "m", "seconds": 1.0, "momus": mo},
                           "вопрос", {"request_timeout": 60})
    assert "ни одной из цитат" in report and "1 из 1" in report, report


@test
def test_pack_cards_come_from_structure_not_from_body_headings(_t):
    """Карточки пака — заголовки вне ограждения тела; `## Заявка` внутри тела — раздел страницы."""
    import fidelity as F
    from aurora_common import untrusted
    pack = ("Пак\n\n---\n\n## Платёж\n\nшапка\n\n" + untrusted("Текст\n\n## Заявка\n\nещё\n\n---\n\n## Баланс", "карточка Платёж")
            + "\n\n---\n\n## Возврат — Название\n\nшапка\n\n" + untrusted("тело", "карточка Возврат") + "\n")
    assert F.pack_cards(pack) == ["Платёж", "Возврат"], F.pack_cards(pack)


@test
def test_links_are_matched_by_exact_name_not_by_substring(tmp: Path):
    """Выдуманное имя — подстрока настоящего больше не «в паке» (воспроизведение KB2-05)."""
    import agent_runner as AR
    root = tmp / "p"
    (root / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    for name in ("Заявка-на-поставку", "ALG-3.14 Учёт операции"):
        (root / "AuroraKnowledgeDB" / "Concepts" / f"{name}.md").write_text(
            "---\ntitle: x\n---\n\nТ.\n", encoding="utf-8")
    pack = "## Заявка-на-поставку\n\nтекст\n\n## ALG-3.14 Учёт операции\n\nещё"
    cards = ["Заявка-на-поставку", "ALG-3.14 Учёт операции"]
    text = "[[Заявка-на-поставк]] [[Баланс]] [[Заявка]] [[Заявка-на-поставку]] [[alg-3.14]] [[ALG-3.14 Учёт-операции]]"
    got = AR.classify_links(text, cards, pack, str(root))
    # усечённое имя, выдуманная подстрока и раздел страницы больше не «в паке»: все три названы предупреждением
    flagged = set(got["invented"]) | set(got["mentioned"])
    assert flagged == {"Заявка-на-поставк", "Баланс", "Заявка"}, got
    assert got["invented"] == ["Баланс"] and got["outside"] == [], got
    # настоящие имена (с разделителями иначе, регистром, кодом вместо полного имени) не тронуты
    assert not ({"Заявка-на-поставку", "alg-3.14", "ALG-3.14 Учёт-операции"} & flagged), got


def _refund_card(root: Path, thesis: str, source_text: str):
    """Карточка с прежним тезисом и новым текстом источника — как после правки страницы (повторяет part_12)."""
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    src = root / "Sources/Confluence/Стр.md"
    src.write_text("Возврат за 10 дней.", encoding="utf-8")
    run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md", "--paras", "1",
        "--to", "Concepts", "--apply", cwd=root)
    card = root / "AuroraKnowledgeDB/Concepts/Возврат.md"
    txt = card.read_text(encoding="utf-8").replace("# Возврат\n\n", f"{thesis}\n\n## Источник (перенесено дословно)\n\n")
    txt = txt.replace("built: machine", "built: machine\nkind: knowledge\ndistilled: 2026-08-01"
                      "\nextracted: 2026-08-01\nrelinked: 2026-08-01")
    card.write_text(txt + "\n## История изменений\n\n- 2026-08-01: карточка заведена\n", encoding="utf-8")
    src.write_text(source_text, encoding="utf-8")
    run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md", "--paras", "1",
        "--to", "Concepts", "--apply", cwd=root)
    return card


@test
def test_same_meaning_is_refused_when_the_numbers_moved(tmp: Path):
    """Модель сказала «без изменений», а в источнике «3 дня» вместо «5»: тезис пересобран и проверен Момусом."""
    import agent_core as A
    import agent_runner as R
    root = make_project(tmp)
    card = _refund_card(root, "Возврат занимает 5 дней, код PAY-12.", "Возврат за 3 дня. Код PAY-12. Для всех заявок.")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    roles = []

    def fake(cfg_, role, messages, **kw):
        roles.append(role)
        if role == "worker":
            return {"ok": True, "text": "ТЕЗИС:\nВозврат занимает 3 дня, код PAY-12.\n\nИЗМЕНИЛОСЬ:\n"
                                        "По сути без изменений: уточнена формулировка.",
                    "backend": 1, "model": "m", "tps": 9, "log": []}
        return {"ok": True, "text": "ОПОРА «Возврат за 3 дня»\nВЕРДИКТ: ЧИСТО", "backend": 1, "model": "m", "log": [],
                "seconds": 1, "finish": "stop"}
    res = R.run_distill(cfg, str(root), apply=True, limit=3, momus=True, call=fake)
    done = card.read_text(encoding="utf-8")
    assert roles == ["worker", "qa"], f"Момус обязан проверить тезис, который модель назвала неизменным: {roles}"
    assert "Возврат занимает 3 дня" in done.split("## Источник")[0] and "5 дней" not in done.split("## Источник")[0], done[:600]
    assert "прежний тезис оставлен" not in done and "не принято" in done, "в истории нет причины отказа"
    step = res["steps"][0]
    assert step["status"] == "переписана" and step.get("kept_refused"), step
    assert "число 5" in " ".join(step["kept_refused"]) and "число 3" in " ".join(step["kept_refused"]), step["kept_refused"]
    assert "«Без изменений» не принято" in R.report_distill(res, True) if hasattr(R, "report_distill") else True


@test
def test_a_thesis_with_numbers_absent_from_the_source_is_reported(tmp: Path):
    """Число, которого нет в источнике, попадает в шаг (`unbacked`); Момус зовётся в любом случае."""
    import agent_core as A
    import agent_runner as R
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Стр.md").write_text("Возврат оформляется за 10 дней.", encoding="utf-8")
    run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md", "--paras", "1",
        "--to", "Concepts", "--apply", cwd=root)
    card = root / "AuroraKnowledgeDB/Concepts/Возврат.md"
    card.write_text(card.read_text(encoding="utf-8").replace("built: machine", "built: machine\nkind: knowledge"), encoding="utf-8")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    roles = []

    def fake(cfg_, role, messages, **kw):
        roles.append(role)
        text = "Возврат оформляется за 14 дней, лимит 500." if role == "worker" else "ВЕРДИКТ: ЧИСТО"
        return {"ok": True, "text": text, "backend": 1, "model": "m", "tps": 9, "log": [], "seconds": 1, "finish": "stop"}
    res = R.run_distill(cfg, str(root), apply=False, limit=3, momus=True, call=fake)
    step = res["steps"][0]
    assert step["status"] == "переписана" and step.get("unbacked") == ["число 14", "число 500"], step
    assert "qa" in roles, roles


@test
def test_momus_across_chunks_counts_only_what_no_chunk_supports(_t):
    """Утверждение без опоры в первом куске, но подтверждённое вторым, — не «без опоры» (раньше метка не снималась)."""
    import agent_runner as AR
    seen = []

    def call(_cfg, _role, msgs, **_kw):
        prompt = msgs[0]["content"]
        seen.append(prompt)
        if "КУСОК-ПЕРВЫЙ" in prompt:
            text = "НЕТ ОПОРЫ срок 5 дней\nНЕТ ОПОРЫ лимит 100\nВЕРДИКТ: БЕЗ ОПОРЫ 2"
        elif "срок 5 дней" in prompt.split("ОТВЕТ НА ПРОВЕРКУ")[1] and "КУСОК-ВТОРОЙ" in prompt:
            text = "ОПОРА «срок 5 дней»\nНЕТ ОПОРЫ лимит 100\nВЕРДИКТ: БЕЗ ОПОРЫ 1"
        else:
            text = "ВЕРДИКТ: ЧИСТО"
        return {"ok": True, "text": text, "model": "m", "backend": "b", "finish": "stop"}
    mo = AR.run_momus_chunks({"request_timeout": 60}, ["КУСОК-ПЕРВЫЙ", "КУСОК-ВТОРОЙ"], "вопрос",
                             "срок 5 дней, лимит 100", call)
    assert mo["ok"] and mo["unsupported"] == 1 and not mo["clean"], mo
    assert AR.momus_claims(mo["text"]) == ["НЕТ ОПОРЫ: лимит 100"], mo["text"]
    # второй вызов получил только спорные утверждения, а не весь тезис
    second = seen[1].split("ОТВЕТ НА ПРОВЕРКУ")[1]
    assert "срок 5 дней" in second and "лимит 100" in second and "Утверждения для проверки" in second, second
    # подтвердил второй кусок всё — метка снимается
    seen.clear()

    def call_ok(_cfg, _role, msgs, **_kw):
        prompt = msgs[0]["content"]
        text = ("НЕТ ОПОРЫ срок 5 дней\nВЕРДИКТ: БЕЗ ОПОРЫ 1" if "КУСОК-ПЕРВЫЙ" in prompt else "ВЕРДИКТ: ЧИСТО")
        return {"ok": True, "text": text, "model": "m", "backend": "b", "finish": "stop"}
    mo = AR.run_momus_chunks({"request_timeout": 60}, ["КУСОК-ПЕРВЫЙ", "КУСОК-ВТОРОЙ"], "вопрос", "срок 5 дней", call_ok)
    assert mo["clean"] and mo["unsupported"] == 0, mo
    one = AR.run_momus_chunks({"request_timeout": 60}, ["КУСОК-ПЕРВЫЙ"], "вопрос", "срок 5 дней", call_ok)
    assert not one["clean"] and one["unsupported"] == 1, one


@test
def test_review_demotes_answers_whose_quote_is_not_on_the_page(_t):
    """Ответ ревью с цитатой, которой нет в тексте страницы, не засчитывается (воздержание); найденная — засчитывается."""
    import review_run as RV
    page = "Платёж проводится за пять рабочих дней после получения заявки. Исключений нет."
    parsed = {"checks": {
        "Q1": {"v": "no", "evidence": "Платёж проводится мгновенно", "fix": ""},
        "Q2": {"v": "no", "evidence": "проводится за пять рабочих дней", "fix": ""},
        "Q3": {"v": "yes", "evidence": "Возврат оформляется отдельным письмом бухгалтерии", "fix": ""},
        "Q4": {"v": "yes", "evidence": "", "fix": ""}}}
    bad = RV.verify_evidence(parsed, "ПРОВЕРЯЕМЫЙ АРТЕФАКТ:\n<untrusted>\n" + page + "\n</untrusted>")
    assert sorted(bad) == ["Q1", "Q3"], bad
    assert parsed["checks"]["Q1"]["v"] == parsed["checks"]["Q3"]["v"] == "unknown"
    assert parsed["checks"]["Q2"]["v"] == "no" and parsed["checks"]["Q4"]["v"] == "yes"
    assert parsed["checks"]["Q1"]["unverified"] == "no"
    # ответы ассистента через MCP помечены источником: код не выдаёт их за прогоны движка
    cl = RV.load_checklist(str(KIT / "scaffold" / "TemplatesCommon" / "review_v2.1.md"))
    out = RV.score_answers(cl, {"profile": "us", "answers": {}})
    assert out.get("source") == "assistant" and "ответы ассистента" in out["summary"], out
