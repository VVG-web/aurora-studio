"""Проверки движка Aurora, часть 11 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import textwrap

from harness import (  # noqa: F401
    ui_source,
    KIT,
    SCRIPTS,
    _mirror_page,
    _quoted_card,
    _sync_state,
    card,
    card_srcs,
    make_project,
    panel_sources,
    run,
    test,
    why,
)


@test
def test_an_invented_expansion_is_caught_by_the_linter(tmp: Path):
    """Расшифровка вопреки словарю — ошибка, а обычная русская фраза перед скобкой — нет.

    Промпт запрещает выдумывать, но промпт это просьба, а не гарантия. Ошибку ищет
    машина: на вид выдуманная расшифровка неотличима от настоящей, и читатель её не
    поймает — он и читает базу затем, чтобы узнать значение.

    Тонкость в том, что форма «<фраза> (АББР)» — обычная русская речь. «Проверка
    достаточности обеспечительного платежа (ОП)» не расшифровка, а предложение, где
    сокращение просто стоит следом. Ругаться на такие значит утопить настоящие находки.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    L = importlib.import_module("kb_lint")

    terms = {"ПРФ": "профиль обслуживания абонента",
             "ЖНЧ": "журнал начислений по счёту",
             "ПП": "прикладная подсистема"}

    bad = L.wrong_expansions("Система хранит признак расчёта фактуры (ПРФ) в базе.", terms)
    assert bad and bad[0][0] == "ПРФ", f"выдуманная расшифровка не поймана: {bad}"
    assert "признак расчёта фактуры" in bad[0][1], bad

    ok_cases = [
        # та же расшифровка другим падежом — не спор
        "Значение профиля обслуживания абонента (ПРФ) берётся на дату подачи.",
        # обычная речь: фраза перед скобкой расшифровкой не является
        "Выполняется проверка достаточности журнала (ЖНЧ) при закрытии периода.",
        # омоним: «ПП» это и подсистема, и платёжное поручение — обе верны в своём месте
        "К заявлению прикладывается копия платёжного поручения (ПП).",
    ]
    for s in ok_cases:
        assert not L.wrong_expansions(s, terms), f"ложная тревога на: {s}"

    # и то же самое целиком, через линтер на настоящем проекте
    root = make_project(tmp)
    card(root, "Reference/Сокращения-проекта.md", type="reference", status="knowledge",
         body="| Сокращение | Значение |\n|---|---|\n"
              "| ПРФ | профиль обслуживания абонента |\n")
    card(root, "Concepts/Расчёт-начислений.md", status="draft",
         body="Начисление считается по признаку расчёта фактуры (ПРФ) на дату подачи.")
    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "ПРФ" in cp.stdout and "расшифровано как" in cp.stdout, cp.stdout


@test
def test_a_section_cannot_be_nested_inside_itself(tmp: Path):
    """`Glossary/Glossary` — не мелочь оформления, а спрятанные карточки.

    Раздел базы — это тип карточки. Второй уровень с тем же именем означает, что
    карточки одного типа разъехались по двум адресам: ссылка по имени ведёт в один,
    ищут в другом, оглавление раздела собирает верхний и не видит нижнего. На живом
    проекте так осели три десятка справочников, и нашлись они только при разборе
    эталонных вопросов.
    """
    root = make_project(tmp)
    card(root, "Glossary/Glossary/ТРФ.md", type="glossary", status="draft",
         body="**ТРФ** — тариф.")
    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "вложена сама в себя" in cp.stdout, cp.stdout
    assert "Glossary/Glossary" in cp.stdout, cp.stdout

    # служебные папки внутри разделов — не нарушение: у них своё назначение
    (root / "AuroraKnowledgeDB" / "Concepts" / "_assets").mkdir(parents=True, exist_ok=True)
    out = run("kb_lint.py", cwd=root).stdout
    assert "_assets: папка вложена" not in out, out


@test
def test_terminology_is_parsed_before_what_refers_to_it(tmp: Path):
    """Словари и списки сокращений идут в разбор первыми.

    Расшифровки подаются модели в промпт, но подать можно только то, что уже разобрано.
    Разбери процесс раньше глоссария — и модель встретит сокращение, которого база ещё
    не знает, а промпт требует определения: она его придумает. Порядок здесь не удобство,
    а условие, при котором запрет выдумывать вообще выполним.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    B = importlib.import_module("build_plan")

    for name in ("Глоссарий-проекта.md", "Термины-и-определения.md",
                 "Список-сокращений.md", "SPR-001-Статусы.md"):
        assert B.is_terminology("Sources/Confluence/" + name), name
    for name in ("Процесс-подачи.md", "US-4.2-Поиск.md", "Требования-к-форме.md"):
        assert not B.is_terminology("Sources/Confluence/" + name), name

    # в плане словарь идёт раньше процесса, даже если крупнее его
    todo = [("Confluence", "Sources/Confluence/Процесс-подачи.md", 1000),
            ("Confluence", "Sources/Confluence/Глоссарий-проекта.md", 90000),
            ("Confluence", "Sources/Confluence/Требования.md", 500)]
    order = {g: i for i, (g, _) in enumerate(B.GROUPS)}
    todo.sort(key=lambda r: (order.get(r[0], 99), 0 if B.is_terminology(r[1]) else 1,
                             r[2], r[1]))
    assert "Глоссарий" in todo[0][1], \
        f"словарь разбирается не первым — сокращения будут выдуманы: {[r[1] for r in todo]}"


@test
def test_search_quality_measures_the_search_that_answers(tmp: Path):
    """Замер качества поиска меряет ту выдачу, которой отвечают человеку.

    Он годами ходил в `kb_embed.search` — чистые вектора. А отвечает «Спросить»
    гибридной выборкой `ctx_pack`. Датчик стоял не на том пути: показывал качество
    индекса вместо качества ответа и не увидел поломки в сложении двух сигналов
    (1.100.38) вовсе — R@1 держался на 0.98, пока вектора в выдаче почти не участвовали.

    Три следствия перехода, каждое проверяется ниже: мерить можно и без индекса; пары
    больше не отбираются по индексу; навигация и заготовки из выдачи убраны, потому что
    их отсекает и сам пак — иначе замер назовёт промахом верный ответ.
    """
    src = (SCRIPTS / "kb_search_quality.py").read_text(encoding="utf-8")
    take = src.split("def ranked")[1].split("\ndef ")[0]
    assert "P.fuse(" in take, \
        "замер снова ходит мимо той выборки, которой отвечают: он не увидит её поломки"
    assert 'c.status or "").strip() == "index"' in take and "is_placeholder" in take, \
        "карта содержания в выдаче замера — промах будет засчитан верному ответу"

    main = src.split("def main")[1]
    assert "cards_with_thesis().items()" in main, \
        "пары снова отбираются по векторному индексу — карточка вне него исчезнет из замера"
    assert "Индекса нет: меряем выдачу по словам" in main, \
        "без индекса замер отказывается, хотя человеку в этом случае отвечают словами"


@test
def test_search_quality_refuses_instead_of_reporting_zero(tmp: Path):
    """Индекс собран другой моделью — это отказ, а не «R@1 0.0».

    `kb_embed.search` на чужой модели возвращает пустой список **молча**: так задумано,
    чужие вектора сравнивать не с чем. Замер, не знающий об этом, честно посчитал бы
    ноль найденных из двухсот и напечатал «R@1 0.0» — приговор базе за расхождение в
    одной строке настроек. Ноль, полученный не измерением, опаснее отсутствия числа:
    по нему пойдут чинить карточки, а чинить надо `AURORA_EMBED_MODEL`.
    """
    root = make_project(tmp)
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "embeddings.json").write_text(
        json.dumps({"model": "e5-large", "dim": 2,
                    "cards": {"Карточка": {"row": 0, "digest": "x"}}}),
        encoding="utf-8")
    env = {**os.environ, "AURORA_EMBED_MODEL": "bge-m3", "AURORA_TESTS_ISOLATED": "1"}
    cp = subprocess.run([sys.executable, str(SCRIPTS / "kb_search_quality.py")],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert cp.returncode == 1, f"молча посчитал на чужом индексе: {cp.stdout}"
    said = cp.stdout + cp.stderr
    assert "e5-large" in said and "bge-m3" in said, \
        f"не назвал обе модели — человек не поймёт, что именно разошлось:\n{said}"
    assert "R@1" not in cp.stdout, f"напечатал меру там, где мерить нечем:\n{cp.stdout}"

    # А молодая база — не поломка: тезисов ещё не написали, мерить нечего, и красный шаг
    # в маршруте «Починить базу» соврал бы про сломанное. Настройка сходится — rc 0.
    (meta / "embeddings.json").write_text(
        json.dumps({"model": "bge-m3", "dim": 2, "cards": {"Карточка": {"row": 0}}}),
        encoding="utf-8")
    cp = subprocess.run([sys.executable, str(SCRIPTS / "kb_search_quality.py")],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert cp.returncode == 0, (
        "молодая база объявлена поломкой: маршрут «Починить базу» покажет ошибку там, "
        f"где просто нечего мерить\n{cp.stdout}{cp.stderr}")
    assert "Мерить нечего" in cp.stdout, cp.stdout


@test
def test_search_quality_asks_with_meaning_not_with_the_title(tmp: Path):
    """Вопрос — тезис из тела, а не заголовок, и не «Заготовка».

    Заголовок и синонимы лежат в самом векторе (`kb_embed.card_texts`), поэтому вопрос
    заголовком меряет совпадение строки с собой и всегда даёт красивое число. Мерить
    надо связь короткой формулировки смысла с полной карточкой — то, что делает человек,
    когда спрашивает базу своими словами. Заготовка смысла не несёт: спрашивать ею нечего.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    Q = importlib.import_module("kb_search_quality")

    card = ("---\ntitle: Профиль абонента\nkind: knowledge\ndistilled: 2026-09-01\n---\n\n"
            "# Профиль абонента\n\n"
            "Набор параметров, определяющий доступные абоненту услуги и порядок их "
            "тарификации в биллинге.\n\n## Связи\n")
    got = Q.thesis(card)
    assert got.startswith("Набор параметров"), f"взят не тезис, а {got!r}"
    assert "title:" not in got and "#" not in got, f"в вопрос утекла шапка: {got!r}"

    stub = ("---\nkind: knowledge\ndistilled: 2026-09-01\n---\n\n"
            "Заготовка: карточка создана ссылкой, содержание не написано.\n")
    assert Q.thesis(stub) == "", "заготовка ушла в вопрос — замер считал бы шум"


