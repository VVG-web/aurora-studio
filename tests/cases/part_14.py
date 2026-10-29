"""Проверки движка Aurora, часть 14: обзор версии 1.148.1 — навыки и MCP, дубли, запись и удаление.

Каркас и помощники — tests/harness.py. Здесь каждая проверка — на одну находку обзора.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from harness import (  # noqa: F401
    KIT,
    _cockpit_on,
    SCRIPTS,
    card,
    make_project,
    run,
    set_home,
    test,
)


@test
def test_mcp_ask_never_writes_a_conversation_into_the_base(tmp: Path):
    """`kb_ask` через MCP не оставляет в базе журнал разговора: MCP только читает.

    Инструмент звал `agent_runner.py --task ask` без `--no-journal`, а команда дописывает
    разговор в `AuroraKnowledgeDB/meta/ask/` — каждый вопрос чужого ассистента рождал файл в
    базе и в рабочем дереве git. Два противоречия сразу: документ сервера обещает «писать в
    базу через MCP нельзя», а следующая пишущая команда отказывалась идти по грязному дереву.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    seen = []
    real = M.run
    try:
        M.run = lambda project, script, args, timeout=120, **kw: seen.append((script, list(args))) or "ответ"
        assert M.call_tool(str(tmp), "kb_ask", {"question": "Что такое заявка?"}) == "ответ"
    finally:
        M.run = real
    script, args = seen[0]
    assert script == "agent_runner.py" and "ask" in args, seen
    assert "--no-journal" in args, \
        f"вопрос через MCP запишется в журнал разговоров базы: {args}"


@test
def test_mcp_survives_a_line_that_is_not_a_json_rpc_object(tmp: Path):
    """Строка, не похожая на запрос JSON-RPC, не обрывает сессию с ассистентом.

    JSON, который не объект (`[]`, `5`, `null`), и пакет запросов — массив объектов — падали
    на `msg.get` с `AttributeError`: сервер выходил, и ассистент терял базу посреди работы.
    Пакет теперь разбирается по элементам, всё остальное — ответ «Invalid Request» с
    `id: null`, как велит JSON-RPC, и следующие строки обслуживаются как обычно.
    """
    root = make_project(tmp)
    lines = ["[1, 2]", "5", "null", "[]", '"строка"',
             json.dumps([{"jsonrpc": "2.0", "id": 7, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 8, "method": "ping"}]),
             json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})]
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    assert cp.returncode == 0, f"сервер упал на кривой строке:\n{cp.stderr[-400:]}"
    got = [json.loads(l) for l in cp.stdout.splitlines() if l.strip()]
    answered = {m["id"] for m in got if m.get("id") is not None}
    assert {7, 8, 9} <= answered, f"запросы после кривых строк остались без ответа: {got}"
    bad = [m for m in got if m.get("id") is None]
    assert bad and all(m["error"]["code"] == -32600 for m in bad), \
        f"кривые строки не получили Invalid Request: {bad}"
    assert len(bad) == 6, f"ждали по ответу на 1, 2, 5, null, [] и строку ({len(bad)}): {bad}"


@test
def test_mcp_reports_a_failed_tool_as_an_error_not_as_knowledge(tmp: Path):
    """Отказ инструмента приходит с `isError: true` и внятным текстом, а не как «знание».

    Неизвестный инструмент, карточка, которой нет, пустой запрос, режим вне перечня и
    сбой скрипта отдавались обычным текстом, и ассистент читал их как ответ базы. А
    недопустимый `mode` доезжал до argparse и возвращался целой справкой `usage:` — чтобы
    понять, что не так, надо было читать флаги чужого скрипта. Перечень режимов теперь
    проверяется на входе, и в тексте названы допустимые.
    """
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified",
         type="concept")
    calls = [("unknown", {}),
             ("kb_card", {"name": "Нет-такой-карточки"}),
             ("kb_card", {"name": ""}),
             ("kb_search", {"query": "   "}),
             ("kb_context", {"topic": "обеспечение", "mode": "выдумка"}),
             ("kb_context", {"topic": ""}),
             ("kb_ask", {"question": ""}),
             ("kb_search", {"query": "обеспечение"}),
             ("kb_card", {"name": "Обеспечение"})]
    lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
             for i, (name, args) in enumerate(calls, 1)]
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=180)
    res = {m["id"]: m["result"] for m in map(json.loads, filter(str.strip, cp.stdout.splitlines()))}
    assert len(res) == len(calls), f"ответили не на все вызовы: {sorted(res)}\n{cp.stderr[-300:]}"

    def text(i):
        return res[i]["content"][0]["text"]
    for i, why in ((1, "неизвестный инструмент"), (2, "нет карточки"), (3, "пустое имя"),
                   (4, "пустой запрос"), (5, "режим вне перечня"), (6, "пустая тема"),
                   (7, "пустой вопрос")):
        assert res[i].get("isError") is True, f"{why}: отказ без isError — {res[i]}"
    assert "usage:" not in text(5), f"режим вне перечня вернул справку argparse: {text(5)[:200]}"
    assert "generate" in text(5) and "review" in text(5), \
        f"в отказе не названы допустимые режимы: {text(5)}"
    assert "kb_search" in text(2), "про карточку, которой нет, не подсказано, чем искать"
    # код 1 у движка — «отработала и нашла, что сказать»: по теме ничего нет — это ответ базы, а не сбой;
    # у `kb_ask` код 1 значит «ни одна модель не ответила», и там это отказ
    fake = tmp / "fake"
    (fake / ".aurora/scripts").mkdir(parents=True)
    # Тексты латиницей: эти скрипты не переключают вывод на UTF-8, как движок, и на Windows
    # труба в cp1252 не пропустила бы русский (см. тест о кодировке дочерних процессов).
    (fake / ".aurora/scripts/exit1.py").write_text("print('nothing found'); raise SystemExit(1)\n",
                                                     encoding="utf-8")
    (fake / ".aurora/scripts/exit2.py").write_text("print('broken'); raise SystemExit(2)\n",
                                                     encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    assert M.run(str(fake), "exit1.py", []) == "nothing found", "код 1 принят за сбой"
    for script, kw in (("exit2.py", {}), ("exit1.py", {"fail_from": 1})):
        try:
            M.run(str(fake), script, [], **kw)
        except M.ToolError as e:
            assert "broken" in str(e) or "nothing found" in str(e), str(e)
        else:
            raise AssertionError(f"{script} {kw}: сбой не стал отказом")
    assert not res[8].get("isError") and "Обеспечение" in text(8), f"обычный поиск: {res[8]}"
    assert not res[9].get("isError") and "Правила обеспечения" in text(9), f"обычная карточка: {res[9]}"


@test
def test_mcp_card_by_name_does_not_follow_a_link_out_of_the_base(tmp: Path):
    """`kb_card` не читает карточку-ссылку на файл вне базы (BL-NEW-01).

    git хранит символические ссылки: `AuroraKnowledgeDB/x/leak.md -> файл вне проекта`
    находилась обходом базы и читалась `open()` как карточка. Имя приходит от модели
    (ассистента или бота), а репозиторий может быть чужим. Ссылка наружу и ссылка на
    секретное имя внутри базы — «карточки нет»; обычная карточка и ссылка на другую
    карточку базы читаются. Ссылки создаются только во временном каталоге.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified",
         type="concept")
    card(root, "Concepts/Credentials.md", "Понятие про учётные данные.", status="verified",
         type="concept")
    outside = tmp / "снаружи"
    outside.mkdir()
    (outside / "чужое.md").write_text("OUTSIDE\n", encoding="utf-8")
    base = root / "AuroraKnowledgeDB" / "Concepts"
    (base / "ключ.pem").write_text("KEY\n", encoding="utf-8")
    try:
        os.symlink(outside / "чужое.md", base / "утечка.md")
        os.symlink(base / "ключ.pem", base / "обычное-имя.md")
        os.symlink(base / "Обеспечение.md", base / "алиас.md")
    except (OSError, NotImplementedError):
        return      # без прав на ссылки (Windows) проверять нечего
    for name in ("утечка", "обычное-имя"):
        assert M.card_path(str(root), name) == "", f"{name}: ссылка из базы наружу прошла как карточка"
        try:
            M.call_tool(str(root), "kb_card", {"name": name})
        except M.ToolError as e:
            assert "в базе нет" in str(e), str(e)
        else:
            raise AssertionError(f"{name}: kb_card отдал файл за пределами базы")
    assert "Правила обеспечения" in M.call_tool(str(root), "kb_card", {"name": "Обеспечение"})
    assert "Правила обеспечения" in M.call_tool(str(root), "kb_card", {"name": "алиас"}), \
        "ссылка на карточку внутри базы перестала читаться"
    assert "учётные данные" in M.call_tool(str(root), "kb_card", {"name": "Credentials"}), \
        "карточка с именем Credentials.md отсечена как секретный файл"


@test
def test_every_card_and_source_reader_refuses_a_link_out_of_the_project(tmp: Path):
    """Ссылка на файл вне проекта не читается ни одним читателем карточек и источников (BL-NEW-01).

    Граница `realpath` стояла в трёх местах (инструменты адаптера, `@`-упоминания, `kb_card`),
    а обход базы (`walk_md`), поиск, контекст-паки («Спросить»), разбор источников и правки
    базы открывали карточку-ссылку `open()` и читали цель. Теперь один помощник
    (`aurora_common.link_escapes`) стоит в общем обходе и в точках разового чтения. Файл вне
    проекта лежит только во временном каталоге; ссылка внутри проекта по-прежнему читается.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    M = importlib.import_module("aurora_mcp")
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified",
         type="concept")
    outside = tmp / "снаружи"
    outside.mkdir()
    secret = "ТАЙНАСНАРУЖИ"
    (outside / "чужое.md").write_text(
        f'---\ntitle: "Утечка"\ntype: concept\nstatus: verified\n---\n\n# Утечка\n\n{secret} '
        "утечка утечка утечка.\n", encoding="utf-8")
    (outside / "ключ").write_text(secret + "\n", encoding="utf-8")
    kb = root / "AuroraKnowledgeDB" / "Concepts"
    body = ("Источник " + "слово " * 60 + secret + "\n\n" + "второй абзац " * 40)
    (root / "Sources" / "Confluence").mkdir(parents=True, exist_ok=True)
    real_page = root / "Sources" / "Confluence" / "Настоящая.md"
    real_page.write_text("# Настоящая\n\n" + body, encoding="utf-8")
    (outside / "страница.md").write_text("# Чужая\n\n" + body, encoding="utf-8")
    try:
        os.symlink(outside / "чужое.md", kb / "утечка.md")
        os.symlink(outside / "ключ", kb / "ключ.md")
        os.symlink(kb / "Обеспечение.md", kb / "алиас.md")
        os.symlink(outside / "страница.md", root / "Sources" / "Confluence" / "Ссылка.md")
        os.symlink(real_page, root / "Sources" / "Confluence" / "Внутренняя.md")
    except (OSError, NotImplementedError):
        return      # без прав на ссылки (Windows) проверять нечего

    base = str(root / "AuroraKnowledgeDB")
    walked = {os.path.basename(p) for p in AC.walk_md(base)}
    assert "утечка.md" not in walked and "ключ.md" not in walked, f"обход базы отдал ссылку наружу: {walked}"
    assert {"Обеспечение.md", "алиас.md"} <= walked, f"ссылка внутри базы перестала обходиться: {walked}"
    assert AC.read_card_text(str(kb / "утечка.md")) is None, "read_card_text прочитал цель ссылки наружу"
    assert "обеспечения" in (AC.read_card_text(str(kb / "алиас.md")) or ""), "ссылка внутри не читается"
    assert not any(secret in c.text for c in AC.load_cards(base).values()), "load_cards унёс текст снаружи"
    assert AC.link_escapes(str(kb / "утечка.md")) and not AC.link_escapes(str(kb / "алиас.md"))
    assert not AC.link_escapes(str(kb / "Обеспечение.md")), "обычный файл принят за ссылку наружу"

    # поиск, контекст-пак и «Спросить» собираются из одного обхода
    for tool, args in (("kb_search", {"query": "утечка"}),
                       ("kb_context", {"topic": "утечка", "mode": "ask"})):
        out = M.call_tool(str(root), tool, args)
        assert secret not in out, f"{tool}: текст файла вне проекта ушёл в ответ"
    assert "Обеспечение" in M.call_tool(str(root), "kb_search", {"query": "обеспечение"}), \
        "контроль: поиск перестал находить обычную карточку"

    # индекс поиска, правки базы
    KE = importlib.import_module("kb_embed")
    assert secret not in " ".join(map(str, KE.card_texts(base).values())), "kb_embed взял текст снаружи"
    KF = importlib.import_module("kb_fix")
    assert not any("утечка" in k for k in KF.load_cards(base)), "kb_fix открыл бы ссылку наружу на правку"

    # разбор источников
    BP = importlib.import_module("build_plan")
    cur = os.getcwd()
    os.chdir(root)
    try:
        names = {os.path.basename(p) for _g, p, _s in BP.sources()}
    finally:
        os.chdir(cur)
    assert "Ссылка.md" not in names, f"план разбора включил ссылку наружу: {names}"
    assert {"Настоящая.md", "Внутренняя.md"} <= names, f"ссылка внутри проекта выпала из плана: {names}"
    cp = run("build_plan.py", "--card", "Демо", "--source", "Sources/Confluence/Ссылка.md",
             "--paras", "1", "--to", "Concepts", cwd=root)
    assert cp.returncode != 0 and "за пределы проекта" in cp.stderr, (cp.returncode, cp.stdout, cp.stderr)
    assert secret not in cp.stdout + cp.stderr
    cp = run("build_plan.py", "--card", "Демо", "--source", "Sources/Confluence/Внутренняя.md",
             "--paras", "1", "--to", "Concepts", cwd=root)
    assert cp.returncode == 0, f"ссылка внутри проекта не читается:\n{cp.stdout}\n{cp.stderr}"

    # шаблон и промпт для агента
    AR = importlib.import_module("agent_runner")
    (root / "Templates").mkdir(exist_ok=True)
    os.symlink(outside / "ключ", root / "Templates" / "t.md")
    os.symlink(real_page, root / "Templates" / "внутри.md")
    assert AR.read_text_file(str(root / "Templates" / "t.md")) == "", "агенту отдан файл вне проекта"
    assert "Настоящая" in AR.read_text_file(str(root / "Templates" / "внутри.md"))


