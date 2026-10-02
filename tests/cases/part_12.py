"""Проверки движка Aurora, часть 12 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time

from harness import (  # noqa: F401
    ui_source,
    KIT,
    SCRIPTS,
    _cockpit_on,
    card,
    make_project,
    panel_sources,
    run,
    test,
    why,
)


@test
def test_extraction_does_not_write_into_dictionary_cards(tmp: Path):
    """Вынос не дописывает текст в словарную карточку и в карточку документа.

    Прогон PRJ-B 24.09.2026: вынос нашёл определения «КПП» и «ИНН» и понёс их в
    `Glossary/КПП.md` (kind: dictionary). Тогда определения там уже были, и встали только
    ссылки; но при другом тексте движок дописал бы абзац модели в выгрузку справочника.
    Определения там нет — кусок остаётся на месте, а не теряется.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    definition = "то есть код причины постановки на учёт из девяти цифр"
    thesis = ("Декларация подаётся по каждому КПП, " + definition + ". Срок подачи — до "
              "двадцатого числа месяца, следующего за налоговым периодом, для всех плательщиков "
              "налога, кроме тех, кто перешёл на упрощённую систему по заявлению.")
    card(root, "Glossary/КПП.md", "КПП — справочник кодов причин постановки.", kind="dictionary",
         status="knowledge")
    donor = card(root, "Concepts/Декларация.md", thesis, kind="knowledge", status="draft",
                 distilled="2026-09-24")

    def fake(cfg, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": json.dumps({"extract": [{"term": "КПП", "definition": definition,
                                                 "keep": ""}]}, ensure_ascii=False)}

    cfg = {"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
           "thinking_roles": {}, "thinking": False, "backends": []}
    before = (root / "AuroraKnowledgeDB/Glossary/КПП.md").read_text(encoding="utf-8")
    st = R.extract_card(cfg, str(donor), fake, apply=True)
    assert (root / "AuroraKnowledgeDB/Glossary/КПП.md").read_text(encoding="utf-8") == before, \
        "в словарную карточку дописан текст модели"
    assert definition in donor.read_text(encoding="utf-8"), "определение вырезано и потеряно"
    assert "словаря или документа" in st["note"], st