@test
def test_search_quality_names_everyone_it_counted_as_a_miss(tmp: Path):
    """Список «не нашлись» обязан совпадать с R@5, а не с R@1.

    Расхождение здесь — худший вид вранья: сводка показывает R@5 0.6, а разбираться
    человеку не с кем, список пуст. Тогда меру перестают читать целиком.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    Q = importlib.import_module("kb_search_quality")

    # выдача: своя карточка на 1-м, на 4-м, на 7-м и не найдена вовсе
    plan = {"первая": 1, "четвёртая": 4, "седьмая": 7, "нету": 0}

    # Подменяем ту выдачу, которой замер и меряет: с 1.100.38 это гибридная выборка,
    # а не `kb_embed.search`. Подмена мимо неё проверяла бы код, который уже не зовут.
    def fake_ranked(question, cfg, model, limit=10):
        pos = plan[question]
        names = [f"чужая-{i}" for i in range(1, Q.TOP + 1)]
        if pos:
            names[pos - 1] = question
        return [(n, round(1.0 - i * 0.01, 4)) for i, n in enumerate(names)]

    old, Q.ranked = Q.ranked, fake_ranked
    try:
        res = Q.measure([(k, k) for k in plan], {}, "m", say=lambda *_: None)
    finally:
        Q.ranked = old

    assert res["R@1"] == 0.25 and res["R@5"] == 0.5, res
    missed = {name for name, _why in res["не нашлись"]}
    assert missed == {"седьмая", "нету"}, \
        f"список расходится с R@5: {missed}"
    assert res["карточек"] == 4 and 0 < res["MRR"] < 1, res


@test
def test_golden_question_is_asked_without_its_own_answer(tmp: Path):
    """Из эталона берём колонку вопроса, а не строку целиком, и годится любая карточка.

    `meta/golden_questions.md` — таблица `| # | Вопрос | Эталон | [[Карточка]] |`, и
    рядом с вопросом лежит **готовый ответ**. Спросить базу строкой целиком значит
    подсказать ей: эталон пересказывает тело карточки, попадание выходит само собой, и
    замер показывает качество подсказки вместо качества поиска. Завышенная мера хуже
    отсутствующей — на неё ссылаются, когда решают, можно ли базе доверять.

    Ссылок в строке может быть несколько. Это не поблажка: одно знание в живой базе
    лежит в справочнике, в процессе, где оно применяется, и в разборе частного случая —
    все трое отвечают верно. Требовать одну конкретную значит мерить угадывание имени.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    Q = importlib.import_module("kb_search_quality")
    importlib.reload(Q)

    gold = tmp / "golden.md"
    gold.write_text(
        "| # | Вопрос | Эталон (кратко) | Карточка-источник |\n"
        "|---|---|---|---|\n"
        "| 1 | Чем профиль отличается от тарифа? | Профиль задаёт набор услуг, тариф — "
        "цену обращения к ним | [[Профиль-абонента]] |\n"
        "| 2 | Где хранится история начислений? | В журнале начислений | "
        "[[Журнал-начислений]], [[Хранение-начислений]] |\n"
        "\n## Известные расхождения в самой базе\n\n"
        "| Что | Верно | Где написано иначе |\n|---|---|---|\n"
        "| Расшифровка кода | так, как в источнике [[Справочник-кодов]] | "
        "иначе сказано в [[Старая-выгрузка]] |\n",
        encoding="utf-8")
    old, Q.GOLDEN = Q.GOLDEN, str(gold)
    try:
        pairs = {names: q for names, q in Q.golden_pairs()}
    finally:
        Q.GOLDEN = old

    by_q = {q: names for names, q in pairs.items()}
    q1 = "Чем профиль отличается от тарифа?"
    assert q1 in by_q, by_q
    assert by_q[q1] == ("Профиль-абонента",), by_q[q1]
    assert "цену обращения" not in q1
    # В файле живут и другие таблицы со ссылками — реестр найденных в базе противоречий,
    # например. Без номера строка не вопрос: иначе замер считал бы то, чего человек в
    # него не клал, и число перестало бы значить обещанное.
    assert len(by_q) == 2, f"в замер уехала строка не из таблицы вопросов: {list(by_q)}"
    assert not any("Расшифровка кода" in q for q in by_q), by_q

    two = next(n for q, n in by_q.items() if "начислений" in q)
    assert set(two) == {"Журнал-начислений", "Хранение-начислений"}, \
        f"вторая карточка строки потеряна — годной считается только одна: {two}"

    # и в замере годится любая из названных. Подменяем `ranked` — с 1.100.38 замер
    # ходит в гибридную выборку, а не в `kb_embed.search`.
    def fake_ranked(question, cfg, model, limit=10):
        return [("Хранение-начислений", 0.9), ("чужая", 0.8)]

    old_s, Q.ranked = Q.ranked, fake_ranked
    try:
        res = Q.measure([(two, "где хранится история начислений")], {}, "m",
                        say=lambda *_: None)
    finally:
        Q.ranked = old_s
    assert res["R@1"] == 1.0, \
        f"вторая годная карточка не засчитана — эталон меряет угадывание имени: {res}"
    assert not res["не нашлись"], res