@test
def test_installing_a_skill_never_leaves_the_agent_without_it(tmp: Path):
    """Обновление скилла не оставляет агента без скилла: сбой копирования не стирает прежний.

    Установка сносила установленную папку (`rmtree`) и только потом копировала новую: сбой
    посреди копирования — диск, права, обрыв — оставлял `/aurora-vault` без единого файла, а
    повторный запуск видел «новый» и не знал, что было. Копия теперь делается рядом, подмена —
    переименованием; и ссылка для другого harness, которую Windows без прав не создаёт, не
    обрывает установку после того, как скиллы уже стоят.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    IS = importlib.import_module("install_skills")
    src, home, link_dir = tmp / "kit-skills", tmp / "home-skills", tmp / "cfg" / "opencode" / "skills"
    (src / "aurora-x").mkdir(parents=True)
    (src / "aurora-x" / "SKILL.md").write_text("новая версия\n", encoding="utf-8")
    (src / "aurora-x" / "more.md").write_text("ещё файл\n", encoding="utf-8")
    (home / "aurora-x").mkdir(parents=True)
    (home / "aurora-x" / "SKILL.md").write_text("прежняя версия\n", encoding="utf-8")
    saved = (IS.SRC, IS.HOME_SKILLS, IS.LINK_DIRS, shutil.copytree)
    IS.SRC, IS.HOME_SKILLS, IS.LINK_DIRS = src, home, (link_dir,)
    try:
        def broken(a, b, *args, **kw):
            Path(b).mkdir(parents=True)
            (Path(b) / "SKILL.md").write_text("обрывок", encoding="utf-8")
            raise OSError("нет места на диске")
        shutil.copytree = broken
        try:
            IS.cmd_install(True)
        except OSError:
            pass
        assert (home / "aurora-x" / "SKILL.md").read_text(encoding="utf-8") == "прежняя версия\n", \
            "сбой копирования стёр установленный скилл"
        assert sorted(p.name for p in home.iterdir()) == ["aurora-x"], \
            f"после сбоя в каталоге скиллов остался мусор: {sorted(p.name for p in home.iterdir())}"
    finally:
        shutil.copytree = saved[3]

    # ссылку для другого harness создать нельзя (Windows без прав): скиллы уже стоят, установка идёт дальше
    real_link = Path.symlink_to
    link_dir.parent.mkdir(parents=True, exist_ok=True)

    def denied(self, *a, **kw):
        raise OSError(1314, "Требуемый клиентом привилегий не хватает")
    try:
        Path.symlink_to = denied
        rc = IS.cmd_install(True)
    finally:
        Path.symlink_to = real_link
        IS.SRC, IS.HOME_SKILLS, IS.LINK_DIRS = saved[:3]
    assert rc == 0, "отказ в создании ссылки оборвал установку"
    assert (home / "aurora-x" / "SKILL.md").read_text(encoding="utf-8") == "новая версия\n", "скилл не обновился"
    assert (home / "aurora-x" / "more.md").is_file()
    assert sorted(p.name for p in home.iterdir()) == ["aurora-x"], "после установки остался временный каталог"


@test
def test_saving_the_same_settings_twice_keeps_the_rollback_copy(tmp: Path):
    """Повторное сохранение тех же настроек не затирает копию «до» — откат остаётся откатом.

    `local/mcp.json` кита и `aurora.config.yaml` перед записью копировались в `.bak`, даже когда
    сохранялось то же самое. Две подряд нажатые «Сохранить» (или «Сохранить» без правок)
    делали `.bak` копией текущего файла, и прежняя версия — единственное, к чему можно
    вернуться, — пропадала. Запись без изменений ничего не пишет и копию не трогает.
    """
    kit = tmp / "kit"
    kit.mkdir()
    ck, restore = _cockpit_on(kit)
    try:
        project = str(tmp / "proj")
        os.makedirs(project)
        a = {"alpha": {"command": "a"}}
        b = {"alpha": {"command": "a"}, "beta": {"command": "b"}}
        assert ck.mcp_write(a).get("ok")
        path = ck.kit_mcp_file()
        assert not os.path.exists(path + ".bak"), "первая запись создала копию из ничего"
        assert ck.mcp_write(b).get("ok")
        was = Path(path + ".bak").read_text(encoding="utf-8")
        assert '"beta"' not in was and '"alpha"' in was, "копия после первой правки — не прежняя версия"
        again = ck.mcp_write(b)
        assert again.get("ok") and again.get("unchanged"), f"то же самое записано заново: {again}"
        assert Path(path + ".bak").read_text(encoding="utf-8") == was, \
            "повторное сохранение затёрло копию «до» самим собой"

        cfg = Path(project) / "aurora.config.yaml"
        save = lambda text: ck.Handler._write_config(None, project, text)
        assert save("project:\n  name: A\n").get("ok")
        assert save("project:\n  name: B\n").get("ok")
        bak = Path(project) / "aurora.config.yaml.bak"
        assert "name: A" in bak.read_text(encoding="utf-8")
        same = save("project:\n  name: B\n")
        assert same.get("ok") and same.get("unchanged"), f"то же самое записано заново: {same}"
        assert "name: A" in bak.read_text(encoding="utf-8"), "повторное сохранение конфига затёрло копию «до»"
        assert "name: B" in cfg.read_text(encoding="utf-8")
    finally:
        restore()


@test
def test_the_panel_does_not_start_the_same_command_twice_at_once(tmp: Path):
    """Та же команда в том же проекте с теми же аргументами не запускается второй раз, пока идёт первая.

    Двойной щелчок по «Запустить», вторая вкладка или повторное нажатие после обрыва связи
    запускали второй процесс рядом с первым: два одинаковых пишущих прогона над одной базой.
    Агент от этого защищён замком, а механические команды (`kb:repair --apply`, синки) — нет.
    Второй запуск получает идентификатор идущего задания и смотрит тот же вывод.
    """
    import threading
    import time
    kit = tmp / "kit"
    kit.mkdir()
    ck, restore = _cockpit_on(kit)
    sleepy = tmp / "sleepy.py"
    sleepy.write_text("import sys, time\nprint('running', flush=True)\ntime.sleep(float(sys.argv[1]) "
                      "if len(sys.argv) > 1 else 3)\n", encoding="utf-8")
    row = {"runnable": True, "flags": ["--apply"], "fixed_flags": ["3"], "script": "sleepy.py", "cmd": "kb:x"}
    saved = (ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running)
    # Нить последнего задания может дойти до `mark_running` уже после возврата настоящей
    # функции: список заданий кита ей недоступен — запись «kb:x / proj» оставалась в нём
    # навсегда и висела строкой «идёт» в панели.
    real_running = ck.RUNNING
    ck.RUNNING = str(tmp / "running.json")
    ck.command_by_name = lambda name: row if name == "kb:x" else None
    ck.script_path = lambda project, script: str(sleepy)
    ck.write_runlog = lambda *a, **k: None
    ck.mark_running = lambda *a, **k: None
    project = str(tmp / "proj")
    os.makedirs(project)
    started = []
    alive_before = set(threading.enumerate())      # чужие нити (панель, соседние проверки) не ждём
    try:
        first = ck.start_job(project, "kb:x", ["--apply"])
        threads = [threading.Thread(target=lambda: started.append(ck.start_job(project, "kb:x", ["--apply"])))
                   for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(started) == {first}, f"параллельные запуски дали разные задания: {started}"
        assert len([j for j in ck.JOBS.values() if not j["done"]]) == 1, "идут два одинаковых задания"
        other = ck.start_job(project, "kb:x", [])
        assert other != first, "команда с другими аргументами приняла чужое задание"
        elsewhere = ck.start_job(str(tmp / "other"), "kb:x", ["--apply"])
        assert elsewhere not in (first, other), "задание чужого проекта принято за своё"
        for _ in range(100):
            if all(j["done"] for j in ck.JOBS.values()):
                break
            time.sleep(0.2)
        again = ck.start_job(project, "kb:x", ["--apply"])
        assert again != first, "после конца задания новый запуск получил старое задание"
    finally:
        # Нить задания закрывает его уже ПОСЛЕ `done` (`mark_running`, `write_runlog`), а
        # последняя могла ещё и не дойти до `Popen`. Подмены и временный список заданий
        # возвращаем, когда нитей этого теста не осталось: поздняя нить иначе записала бы
        # «kb:x / proj» в настоящий список кита, а снять запись пришлось бы в чужой файл —
        # следующая проверка успевала подменить `RUNNING` раньше, чем она закончит.
        mine = [t for t in threading.enumerate()
                if t not in alive_before and t is not threading.current_thread()]
        deadline = time.time() + 20
        for t in mine:
            while t.is_alive() and time.time() < deadline:
                for j in list(ck.JOBS.values()):
                    if j.get("proc") and not j["done"]:
                        j["proc"].kill()
                t.join(0.05)
        ck.JOBS.clear()
        ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running = saved
        ck.RUNNING = real_running
        restore()


@test
def test_setup_wizard_without_a_terminal_ends_cleanly(tmp: Path):
    """Мастер настройки без терминала не падает трассировкой: конец ввода — это «оставить как есть».

    Запущенный из скрипта, конвейера или закрытым `stdin`, мастер читал `input()` и падал
    с `EOFError` на первом вопросе — человек видел страницу трассировки вместо слов. Конец
    ввода теперь значит то же, что пустая строка: оставить текущее значение и перейти
    к следующему; Ctrl+C говорит, что ничего не записано, и выходит кодом 130.
    """
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Старое имя"\n  slug: old\natlassian:\n  confluence:\n    space: SP\n'
        '  jira:\n    project_key: KEY\n', encoding="utf-8")
    script = str(root / ".aurora/scripts/aurora_setup.py")

    def wizard(stdin_text):
        return subprocess.run([sys.executable, script, "--target", str(root)],
                              input=stdin_text, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    cp = wizard("")
    assert cp.returncode == 0, f"мастер упал без ввода:\n{cp.stderr[-400:]}"
    assert "Traceback" not in cp.stderr and "EOFError" not in cp.stderr, cp.stderr[-300:]
    assert "Старое имя" in (root / "aurora.config.yaml").read_text(encoding="utf-8"), \
        "конец ввода изменил значение, которое надо было оставить"
    assert "ввод закончился" in cp.stdout, "человеку не сказано, что остальное оставлено как было"

    cp = wizard("Новое имя\n")                     # один ответ, дальше ввода нет
    assert cp.returncode == 0 and "Новое имя" in (root / "aurora.config.yaml").read_text(encoding="utf-8"), \
        f"ответ до конца ввода не записан:\n{cp.stdout[-300:]}{cp.stderr[-300:]}"

    sys.path.insert(0, str(SCRIPTS))
    import builtins
    import importlib
    S = importlib.import_module("aurora_setup")
    before = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    real_input, real_argv = builtins.input, sys.argv

    def interrupted(*_a):
        raise KeyboardInterrupt
    try:
        builtins.input = interrupted
        sys.argv = ["aurora_setup.py", "--target", str(root)]
        rc = S.main()
    finally:
        builtins.input, sys.argv = real_input, real_argv
    assert rc == 130, f"Ctrl+C: ждали код 130, получили {rc}"
    assert (root / "aurora.config.yaml").read_text(encoding="utf-8") == before, "прерванный мастер что-то записал"


def _outside_project(tmp: Path, root: Path) -> Path:
    """Папка вне проекта для проверок границы чтения ссылок (BL-NEW-01)."""
    out = tmp / "снаружи"
    out.mkdir(exist_ok=True)
    return out


def _link(target, name) -> bool:
    """Ссылка `name` → `target`; False, если система ссылок не даёт (Windows без прав).

    Цель пишется с разделителем системы: Windows не разбирает «/» в цели ссылки, и относительная
    `../Glossary/y.md` оставалась «битой» (git для Windows пишет «\\» по той же причине)."""
    try:
        os.symlink(os.fspath(target).replace("/", os.sep), name)
        return True
    except (OSError, NotImplementedError):
        return False


@test
def test_office_ingest_does_not_launder_a_link_out_of_the_project_into_raw(tmp: Path):
    """Вложение-ссылка на файл вне проекта не превращается в обычный файл Raw/ (BL-NEW-01).

    `.txt` и `.csv` конвертер `plain` читает как есть, а обход вложений шёл `os.walk`: ссылка
    `Raw/project/x.txt` на чужой файл ложилась в `Raw/project/x.md` обычным текстом, и уже он
    проходил все проверки границы — дальше в разбор моделью, в карточку и в git.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    OI = importlib.import_module("office_ingest")
    root = make_project(tmp)
    outside = _outside_project(tmp, root)
    secret = "ТАЙНАВЛОЖЕНИЯ"
    (outside / "чужой.txt").write_text(secret + "\n", encoding="utf-8")
    (outside / "чужая.csv").write_text(f"a,b\n{secret},2\n", encoding="utf-8")
    raw = root / "Raw" / "project"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "свой.txt").write_text("свой текст вложения\n", encoding="utf-8")
    if not (_link(outside / "чужой.txt", raw / "утечка.txt")
            and _link(outside / "чужая.csv", raw / "таблица.csv")):
        return
    assert OI.convert(str(raw / "утечка.txt"), "plain") == (None, None), "convert прочитал цель ссылки"
    found = {os.path.basename(p) for p in OI.targets([str(raw)], str(root))}
    assert found == {"свой.txt"}, f"обход вложений отдал ссылку наружу: {found}"
    assert OI.targets([str(raw / "утечка.txt")], str(root)) == [], "явный путь-ссылка принят"
    run("office_ingest.py", "--converter", "plain", cwd=root, expect_rc=0)
    made = sorted(p.name for p in raw.glob("*.md"))
    assert made == ["свой.md"], f"из ссылок наружу появились транскрипты: {made}"
    assert secret not in "".join(p.read_text(encoding="utf-8") for p in raw.glob("*.md"))


