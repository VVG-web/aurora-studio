"""Проверки движка Aurora, часть 143. Каркас и помощники — tests/harness.py.

«Спросить» быстрее без потери содержания: журнал правок движка («История изменений») не входит в пак — он занимал
четверть запроса, и его читали и модель-отвечающий, и Момус; нормализация слов поиска помнит результат; ответ и отчёт
называют, куда ушло время (пак, ответ, проверка, токены в запросе). Модель и сеть не используются, проекты — только
во временных папках.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from harness import SCRIPTS, card, make_project, run, test

JOURNAL = ("## История изменений\n\n"
           "- 2026-10-01: тезис пересобран — источник изменился (`Raw/project/Регламент.md`). Добавлены метрики источника.\n"
           "- 2026-10-02: тезис пересобран — источник изменился (`Raw/project/Регламент.md`). Уточнён срок подтверждения.\n"
           "- 2026-10-03: связи пересчитаны.\n")


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


# ------------------------------------------------------------------ drop_footer

@test
def test_drop_footer_cuts_only_the_engine_journal(tmp: Path):
    """Журнал карточки вырезается вместе с заголовком и считается по записям; остальной текст — слово в слово."""
    AC = _mod("aurora_common")
    head = "Тезис карточки: срок подтверждения заявки три рабочих дня.\n\n## Источник (перенесено дословно)\n\nТекст страницы.\n"
    text, n = AC.drop_footer(head + "\n" + JOURNAL)
    assert n == 3 and text == head.rstrip("\n") + "\n", (n, text)
    assert "тезис пересобран" not in text and "История изменений" not in text
    # следующий раздел второго уровня после журнала остаётся
    merged, k = AC.drop_footer(head + "\n" + JOURNAL + "\n## Слияние\n\nСлито с двойником.\n")
    assert k == 3 and merged.endswith("## Слияние\n\nСлито с двойником.\n") and "тезис пересобран" not in merged, merged
    assert merged.count("## Слияние") == 1
    # журнала нет — текст тот же объект и ноль записей
    plain, zero = AC.drop_footer(head)
    assert zero == 0 and plain == head


@test
def test_drop_footer_leaves_a_heading_inside_quoted_source_text(tmp: Path):
    """В дословном тексте чужой страницы «История изменений» экранирована (`quote_safe`), и это не журнал карточки:
    её содержимое остаётся — по нему спрашивают («что менялось в версии 1.2 документа»)."""
    AC = _mod("aurora_common")
    page = "## История изменений\n\n- 1.2: добавлен раздел о платежах\n"
    body = ("Тезис.\n\n## Источник (перенесено дословно)\n\n" + AC.quote_safe(page) + "\n" + JOURNAL)
    text, n = AC.drop_footer(body)
    assert n == 3, n
    assert "1.2: добавлен раздел о платежах" in text, text
    assert "тезис пересобран" not in text


# ------------------------------------------------------------------ пак

@test
def test_pack_drops_the_journal_and_keeps_the_knowledge(tmp: Path):
    """В паке нет журнала правок, есть тезис, дословный источник, слово человека и строка с числом записей; карточка
    без журнала строки не получает."""
    root = make_project(tmp)
    card(root, "Concepts/Подтверждение-заявки.md",
         "Заявка подтверждается в течение трёх рабочих дней после поступления.\n\n"
         "## Источник (перенесено дословно)\n\n### Raw/project/Регламент.md\n\nСрок подтверждения заявки — три рабочих дня.\n\n"
         "## Исправления человеком\n\n- Срок считается от даты регистрации.\n\n" + JOURNAL,
         status="knowledge", kind="knowledge")
    card(root, "Concepts/Склад-заказчика.md",
         "Склад заказчика принимает подтверждённые заявки и оформляет накладную.",
         status="knowledge", kind="knowledge")
    out = run("ctx_pack.py", "срок подтверждения заявки", "--mode", "ask", "--no-log", cwd=root, expect_rc=0).stdout
    assert "Заявка подтверждается в течение трёх рабочих дней" in out, out
    assert "Срок подтверждения заявки — три рабочих дня." in out and "Срок считается от даты регистрации." in out, out
    assert "тезис пересобран" not in out and "связи пересчитаны" not in out and "## История изменений" not in out, out
    assert "История изменений карточки в контекст не входит (записей: 3)" in out, out
    assert out.count("в контекст не входит") == 1, "строка только у карточки с журналом"


@test
def test_pack_is_smaller_by_the_journal_and_equal_otherwise(tmp: Path):
    """Пак с журналом отличается от пака без него только журналом: размер падает на длину журнала минус строка-пометка."""
    root = make_project(tmp)
    body = "Подтверждение заявки занимает три рабочих дня, затем склад принимает товар.\n"
    card(root, "Concepts/Подтверждение-заявки-с-журналом.md", body + "\n" + JOURNAL, status="knowledge", kind="knowledge")
    with_j = run("ctx_pack.py", "подтверждение заявки", "--mode", "ask", "--no-log", cwd=root, expect_rc=0).stdout
    (root / "AuroraKnowledgeDB/Concepts/Подтверждение-заявки-с-журналом.md").unlink()
    card(root, "Concepts/Подтверждение-заявки-с-журналом.md", body, status="knowledge", kind="knowledge")
    without = run("ctx_pack.py", "подтверждение заявки", "--mode", "ask", "--no-log", cwd=root, expect_rc=0).stdout
    note = "_История изменений карточки в контекст не входит (записей: 3)._"
    assert with_j.replace("\n\n" + note, "") == without or with_j.replace(note, "").strip() == without.strip(), (with_j, without)
    assert len(with_j) < len(without) + len(JOURNAL), "журнал в пак не попал"


# ------------------------------------------------------------------ нормализация слов

@test
def test_norm_is_cached_and_keeps_its_keys(tmp: Path):
    """`norm` помнит результат (повторные слова не перебирают окончания заново) и отдаёт те же ключи, что и без памяти."""
    CP = _mod("ctx_pack")
    raw = CP.norm.__wrapped__
    words = ["обеспечительного", "обеспечительный", "платежа", "платёж", "Заявки", "заявка", "подтверждение", "ПРМ",
             "статусов", "ё", "ab", "корректировка", "корректировки", "ёлка", "реестру", "реестра"]
    assert [CP.norm(w) for w in words] == [raw(w) for w in words]
    assert CP.norm("обеспечительного") == CP.norm("Обеспечительный")      # словоформы сходятся, как раньше
    CP.norm.cache_clear()
    CP.norm("заявки")
    before = CP.norm.cache_info().hits
    CP.norm("заявки")
    assert CP.norm.cache_info().hits == before + 1


# ------------------------------------------------------------------ время в ответе

def _ask(root: Path, momus: bool) -> dict:
    R = _mod("agent_runner")
    pack = "# Пак (карточек 1)\n\n## Подтверждение-заявки\n\n[knowledge]\n\n<untrusted>\nСрок три рабочих дня.\n</untrusted>\n"

    def fake_pack(cwd, script, args):
        return {"ok": True, "out": pack}

    def fake_call(cfg, role, messages, deadline=None, history=None, **kw):
        if role == "qa":
            return {"ok": True, "text": "ОПОРА «три рабочих дня»\nВЕРДИКТ: ЧИСТО", "backend": 1, "model": "judge",
                    "seconds": 42.0, "log": []}
        return {"ok": True, "text": "Три рабочих дня [[Подтверждение-заявки]].", "backend": 1, "model": "m",
                "seconds": 125.0, "tokens_in": 91_000, "tokens_out": 3_800, "log": []}

    real = R.run_command
    R.run_command = fake_pack
    try:
        return R.run_ask({"request_timeout": 600}, str(root), "Какой срок подтверждения заявки?", "ask", 40,
                         call=fake_call, momus=momus)
    finally:
        R.run_command = real


@test
def test_ask_names_where_the_time_went(tmp: Path):
    """Результат называет время пака и ответа и токены запроса, отчёт — всё это строкой «Время»: разбор «почему шесть минут»
    без чтения логов. Время проверки Момус меряет сам (подставной вызов мгновенен), поэтому в отчёт его подаёт готовый словарь."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    res = _ask(root, momus=True)
    ph = res["phases"]
    assert ph["answer"] == 125.0 and ph["prompt_tokens"] == 91_000 and ph["answer_tokens"] == 3_800, ph
    assert ph["pack"] >= 0.0 and ph["momus"] >= 0.0 and res["momus"]["clean"], (ph, res.get("momus"))
    cfg = {"request_timeout": 600}
    report = R.report_ask(dict(res, phases=dict(ph, pack=17.0, momus=42.0)), "Какой срок подтверждения заявки?", cfg)
    line = next(x for x in report.splitlines() if x.startswith("_Время:"))
    assert line == "_Время: пак 17 с · ответ 2 мин 05 с · проверка 42 с · в запросе ≈91 тыс. ток., модель написала 3800 ток._", line
    solo = R.report_ask(dict(_ask(root, momus=False)), "вопрос", cfg)
    assert "проверка" not in next(x for x in solo.splitlines() if x.startswith("_Время:")), solo
    # токенов от модели нет — запрос оценивается по длине текста, а «модель написала» не печатается
    est = R.report_ask(dict(res, phases=dict(ph, prompt_tokens=60_000, answer_tokens=0)), "вопрос", cfg)
    assert "в запросе ≈60 тыс. ток._" in est and "модель написала" not in est, est