@test
def test_update_route_spends_no_time_on_idle_steps(tmp: Path):
    """Скорость «Обновить базу»: волна разбора на весь пул, холостые шаги — даром.

    Прогон PRJ-B 24.09.2026: лимит 15 источников за запуск при пуле в 24 потока — 42
    источника пошли в три оборота всего цикла; синонимы без единого конфликта стоили 40 с
    (оракул дважды гонял линтер всей базы); каждый коммит маршрута — 20 с, из них 11 —
    линтер всей базы в хуке при снятом храповике.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_PARALLEL": "24"})
    assert R.lap_steps(cfg) == 24, f"волна разбора уже пула: {R.lap_steps(cfg)}"
    one = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    assert R.lap_steps(one) == one["max_steps"], "в один поток лимит шагов изменился"
    root = make_project(tmp, git=True)
    g = lambda *args: subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    g("add", "-A"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    before = len(g("log", "--oneline").stdout.splitlines())
    env = {**os.environ, "AURORA_TESTS_ISOLATED": "1",
           "AURORA_AGENT_BACKEND_1_URL": "http://127.0.0.1:9/v1", "AURORA_AGENT_BACKEND_1_MODEL": "m"}
    cp = subprocess.run([sys.executable, str(root / ".opencode/scripts/agent_runner.py"),
                         "--task", "aliases", "--apply", "--critic"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    assert cp.returncode == 0 and "Конфликтов синонимов: 0" in cp.stdout, cp.stdout + cp.stderr
    assert "Оракул" not in cp.stdout and len(g("log", "--oneline").stdout.splitlines()) == before, \
        "холостые синонимы гоняли оракула или сделали коммит"
    # хук при снятом храповике базу не проверяет: линтер-заглушка оставила бы след
    lint = root / ".opencode/scripts/kb_lint.py"
    lint.write_text("open('lint-ran', 'w').write('1')\nprint('карточек 1 · ошибок 0')\n",
                    encoding="utf-8")
    hooks = subprocess.run([sys.executable, str(SCRIPTS / "aurora_hooks.py"), "--install", "--force"],
                           cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert hooks.returncode == 0, hooks.stdout + hooks.stderr
    (root / "lint-ran").unlink(missing_ok=True)        # установка сама меряет планку
    (root / "AuroraKnowledgeDB/Concepts").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/Concepts/Проба.md").write_text("# Проба\n", encoding="utf-8")
    g("add", "-A")
    done = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "оборот"],
                          cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "AURORA_SKIP_RATCHET": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (root / "lint-ran").exists(), "при снятом храповике хук гонял линтер"


@test
def test_empty_cards_that_hold_nothing_go_to_the_archive(tmp: Path):
    """Пустая карточка, которая ничего не держит, уходит в архив; слово прописными — не сокращение.

    Проверка карточек, где нет ничего, кроме служебного (PRJ-B и PRJ-C 24.09.2026): заготовки
    под ссылки прежних карточек, на которые никто больше не ссылается; заготовки под
    «сокращения» «НАЛОГОВ», «ГОДА», «WHERE» — слова из заголовков, набранных прописными;
    пустая заметка «схема.png.md», которую Obsidian заводит по щелчку на картинке. Заготовку,
    в которую уже пришло знание (есть источник), ремонт не трогает. Поля `owner`/`verified`
    не защищают: их ставила машинная «приёмка», а не человек.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    stub = ("---\ntitle: \"{n}\"\naliases: []\nstatus: placeholder\ntype: concept\n"
            "tags: [заготовка]\n{extra}---\n\n# {n}\n\n{mark}\n")
    link_mark = "_Заготовка: ссылка на это понятие уже есть, знания пока нет._"
    term_mark = "_Заготовка: имя названо в базе, расшифровки база пока не знает, знания нет._"
    for name, mark, extra in (("Забытая", link_mark, ""), ("Нужная", link_mark, ""),
                              ("Наполненная", link_mark, "sources:\n  - \"Sources/Web/x.md\"\n"),
                              ("Принятая-машиной", link_mark, "owner: \"@git\"\nverified_basis: \"x\"\n"),
                              ("НАЛОГОВ", term_mark, ""), ("ОКТМО", term_mark, "")):
        (kb / "Concepts" / f"{name}.md").write_text(
            stub.format(n=name, mark=mark, extra=extra), encoding="utf-8")
    thesis = ("Поступления налогов и платежей растут. Сводка ПОСТУПЛЕНИЯ НАЛОГОВ И ПЛАТЕЖЕЙ за "
              "июнь. Данные платежей сверяет ОКТМО по правилам БИК, а [[Нужная]] описывает "
              "сверку и [[НАЛОГОВ|налогов]].")
    for i in range(3):
        card(root, f"Concepts/Сводка-{i}.md", thesis + " ОКТМО ОКТМО.", kind="knowledge",
             status="knowledge")
    (kb / "media").mkdir(exist_ok=True)
    (kb / "media/схема.png.md").write_text("---\ntitle: \"схема.png\"\nstatus: draft\n---\n",
                                           encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    G = importlib.import_module("kb_gaps")
    gaps = dict(G.missing_cards(G.load(str(kb)), 1))
    assert "ПЛАТЕЖЕЙ" not in gaps and "БИК" in gaps, f"слово принято за сокращение: {gaps}"
    cp = run("kb_fix.py", "--stale-stubs", "--apply", "--allow-dirty", cwd=root, expect_rc=0)
    left = {p.stem for p in (kb / "Concepts").glob("*.md")}
    archived = {p.stem for p in (kb / "_archive").glob("*.md")}
    assert {"Забытая", "НАЛОГОВ", "схема.png", "Принятая-машиной"} <= archived, \
        f"{archived}\n{cp.stdout}"
    assert {"Нужная", "Наполненная", "ОКТМО"} <= left, f"убрана нужная карточка: {left}"
    text = (kb / "Concepts/Сводка-0.md").read_text(encoding="utf-8")
    assert "[[НАЛОГОВ" not in text and "[[Нужная]]" in text, \
        f"ссылка на убранное слово осталась — ремонт ссылок завёл бы его снова:\n{text}"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    fix = scen.split("[fix]")[1].split("\n[")[0]
    assert "--stale-stubs" in fix, "«Починить базу» не убирает пустые карточки"


@test
def test_a_human_correction_is_the_highest_truth(tmp: Path):
    """Исправление человека доходит до карточки и до тезиса и переживает разбор.

    Проверка 24.09.2026: механизм не сработал ни разу. На PRJ-C человек положил в
    `Raw/corrections/` два документа, переведённых из docx: без `corrects:` и со
    `status: draft` от перевода — оба молча пропускались. Тезис писался только по тексту
    источника, а исправление лежало в хвосте карточки; второе исправление той же карточки
    заменяло первое; раздел исправлений в карточке без истории читался текстом источника,
    и замена его блока стирала слово человека.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    root = make_project(tmp)
    src = "Sources/Confluence/Баланс.md"
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / src).write_text("# Баланс\n\nБаланс считается раз в сутки.\n", encoding="utf-8")
    card(root, "Concepts/Аналитический-баланс.md",
         "Аналитический баланс — учёт в сутки.\n\n## Источник (перенесено дословно)\n\n"
         f"### {src}\n\nБаланс считается раз в сутки.\n\n## История изменений\n\n- 2026-09-01: создана",
         kind="knowledge", status="knowledge", distilled="2026-09-20", sources=f'\n  - "{src}"')
    corr = root / "Raw/corrections"
    corr.mkdir(parents=True, exist_ok=True)
    (corr / "Чем отличается аналитический баланс.md").write_text(
        "---\ntitle: \"Чем отличается аналитический баланс\"\nconverter: pandoc\n"
        "converted: 2026-09-14\nstatus: draft\n---\n\n> ⚙️ **Машинная конвертация.** …\n\n"
        "# Чем отличается аналитический баланс\n\n## Коротко\n\n"
        "Аналитический баланс обновляется сразу после платежа, а не раз в сутки.\n",
        encoding="utf-8")
    run("kb_corrections.py", "--new", "Аналитический-баланс", "--text",
        "Сальдо показывается в рублях.", cwd=root, expect_rc=0)
    cp = run("kb_corrections.py", "--apply", cwd=root, expect_rc=0)
    path = root / "AuroraKnowledgeDB/Concepts/Аналитический-баланс.md"
    text = path.read_text(encoding="utf-8")
    assert "сразу после платежа" in text and "в рублях" in text, f"исправление не дошло:\n{cp.stdout}"
    assert text.index("## Исправления человеком") < text.index("## История изменений"), \
        "раздел исправлений не перед историей"
    assert "distilled:" not in text, "тезис писали без исправления — отметка должна сняться"
    doc = (corr / "Чем отличается аналитический баланс.md").read_text(encoding="utf-8")
    assert "Аналитический-баланс" in doc and "corrects_found: auto" in doc, \
        "куда ушло исправление, в документе не записано"
    again = run("kb_corrections.py", "--apply", cwd=root, expect_rc=0).stdout
    assert "Записано в карточки: 0" in again, "повторное применение переписало карточку"
    # разбор обновляет блок источника — исправление остаётся
    B = importlib.import_module("build_plan")
    B.refresh_card(str(path), path.read_text(encoding="utf-8"), "Баланс считается раз в час.",
                   src, True, str(root))
    text = path.read_text(encoding="utf-8")
    assert "раз в час" in text and "сразу после платежа" in text, \
        f"замена блока источника стёрла слово человека:\n{text}"
    # тезис пишется с исправлением, и раздел остаётся на месте
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    seen = []

    def fake(cfg_, role, messages, **kw):
        seen.append(messages[0]["content"])
        return {"ok": True, "text": "ТЕЗИС:\nАналитический баланс обновляется сразу "
                                    "(исправление человека).\n\nИЗМЕНИЛОСЬ:\nЧастота.",
                "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    R.run_distill(cfg, str(root), apply=True, limit=3, momus=False, call=fake)
    assert seen and "ИСПРАВЛЕНИЕ ЧЕЛОВЕКА" in seen[0] and "сразу после платежа" in seen[0], \
        "модель пишет тезис, не видя исправления"
    done = path.read_text(encoding="utf-8")
    assert "## Исправления человеком" in done and "тезис пересобран" in done
    assert done.index("## Исправления человеком") < done.index("## История изменений")
    # поля старой машинной «приёмки» — не подпись человека
    F = importlib.import_module("kb_fix")
    head = ('title: "x"\nowner: "@кто-то"\nverified: 2026-08-05\nreview_by: 2026-11-03\n'
            'verified_hash: ab\nverified_basis: "заготовка: имя пришло из карточек"\n')
    kept = F.drop_retired(head)
    assert "owner" not in kept and "verified" not in kept and "review_by" not in kept, kept
    task = 'title: "Вопрос"\nowner: "@аналитик"\n'
    assert F.drop_retired(task) == task, "у вопроса снят ответственный"


@test
def test_meeting_transcripts_reach_the_base_marked_as_meetings(tmp: Path):
    """Стенограмма встречи разбирается в базу, и каждая карточка помечена «из встречи».

    Решение пользователя 24.09.2026: знание, сказанное только на встречах, в базу не
    попадало (PRJ-B — 16 стенограмм вне разбора). Теперь попадает, но сказанное в разговоре
    не должно читаться записанным в документе: пометку ставит движок, тезис её не теряет, а
    утверждения из встречи модель помечает датой.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    B = importlib.import_module("build_plan")
    root = make_project(tmp)
    src = "Raw/meetings/2026-09-11/Запись экрана 2026-09-09 в 15_39_25_1/transcript.md"
    (root / src).parent.mkdir(parents=True, exist_ok=True)
    lines = ["[0:00:00] [SPEAKER_01] Слышно меня?", "[0:00:03] [SPEAKER_02] Да, слышно.",
             "[0:00:05] [SPEAKER_01] Реестр деклараций сортируется по дате подачи.",
             "[0:00:09] [SPEAKER_01] Новые сверху, по двадцать строк.",
             "[0:00:14] [SPEAKER_02] Ставка пени снижена до 0,1 процента в день.",
             "[0:00:20] [SPEAKER_01] Хорошо, до связи."]
    (root / src).write_text("---\ntitle: \"transcript\"\n---\n\n# transcript\n\n"
                            + "\n".join(lines) + "\n", encoding="utf-8")
    assert any(g == "Встречи" for g, _ in B.GROUPS), "стенограммы вне плана разбора"
    card(root, "Concepts/Пени.md", "Пени начисляются за каждый день просрочки.",
         kind="knowledge", status="knowledge")

    def fake(cfg_, role, messages, **kw):
        text = messages[0]["content"]
        if "стенограммы встречи" in text:
            assert "[3] [0:00:05] [SPEAKER_01] Реестр деклараций" in text, text[:800]
            return {"ok": True, "backend": 1, "model": "m", "log": [], "text": json.dumps(
                {"parts": [{"title": "Реестр деклараций", "from": 3, "to": 3,
                            "to_section": "Concepts"},
                           {"into": "Пени", "from": 4, "to": 4}]}, ensure_ascii=False)}
        return {"ok": True, "backend": 1, "model": "m", "log": [], "tps": 9,
                "text": "Реестр деклараций сортируется по дате подачи (встреча 09.09.2026)."}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    step = R.solve_meeting(cfg, str(root), "Встречи", src, True, call=fake)
    assert step["status"] == "разобран", step
    reg = root / "AuroraKnowledgeDB/Concepts/Реестр-деклараций.md"
    text = reg.read_text(encoding="utf-8")
    assert "по дате подачи" in text and "Новые сверху" in text and "Слышно" not in text, text
    assert "> 🎙 Из встречи: 09.09.2026 15:39" in text, f"нет пометки встречи:\n{text}"
    pen = (root / "AuroraKnowledgeDB/Concepts/Пени.md").read_text(encoding="utf-8")
    assert "0,1 процента" in pen and "🎙 Из встречи" in pen, pen
    man = json.loads((root / "AuroraKnowledgeDB/meta/manifest.json").read_text(encoding="utf-8"))
    assert man["sources"][src]["cards"] == 2, man["sources"]
    # тезис: дата встречи в задании, пометка на месте
    seen = []

    def fake_d(cfg_, role, messages, **kw):
        seen.append(messages[0]["content"])
        return fake(cfg_, role, messages, **kw)

    (root / "AuroraKnowledgeDB/Concepts/Реестр-деклараций.md").write_text(
        text.replace("built: machine", "built: machine\nkind: knowledge"), encoding="utf-8")
    R.run_distill(cfg, str(root), apply=True, limit=5, momus=False, call=fake_d)
    assert any("встреча 09.09.2026 15:39" in x for x in seen), "дату встречи модель не видела"
    done = reg.read_text(encoding="utf-8")
    assert done.count("> 🎙 Из встречи") == 1 and "(встреча 09.09.2026)" in done, done
    assert done.index("🎙 Из встречи") < done.index("## Источник (перенесено дословно)")


@test
def test_about_shows_the_version_in_git_and_an_unpublished_release(tmp: Path):
    """«О проекте» показывает версию в git всегда и называет неопубликованный выпуск.

    Просьба пользователя 24.09.2026: по версии в git пользователь узнаёт об обновлении, а
    автор — что новая версия не опубликована и живёт только у него. Раньше установленная
    новее опубликованной читалась «установлена последняя версия».
    """
    kit = tmp / "aurora-studio"
    kit.mkdir()
    (kit / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    ck, restore = _cockpit_on(kit)
    ck._http_get = lambda url, limit, timeout=20: (b"1.1.0\n" if url.endswith("/VERSION")
                                                  else b"# CHANGELOG\n")
    try:
        st = ck.kit_update_status(fresh=True)
    finally:
        restore()
    assert st["latest"] == "1.1.0" and st["unpublished"] and not st["newer"], st
    js = (KIT / "cockpit/modules/about/view.js").read_text(encoding="utf-8")
    assert "st.unpublished" in js and 't("about.in_git"' in js, "карточка не показывает версию в git"
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/modules/about/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert {"about.in_git", "about.installed", "about.unpublished"} <= set(cat), lang


@test
def test_buttons_explain_themselves_after_a_pause(tmp: Path):
    """Кнопки «Продуктивности», «Файлов» и «Графа» объясняют себя после паузы наведения.

    Просьба пользователя 24.09.2026: подсказка — что кнопка делает, пример использования
    и пример результата, — и появляется через несколько секунд, а не сразу.
    """
    html = ui_source()
    assert "const HELP_DELAY = 2000" in html and "function showHelp" in html, "механизма подсказок нет"
    view = html.split('id="view-files"')[1].split("</section>")[0]
    ids = re.findall(r'id="(file\w+)"[^>]*data-help="([^"]+)"', view)
    assert len(ids) >= 9, f"не у всех кнопок «Файлов» есть подсказка: {ids}"
    keys = {k for _i, k in ids} | {k for k in re.findall(r'"data-help": "(help\.files\.[\w.]+)"', html)
                                   if not k.endswith("_")}
    keys |= {"help.files.filter_" + n for n in ("all", "changed", "base", "artifacts", "drafts")}
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8"))
        miss = [f"{k}.{p}" for k in keys for p in ("what", "how", "result") if f"{k}.{p}" not in cat]
        assert not miss and "help.example" in cat, f"{lang}: {miss}"
    for mod, need in (("work", ("makeGo", "makePublish", "makeAttach", "makeKind")),
                      ("graph", ("graphAll", "graphRebuild", "graphOut", "graphIn", "graphDepth"))):
        v = (KIT / f"cockpit/modules/{mod}/view.html").read_text(encoding="utf-8")
        for bid in need:
            assert re.search(rf'id="{bid}"[^>]*data-help=', v), f"{mod}: у {bid} нет подсказки"


@test
def test_files_counter_counts_every_file(tmp: Path):
    """Счётчик у «Файлов» — все файлы проекта, а не длина списка.

    Живой случай 24.09.2026: обход дерева обрывался на 4000-м файле, и у проектов на 4 014 и
    4 449 файлов счётчик стоял на 4000, а хвост дерева не было видно вовсе.
    """
    proj = tmp / "p"
    for i in range(30):
        (proj / "d").mkdir(parents=True, exist_ok=True)
        (proj / "d" / f"f{i:02}.md").write_text("x", encoding="utf-8")
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    d = ck.file_tree(str(proj), limit=10)
    assert d["total"] == 30 and d["count"] == 10 and d["truncated"], d
    assert ck.FILES_LIMIT >= 20000, "предел списка снова мал для живых проектов"
    html = ui_source()
    assert '$("#navFiles").textContent = d.total' in html, "счётчик снова считает длину списка"


@test
def test_graph_labels_scale_and_follow_the_link_distribution(tmp: Path):
    """«Граф»: шрифт подписей крутится отдельно от масштаба, подписи — трёх уровней.

    Просьба пользователя 24.09.2026: увеличивать и уменьшать только шрифт названий узлов; у
    самых связанных (верхние 10 %) — жирно и крупнее, у наименее связанных (нижние 40 %) —
    самым мелким шрифтом; доли — по распределению числа связей и настраиваются на панели.
    Узлы с одинаковым числом связей — всегда один уровень.
    """
    view = (KIT / "cockpit/modules/graph/view.html").read_text(encoding="utf-8")
    for bid in ("graphFontUp", "graphFontDown", "graphTierTop", "graphTierLow"):
        assert re.search(rf'id="{bid}"[^>]*data-help=', view), f"нет {bid} или его подсказки"
    js = (KIT / "cockpit/modules/graph/view.js").read_text(encoding="utf-8")
    assert "function setFont" in js and "G.cy.style(sheet())" in js, "шрифт перекладывает граф"
    assert "node[tier = 'many']" in js and "node[tier = 'few']" in js
    node = shutil.which("node")
    if not node:
        return
    fn = re.search(r"export function degreeTiers[\s\S]*?\n}\n", js).group(0).replace("export ", "")
    probe = fn + textwrap.dedent("""
        const cnt = r => { const c = {many: 0, mid: 0, few: 0};
                           Object.values(r.tiers).forEach(v => c[v]++); return c; };
        const d = {}; for (let i = 0; i < 100; i++) d["x" + i] = i;
        const t = {}; for (let i = 0; i < 10; i++) t["a" + i] = 1;
        t.hub = 9; t.b = 2; t.c = 2;
        console.log(JSON.stringify([cnt(degreeTiers(d, 10, 40)),
                                    degreeTiers(t, 10, 40).tiers,
                                    cnt(degreeTiers({a: 3, b: 3}, 10, 40))]));
    """)
    f = tmp / "tiers.js"
    f.write_text(probe, encoding="utf-8")
    cp = subprocess.run([node, str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr
    spread, ties, flat = json.loads(cp.stdout)
    assert spread == {"many": 10, "mid": 50, "few": 40}, spread
    ones = {ties[f"a{i}"] for i in range(10)}
    assert len(ones) == 1 and ties["hub"] == "many", f"равное число связей — разные уровни: {ties}"
    assert flat == {"many": 0, "mid": 2, "few": 0}, flat


@test
def test_trust_follows_the_story_and_ignores_the_archive(tmp: Path):
    """Доверие: отставшая подзадача судится по истории; архив не сидит в доле.

    Вопрос пользователя 25.09.2026: «почему у PRJ-B только 59 % доверенных?». Нашлось два
    дефекта движка. 22 карточки были связаны с аналитическими подзадачами в «Анализ», чьи
    истории давно «Закрыто», «Тестирование» или «Аналитика - готово» — подзадачу просто не
    закрыли. И 50 карточек из `_archive/` сидели в знаменателе доли: 59,6 % вместо 69,6 %.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    K = importlib.import_module("kb_trust")
    st = {"PRJ-1": "Анализ", "PRJ-2": "Закрыто", "PRJ-3": "Анализ", "PRJ-4": "Бэклог",
          "PRJ-5": "Закрыто"}
    parents = {"PRJ-1": "PRJ-2", "PRJ-3": "PRJ-4", "PRJ-5": "PRJ-4"}
    trust, draft = {"закрыто"}, {"анализ", "бэклог"}
    eff = K.inherit_story(st, parents, trust)
    assert eff["PRJ-1"] == "Закрыто", "подзадача готовой истории осталась «в анализе»"
    assert eff["PRJ-3"] == "Анализ", "подзадача незаконченной истории стала доверенной"
    assert eff["PRJ-5"] == "Закрыто", "закрытая подзадача взяла статус незакрытой истории"
    table = {"direct": {"Sources/Confluence/A.md": [{"key": "PRJ-1", "why": "номер"}]},
             "indirect": {}}
    cls, why = K.source_class("Sources/Confluence/A.md", table, eff, trust, draft)
    assert cls == "trusted" and "подзадача сама в «Анализ»" in why and "PRJ-2" in why, (cls, why)
    # стенограмма встречи — не подписанный документ, хоть и лежит в Raw/
    cls, why = K.source_class("Raw/meetings/20260424_15-07-12.md", table, eff, trust, draft)
    assert cls == "meeting" and "встречи" in why, (cls, why)

    root = make_project(tmp)
    card(root, "Concepts/Живая.md", "Знание.", status="knowledge", kind="knowledge",
         trust_basis='"ok"')
    card(root, "Concepts/Черновая.md", "Знание.", status="draft", kind="knowledge",
         trust_basis='"задача PRJ-3 в статусе «Анализ»"')
    for i in range(3):
        card(root, f"_archive/Старая-{i}.md", "Было.", status="draft", kind="knowledge")
    card(root, "Concepts/Выведенная.md", "Было.", status="deprecated", kind="knowledge")
    out = run("aurora_stats.py", "--json", cwd=root).stdout
    st_json = json.loads(out[out.index("{"):])
    assert st_json["trust_total"] == 2 and st_json["pct_verified"] == 50.0, \
        f"архив или выведенное попали в долю доверия: {st_json['trust_total']}"


@test
def test_ask_picks_meetings_apart_from_trust(tmp: Path):
    """«Спросить»: встречи — отдельный выбор; доля доверия их не считает.

    Решение пользователя 25.09.2026: карточки из встреч показываются отдельной строкой, а в
    «Спросить» — выбор: только встречи; всё, включая недоверенное и встречи; только
    доверенное и встречи. В «только доверенное» сказанное на встрече не проходит.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    C = importlib.import_module("ctx_pack")
    AC = importlib.import_module("aurora_common")

    def mk(name, status, srcs):
        return C.Card(f"AuroraKnowledgeDB/Concepts/{name}.md",
                      f'---\ntitle: "{name}"\nstatus: {status}\nkind: knowledge\n'
                      + AC.sources_block(srcs) + f"---\n\n# {name}\n\nЗнание о предмете.\n")
    meet = "Raw/meetings/2026-09-11/transcript.md"
    cards = [mk("Док", "knowledge", ["Sources/Confluence/A.md"]),
             mk("Черновик", "draft", ["Sources/Confluence/B.md"]),
             mk("Встреча", "draft", [meet]),
             mk("Смесь", "knowledge", ["Sources/Confluence/A.md", meet])]
    want = {"generate": {"Док", "Смесь"},
            "trusted_meetings": {"Док", "Смесь", "Встреча"},
            "meetings": {"Встреча", "Смесь"},
            "evaluate": {"Док", "Черновик", "Встреча", "Смесь"}}
    for mode, names in want.items():
        got = {c.stem for c in cards if C.mode_allows(c, mode)}
        assert got == names, f"режим {mode}: {sorted(got)} вместо {sorted(names)}"
    # каждый режим панели движок принимает
    view = (KIT / "cockpit/modules/ask/view.html").read_text(encoding="utf-8")
    ui = set(re.findall(r'<option value="(\w+)" data-i18n="ask\.mode_', view))
    runner = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    choices = set(re.findall(r'"(\w+)"', runner.split('"--mode", default="generate"')[1]
                             .split("help=")[0]))
    assert {"generate", "trusted_meetings", "meetings", "evaluate"} <= ui, ui
    assert ui <= choices and ui <= set(C.MODE_STATUSES), f"панель шлёт режим, которого движок не знает: {ui - choices}"

    root = make_project(tmp)
    card(root, "Concepts/Док.md", "Знание.", status="knowledge", kind="knowledge",
         trust_basis='"ok"', sources=f'["Sources/Confluence/A.md"]')
    card(root, "Concepts/Встреча.md", "Сказано.", status="draft", kind="knowledge",
         trust_basis='"сказано на встрече"', sources=f'["{meet}"]')
    out = run("aurora_stats.py", "--json", cwd=root).stdout
    st = json.loads(out[out.index("{"):])
    assert st["trust_total"] == 1 and st["pct_verified"] == 100.0 and st["meetings"] == 1, \
        f"карточка из встречи попала в долю доверия: {st['trust_total']}, встреч {st.get('meetings')}"
    health = (KIT / "cockpit/modules/health/view.js").read_text(encoding="utf-8")
    assert 't("health.trust_meetings"' in health and '"из встреч (вне доли)"' in health, \
        "встречи снова показаны одной из причин недоверия, а не отдельной строкой"


@test
def test_an_epic_does_not_decide_trust(tmp: Path):
    """Статус эпика доверия не решает — только истории и задачи.

    Решение пользователя 25.09.2026: эпик «В работе» живёт месяцами, пока его истории давно
    готовы. Связь только с эпиком — то же, что связей нет; история рядом с эпиком решает сама.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    K = importlib.import_module("kb_trust")
    st = {"PRJ-1": "В работе", "PRJ-2": "Закрыто"}
    trust, draft = {"закрыто"}, {"в работе", "анализ"}
    K.EPICS.clear()
    K.EPICS.add("PRJ-1")
    try:
        only = {"direct": {"Sources/Docs/A.md": [{"key": "PRJ-1", "why": "номер"}]}, "indirect": {}}
        cls, why = K.source_class("Sources/Docs/A.md", only, st, trust, draft)
        assert cls == "unknown" and "эпик" in why, (cls, why)
        both = {"direct": {"Sources/Docs/A.md": [{"key": "PRJ-1", "why": "номер"},
                                                 {"key": "PRJ-2", "why": "номер"}]},
                "indirect": {}}
        cls, why = K.source_class("Sources/Docs/A.md", both, st, trust, draft)
        assert cls == "trusted" and "PRJ-2" in why, f"эпик в работе утянул готовую историю: {cls} — {why}"
    finally:
        K.EPICS.clear()


@test
def test_trust_names_every_wiki_folder_it_no_longer_trusts(tmp: Path):
    """Папка вики в `trusted_sources` доверия не даёт — и пересчёт называет её, как ни пиши.

    Решение пользователя 25.09.2026: вики доверена справочником или задачей Jira, одно
    правило на все проекты. Папку пишут и без косой в конце, и выше вики («Sources») —
    правило действовало, а пересчёт про такую папку молчал.
    """
    root = make_project(tmp)
    with open(root / "aurora.config.yaml", "a", encoding="utf-8") as f:
        f.write("verify:\n  trusted_sources: [Raw/contract, Sources/Confluence, "
                "Sources/Confluence/Справочники/, Sources/Confluence/Проект/]\n")
    trace = root / "AuroraKnowledgeDB/meta/trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "trace.json").write_text('{"direct": {}, "indirect": {}}', encoding="utf-8")
    out = run("kb_trust.py", cwd=root).stdout
    line = next((l for l in out.splitlines() if "доверия больше не дают" in l), "")
    assert "Sources/Confluence," in line + "," and "Sources/Confluence/Проект/" in line, \
        f"папка вики без косой в конце не названа:\n{out[:800]}"
    assert "Справочники" not in line and "Raw/contract" not in line, line


@test
def test_meeting_requirements_are_stated_and_repairs_skip_the_archive(tmp: Path):
    """Требование со встречи — `req_status: stated`; ремонт не судит архив и темы встреч.

    Находки прогона PRJ-B 25.09.2026: 115 «требований» из встреч лежали как требования
    заказчика; `kb:repair --links` давал код 1 из-за ссылки в архивной заметке; папка
    стенограммы («Запись-экрана-…») становилась карточкой темы.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    root = make_project(tmp)
    meet = "Raw/meetings/2026-09-11/Запись экрана 2026-09-09 в 15_39_25_1/transcript.md"
    src_block = lambda p: AC.sources_block([p]).rstrip("\n")
    req = root / "AuroraKnowledgeDB/Requirements/Сортировка-реестра.md"
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text('---\ntitle: "Сортировка реестра"\nstatus: draft\ntype: requirement\n'
                   f"kind: knowledge\n{src_block(meet)}\n---\n\n# Сортировка реестра\n\n"
                   "Реестр сортируется по дате подачи.\n", encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Из-встречи-{i}.md", f"Сказано {i}.", status="draft",
             kind="knowledge", sources=f'["{meet}"]')
    folder = os.path.dirname(meet)
    theme = root / "AuroraKnowledgeDB/Concepts/Запись-экрана.md"
    theme.write_text('---\ntitle: "Запись экрана"\nstatus: draft\ntype: concept\nkind: knowledge\n'
                     f"tags: [тема]\n{src_block(folder)}\n---\n\n# Запись экрана\n\n"
                     "- [[Из-встречи-0]]\n", encoding="utf-8")
    card(root, "_archive/Старая.md", "Ссылка на [[Нет-такой-карточки]].", status="deprecated",
         kind="knowledge")
    cp = run("kb_fix.py", "--links", "--themes", "--meetings", "--apply", "--allow-dirty",
             cwd=root)
    assert cp.returncode == 0, cp.stdout[-800:] + cp.stderr[-800:]
    assert "не решено 0" in cp.stdout, f"ремонт ссылок судит архив:\n{cp.stdout[:800]}"
    assert "Карточки тем по папкам источников: 0" in cp.stdout, \
        f"папка стенограммы стала темой:\n{cp.stdout[:800]}"
    assert not theme.exists() and (root / "AuroraKnowledgeDB/_archive/Запись-экрана.md").exists(), \
        "тема по папке стенограммы не ушла в архив"
    text = req.read_text(encoding="utf-8")
    assert "req_status: stated" in text and "🎙 Из встречи" in text, \
        f"требование со встречи читается согласованным:\n{text}"


@test
def test_a_meeting_past_the_step_budget_waits_whole(tmp: Path):
    """Встреча, не влезающая в бюджет шага, уходит в следующий оборот целиком.

    Живой случай, PRJ-B 25.09.2026: встреча в 10 окон дорабатывала за пределом бюджета
    `agent:build`, и оракул разбора видел «движок засчитал 4, агент объявил 3».
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    src = "Raw/meetings/2026-09-11/transcript.md"
    (root / src).parent.mkdir(parents=True, exist_ok=True)
    (root / src).write_text("# transcript\n\n" + "\n".join(
        f"[0:00:{i:02d}] [SPEAKER_01] Реплика номер {i} о реестре деклараций." for i in range(20))
        + "\n", encoding="utf-8")
    calls = []

    def fake(cfg_, role, messages, **kw):
        calls.append(role)
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": '{"parts": []}'}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    step = R.solve_meeting(cfg, str(root), "Встречи", src, True, call=fake,
                           step_end=time.time() - 1)
    assert step["status"] == "стоп" and not calls, f"встреча пошла за пределом бюджета: {step}, {calls}"
    man = root / "AuroraKnowledgeDB/meta/manifest.json"
    done = json.loads(man.read_text(encoding="utf-8")).get("sources", {}) if man.exists() else {}
    assert src not in done, "недочитанная встреча отмечена разобранной — выпала бы из плана"


@test
def test_a_silent_vector_gateway_is_asked_once(tmp: Path):
    """Шлюз векторов, не ответивший вовсе, до конца процесса не спрашивается.

    Живой случай, PRJ-B 25.09.2026: `ops:search-quality` при недоступном шлюзе ждал таймаута
    на каждом вопросе — 1 ч 55 мин вместо минут.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    E = importlib.import_module("kb_embed")
    asked = []
    saved = (E.endpoints, E.AG.http_json, E.load_index)
    E.endpoints = lambda cfg: [{"url": "http://dead", "key": ""}]
    E.AG.http_json = lambda url, body, key, timeout: (asked.append(url) or (None, {}, "timed out", 0))
    E.load_index = lambda: {}
    E.DEAD.clear()
    try:
        for _ in range(5):
            assert E.embed(["вопрос"], {"request_timeout": 1}, "m") == []
        assert asked == ["http://dead/embeddings"], f"мёртвый шлюз спрошен {len(asked)} раз"
        assert "http://dead" in E.DEAD
    finally:
        E.endpoints, E.AG.http_json, E.load_index = saved
        E.DEAD.clear()


@test
def test_legacy_wiki_branches_become_reference_branches(tmp: Path):
    """Прежнее умолчание веток вики по названию сменяется справочным — в конфиге и в счёте.

    Решение пользователя 25.09.2026: ветки «описания системы» (алгоритмы, GUI, ролевая
    модель) больше не доверены по названию. Список, который проект правил сам, не трогается.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    S = importlib.import_module("aurora_setup")
    legacy = "[" + ", ".join(f'"{b}"' for b in AC.LEGACY_TRUSTED_BRANCHES) + "]"
    text = f"verify:\n  trusted_sources: [Raw/contract]\n  trusted_branches: {legacy}\n"
    new, done = S.fill_trust_defaults(text)
    assert any("справочные ветки" in d for d in done), done
    assert "Алгоритмы" not in new and "Глоссарий" in new, new
    own = "verify:\n  trusted_sources: [Raw/contract]\n  trusted_branches: [Алгоритмы]\n"
    assert "[Алгоритмы]" in S.fill_trust_defaults(own)[0], "список проекта переписан"

    root = make_project(tmp)
    with open(root / "aurora.config.yaml", "a", encoding="utf-8") as f:
        f.write(f"verify:\n  trusted_branches: {legacy}\n")
    trace = root / "AuroraKnowledgeDB/meta/trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "trace.json").write_text('{"direct": {}, "indirect": {}}', encoding="utf-8")
    out = run("kb_trust.py", cwd=root).stdout
    assert "прежние доверенные ветки" in out and "справочные ветки вики: Нормативно" in out, out[:600]


@test
def test_meeting_mark_is_written_once(tmp: Path):
    """Пометка «из встречи» ставится один раз, сколько бы шагов её ни ставили.

    Живой случай 25.09.2026: каждый проход ремонта добавлял пустую строку перед пометкой.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    text = ('---\ntitle: "К"\n' + AC.sources_block(["Raw/meetings/2026-09-11/t.md"])
            + "---\n\n# К\n\nСказано на встрече.\n")
    once = AC.with_meeting_mark(text)
    assert once.count(AC.MEETING_MARK) == 1, once
    assert AC.with_meeting_mark(AC.with_meeting_mark(once)) == once, "пометка растёт с каждым проходом"


@test
def test_analyst_rework_is_counted_apart_for_each_person(tmp: Path):
    """Отчёт аналитика: повторная сдача после возврата — красным, по людям — по типу.

    1.133.0 (TC-088): сдачи одной задачи за неделю — одна; сдача после возврата — отдельное
    ведро. Сверка по людям складывала возвраты в одно ведро «историй», и под фильтром по
    сотруднику возврат инцидента весил как история. Возврат «Аналитика - готово → Анализ»
    не узнавался в проектах, где статус называется «Анализ», а не «Аналитика».
    """
    root = tmp / "p"
    data = root / ".opencode/cache/reports/analyst"
    data.mkdir(parents=True)
    (root / "Settings").mkdir()
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "P"\n  slug: "P"\n\natlassian:\n  jira:\n    project_key: "P"\n\n'
        "reports:\n  analyst:\n    year: 2026\n    roster: Settings/roster.csv\n"
        "    events: Settings/events.csv\n    data_dir: .opencode/cache/reports/analyst\n"
        "    output: Artifacts/reports/{project}_analyst_extended.html\n", encoding="utf-8")
    (root / "Settings/roster.csv").write_text("ФИО;Роль\nАналитик А;Аналитик\nАналитик Б;Аналитик\n",
                                              encoding="utf-8")
    at = lambda day, h=12: f"2026-{day}T{h:02d}:00:00.000+0300"
    READY = "Аналитика - готово"
    hist = {
        # сдана в неделю 10, возвращена из разработки, сдана заново в неделю 12
        "P-1": ("История", "Аналитик А", [("Бэклог", "Аналитика", at("03-02", 9)),
                                           ("Аналитика", READY, at("03-02")),
                                           (READY, "Разработка", at("03-04")),
                                           ("Разработка", "Бэклог", at("03-10")),
                                           ("Бэклог", "Аналитика", at("03-16", 9)),
                                           ("Аналитика", READY, at("03-16"))]),
        # три сдачи за одну неделю 06 — одна
        "P-2": ("История", "Аналитик А", [("Анализ", READY, at("02-02")),
                                           (READY, "Анализ", at("02-03")),
                                           ("Анализ", READY, at("02-04")),
                                           (READY, "Анализ", at("02-05")),
                                           ("Анализ", READY, at("02-06"))]),
        # инцидент: сдан в неделю 10, возвращён в «Анализ», сдан заново в неделю 13
        "P-3": ("Инцидент", "Аналитик Б", [("Анализ", READY, at("03-03")),
                                            (READY, "Анализ", at("03-11")),
                                            ("Анализ", READY, at("03-24"))]),
    }
    issues, full = [], {}
    for key, (typ, who, trs) in hist.items():
        issues.append({"key": key, "fields": {"issuetype": {"name": typ}, "summary": key}})
        full[key] = {"key": key, "assignee_now": who, "issuetype": typ,
                     "status_history": [{"from": a, "to": b, "at": t} for a, b, t in trs]}
    (data / "issues.json").write_text(json.dumps(issues, ensure_ascii=False), encoding="utf-8")
    (data / "full_status.json").write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "AURORA_REPORT_YEAR": "2026"}
    for step in ("make_analyst_metrics.py", "update_analyst_metrics.py", "verify_weekly_by_person.py"):
        cp = subprocess.run([sys.executable, str(KIT / "reports/analyst" / step)], cwd=str(root),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
        assert cp.returncode == 0, f"{step}: {cp.stdout[-600:]}{cp.stderr[-600:]}"
    m = json.loads(next(data.rglob("analyst_metrics.json")).read_text(encoding="utf-8"))
    wk = m["weekly"]
    assert wk["06"]["stories"] == 1, f"три сдачи за неделю посчитаны не одной: {wk['06']}"
    assert wk["10"]["stories"] == 1 and wk["10"]["others"] == 1, wk["10"]
    assert wk["12"]["rework_stories"] == 1 and wk["13"]["rework_others"] == 1, wk
    back = {r["issue"]: r["returned_from"] for r in m["rework_raw"]}
    assert back == {"P-1": "Разработка", "P-3": READY}, f"откуда вернули: {back}"
    b = m["weekly_by_person"]["Аналитик Б"]["13"]
    assert b.get("rework_others") == 1 and not b.get("rework_stories"), \
        f"возврат инцидента у сотрудника лёг в ведро историй: {b}"


@test
def test_kit_launchers_try_python_before_trusting_it(tmp: Path):
    """Пусковые файлы кита проверяют Python и git запуском, а `.bat` хранится с CRLF.

    На чистом macOS `/usr/bin/python3` и `/usr/bin/git` — заглушки, которые без
    инструментов Xcode не работают; на Windows `python.exe` из WindowsApps открывает
    магазин. Поиск по PATH их находил, и пусковой файл падал на запуске панели вместо
    внятного «поставьте Python». С одними LF в `.bat` cmd.exe промахивается мимо меток.
    """
    bat = (KIT / "start-aurora.bat").read_bytes()
    assert bat.count(b"\r\n") == bat.count(b"\n"), "в start-aurora.bat строки без CRLF"
    assert "/start-aurora.bat -text" in (KIT / ".gitattributes").read_text(encoding="utf-8"), \
        "git поменяет концы строк .bat при клоне или в архиве"
    text = bat.decode("utf-8")
    assert "sys.version_info >= (3, 9)" in text and "where python" not in text, \
        ".bat снова доверяет поиску по PATH"
    bash = shutil.which("bash")
    if not bash or os.name == "nt":
        return
    fake = tmp / "bin"
    fake.mkdir()
    for name, say in (("python3", "xcode-select: note: No developer tools were found"),
                      ("git", "xcode-select: note: No developer tools were found"),
                      ("python", "Python 2.7.18")):
        (fake / name).write_text(f"#!/bin/sh\necho '{say}' >&2\nexit 1\n", encoding="utf-8")
        (fake / name).chmod(0o755)
    for tool in ("dirname", "uname"):
        real = shutil.which(tool)
        if real:
            (fake / tool).symlink_to(real)
    kit = tmp / "kit"
    kit.mkdir()
    shutil.copy2(KIT / "start-aurora.command", kit / "start-aurora.command")
    cp = subprocess.run([bash, str(kit / "start-aurora.command")], input="", capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=30, env={"PATH": f"{fake}:/bin", "HOME": str(tmp)})
    assert cp.returncode == 1 and "Не найден Python 3.9" in cp.stdout, \
        f"заглушка python3 принята за Python:\n{cp.stdout}{cp.stderr}"
    (fake / "python3").unlink()
    (fake / "python3").symlink_to(sys.executable)
    cp = subprocess.run([bash, str(kit / "start-aurora.command")], input="", capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=30, env={"PATH": f"{fake}:/bin", "HOME": str(tmp)})
    assert cp.returncode == 1 and "Не найден git" in cp.stdout, \
        f"заглушка git принята за git:\n{cp.stdout}{cp.stderr}"


@test
def test_trust_flows_from_the_user_story(tmp: Path):
    """Доверие вики идёт от пользовательской истории; раздел с галочкой доверен сразу.

    Решение пользователя 27.09.2026: алгоритм доверен, если доверена история, которую он
    реализует («Реализует: US-4.1.1»); контракт — через алгоритм; форма — по историям; из
    нескольких связей решает слабейшая. Галочка «доверять» у корня синка Confluence —
    первое слово, сильнее задач. Живой случай PRJ-C: номер истории искался только в
    заголовке, код `ALG-072` в тексте контракта связью не считался, а через страницу-
    родителя алгоритм получал задачи всех соседних алгоритмов папки.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    T = importlib.import_module("kb_trace_table")
    K = importlib.import_module("kb_trust")
    AC = importlib.import_module("aurora_common")
    root = make_project(tmp)
    jira, conf = root / "Sources/JIRA", root / "Sources/Confluence"

    def page(rel, title, body, pid=""):
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\n{"page_id: " + pid + chr(10) if pid else ""}title: "{title}"\n'
                     f'breadcrumbs: "Алгоритмы"\n---\n\n# {title}\n\n{body}\n', encoding="utf-8")
    jira.mkdir(parents=True, exist_ok=True)
    for key, title, status in (("PRJ-1", "US 4.1.1 Вход", "Закрыто"),
                               ("PRJ-2", "US 4.1.2 Выход", "Анализ"),
                               ("PRJ-3", "Доработка соседнего алгоритма", "Анализ")):
        (jira / f"{key}.md").write_text(f'---\nkey: "{key}"\ntitle: "{title}"\n'
                                        f'status: "{status}"\n---\n', encoding="utf-8")
    page("Истории/US-4.1.1._Вход.md", "US-4.1.1. Вход", "Использует ALG-073 и US-4.1.2.")
    page("Алгоритмы/index.md", "Алгоритмы", "- ALG-072 Регистрация\n- ALG-073 Прочее", pid="100")
    page("Алгоритмы/ALG-072_Регистрация.md", "ALG-072 Регистрация",
         "| Реализует | RU.PRJ.US-4.1.1 |")
    page("Алгоритмы/ALG-073_Прочее.md", "ALG-073 Прочее", "Задача PRJ-3.")
    page("Форматы/Контракт_регистрации.md", "Контракт регистрации", "Вызывает RU.PRJ.ALG-072.")
    page("GUI/Форма_входа.md", "Форма входа", "Истории US-4.1.1 и US-4.1.2.")
    page("Модель/index.md", "Логическая модель", "Сущности.", pid="900")
    page("Модель/Заявитель.md", "Заявитель", "Заполняет ALG-073. Задача PRJ-3.")

    t = T.build(str(root))
    alg = "Sources/Confluence/Алгоритмы/ALG-072_Регистрация.md"
    assert [r["key"] for r in t["direct"].get(alg, [])] == ["PRJ-1"], \
        f"история, названная в тексте алгоритма, не стала связью: {t['direct'].get(alg)}"
    assert alg not in t["indirect"] or all("PRJ-3" != r["key"] for r in t["indirect"][alg]), \
        "через страницу-родителя алгоритм получил задачу соседнего"
    story = "Sources/Confluence/Истории/US-4.1.1._Вход.md"
    assert [r["key"] for r in t["direct"][story]] == ["PRJ-1"] and story not in t["refs"], \
        f"история взяла в опору чужую историю или алгоритм: {t['direct'][story]}, {t['refs'].get(story)}"
    contract = "Sources/Confluence/Форматы/Контракт_регистрации.md"
    assert [r["page"] for r in t["refs"][contract]] == [alg], t["refs"].get(contract)

    K.TRUSTED_ROOTS[:] = K.trusted_roots(str(root), [
        {"page_id": "900", "title": "Логическая модель", "trusted": True},
        {"page_id": "100", "title": "Алгоритмы", "trusted": False}])
    K._REF_MEMO.clear()
    st = K.task_status(str(root))
    trust, draft = {"закрыто"}, {"анализ"}
    judge = lambda src: K.source_class(src, t, st, trust, draft)
    try:
        assert judge(alg)[0] == "trusted", judge(alg)
        assert judge("Sources/Confluence/Алгоритмы/ALG-073_Прочее.md")[0] == "draft"
        cls, why = judge(contract)
        assert cls == "trusted" and "ALG-072" in why, f"контракт не взял доверие алгоритма: {cls} — {why}"
        cls, why = judge("Sources/Confluence/GUI/Форма_входа.md")
        assert cls == "draft" and "PRJ-2" in why, f"слабейшая история не решила за форму: {cls} — {why}"
        assert judge(story)[0] == "trusted", "история взяла статус упомянутого алгоритма"
        cls, why = judge("Sources/Confluence/Модель/Заявитель.md")
        assert cls == "raw" and "Логическая модель" in why, \
            f"раздел с галочкой не доверен сразу: {cls} — {why}"
    finally:
        K.TRUSTED_ROOTS.clear()
        K._REF_MEMO.clear()

    # галочка — в настройке проекта: читается, пишется и доходит до формы панели
    cfg = ('atlassian:\n  confluence:\n    base_url: "https://c.example.com"\n    space: "S"\n'
           '    sync_roots:\n      - page_id: "900"\n        title: "Логическая модель"\n'
           '        trusted: true\n      - page_id: "100"\n        title: "Алгоритмы"\n'
           '  jira:\n    project_key: "PRJ"\n')
    assert [r["trusted"] for r in AC.sync_roots(cfg)] == [True, False]
    S = importlib.import_module("aurora_setup")
    (root / "aurora.config.yaml").write_text(cfg, encoding="utf-8")
    c = S.read_config(root / "aurora.config.yaml")
    assert c["sync_roots"] == [("900", "Логическая модель", True), ("100", "Алгоритмы", False)], c
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {"sync_roots": [{"page_id": "900", "title": "Логическая модель"},
                                            {"page_id": "100", "title": "Алгоритмы",
                                             "trusted": True}]})
    got = AC.sync_roots((root / "aurora.config.yaml").read_text(encoding="utf-8"))
    assert [(r["page_id"], r["trusted"]) for r in got] == [("900", False), ("100", True)], got
    assert "trusted: false" not in (root / "aurora.config.yaml").read_text(encoding="utf-8"), \
        "«не отмечено» записано как «не доверять» — у корня это «работают правила»"
    html = ui_source()
    assert 'title: t("proj.root_trust_hint")' in html and "onchange:e=>r.trusted=" in html, \
        "у корня синка в форме нет галочки «доверять»"
    for lang in ("ru", "en"):
        assert "proj.root_trust_hint" in json.loads(
            (KIT / f"cockpit/i18n/{lang}.json").read_text(encoding="utf-8")), lang


@test
def test_a_thesis_the_model_calls_unchanged_is_kept(tmp: Path):
    """«По сути без изменений» — прежний тезис остаётся, без Момуса и повторных выноса и связи.

    Замер 27.09.2026: так заканчивалась каждая третья пересборка тезиса у PRJ-B и четыре из
    пяти у PRJ-C, а новый тезис в половине случаев переформулировал то же знание. Следом
    шли Момус, повторный вынос и повторное связывание — по новой отметке тезиса.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    src = root / "Sources/Confluence/Стр.md"
    src.write_text("Возврат за 10 дней. Правило действует для всех заявок.", encoding="utf-8")
    run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md",
        "--paras", "1", "--to", "Concepts", "--apply", cwd=root)
    card = root / "AuroraKnowledgeDB/Concepts/Возврат.md"
    txt = card.read_text(encoding="utf-8").replace(
        "# Возврат\n\n", "Возврат занимает десять дней.\n\n## Источник (перенесено дословно)\n\n")
    txt = txt.replace("built: machine", "built: machine\nkind: knowledge\ndistilled: 2026-08-01"
                      "\nextracted: 2026-08-01\nrelinked: 2026-08-01")
    card.write_text(txt + "\n## История изменений\n\n- 2026-08-01: карточка заведена\n",
                    encoding="utf-8")
    src.write_text("Возврат за 10 дней. Правило действует для всех заявок без исключения.",
                   encoding="utf-8")
    run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md",
        "--paras", "1", "--to", "Concepts", "--apply", cwd=root)
    now = card.read_text(encoding="utf-8")
    assert "distilled_was: 2026-08-01" in now and "\ndistilled:" not in now, now[:400]

    # внутри цикла маршрута дописанная карточка ждёт его конца
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    assert str(card) not in R.distill_queue(cfg, str(root), defer_refresh=True)
    assert str(card) in R.distill_queue(cfg, str(root))

    roles = []

    def fake(cfg_, role, messages, **kw):
        roles.append(role)
        return {"ok": True, "text": "ТЕЗИС:\nВозврат оформляется в течение десяти дней.\n\n"
                                    "ИЗМЕНИЛОСЬ:\nПо сути без изменений: уточнена формулировка.",
                "backend": 1, "model": "m", "tps": 9, "log": []}

    res = R.run_distill(cfg, str(root), apply=True, limit=3, momus=True, call=fake)
    done = card.read_text(encoding="utf-8")
    assert roles == ["worker"], f"Момус проверял тезис, который не менялся: {roles}"
    assert "Возврат занимает десять дней." in done.split("## Источник")[0], \
        "проверенный тезис заменён переформулировкой того же"
    assert "distilled: 2026-08-01" in done and "distilled_was" not in done, done[:500]
    assert "прежний тезис оставлен" in done, "в истории не сказано, что тезис оставлен"
    assert [s["status"] for s in res["steps"]] == ["без изменений"], res["steps"]
    # вынос и связывание этот тезис уже видели — в очередь он не возвращается
    fm = R.frontmatter(done) if hasattr(R, "frontmatter") else __import__("aurora_common").frontmatter(done)
    assert fm.get("extracted") == fm.get("distilled") == fm.get("relinked"), fm

    # маршрут: тезисы дописанных карточек — один раз, после цикла
    sc = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    upd = sc.split("[update]", 1)[1].split("\n[", 1)[0]
    loop, after = upd.split("цикл:", 1)[1].split("конец цикла", 1)
    assert "agent:distill" in loop and "--defer-refresh" in loop.split("agent:distill", 1)[1].splitlines()[0]
    first_after = [l.split("|")[0].strip() for l in after.splitlines()[1:4]]
    assert first_after[:2] == ["agent:distill", "agent:extract"], first_after


@test
def test_trace_reads_requirement_yogi_links(tmp: Path):
    """Ключи Requirement Yogi в шапке зеркала — связи доверия без догадок по тексту.

    `ry_links` пишет синк из макросов страницы: «Реализует: RU.X.US-4.1.2» — это связь,
    записанная автором страницы. С 1.138.0 трассировка берёт её и без упоминания в тексте:
    история — прямая связь, ключ алгоритма — ссылка на страницу, где он объявлен.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    T = importlib.import_module("kb_trace_table")
    root = make_project(tmp)
    jira, conf = root / "Sources/JIRA", root / "Sources/Confluence"
    jira.mkdir(parents=True, exist_ok=True)
    (jira / "PRJ-2.md").write_text('---\nkey: "PRJ-2"\ntitle: "US 4.1.2 Выход"\nstatus: "Анализ"\n---\n',
                                   encoding="utf-8")

    def page(rel, title, defines="", links="", body="Текст страницы."):
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\ntitle: "{title}"\nry_defines: [{defines}]\nry_links: [{links}]\n---\n\n'
                     f"# {title}\n\n{body}\n", encoding="utf-8")
    page("Алгоритмы/ALG-072_Выход.md", "ALG-072 Выход", "RU.PRJ.ALG-072", "RU.PRJ.US-4.1.2")
    page("Форматы/Контракт.md", "Контракт выхода", "RU.PRJ.CONTRACT-001", "RU.PRJ.ALG-072")
    t = T.build(str(root))
    alg = "Sources/Confluence/Алгоритмы/ALG-072_Выход.md"
    assert [r["key"] for r in t["direct"].get(alg, [])] == ["PRJ-2"], t["direct"].get(alg)
    assert "Requirement Yogi" in t["direct"][alg][0]["why"], t["direct"][alg]
    contract = "Sources/Confluence/Форматы/Контракт.md"
    assert [(r["page"], r["code"]) for r in t["refs"].get(contract, [])] == [(alg, "RU.PRJ.ALG-072")], \
        t["refs"].get(contract)


@test
def test_graph_export_types_links_and_finds_themes(tmp: Path):
    """Граф наружу: тип и уверенность у связи, темы по модульности, цвета Obsidian.

    Одобренные пользователем 27.09.2026 предложения из разбора graphify: связь называет,
    что она значит и найдена ли в источнике (`EXTRACTED`) или выведена (`INFERRED`);
    темы — группы по модульности (как Лейден у graphify), названные самой связанной
    карточкой знания; цвета графа Obsidian — по статусу, свои группы человека — первыми.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    G = importlib.import_module("kb_graph")
    importlib.reload(G)
    # две плотные группы и одна ниточка между ними — две темы, и так каждый раз
    pairs = {}
    for grp in ("a", "b"):
        for i in range(5):
            for j in range(5):
                if i != j:
                    pairs.setdefault(f"{grp}{i}", set()).add(f"{grp}{j}")
    pairs["a0"].add("b0"); pairs["b0"].add("a0")
    lab = G.louvain(pairs)
    assert len({lab[f"a{i}"] for i in range(5)}) == 1 and len({lab[f"b{i}"] for i in range(5)}) == 1
    assert lab["a0"] != lab["b0"] and G.louvain(pairs) == lab, lab

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    (kb / ".obsidian").mkdir(parents=True, exist_ok=True)
    (kb / ".obsidian/graph.json").write_text(json.dumps(
        {"colorGroups": [{"query": "BPMN", "color": {"a": 1, "rgb": 1}}], "scale": 2}),
        encoding="utf-8")
    names = [f"Карточка-{g}{i}" for g in "АБ" for i in range(5)]
    for n in names:
        grp = n.split("-")[1][0]
        mates = [m for m in names if m.split("-")[1][0] == grp and m != n]
        body = "Связана с " + ", ".join(f"[[{m}]]" for m in mates) + "."
        if n == "Карточка-А0":
            body += " И с [[Карточка-Б0|соседней темой]]."
        card(root, f"Concepts/{n}.md", body, status="knowledge", kind="knowledge")
    (kb / "MOC/Сообщества").mkdir(parents=True, exist_ok=True)
    stale = kb / "MOC/Сообщества/Тема-Прежняя.md"
    stale.write_text(f"---\ntype: moc\n---\n\n{G.GENERATED}\n\n# Прежняя\n", encoding="utf-8")
    mine = kb / "MOC/Сообщества/Моя-заметка.md"
    mine.write_text("# Написал человек\n", encoding="utf-8")
    here, was_min = os.getcwd(), G.COMMUNITY_MIN
    try:
        os.chdir(root)
        G.COMMUNITY_MIN = 3
        data = G.write_cards_graph("AuroraKnowledgeDB/meta/graph.json")
        done = G.export_graph(data)
        G.export_graph(data)                       # повтор не удваивает цвета
    finally:
        G.COMMUNITY_MIN = was_min
        os.chdir(here)
    gj = json.loads((root / G.GRAPH_EXPORT).read_text(encoding="utf-8"))
    ids = {n["id"] for n in gj["nodes"]}
    assert {"nodes", "links", "graph"} <= set(gj) and set(names) <= ids, ids
    rel = {(l["source"], l["target"]): (l["relation"], l["confidence"]) for l in gj["links"]}
    assert rel[("Карточка-А0", "Карточка-Б0")] == ("упоминает", "INFERRED"), rel
    assert rel[("Карточка-А0", "Карточка-А1")] == ("упоминает", "EXTRACTED"), rel
    assert done["communities"] == 2 and len(gj["graph"]["communities"]) == 2, gj["graph"]
    notes = sorted(p.name for p in (kb / "MOC/Сообщества").glob("*.md"))
    assert "Тема-Прежняя.md" not in notes and "Моя-заметка.md" in notes, notes
    assert any(n.startswith("Тема-Карточка-А") for n in notes), notes
    ob = json.loads((kb / ".obsidian/graph.json").read_text(encoding="utf-8"))
    qs = [g["query"] for g in ob["colorGroups"]]
    assert qs[0] == "BPMN" and qs.count("[status:knowledge]") == 1 and ob["scale"] == 2, qs
    sc = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    for tag in ("[update]", "[fix]"):
        part = sc.split(tag, 1)[1].split("\n[", 1)[0]
        assert part.index("kb:trust") < part.index("kb:graph-export"), f"{tag}: выгрузка до доверия"
    # выгрузки — производная на мегабайты: в git им не место, правило доезжает до проектов
    inst = (SCRIPTS / "install_aurora.py").read_text(encoding="utf-8")
    assert "AuroraKnowledgeDB/meta/graphify/" in inst, "выгрузки графа уйдут в историю проекта"
    kbg = (SCRIPTS / "kb_graph.py").read_text(encoding="utf-8")
    assert "graph_page(" in kbg and "GA.to_cypher(" in kbg, \
        "страница графа или выгрузка для Neo4j не подключены"


@test
def test_mechanical_links_do_not_guess(tmp: Path):
    """Механика связывает только названное: одно слово — целиком, рубрики — никогда.

    Замер 27.09.2026 на PRJ-B: с окончаниями у однословных имён «Участок» находил
    «участие», а рубрики «Алгоритмы», «Системы», «Справочники» получали ссылку от каждого
    упоминания слова — модель таких связей не ставила. Пара модели, задевающая уже
    стоящую ссылку, пропускается, а не роняет остальные связи карточки.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    importlib.reload(R)
    root = make_project(tmp)
    card(root, "Concepts/Участок.md", "Земельный участок.", status="draft", kind="knowledge")
    card(root, "Concepts/Алгоритмы.md", "Перечень алгоритмов.", status="draft", kind="knowledge",
         tags="[тема]")
    card(root, "Concepts/Налоговая-декларация.md", "Документ.", status="draft", kind="knowledge")
    card(root, "Concepts/НДС.md", "Налог.", status="draft", kind="knowledge")
    text = ("Участие инспектора обязательно. Алгоритм проверяет налоговую декларацию по НДС "
            "и сверяет её с участком учёта.")
    got, placed = R.mechanical_links(text, str(root), "Проверка")
    assert "Участок" not in placed and "Алгоритмы" not in placed, placed
    assert "[[Налоговая-декларация|налоговую декларацию]]" in got and "[[НДС]]" in got, got
    assert R.strip_links(got) == R.strip_links(text)
    # пара, которая задевает стоящую ссылку, не вставляется
    out, n = R.rescue_links("[[Декларация-по-НДС|декларацию по НДС]]", got)
    assert n == 0 and out == got, out


@test
def test_model_answers_are_cached_by_the_task(tmp: Path):
    """Тот же вызов с тем же заданием второй раз не оплачивается — ответ из кэша.

    Как семантический кэш graphify (1.138.0): встреча, сорвавшаяся на одном окне,
    переразбиралась целиком; пересборка с нуля и повторные прогоны оплачивали всё заново.
    Ключ — роль, кольцо моделей и задание: поменяли модель или задание — спросят снова.
    Разговор с историей, инструменты и тестовый транспорт в кэш не ходят.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AG = importlib.import_module("agent_core")
    saved = (AG.LLM_CACHE, AG._call_role, os.environ.pop("AURORA_TESTS_ISOLATED", None))
    AG.LLM_CACHE = tmp / "cache"
    calls = []

    def real(cfg, role, messages, *a):
        calls.append(role)
        return {"ok": True, "text": f"ответ {len(calls)}", "backend": 1, "model": "m",
                "tokens_in": 10, "tokens_out": 20}
    AG._call_role = real
    try:
        cfg = {"backends": [{"url": "u", "model": "m", "n": 1}], "thinking": False,
               "thinking_roles": {}}
        msgs = [{"role": "user", "content": "Напиши тезис"}]
        first = AG.call_role(cfg, "worker", msgs)
        again = AG.call_role(cfg, "worker", msgs)
        assert calls == ["worker"] and again["text"] == first["text"] and again["cached"], again
        assert again["tokens_out"] == 0, "ответ из кэша посчитан как потраченные токены"
        AG.call_role(cfg, "worker", [{"role": "user", "content": "Другое задание"}])
        AG.call_role(dict(cfg, backends=[{"url": "u", "model": "m2", "n": 1}]), "worker", msgs)
        assert len(calls) == 3, "другое задание или другая модель взяли чужой ответ"
        AG.call_role(cfg, "worker", msgs, history=[{"role": "user", "content": "раньше"}])
        AG.call_role(cfg, "worker", msgs, tools=True)
        assert len(calls) == 5, "разговор или инструменты ответили из кэша"
        os.environ["AURORA_AGENT_CACHE"] = "0"
        AG.call_role(cfg, "worker", msgs)
        assert len(calls) == 6, "выключенный кэш всё равно ответил"
    finally:
        os.environ.pop("AURORA_AGENT_CACHE", None)
        AG.LLM_CACHE, AG._call_role = saved[0], saved[1]
        if saved[2] is not None:
            os.environ["AURORA_TESTS_ISOLATED"] = saved[2]
    RS = importlib.import_module("run_summary")
    line = "\n".join(RS.render({"model_calls": 2, "tokens_in": 1, "tokens_out": 1,
                                "model_cached": 5, "seconds": 1}))
    assert "из кэша ответов 5" in line, line


@test
def test_engine_addons_show_versions_and_install_from_the_panel(tmp: Path):
    """Надстройки движка: Pydantic AI и graphify — версия, выпуск в git, установка из панели.

    Просьба пользователя 27.09.2026: обе надстройки предлагаются к установке и обновляются
    прямо из панели, с текущей версией и последней в git. Каждая — в своём venv; после
    graphify MCP-сервер графа базы подключается к серверам машины, чужие не трогаются.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    EX = importlib.import_module("aurora_extras")
    importlib.reload(EX)
    assert EX._clean("v0.9.69") == "0.9.69" and EX._clean("release-2.51.0") == "2.51.0"
    assert EX._vt("2.51.0") > EX._vt("2.33.0") and not EX._vt("0.9.69") > EX._vt("0.9.69")
    assert set(EX.EXTRAS) == {"pydantic-ai", "graphify"}
    assert EX.EXTRAS["graphify"]["min_python"] >= (3, 10), "graphify требует Python 3.10+"
    assert any("tree-sitter-sql" in spec for spec in EX.EXTRAS["graphify"]["pip"])

    saved = (EX.installed_version, EX.latest, EX.venv_python, EX.kit_mcp_file)
    fake_py = tmp / "gvenv/bin/python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("", encoding="utf-8")
    mcp = tmp / "kit/local/mcp.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"mcpServers": {"mcp-atlassian": {"command": "x"}}}),
                   encoding="utf-8")
    try:
        EX.installed_version = lambda eid: {"pydantic-ai": "2.33.0", "graphify": ""}[eid]
        EX.latest = lambda eid, fresh=False: {"git": "2.51.0" if eid == "pydantic-ai" else "0.9.69",
                                              "pypi": "2.51.0" if eid == "pydantic-ai" else "0.9.60",
                                              "error": ""}
        rows = {r["id"]: r for r in EX.status()}
        assert rows["pydantic-ai"]["update"] and rows["pydantic-ai"]["git"] == "2.51.0"
        assert not rows["graphify"]["installed"] and not rows["graphify"]["update"]
        assert rows["graphify"]["pypi_behind"], "git впереди PyPI — это надо сказать"
        EX.venv_python = lambda eid: fake_py
        EX.kit_mcp_file = lambda: mcp
        assert EX.register_graph_mcp() == ""
        servers = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]
        assert "mcp-atlassian" in servers and servers["aurora-graph"]["args"][-1].endswith(
            "graph.json"), servers
        assert oct(mcp.stat().st_mode)[-3:] == "600", "файл серверов машины открыт не только владельцу"
    finally:
        EX.installed_version, EX.latest, EX.venv_python, EX.kit_mcp_file = saved

    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"/api/extras"' in ck and '"/api/extras/install"' in ck
    view = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    assert "function extrasCard" in view and '"/api/extras/install"' in view
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/modules/install/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert {"install.extras", "install.ex_update", "install.ex_git", "install.ex_mcp_hint"} <= set(cat)
    core = (SCRIPTS / "agent_core.py").read_text(encoding="utf-8")
    assert 'EX.install("pydantic-ai")' in core, "две разные установки Pydantic AI"


@test
def test_graphify_is_optional_under_the_hood(tmp: Path):
    """graphify под капотом — необязателен: не стоит — движок делает то же своей механикой."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    GA = importlib.import_module("agents.graphify_adapter")
    G = importlib.import_module("kb_graph")
    was = GA.python
    GA.python = lambda: ""
    try:
        assert GA.cluster({"a": {"b"}, "b": {"a"}}) is None and GA.serve_argv("g.json") == []
        assert GA.code_graph(str(tmp), [str(tmp)]) is None
        lab = G.cluster({"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}})
        assert lab["a"] == lab["b"] != lab["c"] == lab["d"], lab
    finally:
        GA.python = was
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    for f in ("scripts/aurora_extras.py", "scripts/agents/graphify_adapter.py",
              "scripts/kb_code_graph.py"):
        assert f in man, f"{f} не доезжает до проектов"


@test
def test_sql_on_pages_joins_the_base_graph(tmp: Path):
    """SQL со страниц — в графе базы: таблицы, пары одного запроса, карточки, которые их называют.

    Без модели: блоки ```sql на страницах зеркала разбираются по FROM/JOIN, `public.x` и
    `x` — одна таблица, комментарий запроса таблиц не называет, оглавление — не упоминание.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    C = importlib.import_module("kb_code_graph")
    G = importlib.import_module("kb_graph")
    root = make_project(tmp)
    page = root / "Sources/Confluence/SQL.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: \"SQL\"\n---\n\n```sql\n-- из old\\_table\nselect * from "
                    "public.order\\_log l\njoin order\\_item d on d.id = l.id;\n"
                    "select 1 from orders\n```\n", encoding="utf-8")
    card(root, "Concepts/Журнал-пакетов.md", "Пакеты пишутся в order_log.",
         status="knowledge", kind="knowledge")
    card(root, "MOC/Оглавление.md", "order_log упомянут тут.", status="index", type="moc")
    tables, pairs = C.parse_sql(C.sql_sources(str(root)))
    assert set(tables) == {"order_log", "order_item", "orders"}, tables
    assert pairs == {("order_item", "order_log"): 1}, pairs
    got = C.card_mentions(str(root), sorted(tables))
    assert got == {"order_log": ["Журнал-пакетов"]}, got
    here = os.getcwd()
    try:
        os.chdir(root)
        assert run("kb_code_graph.py", "--apply", cwd=root).returncode == 0
        data = G.write_cards_graph("AuroraKnowledgeDB/meta/graph.json")
        G.export_graph(data)
    finally:
        os.chdir(here)
    gj = json.loads((root / G.GRAPH_EXPORT).read_text(encoding="utf-8"))
    rel = {(l["source"], l["target"]): l["relation"] for l in gj["links"]}
    assert rel.get(("Журнал-пакетов", "table:order_log")) == "упоминает таблицу", rel
    assert rel.get(("table:order_item", "table:order_log")) == "в одном запросе"


@test
def test_deferred_theses_do_not_stall_the_route_loop(tmp: Path):
    """Отложенные тезисы — не остаток оборота: цикл маршрута не выходит с «застоем».

    Прогон PRJ-A 27.09.2026 на 1.138.0: дописанные карточки ждали шага после цикла, а
    строки «осталось: 3» у тезисов и выноса цикл читал как работу оборота — на втором
    обороте ничего не убыло, и маршрут записал «застой», хотя всё шло по плану.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "Дописанная.md").write_text(
        "---\ntitle: \"Дописанная\"\nkind: knowledge\ndistilled_was: 2026-08-01\n---\n\n"
        "Прежний тезис.\n\n## Источник (перенесено дословно)\n\nНовый текст источника.\n",
        encoding="utf-8")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    assert R.distill_queue(cfg, str(root), defer_refresh=True) == []
    res = R.run_extract(cfg, str(root), apply=True, call=lambda *a, **k: {"ok": False})
    assert res["distill_left"] == 0, "отложенная карточка засчитана остатком оборота"
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert '"left": len(distill_queue(cfg, cwd, a.defer_refresh))' in src


@test
def test_web_sync_without_pages_says_one_line(tmp: Path):
    """`sync:web` в проекте без веб-страниц — одна строка, а не образец конфига на каждом прогоне.

    Живой случай, PRJ-A 22.09.2026: шаг стоит в «Обновить базу» и в каждом прогоне печатал
    двенадцать строк образца настройки. Образец переехал в `--help`.
    """
    root = make_project(tmp)
    out = run("web_export.py", "--apply", cwd=root).stdout.strip()
    assert len(out.splitlines()) == 1 and "нечего" in out, f"шаг без страниц шумит:\n{out}"
    helptext = run("web_export.py", "--help", cwd=root).stdout
    assert "pages:" in helptext and "trusted: true" in helptext, "образец блока пропал из --help"


@test
def test_source_maps_report_and_rewrite_only_what_changed(tmp: Path):
    """`kb:moc --by-source` печатает и переписывает только изменившиеся карты документов.

    Живой случай, PRJ-A 22.09.2026: шаг идёт в каждом обороте маршрута и каждый раз печатал
    таблицу всех 305 документов; а дата сборки в карте делала «изменёнными» все карты каждый
    новый день — сотни правок в git без единой новой ссылки.
    """
    root = make_project(tmp)
    for i in range(3):
        card(root, f"Concepts/Карта-{i}.md", status="draft", kind="knowledge",
             source='"Sources/Confluence/Док.md"', body=f"Знание {i}.")
    first = run("kb_moc.py", "--by-source", "--apply", cwd=root).stdout
    assert "| Док.md | 3 |" in first, first
    maps = list((root / "AuroraKnowledgeDB/MOC").glob("Документ--*.md"))
    assert len(maps) == 1
    # на следующий день — та же карта с другой датой: не изменение
    text = maps[0].read_text(encoding="utf-8")
    import re as _re
    maps[0].write_text(_re.sub(r"^updated: \S+$", "updated: 2000-01-01", text, flags=_re.M),
                       encoding="utf-8")
    again = run("kb_moc.py", "--by-source", "--apply", cwd=root).stdout
    assert "| Док.md |" not in again and "без изменений: 1" in again, \
        f"неизменившаяся карта снова напечатана или переписана:\n{again}"
    assert "2000-01-01" in maps[0].read_text(encoding="utf-8"), "карта переписана ради одной даты"


@test
def test_an_empty_build_plan_costs_nothing(tmp: Path):
    """Разбор с пустым планом — холостой шаг без чекпойнта, оракула и коммитов.

    Живой случай, PRJ-A 22.09.2026: «Источников в работе: 0», но ~100 с (линтер всей базы до
    и после) и два коммита на каждый оборот маршрута; а чекпойнт перед каждым шагом агента
    коммитил один журнал запусков панели как «работу человека».
    """
    root = make_project(tmp, git=True)
    g = lambda *args: subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    # как в настоящем проекте: кеш интерпретатора закрыт .gitignore
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    g("add", "-A"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "gitignore")
    count = lambda: len(g("log", "--oneline").stdout.splitlines())
    before = count()
    env = {**os.environ, "AURORA_TESTS_ISOLATED": "1",
           "AURORA_AGENT_BACKEND_1_URL": "http://127.0.0.1:9/v1", "AURORA_AGENT_BACKEND_1_MODEL": "m"}
    cp = subprocess.run([sys.executable, str(root / ".opencode/scripts/agent_runner.py"),
                         "--task", "build", "--apply", "--critic"],
                        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    assert cp.returncode == 0, cp.stderr[-600:]
    assert "Источников в плане: 0 → 0" in cp.stdout, \
        f"цикл маршрута не узнает, что источников не осталось:\n{cp.stdout[-400:]}"
    assert count() == before, "холостой разбор сделал коммиты"
    assert "Оракул" not in cp.stdout, "холостой разбор гонял оракула"

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    log = root / ".opencode/run_log.md"
    log.write_text("| kb:kind | 2026-09-22 | 0 |\n", encoding="utf-8")
    got = R.checkpoint(str(root), "agent:distill", True)
    assert got["ok"] and got["committed"] == 0 and count() == before, \
        f"чекпойнт закоммитил журнал запусков панели как работу человека: {got}"
    (root / "AuroraKnowledgeDB/Concepts").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/Concepts/Правка.md").write_text("# Правка\n", encoding="utf-8")
    got = R.checkpoint(str(root), "agent:distill", True)
    assert got["committed"] == 2 and count() == before + 1, \
        f"настоящая работа человека не зафиксирована: {got}"


@test
def test_twins_do_not_compare_generated_maps(tmp: Path):
    """Двойников ищут среди карточек знания, а не среди порождённых карт и индексов MOC/.

    Живой случай, PRJ-A 22.09.2026: `agent:twins` разбирал пары индексов кодов («Epic-10» /
    «US-10.1») — списки чужих карточек, похожие друг на друга списком, — вызов модели на каждую.
    """
    root = make_project(tmp)
    body = "\n".join(f"- [[Карточка-знания-номер-{i}]] — описание пункта {i} списка" for i in range(40))
    for name in ("US-6.1.4", "US-6.1.8"):
        card(root, f"MOC/{name}.md", status="index", kind="document", body=body)
    out = run("kb_twins.py", "--min", "0.3", "--limit", "0", cwd=root).stdout
    assert "US-6.1.4" not in out and "US-6.1.8" not in out, \
        f"карты содержания сравниваются как двойники:\n{out[-500:]}"


@test
def test_card_links_explain_what_did_not_fit(tmp: Path):
    """`kb:links --cards` объясняет «не влезло», а не бросает голое число.

    Живой случай, PRJ-A 22.09.2026: «не влезло: 127» без пояснения читалось как потеря связей.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    G = importlib.import_module("kb_graph")
    root = make_project(tmp)
    paths = {n: card(root, f"Concepts/{n}.md", status="draft", kind="knowledge", body=f"Про {n}.")
             for n in ("А", "Б", "В", "Г")}
    rel = lambda n: os.path.join("AuroraKnowledgeDB", "Concepts", n + ".md")
    here = os.getcwd()
    try:
        os.chdir(root)
        st = G.apply_card_links({rel("А"): {rel("Б"), rel("В"), rel("Г")}}, False, 1)
    finally:
        os.chdir(here)
    assert st["не влезло"] == 2, st
    assert "сверх предела 1 на карточку" in G.overflow_note(1)
    src = (SCRIPTS / "kb_graph.py").read_text(encoding="utf-8")
    assert 'if st.get("не влезло"):\n            print("\\n" + overflow_note(a.max_related))' in src, \
        "число «не влезло» печатается без объяснения"


@test
def test_a_step_that_fails_to_start_leaves_a_trace(tmp: Path):
    """Команда, упавшая на самом запуске, оставляет причину в архиве прогона.

    Живой случай, PRJ-A 21.09: kb:embed и дважды sync:confluence встали с кодом 2 — ни
    строки вывода, ни папки прогона. Папка архива заводилась только у запустившегося
    процесса, а причина отказа жила в памяти панели до её перезапуска.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    root = make_project(tmp, git=False)
    real_popen, real_running = ck.subprocess.Popen, ck.RUNNING

    def broken(*a, **kw):
        raise OSError("интерпретатор не найден")

    ck.subprocess.Popen = broken
    ck.RUNNING = str(tmp / "running.json")     # настоящий список заданий кита не трогаем
    try:
        jid = ck.start_job(str(root), "kb:lint", [])
        job = ck.JOBS[jid]
        for _ in range(200):
            if job["done"]:
                break
            time.sleep(0.05)
    finally:
        ck.subprocess.Popen, ck.RUNNING = real_popen, real_running
    assert job["done"] and job["rc"] == 2, f"упавший запуск не завершил задание: {job}"
    assert any("команда не запустилась" in l and "интерпретатор не найден" in l
               for l in job["out"]), f"причина не показана: {job['out']}"
    log = Path(ck.runs_dir(str(root))) / job["run_id"] / "console.log"
    assert log.is_file(), "упавший запуск не оставил папки прогона"
    text = log.read_text(encoding="utf-8")
    assert "интерпретатор не найден" in text and "Traceback" in text, \
        f"в архиве нет причины и места падения: {text!r}"


@test
def test_a_route_says_why_a_step_did_not_start_and_saves_its_tail(tmp: Path):
    """Отказ сервера виден в консоли и в событиях шага; результат маршрута зафиксирован.

    Отказ маршруту (движок проекта отстал от кита) показывался всплывающим окном на четыре
    секунды, а в консоли и в итоге стояло «команда не отработала» — хотя команда даже не
    запускалась. И хвост: шаги после цикла (карты, оглавления, трассировка, доверие) не
    фиксировались — в PRJ-A 22.09 после «пройден» осталось 163 незакоммиченных файла.
    """
    ui = panel_sources()
    step = ui[ui.index("async function runStep("):ui.index("const FIX_RUN = {")]
    refuse = step[step.index("if (!res.job)"):step.index("let since = 0")]
    assert "res.error" in refuse and "out.append(" in refuse, \
        "причина отказа не попадает в консоль"
    assert "refused: why" in refuse and "rc:2" in refuse, \
        "отказ неотличим от упавшей команды"
    run = ui[ui.index("async function runRoute("):ui.index("async function resumeLastRoute(")]
    assert "note: res.refused" in run, "в событиях шага нет причины отказа"
    assert "ROUTE.refused ?" in run, "итог маршрута говорит «не отработала» вместо причины"
    tail = run[run.index("const bad = ROUTE.failed"):run.index("ROUTE = null;")]
    assert '"/api/git/commit"' in tail and "if (write && S.project)" in tail, \
        "результат маршрута после цикла не фиксируется"
    assert "skip_ratchet:true" in tail and 't("route.not_saved"' in tail, \
        "фиксация хвоста встанет на храповике или провалится молча"


@test
def test_a_rejected_relink_answer_gets_a_second_try_and_stays_queued(tmp: Path):
    """Неразобранный ответ связывания: механика записана, карточка остаётся в очереди.

    Черновик находок PRJ-A 22.09.2026, №34: отброшенная карточка получала отметку
    `relinked` и больше в очередь не возвращалась — без единой связи (23 из 187). С
    1.138.0 модель возвращает пары, а не текст, и повтор «ты изменила текст» не нужен:
    ответ, который не разобран, — один вызов, связи механики записаны, отметки нет.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    thesis = ("Аналитический баланс получает данные из ФЦОД по расписанию. Сверка "
              "проводится ежедневно и фиксируется в журнале операций. ") * 2
    card(root, "Concepts/ФЦОД.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body="Подсистема обработки платежей. " * 6)
    card(root, "Concepts/Журнал-операций.md", status="draft", kind="knowledge",
         distilled="2026-09-01", body="Журнал, где фиксируется каждая операция. " * 4)
    second = card(root, "Concepts/Сверка.md", status="draft", kind="knowledge",
                  distilled="2026-09-01", body=thesis)
    cfg = {"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
           "thinking_roles": {}, "thinking": False, "backends": [], "parallel": 1}
    seen = []

    def garbled(c, role, messages, **kw):
        seen.append(messages)
        return {"ok": True, "backend": 1, "model": "m", "log": [], "text": "не JSON вовсе"}

    was = R.candidates_for
    R.candidates_for = lambda *a, **k: [("Вне-текста", "Concepts", "")]
    here = os.getcwd()
    try:
        os.chdir(root)
        res = R.run_relink(cfg, str(root), True, call=garbled)
    finally:
        R.candidates_for = was
        os.chdir(here)
    by = {s["card"]: s for s in res["steps"]}
    assert by["Сверка"]["status"] == "отброшен" and "JSON" in by["Сверка"]["note"], by
    assert len([m for m in seen]) == len([s for s in res["steps"] if s["backends"]]), \
        "на отказ ушло больше одного вызова на карточку"
    text = second.read_text(encoding="utf-8")
    assert "[[ФЦОД]]" in text and "[[Журнал-операций|журнале операций]]" in text, \
        "связи механики потеряны вместе с ответом модели"
    assert "relinked:" not in text, "отброшенная карточка отмечена связанной — уйдёт из очереди"


@test
def test_relinking_keeps_the_source_section_on_its_own_line(tmp: Path):
    """Связывание не приклеивает заголовок дословного текста к тезису; ремонт расклеивает.

    Найдено при сверке 23.09.2026: связывание писало ответ модели после `strip()` и теряло
    пустые строки перед «## Источник (перенесено дословно)». Заголовок прилипал к последней
    фразе тезиса — на PRJ-A у 269 карточек, по четырём проектам 833. Движок раздел находит
    подстрокой, но человек и Markdown видят дословный текст продолжением тезиса.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    import aurora_common as AC
    root = make_project(tmp, git=False)
    thesis = ("Аналитический баланс получает данные из ФЦОД по расписанию. Сверка "
              "проводится ежедневно и фиксируется в журнале операций. ") * 2
    card(root, "Concepts/ФЦОД.md", status="draft", kind="knowledge", distilled="2026-09-01",
         body="Подсистема обработки платежей. " * 6)
    path = card(root, "Concepts/Баланс.md", status="draft", kind="knowledge",
                distilled="2026-09-01",
                body=thesis.strip() + "\n\n" + AC.QUOTES + "\n\nИсходный абзац источника.\n")
    linked = thesis.strip().replace("из ФЦОД", "из [[ФЦОД]]", 1)
    was = R.candidates_for
    R.candidates_for = lambda *a, **k: [("ФЦОД", "Concepts", "Подсистема платежей")]
    here = os.getcwd()
    try:
        os.chdir(root)
        st = R.relink_card({"request_timeout": 60, "budget_min": 5, "embed": {"model": "m"},
                            "thinking_roles": {}, "thinking": False, "backends": []},
                           str(path), lambda *a, **k: {"ok": True, "backend": 1, "model": "m",
                                                       "log": [], "text": linked + "\n"},
                           apply=True)
    finally:
        os.chdir(here)
        R.candidates_for = was
    assert st["status"] == "связана", st
    text = path.read_text(encoding="utf-8")
    assert "журнале операций.\n\n" + AC.QUOTES in text, \
        f"заголовок раздела прилип к тезису: {text[-200:]!r}"

    # ремонт уже склеенных: заголовок — с новой строки, текст не меняется
    glued = card(root, "Concepts/Склеенная.md", status="draft", kind="knowledge",
                 distilled="2026-09-01",
                 body="Тезис о склеенной карточке, достаточно длинный для проверки."
                      + AC.QUOTES + "\n\nИсходный абзац.\n")
    cp = run("kb_fix.py", "--frontmatter", "--apply", "--allow-dirty", cwd=root)
    fixed = glued.read_text(encoding="utf-8")
    assert "для проверки.\n\n" + AC.QUOTES in fixed, fixed
    assert "раздел дословного текста снова с новой строки" in cp.stdout, cp.stdout[-800:]
    assert AC.unglue_quotes(fixed)[1] == 0, "ремонт не довёл до неподвижной точки"


@test
def test_extract_finds_the_term_card_in_another_word_form(tmp: Path):
    """Вынос кладёт определение в карточку того же термина в другой форме слова.

    Черновик находок PRJ-A 22.09.2026, №25: вынос заводил двойников рядом с уже
    заведёнными и в одном проходе — термин приходит в той форме, в какой назван в тексте.
    Сверка 23.09 по четырём проектам нашла таких групп семь («Заявители» и «Заявитель»,
    «НАЛОГЕ», «НАЛОГОВ» и «НАЛОГУ»); ложных среди них не было. Отчёт двойников их теперь
    называет, а слияние по правилу не трогает: решает модель (`agent:twins`).
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    BP = importlib.import_module("build_plan")
    R = importlib.import_module("agent_runner")
    assert BP.term_key("Заявители") == BP.term_key("Заявитель")
    assert BP.term_key("Итоговые расчёты") == BP.term_key("Итоговый-расчёт")
    assert BP.term_key("Статус НП") != BP.term_key("Статус НО"), "короткие слова потеряны"
    assert BP.term_key("Корректируемый ЭСФ") != BP.term_key("Корректировочный ЭСФ"), \
        "ключ склеил разные понятия"

    root = make_project(tmp, git=False)
    card(root, "Concepts/Заявитель.md", status="draft", kind="knowledge",
         body="Заявитель — лицо, подающее заявку на регистрацию.")
    assert R.definition_link(str(root), "заявители") == "[[Заявитель|заявители]]"
    got = R.place_definition(str(root), "заявители", "Заявители подают заявки через портал.",
                             "Донор")
    assert got.endswith("Concepts/Заявитель.md"), f"заведён двойник: {got}"
    assert "подают заявки через портал" in Path(got).read_text(encoding="utf-8")
    # в одном проходе: вторая форма того же термина едет в только что заведённую карточку
    first = R.place_definition(str(root), "Текущие расчёты", "Текущие расчёты — расчёты "
                               "за открытый период.", "Донор")
    second = R.place_definition(str(root), "текущий расчёт", "Текущий расчёт пересчитывается "
                                "ночью.", "Донор")
    assert first == second, f"одно понятие в двух карточках: {first} и {second}"

    # отчёт двойников называет группу, слияние по правилу её не трогает
    sys.path.insert(0, str(SCRIPTS))
    card(root, "Concepts/Заявители.md", status="draft", kind="knowledge",
         body="Заявители — физические и юридические лица.")
    cp = run("kb_fix.py", "--dupes", cwd=root)
    assert "одно понятие в разных формах слова" in cp.stdout and "Заявители.md" in cp.stdout, \
        cp.stdout[-1200:]
    cp = run("kb_fix.py", "--merge-all", cwd=root)
    assert "формы одного слова — судит модель" in cp.stdout, cp.stdout[-1200:]
    wf = json.loads(run("kb_fix.py", "--word-forms", cwd=root).stdout.strip().splitlines()[-1])
    assert any(set(g) == {"Заявители", "Заявитель"} for g in wf), \
        f"пара живых форм слова не дошла до модели (`agent:twins`): {wf}"


@test
def test_extract_does_not_say_the_same_thing_twice(tmp: Path):
    """Вынос не повторяет ни связку, ни имя термина рядом со ссылкой на него.

    Найдено при сверке 23.09.2026 по истории PRJ-A: «Закрытие расхождений описывается как
    Закрытие расхождений описывается как [[…]]» (модель вернула в `keep` текст, который и
    так стоит перед определением) и «Автоматические связи [[Автоматические связи]]» (термин
    остался перед ссылкой на себя). Пример из самого задания выноса давал «подсистемы
    ФЦОД, [[ФЦОД]]» вместо задуманного «подсистемы [[ФЦОД]]».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp, git=False)
    cases = [
        ("Закрытие расхождений описывается как отбор нужных расхождений и возврат к реестру.",
         {"term": "Порядок закрытия", "definition": "отбор нужных расхождений и возврат к реестру",
          "keep": "Закрытие расхождений описывается как"},
         "Закрытие расхождений описывается как [[Порядок-закрытия|Порядок закрытия]]."),
        ("Автоматические связи между НП на ГС имеют описание и список связей.",
         {"term": "Автоматические связи", "definition": "между НП на ГС имеют описание и список связей",
          "keep": ""},
         None),     # предложение целиком ушло в карточку термина — голой ссылки не остаётся
        ("Баланс получает информацию из подсистемы ФЦОД, которая является частью проекта МинФин.",
         {"term": "ФЦОД", "definition": "которая является частью проекта МинФин", "keep": ""},
         "Баланс получает информацию из подсистемы [[ФЦОД]]."),
        ("Отчёт уходит в НОФЦОД, которая является частью проекта МинФин.",
         {"term": "ФЦОД", "definition": "которая является частью проекта МинФин", "keep": ""},
         "Отчёт уходит в НОФЦОД, [[ФЦОД]]."),
    ]
    for i, (thesis, item, want) in enumerate(cases):
        path = card(root, f"Concepts/Донор-{i}.md", status="draft", kind="knowledge",
                    distilled="2026-09-01", body=thesis)
        text = path.read_text(encoding="utf-8")
        R.apply_extract_plan(str(root), str(path), text, thesis, [item], True)
        got = path.read_text(encoding="utf-8")
        if want is None:
            own = got.split("\n---", 1)[1]
            assert "[[Автоматические-связи" not in own and "связи Автоматические" not in own, \
                f"случай {i}: осталась голая ссылка или повтор имени\n{got[-300:]}"
            continue
        assert want in got, f"случай {i}: ждали «{want}»\n{got[-300:]}"


@test
def test_our_own_queue_is_not_a_dead_backend(tmp: Path):
    """Срок, истёкший, пока к бэкенду висят наши же запросы, — очередь, а не карантин.

    Черновик находок PRJ-A 22.09.2026, №11: №2 ещё ни разу не ответил за прогон, а движок
    держал к нему десятки запросов на один слот; первый же истёкший срок сажал его в
    карантин на 15 минут. Мёртвый сервер по-прежнему уходит в карантин — последним
    запросом волны, когда наших к нему больше не висит.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_REQUEST_TIMEOUT": "300"})

    def silent(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        return (None, None, "TimeoutError: timed out", timeout)

    A.DOWN.clear(); A.LAST_OK.clear(); A.INFLIGHT.clear()
    A.INFLIGHT[1] = 3                       # ещё три наших запроса к №1 в сети
    try:
        r = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=silent,
                        deadline=time.time() + 3, sleep=lambda s: None, request_timeout=2)
    finally:
        A.INFLIGHT.clear()
    assert not r["ok"] and 1 not in A.DOWN, "очередь из наших же запросов принята за смерть"
    assert any("наших запросов" in l for l in r["log"]), r["log"]

    A.DOWN.clear()
    r = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=silent,
                    deadline=time.time() + 3, sleep=lambda s: None, request_timeout=2)
    assert 1 in A.DOWN, "молчащий без наших запросов рядом больше не уходит в карантин"
    assert A.INFLIGHT.get(1, 0) == 0, f"счёт запросов в сети не вернулся к нулю: {A.INFLIGHT}"
    A.DOWN.clear()


@test
def test_the_build_oracle_does_not_blame_new_cards_for_having_no_links_yet(tmp: Path):
    """Оракул разбора не считает ухудшением карточки, которым связи поставит следующий шаг.

    Черновик находок PRJ-A 22.09.2026, №6: «ошибок в базе стало больше: 10 → 13», и разбор
    вышел с кодом 1. Сверка линтером до и после показала единственную разницу — «карточки
    без связей: 214 → 217», три только что заведённые карточки. Связи им ставит
    `agent:relink` дальше по маршруту.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp, git=False)
    card(root, "Concepts/Баланс.md", status="draft", kind="knowledge",
         body="Баланс получает данные из [[ФЦОД]].")
    card(root, "Concepts/ФЦОД.md", status="draft", kind="knowledge",
         body="Подсистема платежей, питает [[Баланс]].")
    was_all, was = R.lint_errors(str(root)), R.lint_errors(str(root), orphans=False)
    assert was >= 0, "оракул не прочёл счёт линтера"
    card(root, "Concepts/Новая.md", status="draft", kind="knowledge",
         body="Только что разобранная карточка, связей у неё ещё нет.")
    assert R.lint_errors(str(root)) == was_all + 1, "линтер перестал видеть карточку без связей"
    assert R.lint_errors(str(root), orphans=False) == was, \
        "оракул разбора считает новую карточку без связей ухудшением базы"
    card(root, "Concepts/Битая.md", status="draft", kind="knowledge",
         body="Ссылается на [[Нет-такой-карточки]] и на [[Баланс]].")
    assert R.lint_errors(str(root), orphans=False) > was, "оракул разбора ослеп к битым ссылкам"
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    run = src[src.index("def run_build("):src.index("def verdict_build(")]
    assert run.count("lint_errors(cwd, orphans=False)") == 2, "разбор считает оракул по-старому"


@test
def test_unfinished_sections_open_together_with_the_dev_section(tmp: Path):
    """«Граф» и «Отчёты» ещё в разработке: в меню их нет, пока не открыта «Разработка».

    Просьба пользователя 23.09.2026: скрыть их так же, как «Разработку», и открывать вместе
    с ней — семью нажатиями на «О проекте». Встроенный раздел помечается в разметке
    (`data-devonly`), модуль — полем `"dev": true` в манифесте; по адресу страницы в
    закрытый раздел не попасть, кнопка «На графе» в файлах тоже спрятана.
    """
    ui = panel_sources()
    # «Граф» с 1.125.0 — раздел-папка, и в разработке он значится манифестом (ниже).
    # Разметка по-прежнему умеет прятать встроенный раздел: приём остаётся для тех,
    # кто ещё не переехал.
    nav = ui[ui.index("function showDevNav("):ui.index("function tapAbout(")]
    assert 'nav button[data-devonly]' in nav, "разделы в разработке не открываются с «Разработкой»"
    assert "if (m.dev)" in ui and "btn.hidden = !devSectionsOn()" in ui, \
        "модуль с `dev` попадает в меню сразу"
    show = ui[ui.index("function show(view"):ui.index("let ABOUT_TAPS")]
    assert "devOnlyView(view) && !devSectionsOn()" in show, "в закрытый раздел пускает адрес страницы"
    assert '$("#fileGraph").hidden = !inKb || !devSectionsOn()' in ui, \
        "кнопка «На графе» ведёт в закрытый раздел"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    mods = {m["id"]: m for m in ck.modules()}
    unfinished = ("reports", "graph", "dev")
    for mod in unfinished:
        assert mods[mod].get("dev") is True, f"«{mod}» не помечен разделом в разработке"
    assert not any(m.get("dev") for i, m in mods.items() if i not in unfinished), \
        "в разработку попали готовые разделы"
    readme = (KIT / "cockpit/modules/README.md").read_text(encoding="utf-8")
    assert "| `dev` |" in readme, "поле `dev` не описано в контракте модулей"
