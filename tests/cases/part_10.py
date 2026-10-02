"""Проверки движка Aurora, часть 10 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import sys

from harness import (  # noqa: F401
    KIT,
    SCRIPTS,
    card,
    card_srcs,
    make_project,
    run,
    test,
    why,
)


@test
def test_relinking_adds_links_and_proves_the_text_is_untouched(tmp: Path):
    """Связи: механика ставит названное в тексте, модель — только пары «фраза → карточка».

    Тезисы уже написаны и проверены Момусом, переписывать их ради связей нельзя. С 1.138.0
    модель тезис не возвращает вовсе: механика ставит ссылки на карточки, чьё имя, заголовок
    или синоним названы в тексте (и в других падежах), а модель называет пары для
    оставшихся кандидатов. Вставляет их движок дословно и сверяет, что текст тот же.
    Замер 27.09.2026: в среднем 7 тысяч токенов ответа на карточку уходили на то, чтобы
    вернуть тезис буква в букву, а механика находит 44–92 % прежних ссылок модели.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    importlib.reload(R)
    assert R.strip_links("из [[ФЦОД]] и [[Профиль абонента|Профиля]]") == "из ФЦОД и Профиля"
    flat = " ".join(R.PROMPT_RELINK.split())
    assert '"phrase"' in flat and "буква в букву" in flat and "Текст тезиса не меняется" in flat

    root = make_project(tmp)
    thesis = ("Аналитический баланс получает данные из ФЦОД по расписанию и обновляет "
              "профиль абонента. Остатки хранятся за расчётный период. Сверка проводится "
              "ежедневно и фиксируется в журнале операций по каждому счёту.")
    for name, title, extra in (("ФЦОД", "ФЦОД", ""), ("Профиль-абонента", "Профиль абонента", ""),
                               ("Журнал-учёта-операций", "Журнал учёта операций", "")):
        card(root, f"Concepts/{name}.md", status="draft", kind="knowledge",
             body="Подсистема и её назначение. " * 3)
    path = card(root, "Concepts/Аналитический-баланс.md", status="draft", kind="knowledge",
                distilled="2026-09-01", body=thesis)
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}
    calls = []

    def pairs(*links, text=None):
        def fake(c, role, messages, **kw):
            calls.append(messages[0]["content"])
            return {"ok": True, "backend": 1, "model": "m", "log": [],
                    "text": text if text is not None else json.dumps(
                        {"links": [{"phrase": p, "card": n} for p, n in links]},
                        ensure_ascii=False)}
        return fake

    was = R.candidates_for
    here = os.getcwd()
    try:
        os.chdir(root)
        # механика: имя дословно и в другом падеже — без модели, кандидатов не осталось
        R.candidates_for = lambda *a, **k: [("ФЦОД", "Concepts", ""),
                                            ("Профиль-абонента", "Concepts", "")]
        st = R.relink_card(cfg, str(path), pairs(), apply=True)
        assert not calls and st["status"] == "связана" and st["added"] == 2, (st, calls)
        got = path.read_text(encoding="utf-8")
        assert "из [[ФЦОД]] по расписанию" in got and \
            "[[Профиль-абонента|профиль абонента]]" in got, got
        # модель: пара для оставшегося кандидата; фраза не из текста и чужая карточка — мимо
        R.candidates_for = lambda *a, **k: [("Журнал-учёта-операций", "Concepts", "")]
        st = R.relink_card(cfg, str(path), pairs(("журнале операций", "Журнал-учёта-операций"),
                                                 ("журнале сверок", "Журнал-учёта-операций"),
                                                 ("Остатки", "Выдуманная")), apply=True)
        assert st["status"] == "связана" and st["added"] == 1, st
        assert "[[ФЦОД]]" in calls[-1], "модель не видит ссылок, уже поставленных механикой"
        got = path.read_text(encoding="utf-8")
        assert "[[Журнал-учёта-операций|журнале операций]]" in got and "Выдуманная" not in got
        assert R.strip_links(got.split("## ")[0]) .count("ежедневно") == 1
        assert R.strip_links(R.thesis_of(got)).endswith(" ".join(thesis.split()).rstrip()[-40:]), \
            "текст тезиса изменился"
    finally:
        R.candidates_for = was
        os.chdir(here)

    # отметка держит дату тезиса: перепишут тезис — карточка вернётся сама
    R.mark_relinked(str(path))
    assert "relinked: 2026-09-01" in path.read_text(encoding="utf-8")


@test
def test_relinking_runs_pass_after_pass_without_burning_a_pass_to_count(tmp: Path):
    """Заходы связывания гоняет движок, и остаток он знает сам.

    Четыреста карточек за двадцатиминутный бюджет не обойти, поэтому связывание идёт
    заходами. На живом прогоне заходы гонял скрипт в шелле, а остаток спрашивал
    отдельным запуском `--task relink` без `--apply`. Предпросмотр — это не подсчёт:
    он честно гонял модель по всей очереди все двадцать минут и ничего не записывал.
    Половина ночи ушла в холостые прогоны, темп упал вдвое, а в журнале осталась
    запись с пометкой «(dry-run) Ничего не записано» — рядом с настоящей работой.

    Очередь же собирается обходом файлов и стоит даром. Значит петля обязана жить в
    движке: он пересчитывает очередь между заходами бесплатно и сообщает остаток в
    ответе, чтобы спрашивать было нечего.
    """
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert '"left": left' in src, \
        "run_relink не сообщает остаток — петле придётся выяснять его прогоном модели"
    assert 'a.task == "relink" and a.until_done and a.apply' in src, \
        "у связывания нет своей петли — заходы снова придётся гонять скриптом снаружи"
    loop = src.split('a.task == "relink" and a.until_done and a.apply')[1].split("elif a.task ==")[0]
    assert 'run_relink(cfg, cwd, True, a.limit' in loop, \
        "заход внутри петли идёт без записи — очередь не убудет, петля не кончится"
    assert 'until_done(cwd, a, "relink"' in loop, "связывание идёт мимо общего цикла заходов"
    # Петля заходов одна на движок (`until_done`): её правила проверяются там.
    shared = src.split("def until_done(")[1].split("\ndef ")[0]
    assert 'commit_result(cwd, f"agent:{task}"' in shared, \
        "заходы не коммитятся по отдельности — откатить можно будет только всё сразу"
    # После петли остаётся закоммитить журнал. Живой прогон дал ему заголовок последнего
    # захода — «связей поставлено: 10» на коммите, где нет ни одной из этих связей.
    tail = src.split("Журнал прогона")[1].split("if a.task == \"clashes\"")[0]
    assert "журнал прогона: заходов" in tail, \
        "коммит журнала говорит о последнем заходе, а содержит весь прогон"
    assert "looks_offline(res)" in shared, \
        "обрыв связи останавливает ночной прогон вместо ожидания"
    assert 'res["left"]' in shared and "run_relink" in loop, \
        "петля берёт остаток не из ответа прогона"
    # предпросмотр отметок не ставит: очередь не убывает, петля крутилась бы вечно
    guard = src.split('elif a.task == "relink":')[1][:400]
    assert "a.until_done and not a.apply" in guard, \
        "--until-done без --apply зациклится: предпросмотр не двигает очередь"

    # шаг есть в маршрутах и идёт до общего связывания: карты считают брошенных по
    # входящим, и посчитанные раньше связей — врут
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    for tag in ("[update]", "[fix]", "[rebuild]"):
        part = scen.split(tag)[1].split("\n[")[0]
        assert "agent:relink" in part, f"в маршруте {tag} нет связывания тезисов"
        assert part.index("agent:relink") < part.rindex("kb:moc"), \
            f"в маршруте {tag} карты строятся раньше связей — брошенные будут посчитаны неверно"