@test
def test_human_corrections_are_not_read_through_a_link_out_of_the_project(tmp: Path):
    """Исправление-ссылка на файл вне проекта не применяется, не показывается и не правится (BL-NEW-01).

    `corrections()` шло `os.listdir` и `open()`: тело чужого файла по ссылке в
    `Raw/corrections/` `--apply` дословно писал в карточку (слово человека — высший приоритет
    доверия), а `--retire` ещё и дописывал шапку в файл-цель. Те же шапки читают `kb_kind` и
    `kb_trace`.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified")
    outside = _outside_project(tmp, root)
    secret = "ТАЙНАИСПРАВЛЕНИЯ"
    foreign = outside / "чужое.md"
    foreign.write_text(f'---\ntitle: "Чужое"\ncorrects: "[[Обеспечение]]"\nkind: dictionary\n'
                       f'---\n\n{secret}\n', encoding="utf-8")
    cdir = root / "Raw" / "corrections"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "своё.md").write_text('---\ntitle: "Своё"\ncorrects: "[[Обеспечение]]"\n---\n\n'
                                  "Поставка идёт по графику.\n", encoding="utf-8")
    if not _link(foreign, cdir / "утечка.md"):
        return
    before = foreign.read_text(encoding="utf-8")
    cp = run("kb_corrections.py", "--list", cwd=root)
    assert "утечка" not in cp.stdout and secret not in cp.stdout + cp.stderr, cp.stdout
    assert "своё" in cp.stdout.lower() or "Своё" in cp.stdout, f"контроль: своё исправление пропало\n{cp.stdout}"
    run("kb_corrections.py", "--apply", cwd=root)
    text = (root / "AuroraKnowledgeDB" / "Concepts" / "Обеспечение.md").read_text(encoding="utf-8")
    assert secret not in text, "тело файла по ссылке наружу записано в карточку"
    assert "Поставка идёт по графику" in text, f"контроль: своё исправление не применено\n{text}"
    cp = run("kb_corrections.py", "--retire", "утечка", "--reason", "проверка", cwd=root)
    assert cp.returncode != 0, "снятие нашло ссылку наружу как исправление"
    assert foreign.read_text(encoding="utf-8") == before, "--retire записал в файл вне проекта"
    cwd = os.getcwd()
    os.chdir(root)
    try:
        KK = importlib.import_module("kb_kind")
        KT = importlib.import_module("kb_trace")
        assert KK.human_kinds(".") == {}, "kb_kind взял тип из шапки файла вне проекта"
        tdir = root / "AuroraKnowledgeDB" / "Questions"
        tdir.mkdir(parents=True, exist_ok=True)
        (outside / "вопрос.md").write_text('---\nq_id: Q-9\nq_status: open\nblocks: "[[Обеспечение]]"\n'
                                           "---\n", encoding="utf-8")
        assert _link(outside / "вопрос.md", tdir / "Q-9.md")
        assert KT.load(str(tdir)) == [], "kb_trace.load прочитал шапку по ссылке наружу"
        saved = KT.QUESTION_DIR
        KT.QUESTION_DIR = str(tdir)
        try:
            assert KT.load_questions() == {}, "kb_trace.load_questions прочитал ссылку наружу"
        finally:
            KT.QUESTION_DIR = saved
        adir = root / "Artifacts" / "acceptance"
        adir.mkdir(parents=True, exist_ok=True)
        (outside / "акт.md").write_text('---\nverdict: ok\ncovers: "[[R-1]]"\n---\n', encoding="utf-8")
        assert _link(outside / "акт.md", adir / "акт.md")
        assert KT.load_acceptance() == {}, "kb_trace.load_acceptance прочитал ссылку наружу"
    finally:
        os.chdir(cwd)


def _walker_fixture(tmp: Path):
    """Проект с обычной карточкой и ссылками наружу (карточка, SQL) для проверки обходов базы.

    → (корень, секрет) или None, если система не даёт ссылок. Файл вне проекта лежит во
    временном каталоге.
    """
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified")
    outside = _outside_project(tmp, root)
    secret = "ТАЙНАОБХОДА"
    (outside / "чужое.md").write_text(
        f'---\ntitle: "Утечка"\ntype: concept\nstatus: verified\n---\n\n# Утечка\n\n{secret}\n',
        encoding="utf-8")
    (outside / "запрос.sql").write_text(f"select {secret} from t_secret;\n", encoding="utf-8")
    if not (_link(outside / "чужое.md", root / "AuroraKnowledgeDB" / "Concepts" / "утечка.md")
            and _link(outside / "запрос.sql", root / "Sources" / "q.sql")):
        return None
    return root, secret


def _in_project(root: Path, fn):
    """Выполнить `fn` с рабочей папкой в корне проекта (читатели берут пути от неё)."""
    sys.path.insert(0, str(SCRIPTS))
    cwd = os.getcwd()
    os.chdir(root)
    try:
        return fn()
    finally:
        os.chdir(cwd)


@test
def test_moc_reader_skips_a_card_link_out_of_the_project(tmp: Path):
    """Оглавления (`kb_moc.read_cards`) не берут карточку-ссылку наружу (BL-NEW-01)."""
    fx = _walker_fixture(tmp)
    if not fx:
        return
    import importlib

    def go():
        MOC = importlib.import_module("kb_moc")
        cards = MOC.read_cards()
        assert "утечка" not in cards, "kb_moc.read_cards прочитал ссылку наружу"
        assert "Обеспечение" in cards, "контроль: обычная карточка пропала из kb_moc"
    _in_project(fx[0], go)


@test
def test_card_graph_skips_a_card_link_out_of_the_project(tmp: Path):
    """Граф карточек (`kb_graph.write_cards_graph`) не включает карточку-ссылку наружу (BL-NEW-01)."""
    fx = _walker_fixture(tmp)
    if not fx:
        return
    import importlib
    root, secret = fx

    def go():
        KG = importlib.import_module("kb_graph")
        graph = KG.write_cards_graph(str(tmp / "graph.json"))
        dumped = (tmp / "graph.json").read_text(encoding="utf-8") + json.dumps(graph, ensure_ascii=False)
        assert "утечка" not in dumped and secret not in dumped, "граф карточек включил ссылку наружу"
        assert "Обеспечение" in dumped, "контроль: обычная карточка пропала из графа"
    _in_project(root, go)


@test
def test_supersede_does_not_find_a_card_link_out_of_the_project(tmp: Path):
    """Замена карточки (`kb_supersede.find_card`) не находит карточку-ссылку наружу (BL-NEW-01)."""
    fx = _walker_fixture(tmp)
    if not fx:
        return
    import importlib

    def go():
        KS = importlib.import_module("kb_supersede")
        assert KS.find_card("утечка") is None, "kb_supersede нашёл карточку по ссылке наружу"
        assert KS.find_card("Обеспечение"), "контроль: kb_supersede не находит обычную карточку"
    _in_project(fx[0], go)


@test
def test_trace_scan_skips_a_card_link_out_of_the_project(tmp: Path):
    """Трассировка (`kb_trace.scan`) не читает карточку-ссылку наружу (BL-NEW-01)."""
    fx = _walker_fixture(tmp)
    if not fx:
        return
    import importlib

    def go():
        KT = importlib.import_module("kb_trace")
        found = list(KT.scan(["AuroraKnowledgeDB"]))
        assert not [p for p in found if "утечка" in p], f"kb_trace.scan прочитал ссылку наружу: {found}"
        assert [p for p in found if "Обеспечение" in p], "контроль: обычная карточка пропала из трассы"
    _in_project(fx[0], go)


@test
def test_code_graph_sql_sources_skip_a_link_out_of_the_project(tmp: Path):
    """Граф кода (`kb_code_graph.sql_sources`) не читает `.sql`-ссылку наружу (BL-NEW-01)."""
    fx = _walker_fixture(tmp)
    if not fx:
        return
    import importlib
    root, secret = fx
    (root / "Sources" / "свой.sql").write_text("select 1 from t_own;\n", encoding="utf-8")

    def go():
        CG = importlib.import_module("kb_code_graph")
        found = CG.sql_sources(".")
        assert not [s for s in found if secret in s[1]], "kb_code_graph.sql_sources прочитал ссылку наружу"
        assert [s for s in found if "t_own" in s[1]], "контроль: обычный .sql пропал"
    _in_project(root, go)


@test
def test_page_moves_follow_does_not_read_a_card_link_out_of_the_project(tmp: Path):
    """Перенос переехавших страниц (`kb_remap.follow_moves`) не читает карточку-ссылку наружу (BL-NEW-01)."""
    fx = _walker_fixture(tmp)
    if not fx:
        return
    import importlib
    root, secret = fx

    def go():
        KR = importlib.import_module("kb_remap")
        BP = importlib.import_module("build_plan")
        seen = []
        real = BP.retarget_card
        BP.retarget_card = lambda text, full: (seen.append(text), real(text, full))[1]
        try:
            KR.follow_moves("Sources/Confluence", False, everywhere=True)
        finally:
            BP.retarget_card = real
        assert seen, "контроль: follow_moves не прочитал ни одной карточки"
        assert not any(secret in t for t in seen), "kb_remap.follow_moves прочитал ссылку наружу"
    _in_project(root, go)


@test
def test_single_file_readers_refuse_a_file_inside_a_linked_directory(tmp: Path):
    """Файл внутри каталога-ссылки на папку вне проекта читается так же запретно, как сама ссылка (BL-NEW-01).

    `islink` смотрит только последнюю компоненту: `Sources/Web/папка/страница.md` при ссылке
    `папка` на каталог вне проекта считалась обычным файлом, и разовые читатели (`--source`,
    `--slice`, `read_card_text`, `read_text_file`) отдавали цель. Папка-ссылка внутри проекта
    по-прежнему читается; обход (`safe_walk`) в такие каталоги не заходит.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    AR = importlib.import_module("agent_runner")
    root = make_project(tmp)
    outside = _outside_project(tmp, root)
    secret = "ТАЙНАКАТАЛОГА"
    folder = outside / "папка"
    (folder / "вложенная").mkdir(parents=True, exist_ok=True)
    (folder / "страница.md").write_text("# Чужая\n\n" + ("слово " * 60) + secret + "\n", encoding="utf-8")
    (folder / "вложенная" / "глубже.md").write_text("# Глубже\n\n" + secret + "\n", encoding="utf-8")
    web = root / "Sources" / "Web"
    web.mkdir(parents=True, exist_ok=True)
    real_dir = web / "настоящая"
    real_dir.mkdir()
    (real_dir / "страница.md").write_text("# Своя\n\n" + ("слово " * 60) + "\n", encoding="utf-8")
    if not (_link(folder, web / "ссылка") and _link(real_dir, web / "внутренняя")
            and _link(folder / "страница.md", web / "прямая.md")):
        return
    cwd = os.getcwd()
    os.chdir(root)
    try:
        for rel in ("Sources/Web/ссылка/страница.md", "Sources/Web/ссылка/вложенная/глубже.md"):
            assert AC.link_escapes(rel), f"{rel}: файл в каталоге-ссылке наружу принят за обычный"
            assert AC.read_card_text(rel) is None, f"{rel}: read_card_text прочитал цель"
            assert AR.read_text_file(rel) == "", f"{rel}: read_text_file отдал цель агенту"
        assert not AC.link_escapes("Sources/Web/внутренняя/страница.md"), "папка-ссылка внутри проекта закрыта"
        assert not AC.link_escapes("Sources/Web/настоящая/страница.md"), "обычный файл закрыт"
        assert AC.read_card_text("Sources/Web/внутренняя/страница.md"), "папка-ссылка внутри не читается"
        walked = {os.path.relpath(p, ".").replace("\\", "/") for p in AC.walk_md("Sources")}
        assert not any("ссылка" in p for p in walked), f"обход зашёл в каталог-ссылку наружу: {walked}"
        cp = run("build_plan.py", "--slice", "Sources/Web/ссылка/страница.md", cwd=root)
        assert cp.returncode != 0 and secret not in cp.stdout + cp.stderr, (cp.stdout, cp.stderr)
        cp = run("build_plan.py", "--card", "Демо", "--source", "Sources/Web/ссылка/страница.md",
                 "--paras", "1", "--to", "Concepts", cwd=root)
        assert cp.returncode != 0 and secret not in cp.stdout + cp.stderr, (cp.stdout, cp.stderr)
        cp = run("build_plan.py", "--slice", "Sources/Web/прямая.md", cwd=root)
        assert cp.returncode != 0 and secret not in cp.stdout + cp.stderr, \
            f"раскадровка прочитала ссылку наружу: {cp.stdout}"
        cp = run("build_plan.py", "--slice", "Sources/Web/внутренняя/страница.md", cwd=root)
        assert cp.returncode == 0, f"папка-ссылка внутри проекта не читается:\n{cp.stdout}\n{cp.stderr}"
    finally:
        os.chdir(cwd)


