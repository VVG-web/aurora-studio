"""Проверки движка Aurora, часть 3 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import sys

from harness import (  # noqa: F401
    ui_source,
    KIT,
    SCRIPTS,
    _js_function,
    card,
    make_project,
    panel_sources,
    run,
    stub_messages,
    test,
    why,
)


@test
def test_mcp_is_declared_by_the_project_not_guessed(tmp: Path):
    """MCP-серверы объявляет проект, а не панель угадывает по чужой конфигурации.

    Соблазн — прочитать настройку Claude Code или Cursor и предложить оттуда. Она
    меняется без нашего ведома, и панель начала бы врать о том, что доступно: ровно та
    догадка вместо факта, которая уже дважды дорого обошлась.

    Форма конфига — стандартная (`{"mcpServers": {...}}`). Своя заставила бы человека
    держать две конфигурации об одном и том же.

    MCP нужен только там, где движок чего-то не умеет сам: публикация и заведение задач
    работают и без него. Файла нет — серверов нет, и это норма, а не недонастройка.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A

    root = tmp / "проект"
    root.mkdir()
    assert A.mcp_config(str(root)) == {}, "без файла движок что-то придумал"

    (root / "mcp.json").write_text(json.dumps({
        "mcpServers": {"atlassian": {"command": "echo", "args": ["x"],
                                     "about": "Confluence и Jira"}}},
        ensure_ascii=False), encoding="utf-8")
    cfg = A.mcp_config(str(root))
    assert list(cfg["mcpServers"]) == ["atlassian"], f"конфиг не прочитан: {cfg}"

    (root / "mcp.json").write_text("{ это не json", encoding="utf-8")
    assert A.mcp_config(str(root)) == {}, "битый конфиг уронил чтение вместо тишины"

    ad = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert "def mcp_toolsets(" in ad, "адаптер не умеет подключать MCP"
    assert "MCPToolset(Client(one)" in ad, "серверы подключаются не стандартной формой"
    # Toolset строится по серверу, а не один на всех: сторож на исходящее привязан к
    # конкретному серверу, и узнать, чей это вызов, можно только так.
    assert "for name, spec in servers.items():" in ad, \
        "все серверы в одном toolset — сторож не поймёт, чей запрос уходит наружу"
    block = ad.split("def mcp_toolsets(")[1].split("def outbound_hook(")[0]
    assert "except Exception:  # noqa" in block, \
        "неподнятый сервер уронит прогон — а он может быть просто выключен"

    # и это видно человеку: настроил или нет
    ui = panel_sources()
    assert "У проекта своих серверов нет" in ui and "не объявлены" in ui, \
        "панель молчит про MCP — человек не узнает ни что подключено, ни что это норма"


@test
def test_the_panel_never_stores_mcp_secrets(tmp: Path):
    """Секреты MCP в файл проекта не попадают: он уезжает в git проекта.

    Токены держит машина (`<кит>/local/mcp.json`): одноимённый сервер кита отдаёт проекту
    свой `env`, а проект перекрывает остальные поля. Секрет, уже лежащий в файле проекта
    (положили руками), браузеру приходит маской и переживает запись как есть. Правка
    серверов проекта — те же карточки, что у кита (решение пользователя 29.09.2026).
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "проект"
    root.mkdir()
    act = lambda payload: ck.mcp_action(payload, str(root))
    save = lambda name, spec, was="": act({"action": "save_server", "name": name,
                                          "rename_from": was, "spec": spec})

    # Новый сервер: стандартная форма на диск, бэкапа до первой записи нет
    r = save("atlassian", {"command": "npx", "args": ["-y", "mcp-atlassian"]})
    assert r.get("ok") is True, f"обычный сервер не записан: {r}"
    data = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    srv = data["mcpServers"]["atlassian"]
    assert srv == {"command": "npx", "args": ["-y", "mcp-atlassian"]}, f"не стандартная форма: {srv}"
    assert not (root / "mcp.json.bak").exists(), "бэкап до первой записи — пустая форма"

    # Вторая запись: прежняя версия остаётся рядом как .bak
    old = (root / "mcp.json").read_text(encoding="utf-8")
    assert save("atlassian", {"command": "npx", "args": ["-y", "mcp-atlassian", "--x"]},
                "atlassian").get("ok")
    assert (root / "mcp.json.bak").read_text(encoding="utf-8") == old, \
        "бэкап не сохранил прежнюю версию конфига"

    # Новый секрет в файл проекта не проходит ни карточкой, ни вставкой, ни файлом целиком
    why = save("atlassian", {"command": "npx", "env": {"TOKEN": "секрет"}}, "atlassian")
    assert "Настройке кита" in why.get("error", ""), f"панель приняла секрет в проект: {why}"
    why = act({"action": "import", "text": '{"gh": {"command": "npx", "env": {"T": "x1"}}}'})
    assert "Настройке кита" in why.get("error", ""), f"вставка положила секрет в проект: {why}"
    why = act({"action": "save_raw",
               "text": '{"mcpServers": {"w": {"url": "https://x.example/mcp", "headers": {"A": "b"}}}}'})
    assert "Настройке кита" in why.get("error", ""), f"файл целиком положил секрет: {why}"
    assert "секрет" not in (root / "mcp.json").read_text(encoding="utf-8")

    # Секрет, положенный в файл руками, браузеру — маской, а запись его не теряет
    cur = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    cur["mcpServers"]["atlassian"]["env"] = {"TOKEN": "секрет"}
    (root / "mcp.json").write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    st = ck.mcp_state(str(root))
    assert "секрет" not in json.dumps(st, ensure_ascii=False), "значение env ушло в браузер"
    spec = st["servers"]["atlassian"]
    assert spec["env"] == {"TOKEN": st["mask"]}, spec
    spec["args"] = ["-y", "mcp-atlassian"]
    assert save("atlassian", spec, "atlassian").get("ok")
    cur = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    assert cur["mcpServers"]["atlassian"].get("env") == {"TOKEN": "секрет"}, \
        "запись стёрла env, положенный руками, — токены человека потеряны"

    # Прочие кривые формы тоже не доходят до диска
    assert "имя" in save("", {"command": "npx"})["error"]
    assert "command" in save("x", {"args": []})["error"]
    assert "args" in save("x", {"command": "npx", "args": [1]})["error"]

    # Битый файл: правка карточкой не молчит, файл целиком чинит, прежний — в бэкапе
    (root / "mcp.json").write_text("{ это не json", encoding="utf-8")
    assert "Весь конфиг" in save("atlassian", {"command": "npx"})["error"]
    assert act({"action": "save_raw", "text": '{"mcpServers": {"atlassian": {"command": "npx"}}}'}).get("ok")
    assert json.loads((root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["atlassian"]["command"] == "npx"
    assert "{ это не json" in (root / "mcp.json.bak").read_text(encoding="utf-8"), \
        "битая версия не сохранена в бэкапе"

    # Раздел проекта ходит в тот же маршрут, что и прежде, — только карточками
    ui = panel_sources()
    assert '"/api/mcp?project="' in ui and '"/api/mcp" : "/api/mcp/kit"' in ui, \
        "раздел проекта не ходит через /api/mcp"
    assert "Токены (env, headers) в проекте не хранятся" in ui, \
        "человек не видит, где живут токены серверов проекта"


@test
def test_project_shows_machine_mcp_servers_read_only(tmp: Path):
    """Серверы машины видны в настройке проекта, но правятся только в «Настройке кита».

    Как кольцо шлюзов: машинное работает в каждом проекте и видно в нём, а меняется одним
    местом (решение пользователя 29.09.2026). В проекте два вида карточек: машинные — только
    для чтения, проектные — с правкой. Одноимённый сервер проекта перекрывает машинный.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    kitdir = tmp / "кит"
    (kitdir / "local").mkdir(parents=True)
    (kitdir / "local" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "gh-mcp"], "env": {"GH_TOKEN": "ghp-секрет"}},
        "graph": {"command": "python3", "args": ["-m", "graph"]}}}, ensure_ascii=False),
        encoding="utf-8")
    proj = tmp / "проект"
    proj.mkdir()
    saved_file = ck.kit_mcp_file
    ck.kit_mcp_file = lambda: str(kitdir / "local" / "mcp.json")
    try:
        st = ck.mcp_state(str(proj))
        assert sorted(st["kit"]) == ["github", "graph"] and st["servers"] == {}, st
        assert "ghp-секрет" not in json.dumps(st, ensure_ascii=False), "токен машины ушёл в проект"
        assert st["kit"]["github"]["env"] == {"GH_TOKEN": st["mask"]}
        # перекрыть в проекте: то же имя, без секретов — токен остаётся машинный
        r = ck.mcp_action({"action": "save_server", "name": "github", "rename_from": "",
                           "spec": {"command": "npx", "args": ["-y", "gh-mcp", "--read-only"]}},
                          str(proj))
        assert r.get("ok"), r
        assert "github" in ck.mcp_state(str(proj))["servers"]
        # файл машины проектом не тронут
        kit = json.loads((kitdir / "local" / "mcp.json").read_text(encoding="utf-8"))
        assert kit["mcpServers"]["github"]["args"] == ["-y", "gh-mcp"], "проект переписал сервер машины"
    finally:
        ck.kit_mcp_file = saved_file

    sys.path.insert(0, str(KIT / "scripts"))
    A = importlib.import_module("agent_core")
    merged = A.mcp_config(str(proj), kit=str(kitdir))["mcpServers"]
    assert merged["github"]["args"][-1] == "--read-only" and merged["github"]["env"] == {
        "GH_TOKEN": "ghp-секрет"}, f"прогон не получил поля проекта с токеном машины: {merged}"
    assert "graph" in merged, "сервер машины не дошёл до прогона проекта"

    ui = panel_sources()
    render = _js_function(ui, "async function renderMcp(")
    assert 'mcpCard("kit-ro"' in render and 't("mcp.readonly")' in render, \
        "серверы машины в проекте не показаны или показаны с правкой"
    card = _js_function(ui, "function openMcpCard(")
    assert "n.disabled = true" in card and 'openMcpCard("project", name, base)' in card, \
        "сервер машины из проекта правится — или его нельзя перекрыть проектом"
    assert "/api/mcp/probe?project=" in render, "в проекте нечем проверить, работают ли серверы"
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'elif u.path == "/api/mcp/probe":' in src and "AC.mcp_probe(project)" in src
    ad = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert '"--mcp-probe" in sys.argv' in ad and "await client.list_tools()" in ad, \
        "проверка не спрашивает у сервера его инструменты"