@test
def test_the_thesis_writes_its_own_links(tmp: Path):
    """Тезис пишется без ссылок; выдуманная ссылка, если всё же появилась, снимается.

    История: сначала связи ставил движок узко, и 232 карточки из 291 были без единой
    ссылки. Тогда тезису дали список карточек — и с 1.94.1 до 1.118.0 он его всё равно не
    получал: первый тезис по ошибке шёл как пересборка, без списка. Когда ошибку
    исправили, замер PRJ-A 22.09.2026 показал цену списка: модель ссылается на карточки
    как на источники («согласно …», «описано в …»), и строгий Момус находит в тезисах
    втрое больше утверждений без опоры. Связи ставит `agent:relink` — там движок
    доказывает, что текст не изменился ни на символ.

    Ниже — прежняя история решения; она объясняет, зачем снимаются выдуманные ссылки.

    Правило было обратным: «не выдумывай ссылки, связи расставляет движок». Движок
    расставлял их узко — по ключам требований и номерам историй, — и на живой базе
    **232 карточки из 291 не имели в тезисе ни одной ссылки**. База выходила кучей, а
    не сетью: до знания, лежащего рядом, человек не доходил.

    Запрет имел смысл, пока модель не знала состава базы. Теперь она получает список
    карточек — тот же, по которому дописывает знание, — и связь становится частью мысли,
    как ей и положено в картотеке.

    Обратная сторона: выдуманная ссылка ведёт в никуда, а ремонт заводит под неё пустышку.
    Поэтому имя, которого в базе нет, снимается, а текст остаётся словами.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    importlib.reload(R)

    flat = " ".join(R.PROMPT_DISTILL.split())
    assert "Ссылок `[[…]]` не ставь" in flat and "согласно" in flat, \
        "тезису снова велено ссылаться — пойдут отсылки к карточкам, которых нет в источнике"
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    first = src.split("    elif len(parts) == 1:\n")[1].split("    else:\n")[0]
    assert "candidates_block(" not in first and "links_block(" not in first, \
        "первый тезис снова получает список карточек — вернутся отсылки без опоры"
    assert "drop_invented_links(thesis, root)" in src, \
        "выдуманные ссылки не снимаются — ремонт заведёт под них пустышки"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    upd = scen.split("[update]")[1].split("\n[")[0]
    assert upd.index("agent:distill") < upd.index("agent:relink"), \
        "связывание не идёт после тезисов — связей в базе не будет вовсе"

    root = make_project(tmp)
    card(root, "Concepts/ФЦОД.md", status="draft", body="Подсистема обработки платежей.")
    here = os.getcwd()
    try:
        os.chdir(root)
        kept, n = R.drop_invented_links(
            "Данные приходят из [[ФЦОД]] и из [[Небывалая-система]].", str(root))
    finally:
        os.chdir(here)
    assert "[[ФЦОД]]" in kept, "снята ссылка на существующую карточку"
    assert "[[Небывалая-система]]" not in kept and "Небывалая-система" in kept, \
        f"выдуманное имя должно остаться словами, а не ссылкой: {kept}"
    assert n == 1, n


@test
def test_maps_are_drawn_after_the_base_is_linked(tmp: Path):
    """Карты содержания собираются ПОСЛЕ сплошной связки, а не до.

    «Брошенные» считаются по входящим ссылкам. Собранные раньше связывания, они
    объявляют брошенными тех, кого свяжут через минуту: на живой базе страница
    показывала 228 карточек вместо 116 — и именно она встречала человека первой в
    графе Obsidian.
    """
    text = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    for route in ("[update]", "[fix]", "[rebuild]"):
        start = text.index(route)
        end = min((text.index(m, start + 1) for m in ("\n[", ) if m in text[start + 1:]),
                  default=len(text))
        block = text[start:text.find("\n[", start + 1) if text.find("\n[", start + 1) > 0
                     else len(text)]
        moc = [i for i, l in enumerate(block.splitlines())
               if l.startswith("kb:moc") and "--by-source" not in l]
        links = [i for i, l in enumerate(block.splitlines()) if l.startswith("kb:links")]
        if not moc:
            continue
        assert links and min(links) < moc[-1], \
            f"в маршруте {route} карты содержания собираются раньше связей — " \
            "«Брошенные» покажут тех, кого свяжут следующим шагом"


@test
def test_gaps_finds_what_the_linter_cannot_see(tmp: Path):
    """Смысловые дыры: понятие без карточки, названная и не поставленная связь, одиночки.

    `kb:lint` проверяет механику — ссылки, схему, статусы. От этого база не перестаёт
    быть базой. Перестаёт она от другого: сущность названа в восьмидесяти карточках, а
    своей у неё нет; карточка называет соседа и не ссылается на него; карточка не связана
    ни с чем. На живом проекте так и оказалось: `НДС` в 89 карточках без собственной.
    """
    root = make_project(tmp)
    card(root, "Reference/Сокращения-проекта.md", type="reference", status="knowledge",
         body="| Сокращение | Значение |\n|---|---|\n| ПРФ | профиль обслуживания |\n")
    card(root, "Concepts/Профиль-абонента.md", status="draft", kind="knowledge",
         distilled="2026-09-01",
         body="Профиль абонента задаёт услуги. Значение ПРФ берётся на дату подачи.")
    card(root, "Concepts/Тариф.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body="Тариф — цена обращения к услуге. Считается по данным ПРФ и зависит от "
              "того, какой Профиль-абонента назначен договором.")
    card(root, "Concepts/Одинокая.md", status="draft", kind="knowledge",
         distilled="2026-09-01", body="Ни на кого не ссылается и никем не назван.")

    cp = run("kb_gaps.py", "--min-mentions", "2", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    out = cp.stdout
    assert "ПРФ" in out, "понятие, названное в двух карточках без своей, не найдено"
    assert "Наверняка сущности" in out, \
        "словарь проекта не использован — достоверное не отделено от предположения"
    assert "профиль обслуживания" in out, "не сказано, что это за понятие"
    assert "Тариф" in out and "Профиль-абонента" in out, \
        f"не найдена названная и не поставленная связь:\n{out}"
    assert "Одинокая" in out, "одинокая карточка не названа"
    assert "человек" in out, "отчёт не говорит, где работа человека"


@test
def test_clashes_quote_both_sides_and_judge_nobody(tmp: Path):
    """Противоречие показывают цитатами и не решают, кто прав.

    Это последний пункт списка Карпаты, которого механикой не взять: «обе карточки
    нельзя считать верными одновременно» — суждение о смысле. Но суждение опасное:
    спор о том, чего никто не писал, дороже необнаруженного. Поэтому цитата обязана
    быть дословной, а решение остаётся человеку и источникам.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")

    flat = " ".join(R.PROMPT_CLASH.split())
    assert "Цитируй **дословно** обе стороны" in flat, "цитата не требуется дословной"
    assert "Не решай, кто прав" in flat, "модель поставлена судьёй вместо человека"
    for not_a_clash in ("разная подробность", "разные стороны предмета", "разные периоды"):
        assert not_a_clash in flat, f"не сказано, что «{not_a_clash}» — не противоречие"

    root = make_project(tmp)
    card(root, "Concepts/Срок-А.md", status="draft", kind="knowledge",
         body="Срок подтверждения — пять дней с даты подачи.")
    card(root, "Concepts/Срок-Б.md", status="draft", kind="knowledge",
         body="Срок подтверждения — три дня с даты подачи.")
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}

    def found(c, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
            {"clashes": [{"cards": ["Срок-А", "Срок-Б"], "about": "срок подтверждения",
                          "a": "Срок подтверждения — пять дней с даты подачи.",
                          "b": "Срок подтверждения — три дня с даты подачи."}]},
            ensure_ascii=False)}
    st = R.solve_clash(cfg, str(root), ["Срок-А", "Срок-Б"], call=found)
    assert st["status"] == "спор" and len(st["clashes"]) == 1, st
    assert "пять дней" in st["clashes"][0]["a"], st

    # Карточка не из группы и слишком короткая цитата — не находка, а шум
    def noisy(c, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
            {"clashes": [{"cards": ["Срок-А", "Чужая"], "about": "x", "a": "длинная цитата "
                          "про сроки подтверждения", "b": "тоже длинная цитата про сроки"},
                         {"cards": ["Срок-А", "Срок-Б"], "about": "y", "a": "мало", "b": "мало"}]},
            ensure_ascii=False)}
    st2 = R.solve_clash(cfg, str(root), ["Срок-А", "Срок-Б"], call=noisy)
    assert st2["clashes"] == [], \
        f"взяты находки с чужой карточкой или без дословной цитаты: {st2['clashes']}"

    # и отчёт не берётся судить
    rep = R.report_clashes({"steps": [st], "groups": 1, "found": 1, "seconds": 1.0})
    assert "решает человек" in rep and "[[Срок-А]]" in rep, rep


@test
def test_the_accumulation_rule_is_written_down(tmp: Path):
    """Правило «карточка — сущность» записано и доезжает до проектов.

    Это главное правило зеттелькастена, на котором стоит вся схема, и **самый большой
    открытый долг движка**: разбор идёт по оси «документ → карточки» и базы не видит.
    На живом проекте это дало 493 группы дублей — треть базы повторяет саму себя.

    Требование живёт в трёх местах, и каждое нужно: правило — в правилах базы, разрыв
    между правилом и реализацией — в отдельном документе, краткая сводка — в PROJECT.md,
    который читает ассистент. Проверка держит их на месте: незаписанное требование
    исчезает вместе с тем, кто его помнил, а инструменты вокруг дублей выглядят решением
    задачи, хотя лечат симптом.
    """
    rules = (KIT / "docs/knowledge-rules.md").read_text(encoding="utf-8")
    assert "Карточка — сущность" in rules, \
        "правило накопления пропало из правил базы"
    for must in ("накапливает", "выносится в свою карточку", "kb:twins"):
        assert must in rules, f"правило записано неполно: нет «{must}»"

    spec = KIT / "docs/накопление-знания.md"
    assert spec.is_file(), "документ с требованиями к накоплению исчез"
    body = spec.read_text(encoding="utf-8")
    for must in ("PROMPT_BUILD", "build_card", "ALLOWED_WRITES", "kb:split", "source:"):
        assert must in body, f"в требованиях не назван {must} — по ним нельзя работать"
    assert "Как проверить, что сделано" in body, \
        "требование без признака выполнения — это пожелание"

    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "docs/накопление-знания.md" in man, \
        "документ не едет в проекты: там о долге не узнают"
    # На `agent/project/PROJECT.md` не опираемся: он в .gitignore, и у того, кто
    # склонирует кит, его нет — проверка упала бы на пустом месте. Спрашиваем с того,
    # что действительно едет с движком.
    rules_tldr = (KIT / "docs/knowledge-rules-tldr.md").read_text(encoding="utf-8")
    assert "накоплен" in rules_tldr or "сущность" in rules_tldr, \
        "короткая справка молчит о главном правиле — её читают первой"


@test
def test_twins_are_found_by_text_not_by_name(tmp: Path):
    """Одно знание под разными именами: `kb:dedupe` его не видит, `kb:twins` видит.

    Ремонт ищет двойников по имени — свёрнутому регистру, общему синониму, одинаковому
    title. На живом проекте одну и ту же таблицу несли десять карточек с разными именами,
    и ни одна пара по имени не совпадала. Вред не косметический: ссылки расходятся по
    копиям, правка ложится в одну, остальные продолжают говорить прежнее — и обе стороны
    выглядят одинаково достоверно.

    Мера — общие куски текста. Вёрстка, повторяющаяся во многих карточках, отброшена:
    без этого в одну «группу» склеивались семьдесят карточек с одинаковой шапкой таблицы.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    T = importlib.import_module("kb_twins")

    same = ("Профиль обслуживания абонента задаёт перечень доступных услуг и порядок их "
            "тарификации в биллинге. Профиль назначается при заключении договора и "
            "меняется заявкой абонента через личный кабинет. Смена профиля вступает в "
            "силу с первого числа следующего расчётного периода, а начисления за текущий "
            "период считаются по прежнему профилю. ")
    other = ("Журнал начислений хранит историю операций по лицевому счёту абонента за "
             "весь срок действия договора. Записи журнала не удаляются и не правятся: "
             "исправление вносится сторнирующей записью со ссылкой на исходную. Журнал "
             "закрывается на дату окончания расчётного периода. ")

    root = make_project(tmp)
    card(root, "Concepts/Профиль-абонента.md", status="knowledge", body=same * 3)
    card(root, "Concepts/Профиль-обслуживания.md", status="draft", body=same * 3)
    card(root, "Concepts/Что-такое-профиль.md", status="draft", body=same * 3)
    card(root, "Concepts/Журнал-начислений.md", status="knowledge", body=other * 3)
    # пустышки в сравнение не идут: они похожи друг на друга по построению
    card(root, "Concepts/ПРФ.md", status="placeholder", tags="[заготовка]",
         body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._")

    cp = run("kb_twins.py", cwd=root)
    assert "Профиль-абонента" in cp.stdout and "Что-такое-профиль" in cp.stdout, cp.stdout
    assert "Журнал-начислений" not in cp.stdout, \
        f"в группу попала карточка о другом — мера ловит вёрстку, а не знание:\n{cp.stdout}"
    assert "ПРФ" not in cp.stdout, "пустышки пошли в сравнение и дадут ложные группы"
    assert "оставить" in cp.stdout, "не предложено, кого оставить"
    assert "решение человека" in cp.stdout, \
        "отчёт не говорит, что ничего не слито: его прочтут как выполненную работу"


@test
def test_one_concept_is_found_under_both_spellings(tmp: Path):
    """Ссылка кириллицей находит карточку, названную транслитом, — по словарю имён.

    Источники приходят с разными именами: часть по-русски, часть транслитом. Карточка
    наследует имя источника, в тексте соседей то же понятие названо кириллицей, и до
    транслитерованной карточки ссылка не доходит: ремонт объявляет её битой и заводит
    под неё пустышку. Одно понятие, две карточки, ни одной связи.

    Пара пишется в словарь один раз. До этой версии словарь читал ТОЛЬКО тот скрипт,
    который его писал, — и сопоставление не влияло ни на что: ни поиск сущности при
    разборе, ни разрешение ссылок словаря не спрашивали.

    Незаполненная строка переводом не считается: иначе поиск начал бы находить пустое имя.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    BP = importlib.import_module("build_plan")

    root = make_project(tmp)
    (root / "AuroraKnowledgeDB" / "meta").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB" / "meta" / "translit.md").write_text(
        "| Латиницей | Кириллицей | Добавлено | Кем |\n|---|---|---|---|\n"
        "| SPR-001-Statusy-tarifa | SPR-001 Статусы тарифа | 2026-09-10 | kb:translit |\n"
        "| SPR-009-Rezultat-proverki |  | 2026-09-10 | kb:translit |\n", encoding="utf-8")
    card(root, "Concepts/SPR-001-Statusy-tarifa.md", status="draft",
         body="Справочник статусов тарифа.")

    cur = os.getcwd()
    os.chdir(root)
    try:
        m = AC.translit_map()
        assert m == {"SPR-001-Statusy-tarifa": "SPR-001 Статусы тарифа"}, m
        assert AC.translit_names("SPR-001 Статусы тарифа") == \
            {"SPR-001 Статусы тарифа", "SPR-001-Statusy-tarifa"}, "пара не читается в обе стороны"
        assert BP.find_card("SPR-001 Статусы тарифа"), \
            "поиск сущности не нашёл карточку по кириллице — разбор заведёт вторую о том же"
        assert BP.find_card("SPR-001-Statusy-tarifa"), "поиск сломан для собственного имени"
    finally:
        os.chdir(cur)

    cards = {str(p): __import__("kb_fix").Card(str(p), p.read_text(encoding="utf-8"))
             for p in (root / "AuroraKnowledgeDB" / "Concepts").glob("*.md")}
    os.chdir(root)
    try:
        idx = __import__("kb_fix").Index(cards)
        name, how = idx.resolve("SPR-001 Статусы тарифа")
        assert name == "SPR-001-Statusy-tarifa", f"ссылка кириллицей не разрешилась: {name} ({how})"
        assert "словарь" in how, f"разрешилось не по словарю, а случайно: {how}"
    finally:
        os.chdir(cur)