@test
def test_publish_thread_template_and_lint_handle_a_link_out_of_the_project(tmp: Path):
    """Публикация, разговор «Спросить», зеркала шаблонов и линтер знают о ссылке наружу (BL-NEW-01).

    `publish_doc` проверял слой по строке пути и читал цель ссылки, `read_thread` отдавал
    модели историю из файла вне проекта, `project_file` и зеркала шаблонов шли `os.walk`, а
    `kb_lint` молча считал карточку-ссылку карточкой — человек не понимал, почему движок её не видит.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    AR = importlib.import_module("agent_runner")
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified")
    outside = _outside_project(tmp, root)
    secret = "ТАЙНАПУБЛИКАЦИИ"
    (outside / "док.md").write_text(f"# Чужой\n\n{secret}\n", encoding="utf-8")
    (outside / "разговор.md").write_text(
        f"### Вопрос · 2026-01-01\n\n{secret}\n\n### Ответ · x\n\nответ\n", encoding="utf-8")
    (outside / "шаблон.md").write_text(f"# Шаблон\n\n{secret}\n", encoding="utf-8")
    (outside / "карточка.md").write_text(
        f'---\ntitle: "Внешняя"\ntype: concept\nstatus: draft\n---\n\n# Внешняя\n\n{secret}\n', encoding="utf-8")
    (root / "Artifacts").mkdir(exist_ok=True)
    ask = root / "AuroraKnowledgeDB" / "meta" / "ask"
    ask.mkdir(parents=True, exist_ok=True)
    (root / "Templates").mkdir(exist_ok=True)
    if not (_link(outside / "док.md", root / "Artifacts" / "утечка.md")
            and _link(outside / "разговор.md", ask / "утечка.md")
            and _link(outside / "шаблон.md", root / "Templates" / "утечка.md")
            and _link(outside / "карточка.md", root / "AuroraKnowledgeDB" / "Concepts" / "Внешняя.md")
            and _link(root / "Raw", root / "Artifacts" / "слой")):
        return
    # публикация: цель вне проекта и закрытый слой по ссылке не уходят наружу
    cp = run("publish_doc.py", "Artifacts/утечка.md", cwd=root)
    assert cp.returncode == 1 and "не публикуется" in cp.stderr, (cp.returncode, cp.stdout, cp.stderr)
    (root / "Raw" / "x.md").write_text("# Внутренний слой\n", encoding="utf-8")
    cp = run("publish_doc.py", "Artifacts/слой/x.md", cwd=root)
    assert cp.returncode == 1 and "не публикуется" in cp.stderr, (cp.returncode, cp.stdout, cp.stderr)
    # разговор «Спросить»
    assert AR.read_thread(ask / "утечка.md") == [], "история разговора прочитана по ссылке наружу"
    assert AR.threads(str(root)) == [], "список разговоров включил ссылку наружу"
    # шаблоны и зеркала
    assert AC.project_file("утечка", str(root)) == "", "project_file нашёл шаблон-ссылку наружу"
    assert not [p for p in AC._mirror_files(str(root)) if "утечка" in p]
    # линтер называет такую карточку, а не молчит
    cp = run("kb_lint.py", cwd=root)
    assert "Внешняя.md" in cp.stdout + cp.stderr and "за пределы проекта" in cp.stdout + cp.stderr, \
        f"линтер промолчал о карточке-ссылке:\n{cp.stdout}\n{cp.stderr}"
    assert secret not in cp.stdout + cp.stderr


def _linked_kb_fixture(tmp: Path):
    """Проект A, у которого `AuroraKnowledgeDB` и зеркало `Sources/Web` — ссылки на папки чужого
    проекта B (BL-NEW-01). → (корень A, секрет, корень B) или None без ссылок."""
    root = make_project(tmp)
    other = tmp / "проект-B"
    secret = "ТАЙНАЧУЖОЙБАЗЫ"
    (other / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    (other / "AuroraKnowledgeDB" / "Concepts" / "чужая.md").write_text(
        f'---\ntitle: "Чужая"\ntype: concept\nstatus: verified\n---\n\n# Чужая\n\n{secret} чужая.\n',
        encoding="utf-8")
    (other / "Web").mkdir()
    (other / "Web" / "страница.md").write_text("# Чужая страница\n\nid: 4242\n\n" + secret + "\n",
                                                encoding="utf-8")
    shutil.rmtree(root / "AuroraKnowledgeDB")
    web = root / "Sources" / "Web"
    shutil.rmtree(web, ignore_errors=True)
    if not (_link(other / "AuroraKnowledgeDB", root / "AuroraKnowledgeDB") and _link(other / "Web", web)):
        return None
    return root, secret, other


@test
def test_a_linked_root_directory_is_not_walked_by_any_reader(tmp: Path):
    """Каталог базы или зеркала — сам ссылка за пределы проекта: не читает никто (BL-NEW-01).

    `safe_walk` брал границу от `realpath(корня)`, то есть от чужого проекта, и отдавал все его
    файлы; читатели с прямым `open()` (`kb_embed`, `kb_moc`, `kb_graph`, `kb_fix`, `kb_supersede`,
    `kb_remap`) их читали, а `read_card_text` отказывал — граница получалась неодинаковой.
    """
    fx = _linked_kb_fixture(tmp)
    if not fx:
        return
    root, secret, other = fx
    import importlib

    def go():
        AC = importlib.import_module("aurora_common")
        assert list(AC.walk_md("AuroraKnowledgeDB")) == [], "walk_md отдал файлы чужой папки"
        assert list(AC.safe_walk("AuroraKnowledgeDB")) == [], "safe_walk зашёл в корень-ссылку"
        assert list(AC.safe_walk("AuroraKnowledgeDB/Concepts")) == [], "подкаталог корня-ссылки обойдён"
        assert AC.root_link_escapes("AuroraKnowledgeDB") and AC.root_link_escapes("Sources/Web")
        assert not AC.root_link_escapes("Sources"), "обычный каталог принят за ссылку наружу"
        assert AC.read_card_text("AuroraKnowledgeDB/Concepts/чужая.md") is None
        assert AC.load_cards("AuroraKnowledgeDB") == {}
        KE = importlib.import_module("kb_embed")
        assert secret not in " ".join(map(str, KE.card_texts("AuroraKnowledgeDB").values())), \
            "kb_embed взял текст чужой базы"
        MOC = importlib.import_module("kb_moc")
        assert "чужая" not in MOC.read_cards(), "kb_moc.read_cards прочитал чужую базу"
        KG = importlib.import_module("kb_graph")
        graph = KG.write_cards_graph(str(tmp / "graph.json"))
        dumped = (tmp / "graph.json").read_text(encoding="utf-8") + json.dumps(graph, ensure_ascii=False)
        assert "чужая" not in dumped and secret not in dumped, "граф карточек включил чужую базу"
        KF = importlib.import_module("kb_fix")
        assert not any("чужая" in k for k in KF.load_cards("AuroraKnowledgeDB")), \
            "kb_fix открыл чужую базу на правку"
        KS = importlib.import_module("kb_supersede")
        assert KS.find_card("чужая") is None, "kb_supersede нашёл карточку чужой базы"
        KR = importlib.import_module("kb_remap")
        assert KR.scan_disk("Sources/Web") == {}, "kb_remap.scan_disk прочитал чужое зеркало"
    _in_project(root, go)


@test
def test_linted_and_doctored_linked_root_gets_one_line_not_one_error_per_card(tmp: Path):
    """Хранилище-ссылка: линтер и доктор называют каталог одной строкой, храповик не падает (BL-NEW-01)."""
    fx = _linked_kb_fixture(tmp)
    if not fx:
        return
    root, secret, other = fx
    # несколько карточек в чужой базе: раньше на каждую шла своя ошибка
    for i in range(5):
        (other / "AuroraKnowledgeDB" / "Concepts" / f"ещё{i}.md").write_text(
            f'---\ntitle: "Ещё"\ntype: concept\nstatus: verified\n---\n\n# Ещё\n\n{secret}\n', encoding="utf-8")
    cp = run("kb_lint.py", cwd=root)
    out = cp.stdout + cp.stderr
    assert cp.returncode == 0, f"линтер упал на хранилище-ссылке:\n{out}"
    assert out.count("ссылка наружу проекта") == 1, f"ожидалась одна строка о каталоге:\n{out}"
    assert "ошибок 0" in out, "хуку pre-commit нужна итоговая строка"
    assert "ведёт за пределы проекта" not in out and secret not in out, out
    cp = run("kb_lint.py", "--summary", cwd=root)
    assert cp.returncode == 0 and "ошибок 0" in cp.stdout, (cp.returncode, cp.stdout, cp.stderr)

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    D = importlib.import_module("aurora_doctor")
    cwd = os.getcwd()
    os.chdir(root)
    try:
        warns = D.check_linked_roots()
    finally:
        os.chdir(cwd)
    assert len(warns) == 1 and "AuroraKnowledgeDB" in warns[0] and "Sources/Web" in warns[0], warns


@test
def test_a_linked_root_inside_the_project_still_reads_and_lints(tmp: Path):
    """Контроль: база-ссылка на папку ВНУТРИ проекта читается и линтится как обычная (BL-NEW-01)."""
    root = make_project(tmp)
    store = root / "хранилище"
    shutil.move(str(root / "AuroraKnowledgeDB"), str(store))
    if not _link(store, root / "AuroraKnowledgeDB"):
        return
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.", status="verified")
    import importlib

    def go():
        AC = importlib.import_module("aurora_common")
        assert not AC.root_link_escapes("AuroraKnowledgeDB")
        assert [p for p in AC.walk_md("AuroraKnowledgeDB") if p.endswith("Обеспечение.md")], \
            "база-ссылка внутри проекта перестала обходиться"
        assert "обеспечения" in (AC.read_card_text("AuroraKnowledgeDB/Concepts/Обеспечение.md") or "")
    _in_project(root, go)
    cp = run("kb_lint.py", cwd=root)
    assert "ссылка наружу проекта" not in cp.stdout + cp.stderr, cp.stdout + cp.stderr


@test
def test_agent_file_tools_read_ordinary_cards_and_code_named_like_service_folders(tmp: Path):
    """Инструменты чтения модели не отказывают обычным файлам с «служебными» именами (BL-NEW-01).

    После перехода на общий список `read_file` и `list_dir` закрывали карточку
    `AuroraKnowledgeDB/Glossary/Credentials.md` и любой путь с компонентом `state`, `runs`,
    `local` (`src/state/store.py`). Служебное — `local/`, `runs/`, `state/`, `.aurora/state` у
    корня проекта и `.git`, `.ssh`, `.aws` на любой глубине.
    """
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(SCRIPTS / "agents"))
    import importlib
    AD = importlib.import_module("pydantic_ai_adapter")
    AC = importlib.import_module("aurora_common")
    RC = importlib.import_module("request_context")
    root = tmp / "проект"
    files = {
        "AuroraKnowledgeDB/Glossary/Credentials.md": "учётные данные — термин",
        "AuroraKnowledgeDB/state/n.md": "карточка раздела state",
        "src/state/store.py": "x = 1",
        "src/runs/plan.md": "план прогонов",
        "docs/local/readme.md": "локально",
    }
    closed = {
        "local/models.json": "service", "runs/a.md": "service", "state/s.json": "service",
        ".aurora/state/p.json": "service", ".aurora/Runs/a.md": "service",
        ".opencode/state/p.json": "service", ".opencode/runs/a.md": "service",
        ".git/config": "service", "sub/.git/config": "service", "sub/.ssh/id": "service",
        "node_modules/x/a.js": "service", ".env.aurora.local": "secret", "sub/.env": "secret",
        "docs/credentials.json": "secret", "docs/.env.md": "secret", "mcp.json": "secret",
        "keys/server.pem": "secret",
    }
    for rel, text in {**files, **{k: "x" for k in closed}}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    msg = {"service": "служебная папка", "secret": "доступами"}
    for rel, text in files.items():
        got = AD.resolve_project_path(str(root), rel)
        assert Path(got).read_text(encoding="utf-8") == text, rel
    for rel, kind in closed.items():
        try:
            AD.resolve_project_path(str(root), rel)
        except ValueError as e:
            assert msg[kind] in str(e) or kind == "secret", f"{rel}: причина {e}"
        else:
            raise AssertionError(f"{rel}: закрытый путь прочитан")
    # инструменты целиком
    tools = {}

    class FakeAgent:
        def tool_plain(self, fn):
            tools[fn.__name__] = fn
            return fn

    AD.register_tools(FakeAgent(), [str(root)])
    # карточка базы — дословный чужой текст: читается данными, в ограждении (BL-SEC-06)
    assert tools["read_file"]("AuroraKnowledgeDB/Glossary/Credentials.md") == \
        '<untrusted source="AuroraKnowledgeDB/Glossary/Credentials.md">\nучётные данные — термин\n</untrusted>'
    assert tools["read_file"]("src/state/store.py") == "x = 1"
    assert tools["read_file"]("local/models.json").startswith("не прочитан")
    assert set(tools["list_dir"]("src").split("\n")) == {"state", "runs"}, "list_dir прячет обычные имена"
    assert tools["list_dir"]("AuroraKnowledgeDB/Glossary") == "Credentials.md"
    top = set(tools["list_dir"](".").split("\n"))
    assert {"src", "docs", "AuroraKnowledgeDB"} <= top, top
    assert not top & {"local", "runs", "state", ".git", ".env.aurora.local", "mcp.json"}, top
    # одна граница: ссылка на карточку в `state/` внутри проекта — не «побег»
    alias = root / "AuroraKnowledgeDB" / "Glossary" / "алиас.md"
    if _link(root / "AuroraKnowledgeDB" / "state" / "n.md", alias):
        assert not AC.link_escapes(str(alias), str(root)), "ссылка на карточку state/ принята за побег"
        assert AD.resolve_project_path(str(root), "AuroraKnowledgeDB/Glossary/алиас.md")
    # строгая мерка (задание по `@`) прежняя: карточка Credentials.md и state/ там закрыты
    assert RC.secret_kind("AuroraKnowledgeDB/Glossary/Credentials.md") == "secret"
    assert RC.secret_kind("src/state/store.py") == "service"
    assert RC.secret_kind("AuroraKnowledgeDB/Glossary/Credentials.md", strict=False) == ""
    assert RC.secret_kind("src/state/store.py", strict=False) == ""
    store = str(root / "src/state/store.py")
    assert AC.read_verdict(store, str(root), strict=True, every_file=True) == "service"
    assert AC.read_verdict(store, str(root), strict=False, every_file=True) == ""
    assert AC.read_verdict(str(root / ".." / "x.md"), str(root), every_file=True) == "outside"


@test
def test_one_read_boundary_function_serves_every_reader(tmp: Path):
    """Граница чтения одна: у адаптера и `@`-упоминаний нет своих копий правил (BL-NEW-01)."""
    ad = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    rc = (KIT / "scripts/request_context.py").read_text(encoding="utf-8")
    ac = (KIT / "scripts/aurora_common.py").read_text(encoding="utf-8")
    resolver = ad.split("def resolve_project_path")[1].split("def register_tools")[0]
    assert "read_verdict(" in resolver and "SECRET_NAME" not in resolver and "NEVER_DIRS" not in resolver, \
        "resolve_project_path снова держит свою копию правил"
    assert "read_verdict" in rc and "def _link_escapes" not in rc, "request_context держит свою проверку границы"
    # `_project_base` выбирает корень через `project_root_of`, а не своей копией
    base_fn = ac.split("def _project_base(")[1].split("\ndef ")[0]
    assert "project_root_of(" in base_fn and "_PROJECT_MARKERS" not in base_fn, \
        "_project_base снова дублирует выбор корня проекта"
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    f = root / "Templates" / "обычный.md"
    f.write_text("x", encoding="utf-8")
    assert AC.read_verdict(str(f), str(root)) == "" and AC.read_verdict(str(f), str(root), every_file=True) == ""
    assert AC.link_escapes(str(f)) is False


@test
def test_mcp_kb_card_does_not_read_a_linked_root_base(tmp: Path):
    """`kb_card` отказывает и когда сам каталог базы — ссылка на чужую папку (BL-NEW-01).

    `card_path` брал границу от `realpath(AuroraKnowledgeDB)`, то есть от чужой папки, и отдавал
    её карточку, тогда как `read_card_text` на том же файле отказывал.
    """
    fx = _linked_kb_fixture(tmp)
    if not fx:
        return
    root, secret, other = fx
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    M = importlib.import_module("aurora_mcp")
    assert M.card_path(str(root), "чужая") == "", "card_path нашёл карточку чужой базы"
    try:
        M.call_tool(str(root), "kb_card", {"name": "чужая"})
    except M.ToolError as e:
        assert "в базе нет" in str(e), str(e)
    else:
        raise AssertionError("kb_card отдал карточку чужой базы по корню-ссылке")


@test
def test_a_linked_root_with_marker_folders_is_not_walked_from_any_cwd(tmp: Path):
    """Корень-ссылка на чужую папку с `Sources/` внутри не обходится и вне проекта (BL-NEW-01).

    Папка-ссылка сама попадала в «корни проекта», потому что маркеры считались по её цели, и
    `root_link_escapes` возвращал False, когда рабочая папка не внутри проекта (MCP «все
    проекты», абсолютный путь).
    """
    root = make_project(tmp)
    other = tmp / "проект-B"
    (other / "vault" / "Concepts").mkdir(parents=True)
    (other / "vault" / "Sources").mkdir()
    (other / "vault" / "Concepts" / "чужая.md").write_text("# Чужая\n\nЧУЖОЕ\n", encoding="utf-8")
    shutil.rmtree(root / "AuroraKnowledgeDB")
    if not _link(other / "vault", root / "AuroraKnowledgeDB"):
        return
    elsewhere = tmp / "где-то"
    elsewhere.mkdir()
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    base = str(root / "AuroraKnowledgeDB")
    for cwd in (root, elsewhere):
        def go():
            assert AC.root_link_escapes(base), f"cwd={cwd.name}: корень-ссылка принят за свой"
            assert list(AC.walk_md(base)) == [], f"cwd={cwd.name}: обход отдал файлы чужой папки"
            assert AC.read_card_text(base + "/Concepts/чужая.md") is None
        _in_project(cwd, go)
    # контроль: обычная папка с маркерами проходит из любой рабочей папки
    own = root / "Sources"
    (own / "Web").mkdir(parents=True, exist_ok=True)
    assert not _in_project(elsewhere, lambda: AC.root_link_escapes(str(own / "Web")))


@test
def test_linted_linked_root_still_finds_secrets_in_skills_and_config(tmp: Path):
    """Корень-ссылка не отключает остальные проверки линтера: секреты ловятся по-прежнему (BL-NEW-01).

    Ранний `return 0` пропускал и поиск токена в навыках и в `aurora.config.yaml`: хук pre-commit
    получал «ошибок 0» и пускал коммит с паролем. Сама ссылка ошибкой не считается.
    """
    fx = _linked_kb_fixture(tmp)
    if not fx:
        return
    root, secret, other = fx
    cp = run("kb_lint.py", cwd=root)
    assert cp.returncode == 0 and "ссылка наружу проекта" in cp.stdout, (cp.returncode, cp.stdout, cp.stderr)
    with open(root / "aurora.config.yaml", "a", encoding="utf-8") as fh:
        fh.write("password: hunter2\n")
    skill = root / ".claude" / "skills" / "x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("ATLASSIAN_API_TOKEN=abc123\n", encoding="utf-8")
    cp = run("kb_lint.py", cwd=root)
    out = cp.stdout + cp.stderr
    assert cp.returncode == 1, f"секрет при корне-ссылке пропущен:\n{out}"
    assert "aurora.config.yaml: возможный секрет" in out and ".claude/skills/x/SKILL.md" in out, out
    assert out.count("ссылка наружу проекта") == 1 and "карточек 0, ошибок 2" in out, out
    assert secret not in out, out


@test
def test_project_opened_by_a_link_path_keeps_its_own_card_links(tmp: Path):
    """Проект открыт по пути-ссылке, рабочая папка вне него: ссылка на карточку внутри читается (BL-NEW-01).

    `project_root_of(lexical=True)` исключал из корней любую ссылку, и граница сужалась до папки
    файла: карточка-ссылка на соседнюю карточку проекта считалась побегом наружу. Ссылка наружу
    по-прежнему отказывает.
    """
    lab = tmp / "lab"
    proj = lab / "real" / "proj"
    for d in ("AuroraKnowledgeDB/Concepts", "AuroraKnowledgeDB/Glossary", "Sources"):
        (proj / d).mkdir(parents=True)
    (proj / "AuroraKnowledgeDB" / "Glossary" / "y.md").write_text("# Y\n\nСВОЁ\n", encoding="utf-8")
    (lab / "outside.md").write_text("# Снаружи\n\nЧУЖОЕ\n", encoding="utf-8")
    if not (_link("../Glossary/y.md", proj / "AuroraKnowledgeDB" / "Concepts" / "x.md")
            and _link("../../../outside.md", proj / "AuroraKnowledgeDB" / "Concepts" / "out.md")
            and _link(proj, lab / "projlink")):
        return
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    base = lab / "projlink" / "AuroraKnowledgeDB" / "Concepts"

    def go():
        assert not AC.link_escapes(str(base / "x.md")), "ссылка внутри проекта принята за побег"
        assert "СВОЁ" in (AC.read_card_text(str(base / "x.md")) or "")
        assert AC.link_escapes(str(base / "out.md")), "ссылка наружу проекта прошла"
        assert AC.read_card_text(str(base / "out.md")) is None
    _in_project(lab, go)


@test
def test_kb_reset_refuses_a_linked_root_and_leaves_the_foreign_base_alone(tmp: Path):
    """`kb:reset --apply --drop-unknown` при корне-ссылке отказывает кодом 2, чужая база цела (BL-NEW-01)."""
    fx = _linked_kb_fixture(tmp)
    if not fx:
        return
    root, secret, other = fx
    foreign = other / "AuroraKnowledgeDB" / "Concepts" / "чужая.md"
    for args in (("--apply", "--drop-unknown", "--allow-dirty"), ()):
        cp = run("kb_reset.py", *args, cwd=root)
        assert cp.returncode == 2, (args, cp.returncode, cp.stdout, cp.stderr)
        assert "ссылка наружу проекта" in cp.stderr, cp.stderr
        assert foreign.is_file(), f"kb_reset удалил карточку чужой базы ({args})"
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    SC = importlib.import_module("sources_core")
    (other / "Web" / "пусто").mkdir()
    assert _in_project(root, lambda: SC.drop_empty_dirs("Sources/Web")) == 0
    assert (other / "Web" / "пусто").is_dir(), "drop_empty_dirs почистил чужое зеркало"


@test
def test_request_context_engine_dirs_match_the_engine_constant(tmp: Path):
    """Мягкая мерка закрывает `state/runs/local` под теми же папками движка, что и сам движок (BL-NEW-01)."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    RC = importlib.import_module("request_context")
    AC = importlib.import_module("aurora_common")
    assert tuple(RC.ENGINE_DIRS) == tuple(AC.ENGINE_DIRS), (RC.ENGINE_DIRS, AC.ENGINE_DIRS)