@test
def test_routes_build_the_index_after_the_graph_notes(tmp: Path):
    """Оглавления собираются после выгрузки графа: она заводит и снимает заметки тем.

    Прогон PRJ-A 29.09.2026: «Обновить базу» кончился девятью ошибками линтера — темы графа
    переименовались (27 → 24) уже после `kb:index`, и оглавление `MOC/_index.md` называло
    заметки, которых нет.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    seen = 0
    for sc in ck.scenarios():
        cmds = [st.get("cmd") for st in sc["steps"]]
        if "kb:graph-export" in cmds and "kb:index" in cmds:
            seen += 1
            last_index = len(cmds) - 1 - cmds[::-1].index("kb:index")
            assert last_index > cmds.index("kb:graph-export"), \
                f"маршрут {sc['id']}: оглавления собираются до выгрузки графа"
    assert seen >= 3, "маршруты без выгрузки графа — проверять нечего"


@test
def test_settings_groups_fold_like_quickstart_routes(tmp: Path):
    """Части «Настройки кита» и «Настроек проекта» сворачиваются, как маршруты «Быстрого старта».

    Страницы длинные: одно кольцо шлюзов — несколько экранов, и то, что под ним, человек не
    находил. Каждая часть — группа с «+/−»; что раскрыто, помнит браузер (решение
    пользователя 29.09.2026). Память — только удобство: без неё страница рисуется так же.
    """
    ui = panel_sources()
    sg = _js_function(ui, "function sgroup(")
    assert '"−" : "+"' in sg and "body.hidden = !open" in sg and '"aria-expanded"' in sg, \
        "группа не сворачивается или не говорит, свёрнута ли"
    mem = _js_function(ui, "function sgroupsOpen(") + _js_function(ui, "function sgroupRemember(")
    assert mem.count("try") == 2 and "localStorage" in mem, \
        "память групп без защиты: закрытое хранилище оборвёт отрисовку настроек"
    setup = _js_function(ui, "async function renderSetup(")
    for gid in ('"setup:roots"', '"setup:new"', '"setup:mcp"'):
        assert gid in setup, f"в «Настройке кита» нет группы {gid}"
    project = _js_function(ui, "async function renderProject(")
    for gid in ('"project:form"', '"project:tokens"', '"project:mcp"', '"project:yaml"'):
        assert gid in project, f"в «Настройках проекта» нет группы {gid}"
    agent = _js_function(ui, "async function renderAgentCard(")
    assert '"project:agent" : "setup:agent"' in agent, "кольцо шлюзов не свёрнуто в группу"
    assert '"project:kinds"' in _js_function(ui, "async function renderKinds("), \
        "виды артефактов не свёрнуты в группу"
    for page in (setup, project):
        assert 'box.append(el("h2"' not in page, "часть страницы осталась без группы"
    jump = _js_function(ui, "function drawSetupJump(")
    assert "g.sgSet(true)" in jump and 't("sgroup.all_open")' in jump, \
        "переход к части страницы не раскрывает её — или нельзя раскрыть всё разом"
    # несохранённое в свёрнутой группе видно на её заголовке
    assert 'class:"unsaved-badge"' in sg, "свёрнутая группа прячет несохранённую правку"
    assert "sgroupsMarkDirty();" in _js_function(ui, "function setDirty("), \
        "пометка на заголовке группы не следит за несохранённым"


@test
def test_repair_takes_what_ops_todo_used_to_leave_to_a_human(tmp: Path):
    """То, что «Что осталось человеку» звало его решением, берёт ремонт.

    PRJ-C 29.09.2026, после «Починить базу»:
    - двенадцать «артефактов в базе»: пустышки под кодом с приставкой проекта
      («RU.PRJ.US-3.2.5») и под кодом с названием («US-4.2.1 Создание черновика») —
      линтер видел их своим правилом, ремонт искал по голому коду и не брал;
    - эпик и история с именем предмета, которое уже занято, — «это слияние, человеку»,
      хотя по правилу базы совпало имя — совпала сущность;
    - папка раздела в самой себе (`Glossary/Glossary`) — линтер находил, ремонт не поднимал;
    - переименование клало прежнее имя в синонимы, а сверка шапки в том же прогоне
      снимала его как «повтор имени файла» — ссылки на прежнее имя рвались.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    stub = ('---\ntitle: "{t}"\naliases: []\nstatus: draft\ntype: concept\ntags: [заготовка]\n'
            '---\n\n# {t}\n\n_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n')
    for name in ("RU.PRJ.US-3.2.5", "US-4.2.1 Создание черновика заявки"):
        (kb / f"Concepts/{name}.md").write_text(stub.format(t=name), encoding="utf-8")
    card(root, "Concepts/Формирование-заявки.md", "Заявка формируется в личном кабинете.",
         status="knowledge")
    (kb / "Concepts/Epic-4.2-Формирование-заявки.md").write_text(
        '---\ntitle: "Epic-4.2-Формирование-заявки"\naliases: ["Epic-4.2"]\nstatus: draft\n'
        'type: concept\n---\n\nЭпик: заявку подписывают КЭП.\n', encoding="utf-8")
    (kb / "Concepts/Epic-5.1-Приём-платежей.md").write_text(
        '---\ntitle: "Epic-5.1-Приём-платежей"\naliases: ["Приём-платежей", "Epic-5.1"]\n'
        'status: draft\ntype: concept\n---\n\nПлатежи принимаются по QR.\n', encoding="utf-8")
    card(root, "Processes/Оплата.md", "См. [[Epic-5.1-Приём-платежей]] и [[Epic-4.2-Формирование-заявки]].",
         status="knowledge")
    (kb / "Glossary/Glossary").mkdir(parents=True)
    (kb / "Glossary/Glossary/Единицы-измерения.md").write_text(
        '---\ntitle: "Единицы измерения"\nstatus: draft\ntype: glossary\n---\n\nШтука, метр.\n',
        encoding="utf-8")
    cp = run("kb_fix.py", "--all", "--drop-code-stubs", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1), cp.stdout[-600:] + cp.stderr[-400:]
    live = {p.stem: p for p in kb.rglob("*.md") if "_archive" not in p.parts}
    for gone in ("RU.PRJ.US-3.2.5", "US-4.2.1 Создание черновика заявки"):
        assert gone not in live, f"пустышка под кодом артефакта осталась в базе: {gone}"
    assert "Epic-4.2-Формирование-заявки" not in live, "эпик с занятым именем предмета не слит"
    merged = live["Формирование-заявки"].read_text(encoding="utf-8")
    assert "подписывают КЭП" in merged and "Epic-4.2" in A.aliases(merged), merged[:600]
    assert "Приём-платежей" in live and "Epic-5.1-Приём-платежей" in A.aliases(
        live["Приём-платежей"].read_text(encoding="utf-8")), \
        "прежнее имя снято из синонимов — ссылки на него оборвутся"
    assert not (kb / "Glossary/Glossary").exists() and (kb / "Glossary/Единицы-измерения.md").is_file(), \
        ("папка раздела в самой себе не поднята: "
         + str(sorted(str(p.relative_to(kb)) for p in (kb / "Glossary").rglob("*")))
         + "\n" + cp.stdout[-1500:])
    lint = run("kb_lint.py", "--full", cwd=root).stdout
    for bad in ("артефакт в знаниях", "вложена сама в себя", "Epic-5.1-Приём-платежей]]"):
        assert bad not in lint, f"после ремонта осталось: {bad}\n{lint[-800:]}"


@test
def test_lint_expansions_check_only_what_the_model_wrote(tmp: Path):
    """Расшифровку сверяют только в тексте карточки — не в дословном источнике и не в архиве.

    PRJ-C 29.09.2026: «ПДО» звалось выдуманной расшифровкой дважды — строкой источника,
    перенесённой дословно, и той же строкой в заархивированной карточке. Правило ловит
    выдумки модели; слово источника выдумкой не бывает.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    card(root, "Reference/Сокращения.md",
         "| Сокращение | Расшифровка |\n|---|---|\n| ПДО | подтверждение даты отправки |\n",
         status="knowledge")
    text = ("Квитанция содержит подтверждение даты отправки.\n\n" + R.QUOTES +
            "\nКвитанция с идентификатором пакета, датой и временем приема (ПДО), извещением.\n")
    card(root, "Requirements/Квитанция.md", text, status="draft")
    (root / "AuroraKnowledgeDB/_archive").mkdir(parents=True, exist_ok=True)
    (root / "AuroraKnowledgeDB/_archive/Старая.md").write_text(
        "---\ntitle: Старая\n---\n\nПакет с датой и временем приема (ПДО).\n", encoding="utf-8")
    out = run("kb_lint.py", "--full", cwd=root).stdout
    assert "«ПДО» расшифровано" not in out, out[-600:]
    card(root, "Requirements/Выдумка.md", "Пакет, датой и временем приема (ПДО) подписан.",
         status="draft")
    assert "«ПДО» расшифровано" in run("kb_lint.py", "--full", cwd=root).stdout, \
        "выдумка модели в тексте карточки больше не ловится"


@test
def test_schemes_of_a_vanished_page_leave_the_mirror(tmp: Path):
    """Схемы страницы, которой в зеркале больше нет, находят и убирают синк, аудит и `kb_names`.

    PRJ-B 29.09.2026: от раскладки 12.08 осталась папка с тремя `_assets/` и ни одной
    страницы — 36 файлов в git. Путь одного, 205 знаков, doctor звал чинить `kb_names`, а тот
    ничего не находил: правило «схемы принадлежат зеркалу» не спрашивало, есть ли страница.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    SC = importlib.import_module("sources_core")
    root = make_project(tmp)
    m = root / "Sources/Confluence"
    (m / "Живая_assets").mkdir(parents=True)
    (m / "Живая.md").write_text("---\npage_id: 685064916\ntitle: \"Живая\"\nbreadcrumbs: \"Живая\"\n---\n\n# Живая\n", encoding="utf-8")
    (m / "Живая_assets/схема.drawio").write_text("x", encoding="utf-8")
    (m / "Ветка/index_assets").mkdir(parents=True)
    (m / "Ветка/index.md").write_text("---\npage_id: 685064882\ntitle: \"Ветка\"\nbreadcrumbs: \"Ветка\"\n---\n\n# Ветка\n", encoding="utf-8")
    (m / "Ветка/index_assets/схема.drawio").write_text("x", encoding="utf-8")
    (m / "Старое/Дашборд_assets").mkdir(parents=True)
    (m / "Старое/Дашборд_assets/Диаграмма без названия-1.drawio").write_text("x", encoding="utf-8")
    (m / "sync_state.md").write_text(
        "<!-- Confluence sync state -->\n**Sync Date:** 2026-09-29\n**Pages:** 2\n\n"
        "| # | Page ID | Title | Local Path | Status |\n|---|---|---|---|---|\n"
        "| 1 | 685064916 | Живая | Живая.md | SYNCED |\n| 2 | 685064882 | Ветка | Ветка/index.md | SYNCED |\n",
        encoding="utf-8")
    mirror = SC.WikiMirror.__new__(SC.WikiMirror)
    SC.WikiMirror.__init__(mirror, str(m))
    extra = mirror.extra_files(["Живая.md", "Ветка/index.md"])
    assert extra == ["Старое/Дашборд_assets/Диаграмма без названия-1.drawio"], \
        f"чистка синка: {extra}"
    audit = run("sync_audit.py", cwd=root).stdout
    assert "ПОСТОРОННИЕ: **1**" in audit, audit[-600:]
    dry = run("kb_names.py", cwd=root).stdout
    assert "схем без страницы: 1" in dry, dry[-600:]
    run("kb_names.py", "--apply", "--allow-dirty", cwd=root)
    assert not (m / "Старое").exists(), "след прежней раскладки не убран"
    assert (m / "Живая_assets/схема.drawio").is_file() and \
        (m / "Ветка/index_assets/схема.drawio").is_file(), "убраны схемы живых страниц"


@test
def test_registry_cache_notices_a_changed_script(tmp: Path):
    """Ключ кэша реестра панели меняет и правка скрипта, а не только версия и реестр.

    Флаги команд панель берёт из `--help` скриптов. В ките на разработке новый флаг без
    смены версии не появлялся: ключ помнил версию, `commands.txt` и сервер панели.
    """
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    body = src.split("def _registry()")[1].split("\ndef ")[0]
    assert "scripts={newest}" in body and 'f.endswith(".py")' in body, \
        "ключ кэша реестра не видит правки скриптов"


@test
def test_deliverables_have_a_folder_for_archives(tmp: Path):
    """У поставок есть папка архивов — `Deliverables/_archive` (решение пользователя 29.09.2026).

    Пакеты `.zip`, переданные комплектом, и прежние версии документов лежали прямо в
    `Deliverables/` или в самодельной `_arch`, которую doctor честно звал нарушением схемы.
    """
    structure = (KIT / "structure_dirs.txt").read_text(encoding="utf-8")
    assert "\nDeliverables/_archive\n" in structure, "папки архивов нет в схеме проекта"
    root = make_project(tmp)
    assert (root / "Deliverables/_archive").is_dir(), "update не заведёт папку архивов"
    (root / "Deliverables/_archive/Материалы программы.zip").write_bytes(b"PK")
    cp = run("aurora_doctor.py", "--structure", cwd=root)
    assert "Deliverables/_archive" not in cp.stdout + cp.stderr, \
        f"doctor зовёт папку архивов нарушением:\n{(cp.stdout + cp.stderr)[-600:]}"
    for doc in ("docs/INSTALL.md", "templates/agents/AGENTS.md.template"):
        assert "_archive" in (KIT / doc).read_text(encoding="utf-8").split("Deliverables", 1)[1][:200], \
            f"{doc} не знает про папку архивов"