@test
def test_web_pages_are_their_own_source_with_their_own_trust(tmp: Path):
    """Веб-страницы — отдельный источник со своим доверием на каждую ссылку.

    Страница из интернета и страница корпоративной вики приходят из разных мест и
    доверяются по разным правилам: задаче доверие даёт её статус, документу — тот, кто
    его подключил. Поэтому список веб-ссылок не смешивается с корнями Confluence, а
    галочка «доверять» стоит у КАЖДОЙ ссылки: закон и стандарт доверены, чужой блог нет.

    Галочка уходит в шапку сохранённого файла, и класс доверия читается оттуда — так он
    остаётся свойством источника, а не выводится из совпадения пути на диске.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")
    T = importlib.import_module("kb_trust")

    cfg = ('project:\n  name: X\n'
           'web:\n  pages:\n'
           '    - url: https://law.example/nk\n      trusted: true\n'
           '    - url: https://blog.example/post\n      trusted: false\n'
           '    - url: https://example.org/no-flag\n'
           'privacy:\n  scrub: report\n')
    # Третьим идёт признак ленты: раздел новостей разбирается по строгому правилу —
    # карточка на предмет, а не на событие. Здесь лент нет, и все три — False.
    pages = W.pages_from_config(cfg)
    assert pages == [("https://law.example/nk", True, False),
                     ("https://blog.example/post", False, False),
                     ("https://example.org/no-flag", False, False)], pages

    # Имя файла: два разных адреса с одинаковым заголовком не смеют писаться в один файл —
    # заголовки страниц повторяются («Главная», «Документация»), и второй затирал бы первый.
    a = W.slug("https://a.example/x", "Документация")
    b = W.slug("https://b.example/y", "Документация")
    assert a != b, f"разные адреса дали одно имя файла: {a}"

    # В шапке НЕТ даты выгрузки: иначе каждый прогон давал бы дифф на всю папку.
    text = W.card_text("https://law.example/nk", "НК РФ", True, "Статья 1.")
    assert "trusted: true" in text and "url: https://law.example/nk" in text, text
    import datetime as _dt
    assert _dt.date.today().isoformat() not in text, \
        "дата выгрузки в шапке — каждый синк будет давать ложный дифф на всю папку"

    mirror = tmp / "Sources" / "Web"
    mirror.mkdir(parents=True)
    (mirror / "zakon.md").write_text(text, encoding="utf-8")
    (mirror / "blog.md").write_text(
        W.card_text("https://blog.example/post", "Пост", False, "Мнение."), encoding="utf-8")
    (mirror / "molchit.md").write_text('---\ntitle: "Без отметки"\n---\n\nТекст.\n',
                                       encoding="utf-8")
    empty = {"direct": {}, "indirect": {}}
    cur = os.getcwd()
    os.chdir(tmp)
    try:
        cls, why = T.source_class("Sources/Web/zakon.md", empty, {}, set(), set())
        assert cls == "raw", f"объявленная доверенной страница не признана: {cls} — {why}"
        cls, why = T.source_class("Sources/Web/blog.md", empty, {}, set(), set())
        assert cls == "draft", f"снятая галочка не сделала источник черновым: {cls} — {why}"
        assert "галочка" in why, f"основание не объясняет, откуда решение: {why}"
        cls, _ = T.source_class("Sources/Web/molchit.md", empty, {}, set(), set())
        assert cls == "unknown", "источник, ничего не сказавший о доверии, стал доверенным"

        # Снятая галочка СИЛЬНЕЕ общей отметки папки. Папку зеркала легко внести в
        # `trusted_sources` целиком — и тогда блог, которому человек отказал в доверии,
        # молча стал бы знанием. Решение о конкретной ссылке принято позже и точнее, чем
        # решение о папке, поэтому оно и побеждает.
        cls, why = T.source_class("Sources/Web/blog.md", empty, {}, set(), set(),
                                  ("Sources/Web",))
        assert cls == "draft", \
            f"общая отметка папки перебила снятую галочку ссылки: {cls} — {why}"
        cls, _ = T.source_class("Sources/Web/molchit.md", empty, {}, set(), set(),
                                ("Sources/Web",))
        assert cls == "raw", "папка в доверенных не сработала там, где ссылка промолчала"
    finally:
        os.chdir(cur)


@test
def test_an_er_entity_is_named_by_its_code_and_found_by_its_label(tmp: Path):
    """Код ER — адрес в модели данных и он же имя; подпись сущности — синоним (Т-73, Т-74).

    ER.Сущность.Поле — как «таблица.поле» в SQL: ER.AB.KPP и ER.ACT.KPP разные поля, и
    укоротить путь до последнего сегмента значит описать одноимённые поля всех таблиц как
    одно. Сущность с подписью (ER.AS.PDFD-Уведомление-…) называется кодом, подпись уходит в
    синонимы — обратно правилу для кодов документов, где код номер бумаги.

    Одинаковый код — одна сущность по определению. На живой базе один код приходил с двумя
    подписями, а четыре кода совпали с уже заведёнными карточками: отказ «карточка уже
    есть» терял бы знание, а поиск с поправкой на опечатку слил бы ER.AS.CCS и ER.AS.CCr —
    разные справочники.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    BP = importlib.import_module("build_plan")

    assert BP.split_er_label("ER.AS.PDFD-Уведомление-о-статусе-PDF-документа") == \
        ("ER.AS.PDFD", ["Уведомление о статусе PDF документа"])
    assert BP.split_er_label("ER.AS.Acc.OGRN") == ("ER.AS.Acc.OGRN", []), "голый путь поля изменён"
    assert BP.split_er_label("ER.AnalyticalBalanceLog-Core-ЖурналБаланса") == \
        ("ER.AnalyticalBalanceLog", ["Core ЖурналБаланса"]), "подпись с латинского начала не отделена"
    assert BP.split_er_label("AC-3.4.2 Отправка начислений")[1] == [], "код документа принят за ER"

    root = make_project(tmp)
    d = root / "Raw" / "customer"
    d.mkdir(parents=True, exist_ok=True)
    para = "Уведомление формируется при смене статуса документа и отправляется заявителю. " * 4
    (d / "model.md").write_text(para + "\n", encoding="utf-8")
    (d / "model2.md").write_text(para.replace("заявителю", "в налоговый орган") + "\n",
                                 encoding="utf-8")

    def make(name, src):
        return run("build_plan.py", "--card", name, "--source", src, "--sections", "1",
                   "--paras", "1", "--to", "Concepts", "--apply", cwd=root)

    cp = make("ER.AS.PDFD-Уведомление-о-статусе-PDF-документа", "Raw/customer/model.md")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    concepts = root / "AuroraKnowledgeDB" / "Concepts"
    card_path = concepts / "ER.AS.PDFD.md"
    assert card_path.is_file(), f"карточка названа не кодом: {sorted(x.name for x in concepts.glob('*.md'))}"
    text = card_path.read_text(encoding="utf-8")
    assert 'title: "ER.AS.PDFD"' in text, text[:300]
    assert "Уведомление о статусе PDF документа" in text.split("---")[1], "подпись не ушла в синонимы"

    # тот же код из другого источника — та же сущность: знание копится в одной карточке
    cp = make("ER.AS.PDFD-Статус-PDF", "Raw/customer/model2.md")
    assert cp.returncode == 0, "одинаковый код из другого источника дал отказ:\n" + cp.stdout + cp.stderr
    files = sorted(x.name for x in (root / "AuroraKnowledgeDB").rglob("ER.AS.PDFD*.md"))
    assert files == ["ER.AS.PDFD.md"], f"одна сущность разошлась по карточкам: {files}"
    text = card_path.read_text(encoding="utf-8")
    assert {"Raw/customer/model.md", "Raw/customer/model2.md"} <= set(card_srcs(text)), \
        "знание второго источника не накопилось в карточке сущности"
    head = text.split("---")[1]
    assert "Статус PDF" in head and "Уведомление о статусе PDF документа" in head, \
        f"вторая подпись не добавлена в синонимы:\n{head}"
    assert "Уведомление формируется" in text, "дописывание синонима испортило тело карточки"

    # поле — полный путь, ничего не срезается
    cp = make("ER.AS.Acc.OGRN", "Raw/customer/model.md")
    assert (concepts / "ER.AS.Acc.OGRN.md").is_file(), cp.stdout + cp.stderr

    # поиск: по полной форме и по подписи — в ту же карточку; буква кода — другая сущность
    card(root, "Concepts/ER.AS.CCS.md", status="draft", body="Справочник CCS.")
    kb = str(root)
    assert BP.find_card("ER.AS.PDFD-Уведомление-о-статусе-PDF-документа", kb).endswith("ER.AS.PDFD.md"), \
        "полная форма не находит карточку сущности — накопление заведёт дубль"
    assert BP.find_card("Уведомление о статусе PDF документа", kb).endswith("ER.AS.PDFD.md"), \
        "сущность не находится по подписи"
    assert BP.find_card("ER.AS.CCr", kb) == "", \
        "код ER сопоставлен с опечаткой: ER.AS.CCr слит с ER.AS.CCS — это разные сущности"


