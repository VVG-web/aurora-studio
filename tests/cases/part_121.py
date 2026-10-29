"""Проверки движка Aurora, часть 121. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 5 «Цитируемые документы», пакет CT-17: цитаты в ответах «Спросить». Запрос по тексту статьи
возвращает её карточку в первой пятёрке, блок карточки в паке помечен «дословный текст документа» и готовой ссылкой `cite`,
`PROMPT_ASK` требует дословных цитат (правило 7) и не называет выведенных статусов, а код проверяет цитаты ответа по телам
карточек (`check_quotes`) и сообщает «Цитаты: проверено N, не найдено M». Модель и сеть не используются (ответ подставной);
проекты — только во временных папках, настоящий HOME прогона подменён раннером.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

from cases import part_120 as P120
from harness import SCRIPTS, card, run, test

KB = "AuroraKnowledgeDB"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _law_project(tmp: Path, extra: int = 0) -> Path:
    """Проект с нарезанным законом и, если нужно, `extra` обычными карточками про заявки и склад (соперники по словам)."""
    root = P120._project(tmp)
    for n in range(extra):
        card(root, f"Concepts/Заявка поставщика {n}.md",
             f"Заявка поставщика номер {n} описывает, как склад заказчика принимает товар по накладной и подтверждает поставку.",
             status="knowledge", kind="knowledge")
    P120._commit(root)
    run("kb_cited.py", "--apply", cwd=root, expect_rc=0)
    P120._commit(root)
    return root


def _pack(root: Path, topic: str, *args) -> str:
    return run("ctx_pack.py", topic, "--mode", "ask", "--no-log", *args, cwd=root, expect_rc=0).stdout


# ------------------------------------------------------------------ поиск и блок в паке (AC-CT-17.1, AC-CT-17.3)

ARTICLE_PHRASES = {
    1: "порядок передачи товаров поставщиком заказчику на основании заявки",
    2: "Склад — место, в котором заказчик принимает товар от поставщика",
    3: "подтвердить заявку в течение трёх рабочих дней",
    4: "Накладная составляется складом в двух экземплярах",
    5: "Заявки и накладные хранятся в течение пяти лет",
    6: "неустойку в размере одной десятой процента от цены товара",
}


@test
def test_cited_search_top5(tmp: Path):
    """AC-CT-17.1: запрос по тексту статьи возвращает карточку этой статьи в первой пятёрке — и среди карточек-соперников про заявки и склад."""
    root = _law_project(tmp, extra=18)
    for n, phrase in ARTICLE_PHRASES.items():
        pack = _pack(root, phrase, "--max-cards", "5")
        heads = re.findall(r"(?m)^## (.+)$", pack)
        assert f"Закон-о-примерах-ст-{n}" in heads and len(heads) <= 5, (n, heads)


@test
def test_pack_block_cite_header(tmp: Path):
    """AC-CT-17.3: блок единицы в паке — `[<статус> | дословный текст документа | <cite>]`, тело без изменений; «доверенный источник» для неё не печатается."""
    root = _law_project(tmp)
    pack = _pack(root, ARTICLE_PHRASES[4], "--max-cards", "5")
    head = "[knowledge | дословный текст документа | ст. 4, Закон о примерах (№ 1 от 01.02.2020)]"
    assert head in pack, pack[:1500]
    block = pack.split("## Закон-о-примерах-ст-4\n", 1)[1].split("\n---\n", 1)[0]
    assert "доверенный источник" not in block, block[:300]
    text = (root / KB / "Documents" / "Закон-о-примерах-ст-4.md").read_text(encoding="utf-8")
    AC = _mod("aurora_common")
    assert AC.body(text).strip() in block, "тело единицы в паке изменено"
    assert "Накладная составляется складом в двух экземплярах" in block
    # другие состояния
    fm = AC.frontmatter(text)
    assert AC.trust_header(dict(fm, status="draft")) == "[draft | дословный текст документа | " + fm["cite"] + "]"
    assert AC.trust_header(dict(fm, status="deprecated")).startswith("[deprecated | заменено:"), "снятая единица — история"
    assert AC.trust_header({k: v for k, v in fm.items() if k != "cite"}).endswith("| " + fm["title"] + "]"), "без cite — название единицы"
    other = AC.trust_header({"type": "concept", "status": "knowledge", "trust_basis": "задача PRJ-1 в статусе Готово"})
    assert other == "[knowledge | доверенный источник | задача PRJ-1 в статусе Готово]", other
    CP = _mod("ctx_pack")
    assert CP.Card(f"{KB}/Documents/Закон-о-примерах-ст-4.md", text).header() == head


# ------------------------------------------------------------------ запрос к модели (AC-CT-17.4)

@test
def test_ask_prompt_rule7(_t):
    """AC-CT-17.4: в `PROMPT_ASK` есть правило 7 о дословных цитатах; в правилах 3 и 4 нет выведенных статусов `verified`, `imported`; шаблон форматируется."""
    R = _mod("agent_runner")
    prompt = R.PROMPT_ASK
    rules = {int(m.group(1)): m.group(2) for m in re.finditer(r"(?ms)^(\d)\. (.+?)(?=^\d\. |\Z)", prompt.split("Правила ответа", 1)[1])}
    assert sorted(rules) == [1, 2, 3, 4, 5, 6, 7], sorted(rules)
    seven = " ".join(rules[7].split())
    for need in ("«дословный текст документа»", "в «ёлочках»", "`[[Имя карточки]]`", "значение `cite`", "не пересказывай, не сокращай и не меняй порядок слов",
                 "«[…]»", "«в документе этого нет»", "не домысливай"):
        assert need in seven, need
    for n in (3, 4):
        assert "verified" not in rules[n] and "imported" not in rules[n], rules[n]
    assert "`knowledge`" in rules[3] and "`draft`" in rules[3] and "Decision Record" in rules[3]
    assert "verified" not in prompt and "imported" not in prompt
    out = prompt.format(pack="ПАК", question="ВОПРОС")
    assert "ПАК" in out and "ВОПРОС" in out and "{" not in out


# ------------------------------------------------------------------ проверка цитат (AC-CT-17.2)

BODY = ("# Закон о примерах — ст. 3. Подтверждение заявки\n\n### Статья 3. Подтверждение заявки\n\n"
        "1. Поставщик обязан подтвердить заявку в течение трёх рабочих дней с момента её регистрации.\n"
        "2. Заявка, не подтверждённая в указанный срок, считается отклонённой, о чём заказчик уведомляется письменно.\n")
CARDS = {"Закон-ст-3": BODY}


@test
def test_check_quotes_verbatim_and_changed(_t):
    """Дословная цитата — проверена и найдена; изменённое слово — не найдена, и фрагмент назван; регистр различается."""
    R = _mod("agent_runner")
    ok = R.check_quotes("Срок: «Поставщик обязан подтвердить заявку в течение трёх рабочих дней» [[Закон-ст-3]] ст. 3, Закон.", CARDS)
    assert ok == {"checked": 1, "missing": []}, ok
    bad = R.check_quotes("Срок: «Поставщик обязан подтвердить заявку в течение пяти рабочих дней» [[Закон-ст-3]]", CARDS)
    assert bad["checked"] == 1 and len(bad["missing"]) == 1, bad
    assert bad["missing"][0]["card"] == "Закон-ст-3" and "пяти рабочих дней" in bad["missing"][0]["quote"]
    case = R.check_quotes("«поставщик обязан подтвердить заявку в течение трёх рабочих дней» [[Закон-ст-3]]", CARDS)
    assert case["checked"] == 1 and len(case["missing"]) == 1, "регистр сохраняется"
    both = R.check_quotes("«Поставщик обязан подтвердить заявку в течение трёх рабочих дней» [[Закон-ст-3]]. "
                          "«Заявка не считается отклонённой никогда и ни при каких условиях» [[Закон-ст-3]]", CARDS)
    assert both["checked"] == 2 and len(both["missing"]) == 1


@test
def test_check_quotes_normalizes_spaces_dashes_and_quotes(_t):
    """Пробелы (в том числе неразрывные и переносы), тире и кавычки приводятся к одному виду; слова и порядок — нет."""
    R = _mod("agent_runner")
    body = 'Склад — место приёмки.\n\nЗаказчик принимает товар «по накладной» — без исключений, и подписывает акт.'
    cards = {"К": body}
    spaced = "«Заказчик\u00a0принимает  товар\n\u201eпо накладной\u201c \u2013 без исключений, и подписывает акт» [[К]]"
    assert R.check_quotes(spaced, cards) == {"checked": 1, "missing": []}, "вложенные кавычки в ответе — другого вида, чем «ёлочки» цитаты"
    dash = '«Склад \u2014 место приёмки. Заказчик принимает товар "по накладной" - без исключений» [[К]]'
    assert R.check_quotes(dash, cards)["checked"] == 1
    moved = "«Заказчик принимает товар без исключений по накладной, и подписывает акт» [[К]]"
    assert len(R.check_quotes(moved, cards)["missing"]) == 1, "порядок слов важен"


@test
def test_check_quotes_ellipsis_gap_short_fragments_and_foreign_links(_t):
    """«[…]» между кусками одной карточки — части проверяются каждая; короткий фрагмент, ссылка на другую карточку или на карточку не из пака не цитата документа."""
    R = _mod("agent_runner")
    gap = R.check_quotes("«Поставщик обязан подтвердить заявку […] считается отклонённой» [[Закон-ст-3]]", CARDS)
    assert gap == {"checked": 1, "missing": []}, gap
    gap_bad = R.check_quotes("«Поставщик обязан подтвердить заявку […] считается принятой всегда» [[Закон-ст-3]]", CARDS)
    assert len(gap_bad["missing"]) == 1
    dots = R.check_quotes("«Поставщик обязан подтвердить заявку [...] считается отклонённой» [[Закон-ст-3]]", CARDS)
    assert dots["checked"] == 1 and not dots["missing"], "три точки в скобках — то же, что многоточие"
    assert R.check_quotes("«Поставщик обязан» [[Закон-ст-3]]", CARDS) == {"checked": 0, "missing": []}, "короче 25 знаков"
    assert R.check_quotes("«Совсем другая карточка с длинным текстом внутри» [[Заявка]]", CARDS)["checked"] == 0
    assert R.check_quotes("«Поставщик обязан подтвердить заявку в течение трёх рабочих дней» [[Закон-ст-9]]", CARDS)["checked"] == 0
    assert R.check_quotes("«Поставщик обязан подтвердить заявку в течение трёх рабочих дней» ([[Закон-ст-3]], ст. 3)", CARDS)["checked"] == 1
    assert R.check_quotes("«Поставщик обязан подтвердить заявку в течение трёх рабочих дней» [ст. 3] [[Закон-ст-3|ст. 3]]", CARDS)["checked"] == 1
    assert R.check_quotes("", CARDS) == {"checked": 0, "missing": []} and R.check_quotes("текст", {}) == {"checked": 0, "missing": []}
    assert R.check_quotes("«Поставщик обязан подтвердить заявку в течение трёх рабочих дней» [[закон-ст-3]]", CARDS)["checked"] == 1, \
        "регистр имени карточки в ссылке не важен"


@test
def test_cited_bodies_come_from_cited_cards_only(tmp: Path):
    """`cited_bodies` берёт тела только единиц цитируемых документов и только из пака; обычные карточки и пустые имена пропускает."""
    root = _law_project(tmp)
    card(root, "Concepts/Заявка.md", "Заявка — документ.", status="knowledge", kind="knowledge")
    R = _mod("agent_runner")
    got = R.cited_bodies(str(root), ["Закон-о-примерах-ст-2", "Заявка", "Нет-такой", "Закон-о-примерах-ст-9"])
    assert list(got) == ["Закон-о-примерах-ст-2"] and "Основные понятия" in got["Закон-о-примерах-ст-2"]
    assert R.cited_bodies(str(root), ["../Concepts/Заявка", "..\\Concepts\\Заявка", ".Закон", ""]) == {}, "имя с путём или точкой не открывается"


# ------------------------------------------------------------------ строка в отчёте ответа

def _ask(root: Path, answer: str, pack: str) -> dict:
    R = _mod("agent_runner")

    def fake_pack(cwd, script, args):
        return {"ok": True, "out": pack}

    def fake_call(cfg, role, messages, deadline=None, history=None, **kw):
        return {"ok": True, "text": answer, "backend": 1, "model": "m", "log": []}

    real = R.run_command
    R.run_command = fake_pack
    try:
        return R.run_ask({"request_timeout": 60}, str(root), "Какой срок подтверждения заявки?", "ask", 40,
                         call=fake_call, momus=False)
    finally:
        R.run_command = real


@test
def test_ask_reports_checked_quotes(tmp: Path):
    """AC-CT-17.2: подставной ответ с дословной цитатой — «Цитаты: проверено 1, не найдено 0»; с изменённой — «не найдено 1» и фрагмент в списке."""
    R = _mod("agent_runner")
    root = _law_project(tmp)
    pack = ("# Пак (карточек 1)\n\n## Закон-о-примерах-ст-3 — Закон о примерах — ст. 3. Подтверждение заявки\n\n"
            "[knowledge | дословный текст документа | ст. 3, Закон о примерах (№ 1 от 01.02.2020)]\n\n<untrusted>\nтекст\n</untrusted>\n")
    good = ("Подтверждение — в три дня: «Поставщик обязан подтвердить заявку в течение трёх рабочих дней» "
            "[[Закон-о-примерах-ст-3]] ст. 3, Закон о примерах.")
    res = _ask(root, good, pack)
    assert res["ok"] and res["cites"]["checked"] == 1 and res["cites"]["missing"] == [] and res["cites"]["cards"] == ["Закон-о-примерах-ст-3"], res
    report = R.report_ask(res, "Какой срок подтверждения заявки?", {"request_timeout": 60})
    assert "Цитаты: проверено 1, не найдено 0" in report, report
    bad = good.replace("трёх рабочих дней", "пяти рабочих дней")
    res = _ask(root, bad, pack)
    report = R.report_ask(res, "Какой срок подтверждения заявки?", {"request_timeout": 60})
    assert "Цитаты: проверено 1, не найдено 1" in report and "пяти рабочих дней" in report and "[[Закон-о-примерах-ст-3]]" in report, report
    assert res["ok"] and res["answer"] == bad, "ответ не блокируется"
    paraphrase = "Подтвердить заявку надо за три рабочих дня [[Закон-о-примерах-ст-3]]."
    report = R.report_ask(_ask(root, paraphrase, pack), "вопрос", {"request_timeout": 60})
    assert "Цитаты: проверено 0, не найдено 0" in report and "нет дословных цитат" in report, report


@test
def test_ask_report_has_no_quotes_line_without_documents_in_the_pack(tmp: Path):
    """В паке нет единиц документов — строки о цитатах нет совсем: обычные ответы не обрастают пустой проверкой."""
    R = _mod("agent_runner")
    root = _law_project(tmp)
    card(root, "Concepts/Заявка.md", "Заявка — документ, которым заказчик поручает поставку.", status="knowledge", kind="knowledge")
    pack = "# Пак (карточек 1)\n\n## Заявка — Заявка\n\n[knowledge | доверенный источник | основание]\n\n<untrusted>\nЗаявка — документ.\n</untrusted>\n"
    res = _ask(root, "Заявка — это документ, которым заказчик поручает поставку: «Заявка — документ, которым заказчик поручает поставку» [[Заявка]].", pack)
    assert "cites" not in res and "Цитаты:" not in R.report_ask(res, "вопрос", {"request_timeout": 60}), res
    assert json.dumps(res, ensure_ascii=False)


@test
def test_ask_cites_catalog_describes_the_check(_t):
    """Каталог вызовов называет дословные цитаты у «Спросить» на двух языках, а новых мест вызова модели нет: проверка цитат — код."""
    from harness import KIT
    data = json.loads((KIT / "scripts" / "llm_calls.json").read_text(encoding="utf-8"))
    ask = next(c for c in data["calls"] if c["id"] == "ask")
    what = ask["steps"][0]["what"]
    assert "дословно" in what["ru"] and "word for word" in what["en"]
    assert all(":check_quotes" not in code for code in ask["code"]), "check_quotes не вызывает модель и в каталог не входит"
    ru = (KIT / "docs" / "llm-calls.md").read_text(encoding="utf-8")
    en = (KIT / "docs" / "en" / "llm-calls.md").read_text(encoding="utf-8")
    assert "цитируются дословно" in ru and "quoted word for word" in en