@test
def test_a_term_with_a_known_expansion_is_a_definition_not_a_stub(tmp: Path):
    """Термин с известной расшифровкой — определение, а не заготовка.

    Решение пользователя 29.09.2026: заготовка — только карточка без смысла, со ссылками на
    упоминания. «ТКС — Телекоммуникационный канал связи.» — уже определение, пусть и самое
    простое. До 1.143.2 `--terms` заводил такие карточки пустышками, и определение выпадало
    из поиска и контекста модели: 35 карточек PRJ-C, 27 PRJ-A, 14 PRJ-B.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    root = make_project(tmp)
    g = root / "AuroraKnowledgeDB/Glossary"
    g.mkdir(parents=True, exist_ok=True)
    (g / "Глоссарий-проекта.md").write_text(
        '---\ntitle: "Глоссарий проекта"\nstatus: knowledge\ntype: glossary\n'
        'sources:\n  - "Sources/Confluence/Глоссарий.md"\n---\n\n# Глоссарий проекта\n\n'
        "| Сокращение | Расшифровка |\n|---|---|\n| ТКС | Телекоммуникационный канал связи |\n"
        "| ЦУН | Централизованный учет налогоплательщиков |\n", encoding="utf-8")
    for i in range(3):
        card(root, f"Processes/Обмен-{i}.md", f"Квитанция уходит по ТКС, данные — из ЦУН ({i}).",
             status="knowledge")
    # заготовка старого образца: расшифровка есть, а пометка держит её вне выдачи
    (g / "ЦУН.md").write_text(
        '---\ntitle: "ЦУН"\naliases: []\nstatus: placeholder\ntype: glossary\n'
        'tags: [заготовка]\ncreated: 2026-09-09\nupdated: 2026-09-09\nrelated: []\n---\n\n'
        "# ЦУН\n\nЦУН — Централизованный учет налогоплательщиков.\n\n"
        "_Заготовка: имя названо в базе и расшифровка известна, знания о предмете пока нет._\n"
        "_Наполните её при следующем разборе источника — ссылки переписывать не придётся._\n\n"
        "## Названо в карточках\n\n- [[Обмен-0]]\n", encoding="utf-8")
    cp = run("kb_fix.py", "--all", "--terms", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode in (0, 1), cp.stdout[-600:] + cp.stderr[-400:]
    for name in ("ТКС", "ЦУН"):
        path = g / f"{name}.md"
        assert path.is_file(), f"карточки {name} нет: {sorted(p.name for p in g.iterdir())}"
        text = path.read_text(encoding="utf-8")
        assert not A.is_placeholder(A.frontmatter(text), text), f"{name} осталась заготовкой:\n{text}"
        assert "_Заготовка:" not in text and "заготовка" not in A.frontmatter(text).get("tags", "")
        assert A.card_sources(text) == ["Sources/Confluence/Глоссарий.md"], \
            f"у определения нет источника справочника: {A.card_sources(text)}"
        assert "[[Глоссарий-проекта]]" in text, "не видно, откуда взята расшифровка"
    assert "ТКС — Телекоммуникационный канал связи." in (g / "ТКС.md").read_text(encoding="utf-8")

    # без расшифровки — по-прежнему честная заготовка
    K = importlib.import_module("kb_fix")
    src = (SCRIPTS / "kb_fix.py").read_text(encoding="utf-8")
    stub_branch = src.split("def plan_term_stubs(")[1].split("def plan_drop_jira(")[0]
    assert "расшифровки база пока не знает" in stub_branch and "status: {PLACEHOLDER}" in stub_branch


@test
def test_engine_compiles_on_the_oldest_promised_python(tmp: Path):
    """Движок компилируется на старшем из обещанных Python — 3.9, системном у macOS.

    С 1.112.0 по 1.143.0 в `kb_lint.py` стояла f-строка с кавычками того же вида внутри
    выражения — это понимает только Python 3.12+. На системном Python линтер не запускался
    вовсе, а вместе с ним падали починка, «Что осталось человеку» и хук коммита; проверка
    кита шла на 3.12 и этого не видела. Здесь — если на машине есть Python старше 3.12;
    в CI то же делает отдельная задача на 3.9.
    """
    old = None
    for cand in ("/usr/bin/python3", "python3.9", "python3.10", "python3.11"):
        try:
            v = subprocess.run([cand, "-c", "import sys; print(sys.version_info[:2] < (3, 12))"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if v.returncode == 0 and v.stdout.strip() == "True":
            old = cand
            break
    wf = (KIT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    assert 'python-version: "3.9"' in wf and "compileall" in wf, "в CI нет проверки на Python 3.9"
    if not old:
        return
    out = tmp / "pyc"
    cp = subprocess.run([old, "-m", "compileall", "-q",
                         str(KIT / "scripts"), str(KIT / "cockpit"), str(KIT / "aurora.py")],
                        capture_output=True, text=True, timeout=300,
                        env={**os.environ, "PYTHONPYCACHEPREFIX": str(out)})
    assert cp.returncode == 0, f"движок не компилируется на {old}:\n{(cp.stdout + cp.stderr)[-800:]}"


@test
def test_unsupported_claims_are_written_down_for_a_human(tmp: Path):
    """Что именно Момус не нашёл в источнике, записано — человеку есть что проверять.

    До 1.143.1 в карточку уходило только число `unsupported: N`, а утверждения терялись вместе
    с ответом Момуса. На PRJ-A так копились 223 утверждения в 176 карточках, `ops:todo` о них
    молчал, и разобрать их было нельзя: проверять нечего.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    importlib.reload(R)
    verdict = ("1. ОПОРА «ежемесячно»\n2. **НЕТ ОПОРЫ** срок подачи — 10 дней\n"
               "- ПРОТИВОРЕЧИЕ ставка 12 % ↔ «ставка 20 %»\nВЕРДИКТ: БЕЗ ОПОРЫ 2")
    assert R.momus_claims(verdict) == ["НЕТ ОПОРЫ: срок подачи — 10 дней",
                                      "ПРОТИВОРЕЧИЕ: ставка 12 % ↔ «ставка 20 %»"], \
        R.momus_claims(verdict)
    assert R.momus_claims("ВЕРДИКТ: ЧИСТО") == []

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    mk = lambda name, n: (kb / f"{name}.md").write_text(
        f'---\ntitle: "{name}"\nkind: knowledge\nstatus: draft\ntype: concept\n'
        f'unsupported: {n}\n---\n\n{name} подаётся ежемесячно.\n\n{R.QUOTES}\n'
        'Отчёт подаётся ежемесячно, ставка 20 %.\n', encoding="utf-8")
    mk("Отчёт", 2)
    mk("Реестр", 1)
    answers = {"Отчёт": verdict, "Реестр": "ОПОРА «ежемесячно»\nВЕРДИКТ: ЧИСТО"}

    def fake(cfg, role, messages, **kw):
        q = messages[0]["content"]
        name = "Отчёт" if "«Отчёт»" in q else "Реестр"
        return {"ok": True, "text": answers[name], "backend": 1, "model": "qa", "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    res = R.recheck_unsupported(cfg, str(root), True, call=fake)
    assert (res["clean"], res["flagged"], res["claims"]) == (1, 1, 2), res
    fm = lambda name: (kb / f"{name}.md").read_text(encoding="utf-8").split("\n---", 1)[0]
    assert "unsupported: 2" in fm("Отчёт") and "unsupported:" not in fm("Реестр"), \
        "пометка не следует за перепроверкой"
    md = (root / "AuroraKnowledgeDB/meta/unsupported.md").read_text(encoding="utf-8")
    assert "[[Отчёт]]" in md and "срок подачи — 10 дней" in md and "[[Реестр]]" not in md, md
    todo = (SCRIPTS / "aurora_todo.py").read_text(encoding="utf-8")
    assert "unsupported_cards()" in todo and "meta/unsupported.md" in todo, \
        "«Что осталось человеку» снова молчит о непроверенных утверждениях"

    # тезис переписан и проверен чисто — пометка и запись в реестре уходят
    (kb / "Отчёт.md").write_text((kb / "Отчёт.md").read_text(encoding="utf-8").replace(
        "\nОтчёт подаётся ежемесячно.\n", "\n"), encoding="utf-8")
    def fake2(cfg, role, messages, **kw):
        if role == "qa":
            return {"ok": True, "text": "ВЕРДИКТ: ЧИСТО", "backend": 1, "model": "qa", "log": []}
        return {"ok": True, "text": "Отчёт подаётся ежемесячно по ставке 20 %.", "backend": 1,
                "model": "w", "tps": 9, "log": []}
    here = os.getcwd()
    os.chdir(root)
    try:
        R.run_distill(cfg, str(root), apply=True, limit=5, momus=True, call=fake2)
    finally:
        os.chdir(here)
    assert "unsupported:" not in fm("Отчёт"), "чистый новый тезис остался с прежней пометкой"
    assert "[[Отчёт]]" not in (root / "AuroraKnowledgeDB/meta/unsupported.md").read_text(encoding="utf-8")


@test
def test_mcp_tools_never_collide_with_the_agents_own(tmp: Path):
    """Инструменты сервера MCP идут с приставкой его имени — совпасть им не с чем.

    Живая проверка 29.09.2026: сервер базы проекта (`aurora-kb`) называет поиск `kb_search`,
    как и встроенный инструмент агента. Pydantic AI на совпадении имён отказал всему
    вызову, движок повторил его уже без инструментов, и модель честно ответила
    «инструмент не вызван». С приставкой модель вызвала оба сервера — кита и проекта.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS / "agents"))
    P = importlib.import_module("pydantic_ai_adapter")
    importlib.reload(P)
    assert P.tool_prefix("aurora-graph") == "aurora-graph"
    assert P.tool_prefix("mcp.atlassian") != P.tool_prefix("mcp_atlassian"), \
        "два разных сервера получили одну приставку"
    a, b = P.tool_prefix("граф"), P.tool_prefix("база")
    assert a != b and re.fullmatch(r"[A-Za-z0-9_-]{1,24}", a), (a, b)
    src = (SCRIPTS / "agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    lazy = src.split("def lazy_mcp_toolsets(")[1].split("def call_hook(")[0]
    assert ".prefixed(tool_prefix(name))" in lazy, "инструменты серверов снова без приставки"
    assert ".prefixed(tool_prefix(name))" in src.split("def mcp_toolsets(")[1].split("def mcp_catalog(")[0]
    assert "aurora-graph_graph_stats" in P.mcp_catalog({"aurora-graph": {}}), \
        "модель не знает, как называются инструменты подключённого сервера"
    assert '"tools_called": tools_called(' in src, "по ответу не понять, какие инструменты вызваны"


@test
def test_mcp_probe_names_where_each_server_comes_from(tmp: Path):
    """Проверка серверов — то, что получит прогон: машина и проект, в папке проекта."""
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    kitdir = tmp / "кит"
    (kitdir / "local").mkdir(parents=True)
    (kitdir / "local" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "m": {"command": "x"}, "both": {"command": "y"}}}), encoding="utf-8")
    proj = tmp / "проект"
    proj.mkdir()
    (proj / "mcp.json").write_text(json.dumps({"mcpServers": {
        "p": {"command": "z"}, "both": {"args": ["--p"]}}}), encoding="utf-8")
    seen = {}

    def fake_run(argv, input=None, cwd=None, **kw):
        seen.update(argv=argv, task=json.loads(input), cwd=cwd)
        rows = {n: {"ok": True, "tools": 1, "names": ["t"]} for n in seen["task"]["mcpServers"]}
        return subprocess.CompletedProcess(argv, 0, stdout="шум\n" + json.dumps(
            {"ok": True, "servers": rows}) + "\n", stderr="")

    saved = (A.subprocess.run, A._adapter_argv)
    A.subprocess.run, A._adapter_argv = fake_run, lambda: ["py", "adapter.py"]
    try:
        out = A.mcp_probe(str(proj), kit=str(kitdir))
    finally:
        A.subprocess.run, A._adapter_argv = saved
    assert seen["argv"][-1] == "--mcp-probe" and seen["cwd"] == str(proj), seen
    assert seen["task"]["mcpServers"]["both"] == {"command": "y", "args": ["--p"]}, \
        "проверяется не то, что получит прогон"
    assert {n: r["from"] for n, r in out["servers"].items()} == {
        "m": "kit", "both": "both", "p": "project"}, out
    A._adapter_argv, keep = (lambda: []), A._adapter_argv
    try:
        assert "Pydantic AI" in A.mcp_probe(str(proj), kit=str(kitdir))["error"]
    finally:
        A._adapter_argv = keep


@test
def test_machine_mcp_servers_reach_every_project(tmp: Path):
    """Серверы машины видны всем проектам, проект перекрывает одноимённый по полям.

    Так проект может объявить сервер в своём mcp.json (он едет в git вместе с проектом),
    а токены к нему остаются в файле машины и в git не попадают.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A
    kit, proj = tmp / "кит", tmp / "проект"
    (kit / "local").mkdir(parents=True)
    proj.mkdir()
    assert A.kit_mcp_path(str(kit)) == kit / "local" / "mcp.json", \
        "серверы машины лежат не в local/ — туда, где их не увезёт git"
    (kit / "local" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "atlassian": {"command": "uvx", "args": ["mcp-atlassian"], "env": {"TOKEN": "секрет"}},
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}), encoding="utf-8")
    (proj / "mcp.json").write_text(json.dumps({"mcpServers": {
        "atlassian": {"command": "npx", "args": ["-y", "mcp-atlassian"]},
        "local-only": {"url": "http://127.0.0.1:9/mcp"}}}), encoding="utf-8")
    cfg = A.mcp_config(str(proj), kit=str(kit))["mcpServers"]
    assert set(cfg) == {"atlassian", "fetch", "local-only"}, f"слои не сложились: {sorted(cfg)}"
    assert cfg["atlassian"]["command"] == "npx", "поле проекта не перекрыло машинное"
    assert cfg["atlassian"]["env"] == {"TOKEN": "секрет"}, \
        "токен машины потерялся, когда проект объявил сервер с тем же именем"
    assert A.mcp_config("", kit=str(kit))["mcpServers"].keys() == {"atlassian", "fetch"}, \
        "без проекта не видны серверы машины"
    assert A.mcp_config(str(proj), kit=str(tmp / "нет"))["mcpServers"].keys() == \
        {"atlassian", "local-only"}, "без файла машины пропали серверы проекта"
    # Прогон тестов не видит личные серверы машины разработчика.
    assert os.environ.get("AURORA_TESTS_ISOLATED")
    assert A.mcp_config(str(tmp / "пусто")) == {}, "тест увидел личные серверы машины"

    # Файл машины — в папке, закрытой .gitignore и не тронутой обновлением кита.
    ignored = subprocess.run(["git", "-C", str(KIT), "check-ignore", "-q", "local/mcp.json"])
    assert ignored.returncode == 0, "local/mcp.json уедет в git кита вместе с токенами"
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert re.search(r'KIT_KEEP = \([^)]*"local/"', src), \
        "обновление кита из архива перезапишет серверы машины"