@test
def test_search_quality_wired_into_engine(tmp: Path):
    """Замер зарегистрирован везде: реестр, манифест, скилл.

    Скрипт, которого нет в `engine_manifest.txt`, остаётся в ките навсегда — этим уже
    болел `kb_embed.py` (1.100.14). Команда, которой нет в `commands.txt`, не появится
    в панели, а человек без терминала иначе её не запустит.
    """
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "ops | ops:search-quality" in reg, "замера нет в реестре — в панели кнопки не будет"
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scripts/kb_search_quality.py" in man, \
        "замер не едет в проекты: останется только в ките"
    skill = (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    assert "ops:search-quality" in skill, "модель о замере не знает — сама не позовёт"
    assert (SCRIPTS / "kb_search_quality.py").is_file()


@test
def test_common_templates_folder_is_part_of_the_schema(tmp: Path):
    """Общие шаблоны: папка в схеме, файлы едут в проект, doctor их не считает мусором.

    Папка вне `structure_dirs.txt` — блокер doctor'а («папки верхнего уровня вне схемы
    движка»), и человек, поставивший обновление, получает ошибку на ровном месте.
    Правило (seed) в манифесте нужно отдельно: без него правка общего шаблона в ките до
    заведённых проектов не доезжает никогда, а обычная строка затирала бы доводку.
    """
    structure = (KIT / "structure_dirs.txt").read_text(encoding="utf-8")
    assert "\nTemplatesCommon\n" in structure, \
        "папки нет в схеме — doctor назовёт её лишней в каждом проекте"

    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scaffold/TemplatesCommon => (seed) TemplatesCommon" in man.replace("  ", " "), \
        "общие шаблоны не едут в проекты обновлением"

    src = (KIT / "scripts/install_aurora.py").read_text(encoding="utf-8")
    assert 'scaffold/TemplatesCommon", "TemplatesCommon"' in src, \
        "новый проект родится с пустой папкой общих шаблонов"

    folder = KIT / "scaffold/TemplatesCommon"
    assert (folder / "README.md").is_file(), \
        "нет описания папки: модель не узнает, что здесь шаблоны и как их читать"
    templates = sorted(p for p in folder.glob("*.md") if p.name != "README.md")
    assert templates, "папка общих шаблонов пуста — брать за основу нечего"

    # Паспорт шаблона: по нему модель выбирает файл под нужный артефакт и версию.
    # Версия в имени файла — чтобы рядом жили старая и новая: документ, начатый по 1.0,
    # дописывают по 1.0, даже когда в папке уже 2.0.
    for t in templates:
        head = t.read_text(encoding="utf-8").split("---")[1]
        for field in ("artifact:", "template:", "version:", "file:", "author:", "created:"):
            assert field in head, f"{t.name}: в паспорте нет поля {field}"
        version = re.search(r'^version:\s*"?([\d.]+)"?', head, re.M)
        assert version, f"{t.name}: версия не разобралась"
        assert t.name.endswith(f"_v{version.group(1)}.md"), \
            f"{t.name}: версии нет в имени файла — рядом не положить следующую"
        assert re.search(r"^file:\s*" + re.escape(t.name) + r"\s*(#.*)?$", head, re.M), \
            f"{t.name}: поле file не совпадает с именем файла"

    # и на свежем проекте папка появляется вместе с содержимым
    root = make_project(tmp)
    assert (root / "TemplatesCommon").is_dir(), "папки нет в разложенном проекте"


@test
def test_related_and_backlinks_join_the_hop_only_when_asked(tmp: Path):
    """Переход за соседом идёт по трём источникам, и каждый включается явно.

    Связь в базе записана дважды: автором — wiki-ссылкой в теле, движком
    (`kb:links --cards`) — markdown-ссылкой в поле `related:`. Ретрив читал только
    первую: выведенные связи были в карточках, но в пак не попадали, а входящие ссылки
    не работали вовсе. Каждый источник — за своим флагом, потому что шум у них разный:
    `related:` на живой базе тащит списки по тридцать имён, входящие ещё шире.
    """
    root = make_project(tmp)
    card(root, "Concepts/Заявочный-контур.md",
         "Заявочный контур принимает заявку и ведёт её по статусам.",
         status="knowledge", kind="knowledge")
    card(root, "Concepts/Связь-из-related.md",
         "Связанное знание, до которого иначе не дойти.",
         status="knowledge", kind="knowledge")
    card(root, "Concepts/Ссылающаяся-карточка.md",
         "Эта карточка опирается на [[Заявочный-контур]] в своём тексте.",
         status="knowledge", kind="knowledge")
    # related: пишется markdown-ссылками — тем же форматом, что кладёт kb:links --cards
    a = root / "AuroraKnowledgeDB/Concepts/Заявочный-контур.md"
    text = a.read_text(encoding="utf-8")
    a.write_text(text.replace("---\n\n", 'related:\n  - "[Связь-из-related]'
                                         '(Связь-из-related.md)"\n---\n\n', 1),
                 encoding="utf-8")

    base = run("ctx_pack.py", "заявочный контур", "--no-log", cwd=root).stdout
    assert "Заявочный-контур" in base, "seed по теме не найден"
    assert "Связь-из-related" not in base, "related: попал в пак без флага"
    assert "Ссылающаяся-карточка" not in base, "входящая ссылка попала в пак без флага"

    rel = run("ctx_pack.py", "заявочный контур", "--no-log",
              "--retrieval", "hop_related=1", cwd=root).stdout
    assert "Связь-из-related" in rel, "флаг hop_related не подтянул соседа из related:"
    assert "Ссылающаяся-карточка" not in rel, "hop_related включил и входящие — флаги смешаны"

    back = run("ctx_pack.py", "заявочный контур", "--no-log",
               "--retrieval", "hop_backlinks=1", cwd=root).stdout
    assert "Ссылающаяся-карточка" in back, "флаг hop_backlinks не подтянул ссылающуюся"
    assert "Связь-из-related" not in back, "hop_backlinks включил и related: — флаги смешаны"


@test
def test_pagerank_prefers_the_hub_among_relevant(tmp: Path):
    """Центральность узла переставляет НАЙДЕННОЕ и не вытаскивает хабы без совпадения.

    Графовый вес не зависит от запроса, поэтому он — мультипликативный буст к
    релевантным, а не самостоятельный сигнал: аддитивный вес ставил бы карту содержания
    первой по любой теме. Проверяем оба края: при равных словах хаб выше сироты, а
    карточка без совпадения не появляется вовсе, сколько бы на неё ни ссылались.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    P = importlib.import_module("ctx_pack")

    root = make_project(tmp)
    card(root, "Concepts/Ядро-темы.md", "Правило учёта работает одинаково во всех разделах.",
         status="knowledge", kind="knowledge")
    card(root, "Concepts/Записка-про-тему.md", "Правило учёта работает одинаково во всех разделах.",
         status="knowledge", kind="knowledge")
    card(root, "MOC/Карта-понятий.md", "Оглавление без знания о правиле.",
         type="moc", status="index")
    for i in range(6):      # хаб — тот, на кого ссылаются
        card(root, f"Concepts/Частный-случай-{i}.md",
             f"Частный случай номер {i}, смотри [[Ядро-темы]].",
             status="knowledge", kind="knowledge")

    cwd = os.getcwd()
    saved = dict(P.RETRIEVAL)
    try:
        os.chdir(root)
        importlib.reload(P)
        cards = P.load_cards()
        P.measure_rarity(cards)
        P.RETRIEVAL["pagerank"] = 0.0
        plain = [c.stem for _w, c in P.fuse(cards, "правило учёта", {}, limit=6)]
        assert "Карта-понятий" not in plain, "хаб без совпадения с запросом попал в выдачу"
        P.RETRIEVAL["pagerank"] = 1.0
        boosted = [c.stem for _w, c in P.fuse(cards, "правило учёта", {}, limit=6)]
        assert boosted[0] == "Ядро-темы", \
            f"при равных словах первым обязан быть хаб, а не {boosted[0]}"
        assert "Карта-понятий" not in boosted, "буст вытащил нерелевантный хаб в выдачу"
    finally:
        os.chdir(cwd)
        P.RETRIEVAL.clear()
        P.RETRIEVAL.update(saved)
        importlib.reload(P)


@test
def test_collapsed_neighbor_carries_gist_not_body(tmp: Path):
    """Свёрнутый сосед — шапка доверия и суть, а не полный текст.

    Сосед по ссылке нужен модели как обещание «есть такое знание», а не как ещё тысяча
    знаков: сорок полных соседей съедали бюджет пака раньше, чем в него входили seed'ы.
    При neighbor_full=0 тело соседа не уезжает, а указатель на него остаётся.
    """
    root = make_project(tmp)
    card(root, "Concepts/Главная-мысль.md",
         "Загадочный приём описан здесь. Смотри также [[Соседняя-мысль]].",
         status="knowledge", kind="knowledge")
    card(root, "Concepts/Соседняя-мысль.md",
         "УНИКАЛЬНОЕ-ТЕЛО-СОСЕДА: подробности, которые не должны уехать свёрнутыми. "
         + "Длинное тело соседа. " * 40,
         status="knowledge", kind="knowledge", summary='"Суть соседа одной строкой"')

    full = run("ctx_pack.py", "загадочный приём", "--no-log", cwd=root).stdout
    assert "УНИКАЛЬНОЕ-ТЕЛО-СОСЕДА" in full, "по умолчанию сосед едет целиком"

    slim = run("ctx_pack.py", "загадочный приём", "--no-log",
               "--retrieval", "neighbor_full=0", cwd=root).stdout
    assert "Соседняя-мысль" in slim, "свёрнутый сосед пропал из пака совсем"
    assert "УНИКАЛЬНОЕ-ТЕЛО-СОСЕДА" not in slim, "свёрнутый сосед уехал телом"
    assert "Суть соседа одной строкой" in slim, "у свёрнутого соседа потерялась суть"
    assert len(slim) < len(full), "свёртка не уменьшила пак"


@test
def test_config_scalar_rule_lives_in_one_place(tmp: Path):
    """Правило чтения скаляра конфига — одно и живёт в одном файле.

    Живой случай: копий правила было пять (настройка, синки, панель, шаг отчёта и разбор
    на JS), и JQL с датой в кавычках читался то так, то никак — поле формы выглядело
    пустым. Разные прочтения одного конфига хуже любого из них: человек видит одно,
    команда работает по другому.
    """
    import re as _re
    sources = sorted((KIT / "scripts").glob("*.py")) + [
        KIT / "cockpit/aurora_cockpit.py", KIT / "reports/analyst/paths.py"]
    rule = "(?:'((?:[^'"
    holders = [p.name for p in sources if rule in p.read_text(encoding="utf-8")]
    assert holders == ["aurora_common.py"], \
        f"правило разбора скаляра скопировано в {holders} — менять придётся во всех"
    ui = panel_sources()
    assert rule not in ui and "cfgRaw.values" in ui, \
        "панель держит свою копию правила на JS вместо разобранных значений с сервера"

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    SC = importlib.import_module("sources_core")
    AS = importlib.import_module("aurora_setup")
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("analyst_paths", KIT / "reports/analyst/paths.py")
    apaths = _ilu.module_from_spec(spec)
    spec.loader.exec_module(apaths)
    # все читатели обязаны звать именно её — сверяем поведением на всех трёх формах
    sample = ("    a: 'дата >= \"2026-11-01\"'\n    b: \"обычное\"\n    c: голое\n"
              "    d: 'кавычка '' внутри'\n")
    for name, fn in (("sources_core", SC.scalar), ("aurora_setup", AS.yaml_scalar),
                     ("панель", ck.config_value), ("отчёт", apaths.scalar)):
        for key, want in (("a", 'дата >= "2026-11-01"'), ("b", "обычное"),
                          ("c", "голое"), ("d", "кавычка ' внутри")):
            assert fn(sample, key) == want, f"{name} читает {key} иначе: {fn(sample, key)!r}"
    assert AC.yaml_str('дата >= "2026-11-01"').startswith("'"), \
        "значение с двойной кавычкой записано не в одинарных — читатели его потеряют"


@test
def test_engine_settings_reach_commands_but_secrets_do_not(tmp: Path):
    """Ключи `AURORA_*` из файла настроек доезжают до команды, токены — нет.

    В документации ретрива сказано: `AURORA_RETRIEVAL` можно задать в настройках движка.
    Пока окружение дочернего процесса копировалось как есть, написанное там не работало
    при запуске из панели — настройка, которой нельзя пользоваться, хуже её отсутствия.
    Секретам в окружении команд делать нечего: их читают сами скрипты.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    root = make_project(tmp)
    (root / AC.ENV_FILE).write_text(
        "AURORA_RETRIEVAL=pagerank=0\nJIRA_PERSONAL_TOKEN=секрет\n", encoding="utf-8")
    env = AC.child_env(str(root))
    assert env.get("AURORA_RETRIEVAL") == "pagerank=0", \
        "настройка движка не дошла до команды — документация обещает несбыточное"
    assert "JIRA_PERSONAL_TOKEN" not in env, \
        "в окружение команды уехал токен: лишняя копия секрета — лишний способ его обронить"
    os.environ["AURORA_RETRIEVAL"] = "pagerank=0.5"
    try:
        assert AC.child_env(str(root))["AURORA_RETRIEVAL"] == "pagerank=0.5", \
            "файл перебил переменную оболочки — ближняя к запуску настройка должна побеждать"
    finally:
        os.environ.pop("AURORA_RETRIEVAL", None)
    panel = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "child_env(project" in panel, \
        "панель запускает команды без настроек проекта — файл настроек снова не доедет"
    # опечатка в переменной окружения не проходит молча
    card(root, "Concepts/Любая.md", "Любое знание.", status="knowledge", kind="knowledge")
    cp = subprocess.run([sys.executable, str(SCRIPTS / "ctx_pack.py"), "любая", "--no-log"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env={**os.environ, "AURORA_RETRIEVAL": "pagerannk=1"})
    assert "pagerannk" in cp.stderr, f"опечатка в переменной прошла молча:\n{cp.stderr[:300]}"


@test
def test_time_is_recorded_in_utc_and_shown_in_local_time(tmp: Path):
    """Время фиксируется в UTC одним правилом, а показывается по часовому поясу системы.

    Живой случай: в одном журнале стояли местные часы, UTC и время Jira без зоны (выгрузка
    отчёта срезала смещение «+0300»), и сравнить записи между собой было нельзя. Теперь
    «сейчас» движок берёт только в `aurora_common`, а показ переводит время в местное.
    """
    import re as _re
    from datetime import datetime as _dt, timezone as _tz
    files = (sorted((KIT / "scripts").glob("*.py")) + sorted((KIT / "scripts/agents").glob("*.py"))
             + [KIT / "cockpit/aurora_cockpit.py"] + sorted((KIT / "reports/analyst").glob("*.py")))
    naive = _re.compile(r"datetime\.now\(\)|datetime\.datetime\.now\(\)|date\.today\(\)|"
                        r"datetime\.date\.today\(\)|time\.strftime\(|utcnow\(|"
                        r"\.fromtimestamp\([^)]*\)\.strftime")
    offenders = [f"{p.relative_to(KIT)}:{i}" for p in files if p.name != "aurora_common.py"
                 for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if naive.search(line) and not line.lstrip().startswith("#")]
    assert not offenders, \
        f"время берётся мимо общего правила (aurora_common) — запись уйдёт в местных часах: {offenders[:8]}"

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    A = importlib.import_module("aurora_common")
    assert _re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", A.utc_stamp()), A.utc_stamp()
    assert A.utc_label().endswith(" UTC") and _re.fullmatch(r"\d{8}-\d{6}Z", A.utc_slug())
    # Jira пишет смещение без двоеточия — его нельзя срезать, его надо учесть
    assert A.utc_stamp(A.parse_time("2026-09-17T17:39:52.000+0300")) == "2026-09-17T14:39:52Z", \
        "время Jira переведено в UTC неверно — смещение сервера потеряно"
    assert A.utc_stamp(A.parse_time("2026-09-21 10:15 UTC")) == "2026-09-21T10:15:00Z"
    # запись без зоны — прежняя, сделанная по местному времени
    legacy = A.parse_time("2026-09-15 14:48")
    assert legacy.astimezone().strftime("%Y-%m-%d %H:%M") == "2026-09-15 14:48", \
        "прежняя местная запись прочитана со сдвигом"
    # показ — в часовом поясе системы
    want = _dt(2026, 9, 21, 10, 15, 3, tzinfo=_tz.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    assert A.local_view("2026-09-21T10:15:03Z") == want, "показ не перевёл UTC в местное время"
    assert A.local_view("22:17") == "22:17", "не-время испорчено показом"
    # неделя отчёта — по часам системы: одна функция на все шаги
    y, w, _d = _dt(2026, 9, 20, 22, 30, tzinfo=_tz.utc).astimezone().isocalendar()
    assert A.local_week("2026-09-20T22:30:00Z") == (y, w), "неделя посчитана не по часам системы"
    for step in ("make_analyst_metrics.py", "verify_weekly_by_person.py"):
        src = (KIT / "reports/analyst" / step).read_text(encoding="utf-8")
        assert "from aurora_common import iso_week, iso_year" in src and "def iso_week" not in src, \
            f"{step} считает неделю своей копией правила"
    assert A.iso_week("2026-09-20T22:30:00Z") == f"{w:02d}" and A.iso_year("2026-09-20T22:30:00Z") == y, \
        "iso_week/iso_year не по часам системы"
    fetch = (KIT / "reports/analyst/fetch_full.py").read_text(encoding="utf-8")
    assert '_utc(h.get("created", ""))' in fetch and "[:19]" not in fetch, \
        "выгрузка отчёта снова срезает смещение Jira"

    ui = panel_sources()
    assert ' UTC$/, "$1T$2Z")' in ui, "панель не понимает отметку «… UTC» из заголовков журналов"
    assert "(\\d{2})(Z?)/.exec(runId" in ui, "панель читает имя прогона в UTC как местное время"
    # Переводчик один, зовут его по-разному: в ядре напрямую, в модуле — через ctx.fmt.
    # `r.since` из этого списка убран намеренно: в реестре это версия движка
    # («1.3.0»), а не время. Формат времени превращал её в дату 3 января.
    for shown in ('h.ping.when', 'h.retrieval.when',
                  'v.when', 'turn.at', 'agent.since'):
        assert f"histWhen({shown})" in ui or f"fmt.when({shown})" in ui, \
            f"панель показывает сырую отметку без перевода в местное время: {shown}"


@test
def test_run_summary_counts_everything_the_human_asked(tmp: Path):
    """В конце прогона — время, токены и скорость, документы, карточки и ошибки по видам.

    Просьба заказчика: итоговое время всего прогона, потраченные токены, средняя скорость
    генерации, сколько документов обработано, пропущено и не разобрано, сколько карточек
    создано, дополнено и удалено, сколько не удалось создать или обновить, сколько и каких
    ошибок было. Раньше в конце стояло «пройден: 28 шагов», остальное — ищи в выводе.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    RS = importlib.import_module("run_summary")
    steps = [{"source": "a", "status": "разобран"}, {"source": "b", "status": "разобран по абзацам"},
             {"source": "c", "status": "пропущена"},
             {"source": "d", "status": "сбой", "note": "шлюз не ответил"},
             {"source": "e", "status": "сбой", "content_fail": True},
             {"card": "X", "status": "сбой", "why": "ответ пустой"}]
    usage = {"calls": 10, "failed": 1, "tokens_in": 1000, "tokens_out": 400, "gen_seconds": 8.0,
             "errors": {"модель не уложилась в срок": 1}}
    s = RS.from_agent(steps, usage, 125.0, {"created": 3, "updated": 5, "deleted": 2})
    assert (s["docs_done"], s["docs_skipped"], s["docs_failed"]) == (2, 1, 2), s
    assert s["cards_failed"] == 1 and s["cards_created"] == 3 and s["cards_deleted"] == 2, s
    text = "\n".join(RS.render(s))
    for need in ("Время: 2 мин 5 с", "токенов 1 400", "50.0 ток/с", "обработано 2", "пропущено 1",
                 "не удалось разобрать 2", "создано 3", "обновлено/дополнено 5", "удалено 2",
                 "не удалось создать/обновить 1", "Ошибки: 3", "неудачных 1",
                 "карточка не записана", "сбой: шлюз не ответил — 1", "сбой: ответ пустой — 1"):
        assert need in text, f"в итоге нет «{need}»:\n{text}"
    # Неудачный вызов модели — не ошибка прогона, пока есть шаги: его повторяют, и он в строке
    # модели. Иначе один сбой считался дважды (PRJ-A 22.09.2026: «Ошибки: 198» при 99 карточках).
    assert "модель не уложилась в срок" not in text.split("Ошибки:")[1], \
        f"неудачный вызов модели посчитан ошибкой рядом с упавшим шагом:\n{text}"
    alone = "\n".join(RS.render(RS.from_agent([], usage, 5.0, None)))
    assert "модель не уложилась в срок — 1" in alone, "без шагов причина неудачи вызова пропала"
    assert RS.parse(["шум", RS.emit(s)]) == [s], "машинная строка итога не читается обратно"

    # изменения базы — по git: создание, правка, удаление, перенос в архив; служебное не в счёт
    repo = tmp / "repo"
    kb = repo / "AuroraKnowledgeDB"
    for rel in ("Concepts/A.md", "Concepts/B.md", "Concepts/C.md", "Concepts/D.md"):
        (kb / rel).parent.mkdir(parents=True, exist_ok=True)
        (kb / rel).write_text("# " + rel + "\n", encoding="utf-8")
    g = lambda *args: subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace")
    g("init", "-q"); g("add", "-A")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "база")
    since = RS.git_head(str(repo))
    (kb / "Concepts/A.md").write_text("# A\n\nдополнено\n", encoding="utf-8")        # обновлена
    (kb / "Concepts/B.md").unlink()                                                  # удалена
    (kb / "_archive").mkdir()
    g("mv", "AuroraKnowledgeDB/Concepts/C.md", "AuroraKnowledgeDB/_archive/C.md")     # в архив
    (kb / "Concepts/E.md").write_text("# E\n", encoding="utf-8")                     # создана
    (kb / "Concepts/D.md").write_text("---\nextracted: 2026-09-22\n---\n# Concepts/D.md\n",
                                      encoding="utf-8")                              # только шапка
    (kb / "MOC").mkdir()
    (kb / "MOC/Карта.md").write_text("# карта\n", encoding="utf-8")                  # служебное
    delta = RS.kb_delta(str(repo), since)
    assert delta == {"created": 1, "updated": 1, "marked": 1, "deleted": 2}, \
        f"изменения базы посчитаны неверно (правка одной шапки — не правка знания): {delta}"

    got = RS.route(str(repo), since, 3600, [
        {"cmd": "agent:build", "rc": 0, "summary": [s]}, {"cmd": "kb:repair", "rc": 2, "summary": []}])
    lines = "\n".join(got["lines"])
    assert got["lines"][0] == "■ Итог прогона" and "Время: 1 ч 0 мин" in lines, lines
    assert "шаг kb:repair не отработал (код 2) — 1" in lines, "упавший шаг не попал в ошибки"
    assert "создано 1 · обновлено/дополнено 1 · удалено 2" in lines \
        and "служебных отметок 1" in lines, \
        f"карточки маршрута посчитаны не по git, а суммой шагов:\n{lines}"
    assert RS.route(str(tmp), "", 1, [])["data"]["cards_known"] is False, \
        "без git изменения базы выданы за посчитанные"

    ui = panel_sources()
    # Маршрут ведёт сервер (1.152.0): итог складывает он, тем же `run_summary`.
    rr = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert "ck.RS.git_head(self.project)" in rr and "ck.RS.route(self.project, head" in rr, \
        "маршрут в панели не собирает итог прогона"
    assert ui.count("consoleLine(out, l)") >= 2 and "startsWith(SUMMARY_MARK)) return;" in ui, \
        "машинная строка итога показывается человеку в консоли"
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "RS.route(project" in srv and "RS.git_head(project)" in srv, \
        "итог маршрута собирает не движок — у панели появился бы свой счёт"
    ar = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert "print(RS.emit(summ))" in ar and "RS.render(summ" in ar, "агент не печатает итог прогона"
    assert "scripts/run_summary.py" in (KIT / "engine_manifest.txt").read_text(encoding="utf-8"), \
        "модуль итога не доедет до проектов"


@test
def test_model_usage_is_counted_for_the_run_summary(tmp: Path):
    """Токены, скорость генерации и неудачные вызовы модели считаются там, где модель зовут.

    Считать их по отчётам шагов нельзя: у каждой задачи отчёт свой, и новая задача молча
    выпала бы из счёта. Вызов модели один на движок — там и счётчик.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib, time as _t
    AG = importlib.import_module("agent_core")
    AG.DOWN.clear()
    AG.USAGE.update(calls=0, failed=0, tokens_in=0, tokens_out=0, gen_seconds=0.0, errors={})

    def good(kind, b, payload, timeout):
        if kind == "slots":
            return 200, {"slots_idle": 1}, "", 0.0
        return 200, {"choices": [{"message": {"content": "ответ"}}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 50}}, "", 2.0

    cfg = {"backends": [{"n": 1, "url": "http://a", "key": "", "model": "m", "models": {}}],
           "request_timeout": 100, "thinking": False}
    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                     transport=good, deadline=_t.time() + 5, sleep=lambda s: None)
    assert r["ok"], r
    u = AG.USAGE
    assert (u["calls"], u["tokens_in"], u["tokens_out"]) == (1, 100, 50), f"токены не посчитаны: {u}"
    assert u["gen_seconds"] > 0, "время генерации не посчитано — средней скорости не будет"

    def dead(kind, b, payload, timeout):
        if kind == "slots":
            return 200, {"slots_idle": 1}, "", 0.0
        return 0, {}, "TimeoutError: timed out", 0.05

    AG.DOWN.clear()
    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                     transport=dead, deadline=_t.time() + 0.3, sleep=lambda s: None)
    assert not r["ok"]
    assert AG.USAGE["failed"] == 1 and sum(AG.USAGE["errors"].values()) == 1, \
        f"неудачный вызов модели не попал в ошибки итога: {AG.USAGE}"
    AG.DOWN.clear()


@test
def test_distill_drains_the_queue_in_one_pass_and_keeps_paid_answers(tmp: Path):
    """Тезисы — вся очередь за один проход; остановка не выбрасывает оплаченных ответов.

    Живой случай, PRJ-A 21.09.2026: «Обновить базу» шёл десять часов. Переосмысление брало
    по пятнадцать карточек за оборот маршрута, оборот ждал самую медленную из них, а каждый
    оборот заново гонял разбор, типы, вектора, связи и карты — 576 карточек без тезиса это
    тридцать пять оборотов. После «трёх сбоев подряд» выход из пула доигрывал всю партию в
    лежащий шлюз и выбрасывал ответы.
    """
    import time as _t
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    def base(name, n):
        root = tmp / name
        kb = root / "AuroraKnowledgeDB/Concepts"
        kb.mkdir(parents=True)
        for i in range(n):
            (kb / f"К{i:02d}.md").write_text(
                f'---\nid: X{i}\ntitle: "К{i:02d}"\nkind: knowledge\nstatus: knowledge\n'
                f'---\n\nтело-{i:02d}-конец\n', encoding="utf-8")
        return root

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m", "AURORA_AGENT_PARALLEL": "4"})

    def card_of(messages):
        import re as _re
        text = " ".join(m.get("content") or "" for m in messages)
        return int(_re.search(r"тело-(\d\d)-конец", text).group(1))

    # 1. Окно вместо бюджета: вся очередь за проход, фиксация по ходу
    commits = []

    def fine(cfg_, role, messages, prefer=0, **kw):
        _t.sleep(0.01)
        return {"ok": True, "text": "Тезис карточки.", "backend": 1, "model": "m", "tps": 9,
                "log": []}

    root = base("all", 40)
    res = R.run_distill(cfg, str(root), apply=True, limit=0, momus=False, call=fine,
                        window_s=60, commit=commits.append, commit_every=10)
    assert len(res["steps"]) == 40 and res["left"] == 0, \
        f"проход до конца очереди остановился на {len(res['steps'])} из 40 — снова обороты"
    assert commits == [10, 20, 30, 40], f"проход не фиксирует сделанное по ходу: {commits}"
    assert all("distilled:" in p.read_text(encoding="utf-8")
               for p in (root / "AuroraKnowledgeDB/Concepts").glob("*.md"))
    assert R.distill_queue(cfg, str(root)) == [], "очередь не опустела после записи тезисов"

    # 2. Остановка: новых не берём, начатые дописываем
    asked = []

    def flaky(cfg_, role, messages, prefer=0, **kw):
        i = card_of(messages)
        asked.append(i)
        if i < 3:
            return {"ok": False, "log": ["№1: connection refused"]}
        _t.sleep(0.3)                      # в воздухе, когда остальные уже упали
        return {"ok": True, "text": "Тезис карточки.", "backend": 1, "model": "m", "tps": 9,
                "log": []}

    root = base("stop", 12)
    res = R.run_distill(cfg, str(root), apply=True, limit=12, momus=False, call=flaky)
    assert res["gateways_down"], "три сбоя подряд не названы остановкой шлюза"
    # Три быстрых сбоя приходят раньше медленных ответов; до третьего пул вправе взять
    # по карточке на каждый освободившийся слот, после — ни одной.
    assert len(asked) <= 4 + R.FAILS_IN_A_ROW - 1, \
        f"после остановки пул брал новые карточки: {sorted(asked)}"
    started = [i for i in asked if i >= 3]
    assert started and all(
        "distilled:" in (root / f"AuroraKnowledgeDB/Concepts/К{i:02d}.md").read_text(
            encoding="utf-8") for i in started), \
        "начатая до остановки карточка выброшена — ответ оплачен впустую"

    # 3. Заход не берёт снова то, что уже пробовал
    root = base("skip", 5)
    first = sorted(R.distill_queue(cfg, str(root)))[:2]
    res = R.run_distill(cfg, str(root), apply=False, limit=5, momus=False, call=fine,
                        skip=set(first))
    assert len(res["steps"]) == 3, "заход снова взял карточки, которые этот прогон уже пробовал"

    # 4. В движке: ветка до конца очереди — общий цикл заходов, проход в окне
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    branch = src.split('elif a.task == "distill" and a.until_done and a.apply:')[1] \
        .split('elif a.task == "distill":')[0]
    assert 'until_done(cwd, a, "distill"' in branch and "window_s=" in branch \
        and "commit_every=" in branch, "тезисы до конца очереди идут мимо общего цикла"
    # Остаток — настоящий: внутри цикла маршрута без отложенных (`--defer-refresh`), их
    # перепишет шаг после цикла (1.138.1).
    assert '"left": len(distill_queue(cfg, cwd, a.defer_refresh))' in branch, \
        "итог прохода называет не настоящий остаток — маршрут не узнает о невышедших"


@test
def test_update_route_takes_theses_and_definitions_in_one_lap(tmp: Path):
    """«Обновить базу» и «Пересобрать» проходят очередь тезисов и выноса за оборот.

    Цикл маршрута держит обороты, пока движок говорит, что работа есть. Тезисы шли по
    пятнадцать, вынос определений — в бюджете двадцать минут: оборотов было столько же,
    сколько партий, и каждый повторял разбор, вектора, связи и карты.
    """
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    for tag in ("[update]", "[rebuild]"):
        part = scen.split(tag)[1].split("\n[")[0]
        cycle = part.split("цикл:")[1].split("конец цикла")[0]
        for cmd in ("agent:distill", "agent:extract"):
            line = next(l for l in cycle.splitlines() if l.startswith(cmd))
            assert "--until-done" in line and "--apply" in line, \
                f"в маршруте {tag} {cmd} снова идёт партиями по обороту: {line}"
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert "window_min=a.hours * 60 if a.until_done else 0" in src, \
        "вынос определений в --until-done по-прежнему в бюджете шага"
    sys.path.insert(0, str(KIT / "scripts"))
    import inspect
    import agent_runner as R
    body = inspect.getsource(R.run_extract)
    assert 'min(budget, time.time() + AG.call_budget(cfg, "planner"))' in body, \
        "в окне на двенадцать часов одна карточка ждала бы лежащий шлюз до утра"
    guard = src.split('elif a.task == "distill":')[1][:400]
    assert "a.until_done and not a.apply" in guard, \
        "«Посмотреть» маршрут снимает --apply: предпросмотр не пишет, и очередь не убудет"


@test
def test_embed_gateway_hiccup_does_not_stop_the_route(tmp: Path):
    """Шлюз векторов не ответил — шаг не поломан: код 1, индекс цел, маршрут идёт дальше.

    Живой случай, PRJ-A 21.09.2026: `kb:embed` вернул 2 на мигнувшем шлюзе, и маршрут
    «Обновить базу» встал с «команда не отработала», хотя индекс — производная и прежний
    работал. Проверка после остановки нашла шлюз живым.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "Карточка.md").write_text('---\ntitle: "Карточка"\nkind: knowledge\n---\n\nзнание\n',
                                    encoding="utf-8")
    # В изоляции движок читает из окружения только `AURORA_AGENT_*`: шлюз векторов —
    # кольцо чата, первый бэкенд. Порт 9 закрыт — отказ приходит сразу.
    env = {**os.environ, "AURORA_TESTS_ISOLATED": "1",
           "AURORA_AGENT_BACKEND_1_URL": "http://127.0.0.1:9/v1",
           "AURORA_AGENT_REQUEST_TIMEOUT": "5"}
    cp = subprocess.run([sys.executable, str(root / ".opencode/scripts/kb_embed.py"), "--apply"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    assert cp.returncode == 1, \
        f"сбой шлюза векторов снова останавливает маршрут: код {cp.returncode}\n{cp.stderr[-400:]}"
    assert "индекс не тронут" in cp.stderr, f"сбой не назван человеку:\n{cp.stderr[-400:]}"
    ui = panel_sources()
    assert "const failed = rc => rc >= 2 || rc < 0;" in ui, \
        "маршрут считает поломкой не то, что раньше: код 1 шага не должен его останавливать"


@test
def test_embed_keeps_what_it_counted_before_the_gateway_dropped(tmp: Path):
    """Шлюз векторов оборвался посреди сборки — посчитанное остаётся в индексе.

    Прогон PRJ-A 29.09.2026: шлюз отвечал медленно, 192 куска из 543 были готовы, а один
    запрос не уложился в срок. `kb:embed` выбрасывал всё посчитанное, маршрут пережидал сеть
    и начинал заново — с тем же исходом на каждом повторе.
    """
    import http.server
    import threading
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    for i in range(40):
        (kb / f"Карточка-{i}.md").write_text(
            f'---\ntitle: "Карточка {i}"\nkind: knowledge\n---\n\nзнание номер {i}\n',
            encoding="utf-8")
    state = {"calls": 0, "fail_from": 2, "inputs": []}

    class Fake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"] += 1
            if state["fail_from"] and state["calls"] >= state["fail_from"]:
                self.send_response(500)
                self.end_headers()
                return
            state["inputs"].append(len(body["input"]))
            data = [{"index": i, "embedding": [1.0, float(i % 7), 0.5, 0.25]}
                    for i in range(len(body["input"]))]
            out = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "AURORA_TESTS_ISOLATED": "1",
           "AURORA_AGENT_BACKEND_1_URL": f"http://127.0.0.1:{srv.server_address[1]}/v1",
           "AURORA_AGENT_REQUEST_TIMEOUT": "5"}
    run_embed = lambda: subprocess.run(
        [sys.executable, str(root / ".opencode/scripts/kb_embed.py"), "--apply"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    try:
        cp = run_embed()
        total = int(re.search(r"Пересчитать: (\d+)", cp.stdout).group(1))
        assert total > 32, cp.stdout
        assert cp.returncode == 1, f"недосчитанный индекс выдан за готовый: {cp.returncode}"
        assert f"посчитано 32 из {total}" in cp.stderr, cp.stderr[-400:]
        idx = json.loads((root / "AuroraKnowledgeDB/meta/embeddings.json").read_text(encoding="utf-8"))
        assert len(idx["cards"]) == 32, f"посчитанное до обрыва не сохранено: {len(idx['cards'])}"

        # связь вернулась — досчитывается только остаток
        state.update(calls=0, fail_from=0, inputs=[])
        cp = run_embed()
        assert cp.returncode == 0, cp.stdout[-400:] + cp.stderr[-400:]
        assert sum(state["inputs"]) == total - 32, \
            f"повтор посчитал заново готовое: {sum(state['inputs'])} вместо {total - 32}"
        idx = json.loads((root / "AuroraKnowledgeDB/meta/embeddings.json").read_text(encoding="utf-8"))
        assert len(idx["cards"]) == total
    finally:
        srv.shutdown()


@test
def test_candidate_search_does_each_piece_of_work_once(tmp: Path):
    """Подбор кандидатов: тот же результат без лишнего счёта процессора.

    Замер на PRJ-A 21.09.2026: подбор кандидатов для одной карточки — от 2 до 10 секунд
    чистого процессора. Запрос сверялся с 5558 кусками индекса генератором по 1024
    измерениям, а разбор запроса на слова повторялся для каждой из 1808 карточек. Под общим
    замком интерпретатора потоки переосмысления ждали этот счёт по очереди. После правки —
    те же 40 выдач из 40, в три раза быстрее.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    import random
    from operator import mul
    E = importlib.import_module("kb_embed")
    P = importlib.import_module("ctx_pack")
    rnd = random.Random(7)
    a = [rnd.uniform(-1, 1) for _ in range(1024)]
    b = [rnd.uniform(-1, 1) for _ in range(1024)]
    assert abs(E.dot(a, b) - sum(map(mul, a, b))) < 1e-9, "скалярное произведение считает не то"
    src = (KIT / "scripts/kb_embed.py").read_text(encoding="utf-8")
    assert "for k in range(dim)" not in src and "for j in range(dim)" not in src, \
        "сверка с индексом снова идёт генератором по измерениям"

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        (kb / f"Предмет-{i}.md").write_text(
            f'---\ntitle: "Предмет {i}"\nkind: knowledge\n---\n\nреестр налогоплательщиков {i}\n',
            encoding="utf-8")
    here = os.getcwd()
    try:
        os.chdir(root)
        cards = P.load_cards()
        topic = "реестр налогоплательщиков и отчёт по НДС"
        calls = []
        real = P.words

        def counting(text):
            if text == topic:
                calls.append(1)
            return real(text)

        P.words = counting
        try:
            got = P.fuse(cards, topic, close={}, limit=10)
        finally:
            P.words = real
        best = max(P.score(c, topic) for c in cards.values())
        top = P.score(got[0][1], topic) if got else None
    finally:
        os.chdir(here)
    assert len(calls) == 1, f"запрос разбирается на слова {len(calls)} раз на один поиск"
    assert top == best, "первой в выдаче стоит не лучшая по словам карточка"


@test
def test_thinking_advice_matches_the_measurement(tmp: Path):
    """Совет «выключите рассуждения у пересказа — не хуже» замер не подтвердил.

    Замер PRJ-A 21.09.2026 с перекрёстной проверкой: тезисы без рассуждений приукрашивают
    («возможен вариант» → «исключительно»), и строгий Момус находит в них втрое больше
    утверждений без опоры. Момус без рассуждений не нашёл ни одного из девяти. Совет в
    ките обязан говорить это, иначе по нему выключат рассуждения и испортят базу.
    """
    for rel in ("skills/aurora-vault/SKILL.md", "scripts/agent_core.py", "commands.txt",
                "cockpit/ui"):
        text = ui_source() if rel == "cockpit/ui" else (KIT / rel).read_text(encoding="utf-8")
        assert "тезис выходит не хуже" not in text and "в 11 раз быстрее и не хуже" not in text, \
            f"{rel}: снова обещано, что без рассуждений тезис не хуже"
    skill = (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    assert "втрое больше" in skill and "Момус без рассуждений" in skill, \
        "навык не говорит, чем платят за выключенные рассуждения"


@test
def test_first_thesis_is_not_a_redistill_and_false_history_is_repaired(tmp: Path):
    """Первый тезис — не «пересборка»; ложная история первых тезисов снимается ремонтом.

    Живой случай, PRJ-A 22.09.2026: все 399 карточек с тезисом прошли первый тезис как
    пересборку. Карточка, которую разбор только что перенёс, не имеет раздела дословного
    текста, и всё её тело принималось за «прежний тезис»: модель получала источник дважды
    и не получала списка карточек для ссылок, а в историю ложилась запись «источник
    изменился» с источником под «прежним тезисом» — карточка вдвое толще, 1,44 млн знаков
    дублей на базу.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    A = importlib.import_module("agent_core")
    root = make_project(tmp)
    source = "Реестр отчётов показывает отчёты налогоплательщика за выбранный период. " * 4
    fresh = card(root, "Concepts/Реестр-отчётов.md", status="draft", kind="knowledge",
                 body=source)
    old_thesis = "Реестр отчётов — перечень отчётов плательщика за период."
    card(root, "Concepts/Реестр-решений.md", status="draft", kind="knowledge",
         body=old_thesis + "\n\n" + R.QUOTES + "\n" + source.replace("отчёт", "решени"))
    asked = {}

    def fake(cfg, role, messages, **kw):
        text = " ".join(m.get("content") or "" for m in messages)
        key = "пересборка" if "изменился, и тезис надо пересобрать" in text else "первый"
        asked.setdefault(key, []).append(text)
        answer = ("Реестр решений — перечень решений.\nИЗМЕНИЛОСЬ:\nуточнён состав."
                  if key == "пересборка" else
                  "Реестр отчётов — перечень отчётов за период.")
        return {"ok": True, "text": answer, "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    R.run_distill(cfg, str(root), apply=True, limit=5, momus=False, call=fake)
    assert len(asked.get("первый", [])) == 1 and len(asked.get("пересборка", [])) == 1, \
        f"первый тезис и пересборка перепутаны: { {k: len(v) for k, v in asked.items()} }"
    assert "Реестр отчётов показывает" in asked["первый"][0]
    text = fresh.read_text(encoding="utf-8")
    assert "тезис пересобран" not in text, "первому тезису записана ложная история"
    assert text.count("Реестр отчётов показывает отчёты") == 4, \
        "источник лёг в карточку не один раз"
    kept = (root / "AuroraKnowledgeDB/Concepts/Реестр-решений.md").read_text(encoding="utf-8")
    assert "тезис пересобран" in kept and old_thesis in kept, \
        "настоящая пересборка потеряла историю и прежний тезис"

    # ремонт: ложная запись (прежний тезис — сам источник) снимается, настоящая остаётся
    body = "# Карточка\n\n" + source.strip()
    false = ("Тезис.\n\n" + R.QUOTES + "\n" + body + "\n\n" + R.FOOTER + "\n\n"
             "- 2026-08-23: тезис пересобран — источник изменился (`Sources/a.md`). "
             "Добавлены шаги.\n  <details><summary>прежний тезис</summary>\n\n"
             + "\n".join("  " + l for l in body.splitlines()) + "\n\n  </details>\n")
    card(root, "Concepts/Ложная-история.md", status="draft", kind="knowledge",
         distilled="2026-08-23", body=false)
    run("kb_fix.py", "--frontmatter", "--apply", cwd=root)
    fixed = (root / "AuroraKnowledgeDB/Concepts/Ложная-история.md").read_text(encoding="utf-8")
    assert "тезис пересобран" not in fixed and R.FOOTER not in fixed, \
        "ложная история первого тезиса осталась"
    assert fixed.count("Реестр отчётов показывает отчёты") == 4 and "Тезис." in fixed, \
        "ремонт задел тезис или сам источник"
    again = (root / "AuroraKnowledgeDB/Concepts/Реестр-решений.md").read_text(encoding="utf-8")
    assert "тезис пересобран" in again, "ремонт снял настоящую историю пересборки"


@test
def test_thinking_calls_get_a_deadline_that_fits_the_thinking(tmp: Path):
    """Срок вызова с рассуждениями — по скорости шлюза, а не жёсткие 300 с.

    Замер PRJ-A 22.09.2026 без обрыва: рассуждение с тезисом — 7–26 тыс. токенов, 160–470 с
    при ~50 ток/с под нагрузкой. Жёсткий срок в 300 с обрывал половину тезисов на
    середине: работа выбрасывалась, попытка шла заново, уходила на запасную машину с
    другой моделью или карточка падала «сбоем» до следующего оборота. На 16 карточках
    вызовы тезиса шли 8200 с, из них генерации — 2000.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    A = importlib.import_module("agent_core")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_REQUEST_TIMEOUT": "300"})
    saved = dict(A.USAGE)
    try:
        A.USAGE.update(tokens_out=0, gen_seconds=0.0)
        assert A.request_timeout_for(cfg, False) == 300, "срок без рассуждений изменился"
        assert A.request_timeout_for(cfg, True) == 1200, \
            "без замера скорости срок не потолок — днём первые длинные тезисы оборвутся"
        A.USAGE.update(tokens_out=50 * 600, gen_seconds=600.0)        # 50 ток/с
        assert A.request_timeout_for(cfg, True) == 600, "срок не по скорости шлюза"
        A.USAGE.update(tokens_out=5 * 600, gen_seconds=600.0)         # 5 ток/с — шлюз еле жив
        assert A.request_timeout_for(cfg, True) == 1200, \
            "у срока нет потолка — зависший шлюз держал бы слот вечно"
        A.USAGE.update(tokens_out=500 * 600, gen_seconds=600.0)       # быстрый шлюз
        assert A.request_timeout_for(cfg, True) == 300, "срок стал короче настроенного"
        off = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                              "AURORA_AGENT_THINKING_WORKER": "0"})
        assert A.call_budget(off, "worker") == off["request_timeout"], \
            "роли без рассуждений дан срок рассуждающей"
    finally:
        A.USAGE.clear(); A.USAGE.update(saved)
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert 'time.time() + cfg["request_timeout"])' not in src, \
        "срок шага снова жёсткий — рассуждение оборвётся на середине"
    core = (SCRIPTS / "agent_core.py").read_text(encoding="utf-8")
    assert "request_timeout or request_timeout_for(cfg, think)" in core, \
        "вызов модели берёт срок мимо общего правила"