@test
def test_a_machine_transcript_is_not_parsed_when_a_copy_exists(tmp: Path):
    """Документ и его машинная расшифровка — один источник, а не два.

    `kb:ingest-office` кладёт `X.converted.md` рядом с оригиналом, а текстовая копия
    `X.md` бывает уже сделана. Разбирались обе — и знание раздваивалось: на живой
    пересборке один документ дал девять параллельных карточек и две группы двойников.
    Правило заказчика: есть копия — машинную расшифровку не разбирать.

    Копия обязана быть годным источником сама. Пропустив расшифровку при пустой копии,
    мы потеряли бы документ целиком — копию отсечёт порог размера, а расшифровку
    отсекло бы это правило.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    BP = importlib.import_module("build_plan")

    root = make_project(tmp)
    d = root / "Raw" / "customer"
    d.mkdir(parents=True, exist_ok=True)
    text = "Порядок возврата обеспечительного платежа после проверки декларации. " * 8
    (d / "Полный.md").write_text(text, encoding="utf-8")            # копия есть
    (d / "Полный.converted.md").write_text(text, encoding="utf-8")
    (d / "Одинокий.converted.md").write_text(text, encoding="utf-8")  # копии нет
    (d / "Пустой.md").write_text("—", encoding="utf-8")               # копия негодная
    (d / "Пустой.converted.md").write_text(text, encoding="utf-8")

    cur = os.getcwd()
    os.chdir(root)
    try:
        got = {os.path.basename(p) for _g, p, _s in BP.sources()}
    finally:
        os.chdir(cur)

    assert "Полный.md" in got, "копия документа выпала из плана"
    assert "Полный.converted.md" not in got, \
        "машинная расшифровка разбирается рядом с копией — знание раздвоится"
    assert "Одинокий.converted.md" in got, \
        "расшифровка без копии выпала из плана — документ потерян"
    assert "Пустой.converted.md" in got, \
        "расшифровку отсекли из-за пустой копии — документ потерян целиком"
    assert "Пустой.md" not in got, "пустая копия попала в план"


@test
def test_a_news_item_becomes_a_card_only_when_it_has_a_subject(tmp: Path):
    """Лента разбирается по строгому правилу: карточка на предмет, а не на событие.

    Новостная заметка — хроника: «Встреча делегации», «Назначен заместитель». Завтра
    выйдет следующая, и карточка устареет, не успев пригодиться. По правилу заказчика
    карточка — сущность, а не документ, и событие сущностью не является.

    Но лента приносит и предмет: «Пилотный проект внедрения онлайн-касс» — это порядок,
    а не происшествие. Поэтому ленту не выключают целиком, а судят по содержанию.

    Признак ставит ЗЕРКАЛО по разделу, который назвал человек, — не разбор по догадке о
    тексте. Явное `feed:` в конфиге сильнее догадки по адресу: слова для новостного
    раздела у каждого сайта свои.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")
    R = importlib.import_module("agent_runner")

    assert W.is_feed("https://a.example/news/x"), "раздел новостей не опознан лентой"
    assert W.is_feed("https://a.example/press-centr/"), "пресс-центр не опознан лентой"
    assert not W.is_feed("https://a.example/docs/"), "свод документов принят за ленту"
    assert not W.is_feed("https://a.example/news/x", declared=False), \
        "явное «не лента» в конфиге проиграло догадке по адресу"
    assert W.is_feed("https://a.example/docs/", declared=True), \
        "явное «лента» в конфиге проиграло догадке по адресу"

    cfg = ("web:\n  pages:\n"
           "    - url: https://a.example/news/\n      trusted: true\n"
           "    - url: https://a.example/docs/\n      trusted: true\n"
           "    - url: https://a.example/blog/\n      trusted: false\n      feed: false\n")
    got = W.pages_from_config(cfg)
    assert got == [("https://a.example/news/", True, True),
                   ("https://a.example/docs/", True, False),
                   ("https://a.example/blog/", False, False)], got

    root = tmp / "p"
    (root / "Sources" / "Web").mkdir(parents=True)
    (root / "Sources/Web/zametka.md").write_text(
        W.card_text("https://a.example/news/x", "Заметка", True, "Текст.",
                    "https://a.example/news/", [], True), encoding="utf-8")
    (root / "Sources/Web/prikaz.md").write_text(
        W.card_text("https://a.example/docs/x", "Приказ", True, "Текст.",
                    "https://a.example/docs/", [], False), encoding="utf-8")
    assert R.source_is_feed(str(root), "Sources/Web/zametka.md"), \
        "отметка ленты не читается разбором — правило не применится"
    assert not R.source_is_feed(str(root), "Sources/Web/prikaz.md"), \
        "свод документов принят за ленту: половина знания не попадёт в базу"

    # Правило обязано доезжать до задания модели, а не только жить в константе.
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    blk = src[src.index("def solve_source("):src.index("\ndef judge_empty(")]
    assert "feed_rule" in blk and "source_is_feed" in blk, \
        "разбор не спрашивает, лента ли это — правило останется мёртвой строкой"
    assert '"cards": []' in R.FEED_RULE, \
        "модели не сказано, ЧТО отвечать, когда предмета в заметке нет"


@test
def test_a_crawled_page_survives_an_interrupted_run(tmp: Path):
    """Страница пишется до вложений, а текст документа становится источником.

    Вложений у страницы бывает полтора десятка, качаются они минуты. Пока страница
    записывалась ПОСЛЕ них, прогон, прерванный посередине, оставлял на диске картинки
    без страницы: работа сделана, записи о ней нет. На живом сайте так и вышло — восемь
    фотографий и ни одной страницы, и со стороны это выглядело как «скачались одни
    картинки».

    Второе: расшифровка скачанного документа лежит в `_files/`, куда разбор не заходит
    и не должен — папка с подчёркивания это вложения. Значит закон и приказ остались бы
    в базе файлом на диске: он есть, знания из него нет. Расшифровку поднимаем в корень
    зеркала отдельным источником, наследуя доверие от страницы, которая на него сослалась.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")

    out = tmp / "Sources" / "Web"
    (out / W.ASSET_DIR).mkdir(parents=True)

    # страница-владелец: доверенная, ссылается на приказ
    (out / "Документы-aaaaaaaa.md").write_text(
        W.card_text("https://law.example/docs/", "Документы", True,
                    "Список документов.\n\n[Приказ](_files/Приказ-11111111.pdf)",
                    "https://law.example/docs/", ["Приказ-11111111.pdf"]),
        encoding="utf-8")
    (out / W.ASSET_DIR / "Приказ-11111111.pdf").write_bytes(b"%PDF-1.4")
    (out / W.ASSET_DIR / "Приказ-11111111.md").write_text(
        '---\ntitle: "Приказ-11111111"\nconverter: pandoc\n---\n\nПункт 1. Ставка налога.\n',
        encoding="utf-8")
    # вложение, на которое никто не ссылается: доверие не наследуется ниоткуда
    (out / W.ASSET_DIR / "Ничей-22222222.pdf").write_bytes(b"%PDF-1.4")
    (out / W.ASSET_DIR / "Ничей-22222222.md").write_text("Текст ничей.\n", encoding="utf-8")

    made = W.promote_documents(str(out), apply=True)
    assert made == 2, f"расшифровки не подняты в источники: {made}"

    lifted = (out / "Приказ-11111111.md").read_text(encoding="utf-8")
    assert "trusted: true" in lifted, \
        f"документ не унаследовал доверие страницы, которая на него сослалась:\n{lifted[:300]}"
    assert "url: https://law.example/docs/" in lifted, "провенанс потерян"
    assert "Пункт 1. Ставка налога." in lifted, "текст документа не перенесён"
    assert "document: _files/Приказ-11111111.pdf" in lifted, "не сказано, с какого файла снят текст"

    orphan = (out / "Ничей-22222222.md").read_text(encoding="utf-8")
    assert "trusted: false" in orphan, \
        "документ без страницы-владельца объявлен доверенным — доверие взялось ниоткуда"

    # повторный подъём ничего не меняет: прогон маршрута идёт по многу раз
    assert W.promote_documents(str(out), apply=True) == 0, "подъём не идемпотентен"

    # имя документа берётся из блока страницы, а не из кнопки «Скачать»
    from bs4 import BeautifulSoup                                   # noqa: PLC0415
    soup = BeautifulSoup(
        '<div><div>ПРИКАЗ №38 О пилотном проекте'
        '<div>Дата публикации: 21 Мая 2026<a href="/x/y.pdf">Скачать (PDF, 354.88 КБ)</a>'
        "</div></div></div>", "html.parser")
    label = W.link_label(soup.find("a"))
    assert "ПРИКАЗ" in label and "Скачать" not in label, \
        f"именем документа стала подпись кнопки: «{label}»"
    assert W.asset_name("https://law.example/x/y.pdf", label).endswith(".pdf")


@test
def test_project_templates_do_not_drift_from_the_trust_model(tmp: Path):
    """Шаблоны, что едут в каждый проект, не возвращают снятую приёмку и мёртвые скрипты.

    Скилл с 1.89.0 говорит, что доверие вычисляется, а AGENTS.md проекта читает агент:
    пока шаблон просил «verified» и «verify-гейт», агент получал правила, которых в
    движке нет. Тест ловит и ссылки на скрипты, которых в ките больше нет (`kb_queue.py`).
    """
    files = [p for pat in ("templates", "scaffold/Prompts", "scaffold/Templates")
             for p in (KIT / pat).rglob("*") if p.is_file()]
    assert len(files) > 10, "шаблоны кита не найдены"
    old = re.compile(r"kb_queue|kb:queue|kb:verify|verify-гейт|verified-карточ|верифицированн|"
                     r"^status:\s*(verified|imported)|^verified:|^review_by:", re.M)
    scripts = {p.name for p in (KIT / "scripts").rglob("*.py")} | {"aurora.py"}
    bad = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        bad += [f"{p.relative_to(KIT)}: «{m.group(0).strip()}»" for m in old.finditer(text)]
        bad += [f"{p.relative_to(KIT)}: нет скрипта {n}"
                for n in sorted(set(re.findall(r"\b([a-z][a-z0-9_]+\.py)\b", text)) - scripts)]
    assert not bad, "шаблоны расходятся с моделью доверия:\n" + "\n".join(bad)


@test
def test_an_unchanged_web_page_is_not_downloaded_again(tmp: Path):
    """Страница, которую сервер назвал неизменной (304), не качается и не переписывается.

    Веб-зеркало заново скачивало и разбирало каждую страницу на каждом прогоне, тогда как
    зеркала Confluence и Jira уже пропускали неизменное. Спрашиваем сервер по ETag; без
    ответа 304, при смене настройки страницы или с `--force` — полный проход.
    """
    import argparse
    import contextlib
    import http.server
    import io
    import threading
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")

    state = {"body": "<html><title>Раздел</title><body><main><p>Первый текст.</p></main></body></html>",
             "etag": '"v1"', "full": 0, "cond": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("If-None-Match") == state["etag"]:
                state["cond"] += 1
                self.send_response(304)
                self.end_headers()
                return
            state["full"] += 1
            data = state["body"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("ETag", state["etag"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/razdel/"
    cwd = os.getcwd()
    os.chdir(tmp)
    saved_attrs = {k: getattr(W, k) for k in ("config_text", "PAUSE")}
    trusted = {"v": "true"}
    W.config_text = lambda: f"web:\n  pages:\n    - url: {url}\n      trusted: {trusted['v']}\n"
    W.PAUSE = 0
    out = tmp / "Sources" / "Web"

    def run_once(force=False) -> None:
        args = argparse.Namespace(out=str(out), apply=True, prune=False, verify=False, force=force)
        with contextlib.redirect_stdout(io.StringIO()):
            W.run(args)

    def page() -> Path:
        return next(p for p in out.glob("*.md") if p.name not in ("update_log.md", "sync_state.md"))

    try:
        run_once()
        assert state["full"] == 1 and (tmp / W.PAGE_CACHE).is_file(), "первый прогон не скачал или не запомнил"
        first = page().read_text(encoding="utf-8")
        mtime = page().stat().st_mtime_ns
        run_once()
        assert state["full"] == 1 and state["cond"] == 1, \
            f"неизменная страница скачана заново: полных {state['full']}, условных {state['cond']}"
        assert page().stat().st_mtime_ns == mtime and page().read_text(encoding="utf-8") == first, \
            "неизменная страница переписана"

        run_once(force=True)
        assert state["full"] == 2, "--force не скачал страницу заново"

        trusted["v"] = "false"
        run_once()
        assert state["full"] == 3 and "trusted: false" in page().read_text(encoding="utf-8"), \
            "смена доверия не дошла до файла: страница взята из кэша"

        state["body"] = state["body"].replace("Первый", "Второй")
        state["etag"] = '"v2"'
        run_once()
        assert "Второй текст." in page().read_text(encoding="utf-8"), "изменённая страница не обновилась"
    finally:
        os.chdir(cwd)
        srv.shutdown()
        for k, v in saved_attrs.items():
            setattr(W, k, v)


@test
def test_a_mirrored_page_is_not_rewritten_back_and_forth(tmp: Path):
    """Страница с вложениями за прогон не меняется туда и обратно.

    Страницу записывали дважды: сначала со ссылками на сайт, потом, после вложений, с
    локальными. На странице, что уже лежала на диске, это значило два изменения файла за
    прогон (дифф и время файла) ради промежуточного текста. Новая страница по-прежнему
    пишется сразу, до вложений, — на этом держится прерванный прогон.
    """
    import argparse
    import contextlib
    import io
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")

    url = "https://site.example/docs/"
    asset = "https://site.example/files/prikaz.pdf"
    state = {"body": f"Текст.\n\n[Приказ]({asset})"}
    writes: list = []
    real_open = open

    def counting_open(path, mode="r", *args, **kw):
        if "w" in mode and str(path).endswith(".md") and "Sources" in str(path):
            writes.append(str(path))
        return real_open(path, mode, *args, **kw)

    saved_attrs = {k: getattr(W, k) for k in ("config_text", "fetch", "to_markdown",
                                              "fetch_asset", "PAUSE")}
    W.config_text = lambda: f"web:\n  pages:\n    - url: {url}\n      trusted: true\n"
    W.fetch = lambda u, token, known=None, meta=None: ("<html></html>", "")
    W.to_markdown = lambda html, u: ("Документы", state["body"], [], [(asset, "Приказ")])
    W.PAUSE = 0
    W.open = counting_open
    fail_assets = {"on": False}

    def fake_asset(src, out_dir, apply, label=""):
        if fail_assets["on"]:
            return "", "сайт не отвечает"
        return "Приказ-11111111.pdf", ""
    W.fetch_asset = fake_asset

    out = tmp / "Sources" / "Web"
    args = argparse.Namespace(out=str(out), apply=True, prune=False, verify=False)

    def run_once() -> int:
        writes.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            W.run(args)
        page = [p for p in writes if os.path.basename(p) != "update_log.md"]
        return len(page)

    try:
        first = run_once()
        pages = [p for p in out.glob("*.md") if p.name not in ("update_log.md", "sync_state.md")]
        assert len(pages) == 1, f"страница не записана: {list(out.iterdir())}"
        text = pages[0].read_text(encoding="utf-8")
        assert "_files/Приказ-11111111.pdf" in text, f"ссылка не стала локальной:\n{text}"
        assert first == 2, f"новая страница пишется сразу и после вложений: {first}"

        assert run_once() == 0, "страница с вложениями переписывается на каждом прогоне"
        assert pages[0].read_text(encoding="utf-8") == text, "текст страницы изменился"

        # страница изменилась на сайте, а вложения не пришли: обновление не теряется
        state["body"] = f"Новый текст.\n\n[Приказ]({asset})"
        fail_assets["on"] = True
        run_once()
        assert "Новый текст." in pages[0].read_text(encoding="utf-8"), \
            "правка страницы потеряна из-за недоступных вложений"
    finally:
        for k, v in saved_attrs.items():
            setattr(W, k, v)
        if hasattr(W, "open"):
            del W.open


@test
def test_a_document_is_trusted_wherever_the_project_keeps_it(tmp: Path):
    """Документ доверен по своей природе, а не по тому, лежит ли он в `Raw/`.

    Закон, госконтракт, техническое задание, справочник — источники, подтверждать которые
    нечем и незачем: они и есть подтверждение. Правило знало ровно один путь — `Raw/`, —
    а ключ конфига `trusted_sources`, которым проект объявляет остальные, не читал никто:
    его писала настройка проекта и правила панель, и на этом всё. На живой базе из-за
    этого 224 карточки, собранные из зеркала документов, остались черновиками.

    Совпадение — по границе пути: объявленный `Sources/Docs` не делает доверенным
    соседний `Sources/Docs-2`. Вики этим правилом не доверяется (решение 25.09):
    страница — справочник по названию ветки или задача, а не папка в конфиге.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    T = importlib.import_module("kb_trust")
    empty = {"direct": {}, "indirect": {}}

    cls, why = T.source_class("Raw/contract/ГК.md", empty, {}, set(), set())
    assert cls == "raw", f"первоисточник в Raw/ перестал быть доверенным: {cls} — {why}"

    cls, _ = T.source_class("Sources/Confluence/Стр.md", empty, {}, set(), set())
    assert cls == "unknown", "необъявленный источник стал доверенным сам собой"

    cls, why = T.source_class("Sources/Docs/Акт.md", empty, {}, set(), set(), ("Sources/Docs",))
    assert cls == "raw", f"объявленный доверенным источник не признан: {cls} — {why}"
    assert "конфиге" in why, f"основание не называет, откуда взято доверие: {why}"

    cls, _ = T.source_class("Sources/Docs-2/Акт.md", empty, {}, set(), set(), ("Sources/Docs",))
    assert cls == "unknown", "доверие протекло на соседний путь с тем же началом"

    # папка вики в `trusted_sources` доверия не даёт (решение 25.09) — только справочник
    # по названию ветки или связанная задача
    cls, _ = T.source_class("Sources/Confluence/Проект/Стр.md", empty, {}, set(), set(),
                            ("Sources/Confluence/Проект",))
    assert cls == "unknown", "папка вики в конфиге снова дала доверие"
    cls, _ = T.source_class("Sources/Confluence/Справочники/Таблица.md", empty, {}, set(), set())
    assert cls == "raw", "справочная ветка вики перестала доверяться по природе"