@test
def test_machine_mcp_secrets_never_reach_the_browser(tmp: Path):
    """Панель принимает токены серверов машины, но обратно отдаёт только маску.

    Маска, пришедшая обратно, значит «оставить как было» — и в карточке, и в файле
    целиком. Потерять токен молча нельзя: маска без сохранённого значения — ошибка.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    old_kit = ck.KIT
    ck.KIT = str(tmp)
    try:
        act, state = ck.mcp_kit_action, ck.mcp_kit_state
        assert state()["servers"] == {} and "error" not in state(), "пустая машина — не ошибка"
        paste = json.dumps({"mcpServers": {"gh": {
            "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "ghp_секрет"}, "timeout": 30},
            "ctx": {"url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "Bearer токен"}}}})
        dry = act({"action": "import", "text": paste, "dry": True})
        assert dry.get("ok") and not (tmp / "local/mcp.json").exists(), \
            f"предпросмотр вставки записал файл: {dry}"
        assert "ghp_секрет" not in json.dumps(dry, ensure_ascii=False), \
            "предпросмотр вставки вернул браузеру токен"
        assert dry["servers"][0]["env"] == ["GITHUB_TOKEN"], "предпросмотр не назвал переменные"
        r = act({"action": "import", "text": paste})
        assert r.get("ok"), r
        path = tmp / "local/mcp.json"
        assert oct(path.stat().st_mode & 0o777) == "0o600", "файл с токенами читают все"

        st = state()
        seen = json.dumps(st, ensure_ascii=False)
        assert "ghp_секрет" not in seen and "Bearer токен" not in seen, \
            "секрет ушёл в браузер"
        assert st["servers"]["gh"]["env"]["GITHUB_TOKEN"] == st["mask"], "вместо маски пусто"
        assert st["servers"]["gh"]["timeout"] == 30, "поле вне формы потерялось"

        # Карточка: правка без знания токена сохраняет его; переименование — тоже.
        spec = st["servers"]["gh"]
        spec["args"].append("--read-only")
        assert act({"action": "save_server", "name": "github", "rename_from": "gh",
                    "spec": spec}).get("ok")
        disk = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert list(disk) == ["github", "ctx"], f"переименованный сервер сменил место: {list(disk)}"
        assert disk["github"]["env"]["GITHUB_TOKEN"] == "ghp_секрет", "токен потерян при правке"
        assert (tmp / "local/mcp.json.bak").is_file(), "прежняя версия не сохранена"

        # Весь файл: маски восстанавливаются, новое значение пишется.
        text = state()["text"].replace("mcp.example.com", "mcp2.example.com")
        assert act({"action": "save_raw", "text": text}).get("ok")
        disk = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert disk["ctx"]["headers"]["Authorization"] == "Bearer токен" \
            and disk["ctx"]["url"].startswith("https://mcp2."), "весь файл не сохранился как надо"
        broken = state()["text"].replace('"github"', '"renamed"')
        r = act({"action": "save_raw", "text": broken})
        assert "скрыто маской" in r.get("error", ""), \
            f"маска без сохранённого значения записалась вместо токена: {r}"
        assert act({"action": "import", "text": state()["text"]}).get("error"), \
            "вставка настройки из самой панели (с масками) прошла"
        assert "имя" in act({"action": "save_server", "name": "плохое имя", "rename_from": "",
                             "spec": {"command": "x"}}).get("error", "")
        assert "command" in act({"action": "save_server", "name": "x", "rename_from": "",
                                 "spec": {"args": []}}).get("error", "")
        assert act({"action": "delete_server", "name": "ctx"}).get("ok")
        assert list(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == ["github"]
    finally:
        ck.KIT = old_kit

    # Маска — одна на панель: раздел берёт её из ответа сервера, а не держит свою копию.
    ui = ui_source()
    for fn in ("async function renderMcpKit(", "function mcpCard(", "function mcpPairs(",
               "function openMcpCard(", "function openMcpRaw("):
        assert "••••••" not in _js_function(ui, fn), f"{fn} держит свою копию маски"


@test
def test_mcp_paste_understands_the_shapes_people_copy(tmp: Path):
    """Вставка понимает то, что копируют из Claude Desktop, Claude Code, Cursor и VS Code."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    P = ck.mcp_parse_paste
    shapes = {
        '{"mcpServers": {"a": {"command": "npx", "args": ["x"]}}}': ["a"],
        '{"servers": {"b": {"type": "stdio", "command": "uvx"}}}': ["b"],
        '{"mcp": {"servers": {"c": {"url": "https://sse.example.com/sse", "type": "sse"}}}}': ["c"],
        '{"d": {"command": "uvx"}, "e": {"url": "https://x.example.com/mcp"}}': ["d", "e"],
        '"f": {"command": "uvx", "args": ["mcp-f"]},': ["f"],
        '{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github@1.0"]}': ["github"],
        '{"url": "https://mcp.example.org/mcp"}': ["example"],
        # JSONC: комментарии и висячие запятые, `//` в адресе — не комментарий
        '{\n // мой сервер\n "servers": {"g": {"url": "https://g.example.com/mcp", /* x */},},\n}': ["g"],
    }
    for text, names in shapes.items():
        srv, err = P(text)
        assert not err and list(srv) == names, f"не разобрано: {text[:50]} → {err or list(srv)}"
    assert P('{"servers": {"g": {"url": "https://g.example.com/mcp"}}}')[0]["g"]["url"] == "https://g.example.com/mcp", \
        "разбор комментариев съел адрес после //"
    for bad, why in (("", "пуста"), ('{"a": ', "не JSON"), ('{"x": {"y": 1}}', "command или url")):
        assert why in P(bad)[1], f"плохая вставка не объяснена: {bad!r} → {P(bad)}"