@test
def test_an_undeclared_backend_gets_one_slot_when_others_declare_width(tmp: Path):
    """Бэкенд без объявленной ширины не получает весь остаток потолка, если ширину объявили другим.

    Живой случай, PRJ-A 22.09.2026: у №1 объявлено 16, общий потолок 99, в параллель включили
    №2 — сервер llama.cpp с одним слотом, ширина не объявлена. `pool()` отдал ему 99 − 16 = 83
    потока: очередь на его стороне, 99 карточек подряд упали по сроку, переосмысление встало.
    Кто объявил ширину хоть одному шлюзу, знает свои серверы: про необъявленный он ничего не
    сказал — ему один слот. Кто не объявил ширину никому, получает прежний раздел потолка.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_WIDTH": "16",
                          "AURORA_AGENT_BACKEND_2_URL": "http://b", "AURORA_AGENT_BACKEND_2_MODEL": "m",
                          "AURORA_AGENT_BACKEND_2_PARALLEL": "1", "AURORA_AGENT_PARALLEL": "99"})
    slots = A.pool(cfg)
    assert slots.count(1) == 16 and slots.count(2) == 1, \
        f"необъявленный бэкенд получил остаток потолка: №2 × {slots.count(2)}"
    A._SEM.clear()
    b2 = [b for b in cfg["backends"] if b["n"] == 2][0]
    assert A._slot_semaphore(b2, cfg)._value == 1, "канал к необъявленному шире одного запроса"
    A._SEM.clear()
    # потолок меньше суммы — необъявленный не пролезает сверх потолка
    tight = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                            "AURORA_AGENT_BACKEND_1_WIDTH": "4",
                            "AURORA_AGENT_BACKEND_2_URL": "http://b", "AURORA_AGENT_BACKEND_2_MODEL": "m",
                            "AURORA_AGENT_PARALLEL": "4"})
    assert A.pool(tight) == [1, 1, 1, 1], f"потолок нарушен: {A.pool(tight)}"
    # прежние настройки: ширину не объявил никто — делят потолок
    old = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_2_URL": "http://b", "AURORA_AGENT_BACKEND_2_MODEL": "m",
                          "AURORA_AGENT_PARALLEL": "6"})
    assert sorted(A.pool(old)) == [1, 1, 1, 2, 2, 2], f"прежний раздел потолка сломан: {A.pool(old)}"


@test
def test_extract_and_relink_hand_out_work_by_slots(tmp: Path):
    """Вынос и связывание раздают задания по слотам пула, как переосмысление.

    Живой случай, PRJ-A 22.09.2026: `run_extract` и `run_relink` звали карточку без `prefer`.
    Все потоки начинали с первого шлюза, а кольцо без `prefer` тянуло №3, которому в настройке
    запрещены и параллель, и подмена. Связывание в итоге упёрлось в свою же перегрузку.
    Срок карточки выноса был фиксированным (2 × request_timeout = 600 с) и обрывал рассуждение.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    thesis = ("Реестр налогоплательщиков хранит сведения о каждом плательщике и его статусе. "
              "Реестр обновляется ежедневно из подсистемы учёта и сверяется с отчётами. " * 3)
    for i in range(6):
        card(root, f"Concepts/Карточка-{i}.md", status="draft", kind="knowledge",
             distilled="2026-09-01", body=thesis)
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_WIDTH": "2",
                          "AURORA_AGENT_BACKEND_2_URL": "http://b", "AURORA_AGENT_BACKEND_2_MODEL": "m",
                          "AURORA_AGENT_BACKEND_2_WIDTH": "2", "AURORA_AGENT_PARALLEL": "4"})
    seen = {"extract": [], "relink": []}
    deadlines = []
    import threading as _th
    lock = _th.Lock()

    def fake(kind):
        def call(cfg_, role, messages, prefer=0, deadline=None, **kw):
            with lock:
                seen[kind].append(prefer)
                if kind == "extract":
                    deadlines.append(deadline)
            text = '{"extract": []}' if kind == "extract" else "нет"
            return {"ok": True, "text": text, "backend": prefer or 1, "model": "m", "log": []}
        return call

    import time as _t
    t0 = _t.time()
    R.run_extract(cfg, str(root), False, call=fake("extract"))
    R.run_relink(cfg, str(root), False, call=fake("relink"))
    for kind in ("extract", "relink"):
        got = set(seen[kind])
        assert got == {1, 2}, f"{kind}: задания не розданы по слотам пула — prefer {sorted(got)}"
    budget = A.call_budget(cfg, "planner")
    assert all(d and d - t0 >= budget - 5 for d in deadlines), \
        "срок карточки выноса короче срока рассуждающего вызова — длинное рассуждение оборвётся"