@test
def test_run_journals_do_not_count_as_links(tmp: Path):
    """Журнал прогона, упомянувший карточку, связью не является.

    Счёт входящих ссылок обходил базу вместе со служебными файлами — единственное место
    в движке, где `meta/` не отсекался. Журналы разбора живут неделю и уезжают по сроку
    хранения, а карточка остаётся; на живом проекте четыре брошенные карточки не попали
    в карту брошенных именно потому, что «на них ссылался» протокол разбора.

    Оглавления при этом связью остаются: `_index.md` — навигация базы, а не служебная
    запись движка, и присутствие в оглавлении для веса карточки честно.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")

    root = make_project(tmp)
    card(root, "Concepts/Одинокая.md", status="draft", body="Знание о предмете.")
    card(root, "Concepts/Соседка.md", status="draft", body="Про [[Одинокая]] здесь сказано.")
    runs = root / "AuroraKnowledgeDB" / "meta" / "agent-runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-09-09_2300_distill.md").write_text(
        "# Прогон\n\n- переписана [[Одинокая]]\n", encoding="utf-8")
    (root / "AuroraKnowledgeDB" / "Concepts" / "_index.md").write_text(
        "---\ntitle: \"_index\"\n---\n\n- [[Одинокая]]\n", encoding="utf-8")

    kb = str(root / "AuroraKnowledgeDB")
    only_cards = AC.inbound_counts(kb, skip_nav=True)
    assert only_cards.get("Одинокая") == 1, \
        f"журнал прогона засчитан как связь: {only_cards.get('Одинокая')} вместо 1"

    with_nav = AC.inbound_counts(kb)
    assert with_nav.get("Одинокая") == 2, \
        f"оглавление перестало считаться связью: {with_nav.get('Одинокая')} вместо 2"


@test
def test_idle_run_leaves_a_line_not_a_journal(tmp: Path):
    """Прогон, ничего не изменивший, не заводит отдельный журнал.

    Маршрут гоняет одни и те же задачи оборот за оборотом, и каждый холостой оборот писал
    полноценный файл «переписано: 0 · осталось: 0». На живом проекте таких файлов набралось
    123 из 381 — половина базы стала протоколом собственных прогонов.

    Журналы к тому же не копятся бесконечно: точка отката, которой больше нескольких
    десятков прогонов, бесполезна — поверх давно легли другие коммиты.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AR = importlib.import_module("agent_runner")

    root = make_project(tmp)
    runs = root / "AuroraKnowledgeDB" / "meta" / "agent-runs"
    runs.mkdir(parents=True, exist_ok=True)

    AR.note_empty_run(runs, "2026-09-09_2320", "distill")
    AR.note_empty_run(runs, "2026-09-09_2325", "twins")
    assert not list(runs.glob("*_distill.md")), "холостой прогон завёл отдельный журнал"
    text = (runs / AR.EMPTY_LOG).read_text(encoding="utf-8")
    assert "2026-09-09 23:20 · distill" in text, f"время не читается: {text}"
    marks = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(marks) == 2, f"отмечены не оба холостых прогона: {marks}"
    assert marks[1].endswith("twins — работы не нашлось"), marks[1]

    for i in range(AR.KEEP_RUNS + 5):
        (runs / f"2026-09-{i % 28 + 1:02d}_{i:02d}00_distill.md").write_text("x", encoding="utf-8")
    (runs / "2026-09-01_0100_twins.md").write_text("x", encoding="utf-8")
    dropped = AR.prune_runs(runs, "distill")
    assert dropped == 5, f"удалено {dropped}, ждали 5"
    assert len(list(runs.glob("*_distill.md"))) == AR.KEEP_RUNS
    assert list(runs.glob("*_twins.md")), "чистка задела журналы другой задачи"


@test
def test_verdict_kept_apart_lives_in_the_cards(tmp: Path):
    """«Это разные сущности» записывается в карточки, а не только в журнал прогона.

    Похожесть по тексту никуда не девается: группа выпадала в очередь двойников каждый
    оборот маршрута и каждый раз стоила вызова модели, а человек, открывший карточку, не
    видел, что вопрос уже разбирали. Решение о сущности принадлежит сущности.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AR = importlib.import_module("agent_runner")

    root = make_project(tmp)
    card(root, "Concepts/Признаки-ФЛ.md", status="knowledge", body="Признаки постановки ФЛ.")
    card(root, "Concepts/Признаки-ЮЛ.md", status="knowledge", body="Признаки постановки ЮЛ.")
    group = ["Признаки-ФЛ", "Признаки-ЮЛ"]

    assert not AR.decided_apart(str(root), group), "вердикт найден там, где его не писали"
    assert AR.mark_apart(str(root), group, "Один про ФЛ, другой про ЮЛ.") == 2
    assert AR.decided_apart(str(root), group), "записанный вердикт не читается обратно"
    assert AR.mark_apart(str(root), group, "ещё раз") == 0, "запись не идемпотентна"

    text = (root / "AuroraKnowledgeDB" / "Concepts" / "Признаки-ФЛ.md").read_text(encoding="utf-8")
    assert "[[Признаки-ЮЛ]]" in text and "Один про ФЛ" in text, text
    assert "Признаки постановки ФЛ." in text, "тело карточки пострадало"


@test
def test_a_card_without_a_body_never_reaches_the_model(tmp: Path):
    """Пустая карточка не попадает в очередь тезисов, а вердикт «знания нет» записывается.

    На живом прогоне ОДНА карточка в 187 байт — только шапка, тела нет — держала весь
    маршрут «Обновить базу» двенадцать оборотов при пустой очереди источников: каждый
    оборот её отдавали модели, получали «знания нет» и не записывали ответ.

    Отметка отдельная, не `distilled`: тезиса у карточки нет, а по `distilled` идут
    связывание, вынос определений и замер качества поиска. Разбор снимает её вместе с
    `distilled`, когда переносит новый текст источника.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")

    root = make_project(tmp)
    empty = root / "AuroraKnowledgeDB" / "Concepts" / "Пустая.md"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text('---\ntitle: "Пустая"\nstatus: draft\nkind: knowledge\n---\n',
                     encoding="utf-8")
    card(root, "Concepts/Живая.md", status="draft", kind="knowledge",
         body="НДС считается по ставке 20 процентов от налоговой базы.")

    def queue() -> list:
        out = []
        for p in AC.walk_md(str(root / "AuroraKnowledgeDB"), skip_service=True,
                            skip_archive=True):
            text = open(p, encoding="utf-8", errors="ignore").read()
            fm = AC.frontmatter(text)
            if (fm.get("kind") or "").strip().strip('"') != "knowledge":
                continue
            if (fm.get("distilled") or "").strip() or (fm.get("distill_empty") or "").strip():
                continue
            if not AC.card_body(text).strip():
                continue
            out.append(os.path.basename(p)[:-3])
        return sorted(out)

    assert queue() == ["Живая"], f"пустая карточка пошла к модели: {queue()}"

    live = root / "AuroraKnowledgeDB" / "Concepts" / "Живая.md"
    was = live.read_text(encoding="utf-8")
    live.write_text(AC.with_fields(was, {"distill_empty": "2026-09-09"}), encoding="utf-8")
    assert "НДС считается" in live.read_text(encoding="utf-8"), \
        "запись вердикта обнулила карточку"
    assert queue() == [], f"после вердикта карточку спросят снова: {queue()}"

    text = live.read_text(encoding="utf-8")
    live.write_text(re.sub(r"^distill_empty:.*$\n?", "", text, flags=re.M), encoding="utf-8")
    assert queue() == ["Живая"], "после нового текста источника вопрос не задаётся заново"


