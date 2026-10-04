"""Проверки движка Aurora, часть 4 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import time

from harness import (  # noqa: F401
    ui_source,
    KIT,
    SCRIPTS,
    card,
    make_project,
    panel_sources,
    run,
    test,
    why,
)


@test
def test_cards_are_distilled_side_by_side(tmp: Path):
    """Карточки разбираются одновременно: ожидание шлюза — не работа машины.

    Разбор был строго последовательным: один запрос в воздухе, пока шлюз обслуживает
    несколько. На 1359 карточках это ночь вместо часа. Замер на подставном вызове по
    0.3 с: восемь карточек последовательно — 2.4 с, восемью потоками — 0.3 с.

    Умолчание остаётся прежним (1): ставить ширину больше, чем держит шлюз, бессмысленно,
    и решать это должен человек, знающий свой сервер.
    """
    import time
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    kb = tmp / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True)
    for i in range(6):
        (kb / f"К{i}.md").write_text(
            f'---\nid: KB-{i}\ntitle: "К{i}"\nkind: knowledge\nstatus: knowledge\n'
            f'---\n\nтело {i}\n', encoding="utf-8")

    import threading
    gate = threading.Lock()
    live = {"now": 0, "peak": 0}

    def slow(cfg, role, messages, **kw):
        with gate:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.2)
        with gate:
            live["now"] -= 1
        return {"ok": True, "text": "ТЕЗИС: тезис\nОПОРА: цитата", "backend": 1,
                "model": "тест", "seconds": 0.2, "tps": 10, "log": []}

    def go(width):
        cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                              "AURORA_AGENT_BACKEND_1_MODEL": "m",
                              "AURORA_AGENT_PARALLEL": str(width)})
        assert cfg["parallel"] == width, "ширина не читается из настроек"
        live["peak"] = 0
        t0 = time.time()
        res = R.run_distill(cfg, str(tmp), apply=False, limit=6, momus=False, call=slow)
        return time.time() - t0, res, live["peak"]

    one, res1, peak1 = go(1)
    many, res6, peak6 = go(6)
    assert len(res1["steps"]) == len(res6["steps"]) == 6, "часть карточек потерялась"
    # Параллельность считаем по числу одновременных обращений к модели, а не по секундомеру: на
    # общей машине CI (Windows) накладные расходы потоков съедали выигрыш, и время плавало.
    assert peak1 == 1 and peak6 >= 3, \
        f"ширина не работает: при 1 одновременно {peak1}, при 6 — {peak6} обращений"


@test
def test_distill_does_not_write_theses_for_placeholders(tmp: Path):
    """Заготовке тезис не пишется: писать не из чего, а пересказ пустоты её маскирует.

    `agent:distill` единственный из шагов агента брал пустышку: модель пересказывала «знаний
    пока нет» своими словами, служебная строка `_Заготовка:` исчезала, вызов был оплачен
    впустую, а ремонт затем принимал пересказ за знание и снимал отметку. На живом проекте
    все 159 пустышек, ушедших в поиск как знание, прошли через этот шаг.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    kb = tmp / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True)
    (kb / "Знание.md").write_text(
        '---\ntitle: "Знание"\nkind: knowledge\nstatus: draft\n---\n\nтело знания\n',
        encoding="utf-8")
    (kb / "Пустышка.md").write_text(
        '---\ntitle: "Пустышка"\nkind: knowledge\nstatus: placeholder\ntags: [заготовка]\n'
        '---\n\n_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n\n'
        '## Упоминается в\n\n- [[Знание]]\n', encoding="utf-8")
    asked = []

    def spy(cfg, role, messages, **kw):
        asked.append(" ".join(m.get("content") for m in messages
                              if isinstance(m.get("content"), str)))
        return {"ok": True, "text": "ТЕЗИС: тезис\nОПОРА: цитата", "backend": 1,
                "model": "тест", "seconds": 0.1, "tps": 10, "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    res = R.run_distill(cfg, str(tmp), apply=False, limit=10, momus=False, call=spy)
    assert len(res["steps"]) == 1, f"тезис писался заготовке: шагов {len(res['steps'])}"
    assert not any("_Заготовка:" in a for a in asked), \
        "модели отдали пустышку — вызов оплачен впустую, а пересказ снимет с неё отметку"


@test
def test_reset_keeps_only_what_it_cannot_identify(tmp: Path):
    """«Нет источника» ≠ «писал человек». Оставляем только НЕОПОЗНАННОЕ — и говорим об этом.

    Сначала защита висела на именах папок (`Decisions/`, `Questions/`, `Reference/`) и
    ошибалась в обе стороны: на живом проекте внутри них невосстановимых 31 из 269, а
    снаружи 451. Потом — на отсутствии `source:`, и это оказалось не лучше: из 425
    карточек без источника 137 создал `kb:repair --stubs` заготовками, 64 пришли
    массовым коммитом, а рукотворными были единицы. Беречь заготовки вредно вдвойне:
    пересборка их не вернёт, а держать пустышки незачем.

    Верный признак — положительный: машина ставит `built: machine` сама. На пересобранной
    базе таких 965 из 965, и неопознанных не остаётся вовсе. Что не опознано — остаётся,
    но названо своим именем: «происхождение неизвестно», а не «ваша работа».
    """
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("текст", encoding="utf-8")
    card(root, "Concepts/Из-зеркала.md", "тело", status="knowledge",
         source="Sources/Confluence/Док.md", built="machine")
    # заготовка старого движка: источника нет, метки нет — но и человек её не писал
    card(root, "Concepts/Заготовка.md", "тело", status="knowledge", built="machine")
    # а это уже неопознанное: ни того, ни другого
    card(root, "Concepts/Неопознанная.md", "тело", status="knowledge")
    card(root, "Decisions/DR-001.md", "почему выбрали так", status="knowledge")

    cp = run("kb_reset.py", cwd=root, expect_rc=0)
    assert "происхождение неизвестно" in cp.stdout, "неопознанное не названо своим именем"
    assert "НЕ обязательно ваша работа" in cp.stdout, \
        "движок выдаёт догадку за факт — именно так он и ошибся дважды"

    named = run("kb_reset.py", "--list-unknown", cwd=root, expect_rc=0)
    assert "Неопознанная" in named.stdout and "Заготовка" not in named.stdout, \
        f"список неопознанных собран неверно:\n{named.stdout}"
    assert "Всего: 2" in named.stdout, "в списке не все неопознанные (DR тоже без метки)"

    run("kb_reset.py", "--apply", cwd=root, expect_rc=0)
    assert not (root / "AuroraKnowledgeDB/Concepts/Из-зеркала.md").exists(), \
        "карточка с живым источником пережила сброс — после пересборки будет двойник"
    assert not (root / "AuroraKnowledgeDB/Concepts/Заготовка.md").exists(), \
        "заготовка сбережена: пересборка её не вернёт, но и держать пустышку незачем"
    assert (root / "AuroraKnowledgeDB/Concepts/Неопознанная.md").exists(), \
        "снесено то, про что движок не знает, чьё оно"

    cp2 = run("kb_reset.py", "--drop-unknown", "--apply", "--allow-dirty", cwd=root,
              expect_rc=0)
    assert "неизвестного происхождения" in cp2.stdout, "полный снос не назвал потерю"
    assert not (root / "AuroraKnowledgeDB/Concepts/Неопознанная.md").exists(), \
        "--drop-unknown не снёс то, ради чего его просят"


@test
def test_console_stops_chasing_the_bottom_when_you_scroll_up(tmp: Path):
    """Отмотал вверх — консоль перестаёт прыгать в конец.

    Живой случай, слово в слово: «не могу скролить вывод консоли, она каждую секунду
    сама проматывается в конец». Вывод длинного прогона так не прочитать: человек ищет,
    когда всё началось, или было ли переключение на запасную модель, — а страница каждые
    450 мс возвращает его в конец. Слежение включается обратно само, когда он домотает
    вниз, и кнопкой «↓ вывод продолжается».
    """
    ui = panel_sources()
    assert "function stickToBottom(" in ui and "function watchScroll(" in ui, \
        "нет отдельной прокрутки со слежением"
    assert 'box.dataset.follow === "0"' in ui, "прокрутка не спрашивает, смотрит ли человек конец"
    assert ui.count("scrollTop = out.scrollHeight") == 0, \
        "осталась безусловная прокрутка — она перебьёт слежение в одном из трёх мест"
    assert ui.count("stickToBottom(out)") >= 3, \
        "не все места вывода переведены на слежение: маршрут, шаг и обычная команда"
    assert 'id="consoleTail"' in ui, "нет кнопки возврата в конец: слежение выключилось молча"
    assert "followAgain(" in ui, "вернуться к слежению нечем"


@test
def test_reset_warns_about_facts_not_folder_names(tmp: Path):
    """«Заново не выведется» — про карточки без документа, а не про имя раздела.

    Предупреждение висело на списке разделов: Reference, meta, Decisions. На живом
    проекте это оказалось неправдой — все 105 карточек `Reference/` имели `source:` в
    зеркале и вернулись бы сами. Человек читал «⚠️ заново не выведется» и понимал это
    как «удалятся исходные документы», хотя за пределами базы не трогается ничего.

    Считать надо факт: есть ли за карточкой файл, из которого её собрали. Карты и
    оглавления исключение — их собирают из самой базы.
    """
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("текст", encoding="utf-8")
    card(root, "Reference/Из-зеркала.md", "тело", status="knowledge",
         source="Sources/Confluence/Док.md")
    card(root, "Reference/Ниоткуда.md", "тело", status="knowledge")
    card(root, "Reference/Ссылка-в-никуда.md", "тело", status="knowledge",
         source="Sources/Confluence/Пропал.md")
    (root / "AuroraKnowledgeDB/MOC").mkdir(parents=True, exist_ok=True)
    card(root, "MOC/Карта.md", "карта", status="index")

    cp = run("kb_reset.py", "--drop-unknown", cwd=root, expect_rc=0)
    assert "Reference: 3  ⚠️ из них 2 не выведется" in cp.stdout, \
        f"счёт невосстановимых считается не по факту:\n{cp.stdout}"
    assert "MOC: 1" in cp.stdout and "MOC: 1  ⚠️" not in cp.stdout, \
        "карты объявлены потерей — их собирает kb:moc из самой базы"
    assert "Идут под снос 2 карточек" in cp.stdout, "нет итоговой строки"
    assert "Sources/, Raw/" in cp.stdout, "не сказано, что источники не трогаются"

    # база, целиком выведенная из источников, не должна пугать вовсе
    (root / "AuroraKnowledgeDB/Reference/Ниоткуда.md").unlink()
    (root / "AuroraKnowledgeDB/Reference/Ссылка-в-никуда.md").unlink()
    cp2 = run("kb_reset.py", "--drop-unknown", cwd=root, expect_rc=0)
    assert "⚠️ из них" not in cp2.stdout, "предупреждение осталось там, где терять нечего"
    assert "Идут под снос" not in cp2.stdout, "предупреждение о потере без потери"


@test
def test_refusing_to_write_stops_the_route(tmp: Path):
    """Отказ писать — код 2, а не 1: маршрут обязан встать, а не идти дальше.

    Живой случай: на пересборке базы `kb:reset` уперлась в git-guard (294 незакоммиченных
    файла) и вернула 1. Единица в панели значит «команда отработала и нашла, что чинить»,
    поэтому маршрут пошёл дальше — по НЕ сброшенной базе. `agent:build` увидел ноль
    источников (разбирать нечего, всё на месте) и отрапортовал успехом, `kb:kind` тоже.
    Четыре шага из четырнадцати объявили себя выполненными, не сделав ничего.

    Разница смысловая: 1 — «работа сделана, есть замечания», 2 — «работа не сделана».
    Отказ git-guard это всегда второе.
    """
    root = make_project(tmp)
    card(root, "Systems/Одна.md", "тело см. [[Две]]", status="knowledge")
    card(root, "Systems/Две.md", "тело см. [[Одна]]", status="knowledge")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)
    (root / "AuroraKnowledgeDB/Systems/Одна.md").write_text("грязь\n", encoding="utf-8")

    cp = run("kb_reset.py", "--apply", cwd=root, expect_rc=2)
    assert "git-guard" in cp.stdout + cp.stderr, "отказ не объяснён"
    assert (root / "AuroraKnowledgeDB/Systems/Две.md").exists(), \
        "guard отказал, а файлы всё равно удалены"

    # правило общее: ни одна команда не отвечает единицей на отказ писать
    for name in ("kb_reset.py", "kb_moc.py", "kb_graph.py", "kb_schema.py",
                 "kb_scrub.py", "jira_status.py", "sync_audit.py"):
        code = (KIT / "scripts" / name).read_text(encoding="utf-8")
        for i, line in enumerate(code.splitlines()):
            if "git_guard(" in line and "def " not in line:
                tail = "\n".join(code.splitlines()[i:i + 4])
                assert "return 1" not in tail, \
                    f"{name}: отказ писать возвращает 1 — маршрут пойдёт дальше вхолостую"