@test
def test_parallel_build_keeps_the_console(tmp: Path):
    """Перехват вывода build_plan действует для своего потока и не глушит процесс.

    Живой случай, PRJ-A 22.09.2026: разбор в 7 потоков, `redirect_stdout` на весь процесс —
    подмены переплелись, и весь дальнейший вывод ушёл в чужой брошенный буфер: консоль
    оборвалась на 5 строках из ~40, пропали отчёт, итог и «Источников в плане: 7 → 0».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib, io, threading, time as _t
    R = importlib.import_module("agent_runner")
    real = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = real
    bufs = {}
    try:
        def worker(n):
            out, err = io.StringIO(), io.StringIO()
            for k in range(20):
                with R.capture_this_thread(out, err):
                    print(f"поток {n} внутри {k}")
                    _t.sleep(0.001)
                print(f"поток {n} снаружи {k}", file=sys.stderr)
            bufs[n] = out.getvalue()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(7)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        print("после разбора — итог прогона")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    text = real.getvalue()
    assert "после разбора — итог прогона" in text, "вывод процесса после разбора пропал"
    assert all(text.count(f"поток {n} снаружи") == 20 for n in range(7)), \
        "строки прогресса потоков потерялись"
    assert "внутри" not in text, "перехваченный вывод просочился в консоль"
    for n, got in bufs.items():
        assert got.count(f"поток {n} внутри") == 20 and "снаружи" not in got, \
            f"буфер потока {n} получил чужой вывод"
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    body = src.split("def run_build_plan(")[1].split("\ndef ")[0]
    assert "redirect_stdout" not in body and "capture_this_thread(out, err)" in body, \
        "build_plan снова перехватывается на весь процесс"


@test
def test_slow_answers_do_not_trip_the_dead_gateway_breaker(tmp: Path):
    """«Не уложился в срок» — живой шлюз: предохранитель «3 сбоя подряд» его не считает.

    Живой случай, PRJ-A 22.09.2026: связывание само загрузило обе локальные машины, ответы
    пошли медленно — и предохранитель «это шлюз, а не карточки» остановил шаг, оставив 428
    карточек без связей. А причина сбоя в отчёте была одна на всех — «дедлайн исчерпан»:
    что ответил каждый шлюз, по журналам было не восстановить.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")

    note = R.model_fail_note({"log": ["№1 m: не уложился в 600 с", "№3: слот занят (/slots) — дальше по кольцу",
                                      "круг 1 неудачен — пауза 10 с, снова с первого",
                                      "дедлайн исчерпан: ни один бэкенд не ответил осмысленно"]})
    assert "№1 m: не уложился в 600 с" in note and "№3: слот занят" in note and "дедлайн исчерпан" in note, \
        f"причина сбоя не называет бэкенды: {note}"

    def base(name, n):
        root = tmp / name
        kb = root / "AuroraKnowledgeDB/Concepts"
        kb.mkdir(parents=True)
        for i in range(n):
            (kb / f"К{i:02d}.md").write_text(
                f'---\ntitle: "К{i:02d}"\nkind: knowledge\nstatus: knowledge\n'
                f'distilled: 2026-09-01\n---\n\n' + "Тезис карточки про реестр. " * 12 + f"{i}\n",
                encoding="utf-8")
        return root

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1", "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    def slow(cfg_, role, messages, **kw):
        return {"ok": False, "timed_out": True,
                "log": ["№1 m: не уложился в 600 с", "никто не уложился в срок: 600 с на запрос"]}

    def dead(cfg_, role, messages, **kw):
        return {"ok": False, "log": ["№1: connection refused"]}

    # переосмысление: карточки без тезиса
    root = tmp / "d"
    kb = root / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True)
    for i in range(8):
        (kb / f"К{i}.md").write_text(f'---\ntitle: "К{i}"\nkind: knowledge\nstatus: knowledge\n'
                                     f'---\n\nтело {i}\n', encoding="utf-8")
    res = R.run_distill(cfg, str(root), apply=False, limit=8, momus=False, call=slow)
    assert len(res["steps"]) == 8 and not res["gateways_down"], \
        f"медленные ответы остановили переосмысление: пройдено {len(res['steps'])} из 8"
    assert all(s.get("slow") for s in res["steps"]), "сбой по сроку не помечен медленным"
    res = R.run_distill(cfg, str(root), apply=False, limit=8, momus=False, call=dead)
    assert res["gateways_down"] and len(res["steps"]) <= R.FAILS_IN_A_ROW, \
        "лежащий шлюз больше не останавливает переосмысление"

    # связывание
    res = R.run_relink(cfg, str(base("r1", 8)), False, call=slow)
    assert not res["gateways_down"], "медленные ответы остановили связывание"
    res = R.run_relink(cfg, str(base("r2", 8)), False, call=dead)
    assert res["gateways_down"], "лежащий шлюз больше не останавливает связывание"
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert '["log"][-2:]' not in src, "причина сбоя снова берётся последними строками журнала"