@test
def test_one_translation_per_name_is_remembered(tmp: Path):
    """Транслит в имени переводится один раз, и перевод переиспользуется.

    Источники приходят с разными именами страниц: часть по-русски, часть транслитом.
    Карточка наследует имя источника, и понятие расщепляется — в других карточках оно
    названо кириллицей, ссылка до транслитерованной карточки не доходит, и под неё
    заводится пустышка. Одно понятие, две карточки, ни одной связи.

    Механически транслит не развернуть, поэтому перевод делается один раз и живёт в
    словаре. Второй разбор того же понятия обязан получить то же имя.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    TR = importlib.import_module("kb_translit")

    assert TR.is_latin_name("Statusy-profilya-abonenta"), "транслит не опознан"
    assert TR.is_latin_name("us-3.6.4-storno-nachisleniy"), "транслит в нижнем регистре"
    assert not TR.is_latin_name("Статусы-профиля"), "кириллица принята за транслит"
    assert not TR.is_latin_name("ALG-105"), "код принят за транслит"
    assert not TR.is_latin_name("SPR-001"), "код справочника принят за транслит"

    root = make_project(tmp)
    card(root, "Concepts/Profil-abonenta.md", status="draft",
         body="Профиль обслуживания абонента задаёт перечень доступных услуг.")
    card(root, "Concepts/Профиль-договора.md", status="draft", body="Русское имя, не транслит.")

    cp = run("kb_translit.py", "--apply", cwd=root)
    assert "Profil-abonenta" in cp.stdout, cp.stdout
    assert "Профиль-договора" not in cp.stdout, "русское имя попало в словарь транслита"
    d = root / "AuroraKnowledgeDB" / "meta" / "translit.md"
    assert d.is_file(), "словарь не создан"
    rows = TR.read_dict(str(d))
    assert rows.get("Profil-abonenta") == "", \
        f"перевод придуман машиной вместо человека: {rows}"

    # человек вписал перевод — он переиспользуется, старое имя уходит в синонимы
    d.write_text(d.read_text(encoding="utf-8").replace(
        "| Profil-abonenta |  |", "| Profil-abonenta | Профиль абонента |"), encoding="utf-8")
    assert TR.read_dict(str(d))["Profil-abonenta"] == "Профиль абонента"
    run("kb_translit.py", "--rename", "--apply", "--allow-dirty", cwd=root)
    new = root / "AuroraKnowledgeDB/Concepts/Профиль-абонента.md"
    assert new.is_file(), "карточка не переименована по словарю"
    assert "Profil-abonenta" in new.read_text(encoding="utf-8"), \
        "старое написание не ушло в синонимы — ссылки на него сломаются"


@test
def test_a_failure_says_what_broke_not_the_tail_of_a_traceback(_t):
    """Сбой называет ошибку, а не показывает хвост трассировки.

    В отчёт уходили последние сто шестьдесят символов вывода — и на двух живых
    пересборках это оказалась строка кареток: «отметка не поставлена: ~~~~^^^^^^^^».
    По такому сообщению нельзя ни понять причину, ни воспроизвести; я дважды искал её
    вслепую. Причина должна идти первой строкой, а не тонуть в трассировке.
    """
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    block = src[src.index("def run_build_plan("):src.index("# ----------", src.index("def run_build_plan("))]
    assert '"why": why' in block, "сбой не называет ошибку отдельным полем"
    assert 'why = f"build_plan: {type(ex).__name__}: {ex}"' in block, \
        "тип и текст ошибки не собираются в одну строку"
    assert 'why + "\\n" +' in block, \
        "причина не первой строкой — её вытеснит трассировка, напечатанная раньше"
    assert "[-160:]" not in src and "[-200:]" not in src, \
        "отчёт по-прежнему берёт хвост вывода: в журнале останется «~~~~^^^^»"


@test
def test_a_broken_manifest_stops_the_run_instead_of_forgetting(tmp: Path):
    """Нечитаемый манифест — отказ, а не «начнём с нуля».

    Манифест это учёт того, что уже разобрано. Прежде любая ошибка чтения возвращала
    пустой словарь, и следующая же запись затирала учёт целиком: на живой пересборке
    сто тридцать один разобранный источник разом стал неразобранным, план вырос вдвое,
    и разбор пошёл по второму кругу. Движок это заметил и записал в сообщение коммита,
    но не остановился — а должен был.

    Пустой файл и отсутствие файла — законное «ещё ничего не разбирали»; битый — авария.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    B = importlib.import_module("build_plan")
    importlib.reload(B)

    root = make_project(tmp)
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    here = os.getcwd()
    try:
        os.chdir(root)
        assert B.load_manifest() == {}, "нет файла — это не авария"
        (meta / "manifest.json").write_text("", encoding="utf-8")
        assert B.load_manifest() == {}, "пустой файл — это не авария"

        # Запись должна быть неделимой: читатель не увидит половину файла
        B.save_manifest({"sources": {"A.md": {"cards": 3}}})
        assert not list(meta.glob(".manifest-*")), "временный файл не убран"
        assert not (meta / "manifest.json.tmp").exists(), \
            "временный файл с общим именем: два писателя затрут друг друга"
        assert B.load_manifest()["sources"]["A.md"]["cards"] == 3

        # Запись из нескольких потоков разом не должна портить файл. Общий временный файл
        # давал ровно это: один усекает, другой пишет, и после переименования за
        # закрывающей скобкой оставался обрывок чужого содержимого — манифест становился
        # нечитаемым при неделимой, казалось бы, записи.
        import concurrent.futures as _cf
        def writer(n):
            B.save_manifest({"sources": {f"S{i}.md": {"cards": i} for i in range(n)}})
        with _cf.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(writer, [400, 3, 250, 5, 300, 7, 200, 9] * 3))
        got = B.load_manifest()          # не должно бросить
        assert isinstance(got.get("sources"), dict), got
        assert not list(meta.glob(".manifest-*")), "временные файлы копятся рядом с базой"

        (meta / "manifest.json").write_text('{"sources": {"A.md": ', encoding="utf-8")
        try:
            B.load_manifest()
        except B.ManifestBroken as e:
            assert "не читается" in str(e), str(e)
            assert "git checkout" in str(e), "не сказано, как вернуть учёт"
            # Обычное исключение, а не SystemExit: функцию зовут из потоков разбора,
            # и `except Exception` вокруг вызова обязан её поймать.
            assert isinstance(e, Exception), \
                "ошибка чтения манифеста не ловится обычным except — поток умрёт молча"
        else:
            raise AssertionError("битый манифест прочитан как пустой — "
                                 "следующая запись сотрёт весь учёт разбора")
    finally:
        os.chdir(here)