@test
def test_a_route_will_not_run_on_two_engine_versions(tmp: Path):
    """Маршрут не начинается, когда движок проекта отстал от кита.

    Живой случай: панель кита 1.92 на проекте с движком 1.85. Скрипт, которого в проекте
    нет, берётся из кита — так задумано, чтобы новая команда работала сразу. `kb:kind`
    пришла из кита и отработала; `agent:distill` попала в `agent_runner.py` проекта, где
    такой задачи нет, и маршрут развалился на пятом шаге, успев объявить четыре
    предыдущих успешными. База осталась собранной наполовину одними правилами,
    наполовину другими — и разобрать, где чьё, уже нельзя.

    Отдельную команду так запускать можно: человек видит, что делает. Маршрут — нет.
    """
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "def version_gap(" in src, "нет сверки версий движка"
    run_block = src.split('if u.path == "/api/run"')[1][:1200]
    assert "version_gap(" in run_block and 'payload.get("route")' in run_block, \
        "проверка версии не стоит на запуске шага маршрута"
    # страница обязана помечать шаги маршрута — иначе серверу нечего проверять
    ui = panel_sources()
    step = ui.split("async function runStep(")[1][:600]
    assert "route:true" in step, "шаг маршрута не помечен как шаг маршрута"


@test
def test_version_gap_speaks_only_on_a_real_mismatch(tmp: Path):
    """Сверка версий говорит делом, а не строками в исходнике.

    26.09.2026 гейт поймал вживую: панель кита 1.135.0, проект 1.134.0 — маршрут не
    стартовал, раскатка штампом открыла его. Соседний тест проверяет только наличие
    кода; этот зовёт функцию и сверяет речь: меньше версии — молчать, отставание —
    назвать обе версии и куда идти лечиться.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    def project_with(ver):
        d = tmp / ("п-" + ver)
        (d / "AuroraKnowledgeDB" / "meta").mkdir(parents=True)
        (d / "AuroraKnowledgeDB" / "meta" / "aurora_version.txt").write_text(
            ver + "\n", encoding="utf-8")
        return str(d)

    kit = ck.kit_version()
    assert ck.version_gap(project_with(kit)) == "", "совпавшие версии не должны говорить"

    # патч-релиз проекта от кита — тот же минор: скрипты совместимы, маршрут не блокируют
    base, patch = kit.rsplit(".", 1)
    other = str(int(patch) + 1)               # не равна версии кита, иначе папка проекта та же
    assert ck.version_gap(project_with(base + "." + other)) == "", \
        "патч-разница внутри минора не должна блокировать маршрут"

    gap = ck.version_gap(project_with("1.0.0"))
    assert "1.0.0" in gap and kit in gap, f"гейт не называет версии: {gap!r}"
    assert "Версия" in gap, "гейт не говорит, где раскатывать движок проекта"



@test
def test_writing_a_field_refuses_to_touch_the_body(tmp: Path):
    """`with_fields` сторожит себя сама — в любом проекте, на каждой записи.

    Тесты гоняются при разработке кита, а команды человек запускает у себя: между этими
    двумя моментами лежат недели. Поэтому проверка «тело не тронуто» живёт не в тестах,
    а внутри самой записи — она срабатывает у человека, на его карточках, до того как
    испорченный текст попадёт на диск.

    Здесь проверяем обе стороны: обычная запись проходит и ничего не ломает, а подмена
    сборки ловится исключением, а не тихой порчей.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import aurora_common as A

    src = '---\nid: KB-1\nstatus: draft\n---\n\nТело. см. [[Другая]]\n'
    out = A.with_fields(src, {"kind": "knowledge", "status": "knowledge"})
    assert A.card_body(out) == A.card_body(src), "запись поля тронула тело"
    assert A.frontmatter(out)["kind"] == "knowledge", "поле не встало"
    assert A.frontmatter(out)["status"] == "knowledge", "поле не заменилось"
    assert out.count("---") == 2, "разделители удвоились — классика неверной сборки"

    # карточка без шапки: ставить поле некуда, и молча дописывать его в начало текста
    # нельзя — так рождается ровно то повреждение, от которого эта функция и заведена
    try:
        A.with_fields("Просто текст без шапки\n", {"kind": "knowledge"})
        raise AssertionError("поле поставлено карточке без шапки")
    except ValueError:
        pass

    # сама сборка «---» + head + rest — не догадка вызывающего кода: в скриптах, которые
    # правят карточки, ручной сборки остаться не должно
    for name in ("kb_kind.py", "kb_trust.py", "sync_audit.py"):
        code = (KIT / "scripts" / name).read_text(encoding="utf-8")
        assert "with_fields(" in code, f"{name} правит шапку в обход with_fields"


@test
def test_lint_catches_a_field_that_slid_into_the_body(tmp: Path):
    """Поле шапки в первой строке тела — повреждение, и линтер обязан его назвать.

    Живой случай: неверная сборка файла разнесла `kind:` по первой строке тела 2033
    карточек за один прогон. Ни одна проверка не сработала — шапка разбиралась, ссылки
    были целы, число ошибок не выросло, храповик пропустил бы коммит. Заметили случайно.
    Проверка ловит повреждение от любого источника: старой версии движка, чужого
    скрипта, правки руками.
    """
    root = make_project(tmp)
    card(root, "Systems/Целая.md", "Тело. см. [[Битая]]", status="knowledge",
         type="system")
    broken = root / "AuroraKnowledgeDB/Systems/Битая.md"
    broken.write_text('---\nid: KB-2\ntitle: Битая\nstatus: knowledge\ntype: system\n'
                      '---\nkind: knowledge\n\nТело. см. [[Целая]]\n', encoding="utf-8")

    cp = run("kb_lint.py", cwd=root, expect_rc=1)
    assert "уехало в тело" in cp.stdout, \
        "поле шапки в теле карточки прошло мимо линтера — так и разошлось 2033 карточки"
    assert "Битая" in cp.stdout and "Целая" not in cp.stdout.split("уехало в тело")[0][-200:], \
        "названа не та карточка"


@test
def test_the_panel_script_actually_parses(tmp: Path):
    """Скрипт панели должен разбираться целиком: синтаксис — это всё или ничего.

    Живой случай: в одной функции оказалось два `const said` — вторую добавили вместе с
    подписью «какая модель ответила». Это SyntaxError на разборе, а разбор у браузера
    один на весь `<script>`: не выполняется ни одна строка. Панель при этом открывается
    и выглядит почти нормально — шапка, заголовки, заготовки под загрузку, — но цифр,
    проектов и скина нет никогда. Ни один тест этого не ловил: разметка на месте, пути
    на месте, сервер отвечает 200 — мёртв только браузер.

    Проверяем настоящим разборщиком (node или deno). Если ни того ни другого нет —
    тест падает, а не молчит: молчание здесь неотличимо от успеха, и именно так этот
    дефект прожил недели.
    """
    import shutil
    ui = panel_sources()
    js = "\n".join(m.group(1) for m in
                   re.finditer(r"<script[^>]*>(.*?)</script>", ui, re.S))
    assert js.strip(), "в панели не осталось скрипта — разметка изменилась неузнаваемо"

    engine = shutil.which("node") or shutil.which("deno")
    assert engine, ("нет ни node, ни deno — синтаксис панели проверить нечем; "
                    "поставьте любой из них, иначе SyntaxError доедет до человека")
    src = tmp / "panel.js"
    src.write_text(js, encoding="utf-8")
    cmd = ([engine, "--check", str(src)] if Path(engine).stem.lower() == "node"
           else [engine, "check", "--no-lock", str(src)])
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, ("скрипт панели не разбирается — в браузере не выполнится "
                                f"ни одна строка:\n{(cp.stderr or cp.stdout)[:800]}")