@test
def test_mcp_paste_understands_zed(tmp: Path):
    """Вставка из Zed: сервер расширения, свой сервер в старой форме, выключенный, обёртка.

    Живой случай 24.09.2026: пользователь вставил сервер Tavily из настроек Zed — `enabled`,
    `remote` и `settings` с ключом, без command и url, — и панель ответила «не похоже на
    сервер MCP». Команду запуска у Zed знает расширение, поэтому известные расширения
    описаны в движке, а для неизвестного ошибка говорит, что дописать.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    paste = '''"mcp-server-tavily": {
      "enabled": true,
      "remote": false,
      "settings": {
        "tavily_api_key": "tvly-test-0000",
      },
    },'''
    srv, err, notes = ck.mcp_read_paste(paste)
    assert not err, err
    spec = srv["mcp-server-tavily"]
    assert spec["command"] == "npx" and spec["args"] == ["-y", "tavily-mcp@latest"], spec
    assert spec["env"] == {"TAVILY_API_KEY": "tvly-test-0000"}, "ключ из settings не стал переменной"
    assert not {"settings", "enabled", "remote"} & set(spec), f"поля Zed уехали в настройку: {spec}"
    assert notes and "расширения Zed" in notes[0] and "tvly-test" not in " ".join(notes), \
        f"пояснение не сказано или показывает ключ: {notes}"
    assert not ck.mcp_check(srv), ck.mcp_check(srv)

    srv, err, _ = ck.mcp_read_paste('{"context_servers": {"my": {"command": {"path": "uvx", '
                                    '"args": ["mcp-my"], "env": {"K": "v"}}, "settings": {}}}}')
    assert not err and srv["my"] == {"command": "uvx", "args": ["mcp-my"], "env": {"K": "v"}}, (err, srv)

    srv, err, notes = ck.mcp_read_paste('{"a": {"command": "uvx"}, "b": {"command": "x", "enabled": false}}')
    assert list(srv) == ["a"] and any("«b» выключен" in n for n in notes), (srv, notes)

    srv, err, _ = ck.mcp_read_paste('"mcp-server-unknown": {"settings": {"some_api_key": "k"}}')
    assert "расширения Zed" in err and "SOME_API_KEY" in err and "k" not in err.split(":")[-1].split(","), err

    # через действие панели: предпросмотр отдаёт пояснения, секрет — только именем
    kitdir = tmp / "kit"
    (kitdir / "local").mkdir(parents=True)
    was = ck.kit_mcp_file
    ck.kit_mcp_file = lambda: str(kitdir / "local" / "mcp.json")
    try:
        dry = ck.mcp_kit_action({"action": "import", "text": paste, "dry": True})
    finally:
        ck.kit_mcp_file = was
    assert dry.get("ok") and dry["notes"] and dry["servers"][0]["env"] == ["TAVILY_API_KEY"], dry
    assert "tvly-test" not in json.dumps(dry, ensure_ascii=False), "предпросмотр показал ключ"


@test
def test_kit_setup_has_machine_mcp_section(tmp: Path):
    """«Настройка кита» показывает серверы машины карточками и умеет вставку и весь файл."""
    ui = ui_source()
    setup = _js_function(ui, "async function renderSetup(")
    assert "renderMcpKit()" in setup, "в «Настройке кита» нет раздела MCP"
    assert "drawSetupJump(box)" in setup, "длинная страница без переходов — раздел не найти"
    for fn in ("function mcpCard(", "function openMcpCard(", "function openMcpPaste(",
               "function openMcpRaw("):
        assert fn in ui, f"нет части раздела MCP: {fn}"
    for action in ('"save_server"', '"delete_server"', '"import"', '"save_raw"'):
        assert action in ui, f"раздел не зовёт {action}"
    close = _js_function(ui, "function closeMcp(")
    assert '$("#mcpDrawer").innerHTML = ""' in close, \
        "после закрытия окна вставленные токены остаются в разметке страницы"
    assert 'type:"password"' in _js_function(ui, "function mcpPairs("), \
        "значения переменных видны при вводе, как обычный текст"
    assert 'id="mcpOverlay"' in ui, "окну правки негде открыться"


@test
def test_publishing_does_not_overwrite_someone_elses_edit(tmp: Path):
    """У артефакта две жизни: черновик на диске и чистовик на странице команды.

    Публикация перезаписывает страницу — и однажды сотрёт правку коллеги, а узнают об
    этом через месяц. Поэтому в шапке артефакта хранится версия страницы, которую
    опубликовали мы: разошлась с текущей — публикация останавливается и называет, кто и
    когда правил. Это тот же класс, что дрейф источника, только зеркально.

    Выбор файла — из готового, а не набором пути: первая же опечатка ушла бы в Confluence
    чужой страницей.
    """
    src = (KIT / "scripts/publish_doc.py").read_text(encoding="utf-8")
    assert "published_version" in src, "версия опубликованной страницы не запоминается"
    assert 'was != now and not a.force' in src, \
        "публикация перезаписывает страницу, не глядя на чужие правки"
    assert '"--force"' in src, "нет способа настоять на своей версии осознанно"
    assert "published_url" in src, \
        "в черновике не остаётся адреса чистовика — связи между двумя жизнями нет"

    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "def artifact_files(" in srv and '"/api/artifacts"' in srv, \
        "панель не умеет показать, что уже создано по типу"

    ui = panel_sources()
    assert "async function publish(ctx)" in ui and '"ship:publish"' in ui, \
        "нет публикации из панели"
    assert 'cmd:"ship:publish"' in ui, "публикация идёт мимо движка"
    assert 'f.status === "draft"' in ui, \
        "непройденная цепочка публикуется молча — команда увидит непроверенное как чистовик"
    assert "rec.publish_url" in ui, \
        "кнопка не смотрит на адрес публикации: тип без адреса опубликовать нельзя"


@test
def test_the_agent_can_hold_a_conversation_and_use_tools(tmp: Path):
    """В ките лежал агентный фреймворк, работавший как `curl`.

    `Agent(...)` создавался и тут же использовался для одноразовой подстановки текста:
    все сообщения склеивались в одну строку, `message_history` не передавалась,
    инструменты не регистрировались. Диалог был невозможен в принципе — модель не помнила,
    что спрашивала минуту назад.

    Достроено ровно то, чего не хватало, и **рядом** со старым путём: на старом стоит
    работающий разбор базы, и ронять его ради нового экрана нельзя. Развилка временная —
    её снимают, когда новый путь докажет себя на живой работе.

    Инструменты — все на чтение и все внутри проекта. Файл создаёт код по ответу модели:
    так путь всегда внутри объявленной папки, шапка собрана кодом, точка записи одна.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import inspect
    import agent_core as A

    assert "history" in inspect.signature(A.call_role).parameters, \
        "вызов не принимает историю — диалога не будет"
    assert "tools" in inspect.signature(A.call_role).parameters, \
        "вызов не умеет давать модели инструменты"

    ad = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert "message_history=history or None" in ad, \
        "адаптер снова склеивает разговор в строку — модель не помнит предыдущей реплики"
    assert "def register_tools(" in ad, "инструменты не регистрируются"
    for tool in ("read_file", "list_dir", "kb_search", "kb_context", "artifact_spec"):
        assert f"def {tool}(" in ad, f"нет инструмента {tool}"
    # ни одного на запись: файл пишет движок, а не модель
    for forbidden in ("def write_file", "def save_", "def create_file", "def apply_"):
        assert forbidden not in ad, f"у модели появился инструмент записи: {forbidden}"
    assert 'raise ValueError("путь вне проекта")' in ad, \
        "инструменты не держат границу проекта — модель прочитает что угодно на машине"
    # Границы проекта мало: секреты лежат ВНУТРИ него. Модель, прочитавшая
    # `.env.aurora.local`, может вписать токен в артефакт, а артефакт уходит в Confluence
    # и в git. Найдено на живом коде уже после того, как инструменты были написаны.
    assert 'raise ValueError("файл с доступами читать нельзя")' in ad, \
        "модель может прочитать токены проекта и вписать их в документ"
    assert 'SECRET = (' in ad and '".env"' in ad, "список файлов с доступами не объявлен"
    assert '"служебная папка: читать нечего"' in ad, \
        "модель ходит в .git и .ssh — там нет знания, но есть чем навредить"

    # планировщик получает инструменты, воркер — нет: у него есть план и пак, а лишний
    # поиск на этом шаге размывает основания документа
    run = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    # именно планировщик производства: тот, что размечает источник, работает по описи
    # абзацев, и инструменты ему не нужны
    planner = run[run.index("def run_make("):run.index("def read_text_file(")]
    assert "tools=True" in planner, "планировщик производства не может доискать недостающее"
    writer = planner[planner.index("PROMPT_MAKE_WRITE.format"):][:400]
    assert "tools=True" not in writer, "воркеру даны инструменты — основания размоются"

    # и навык разбора берётся из файла, а не из копии в промпте
    assert "def grill_method(" in run and "aurora-grill" in run, \
        "метод разбора вшит в промпт — копия разойдётся с навыком и никто не заметит"
    assert (KIT / "skills/aurora-grill/SKILL.md").is_file(), "навыка нет в поставке"
    # и он не затирает чужой grill-me в общем каталоге
    assert not (KIT / "skills/grill-me").exists(), \
        "навык назван так же, как чужой в ~/.claude/skills — установка затрёт настроенное"