@test
def test_the_plan_does_not_feed_on_its_own_work(tmp: Path):
    """Карточка, нарезанная из справочника, не возвращается в план источником.

    Справочники раздела `Reference` ведут руками — они законный источник. Карточка,
    извлечённая из справочника, ложится рядом с ним, и без отсева план получал бы её
    как новый источник: разобрал источник — получил источник.

    Отсев смотрит на происхождение, и читать его надо тем же кодом, что и все.
    Проверка на голое поле `source:` перестала работать в день, когда происхождение
    стало списком, — и разбор на живой пересборке немедленно принялся скармливать себе
    собственные карточки.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    B = importlib.import_module("build_plan")
    importlib.reload(B)

    root = make_project(tmp)
    ref = root / "AuroraKnowledgeDB" / "Reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "Справочник-кодов.md").write_text(
        '---\ntitle: "Справочник кодов"\nsources: []\n---\n\n| код | имя |\n|---|---|\n',
        encoding="utf-8")
    (ref / "Код-А.md").write_text(
        '---\ntitle: "Код А"\nsources:\n  - "AuroraKnowledgeDB/Reference/Справочник-кодов.md"\n'
        "---\n\nЗначение кода.\n", encoding="utf-8")
    (ref / "Старая-нарезка.md").write_text(
        '---\ntitle: "Старая нарезка"\nsource: "AuroraKnowledgeDB/Reference/Справочник-кодов.md"\n'
        "---\n\nЗначение.\n", encoding="utf-8")

    assert not B.derived_card(str(ref / "Справочник-кодов.md")), \
        "справочник, который ведут руками, объявлен производным — он выпадет из плана"
    assert B.derived_card(str(ref / "Код-А.md")), \
        "карточка со списком источников не опознана как производная — вернётся в план"
    assert B.derived_card(str(ref / "Старая-нарезка.md")), \
        "карточка со старой записью источника не опознана: базы прошлых версий сломаются"


@test
def test_knowledge_accumulates_in_one_card(tmp: Path):
    """Про одну сущность говорят разные документы — знание копится в одной карточке.

    Это главное правило зеттелькастена, на котором стоит схема. До него разбор умел
    только два хода: тот же источник — перечитать, другой — **отказать**. Отказ был
    написан для человека («допишите уточнение»), а читала его модель и делала
    единственное доступное: придумывала другое имя. На живом проекте так появились
    двенадцать карточек об одном процессе.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    B = importlib.import_module("build_plan")
    importlib.reload(B)

    root = make_project(tmp)
    (root / "Sources" / "Confluence").mkdir(parents=True, exist_ok=True)
    long = "Профиль назначается при заключении договора и меняется заявкой абонента. " * 6
    for name, body in (("A.md", "## Про профиль\n\n" + long),
                       ("B.md", "## Ещё про профиль\n\n" + long.replace("заявкой", "письмом"))):
        (root / "Sources/Confluence" / name).write_text(body, encoding="utf-8")
    card(root, "Concepts/Профиль-абонента.md", status="draft", distilled="2026-08-02",
         body="Тезис: набор параметров.\n\n## Источник (перенесено дословно)\n\n"
              "### Sources/Confluence/A.md\n\nПервый текст.")
    path = root / "AuroraKnowledgeDB/Concepts/Профиль-абонента.md"
    # у карточки уже есть источник — как после обычного разбора
    path.write_text(path.read_text(encoding="utf-8").replace(
        "type: concept", 'type: concept\nsources:\n  - "Sources/Confluence/A.md"'),
        encoding="utf-8")

    cp = run("build_plan.py", "--append", "Профиль абонента",
             "--source", "Sources/Confluence/B.md", "--sections", "1", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    got = path.read_text(encoding="utf-8")
    assert card_srcs(got) == ["Sources/Confluence/A.md", "Sources/Confluence/B.md"], \
        f"источники не накопились: {card_srcs(got)}"
    assert "Первый текст." in got, "прежний текст затёрт — знание потеряно при накоплении"
    assert "письмом" in got, "новый текст не дописан"
    assert "### Sources/Confluence/B.md" in got, \
        "блок не подписан источником — разнести обратно будет нечем"
    assert "distilled:" not in got.split("---")[1], \
        "тезис остался помеченным готовым: он написан по прежнему тексту, а знания стало больше"

    # тот же источник дважды не удваивает блок: источник правят и разбирают снова
    run("build_plan.py", "--append", "Профиль абонента", "--source",
        "Sources/Confluence/B.md", "--sections", "1", "--apply", cwd=root)
    twice = path.read_text(encoding="utf-8")
    assert twice.count("### Sources/Confluence/B.md") == 1, \
        "повторный разбор удвоил блок — карточка растёт от собственных прогонов"
    assert len(card_srcs(twice)) == 2, card_srcs(twice)

    # Занятое имя из другого источника разбор не роняет, а дописывает сам. На первом же
    # живом прогоне отказ научился называть `--append`, а исполнить его было некому:
    # источник объявлялся сбойным, и разобранное терялось. По правилам базы имя карточки
    # это имя сущности — совпало имя, значит совпала сущность.
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert 'if not res["ok"] and not into_name and "--append" in' in src, \
        "разбор не дописывает при занятом имени — источник будет объявлен сбойным"
    assert "дописана в существующую" in src, \
        "человеку не сказано, что карточку дописали, а не завели"

    # Опечатка в имени не теряет источник: одна буква в длинном имени — это опечатка,
    # а не другое понятие. На живой пересборке модель поставила латинскую букву в русском
    # слове, и источник пропал целиком при том, что карточка лежала рядом.
    typo = run("build_plan.py", "--append", "Профиль абонeнта",  # латинская e
               "--source", "Sources/Confluence/B.md", "--sections", "1", "--apply", cwd=root)
    assert typo.returncode == 0, typo.stdout + typo.stderr
    assert not (root / "AuroraKnowledgeDB/Concepts/Профиль-абонeнта.md").is_file(), \
        "опечатка завела карточку-двойник вместо дополнения существующей"

    # и отказ при занятом имени теперь называет операцию, а не советует невозможное
    (root / "Sources/Confluence/C.md").write_text("## Раздел\n\n" + long, encoding="utf-8")
    ref = run("build_plan.py", "--card", "Профиль абонента", "--source",
              "Sources/Confluence/C.md", "--sections", "1", "--to", "Concepts", cwd=root)
    assert ref.returncode == 1, ref.stdout
    assert "--append" in ref.stderr, \
        f"отказ не называет операцию — модель придумает другое имя:\n{ref.stderr}"

    # А имени, которого нет вовсе, соответствует новая карточка — но не потеря источника:
    # знание разобрано и годно, лишнюю карточку человек сведёт `kb:twins`. Терять источник
    # хуже: в отчёте будет «сбой», и разбирать его придётся заново.
    missing = run("build_plan.py", "--append", "Неизвестное понятие связи",
                  "--source", "Sources/Confluence/C.md", "--sections", "1",
                  "--to", "Concepts", "--apply", cwd=root)
    assert missing.returncode == 0, \
        f"источник потерян из-за имени, которого нет:\n{missing.stderr}"
    assert (root / "AuroraKnowledgeDB/Concepts/Неизвестное-понятие-связи.md").is_file(), \
        "карточка не заведена — знание пропало"


@test
def test_parsing_is_shown_what_the_base_already_has(tmp: Path):
    """Разбор видит существующие карточки и может дописать знание в них.

    Без этого правило «карточка — сущность» невыполнимо: модель не знает, что карточка
    про этот объект уже есть, и заводит вторую под другим именем. Список кандидатов —
    то, чего разбору не хватало, чтобы правило стало исполнимым.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")

    block = R.candidates_block([("Профиль-абонента", "Concepts", "Набор услуг и тарификация"),
                                ("Журнал-начислений", "Concepts", "История операций")])
    assert "Профиль-абонента" in block and "Набор услуг" in block, block
    assert "into" in block, "модель не узнает, как дописать в существующую"
    for must in ("ТУ ЖЕ САМУЮ сущность", "Сомневаешься — заводи новую"):
        assert must in block, f"нет правила «{must}»: модель начнёт склеивать похожее"
    assert R.candidates_block([]) == "", \
        "пустой список кандидатов всё равно печатается — это собьёт модель"

    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert "candidates_block(candidates_for(cwd, cfg, listing))" in src, \
        "кандидаты не доезжают до промпта разбора"
    # Путь карточки ищется по карте, а не обходом базы на каждого кандидата: два десятка
    # кандидатов на карточку и тысячи карточек дают миллионы обращений к диску за прогон.
    block = src[src.index("def _card_path("):src.index("def candidates_block(")]
    assert "_PATHS.get(stem" in block and "for p in AC.walk_md" not in block.split("if time")[0], \
        "путь карточки ищется обходом всей базы — на большом проекте это минуты на карточку"
    assert 'карточка — это сущность, а не пересказ документа' in src.lower(), \
        "правило не сказано в самом промпте — список кандидатов без него бесполезен"
    # Задача о работе — не знание. Без этого правила пересборка делает карточки вида
    # «Разработка таблицы X — задача PRJ-000. В источнике прямо указано: разработка
    # таблицы X»: пересказ заголовка, который линтер потом честно зовёт артефактом.
    flat = " ".join(R.PROMPT_BUILD.split())
    assert "задача о выполнении работы — не сущность" in flat, \
        "разбор сделает знание из задачи о работе — в базе появятся пересказы заголовков"
    assert "дописываются в карточку предмета" in flat, \
        "сведения о задаче теряются: их код и ссылка нужны для доверия и таблицы связей"
    assert "карточку делай про **предмет**" in flat, \
        "не сказано, что делать, когда предмет в задаче описан"

    # разбор умеет обе записи, и не обе сразу
    assert R.check_cards([{"into": "Профиль-абонента", "sections": "1"}],
                         [(1, "Раздел", 100, "…")]) == "", "запись `into` не принята"
    both = R.check_cards([{"into": "А", "title": "Б", "sections": "1"}],
                         [(1, "Раздел", 100, "…")])
    assert "into" in both and "title" in both, f"смешение двух записей не поймано: {both}"

    # Фильтр между моделью и исполнителем обязан пропускать обе записи. Требовавший
    # `title` молча выбрасывал `into`, и источник, про который модель сказала «это уже
    # есть в карточке N», объявлялся сбойным — на живой пересборке так упала вся партия.
    assert 'if (c.get("title") or c.get("into")) and c.get("sections")' in src, \
        "ответы вида `into` отсеиваются до исполнителя — накопление не сработает ни разу"

    # и операция дополнения разрешена агенту: без белого списка он ею не воспользуется
    A = importlib.import_module("agent_core")
    ok, why = A.write_allowed("build_plan.py", ["--append", "Карточка", "--apply"])
    assert ok, f"агенту запрещено дополнять карточки: {why}"


@test
def test_extraction_moves_text_and_never_loses_it(tmp: Path):
    """Чужое определение переезжает в свою карточку дословно — и не пропадает.

    Знание о ФЦОД, объяснённое попутно внутри карточки про аналитический баланс, лежит
    не там: его ищут по имени системы, а оно внутри чужого тезиса. Перенос — механика:
    движок вырезает присланный кусок и вставляет. Не нашёл дословно — отменяет, потому
    что взять похожее значит переписать знание под видом переезда.

    Инвариант, который здесь и проверяется: **вырезанный текст обязан где-то оказаться.**
    Ход, который удалил бы кусок и никуда не положил, невозможен по построению.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    definition = "которая является частью проекта обработки платежей"
    thesis = ("Аналитический баланс получает информацию из подсистемы ФЦОД, "
              + definition + ". Баланс обновляется по факту поступления платежа и "
              "хранит остатки по каждому лицевому счёту за расчётный период. "
              "Сверка проводится ежедневно.")
    card(root, "Concepts/Аналитический-баланс.md", status="draft", kind="knowledge",
         distilled="2026-09-01", sources='\n  - "Sources/Confluence/Баланс.md"', body=thesis)
    path = root / "AuroraKnowledgeDB/Concepts/Аналитический-баланс.md"

    def fake(cfg, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": json.dumps({"extract": [
                    {"term": "ФЦОД", "definition": definition, "keep": ""}]},
                    ensure_ascii=False)}

    step = R.extract_card({"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
                           "thinking_roles": {}, "thinking": False, "backends": []},
                          str(path), fake, apply=True)
    assert step["status"] == "вынесено", step
    left = path.read_text(encoding="utf-8")
    assert "[[ФЦОД]]" in left, "ссылка не подставлена на место определения"
    assert definition not in left, "определение осталось в исходной карточке — знание удвоено"
    assert "Баланс обновляется по факту" in left, "задет чужой текст"

    made = root / "AuroraKnowledgeDB/Concepts/ФЦОД.md"
    assert made.is_file(), "карточка сущности не заведена — текст пропал бы"
    body = made.read_text(encoding="utf-8")
    assert definition in body, "определение переехало не дословно"
    assert "Аналитический-баланс" in body, "не сказано, откуда перенесено"
    # Происхождение переезжает вместе со знанием: без него карточка не доверялась никогда и
    # переживала снос базы — на живом проекте так завелись 32 карточки.
    assert card_srcs(body) == ["Sources/Confluence/Баланс.md"], \
        f"вынесенная карточка не унаследовала источник донора: {card_srcs(body)}"

    # ход печатает работу по мере её выполнения: молчащий несколько минут шаг
    # неотличим от зависшего, и вести прогон по логам становится нечем
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    block = src[src.index("def run_extract("):src.index("def report_extract(")]
    assert "flush=True" in block and "Карточек к осмотру" in block, \
        "ход выделения молчит до конца — по логам его не проконтролировать"

    # Осмотренную карточку второй раз не смотрим: без отметки каждый оборот пересборки
    # перебирал бы всю базу заново — часы вызовов модели ради уже полученного ответа.
    # Отметка держит дату тезиса: перепишут тезис — карточка вернётся на осмотр сама.
    again = path.read_text(encoding="utf-8")
    assert "extracted: 2026-09-01" in again, \
        f"карточка не помечена осмотренной — её будут осматривать каждый оборот:\n{again[:300]}"
    cfg2 = {"request_timeout": 60, "budget_min": 5, "backends": [],
            "embed": {"model": "m"}, "thinking_roles": {}, "thinking": False}
    res = R.run_extract(cfg2, str(root), apply=False,
                        call=lambda *a, **k: {"ok": True, "backend": 1, "model": "m",
                                              "log": [], "text": '{"extract": []}'})
    looked = [s["card"] for s in res["steps"]]
    assert "Аналитический-баланс" not in looked, \
        f"осмотренная карточка пошла на второй круг: {looked}"

    # «Термин (расшифровка)» уходит целиком: иначе остаются скобки и имя дважды —
    # «введена УСН ([[УСН]])». Ровно это вышло на первом живом прогоне.
    card(root, "Concepts/Ставка.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body="Для оборота до тридцати миллионов введена УСН (упрощённая система "
              "налогообложения). Ставка применяется с начала календарного года и не "
              "меняется до его конца. Переход оформляется заявлением в инспекцию.")
    def paren(cfg, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
            {"extract": [{"term": "УСН", "definition": "упрощённая система налогообложения",
                          "keep": ""}]}, ensure_ascii=False)}
    sp = root / "AuroraKnowledgeDB/Concepts/Ставка.md"
    R.extract_card({"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
                    "thinking_roles": {}, "thinking": False, "backends": []},
                   str(sp), paren, apply=True)
    after = sp.read_text(encoding="utf-8")
    assert "введена [[УСН]]" in after, f"скобки остались от конструкции:\n{after}"
    assert "УСН ([[УСН]])" not in after, "имя выведено дважды, скобка пустая"

    # пересказанное определение не переносится вовсе: это правка, а не переезд
    card(root, "Concepts/Другая.md", status="draft", kind="knowledge",
         distilled="2026-09-01",
         body="Реестр платежей ведёт подсистема УФК, отвечающая за казначейские операции. "
              "Реестр закрывается на конец расчётного периода и передаётся в архив на "
              "долговременное хранение. Хранение — три года с даты закрытия периода. "
              "Записи реестра не правятся: исправление вносится сторнирующей записью со "
              "ссылкой на исходную операцию и датой внесения.")
    def paraphrase(cfg, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": json.dumps({"extract": [
                    {"term": "УФК", "definition": "которая отвечает за казначейские операции",
                     "keep": ""}]}, ensure_ascii=False)}
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    other = root / "AuroraKnowledgeDB/Concepts/Другая.md"
    before = AC.card_body(other.read_text(encoding="utf-8"))
    st2 = R.extract_card({"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
                          "thinking_roles": {}, "thinking": False, "backends": []},
                         str(other), paraphrase, apply=True)
    assert st2["status"] == "нечего выносить", st2
    # Сверяем ТЕЛО: шапка меняется законно — карточку пометили осмотренной.
    assert AC.card_body(other.read_text(encoding="utf-8")) == before, \
        "карточка изменена по пересказанному куску — знание переписано под видом переноса"
    assert "не найдено дословно" in st2["note"], st2