@test
def test_a_newer_document_version_keeps_trust_and_marks_the_old_one(tmp: Path):
    """Сайт выложил новую версию документа — старая не теряет доверие молча, новая получает текст.

    Живой случай: страница «Документы» стала ссылаться на новые файлы, старые остались в
    `_files/`, и подъём расшифровок записал им пустой адрес и «не доверять». Восемь карточек
    стали черновиками с основанием «галочка не стоит», хотя галочку никто не снимал, а новые
    версии не получили текста: перевод делал отдельный шаг, и только для Raw/.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")
    out = tmp / "Sources" / "Web"
    files = out / W.ASSET_DIR
    files.mkdir(parents=True)
    (out / "Документы-aaaaaaaa.md").write_text(
        W.card_text("https://law.example/docs/", "Документы", True,
                    "Список.\n\n[Протокол](_files/Протокол-2f73494b.txt)",
                    "https://law.example/docs/", ["Протокол-2f73494b.txt"]), encoding="utf-8")
    (files / "Протокол-43a2f2c7.txt").write_text("Старая версия протокола.\n", encoding="utf-8")
    (files / "Протокол-43a2f2c7.md").write_text(
        '---\ntitle: "Протокол-43a2f2c7"\nconverted_from: "x"\nconverter: plain\n'
        'source_hash: 0\n---\n\nСтарая версия протокола.\n', encoding="utf-8")
    (out / "Протокол-43a2f2c7.md").write_text(
        '---\ntitle: "Протокол"\nurl: https://law.example/docs/\ntrusted: true\n'
        'source_kind: web-document\ndocument: _files/Протокол-43a2f2c7.txt\n---\n\nСтарая.\n',
        encoding="utf-8")
    (files / "Протокол-2f73494b.txt").write_text("Новая версия протокола, пункт 2.\n",
                                                 encoding="utf-8")
    (files / "Ничей-22222222.txt").write_text("Ничей.\n", encoding="utf-8")
    (files / "Ничей-22222222.md").write_text(
        '---\nconverted_from: "x"\nsource_hash: 0\n---\n\nНичей.\n', encoding="utf-8")
    (out / "Ничей-22222222.md").write_text(
        '---\ntitle: "Ничей"\nurl: https://law.example/old/\ntrusted: true\n'
        'source_kind: web-document\ndocument: _files/Ничей-22222222.txt\n---\n\nНичей.\n',
        encoding="utf-8")

    assert W.convert_documents(str(out), apply=True) >= 1, "скачанный документ не получил текста"
    fresh = files / "Протокол-2f73494b.md"
    assert fresh.is_file() and "Новая версия протокола" in fresh.read_text(encoding="utf-8"), \
        "новая версия документа осталась файлом без расшифровки"
    W.promote_documents(str(out), apply=True)
    new = (out / "Протокол-2f73494b.md").read_text(encoding="utf-8")
    assert "trusted: true" in new and "url: https://law.example/docs/" in new, new[:300]
    old = (out / "Протокол-43a2f2c7.md").read_text(encoding="utf-8")
    assert "trusted: true" in old and "url: https://law.example/docs/" in old, \
        f"старая версия потеряла доверие и адрес:\n{old[:300]}"
    assert "superseded_by: Протокол-2f73494b.md" in old, f"не сказано, чем заменён документ:\n{old[:300]}"
    lone = (out / "Ничей-22222222.md").read_text(encoding="utf-8")
    assert "trusted: true" in lone and "url: https://law.example/old/" in lone, \
        "документ без страницы потерял прежнее решение о доверии"
    assert W.promote_documents(str(out), apply=True) == 0, "подъём не идемпотентен"
    A = importlib.import_module("sync_audit")
    (out / "_outdated").mkdir()
    shutil.copy(out / "Протокол-43a2f2c7.md", out / "_outdated" / "Протокол-43a2f2c7.md")
    got = A.superseded_documents({
        "Протокол-43a2f2c7.md": str(out / "Протокол-43a2f2c7.md"),
        "_outdated/Протокол-43a2f2c7.md": str(out / "_outdated" / "Протокол-43a2f2c7.md"),
        "Протокол-2f73494b.md": str(out / "Протокол-2f73494b.md")})
    assert got == [("Протокол-43a2f2c7.md", "Протокол-2f73494b.md")], \
        f"проверка зеркал не называет заменённый документ или зовёт уже убранный в архив: {got}"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert "sync:web" in scen


@test
def test_fix_button_is_offered_only_for_what_repair_can_fix(tmp: Path):
    """«Починить» зовёт, только когда есть что чинить; линтер и ремонт судят одинаково.

    Живой случай: после «Починить базу» оставалось 19 ошибок, которых не убирала ни одна
    команда — ссылки из архива, образец `[[...]]`, ссылки на шаблоны проекта, имя-определение,
    файл в своей папке проекта без типа, — и кнопка «Починить» висела после каждой починки.
    """
    root = make_project(tmp, git=True)
    card(root, "Concepts/Термин.md", "Термин — понятие.", status="draft", type="concept")
    card(root, "Concepts/Ссылки.md",
         "См. [[Термин-—-важное-понятие,-которое-объясняет-всё|термин]], шаблон "
         "[[spec_template]], образец [[...]] и [[Нет-такой-карточки]].",
         status="draft", type="concept")
    card(root, "_archive/Старое.md", "Когда-то: [[Пропавшее-давно]].", status="deprecated",
         type="concept")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "spec_template.md").write_text("# шаблон\n", encoding="utf-8")
    media = root / "AuroraKnowledgeDB" / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "схема.png.md").write_text('---\ntitle: "схема.png"\nstatus: draft\n---\n\nСхема.\n',
                                        encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "paths:\n  extra_structure_dirs: [AuroraKnowledgeDB/media]\n", encoding="utf-8")

    before = run("kb_lint.py", cwd=root).stdout
    assert "Пропавшее-давно" not in before, "ссылка из архива названа ошибкой — ремонт архив не трогает"
    assert "[[...]]" not in before, "образец из шаблона назван битой ссылкой — ремонт судит его иначе"
    assert "схема.png.md: нет type" not in before, "своя папка проекта требует типа, которого в схеме нет"

    run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    text = (root / "AuroraKnowledgeDB/Concepts/Ссылки.md").read_text(encoding="utf-8")
    assert "[[Термин|термин]]" in text, f"имя-определение не сведено к термину:\n{text}"
    assert "[[spec_template]]" not in text and "spec_template" in text, \
        f"ссылка на шаблон проекта не снята:\n{text}"
    term = (root / "AuroraKnowledgeDB/Concepts/Термин.md").read_text(encoding="utf-8")
    assert "важное-понятие" not in term, "фраза-определение записана в синонимы термина"

    r = run("kb_lint.py", "--residue", cwd=root)
    assert "нового после починки: 0" in r.stdout and "Нет-такой-карточки" in r.stdout, r.stdout
    card(root, "Concepts/Новая.md", "См. [[Ещё-одна-пропажа]].", status="draft", type="concept")
    r = run("kb_lint.py", "--summary", cwd=root)
    m = re.search(r"нового после починки: (\d+)", r.stdout)
    assert m and int(m.group(1)) > 0, f"новая ошибка не отличена от остатка: {r.stdout}"

    ui = panel_sources()
    assert ('fresh ? goRoute("fix","Починить базу")' in ui
            or 'fresh ? ctx.ui.goRoute("fix"' in ui) \
        and ('"Что решить вам"' in ui or "health.go_decide" in ui), \
        "Мостик снова зовёт «Починить» при любой ошибке, а не при новой"
    assert 'sc.id === "fix" ? null : fixButton(x.what)' in ui, \
        "итог «Починить базу» предлагает запустить ремонт, который только что прошёл"
    # Строку «нашла, что чинить» пишет в журнал маршрута сервер — одну на команду.
    rr = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert 't("route.found_times", n=n)' in rr and "times[x[\"cmd\"]]" in rr, \
        "итог маршрута повторяет одну строку на каждый шаг"
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'lint_info["fresh"]' in src, "сервер панели не отдаёт число новых ошибок"


@test
def test_finding_carries_a_button_not_a_riddle(tmp: Path):
    """Команда с кодом 1 не теряет «Применить», а находка получает кнопку лечения.

    Живой случай: `kb:index` написала «повторите с --apply», а кнопки не было — условие
    показа требовало кода 0, тогда как записывать надо как раз после кода 1 («отработала
    и нашла, что чинить»). Человек ушёл искать флаг, которого в панели нет.
    """
    ui = panel_sources()
    assert "if (rc<=1 && PENDING_APPLY" in ui, \
        "команда, вернувшая 1, снова осталась без кнопки «Применить»"
    assert "const FIX_RUN" in ui and "function fixButton" in ui, \
        "находка объясняет лечение словами, но запустить его из панели нельзя"
    assert "fixButton(x.what)" in ui, "кнопка лечения не доходит до итогов маршрута"
    # Решение человека кнопкой не подменяется: --force затирает чужой текст
    assert "--force" not in ui.split("const FIX_RUN")[1].split("};")[0], \
        "в кнопку лечения попал --force: это решение человека, а не автоматика"

    # Финальная строка отчёта не должна отправлять нажимать --apply впустую
    src = (KIT / "scripts/kb_index.py").read_text(encoding="utf-8")
    assert "if written and not a.apply:" in src, \
        "«повторите с --apply» печатается даже когда записывать нечего"
    assert "kb:index --force --apply" in src, \
        "не сказано, что на самом деле поможет, когда оглавления рукотворные"


@test
def test_panel_asks_only_endpoints_the_server_has(tmp: Path):
    """Каждый путь, который просит страница, у сервера существует.

    Живой случай: в конце пишущего маршрута страница спрашивала `/api/projects`, которого
    у сервера нет (проекты отдаёт `/api/state`). Вместо предупреждения о незакоммиченной
    работе человек получал всплывающее «неизвестный маршрут» — и не понимал, что именно
    в маршруте сломалось. Опечатка в пути не видна ни глазами, ни при запуске: страница
    просто получает 404 и молчит либо пугает.
    """
    ui = panel_sources()
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    known = set(re.findall(r'u\.path == "([^"]+)"', srv))
    assert "/api/state" in known, "разбор путей сервера сломался — тест перестал что-то проверять"
    asked = {m.group(1) for m in re.finditer(r'api\(\s*[`"\']([^`"\'?]+)', ui)
             if m.group(1).startswith("/api")}
    missing = sorted(asked - known)
    assert not missing, f"страница просит пути, которых у сервера нет: {missing}"


@test
def test_bridge_knows_what_runs_and_what_stopped(tmp: Path):
    """Сервер называет по проекту, что идёт и что встало: задания, замок агента, маршрут.

    Проектов на машине несколько, а работа была видна только в «Консоли» выбранного:
    человек не понимал, для какого проекта обновление идёт, а для какого остановилось.
    Отметка на карточке Мостика строится из трёх следов, и каждый проверяем по отдельности:
    мёртвый pid в замке — это оборванный прогон, а не идущий.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    quiet, busy = tmp / "quiet", tmp / "busy"
    for d in (quiet, busy):
        (d / ".opencode/state").mkdir(parents=True)
    assert ck.project_activity(str(quiet)) == {"running": [], "agent": None, "route": None}, \
        "у тихого проекта нашлась работа, которой нет"

    ck.JOBS.clear()
    try:
        ck.JOBS["a"] = {"id": "a", "cmd": "agent:build", "args": ["--apply"], "project": str(busy),
                        "out": [], "started": 100.0, "done": False, "rc": None}
        ck.JOBS["b"] = {"id": "b", "cmd": "kb:lint", "args": [], "project": str(busy),
                        "out": [], "started": 50.0, "done": True, "rc": 0}
        ck.JOBS["c"] = {"id": "c", "cmd": "sync:jira", "args": [], "project": str(quiet),
                        "out": [], "started": 90.0, "done": False, "rc": None}
        act = ck.project_activity(str(busy))
    finally:
        ck.JOBS.clear()
    assert [j["cmd"] for j in act["running"]] == ["agent:build"], \
        f"в идущих не то: закончившееся или чужое задание — {act['running']}"

    lock = busy / ".opencode/state/agent.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "task": "agent:build", "since": "17:55:00"}),
                    encoding="utf-8")
    agent = ck.project_activity(str(busy))["agent"]
    assert agent and agent["alive"] and agent["task"] == "agent:build", \
        f"живой прогон агента вне панели не виден: {agent}"

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    lock.write_text(json.dumps({"pid": gone.pid, "task": "agent:distill", "since": "03:10:00"}),
                    encoding="utf-8")
    agent = ck.project_activity(str(busy))["agent"]
    assert agent and not agent["alive"] and agent["task"] == "agent:distill", \
        f"оборванный прогон выдан за идущий: {agent}"

    lock.write_text("{порван", encoding="utf-8")
    assert ck.project_activity(str(busy))["agent"] is None, "порванный замок уронил отметку"

    (busy / ".opencode/state/last_route.json").write_text(json.dumps(
        {"scId": "update", "runId": "r1", "title": "Обновить базу", "write": True,
         "reason": "offline", "step": "agent:build", "attempts": 1,
         "nextRetryAt": 1789401064942, "at": "2026-09-14T15:36:04.942Z"}), encoding="utf-8")
    route = ck.project_activity(str(busy))["route"]
    assert route and route["title"] == "Обновить базу" and route["reason"] == "offline" \
        and route["step"] == "agent:build", f"остановленный маршрут не назван: {route}"

    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'p["activity"] = project_activity(p["path"])' in srv, \
        "Мостик рисуется без отметок до первого опроса"
    assert srv.count('"projects": find_projects(self.server.roots)') == 0, \
        "/api/state снова обходит папки проектов в ответе — и, может быть, дважды"


@test
def test_bridge_card_says_what_runs_and_what_stopped(tmp: Path):
    """Карточка проекта на Мостике отмечает идущую, остановленную и оборванную работу.

    Функция отметки чистая — от ответа сервера и часов, — поэтому выполняем её в node на
    живых случаях: два задания панели, агент из терминала, оборванный замок, маршрут,
    остановленный человеком и ждущий сеть.
    """
    import shutil
    ui = panel_sources()
    def extract(name):
        start = ui.index(f"function {name}(")
        depth, i = 0, ui.index("{", start)
        while True:
            depth += {"{": 1, "}": -1}.get(ui[i], 0)
            if depth == 0:
                break
            i += 1
        return ui[start:i + 1]
    # отметка показывает время общим помощником панели — он едет в проверку вместе с ней
    # Надписи отметки живут в каталоге строк, а берёт их `t()` — в проверку едут оба,
    # иначе вместо отметки получится имя ключа, и тест поймает переезд вместо дефекта.
    ru = (KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8")
    func = (f"const I18N = {ru}, RU = I18N, S = {{lang: 'ru'}};\n"
            + extract("t") + "\n" + extract("histWhen") + "\n" + extract("activityChips"))
    assert "drawActivity();" in ui and "/api/activity" in ui, \
        "отметка посчитана, но на карточку не выводится или не обновляется"

    engine = shutil.which("node") or shutil.which("deno")
    assert engine, "нет ни node, ни deno — отметку карточки проверить нечем"
    harness = func + """
const now = Date.parse("2026-09-14T18:00:00Z");
const at = now / 1000;
const route = {title: "Обновить базу", step: "agent:build", reason: "stopped",
               at: "2026-09-14T15:36:04Z"};
console.log(JSON.stringify({
  none: activityChips(null, now),
  quiet: activityChips({running: [], agent: null, route: null}, now),
  running: activityChips({running: [{cmd: "agent:build", args: ["--apply"], started: at - 600},
                                    {cmd: "kb:lint", args: [], started: at - 60}],
                          agent: {task: "agent:build", pid: 1, alive: true}, route}, now),
  outside: activityChips({running: [], agent: {task: "agent:build", pid: 42, since: "17:55:00",
                                                alive: true, at}, route: null}, now),
  broken: activityChips({running: [], agent: {task: "agent:build", pid: 42, since: "17:55:00",
                                               alive: false, at: at - 3600}, route: null}, now),
  stopped: activityChips({running: [], agent: {task: "agent:build", pid: 42, alive: false, at},
                          route}, now),
  waiting: activityChips({running: [], agent: null, route: {title: "Обновить базу",
    step: "agent:build", reason: "offline", nextRetryAt: now + 600000}}, now),
  gaveUp: activityChips({running: [], agent: null, route: {title: "Пересобрать базу с нуля",
    step: 3, reason: "offline", nextRetryAt: now - 600000}}, now),
}));
"""
    src = tmp / "chips.js"
    src.write_text(harness, encoding="utf-8")
    cmd = [engine, str(src)] if Path(engine).stem.lower() == "node" else [engine, "run", "--quiet", str(src)]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), \
        f"функция отметки не выполнилась:\n{(cp.stderr or cp.stdout)[:800]}"
    r = json.loads(cp.stdout.strip().splitlines()[-1])

    assert r["none"] == [] and r["quiet"] == [], f"тихому проекту нарисована отметка: {r['quiet']}"
    [run_] = r["running"]
    assert "live" in run_["cls"] and "идёт: agent:build" in run_["text"] \
        and "ещё 1" in run_["text"] and "10 мин" in run_["text"], \
        f"идущие задания панели не названы: {run_}"
    assert "Обновить базу" in run_["title"], "за идущим заданием потерян остановленный маршрут"
    [out] = r["outside"]
    assert "live" in out["cls"] and "вне панели" in out["text"], \
        f"прогон агента из терминала не отмечен: {out}"
    [br] = r["broken"]
    assert br["cls"] == "bad" and "оборван: agent:build" in br["text"], \
        f"оборванный прогон не отмечен: {br}"
    [st] = r["stopped"]
    assert st["cls"] == "warn" and "маршрут остановлен: «Обновить базу»" in st["text"], \
        f"остановленный маршрут не отмечен: {st}"
    assert "шаг agent:build" in st["title"] and "остановлен вами" in st["title"] \
        and "оборвался" in st["title"], f"в подсказке нет шага, причины или замка: {st['title']}"
    assert "маршрут ждёт сеть" in r["waiting"][0]["text"], \
        f"маршрут, который повторит попытку сам, выдан за остановленный: {r['waiting']}"
    [gu] = r["gaveUp"]
    assert "маршрут остановлен" in gu["text"] and "нет сети" in gu["title"] \
        and "шаг 3" not in gu["title"], f"маршрут без повтора назван неверно: {gu}"