@test
def test_artifact_shows_what_was_asked_assumed_and_grounded(tmp: Path):
    """Документ показывает, на чём стоит, что выяснили и что приняли молча.

    Раньше он этого не показывал вовсе: `based_on` не писался, ответы человека жили в
    служебной сессии, а молчаливые умолчания не назывались нигде. Через месяц читатель
    не отличал выясненное от угаданного, а «на чём документ стоит» узнавали чтением
    контекста, которого уже нет.

    Отдельно — граница производства. В чистовик уходит только документ; уточнения,
    допущения, замечания критика и план остаются в черновике, с которым работают
    аналитики. Режем по маркеру, а не по списку заголовков: список в трёх местах
    разошёлся бы на первом же новом разделе.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    from aurora_common import MADE_MARK, clean_copy

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n'
                          '    tech_agnostic: "true"\n', encoding="utf-8")
    for i in range(6):
        card(root, f"Concepts/Заявка-{i}.md",
             f"Заявка проходит статусы. Срок десять дней. см. [[Заявка-{(i + 1) % 6}]]",
             status="knowledge", kind="knowledge")

    seen, rounds = {}, {"n": 0}

    def fake(cfg_, role, messages, **kw):
        seen[role] = stub_messages(messages, kw)[0]["content"]
        if role == "planner":
            rounds["n"] += 1
            if rounds["n"] == 1:
                return {"ok": True, "backend": 1, "model": "p", "tps": 9, "log": [],
                        "text": json.dumps({"questions": [{"q": "Какой срок?",
                                                           "why": "меняет критерий",
                                                           "rec": "10 дней"}],
                                            "assumptions": [], "plan": ""},
                                           ensure_ascii=False)}
            return {"ok": True, "backend": 1, "model": "p", "tps": 9, "log": [],
                    "text": json.dumps({"questions": [], "assumptions": ["хранение 3 года"],
                                        "plan": "1. Критерии"}, ensure_ascii=False)}
        if role == "critic":
            return {"ok": True, "backend": 1, "model": "c", "tps": 9, "log": [],
                    "text": json.dumps({"ok": True, "issues": [],
                                        "coverage": {"объём": "полно",
                                                     "крайние случаи": "пробел"}},
                                       ensure_ascii=False)}
        if role == "qa":
            return {"ok": True, "backend": 1, "model": "q", "tps": 9, "log": [],
                    "text": "ВЕРДИКТ: чисто"}
        return {"ok": True, "backend": 1, "model": "w", "tps": 9, "log": [],
                "text": "# AC\n\nСрок ([[Заявка-1]]), проверяется отчётом. См. [[Выдуманная]]."}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    # 1) файл рождается СРАЗУ после обогащения: ответы человека не должны жить в сессии
    # тема должна находиться в базе: производство на пустом контексте отказывает, и
    # это правильно — но проверяем мы здесь не его
    r1 = R.run_make(cfg, str(root), "ac", "срок заявки", "", "", False, call=fake)
    assert r1.get("ok"), f"обогащение не прошло: {r1.get('why')}"
    early = R.load_session(str(root), r1["sid"]).get("path")
    assert early and (root / early).is_file(), \
        "файла нет до воркера — ответы человека уйдут в служебную папку и потеряются"
    assert "не написан" in (root / early).read_text(encoding="utf-8"), \
        "пустой документ не объясняет, почему он пуст"

    # 2) ответ попадает в документ немедленно, до всякого воркера
    R.run_make(cfg, str(root), "", "", r1["sid"], "10 дней", False, call=fake)
    doc = (root / R.load_session(str(root), r1["sid"])["path"]).read_text(encoding="utf-8")
    assert "## Уточнения" in doc and "10 дней" in doc, "ответ человека не дошёл до документа"

    # 3) правило «без технологий» включено ТИПОМ и дошло до обоих
    assert "без технологий" in seen["worker"], "tech_agnostic не дошёл до воркера"
    assert "просочилось решение об архитектуре" in seen["critic"], \
        "tech_agnostic не дошёл до критика"
    assert "как проверить" in seen["worker"], "проверяемость критериев не потребована"

    # 4) основания — из цитат, а не из всего пака; выдумка названа выдумкой
    assert 'based_on: ["[[Заявка-1]]"]' in doc, \
        f"основания не из цитат: сорок карточек в паке — не сорок оснований\n{doc[:400]}"
    assert "Выдуманная" in doc.split("## Под вопросом")[1], \
        "ссылка на карточку не из пака не названа — Момус имён не проверяет"

    # 5) допущения с источником
    assert "## Допущения" in doc and "решила модель" in doc, \
        "молчаливое умолчание не названо — читатель не отличит его от выясненного"

    # 6) покрытие заполняет критик, и оно в шапке
    assert "coverage: объём=полно" in doc, "покрытие не попало в шапку"

    # 7) граница: в чистовик уходит только документ
    assert MADE_MARK in doc, "нет границы производства"
    clean = clean_copy(doc)
    for gone in ("## Уточнения", "## Допущения", "## План", "## Под вопросом"):
        assert gone not in clean, f"{gone} уедет заказчику"
    assert "Критерии приёмки" in clean or "Срок" in clean, "чистовик потерял сам документ"

    # Критик после реализации нашёл две дыры в том, что уже «работало».
    # 1) Ссылка на раздел карточки: своя регулярка не знала про якоря, и
    #    «[[Заявка-1#Статусы]]» не сходилась с «Заявка-1» — настоящее основание
    #    объявлялось выдумкой, а документ выглядел стоящим ни на чём.
    st_anchor = dict(R.load_session(str(root), r1["sid"]))
    st_anchor["draft"] = "См. [[Заявка-1#Статусы]], [[Заявка-2|заявку]], [[дом/Заявка-3.md]]."
    st_anchor["pack"] = "## Заявка-1 — Заявка\n\n## Заявка-2 — Ещё\n\n## Заявка-3 — И три\n"
    st_anchor["path"] = None
    anchored = (root / R.write_artifact(str(root), st_anchor)).read_text(encoding="utf-8")
    base = [ln for ln in anchored.splitlines() if ln.startswith("based_on:")]
    assert base and all(f'"[[Заявка-{n}]]"' in base[0] for n in (1, 2, 3)), \
        f"ссылка с якорём/подписью/путём не признана основанием: {base}"
    assert "по памяти" not in anchored, "настоящая карточка объявлена выдумкой"

    # 2) Критик возвращает JSON, а не гарантию: строка вместо словаря роняла запись
    #    артефакта на последнем шаге — вся работа прогона терялась; перевод строки
    #    в значении дописывал во frontmatter собственное поле.
    assert R.clean_coverage("полно") == {} and R.clean_coverage(["полно"]) == {}, \
        "покрытие не-словарём не отброшено — agent:make упадёт на записи"
    assert R.clean_coverage({"объём": "полно\nsupersedes: чужое"}) \
        == {"объём": "полно supersedes чужое"}, "покрытие подделывает поля frontmatter"

    # 3) Маркер внутри блока кода резал документ: артефакт про саму Аврору потерял бы
    #    всё, что ниже, и молча — в опубликованной странице этого не видно.
    inside = "# AC\n\n```\n    " + MADE_MARK + "\n```\n\nВажный текст.\n"
    assert "Важный текст" in clean_copy(inside), \
        "маркер в блоке кода режет документ — публикация потеряет содержание"
    assert clean_copy("# AC\n\nТекст.\n\n" + MADE_MARK + "\n\n## Уточнения\n").strip() \
        == "# AC\n\nТекст.", "настоящая граница перестала резать"

    pub = (KIT / "scripts/publish_doc.py").read_text(encoding="utf-8")
    ship = (KIT / "scripts/ship_doc.py").read_text(encoding="utf-8")
    assert "clean_copy(md_body(" in pub and "clean_copy(md_body(" in ship, \
        "публикация или выгрузка режут не по маркеру — список заголовков разойдётся"


@test
def test_replacing_a_requirement_asks_what_changed(tmp: Path):
    """Требование не заменить, не сказав что изменилось и что делать с реализованным.

    По старой редакции могли написать код и пройти испытания. Момент замены —
    единственный, когда человек это помнит: через неделю не восстановит, и линтер будет
    ругаться в пустоту. Поэтому отказ здесь, а не жалоба потом.

    У обычной карточки знания этих полей нет: там замена — это уточнение формулировки,
    и требовать миграцию значило бы просить выдумывать.
    """
    root = make_project(tmp)
    (root / "AuroraKnowledgeDB/Requirements").mkdir(parents=True, exist_ok=True)
    for a, b in (("REQ-001", "REQ-002"), ("REQ-002", "REQ-001")):
        (root / f"AuroraKnowledgeDB/Requirements/{a}.md").write_text(
            f'---\ntitle: "{a}"\ntype: requirement\nreq_status: agreed\n'
            f'status: knowledge\nrelated: []\n---\n\nТребование. см. [[{b}]]\n',
            encoding="utf-8")

    cp = run("kb_supersede.py", "REQ-001", "REQ-002", "--apply", cwd=root, expect_rc=2)
    out = cp.stdout + cp.stderr
    assert "--changed" in out and "--migration" in out, "отказ не называет, чего не хватает"
    assert "через неделю" in out, "отказ не объясняет, почему спрашивают именно сейчас"
    assert "панели" in out, "человеку не сказано, где это сделать мышью"
    assert (root / "AuroraKnowledgeDB/Requirements/REQ-001.md").is_file(), \
        "карточка тронута, несмотря на отказ"

    run("kb_supersede.py", "REQ-001", "REQ-002", "--changed", "срок с 10 до 14 дней",
        "--migration", "переделать проверку, тесты перезапустить", "--apply", cwd=root)
    arch = (root / "AuroraKnowledgeDB/_archive/REQ-001.md").read_text(encoding="utf-8")
    assert "Что изменилось: срок с 10 до 14" in arch, "изменение не записано в историю"
    assert "Что делать с реализованным" in arch, "миграция не записана"

    # обычная карточка знания заменяется как раньше: там миграции не бывает
    card(root, "Concepts/Старое.md", "тело см. [[Новое]]", status="knowledge")
    card(root, "Concepts/Новое.md", "тело см. [[Старое]]", status="knowledge")
    run("kb_supersede.py", "Старое", "Новое", "--apply", cwd=root)
    assert (root / "AuroraKnowledgeDB/_archive/Старое.md").is_file(), \
        "обычная карточка перестала заменяться — правило утекло за пределы требований"


@test
def test_making_an_artifact_survives_an_interruption(tmp: Path):
    """Цепочка производства идёт этапами, и обрыв не заставляет начинать сначала.

    Между обогащением и готовым документом стоит человек: планировщик задаёт вопросы и
    ждёт ответов. Значит между вызовами проходят минуты и часы — вкладка закрывается,
    браузер падает, ночь кончается. Состояние живёт в сессии, этапы отмечены и в ней, и в
    шапке документа: по ней видно, докуда дошли, не заглядывая в панель.

    Документ появляется сразу после воркера и лежит со `status: draft`, пока цепочка не
    пройдена. Прятать сделанную работу нельзя — человек хочет её видеть; выдавать
    непроверенное за готовое нельзя тем более — оно уедет заказчику.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Предусловия\n\n## Сценарии\n",
                                          encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "Критерии приёмки"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    for i in range(6):
        card(root, f"Concepts/Заявка-{i}.md",
             f"Заявка проходит статусы. Срок десять дней. см. [[Заявка-{(i + 1) % 6}]]",
             status="knowledge", kind="knowledge")

    seen, rounds = [], {"n": 0}

    def fake(cfg_, role, messages, **kw):
        seen.append(role)
        if role == "planner":
            rounds["n"] += 1
            if rounds["n"] == 1:
                return {"ok": True, "backend": 1, "model": "p", "tps": 9, "log": [],
                        "text": json.dumps({"questions": [{"q": "Какой срок?",
                                                           "why": "меняет критерий",
                                                           "rec": "10 дней"}], "plan": ""},
                                           ensure_ascii=False)}
            return {"ok": True, "backend": 1, "model": "p", "tps": 9, "log": [],
                    "text": json.dumps({"questions": [], "plan": "1. Предусловия"},
                                       ensure_ascii=False)}
        if role == "critic":
            return {"ok": True, "backend": 1, "model": "c", "tps": 9, "log": [],
                    "text": json.dumps({"ok": False, "issues": ["нет раздела «Сценарии»"]},
                                       ensure_ascii=False)}
        if role == "qa":
            return {"ok": True, "backend": 1, "model": "q", "tps": 9, "log": [],
                    "text": "УТВЕРЖДЕНИЕ: срок\nНЕТ ОПОРЫ\n\nВЕРДИКТ: без опоры 1"}
        return {"ok": True, "backend": 1, "model": "w", "tps": 9, "log": [],
                "text": "# AC\n\n## Предусловия\n\nЗаявка создана."}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    # 1) первый вызов доходит до вопросов и останавливается — человек ещё не ответил
    r1 = R.run_make(cfg, str(root), "ac", "статусы заявки и срок", "", "", False, call=fake)
    assert r1["ok"] and r1["stage"] == "planning", f"планировщик не задал вопросов: {r1}"
    assert r1["questions"] and r1["questions"][0].get("rec"), \
        "вопрос без рекомендации — на такой отвечают абзацем вместо слова"
    sid = r1["sid"]
    assert "worker" not in seen, "воркер запущен до того, как человек ответил"

    # 2) ответили — цепочка идёт до конца
    r2 = R.run_make(cfg, str(root), "", "", sid, "10 дней", False, call=fake)
    assert r2["ok"] and r2["stage"] == "done", f"цепочка не завершилась: {r2}"
    st = R.load_session(str(root), sid)
    for stage in R.MAKE_STAGES:
        assert st["stages"].get(stage), f"этап {stage} не отмечен"
    doc = (root / st["path"]).read_text(encoding="utf-8")
    assert "pipeline:" in doc and "checked:" in doc, "в шапке нет контрольных точек"
    assert "status: draft" in doc, \
        "документ с замечаниями критика назван готовым — такой уедет заказчику"
    assert "## Под вопросом" in doc, "утверждения без опоры не выделены"
    assert "## План, по которому собран" in doc, \
        "план исчез: через полгода «почему здесь так» спросят у документа"

    # 3) обрыв: снимаем две последние точки и продолжаем — ранние этапы не повторяются
    st["stages"].pop("checked"); st["stages"].pop("reviewed")
    R.save_session(str(root), sid, st)
    seen.clear()
    R.run_make(cfg, str(root), "", "", sid, "", False, call=fake)
    assert seen == ["critic", "qa"], \
        f"после обрыва переделано лишнее: {seen} — обогащение и план должны быть пропущены"

    # 3.5) Живой прогон показал два дефекта, которых на подставных вызовах не было.
    # Момус нашёл шесть утверждений без опоры, а документ был помечен `status: ready` —
    # потому что «готов» проверял только замечания критика. И модель вернула документ
    # СО СВОЕЙ шапкой (она видит её в шаблоне и честно повторяет), а движок приклеил
    # свою поверх: в файле оказались две шапки, вторую разборщик читает как текст.
    st2 = {"kind": "ac", "idea": "передача квитанций", "sid": "s-live",
           "stages": {k: "2026-08-22" for k in R.MAKE_STAGES},
           "spec": {"title": "AC", "out": "Artifacts/ac", "template": "t"},
           "plan": "1. Раздел",
           "draft": '---\ntitle: "Критерии приёмки для передачи квитанций"\n'
                    'aliases: []\n---\n\n# Документ\n\nТекст.',
           "issues": [], "momus": {"ok": True, "clean": False, "unsupported": 6,
                                   "report": "нет опоры"}}
    live = root / R.write_artifact(str(root), st2)
    text2 = live.read_text(encoding="utf-8")
    sys.path.insert(0, str(KIT / "scripts"))
    from aurora_common import frontmatter as _fm, split_frontmatter as _sf
    head2, rest2 = _sf(text2)
    assert head2 is not None, "шапка артефакта не разбирается"
    assert "status: draft" in head2, \
        "документ с шестью утверждениями без опоры назван готовым — Момус весит не меньше критика"
    assert not rest2.lstrip("\n-").startswith("title:"), \
        "в файле две шапки: вторую разборщик прочитает как текст, а Obsidian покажет мусором"
    assert 'title: "Критерии приёмки для передачи квитанций"' in head2, \
        "заголовок, который написала модель про этот документ, потерян"

    # 4) чужой файл с тем же именем не затирается: человек мог писать его руками, и в
    # git он мог не попасть — тогда потеря безвозвратна. Найдено на живом прогоне.
    mine = root / st["path"]
    theirs = mine.parent / "заявка-и-статусы.md"
    theirs.write_text("# Мой документ\n", encoding="utf-8")
    seen.clear(); rounds["n"] = 1
    r3 = R.run_make(cfg, str(root), "ac", "заявка и статусы", "", "", True, call=fake)
    assert r3["ok"], f"производство упало на занятом имени: {r3}"
    assert theirs.read_text(encoding="utf-8").startswith("# Мой документ"), \
        "рукотворный документ затёрт машинным — потеря без следа"
    made = R.load_session(str(root), r3["sid"])["path"]
    assert made.endswith("-2.md"), f"машина положила документ не рядом, а поверх: {made}"

    # 5) продолжение той же сессии пишет в свой файл, а не плодит новые на каждом этапе
    st3 = R.load_session(str(root), r3["sid"])
    st3["stages"].pop("reviewed", None)
    R.save_session(str(root), r3["sid"], st3)
    R.run_make(cfg, str(root), "", "", r3["sid"], "", True, call=fake)
    files = sorted(p.name for p in (root / "Artifacts/ac").glob("заявка-и-статусы*.md"))
    assert files == ["заявка-и-статусы-2.md", "заявка-и-статусы.md"], \
        f"каждый этап заводит новый файл: {files}"

    # 6) база не знает темы — не выдумываем документ
    bad = R.run_make(cfg, str(root), "ac", "квантовая криптография", "", "", False, call=fake)
    assert not bad["ok"] and "не найдено" in bad["why"], \
        "артефакт собран на пустом контексте — это домысел с шапкой доверия"


