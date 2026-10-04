"""Проверки движка Aurora, часть 17: контроль PRJ-C после облачных выпусков 1.147–1.150.

Каркас и помощники — tests/harness.py. Здесь — то, что нашла сверка живой базы PRJ-C
4.10.2026 с выпусками, сделанными параллельно: шаг, который падает, и маршрут, который
этого не видит.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from harness import SCRIPTS, card, make_project, run, test  # noqa: F401


@test
def test_a_crashing_engine_script_exits_with_code_three(tmp: Path):
    """Необработанная ошибка скрипта движка — код 3, а не 1.

    Код 1 у движка — «есть находки» (линтер, ремонт), и маршрут идёт дальше. Трассировка
    Python тоже даёт 1: на PRJ-C 1.10.2026 `kb:repair --stub-text` трижды падал внутри
    «Починить базу», а маршрут каждый раз рапортовал «пройден». Код 3 маршрут и итог
    называют упавшим шагом.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    code = ("import sys; sys.path.insert(0, %r); import aurora_common; raise RuntimeError('x')"
            % str(SCRIPTS))
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert cp.returncode == 3, (cp.returncode, cp.stderr[-300:])
    assert "RuntimeError" in cp.stderr, "трассировка пропала — причину не прочесть"
    RS = importlib.import_module("run_summary")
    got = RS.route(str(tmp), "", 1.0, [{"cmd": "kb:repair", "rc": 3}])
    assert any("упал" in k for k in got["data"]["errors"]), got["data"]["errors"]


@test
def test_returning_a_stub_its_look_survives_the_whole_repair(tmp: Path):
    """`kb:repair --all --stub-text` проходит до конца на заготовке с историей.

    После того как `--stub-text` вернул заготовке её вид, заголовок дословного раздела
    оставался только в истории изменений («прежний тезис»), и `drop_false_redistill` в шаге
    `--frontmatter` падал с `IndexError`: ни одна из 121 заготовки PRJ-C не починилась.
    """
    root = make_project(tmp)
    card(root, "Concepts/Авторизация.md",
         "Авторизация — заготовка понятия, в которой ссылка уже есть.\n\n"
         "## Источник (перенесено дословно)\n\n# Авторизация\n\n"
         "_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n\n"
         "## История изменений\n\n- 2026-08-30: тезис пересобран — источник изменился\n"
         "  <details><summary>прежний тезис</summary>\n\n"
         "  ## Источник (перенесено дословно)\n\n  # Авторизация\n\n  </details>\n",
         status="draft", tags="[заготовка]", distilled="2026-08-30")
    cp = run("kb_fix.py", "--all", "--stub-text", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1), cp.stderr[-600:]
    assert "Traceback" not in cp.stderr, cp.stderr[-600:]
    text = (root / "AuroraKnowledgeDB/Concepts/Авторизация.md").read_text(encoding="utf-8")
    assert "status: placeholder" in text and "заготовка понятия, в которой" not in text, text


@test
def test_the_test_run_never_reads_the_developers_personal_kit_files(_t):
    """Прогон тестов не читает личные файлы настоящего кита — ни ключи шлюзов, ни MCP.

    Изоляция агента ловила это для вызова без кита, а панель передаёт кит явно: на машине
    разработчика три теста английского режима видели его MCP-сервер и кольцо эмбеддингов и
    краснели, а в CI были зелёными (4.10.2026). Тесту, которому нужна настройка машины,
    положено заводить свой временный кит — его файлы читаются как обычно.
    """
    import importlib
    import os
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    kit = os.path.dirname(str(SCRIPTS))
    assert os.environ.get("AURORA_TESTS_ISOLATED"), "прогон идёт без изоляции"
    assert A.personal_kit_file(os.path.join(kit, A.ENV_FILE))
    assert A.personal_kit_file(os.path.join(kit, "local", "mcp.json"))
    assert A.load_env(os.path.join(kit, A.ENV_FILE)) == {}
    assert not A.personal_kit_file(os.path.join(str(_t), A.ENV_FILE)), \
        "временный кит теста объявлен личным — тесты настроек машины ослепнут"