@test
def test_route_counts_its_own_steps_and_resumes_inside_the_lap(tmp: Path):
    """Счётчик маршрута не прибавляет повторы цикла, а продолжение встаёт туда, где стоял.

    Живой случай: после трёх оборотов «Обновить базу» панель показывала «шаг 36 из 20» —
    исправление 1.100.41 развело счёт только внутри оборота. И продолжение после остановки
    всегда начинало цикл с первого шага первого оборота: в маршруте, где вся работа в цикле,
    это читалось и работало как запуск заново.
    """
    # Маршрут ведёт сервер (`cockpit/route_runner.py`, 1.152.0) — проверяем его журнал.
    import importlib
    sys.path.insert(0, str(KIT / "cockpit"))
    rr = importlib.import_module("route_runner")
    fake = importlib.import_module("cases.part_18").FakePanel
    sc = {"id": "update", "title": "Обновить базу", "group": "база", "steps": [
        {"manual": False, "cmd": "kb:repair", "why": "до", "flags": []},
        {"manual": False, "cycle": "цикл:", "why": ""},
        {"manual": False, "cmd": "agent:build", "why": "разбор", "flags": []},
        {"manual": False, "cmd": "kb:lint", "why": "проверка", "flags": []},
        {"manual": False, "cycle": "конец цикла", "why": ""},
        {"manual": False, "cmd": "kb:moc", "why": "после", "flags": []}]}
    script = {"kb:repair": [(0, [])], "kb:lint": [(0, [])], "kb:moc": [(0, [])],
              "agent:build": [(0, ["Источников в плане: 5 → 3"]), (0, ["Источников в плане: 3 → 1"]),
                              (0, ["Источников в плане: 1 → 0"])]}
    ck = fake(tmp, script, [sc])
    run = rr.RouteRun(ck, str(tmp), sc, True)
    run.run()
    heads = [e["s"] for e in run.journal.entries if e["k"] == "head"]
    assert heads[0].startswith("▸ шаг 1 из 2 ·"), heads
    assert heads[-1].startswith("▸ шаг 2 из 2 ·"), \
        "шаги цикла снова идут в общий счёт маршрута — вернётся «шаг 36 из 20»: " + heads[-1]
    assert any("шаг 2 из 2 в обороте · оборот 3" in h for h in heads), heads

    ck = fake(tmp, dict(script, **{"agent:build": [(0, ["Источников в плане: 3 → 0"])]}), [sc])
    run = rr.RouteRun(ck, str(tmp), sc, True,
                      resume={"skipSigs": ["kb:repair"], "cycleAt": {"lap": 1, "inLap": 2}})
    run.run()
    # Первый оборот продолжения неполный: пройденный `agent:build` пропущен, остаток по
    # такому обороту не меряется — следующий оборот идёт целиком.
    assert [c[1] for c in ck.calls] == ["kb:lint", "agent:build", "kb:lint", "kb:moc"], \
        "продолжение не пропускает пройденные шаги оборота: " + str([c[1] for c in ck.calls])
    notes = [e["s"] for e in run.journal.entries if e["k"] == "note"]
    assert any("оборота пропущен" in n for n in notes), notes
    # Состояние для «Продолжить» пишется после каждого шага: и сделанное, и место в обороте.
    ck = fake(tmp, {"kb:repair": [(0, [])], "agent:build": [(2, ["упало"])],
                    "kb:lint": [(0, [])], "kb:moc": [(0, [])]}, [sc])
    rr.RouteRun(ck, str(tmp), sc, True).run()
    assert ck.route_state["done"] == ["kb:repair"], ck.route_state
    assert ck.route_state["cycleAt"] == {"lap": 1, "inLap": 1}, \
        "остановленный маршрут не запоминает, на каком шаге оборота встал"
    ui = panel_sources()
    resume = ui[ui.index("async function resumeLastRoute("):ui.index("function dropResumeButtons(")]
    assert "cycleAt: last.cycleAt" in resume and "last.done" in resume, \
        "кнопка «Продолжить» не передаёт сделанное и место в обороте"


@test
def test_model_json_is_found_inside_reasoning_and_trailing_text(tmp: Path):
    """JSON в ответе модели находится, даже когда вокруг рассуждение и скобки.

    Жадный шаблон «от первой `{` до последней `}`» ломался на обычном ответе: рассуждение
    с фигурными скобками перед объектом или пояснение со скобкой после него. На живом
    проекте так падала страница-лента на каждом обороте, и маршрут вставал застоем.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    assert R.parse_json('Думаю {так}. Ответ: {"cards": [], "empty": "нет знания"} — всё}') \
        == {"cards": [], "empty": "нет знания"}, "объект после рассуждения со скобками не найден"
    assert R.parse_json('<think>{"x": 1}</think>\n```json\n{"verdict": "ok"}\n```') \
        == {"verdict": "ok"}, "рассуждение в <think> принято за ответ"
    assert R.parse_json('{"a": {"b": 1}} и ещё скобка }') == {"a": {"b": 1}}, \
        "хвост со скобкой ломает разбор"
    assert R.parse_json("ответа нет") is None and R.parse_json("") is None


@test
def test_unparsed_answer_is_asked_again_and_then_parked(tmp: Path):
    """Неразобранный ответ модели переспрашивается, а упорно сбойный источник откладывается.

    Источник, на который модель дважды не дала разбираемого ответа, оставался в плане:
    «осталось» не убывало, и маршрут «Обновить базу» вставал застоем четыре прогона подряд.
    Сбой по содержанию учитывается, после двух подряд на тот же текст план откладывает
    источник; изменился файл — источник возвращается сам.
    """
    from unittest.mock import patch
    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    import agent_runner as R

    root = make_project(tmp)
    src_rel = "Sources/Confluence/Лента.md"
    src = root / src_rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Лента\n\n## Заметка\n\n" + "Текст заметки о предмете. " * 20 + "\n",
                   encoding="utf-8")
    cfg = parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://test",
                        "AURORA_AGENT_BACKEND_1_MODEL": "test"})
    sections = [(1, "Заметка", 500, "превью")]
    calls = []

    def bad(cfg_, role, messages, **k):
        calls.append(role)
        return {"ok": True, "text": "Рассуждаю о ленте, объект не получился", "backend": 1,
                "model": "m", "log": []}

    with patch("agent_runner.read_sections", return_value=sections):
        step = R.solve_source(cfg, str(root), "Confluence", src_rel, True, False, call=bad)
    assert calls.count("worker") == 2, f"неразобранный ответ не переспрошен: {calls}"
    assert step["status"] == "сбой" and step.get("content_fail"), step
    assert "начало ответа: «Рассуждаю о ленте" in step["note"], \
        f"в отчёте нет начала ответа — сбой нечем разобрать: {step['note']}"

    answers = iter(["не JSON", '{"cards": [{"title": "Заметка-о-предмете", "sections": "1", '
                               '"to": "Concepts"}]}'])

    def second_try(cfg_, role, messages, **k):
        return {"ok": True, "text": next(answers), "backend": 1, "model": "m", "log": []}

    with patch("agent_runner.read_sections", return_value=sections):
        step = R.solve_source(cfg, str(root), "Confluence", src_rel, True, False, call=second_try)
    assert step["status"] == "разобран", f"переспрос не спас разбираемый со второго раза ответ: {step}"

    other = root / "Sources/Confluence/Другая.md"
    other.write_text("# Другая\n\n" + "Содержательный текст страницы. " * 20, encoding="utf-8")

    def left():
        out = run("build_plan.py", "--status", cwd=root).stdout
        return int(re.search(r"осталось:\s*(\d+)", out).group(1)), out
    before, _ = left()
    run("build_plan.py", "--failed", "Sources/Confluence/Другая.md", "--note", "сбой", cwd=root)
    assert left()[0] == before, "один сбой уже откладывает источник — модель могла ошибиться случайно"
    run("build_plan.py", "--failed", "Sources/Confluence/Другая.md", "--note", "сбой", cwd=root)
    n, out = left()
    assert n == before - 1 and "отложено: 1" in out and "Другая.md" in out, \
        f"два сбоя подряд не отложили источник — цикл снова встанет:\n{out}"
    other.write_text(other.read_text(encoding="utf-8") + "\nНовый абзац.\n", encoding="utf-8")
    assert left()[0] == before, "изменённый источник остался отложенным — новый текст не разберут"
    run("build_plan.py", "--failed", "Sources/Confluence/Другая.md", "--note", "сбой", cwd=root)
    run("build_plan.py", "--failed", "Sources/Confluence/Другая.md", "--note", "сбой", cwd=root)
    run("build_plan.py", "--retry-failed", cwd=root)
    assert left()[0] == before, "--retry-failed не вернул отложенный источник в план"

    code = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    body = code[code.index("def run_build("):code.index("def verdict_build(")]
    assert body.count("defer_if_content_fail(cwd, source, step, apply)") == 2, \
        "сбой разбора учитывается не в обеих ветках разбора — в одном потоке или в нескольких"


@test
def test_relink_stops_when_gateways_do_not_answer(tmp: Path):
    """Связывание останавливается после трёх сбоев подряд, а остаток ждёт следующий прогон.

    Живой случай: второй заход связывания потратил двадцать минут на двадцать карточек и не
    сделал ни одной — слот шлюза был занят, и каждая карточка ждала пятиминутного срока.
    """
    from unittest.mock import patch
    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    import agent_runner as R

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB" / "Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    for i in range(8):
        (kb / f"К{i}.md").write_text(
            f'---\ntitle: "К{i}"\nkind: knowledge\nstatus: draft\ndistilled: 2026-09-01\n---\n\n'
            f"# К{i}\n\nТезис карточки номер {i} о предмете.\n", encoding="utf-8")
    cfg = parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://test",
                        "AURORA_AGENT_BACKEND_1_MODEL": "test"})
    seen = []

    def busy(cfg_, path, call, apply, deadline=None, prefer=0):
        seen.append(path)
        return {"card": os.path.basename(path), "status": "сбой", "added": 0, "backends": [1],
                "note": "№1: слот занят (/slots) — дальше по кольцу; никто не уложился в срок"}

    with patch("agent_runner.relink_card", busy):
        res = R.run_relink(cfg, str(root), True)
    assert res["gateways_down"], f"предохранитель не сработал: {res}"
    assert len(seen) <= R.FAILS_IN_A_ROW + 1, f"после трёх сбоев подряд связывание шло дальше: {len(seen)}"
    assert res["left"] >= 8 - len(seen) and "свяжет следующий прогон" in res["steps"][-1]["note"], res
    assert not any("relinked:" in p.read_text(encoding="utf-8") for p in kb.glob("*.md")), \
        "карточка без связей отмечена связанной — из очереди она выпала бы навсегда"
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    loop = src[src.index('elif a.task == "relink" and a.until_done and a.apply:'):]
    assert 'until_done(cwd, a, "relink"' in loop[:3000], \
        "заходы связывания идут мимо общего цикла заходов"
    shared = src.split("def until_done(")[1].split("\ndef ")[0]
    assert 'if res.get("gateways_down"):' in shared, \
        "заходы продолжаются, когда шлюзы уже не отвечают"


@test
def test_lint_reads_golden_questions_not_engine_reports(tmp: Path):
    """«Контрольный вопрос без карточки» — только из эталона, не из отчётов движка в meta/.

    Живой случай: из 20 «пропавших контрольных вопросов» часть была ссылками отчёта
    `meta/gaps.md` — отчёт о дырах выдавался за потерянный эталон.
    """
    root = make_project(tmp)
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "gaps.md").write_text("# Дыры\n\n- [[Карточка-которой-нет-в-отчёте]]\n", encoding="utf-8")
    (meta / "golden_questions.md").write_text(
        "# Эталон\n\n| 1 | Вопрос? | Ответ | [[Пропавшая-карточка-эталона]] |\n", encoding="utf-8")
    out = run("kb_lint.py", cwd=root, expect_rc=None).stdout
    assert "Пропавшая-карточка-эталона" in out, "пропажа цели эталона перестала замечаться"
    assert "Карточка-которой-нет-в-отчёте" not in out, \
        f"ссылка отчёта движка названа контрольным вопросом:\n{out[:800]}"


@test
def test_health_report_names_panel_commands(tmp: Path):
    """Отчёт о здоровье называет команды панели, а не пути к скриптам.

    Живой случай: «Дальше» предлагал `python3 .opencode/scripts/kb_queue.py` — скрипта нет в
    движке вовсе, а человек нажимает кнопку, а не набирает python3.
    """
    src = (KIT / "scripts/aurora_stats.py").read_text(encoding="utf-8")
    tail = src[src.index('"## Дальше"'):src.index("def append_metrics(")]
    assert "python3 .opencode/scripts" not in tail and "kb_queue" not in tail, tail[:400]
    for cmd in ("`ops:todo`", "`kb:lint`", "`kb:repair`", "`sync:audit`"):
        assert cmd in tail, f"в «Дальше» нет {cmd}"


@test
def test_document_maps_of_vanished_sources_are_removed(tmp: Path):
    """Карта документа, который больше не даёт карточек, уходит; руками писанная остаётся.

    Живой случай: карта удалённых доавроровских карточек держала пятнадцать битых ссылок —
    её не порождали, значит и не переписывали, и линтер звал её ссылки битыми вечно.
    """
    root = make_project(tmp)
    moc = root / "AuroraKnowledgeDB" / "MOC"
    moc.mkdir(parents=True, exist_ok=True)
    for name in ("Первая", "Вторая"):
        card(root, f"Concepts/{name}.md", "Текст карточки о предмете.",
             source="Sources/Confluence/Живой.md", status="draft")
    gen = "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_moc.py — ручные правки будут потеряны. -->"
    (moc / "Документ--Удалённый.md").write_text(
        f'---\ntitle: "Документ · Удалённый"\n---\n\n{gen}\n\n- [[Карточки-больше-нет]]\n',
        encoding="utf-8")
    (moc / "Документ--Ручной.md").write_text("# Карта руками\n\n- [[Первая]]\n", encoding="utf-8")

    dry = run("kb_moc.py", "--by-source", cwd=root, expect_rc=None).stdout
    assert "уйдёт карта документа без карточек" in dry and (moc / "Документ--Удалённый.md").exists(), \
        f"предпросмотр не называет устаревшую карту или уже удалил её:\n{dry[:600]}"
    run("kb_moc.py", "--by-source", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    assert not (moc / "Документ--Удалённый.md").exists(), \
        "карта документа без карточек осталась и продолжает ссылаться в пустоту"
    assert (moc / "Документ--Ручной.md").exists(), "удалена карта, написанная руками"
    assert (moc / "Документ--Живой.md").exists(), "карта живого документа не собрана"


@test
def test_audit_reads_the_web_mirror_and_its_lifted_documents(tmp: Path):
    """Аудит понимает состояние веб-зеркала, а расшифровки документов не зовёт сиротами.

    Живой случай: аудит писал «нет update_log.md», глядя на файл из сорока строк, — он
    понимал только строки задач Jira. А веб-модуль называл «лишними» десять законов и
    приказов, поднятых из вложений, и `--prune` снёс бы каждый, на который ещё не сослалась
    карточка.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    SC = importlib.import_module("sources_core")
    A = importlib.import_module("sync_audit")

    web = tmp / "Sources" / "Web"
    (web / "_files").mkdir(parents=True)
    (web / "Новости-1a2b3c4d.md").write_text(
        '---\ntitle: "Новости"\nurl: https://example.org/news/\ntrusted: true\n'
        "source_kind: web\n---\n\n# Новости\n", encoding="utf-8")
    (web / "Закон-5e6f7a8b.md").write_text(
        '---\ntitle: "Закон"\nurl: https://example.org/docs/\ntrusted: true\n'
        "source_kind: web-document\ndocument: _files/Закон-5e6f7a8b.pdf\n---\n\n# Закон\n",
        encoding="utf-8")
    (web / "_files" / "Закон-5e6f7a8b.md").write_text(
        '---\ntitle: "Закон"\nconverted_from: "_files/Закон-5e6f7a8b.pdf"\n---\n\nтекст\n',
        encoding="utf-8")
    (web / "update_log.md").write_text(
        "<!-- Web sync state — генерируется web_export.py, не править руками -->\n"
        "**Sync Date:** 2026-09-15\n**Pages:** 1\n\n| # | URL | Trusted | Local Path | Status |\n"
        "|---|---|---|---|---|\n| 1 | https://example.org/news/ | да | Новости-1a2b3c4d.md | обновлена |\n",
        encoding="utf-8")

    assert SC.is_promoted_document(str(web / "Закон-5e6f7a8b.md")), "поднятый документ не узнан"
    assert not SC.is_promoted_document(str(web / "Новости-1a2b3c4d.md")), "страница принята за документ"
    rows, latest = A.parse_board_state(str(web), "update_log.md")
    assert [k for k, _d in rows] == ["Новости-1a2b3c4d"] and latest == "2026-09-15", \
        f"состояние веб-зеркала не прочитано: {rows}, {latest}"
    out, stats = [], {}
    A.audit_board({"id": "Web", "path": str(web), "state": "update_log.md", "command": "sync:web"},
                  14, out, stats)
    assert stats["Web"].get("orphan") == 0 and stats["Web"].get("missing") == 0, \
        "расшифровки документов или вложения названы сиротами:\n" + "\n".join(out)

    code = (KIT / "scripts/web_export.py").read_text(encoding="utf-8")
    assert "and not is_promoted_document(os.path.join(mirror.out, r))" in code \
        and "is_promoted_document, report_stale" in code, \
        "веб-модуль снова считает поднятые документы лишними файлами"