@test
def test_relink_keeps_links_when_the_model_also_edited_the_text(tmp: Path):
    """Пары модели переносятся в исходный тезис дословно — `rescue_links`.

    Живой случай, PRJ-A 22.09.2026: 23 из 187 ответов связывания отброшены целиком («текст
    изменён»), а карточка всё равно получала отметку «связано» и больше в очередь не
    возвращалась — без единой связи. Текст по-прежнему не меняется ни на символ.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    thesis = "Данные приходят из ФЦОД по расписанию. Итоговый ЭСФ считается ночью."
    got = "Данные поступают из [[ФЦОД]] по расписанию. [[Итоговый-ЭСФ|Итоговый ЭСФ]] считается ночью."
    out, n = R.rescue_links(got, thesis)
    assert n == 2 and out == ("Данные приходят из [[ФЦОД]] по расписанию. "
                              "[[Итоговый-ЭСФ|Итоговый ЭСФ]] считается ночью."), out
    assert R.strip_links(out) == R.strip_links(thesis), "перенос связей изменил текст"
    # середина слова и уже стоящая ссылка — не трогаем
    out, n = R.rescue_links("[[НДС]] и [[Реестр]]", "НДСный учёт; [[Реестр]] есть")
    assert n == 0 and out == "НДСный учёт; [[Реестр]] есть", out


@test
def test_extracted_definition_links_to_the_card_it_lands_in(tmp: Path):
    """Вынос ставит ссылку на имя заведённой карточки; имена — по канону; скобки не ломают ссылки.

    Живой случай, PRJ-A 22.09.2026: вынос ставил `[[Итоговый ЭСФ]]` при карточке
    «Итоговый-ЭСФ» (26 битых ссылок до хвоста маршрута), заводил «текущий-расчёт» со строчной,
    а карточка «…тип Option[String]» с «]» в имени осталась без единой ссылки.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    AC = importlib.import_module("aurora_common")

    assert R.canon_term("текущий расчёт") == "Текущий расчёт"
    assert R.canon_term("vat_report") == "vat_report", "регистр идентификатора из кода испорчен"
    assert "[" not in AC.card_filename("Семантика полей тип Option[String]") \
        and "|" not in AC.card_filename("а|б"), "имя карточки пропускает разметку ссылки"

    root = make_project(tmp)
    d1 = "синтетическая сущность для расчёта расхождений по периоду"
    d2 = "результат расчёта на текущую дату по всем записям реестра"
    thesis = ("Расчёт расхождений использует Итоговый ЭСФ, " + d1 + ". Ведётся текущий расчёт, "
              + d2 + ". Результаты сверяются с отчётами налогоплательщиков ежедневно.")
    card(root, "Processes/Расчет.md", status="draft", kind="knowledge", distilled="2026-09-01",
         sources='\n  - "Sources/Confluence/Расчет.md"', body=thesis)
    path = root / "AuroraKnowledgeDB/Processes/Расчет.md"
    plan = [{"term": "Итоговый ЭСФ", "definition": d1, "keep": ""},
            {"term": "текущий расчёт", "definition": d2, "keep": ""}]
    here = os.getcwd()
    try:
        os.chdir(root)
        text = path.read_text(encoding="utf-8")
        got = R.apply_extract_plan(str(root), str(path), text, R.thesis_of(text), plan, True)
    finally:
        os.chdir(here)
    assert sorted(got["made"]) == ["Итоговый ЭСФ", "текущий расчёт"], got
    left = path.read_text(encoding="utf-8")
    assert "[[Итоговый-ЭСФ|Итоговый ЭСФ]]" in left and "[[Текущий-расчёт|текущий расчёт]]" in left, \
        f"ссылка ведёт не на имя заведённой карточки:\n{left}"
    new = root / "AuroraKnowledgeDB/Concepts/Текущий-расчёт.md"
    assert new.is_file(), "русское понятие заведено со строчной"
    assert '"текущий расчёт"' in new.read_text(encoding="utf-8"), "прежнее написание не ушло в синонимы"
    lint = run("kb_lint.py", cwd=root).stdout
    assert "Итоговый ЭСФ]]" not in lint and "текущий расчёт]]" not in lint, \
        f"вынос оставил битые ссылки:\n{lint[-800:]}"

    # ремонт: уже заведённая карточка со скобками получает имя по правилу
    card(root, "Concepts/Тип-Option[String].md", status="draft", kind="knowledge",
         body="Поле может быть пустым.")
    run("kb_fix.py", "--names", "--apply", cwd=root)
    names = {p.name for p in (root / "AuroraKnowledgeDB/Concepts").glob("*.md")}
    assert "Тип-Option-String.md" in names and "Тип-Option[String].md" not in names, \
        f"карточка со скобками в имени не переименована: {sorted(names)}"

    flat = " ".join(R.PROMPT_EXTRACT.split())
    assert "значения статусов" in flat and "коды задач и историй" in flat, \
        "задание выноса снова не отличает сущность от статуса и кода"