@test
def test_merging_twins_is_decided_by_the_model(tmp: Path):
    """«Сливать или нет» решает модель по тексту, а не человек.

    Пересборка базы с нуля не должна упираться в этот вопрос: на живом проекте групп
    двойников оказалось 493, и каждая означала бы остановку прогона. Судить о том, одна
    это сущность или разные, можно по тексту — значит, это работа модели.

    Но не любой ценой: слитое по ошибке разъединять придётся вручную, восстанавливая, что
    откуда. Поэтому модель обязана уметь сказать «разные», и её «разные» должно
    исполняться так же строго, как «одна».
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")

    # Промпт свёрстан по ширине: искать по сырому тексту значит ловить перенос строки,
    # а не отсутствие правила.
    flat = " ".join(R.PROMPT_TWINS.split())
    for must in ("одна это сущность или разные", "Сомневаешься — НЕ сливай",
                 "имя точнее называет сущность"):
        assert must in flat, f"в промпте нет правила «{must}»"

    root = make_project(tmp)
    # Текст нарочно длинный: мера сравнивает куски по восемь слов, и на коротком теле
    # их набирается слишком мало, чтобы говорить о совпадении.
    same = ("Профиль обслуживания абонента задаёт перечень доступных услуг и порядок их "
            "тарификации в биллинге. Профиль назначается при заключении договора и "
            "меняется заявкой абонента через личный кабинет. Смена профиля вступает в "
            "силу с первого числа следующего расчётного периода, а начисления за текущий "
            "период считаются по прежнему профилю. ")
    card(root, "Concepts/Профиль-абонента.md", status="knowledge", kind="knowledge",
         body=same * 3)
    card(root, "Concepts/Что-такое-профиль.md", status="draft", kind="knowledge",
         body=same * 3)
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}

    def says_merge(c, role, messages, **kw):
        assert "Профиль-абонента" in messages[0]["content"], "модели не показали карточки"
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
            {"merge": True, "keep": "Профиль-абонента", "why": "одно понятие"},
            ensure_ascii=False)}

    step = R.solve_twins(cfg, str(root), ["Профиль-абонента", "Что-такое-профиль"],
                         apply=True, call=says_merge)
    assert step["status"] == "слито", step
    assert step["keep"] == "Профиль-абонента", step
    kept = (root / "AuroraKnowledgeDB/Concepts/Профиль-абонента.md")
    assert kept.is_file(), "победитель исчез"
    assert not (root / "AuroraKnowledgeDB/Concepts/Что-такое-профиль.md").is_file() \
        or "deprecated" in (root / "AuroraKnowledgeDB/Concepts/Что-такое-профиль.md"
                            ).read_text(encoding="utf-8"), \
        "проигравшая осталась живой карточкой — знание по-прежнему в двух местах"

    # «Разные» — тоже решение, и оно записывается в сами карточки. Тело при этом не
    # трогают: слияния не было, знание остаётся там, где лежало. Раньше вердикт жил
    # только в журнале прогона, журнал удалялся по сроку хранения, и следующий оборот
    # маршрута спрашивал модель о той же паре заново — а человек, открывший карточку,
    # не видел, что вопрос уже разбирали.
    card(root, "Concepts/Смена-профиля.md", status="draft", kind="knowledge", body=same * 3)
    def says_no(c, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
            {"merge": False, "why": "объект и действие над ним"}, ensure_ascii=False)}
    moved = root / "AuroraKnowledgeDB/Concepts/Смена-профиля.md"
    st = R.solve_twins(cfg, str(root), ["Профиль-абонента", "Смена-профиля"],
                       apply=True, call=says_no)
    assert st["status"] == "оставлено" and "действие" in st["why"], st
    after = moved.read_text(encoding="utf-8")
    assert same.strip()[:40] in after, "тело карточки пострадало от решения «разные»"
    assert "[[Профиль-абонента]]" in after and "действие" in after, \
        f"вердикт «разные сущности» не записан в карточку:\n{after[:400]}"
    assert R.decided_apart(str(root), ["Профиль-абонента", "Смена-профиля"]), \
        "записанный вердикт не читается обратно — пара вернётся в очередь"
    assert st.get("apart") == 2, f"не сказано, скольким карточкам записан вердикт: {st}"

    # Группы читаются из отчёта `kb:twins`: разбор чужого вывода молча вернул бы пустой
    # список, и весь ход стал бы бездействием, неотличимым от «двойников нет».
    #
    # И отчёт обязан печатать ВСЕ группы, когда его читает машина. На живом проекте ход
    # обработал сорок групп из пятисот и отрапортовал как о завершённой работе: отчёт
    # печатает сорок по умолчанию, а `twin_groups` читает именно печатное.
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert '"--limit", "0"' in src, \
        "ход читает отчёт с урезанной печатью — возьмёт сорок групп из пятисот и умолкнет"
    twins = (SCRIPTS / "kb_twins.py").read_text(encoding="utf-8")
    assert "groups if not a.limit else" in twins, \
        "ноль в --limit не значит «все» — ход прочитает пустой отчёт"

    # Пара, которую уже развели вердиктом, в очередь не возвращается — это проверено
    # выше. Значит для проверки РАЗБОРА отчёта нужна пара, о которой ещё не судили:
    # иначе тест ловил бы не разбор, а фильтр, и падал бы на верной работе движка.
    card(root, "Concepts/Профиль-тарифный.md", status="draft", kind="knowledge", body=same * 3)
    card(root, "Concepts/Профиль-услуг.md", status="draft", kind="knowledge", body=same * 3)

    groups = R.twin_groups(str(root))
    assert groups, "группы не прочитаны из отчёта — ход будет молча ничего не делать"
    assert any("Профиль-тарифный" in g for g in groups), \
        f"нерешённая пара не попала в очередь: {groups}"
    # Разведённая пара не возвращается САМА ПО СЕБЕ. Внутри большей группы, где о
    # соседях ещё не судили, она законно появляется снова: вопрос «Профиль-тарифный —
    # то же, что Профиль-абонента?» вердиктом о другой паре не закрыт.
    assert not any(set(g) == {"Профиль-абонента", "Смена-профиля"} for g in groups), \
        f"разведённая вердиктом пара вернулась в очередь отдельной группой: {groups}"
    assert all(len(g) > 1 for g in groups), f"группа из одной карточки — не группа: {groups}"

    # названная не из группы — сбой, а не «сольём что-нибудь»
    def says_alien(c, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
            {"merge": True, "keep": "Чужая-карточка", "why": "…"}, ensure_ascii=False)}
    bad = R.solve_twins(cfg, str(root), ["Профиль-абонента", "Смена-профиля"],
                        apply=True, call=says_alien)
    assert bad["status"] == "сбой" and "не из группы" in bad["why"], bad


@test
def test_no_script_shadows_what_it_imports(_t):
    """Скрипт не определяет функцию с именем того, что сам импортирует из движка.

    За одну сессию это выстрелило трижды. `kb_fix` имел свою `is_placeholder` про
    шаблонные ссылки — импорт молча её перекрыл, и ремонт упал на живом прогоне.
    `build_plan` имел свою `card_sources` без аргументов — разбор упал `TypeError` уже
    после того, как половина карточек была записана. Python не предупреждает: последнее
    определение побеждает, и падает оно не там, где ошиблись.

    Инвариант, а не совет: имя из `aurora_common` в скрипте движка значит ровно то, что
    в `aurora_common`. Нужно другое — назовите иначе.
    """
    import ast as _ast
    bad = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name == "aurora_common.py":
            continue
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and (node.module or "") == "aurora_common":
                for al in node.names:
                    imported.add(al.asname or al.name)
        if not imported:
            continue
        for node in tree.body:            # только верхний уровень: методы классов свои
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                    and node.name in imported:
                bad.append(f"{path.name}:{node.lineno} — «{node.name}» перекрывает импорт "
                           "из aurora_common")
    assert not bad, ("имя занято дважды, и побеждает последнее определение:\n  "
                     + "\n  ".join(bad))


@test
def test_the_model_is_told_what_the_abbreviations_mean(tmp: Path):
    """Разбору источника подаются расшифровки проекта и запрет придумывать остальные.

    Модель, разбирающая источник, видит незнакомое сокращение и **придумывает**
    правдоподобную расшифровку — не по злому умыслу, а потому что промпт требует
    определения, а сказать «не знаю» ей никто не разрешал. На живом проекте так родились
    два выдуманных значения одной аббревиатуры, разошедшиеся по восьми карточкам и
    читаемые как факт: на вид ошибка неотличима от знания.

    Лечится двумя вещами сразу, и обе обязаны быть: **дать** то, что база уже знает, и
    **явно разрешить не знать** остальное. Одного списка мало — сокращений всегда больше,
    чем записано; одного запрета мало — тогда модель оставит как есть и то, что в базе
    расшифровано.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    card(root, "Reference/Сокращения-проекта.md", type="reference", status="knowledge",
         body="| Сокращение | Значение |\n|---|---|\n"
              "| ПРФ | профиль обслуживания абонента |\n"
              "| ЖНЧ | журнал начислений по счёту |\n")
    card(root, "Glossary/ТРФ.md", type="glossary", status="draft",
         body="**ТРФ** — тариф: цена обращения к услуге.\n")
    # заготовка расшифровкой не является: подать её значит научить модель, что
    # «ЗГТ — заготовка, знания пока нет»
    card(root, "Glossary/ЗГТ.md", type="glossary", status="draft",
         body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n")

    terms = AC.project_terms(str(root / "AuroraKnowledgeDB"))
    assert terms.get("ПРФ") == "профиль обслуживания абонента", terms
    assert terms.get("ЖНЧ") == "журнал начислений по счёту", terms
    assert terms.get("ТРФ", "").startswith("тариф"), \
        f"повтор имени не убран из расшифровки: {terms.get('ТРФ')!r}"
    assert "ЗГТ" not in terms, "заготовка попала в словарь как определение"

    text = "Абонент меняет ПРФ, начисление уходит в ЖНЧ."
    block = AC.terms_block(text, terms)
    assert "ПРФ — профиль обслуживания абонента" in block, block
    assert "ЖНЧ" in block and "ТРФ" not in block, \
        "в промпт ушли расшифровки, которых в тексте нет — модель пристегнёт их к чужому"
    for must in ("не расшифровывай", "оставь ровно так"):
        assert must in block.lower(), f"нет запрета придумывать: {block}"

    # запрет печатается и тогда, когда база не знает ни одного сокращения: он и есть
    # главная часть, а список — вспомогательная
    empty = AC.terms_block(text, {})
    assert "не расшифровывай" in empty.lower() and "не выдумывай" in empty.lower(), empty

    # и всё это действительно доезжает до промптов разбора, а не лежит рядом
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    for prompt in ("PROMPT_BUILD", "PROMPT_DISTILL", "PROMPT_DISTILL_PART",
                   "PROMPT_REDISTILL", "PROMPT_NO_SECTIONS"):
        assert re.search(r"with_terms\(\s*\n?\s*" + prompt, src), \
            f"{prompt} уходит модели без словаря проекта"
    got = R.with_terms("<промпт>", text, str(root))
    assert "ПРФ — профиль" in got and got.rstrip().endswith("<промпт>"), got[:400]