@test
def test_settings_form_keeps_what_it_does_not_manage(tmp: Path):
    """Сохранение формы настроек не стирает то, чего шаблон настройки не знает.

    Живой случай: форма собирала конфиг заново по шаблону, и при добавлении веб-страницы
    пропали реестр из десяти видов документов (`artifacts:`) и настройки отчёта (`reports:`).
    Объявленная папка проекта (`paths → extra_structure_dirs`) ушла бы следующей.
    """
    import contextlib
    import io
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("aurora_setup")
    root = tmp / "проект"
    root.mkdir()
    cfg = root / "aurora.config.yaml"
    cfg.write_text(
        'aurora:\n  version: 1\n\nproject:\n  name: "Старое имя"\n  slug: "P"\n'
        '  owner: "Отдел анализа"\n\npaths:\n  knowledge_db: AuroraKnowledgeDB\n'
        "  extra_structure_dirs: [AuroraKnowledgeDB/media]\n\n# Виды документов проекта\n"
        'artifacts:\n  opz:\n    title: "ОПЗ"\n    out: "Deliverables/work"\n\n'
        "reports:\n  analyst:\n    year: 2026\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        A.run_answers(root, {"name": "Новое имя"})
    text = cfg.read_text(encoding="utf-8")
    assert 'name: "Новое имя"' in text, "форма не записала то, что правила"
    for need in ("extra_structure_dirs: [AuroraKnowledgeDB/media]", "# Виды документов проекта",
                 "artifacts:", "  opz:", 'out: "Deliverables/work"', "reports:", "year: 2026",
                 'owner: "Отдел анализа"'):
        assert need in text, f"сохранение формы стёрло «{need}»:\n{text}"
    for section in ("artifacts:", "paths:", "reports:", "project:"):
        assert text.count("\n" + section) == 1, f"раздел {section} удвоился:\n{text}"
    with contextlib.redirect_stdout(io.StringIO()):
        A.run_answers(root, {})
    assert cfg.read_text(encoding="utf-8") == text, "повторное сохранение формы меняет конфиг"


@test
def test_result_folder_is_a_folder_not_a_file_path(tmp: Path):
    """Папка результата вида документа не может быть путём к файлу.

    Живой случай: в поле папки стоял `Artifacts/Activity_Epic_US.md`, движок создал каталог
    с именем файла и сложил туда копию реестра и свою работу, а doctor звал его структурной
    папкой вне схемы.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    MK = importlib.import_module("make_kinds")
    ck = importlib.import_module("aurora_cockpit")
    R = importlib.import_module("agent_runner")
    assert "путь к файлу" in MK.out_problem("Artifacts/Activity_Epic_US.md")
    assert MK.out_problem("/abs/Deliverables") and MK.out_problem("../чужое")
    assert not MK.out_problem("Deliverables/work") and not MK.out_problem("Artifacts/us/v1.2"), \
        "обычная папка названа файлом"

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "b.md").write_text("шаблон", encoding="utf-8")
    bad = ck.kinds_write(str(root), {"backlog": {"title": "Бэклог", "template": "Templates/b.md",
                                                  "out": "Artifacts/Activity_Epic_US.md"}})
    assert "error" in bad and not (root / "Artifacts" / "Activity_Epic_US.md").exists(), \
        f"панель приняла путь к файлу и создала каталог с его именем: {bad}"
    assert ck.kinds_write(str(root), {"pr": {"title": "ПР", "template": "Templates/b.md",
                                             "out": "Deliverables/work"}}).get("ok")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('out: "Deliverables/work"',
                                                           'out: "Artifacts/Реестр.md"'),
                   encoding="utf-8")
    assert any("путь к файлу" in why for _k, why in MK.check(str(root), MK.read_kinds(str(root)))), \
        "реестр видов не замечает путь к файлу в папке результата"
    spec = R.make_spec(str(root), "pr")
    assert "путь к файлу" in spec.get("error", ""), f"агент изготовления возьмёт такой вид в работу: {spec}"


@test
def test_task_outweighs_a_trusted_folder_only_by_direct_link(tmp: Path):
    """Задача Jira в статусе-предположении сильнее доверенной папки — при прямой связи.

    Решение заказчика 15.09: доверие наследуется от источника, но документ, прямо связанный
    с задачей, чья постановка ещё меняется, говорит о нерешённом. Через трассировку одна
    широкая задача понизила бы сотни карточек, поэтому косвенная связь класс не трогает.

    Решение 25.09: папка доверяется по пути только вне вики — страница вики доверяется
    справочником по названию ветки или задачей, а не папкой в конфиге.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    U = importlib.import_module("kb_trust")
    folder = "Sources/Docs/Архитектура"
    table = {"direct": {f"{folder}/A.md": [{"key": "PRJ-9", "why": "ключ"}],
                        f"{folder}/B.md": [{"key": "PRJ-1", "why": "ключ"}]},
             "indirect": {f"{folder}/C.md": [{"key": "PRJ-9", "trail": ["C", "A"], "depth": 1}]}}
    st = {"PRJ-1": "Закрыто", "PRJ-9": "Бэклог"}
    trust, draft, docs = {"закрыто"}, {"бэклог"}, (folder,)
    cls, why = U.source_class(f"{folder}/A.md", table, st, trust, draft, docs)
    assert cls == "draft" and "PRJ-9" in why and "сильнее папки" in why, (cls, why)
    assert U.source_class(f"{folder}/B.md", table, st, trust, draft, docs)[0] == "raw", \
        "задача в доверенном статусе понизила документ из доверенной папки"
    assert U.source_class(f"{folder}/C.md", table, st, trust, draft, docs)[0] == "raw", \
        "косвенная связь понизила документ — одна широкая задача уронит сотни карточек"
    assert U.source_class(f"{folder}/D.md", table, st, trust, draft, docs)[0] == "raw"
    # Вики-папка в `trusted_sources` доверия больше не даёт (решение 25.09): страница либо
    # справочник, либо судится задачей.
    cls, why = U.source_class("Sources/Confluence/Архитектура/D.md", table, st, trust, draft,
                              ("Sources/Confluence/Архитектура",))
    assert cls == "unknown", f"папка вики в конфиге снова дала доверие: {cls} — {why}"
    cls, _ = U.source_class("Sources/Confluence/Справочники/Таблица.md", table, st, trust, draft)
    assert cls == "raw", "справочная ветка вики перестала доверяться по природе"

    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    fix = scen[scen.index("[fix]"):]
    fix = fix[:fix.index("\n[", 1)]
    for need in ("ops:trace-table |", "kb:trust |", "--drop-code-stubs"):
        assert need in fix, f"в «Починить базу» нет шага {need}"
    assert fix.index("kb:trust |") < fix.index("kb:embed |"), "доверие пересчитывается после индекса"
    ui = panel_sources()
    assert 't("overview.sources", {pct: h.build.pct})' in ui, \
        "на карточке Мостика нет второго числа — разбора источников"


@test
def test_trust_defaults_are_a_setting_written_into_the_config(tmp: Path):
    """Значения доверия по умолчанию — обычная настройка проекта, записанная в конфиг.

    Живой случай: у проекта статусы задач и доверенные источники остались пустыми, и вся вики
    получила «класс не определён» — 23 % доверия. Решение заказчика 15.09: одна настройка по
    умолчанию для всех проектов. Её пишут шаблон, форма и обновление движка — там, где список
    пуст или ключа нет; заданное проектом не трогается. Пустой список движок читает как те же
    значения. Карточка, чья опора — сама задача, берёт её статус.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    AC = importlib.import_module("aurora_common")
    S = importlib.import_module("aurora_setup")
    U = importlib.import_module("kb_trust")

    empty = ('project:\n  name: "T"\n\natlassian:\n  jira:\n    project_key: "T"\n'
             '    trust_statuses: []\n    assumption_statuses: []   # старый комментарий\n'
             '  auth:\n    mode: mcp_user\n\nverify:\n  trusted_sources: []\n'
             '  trusted_sections: [Glossary]\n\nprivacy:\n  scrub: report\n')
    new, done = S.fill_trust_defaults(empty)
    assert "trust_statuses: [Закрыто, Разработка, Тестирование" in new and '"Code Review"' in new, new
    assert "assumption_statuses: [Аналитика, Анализ" in new, new
    assert "trusted_sources: [Raw/contract, Raw/customer" in new and "trusted_branches: [" in new, new
    assert "trusted_sections" not in new, "мёртвый ключ остался в конфиге"
    assert "mode: mcp_user" in new and "scrub: report" in new, f"задет чужой текст конфига:\n{new}"
    assert S.fill_trust_defaults(new)[1] == [], "повторное обновление снова правит конфиг"

    bare = ('project:\n  name: "T"\n\natlassian:\n  jira:\n    project_key: "T"\n'
            '  auth:\n    mode: mcp_user\n\nprivacy:\n  scrub: report\n')
    new, _ = S.fill_trust_defaults(bare)
    jira = new.split("  jira:\n", 1)[1].split("  auth:", 1)[0]
    assert "trust_statuses: [" in jira and "assumption_statuses: [" in jira, \
        f"статусы записаны не в блок jira:\n{new}"
    assert "verify:" in new and new.index("verify:") < new.index("privacy:"), \
        f"раздел verify не на месте:\n{new}"

    own = empty.replace("trust_statuses: []", "trust_statuses: [Готово]").replace(
        "trusted_sources: []", "trusted_sources: [Raw/мой]")
    new, _ = S.fill_trust_defaults(own)
    assert "trust_statuses: [Готово]" in new and "trusted_sources: [Raw/мой]" in new, \
        "обновление переписало настройку, которую проект задал сам"
    tpl = (KIT / "templates/aurora.config.yaml.template").read_text(encoding="utf-8")
    assert S.fill_trust_defaults(tpl)[1] == [], \
        "шаблон нового проекта расходится со значениями по умолчанию"

    (tmp / "u").mkdir()
    up = make_project(tmp / "u")
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_update.py"), str(up), "--apply"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout[-800:] + cp.stderr[-800:]
    cfg_text = (up / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "trust_statuses: [Закрыто" in cfg_text and "trusted_branches: [" in cfg_text, \
        f"обновление движка не записало значения доверия в конфиг:\n{cfg_text}"

    root = make_project(tmp)
    conf = root / "Sources" / "Confluence"
    for name in ("Раздел_-_Алгоритмы", "Логическая_модель_(ERD)",
                 "Нормативно-справочная_информация_(НСИ)", "Справочники_номенклатуры",
                 "Классификаторы_документов", "Контракты", "Протоколы_встреч", "_архив"):
        (conf / name).mkdir(parents=True, exist_ok=True)
    (conf / "Глоссарий.md").write_text("# Глоссарий\n", encoding="utf-8")
    def branches(kinds=AC.TRUSTED_BRANCHES_DEFAULT):
        return [f"Sources/Confluence/{n}" for n in sorted(os.listdir(conf))
                if not n.startswith((".", "_")) and AC.branch_kind(n, kinds)]
    got = branches()
    for need in ("Нормативно-справочная_информация_(НСИ)", "Справочники_номенклатуры",
                 "Классификаторы_документов", "Глоссарий.md"):
        assert f"Sources/Confluence/{need}" in got, f"справочная ветка не узнана по названию: {need} · {got}"
    for no in ("Раздел_-_Алгоритмы", "Логическая_модель_(ERD)",
               "Контракты", "Протоколы_встреч", "_архив"):
        assert f"Sources/Confluence/{no}" not in got, \
            f"вики доверена по названию — справочник или задача, не папка: {no}"
    assert not AC.branch_kind("GUI_-_Экранные_формы"), "GUI снова справочная — это постановка"
    assert not AC.branch_kind("Полный_перечень_работ"), "название узнано по куску слова"
    assert branches(("Контракты",)) == ["Sources/Confluence/Контракты"]

    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
        '    project_key: "T"\n',
        '    project_key: "T"\n    trust_statuses: []\n    assumption_statuses: []\n')
        + "verify:\n  trusted_sources: []\n  trusted_branches: []\n", encoding="utf-8")
    cur = os.getcwd()
    os.chdir(root)
    try:
        assert U.config_statuses("trust_statuses") == set(), \
            "пустой список прочитан как список из пустой строки — значения по умолчанию не включатся"
    finally:
        os.chdir(cur)
    cls, why = U.source_class("Sources/JIRA/PRJ-2.md", {"direct": {}, "indirect": {}},
                              {"PRJ-2": "Анализ"}, {"закрыто"}, {"анализ"})
    assert cls == "draft" and "сама задача" in why, (cls, why)

    jira = root / "Sources" / "JIRA"
    jira.mkdir(parents=True, exist_ok=True)
    (jira / "PRJ-1.md").write_text('---\nkey: "PRJ-1"\nstatus: "Закрыто"\n---\n', encoding="utf-8")
    (jira / "PRJ-2.md").write_text('---\nkey: "PRJ-2"\nstatus: "Анализ"\n---\n', encoding="utf-8")
    trace = root / "AuroraKnowledgeDB/meta/trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "trace.json").write_text(json.dumps({"direct": {
        "Sources/Confluence/Протоколы_встреч/Итог.md": [{"key": "PRJ-1", "why": "ключ"}],
        "Sources/Confluence/Протоколы_встреч/Спор.md": [{"key": "PRJ-2", "why": "ключ"}]},
        "indirect": {}}, ensure_ascii=False), encoding="utf-8")
    for name, status, src in (("Справочник", "draft", "Sources/Confluence/Справочники_номенклатуры/Ведомость.md"),
                              ("Итог", "draft", "Sources/Confluence/Протоколы_встреч/Итог.md"),
                              ("Спор", "knowledge", "Sources/Confluence/Протоколы_встреч/Спор.md"),
                              ("Опора", "draft", "Sources/JIRA/PRJ-1.md")):
        card(root, f"Concepts/{name}.md", status=status, kind="knowledge",
             sources=f'\n  - "{src}"', body=f"{name} — знание.")
    r = run("kb_trust.py", "--apply", cwd=root)
    assert "значения по умолчанию" in r.stdout, f"пересчёт не сказал, что взял значения по умолчанию:\n{r.stdout}"
    text = lambda n: (root / f"AuroraKnowledgeDB/Concepts/{n}.md").read_text(encoding="utf-8")
    assert "status: knowledge" in text("Справочник"), "справочная ветка вики не доверена по умолчанию"
    assert "status: knowledge" in text("Итог"), "статус «Закрыто» не дал доверия по умолчанию"
    assert "status: draft" in text("Спор"), "статус «Анализ» не признан предположением по умолчанию"
    assert "status: knowledge" in text("Опора"), "карточка с опорой на задачу не взяла её статус"

    cfg.write_text(cfg.read_text(encoding="utf-8").replace("trusted_branches: []",
                                                           "trusted_branches: [Глоссарий]"),
                   encoding="utf-8")
    run("kb_trust.py", "--apply", cwd=root)
    assert "status: draft" in text("Справочник"), \
        "заданный проектом список справочных веток не заменил значения по умолчанию"


@test
def test_settings_form_drops_the_dead_trust_key_and_clears_to_defaults(tmp: Path):
    """Форма не пишет и не переносит `trusted_sections`; очищенное поле доверия — умолчание.

    Ключ форма писала, а не читал ни один скрипт: доверие наследуется от источника, а не от
    раздела базы. Поле доверия, которое человек стёр, должно вернуть значения по умолчанию —
    пустой ответ формы раньше молча оставлял прежний список.
    """
    import contextlib
    import io
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("aurora_setup")
    root = tmp / "проект"
    root.mkdir()
    cfg = root / "aurora.config.yaml"
    cfg.write_text('project:\n  name: "П"\n  slug: "P"\n\natlassian:\n  jira:\n'
                   '    project_key: "P"\n    trust_statuses: [Закрыто]\n\n'
                   'verify:\n  trusted_sources: [Raw/contract]\n'
                   '  trusted_sections: [Glossary, Reference]\n', encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        A.run_answers(root, {"name": "П"})
    text = cfg.read_text(encoding="utf-8")
    assert "trusted_sections" not in text, f"форма перенесла ключ, который никто не читает:\n{text}"
    assert "trust_statuses: [Закрыто]" in text and "trusted_sources: [Raw/contract]" in text, \
        f"форма потеряла заданные списки доверия:\n{text}"
    with contextlib.redirect_stdout(io.StringIO()):
        A.run_answers(root, {"trust_statuses": "", "trusted_sources": " "})
    text = cfg.read_text(encoding="utf-8")
    assert "trust_statuses: [Закрыто, Разработка, Тестирование" in text \
        and "trusted_sources: [Raw/contract, Raw/customer" in text and "trusted_branches: [" in text, \
        f"очищенное поле не вернуло значения по умолчанию:\n{text}"
    for rel, src in (("cockpit/ui", ui_source()),
                     ("templates/aurora.config.yaml.template",
                      (KIT / "templates/aurora.config.yaml.template").read_text(encoding="utf-8"))):
        assert "trusted_sections" not in src, \
            f"{rel} всё ещё предлагает мёртвый ключ"


@test
def test_trace_ignores_service_files_short_names_and_hubs(tmp: Path):
    """Трассировка не связывает всё со всем: служебный файл, подстрока, общее имя, узел.

    Живой случай: состояние синка перечисляло все страницы, короткие имена справочников
    («Пол», «МНС») находились внутри других слов, а глоссарий упоминали все — и у каждого
    косвенного артефакта набиралось 33 задачи из 34. Одна из них всегда была в анализе, и
    закон оказывался черновиком из-за подзадачи про контракт.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    T = importlib.import_module("kb_trace_table")
    root = make_project(tmp)
    jira, conf = root / "Sources/JIRA", root / "Sources/Confluence"
    jira.mkdir(parents=True, exist_ok=True)
    conf.mkdir(parents=True, exist_ok=True)
    (jira / "PRJ-1.md").write_text(
        '---\nkey: "PRJ-1"\ntitle: "US-1.1. Экран"\nstatus: "Анализ"\n---\n', encoding="utf-8")
    page = lambda rel, title, body: ((conf / rel).parent.mkdir(parents=True, exist_ok=True),
                                     (conf / rel).write_text(f'---\ntitle: "{title}"\n---\n\n{body}\n',
                                                             encoding="utf-8"))
    # Прямая связь — ключом задачи в тексте; соседи называют страницу по имени. Код истории
    # в тексте (US-1.1) — уже прямая связь (1.137.0), поэтому соседи им не пользуются.
    page("Экран-оплаты.md", "Экран оплаты", "Задача PRJ-1.")
    page("Алгоритм.md", "Алгоритм", "см. Экран-оплаты")
    # у узла своя прямая связь: её задачи всё равно не раздаются тем, кто упомянул термин
    page("Термин.md", "Термин", "употреблён на экране Экран-оплаты, история US-1.1")
    names = [f"Страница-{i:02d}" for i in range(25)]
    for n in names:
        page(f"{n}.md", n, "Термин встречается и здесь. Полный текст.")
    page("Пол.md", "Пол", "см. Экран-оплаты")
    page("Сирота.md", "Сирота", "Полный список без ссылок.")
    page("А/index.md", "Ветка А", "Оглавление.")
    page("Б/index.md", "Ветка Б", "см. Экран-оплаты")
    page("Ссылка-на-оглавление.md", "Ссылка на оглавление", "см. index")
    (conf / "sync_state.md").write_text(
        "<!-- Confluence sync state — генерируется, не править руками -->\n| Title |\n|---|\n"
        + "".join(f"| {n} |\n" for n in names + ["Экран-оплаты", "Сирота", "Термин"]),
        encoding="utf-8")

    _tasks, arts = T.collect(str(root))
    assert not any(a["path"].endswith("sync_state.md") for a in arts), "состояние синка стало артефактом"
    t = T.build(str(root))
    ind = {k.split("Sources/Confluence/", 1)[-1]: v for k, v in t["indirect"].items()}
    assert "Алгоритм.md" in ind and ind["Алгоритм.md"][0]["depth"] == 1, f"прямой сосед потерян: {ind}"
    assert "Пол.md" in ind, "страница с коротким именем потеряла свою связь"
    assert "Сирота.md" not in ind, "подстрока «Пол» внутри «Полный» связала чужую страницу"
    assert "Ссылка-на-оглавление.md" not in ind, "имя двух файлов (index) принято за адрес"
    assert "Страница-00.md" not in ind, "трассировка прошла через страницу-узел"
    assert any(h.endswith("Термин.md") for h in t["hubs"]), f"узел не распознан: {t['hubs']}"


@test
def test_build_plan_skips_sync_instructions_in_mirrors(tmp: Path):
    """Правила и промпты прежних синков в зеркале — не источник знаний о проекте.

    Живой случай: из правил перевода вики и задач в Markdown сборка сделала девять карточек и
    разложила их по требованиям проекта. Выгрузка и аудит эти файлы узнавали, план сборки —
    нет. В Raw/ правило не действует: там слово в имени не делает документ служебным.
    """
    root = make_project(tmp)
    body = "Правило оформления. " * 40
    conf = root / "Sources/Confluence"
    conf.mkdir(parents=True, exist_ok=True)
    for name in ("confluence-to-md-rules.md", "Confluence_prompt.md", "Алгоритм_расчёта.md"):
        (conf / name).write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    raw = root / "Raw/customer"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "Business_rules.md").write_text(f"# Правила бизнеса\n\n{body}\n", encoding="utf-8")
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    cur = os.getcwd()
    os.chdir(root)
    try:
        B = importlib.import_module("build_plan")
        got = [p for _g, p, _s in B.sources()]
    finally:
        os.chdir(cur)
    assert any(p.endswith("Алгоритм_расчёта.md") for p in got), f"страница вики выпала из плана: {got}"
    for bad in ("confluence-to-md-rules.md", "Confluence_prompt.md"):
        assert not any(p.endswith(bad) for p in got), f"инструкция синка попала в план: {bad}"
    assert any(p.endswith("Business_rules.md") for p in got), \
        "документ заказчика выпал из плана из-за слова в имени"