@test
def test_artifact_is_a_production_recipe_not_a_template(tmp: Path):
    """У типа артефакта есть всё производство, и записанное читается обратно.

    Реестр знал три поля: название, шаблон, папка. Этого хватает, чтобы положить файл, и
    не хватает ни на что дальше: чем его наполнять (промпт), куда публиковать, какую
    задачу заводить. Всё это жило в голове аналитика и терялось при передаче работы.

    Проверяется круг целиком: панель записала — движок прочитал то же самое. Форма,
    умеющая сохранить поле, которого чтение не знает, — это настройка, пропадающая молча.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    M = importlib.import_module("make_kinds")
    ck = importlib.import_module("aurora_cockpit")

    for f in ("title", "template", "prompt", "out", "publish_url", "mcp"):
        assert f in M.FIELDS, f"поле {f} не описано в движке"
    for f in ("project", "type", "assignee", "labels", "components", "epic"):
        assert f in M.TASK_FIELDS, f"свойство задачи {f} не описано"

    root = tmp / "проект"
    (root / "Templates").mkdir(parents=True)
    (root / "Templates/AC.md").write_text("шаблон", encoding="utf-8")
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: Т\nartifacts:\n  ac:\n    title: "AC"\n'
        '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n', encoding="utf-8")

    written = {"ac": {"title": "Критерии приёмки", "template": "Templates/AC.md",
                      "prompt": "Prompts/AC.md", "out": "Artifacts/ac",
                      "publish_url": "https://conf.example.com/display/PRJ/AC",
                      "mcp": "atlassian",
                      "task": {"project": "PRJ", "type": "Task", "assignee": "@vadim",
                               "labels": "ac, аналитика", "epic": "PRJ-1"}}}
    res = ck.kinds_write(str(root), written)
    assert res.get("ok"), f"реестр не записан: {res}"

    back = M.read_kinds(str(root))["ac"]
    assert back["prompt"] == "Prompts/AC.md", "промпт не дожил до чтения"
    assert back["publish_url"].startswith("https://"), "адрес публикации не дожил"
    assert back["mcp"] == "atlassian", "выбор MCP-сервера не дожил"
    assert back["task"]["assignee"] == "@vadim", "свойства задачи не дожили"
    assert back["task"]["labels"] == ["ac", "аналитика"], \
        f"метки должны читаться списком, а не строкой: {back['task']['labels']}"
    assert back["task"].get("components") in (None, "", []), \
        "пустое свойство записано — пустое значит «ассистент решит», а не «поставь пусто»"

    # папка результата создаётся при сохранении: объявить и не найти — та же ловушка,
    # что и с несуществующим шаблоном, только вскрывается в момент записи артефакта
    assert (root / "Artifacts/ac").is_dir(), "папка результата не создана"

    # и форма показывает ровно те поля, что знает движок
    ui = panel_sources()
    for f in ("prompt", "publish_url", "mcp"):
        assert f'field("{f}"' in ui, f"поля {f} нет в форме — настроить его будет негде"
    assert 'tfield("labels"' in ui and 'tfield("assignee"' in ui, \
        "свойств задачи нет в форме"


@test
def test_kit_and_project_settings_are_separate(tmp: Path):
    """Настройка машины и настройка проекта — разные вкладки, и видно, что откуда.

    Одна форма держала и общее, и частное: корни поиска рядом с доступами проекта, а
    кольцо бэкендов писалось то в кит, то в проект — смотря выбран ли проект. Вопрос
    «почему правка не подействовала на другом проекте» повторялся, и ответить на него по
    виду формы было нельзя.

    Теперь у поля есть происхождение: сервер отдаёт `own` — что задано в самом проекте, —
    и всё остальное помечается «из кита». Без этой пометки человек правит унаследованное
    значение, считая его своим.
    """
    ui = panel_sources()
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")

    assert 'data-view="project"' in ui and 'id="view-project"' in ui, \
        "нет вкладки настроек проекта"
    assert "async function renderProject(" in ui, "вкладку нечем рисовать"
    assert 'if (view==="project") renderProject();' in ui, "вкладка не открывается"

    # реестр артефактов принадлежит проекту и не должен дублироваться в ките
    calls = [l for l in ui.splitlines()
             if "renderKinds(box)" in l and "function renderKinds" not in l]
    assert len(calls) == 1, \
        f"реестр артефактов рисуется {len(calls)} раз(а) — он принадлежит проекту, и место у него одно"

    # происхождение значения приходит с сервера, а не угадывается формой
    assert '"own": sorted(own)' in srv, "сервер не говорит, что задано в самом проекте"
    assert "const inherited = k =>" in ui and '"из кита"' in ui, \
        "форма не помечает унаследованные поля"

    # общая настройка называется общей: подпись пункта меню тоже часть ответа
    assert 'data-i18n="nav.setup">Настройка кита<' in ui, \
        "пункт меню по-прежнему называется «Настройка» — по имени не отличить от проектной"

    # смена проекта перерисовывает вкладку: иначе на ней остаются чужие значения
    assert 'if (S.view === "project") renderProject();' in ui, \
        "после смены проекта настройки остаются от прежнего — правка уйдёт не туда"


@test
def test_the_base_explains_itself_to_a_stranger(tmp: Path):
    """В базе лежит проводник, и он описывает движок, а не то, чем движок был.

    Ассистент, открывший `AuroraKnowledgeDB/` впервые, видит полторы тысячи файлов и не
    знает ни что такое `MOC/`, ни почему у карточки два статуса, ни куда смотреть, чтобы
    найти ответ. Пока это знание жило только в скилле, любой другой харнесс работал с
    базой вслепую — и правил то, что править нельзя.

    Проверяем не наличие файла, а его правдивость: статусы, типы и команды в нём должны
    быть теми же, что в коде. Документ, отставший от движка, хуже отсутствующего — он
    уверенно ведёт не туда.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import aurora_common as A, kb_kind as KIND, kit_commands as C

    guide = (KIT / "templates/meta/READING.md").read_text(encoding="utf-8")
    short = (KIT / "docs/knowledge-rules-tldr.md").read_text(encoding="utf-8")

    for s in A.STATUSES:
        assert f"`{s}`" in guide, f"проводник не называет статус {s}"
        assert f"`{s}`" in short, f"короткая справка не называет статус {s}"
    for k in KIND.KINDS:
        assert f"`{k}`" in guide, f"проводник не называет тип карточки {k}"

    known = {r["cmd"] for r in C.read_registry()}
    for doc, name in ((guide, "проводник"), (short, "короткая справка")):
        for cmd in set(re.findall(r"`((?:kb|ctx|ops|agent|make|ship|sync|kit):[a-z-]+)", doc)):
            assert cmd in known, f"{name} зовёт несуществующую команду {cmd}"

    # поля, которые движок пишет сам, обязаны быть объяснены — иначе читатель их выдумает
    for field in ("kind", "trust", "trust_basis", "built", "distilled", "part_of",
                  "source_hash", "related", "applies_to"):
        assert f"`{field}`" in guide, f"проводник не описывает поле {field}"

    # и главное: чем MOC и _index отличаются от знания
    for must in ("MOC", "_index.md", "status: index", "generated"):
        assert must in guide, f"проводник не объясняет {must}"
    assert "Отсутствие метки" in guide and "писал человек" in guide, \
        "проводник не предупреждает про отсутствие метки — читатель сделает вывод из пустоты"

    # проводник доезжает до проектов вместе с движком
    manifest = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "templates/meta/READING.md" in manifest and "AuroraKnowledgeDB/README.md" in manifest, \
        "проводник не входит в манифест движка — в проектах он не появится и не обновится"
    assert "docs/knowledge-rules-tldr.md" in manifest, "короткая справка не доезжает до проектов"

    # и появляется на свежем проекте
    root = make_project(tmp)
    assert (KIT / "templates/meta/READING.md").is_file()
    src = (KIT / "scripts/install_aurora.py").read_text(encoding="utf-8")
    assert 'AuroraKnowledgeDB/README.md' in src, \
        "установщик не кладёт проводник: новый проект родится без объяснения"


@test
def test_the_engine_does_not_strip_what_it_just_wrote(tmp: Path):
    """Поле `trust` пишет `kb:trust` — снимать его как «наследие» нельзя.

    Имя `trust` было у прежней схемы и значило «уровень доверия, выставленный
    человеком»; вместе с приёмкой поле сняли и внесли в список наследия, который
    вычищают `kb:repair --frontmatter` и `kit:doctor`. С 1.92 то же имя пишет `kb:trust`
    — класс источника, посчитанный по статусам задач. Список остался прежним, и ремонт
    стирал бы то, что пересчёт доверия только что записал: каждый прогон «Починить»
    отменял бы результат «Обновить», и оба при этом отчитывались бы успехом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import aurora_common as A

    assert "trust" not in A.RETIRED_FIELDS, \
        "поле trust снова объявлено наследием — ремонт будет стирать вычисленное доверие"
    for gone in ("audience", "confirmed_by"):
        assert gone in A.RETIRED_FIELDS, f"поле {gone} перестало вычищаться"

    # и на живой карточке: пересчёт доверия и ремонт не воюют друг с другом
    root = make_project(tmp)
    card(root, "Concepts/Понятие.md", "тело см. [[Другое]]", status="knowledge",
         kind="knowledge", trust="trusted", audience="внутренняя")
    card(root, "Concepts/Другое.md", "тело см. [[Понятие]]", status="knowledge",
         kind="knowledge")
    run("kb_fix.py", "--frontmatter", "--apply", "--allow-dirty", cwd=root)
    now = (root / "AuroraKnowledgeDB/Concepts/Понятие.md").read_text(encoding="utf-8")
    assert "trust: trusted" in now, "ремонт стёр вычисленный класс доверия"
    assert "audience:" not in now, "ремонт не убрал настоящее наследие"


@test
def test_quality_review_measures_instead_of_judging(tmp: Path):
    """Ревизия качества приходит с числом, а не с мнением, и укладывается в секунды.

    «Модель считает это пересказом» проверить нельзя; «эти две карточки на 0.94 похожи» —
    можно. Поэтому кандидатов отбирает механика по векторам семантического индекса, а
    решение — слить или развести — остаётся человеку: две стороны одного понятия тоже
    похожи.

    Второе: это шаг внутри «Починить», а не новая команда. Ревизия, ради которой надо
    помнить отдельную кнопку, не делается никогда.

    Третье — скорость. Попарная близость это n²·dim умножений: на живой базе в 1712
    карточек по 1024 измерения вышло **пять минут двадцать секунд**. Матрицей — 0.3 с,
    результат тот же.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import kb_graph as G

    src = (KIT / "scripts/kb_graph.py").read_text(encoding="utf-8")
    assert "def look_alike(" in src, "нет отбора похожих карточек"
    assert "import numpy as np" in src and "except ImportError:" in src, \
        "нет быстрого пути либо нет запасного — на машине без numpy шаг встанет на минуты"
    assert "LOOK_ALIKE" in src, "порог близости не назван и не объясним"
    # находка обязана нести число: без него это мнение
    assert 'f"- **{s}** · [[{x}]] ↔ [[{y}]]"' in src, "пара выводится без близости"
    # раздутость меряется отрывом от медианы этой базы, а не абсолютом
    assert "median * 6" in src, "порог размера абсолютный — у словаря и процессов разная норма"

    # семейство однотипных карточек не должно вытеснять настоящих двойников
    assert "seen.get(x, 0) >= 2" in src, "одна семья карточек забьёт весь список"

    # и это шаг «Починить», а не отдельная команда
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    fix = next(s for s in ck.scenarios() if s["id"] == "fix")
    assert "kb:map" in [st.get("cmd") for st in fix["steps"]], \
        "ревизия качества не входит в «Починить» — значит её не будут делать"