@test
def test_real_inside_is_one_project_boundary_for_cockpit_and_request_context(tmp: Path):
    """`real_inside`/`real_rel` — одна граница проекта; `inside` панели и `@`-упоминаний идут через неё (BL-SEC-20)."""
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    AC = importlib.import_module("aurora_common")
    RC = importlib.import_module("request_context")
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    root = tmp / "Proj"
    (root / "Sources").mkdir(parents=True)
    (root / "Sources" / "a.md").write_text("x", encoding="utf-8")
    outside = tmp / "Proj-evil"
    outside.mkdir()
    (outside / "s.md").write_text("секрет", encoding="utf-8")
    rp = os.path.realpath(root)
    want = os.path.join(rp, "Sources", "a.md")
    # одна функция на оба потребителя: те же ответы, что у границы
    for fn in (AC.real_inside, ck.inside, RC.inside):
        assert fn(str(root), "Sources/a.md") == want, fn
        assert fn(str(root), "/Sources/a.md") == want and fn(str(root), "\\Sources/a.md") == want, fn
        assert fn(str(root), "") == rp and fn(str(root), "Sources/..") == rp, fn
        # несуществующий хвост — не ложный отказ (файл создают, а не читают)
        assert fn(str(root), "Artifacts/new/x.md") == os.path.join(rp, "Artifacts", "new", "x.md"), fn
        # `..` и сосед с общей приставкой имени корня — наружу
        assert fn(str(root), "../Proj-evil/s.md") == "" and fn(str(root), "Sources/../../Proj-evil") == "", fn
        assert fn(str(root), "..") == "" and fn(str(root), "../../etc/passwd") == "", fn
    assert "real_inside(" in (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8").split("def inside(")[1] \
        .split("\ndef ")[0], "inside панели снова держит свою копию границы"
    # ссылка наружу и корень-ссылка
    try:
        os.symlink(outside, root / "Sources" / "out", target_is_directory=True)
        link_root = tmp / "link-root"
        os.symlink(root, link_root, target_is_directory=True)
    except OSError:
        link_root = None   # Windows без прав на ссылки: остальное проверено выше
    if link_root is not None:
        for fn in (AC.real_inside, ck.inside, RC.inside):
            assert fn(str(root), "Sources/out/s.md") == "", f"{fn}: ссылка наружу открыла выход"
            assert fn(str(link_root), "Sources/a.md") == want, f"{fn}: корень-ссылка не разрешён до сравнения"
        assert AC.real_inside(str(link_root), "Sources/out/s.md") == ""
    # real_rel: прямые слэши от realpath корня; вне корня и корень сам — пусто
    assert AC.real_rel(str(root), want) == "Sources/a.md"
    assert AC.real_rel(str(root), rp) == "" and AC.real_rel(str(root), str(outside / "s.md")) == ""
    assert AC.real_rel(str(root), str(root / "Artifacts" / "new" / "x.md")) == "Artifacts/new/x.md"
    # регистр: на Windows/macOS без учёта регистра корень — один; выхода это не открывает
    real_nc = os.path.normcase
    try:
        os.path.normcase = str.lower
        up = os.path.join(os.path.dirname(rp), "PROJ", "Sources", "a.md")
        assert AC.real_inside(rp.upper(), "Sources/a.md").lower() == want.lower(), "регистр корня дал ложный отказ"
        assert AC.real_inside(str(root), "../PROJ/Sources/a.md").lower() == want.lower()
        assert AC.real_rel(str(root), up) == "Sources/a.md", "real_rel не нашёл путь, записанный другим регистром"
        assert AC.real_inside(str(root), "../PROJ-EVIL/s.md") == "", "регистр открыл соседа с общей приставкой"
        assert AC.real_inside(str(root), "..") == "" and AC.real_rel(str(root), str(outside / "s.md")) == ""
        if link_root is not None:
            assert AC.real_inside(str(root), "SOURCES/out/s.md") == "" or \
                not os.path.exists(os.path.join(rp, "SOURCES", "out")), "регистр обошёл ссылку наружу"
    finally:
        os.path.normcase = real_nc


@test
def test_secret_names_are_closed_in_every_windows_spelling(tmp: Path):
    """Имя секрета закрыто в записях Windows (хвостовые точки и пробелы, поток NTFS, регистр, обе черты) и по расширенному списку (BL-SEC-20)."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    RC = importlib.import_module("request_context")
    # (путь, мерка строгая, мерка чтения проекта): "" — открыт
    closed = [
        ("id_rsa.", "secret", "secret"), ("id_rsa", "secret", "secret"), ("keys/ID_RSA. .", "secret", "secret"),
        (".env::$DATA", "secret", "secret"), (".env:stream", "secret", "secret"), (".ENV ", "secret", "secret"),
        (".Env.Local.", "secret", "secret"), ("a/server.PEM.", "secret", "secret"),
        ("id_dsa", "secret", "secret"), ("id_dsa_backup", "secret", "secret"),
        (".pgpass", "secret", "secret"), (".dockercfg", "secret", "secret"), ("_netrc", "secret", "secret"),
        ("vault/Base.KDBX", "secret", "secret"), ("putty/key.ppk", "secret", "secret"),
        ("secrets", "secret", "secret"), ("config/secrets.yaml", "secret", "secret"),
        ("Secrets.JSON", "secret", "secret"), ("secrets.prod.json", "secret", "secret"),
        # папки: регистр, точка/пробел и поток на каталоге, обратная черта
        (".GIT/config", "service", "service"), (".git./config", "service", "service"),
        (".git ::$INDEX_ALLOCATION/config", "service", "service"),
        ("a\\.ssh\\id_x", "service", "service"), (".aws\\credentials", "service", "secret"),
        ("sub/Node_Modules/x.js", "service", "service"), ("Local/models.json", "service", "service"),
    ]
    for rel, strict, soft in closed:
        assert RC.secret_kind(rel) == strict, (rel, "строгая", RC.secret_kind(rel))
        assert RC.secret_kind(rel, strict=False) == soft, (rel, "чтения", RC.secret_kind(rel, strict=False))
    # карточка `.md` в мягкой мерке открыта, `.env*.md` закрыт по-прежнему
    for rel in ("Glossary/Credentials.md", "Glossary/Credentials.md.", "Glossary/Secrets.md", "Glossary/secrets.md::$DATA"):
        assert RC.secret_kind(rel, strict=False) == "", rel
        assert RC.secret_kind(rel) == "secret", rel
    assert RC.secret_kind("Glossary/.env.md", strict=False) == "secret"
    # похожие, но обычные имена не закрываются
    for rel in ("id_rsa.pub", "secretsx.py", "my-secrets-guide.md", "src/idea_rsa.txt", "env.md", "keyboard.txt",
                "docs/pgpass.md", "notes/dockercfg.txt", "a.keys", "src/state/store.py", "Sources/key.md"):
        soft = RC.secret_kind(rel, strict=False)
        assert soft == "", (rel, soft)
        assert RC.secret_kind(rel) in ("", "service"), rel
    for rel in ("id_rsa.pub", "secretsx.py", "my-secrets-guide.md", "src/idea_rsa.txt", "keyboard.txt", "a.keys"):
        assert RC.secret_kind(rel) == "", rel
    assert RC.secret_kind("") == "" and RC.secret_kind(".") == "" and RC.secret_kind("./") == ""
    assert RC.is_secret(".env::$DATA") and not RC.is_secret("id_rsa.pub")
    # на POSIX и macOS «:» — обычная часть имени: срез по первому «:» не должен открывать ключ
    # (`prod:2026.pem`, `key:1.key`, `host:prod.key`, `cert:443.pem`, `a:b.p12` были закрыты и обязаны остаться)
    for rel in ("prod:2026.pem", "keys/key:1.key", "host:prod.key", "a/cert:443.pem", "a:b.p12", "X:Y.PEM."):
        assert RC.secret_kind(rel) == "secret", (rel, "строгая", RC.secret_kind(rel))
        assert RC.secret_kind(rel, strict=False) == "secret", (rel, "чтения", RC.secret_kind(rel, strict=False))
    assert RC.secret_kind("prod:2026.pem") == "secret" and RC.secret_kind("prod:2026.pem", strict=False) == "secret"
    assert RC.secret_kind("a:b.p12") == "secret" and RC.secret_kind("sub/cert:443.pem", strict=False) == "secret"
    assert RC.is_secret("host:prod.key") and RC.is_secret("key:1.key", strict=False)
    # карточка `.md` с двоеточием по-прежнему открыта в мягкой мерке, а не-секреты с «:» не закрываются
    assert RC.secret_kind("Glossary/key:1.md", strict=False) == "" and RC.secret_kind("notes/a:b.txt") == ""
    assert RC.name_forms("prod:2026.pem.") == ("prod", "prod:2026.pem")
    # имя с ведущим «:» на POSIX — обычное имя ключа: пустая часть до «:» не должна выбрасывать компоненту
    for rel in (":x.pem", "a/:secret.key", "keys/:k.key", "foo/.:x.pem", ":.key"):
        assert RC.secret_kind(rel) == "secret", (rel, "строгая", RC.secret_kind(rel))
        assert RC.secret_kind(rel, strict=False) == "secret", (rel, "чтения", RC.secret_kind(rel, strict=False))
    assert RC.secret_kind("./", strict=False) == "" and RC.secret_kind("a/./b.txt") == "" and RC.secret_kind(":a.txt") == ""


@test
def test_file_create_checks_the_real_path_through_a_folder_link(tmp: Path):
    """`file_create` смотрит и реальный путь: ссылка-папка в разрешённом месте не выводит создание в Sources/ и базу знаний (BL-SEC-20)."""
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    root = tmp / "Proj"
    for d in ("Workspaces", "Sources/Confluence", "AuroraKnowledgeDB/Cards", "AuroraKnowledgeDB/Decisions", "Raw/decisions", "Artifacts"):
        (root / d).mkdir(parents=True)
    try:
        os.symlink(root / "Sources" / "Confluence", root / "Workspaces" / "lnk", target_is_directory=True)
        os.symlink(root / "AuroraKnowledgeDB" / "Cards", root / "Workspaces" / "kb", target_is_directory=True)
        os.symlink(root / "AuroraKnowledgeDB" / "Decisions", root / "Workspaces" / "mirror", target_is_directory=True)
        os.symlink(root / "Raw" / "decisions", root / "Workspaces" / "dec", target_is_directory=True)
        # места, которые не «только чтение», но и не из разрешённых для создания: их отказывает
        # именно `why_no_create_real` в `file_create` (а не `why_readonly_real`)
        os.symlink(root / "Artifacts", root / "Workspaces" / "art", target_is_directory=True)
        os.symlink(root, root / "Workspaces" / "rootlnk", target_is_directory=True)
    except OSError:
        return          # Windows без прав на ссылки: поведение без ссылок проверено в других тестах
    for rel, folder in (("Workspaces/lnk/new.md", "Sources/Confluence"), ("Workspaces/kb/new.md", "AuroraKnowledgeDB/Cards"),
                        ("Workspaces/mirror/new.md", "AuroraKnowledgeDB/Decisions"),
                        ("Workspaces/art/new.md", "Artifacts"), ("Workspaces/rootlnk/new.md", ".")):
        res = ck.file_create(str(root), rel, "x")
        assert res.get("error") and not res.get("ok"), (rel, res)
        assert not (root / folder / "new.md").exists(), f"{rel}: файл создан в {folder}"
        # вход записи даёт тот же отказ: входы не расходятся
        assert ck.file_write(str(root), rel, "x").get("error"), rel
        assert not (root / folder / "new.md").exists()
    # Artifacts/ и корень — не «только чтение»: отказ даёт именно `why_no_create_real` (вызов в `file_create`)
    for rel in ("Workspaces/art/new.md", "Workspaces/rootlnk/new.md"):
        full = ck.inside(str(root), rel)
        assert not ck.why_readonly_real(str(root), rel, full), rel
        assert ck.why_no_create_real(str(root), rel, full), rel
    # законная ссылка — на место, где создавать можно (мастер-файлы решений в Raw/decisions), — не ломается
    assert ck.file_create(str(root), "Workspaces/dec/ok.md", "x").get("ok")
    assert (root / "Raw" / "decisions" / "ok.md").is_file()
    assert ck.file_create(str(root), "Workspaces/обычный.md", "x").get("ok")
    assert ck.why_no_create_real(str(root), "Workspaces/lnk/n.md", os.path.join(os.path.realpath(root), "Sources", "Confluence", "n.md"))


@test
def test_read_verdict_compares_the_boundary_through_normcase(tmp: Path):
    """`read_verdict` сверяет корень и цель через `normcase`: регистр диска границу не ломает и не открывает (BL-SEC-20)."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    root = tmp / "Proj"
    (root / "Sources").mkdir(parents=True)
    ok = root / "Sources" / "a.md"
    ok.write_text("x", encoding="utf-8")
    key = root / "Sources" / "k.pem"
    key.write_text("k", encoding="utf-8")
    evil = tmp / "Proj-evil"
    evil.mkdir()
    (evil / "s.md").write_text("чужой", encoding="utf-8")
    rp = os.path.realpath(root)
    ok, key, evil = (Path(rp) / "Sources" / "a.md", Path(rp) / "Sources" / "k.pem", Path(os.path.realpath(evil)))
    # 8.3-имена (`PROGRA~1`) раскрывает `os.path.realpath` (Windows, Python 3.8+) ещё до сравнения;
    # здесь проверяется только нормализация регистра — на подставленных строках, без записи в HOME
    real_nc = os.path.normcase
    try:
        os.path.normcase = str.lower        # как на Windows и в macOS без учёта регистра
        up = rp.upper()
        assert AC.read_verdict(str(ok), up, every_file=True) == "", "регистр корня дал ложный отказ"
        assert AC.read_verdict(str(ok), up, strict=True, every_file=True) == ""
        assert AC.read_verdict(str(key), up, every_file=True) == "secret", "регистр корня открыл секрет"
        assert AC.read_verdict(str(evil / "s.md"), up, every_file=True) == "outside", "регистр открыл соседа с общей приставкой"
        assert AC.read_verdict(str(Path(rp) / ".." / "x.md"), up, every_file=True) == "outside"
    finally:
        os.path.normcase = real_nc
    # без подмены (регистрозависимая ФС) прежнее поведение: чужой регистр — другой корень
    if os.path.normcase("A") == "A" and not os.path.exists(rp.upper()):
        assert AC.read_verdict(str(ok), rp.upper(), every_file=True) == "outside"
    assert AC.read_verdict(str(ok), rp, every_file=True) == ""


@test
def test_kb_lint_gate_on_a_linked_base_is_code_2_not_a_pass(tmp: Path):
    """kb_lint --gate при базе-ссылке за пределы проекта — код 2 и без вердикта, а не «карточек 0 — pass».

    Без `--gate` код прежний (0, одна строка о ссылке): храповик и pre-commit на таком проекте не падают.
    """
    fx = _linked_kb_fixture(tmp)
    if not fx:
        return
    root, secret, other = fx
    cp = run("kb_lint.py", "--gate", cwd=root, expect_rc=2)
    assert "AURORA-GATE" not in cp.stdout and "ссылка наружу проекта" in cp.stdout, cp.stdout
    run("kb_lint.py", "--gate=warn", cwd=root, expect_rc=2)
    run("kb_lint.py", cwd=root, expect_rc=0)