@test
def test_artifact_codes_get_no_stubs_and_expansions_come_from_cards(tmp: Path):
    """Под голый код артефакта заготовка не заводится, а расшифровка берётся из карточек.

    Решения заказчика: заготовки под US/AC/Epic запрещены так же, как карточки из задач Jira
    (решения DR — предмет знания); расшифровку сокращения можно брать из текста самих
    карточек, если она сходится по первым буквам слов.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    F = importlib.import_module("kb_fix")
    for code in ("US-4.4.4", "AC-4.4", "REQ-044", "SPEC-012", "Epic 3", "Epic-3", "Эпик 2.1"):
        assert F.ARTIFACT_CODE_RE.match(code), f"код артефакта не узнан: {code}"
    for name in ("DR-0012", "ER.AS.KSM", "ПП-АРМ", "US-3.6.6 Получение сальдо"):
        assert not F.ARTIFACT_CODE_RE.match(name), f"не код артефакта принят за код: {name}"
    assert F.acronym_fits("ЭСФ", "электронный счёт-фактура")
    assert F.acronym_fits("FTA", "Federal Tax Authority")
    assert F.acronym_fits("НДС", "налог на добавленную стоимость")
    assert not F.acronym_fits("НДС", "см. раздел 3")

    class C:
        def __init__(self, text):
            self.text = text
    got = F.expansions_from_cards({
        "a": C("Счёт ЭСФ (электронный счёт-фактура) выставляется продавцом."),
        "b": C("Документ принимает Federal Tax Authority (FTA) в течение дня."),
        "c": C("НДС (см. раздел 3) начисляется отдельно.")})
    assert got.get("эсф") == "электронный счёт-фактура" and got.get("fta") == "Federal Tax Authority", got
    assert "ндс" not in got, f"пояснение в скобках принято за расшифровку: {got}"

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    card(root, "Concepts/Реестр.md", "Реестр описан в [[US-4.4.4]] и связан с [[Новое-понятие]].",
         status="draft")
    run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    assert (kb / "Concepts" / "Новое-понятие.md").exists(), "заготовка под понятие не заведена"
    assert not list(kb.rglob("US-4.4.4.md")), "под код истории заведена заготовка"

    card(root, "Concepts/US-4.4.4.md", "_Заготовка: имя названо в базе, знания пока нет._",
         status="placeholder", tags="[заготовка]")
    card(root, "Concepts/AC-4.4.md", "Критерии приёмки реестра: " + "поле заполнено. " * 10,
         status="draft")
    card(root, "Concepts/Экспорт.md", "Экспорт относится к US-4.4.4 и к [[Epic 3]].", status="draft")
    card(root, "Concepts/Отчёт.md", "Отчёт тоже из Эпик 3: этап внедрения.", status="draft")
    run("kb_fix.py", "--drop-code-stubs", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    assert (kb / "_archive" / "US-4.4.4.md").exists() and not (kb / "Concepts" / "US-4.4.4.md").exists(), \
        "пустышка под код артефакта осталась в базе"
    assert (kb / "Concepts" / "AC-4.4.md").exists(), "карточка с содержанием убрана вместе с пустышками"
    assert "[[US-4.4.4]]" in (kb / "Concepts" / "Реестр.md").read_text(encoding="utf-8"), \
        "ссылка на код снята — связь между карточками одной истории потеряна"

    # Код, который уже синоним карточки со знанием, индекса не получает: узел у него есть.
    card(root, "Concepts/Экспорт-реестра.md", "Выгрузка реестра в учётную систему.",
         status="draft", aliases='["US-7.1"]')
    card(root, "Concepts/Сверка.md", "Сверка идёт после US-7.1.", status="draft")
    card(root, "Concepts/Отчёт-сверки.md", "Отчёт строится по итогам US-7.1.", status="draft")

    # Индекс кода: связь по одной истории и одному эпику сохраняется, ссылки не битые.
    run("kb_moc.py", "--by-code", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    us = (kb / "MOC" / "US-4.4.4.md").read_text(encoding="utf-8")
    assert "[[Реестр|" in us and "[[Экспорт|" in us and "status: index" in us, \
        f"индекс кода не ведёт к карточкам, где код упомянут:\n{us}"
    epic = (kb / "MOC" / "Epic-3.md").read_text(encoding="utf-8")
    assert '"Epic 3"' in epic and '"Эпик 3"' in epic and "[[Отчёт|" in epic, \
        f"написания эпика не собраны в один индекс:\n{epic}"
    lint = run("kb_lint.py", cwd=root, expect_rc=None).stdout
    assert "[[US-4.4.4]]" not in lint and "[[Epic 3]]" not in lint, \
        f"ссылка на код не нашла индекс:\n{lint[:800]}"
    assert not (kb / "MOC" / "US-7.1.md").exists(), \
        "индекс заведён под код, который уже синоним карточки со знанием, — он перехватит ссылку"
    moc_errors = [l for l in lint.splitlines() if "/MOC/" in l and "артефакт в знаниях" in l]
    assert not moc_errors, f"служебный индекс кода назван артефактом в знаниях: {moc_errors[:3]}"


@test
def test_faq_question_is_one_section_and_a_card_fills_its_shadow_stub(tmp: Path):
    """Вопрос FAQ — одна секция целиком, а знание дописывается в заготовку из другого раздела.

    Живые случаи: склейка коротких секций уносила заголовок «Вопрос 5» в хвост предыдущего
    вопроса, и ответ на него не попал ни в одну карточку. И путь `--card` заводил знание в
    своём разделе рядом с пустой заготовкой того же имени — шесть таких пар на проекте.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    BP = importlib.import_module("build_plan")
    text = ("# FAQ\n\n**Вопрос 1. Сроки оформления**\n\n**Проблема:**\n\n"
            + "Описание проблемы первого вопроса. " * 3 + "\n\n**Вопросы:**\n\n- "
            + "Как быть в таком случае? " * 5 + '\n\n**<span class="mark">Ответ:</span>**\n\n'
            "**Да, оформлять необходимо.**\n\n**Вопрос 2. Навигационные пломбы**\n\n**Проблема:**\n\n"
            "С 11 февраля начался этап применения пломб.\n\n**Вопросы:**\n\n- "
            + "Будет ли исключение из системы? " * 5 + '\n\n**<span class="mark">Ответ:</span>**\n\n'
            "**Исключения не предусматриваются.**\n")
    secs = {t.split(".")[0]: b for t, b in BP.sections(text) if t.startswith("Вопрос")}
    assert set(secs) == {"Вопрос 1", "Вопрос 2"}, f"вопросы нарезаны не по одному: {list(secs)}"
    assert "Да, оформлять" in secs["Вопрос 1"] and "пломб" not in secs["Вопрос 1"], \
        "начало следующего вопроса уехало в хвост предыдущего"
    assert "Исключения не предусматриваются" in secs["Вопрос 2"], "ответ оторван от своего вопроса"

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    card(root, "Glossary/ПП-АРМ.md", "_Заготовка: имя названо в базе, знания о предмете пока нет._",
         status="placeholder", tags="[заготовка]")
    src = root / "Sources" / "Confluence" / "ПП.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# ПП АРМ\n\n## Назначение\n\n" + "Подсистема принимает документы. " * 12 + "\n",
                   encoding="utf-8")
    run("build_plan.py", "--card", "ПП АРМ", "--source", "Sources/Confluence/ПП.md",
        "--sections", "1", "--to", "Systems", "--apply", cwd=root, expect_rc=None)
    assert not (kb / "Systems" / "ПП-АРМ.md").exists(), "рядом с заготовкой заведена вторая карточка"
    got = (kb / "Glossary" / "ПП-АРМ.md").read_text(encoding="utf-8")
    assert "Подсистема принимает документы" in got, "знание не дописано в существующую карточку"
    assert "status: placeholder" not in got and "_Заготовка:" not in got, \
        "наполненная карточка осталась заготовкой — её не найдут в поиске"


