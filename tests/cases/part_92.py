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
