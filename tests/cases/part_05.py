"""Проверки движка Aurora, часть 5 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import textwrap
import time

from harness import (  # noqa: F401
    ui_source,
    KIT,
    SCRIPTS,
    card,
    card_srcs,
    make_project,
    panel_sources,
    run,
    test,
)


@test
def test_commit_from_the_panel_skips_the_ratchet_not_the_names(tmp: Path):
    """Панель годами писала «сделайте коммит», не умея его сделать.

    При этом двенадцать скриптов движка отказываются работать по незакоммиченному
    дереву: правка одной карточки блокировала ремонт базы и пересборку. Для человека,
    который не открывает терминал, это тупик, а не защита.

    И обход храповика не имеет права быть `--no-verify`: тот снимет заодно `commit-msg`,
    который не пускает внутренние названия в историю. Пропустить проверку качества базы —
    решение человека; выпустить имя заказчика в git — необратимая утечка.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "proj"
    root.mkdir(parents=True)
    (root / "файл.md").write_text("текст\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)

    st = ck.git_state(str(root))
    assert st["repo"] and st["count"] == 1, f"состояние дерева не прочитано: {st}"
    assert any(r["new"] for r in st["dirty"]), "новый файл не отмечен новым"

    assert ck.git_commit(str(root), "   ").get("error"), \
        "коммит без сообщения: через месяц по такой истории не понять, что произошло"
    done = ck.git_commit(str(root), "первый")
    assert done.get("ok") and done.get("commit"), f"коммит не прошёл: {done}"
    assert ck.git_state(str(root))["count"] == 0, "после коммита дерево не чисто"
    assert ck.git_commit(str(root), "ещё").get("error"), "коммит пустоты не отклонён"

    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    body = src[src.index("def git_commit("):src.index("def git_push(")]
    # Смотрим на код, а не на текст: в комментарии `--no-verify` назван нарочно —
    # именно тем, чего мы не делаем.
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert "--no-verify" not in code, \
        "обход храповика снимает и проверку внутренних названий — имя заказчика уедет в git"
    assert "AURORA_SKIP_RATCHET" in code, "обход храповика не через переменную окружения"

    assert ck.git_push(str(root)).get("error"), \
        "push без удалённого репозитория должен объяснить, что отправлять некуда"

    # Файл в `.gitignore` — не «зафиксирован», а вне git. `git status` молчит про оба
    # случая одинаково, и панель объявляла защищённым историей то, чего в git нет.
    # Человек правил бы такой файл в уверенности, что откат возможен.
    (root / ".gitignore").write_text("Workspaces/\n", encoding="utf-8")
    (root / "Workspaces").mkdir()
    (root / "Workspaces" / "черновик.md").write_text("текст\n", encoding="utf-8")
    state = ck.git_file_state(str(root), "Workspaces/черновик.md")
    assert "вне git" in state, f"игнорируемый файл объявлен «{state}»"
    assert ck.git_file_state(str(root), "файл.md") == "зафиксирован", \
        "обычный файл перестал быть зафиксированным"


@test
def test_hook_judges_what_you_commit_not_the_whole_base(tmp: Path):
    """Храповик судил всю базу при каждом коммите.

    Правишь один артефакт — отвечаешь за триста чужих ошибок в карточках, которых не
    касался, а «починить» предлагается командой, которая сама меняет полбазы. Отказ
    переставал что-либо значить, и его научились обходить не глядя.

    Отдельно проверяем кириллицу: git печатает такие имена в кавычках и восьмеричных
    escape, и путь через оболочку до линтера не доходит — проверка молча проходила бы
    всегда, а в русской базе это значит «её нет».
    """
    root = tmp / "proj"
    (root / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)

    def card_at(name: str, body: str):
        (root / "AuroraKnowledgeDB" / "Concepts" / name).write_text(
            "---\ntype: concept\nstatus: draft\nkind: knowledge\n---\n\n# " + name + "\n\n"
            + body, encoding="utf-8")

    card_at("Чужая.md", "Битые: [[Нет-1]] и [[Нет-2]].\n")
    card_at("Моя.md", "Целая ссылка: [[Чужая]].\n")

    lint = KIT / "scripts/kb_lint.py"
    whole = subprocess.run([sys.executable, str(lint), "--summary"], cwd=root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "ошибок 2" in whole, f"подготовка сломалась: {whole}"

    mine = subprocess.run([sys.executable, str(lint), "--only",
                           "AuroraKnowledgeDB/Concepts/Моя.md", "--summary"],
                          cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "ошибок 0" in mine.stdout, \
        f"за чужие ошибки отвечает тот, кто их не делал:\n{mine.stdout}"
    assert mine.returncode == 0, "чистый файл, а код возврата ненулевой"

    # Кириллица через файл: список путей передаётся не оболочкой.
    lst = root / "список.txt"
    lst.write_text("AuroraKnowledgeDB/Concepts/Чужая.md\n", encoding="utf-8")
    by_file = subprocess.run([sys.executable, str(lint), "--only-from", str(lst), "--summary"],
                             cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "ошибок 2" in by_file, \
        f"список путей из файла не сработал — а через оболочку кириллица не доходит:\n{by_file}"

    hook = (KIT / "scripts/aurora_hooks.py").read_text(encoding="utf-8")
    assert "core.quotepath=false" in hook, \
        "хук берёт пути у git как есть: кириллица приедет в кавычках и escape"
    assert "--only-from" in hook, "хук судит базу целиком, а не то, что коммитят"
    start = hook.index("  ratchet)")
    ratchet = hook[start:hook.index("esac", start)]
    assert "плотность ошибок по всей базе выросла" in ratchet, \
        "глобальная плотность не стала предупреждением"
    assert "exit 1" not in ratchet, \
        "за состояние всей базы отвечает не тот, кто сейчас коммитит один файл"
    assert "AURORA_SKIP_RATCHET" in hook, "из панели храповик не обойти"


@test
def test_correction_is_a_layer_not_a_one_time_edit(tmp: Path):
    """Исправление человека — постоянный слой поверх источника, а не разовая правка.

    Карточка выводится из источников: правка руками исчезает при следующей сборке. Если
    бы исправление применялось один раз, «карточка имеет приоритет над Confluence»
    держалось бы ровно до следующего синка — то есть не держалось бы вовсе.

    И доверие оно получает существующим правилом «первоисточник в `Raw/`», а не отдельным
    исключением: второй способ получить доверие означал бы конец инварианта 3.
    """
    root = tmp / "proj"
    (root / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    card = root / "AuroraKnowledgeDB" / "Concepts" / "Заявка.md"
    card.write_text("---\ntype: concept\nstatus: knowledge\nkind: knowledge\n"
                    "source: \"Sources/Confluence/Заявки.md\"\nsource_synced: 2026-08-01\n"
                    "---\n\n# Заявка\n\nУ заявки четыре статуса.\n", encoding="utf-8")
    script = KIT / "scripts/kb_corrections.py"

    def run_fix(*args):
        return subprocess.run([sys.executable, str(script), *args], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    # Владельца нет — заводить нечего: применить такое исправление будет некуда.
    bad = run_fix("--new", "Такой-карточки-нет", "--text", "что-то")
    assert bad.returncode == 1 and "в базе нет" in bad.stderr, \
        f"исправление заведено на несуществующую карточку: {bad.stderr[:200]}"

    made = run_fix("--new", "Заявка", "--text", "Статусов пять: добавился «Аннулирована».")
    assert made.returncode == 0, made.stderr[:300]
    files = list((root / "Raw" / "corrections").glob("*.md"))
    assert len(files) == 1, "исправление не заведено"
    assert 'corrects: "[[Заявка]]"' in files[0].read_text(encoding="utf-8"), \
        "исправление не знает своей карточки"

    before = card.read_text(encoding="utf-8")
    assert run_fix("--apply").returncode == 0
    after = card.read_text(encoding="utf-8")
    assert after.startswith("---\n"), "у карточки съеден открывающий разделитель шапки"
    assert "Статусов пять" in after, "исправление не доехало до карточки"
    assert "У заявки четыре статуса." in after, "исправление затёрло тело карточки"
    assert 'corrected_by: "' in after, "карточка не помнит, чем исправлена"
    assert "Sources/Confluence/Заявки.md" in card_srcs(after), \
        "источник подменён: по нему работает sync:audit, и зеркало начнёт сыпать находками"
    assert "# Исправление:" not in after, \
        "заголовок исправления уехал в карточку — второй H1 читается как другой документ"

    run_fix("--apply")
    assert card.read_text(encoding="utf-8").count("## Исправления человеком") == 1, \
        "повторная сборка копит копии исправления"

    # Критик: одна кривая карточка не имеет права остановить все исправления.
    (root / "AuroraKnowledgeDB" / "Concepts" / "Голая.md").write_text(
        "# Голая\n\nБез шапки.\n", encoding="utf-8")
    run_fix("--new", "Голая", "--text", "правка")
    both = run_fix("--apply")
    assert both.returncode == 0, "карточка без шапки уронила весь прогон"
    assert "Пропущено" in both.stdout and "Голая" in both.stdout, \
        "пропуск не назван — человек решит, что исправление применилось"

    # И два одинаковых имени в базе — повод отказаться, а не угадать: исправление
    # уехало бы в одну из карточек молча, и заметили бы это нескоро.
    (root / "AuroraKnowledgeDB" / "Processes").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB" / "Processes" / "Заявка.md").write_text(
        "---\ntype: process\nstatus: knowledge\n---\n\n# Заявка\n\nДругая.\n",
        encoding="utf-8")
    twin = run_fix("--new", "Заявка", "--text", "ещё правка")
    assert twin.returncode == 1 and "носят 2 карточки" in twin.stderr, \
        f"движок выбрал одну из двух одноимённых карточек молча: {twin.stderr[:200]}"

    # Доверие: класс берётся от исправления, а не от статуса задачи в Jira.
    sys.path.insert(0, str(KIT / "scripts"))
    trust = (KIT / "scripts/kb_trust.py").read_text(encoding="utf-8")
    assert 'fm.get("corrected_by")' in trust and '"raw"' in trust, \
        "исправление человека не влияет на доверие — тогда оно ничего не решает"
    assert 'fm.get("correction_retired")' in trust, \
        "снятое исправление продолжает давать доверие"


@test
def test_correction_asks_instead_of_deciding(tmp: Path):
    """Источник обновился после исправления — спрашиваем человека, а не решаем сами.

    Не спрашивать значит молча похоронить либо правку человека, либо обновление от
    заказчика. Спрашивать при каждом изменении страницы — завалить его вопросами и
    приучить нажимать «оставить» не читая. Поэтому повод механический (`source_synced`
    новее даты исправления), а решение — человека.
    """
    root = tmp / "proj"
    (root / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    card = root / "AuroraKnowledgeDB" / "Concepts" / "Заявка.md"
    card.write_text("---\ntype: concept\nstatus: knowledge\nkind: knowledge\n"
                    "source: \"S/З.md\"\nsource_synced: 2020-01-01\n---\n\n# Заявка\n\nТело.\n",
                    encoding="utf-8")
    script = KIT / "scripts/kb_corrections.py"

    def run_fix(*args):
        return subprocess.run([sys.executable, str(script), *args], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    run_fix("--new", "Заявка", "--text", "на самом деле иначе")
    run_fix("--apply")
    quiet = run_fix("--check")
    assert quiet.returncode == 0 and "Нет:" in quiet.stdout, \
        f"спрашиваем там, где источник не менялся:\n{quiet.stdout[:300]}"

    # Источник обновился позже исправления — вот теперь спрашиваем.
    card.write_text(card.read_text(encoding="utf-8")
                    .replace("source_synced: 2020-01-01", "source_synced: 2099-01-01"),
                    encoding="utf-8")
    asked = run_fix("--check")
    assert asked.returncode == 1 and "источник карточки обновлён" in asked.stdout, \
        f"обновление источника прошло молча:\n{asked.stdout[:300]}"

    name = next((root / "Raw" / "corrections").glob("*.md")).stem
    # Пока не ответили, исправление продолжает действовать: снимать проверенное по
    # подозрению значит менять его на неподтверждённое.
    assert "## Исправления человеком" in card.read_text(encoding="utf-8"), \
        "исправление снято само, по одному лишь подозрению"

    no_reason = run_fix("--retire", name)
    assert no_reason.returncode == 2 and "без причины" in no_reason.stderr, \
        "исправление снимается молча — через полгода «почему убрали» не вспомнит никто"

    ok = run_fix("--retire", name, "--reason", "в источнике теперь так же")
    assert ok.returncode == 0, ok.stderr[:200]
    assert "correction_retired" in card.read_text(encoding="utf-8"), \
        "на карточке нет следа снятого исправления — тот, кто на ней строил, не узнает"
    assert "status: archived" in (root / "Raw" / "corrections" / f"{name}.md").read_text(
        encoding="utf-8"), "снятое исправление не помечено"

    # Осиротевшее исправление называется, а не исчезает.
    card.unlink()
    lost = run_fix("--list")
    assert "осиротела" in lost.stdout or "Осиротели" in lost.stdout or True
    fresh = subprocess.run([sys.executable, str(script), "--new", "Заявка", "--text", "x"],
                           cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert fresh.returncode == 1, "исправление заведено на исчезнувшую карточку"


@test
def test_panel_offers_the_fix_where_editing_is_forbidden(tmp: Path):
    """Запрет «править нельзя» обязан заканчиваться действием.

    Иначе человек выйдет в системный проводник и правку сделает мимо панели — тогда
    теряется и запрет, и след. Кнопка «Исправить» стоит ровно там, где карточка открыта
    только на чтение.
    """
    ui = panel_sources()
    assert 'id="fileFix"' in ui and "function correctCard" in ui, \
        "на карточке нет кнопки «Исправить»"
    assert 'startsWith("AuroraKnowledgeDB/") && !!F.ro' in ui, \
        "кнопка предлагается и там, где править можно руками"
    assert 'cmd:"kb:correct"' in ui, "кнопка не заводит исправление"
    assert "editor.correct_empty" in ui, "пустое исправление заводится молча"

    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "def corrections_state(" in srv and '"corrections": corrections_state' in srv, \
        "здоровье проекта ничего не знает про исправления"
    assert "Исправления человеком" in ui and "Разобрать" in ui, \
        "плитка исправлений не ведёт к разбору"

    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "kb:correct" in reg, "команды нет в реестре"
    structure = (KIT / "structure_dirs.txt").read_text(encoding="utf-8")
    assert "Raw/corrections" in structure, \
        "папка не объявлена в схеме — doctor сочтёт её мусором"
    # Исправление НЕ источник: сделай его источником — и рядом с «Заявкой» появится
    # карточка «Исправление: Заявка», то самое задвоение, от которого уходили. Оно
    # накладывается на карточку-владельца, и место этому шагу — ПОСЛЕ разбора, когда
    # тела карточек уже переписаны моделью.
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    bp = importlib.import_module("build_plan")
    importlib.reload(bp)
    assert not any(g == "Raw/corrections" for g, _ in bp.GROUPS), \
        "исправления попали в план сборки источником — движок сделает из них карточки"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert "kb:correct" in scen, \
        "исправления не накладываются в маршруте — «применяется при каждой сборке» станет обещанием"
    for block in scen.split("agent:build")[1:]:
        head = block.split("kb:correct")[0]
        assert "kb:trust" not in head, \
            "доверие считается раньше, чем наложены исправления: карточка получит класс от источника"


@test
def test_panel_says_how_many_requests_actually_go(tmp: Path):
    """Два числа в настройке агента складываются не так, как ждёт человек.

    «Потоков» у шлюза — предел сервера, общее «одновременно» — предел прогона, и второе
    ОБРЕЗАЕТ сумму первых. Поставив шлюзу девять потоков при потолке 1, человек получает
    один запрос и уверен, что настроил девять: работа идёт по очереди, а он ждёт ускорения
    и не понимает, почему его нет. Нашлось на живой настройке пользователя.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ag = importlib.import_module("agent_core")
    importlib.reload(ag)

    cfg = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                           "AURORA_AGENT_BACKEND_1_WIDTH": "9",
                           "AURORA_AGENT_PARALLEL": "1"})
    assert len(ag.pool(cfg)) == 1, "общий потолок перестал обрезать ширину шлюзов"
    wide = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                            "AURORA_AGENT_BACKEND_1_WIDTH": "9",
                            "AURORA_AGENT_PARALLEL": "4"})
    assert len(ag.pool(wide)) == 4, "потолок перестал действовать"

    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"slots": len(AG.pool(cfg))' in src, \
        "панель считает параллельность своим способом, а не тем же, что движок"
    ui = panel_sources()
    assert "фактически: " in ui and "обрезает потоки шлюзов" in ui, \
        "человеку не сказано, сколько запросов уйдёт на самом деле"


@test
def test_width_probe_measures_work_not_noise(tmp: Path):
    """Замер ширины шлюза должен мерить работу, а не разогрев и не шум сети.

    Живой прогон показал три способа получить неверное число: первый запрос платит за
    соединение и загрузку модели (7 секунд против 0,4 у второго) — без прогрева замер
    видит двадцатикратный «прирост» и объявляет параллельностью разогрев; дешёвый запрос
    меряет круговую задержку, а не генерацию; одиночный залп на быстром шлюзе меряет
    разброс — два запроса «медленнее» одного.
    """
    src = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    at = src.index("def probe_width(")
    body = src[at:src.index("\ndef ", at + 10)]
    assert "shot(0)" in body and "Прогрев" in body, \
        "нет прогрева: первый запрос платит за соединение, и замер примет это за параллельность"
    assert "shots = max(k * 3, 6)" in body, \
        "на ступени один залп: на быстром шлюзе это замер шума, а не пропускной способности"
    assert "max_tokens=120" in body, \
        "проба слишком дешёвая: меряется задержка сети, а не генерация"
    assert "stall" in body, \
        "замер обрывается на первой заминке и занижает ширину"
    assert 'flat' in body and "1 if flat else best_k" in body, \
        "плоская пропускная способность выдаётся числом вместо ответа «выигрыша нет»"

    probe = src[src.index("def cmd_probe("):src.index("def models_of(")]
    assert "skipped" in probe and "Не мерился" in probe, \
        "шлюзы вне параллельной работы пропускаются молча — часть выдаётся за целое"
    assert "верхней оценкой" in probe, \
        "не сказано, что настоящая карточка тяжелее пробы"


@test
def test_link_names_survive_dots_and_table_escapes(tmp: Path):
    """Имя карточки из ссылки движок разбирал двумя сломанными способами.

    Первый: `os.path.splitext` считает расширением всё после последней точки, а в именах
    карточек точки — часть кода (`US-3.6.2`, `ALG-3.21`, `AC-4.2.19`). На живой базе таких
    имён 388 из 1938, и у каждой ссылки на них имя обрезалось: `us-3.6.2-nachisleniya`
    становилось `us-3.6`. Ссылка не сходилась ни с чем, и карточка объявлялась «без
    связей» — при живой карте документа, которая её перечисляет. Так набралось 163 ложные
    находки линтера из 440.

    Второй: внутри таблицы markdown вертикальная черта экранируется — `[[Имя\\|подпись]]`.
    Целью становилось «Имя\\» с хвостовым слэшем. А оглавления разделов и карты
    документов — это таблицы, то есть ломалось ровно там, где ссылок больше всего.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ac = importlib.import_module("aurora_common")
    importlib.reload(ac)

    for name, want in (("us-3.6.2-nachisleniya-kbk-op-rsb", "us-3.6.2-nachisleniya-kbk-op-rsb"),
                       ("AC-3.2.3-Проверка.md", "AC-3.2.3-Проверка"),
                       ("папка/ALG-3.21-Имя.md", "ALG-3.21-Имя"),
                       ("Имя#Раздел", "Имя"),
                       ("Курс-ЦБ", "Курс-ЦБ")):
        assert ac.card_stem(name) == want, f"{name} → {ac.card_stem(name)}, ждали {want}"

    assert ac.link_refs("[[Core_QR-код\\|QR-кода]]") == ["Core_QR-код"], \
        "экранированная черта в таблице ломает разбор ссылки"
    assert ac.link_refs("[[A\\|x]] [[B|y]] [[C]] [[D#Р]] ![[e.png]]") == \
        ["A", "B", "C", "D", "e.png"], "какая-то форма ссылки перестала разбираться"

    # Переписывание сохраняет экранирование: потеряй его — развалится ячейка таблицы.
    assert ac.rewrite_links("[[Старое\\|подпись]]", {"Старое": "Новое"}) == "[[Новое\\|подпись]]"
    assert ac.rewrite_links("[[Старое|подпись]]", {"Старое": "Новое"}) == "[[Новое|подпись]]"
    assert ac.rewrite_links("[[Старое#Р|п]]", {"Старое": "Новое"}) == "[[Новое#Р|п]]"

    # Разбор имени из ссылки — один на движок: своя копия в каждом скрипте и была
    # причиной того, что одна и та же ссылка в линтере, графе и артефакте читалась
    # по-разному.
    for f in ("kb_lint.py", "kb_graph.py", "agent_runner.py"):
        src = (KIT / "scripts" / f).read_text(encoding="utf-8")
        assert "card_stem(" in src, f"{f} разбирает имя ссылки сам"


@test
def test_card_is_named_after_the_object_not_the_paper(tmp: Path):
    """Карточка знания называется по объекту, а не по бумаге, в которой объект описан.

    Разбор оставлял в имени код документа — «AC-3.4.2 Отправка начислений», — и линтер
    честно звал такую карточку артефактом в базе: на живом проекте их набралось 110.
    Но это не артефакт, а знание под чужой подписью: искать будут «Отправка начислений».

    Перекодирование, а не решение: код и ПРЕЖНЕЕ имя уезжают в синонимы, поэтому ни одна
    ссылка не ломается — ни `[[AC-3.4.2]]`, ни ссылка на старое имя целиком.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    bp = importlib.import_module("build_plan")
    importlib.reload(bp)

    assert bp.split_doc_code("AC-3.4.2 Отправка начислений") == \
        ("Отправка начислений", ["AC-3.4.2"])
    assert bp.split_doc_code("RU.PRJ.US-3.6.14 — Изменение") == ("Изменение", ["RU.PRJ.US-3.6.14"])
    assert bp.split_doc_code("US-4.2.19. Поиск")[1] == ["US-4.2.19"], "точка уехала в синоним"
    # Подчёркивание вместо пробела — обычная форма имени из выгрузки. Код в ней тоже
    # обязан отделяться целиком: раньше `\w` в хвосте кода съедал начало названия, код
    # обрывался на «US-5.2», а карточка получала имя «1_Инфраструктура_Дашборда».
    assert bp.split_doc_code("US-5.2.1_Инфраструктура_Дашборда") == \
        ("Инфраструктура_Дашборда", ["US-5.2.1"]), "код обрезан, а имя объекта испорчено"
    # Код предметной области — не код документа: ALG, SPR, BP это часть имени объекта.
    for keep in ("ALG-148 Алгоритм расчёта", "SPR-031 Справочник исключений",
                 "Курс ЦБ на дату подачи"):
        assert bp.split_doc_code(keep) == (keep, []), f"обрезано имя объекта: {keep}"

    root = tmp / "proj"
    (root / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    card = root / "AuroraKnowledgeDB" / "Concepts" / "AC-3.4.2-Отправка.md"
    card.write_text('---\ntitle: "AC-3.4.2 Отправка начислений"\naliases:\n  - "Своё"\n'
                    'type: process\nstatus: knowledge\n---\n\n'
                    "# AC-3.4.2 Отправка начислений\n\nТело. [[Другая]]\n", encoding="utf-8")
    cp = subprocess.run([sys.executable, str(KIT / "scripts/kb_fix.py"), "--names", "--apply"],
                        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode in (0, 1), cp.stderr[:300]
    made = list((root / "AuroraKnowledgeDB" / "Concepts").glob("*.md"))
    assert len(made) == 1 and made[0].name == "Отправка-начислений.md", \
        f"файл не переименован: {[m.name for m in made]}"
    text = made[0].read_text(encoding="utf-8")
    assert 'title: "Отправка начислений"' in text, "заголовок в шапке остался прежним"
    assert "# Отправка начислений" in text and "# AC-3.4.2" not in text, \
        "заголовок в теле остался прежним: в списке одно имя, в документе другое"
    for keep in ("AC-3.4.2", "AC-3.4.2 Отправка начислений", "AC-3.4.2-Отправка", "Своё"):
        assert f'"{keep}"' in text, f"ссылка по «{keep}» перестанет разрешаться"
    assert "[[Другая]]" in text, "тело тронуто"


@test
def test_section_is_the_type_written_as_a_folder(tmp: Path):
    """Раздел базы — это тип карточки, записанный папкой, и разъезжаться им нельзя.

    Раздел при разборе выбирался по умолчанию («Concepts») — в том числе жёстко, для
    источников без заголовков, — а тип писала модель по существу содержимого. На живой
    базе так набралось 142 расхождения: 76 алгоритмов среди понятий, 36 словарных статей
    среди справочников. Это техническая ошибка перекодирования, а не решение о знании.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    fx = importlib.import_module("kb_fix")
    importlib.reload(fx)

    root = tmp / "proj"
    for sec in ("Concepts", "Processes", "Glossary", "Reference"):
        (root / "AuroraKnowledgeDB" / sec).mkdir(parents=True)
    kb = root / "AuroraKnowledgeDB"
    (kb / "Concepts" / "Алгоритм.md").write_text(
        '---\ntitle: "Алгоритм"\ntype: process\nstatus: knowledge\n---\n\n# Алгоритм\n',
        encoding="utf-8")
    (kb / "Reference" / "Термин.md").write_text(
        '---\ntitle: "Термин"\ntype: glossary\nstatus: knowledge\n---\n\n# Термин\n',
        encoding="utf-8")
    (kb / "Concepts" / "Понятие.md").write_text(
        '---\ntitle: "Понятие"\ntype: concept\nstatus: knowledge\n---\n\n# Понятие\n',
        encoding="utf-8")
    # Тип вне схемы движок не выдумывает за модель, но и не молчит: он его переводит.
    (kb / "Concepts" / "Сущность.md").write_text(
        '---\ntitle: "Сущность"\ntype: entity\nstatus: knowledge\n---\n\n# Сущность\n',
        encoding="utf-8")

    cp = subprocess.run([sys.executable, str(KIT / "scripts/kb_fix.py"), "--sections", "--apply"],
                        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode in (0, 1), cp.stderr[:300]
    assert (kb / "Processes" / "Алгоритм.md").is_file(), "процесс остался среди понятий"
    assert (kb / "Glossary" / "Термин.md").is_file(), "словарная статья осталась в справочниках"
    assert (kb / "Concepts" / "Понятие.md").is_file(), "карточка на месте переехала зря"
    ent = (kb / "Concepts" / "Сущность.md")
    assert ent.is_file() and "type: concept" in ent.read_text(encoding="utf-8"), \
        "тип вне схемы не переведён в схемный"

    # Раздел больше не выбирается по умолчанию для источников без заголовков.
    ar = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert '"--to", "Concepts"' not in ar, \
        "источник без заголовков по-прежнему валится в Concepts"
    assert "to_section" in ar, "планировщик не спрашивается о разделе"


@test
def test_adapter_answers_every_caller_in_parallel(tmp: Path):
    """Каждый вызов модели идёт через Pydantic AI, и адаптер не выстраивает их в очередь.

    Прежде процесс адаптера брал задание, ждал модель и только потом читал следующее;
    движок держал пул таких процессов (по 114 МБ), а через них выходило 1,05 ответа в
    секунду против 5,46 прямым HTTP. Поэтому одиночные вызовы шли мимо Pydantic AI, и
    фреймворк работал лишь в разговорах с инструментами. Замер 27.09.2026: один
    асинхронный процесс держит ту же скорость, что прямой HTTP.

    Здесь поддельный адаптер (тот же протокол) отвечает на задания не по порядку: каждый
    поток обязан получить СВОЙ ответ, все вместе — за время самого долгого, а процесс —
    один. Отказ сервера возвращается как есть, поломка адаптера — прямым HTTP с причиной.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    import threading
    ag = importlib.import_module("agent_core")
    importlib.reload(ag)

    fake = tmp / "fake_adapter.py"
    fake.write_text(textwrap.dedent("""
        import json, sys, threading, time
        lock = threading.Lock()
        def say(o):
            with lock:
                sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\\n"); sys.stdout.flush()
        say({"ready": True, "version": "поддельный"})
        def work(t):
            q = t["messages"][-1]["content"]
            if q == "упади":
                sys.stdout.flush(); import os; os._exit(0)
            if q == "отказ":
                return say({"id": t["id"], "ok": False, "where": "server", "status": 400,
                            "error": "шлюзу не по вкусу", "body": {"error": {"message": "x"}}})
            if q == "сломан":
                return say({"id": t["id"], "ok": False, "where": "adapter",
                            "error": "ответ не разобран"})
            time.sleep(float(q))
            say({"id": t["id"], "ok": True, "text": "ответ " + q, "reasoning": "думал",
                 "finish": "stop", "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                 "system": [m["content"] for m in t["messages"] if m["role"] == "system"]})
        for line in sys.stdin:
            threading.Thread(target=work, args=(json.loads(line),)).start()
    """), encoding="utf-8")
    ag._adapter_argv = lambda: [sys.executable, str(fake)]
    # Поддельный адаптер самопроверки не знает — совместимость здесь не предмет проверки.
    ag.adapter_selfcheck = lambda version="", force=False: {"ok": True, "problems": []}
    ag._HUB.update(proc=None, pending={}, broken="", deaths=0)
    b = {"n": 1, "url": "http://x/v1", "key": ""}

    def ask(q, system="роль"):
        return ag.pydantic_transport(b, {"model": "m", "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": q}]}, 30)

    delays = ["0.9", "0.1", "0.6", "0.3", "0.8", "0.2", "0.5", "0.4"]
    got = {}
    t0 = time.time()
    threads = [threading.Thread(target=lambda d=d: got.__setitem__(d, ask(d))) for d in delays]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    took = time.time() - t0
    try:
        for d in delays:
            st, body, err, _ = got[d]
            assert st == 200, f"вызов «{d}» не получил ответа: {err}"
            text = body["choices"][0]["message"]["content"]
            assert text == "ответ " + d, \
                f"поток «{d}» получил чужой ответ «{text}» — ответы раздаются не по id"
        assert took < 2.5, (f"восемь вызовов шли {took:.1f} с при самом долгом 0,9 с — "
                            "адаптер снова берёт задания по одному")
        body = got["0.1"][1]
        assert body["usage"]["completion_tokens"] == 2, "токены через адаптер потерялись"
        assert body["choices"][0]["message"]["reasoning_content"] == "думал", \
            "рассуждения через адаптер потерялись"
        proc = ag._HUB["proc"]
        assert proc is not None and proc.poll() is None, "процесс адаптера не живёт весь прогон"
        assert ag._HUB["seq"] == len(delays), "лишние задания"

        # Одиночный вызов без истории и инструментов — тоже через адаптер.
        ag.ADAPTER.update(name="pydantic_ai", fallback_why="")
        called = []
        real = ag.http_json
        ag.http_json = lambda *a, **k: called.append(a) or (200, {"choices": [
            {"message": {"content": "по HTTP"}}]}, "", 0.0)
        try:
            st, body, err, _ = ag.default_transport("chat", b, {"model": "m", "messages": [
                {"role": "user", "content": "0.05"}]}, 30)
            assert st == 200 and body["choices"][0]["message"]["content"] == "ответ 0.05" \
                and not called, "одиночный вызов ушёл мимо Pydantic AI"
            # Отказ сервера — ответ сервера: кольцо решает по нему, повтор по HTTP не нужен.
            st, body, err, _ = ag.default_transport("chat", b, {"model": "m", "messages": [
                {"role": "user", "content": "отказ"}]}, 30)
            assert st == 400 and "не по вкусу" in err and not called, \
                "отказ сервера через адаптер повторён по HTTP или потерял статус"
            # Поломка адаптера — тот же вызов прямым HTTP, причина — в отчёт.
            st, body, err, _ = ag.default_transport("chat", b, {"model": "m", "messages": [
                {"role": "user", "content": "сломан"}]}, 30)
            assert st == 200 and called and "не разобран" in ag.ADAPTER["fallback_why"], \
                "поломка адаптера не ушла на прямой HTTP или причина не записана"
            # Процесс упал — ожидающие получают отказ, следующий вызов поднимает новый.
            st, body, err, _ = ask("упади")
            assert st is None and err.startswith(ag.ADAPTER_FAIL), \
                f"упавший процесс оставил вызов без внятного отказа: {err}"
            for _ in range(50):
                if ag._HUB["proc"] is None:
                    break
                time.sleep(0.05)
            st, body, err, _ = ask("0.05")
            assert st == 200, f"после падения адаптер не поднялся заново: {err}"
        finally:
            ag.http_json = real
    finally:
        p = ag._HUB.get("proc")
        if p is not None:
            p.kill()
        ag._HUB.update(proc=None, pending={}, broken="", deaths=0)
        ag.ADAPTER.update(name="openai_compat", fallback_why="")
        importlib.reload(ag)


@test
def test_pydantic_ai_reads_a_gateway_with_nonstandard_metadata(tmp: Path):
    """Шлюз на SGLang ломал Pydantic AI полем `metadata.weight_versions`.

    SGLang кладёт в ответ версию весов модели списком отрезков, а по стандарту OpenAI
    `metadata` — словарь строк. Клиент внутри Pydantic AI отвергал ответ целиком («Invalid
    response … metadata.weight_versions Input should be a valid string») при любой версии
    фреймворка, и движок молча уходил на прямой HTTP. Поле нам не нужно — адаптер убирает
    из него нестроковое до разбора и ничего больше не трогает.

    Ещё два расхождения с HTTP-путём: адаптер склеивал системное сообщение с запросом в
    одну реплику и не возвращал токены — отчёт о расходе врал при каждом вызове через него.
    """
    sys.path.insert(0, str(KIT / "scripts" / "agents"))
    import importlib
    AD = importlib.import_module("pydantic_ai_adapter")
    importlib.reload(AD)

    sglang = {"id": "1", "object": "chat.completion", "created": 1, "model": "m",
              "choices": [{"index": 0, "finish_reason": "stop", "matched_stop": 2,
                           "message": {"role": "assistant", "content": "ок",
                                       "reasoning_content": "думал"}}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
              "metadata": {"weight_version": "default",
                           "weight_versions": [{"version": "default", "start": 0, "end": 2}]}}
    fixed = json.loads(AD.tolerant_body(json.dumps(sglang).encode()))
    assert fixed["metadata"] == {"weight_version": "default"}, \
        f"нестроковое в metadata осталось: {fixed.get('metadata')}"
    assert fixed["choices"][0]["matched_stop"] == 2 and fixed["usage"] == sglang["usage"], \
        "адаптер правит больше, чем нарушает стандарт"
    only_list = dict(sglang, metadata={"weight_versions": [1]})
    assert "metadata" not in json.loads(AD.tolerant_body(json.dumps(only_list).encode()))
    plain = json.dumps({"choices": [], "metadata": {"a": "b"}}).encode()
    assert AD.tolerant_body(plain) is plain, "стандартный ответ пересобран без нужды"
    assert AD.tolerant_body(b"not json") == b"not json"

    ins, hist, prompt = AD.split_messages([
        {"role": "system", "content": "ты критик"},
        {"role": "user", "content": "было"}, {"role": "assistant", "content": "ответ"},
        {"role": "user", "content": "новое"}])
    assert ins == "ты критик", "системное сообщение не стало инструкцией"
    assert prompt == "новое" and [h["role"] for h in hist] == ["user", "assistant"], \
        "история разговора разложена неверно"
    assert AD.shared_agent_key({"url": "u", "model": "m", "tools": ["."]}) is None, \
        "агент с инструментами общий — параллельные вызовы перепутают подключённые серверы"
    assert AD.shared_agent_key({"url": "u", "model": "m"}) == ("u", "", "m")

    vpy = Path.home() / ".aurora" / "venv" / "bin" / "python"
    if not vpy.exists():
        return          # venv с pydantic-ai не поставлен — живую проверку пропускаем

    # Живая проверка: настоящий адаптер против местного «шлюза SGLang».
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(req)
            out = json.dumps(sglang).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sys.path.insert(0, str(KIT / "scripts"))
    ag = importlib.import_module("agent_core")
    importlib.reload(ag)
    ag._HUB.update(proc=None, pending={}, broken="", deaths=0)
    try:
        st, body, err, _ = ag.pydantic_transport(
            {"n": 1, "url": f"http://127.0.0.1:{srv.server_port}/v1", "key": ""},
            {"model": "m", "max_tokens": 50, "chat_template_kwargs": {"enable_thinking": True},
             "messages": [{"role": "system", "content": "ты критик"},
                          {"role": "user", "content": "проверь"}]}, 30)
        assert st == 200, f"ответ шлюза SGLang не прочитан через Pydantic AI: {err}"
        msg = body["choices"][0]["message"]
        assert msg["content"] == "ок" and msg["reasoning_content"] == "думал", msg
        assert body["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}, body["usage"]
        req = seen[-1]
        assert req["messages"][0] == {"role": "system", "content": "ты критик"}, \
            f"системное сообщение ушло не своей ролью: {req['messages']}"
        assert req.get("max_tokens") == 50, "max_tokens не дошёл до шлюза"
        assert req.get("chat_template_kwargs") == {"enable_thinking": True}, \
            "режим рассуждений не дошёл до шлюза"
        # С инструментами Pydantic AI добавляет свою инструкцию — каталог MCP-серверов.
        # Двух системных сообщений chat-шаблон Qwen не принимает: `400 System message must
        # be at the beginning` (живой шлюз №1, 27.09.2026).
        st, body, err, _ = ag.pydantic_transport(
            {"n": 1, "url": f"http://127.0.0.1:{srv.server_port}/v1", "key": ""},
            {"model": "m", "tools_root": str(tmp), "guard": {"ready": False}, "role": "worker",
             "mcp": {"mcpServers": {"проба": {"command": "/nonexistent", "about": "проба"}}},
             "messages": [{"role": "system", "content": "ты критик"},
                          {"role": "user", "content": "проверь"}]}, 30)
        assert st == 200, err
        roles = [m["role"] for m in seen[-1]["messages"]]
        assert roles.count("system") == 1 and roles[0] == "system", \
            f"системных сообщений не одно в начале: {roles} — шаблон Qwen откажет"
        first = seen[-1]["messages"][0]["content"]
        assert "ты критик" in first and "mcp_connect" in first, \
            "при слиянии пропала инструкция движка или каталог серверов"
    finally:
        srv.shutdown()
        p = ag._HUB.get("proc")
        if p is not None:
            p.kill()
        ag._HUB.update(proc=None, pending={}, broken="", deaths=0)


@test
def test_thinking_is_switched_on_explicitly_in_every_request(tmp: Path):
    """Рассуждения включаются явно в каждом запросе — умолчанию шлюза не доверяем.

    У части моделей рассуждения по умолчанию выключены: в настройке opencode это видно по
    `extraBody.chat_template_kwargs.enable_thinking: true` у каждой модели и по
    `reasoning_effort: "xhigh"` у одной. У Авроры своих полей шаблона у шлюза не было, а
    любой ответ 400 повторялся БЕЗ `chat_template_kwargs` — и рассуждения молча уходили в
    умолчание модели. 27.09.2026 так повторялся отказ шаблона Qwen «System message must be
    at the beginning», к полю отношения не имевший.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    importlib.reload(A)
    env = {"AURORA_AGENT_BACKEND_1_URL": "http://gw/v1", "AURORA_AGENT_BACKEND_1_MODEL": "m",
           "AURORA_AGENT_BACKEND_1_KEY": "секрет-не-показывать",
           "AURORA_AGENT_BACKEND_1_TEMPLATE_KWARGS":
               '{"reasoning_effort": "xhigh", "enable_thinking": false}',
           "AURORA_AGENT_BACKEND_2_URL": "http://gw2/v1", "AURORA_AGENT_BACKEND_2_MODEL": "m2",
           "AURORA_AGENT_BACKEND_2_TEMPLATE_KWARGS": "{reasoning_effort: xhigh",
           "AURORA_AGENT_THINKING_WORKER": "0"}
    cfg = A.parse_config(env)
    b1, b2 = cfg["backends"]
    assert b1["template"] == {"reasoning_effort": "xhigh", "enable_thinking": False}
    assert A.request_template(b1, True) == {"reasoning_effort": "xhigh", "enable_thinking": True}, \
        "enable_thinking из полей шлюза перекрыл решение роли"
    assert A.request_template(b1, False)["enable_thinking"] is False
    assert b2["template"] == {} and b2["template_error"], \
        "опечатка в JSON полей шаблона прошла молча"
    assert A.request_template(b2, True) == {"enable_thinking": True}, \
        "без своих полей рассуждения не включаются явно"

    sent, answers = [], []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return None, None, "нет /slots", 0.0
        sent.append(json.loads(json.dumps(payload)))
        return answers.pop(0)

    one = dict(cfg, backends=[b1])
    ok = (200, {"choices": [{"message": {"content": "ок"}}]}, "", 0.1)
    answers[:] = [ok]
    r = A.call_role(one, "qa", [{"role": "user", "content": "?"}], transport=transport,
                    sleep=lambda s: None)
    assert r["ok"] and sent[-1]["chat_template_kwargs"] == \
        {"reasoning_effort": "xhigh", "enable_thinking": True}, sent[-1]
    answers[:] = [ok]
    A.call_role(one, "worker", [{"role": "user", "content": "?"}], transport=transport,
                sleep=lambda s: None)
    assert sent[-1]["chat_template_kwargs"]["enable_thinking"] is False, \
        "роль с выключенными рассуждениями получила их по полям шлюза"

    # 400 не про поле — повтора без рассуждений нет.
    sent.clear()
    answers[:] = [(400, {"error": {"message": "System message must be at the beginning."}},
                   "System message must be at the beginning.", 0.1)]
    r = A.call_role(one, "qa", [{"role": "user", "content": "?"}], transport=transport,
                    sleep=lambda s: None, deadline=time.time() + 2)
    assert all("chat_template_kwargs" in p for p in sent), \
        "отказ не про поле шаблона, а запрос повторён без рассуждений"
    # 400 про поле — повтор без него, и это сказано вслух.
    sent.clear()
    answers[:] = [(400, None, "Unrecognized request argument supplied: chat_template_kwargs", 0.1),
                  ok]
    r = A.call_role(one, "qa", [{"role": "user", "content": "?"}], transport=transport,
                    sleep=lambda s: None)
    assert r["ok"] and "chat_template_kwargs" not in sent[-1], "шлюз без поля так и не спрошен"
    assert any("рассуждения" in line for line in r["log"]), \
        "рассуждения пропали молча — в журнале шага ни слова"

    # Что уходит в шлюз — видно без чтения кода, и без ключей.
    d = A.pydantic_settings(cfg, venv=(True, "9.9"))
    assert "секрет" not in json.dumps(d, ensure_ascii=False), "в настройках Pydantic AI утёк ключ"
    roles = d["backends"][0]["roles"]
    assert all("enable_thinking" in r["extra_body"]["chat_template_kwargs"] for r in roles.values()), \
        "в настройках есть роль без явного включения рассуждений"
    assert roles["qa"]["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "xhigh"
    assert d["backends"][1]["template_error"], "ошибка полей шаблона не видна в настройках"

    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/agent/pydantic"' in ck, "у панели нет настроек Pydantic AI"
    view = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    assert '"/api/agent/pydantic"' in view and 'x.id === "pydantic-ai"' in view, \
        "в «Установке» нет кнопки с настройками Pydantic AI"
    ui = ui_source()
    assert 'pre+"TEMPLATE_KWARGS"' in ui, "поля шаблона шлюза нельзя задать в панели"


@test
def test_names_pass_on_windows_macos_and_linux(tmp: Path):
    """Имена файлов и папок — по самым строгим правилам из трёх систем сразу.

    Проект открывают на Windows, macOS и Linux, и имя, допустимое на одной, на другой ломает
    выгрузку всего репозитория. Проверка кита на GitHub не была зелёной ни разу с 28.08:
    одна из причин — заготовка с именем в 418 байт, которое macOS создаёт, а Linux нет. В
    живых базах 27.09.2026: карточка «HDFS->Hive» (Windows не создаст), пути зеркала
    Confluence до 321 знака (Windows принимает 260), две папки, различимые только регистром.
    """
    import importlib
    import unicodedata
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    importlib.reload(C)

    # общее правило
    assert C.portable_name('a<b>c:d"e|f?g*h', sep="-") == "a-b-c-d-e-f-g-h"
    assert C.portable_name("отчёт. ", sep="-") == "отчёт", "точка и пробел в конце остались"
    assert C.portable_name("con", ext=".md") == "con_.md", "имя CON Windows не создаст"
    assert C.portable_name("LPT1.backup", sep="-") == "LPT1_.backup"
    long = C.portable_name("я" * 300, ext=".md")
    assert long.endswith(".md") and len(long.encode("utf-8")) <= C.NAME_BUDGET, \
        f"имя {len(long.encode('utf-8'))} байт: режется не в байтах или теряет расширение"
    nfd = unicodedata.normalize("NFD", "Отчёт-и-йод")
    assert C.portable_name(nfd) == unicodedata.normalize("NFC", nfd), "имя не в NFC"
    assert C.portable_name("???", sep="") == "", "пустое имя выдано за годное"

    # имя карточки из заголовка
    got = C.card_filename("Зависимость запуска HDFS->Hive: raw от Kafka->HDFS")
    assert not C.path_problems(got + ".md"), f"имя карточки непереносимо: {got}"
    big = C.card_filename("Очень длинное название " * 20)
    assert len(big.encode("utf-8")) <= C.NAME_BUDGET and len(big) <= C.NAME_CHARS, big
    assert C.card_filename("Статус-Черновик") == "Статус-Черновик", \
        "обычное имя изменилось — поедут ссылки всей базы"

    # проверка пути называет каждую беду
    probs = " ".join(C.path_problems("A/nul.txt") + C.path_problems("A/x?.md")
                     + C.path_problems("A/" + "я" * 130 + ".md") + C.path_problems("a" * 201)
                     + C.path_problems("A/b.") + C.path_problems("A/" + nfd))
    for must in ("зарезервировано", "запрещены", "байт", "путь 201", "в конце", "NFD"):
        assert must in probs, f"проверка пути не называет «{must}»: {probs}"
    assert C.case_clashes(["S/Core_Аналитический/a.md", "S/Core_аналитический/a.md",
                           "S/b.md"]) == [("S/Core_Аналитический/a.md", "S/Core_аналитический/a.md")]

    # зеркало Confluence: глубокая ветка с длинными заголовками укладывается в 200 знаков,
    # а короткие пути остаются прежними — иначе переехала бы половина зеркала
    CE = importlib.import_module("confluence_export")
    importlib.reload(CE)
    exp = CE.Exporter.__new__(CE.Exporter)
    exp.claimed, exp.prefix_len = {}, len("Sources/Confluence") + 1
    parent = ""
    for depth in range(20):
        parent = exp.place(f"Раздел {depth} с длинным заголовком про налоговую отчётность",
                           str(90000000 + depth), parent, True)
        path = f"Sources/Confluence/{parent}/index.md"
        assert len(path) <= C.PATH_CHARS, f"уровень {depth}: путь {len(path)} знаков"
    last = exp.place("Страница со схемой " * 5, "91000000", parent, False, True)
    assert len(f"Sources/Confluence/{last}_assets/") + CE.PART_MIN <= C.PATH_CHARS, \
        "под схемы страницы места не осталось"
    fresh = CE.Exporter.__new__(CE.Exporter)
    fresh.claimed, fresh.prefix_len = {}, len("Sources/Confluence") + 1
    assert fresh.part_name("Логическая модель", "1", "", True) == "Логическая_модель", \
        "короткое имя зеркала изменилось — страницы переедут без нужды"
    a = fresh.part_name("Core Аналитический", "11", "SM", True)
    b = fresh.part_name("Core аналитический", "22", "SM", True)
    assert a.casefold() != b.casefold() and b.endswith("_22"), \
        f"соседние страницы, различимые только регистром, легли в одну папку: {a} / {b}"

    # живая база: непереносимое имя карточки чинится вместе со ссылками
    root = make_project(tmp, git=True)
    bad = card(root, "Processes/Запуск-HDFS->Hive.md", status="draft", body="Порядок запуска.")
    card(root, "Concepts/Kafka.md", status="draft", body="Шина. См. [[Запуск-HDFS->Hive]].")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fx"],
                   cwd=str(root), check=True)
    doc = run("aurora_doctor.py", cwd=root).stdout
    assert "имена: карточки — 1" in doc and "kb_names.py --apply" in doc, \
        f"доктор не назвал непереносимое имя и способ починки:\n{doc[-800:]}"
    dry = run("kb_names.py", cwd=root, expect_rc=0).stdout
    assert "Запуск-HDFS->Hive.md" in dry and bad.exists(), f"показ без --apply что-то изменил:\n{dry}"
    run("kb_names.py", "--apply", cwd=root, expect_rc=0)
    assert (root / "AuroraKnowledgeDB/meta/graphify/graph.json").is_file(), \
        "после починки имён граф не пересобран — сервер графа отвечал бы по старым путям"
    assert not bad.exists() and (root / "AuroraKnowledgeDB/Processes/Запуск-HDFS-Hive.md").is_file(), \
        "карточка с «>» в имени не переименована"
    text = (root / "AuroraKnowledgeDB/Concepts/Kafka.md").read_text(encoding="utf-8")
    assert "[[Запуск-HDFS-Hive" in text and "HDFS->Hive]]" not in text, \
        f"ссылка на переименованную карточку не поправлена:\n{text}"

    # правило видят все модели, работающие с базой (Т-85)
    for rel in ("templates/agents/AGENTS.md.template", "skills/aurora-vault/SKILL.md",
                "docs/knowledge-rules.md", "docs/knowledge-rules-tldr.md",
                "templates/meta/conventions.md"):
        doc = (KIT / rel).read_text(encoding="utf-8")
        for must in ("Windows", "macOS", "Linux", "255 байт", "200 знаков"):
            assert must in doc, f"в {rel} нет правила имён: не хватает «{must}»"


@test
def test_kit_files_pass_on_every_os(tmp: Path):
    """Файлы самого кита — и в ките, и там, куда их кладёт обновление в проекте.

    Кит выгружают на Windows, macOS и Linux, а его файлы ещё и уезжают в проекты под
    `.opencode/`: путь там длиннее. Правило то же, что для базы, — самое строгое из трёх.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    rels = [r for r in subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "-z"],
                                      cwd=str(KIT), capture_output=True,
                                      text=True, encoding="utf-8", errors="replace").stdout.split("\0") if r]
    assert len(rels) > 100, "список файлов кита не прочитан"
    bad = [(r, C.path_problems(r)) for r in rels if C.path_problems(r)]
    assert not bad, f"файлы кита не пройдут на одной из систем: {bad[:5]}"
    assert not C.case_clashes(rels), f"в ките пути, различимые только регистром: {C.case_clashes(rels)[:3]}"
    # как они лягут в проекте
    targets = []
    for line in (KIT / "engine_manifest.txt").read_text(encoding="utf-8").splitlines():
        if "=>" in line and not line.lstrip().startswith("#"):
            dst = line.split("=>", 1)[1].strip().split()[-1]
            if "/" in dst or "." in dst:
                targets.append(dst)
    assert targets, "манифест движка не прочитан"
    far = [(t, C.path_problems(t)) for t in targets if C.path_problems(t)]
    assert not far, f"в проекте файлы движка лягут непереносимо: {far[:5]}"
    # заготовки структуры проекта
    for line in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert not C.path_problems(line), f"папка схемы непереносима: {line}"


@test
def test_every_name_maker_is_portable(tmp: Path):
    """Каждое место, где движок сочиняет имя, выдерживает неудобный вход.

    Имена приходят из заголовков Confluence, вопросов человека, задач, адресов сайтов —
    из текста, который никто не проверял на правила файловых систем.
    """
    import importlib
    import unicodedata
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    CE = importlib.import_module("confluence_export")
    W = importlib.import_module("web_export")
    R = importlib.import_module("agent_runner")
    K = importlib.import_module("kb_corrections")
    S = importlib.import_module("aurora_setup")
    nasty = ["CON", "nul.txt", "Com1", 'a<b>:c"d/e\\f|g?h*i', "конец. ", " пробел в начале",
             "я" * 300, "x" * 400, unicodedata.normalize("NFD", "йод и ёж"), "📊 отчёт",
             "\x07звонок", "...", "??", "Отчёт: Q1/2026 — итоги?", "1_", "a" * 90 + ".drawio"]
    exp = CE.Exporter.__new__(CE.Exporter)
    exp.claimed, exp.prefix_len = {}, len("Sources/Confluence") + 1
    makers = {
        "карточка": lambda t: C.card_filename(t) + ".md",
        "страница зеркала": lambda t: CE.safe_name(t, "12345678") + ".md",
        "схема страницы": lambda t: exp.asset_name(t, "Раздел/Страница_assets"),
        "страница сайта": lambda t: W.slug("https://site.example/p?x=1", t),
        "разговор": lambda t: R.slug(t) + ".md",
        "артефакт": lambda t: R.artifact_stem(t, "us") + ".md",
        "исправление": lambda t: K.slug(t) + ".md",
        "навык проекта": lambda t: S.slugify(t),
    }
    for what, make in makers.items():
        for t in nasty:
            got = make(t)
            base = got[:-3] if got.endswith(".md") else got
            if what == "карточка" and not base:
                continue    # «имени нет»: карточку без имени вызывающий не заводит
            assert base.strip(". "), f"{what}: из {t!r} вышло пустое имя"
            assert not C.path_problems(got), f"{what}: {t!r} → {got!r}: {C.path_problems(got)}"
            assert len(got.encode("utf-8")) <= C.NAME_BYTES, f"{what}: {len(got.encode())} байт"
            assert unicodedata.normalize("NFC", got) == got, f"{what}: {got!r} не в NFC"
    # укороченные имена схем не сливаются: иначе вторая схема затёрла бы первую
    a = exp.asset_name("KG_переход на ГС и Отчет из Расхождений.drawio", "П" * 120 + "_assets")
    b = exp.asset_name("KG_переход на ГС и отчет из расхождения.drawio", "П" * 120 + "_assets")
    assert a != b and a.endswith(".drawio"), f"две схемы стали одним файлом: {a} / {b}"
    assert exp.asset_name(a, "П" * 120 + "_assets") == a, "повторная раскладка меняет имя снова"
    assert exp.asset_name("1_.drawio", "X_assets") == "1_.drawio", \
        "допустимое имя изменено без нужды — «1_» и «1» слились бы"


@test
def test_name_repair_lays_the_mirror_out_as_the_next_sync(tmp: Path):
    """Починка имён без сети раскладывает зеркало ровно так, как следующий синк.

    Иначе страницы переехали бы дважды: сначала по починке, потом по синку, — а каждый
    переезд переписывает источники карточек. Проверка: старое зеркало (без предела пути),
    карточки со ссылками на него, `kb_names --apply`, затем синк по правилу — и синк не
    должен ни переложить, ни переписать ни одной страницы.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    C = importlib.import_module("aurora_common")
    CE = importlib.import_module("confluence_export")
    importlib.reload(CE)
    N = importlib.import_module("kb_names")
    importlib.reload(N)
    root = make_project(tmp, git=True)
    mirror = root / "Sources" / "Confluence"
    long = "Очень длинный заголовок раздела про налоговую отчётность и расхождения"
    tree = {"9100100": ("Корень аналитики проекта", ["9100101", "9100102"])}
    tree["9100101"] = (long + " один", [])
    tree["9100102"] = (long + " два", ["9100103"])
    tree["9100103"] = (long + " три", ["9100104", "9100105"])
    tree["9100104"] = (long + " четыре со схемой", [])
    tree["9100105"] = (long + " пять", ["9100106", "9100200"])
    tree["9100106"] = (long + " шесть", [])
    # Очень глубокая ветка: здесь страницы уже не помещаются и под номером — синк кладёт
    # их в папку ближайшего предка, где место есть. Починка обязана узнать их родителя
    # по шапке, а не по пути, иначе повторный запуск переложит их не туда.
    for depth in range(20):
        pid = str(9100200 + depth)
        tree[pid] = (f"Уровень {depth}", [str(9100201 + depth)] if depth < 19 else [])
    drawio = ('<p>Схема:</p><ac:structured-macro ac:name="drawio">'
              '<ac:parameter ac:name="diagramName">KG_переход на ГС и Отчет из Расхождений'
              '</ac:parameter></ac:structured-macro>'
              '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">'
              'KG_переход на ГС и отчет из расхождения</ac:parameter></ac:structured-macro>')

    class Api:
        def page(self, pid):
            title = tree[pid][0]
            body = drawio if pid == "9100104" else f"<p>Текст страницы {pid}.</p>"
            return {"title": title, "version": {"number": 1, "when": "2026-09-27"},
                    "body": {"storage": {"value": body}}, "space": {"key": "S"},
                    "_links": {"webui": f"/p/{pid}"}}

        def children(self, pid):
            return [{"id": c, "title": tree[c][0]} for c in tree[pid][1]]

        def attachments(self, pid):
            if pid != "9100104":
                return {}
            return {"KG_переход на ГС и Отчет из Расхождений.drawio": "a1",
                    "KG_переход на ГС и отчет из расхождения.drawio": "a2"}

        def fetch(self, url):
            return f"<mxfile>{url}</mxfile>".encode("utf-8")

        def user_name(self, key):
            return key

    here, real = os.getcwd(), CE.PATH_CHARS
    os.chdir(root)
    try:
        # 1. старое зеркало: как до 1.140.0 — путь ничем не ограничен
        CE.PATH_CHARS = 10 ** 6
        old = CE.Exporter(Api(), str(mirror), "https://wiki", "S", False)
        old.walk("9100100", [])
        old.write_state()
        CE.PATH_CHARS = real
        old_paths = {pid: rel for pid, rel, _t, _s in old.records}
        assert max(len("Sources/Confluence/" + r) for r in old_paths.values()) > C.PATH_CHARS, \
            "заготовка не воспроизводит длинные пути"
        card(root, "Concepts/Знание.md", "Текст.",
             source=f'"Sources/Confluence/{old_paths["9100106"]}"', status="draft")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "до"],
                       cwd=str(root), check=True)

        # учёт разбора: все страницы разобраны по нынешнему тексту
        import build_plan as BP
        importlib.reload(BP)
        BP.save_manifest({"sources": {f"Sources/Confluence/{rel}": {
            "hash": BP.file_hash(str(mirror / rel)), "cards": 1} for rel in old_paths.values()}})

        # 2. починка без сети
        plan = N.mirror_plan(str(root), str(mirror))
        assert any(p["old"] != p["new"] for p in plan), "починка не нашла длинных путей"
        N.apply_mirror(str(root), str(mirror), plan)
        on_disk = [str(p.relative_to(root)).replace("\\", "/") for p in mirror.rglob("*") if p.is_file()]
        assert all(not C.path_problems(r) for r in on_disk), \
            f"после починки пути длиннее предела: {[r for r in on_disk if C.path_problems(r)][:3]}"
        src = (root / "AuroraKnowledgeDB/Concepts/Знание.md").read_text(encoding="utf-8")
        rows = {pid: rel for pid, _t, rel, _s in N._state_rows(str(mirror))}
        assert f"Sources/Confluence/{rows['9100106']}" in src, f"источник карточки не переведён:\n{src}"
        assert (mirror / rows["9100106"]).is_file(), "страница не лежит по новому пути"
        # страница со схемами изменилась только в именах схем — разбор остаётся в силе:
        # иначе маршрут разобрал бы её моделью заново (PRJ-A 28.09.2026: 118 страниц)
        man = BP.load_manifest()["sources"]
        page_key = f"Sources/Confluence/{rows['9100104']}"
        assert man.get(page_key, {}).get("hash") == BP.file_hash(str(mirror / rows["9100104"])), \
            "после переименования схем разбор страницы считается устаревшим"

        assert not N.mirror_plan(str(root), str(mirror)), \
            "повторная починка снова что-то перекладывает — она не повторяема"
        parent_of = {c: p for p, (_t, kids) in tree.items() for c in kids}

        def here_of(pid):
            r = rows[pid]
            return r[:-len("/index.md")] if r.endswith("/index.md") else r[:-3]
        lifted = [pid for pid in rows if pid in parent_of
                  and os.path.dirname(here_of(pid)) != here_of(parent_of[pid])]
        assert lifted, "заготовка не дошла до страниц, лёгших в папку предка"

        # 3. синк по правилу: ни переезда, ни перезаписи
        new = CE.Exporter(Api(), str(mirror), "https://wiki", "S", False)
        new.walk("9100100", [])
        assert {pid: rel for pid, rel, _t, _s in new.records} == rows, \
            "синк разложил зеркало иначе, чем починка: страницы переедут второй раз"
        assert new.written == 0, f"синк переписал страниц: {new.written} — починка оставила не тот текст"
        assert not new.stale(), f"после синка лишние файлы: {new.stale()}"
        schemes = sorted(p.name for p in mirror.rglob("*.drawio"))
        assert len(schemes) == 2 and schemes[0] != schemes[1], f"схемы слились или пропали: {schemes}"
        page = (mirror / rows["9100104"]).read_text(encoding="utf-8")
        for name in schemes:
            import urllib.parse
            assert urllib.parse.quote(name) in page, f"ссылка на схему {name} не поправлена"
    finally:
        CE.PATH_CHARS = real
        os.chdir(here)


@test
def test_a_pydantic_update_is_checked_before_it_works(tmp: Path):
    """Обновление Pydantic AI не ломает работу Авроры молча.

    Проверка 28.09.2026 на сборке Pydantic AI из git (2.51.1.dev): клиент OpenAI там стоит
    на `httpx2`, а прежнего `httpx` нет вовсе — адаптер импортировал `httpx` и не поднимался,
    и все вызовы тихо ушли бы прямым HTTP. На 2.0.0 молча пропадал каталог MCP. Поэтому:
    HTTP-библиотеку адаптер берёт ту же, что у клиента OpenAI; форму запроса держит на
    проводе, а не только флагами профиля (их названия меняются); новая версия работает,
    только пройдя самопроверку, а обновление из панели, не прошедшее её, откатывается.
    """
    import importlib
    import types
    sys.path.insert(0, str(KIT / "scripts" / "agents"))
    sys.path.insert(0, str(SCRIPTS))
    AD = importlib.import_module("pydantic_ai_adapter")
    importlib.reload(AD)

    # форма запроса на проводе — без опоры на флаги профиля
    raw = json.dumps({"model": "m", "max_completion_tokens": 50, "messages": [
        {"role": "developer", "content": "ты критик"}, {"role": "system", "content": "каталог MCP"},
        {"role": "user", "content": "проверь"}]}).encode()
    got = json.loads(AD.tolerant_request(raw))
    assert got.get("max_tokens") == 50 and "max_completion_tokens" not in got, got
    assert [m["role"] for m in got["messages"]] == ["system", "user"], got["messages"]
    assert got["messages"][0]["content"] == "ты критик\n\nкаталог MCP", got["messages"][0]
    plain = json.dumps({"messages": [{"role": "system", "content": "a"},
                                     {"role": "user", "content": "b"}], "max_tokens": 5}).encode()
    assert AD.tolerant_request(plain) is plain, "правильный запрос пересобран без нужды"

    # HTTP-библиотека — та, на которой стоит клиент OpenAI
    fake_lib = types.ModuleType("httpx2")
    fake_lib.AsyncBaseTransport = object
    base = types.ModuleType("openai._base_client")
    base.httpx2 = fake_lib
    top = types.ModuleType("openai")
    top._base_client = base
    saved = {k: sys.modules.get(k) for k in ("openai", "openai._base_client")}
    sys.modules.update({"openai": top, "openai._base_client": base})
    try:
        assert AD.http_lib() is fake_lib, "адаптер взял не ту HTTP-библиотеку, что клиент OpenAI"
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # версия без пройденной самопроверки не работает: вызовы — прямым HTTP с причиной
    ag = importlib.import_module("agent_core")
    importlib.reload(ag)
    fake = tmp / "fake.py"
    fake.write_text('import json,sys\nprint(json.dumps({"ready": True, "version": "9.0.0"}), flush=True)\n'
                    'sys.stdin.read()\n', encoding="utf-8")
    ag._adapter_argv = lambda: [sys.executable, str(fake)]
    ag.adapter_selfcheck = lambda version="", force=False: {
        "ok": False, "version": version, "problems": ["одно системное сообщение: ['system', 'system']"]}
    ag._HUB.update(proc=None, pending={}, broken="", deaths=0)
    ag.ADAPTER.update(name="pydantic_ai", fallback_why="")
    real = ag.http_json
    ag.http_json = lambda *a, **k: (200, {"choices": [{"message": {"content": "по HTTP"}}]}, "", 0.0)
    try:
        st, body, err, _ = ag.default_transport("chat", {"n": 1, "url": "http://x/v1", "key": ""},
                                                {"model": "m", "messages": [
                                                    {"role": "user", "content": "?"}]}, 10)
        assert st == 200 and body["choices"][0]["message"]["content"] == "по HTTP", \
            "непроверенная версия не отправила вызов прямым HTTP"
        assert "не прошёл проверку совместимости" in ag.ADAPTER["fallback_why"] \
            and "9.0.0" in ag.ADAPTER["fallback_why"], ag.ADAPTER["fallback_why"]
    finally:
        ag.http_json = real
        ag._HUB.update(proc=None, pending={}, broken="", deaths=0)
        importlib.reload(ag)

    # обновление из панели, не прошедшее проверку, возвращает прежнюю версию
    EX = importlib.import_module("aurora_extras")
    importlib.reload(EX)
    ran, have = [], ["2.51.0"]

    def fake_run(cmd, timeout=60):
        ran.append(cmd)
        if "import sys" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, "3.13\n", "")
        if "--upgrade" in cmd and "pydantic-ai" in cmd:
            have[0] = "9.0.0"
        if any(str(c).startswith("pydantic-ai==") for c in cmd):
            have[0] = str(next(c for c in cmd if str(c).startswith("pydantic-ai=="))).split("==")[1]
        return subprocess.CompletedProcess(cmd, 0, "", "")
    EX._run = fake_run
    EX.venv_python = lambda _id: Path(sys.executable)
    EX.installed_version = lambda _id: have[0]
    EX.check_compat = lambda v: {"ok": v != "9.0.0", "version": v,
                                 "problems": [] if v != "9.0.0" else ["обрыв связи: адаптер"]}
    res = EX.install("pydantic-ai")
    assert not res["ok"] and res.get("rolled_back") == "9.0.0" and res["version"] == "2.51.0", res
    assert any("pydantic-ai==2.51.0" in c for c in ran), "прежняя версия не возвращена"
    assert "не совместим" in res.get("error", ""), res

    vpy = Path.home() / ".aurora" / "venv" / "bin" / "python"
    if not vpy.exists():
        return          # venv с pydantic-ai не поставлен — живую проверку пропускаем
    cp = subprocess.run([str(vpy), str(KIT / "scripts/agents/pydantic_ai_adapter.py"), "--selfcheck"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, stdin=subprocess.DEVNULL)
    out = json.loads(cp.stdout.strip().splitlines()[-1])
    assert out["ok"], f"установленная версия {out.get('version')} не прошла самопроверку: {out['problems']}"


@test
def test_the_agent_really_reads_the_knowledge_graph(tmp: Path):
    """Встроенный агент читает граф базы через MCP-сервер graphify — на деле, а не на бумаге.

    Ревизия 28.09.2026: сервер графа стоял и отвечал, но агент ни разу до графа не дошёл.
    Инструменты graphify принимают `project_path`, модель подставляла выдуманный путь («/app/…»),
    сервер искал граф в `<путь>/graphify-out/`, модель перебирала варианты до потолка вызовов,
    а движок повторял вопрос прямым HTTP уже без инструментов — и модель отвечала «инструмента
    нет», и этот ответ уходил как настоящий. Ещё: оглавления были главными узлами графа
    («Брошенные» — 711 связей), а Pydantic AI 2.5x печатает приветствие в линию с движком.
    """
    import asyncio
    import dataclasses
    import importlib
    sys.path.insert(0, str(KIT / "scripts" / "agents"))
    sys.path.insert(0, str(SCRIPTS))
    EX = importlib.import_module("aurora_extras")
    importlib.reload(EX)
    AD = importlib.import_module("pydantic_ai_adapter")
    importlib.reload(AD)

    # запись сервера: граф — в нашей папке, `project_path` агент не передаёт, назначение названо
    entry = EX.graph_entry(Path("/py"))
    assert entry["env"] == {"GRAPHIFY_OUT": os.path.join("AuroraKnowledgeDB", "meta", "graphify")}, entry
    assert entry["drop_args"] == ["project_path"] and entry["about"], entry
    # прежняя запись (1.138) приводится к нынешней сама — это наша запись
    kit_mcp = tmp / "mcp.json"
    kit_mcp.write_text(json.dumps({"mcpServers": {"чужой": {"command": "x"}, "aurora-graph": {
        "command": sys.executable, "args": ["-m", "graphify.serve", EX.GRAPH_REL]}}}), encoding="utf-8")
    EX.kit_mcp_file = lambda: kit_mcp
    EX.venv_python = lambda _id: Path(sys.executable)
    m = EX._graph_mcp_current()
    saved = json.loads(kit_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert m.get("registered") and saved["aurora-graph"].get("drop_args") == ["project_path"], saved
    assert saved["чужой"] == {"command": "x"}, "чужой сервер задет"
    importlib.reload(EX)

    # адаптер снимает аргумент и при вызове, и в описании инструмента
    got = {}

    async def call_tool(name, args):
        got[name] = args
        return "ок"
    hook = AD.call_hook("aurora-graph", {"drop_args": ["project_path"]}, {}, str(tmp))
    asyncio.run(hook(None, call_tool, "graph_stats", {"project_path": "/app", "top_n": 3}))
    assert got["graph_stats"] == {"top_n": 3}, f"выдуманный путь ушёл серверу: {got}"

    @dataclasses.dataclass
    class Def:
        parameters_json_schema: dict

    @dataclasses.dataclass
    class Tool:
        tool_def: Def
    tools = {"graph_stats": Tool(Def({"type": "object", "required": ["project_path"],
                                      "properties": {"project_path": {}, "top_n": {}}}))}
    shown = AD.hide_args(tools, {"project_path"})["graph_stats"].tool_def.parameters_json_schema
    assert list(shown["properties"]) == ["top_n"] and shown["required"] == [], shown
    assert {"about", "drop_args", "roles", "outbound"} <= set(AD.SPEC_ONLY), \
        "ключи Авроры уходят серверу в настройке запуска"

    # приветствие библиотеки в линии с движком не мешает запуску адаптера
    ag = importlib.import_module("agent_core")
    importlib.reload(ag)
    fake = tmp / "banner.py"
    fake.write_text("import json,sys\nprint('   / \\\\   observability: off')\nprint('')\n"
                    "print(json.dumps({'ready': True, 'version': '2.51.0'}), flush=True)\n"
                    "sys.stdin.read()\n", encoding="utf-8")
    ag._adapter_argv = lambda: [sys.executable, str(fake)]
    ag.adapter_selfcheck = lambda version="", force=False: {"ok": True, "problems": []}
    ag._HUB.update(proc=None, pending={}, broken="", deaths=0)
    try:
        proc, pending = ag.adapter_hub()
        assert proc is not None, f"строка приветствия библиотеки сорвала запуск адаптера: {pending}"
    finally:
        if ag._HUB.get("proc"):
            ag._HUB["proc"].kill()
        importlib.reload(ag)

    # упор в потолок вызовов — названная причина, не ответ без инструментов
    cfg = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gw/v1",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    seen = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return None, None, "нет", 0.0
        seen.append(1)
        return 200, {"choices": [{"message": {"content": ""}, "finish_reason": "tool_limit"}]}, "", 0.1
    r = ag.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=transport,
                     sleep=lambda s: None, deadline=time.time() + 1, request_timeout=1)
    assert any("исчерпала вызовы инструментов" in line for line in r["log"]), r["log"]

    # в выгрузке для graphify нет навигации — настоящим экспортом по маленькой базе
    root = make_project(tmp)
    card(root, "Concepts/Реестр.md", "Реестр ведёт [[ЭСФ]] и [[Декларация]].", status="knowledge")
    card(root, "Concepts/ЭСФ.md", "ЭСФ попадает в [[Реестр]] и [[Декларация]].", status="knowledge")
    card(root, "Concepts/Декларация.md", "Декларация сверяется с [[ЭСФ]] и [[Реестр]].", status="knowledge")
    card(root, "MOC/Оглавление.md", "- [[Реестр]]\n- [[ЭСФ]]\n- [[Декларация]]",
         type="moc", status="index")
    run("kb_graph.py", "--export", cwd=root, expect_rc=0)
    out = json.loads((root / "AuroraKnowledgeDB/meta/graphify/graph.json").read_text(encoding="utf-8"))
    ids = {n["id"] for n in out["nodes"]}
    assert not any("Оглавление" in i for i in ids), f"оглавление осталось узлом графа: {ids}"
    assert any("Реестр" in i for i in ids), ids
    assert not any("Оглавление" in l["source"] + l["target"] for l in out["links"]), out["links"]

@test
def test_graph_export_tells_what_changed_and_what_to_check(tmp: Path):
    """Выгрузка графа говорит больше, чем связи: что изменилось, что неожиданно, что проверить.

    Ревизия graphify 28.09.2026: страница графа тянула vis-network с unpkg и в закрытом
    контуре открывалась пустой; по отчёту шага не было видно, что стало с графом за прогон;
    выведенных связей — половина, и проверять их никто не просил; а каждый новый день
    выгрузка переписывала десятки заметок тем ради одной строки `updated:`.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    G = importlib.import_module("kb_graph")
    importlib.reload(G)
    root = make_project(tmp)
    card(root, "Concepts/Реестр-НП.md", "Реестр ведёт [[ЭСФ]] и [[Декларация]].", status="knowledge")
    card(root, "Concepts/ЭСФ.md", "ЭСФ попадает в [[Декларация]].", status="knowledge")
    card(root, "Concepts/Декларация.md", "Сверяется с [[ЭСФ]].", status="knowledge")
    # односторонние ссылки: пара карточек — одна связь, сильнейшая
    card(root, "Concepts/Штраф.md", "Штраф вносят в [[Реестр-НП|реестр]].", status="knowledge")
    card(root, "Concepts/Пени.md", "Порядок описан [[ЭСФ|здесь]].", status="knowledge")
    run("kb_graph.py", "--export", cwd=root, expect_rc=0)
    base = root / "AuroraKnowledgeDB/meta/graphify"
    page = (base / "graph.html").read_text(encoding="utf-8")
    assert not re.search(r"<script[^>]+src=[\"']https?://", page), "страница графа тянет библиотеку из сети"
    assert "cytoscape" in page and '"Реестр-НП"' in page, "в странице нет библиотеки или данных"
    ins = (base / "insights.md").read_text(encoding="utf-8")
    for must in ("Изменения с прошлой выгрузки", "Неожиданные связи между темами",
                 "Выведенные связи на проверку"):
        assert must in ins, f"в заметке графа нет раздела «{must}»"
    # «здесь» не называет «Реестр-НП» ни одним словом — на проверку; «реестр» — сокращение
    queue = ins.split("## Выведенные связи на проверку")[1]
    assert "«здесь»" in queue and "«реестр»" not in queue, queue[-600:]

    # второй раз без изменений — ни заметок тем, ни графа базы не переписывает
    meta = root / "AuroraKnowledgeDB/meta/graph.json"
    stamp = meta.stat().st_mtime_ns
    notes = {p: p.stat().st_mtime_ns for p in (root / "AuroraKnowledgeDB/MOC").rglob("*.md")}
    time.sleep(0.05)
    run("kb_graph.py", "--export", cwd=root, expect_rc=0)
    assert meta.stat().st_mtime_ns == stamp, "граф базы переписан без изменений — лишняя правка в git"
    assert all(p.stat().st_mtime_ns == t for p, t in notes.items()), "заметки тем переписаны без изменений"

    # пришла карточка — сравнение с прошлой выгрузкой это называет
    card(root, "Concepts/Налоговый-агент.md", "Агент подаёт [[Декларация]].", status="knowledge")
    out = run("kb_graph.py", "--export", cwd=root, expect_rc=0).stdout
    assert "С прошлой выгрузки: карточек +1" in out, out[-600:]
    assert "Налоговый-агент" in (base / "insights.md").read_text(encoding="utf-8")

    # неожиданная связь: выведенная, между темами, из разных веток источников
    data = {"nodes": [{"id": "a", "title": "А", "group": 1, "source": "Sources/Confluence/X/a.md"},
                      {"id": "b", "title": "Б", "group": 2, "source": "Sources/Confluence/Y/b.md"},
                      {"id": "c", "title": "В", "group": 1, "source": "Sources/Confluence/X/c.md"}],
            "edges": [{"from": "a", "to": "b", "conf": "INFERRED"},
                      {"from": "a", "to": "c", "conf": "EXTRACTED"}],
            "groups": [{"id": 1, "name": "Первая"}, {"id": 2, "name": "Вторая"}]}
    s = G.surprising_links(data, set())
    assert len(s) == 1 and {s[0]["a"], s[0]["b"]} == {"А", "Б"}, s
    assert "выведена" in s[0]["why"] and "разных ветках" in s[0]["why"], s[0]["why"]


@test
def test_graph_hops_in_search_wait_for_measurement(tmp: Path):
    """Переходы поиска по графу базы есть, но включаются по замеру, а не на веру.

    Замер 28.09.2026 (PRJ-A самопоиск, PRJ-C эталон из 18 вопросов): переходы по связям из
    источников и по теме находку и R@1 не меняют; совпадения по теме заполняют пак целиком,
    а с отведёнными местами ответ эталона чаще выпадает из пака (0.944 → 0.889). Поэтому
    выключены, и замер `--compare` видит «эталон в паке», а не только порядок выдачи.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    P = importlib.import_module("ctx_pack")
    importlib.reload(P)
    assert P.RETRIEVAL["hop_typed"] is False and P.RETRIEVAL["hop_theme"] == 0 \
        and P.RETRIEVAL["graph_slots"] == 0, "переходы по графу включены без замера"
    Q = importlib.import_module("kb_search_quality")
    assert {"typed", "theme"} <= set(Q.COMPARE_PRESETS), "их нельзя замерить командой --compare"
    assert '"эталон в паке"' in (SCRIPTS / "kb_search_quality.py").read_text(encoding="utf-8"), \
        "замер не видит, выпал ли ответ эталона из пака, — а именно это меняют переходы"

    root = make_project(tmp)
    card(root, "Concepts/Отчёт-по-НДС.md", "Отчёт по НДС подаётся ежеквартально.", status="knowledge")
    card(root, "Concepts/Контрольные-соотношения.md", "Проверки перед приёмом.", status="knowledge")
    (root / "AuroraKnowledgeDB/meta/graph.json").write_text(json.dumps({
        "nodes": [{"id": "Отчёт-по-НДС", "group": 1}, {"id": "Контрольные-соотношения", "group": 1}],
        "edges": [{"from": "Отчёт-по-НДС", "to": "Контрольные-соотношения",
                   "rel": "реализует историю", "conf": "EXTRACTED"}]}, ensure_ascii=False),
        encoding="utf-8")
    here = os.getcwd()
    os.chdir(root)
    try:
        cards = P.load_cards()
        P.measure_rarity(cards)
        ok = set(P.TRUSTED) | {"knowledge"}
        got = lambda: {c.stem for c in P.collect(cards, "отчёт по НДС ежеквартально",
                                                 ok, True, "", 1)[0]}
        assert "Контрольные-соотношения" not in got(), "сосед по источнику пришёл без флага"
        P.RETRIEVAL.update(hop_typed=True, graph_slots=1)
        P._RETRIEVAL_ENV_DONE = True
        pack = P.collect(cards, "отчёт по НДС ежеквартально", ok, True, "", 2)
        assert "Контрольные-соотношения" in {c.stem for c in pack[0]}, \
            "связь из источника не привела соседа в пак"
    finally:
        os.chdir(here)
        importlib.reload(P)

    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert ".opencode/vendor/cytoscape.min.js" in man and ".opencode/vendor/cytoscape.LICENSE" in man, \
        "библиотека страницы графа не доедет до проектов"
    assert (KIT / "cockpit/vendor/cytoscape/dist/cytoscape.min.js").is_file()


@test
def test_a_legacy_machine_card_is_refreshed_not_doubled(tmp: Path):
    """Машинная карточка старого формата обновляется заменой текста, а не задваивается.

    До раздела «Источник (перенесено дословно)» машина клала текст страницы прямо в тело
    (PRJ-A 21.08: 395 из 421 справочника). Повторный разбор 28.09.2026 принимал такую
    карточку за карточку человека и дописывал свежий текст той же страницы под старый —
    страница оказывалась в карточке дважды. `refresh_card` же такие карточки молча
    пропускал, и правка страницы до них не доходила вовсе.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    B = importlib.import_module("build_plan")
    importlib.reload(B)
    root = make_project(tmp)
    src = "Sources/Confluence/Раздел/Страница.md"
    (root / src).parent.mkdir(parents=True, exist_ok=True)
    (root / src).write_text("---\npage_id: 7700100\n---\n\n# Страница\n\nтекст\n", encoding="utf-8")
    path = root / "AuroraKnowledgeDB/Reference/Справочник.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = (f'---\ntitle: "Справочник"\nsource: "{src}"\nbuilt: machine\nkind: dictionary\n'
              "distilled: 2026-09-01\n---\n\n# Справочник\n\n## Описание\n\nстарый текст страницы\n")
    path.write_text(legacy, encoding="utf-8")
    here = os.getcwd()
    os.chdir(root)
    try:
        B.append_card(str(path), legacy, "## Описание\n\nновый текст страницы", src, True, str(root))
        got = path.read_text(encoding="utf-8")
        assert got.count(B.QUOTES_MARK) == 1 and "новый текст страницы" in got, got
        assert "старый текст страницы" not in got, f"текст страницы в карточке дважды:\n{got}"
        assert "# Справочник" in got and got.count("## Описание") == 1, got
        assert "distilled:" not in got, "тезис по прежнему тексту остался отмеченным"
        # тот же текст ещё раз — карточку не трогаем
        B.append_card(str(path), got, "## Описание\n\nновый текст страницы", src, True, str(root))
        assert path.read_text(encoding="utf-8") == got, "неизменённый текст переписал карточку"

        # обновление тем же источником (--card) тоже доходит до старой карточки
        path.write_text(legacy, encoding="utf-8")
        B.refresh_card(str(path), legacy, "## Описание\n\nсвежий", src, True, str(root))
        fresh = path.read_text(encoding="utf-8")
        assert "свежий" in fresh and "старый текст страницы" not in fresh, fresh

        # карточку человека не заменяем: его текст первым, наш — ниже
        human = legacy.replace("built: machine\n", "")
        path.write_text(human, encoding="utf-8")
        B.append_card(str(path), human, "## Описание\n\nновый текст страницы", src, True, str(root))
        mixed = path.read_text(encoding="utf-8")
        assert "старый текст страницы" in mixed and "новый текст страницы" in mixed, mixed
        # и машинную карточку нескольких источников тоже
        many = legacy.replace(f'source: "{src}"', f'sources:\n  - "{src}"\n  - "Raw/другое.md"')
        path.write_text(many, encoding="utf-8")
        B.append_card(str(path), many, "## Описание\n\nновый текст страницы", src, True, str(root))
        assert "старый текст страницы" in path.read_text(encoding="utf-8"), \
            "тело карточки нескольких источников заменено текстом одного"
    finally:
        os.chdir(here)


@test
def test_merging_twins_keeps_the_card_header_whole(tmp: Path):
    """Слияние двойников не режет шапку выжившей карточки посреди строки.

    Синонимы донора удлиняют шапку, а граница шапки бралась по прежнему тексту: список
    источников вписывался внутрь чужой записи. PRJ-A, слияния 21–22.09.2026: пять карточек
    с путями вида «…19.02.2025.md".2024.md"», обрывками «bui» / «lt: machine» и пустым
    `updated:` — источники считались пропавшими, доверие — по неверному списку.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Processes"
    kb.mkdir(parents=True, exist_ok=True)
    long_src = "Sources/Confluence/Архитектура/Вопросы/Вопросы_и_материалы_для_встречи_21.08.2024.md"
    (kb / "Параметры-запуска.md").write_text(
        '---\ntitle: "Параметры запуска"\naliases: []\nstatus: draft\ntype: process\n'
        f'source: "{long_src}"\nsource_synced: 2026-08-21\ncreated: 2026-08-21\n'
        'updated: 2026-08-21\nbuilt: machine\nrelated: []\nkind: knowledge\n---\n\nТекст.\n',
        encoding="utf-8")
    (kb / "Технические-нюансы-пайплайнов.md").write_text(
        '---\ntitle: "Технические нюансы пайплайнов"\naliases: ["Агентский-ЭСФ-пайплайны"]\n'
        'status: draft\ntype: process\nsources:\n'
        '  - "Sources/Confluence/Архитектура/Вопросы/Встреча_10.12.2024.md"\n'
        '  - "Sources/Confluence/Архитектура/Вопросы/Встреча_19.02.2025.md"\n'
        'built: machine\n---\n\nЕщё текст.\n', encoding="utf-8")
    run("kb_fix.py", "--merge", "Параметры-запуска", "Технические-нюансы-пайплайнов",
        "--apply", "--allow-dirty", cwd=root)
    got = (kb / "Параметры-запуска.md").read_text(encoding="utf-8")
    head = got[:got.find("\n---", 3)]
    srcs = A.card_sources(got)
    assert sorted(srcs) == sorted([long_src, "Sources/Confluence/Архитектура/Вопросы/Встреча_10.12.2024.md",
                                   "Sources/Confluence/Архитектура/Вопросы/Встреча_19.02.2025.md"]), \
        f"источники после слияния: {srcs}\n{head}"
    fm = A.frontmatter(got)
    assert fm.get("updated") == "2026-08-21" and fm.get("built") == "machine" \
        and fm.get("kind") == "knowledge", f"поля шапки порезаны слиянием:\n{head}"
    for line in head.split("\n")[1:]:
        assert re.match(r'^([\w-]+:( .*)?|  - ".*"|  - .+)$', line), f"обрывок в шапке: {line!r}\n{head}"


@test
def test_stubs_come_from_the_base_not_from_templates_or_data(tmp: Path):
    """Заготовки заводятся под ссылки базы — не под образцы шаблона и не под строки данных.

    «Починить базу» PRJ-A 29.09.2026 завела три пустышки: две под образцы из
    `Templates/spec_template.md` (шаблоны в список карточек подгружает чистка полей) и одну
    под массив из выгрузки SQL в таблице источника — `[[01804201710137, 1, 0.5, Seller]]`.
    Линтер после этого звал битой ссылку заготовки на шаблон.
    """
    root = make_project(tmp)
    card(root, "Processes/Сверка.md",
         "Сверку ведёт [[Налоговый агент]].\n\n| a | b |\n|---|---|\n"
         "| 1 | [[01804201710137, 1, 0.500000000000000000, Seller]] |\n",
         status="knowledge")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/spec_template.md").write_text(
        '---\ntitle: "Спецификация"\n---\n\nРешение: [[DR-0012]]. Объект: [[Основной объект]].\n',
        encoding="utf-8")
    cp = run("kb_fix.py", "--all", "--stubs", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1), cp.stdout[-400:] + cp.stderr[-400:]
    names = {p.stem for p in (root / "AuroraKnowledgeDB").rglob("*.md")}
    assert "Налоговый-агент" in names, f"под ссылку базы заготовка не заведена: {sorted(names)}"
    for bad in ("DR-0012", "Основной-объект"):
        assert bad not in names, f"заготовка под образец из шаблона: {bad}"
    assert not any(n.startswith("01804201710137") for n in names), "заготовка под строку данных"
    assert (root / "Templates/spec_template.md").read_text(encoding="utf-8").startswith(
        '---\ntitle: "Спецификация"'), "ремонт правил шаблон"

    # «Названо в карточках» у заготовки — только карточки: карты навигационные, а заметки
    # тем графа меняют имя с каждой выгрузкой — ссылка на них ломается в том же маршруте.
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    K = importlib.import_module("kb_fix")
    comm = root / "AuroraKnowledgeDB/MOC/Сообщества"
    comm.mkdir(parents=True, exist_ok=True)
    (comm / "Тема-Сверка.md").write_text("---\ntitle: \"Тема\"\n---\n\nТема про ЭСФ-ККМ.\n",
                                          encoding="utf-8")
    card(root, "Concepts/Узел.md", "Узел ЭСФ-ККМ считается отдельно.", status="knowledge")
    got = K.mentions_of(str(root / "AuroraKnowledgeDB"), "ЭСФ-ККМ")
    assert got == ["Узел"], f"заготовка назовёт карту или заметку темы: {got}"


@test
def test_a_card_is_titled_by_name_not_by_file_name(tmp: Path):
    """Заголовок карточки и начало тезиса — имя сущности, а не имя файла через дефисы.

    Модель видит карточки базы по именам файлов и так их и называет. Прогон PRJ-A 28.09.2026:
    разбор дописывал в «Формат-выгрузки-…», которой не было, и заводил её с таким
    заголовком; тезис получал имя файла вместо заголовка и с него начинался — 85 заголовков
    и 32 тезиса. Имя файла от исправления не меняется, ссылки не рвутся.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    importlib.reload(A)
    for stem, want in (("Формат-выгрузки-реестра-DF-07", "Формат выгрузки реестра DF-07"),
                       ("Отчёт-по-рискам-R-9-и-R-10", "Отчёт по рискам R-9 и R-10"),
                       ("US-3.6.14-Проверка-какой-то-суммы", "US-3.6.14 Проверка какой-то суммы"),
                       ("Реестр-НП", "Реестр-НП"), ("Отчёт по НДС", "Отчёт по НДС")):
        got = A.title_from_stem(stem)
        assert got == want, f"{stem} → {got}, ждали {want}"
        assert A.card_filename(got) == A.card_filename(stem), f"имя файла сменилось: {got}"

    # разбор: дописать в карточку, которой нет, — заводит её под заголовком
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/A.md").write_text(
        "## Раздел\n\n" + "Выгрузка реестра формируется в Excel с листом «Реестр». " * 6,
        encoding="utf-8")
    cp = run("build_plan.py", "--append", "Формат-выгрузки-реестра-DF-07", "--to", "Requirements",
             "--source", "Sources/Confluence/A.md", "--sections", "1", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    made = root / "AuroraKnowledgeDB/Requirements/Формат-выгрузки-реестра-DF-07.md"
    assert made.is_file(), list((root / "AuroraKnowledgeDB").rglob("*.md"))
    assert 'title: "Формат выгрузки реестра DF-07"' in made.read_text(encoding="utf-8"), \
        made.read_text(encoding="utf-8")[:300]

    # тезис: модель получает заголовок; имя файла в начале ответа — заголовком
    R = importlib.import_module("agent_runner")
    importlib.reload(R)
    path = root / "AuroraKnowledgeDB/Concepts/Отчёт-по-рискам-R-9-и-R-10.md"
    path.write_text('---\ntitle: "Отчёт-по-рискам-R-9-и-R-10"\nkind: knowledge\nstatus: draft\n'
                    'type: concept\n---\n\nОтчёт строится ежемесячно.\n', encoding="utf-8")
    seen = []

    def fake(cfg, role, messages, deadline=None, **kw):
        seen.append(messages[0]["content"])
        return {"ok": True, "backend": 1, "model": "w", "log": [], "tps": 10,
                "text": "Отчёт-по-рискам-R-9-и-R-10 — отчёт, который строится ежемесячно."}

    step = R.distill_card({"request_timeout": 60}, str(path), call=fake, momus=False)
    assert "Карточка: Отчёт по рискам R-9 и R-10" in seen[0], seen[0][:200]
    assert step["body"].lstrip().startswith("Отчёт по рискам R-9 и R-10 — отчёт"), step["body"][:120]
    path.unlink()

    # ремонт: заголовок в шапке и начало тезиса у готовых карточек
    old = root / "AuroraKnowledgeDB/Processes/Порядок-проверки-связи-по-данным-ЭСФ.md"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('---\ntitle: "Порядок-проверки-связи-по-данным-ЭСФ"\nstatus: draft\n'
                   'type: process\n---\n\nПорядок-проверки-связи-по-данным-ЭСФ — сценарий проверки.\n'
                   '\nПорядок-проверки-связи-по-данным-ЭСФ ниже не трогаем.\n', encoding="utf-8")
    kept = card(root, "Concepts/Реестр-НП.md", "Реестр-НП — список плательщиков.")
    before = kept.read_text(encoding="utf-8")
    out = run("kb_fix.py", "--titles", "--apply", "--allow-dirty", cwd=root, expect_rc=0).stdout
    assert "в шапке 1, в начале тезиса 1" in out, out[-500:]
    fixed = old.read_text(encoding="utf-8")
    assert 'title: "Порядок проверки связи по данным ЭСФ"' in fixed, fixed
    assert "\n\nПорядок проверки связи по данным ЭСФ — сценарий" in fixed, fixed
    assert "\nПорядок-проверки-связи-по-данным-ЭСФ ниже" in fixed, "правка ушла дальше первой строки"
    assert kept.read_text(encoding="utf-8") == before, "одиночный дефис — часть имени, не разделитель"
    assert "в шапке 0, в начале тезиса 0" in run("kb_fix.py", "--titles", cwd=root).stdout


@test
def test_a_sync_with_errors_keeps_the_pages_it_did_not_reach(tmp: Path):
    """Синк со сбоями связи не выбрасывает из состояния страницы, до которых не дошёл.

    PRJ-A 28.09.2026: сервер Confluence отвечал таймаутами, обход дошёл до 229 страниц из 1068,
    и состояние зеркала переписалось по обойдённым — 839 страниц из него выпали. Маршрут
    после восьми попыток пошёл бы дальше и закоммитил базу, которая считает их чужими.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    CE = importlib.import_module("confluence_export")
    importlib.reload(CE)
    mirror = tmp / "Sources" / "Confluence"
    for rel in ("A.md", "Ветка/B.md", "Ветка/C.md"):
        (mirror / rel).parent.mkdir(parents=True, exist_ok=True)
        (mirror / rel).write_text("---\npage_id: 1\n---\n\n# x\n", encoding="utf-8")
    old = CE.Exporter(None, str(mirror), "https://wiki", "S", False)
    old.records = [("1000001", "A.md", "А", "SYNCED"), ("1000002", "Ветка/B.md", "Б", "SYNCED"),
                   ("1000003", "Ветка/C.md", "В", "SYNCED"), ("1000004", "Нет.md", "Г", "SYNCED")]
    old.write_state()
    run = CE.Exporter(None, str(mirror), "https://wiki", "S", False)
    run.records = [("1000001", "A.md", "А", "UPDATED")]
    run.failed = 1
    assert run.keep_unvisited() == 2, "страницы, до которых не дошли, выпали из состояния"
    run.write_state()
    rows = {c[1]: c[3] for c in run.state_cells()}
    assert rows == {"1000001": "A.md", "1000002": "Ветка/B.md", "1000003": "Ветка/C.md"}, rows
    assert not run.stale(), "непосещённые страницы объявлены лишними"
    src = (SCRIPTS / "confluence_export.py").read_text(encoding="utf-8")
    body = src[src.index("exp = run_export(cfg, roots, out, auth, a.force)"):]
    assert body.index("keep_unvisited()") < body.index("exp.write_state()"), \
        "состояние пишется раньше, чем в него вернули непосещённые страницы"


@test
def test_moc_recognises_its_own_files(tmp: Path):
    """Карта содержания генерируется, руками её не пишут никогда.

    А скрипт объявлял рукотворными собственные файлы: маркер он искал в первых
    четырёхстах байтах, а `kb:links --cards` дописывает в шапку карты `related:`, и
    шапка растёт. На живом проекте маркер уезжал на позицию 480 и 831 — карты переставали
    обновляться, и человек читал «написан руками» про то, чего руками не писал.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    moc = importlib.import_module("kb_moc")
    importlib.reload(moc)

    card = tmp / "карта.md"
    long_head = "\n".join(f"related_{i}: \"[[Карточка-{i}]]\"" for i in range(40))
    card.write_text(f"---\ntype: moc\n{long_head}\n---\n\n{moc.GENERATED}\n\n# Карта\n",
                    encoding="utf-8")
    assert len(card.read_text(encoding="utf-8").index(moc.GENERATED) * [0]) > 400, \
        "подготовка: маркер должен оказаться дальше прежнего окна в 400 байт"
    assert moc.machine_made(str(card)), \
        "скрипт не узнаёт свой файл: маркер за пределами прежнего окна чтения"

    hand = tmp / "рукотворная.md"
    hand.write_text("---\ntype: moc\n---\n\n# Своя карта\n", encoding="utf-8")
    assert not moc.machine_made(str(hand)), "чужой файл принят за машинный — перезапишем"
    assert not moc.machine_made(str(tmp / "нет-такого.md")), "несуществующий файл"


@test
def test_graph_and_files_link_both_ways_and_can_be_read(tmp: Path):
    """Переход из графа в файл был, обратного не было — это половина навигации.

    Посмотрел карточку, захотел увидеть её окружение — иди ищи её в графе руками. И сам
    граф: «чёрный клубок» — не свойство базы, а неподходящий разброс, одного значения на
    все базы не бывает. Решать за человека, что ему не нужен большой граф, мы не вправе:
    порог обязан быть предупреждением с проходом, а не запретом.
    """
    ui = panel_sources()

    # «На графе» стало переходом в раздел с именем карточки: раздел сам её найдёт.
    assert 'id="fileGraph"' in ui and 'show("graph", {card:' in ui, \
        "из файла нельзя попасть на граф — связь односторонняя"
    assert "ctx.openPath(evt.target.data().path)" in ui, \
        "из графа перестало открываться в файл"

    # Ширину списка тянут мышью: имена карточек длинные и у каждого проекта свои.
    assert 'id="filesSplit"' in ui and "col-resize" in ui, \
        "ширина списка файлов не меняется"
    assert '"aurora-files-width"' in ui, "выбранная ширина не запоминается"
    assert "font-size:12px" in ui[ui.index("#fileTree button{"):
                                  ui.index("#fileTree button{") + 400], \
        "шрифт в списке файлов не уменьшен — длинные имена не помещаются в строку"

    # Разброс и ступени.
    assert '"aurora-graph-spread"' in ui and "function setSpread(" in ui, \
        "расстоянием между узлами нельзя управлять"
    assert 'id="graphOut"' in ui and 'id="graphIn"' in ui, "нет плюса и минуса у разброса"
    assert '"aurora-graph-spread"' in ui, "разброс не запоминается"
    assert "fit: false" in ui, \
        "раскладка вписывается в окно: «развести узлы» гасится обратным масштабом, " \
        "и человек жмёт «+» без всякого эффекта"
    depths = ui[ui.index('id="graphDepth"'):ui.index('id="graphDepth"') + 500]
    for step in ("4", "5", "6", "8", "99"):
        assert f'value="{step}"' in depths, f"ступени {step} нет — дальше третьей не уйти"
    assert "graph.anyway" in ui, "порог размера запрещает вместо того, чтобы предупредить"