@test
def test_golden_targets_are_proposed_not_rewritten(tmp: Path):
    """Переезд цели эталона предлагается списком, а переписываются только принятые строки.

    Эталон — измерительный прибор: прибор, который сам подгоняется под базу, перестаёт ловить
    деградацию (решение заказчика 15.09). Кандидата ищут по тексту вопроса и ответа.
    """
    import contextlib
    import io
    from unittest.mock import patch
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    Q = importlib.import_module("kb_search_quality")
    root = make_project(tmp)
    card(root, "Concepts/Движение-платежа.md", "Платёж проходит путь от банка до казначейства.",
         status="knowledge")
    gold = root / "AuroraKnowledgeDB" / "meta" / "golden_questions.md"
    gold.parent.mkdir(parents=True, exist_ok=True)
    gold.write_text("# Эталон\n\n| # | Вопрос | Эталон | Карточки |\n|---|---|---|---|\n"
                    "| 1 | Какой путь проходит платёж? | от банка до казначейства | [[Путь-платежа-ОП]] |\n"
                    "| 2 | Что такое реестр деклараций? | список поданных деклараций | [[Реестр-НД]] |\n"
                    "| 3 | Как движется платёж по счетам? | от банка до казначейства | [[Старое-имя]], [[Ещё-старое]] |\n",
                    encoding="utf-8")
    before = gold.read_text(encoding="utf-8")
    cwd = os.getcwd()
    try:
        os.chdir(root)
        with patch("kb_search_quality.ranked", return_value=[("Движение-платежа", 0.9)]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                Q.golden_remap({}, "m", set(), False)
            assert gold.read_text(encoding="utf-8") == before, "предложение переписало эталон само"
            assert "Путь-платежа-ОП" in buf.getvalue() and "Движение-платежа" in buf.getvalue(), \
                f"предложение не названо: {buf.getvalue()[:400]}"
            with contextlib.redirect_stdout(io.StringIO()):
                Q.golden_remap({}, "m", {1, 3}, True)
    finally:
        os.chdir(cwd)
    text = gold.read_text(encoding="utf-8")
    assert "| 1 | Какой путь проходит платёж? | от банка до казначейства | [[Движение-платежа]] |" in text, text
    assert "[[Реестр-НД]]" in text, "переписана строка, которую человек не принимал"
    assert "| 3 | Как движется платёж по счетам? | от банка до казначейства | [[Движение-платежа]] |" in text, \
        f"несколько пропавших целей переехали в одну карточку повтором ссылок:\n{text}"


@test
def test_running_command_survives_a_page_reload(tmp: Path):
    """Работающая команда видна после перезагрузки страницы, и второй запуск переспрашивают.

    Задание живёт в процессе панели, консоль — в открытой странице. Перезагрузили её
    (обновили kit, открыли заново) — команда работает, а консоль пуста: человек делает
    единственный разумный вывод «оборвалось» и запускает второй маршрут поверх первого.
    Очереди у панели нет: два задания идут одновременно и дерутся за git и зеркала.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ck.JOBS.clear()
    ck.JOBS["a"] = {"id": "a", "cmd": "kb:lint", "args": [], "project": "/p/one",
                    "out": ["строка"], "started": 100.0, "done": False, "rc": None}
    ck.JOBS["b"] = {"id": "b", "cmd": "sync:jira", "args": [], "project": "/p/two",
                    "out": [], "started": 90.0, "done": False, "rc": None}
    ck.JOBS["c"] = {"id": "c", "cmd": "ops:stats", "args": [], "project": "/p/one",
                    "out": [], "started": 80.0, "done": True, "rc": 0}
    live = [j for j in ck.JOBS.values() if not j["done"] and j["project"] == "/p/one"]
    assert [j["id"] for j in live] == ["a"], \
        "живые задания проекта отбираются неверно: чужое или завершённое попало в список"
    ck.JOBS.clear()

    ui = panel_sources()
    assert "/api/jobs?project=" in ui, "панель не спрашивает, что выполняется прямо сейчас"
    assert 'id="consoleLive"' in ui and "function attachJob" in ui, \
        "к работающему заданию нельзя подключиться — его вывод потерян навсегда"
    assert "busyElsewhere" in ui and "не ставит задания в очередь" in ui, \
        "второй запуск поверх работающего идёт молча, а это гонка, а не очередь"
    run = ui[ui.index("async function runRoute("):ui.index("async function attachRoute(")]
    assert run.index("await busyElsewhere(sc.title)") < run.index('"/api/route/run"'), \
        "маршрут спрашивает про занятость после того, как начал"


@test
def test_cockpit_can_recount_metrics(tmp: Path):
    """Базу правят не только команды панели — числа нужно уметь пересчитать и вручную."""
    ui = panel_sources()
    for btn, page in (("refreshHealth", "«Здоровье»"), ("refreshOverview", "«Мостик»")):
        assert f'id="{btn}"' in ui, f"на странице {page} нет кнопки пересчёта"
        # Раздел-модуль вешает обработчик на свой узел (`ctx.$("#…")`), ядро — на общий.
        assert (f'$("#{btn}").onclick' in ui or f'ctx.$("#{btn}")' in ui), \
            f"кнопка пересчёта на {page} ничего не делает"
    assert ('stamp("#healthStamp")' in ui or 'ctx.$("#healthStamp")' in ui) \
        and 'stamp("#overviewStamp")' in ui, \
        "нет отметки времени: по числам не понять, до работы они посчитаны или после"


@test
def test_cockpit_warns_when_project_engine_lags(tmp: Path):
    """Флаги панель берёт из kit'а, а запускает движок проекта — расхождение нужно назвать."""
    ui = panel_sources()
    assert "S.project.behind && !r.from_kit" in ui, \
        "ящик команды не предупреждает, что флаги из kit'а, а движок проекта другой"
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"from_kit": script in KIT_SIDE' in srv, \
        "панель не различает скрипты, которые и так запускаются из kit'а"


@test
def test_every_command_is_reachable_in_the_panel(tmp: Path):
    """У каждой команды реестра есть ровно один путь в панели.

    Реестр растёт, панель — нет: команда появляется в `commands.txt`, а нажать её негде.
    Обратная беда тише: команда показана в двух местах, и при правке расходятся оба.
    Разделение простое — движковые (`dev:` и `kit:skills`) живут в скрытом разделе
    «Разработка», остальные в «Командах».
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    rows = ck.registry()
    ui = panel_sources()

    assert "ENGINE_CMDS" in ui and "isEngineCmd" in ui, \
        "в панели нет правила, что считать движковой командой"
    engine = [r for r in rows if r["ns"] == "dev" or r["cmd"] == "kit:skills"]
    assert engine, "движковые команды пропали из реестра панели"

    # «Команды» показывают всё, кроме движковых, — и не требуют исключений по одной
    # Список команд уехал в раздел-папку и зовёт то же правило через ctx: важно, что
    # правило одно на панель, а не то, как называется переменная рядом с ним.
    assert ("S.state.commands.filter(r => !isEngineCmd(r))" in ui
            or "ctx.state.commands.filter(r => !ctx.isEngineCmd(r))" in ui), \
        "общий список команд фильтруется не по общему правилу"
    # «Разработка» показывает ровно движковые
    assert "(ctx.state.commands || []).filter(ctx.isEngineCmd)" in ui, \
        "раздел разработки собирается не по тому же правилу"

    # ни одна команда не потерялась и не показана дважды
    titles = {"kit", "sync", "kb", "ctx", "make", "ship", "ops", "dev", "agent"}
    lost = [r["cmd"] for r in rows if r["ns"] not in titles]
    assert not lost, f"команды вне известных групп — в панели им нет места: {lost}"
    # Подписи групп уехали в каталог раздела «Команды»: проверяем ключ, а не форму
    # записи объекта, которой больше нет.
    assert "commands.ns.agent" in ui, "группа agent не подписана в «Командах»"

    # у каждой запускаемой команды есть исполнитель на диске
    for r in rows:
        if r["runnable"]:
            assert (KIT / "scripts" / r["script"]).is_file(), \
                f"{r['cmd']}: панель предложит запуск, а файла нет — {r['script']}"

    # порядок в разделе разработки перечисляет реальные команды, а не выдуманные
    dev_block = (KIT / "cockpit/modules/dev/view.js").read_text(encoding="utf-8")
    order = re.search(r"const order = \[(.*?)\];", dev_block, re.S).group(1)
    named = re.findall(r'"([\w:-]+)"', order)
    known = {r["cmd"] for r in rows}
    assert all(n in known for n in named), \
        f"в порядке раздела названы несуществующие команды: {[n for n in named if n not in known]}"


@test
def test_editor_cannot_reach_outside_the_project(tmp: Path):
    """Редактор — первая в панели возможность «записать что угодно куда угодно».

    Панель принципиально не исполняет произвольных строк и запускает только команды
    реестра. Файловый редактор эту стену пробивает, и держать её теперь должен код, а
    не интерфейс: путь вне проекта, ссылка наружу, чужое расширение.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "proj"
    (root / "Artifacts" / "ac").mkdir(parents=True)
    (root / "Artifacts" / "ac" / "AC-1.md").write_text("# AC\n\nтело\n", encoding="utf-8")
    secret = tmp / "снаружи.md"
    secret.write_text("чужое\n", encoding="utf-8")

    assert ck.inside(str(root), "Artifacts/ac/AC-1.md"), "свой файл не признан своим"
    assert not ck.inside(str(root), "../снаружи.md"), "путь наверх не отсечён"
    assert not ck.inside(str(root), "Artifacts/../../снаружи.md"), "путь наверх через папку"
    # Абсолютный путь превращается в относительный внутри проекта, а не в побег:
    # `os.path.join(root, "/etc/passwd")` в Python вернул бы «/etc/passwd».
    assert ck.inside(str(root), "/etc/passwd").startswith(str(root.resolve())), \
        "абсолютный путь ушёл за пределы проекта"

    # Ссылка наружу опаснее «..»: её не видно в самом пути.
    os.symlink(str(tmp), str(root / "вон"))
    assert "text" not in ck.file_read(str(root), "вон/снаружи.md"), \
        "прочитали файл за периметром по символической ссылке"

    assert ck.file_write(str(root), "../снаружи.md", "затёрто").get("error"), \
        "запись за пределы проекта не отклонена"
    assert secret.read_text(encoding="utf-8") == "чужое\n", "файл снаружи всё-таки изменён"


@test
def test_editor_respects_what_must_not_be_edited(tmp: Path):
    """Инварианты базы — не текст в скилле, а поведение: мышью их нарушить нельзя.

    Поставленное неизменяемо, доказательная часть Raw не правится, зеркало сотрёт
    следующий синк, а карточка базы выводится из источников — её правят корректирующим
    артефактом. Редактор, открывающий всё подряд на запись, обнуляет это одним движением.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    closed = ["Deliverables/released/ОПЗ.md", "Raw/contract/ТЗ.md", "Raw/meetings/п.md",
              "Raw/laws/з.md", "Raw/customer/письмо.md", "Sources/Confluence/Стр.md",
              "AuroraKnowledgeDB/Concepts/Заявка.md"]
    for rel in closed:
        why = ck.why_readonly(rel)
        assert why, f"{rel} открыт на запись"
        assert len(why) > 20, f"{rel}: запрет без объяснения — его обойдут мимо панели"

    # Рукотворное в базе ниоткуда не выводится: корректирующий артефакт для него
    # бессмыслен, потому что нет карточки-владельца в источниках.
    for rel in ["AuroraKnowledgeDB/Decisions/DR-1.md", "AuroraKnowledgeDB/Questions/Q-1.md",
                "AuroraKnowledgeDB/meta/releases.md", "Raw/project/заметка.md",
                "Raw/examples/пример.md", "Artifacts/ac/AC-1.md", "Workspaces/x/черновик.md"]:
        assert not ck.why_readonly(rel), f"{rel} закрыт зря — править его законно"

    assert ck.why_readonly("Artifacts/x.md", "---\nkind: document\n---\n"), \
        "карточка kind: document правится: тело переносится дословно и не переписывается"


@test
def test_saving_does_not_silently_overwrite(tmp: Path):
    """Файл под редактором меняется и снаружи: агент дописывает артефакт, синк приносит
    чужое, его правят в Obsidian. Молча затирать — тот же класс, что публикация поверх
    чужой страницы: работа исчезает, и об этом никто не узнаёт."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "proj"
    (root / "Artifacts" / "ac").mkdir(parents=True)
    path = root / "Artifacts" / "ac" / "AC-1.md"
    path.write_text("# AC\n\nпервое\n", encoding="utf-8")

    opened = ck.file_read(str(root), "Artifacts/ac/AC-1.md")
    assert opened["digest"], "файл открыт без слепка — расхождение потом не поймать"

    path.write_text("# AC\n\nчужое\n", encoding="utf-8")     # кто-то записал, пока читали
    res = ck.file_write(str(root), "Artifacts/ac/AC-1.md", "# AC\n\nмоё\n",
                        expect=opened["digest"])
    assert res.get("conflict"), "чужая правка затёрта молча"
    assert res.get("disk", "").strip().endswith("чужое"), \
        "конфликт объявлен, а показать человеку нечего — выбирать не из чего"
    assert path.read_text(encoding="utf-8").endswith("чужое\n"), "затёрли, объявив конфликт"

    # Осознанное решение человека «оставить моё» проходит: слепок не передан.
    ok = ck.file_write(str(root), "Artifacts/ac/AC-1.md", "# AC\n\nмоё\n")
    assert ok.get("ok") and path.read_text(encoding="utf-8").endswith("моё\n")
    assert not list(path.parent.glob("*.aurora-tmp")), \
        "временный файл остался рядом: следующий обход дерева покажет его человеку"

    # Критик после реализации: отказ файловой системы уходил трассировкой. Длинное имя,
    # полный диск, папка без прав — обычные исходы, а не исключительные; для человека
    # трассировка означает «панель сломалась», хотя сломался его путь.
    long = ck.file_write(str(root), "Artifacts/ac/" + "д" * 400 + ".md", "текст")
    assert long.get("error") and "записать" in long["error"], \
        f"отказ файловой системы не превращён в ответ: {long}"
    assert not list((root / "Artifacts" / "ac").glob("*.aurora-tmp")), \
        "после отказа остался обрубок"

    # И линтер после сохранения не имеет права решать судьбу сохранения: он читает базу
    # целиком, на большой базе висел до потолка — а файл к тому моменту уже записан.
    (root / ".opencode" / "scripts").mkdir(parents=True)
    (root / ".opencode" / "scripts" / "kb_lint.py").write_text(
        "import time; time.sleep(300)", encoding="utf-8")
    slow = ck.file_write(str(root), "Artifacts/ac/AC-1.md", "# AC\n\nещё\n")
    assert slow.get("ok"), "зависший линтер отменил сохранение"
    assert slow["lint"]["lines"] and "не ответил" in slow["lint"]["lines"][0], \
        "молчание линтера выглядит как «находок нет»"