@test
def test_a_data_literal_in_brackets_is_not_a_link(tmp: Path):
    """`[[01804201710137, 1, 0.5, Seller]]` из таблицы выгрузки — данные, а не ссылка.

    Живой случай, PRJ-A 22.09.2026: строка SQL-выгрузки, перенесённая из источника дословно,
    дала четыре «битые ссылки»; линтер звал их ошибкой, ремонт не брал — и база оставалась
    «с ошибками» после каждой починки. Правило одно для линтера и ремонта.
    """
    root = make_project(tmp)
    card(root, "Processes/Выгрузка.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body="Сверка идёт по [[Реестр]].\n\n|2 |2021-02-10 |[[01804201710137, 1, 0.500000000000000000, Seller]] |[] |\n")
    card(root, "Concepts/Реестр.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body="Реестр плательщиков. См. [[Выгрузка]].")
    out = run("kb_lint.py", cwd=root).stdout
    assert "01804201710137" not in out, f"литерал данных назван битой ссылкой:\n{out[-600:]}"
    fix = run("kb_fix.py", "--links", cwd=root).stdout
    assert "01804201710137" not in fix, "ремонт пытается чинить литерал данных как ссылку"
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    assert not AC.not_a_card_link("US-2.1.25") and not AC.not_a_card_link("1._Алгоритмы"), \
        "обычное имя карточки принято за литерал данных"


@test
def test_the_thesis_is_checked_by_the_primary_judge_not_its_own_slot(tmp: Path):
    """Тезис, написанный на слоте №2, проверяет основной шлюз, а не тот же №2.

    Живой случай, PRJ-A 22.09.2026: Момус получал `prefer` тезиса, и тезис 27b с коротким
    рассуждением судила та же 27b — самопроверка.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    kb = tmp / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True)
    p = kb / "К.md"
    p.write_text('---\ntitle: "К"\nkind: knowledge\nstatus: knowledge\n---\n\nРеестр хранит сведения.\n',
                 encoding="utf-8")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_2_URL": "http://b", "AURORA_AGENT_BACKEND_2_MODEL": "m"})
    asked = []

    def fake(cfg_, role, messages, prefer=0, **kw):
        asked.append((role, prefer))
        text = "ВЕРДИКТ: ЧИСТО" if role == "qa" else "Реестр хранит сведения."
        return {"ok": True, "text": text, "backend": prefer or 1, "model": "m", "log": []}

    R.distill_card(cfg, str(p), call=fake, momus=True, prefer=2)
    roles = dict(asked)
    assert roles.get("worker") == 2, f"тезис пошёл не на свой слот: {asked}"
    assert roles.get("qa") == 0, f"проверка пошла на слот, который писал тезис: {asked}"


@test
def test_extract_tells_the_cycle_how_many_cards_wait_for_a_thesis(tmp: Path):
    """Вынос сообщает остаток переосмысления вместе с карточками, которые он сам завёл.

    Живой случай, PRJ-A 22.09.2026: переосмысление отчиталось «осталось 0», после него вынос
    завёл две карточки, и цикл маршрута счёл работу законченной — две карточки остались без
    тезиса до следующего прогона. Цикл берёт последний «· осталось: N» оборота.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib, json as _json, re as _re
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    definition = "которая является частью проекта обработки платежей"
    thesis = ("Аналитический баланс получает информацию из подсистемы ФЦОД, " + definition +
              ". Баланс обновляется по факту поступления платежа и хранит остатки по счетам. "
              "Сверка проводится ежедневно.")
    card(root, "Concepts/Баланс.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body=thesis)
    plan = _json.dumps({"extract": [{"term": "ФЦОД", "definition": definition, "keep": ""}]},
                       ensure_ascii=False)

    def fake(cfg, role, messages, **kw):
        return {"ok": True, "text": plan, "backend": 1, "model": "m", "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    here = os.getcwd()
    try:
        os.chdir(root)
        res = R.run_extract(cfg, str(root), True, call=fake)
    finally:
        os.chdir(here)
    rep = R.report_extract(res, True)
    m = _re.search(r"·\s*осталось:\s*(\d+)", rep)
    assert m and int(m.group(1)) == 1, f"вынос не сказал, что новая карточка ждёт тезиса:\n{rep[:400]}"
    rr = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert 'r"·\\s*осталось:\\s*(\\d+)"' in rr, "цикл маршрута читает остаток по другому образцу"


@test
def test_until_done_names_the_real_remainder(tmp: Path):
    """Остановка заходов называет настоящий остаток очереди, а не остаток захода.

    Живой случай, PRJ-A 22.09.2026: «остаток 379 доделает следующий прогон» при 478 карточках в
    очереди — в сообщение шёл остаток захода, который не берёт уже пробованные карточки.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib, io, contextlib, types
    R = importlib.import_module("agent_runner")
    a = types.SimpleNamespace(hours=1.0, no_checkpoint=True)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        R.until_done(str(tmp), a, "distill",
                     lambda: {"left": 379, "steps": [{"status": "сбой"}], "gateways_down": True},
                     lambda r: "", lambda r: "", lambda r: 0, remaining=lambda: 478)
    said = err.getvalue()
    assert "остаток 478" in said and "379" not in said, f"назван остаток захода, а не очереди:\n{said}"


@test
def test_update_route_prunes_the_mirror_only_after_a_clean_export(tmp: Path):
    """«Обновить базу» убирает из зеркала удалённые страницы — но только после чистой выгрузки.

    Живой случай, PRJ-A 22.09.2026: синк без `--prune` оставлял зеркала удалённых страниц, и
    аудит каждый прогон находил те же ORPHAN 2. Но «лишнее» — это то, чего не нашёл обход:
    упала загрузка страницы — её поддерево выглядит удалённым. Такое чистить нельзя.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    C = importlib.import_module("confluence_export")
    assert C.prune_allowed(["a.md", "b.md"], 1064, 0) == (True, ""), "обычная чистка запрещена"
    ok, why = C.prune_allowed(["a.md"], 1064, 3)
    assert not ok and "ошибками" in why, "чистка после выгрузки с ошибками разрешена"
    ok, why = C.prune_allowed(["x.md"] * 300, 1064, 0)
    assert not ok and "слишком много" in why, "массовая чистка разрешена без человека"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    upd = scen.split("[update]")[1].split("\n[")[0]
    line = next(l for l in upd.splitlines() if l.startswith("sync:confluence"))
    assert line.rstrip().endswith("| --prune"), f"маршрут не убирает зеркала удалённых страниц: {line}"


@test
def test_a_moved_page_stays_one_source(tmp: Path):
    """Страница, переехавшая в Confluence, — тот же источник, а не второй.

    Живой случай, PRJ-B 24.09.2026: родительскую папку переименовали, синк положил
    страницу по новому пути, а старую копию оставил — чистку держал порог. Разбор счёл
    новый путь новым источником и дописал тот же текст в ту же карточку вторым блоком
    (27 карточек), а почти вся работа модели за прогон ушла на переписывание тезисов при
    неизменном знании.
    """
    root = make_project(tmp)
    old_a = _mirror_page(root, "Старая/Алгоритм.md", 111111, "Шаг один\nШаг два было")
    new_a = _mirror_page(root, "Новая/Алгоритм.md", 111111, "Шаг один\nШаг два стало")
    old_s = _mirror_page(root, "Старая/Справка.md", 222222, "Текст справки")
    new_s = _mirror_page(root, "Новая/Справка.md", 222222, "Текст справки")
    (root / "Sources/Confluence/Старая/Справка_assets").mkdir()
    (root / "Sources/Confluence/Старая/Справка_assets/схема.drawio").write_text("x", encoding="utf-8")
    _sync_state(root, [(111111, "Новая/Алгоритм.md"), (222222, "Новая/Справка.md")])
    _quoted_card(root, "Processes/Алгоритм.md", [old_a, new_a],
                 [(old_a, "Шаг один\nШаг два было"), (new_a, "Шаг один\nШаг два стало")],
                 "distilled: 2026-09-20\n")
    _quoted_card(root, "Processes/Справка.md", [old_s], [(None, "Текст справки")],
                 "distilled: 2026-09-20\n")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    B = importlib.import_module("build_plan")
    man = root / "AuroraKnowledgeDB/meta/manifest.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"sources": {
        old_a: {"hash": B.file_hash(str(root / old_a)), "cards": 1, "processed": "2026-09-01"},
        new_a: {"hash": B.file_hash(str(root / new_a)), "cards": 1, "processed": "2026-09-24"},
        old_s: {"hash": B.file_hash(str(root / old_s)), "cards": 1, "processed": "2026-09-01"},
    }}, ensure_ascii=False), encoding="utf-8")

    dry = run("kb_remap.py", "--moved", cwd=root, expect_rc=0).stdout
    assert "Переехавших страниц: 2" in dry and (root / old_a).is_file(), f"dry-run записал:\n{dry}"
    run("kb_remap.py", "--moved", "--apply", cwd=root, expect_rc=0)

    alg = (root / "AuroraKnowledgeDB/Processes/Алгоритм.md").read_text(encoding="utf-8")
    assert card_srcs(alg) == [new_a], f"старый путь остался в источниках: {card_srcs(alg)}"
    quoted = alg.split("## Источник (перенесено дословно)")[1].split("## История изменений")[0]
    assert quoted.count("Шаг один") == 1 and "было" not in quoted, f"текст страницы дважды:\n{quoted}"
    assert "distilled:" not in alg, "тезис писали и по прежнему тексту — отметка должна сняться"
    assert "второй экземпляр" in alg, "в истории карточки не сказано, что убрано"
    ref = (root / "AuroraKnowledgeDB/Processes/Справка.md").read_text(encoding="utf-8")
    assert card_srcs(ref) == [new_s] and "distilled: 2026-09-20" in ref, \
        f"переезд без правки текста не должен трогать тезис:\n{ref}"
    assert not (root / old_a).exists() and not (root / old_s).exists(), "старые копии в зеркале"
    assert not (root / "Sources/Confluence/Старая/Справка_assets").exists(), "осталась папка схем"
    recs = json.loads(man.read_text(encoding="utf-8"))["sources"]
    assert old_a not in recs and old_s not in recs, f"старые пути в учёте разбора: {list(recs)}"
    assert recs[new_s]["hash"] == B.file_hash(str(root / new_s)), \
        "страница с тем же текстом после переезда снова ушла бы в разбор"