@test
def test_a_changed_source_reaches_the_base(tmp: Path):
    """Страницу поправили — правка доходит до карточки, а прежний тезис остаётся в истории.

    Цепочка была разорвана в двух местах, и обе тихие. `kb:build --reopen` возвращал в
    план только **бесплодные** источники: изменившаяся страница, из которой карточки
    есть, не возвращалась никогда. А если бы и вернулась, `build_plan --card` печатал
    «(уже собрана из этого же источника)» и не делал ничего. Итог: правка в Confluence в
    базу не попадала, и узнать об этом можно было только сверив карточку с источником
    руками.

    Теперь: изменившийся источник возвращается в план по несовпадению отпечатка; разбор
    заменяет в карточке **только** перенесённый текст и снимает `distilled`; `agent:distill`
    видит карточку без отметки, но с прежним тезисом — и пишет новый, а прежний убирает в
    историю карточки вместе с датой, документом и строкой «что изменилось».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    src = root / "Sources/Confluence/Стр.md"
    src.write_text("Возврат за 10 дней. Правило действует для всех заявок.", encoding="utf-8")

    cp = run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md",
             "--paras", "1", "--to", "Concepts", "--apply", cwd=root)
    card = root / "AuroraKnowledgeDB/Concepts/Возврат.md"
    assert card.exists(), f"карточка не собрана:\n{cp.stdout}{cp.stderr}"

    # доводим её до вида «с тезисом и историей», как после agent:distill
    txt = card.read_text(encoding="utf-8")
    txt = txt.replace("# Возврат\n\n",
                      "Возврат занимает десять дней.\n\n## Источник (перенесено дословно)\n\n")
    # `kind` карточке ставит `kb:kind` — в маршруте он идёт следом за разбором
    txt = txt.replace("built: machine",
                      "built: machine\nkind: knowledge\ndistilled: 2026-08-01")
    txt += "\n## История изменений\n\n- 2026-08-01: карточка заведена\n"
    card.write_text(txt, encoding="utf-8")

    src.write_text("Возврат за 14 дней. Правило действует для всех заявок.", encoding="utf-8")
    cp2 = run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md",
              "--paras", "1", "--to", "Concepts", "--apply", cwd=root)
    assert "обновлён источник" in cp2.stdout, \
        f"изменившийся источник снова не дошёл до карточки:\n{cp2.stdout}"
    now = card.read_text(encoding="utf-8")
    assert "14 дней" in now, "перенесённый текст не обновился"
    assert "Возврат занимает десять дней" in now, "тезис затёрт вместе с текстом источника"
    assert "- 2026-08-01: карточка заведена" in now, "история затёрта обновлением источника"
    assert "distilled:" not in now, "отметка о тезисе осталась — distill карточку не возьмёт"

    def fake(cfg_, role, messages, **kw):
        assert "Прежний тезис" in messages[0]["content"], \
            "модель пересобирает тезис, не видя прежнего — сравнить ей не с чем"
        return {"ok": True, "text": "ТЕЗИС:\nВозврат занимает четырнадцать дней.\n\n"
                                    "ИЗМЕНИЛОСЬ:\nСрок вырос с десяти дней до четырнадцати.",
                "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    R.run_distill(cfg, str(root), apply=True, limit=3, momus=False, call=fake)
    done = card.read_text(encoding="utf-8")
    assert "четырнадцать дней" in done.split("## Источник")[0], "тезис не пересобран"
    assert "- 2026-08-01: карточка заведена" in done, "прежняя история потеряна"
    assert "тезис пересобран" in done and "Срок вырос" in done, \
        "в истории нет строки о том, что и почему изменилось"
    assert "прежний тезис" in done and "десять дней" in done.split("## История")[1], \
        "прежний тезис не сохранён — восстановить его больше неоткуда"

    # и наоборот: неизменившийся источник карточку не трогает
    cp3 = run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md",
              "--paras", "1", "--to", "Concepts", "--apply", cwd=root)
    assert "без изменений" in cp3.stdout, cp3.stdout
    again = card.read_text(encoding="utf-8")
    assert again.count("тезис пересобран") == 1, \
        "повторный проход по неизменившемуся источнику дописал историю впустую"
    assert "distilled:" in again, "тезис по неизменному тексту снят — модель перепишет его зря"


@test
def test_five_buttons_instead_of_eleven_routes(tmp: Path):
    """Одиннадцать маршрутов свелись в пять, и каждый отвечает на свой вопрос.

    «Привести базу в порядок», «Обновить базу», «Утренний обход», «Пересчитать доверие»,
    «Прополка», «Разобрать всё» — шесть кнопок, делающих пересекающиеся вещи, и человек
    каждый раз выбирал между ними, не зная разницы. Осталось два вопроса: **взять новое
    из источников** («Обновить») и **привести в порядок то, что есть** («Починить»).
    Граница между ними проверяемая: «Починить» в источники не ходит.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ids = [s["id"] for s in ck.scenarios()]
    assert set(ids) == {"update", "fix", "rebuild", "write", "deliver"}, \
        f"набор маршрутов не тот, что решили: {ids}"
    for gone in ("all", "morning", "trust", "garden", "bulk", "trace", "outward", "assemble"):
        assert gone not in ids, f"старый маршрут {gone} остался: сложность просто спрятана"

    fix = next(s for s in ck.scenarios() if s["id"] == "fix")
    assert not any((st.get("cmd") or "").startswith("sync:") for st in fix["steps"]), \
        "«Починить» ходит в источники — тогда граница между кнопками исчезает"
    upd = next(s for s in ck.scenarios() if s["id"] == "update")
    assert any((st.get("cmd") or "").startswith("sync:") for st in upd["steps"]), \
        "«Обновить» не ходит в источники — тогда она не про новое"

    # цикл: полный оборот по партии, а не фазы по всей базе
    for rid in ("update", "rebuild"):
        s = next(x for x in ck.scenarios() if x["id"] == rid)
        marks = [st.get("cycle") for st in s["steps"] if st.get("cycle")]
        assert marks == ["цикл:", "конец цикла"], f"{rid}: цикл размечен неверно: {marks}"
        inside, seen = [], False
        for st in s["steps"]:
            if st.get("cycle") == "цикл:": seen = True; continue
            if st.get("cycle") == "конец цикла": break
            if seen: inside.append(st.get("cmd"))
        for need in ("agent:build", "kb:kind", "agent:distill", "kb:links"):
            assert need in inside, \
                f"{rid}: в обороте нет {need} — остановка на середине даст заготовки"


@test
def test_dashboards_say_what_they_measured(tmp: Path):
    """Плитка не должна выдавать «не измеряли» за «в порядке».

    Панель показывала «Типы карточек: у всех проставлен» на проекте, где типы вообще не
    считались: движок проекта старее той версии, где типы появились. Пустое значение
    прочиталось как ноль проблем — та же догадка вместо факта, за которую мы уже платили.

    И вторая половина: у плитки должен быть смысл и адрес. Число без действия — это
    сообщение, которое некуда отнести.
    """
    ui = panel_sources()
    assert "function metricCard(" in ui and "function baseCards(" in ui, \
        "нет панели с плитками здоровья базы"
    assert "function sourceCards(" in ui, "нет панели здоровья источников"
    assert "не измерялись: движок проекта старее" in ui, \
        "пустое измерение снова читается как «в порядке»"
    assert "goRoute(" in ui and "goCmd(" in ui, "плитки никуда не ведут"

    # снятые понятия не должны висеть плитками
    for gone in ("протухших verified", "verified без владельца", "kb:queue", "kb:verify"):
        assert gone not in ui, f"панель показывает снятое понятие: {gone}"

    # сервер обязан отдавать то, что панель рисует
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for field in ('"trace"', '"todo"', '"source_health"'):
        assert field in srv, f"панель просит {field}, а сервер его не отдаёт"
    assert "trace-summary.json" in srv, \
        "дашборд читает всю таблицу трассировки — это двадцать мегабайт на открытие"


@test
def test_each_backend_declares_what_it_is_for(tmp: Path):
    """Параллельность — свойство шлюза, а не прогона: у каждого своя пропускная способность.

    Ширина была одна на всё кольцо, и это неверно с обеих сторон: корпоративный шлюз
    держит десяток запросов, домашняя llama.cpp — один. Плюс две разные роли, которые
    раньше путались: бэкенд может держать поток заданий (в пул), может ждать своей
    очереди на случай отказа (запасной), а может и то и другое.

    Первому бэкенду галочки не нужны: он всегда и в пуле, и запасной. Общий
    `AURORA_AGENT_PARALLEL` остаётся потолком — без него сумма ширин подняла бы три
    десятка потоков разом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A

    cfg = A.parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_WIDTH": "4",
        "AURORA_AGENT_BACKEND_2_URL": "u", "AURORA_AGENT_BACKEND_2_MODEL": "m",
        "AURORA_AGENT_BACKEND_2_PARALLEL": "0",
        "AURORA_AGENT_BACKEND_3_URL": "u", "AURORA_AGENT_BACKEND_3_MODEL": "m",
        "AURORA_AGENT_BACKEND_3_FALLBACK": "0", "AURORA_AGENT_BACKEND_3_WIDTH": "2",
        "AURORA_AGENT_BACKEND_2_WIDTH": "1",
        "AURORA_AGENT_PARALLEL": "8"})

    b1, b2, b3 = cfg["backends"]
    assert b1["parallel"] and b1["fallback"], "первый обязан быть и в пуле, и запасным"
    assert not b2["parallel"] and b2["fallback"], "роли второго прочитаны неверно"
    assert b3["parallel"] and not b3["fallback"], "роли третьего прочитаны неверно"

    slots = A.pool(cfg)
    assert slots.count(1) == 4 and slots.count(3) == 2, f"слоты розданы не по ширине: {slots}"
    assert 2 not in slots, "запасной попал в пул — он там не для этого"

    # потолок режет сумму ширин, а не наоборот
    narrow = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                             "AURORA_AGENT_BACKEND_1_WIDTH": "16"})
    assert len(A.pool(narrow)) == 1, "потолок по умолчанию (1) не удержал ширину шлюза"

    # прежние настройки не должны потерять смысл: кто поставил только потолок — получает его
    old_way = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u",
                              "AURORA_AGENT_BACKEND_1_MODEL": "m",
                              "AURORA_AGENT_PARALLEL": "6"})
    assert A.pool(old_way) == [1] * 6, \
        "бэкенд без объявленной ширины перестал делить общий потолок — прежние настройки мертвы"

    # кольцо: своё задание идёт на свой шлюз, подменяют только запасные
    # Без `prefer` — первый и объявленные запасными: `FALLBACK=0` у третьего — запрет подмены,
    # и одиночный вызов его слушает (PRJ-A 22.09: вынос и связывание гнали работу на такой).
    assert [b["n"] for b in A.ring_order(cfg)] == [1, 2], \
        "бэкенд с FALLBACK=0 снова подменяет упавшего в вызове без prefer"
    assert [b["n"] for b in A.ring_order(cfg, 3)] == [3, 1, 2], "вызов не начался со своего шлюза"
    assert [b["n"] for b in A.ring_order(cfg, 1)] == [1, 2], \
        "третий подменяет упавшего, хотя запасным не объявлен"


@test
def test_a_dead_gateway_stops_the_run_but_one_bad_card_does_not(tmp: Path):
    """Одна нечитаемая карточка не роняет ночной прогон; мёртвый шлюз — останавливает.

    Раньше исключение в потоке всплывало из пула и уносило всю партию: одна карточка
    портила четыре часа работы. А отказ шлюза, наоборот, никого не останавливал — восемь
    потоков молотили в мёртвый сервер до конца базы.

    Разница смысловая: сбой на карточке — это карточка, три сбоя подряд — это шлюз.
    """
    import time as _t
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    def base(n):
        root = tmp / f"p{n}"
        kb = root / "AuroraKnowledgeDB/Concepts"
        kb.mkdir(parents=True)
        for i in range(n):
            (kb / f"К{i}.md").write_text(
                f'---\nid: X\ntitle: "К{i}"\nkind: knowledge\nstatus: knowledge\n'
                f'---\n\nтело {i}\n', encoding="utf-8")
        return root

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    seen = {"n": 0}

    def boom(cfg_, role, messages, prefer=0, **kw):
        seen["n"] += 1
        if seen["n"] == 3:
            raise RuntimeError("шлюз выплюнул мусор")
        return {"ok": True, "text": "ТЕЗИС", "backend": 1, "model": "m", "tps": 9, "log": []}

    res = R.run_distill(cfg, str(base(12)), apply=False, limit=12, momus=False, call=boom)
    got = [s["status"] for s in res["steps"]]
    assert len(got) == 12, f"исключение унесло партию: сделано {len(got)} из 12"
    assert got.count("сбой") == 1 and got.count("переписана") == 11, \
        f"сбой посчитан неверно: {got}"

    def dead(cfg_, role, messages, prefer=0, **kw):
        return {"ok": False, "log": ["№1: connection refused"]}

    res2 = R.run_distill(cfg, str(base(30)), apply=False, limit=30, momus=False, call=dead)
    assert len(res2["steps"]) <= R.FAILS_IN_A_ROW, \
        f"мёртвый шлюз не остановил прогон: сделано {len(res2['steps'])} шагов из 30"


@test
def test_route_progress_is_visible_from_any_tab(tmp: Path):
    """Ход маршрута видно отовсюду, а не только в консоли.

    Прогон запускают и уходят на другую вкладку — спросить базу, посмотреть зеркала.
    Ход, привязанный к консоли, в этот момент спрятан, и человек не знает ни где он, ни
    сколько ждать. Полоса живёт в шапке, пустует, когда ничего не идёт, и по клику
    возвращает в консоль.
    """
    ui = panel_sources()
    assert 'id="routeBar"' in ui, "нет полосы хода в шапке"
    assert "function drawRouteBar(" in ui, "полосу нечем обновлять"
    assert ui.count("drawRouteBar(") >= 3, \
        "полоса обновляется не на каждом шаге либо не гаснет в конце"
    assert 'drawRouteBar(null)' in ui, "полоса не гаснет после маршрута"
    assert '$("#routeBar").onclick' in ui, "по полосе нельзя вернуться к прогону"

    # роли бэкендов должны быть видны человеку, а не только в .env
    for field in ("PARALLEL", "FALLBACK", "WIDTH", "CONTEXT"):
        assert f'pre+"{field}"' in ui, f"поле {field} не выведено в панель"
    assert "первый: всегда в параллель и всегда запасной" in ui, \
        "первому бэкенду показывают галочки, которые ничего не решают"