@test
def test_the_windows_launcher_keeps_crlf_in_the_projects_git(tmp: Path):
    """Пусковой .bat проекта попадает в git с CRLF, а не с LF.

    Кит пишет start-aurora.bat с CRLF, но у проектов не было .gitattributes: git с
    core.autocrlf=input (обычная настройка Mac) сохранял файл с одними LF. В PRJ-A он так и
    лежал в истории 4.10.2026 — на Windows из такого клона cmd.exe промахивается мимо меток
    goto. Правило кладут и установка, и обновление движка.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    U = importlib.import_module("aurora_update")
    bat = b"@echo off\r\ngoto :start\r\n:start\r\necho ok\r\n"

    def staged(repo: Path) -> bytes:
        repo.mkdir(exist_ok=True)
        (repo / "start-aurora.bat").write_bytes(bat)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=input", "add", "-A"],
                       check=True, capture_output=True)
        return subprocess.run(["git", "-C", str(repo), "cat-file", "blob", ":start-aurora.bat"],
                              check=True, capture_output=True).stdout

    assert b"\r\n" not in staged(tmp / "без правила"), \
        "без правила git должен был срезать CRLF — проверка ничего не ловит"

    repo = tmp / "проект"
    repo.mkdir()
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    added = U.refresh_gitattributes(repo)
    assert added == ["/start-aurora.bat -text"], added
    assert U.refresh_gitattributes(repo) == [], "повторное обновление дописало правило ещё раз"
    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in attrs, "строка человека потерялась"
    assert staged(repo) == bat, "пусковой файл ушёл в git проекта не байт в байт"

    upd = (SCRIPTS / "aurora_update.py").read_text(encoding="utf-8")
    inst = (SCRIPTS / "install_aurora.py").read_text(encoding="utf-8")
    assert "refresh_gitattributes(target)" in upd, "обновление движка не кладёт правило"
    assert '".gitattributes", GITATTRIBUTES_BLOCK' in inst, "установка не кладёт правило"


@test
def test_a_thin_source_goes_back_to_the_plan_once_per_text(tmp: Path):
    """«Починить базу» возвращает тонко разобранное в план, а не только перечисляет его.

    На PRJ-C 4.10.2026 отчёт `--thin` назвал 236 источников и ничего с ними не сделал: на
    человеке оставалось решать, что перечитывать. Теперь возврат — шаг маршрута, и он не
    крутится: перечитанный и всё равно тонкий источник — законно короткий пересказ, и
    второй раз он не возвращается, пока его текст не изменится. Отозванную авторами
    страницу перечитывать незачем вовсе; за проход — не больше `THIN_REOPEN_CAP`.
    """
    root = make_project(tmp)
    body = "".join(f"## Раздел {i}\n\n" + "текст " * 400 + "\n\n" for i in range(12))

    def thin_source(name: str) -> str:
        rel = f"Sources/Confluence/{name}.md"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f'---\ntitle: "{name}"\n---\n\n' + body, encoding="utf-8")
        card(root, f"Concepts/Из-{name}.md", "знание", source=f'"{rel}"')
        run("build_plan.py", "--done", rel, "--cards", "1", cwd=root)
        return rel

    fat = thin_source("Толстая")
    retired = thin_source("[не_используем]ALG-029")
    report = run("build_plan.py", "--thin", cwd=root).stdout
    assert "Толстая.md" in report, report[:400]
    assert "не_используем" not in report, "отозванная авторами страница пошла на перечитку"

    run("build_plan.py", "--thin", "--reopen", "--apply", cwd=root)
    assert "Толстая.md" in run("build_plan.py", cwd=root).stdout, "тонкий источник не вернулся"
    run("build_plan.py", "--done", fat, "--cards", "1", cwd=root)
    again = run("build_plan.py", "--thin", cwd=root).stdout
    assert "Толстая.md" not in again, "перечитанный источник возвращается по кругу"
    (root / fat).write_text((root / fat).read_text(encoding="utf-8") + "\n## Новое\n\nтекст\n",
                            encoding="utf-8")
    run("build_plan.py", "--done", fat, "--cards", "1", cwd=root)
    assert "Толстая.md" in run("build_plan.py", "--thin", cwd=root).stdout, \
        "изменившийся источник больше не проверяется на тонкость"
    assert retired

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    cap = importlib.import_module("build_plan").THIN_REOPEN_CAP
    for i in range(cap + 2):
        thin_source(f"Ещё-{i:02d}")
    out = run("build_plan.py", "--thin", "--reopen", "--apply", cwd=root).stdout
    assert f"Возвращено в план: {cap}" in out and "в следующие проходы" in out, out[-400:]
    scen = (SCRIPTS.parent / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert "| --thin --reopen --apply" in scen, "«Починить базу» снова только отчитывается"


@test
def test_linking_an_unchanged_base_again_costs_nothing(tmp: Path):
    """Оборот без новых карточек не пересчитывает связи всей базы заново.

    На PRJ-C 30.09 оборот, в котором разбор не дал ни одной карточки, всё равно гонял
    `kb:links --cards` по всей базе — 72 секунды ради того же результата. Теперь шаг
    сверяет отпечаток базы и зеркал с прошлым записанным проходом; изменилась хоть одна
    карточка — считает заново.
    """
    root = make_project(tmp, git=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    card(root, "Concepts/Альфа.md", "Про [[Бета]].")
    card(root, "Concepts/Бета.md", "Про [[Альфа]].")
    first = run("kb_graph.py", "--cards", "--apply", "--allow-dirty", cwd=root).stdout
    assert "без изменений" not in first, first
    again = run("kb_graph.py", "--cards", "--apply", "--allow-dirty", cwd=root).stdout
    assert "без изменений" in again, "неизменная база связывается заново:\n" + again
    card(root, "Concepts/Гамма.md", "Про [[Альфа]].")
    third = run("kb_graph.py", "--cards", "--apply", "--allow-dirty", cwd=root).stdout
    assert "без изменений" not in third, "новая карточка не попала в связывание"
    dry = run("kb_graph.py", "--cards", cwd=root).stdout
    assert "без изменений" not in dry, "просмотр без записи должен считать, а не ссылаться"


@test
def test_a_link_to_a_mirror_page_becomes_a_link_to_its_code(tmp: Path):
    """Ссылка на страницу истории по имени файла зеркала ведёт на код истории.

    В PRJ-C 4.10.2026 после «Починить базу» осталась одна битая ссылка:
    `[[US-3.6.28._Приём_и_обработка_…|AC-3.6.28]]` — модель записала имя файла страницы,
    а узел базы для истории — её код. Ремонт ссылок переписывает её на `[[US-3.6.28|…]]`,
    и `kb:moc --by-code` того же маршрута заводит индекс коду, на который сослались.
    """
    root = make_project(tmp, git=True)
    page = root / "Sources/Confluence/Epic_3.6/US-3.6.28._Приём_платежей.md"
    page.parent.mkdir(parents=True)
    page.write_text('---\ntitle: "US-3.6.28. Приём платежей"\n---\n\nТекст истории.\n',
                    encoding="utf-8")
    card(root, "Concepts/Баланс.md",
         "Баланс ведёт система ([[US-3.6.28._Приём_платежей|AC-3.6.28]]).")
    run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    text = (root / "AuroraKnowledgeDB/Concepts/Баланс.md").read_text(encoding="utf-8")
    assert "[[US-3.6.28|AC-3.6.28]]" in text, text
    run("kb_moc.py", "--by-code", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    assert (root / "AuroraKnowledgeDB/MOC/US-3.6.28.md").is_file(), \
        "индекс кода не заведён — ссылка осталась битой"
    lint = run("kb_lint.py", cwd=root, expect_rc=None).stdout
    assert "US-3.6.28" not in lint, lint[-600:]


@test
def test_a_card_left_without_its_page_goes_to_the_archive(tmp: Path):
    """Машинная карточка, чья страница ушла из зеркала, не висит в базе без опоры.

    PRJ-A 4.10.2026: страница о внутреннем учёте времени ушла из Confluence, ремонт снял
    источник, и две карточки месяц стояли «без класса» — ни доверия, ни способа проверить.
    Решено: такая карточка уходит в архив, ссылки на неё становятся словами. Тема папки,
    которую переименовали (PRJ-B: «Налоговые_декларации» → «ALG_Налоговые_декларации»),
    сводится с темой новой папки: синоним и ссылки переходят к живой. Слово человека
    держит карточку в базе.
    """
    root = make_project(tmp, git=True)
    new_dir = root / "Sources/Confluence/Алгоритмы/ALG_Налоговые_декларации"
    new_dir.mkdir(parents=True)
    (new_dir / "Страница.md").write_text("---\ntitle: x\n---\n\nтекст\n", encoding="utf-8")
    src = lambda p: f'\n  - "{p}"'
    card(root, "Concepts/ALG-Налоговые-декларации.md", "Тема папки.", built="machine",
         tags="[тема]", sources=src("Sources/Confluence/Алгоритмы/ALG_Налоговые_декларации"))
    card(root, "Concepts/Налоговые-декларации.md", "Тема папки.", built="machine",
         tags="[тема]", sources=src("Sources/Confluence/Алгоритмы/Налоговые_декларации"))
    card(root, "Processes/Учёт-времени.md", "Учёт времени по неделям.", built="machine",
         kind="knowledge", sources=src("Sources/Confluence/Команда/Учёт.md"))
    card(root, "Processes/Отпуск.md", "Отпуск.\n\n## Исправления человеком\n\n- верно\n",
         built="machine", kind="knowledge", sources=src("Sources/Confluence/Команда/Отпуск.md"))
    card(root, "Concepts/Читатель.md",
         "См. [[Налоговые-декларации]] и [[Учёт-времени|учёт]].", built="machine")
    out = run("kb_fix.py", "--gone-sources", "--apply", "--allow-dirty", cwd=root).stdout
    kb = root / "AuroraKnowledgeDB"
    assert "сведена в тему ALG-Налоговые-декларации" in out, out[-800:]
    assert (kb / "_archive/Налоговые-декларации.md").is_file(), "прежняя тема осталась в базе"
    assert (kb / "_archive/Учёт-времени.md").is_file(), "карточка без опоры осталась в базе"
    assert (kb / "Processes/Отпуск.md").is_file(), "слово человека не удержало карточку"
    reader = (kb / "Concepts/Читатель.md").read_text(encoding="utf-8")
    assert "[[ALG-Налоговые-декларации]]" in reader, reader
    assert "[[Учёт-времени" not in reader and "учёт" in reader, reader
    live = (kb / "Concepts/ALG-Налоговые-декларации.md").read_text(encoding="utf-8")
    assert "Налоговые-декларации" in live.split("---")[1], "прежнее имя не стало синонимом"
    gone = (kb / "_archive/Учёт-времени.md").read_text(encoding="utf-8")
    assert "знание ушло вместе с ней" in gone, "в истории не сказано, почему карточка ушла"


@test
def test_the_golden_reference_follows_its_lost_targets_by_meaning(tmp: Path):
    """Эталон с пропавшими целями: имя — механикой, остальное — модель по ответу, не по похожести.

    На PRJ-C 14 контрольных вопросов месяц указывали на пропавшие карточки: строки принимал
    только человек, и замер качества мерил пустоту. 4.10.2026 пользователь передал это
    движку. Цель, сменившая имя (слияние с `superseded_by`), переводится сразу; для остальных
    модель смотрит кандидатов и переписывает строку, только если ответ в тексте есть. Нет —
    строка остаётся: потерянное знание должно быть видно.
    """
    import json
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    AG = importlib.import_module("agent_core")
    root = make_project(tmp, git=True)
    card(root, "Concepts/Движение-платежа.md",
         "Платёж проходит путь от банка до казначейства через расчётный центр.",
         status="knowledge", kind="knowledge")
    arch = root / "AuroraKnowledgeDB/_archive"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "Старое-имя.md").write_text('---\ntitle: "Старое-имя"\nstatus: deprecated\n'
                                        'superseded_by: "[[Движение-платежа]]"\n---\n\nx\n',
                                        encoding="utf-8")
    gold = root / "AuroraKnowledgeDB/meta/golden_questions.md"
    gold.parent.mkdir(parents=True, exist_ok=True)
    gold.write_text("# Эталон\n\n| # | Вопрос | Эталон | Карточки |\n|---|---|---|---|\n"
                    "| 1 | Какой путь проходит платёж до казначейства? | от банка до казначейства "
                    "| [[Путь-платежа-ОП]] |\n"
                    "| 2 | Сколько стоит обслуживание платежа в банке? | бесплатно | [[Тарифы]] |\n"
                    "| 3 | Как движется платёж по счетам? | через расчётный центр | [[Старое-имя]] |\n",
                    encoding="utf-8")
    asked = []

    def model(c, role, messages, **kw):
        text = messages[0]["content"]
        asked.append(text)
        pick = "Движение-платежа" if "Какой путь" in text else ""
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": json.dumps({"card": pick, "why": "ответ в тексте" if pick else "нет"},
                                   ensure_ascii=False)}

    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}
    res = R.run_golden(cfg, str(root), True, call=model)
    text = gold.read_text(encoding="utf-8")
    assert res["moved"] == 1, res
    assert "| [[Движение-платежа]] |\n| 2" in text, "строка 1 не переведена:\n" + text
    assert "[[Тарифы]]" in text, "строка без ответа в кандидатах переписана — прибор подогнан"
    assert "через расчётный центр | [[Движение-платежа]]" in text, "слияние не прослежено"
    assert not any("Как движется" in a for a in asked), "модель спрашивали о решённом механикой"
    allowed, _why = AG.write_allowed("kb_search_quality.py",
                                     ["--golden-remap", "--accept", "1", "--apply"])
    assert not allowed, "агенту дали принимать строку по одной похожести"
    scen = (SCRIPTS.parent / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert scen.index("agent:golden") < scen.index("ops:search-quality | "), \
        "эталон чинится после замера — замер мерит пустоту"