@test
def test_build_replaces_the_block_of_the_same_page(tmp: Path):
    """Разбор кладёт страницу в карточку одним блоком — даже под новым путём.

    Три дефекта одной природы (PRJ-B 24.09.2026): (1) переехавшая страница дописывалась
    вторым блоком; (2) обновление одной страницы (`refresh_card`) стирало дословный текст
    всех остальных источников карточки; (3) заголовок «### …» внутри текста страницы
    делил её на два блока, и замена оставляла в карточке прежний хвост.
    """
    root = make_project(tmp)
    old = _mirror_page(root, "Старая/Понятие.md", 333333, "x")
    new = _mirror_page(root, "Новая/Понятие.md", 333333, "x")
    jira = "Sources/JIRA/PRJ-1.md"
    path = _quoted_card(root, "Concepts/Понятие.md", [old, jira],
                        [(old, "Абзац один\n\n### Подраздел\n\nхвост старый"),
                         (jira, "из задачи")], "distilled: 2026-09-20\n")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    B = importlib.import_module("build_plan")
    fresh = "Абзац один\n\n### Подраздел\n\nхвост новый"
    B.append_card(str(path), path.read_text(encoding="utf-8"), fresh, new, True, str(root))
    text = path.read_text(encoding="utf-8")
    assert card_srcs(text) == [new, jira], f"источники: {card_srcs(text)}"
    quoted = text.split("## Источник (перенесено дословно)")[1].split("## История изменений")[0]
    assert "хвост старый" not in quoted and quoted.count("Абзац один") == 1, \
        f"прежний текст страницы остался:\n{quoted}"
    assert "из задачи" in quoted and "distilled:" not in text
    # тот же текст ещё раз — карточка не меняется, тезис не снимается
    again = text.replace("type: process\n", "type: process\ndistilled: 2026-09-24\n")
    path.write_text(again, encoding="utf-8")
    B.append_card(str(path), again, fresh, new, True, str(root))
    assert path.read_text(encoding="utf-8") == again, "неизменный текст переписал карточку"
    # обновление другого источника меняет только его блок
    B.refresh_card(str(path), again, "из задачи, поправлено", jira, True, str(root))
    text = path.read_text(encoding="utf-8")
    assert "хвост новый" in text and "из задачи, поправлено" in text, \
        f"обновление одного источника стёрло текст другого:\n{text}"
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    merged = R.merge_same_target([{"title": "А", "sections": "1,2"}, {"title": "Б", "sections": "3"},
                                  {"into": "а", "sections": "5"}])
    assert merged == [{"title": "А", "sections": "1,2,5"}, {"title": "Б", "sections": "3"}], merged


@test
def test_sync_follows_moves_and_prunes_only_the_gone(tmp: Path):
    """Синк переводит базу за переехавшими страницами, а порог чистки считает только исчезнувшие.

    PRJ-B 24.09.2026: 63 «лишних» из 204, из них 57 — переезды. Порог (не больше 20
    или 10 %) держал чистку закрытой каждый прогон, а аудит зеркал каждый раз находил те же
    63 файла.
    """
    root = make_project(tmp)
    rows = []
    for i in range(30):
        _mirror_page(root, f"Старая/Стр{i}.md", 500000 + i, f"текст {i}")
        _mirror_page(root, f"Новая/Стр{i}.md", 500000 + i, f"текст {i}")
        rows.append((500000 + i, f"Новая/Стр{i}.md"))
    gone = _mirror_page(root, "Старая/Удалённая.md", 599999, "нет её")
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SCRIPTS)!r})
        import confluence_export as C
        C.read_config = lambda: {{"base_url": "https://wiki", "space": "S", "out": "Sources/Confluence",
                                 "roots": ["1"]}}
        C.read_secret = lambda: ("x", "токен")
        def fake(cfg, roots, out, auth, force):
            exp = C.Exporter(None, out, "https://wiki", "S", False)
            exp.records = [(str(pid), rel, "x", "SYNCED") for pid, rel in {rows!r}]
            return exp
        C.run_export = fake
        sys.argv = ["confluence_export.py", "--prune"]
        sys.exit(C.main())
    """)
    cp = subprocess.run([sys.executable, "-c", script], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    left = sorted(p.name for p in (root / "Sources/Confluence").rglob("*.md") if "Старая" in str(p))
    assert left == [], f"старые копии и удалённая страница остались: {left}\n{cp.stdout}"
    assert "Переехали страниц: 30" in cp.stdout and "Удалено: 1" in cp.stdout, cp.stdout
    assert not (root / gone).exists()


@test
def test_distill_report_lists_under_the_warning_only_flagged_cards(tmp: Path):
    """Под предупреждением Момуса — только помеченные карточки, переписанные — отдельно.

    Живой случай, PRJ-B 24.09.2026: под «Момус нашёл утверждений без опоры: 3. Эти
    карточки помечены…» стоял список всех 13 переписанных карточек без своего заголовка —
    у первых двух отметки не было, и человек пошёл бы проверять не те.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    steps = [{"card": "Чистая.md", "status": "переписана", "note": "тезис"},
             {"card": "Спорная.md", "status": "переписана", "note": "тезис", "unsupported": 2},
             {"card": "Ещё-спорная.md", "status": "переписана", "note": "тезис", "unsupported": 1}]
    rep = R.report_distill({"steps": steps, "left": 0, "unsupported": 3, "seconds": 1.0}, True)
    warn, _, rest = rep.partition("⚠️")[2].partition("## Переписанные тезисы")
    assert "Спорная.md" in warn and "Ещё-спорная.md" in warn and "Чистая.md" not in warn, \
        f"под предупреждением не те карточки:\n{rep}"
    assert all(n in rest for n in ("Чистая.md", "Спорная.md", "Ещё-спорная.md")), rep


@test
def test_rewritten_thesis_is_examined_only_in_its_new_part(tmp: Path):
    """Вынос после переписанного тезиса читает только новые предложения.

    Прогон PRJ-B 24.09.2026: 40 карточек с переписанным тезисом осмотрены целиком за
    15 минут — новых определений ноль (PRJ-A: 0 из 13 и 0 из 14 при повторных осмотрах).
    Прежние предложения модель уже видела и решила; нового текста нет — модель не зовётся.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    old = ("Реестр деклараций показывает все поданные декларации за выбранный период. "
           "Сортировка идёт по дате подачи, новые сверху, по двадцать строк на странице.")
    new_line = "Фильтр по инспекции ограничивает список декларациями своего региона."
    hist = ("\n\n## Источник (перенесено дословно)\n\nтекст страницы"
            "\n\n## История изменений\n\n- 2026-09-24: тезис пересобран — источник изменился. "
            "Добавлен фильтр.\n  <details><summary>прежний тезис</summary>\n\n  " + old
            + "\n\n  </details>\n")
    cfg = {"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
           "thinking_roles": {}, "thinking": False, "backends": []}
    seen = []

    def fake(cfg, role, messages, **kw):
        seen.append(messages[0]["content"])
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": '{"extract": []}'}

    card(root, "Concepts/Реестр.md", status="draft", kind="knowledge", distilled="2026-09-24",
         extracted="2026-09-20", body=old + " " + new_line + hist)
    path = root / "AuroraKnowledgeDB/Concepts/Реестр.md"
    R.extract_card(cfg, str(path), fake, apply=True)
    assert len(seen) == 1 and new_line in seen[0] and "по двадцать строк" not in seen[0], \
        "модели ушёл весь тезис, а не новая часть"
    card(root, "Concepts/Реестр-2.md", status="draft", kind="knowledge", distilled="2026-09-24",
         extracted="2026-09-20", body=old + hist)
    path2 = root / "AuroraKnowledgeDB/Concepts/Реестр-2.md"
    st = R.extract_card(cfg, str(path2), fake, apply=True)
    assert len(seen) == 1, "тезис без нового текста снова ушёл модели"
    assert "extracted: 2026-09-24" in path2.read_text(encoding="utf-8"), st
